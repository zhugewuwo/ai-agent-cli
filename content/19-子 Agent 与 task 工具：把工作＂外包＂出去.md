# 第 19 章 子 Agent 与 task 工具

Agent loop 的运转到这里已经讲透：模型生成回复，X-Code CLI 执行工具调用，结果回填到消息历史，模型继续推理。但有一类任务和这个循环格格不入——它们的特征是输入很多、产出很短。例如「审查这个 PR 里有没有安全问题」，需要读 git diff 全部改动、逐个文件查看源码、遍历调用方……最终只输出一份缺陷清单。这类任务如果在主 loop 里直接执行，主对话会被大量探索性中间产物填满——那些 diff 文本和源码片段一旦进入主 `LoopState.messages`，每轮请求都要重新发送一遍，但下一步基本用不上。

`task` 工具是 X-Code CLI 给出的解法：把这段任务委派给一个独立的子 Agent（Sub-Agent），子 Agent 自己运行一个嵌套的 agent loop，探索结束后只把一份结论递回主 Agent。所有中间的 `readFile` / `grep` / `shell` 输出都限制在子 loop 内部，不进入主对话历史。这种「输入很多 → 产出很短」的结构性隔离，正是 `task` 工具最核心的价值。

本章先沿着一个完整场景走一遍子 Agent 的启动与回收过程，再逐一讨论支撑这套机制运行的几个关键设计选择，分为如下几个部分进行介绍。

- 19.1 节用审查 auth 模块的完整例子展示子 Agent 的启动与回收过程。
- 19.2 节解释为什么子 Agent 必须从零起步，不继承父 Agent 的消息历史。
- 19.3 节讲解输出契约：最终消息即父 Agent 看到的全部内容。
- 19.4 节列出四个内置 Agent 的边界划分。
- 19.5 节讨论递归硬禁：子 Agent 不能再启动子 Agent。
- 19.6 节介绍连续 task 调用的并发执行机制。
- 19.7 节展示如何用一个 Markdown 文件定义自定义 Agent。
- 19.8 节讲解取消级联与权限继承。
- 19.9 节给出本章小结。


![task.gif](https://p9-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/12420b2d1d3940c88b344d3765dca09d~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1327&h=536&s=317187&e=gif&f=118&b=282922)

## 19.1 一个完整的例子：审查 auth 模块

假设用户输入：「我刚改了 auth 模块，帮忙看看有没有 bug 或安全问题」。模型判断这是典型的「读很多、产出很短」的任务，于是调用 `task` 工具：

```jsonc
{
  "description": "审查 auth 模块",
  "subagent_type": "code-reviewer",
  "prompt": "审查 src/auth/ 下的待提交改动。重点：1) src/auth/jwt.ts 里的 token 签发与验证；2) src/auth/session.ts 的会话管理；3) src/routes/login.ts 登录端点。输出一份按文件分组的缺陷清单，每条标 critical/warning/nit + file:line + 一句话描述。"
}
```

X-Code CLI 收到这个 tool call 后，为 `code-reviewer` 类型的子 Agent 新建一个独立的 `agentLoop`。子 Agent 在自己的世界里展开工作：

    子 Agent 第 1 轮：shell "git diff" → 看到 4 个文件的改动
    子 Agent 第 2 轮：readFile src/auth/jwt.ts → 200 行源码进入子 Agent 的消息历史
    子 Agent 第 3 轮：readFile src/auth/session.ts → 又 150 行进入子 Agent 的历史
    子 Agent 第 4 轮：grep "verify" + readFile src/routes/login.ts → 又一批
    子 Agent 第 5 轮：综合分析，生成最终回复

整整 5 轮、读了约 600 行代码——但主 Agent 对这些中间过程一无所知。子 Agent 完成后，`runner.ts` 里的 `extractFinalText` 从子 Agent 的消息历史中取出最后一条 assistant 消息，包上 `<task_result>` 标签，作为 `task` 工具的结果回填到主对话：

    <task_result>
    src/auth/jwt.ts:
      - critical: line 42 — JWT secret 直接读环境变量未校验，启动时若未设置会签发空 secret
      - warning: line 58 — verify() 返回的 payload 没做 issuer / audience 校验
      ...
    </task_result>
    <task_stats tool_calls="6" tokens="3420" duration_ms="8200" />

主 Agent 只看到这份约 30 行的清单和一行统计。子 Agent 看过的 600 行源码和 git diff 文本，永远不会进入主对话历史。下面的序列图展示了这个过程的完整生命周期——注意虚线框内的所有交互都发生在子 Agent 的独立 `LoopState` 中，不进入主对话：


![image.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/5ff7d0149dbd420a96ff66ca82cf5a8d~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1147&h=838&s=70607&e=png&b=fffefe)

*图 19-1 task 工具的完整生命周期*

接下来逐一讨论支撑这套机制的几个设计选择。

## 19.2 为什么子 Agent 必须从零起步

子 Agent 运行在独立的嵌套 `agentLoop` 里——它带来的隔离边界正是 `task` 工具能节省 token 的根本原因。

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/runner.ts

// 创建全新空白 LoopState
const subState = createLoopState('default')
// 装上子 Agent 的系统提示词
subState.systemPromptCache = subSystemPrompt

const subOptions: AgentOptions = {
  ...parentOptions,
  maxTurns: agentDef.maxTurns,     // 子 Agent 自己的轮次预算
  toolFilter,                       // 工具过滤器（下面展开）
  abortSignal: parentOptions.abortSignal, // 共享父的取消信号
  permissionMode: 'default',        // 子 Agent 永远从 default 起步
  subAgentRegistry: undefined,      // 禁止再启动子 Agent
}

const { state: finalSubState, turnCount } =
  await agentLoop(prompt, subModel, subOptions, subCallbacks, subState)
```

`createLoopState('default')` 拿到的是一份全新的、空的 `LoopState`：`messages` 是空数组、`recentToolCalls`（循环护栏窗口，详见 Ch.32）是空的、`todos` 也是空的。子 Agent 唯一的输入来源是父 Agent 通过 `task` 的 `prompt` 字段传入的那段任务说明，此外没有任何上下文。这种设计选择的原因在于，如果让子 Agent 共享父的消息历史，父对话此刻可能已经累积几十轮、十几万 token，子 Agent 的每一次内部 LLM 请求都要把这十几万 token 重新发送一遍，既慢又贵，而其中大部分内容与当前子任务毫无关系。

从零起步把父→子的接口收窄到仅一段 prompt 字符串。这意味着父 Agent 必须把背景信息、文件路径、期望的输出格式全部写清楚——`task` 工具的描述里反复强调这一点：「Brief the agent like a smart colleague who just walked into the room — it hasn't seen this conversation」。而子→父的接口则仅有最后一条 assistant 消息——子 Agent 的所有中间 tool call 和 tool result 都保留在 `subState.messages` 里，不会回流到父对话。

token 用量是唯一跨越这道边界回流的数据。子 Agent 完成后，`runner.ts` 把子 Agent 的 `tokenUsage` 累加到父的 `parentState.tokenUsage`：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/runner.ts

parentState.tokenUsage.inputTokens += finalSubState.tokenUsage.inputTokens
parentState.tokenUsage.outputTokens += finalSubState.tokenUsage.outputTokens
// ...
callbacks.onUsageUpdate(parentState.tokenUsage)
```

这样累加的目的不是为了主对话底部的状态栏——状态栏显示的「当前 context 占用 / 上限」是从最新一次主 Agent API 响应里直接读出的快照值（`currentContextTokens`），子 Agent 不会影响它。但 `/usage` 命令、JSONL 会话日志里「本次会话累计消耗了多少 token」这种累计型统计就不一样了——必须把子 Agent 的成本计入，否则总量永远对不上。

子 Agent 的轮次计数（`turnCount`）和父的互相独立。`turnCount` 是 `agentLoop` 函数内部的局部变量，每次调用从 0 开始，子 Agent 有自己的硬编码 `maxTurns`（内置 Agent 分别是 25 / 40 / 30 / 25，对应下一节表中的四行），超出后被强制收尾。主 Agent 在交互模式下默认不传 `maxTurns`——没有数字上限，由用户按 Esc 终止。子 Agent 设硬上限是有意为之：它被启动来完成一段有边界的子任务，无人值守，必须有上限防止失控；主交互 loop 则相反——有人值守，按 Esc 比任何数字上限都更灵敏可靠。

## 19.3 输出契约：最终消息即父 Agent 看到的全部

上一节说子→父只通过「最后一条 assistant 消息」传值。子 Agent 不能在最终消息里写「详见文件 X」或「如前所述」，必须把所有相关细节内联进去。

反例最清楚。假设子 Agent（`explore` 类型）的最终消息是这样：

    我看了 src/auth/jwt.ts 和 src/auth/session.ts，
    具体的 JWT 签名实现见 jwt.ts 的 signToken 函数；
    session 存储用的是 cookie，详见 session.ts 第 40-80 行。

父 Agent 拿到这段话之后，并没有读过 `jwt.ts` 和 `session.ts`。子 Agent 确实「看到了」那些文件，但内容只存在于子 Agent 的 `subState.messages` 里，不会回流。父 Agent 想拿到那两个函数的实际代码，只能自己再 `readFile` 一遍——子 Agent 刚才读过的内容等于白费。

因此 X-Code CLI 在每个内置 Agent 的系统提示词末尾都加了一段强约束，抽成共享常量：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/built-in.ts

const FINAL_MESSAGE_CONTRACT_HEADER =
  'CRITICAL — your final message is ALL the parent agent sees. ' +
  "It will NOT re-read files you've already read."
```

这段文本明确告诉子 Agent：关键代码片段要内联进最终消息（函数签名、关键逻辑、类型定义），不要说「see file X for details」——父无法看到 X，但也不要原样倾倒工具输出——综合成结构化答案。

这个约束反过来定义了什么任务适合委派给子 Agent：输入很多、输出可以收敛成一段自包含的结论。两个条件都不满足时——输入少或输出必须包含大量原始内容——就不应该使用 `task`。`task` 工具描述里的「When NOT to use」清单专门列出了几类不适合委派的场景：读单个文件路径、查 `class Foo` 这种已知符号、在 1-3 个文件内搜索。这些任务的特征是输出必须保留原始信息，直接调用 `readFile` / `grep` 更合适。

## 19.4 四个内置 Agent 的边界划分

X-Code CLI 启动时注册 4 个内置 Agent，每个的工具白名单和系统提示词都不一样：

| Agent 名称        | 工具白名单                                        | maxTurns | 适合的场景             |
| --------------- | -------------------------------------------- | -------- | ----------------- |
| explore         | readFile / glob / grep / listDir / shell（受限） | 25       | 只读代码探索（「X 在哪里定义」） |
| general-purpose | 全部（`*`）                                      | 40       | 多步研究或含写操作的任务      |
| plan            | readFile / glob / grep / listDir（无 shell）    | 30       | 实施计划设计            |
| code-reviewer   | readFile / glob / grep / listDir / shell（受限） | 25       | 审查待提交改动           |

表中的「shell（受限）」指 `explore` 和 `code-reviewer` 在 shell 工具上额外加了一组关键字拒绝列表（`SHELL_DENY_KEYWORDS`），包含 27 个条目，覆盖文件删除（`rm`、`rmdir`、`del`、`rd`）、文件移动和重命名（`mv`、`move`、`ren`）、git 写操作（`git commit`、`git push`、`git merge`、`git rebase`、`git reset`、`git checkout -b`、`git branch -d`、`git branch -D`）、输出重定向（`>`、`>>`、`tee`）、权限修改（`chmod`、`chown`）、包发布（`npm publish`、`pnpm publish`、`yarn publish`）以及容器删除（`docker rm`、`docker rmi`）。部分关键字同时匹配空格和制表符两种分隔形式（如 `rm ` 和 `rm\t`），因此实际条目数多于上面按类别罗列的数量。即使模型误调 `rm -rf`，子 Agent 也无法执行。

回到审查 auth 模块的例子。用户的请求是「看看有没有 bug 或安全问题」，模型选了 `code-reviewer`，于是子 Agent 只能用 `readFile` / `glob` / `grep` / `listDir` / `shell`（受限），无法修改任何文件——这正是审查任务所需要的能力边界。如果用户的请求是「帮我把所有 TODO 注释清理掉」，模型就应该选 `general-purpose`，因为只有它拥有 `writeFile` / `edit` 权限。

为什么要拆成四个 Agent，而不是做一个「通用子 Agent」让模型自己选择工具？原因有两个。

第一个原因是不同任务的安全边界不一样。`plan` 子 Agent 的设计意图是产出一份计划——它不需要 shell，因此从工具白名单里直接剔除，从源头消除模型误调 shell 的可能。`explore` 类型只做只读探索，保留 shell 是为了 `git log` 这种只读命令，因此用关键字拒绝列表把所有写操作拦截。`general-purpose` 是唯一的「全工具」Agent，可以写文件、执行任意 shell——但它的系统提示词里明确写着「When the work is investigative, do NOT modify code — just report」。

第二个原因是让模型选 Agent 比让它逐次选工具更稳定。让模型在主 prompt 里反复判断「读完这些文件后要不要写」会持续消耗它的注意力。把这种决策前置到选 Agent 那一刻——选了 `code-reviewer` 就不必再考虑写代码，选了 `plan` 就不必再考虑 shell。这是一种用更粗的边界减少决策错误的设计思路。

每个内置 Agent 的系统提示词都包含三段固定结构：角色定义（当前是什么 Agent、要完成什么任务）、行为指南（该如何开始工作、把握什么样的深度、有哪些常见误区）、以及上一节讲过的输出契约（`FINAL_MESSAGE_CONTRACT_HEADER` + 自包含格式要求）。模型在选 Agent 那一刻就加载了对应的角色与行为约束——比在主 prompt 里逐条提醒更可靠。

## 19.5 递归硬禁：子 Agent 不能再启动子 Agent

子 Agent 自己再调 `task` 工具启动新的子 Agent 会怎样？理论上 agent loop 没有任何机制阻止递归。但 X-Code CLI 在实现里把这条路彻底封死，而且是双重保险。

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/runner.ts

function buildToolFilter(agentDef, parentPermissionMode) {
  // 永远把 task 加进 deny 列表
  const deny = [...(agentDef.disallowedTools ?? []), 'task']
  // ...
}
```

第一层保险：每次构造子 Agent 的 `toolFilter` 时，`task` 都被强制加进 deny 列表——无论该 Agent 的定义里是否使用了 `tools: ['*']` 通配符。第二层保险：在 `subOptions` 里把 `subAgentRegistry: undefined` 显式置空，即使 `task` 工具侥幸通过了过滤器，也会因为找不到 registry 而返回错误。

为什么要这么严格？允许递归会带来三个失控问题。嵌套深度没有合理的上界——子 Agent A 派子 Agent B 派 C 派 D，token 成本指数级增长。调试变得不可行——出问题时调用栈是「主→A→B→C→…」，每一层都看不到上下文，复盘极其困难。而且在实际场景中几乎用不上——一层委派加上子 Agent 内部直接完成已经覆盖绝大多数用例。

X-Code CLI 选了最简的设计——子 Agent 永远是叶子节点，所有委派都从主 Agent 发出。需要「先研究再实施」的复杂任务，由主 Agent 负责协调：先用一个 `task` 子 Agent 做研究、拿回结论，然后自己开始实施（或者再开一个 `task` 子 Agent 实施）。把「协调」和「执行」显式分到主/子两层，比让子 Agent 自行再分裂层级要清晰得多。

## 19.6 连续 task 调用的并发执行

X-Code CLI 在工具执行时采取的策略基本是「一次执行一个」——`writeFile` / `edit` / `shell` / `askUser` 等均严格串行。但 `task` 是唯一的例外——同一轮里如果模型连续发出多个 `task` 调用，X-Code CLI 把它们合并到同一个 `Promise.all` 里并发执行。

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/tool-execution.ts

export function partitionToolCalls(calls: ToolCall[]): ToolCall[][] {
  const batches: ToolCall[][] = []
  let i = 0
  while (i < calls.length) {
    let end = i + 1
    if (calls[i]!.toolName === 'task') {
      // 连续的 task 调用合并到同一批
      while (end < calls.length && calls[end]!.toolName === 'task') {
        end++
      }
    }
    batches.push(calls.slice(i, end))
    i = end
  }
  return batches
}
```

`partitionToolCalls` 的逻辑很直接：遍历一轮 tool call 列表，遇到连续的 `task` 就合并成一个批次，遇到任何其他工具就单独成批。随后 `processToolCalls` 对每个批次调用 `Promise.all`——如果批次里只有一个工具调用，效果等同于串行；如果批次里有多个 `task`，它们就并发执行。

为什么只有 `task` 可以并发，而其他工具不行？因为子 Agent 天生就是隔离的，并发也不会互相影响上下文。每个子 Agent 通过 `createLoopState()` 拿到全新的 `LoopState`，消息历史、工具调用记录、todo 清单互不干扰。父 `parentState.tokenUsage` 的 `+=` 累加操作在 JavaScript 单线程事件循环中按 Promise resolve 的顺序执行，天然不会出现竞态——不需要锁。多个子 Agent 同时请求权限审批时，UI 端的 `permissionResolversRef` 队列会按到达顺序逐个弹出对话框。

而其他工具都在修改共享状态：`writeFile` / `edit` 修改文件系统和 `state.filesModified`；`shell` 增量打印 stdout 到主 UI——两个 shell 同时执行，输出会交错错乱；`askUser` / 权限对话框占用同一个 UI 模态区域；`todoWrite` / `enterPlanMode` / `exitPlanMode` 修改 `LoopState` 上的字段，下一轮需要读取。这些工具只能串行执行。

回到审查 auth 模块的例子。假设用户改了 auth 模块和 payment 模块，主 Agent 在同一轮里同时请求「检查 auth 模块」和「检查 payment 模块」，两个 `code-reviewer` 子 Agent 并行执行，总耗时接近单个子 Agent 的耗时而不是两倍——这正是 `task` 并发执行带来的效率收益。

`task` 工具本身只是一个委派入口——写入能力由子 Agent 决定。四个内置 Agent 中只有 `general-purpose` 拥有完整写权限（详见 19.4 节），`explore` / `plan` / `code-reviewer` 工具白名单里不含写工具，因此这三种子 Agent 即便多个并发执行也完全安全。真正需要注意的场景是主 Agent 在一轮里同时派出多个 `general-purpose` 子 Agent 且两边都打算修改同一个文件——写入顺序未定义，可能互相覆盖。这条纪律靠 `task` 工具描述里的并发安全约束（「NEVER launch multiple sub-agents in one turn if they could modify the same files or resources」）来约束模型行为；运行时不做检测，因为子 Agent 内部要修改哪些文件无法静态预知——只有运行起来读完代码才知道要修改哪些位置。

## 19.7 自定义 Agent：一个 Markdown 文件就够

4 个内置 Agent 是 X-Code CLI 默认带的。用户还可以自己定义子 Agent——例如团队有一个专门检查数据库迁移脚本安全性的 `migration-checker` 子 Agent。自定义 Agent 的格式是一个 Markdown 文件，YAML frontmatter 描述元信息，正文部分就是系统提示词：

```markdown
---
name: migration-checker
description: 检查数据库迁移脚本的并发安全性
tools: [readFile, grep, listDir, shell]
shellRestrictions: ['rm ', 'git push', '>', '>>']
maxTurns: 20
---

You are a database migration safety reviewer. Given a migration SQL file:

1. Identify any non-concurrent indexes or full table rewrites
2. Check for missing NOT NULL defaults on backfill columns
3. Flag any DDL that locks production reads
...
```

文件放在 `~/.x-code/agents/migration-checker.md`（用户级，跨项目可用）或 `<repo>/.x-code/agents/migration-checker.md`（仅当前仓库可用）。

启动时 X-Code CLI 扫描这两个目录，解析每个 `.md` 文件的 YAML frontmatter，用 Zod 校验后写入 `SubAgentRegistry`。整个加载流程有三个值得展开的设计选择。

第一个选择是 frontmatter 文件损坏或语法错误不会阻塞 CLI 启动。任意一个 `.md` 文件的 frontmatter Zod 校验失败时，loader 输出告警、跳过该文件继续加载剩余的 Agent：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/loader.ts

const result = frontmatterSchema.safeParse(parsed.data)
if (!result.success) {
  console.error(
    `[sub-agents] Skipping ${filePath}: invalid frontmatter — ...`
  )
  continue
}
```

如果一个写错的 Agent 文件直接导致 CLI 启动失败，用户体验会很差——用户可能不知道哪个文件出了问题，临时也修不好。跳过加告警的策略保证 CLI 随时可用，看到告警后再修复那一个文件即可。未指定 `maxTurns` 的自定义 Agent 会使用默认值 30。

第二个选择是覆盖优先级：后加载的同名 Agent 覆盖前面加载的。`SubAgentRegistry` 内部使用 `Map`，加载顺序是 `built-in → user → plugin → project`（`registry.ts` 中先展开 `builtInAgents`，再展开 `loadCustomAgents` 的返回值；`loadCustomAgents` 内部按 user → plugin → project 的顺序拼接数组），后来的 `Map.set(name, def)` 覆盖前面的：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/registry.ts

export async function createSubAgentRegistry(opts) {
  const custom = await loadCustomAgents(opts)
  // 加载顺序：built-in → custom（user → plugin → project）
  // Map 插入时同名覆盖，因此 project 级优先于 user 级优先于 built-in
  return new SubAgentRegistry([...builtInAgents, ...custom])
}
```

这意味着用户可以在 `~/.x-code/agents/explore.md` 里重新定义 `explore` Agent——内置的 `explore` 被覆盖。团队也可以在 `<repo>/.x-code/agents/explore.md` 里提供项目专属的探索规则，覆盖用户级和内置版本。

第三个选择涉及 registry 的生命周期。registry 在启动时构建一次，正常使用中不会自动变化。原因与 Ch.12 的提示词缓存（Prompt Cache）有关——`task` 工具的描述里动态嵌入了可用 Agent 列表（形如「Available sub-agents: explore, plan, code-reviewer, ...」）。这段字符串进入 `systemPromptCache` 后必须字节稳定，才能让各家供应商的提示词缓存命中。如果 registry 随意变化，描述随之改变，缓存整段失效，每轮请求都要重发系统提示词。不过 X-Code CLI 并没有把 registry 彻底冻结——`SubAgentRegistry` 提供了 `reload` 方法，用户可以通过 `/plugin refresh` 命令在会话中触发热加载。热加载完成后 CLI 会调用 `invalidateSystemPromptCache()` 主动清除缓存，下一轮请求重建系统提示词。这意味着热加载的代价是一次缓存未命中——后续请求重新建立缓存后恢复正常。日常修改或新增 Agent 文件后，最简单的生效方式是运行 `/plugin refresh` 而不必重启 CLI。

## 19.8 取消级联与权限继承

最后两个细节放在一起讨论。

取消级联解决的问题是：用户按 Esc 取消主 Agent 时，正在执行的子 Agent 必须同步被取消——不能让父 Agent 已经停止、子 Agent 还在后台继续消耗 token。实现方式是把父的 `abortSignal` 直接传给子：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/runner.ts

const subOptions: AgentOptions = {
  ...parentOptions,
  abortSignal: parentOptions.abortSignal, // 共用同一个 signal
}
```

子 Agent 内部的 `streamText({ abortSignal })` 收到信号后抛出 `AbortError`，子 `agentLoop` 干净退出。`runner.ts` 的 `catch` 块识别到 abort 后把 `[Sub-agent interrupted by user]` 作为字符串结果返回。这一点非常重要——必须给父 Agent 一个完整的 tool\_result，否则下一轮请求会因为「tool\_call 没有对应 tool\_result」被严格的供应商返回 400 错误。

权限继承解决的问题是：子 Agent 调用 `writeFile` / `edit` / `shell` 时也会触发权限审批对话框，这个对话框应该在主 UI 中弹出、使用同一个对话框组件。实现方式是子 Agent 的 callbacks 直接复用父的：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/sub-agents/runner.ts

const subCallbacks: AgentCallbacks = {
  // ...
  onAskPermission: callbacks.onAskPermission,  // 共享父的权限回调
  onAskUser: callbacks.onAskUser,              // 共享父的 askUser 回调
  onPlanApprovalRequest: callbacks.onPlanApprovalRequest,
  // ...
}
```

不需要单独的「子 Agent 权限对话框」组件——共享 callback 即可，UI 端的 `permissionResolversRef` 队列自然处理多个并发请求的排队弹出。

还有一层「父→子能力衰减」的特殊处理——主会话处于计划模式（Plan Mode）时，`general-purpose` 子 Agent 的 `writeFile` / `edit` 在 `buildToolFilter` 里被自动加入 deny 列表。这是计划模式整体约束机制的一部分——主 Agent 在计划模式下靠系统提示词约束不写文件（软约束），子 Agent 则靠工具过滤器硬拦截（硬约束）——完整动机和实现细节详见 Ch.21。整体不变式很简洁：子 Agent 拥有的能力永远不能超过父。父 Agent 只读时子 Agent 也只读；父 Agent 可写时子 Agent 按自己定义的白名单决定是否可写。

## 19.9 小结

本章围绕审查 auth 模块这个场景，完整走过了子 Agent 从启动到回收的全过程。`task` 工具的核心价值是结构性隔离——用一段独立的 loop 把「输入多 → 产出少」任务的中间产物挡在主对话之外。这种隔离由几层设计共同支撑：独立 `LoopState` 让子 Agent 从零起步（19.2 节）、`FINAL_MESSAGE_CONTRACT_HEADER` 定义了「最终消息即全部」的输出契约（19.3 节）、四个内置 Agent 按任务类型切分安全边界（19.4 节）、递归硬禁让子 Agent 永远是叶子节点（19.5 节）、`partitionToolCalls` 让连续 `task` 调用并发执行（19.6 节）、自定义 Agent 用一个 Markdown 文件就能扩展（19.7 节），取消级联和权限继承则保证了子 Agent 在父的控制边界之内安全运行（19.8 节）。
