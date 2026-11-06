# 第 10 章 流式 Chunk 处理：从模型输出到终端渲染

上一章讨论了 Agent Loop 的整体结构，读者已经知道循环的每一轮由「发送请求 → 消费流 → 执行工具 → 收集响应」四个阶段组成。本章聚焦其中「消费流」这个阶段：`streamText()` 返回的 `fullStream` 是一个异步迭代器（Async Iterator），每个 chunk 代表模型流式输出的一片信息——文本片段、工具调用、工具结果、推理内容、错误、结束标记。如何处理这些 chunk，直接决定了两件事：用户在终端看到的实时反馈是否流畅，以及异常发生时程序能否正确恢复而非崩溃退出。

为了让这些机制有具体的画面，我们沿用 Ch.08 的贯穿示例：用户输入「帮我把 `src/utils.ts` 里的 `formatDate` 函数改成支持时区参数」。当模型决定先读取文件内容时，Agent Loop 调用 `streamText()` 发出请求，模型的响应以 chunk 流的形式逐片到达。本章将拆解这条流从“到达”到“被消费”的完整过程。本章将分为如下几个部分进行介绍。

- 10.1 节展示 `fullStream` 的 8 种 chunk 类型，说明 AI SDK 如何将多家供应商的协议差异统一为一种格式。
- 10.2 节讲解 `streamChunksToUI` 的分流逻辑——每类 chunk 被路由到哪里、为什么 `error` chunk 必须主动抛出。
- 10.3 节讨论推理 chunk 的取舍——为什么在 UI 层丢弃但 token 照样计费，以及 DeepSeek 等模型对 `reasoning_content` 字段的强制要求。
- 10.4 节分析工具进度上报的时序约束——`setProgressReporter` 为什么必须在 SDK 调用 `execute` 之前完成注册。
- 10.5 节说明排空（Drain）机制与异常处理——`drainStreamResult` 如何防止 `UnhandledPromiseRejection`，以及 `error` chunk 与 `NoOutputGeneratedError` 的关系。
- 10.6 节给出本章小结。

## 10.1 fullStream：统一的 chunk 流

> 源码：`packages/core/src/agent/loop.ts::streamChunksToUI`

回到示例：模型收到用户的请求后，决定先调用 `readFile` 工具查看 `src/utils.ts` 的内容。这个决策过程中，模型会先输出一段文字说明意图，然后发出一条工具调用指令。这些内容不是一次性返回的，而是以流式 chunk 的形式逐片到达客户端。AI SDK v6 的 `result.fullStream` 将所有供应商的流式协议归一为一种 chunk 格式，如表 10-1 所示。

| chunk 类型 | 含义 |
|---|---|
| `text-delta` | 模型生成的文本片段，每个可能只有 1 到 5 个字符 |
| `tool-call` | 模型决定调用一个工具，携带工具名称和参数 |
| `tool-result` | 自动执行工具的返回值（`execute` 函数的返回值） |
| `reasoning-start` | 推理段开始，thinking 模型独有 |
| `reasoning-delta` | 推理内容片段 |
| `reasoning-end` | 推理段结束 |
| `error` | 供应商返回错误响应，SDK 将错误包装为 chunk 入队后关闭流 |
| `finish` | 本轮结束，携带 `finishReason` 标记 |

> 表 10-1 AI SDK v6 fullStream 的 8 种 chunk 类型

Anthropic 用 content blocks 传递工具调用，OpenAI 用 `tool_calls` 数组，Google 用 `functionCall` 字段——三家的协议命名和嵌套结构各不相同，但 AI SDK 在内部完成了归一化处理。客户端只需要检查 `chunk.type`，不需要知道当前使用的是哪家供应商。这种归一化与 Ch.09 讨论的供应商注册表是同一种设计思路：将差异封装在适配层内部，让上层代码面对统一的接口。在示例的执行过程中，`fullStream` 推出的 chunk 序列大致如下。模型先输出若干 `text-delta` chunk 说明意图，然后发出一个 `tool-call` chunk 调用 `readFile({path: "src/utils.ts"})`。SDK 自动执行工具后，一个 `tool-result` chunk 携带文件内容返回。模型看到文件内容，再输出若干 `text-delta` chunk 给出修改方案，最后一个 `finish` chunk 标记本轮结束。

## 10.2 六类 chunk 的分流逻辑

`streamChunksToUI` 是消费流的核心函数，它在一个 `for await` 循环中逐个处理 chunk，根据类型将其路由到不同的回调。以下是简化后的核心结构。

<!-- check:ignore=punctuation -->
```ts
async function streamChunksToUI(
  result: StreamResult,
  callbacks: AgentCallbacks
): Promise<void> {
  for await (const chunk of result.fullStream) {
    if (chunk.type === 'error') {
      // SDK 在请求失败时不抛异常，而是将错误包装为 chunk 入队后关闭流。
      // 如果不在这里主动抛出，循环会「正常」结束，
      // 随后 await result.response 才 reject，
      // 将真实错误（如余额不足、鉴权失败）替换为 NoOutputGeneratedError
      throw chunk.error instanceof Error
        ? chunk.error
        : new Error(String(chunk.error))
    }
    if (chunk.type === 'text-delta') {
      callbacks.onTextDelta(chunk.text ?? '')
    } else if (chunk.type === 'tool-call') {
      // 注册进度通道（必须在 SDK 调用 execute 之前完成）
      if (chunk.toolCallId) {
        setProgressReporter(
          chunk.toolCallId,
          (msg) => callbacks.onToolProgress(chunk.toolCallId, msg)
        )
      }
      callbacks.onToolCall(chunk.toolCallId, chunk.toolName, chunk.input)
    } else if (chunk.type === 'tool-result') {
      const raw = typeof chunk.output === 'string'
        ? chunk.output
        : JSON.stringify(chunk.output ?? '')
      if (chunk.toolCallId) clearProgressReporter(chunk.toolCallId)
      callbacks.onToolResult(chunk.toolCallId, truncateToolResult(raw))
    }
    // reasoning-* chunk 在 UI 层不处理，仅写入 debugLog
  }
}
```

六类 chunk 的处理策略各不相同。（1）`error` chunk 必须立即抛出，将控制权交给外层 `try/catch`，原因在 10.5 节详细讨论。（2）`text-delta` chunk 传递给 `onTextDelta` 回调，由 UI 层累积到流式缓冲区并逐字渲染到终端（详见 Ch.26）。（3）`tool-call` chunk 触发两步操作：先注册进度上报通道，再通知 UI 显示工具开始执行，这两步的顺序不能颠倒，10.4 节会展开讨论。（4）`tool-result` chunk 清除该工具的进度通道，然后将工具返回值截断后传递给 UI——截断逻辑由 `truncateToolResult` 负责，控制单次工具结果的大小以避免撑满上下文窗口（详见 Ch.17）。（5）`reasoning-*` 系列 chunk（`reasoning-start`、`reasoning-delta`、`reasoning-end`）在 UI 层不做任何处理，仅写入调试日志。（6）`finish` chunk 同样不直接处理，流结束后由 `collectTurnResponse` 统一收集 `finishReason`、`usage` 等信息。

回到示例：当模型输出「我来看一下这个文件的内容」时，终端上的文字是逐字出现的——每个 `text-delta` chunk 可能只包含一两个字，UI 接收到后立刻渲染。随后模型发出 `readFile` 的 `tool-call` chunk，UI 显示工具图标和参数；SDK 自动执行 `readFile`，文件内容通过 `tool-result` chunk 返回，UI 显示工具执行结果。整个过程中，用户看到的是一个流畅的交互序列，背后是 `streamChunksToUI` 在逐个 chunk 地分流和派发。

## 10.3 推理 chunk 的取舍：丢弃的是画面，不是 token

支持推理（Reasoning）的模型——DeepSeek-V4、OpenAI o 系列、Claude extended thinking、Gemini 2.5 Pro——在输出最终答案之前，会先输出一段内部思考过程。这段思考以 `reasoning-start` → 若干 `reasoning-delta` → `reasoning-end` 的 chunk 序列到达客户端。如果直接渲染这些 chunk，用户会看到类似下面的输出：

```text
让我想想...用户问的是 X，我应该先 readFile...
等等，文件路径可能是 Y...
还是先 grep 一下...
```

这类自言自语式的内容大量增加了终端输出的噪声，干扰用户获取有效信息。X-Code CLI 选择在 UI 层丢弃所有 `reasoning-*` chunk，只在 `debugLog` 中保留记录（设置 `DEBUG_STDOUT=1` 时写入 `~/.x-code/logs/debug.log`）。

这里的「丢弃」仅指 UI 不渲染，不影响 token 的实际消耗。从输出 token（Output Token）的角度看，推理内容是模型已经生成的文本，供应商已经完成计费，客户端是否渲染不改变费用。从输入 token（Input Token）的角度看，AI SDK 仍然会通过 `response.messages` 将推理内容写回 `state.messages`，下一轮 API 请求会原样发送。DeepSeek-V4 要求每条 `assistant` 消息必须携带 `reasoning_content` 字段，如果缺失会导致下一轮请求直接返回 HTTP 400 错误。X-Code CLI 通过 `ensureReasoningContentParts` 函数主动补齐空字段来应对这一要求（详见 Ch.34）。这是一个纯粹的 UI 层决策：用户看不到模型的中间思考过程，但 token 消耗不变。选择丢弃的依据是，开启 thinking 模式的用户期望的是更高质量的最终答案，而非观看模型的思考过程。如果将来需要查看推理内容用于调试，调试日志中已有完整记录。

## 10.4 工具进度上报的时序约束

> 源码：`packages/core/src/tools/progress.ts`

回到示例：模型发出 `readFile` 的工具调用后，终端上会短暂出现一行状态文字「Reading src/utils.ts」，告知用户工具正在执行。这条进度消息由工具内部主动上报，经由一个全局的进度通道传递到 UI。本节讨论这条通道的实现，以及为什么注册时机必须精确。

进度上报依赖一个全局的 `reporters` Map，结构如下。

```ts
const reporters = new Map<string, ProgressReporter>()

export function setProgressReporter(toolCallId: string, fn: ProgressReporter) {
  reporters.set(toolCallId, fn)
}

export function reportProgress(toolCallId: string | undefined, message: string) {
  if (!toolCallId) return
  reporters.get(toolCallId)?.(message)
}

export function clearProgressReporter(toolCallId: string) {
  reporters.delete(toolCallId)
}
```

工具在 `execute` 函数的开头调用 `reportProgress(toolCallId, msg)` 上报进度，这个函数从 Map 中查找对应的回调并触发。回调由 Agent Loop 在收到 `tool-call` chunk 时写入 Map（`setProgressReporter`），工具执行完毕后清除（`clearProgressReporter`）。

注册顺序不能颠倒，图 10-1 展示了正确时序下各角色之间的交互。


![image.png](https://p9-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/ad8de41adbca45a5adfdf899ebce019f~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1097&h=524&s=47708&e=png&b=fefdfd)

> 图 10-1 进度注册的正确时序——步骤 2 必须在步骤 7 之前完成，否则首条进度消息丢失

在 `streamChunksToUI` 的代码中，处理 `tool-call` chunk 时先调用 `setProgressReporter`，再调用 `callbacks.onToolCall`：

<!-- check:ignore=punctuation -->
```ts
if (chunk.toolCallId) {
  // 步骤 1：将进度回调注册到全局 Map
  setProgressReporter(
    chunk.toolCallId,
    (msg) => callbacks.onToolProgress(chunk.toolCallId, msg)
  )
}
// 步骤 2：通知 UI 工具开始执行
callbacks.onToolCall(chunk.toolCallId, chunk.toolName, chunk.input)
```

这两步之间没有 `await`，是同步执行的。处理完这个 chunk 后，下一次 `for await` 将控制权交还给 SDK，SDK 会同步调用工具的 `execute` 函数。工具一进入 `execute` 就会调用 `reportProgress`——如果此时 Map 中还没有注册对应的回调，`reporters.get(toolCallId)` 返回 `undefined`，首条进度消息就会被静默丢弃，不会报错。具体表现是用户看到工具图标出现后，过了几秒才看到「Reading src/utils.ts」这条状态文字，中间有一段空白期。因此步骤 1 必须在步骤 2 之前完成，确保 SDK 调用 `execute` 的时刻 Map 中已经有了对应的回调。这条约束对所有使用 `reportProgress` 的自动执行工具都成立——它们的 `execute` 实现都是在 `try` 块的第一行就调用 `reportProgress`，首条进度消息与 SDK 调用 `execute` 几乎在同一个 tick 到达。

自动执行的工具中，有 6 个使用了 `reportProgress`，各自上报的进度消息如表 10-2 所示。

| 工具 | 进度消息 |
|---|---|
| `readFile` | `Reading <filePath>` |
| `glob` | `Matching <pattern>` |
| `grep` | `Searching for /<pattern>/` |
| `listDir` | `Listing <dirPath>` |
| `webFetch` | `Fetching <url>`（命中缓存时为 `Using cached copy`） |
| `webSearch` | `Searching: <query>` |

> 表 10-2 自动执行工具的进度消息

每个工具通常只在开始时上报一次进度，UI 以覆盖式更新显示最新一条。手动派发的工具（`writeFile`、`edit`、`shell`、`task`、MCP 工具）在 `tool-execution.ts` 中同样使用 `reportProgress`，但它们走的是另一条执行路径（详见 Ch.08）。其中 `shell` 比较特殊——启动时通过 `reportProgress` 上报一次「Running command...」，之后子进程的流式输出同时走两条通道：`onShellOutput` 回调将完整输出推送到 UI 的滚动区域，`reportProgress` 以每 50ms 节流的频率更新进度行，显示最新一行输出作为状态指示。

## 10.5 排空（Drain）与异常处理

> 源码：`packages/core/src/agent/stream-utils.ts::drainStreamResult`

`streamText()` 返回的结果对象上，除了 `fullStream` 迭代器之外，还挂载了四个独立的 Promise 属性——`response`、`usage`、`finishReason`、`toolCalls`——分别用于获取响应元数据、token 用量、结束原因和工具调用列表。本章将它们统称为结果 Promise（Result Promises），因为它们依附于同一个底层流：流正常结束时它们各自 resolve，流出错时它们同时 reject。问题在于：如果流在迭代过程中抛出错误，而这些结果 Promise 上没有预先注册错误处理器，Node.js 就会将它们视为未处理的拒绝（Unhandled Rejection），输出警告到 stderr，严重时甚至终止进程。排空（Drain）是指在消费流之前，预先为每个结果 Promise 附加一个空的错误处理器，使得流出错时这些 Promise 虽然被拒绝，但不会触发 Node.js 的未处理拒绝检测。函数名 `drainStreamResult` 中的 drain 取「排空隐患」之意。

<!-- check:ignore=punctuation -->
```ts
export function drainStreamResult(result: StreamResult): void {
  const noop = () => {}
  Promise.resolve(result.response).catch(noop)
  Promise.resolve(result.finishReason).catch(noop)
  Promise.resolve(result.usage).catch(noop)
  Promise.resolve(result.toolCalls).catch(noop)
}
```

排空操作必须在流消费之前执行。原因在于流的迭代随时可能抛出错误，而结果 Promise 的 `reject` 几乎与流的错误同时发生——如果此时尚未注册错误处理器，就会触发未处理拒绝 `UnhandledPromiseRejection`。排空是幂等的，不影响后续对这些 Promise 的正常消费：调用方仍然可以 await 它们来获取拒绝的错误对象，走正常的错误处理路径。在实际代码中，排空函数出现在三个位置：（1）流消费之前调用一次，防止迭代过程中的错误导致结果 Promise 未被处理；（2）流消费的异常捕获块中再调一次，处理流抛出错误后可能新产生的待定 Promise；（3）响应收集阶段的异常捕获块中再调一次，覆盖收集过程中的错误。三次调用确保错误在任何时机出现都不会遗漏。

错误 chunk 的处理与排空机制配合，构成了异常处理的完整链路。当供应商返回错误响应时，SDK 不会直接从迭代器抛出异常，而是将错误包装为一个 `error` chunk 入队，然后关闭流。如果消费函数不识别这种 chunk 并主动将其作为异常抛出，循环会以「正常结束」的方式退出，随后 await 结果 Promise 时才发现被 `reject`——但此时 SDK 已经将真实的错误信息（如余额不足、鉴权失败、内容违规）替换为一个泛化的「无输出」错误。主动抛出 `error` chunk 中携带的原始错误，可以让外层的错误分类器（Ch.11）拿到准确的错误类型，做出正确的重试或终止决策。以下是外层异常处理的简化结构。

<!-- check:ignore=punctuation -->
```ts
try {
  await streamChunksToUI(result, callbacks)
} catch (err) {
  drainStreamResult(result)
  if (isContextTooLongError(err)) {
    return handleContextTooLong(state, model, callbacks)
  }
  callbacks.onError(new Error(classifyApiError(err).message))
  return { kind: 'error' }
}
```

外层异常捕获块在收到错误后，首先判断是否属于上下文过长——如果是，则触发响应式压缩并重试当前轮（详见 Ch.31）；其余错误经分类后传递给 UI 层展示。除了 `error` chunk 之外，流的迭代还可能因其他原因中断。用户按 Esc 时中止信号会终止迭代器，这是不可重试的中断，Agent Loop 直接退出当前轮。供应商中途关闭连接会抛出连接错误，走错误分类路径。SDK 在既无文本输出也无错误 chunk 的边缘情况下会抛出「无输出」异常，同样走错误分类。所有这些异常最终都汇入同一个捕获块统一处理。

## 10.6 小结

本章拆解了 `fullStream` 从到达客户端到被消费完毕的完整过程。AI SDK v6 将 Anthropic、OpenAI、Google 三家供应商各不相同的流式协议归一为 8 种 chunk 类型，客户端只需检查 `chunk.type` 即可完成分流，不需要关心底层协议差异。`streamChunksToUI` 按类型将 chunk 分别路由到文本渲染、工具执行、进度上报等不同通道，其中 `error` chunk 必须主动抛出以保留原始错误信息，`reasoning-*` chunk 在 UI 层丢弃但 token 照样计费。工具进度上报依赖一个全局 Map，注册时机必须早于 SDK 调用 `execute`，否则首条进度消息会被静默丢弃。`drainStreamResult` 在流消费之前为所有 sibling Promise 附加空的 `.catch` 处理器，防止流错误时触发 `UnhandledPromiseRejection`，在实际代码中出现三次以覆盖所有错误时机。

下一章（Ch.11）将讨论 `classifyApiError` 的完整错误分类体系——本章的 `error` chunk 和异常只是将错误抛到了外层，如何根据错误类型决定重试、终止还是切换模型，是错误恢复章节的核心内容。
