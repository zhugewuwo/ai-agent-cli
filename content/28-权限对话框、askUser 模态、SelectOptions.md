# 第 28 章 权限对话框与 askUser 模态

模型在执行写文件、编辑、Shell 命令等操作前需要用户确认是否放行；调用 askUser 工具时需要用户在选项中做选择。这两类场景有一个共同的技术难题：agentLoop 是一个 async 函数，如何让它「暂停」在某一行，等用户在 UI 上做出响应后再继续执行？本章围绕这个问题展开，分为如下几个部分进行介绍。

- 28.1 节讲解挂起等待的统一模式：如何用 Promise + resolve 让 agentLoop 暂停等待 UI 响应。
- 28.2 节介绍权限确认对话框的渲染结构：标题、参数预览、选项行，以及参数行的自动截断机制。
- 28.3 节讨论 askUser 工具的 SelectOptions 实现：选项渲染、Other 后备选项、键盘路由共享。
- 28.4 节说明队列设计与挂起期间的 UI 状态管理。
- 28.5 节用一次完整的 edit 确认串讲整条数据流。
- 28.6 节给出本章小结。

## 28.1 挂起等待的统一模式

agentLoop 在两个地方需要暂停：权限确认（`writeFile` / `edit` / `shell` 在执行前问用户 Yes / Yes don't ask again / No）和 askUser 工具（模型显式向用户提问，等用户在选项中做选择）。两者的实现思路一致，都利用了 JavaScript 中 Promise 的阻塞语义。`await somePromise` 会让当前 async 函数停在这一行，直到 Promise 被 `resolve` 才继续。X-Code CLI 让 agentLoop `await` 一个 Promise，把这个 Promise 的 `resolve` 函数交给 UI 保管。用户在 UI 上选择 Yes / No 后，UI 调用这个 `resolve` 函数把决定（`'yes'` / `'no'` / `'always'`）传回去，`await` 才拿到结果继续执行。

整套流程分四个阶段：

<!-- check:ignore=code-block-size -->

```text
阶段 1 — agentLoop 调回调请求确认，自己 await 等待
   ┌─────────────────────────────────────────────────────────────┐
   │ agentLoop (core 层):                                          │
   │                                                               │
   │   const decision = await callbacks.onAskPermission(toolCall) │
   │                          ↑                                    │
   │                          这一行让 agentLoop 阻塞，等待结果   │
   └─────────────────────────────────────────────────────────────┘
                              │ 调用 UI 注入的回调
                              ▼
阶段 2 — UI 回调里创建 Promise、resolve 函数存入 ref、请求进 state 队列
   ┌─────────────────────────────────────────────────────────────┐
   │ onAskPermission 回调（use-agent.ts）：                        │
   │                                                               │
   │   return new Promise((resolve) => {                           │
   │     permissionResolversRef.current.push(resolve)  ← 存入 ref  │
   │     setState((prev) => ({ ...prev,                            │
   │       permissionQueue: [...prev.permissionQueue, toolCall]    │
   │     }))                                            ← 进 state │
   │   })                                                          │
   └─────────────────────────────────────────────────────────────┘
                              │
                              │ React 检测到 state 变化、触发重渲
                              ▼
阶段 3 — ChatInput 看见队列有内容，显示对话框
   ┌─────────────────────────────────────────────────────────────┐
   │ ChatInput.tsx：                                               │
   │                                                               │
   │   if (permissionQueue.length > 0) {                           │
   │     // 渲染队首的对话框：标题 / 参数 / Yes / No                │
   │     // 监听键盘：y / n / ↑↓ / Enter                           │
   │   }                                                           │
   └─────────────────────────────────────────────────────────────┘
                              │ 用户按 y
                              ▼
阶段 4 — 取出对应 resolve 函数调用，解除阻塞
   ┌─────────────────────────────────────────────────────────────┐
   │ resolvePermission('yes'):                                     │
   │                                                               │
   │   permissionResolversRef.current.shift()              ← 取出 │
   │   resolve('yes')                                      ← 调用 │
   │   setState({ permissionQueue: queue.slice(1) })     ← 队列前移│
   └─────────────────────────────────────────────────────────────┘
                              │
                              │ 阶段 1 那个 await 终于拿到 'yes'
                              ▼
   ┌─────────────────────────────────────────────────────────────┐
   │ agentLoop 继续：                                              │
   │   if (decision === 'yes') 真正执行 edit(...)                  │
   └─────────────────────────────────────────────────────────────┘
```

这套设计有两个关键的分工决策。

（1）`resolve` 函数本身就是「按钮」。保存它的引用，之后任何时候调用，就能让远端的 `await` 解除阻塞。

（2）ref 存 resolve、state 存请求。state 用来驱动重渲（让 UI 看见有新请求要显示），ref 用来在用户响应或取消时同步访问 resolve 函数。

为什么 resolve 函数放 ref 而不是 state？原因在于取消场景需要同步访问。用户按 Esc 取消整轮时，`abort()` 要一次性遍历所有挂起的 resolve 函数、全部调用 `resolve('no')` 解除阻塞。`setState` 是异步的，读到的可能是旧值，遗漏新请求；ref 的 `.current` 永远指向最新数组，不受渲染批次影响。代价是 ref 数组和 state 队列之间存在隐式的一一对应关系，push 和 shift 两侧必须保持同步。

## 28.2 权限确认对话框

> 源码：`packages/cli/src/ui/components/ChatInput.tsx` + `chat-input/permission.ts`

触发时机：`writeFile` / `edit` / `shell` / `enterPlanMode` 走 ask 路径时（详见 Ch.29）。对话框结构由标题、参数预览、选项行三段组成。先看 edit 工具的对话框：

```text
   ┌────────────────────────────────────────────────────────┐
   │ X-Code wants to edit a file                            │  ← 标题
   │                                                         │
   │ src/auth/login.ts                                       │  ← 参数预览
   │                                                         │
   │ ❯ Yes                                                   │  ← 选项行（3 项）
   │   Yes, don't ask again for: all edits this session      │
   │   No                                                    │
   └────────────────────────────────────────────────────────┘
   y / n 单字母快捷键  ·  ↑↓ 切换  ·  Enter 确认
```

shell 工具会多一个风险标签（read-only / write / dangerous），按级别染色：

```text
   ┌────────────────────────────────────────────────────────┐
   │ X-Code wants to run a shell command                    │
   │                                                         │
   │ $ rm -rf node_modules  [dangerous]                      │  ← [dangerous] 染红
   │                                                         │
   │ ❯ Yes                                                   │
   │   Yes, don't ask again for: this exact command          │
   │   No                                                    │
   └────────────────────────────────────────────────────────┘
```

shell 的第二项标签根据命令内容自动生成：能提取前缀的命令（如 `git commit -m "..."`）显示 `git commit:*`，放行同类命令；无法提取的（如 `rm -rf node_modules`）显示 `this exact command`，只放行完全相同的调用。`enterPlanMode` 不显示第二项。

各工具的标题与参数预览映射如下：

<!-- check:ignore=punctuation -->

| 工具          | 标题                                  | 参数预览                                                          |
| ------------- | ------------------------------------- | ----------------------------------------------------------------- |
| shell         | `X-Code wants to run a shell command` | `$ <command>` + 风险标签 `[read-only]` / `[write]` / `[dangerous]` |
| writeFile     | `X-Code wants to write a file`        | `<path> (new file)`（dim 后缀）                                   |
| edit          | `X-Code wants to edit a file`         | `<path>`（仅路径，不显示 diff）                                   |
| enterPlanMode | `X-Code wants to enter plan mode`     | `Read-only exploration; no edits until you approve a plan.`       |
| 其他          | `X-Code wants to use <tool>`          | 无参数预览行                                                      |

shell 的风险标签颜色随级别变化：`[read-only]` 绿色、`[write]` 黄色、`[dangerous]` 红色加粗，让用户一眼看出危险等级。

选项行通常有 3 项。Yes（`y` 快捷键）本次放行；Yes, don't ask again for `<label>` 放行并生成 `AllowRule`，后续同类操作自动放行；No（`n` 快捷键）拒绝。第 2 项仅在 `suggestRuleLabel` 返回非 `null` 时显示。上文提到，除 `enterPlanMode` 外的所有工具都会返回标签，因此绝大多数对话框都是 3 项，只有进入 Plan Mode 时显示 Yes / No 两项。

为什么 diff 不放进对话框？对话框是固定高度的单帧结构，diff 长度不可控，强行塞入会导致选项行错位。此外，写动作可能失败，先展示 diff 后执行失败会让用户困惑。因此 diff 在工具执行成功后才输出到终端历史区，屏幕上看到 diff 就意味着写入已经发生。同样的布局约束也体现在参数行上：参数行必须保持单行高度，超出终端列宽会触发自动换行，同样导致下方选项行错位。因此渲染前会根据终端宽度提前截断，超出部分用 `…` 标记。

## 28.3 askUser 与 SelectOptions

> 源码：`packages/cli/src/ui/components/ChatInput.tsx`

触发时机：模型调用 `askUser(question, options)` 工具时，挂起 Agent、把问题和选项交给 UI，等用户做选择再恢复。挂起和恢复用的是跟权限对话框同一种 Promise + resolve 模式。对话框的结构如下：

```text
   ┌────────────────────────────────────────────────────────┐
   │ ? 数据库用哪个方案？                                    │  ← 标题（模型传入的问题）
   │                                                         │
   │ ❯ PostgreSQL — 关系型，适合复杂查询                     │  ← 选项 1
   │   SQLite — 轻量级，无需服务器                           │  ← 选项 2
   │   MongoDB — 文档型，Schema 灵活                         │  ← 选项 3
   │   Other — Type a custom answer.                         │  ← 自带后备选项
   │                                                         │
   │ ↑↓ 移动  ·  Enter 确认                                  │
   └────────────────────────────────────────────────────────┘
```

上下箭头移动光标，Enter 确认选择。选中普通选项时直接把结果传回给模型；选中 Other 后切换到自由文本输入模式，用户输入自定义答案再传回。为什么末尾自带 Other？模型给的选项不一定覆盖用户实际想表达的情况。没有后备选项时，用户只能强行选一个不准确的、或者取消整轮对话。

## 28.4 队列设计与挂起期间的 UI 状态

挂起请求在数据层的建模方式是：权限请求用队列，askUser 用单值。

<!-- check:ignore=code-source -->

```ts
permissionQueue: PendingPermission[]
pendingQuestion: PendingQuestion | null  // askUser 同时只能有一个
```

为什么两者不同？权限请求用队列是因为模型一次可能返回多个工具调用，每个都需要独立确认，用户依次处理，每次确认后下一个自动弹出。askUser 用单值是因为模型一次只能调一个 askUser，不存在并发排队的场景。对话框显示期间，UI 进入模态状态：输入框禁用，键盘被对话框拦截。但进度指示器保持可见，因为用户需要看到「Running edit on foo.ts」这类提示才知道系统仍在工作，否则会以为无响应。权限对话框和 SelectOptions 都统一进 ChatInput 的 cell buffer 渲染，不使用 Ink 的动态区。原因在于 Ink 的 auto-scroll 会把对话框推进终端历史区后留下永久残行（详见 Ch.24）。cell buffer 自管整体高度，避免了这个问题。

## 28.5 完整数据流：以一次 edit 确认为例

前面各节分别讲解了挂起模式、对话框渲染和队列设计。本节以一次 `edit('src/auth/login.ts', oldText, newText)` 确认为例，把各节的机制串联起来。整体流程遵循 28.1 节的四阶段图：agentLoop `await onAskPermission` 阻塞 → UI 回调创建 Promise、存 resolve 到 ref、请求进 state 队列 → React 重渲触发 ChatInput 渲染对话框 → 用户按键后 `resolvePermission` 解除阻塞。这里补充四阶段图中未展开的两个环节，即用户侧看到的具体画面，以及三种决定的后续分支：

```text
用户在屏幕上看到对话框：
  X-Code wants to edit a file
  src/auth/login.ts
  ❯  Yes  
  ·  Yes, don't ask again for: all edits this session  
  ·  No

选中 Yes、按 Enter 后 agentLoop 拿到 'yes'：
  - 'yes'    → 本次放行，真正执行 edit
  - 'always' → 放行，同时生成一条允许规则写入会话，后续同类操作自动跳过确认
  - 'no'     → 抛 PermissionDeniedError，作为 tool_result 返回模型
```

这套模式不是权限或 askUser 独有的，任何「Agent 暂停等用户输入」的场景都可以应用。Plan Mode 就是直接复用这套机制（详见 Ch.21）：`enterPlanMode` 工具借助同样的回调 + Promise + state queue 让用户审核计划，再决定 keep / accept / cancel。

## 28.6 小结

本章讨论了 X-Code CLI 中权限对话框和 askUser 模态的实现。核心机制是 Promise + resolve 的挂起等待模式：agentLoop `await` 一个 Promise 阻塞自身，UI 持有 `resolve` 函数，用户做出选择后调用 `resolve` 解除阻塞。权限确认和 askUser 共用这套模式，区别仅在于数据建模：前者用队列处理并发请求，后者用单值。两种对话框都作为 ChatInput 内部的逻辑分支渲染进 cell buffer，避免了独立组件争夺 stdin 和 Ink 动态区残行的问题。
