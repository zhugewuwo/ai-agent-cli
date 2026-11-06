# 第 8 章 实现 Agent Loop：从 50 行示例到生产级循环

上一章建立了工具调用协议的心智模型：模型只能输出「我想调用 X」然后停下来，客户端必须执行工具并将结果回传，模型拿到结果后才能继续推理。这意味着客户端必须维护一个循环——发请求、收响应、执行工具、将结果回传、再发下一轮——直到模型不再请求工具。本章讨论这个循环在 X-Code CLI 中的具体实现：源码集中在 `packages/core/src/agent/loop.ts`，核心入口是 `agentLoop()` 函数。为了避免纸上谈兵，全章用一条贯穿始终的请求——「帮我把 `src/utils.ts` 里的 `formatDate` 函数改成支持时区参数」——来串联所有知识点。读者将跟着这条请求走完 Agent Loop 的完整生命周期：从会话初始化到模型决策，从只读工具的自动执行到写操作的权限确认，最终到文本回复和循环退出。本章分为如下几个部分进行介绍。 <!-- check:ignore-line -->

- 8.1 节对比 50 行示例与生产级循环，列出简单循环在生产环境中会断裂的五类问题。
- 8.2 节讨论会话首轮的初始化过程：会话状态为什么只初始化一次。
- 8.3 节沿示例请求的第 1 轮展开，解释只读工具为什么让 SDK 自动执行。
- 8.4 节沿第 2 轮展开，解释写操作为什么走手动派发。
- 8.5 节沿第 3 轮展开，讨论循环如何决定退出。
- 8.6 节讨论当会话变长时上下文压缩的两条路径。
- 8.7 节给出本章小结。

> AI 知识背景：ReAct 框架
>
> 这种「思考 → 行动 → 观察」的循环模式不是 X-Code CLI 的发明——它来自 2022 年 Yao 等人提出的 [ReAct（Reasoning + Acting）](https://arxiv.org/abs/2210.03629) 框架。核心思路是让模型在同一轮生成中交替输出推理和行动：每一步先说明打算做什么以及原因，然后调用一个工具，拿到结果后再决定下一步。实验表明这种模式同时缓解了两个问题：纯推理（Chain-of-Thought）容易产生幻觉（Hallucination，即编造不存在的事实），纯行动（直接调用工具而不做推理）则缺乏反思能力——遇到障碍时不会主动换策略。ReAct 将两者纳入同一个循环，使其互相纠错。当前所有主流 Agent CLI 和 SDK（Claude Code、Codex、LangGraph、AutoGen 等）的核心循环都是 ReAct 的工程化实现——「思考」对应模型输出的自然语言部分，「行动」对应 `tool_call`。本章讨论的 `agentLoop` 就是 ReAct 在 X-Code CLI 中的具体形态。 <!-- check:ignore-line -->

这种循环结构与 JavaScript 的事件循环（Event Loop）有相似之处。相同点是事件驱动的循环结构和单线程执行模型——两者都是「等待事件 → 处理事件 → 回到等待」的轮询。不同之处在于，事件循环处理的事件来自外部（I/O 完成、定时器、UI 操作），每个事件只是触发一个回调函数，不包含决策逻辑；Agent Loop 的事件源是模型自身的 `streamText` 输出，每一轮除了执行工具之外还包含「模型决定下一步做什么」的推理决策——事件循环是被动的调度器，Agent Loop 是带推理能力的主动调度器。 <!-- analogy: agent-loop-event-loop -->

## 8.1 从 demo 到生产级：简单循环在哪里断裂

Ch.01 Hello Agent 用 50 行代码写出了一个可运行的 Agent Loop——`streamText` 加 `for await chunk` 即可。那个示例虽然能够正常运行，但它只适用于最简单的场景：单个供应商、对话不超过上下文窗口（Context Window）、不需要持久化、不需要权限控制。一旦将它放到真实的业务环境中，简单循环会以多种方式失败。例如读者在同一会话中连续输入二十条修改请求后，消息数组累积了大量的工具调用结果，然后下一次 API 请求返回 `prompt is too long` 直接报错——50 行示例没有任何压缩机制来应对这种情况。又如模型决定修改一个关键配置文件，50 行示例会直接执行写入，读者对此毫不知情——没有权限确认意味着任何工具调用都会被静默执行，包括破坏性操作。

本章讨论的 `agentLoop` 是 X-Code CLI 的实际实现。骨架与 50 行示例一致——都是「调用模型 → 模型停下 → 执行工具 → 结果回传 → 再次调用」的循环——但额外完成了五类工作来应对生产环境中的各种边界情况。

（1）会话级元数据初始化：生成文件名标识、探测 Git 仓库、加载项目知识上下文，只在会话首轮执行一次。

（2）每轮的请求前准备：修复孤立工具调用、将图像降级为文本以适配纯文本供应商、注入供应商专属缓存标记、适配 thinking 模式参数。

（3）流式数据块分流：模型返回的响应中既包含文本片段也包含工具调用，需要分别路由到 UI 渲染和工具执行两个通道（详见 Ch.10）。

（4）多种结束分支：正常结束、工具调用继续、截断自动续写、不可恢复错误、用户取消——每种情况对应不同的处理策略。

（5）退出时的收尾工作：后台提炼可复用记忆写入磁盘、将未持久化的消息刷写到日志文件。

这五类工作将在后续各节中，沿着示例请求的执行轨迹逐一展开。以下是「改 formatDate 支持时区」这条请求在三方之间的完整执行轨迹：

```text
用户输入「帮我把 formatDate 改成支持时区参数」
  │
  ├─ 会话初始化：生成文件名标识、探测 Git 仓库、加载知识上下文（8.2 节）
  │
  ├─ 第 1 轮 ─ 模型决定先看代码
  │   ├─ 请求前准备（修复孤立工具调用、注入缓存标记等）
  │   ├─ streamText → 模型返回 readFile("src/utils.ts") 调用
  │   ├─ SDK 自动执行 readFile，结果回流到消息数组（8.3 节）
  │   └─ finishReason = tool-calls → 继续循环
  │
  ├─ 第 2 轮 ─ 模型决定修改代码
  │   ├─ 请求前准备
  │   ├─ streamText → 模型返回 edit("src/utils.ts", ...) 调用
  │   ├─ 权限确认：onAskPermission 触发，用户点击 Yes（8.4 节）
  │   ├─ processToolCalls 派发 edit 执行
  │   └─ finishReason = tool-calls → 继续循环
  │
  ├─ 第 3 轮 ─ 模型输出文本回复
  │   ├─ streamText → 模型返回「已完成修改，以下是变更内容……」
  │   └─ finishReason = stop → 退出循环（8.5 节）
  │
  └─ 退出后：后台提炼记忆、刷写日志
```

以下时序图展示了同一请求在用户、助手、工具三方之间的流转——每一次 assistant → tool 的往返对应一轮 `runTurn`：

```text
    user                 assistant              tool
     │                       │                    │
     │"改 formatDate 支持时区"│                    │
     │ ─────────────────────▶│                    │
     │                       │ readFile utils.ts  │
     │                       │ ──────────────────▶│
     │                       │     文件内容       │
     │                       │ ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
     │                       │ edit 修改函数签名  │
     │                       │ ──────────────────▶│
     │                       │     修改完成       │
     │                       │ ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │
     │  "已完成修改"          │                    │
     │ ◀╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌ │                    │
     ▼                       ▼                    ▼
```

第 1 轮 `readFile` 和第 2 轮 `edit` 的 `finishReason` 都是 `'tool-calls'`，循环继续；第 3 轮模型输出文本回复，`finishReason` 变为 `'stop'`，循环退出。后续各节将沿着这条时间线逐一拆解每一步的具体工作。

## 8.2 请求到达：会话状态为什么只初始化一次

示例中的「改 formatDate 支持时区」是读者打开 CLI 后输入的第一条消息。这条消息进入 `agentLoop` 函数后，首先需要一个能在多轮循环之间共享数据的容器——如果每一轮都从零开始，模型在第 2 轮就无法知道第 1 轮读到了什么文件内容。这个容器就是循环状态（`LoopState`），它持有对话消息数组、token 用量统计、会话标识、系统提示词缓存等 18 个字段（完整清单见 8.7 节附表），在整个会话生命周期内保持同一份实例。第一次调用 `agentLoop` 时由 `createLoopState` 从零创建；之后每次用户提交新消息，调用方把同一份实例通过 `existingState` 参数传回去复用，使得同一会话内的多次提问共享一份消息历史，模型基于完整上下文进行回答。

> 源码：`packages/core/src/agent/loop.ts`

<!-- check:ignore=punctuation -->
```ts
export interface AgentLoopResult {
  state: LoopState
  turnCount: number
}

export async function agentLoop(
  userMessage: UserContent,
  model: LanguageModel,
  options: AgentOptions,
  callbacks: AgentCallbacks,
  existingState?: LoopState,
): Promise<AgentLoopResult> {
  const state = existingState ?? createLoopState(options.permissionMode ?? 'default')
  state.messages.push({ role: 'user', content: effectiveUserMessage })
  // ...
}
```

消息推入 `state.messages` 之后，`agentLoop` 执行一轮会话级初始化。这部分只在会话的第一次调用时执行（通过 `state.taskSlug` 是否为空判断），后续提交跳过。初始化包含四项并行任务：

| 步骤 | 作用 | 执行方式 |
| --- | --- | --- |
| `generateTaskSlug` | 为日志文件生成人类可读的短标识，如 `format-date-timezone` | 纯 ASCII 走本地 `slugify`（不调用 LLM），中日韩字符发一次独立的 `generateText` 翻译 |
| `buildKnowledgeContext` | 读取 AGENTS.md 知识链与自动记忆，结果写入 `state.knowledgeContext` | 磁盘 I/O，不调用 LLM |
| `isGitRepo` 探测 | 检查当前目录是否是 Git 仓库，结果写入 `state.isGitRepo` | 一次 `fs.stat('.git')` |
| `appendHeader` | 往 jsonl 日志第一行写入会话元数据（model id、cwd、启动时间） | 发后即忘（fire-and-forget），不阻塞循环 |

四项任务之所以只执行一次而非每轮都跑，原因在于它们产生的结果在整个会话内不会变化。以 `taskSlug` 为例：这个值用作日志文件和 plan 文件的文件名。如果中途重新生成，已写入磁盘的文件名就会与新值对不上，留下无法匹配的孤儿文件。因此即使读者在第二条消息中谈论了完全不同的话题，文件名仍然沿用第一条消息产生的 slug。这是一种有意的「设置后不变」（set-once）语义——牺牲了文件名的精确性，换来了磁盘文件的一致性。`generateTaskSlug` 与其他三项任务并行启动，因为中日韩字符的翻译需要一次独立的 LLM 调用，并行执行可以避免串行等待带来的首轮延迟。

初始化完成后，函数构建工具注册表（`buildTools`）并进入主循环。在主循环的每轮开头，有一个对费用影响很大的缓存机制——系统提示词缓存（System Prompt Cache）：

<!-- check:ignore=punctuation -->
```ts
if (!state.systemPromptCache) {
  state.systemPromptCache = buildSystemPrompt({
    knowledgeContext: fullKnowledgeContext,
    modelId: options.modelId,
    isGitRepo,
    planMode: state.permissionMode === 'plan',
    planFilePath: state.currentPlanPath ?? undefined,
    mcpTools: options.mcpRegistry ? toSystemPromptEntries(options.mcpRegistry.list()) : undefined,
    skills: options.skillRegistry ? options.skillRegistry.list() : undefined,
  })
}
```

`systemPromptCache` 构建一次后整个会话复用同一份字符串。这种做法是 OpenAI 兼容供应商（DeepSeek、Moonshot、Alibaba、智谱、xAI）前缀缓存（Prefix Caching）命中的前提：前缀必须字节级稳定。任何在系统提示词中拼入动态内容：例如拼入当前时间、上一轮 token 数、已输入字符数等，都会让缓存命中率降到零。这不会产生任何错误提示，但每次请求都需要重新处理完整的系统提示词，费用和延迟都会增加。因此 `systemPromptCache` 一旦构建就只读不改。唯一的例外是进入或退出计划模式（Plan Mode）时，提示词内容随权限模式变化，旧缓存不再有效，工具执行层会将其置为 `null`，下一轮重新构建。非切换场景下始终复用。这一机制的完整原理详见 Ch.33。

## 8.3 第 1 轮：只读工具为什么让 SDK 自动执行

回到示例：初始化完成后，主循环进入第 1 轮。模型看到用户消息「帮我把 formatDate 改成支持时区参数」，决定先读取 `src/utils.ts` 了解当前实现。在调用模型之前，`runTurn` 需要完成一系列请求前准备，确保发给供应商的请求体格式正确。以下伪代码展示了主循环骨架和单轮执行的核心流程：

> 源码：`packages/core/src/agent/loop.ts`

<!-- check:ignore=punctuation -->
```ts
let turn = 0
while (options.maxTurns === undefined || turn < options.maxTurns) {
  turn++
  void flushPendingMessages(state)                          // 增量追加日志
  await checkAndCompressContext(state, model, threshold, callbacks)  // 主动压缩（8.6 节）

  const outcome = await runTurn(state, model, options, systemPrompt, callbacks, effectiveTools, turn)

  if (outcome.kind === 'aborted') break       // 用户按 Esc 取消
  if (outcome.kind === 'error') break         // 不可恢复错误
  if (outcome.kind === 'retry') {
    turn--                                    // 失败的请求不计入轮次配额
    continue                                  // 被动压缩已完成，重试本轮
  }

  if (outcome.finishReason === 'tool-calls') {
    await processToolCalls(await outcome.result.toolCalls, state, options, callbacks, model)
    continue
  }
  if (outcome.finishReason === 'length') { /* 续写逻辑，详见 8.5 节 */ continue }
  // finishReason === 'stop'：模型主动结束，退出循环
  break
}
```

`runTurn` 在调用 `streamText` 之前依次完成六步准备工作。第 1 轮中消息数组只有一条用户消息，大部分步骤实际上不会产生可见效果——但在后续轮次或特定供应商组合下每一步都有实际作用：

> 源码：`packages/core/src/agent/loop.ts::runTurn`

<!-- check:ignore=punctuation -->
```ts
async function runTurn(state, model, options, systemPrompt, callbacks, effectiveTools, turn) {
  // 1. 修复孤立的 tool_call（缺少 tool_result 的调用，供应商会拒绝整个请求）
  repairOrphanToolCalls(state.messages)
  // 2. 图像/文件类型降级为 OCR 文本（纯文本供应商不支持图像输入）
  await downgradeBinaryPartsForProvider(state.messages, options.modelId)
  // 3. 供应商专属缓存标记注入（工具定义也一并传入）
  const cached = applyCacheControl({
    system: systemPrompt, messages: state.messages, tools: effectiveTools, modelId: options.modelId, ...
  })
  // 4. thinking 模式参数适配（各供应商格式不统一）
  const thinkingOptions = getThinkingProviderOptions(options.modelId, options.thinking ?? false)
  const mergedProviderOptions = mergeThinkingOptions(cached.providerOptions, thinkingOptions)

  let result = streamText({
    model, system: cached.system, messages: cached.messages,
    tools: cached.tools ?? effectiveTools,
    maxRetries: 3, abortSignal: options.abortSignal,
    maxOutputTokens: getMaxOutputTokens(options.modelId),      // 5. 按模型设置输出 token 上限
    providerOptions: mergedProviderOptions,
  })
  drainStreamResult(result)  // 6. 预先 catch 所有同源 Promise

  // 后续：streamChunksToUI + collectTurnResponse
}
```

六步准备中任何一步缺失都会在某个供应商或某个使用场景下引发问题。例如第 1 步：如果上一轮因为网络中断导致模型发出了 `tool_call` 但客户端没有返回 `tool_result`，消息数组中就会出现孤立调用。供应商严格要求 `tool_call` 与 `tool_result` 配对，发现不配对会拒绝整个请求（HTTP 400）。`repairOrphanToolCalls` 在每轮开头扫描一遍消息数组，为任何缺少结果的 `tool_call` 补上一条合成的错误结果，使请求体恢复合法。这一步幂等执行——每轮都跑，代价极低（线性扫描），但防御了所有可能导致消息不配对的边界情况。各步骤的详细机制将在后续章节中逐一展开。

第 6 步需要特别说明。AI SDK 在调用 `streamText` 后会同时创建多个 Promise（`response`、`usage`、`finishReason`、`toolCalls`），它们共享同一个底层流。如果流在传输中出错，所有 Promise 都会在同一个事件循环 tick 中被拒绝。以下示意图对比了没有预捕获和有预捕获两种情况下的 Promise 状态变化：

```text
没有 drainStreamResult 的情况
──────────────────────────────────────────────────────────
  streamText() 创建四个兄弟 Promise
  ┌─────────┐ ┌─────────┐ ┌──────────────┐ ┌───────────┐
  │response │ │ usage   │ │ finishReason │ │ toolCalls │
  │(无 catch)│ │(无 catch)│ │  (无 catch)   │ │ (无 catch) │
  └────┬────┘ └────┬────┘ └──────┬───────┘ └─────┬─────┘
       │           │             │               │
  ── tick 1: 底层流报错，四个 Promise 同时 reject ──
       │           │             │               │
  await fullStream │             │               │
  捕获到错误 ✓     │             │               │
                   ✗ 未处理拒绝  ✗ 未处理拒绝     ✗ 未处理拒绝
                   └─────── Node.js 进程终止 ──────┘

有 drainStreamResult 的情况
──────────────────────────────────────────────────────────
  streamText() 创建四个兄弟 Promise
  drainStreamResult(result)  ← 立刻给每个 Promise 挂 .catch(noop)
  ┌─────────┐ ┌─────────┐ ┌──────────────┐ ┌───────────┐
  │response │ │ usage   │ │ finishReason │ │ toolCalls │
  │(.catch) │ │(.catch) │ │  (.catch)    │ │ (.catch)  │
  └────┬────┘ └────┬────┘ └──────┬───────┘ └─────┬─────┘
       │           │             │               │
  ── tick 1: 底层流报错，四个 Promise 同时 reject ──
       │           │             │               │
  await fullStream ✓ 已有处理器  ✓ 已有处理器     ✓ 已有处理器
  捕获到错误 ✓     （静默吸收）  （静默吸收）     （静默吸收）
                   └─ 进程安全，后续 await 仍可正常捕获 ─┘
```

`drainStreamResult` 的实现只有四行——给每个 Promise 挂上空的 `.catch(() => {})` 处理器——但必须在流开始之前完成。挂上空处理器不影响后续的正常 `await`：如果之后的代码 `await result.response`，拒绝仍然会正常传播到 `try/catch` 块中。空处理器只是防止错误在「被正式捕获前的那一个 tick」内成为未处理 reject。

`streamText` 调用发出后，AI SDK 开始处理流式响应。当模型返回 `readFile("src/utils.ts")` 工具调用时，一个关键的设计分叉发生了：`readFile` 是只读工具，AI SDK 会在流式阶段自动执行它并将结果写入响应消息，循环不需要暂停等待用户确认。这是 X-Code CLI 工具系统的两条执行路径（Execution Path）之一——只读工具（`readFile`、`glob`、`grep`、`listDir`、`webSearch`、`webFetch`）走 AI SDK 自动执行，写类和状态类工具走 `processToolCalls` 手动派发（下一节详述）。选择让只读工具自动执行而非统一走手动派发，是因为只读工具没有副作用，不需要权限确认也不需要循环护栏检测，自动执行可以减少一轮循环的延迟。这两条路径的工程动机详见 Ch.12。

自动执行带来一个需要注意的副作用：SDK 把工具结果写入 `response.messages` 时不经过截断处理。如果一次 `readFile` 返回上万行内容，这会迅速消耗掉上下文窗口。因此在收集完本轮响应后，`collectTurnResponse` 会调用 `truncateToolResultsInMessages` 对消息数组中所有自动执行的工具结果做一次补充截断——每种工具有各自的截断策略（`readFile` 保留头尾、`grep` 和 `glob` 只保留头部 500 行等），详见 Ch.17。

第 1 轮结束后，`runTurn` 返回一个判别联合（Discriminated Union）类型的结果：

<!-- check:ignore=punctuation -->
```ts
type TurnOutcome =
  | { kind: 'done'; finishReason: string; result: StreamResult }
  | { kind: 'error' }
  | { kind: 'retry' }
  | { kind: 'aborted' }
```

四种 `kind` 分别对应：正常完成（按 `finishReason` 进一步分支）、不可恢复错误、上下文超限后重试、用户取消。示例的第 1 轮返回 `kind: 'done'` + `finishReason: 'tool-calls'`——模型还要继续调用工具，主循环重置续写计数器后执行 `processToolCalls`，然后进入下一轮。

## 8.4 第 2 轮：写操作为什么走手动派发

第 1 轮结束后，消息数组中已经有了 `src/utils.ts` 的完整内容。模型在第 2 轮决定调用 `edit` 修改函数签名，添加时区参数。与第 1 轮的 `readFile` 不同，`edit` 是一个写操作——它会修改读者磁盘上的真实文件。直接自动执行显然不合适：读者对将要发生的文件修改应该有知情权和否决权。因此 `edit` 在工具注册时没有定义 `execute` 函数，AI SDK 会把它留在 `result.toolCalls` 中不执行，交给主循环的 `processToolCalls` 手动派发。

`processToolCalls` 接收到 `result.toolCalls` 解析后的完整调用列表，并不立即逐个执行。它会先剔除不该执行的调用（格式损坏的、已经执行过的），然后把剩余调用分成若干批次依次处理。示例的第 2 轮只有一个 `edit` 调用，管线各阶段直接放行。为了展示完整流程，我们用一个更丰富的假设场景来说明——模型在某一轮同时返回了五个工具调用：

> 源码：`packages/core/src/agent/tool-execution.ts` — `processToolCalls`

<!-- check:ignore=punctuation -->
```
result.toolCalls（模型本轮返回的全部调用）
┌──────────────────────────────────────┐
│  readFile  call-1  ← SDK 已自动执行  │
│  edit      call-2                    │
│  task      call-3                    │
│  task      call-4                    │
│  shell     call-5                    │
└────────────────┬─────────────────────┘
                 ▼
   ┌─────────────────────────────────┐
   │ 过滤 1：去除幽灵调用            │
   │ 回溯消息数组，收集本轮助手消息  │
   │ 中实际提交的 tool_call ID 集合  │
   │ → 不在集合中的调用直接丢弃      │
   │   （本例 5 个全部通过）         │
   └────────────────┬────────────────┘
                    ▼
   ┌─────────────────────────────────┐
   │ 过滤 2：跳过已完成调用          │
   │ 收集已有 tool-result 的调用 ID  │
   │ → readFile call-1 已有结果，跳过│
   └────────────────┬────────────────┘
                    ▼
   存活列表: [edit, task, task, shell]
                    │
                    ▼
   ┌────────────────────────────────────────┐
   │ 分批: partitionToolCalls               │
   │ ┌────────┐ ┌────────────┐ ┌─────────┐ │
   │ │ [edit]  │ │[task, task]│ │ [shell] │ │
   │ │  串行   │ │   并行     │ │  串行   │ │
   │ └───┬────┘ └─────┬──────┘ └────┬────┘ │
   │     ▼            ▼             ▼       │
   │   await    Promise.all       await     │
   └────────────────────────────────────────┘
```

两道筛选分别应对流式传输和自动执行带来的两类边界问题。第一道针对「幽灵调用」：AI SDK 在流式阶段如果遇到格式错误的工具输入（例如 JSON 不完整），会静默丢弃该调用在助手消息中的 `tool_call` 部分，但这个调用仍然出现在最终解析的工具调用列表中。如果不过滤就执行，产生的 `tool-result` 消息在下一轮 API 请求中找不到匹配的 `tool_call`，API 会直接报错。因此管线的第一步从消息数组末尾向前回溯，收集本轮所有助手消息中实际提交的调用 ID 集合，不在集合中的调用直接丢弃，也不为它生成任何结果消息——因为助手消息里根本没有对应的 `tool_call`，生成结果反而会制造新的孤立消息。第二道筛选处理重复执行问题。上一节提到，只读工具由 SDK 自动执行，结果已经推入了消息数组。这些调用同样出现在待处理列表中，如果再执行一次，轻则浪费资源（重复发起网络请求），重则破坏消息格式——同一个调用 ID 出现两条结果消息，部分模型供应商（如 DeepSeek）会直接拒绝这种请求。第二步用同样的回溯策略收集已有结果的 ID 集合，命中即跳过。

两道筛选之后的存活列表进入分批阶段。分批规则是：扫描存活列表，遇到连续的 `task`（子 Agent）调用就归入同一批，用 `Promise.all` 并行执行；其余工具各成独立的单元素批次，按顺序串行处理。只有子 Agent 能并行的原因在于隔离程度的差异——每个子 Agent 在启动时创建全新的循环状态，拥有独立的消息数组、工具调用记录和权限模式，与父循环不共享任何可变字段。子 Agent 完成后，token 用量通过累加方式汇总回父状态，单线程事件循环保证了这种累加不会出现撕裂写入。其他手动工具则都涉及共享资源的修改：`edit` 写入文件系统并更新文件修改记录，`shell` 的标准输出需要顺序到达终端以避免多进程输出交错，权限对话框同一时刻只能显示一个。如果并行执行这些工具，文件系统写入和终端输出都会出现竞态条件，这就是串行处理的代价所换取的正确性保证。

每个工具调用进入 `handleToolCall` 后，经过一条四步管线（Pipeline）：

> 源码：`packages/core/src/agent/tool-execution.ts`

<!-- check:ignore=punctuation -->
```ts
async function handleToolCall(tc, state, options, callbacks, parentModel, deferred) {
  const ctx = { toolName: tc.toolName, input: tc.input, toolCallId: tc.toolCallId, ... }

  // 1. PreToolUse 钩子——外部脚本可以拦截调用或修改输入
  // （hookResult.denied 为 true 时，追加一条拒绝结果后直接返回）

  // 2. 旁路处理器——不需要循环护栏的工具直接处理
  const bypassHandler = BYPASS_LOOP_GUARD_HANDLERS[ctx.toolName]
  if (bypassHandler) { await bypassHandler(ctx); return }

  // 3. 循环护栏检测（详见 Ch.32）
  if (await applyLoopGuard(ctx, deferred)) return

  // 4. 权限检查 + 实际执行
  if (!(await checkWriteOrShellPermission(ctx))) return
  const result = await executeWriteOrShell(ctx)
}
```

第 1 步的 PreToolUse 钩子（Hook）最先触发，允许外部脚本在工具执行前拦截或修改输入——例如一个安全审计脚本可以阻止模型删除特定目录。第 2 步的旁路处理器（Bypass Handler）涵盖 7 种工具——`askUser`、`task`、`todoWrite`、`enterPlanMode`、`exitPlanMode`、`listMcpResources`、`readMcpResource`。这些工具要么是交互性质（不会产生重复调用模式），要么自身携带独立的状态管理机制，跳过循环护栏检测不会带来风险。第 3 步循环护栏（Loop Guard）检测重复调用模式（详见 Ch.32）。示例中的 `edit` 不在旁路列表中，因此依次通过第 2、3 步检查，到达第 4 步——权限检查。

权限检查触发了回调函数 `callbacks.onAskPermission`。这是 Agent Loop 与终端 UI 之间的桥梁——`agentLoop` 本身不知道终端面板长什么样，也不负责渲染 Yes / No 按钮。它通过一组回调函数（`AgentCallbacks` 接口中定义了 13 个必选回调）将所有状态变化通知给 UI 层，由 UI 层决定如何呈现。`onAskPermission` 返回一个 Promise，循环只需 `await` 这个 Promise 即可挂起，等读者点击按钮后 Promise 完成（resolve 为 `'yes'`、`'always'` 或 `'no'`），循环自动恢复执行。`onAskUser` 也使用同样的挂起等待模式。这种模式不需要引入额外的状态机来管理「等待中 / 已批准 / 已拒绝」的状态转换。

> 源码：`packages/core/src/types/index.ts`

<!-- check:ignore=punctuation -->
```ts
export interface AgentCallbacks {
  onTextDelta: (text: string) => void
  onToolCall: (toolCallId: string, toolName: string, input: Record<string, unknown>) => void
  onToolProgress: (toolCallId: string, message: string) => void
  onToolResult: (toolCallId: string, result: string, isError?: boolean) => void
  onAskPermission: (toolCall: { toolCallId: string; toolName: string; input: Record<string, unknown> })
    => Promise<'yes' | 'always' | 'no'>
  onAskUser: (question: string, options: { label: string; description: string }[]) => Promise<string>
  onShellOutput: (chunk: string) => void
  onUsageUpdate: (usage: TokenUsage) => void
  onContextCompressed: (summary: string) => void
  onError: (error: Error) => void
  onPlanApprovalRequest: (planText: string) => Promise<boolean>
  onPlanModeChange: (mode: PermissionMode) => void
  onTodosUpdate: (todos: TodoItem[]) => void
  // 文件编辑、子 Agent 事件、记忆写入等可选回调
}
```

回调模式（Callbacks Pattern）带来三个直接收益。第一，`@x-code-cli/core` 包的依赖清单中没有任何 UI 库——不依赖 React 和 Ink。核心逻辑可以被移植到 VSCode 插件或 Web 版本而无需修改。第二，测试时只需传入模拟回调函数即可验证循环行为，不需要启动渲染框架：

<!-- check:ignore=punctuation -->
```ts
const onTextDelta = vi.fn()
const onToolCall  = vi.fn()
await agentLoop(userMessage, model, options, { onTextDelta, onToolCall, ... })
expect(onTextDelta).toHaveBeenCalledWith('Hello')
expect(onToolCall).toHaveBeenCalledTimes(1)
```

第三，异步交互天然支持——`onAskPermission` 和 `onAskUser` 都返回 Promise，循环挂起和恢复不需要额外的状态机。这种模式的代价是回调接口本身比较长，每增加一种新的交互模式都要在接口中添加新字段。但相比于让核心包直接依赖 UI 框架，这个代价是合理的——接口的扩展是可控的线性增长，而 UI 框架的耦合一旦引入就很难移除。UI 层实现细节详见 Ch.28。

读者点击 Yes 后，`edit` 工具执行修改，结果通过 `pushToolResult` 写入 `state.messages`，同时将修改的文件路径加入 `state.filesModified` 集合。第 2 轮的 `finishReason` 仍然是 `'tool-calls'`，主循环继续。

## 8.5 第 3 轮：循环如何决定退出

第 2 轮结束后，消息数组中已经有了修改成功的反馈。模型在第 3 轮输出一段文本回复：「已完成修改，以下是变更内容……」。这一轮没有工具调用，`finishReason` 变为 `'stop'`——模型主动结束了本轮对话。主循环将 `completedNormally` 标志置为 `true`，执行 `break` 退出 `while` 循环。

循环退出后，如果 `completedNormally` 为 `true` 且未被中断，`agentLoop` 会在后台启动记忆提取器（Memory Extractor）——一个异步任务，从本轮对话中提炼可复用的记忆写入磁盘（`~/.x-code/memory/`）。这个任务以发后即忘模式运行：读者可以立刻输入下一条消息，不需要等待提取完成。如果提取器在运行过程中找到了值得保存的知识点，会通过 `callbacks.onMemoryWrite` 在终端显示一条「Remembered: …」通知。同时，`flushPendingMessages` 做最后一次日志刷写，确保退出循环的那一轮消息也被写入磁盘。记忆提取的完整机制详见 Ch.36。

示例的三轮循环只用到了 `'tool-calls'` 和 `'stop'` 两种 `finishReason`。但生产环境中还存在其他结束方式。`runTurn` 返回 `outcome.kind === 'done'` 时，`agentLoop` 按 `finishReason` 字符串进行分支处理。AI SDK 定义了多个取值，X-Code CLI 显式处理其中 4 个常见值：

| finishReason | 含义 | 主循环的处理 |
| --- | --- | --- |
| `'stop'` | 模型主动结束 | 置 `completedNormally = true`，退出循环 |
| `'tool-calls'` | 模型请求继续调用工具 | 重置续写计数器，执行 `processToolCalls`，继续下一轮 |
| `'length'` | 输出被 `max_tokens` 截断 | 追加续写提示，自动重试，最多 3 次后报错退出 |
| `'content-filter'` | 供应商内容审核拦截 | 调用 `onError` 后退出循环 |

其中 `'length'` 最不直观——它对应的是模型输出尚未结束就被输出 token 上限截断的场景。例如模型生成代码写到一半，输出 token 数达到上限，回复在中途中断。此时 `agentLoop` 往 `state.messages` 中追加一条不显示给读者的续写提示：

<!-- check:ignore=punctuation -->
```ts
state.messages.push({
  role: 'user',
  content: 'Output token limit hit. Resume directly — no apology, no recap. '
    + 'Pick up mid-thought if that is where the cut happened. '
    + 'Break remaining work into smaller pieces.',
})
```

这条消息让模型从中断处继续输出。续写最多自动执行 3 次（`MAX_CONTINUATIONS = 3`），超过后向读者报错提示缩小问题范围。续写过程中读者看到的是一段连续的流式输出，中间最多有短暂停顿——续写提示不进入 UI 渲染通道，读者不会察觉到实际发生了多次 API 请求。续写机制的完整实现详见 Ch.11。

主循环还有两个与 `finishReason` 无关的出口。第一个是 `turn >= options.maxTurns`——仅在读者传入 `--max-turns` 参数或调用方是子 Agent / `--print` 模式时生效。交互模式下 `maxTurns` 为 `undefined`，`while` 条件永远为真，这条出口不会触发，按 Esc 才是唯一的停止信号。第二个是 `runTurn` 返回 `kind: 'error'` 或 `kind: 'aborted'`——不可恢复错误或用户取消，直接退出循环。需要澄清的是：循环护栏（Loop Guard）的拦截不会导致 `agentLoop` 退出循环。它将单次工具调用短路——追加一条伪造的 `tool_result` 让模型重新决策——循环本身继续运行。循环护栏的双阈值机制详见 Ch.32。

## 8.6 当会话变长：上下文压缩的两条路径

示例的三轮循环顺利完成，不过消息数组只有几条消息，远未触及上下文窗口上限。但在实际使用中，读者在同一会话里连续提出二十个修改请求并不罕见。每轮的工具调用和返回结果会迅速累积，消息数组的 token 总量会逐渐接近甚至超过模型的上下文窗口——如果不加处理，下一次 API 请求会直接失败。X-Code CLI 用两条路径协作应对这个问题，一条主动预防，一条被动兜底。

主动路径在每轮 API 调用之前检查当前上下文大小是否接近窗口容量上限。一旦超过阈值就触发压缩，压缩策略分两级：先尝试轻量裁剪（丢弃循环护栏拦截产生的失败调用对——这类消息占空间但对后续推理无贡献，裁剪不调用模型，毫秒级完成）；如果裁剪后仍然超限，再调用模型将旧消息总结成一段摘要，只保留最近几条消息原样不动。先轻后重的分级策略避免了每次压缩都消耗一次额外的 API 调用。被动路径是主动路径的安全网。基于 token 数的估算总会有误差——一次读入大文件或模型同时调用多个工具，都可能导致实际 token 数远超预期。如果 API 确实返回上下文超限错误，循环会强制压缩一次并重试本轮，这次失败不计入轮次配额。读者在大多数情况下察觉不到压缩在发生：主动路径在后台默默处理，被动路径只在极少数估算偏差过大时触发，触发后也是静默重试。两条路径的阈值选择、估算策略和压缩实现详见 Ch.31。

## 8.7 小结

本章用一条「改 formatDate 支持时区」的请求贯穿始终，跟踪它从进入 `agentLoop` 到模型完成修改的完整生命周期。生产级循环的骨架与 Ch.01 的 50 行示例一致，但额外完成了五类工作：会话级元数据初始化（只执行一次，结果缓存在 `LoopState` 中）、每轮六步请求前准备（孤立工具调用修复、图像降级、缓存标记注入、thinking 参数适配、输出 token 上限、同源 Promise 预捕获）、流式数据块分流（详见 Ch.10）、四种结束分支（`done` / `error` / `retry` / `aborted`）、退出时的后台记忆提取和日志写入。工具分为两条执行路径——只读工具由 AI SDK 自动执行，写类和状态类工具由 `processToolCalls` 手动派发，后者经过四步管线（PreToolUse 钩子 → 旁路处理 → 循环护栏 → 权限检查与执行）。回调模式将循环逻辑与终端 UI 解耦，使 core 包不依赖任何 UI 库。`systemPromptCache` 保持字节级稳定以满足前缀缓存的命中要求，上下文压缩通过主动估算和被动重试两条路径协作，在读者不会察觉的情况下控制消息数组大小。

接下来三章将把本章未展开的部分逐项补完：Ch.09 讨论多供应商支持，Ch.10 讨论流式数据块处理，Ch.11 讨论错误恢复与续写机制。

以下表格汇总了 `LoopState` 的 18 个字段，供读者查阅：

> 源码：`packages/core/src/agent/loop-state.ts`

| 分组 | 字段 | 用途 |
| --- | --- | --- |
| 对话内容与统计 | `messages` / `tokenUsage` / `lastInputTokens` | 消息数组、累计 token 用量、上一轮输入 token 数（压缩阈值判断依据） |
| 会话身份 | `sessionId` / `startedAt` / `taskSlug` | 唯一标识、启动时间、日志文件名短标识（set-once 语义） |
| 模式与计划 | `permissionMode` / `currentPlanPath` / `todos` / `checkpoints` | 当前权限模式、plan 文件路径、待办清单、回退快照 |
| 缓存与适配 | `systemPromptCache` / `recentToolCalls` / `knowledgeContext` / `isGitRepo` / `prevTurnCacheRead` / `expectCacheMiss` | 系统提示词缓存、循环护栏滑动窗口、知识上下文、Git 状态、缓存命中检测 |
| 文件追踪 | `filesModified` | 本会话修改过的文件路径集合 |
| 持久化游标 | `persistedMessageCount` | 已写入磁盘的消息条数，增量追加依据 |

