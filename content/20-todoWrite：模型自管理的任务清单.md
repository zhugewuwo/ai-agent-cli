# 第 20 章 todoWrite：模型自管理的任务清单

上一章的 `task` 工具把一段子任务整段委派给独立的子 Agent 去执行；本章的 `todoWrite` 不开新对话——只在当前这次对话里维护一份清单，模型每完成一步就更新一次状态，用户在输入框上方看到一份实时刷新的 `◻ ◼ ✓` 列表。它表面上只是界面上的一个小组件，但真正的价值是约束模型——让它先把任务拆完再动手，避免散弹式推进（边做边想、东补一处西补一处，前后步骤缺乏整体规划）。多步任务里如果没有显式进度面板，用户既看不到 Agent 此刻在做什么、还剩多少步、是否受阻，模型也容易陷入「做到哪算哪」的模式。

本章先沿着一个具体场景把整个工具走一遍，再回头讨论几个让 `todoWrite` 真正可用的关键设计选择，分为如下几个部分进行介绍。

*   20.1 节用五步 JWT 重构的完整例子展示清单的创建、更新与完成过程。
*   20.2 节解释为什么用整体替换而非增量修改。
*   20.3 节讨论 schema 为什么把全部字段标为可选。
*   20.4 节讲解三条硬规则如何维持清单的实时感。
*   20.5 节介绍清单全部完成后的验证提醒机制。
*   20.6 节解释为什么清单不持久化到磁盘。
*   20.7 节给出本章小结。


![todo-write.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/005d2ef372a145b7ab35a548d0a10e8b~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1353&h=320&s=55746&e=png&b=272822)

## 20.1 一个完整的例子：五步 JWT 重构

假设用户输入：「把项目的鉴权重构成 JWT 模式，同时更新登录流程」。经过几轮探索后，模型在第一次开始动手时调用 `todoWrite`，参数大致如下：

```jsonc
{
  "todos": [
    { "content": "阅读现有鉴权实现", "activeForm": "正在阅读鉴权代码", "status": "in_progress" },
    { "content": "添加 JWT 签名/验证工具", "activeForm": "正在添加 JWT 工具", "status": "pending" },
    { "content": "更新登录处理器", "activeForm": "正在更新登录", "status": "pending" },
    { "content": "更新受保护路由的中间件", "activeForm": "正在更新中间件", "status": "pending" },
    { "content": "为新鉴权流程添加测试", "activeForm": "正在编写鉴权测试", "status": "pending" }
  ]
}
```

终端上立刻出现一块面板（贴在进度指示器上方）：

    ⎿ Update Todos
      ◼ 阅读现有鉴权实现
      ◻ 添加 JWT 签名/验证工具
      ◻ 更新登录处理器
      ◻ 更新受保护路由的中间件
      ◻ 为新鉴权流程添加测试

在传统 ConHost（老版 cmd.exe 或老版 PowerShell）下，`◻ ◼ ✓` 会自动降级到 `- # +`，详见 Ch.25 跨终端渲染。

模型读完源码后立即再发一次 `todoWrite`，把清单第一项标 `completed`、第二项切 `in_progress`：

```jsonc
{
  "todos": [
    { "content": "阅读现有鉴权实现", "status": "completed" },
    { "content": "添加 JWT 签名/验证工具", "status": "in_progress" },
    // ...另外三项仍 pending
  ]
}
```

面板不会整片重绘，而是只刷新真正变化的那两行（背后机制详见 Ch.24）：

    ⎿ Update Todos
      ✓ 阅读现有鉴权实现
      ◼ 添加 JWT 签名/验证工具
      ◻ 更新登录处理器
      ◻ 更新受保护路由的中间件
      ◻ 为新鉴权流程添加测试

五步全部 completed 之后，模型再发一次 `todoWrite`，用户看到的结果是面板直接消失了。这是 X-Code CLI 在 `handleTodoWrite` 里的一个隐式行为：每次写入后检测「是否全部 completed」，若是则把 `state.todos` 清空，下一帧自然就不再渲染面板：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/plan-tools.ts

const allDone = normalized.length > 0
  && normalized.every((t) => t.status === 'completed')
// 全部完成则清空，面板自然消失
state.todos = allDone ? [] : normalized
callbacks.onTodosUpdate(state.todos)
```

整个交互到此结束。下面五节讨论这个例子里的几个设计选择。

## 20.2 为什么用整体替换而非增量修改

清单会持续变化——某个 todo 从 pending 切到 in\_progress、某些 todo 切到 completed、可能还要追加新条目。模型该怎么把「变化」传给 CLI？有两种方法。

方法 A 是拆成三个工具——加一个、改一个、删一个。这是最像传统业务系统 API 的设计，提供 `addTodo` / `updateTodo` / `deleteTodo` 三个端点，模型按需调用。看起来直观，但对模型而言并不友好：每个 todo 必须有 id，模型必须记住上一次清单里每个 todo 的 id——记错了，`updateTodo({ id: "todo-3" })` 改到的可能是另一个 todo。一次想动三处就要发三次工具调用，且顺序不能错。中间任何一次出错，整个清单状态就会错乱。

方法 B 是每次让模型把整份清单重发一遍。这是 `todoWrite` 的选择。模型不调「加/改/删」，只调一个 `todoWrite`，参数是当前完整的清单——包括那些没改动的条目。改第一项的状态？把全部 5 项再写一遍，第一项的状态改掉，其他原样照抄：

<!-- check:ignore=punctuation -->

```jsonc
{
  "todos": [
    { "content": "阅读现有鉴权实现", "status": "completed" },       // 改了
    { "content": "添加 JWT 签名/验证工具", "status": "in_progress" }, // 改了
    { "content": "更新登录处理器", "status": "pending" },            // 没动，原样
    { "content": "更新受保护路由的中间件", "status": "pending" },     // 没动，原样
    { "content": "为新鉴权流程添加测试", "status": "pending" }        // 没动，原样
  ]
}
```

看起来冗余——明明只改了两处，整张表又抄了一遍。但对模型而言反而更可靠。不用维护 id——条目按位置和文字识别，模型不需要记「上一次第三条的 id 是 todo-3 还是 todo-2」。一次调用完成——不论改一处还是改五处，永远是一次 `todoWrite`。天然自纠——上一次列错了一项，下一次重写时直接改对即可，不需要补一个 `delete id-3` 调用。代价是每次调用的参数变长（5 项永远要传 5 项），但对 LLM 而言，多写几个字远比出错率高要好——后者会让整个清单功能不可用。

工具描述里的硬规则第 6 条正是这样表述的：「Pass the FULL list every call — todoWrite REPLACES the list, not merges. Include unchanged items.」REPLACE（替换）这个词是核心：每次调用都把内存里上一份清单整个丢弃，用模型新传的这份覆盖。这是「工具粒度」的一个经典取舍——传统业务系统设计 API 时倾向于把操作拆细（一个端点做一件事），但给 LLM 设计工具时反而应该合粗。模型不擅长跨多次调用维护一致性，单工具加整体覆盖把这种风险消灭在工具形态这一层。

## 20.3 schema 为什么全部标可选

模型调用工具时会输出一段 JSON 参数（前面例子里那段 `{ "todos": [...] }`），但 LLM 输出本身带有不确定性，可能漏字段、可能字段名拼错。schema 就是工具收到这段 JSON 后自动校验一遍的规则——形态类似「参数表单的字段验证」，不符合就直接打回。X-Code CLI 用 `zod` 这个库写 schema。

直觉答案是 schema 应该越严越好——三个字段都必填，状态值必须是三选一，少一个就拒绝。但打开 `todo-write.ts` 一看，写法恰好相反：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/tools/todo-write.ts

z.object({
  content: z.string().optional(),    // 可选
  activeForm: z.string().optional(), // 可选
  status: z.enum(['pending', 'in_progress', 'completed']).optional(), // 可选
})
```

三个字段全是可选。工具描述里明确写着「三个字段都必须传」，为什么代码里反而放得这么松？答案写在源文件那段 SCHEMA LENIENCY 注释里：弱模型经常漏字段。DeepSeek-flash、GLM、Qwen 这一档模型在写多个 todo 时——尤其是当前处于 in\_progress 状态的那个——很常漏 `status`（前面写过就不再重复），偶尔漏 `content`（`activeForm` 已经表达了意思）。这些都是合理的 `todoWrite` 调用，意图明确，只是格式上少了一个字段。

如果 schema 写严格，会引发连锁故障。zod 校验失败后 AI SDK 拒掉这个工具调用，不交给业务代码执行。对话历史里留下了 assistant 的 tool\_call（模型尝试调 `todoWrite`），但没有对应的 tool\_result。下一次 API 请求带着这条孤立的 tool\_call 发出去，严格的供应商（如 Anthropic）会校验「每个 tool\_call 必须有对应的 tool\_result」，API 返回 400 错误，整个对话从此无法继续。本来只是「漏一个字段」这种小问题，因为 schema 写严了，演变成了整个对话彻底阻断。

X-Code CLI 选了另一条路：schema 写宽松一点，让模型怎么传都能过校验；真正的归一化放在自己的处理函数 `handleTodoWrite` 里手动做：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/plan-tools.ts

const content = (t.content ?? '').trim()
const activeForm = (t.activeForm ?? '').trim()
if (!content && !activeForm) continue // 两个都空才丢弃这一项
normalized.push({
  content: content || activeForm,     // 缺哪个用另一个填
  activeForm: activeForm || content,
  status: t.status ?? 'pending',      // 缺 status 默认 pending
})
```

这段代码做了三件事：两个文本字段都为空——这个条目没有任何意义，丢弃；只缺一边文字——拿另一边填上，至少能显示；缺 status——默认成 `pending`，是最保守的选择（比默认成 `in_progress` 安全）。如果这一轮丢弃了 N 个无意义条目（即两个文本字段同时为空），处理函数还会在工具结果里追加一句话给模型看，意思是「本次有 N 项因为同时缺 `content` 和 `activeForm` 被丢弃，下次请把两个字段都带上」——形成一个自然的反馈闭环：模型本轮的工作不受影响，下一次输出时收到这条提示，自己就会改进。

这条经验放大到工具设计层面是通用的：面对 LLM，入口校验要宽——宽到能接住模型常见的小抖动，避免一次漏字段把整轮对话彻底阻断；反馈要严——在自己的处理函数里把「模型哪里没做对」明确告诉它，让它下次做得更好。宁可自己代码里多写几行容错逻辑，也不要让 zod 这一层的拒绝直接把对话推入死路。

## 20.4 三条硬规则如何维持清单的实时感

清单的核心价值在于实时感——用户瞄一眼面板，就能知道 Agent 当前在做什么、做完了什么、还剩什么。然而模型在没有规则约束的情况下，很容易破坏这种实时感。原因在于 agent 循环的一次迭代中模型可以发出多个工具调用，CLI 依次执行完后才让模型生成下一段回复。回到 JWT 例子：假设模型在一轮中连续发出四个工具调用——前三个是 `edit`，分别修改 jwt.ts、login.ts 和 middleware.ts，第四个才是 `todoWrite` 把这三个 todo 全标 completed。这一轮的执行可能耗时十几秒，但用户看到的清单上 in\_progress 标记始终没变，直到最后那次 `todoWrite` 被执行才一次性跳了三步——用户的体感是 Agent 卡住了好一会儿然后突然跳步。另一种情况是虚假完成：测试已经失败，模型把「代码写完」等同于「任务做完」标了 completed，用户以为任务完成了，实际上任务并未真正完成。

工具描述里共有七条硬规则，其中三条最关键：

| 规则                      | 不遵守的后果                  |
| ----------------------- | ----------------------- |
| 同一时刻恰好 1 项 in\_progress | 用户看面板无法判断「Agent 现在在做什么」 |
| 做完立即标 completed         | 用户看到的清单总是滞后的            |
| 没真正完成绝不标 completed      | 测试失败被标完成，用户以为已完成        |

三条都指向同一个目标：确保处于 in\_progress 状态的 todo 就等于 Agent 此刻在做的事。下面依次展开。

先看第一条。如果允许 0 项 in\_progress，模型读完源码后有可能写出这样的清单：

    ✓ 阅读现有鉴权实现
    ◻ 添加 JWT 签名/验证工具   ← 哪一项是 Agent 现在要做的？
    ◻ 更新登录处理器

三项都是 pending，没有一项表示「我正在做这个」。用户盯着面板，无法判断 Agent 是在思考、在等待，还是已经开始动手。实际情况往往是模型内部已经决定去改 JWT 工具了，但它没有更新清单就直接去写文件——这一刻清单和事实就脱节了。强制「恰好 1 项 in\_progress」意味着模型每次更新清单时必须明确标出接下来要做的是哪一项，让清单永远反映当下状态。

再看第二条。沿用前面的例子：模型在一轮里依次发出 `edit jwt.ts` → `edit login.ts` → `edit middleware.ts` → `todoWrite`，前三步执行的十几秒里面板上标为 in\_progress 的 todo 始终没变，用户以为 Agent 卡住了。规则要求模型每做完一项就立即发一次 `todoWrite`，而不是攒到一轮结束时批量更新。在 agent 循环的机制下，这相当于要求模型把工作拆成多轮——A 轮修改 jwt.ts 并发一次 `todoWrite`，B 轮修改 login.ts 并发一次 `todoWrite`，C 轮修改 middleware.ts 并发一次 `todoWrite`。每轮的 `todoWrite` 同时完成两件事：把刚做完的 todo 标为 completed，把下一个 todo 标为 in\_progress。这样每一轮结束后清单都会更新，用户看到的面板和 Agent 的实际进展保持同步。

最后看第三条——没真正完成绝不标 completed。读者可能会疑惑：这是否意味着模型每写完一步代码都要先执行一遍测试或类型检查，确认通过了才能标 completed？并非如此。实际场景中，模型完成前面几个修改代码的步骤后会直接标 completed，并不为每个步骤单独执行验证；验证（测试、类型检查）通常作为清单的最后一项，确保在最后执行。这种做法完全合规。规则真正禁止的是四种情况，工具描述的原文写得很明确：「Only mark complete when truly done — if tests are failing, the implementation is partial, you hit an error, or you're going to follow up later: leave it as in\_progress and add a NEW pending todo describing the unresolved part.」即测试已经跑过且失败了（注意是「已经跑过且失败」，不是「还没跑」）、实现只做了一半、遇到了错误（`edit` 返回 `old_string not found in <path>`、`tsc` 报错、shell 命令非 0 退出）、以及打算稍后再回来收尾。出现这些情况时，模型应该把这个 todo 的状态保持为 in\_progress，同时追加一个新的 pending todo 写明未完成的部分。

回到 JWT 例子来看一个具体的反面场景。假设模型在执行清单第二项「添加 JWT 签名/验证工具」时，调 `edit` 工具想替换 `jwt.ts` 里的一段代码，工具返回 `old_string not found in jwt.ts`——文件里并不存在模型要替换的那段字符串。这属于四种禁止情况中的「遇到了错误」。此时清单不应该把这一项标 completed 跳到下一步，而应该把它留在 in\_progress，同时追加一项新的 pending todo 写明未解决的问题（例如「排查 jwt.ts 里 edit 失败的字符串匹配」）。如果允许模型在出错后静默标 completed 跳过，就会导致 UI 状态和实际状态不一致——用户看到五项全 completed，以为任务已完成，实际上有一项还没成功。

以上三条规则全部是提示词层面的约束——写在工具描述里让模型自己遵守。`handleTodoWrite` 的代码不检查工具调用历史，也不会拒绝任何 status 转换，模型如果不遵守规则，代码层面并不拦截。这是一个有意为之的设计选择：与其在代码里写复杂的状态机逻辑去校验「上一次调用是否真的做完了」，不如把规则清晰地写在工具描述里，依赖模型的指令遵从能力。这也意味着「出问题不要隐藏」（第三条规则）和「何时做最后的整体验证」是两件独立的事——前者靠提示词约束，后者还有代码层面的验证提醒（Verification Nudge）作为补充，详见下一节。

## 20.5 清单全部完成后的验证提醒

继续 JWT 重构的例子。模型五步全部 completed，面板消失，看起来工作已完成。但 X-Code CLI 在面板消失之前，隐式做了一件事——检查这份清单里是否包含「验证」动作。如果检查没通过，就在工具返回结果里追加一句话给模型看：「Before wrapping up, verify your work — run tests, lint, or type-check as appropriate for this project.」（收尾之前先验证一下——根据项目情况跑测试 / lint / 类型检查。）

源码里这个机制叫 verification nudge——nudge 是「轻推」的意思——不是强制，而是在合适的时机给模型一个提示，让它意识到应该做最后一步验证再结束。模型存在一种常见的失误模式：把「代码写完了」等同于「任务做完了」，改了五个文件、把五个 todo 全标完、看似一切就绪——但实际上类型可能并未通过 `tsc`、测试有可能失败了、lint 可能仍有报错。用户在另一端看到面板消失以为 Agent 已经完成，结果编译出错或测试失败。验证提醒正是为了防止这种情况：在面板消失这一刻，回看清单里是否有任何一项提到验证——有，模型大概率已规划在内；没有，主动追加一句提醒。

触发条件的代码很简洁：

<!-- check:ignore=code-source,punctuation -->

```ts
// 源码：packages/core/src/agent/plan-tools.ts

const VERIFY_RE = /\b(verif|test|check|lint|build|typecheck|tsc)\b/i
const needsVerifyNudge =
  allDone &&                   // 整张清单全 completed
  normalized.length >= 3 &&    // 至少 3 项（短清单不必提醒）
  !normalized.some((t) =>      // 没有任何一项含验证关键词
    VERIFY_RE.test(t.content) || VERIFY_RE.test(t.activeForm))
```

三个条件同时满足才追加提醒：整张清单全 completed；至少 3 项——只有一两项的小任务用不着这种提醒；没有任何一项的 `content` 或 `activeForm` 含 `verif` / `test` / `check` / `lint` / `build` / `typecheck` / `tsc` 这些关键词——意味着模型自己没规划验证步骤。

回到 JWT 例子，清单第五项是「为新鉴权流程添加测试」——`test` 关键词命中，不触发这个提醒。但假设用户的任务换成「把这个模块的所有变量名改成驼峰」——五步可能都是「重命名 X」/「重命名 Y」，没有任何一项提到验证，就会触发提醒。模型读到这句提示后通常会主动补一步 `pnpm test` 或 `tsc -b` 收尾。

这里涉及一个工具设计上的取舍。「验证提醒」这种「工具调用后隐式检查、并可能往结果里追加内容」的逻辑，是写在 X-Code CLI 自己的处理函数 `handleTodoWrite` 里的，不是交给 AI SDK 自动处理。原因在于 `todoWrite` 的副作用涉及多件事——修改内存里的清单（`state.todos`）、通知界面刷新、检查验证提醒、生成「本次有几项被丢弃」的反馈——这些都需要拿到完整的对话上下文（即 `LoopState`）才能正确处理。AI SDK 的自动执行适合只看参数、不看会话状态的工具——给参数、出结果、结束（例如 `readFile`、`grep` 这类）。需要访问 `LoopState`（清单、消息历史、权限模式等）的工具，更适合 X-Code CLI 自己接管，详见 Ch.08 的「工具的两条执行路径」。

## 20.6 为什么清单不持久化

todos 这份清单只存在内存里——`LoopState.todos`。会话退出（关闭终端）、`/clear`（清空当前对话）、`/resume`（切换到其他历史会话）这三种情况下都会清空清单，不写入磁盘。`/compact`（压缩对话历史）不会清空 todos——它只重写 `state.messages` 把历史摘要化，`state.todos` 原样保留，让模型在压缩后仍能按原计划继续执行。

X-Code CLI 不把清单持久化，原因是清单是这一次对话的工作记忆，而不是项目的历史档案。

这两件事的区别值得展开。工作记忆是「接下来五分钟要做哪几步」——变化快、对当下意义大、过期即失效。例如清单上写「先读源码、再加 JWT 工具、再改登录」——这些步骤只在本次重构刚开始时有意义，五分钟后这次重构完成了，这份清单就成了历史，保留下来只会污染下一次对话。历史档案是「这个项目过去做过什么、未来要做什么」——变化慢、跨会话有意义，例如 feature roadmap、bug 列表、release plan——这些应放在 git commit 信息、issue tracker、AGENTS.md 等位置。

## 20.7 小结

本章围绕五步 JWT 重构这个场景，完整走过了 `todoWrite` 工具从创建清单到面板消失的全过程。整体替换（REPLACE）语义让模型不必维护条目 id，一次调用即可完成多处变更（20.2 节）。schema 全部标可选加上 `handleTodoWrite` 里的手动归一化，把弱模型漏字段的小问题拦在对话阻断之前（20.3 节）。三条硬规则——恰好 1 项 in\_progress、做完立即标、没完成绝不标——共同维持清单的实时感，让用户看标为 in\_progress 的 todo 就等于「Agent 此刻在做的事」（20.4 节）。验证提醒在三个条件同时满足时轻推模型补一步收尾验证，防止「代码写完了等于任务做完了」的常见失误（20.5 节）。清单只存内存不持久化，用「不跨会话」这一约束反过来定义工具的语义——只记当下要做的事，不做长期任务管理（20.6 节）。
