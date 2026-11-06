# 第 18 章 控制类工具：askUser、enterPlanMode 与 exitPlanMode

前几章讨论的工具都在「做事」——读文件、改文件、执行命令、搜索代码。本章的三个工具属于另一个类别：它们的作用是将控制权暂时交回用户。askUser 在模型遇到歧义时向用户提问，enterPlanMode 在模型判断任务复杂度较高时请求进入计划模式（Plan Mode），exitPlanMode 在计划完成后请求用户审批。三者表面用途各异，底层却是同一种形态——模型发起挂起请求，agent loop 暂停执行并等待用户响应，用户的选择作为工具结果返回给模型，循环恢复运转。本章重点讲解这种「暂停 - 等待 - 恢复」的工具形态是如何实现的，以及三个工具在回调通道、状态变更、UI 交互上各有哪些差异。enterPlanMode 和 exitPlanMode 涉及的计划模式完整状态机留给 Ch.21 展开。本章分为如下几个部分进行介绍。

- 18.1 节用一次迁移任务里的三次暂停串起全章的知识点。
- 18.2 节解释没有 execute 的工具怎样执行副作用。
- 18.3 节拆解三个工具各自的挂起与恢复通道。
- 18.4 节讲解 askUser 的选项约束与逃生口设计。
- 18.5 节讨论 enterPlanMode 与 exitPlanMode 的 description 长度为什么相差四倍。
- 18.6 节给出本章小结。

## 18.1 场景：一次迁移任务里的三次暂停

假设用户输入「把项目从 Express 迁到 Fastify」。模型判断这是一个跨多文件的复杂任务，主动调用 enterPlanMode 请求进入计划模式。CLI 弹出审批对话框，用户选择同意。进入计划模式后，模型在探索代码库的过程中遇到一个需要用户决定的分支，于是调用 askUser：

```jsonc
{
  "question": "测试现在用 supertest，迁移方案选哪个？",
  "options": [
    { "label": "继续用 supertest（兼容层）", "description": "改动小，但失去 fastify.inject 的速度优势" },
    { "label": "全部改成 fastify.inject", "description": "测试更快，但需要修改约 30 个测试文件" }
  ]
}
```

CLI 在输入框上方渲染选项菜单，用户选择第二项。模型将这个决定纳入计划，继续完善步骤细节。计划定稿后，模型调用 exitPlanMode，CLI 将计划全文渲染在对话区域，再弹出一个「Approve the plan above?」的 Yes/No 对话框。用户选择 Yes，会话模式从 plan 切换到 acceptEdits，模型开始按照计划修改代码。整轮交互里发生了三次「模型暂停 → 等待用户 → 恢复执行」，只是每次暂停的触发点和恢复条件不同。这三个工具的共同特征是：它们都不执行任何文件操作或命令，也不产生可观测的输出——它们做的事情就是让自动化流程在合适的时机停下来，把决策权交回给用户。

## 18.2 没有 execute 的工具怎样执行副作用

回顾 Ch.12 的工具分类，大多数工具在定义时提供了 execute 函数，AI SDK 收到模型的工具调用后直接执行该函数并将返回值作为工具结果。控制类工具不同——它们的副作用（弹出选项菜单、触发审批对话框、修改会话状态）需要访问 LoopState、AgentCallbacks、UI 通信通道等运行时上下文，而 AI SDK 的 execute 签名只接收工具输入参数，无法传递这些依赖。因此三个工具的定义都省略了 execute 字段：

```ts
// 三个工具的定义结构完全一致——都没有 execute
export const askUser = tool({
  description: '...',
  inputSchema: z.object({ question, options }),
  // 没有 execute
})

export const enterPlanMode = tool({
  description: '...',
  inputSchema: z.object({ topic: z.string().optional() }),
  // 没有 execute
})

export const exitPlanMode = tool({
  description: '...',
  inputSchema: z.object({ plan: z.string().optional() }),
  // 没有 execute
})
```

<!-- check:ignore=code-source,punctuation -->
```ts
// packages/core/src/agent/tool-execution.ts
// 绕过循环守卫的分发表，共 7 条入口
const BYPASS_LOOP_GUARD_HANDLERS: Record<string, ToolHandler> = {
  askUser:           handleAskUser,
  task:              handleTask,
  todoWrite:         ({ input, toolCallId, state, callbacks }) =>
    handleTodoWrite(input, toolCallId, state, callbacks, pushToolResult),
  enterPlanMode:     ({ input, toolCallId, state, options, callbacks }) =>
    handleEnterPlanMode(input, toolCallId, state, options, callbacks, pushToolResult),
  exitPlanMode:      ({ input, toolCallId, state, callbacks }) =>
    handleExitPlanMode(input, toolCallId, state, callbacks, pushToolResult),
  listMcpResources:  handleListMcpResources,
  readMcpResource:   handleReadMcpResource,
}
```

没有 execute 意味着 AI SDK 不会自动执行这些工具。它们的副作用统一由 `tool-execution.ts` 里的 `BYPASS_LOOP_GUARD_HANDLERS` 分发表接管。这张表共 7 条入口，名字中的「bypass loop guard」说明这些工具绕过了循环守卫（Loop Guard）机制（详见 Ch.32）。循环守卫的目的是检测模型陷入无效重复调用的死循环，但控制类工具天然会被反复触发——用户在同一轮对话里连续被问几次决定是合法的使用模式，不应被误判为死循环。其余 4 条入口（task、todoWrite、listMcpResources、readMcpResource）也各自有「重复调用合理」的理由，具体说明见 Ch.12。执行入口在 `handleToolCall` 函数里，逻辑很简单——先查 bypass 分发表，命中就走 bypass 路径并跳过循环守卫，否则进入常规的权限检查加执行管线：

<!-- check:ignore=code-source,punctuation -->
```ts
// packages/core/src/agent/tool-execution.ts::handleToolCall
const bypassHandler = BYPASS_LOOP_GUARD_HANDLERS[ctx.toolName]
if (bypassHandler) {
  await bypassHandler(ctx)  // 直接调用，不经过循环守卫
  return
}
// 否则走常规路径：权限检查 → 循环守卫 → 执行
```

## 18.3 挂起与恢复：三条不同的回调通道

三个工具都通过「创建 Promise → 等待用户操作 → resolve → 恢复执行」这一模式暂停 agent loop，但它们使用的回调通道并不相同。原因在于三者触发的 UI 交互形态不一样，需要不同的 React 状态机制来承接。理解这三条通道的差异，是理解控制类工具设计的关键。

askUser 的 handler 调用 `callbacks.onAskUser(question, options)`，返回一个 `Promise<string>`。UI 层收到回调后，将问题文本和选项列表写入 React 状态中的 `pendingQuestion` 字段，前端据此渲染选项菜单组件。用户选择后，UI 调用 resolve 将答案传回，Promise 兑现，handler 将选择结果格式化为 `User answered: <选项文本>` 作为工具结果推入消息历史，agent loop 恢复执行。整个过程中 askUser 不改变任何会话状态——它只是将用户的决定传达给模型。

enterPlanMode 的 handler 调用 `callbacks.onAskPermission({ toolCallId, toolName, input })`，返回一个 `Promise<string>`，resolve 值为 `'yes'` 或 `'no'`。这个回调走的是权限队列（`permissionQueue`）通道——与 writeFile、edit 等写入类工具的权限弹窗共用同一套排队机制。审批请求进入队列后，UI 依次弹出对话框，用户做出同意或拒绝的决定。

exitPlanMode 的 handler 调用 `callbacks.onPlanApprovalRequest(planBody)`，返回一个 `Promise<boolean>`。这是一个专属通道，行为分两步：先将计划全文作为一条 assistant 消息追加到对话区域（完整的 Markdown 渲染——标题、列表、代码块都正常显示），然后再弹出一个精简的 Yes/No 对话框。为什么不直接复用 askUser 的通道？原因是计划文本通常有几十行或数百行，如果放入选项菜单的提问区域，会溢出可视范围并将 Yes/No 按钮挤出屏幕。先渲染后审批的两步设计让用户能从容阅读计划全文，然后在一个干净的对话框里做出审批决定。

下面的序列图展示了三条通道的共同骨架：


![image.png](https://p6-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/78da38b0c5414f2a876cdc94573e78ca~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1232&h=438&s=37862&e=png&b=ffffff)

图 18-1：控制类工具的挂起 - 恢复序列。三个工具的骨架相同，差异在于第二步使用的回调通道和第四步渲染的 UI 组件不同。完整的 React 状态队列设计（ref 与 state 的取舍、并发工具调用的排队策略）详见 Ch.28。

挂起期间，终端进入模态状态：输入框禁用，不接受字符输入；提示语切换为等待文案；进度指示器暂停，避免视觉干扰。这些措施防止用户在等待选择期间发送新消息——如果一条新消息在 agent loop 暂停时进入消息历史，会破坏工具调用与工具结果的配对关系。

## 18.4 askUser：选项约束与逃生口

askUser 的 inputSchema 对选项数量做了硬约束——最少 2 个、最多 4 个：

<!-- check:ignore=code-source,punctuation -->
```ts
// packages/core/src/tools/ask-user.ts
options: z.array(
  z.object({
    label: z.string(),        // 选项标签，1-5 个词
    description: z.string(),  // 选项说明，解释选择后果
  }),
)
.min(2)
.max(4)
```

上限设为 4 而非更大的数字，原因是终端界面的垂直空间有限。每个选项占两行（标签加说明），4 个选项加上问题文本已经接近终端可视区域的一半。如果允许 8 个或 10 个选项，菜单本身就需要滚动，用户体验会退化为在列表里翻页——这与「快速决策」的设计意图相悖。下限设为 2 是因为单选项没有「选择」的含义，模型应该直接陈述结论而非假装让用户做决定。

UI 层在模型提供的选项之后自动追加一个「Other」选项。选中后会展开一个行内输入框，允许用户输入任意文本回答。这是给用户的逃生口——模型给出的 2-4 个选项未必涵盖用户实际想表达的意图，「Other」确保用户始终有自由表达的空间。工具描述里明确告知模型「There should be no "Other" option — the UI auto-appends one as the last row, so adding your own creates a duplicate」，防止模型自行添加重复的「其他」选项。

在计划模式下，UI 还会额外追加两个选项：

（1）「Chat about this」——用户希望继续对话讨论，而不是从菜单里选一个固定答案

（2）「Skip interview and plan immediately」——用户认为信息收集已经充分，要求模型跳过后续提问直接完成计划定稿

<!-- check:ignore=code-source,punctuation -->
```ts
// packages/cli/src/ui/hooks/use-agent.ts
// 计划模式下追加的两个 UI 侧选项
const planMeta =
  permissionModeRef.current === 'plan'
    ? [
        {
          label: 'Chat about this',
          description: 'Reply in conversation without picking an option above.',
        },
        {
          label: 'Skip interview and plan immediately',
          description: 'Stop the questions — produce the final plan now with everything gathered so far.',
        },
      ]
    : []
// 最终选项列表：模型提供的 + 计划模式专属的 + Other
const augmented = [...opts, ...planMeta, OTHER_OPTION]
```

这两个选项是计划模式「迭代式访谈」工作流的组成部分（详见 Ch.21）。askUser 工具本身对它们一无所知——是 UI 层在检测到当前处于计划模式时自动注入的。模型通过系统提示词中的 plan-mode overlay 得知这两个选项的存在，当用户选择后，模型收到的工具结果与普通选项格式完全相同——例如 `User answered: Chat about this`。这种设计让 askUser 保持纯粹的「提问 - 回答」语义，不需要感知计划模式的存在。

## 18.5 enterPlanMode 与 exitPlanMode：描述长度为什么相差四倍

打开这两个工具的源码，读者会注意到一个显著差异：enterPlanMode 的 description 长达约 600 个 token，列出了 7 类触发场景和多组 GOOD/BAD 示例；而 exitPlanMode 的 description 只有约 150 个 token，单段说明加一条边界提醒。这不是疏忽，而是刻意的设计选择，背后的原因与两个工具承担的「决策负担」直接相关。enterPlanMode 需要模型主动判断「当前任务是否应该进入计划模式」——这是一个开放性决策，没有简单的关键词可以匹配。如果描述只写一句「用于复杂任务」，模型在实际对话中几乎不会主动调用它，因为「复杂」是一个没有锚点的形容词。源码里的描述因此列出了 7 类具体触发场景：

（1）新功能实现——需要确定位置、交互方式、错误处理等细节

（2）存在多种可行方案——例如缓存可以用 Redis、内存、文件系统等不同方式实现

（3）修改已有行为——影响范围不确定，需要先理解现有逻辑

（4）架构决策——需要在不同技术方案之间做取舍

（5）跨多文件改动——涉及两三个以上文件的修改

（6）需求不明确——需要先探索才能理解任务全貌

（7）用户偏好影响实现方式——实现路径因用户偏好不同而不同

每类场景配有具体示例（如「Add user authentication to the app」属于第一类，「Fix the typo in the README」属于不应触发的简单任务），让模型读完后能将用户的请求与具体场景匹配。这 600 个 token 是购买「模型主动触发计划模式」行为的代价；源码注释里也明确写道——短描述（如「use for complex tasks」）会导致模型几乎不调用这个工具。exitPlanMode 的触发条件则简单得多：模型已经在计划模式里，计划文件写完了，调用它请求审批即可。单段说明加一条边界提醒（「不要用 askUser 代替 exitPlanMode 来请求审批」）已经足够，不需要额外的场景枚举来引导模型。

两个工具在审批通过后改变的状态也不同。handleEnterPlanMode 在用户同意后执行以下操作：

<!-- check:ignore=code-source,punctuation -->
```ts
// packages/core/src/agent/plan-tools.ts::handleEnterPlanMode
// 用户审批通过后的状态变更
state.permissionMode = 'plan'          // 切换权限模式
state.systemPromptCache = null         // 清除系统提示词缓存
state.expectCacheMiss = true           // 标记下一轮预期缓存未命中
// 如果当前没有计划文件路径，则根据用户输入生成一个
if (!state.currentPlanPath) {
  const topic = (input.topic as string | undefined)?.trim()
  state.currentPlanPath = makePlanFilePath(fallbackText, { slug: explicitSlug })
}
callbacks.onPlanModeChange('plan')     // 通知 UI 层模式已切换
```

清除系统提示词缓存并标记下一轮预期缓存未命中，是因为进入计划模式后系统提示词的内容会发生变化（需要拼入 plan-mode overlay），旧缓存必须失效（缓存机制详见 Ch.33）。此外，handler 还会根据模型传入的 topic 参数生成计划文件路径并存入 `state.currentPlanPath`，后续的写入操作可以直接写入这个文件。如果模型已经在计划模式下重复调用了 enterPlanMode，handler 会直接返回一条提示而不改变任何状态。

handleExitPlanMode 的审批通过路径更为复杂。它首先确定计划文件路径（优先使用 `state.currentPlanPath`），然后读取计划内容——如果模型传入了 plan 参数则使用覆盖值，否则从文件读取。如果计划内容为空，handler 返回错误要求模型先写入内容再重试。计划内容就绪后，通过 `callbacks.onPlanApprovalRequest(planBody)` 触发审批对话框。用户同意后的状态变更如下：

<!-- check:ignore=code-source,punctuation -->
```ts
// packages/core/src/agent/plan-tools.ts::handleExitPlanMode
// 审批通过后的连锁变更
state.permissionMode = 'acceptEdits'   // 切换到自动批准写入模式
state.systemPromptCache = null         // 清除缓存，移除 plan-mode overlay
state.expectCacheMiss = true
state.currentPlanPath = null           // 清除当前计划路径
callbacks.onPlanModeChange('acceptEdits')
// 向消息历史追加一条分界标记
state.messages.push({
  role: 'user',
  content: 'You have exited plan mode. You can now make edits, run tools, ...',
})
```

切换到 `acceptEdits` 而非 `default` 是一个有意的设计决定——用户刚刚审批了一份详细计划，如果接下来每次文件写入都弹出权限对话框，审批计划时的「同意」就失去了意义。`acceptEdits` 的具体语义和切换动机详见 Ch.21（Plan Mode 完整流程）和 Ch.29（三级权限模型）。审批通过后追加的 user 角色消息起到分界标记的作用，确保模型在后续回合中清楚地意识到自己已经离开了计划模式。

如果用户拒绝审批，会话留在计划模式，handler 返回一条错误结果告知模型：「Plan rejected by user. You are still in plan mode. Read the user's next message for feedback, revise the plan accordingly, and call exitPlanMode again.」模型收到后应根据用户的反馈修订计划文件，然后再次调用 exitPlanMode 请求审批。这种「提交 - 拒绝 - 修订 - 重新提交」的循环可以反复执行，直到用户满意为止。

## 18.6 小结

下面的对比表将本章讨论的三个工具的关键差异汇总在一起：

| 特征 | askUser | enterPlanMode | exitPlanMode |
| --- | --- | --- | --- |
| 是否有 execute | 否 | 否 | 否 |
| 回调通道 | onAskUser | onAskPermission | onPlanApprovalRequest |
| 触发的 UI | 选项菜单 | 权限审批对话框 | 计划全文渲染 + Yes/No 对话框 |
| 是否改变 LoopState | 否 | permissionMode → plan | permissionMode → acceptEdits |
| 是否重组系统提示词 | 否 | 是（加入 plan-mode overlay） | 是（移除 plan-mode overlay） |
| 审批拒绝时的行为 | 无拒绝概念 | 留在原模式 | 留在计划模式，模型修订后重试 |

本章讨论的三个工具代表了工具系统中一个独立的类别——控制类工具。它们不执行文件读写或命令运行等操作，而是在 agent loop 的自动执行流程中插入人工决策点。三者共享「没有 execute、走 bypass 路径、通过 callback 加 Promise 挂起循环」的骨架，但在回调通道的选择上各不相同：askUser 将选项写入 `pendingQuestion` 渲染选项菜单，enterPlanMode 复用权限队列的排队机制，exitPlanMode 需要一个专属通道来先渲染计划全文再弹出审批对话框。这种差异源自各自的 UI 需求。enterPlanMode 的描述长达 600 个 token，因为模型需要足够多的具体场景才能学会主动判断何时该进入计划模式；exitPlanMode 审批通过后切到 acceptEdits 而非 default，因为用户在宏观层面的审批应当免除微观层面的重复确认。
