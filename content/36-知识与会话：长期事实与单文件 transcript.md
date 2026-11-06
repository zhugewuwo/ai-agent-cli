# 第 36 章 知识系统与会话状态

模型每次启动时没有上下文记忆：上次会话学到的项目细节、用户偏好、纠正过的错误，都不会自动延续到下一次。让模型在后续对话中保有这些信息，是 Agent CLI 从工具升级到助手的关键一步。X-Code CLI 把这个问题拆成两条独立的链路：长期事实走知识系统，短期会话状态走 JSONL（JSON Lines，每行一条独立 JSON 记录的文本格式）transcript。知识系统把用户写的项目说明（AGENTS.md）和 AI 学到的经验（auto.md）分开管理，每轮加载进 system prompt。JSONL transcript 以单文件追加的方式记录每轮对话，支持 `/resume` 恢复历史会话，崩溃时最多丢失正在写的那一行。本章将分为如下几个部分进行介绍。

- 36.1 节介绍知识系统的角色分工（用户写 vs AI 写）和完整的文件布局。
- 36.2 节讲解 AGENTS.md 的加载规则（单仓、monorepo、CLAUDE.md 回退）以及 `/init` 命令的工作方式。
- 36.3 节讨论 auto-memory 的写入机制：后台抽取器、zod schema 约束和安全边界。
- 36.4 节介绍 auto-memory 的存储格式、冲突检测、TTL 策略和用户可见性。
- 36.5 节给出 5 层知识源注入 system prompt 的完整顺序。
- 36.6 节讲解 JSONL transcript 的条目格式、写入路径和 `/resume` 会话恢复机制。
- 36.7 节给出本章小结。

## 36.1 角色分工与文件布局

知识有两类来源：用户主动写入的项目说明和偏好，以及 AI 在对话中学到的事实。这两类信息被严格分到不同文件，避免混淆。

<!-- check:ignore=punctuation -->

| 角色     | 写入者       | 目的                            | 文件                        |
| -------- | ------------ | ------------------------------- | --------------------------- |
| 项目说明 | 用户（团队）   | 项目是什么、团队约定、业务背景  | `AGENTS.md`（项目根）       |
| 用户偏好 | 用户（用户）   | 跨项目的个人偏好                | `~/.x-code/AGENTS.md`       |
| 本地覆盖 | 用户（用户）   | 不提交到 git 的个人项目偏好     | `AGENTS.local.md`（项目根） |
| 项目记忆 | AI           | AI 在对话中学到的项目相关事实   | `.x-code/memory/auto.md`    |
| 用户记忆 | AI           | AI 学到的用户相关事实（跨项目） | `~/.x-code/memory/auto.md`  |

`AGENTS.md` 放在项目根目录而不在隐藏目录，这是行业已经形成的供应商约定。好处在于：用户如果已经为其他 CLI 写过同名文件，安装 X-Code CLI 后立刻能用，零迁移成本。完整的目录结构如下：

<!-- check:ignore=code-block-size -->

```text
项目根/
├── AGENTS.md                       ← 项目说明（用户写，git 追踪）
├── AGENTS.local.md                 ← 个人项目偏好（用户写，gitignored）
└── .x-code/                        ← CLI 内部状态（不建议手工编辑）
    ├── memory/
    │   └── auto.md                 ← AI 自动写入的项目记忆
    ├── sessions/
    │   └── <slug>-<sessionId>.jsonl   ← 每个会话一个 jsonl，append-only
    └── local/
        ├── .gitignore              ← 内容是 `*`，整个 local/ 不提交
        └── permissions.json        ← 用户已批准的 shell-prefix 规则

~/.x-code/
├── AGENTS.md                       ← 用户偏好（用户写）
├── config.json                     ← /model 与 /thinking 持久化
├── memory/
│   └── auto.md                     ← 用户自动记忆
├── tessdata/                       ← tesseract OCR 权重（约 7.6 MB）
└── logs/
    └── debug.log                   ← DEBUG_STDOUT=1 启用，10 MB 滚动
```

`~/.x-code/` 目录在 Windows 上对应为 `C:\Users\<user>\.x-code\`（Node 的 `os.homedir()` 返回 `USERPROFILE`）。本章所有 `~/.x-code/...` 路径在 Windows 资源管理器里都按这条规则换算。API Key 走环境变量（`ANTHROPIC_API_KEY` / `OPENAI_API_KEY` 等），`config.json` 只存 UX 偏好，不存放密钥。

## 36.2 AGENTS.md 加载链与 /init 命令

单仓库项目直接读项目根目录的 `AGENTS.md`，不存在则跳过。monorepo 项目的加载规则更复杂：从当前工作目录向上遍历到 `.git` 目录（含）或文件系统根目录，沿路径每一层尝试读取知识文件，按从根节点到叶子节点的顺序拼接到 system prompt。

> 源码：`packages/core/src/knowledge/loader.ts::collectProjectKnowledgeChain`

<!-- check:ignore=punctuation -->

```ts
async function collectProjectKnowledgeChain(
  startDir: string,
): Promise<Array<{ dir: string; fileName: string; content: string }>> {
  // 从 startDir 一路向上，每个目录调一次 readKnowledgeFile(dir):
  // 找 AGENTS.md 优先，缺失才回退 CLAUDE.md。
  // 遇到 .git 或文件系统根停止；返回按从根到叶排列的列表。
}
```

在 monorepo 结构中，子包目录的 AGENTS.md 拼接在仓库根目录的 AGENTS.md 之后。由于模型对 system prompt 中靠后的内容赋予更高注意力，当子包与根目录的约定冲突时，子包的内容会覆盖根目录。例如在 `packages/frontend/` 下启动时，加载顺序是：

```text
### Project AGENTS.md (.)                  ← 仓库根目录（通用约定）
### Project AGENTS.md (packages/frontend)  ← 子包目录（前端特有约定）
```

根目录的 AGENTS.md 可以写「本仓库用 pnpm + TypeScript」，子包目录的 AGENTS.md 可以覆写「本包用 Vitest，其他包用 Jest」，两条同时生效，子包内容更贴近当前任务。

每个目录（用户级 `~/.x-code/` 和项目链上每一层）按以下顺序匹配第一个存在的文件：先找 `AGENTS.md`，找不到才回退 `CLAUDE.md`。实现集中在 `loader.ts` 的 `KNOWLEDGE_FILENAMES` 数组，顺序就是优先级。`readKnowledgeFile(dir)` 辅助函数遍历数组，第一个存在就返回，后面跳过。

<!-- check:ignore=punctuation -->

| 同目录情况       | 加载结果                               |
| ---------------- | -------------------------------------- |
| 只有 `AGENTS.md` | 加载 AGENTS.md                         |
| 只有 `CLAUDE.md` | 加载 CLAUDE.md（回退）                 |
| 两个都有         | 只加载 `AGENTS.md`，`CLAUDE.md` 会被忽略 |
| 两个都没有       | 跳过                                   |

注入 system prompt 时段落标题用真实文件名（`### Project AGENTS.md (.)` 或 `### Project CLAUDE.md (.)`），模型能从标题判断这条来自哪个文件。这个回退机制的迁移路径是：已有 `CLAUDE.md` 的用户安装 X-Code CLI 后不需要做任何迁移，CLI 直接读取；之后用户运行 `/init` 会让模型分析代码库后写一份 AGENTS.md（已存在则增量合并），回退不再触发，`CLAUDE.md` 会被忽略。想清理就删除 `CLAUDE.md`，不想清理也无害，因为 AGENTS.md 总是优先。写入永远只往 AGENTS.md 文件写，`/init` 不会修改用户已有的 `CLAUDE.md`。 <!-- check:ignore-line -->

`/init` 命令会把一段 prompt 静默发给主 agent，由模型自己用工具完成实际工作。如果 AGENTS.md 不存在，模型扫描代码库的项目配置文件（如 `package.json`、`tsconfig.json` 等）、README、以及其他 AI 工具的遗留规则文件（如 `.cursor/rules`、`.github/copilot-instructions.md` 等），分析整理后创建 AGENTS.md。如果 AGENTS.md 已存在，模型读取现有内容后增量合并改进，保留用户手写的部分，而不是整个文件覆盖。生成过程中有几条硬约束引导模型精简内容：对每一条规则判断「如果不写进去，模型会不会犯错」，不会就不写——AGENTS.md 每轮都加载进 system prompt，即使有提示词缓存（详见 Ch.33），每行都会占用上下文窗口的容量，内容越多留给对话本身的空间就越少；不把项目配置文件里已经写明的信息（如「本项目用 React 18」）重复写进 AGENTS.md，因为模型在需要时可以直接读取 `package.json` 等源文件获得这些信息；个人偏好不写进 AGENTS.md 而是留给 AGENTS.local.md，因为前者是团队共享文件，个人习惯不应强加给团队其他成员。

> 源码：`packages/cli/src/ui/components/App.tsx`

<!-- check:ignore=punctuation -->

```ts
case 'init':
  echoCommand(text)                                // 显示 "/init"
  await submit(INIT_PROMPT, { silent: true })      // prompt 走模型，不进 UI
  return
```

`silent: true` 选项的效果是让用户视角和模型视角分开：UI 端不把这条 user 消息追加到终端历史区（用户屏幕上只看到自己输入的 `/init` 那一行，看不到 INIT_PROMPT 的完整内容），但 `agentLoop` 内部的 `loopState.messages` 照常推入（模型完整读到 INIT_PROMPT）。这套思路可以复用给其他作者侧 prompt 命令（`/security-review`、`/review` 等）。

## 36.3 auto-memory 写入机制

> AI 知识背景：LLM 长期记忆的三条主流路线
>
> 「让 LLM 记住跨会话的事实」是 agent 系统的核心难题，行业有三条主流路线。（1）RAG（检索增强生成，[Lewis et al. 2020](https://arxiv.org/abs/2005.11401)）：把事实存进向量数据库，每轮对话前先做向量检索拿回相关片段拼进 prompt。优点是信息容量无限、可解释，缺点是召回质量取决于 embedding 质量且检索开销不小。（2）超长上下文：直接把所有历史和文档放进 200k / 1M 窗口。优点是实现简单，缺点是计费高昂（按 input token 计费）且存在 lost-in-the-middle 问题（详见 Ch.31）。（3）显式 memory 文件：AI 把学到的事实写成结构化文件（markdown / json），每轮把这些文件的内容拼入 system prompt 的开头部分，作为模型每次对话都能看到的固定上下文。优点是可读、可手工编辑、缓存命中率高，缺点是写入策略需要设计。X-Code CLI 选择第三条路线，这一节讲写入策略的实现。

记忆的写入路径只有一条：主对话正常结束后，由后台抽取器（extractor）自动执行一次抽取。主 agent 自始至终不持有任何记忆写入工具，这是有意的设计——让主对话只关心「做事」，把「这条对话要不要记下来」剥离成独立的后置步骤，避免主 agent 把宝贵的上下文消耗在元认知判断上。主循环在正常退出时，以异步不等待（fire-and-forget）的方式触发抽取——发出调用后不阻塞、不处理结果、出错也不传播回来，主流程立刻返回。

> 源码：`packages/core/src/agent/memory-extractor.ts`

抽取器不是再执行一遍完整的 agent 循环，而是单次结构化输出调用。通过 zod schema 把模型输出强制约束成 JSON 数组，每条结果直接写入对应的记忆文件：

<!-- check:ignore=punctuation -->

```ts
const MemorySchema = z.object({
  memories: z
    .array(
      z.object({
        category: z.enum(['user', 'feedback', 'project', 'reference']),
        scope: z.enum(['project', 'user']),
        key: z.string().min(1),
        fact: z.string().min(1),
      }),
    )
    .max(MAX_MEMORIES_PER_PASS), // 3
})
```

四类分类法按「知识类型」分而不是按「主题」分，四类互斥，边界清晰：

<!-- check:ignore=punctuation -->

| 类别        | 含义                                               | 典型触发                                             |
| ----------- | -------------------------------------------------- | ---------------------------------------------------- |
| `user`      | 关于用户本身（角色、专长、长期约束）               | 「我是十年 Go 工程师，第一次碰 React」               |
| `feedback`  | 用户的纠正或认可（都要含原因）                     | 「不要 mock 数据库，上季度 mock 过测试通过但迁移失败」|
| `project`   | 进行中的工作、决策、非代码可推导的状态             | 「mobile release 冻结从 2026-03-05 开始」            |
| `reference` | 指向外部资源的「去哪查」信息（看板、文档、仪表盘） | 「pipeline bug 都在 Linear 的 INGEST 项目」          |

为避免记忆膨胀和误导，以下类型的事实不进 auto-memory：代码里能看到的事实（「这个项目用 React」，AI 读 `package.json` 就知道）；Git history 里有的（commit message、作者、时间）；AGENTS.md 已经说过的；一次性调试解决方案。

抽取器有多层安全边界。首先，模块级锁保证同一时刻只有一个抽取任务在运行——例如用户在终端开了两个标签页分别进行对话，两个对话几乎同时结束时，第二个抽取任务会等第一个完成后再执行，避免并发写入导致记忆文件损坏。其次，对话消息不足 4 条时跳过抽取（单轮问候不值得记忆），单次抽取最多写入 3 条记忆，防止过度写入。用户按 Esc 中断主对话时，正在运行的抽取器也会被级联取消。错误退出、中断退出、内容过滤、长度截断等异常路径不触发抽取，因为失败轮次没有可抽取的内容。抽取 prompt 中加了反幻觉硬规则：不许推断或归纳用户未说过的事实（如「用户对英文理解有限」），不许编造项目规则（如「代码注释保持英文」）。prompt 还会注入当前已有记忆的快照，要求模型在写入前先判断是否已存在等价条目——已存在就返回空，需要更新就复用已有的 key 触发覆盖写入。代价是每次抽取多消耗几百个 prompt token。

触发条件如下：

> 源码：`packages/core/src/agent/loop.ts`

<!-- check:ignore=punctuation -->

```ts
if (completedNormally && !options.abortSignal?.aborted) {
  void runMemoryExtractor({
    parentState: state,
    parentModel: model,
    abortSignal: options.abortSignal,
    onWrite: callbacks.onMemoryWrite,
  })
}
```

`completedNormally` 只在 `outcome.finishReason === 'stop'` 分支才置为 true，确保只有「模型完整说完话」这种健康路径才会触发 memory 抽取。

## 36.4 auto-memory 存储与可见性

记忆文件是 markdown 格式，便于用户直接审阅和编辑：

```markdown
## Auto Memory

### feedback

- [2026-04-18] testing-db-policy: 集成测试必须打真库，不能 mock。原因：Q1 migration 事故
- [2026-04-18] refactor-batching: 大规模重构倾向打成一个 PR——用户验证过减少 churn

### user

- [2026-04-18] user-stack: 十年 Go 工程师，React 新手，前端例子可类比后端概念

### project

- [2026-04-18] release-freeze: mobile release 冻结 2026-03-05 起，非 critical PR 要 flag
```

每条记录前缀的日期是写入时间，TTL 计算的依据。`AutoMemory.add()` 写入时先检查有没有同 `category` 且同 `key` 的旧条目，有就直接用新值覆盖，没有再追加。这就是「字面去重」：只要拼出来的 `(category, key)` 字符串一模一样就算同一条。同时 `sanitizeLine` 把多行和空白折叠成一行，保证一条 fact 永远只占一行。字面去重有一个局限：`role`、`user-stack`、`user-profile` 在 `add()` 看来是三个不同的 key，模型如果换着 key 写同一事实，三条会全部保留。要解决这个问题，靠的是 36.3 节提到的 prompt 注入当前 memory 快照加第 5 条硬规则，让模型自己判断是否已有等价条目。

auto-memory 有两个容量控制。TTL 方面，90 天未更新的条目在启动时自动清除（`AutoMemory.evict(90)`）。大小方面，注入 system prompt 时只取前 200 行（`MAX_LOAD_LINES`），防止记忆膨胀拖累每轮 prompt 的 token 开销。

`/memory` 命令把当前项目加用户级 memory 按 category 分组列出。这个命令只展示，不带删改子命令。原因是文件本身就是普通 markdown，要修改直接编辑 `~/.x-code/memory/auto.md`（用户级）或 `<repo>/.x-code/memory/auto.md`（项目）即可。启动时 `parseMemoryFile` 会重新加载，只要保留 `### <category>` 子标题加 `- [日期] key: fact` 的行格式即可。不内置增删改查子命令是有意的：CLI 内做选择器删除会增加 UI 复杂度，而文件编辑器在这种小规模手工编辑场景下始终更方便。

抽取器默认在后台静默写入记忆文件，终端界面保持干净，主 agent 全程不被打断。代价是用户对「AI 刚记住了什么」零感知，需要主动去查看记忆文件才知道。由于抽取是异步完成的（主对话已经结束、界面已经空闲），做不出「用户先看再批准」的同步确认对话框，但一条事后通知仍然是有价值的。抽取器每次成功写入一条记忆后，通过回调在终端历史区追加一行通知：

```text
⎿  Remembered (user · user) `language-cn`: User wants replies in Chinese.
```

由于抽取是异步完成的，这行可能在用户已经开始输入下一条时才出现。它插在终端历史区（输入框上方），不打断输入。这是一个简单但足够清晰的「AI 在做事」的信号：异步通知比同步对话框更克制，比完全静默更透明。

## 36.5 知识加载的完整顺序

`buildKnowledgeContext()` 按下面顺序拼接，后出现的在 prompt 末尾，对模型权重更高：

```text
1. User Preferences   (~/.x-code/AGENTS.md)         用户写，跨项目
2. User Auto Memory                                  AI 写，跨项目
3. Project AGENTS.md (.)                             用户写，项目根目录
4. Project AGENTS.md (packages/x)                    用户写，monorepo 子包（如有）
5. Project Auto Memory                               AI 写，本项目
6. Local Preferences                                 用户写，gitignored
```

以上六层按顺序拼接后，整段作为 system prompt 末尾的知识段注入。由于模型对 prompt 末尾的内容赋予更高权重，排在后面的来源在语义上优先：项目级覆盖用户级，monorepo 子包覆盖仓库根目录，本地偏好覆盖项目级。


## 36.6 JSONL transcript 与会话恢复

知识系统记的是长期事实，会话状态记的是短期工作上下文（正在做什么、做到哪了）。X-Code CLI 用单文件 JSONL transcript 记录会话状态，写入方式是只追加不修改：新事件追加到文件尾部，已写入的内容不会被回头改动。每条轮次、工具调用和系统事件各占一行 JSON。这套设计在两个场景下都可靠——程序崩溃时最多丢失正在写的那一行，前面的内容完整可读；用户关闭终端后重新打开，通过 `/resume` 恢复时能拿到完整的对话时间线，不需要依赖压缩后的摘要。

每个会话对应一个独立文件，文件名由首条用户消息的摘要加会话 ID 组成（纯中文首句无 ASCII 时回退为时间戳）。每行一条条目，共 6 种类型（其中 checkpoint 用于 `/rewind` 回退点，本章不展开）：

<!-- check:ignore=punctuation -->
<!-- check:ignore=code-block-size -->

```jsonc
// kind: header ── 会话启动写一次（resume 同名文件已存在时跳过）
{"t":"meta","kind":"header","cwd":"D:\\res\\proj","gitBranch":"main","modelId":"anthropic:claude-sonnet-4-5","startedAt":"2026-04-30T10:12:33.001Z","firstPrompt":"升级到 React 19...","taskSlug":"upgrade-react-19","sessionId":"20260430-101233-001"}

// t: msg ── 一条 ModelMessage（user / assistant / tool）+ 时间戳
{"t":"msg","message":{"role":"user","content":"..."},"ts":"2026-04-30T10:12:34Z"}
{"t":"msg","message":{"role":"assistant","content":[{"type":"tool-call","toolCallId":"toolu_01","toolName":"shell","input":{"command":"ls"}}]},"ts":"..."}

// kind: usage ── 每轮一条 token usage 快照
{"t":"meta","kind":"usage","usage":{"inputTokens":1234,"outputTokens":567,"totalTokens":1801,"cacheReadTokens":890,"cacheCreationTokens":0,"currentContextTokens":1801},"modelId":"anthropic:claude-sonnet-4-5","ts":"..."}

// kind: compact-boundary ── 压缩点；深度压缩带 summary，轻量不带
{"t":"meta","kind":"compact-boundary","summary":"前面讨论了 React 19 升级的兼容性...","ts":"..."}

// kind: interrupted ── Ctrl+C 中断标记，选择器显示「上次中断」提示
{"t":"meta","kind":"interrupted","ts":"..."}
```

以上五种加上 `CheckpointJsonlEntry`（`kind:"checkpoint"`，用于 `/rewind` 回退点记录），共六种条目都定义在 `agent/session-store.ts`。

transcript 的写入不是集中在一个地方完成的。5 个写入函数分别挂在 agent 生命周期的 5 个时间点，各管各的事件类型。所有写入都是异步不等待的：每个函数内部执行一次文件追加，调用方不阻塞、出错被忽略，不影响 agent 主循环。

<!-- check:ignore=punctuation -->

| 函数                     | 写什么                  | 调用时机                                                          |
| ------------------------ | ----------------------- | ----------------------------------------------------------------- |
| `appendHeader`           | 一条 `meta.header`      | 会话启动、首轮之前。resume 同名文件已存在时跳过                   |
| `flushPendingMessages`   | 一批 `t:msg`            | 每轮开头、每轮结束、`saveSession` 收尾；只写入尚未持久化的增量        |
| `appendUsage`            | 一条 `meta.usage`       | 每轮 `collectTurnResponse` 末尾                                   |
| `markBoundaryAndReflush` | `meta.compact-boundary` | 压缩成功后；恢复时 loader 取最后一个 boundary 之后的内容          |
| `appendInterrupted`      | 一条 `meta.interrupted` | 用户按 Ctrl+C / Esc 中断当前轮时                                  |

`/resume` 弹出 SelectOptions 模态（复用 Ch.28 的挂起等待模式），按 mtime 倒序展示这个项目下所有历史会话：

<!-- check:ignore=code-block-size -->

```text
Pick a session to resume (352 total in this project):

 ❯ 1. 查询一下广东的天气和合适的活动建议  ·  5h ago     deepseek:deepseek-v4-flash  ·  39,323 tokens  ·  20260514-093041-257
   2. hello  ·  5h ago                                  deepseek:deepseek-v4-flash  · 151,478 tokens  ·  20260514-092826-609
   3. hello  ·  15h ago                                 deepseek:deepseek-v4-flash  ·  27,343 tokens  ·  20260513-234558-862
   4. 快速的读取一下各个 readme 文件  ·  17h ago          deepseek:deepseek-v4-flash  ·  42,540 tokens  ·  20260513-220014-635
   ...
 ↓ 18 more below

Enter to select  ·  ↑/↓ to navigate  ·  Esc to cancel
```

选择器列表的生成不依赖独立的索引文件。每次打开选择器时，程序扫描会话目录下所有 `.jsonl` 文件，对每个文件只读头部（取会话标题、使用的模型、首句 prompt 等元信息）和尾部（取最后一条 token 用量记录），按修改时间倒序排列。jsonl 文件本身的头尾就是它的元数据，不需要额外维护一份索引，因此选择器在数百个历史会话下依然秒开。这是 transcript 设计成「启动信息写一次在文件头、token 用量每轮追加一条在文件尾」的直接收益。

用户选中一个历史会话后，恢复过程分五步。（1）从头到尾顺序扫描 jsonl，启动信息行提取元数据，每条消息行推入消息数组。（2）遇到压缩边界行时把消息数组清零——因为压缩发生时，系统已经把修剪后的消息重新追加到了边界行之后，所以只保留最后一个压缩边界之后的内容就等同于还原压缩后的状态。（3）token 用量取最后一条。（4）中断标记仅供选择器显示「上次中断」提示，不影响状态重建。（5）剪掉尾部孤立的工具调用（没有对应的工具返回结果），因为供应商严格要求工具调用与返回结果配对，恢复时若停在工具执行中途必须修剪到上一个完整边界。启动参数 `xc -c` / `xc --resume <会话ID>` 走同一条恢复路径，只是跳过了选择器步骤。

每轮对话结束后会追加一行 token 用量记录，包含输入 token 数、输出 token 数、总量、缓存读取量、缓存创建量和当前上下文 token 数共六个字段。`/usage` 命令直接读取内存中的累计值渲染当前会话的 token 用量，`/usage-history` 命令则扫描本项目下所有 jsonl 文件、每个只读尾部取最后一条用量记录渲染成历史列表。两条命令共用同一份数据源，没有独立的 usage 文件。

## 36.7 小结

本章讨论了 X-Code CLI 的知识系统与会话状态。用户写的知识（AGENTS.md、AGENTS.local.md）和 AI 写的记忆（auto.md）严格分开，主 agent 全程不持有记忆写入工具。AGENTS.md 加载链支持 monorepo（从根到叶，子包覆盖根），每个目录优先读 AGENTS.md、缺失才回退 CLAUDE.md（只读兼容，写入永远只往 AGENTS.md）。`/init` 命令把作者侧 prompt 静默发给模型，由模型自行创建或增量合并 AGENTS.md。auto-memory 由主对话正常结束后的后台抽取器独占写入，采用单次结构化输出加 zod schema 强制四类分类法（user / feedback / project / reference），单次最多 3 条、90 天自动清除、注入上限 200 行。写入完成后在终端历史区追加一行通知，提供可见性。6 层知识源按用户偏好、用户记忆、项目 AGENTS.md 链、项目记忆、本地偏好的顺序拼接到 system prompt 末尾，后出现的权重更高。JSONL transcript 是单文件只追加的 6 种条目，崩溃只丢正在写的那一行，`/resume` 取最后一个压缩边界之后的内容等同于内存压缩态，会话列表不依赖索引文件而是直接扫目录读头尾。
