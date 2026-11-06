# 第 7 章 Tool Use 协议：为什么 Agent Loop 必须是循环

上一章完成了本地环境搭建，读者手中已经有了一份可运行的 X-Code CLI 源码实例。从本章开始，我们进入第三部分「Agent Loop 引擎」。这一部分的核心问题只有一个：驱动 AI 编程助手反复调用工具、直到任务完成的那个循环，究竟是怎么工作的？

回答这个问题之前，需要先澄清一个前提——Agent Loop 之所以是循环，不是工程上的设计选择，而是大模型 API 协议层的硬性约束。本章不讲实现细节，而是建立关于工具调用（Tool Use）协议的心智模型，为 Ch.08 的实现做铺垫。本章将分为如下几个部分进行介绍。

- 7.1 节解释为什么大模型不能自己执行代码，以及工具调用从非结构化 prompt 到结构化协议的演进过程。
- 7.2 节用一个完整的 HTTP 请求与响应序列，展示工具调用循环在协议层面的具体形态。
- 7.3 节对比 Anthropic、OpenAI、Google 三家厂商在协议字段命名上的差异与共同骨架。
- 7.4 节说明 Vercel AI SDK 如何将这些差异抽象为统一接口，以及这种抽象的代价。
- 7.5 节梳理后续章节频繁出现的关键术语。
- 7.6 节讨论协议层的异步约束，以及 X-Code CLI 在工具派发策略上的具体选择。
- 7.7 节列举协议之外、但生产级 Agent 必须处理的工程问题。
- 7.8 节给出本章小结。

## 7.1 模型不能自己执行代码

大语言模型（Large Language Model, LLM）是纯文本生成器，没有副作用执行能力。即便模型在训练数据中见过 `fs.readFileSync` 的调用方式，它能做的也只是输出一段声称要调用该函数的文本——它无法真正打开文件系统、读取字节、返回结果。因此，当模型决定需要调用某个工具时，它唯一能做的事情是输出「我想调用 X，参数是 Y」，然后停下来。客户端（即运行 Agent Loop 的程序）必须解析这条请求、执行对应的工具、将执行结果作为新消息传回给模型，模型拿到结果后才能继续推理。这种「调用模型 → 模型停下 → 客户端执行 → 结果传回 → 再次调用模型」的往返，就是工具调用协议的本质。所有主流 AI 编程助手（Claude Code、Codex、Gemini CLI、opencode、X-Code CLI）都遵循这个模式。

> AI 知识背景：工具调用的演进
>
> 早期大语言模型只能输出自然语言。如果开发者想让模型调用外部接口，需要自己编写 prompt 教模型按某种格式输出，再用正则表达式解析。这种方式脆弱且容易出现格式错误。2023 年 6 月，OpenAI 在 `gpt-4-0613` 和 `gpt-3.5-turbo-0613` 上正式发布了函数调用（Function Calling）能力——将工具定义放进请求的独立字段，模型经过专门的微调，能够识别何时应当调用工具、如何构造参数，输出结构化的 JSON 调用而非自由文本。这是工具调用走向工业化的起点。此后 Anthropic 的 `tool_use`、Google 的 `functionCall` 都是同一思路的不同命名。 <!-- check:ignore-line -->
>
> 2024 年 11 月，Anthropic 提出了模型上下文协议（[Model Context Protocol, MCP](https://modelcontextprotocol.io/)），将工具定义从单一应用内拓展为跨应用的开放协议：任何符合 MCP 规范的工具服务器都能被任何 MCP 客户端（Claude Code、ChatGPT、Cursor 等）接入。MCP 现已成为工具生态的事实标准，2025 年 OpenAI 和 Codex 也全面跟进。本章讨论的工具调用协议是函数调用时代的核心协议；MCP 是它的网络化扩展，详见 Ch.37。

这种协议结构和 Web 服务器有一个可类比之处。所有 Web 服务器本质上都是 `while(true) { conn = accept(); handle(conn) }` 这样一个循环，Nginx 和手写的 HTTP 服务器在主干结构上没有区别。区别在于循环内部附加的工程能力——连接池、负载均衡、缓存、压缩、超时处理。Agent CLI 也一样，主干都是工具调用循环，区别在于循环内部附加的并发控制、提示词缓存（Prompt Caching）、上下文压缩（Context Compression）、循环护栏（Loop Guard）等工程能力。不同之处在于，Web 服务器的循环由外部请求驱动，每轮处理一个独立的连接；Agent Loop 的循环由模型输出驱动，每轮的输入是上一轮所有消息的累积——消息只增不删，上下文持续膨胀，这正是后续章节需要引入压缩和截断机制的根本原因。 <!-- analogy: agent-loop-event-loop -->

## 7.2 一次完整对话的请求与响应序列

将抽象的「循环」展开为具体的 HTTP 请求与响应，读者可以清楚地看到协议层面发生了什么。图 7-1 展示了一次完整对话中用户、客户端、模型、工具四个角色之间的交互时序。


![image.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/7b97b3eec8cb4454b3a9836c124ee475~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1073&h=768&s=80579&e=png&b=fffcfc)

> 图 7-1 工具调用协议的两轮往返——`stop_reason` 决定循环继续还是退出

以下示例使用 Anthropic 的协议格式，其他厂商的字段名不同但骨架一致（7.3 节会对比）。

### 7.2.1 第 1 轮：模型请求读取文件

客户端发送用户消息和工具定义：

```text
POST /v1/messages
{
  "messages": [
    { "role": "user", "content": "帮我看一下 src/utils.ts 里的 formatDate 函数" }
  ],
  "tools": [
    { "name": "readFile", "input_schema": {...} },
    { "name": "edit", "input_schema": {...} },
    ...
  ]
}
```

模型返回一个工具调用请求，然后停下来：

```text
{
  "content": "好的，我先读一下这个文件",
  "tool_use": [
    { "id": "tu_1", "name": "readFile", "input": { "filePath": "src/utils.ts" } }
  ],
  "stop_reason": "tool_use"
}
```

`stop_reason: "tool_use"` 是控制流的关键信号——它告诉客户端「模型还没有结束对话，它在等待工具执行结果」。客户端解析出这个信号后，执行 `readFile` 工具读取文件内容。

### 7.2.2 第 2 轮：工具结果回传与模型的最终回复

客户端将工具执行结果作为新消息追加到消息数组中，再次调用模型：

```text
POST /v1/messages
{
  "messages": [
    ...第 1 轮的 user 与 assistant 消息...,
    {
      "role": "user",
      "content": [
        {
          "type": "tool_result",
          "tool_use_id": "tu_1",
          "content": "1  export function formatDate(d: Date) { ..."
        }
      ]
    }
  ],
  "tools": [...]
}
```

模型拿到文件内容后不再请求工具，生成最终回复：

```text
{
  "content": "看完了，这个函数没有时区参数。我建议加一个可选参数...",
  "stop_reason": "end_turn"
}
```

`stop_reason: "end_turn"` 表示模型主动结束对话，客户端收到完整回答，循环退出。

这两轮往返中有三处不变量贯穿整个协议：

（1）每轮的 `messages` 数组持续追加，不删不改。第 2 轮的请求包含第 1 轮的全部消息加上新的工具结果——模型依赖完整的对话历史来维持上下文连贯性。这也是长对话中上下文窗口（Context Window）会被逐渐填满的原因。

（2）每轮的 `tools` 数组都要重新发送。模型不会「记住」上一轮有哪些工具可用，它在每轮请求中只能看到当前请求携带的工具定义。

（3）`stop_reason`（或其他厂商的 `finish_reason`）是控制流的唯一判据。值为 `tool_use` 表示循环继续，值为 `end_turn` 表示循环结束。客户端的分支逻辑完全由这个字段驱动。

## 7.3 主流厂商的协议差异

虽然语义一致，各厂商在字段名和消息结构上存在差异。以下分别列出 Anthropic、OpenAI、Google 三家的核心字段。

Anthropic 将工具调用和工具结果嵌入 `content` 数组，用 `type` 字段区分：

```ts
// 模型请求
content: [{ type: 'tool_use', id, name, input }]
// 客户端回传
content: [{ type: 'tool_result', tool_use_id, content }]
```

OpenAI 将工具调用放在独立的 `tool_calls` 字段，工具结果使用专门的 `role: 'tool'` 消息：

<!-- check:ignore=punctuation -->
```ts
// 模型请求
tool_calls: [{ id, type: 'function', function: { name, arguments } }]
// 客户端回传（独立的 role:'tool' 消息）
{ role: 'tool', tool_call_id, content }
```

Google 使用 `functionCall` 和 `functionResponse` 两个字段，不引入额外的 role：

```ts
// 模型请求
{ functionCall: { name, args } }
// 客户端回传
{ functionResponse: { name, response } }
```

字段命名不同、消息结构不同，但协议骨架完全一致：模型输出工具调用请求 → 停下等待 → 客户端执行工具 → 将结果回传 → 模型继续推理。如果客户端代码直接对接这三套协议，每接入一个新的供应商都需要重新编写流式解析、工具结果回传、错误处理的适配代码。这正是 Vercel AI SDK 要解决的问题。

## 7.4 Vercel AI SDK 的统一抽象

X-Code CLI 没有直接对接各厂商的原始 API，而是通过 Vercel AI SDK 将差异抽象为统一的接口。核心调用方式如下：

> 源码：`packages/core/src/agent/loop.ts`

<!-- check:ignore=code-source -->
```ts
import { streamText, tool } from 'ai'

const result = streamText({
  model,
  system,
  messages,
  tools: toolRegistry,
  maxRetries: 3,
})

for await (const chunk of result.fullStream) {
  // 处理流式数据块
}
```

`model` 参数接受任意供应商的 `LanguageModel` 实例（由 `@ai-sdk/anthropic`、`@ai-sdk/openai`、`@ai-sdk/google` 等适配包创建），`tools` 参数接受用 AI SDK 的 `tool()` 辅助函数定义的工具集，`tool()` 内部使用 Zod schema 描述参数类型并自动转换为各厂商要求的 JSON Schema 格式。`maxRetries: 3` 表示遇到网络错误或供应商临时故障时最多重试 3 次。客户端代码不需要知道当前连接的是 Anthropic 还是 OpenAI——流式数据块的格式、工具调用 ID 的关联、结束原因的取值都由 SDK 统一处理。

这种抽象的代价是：供应商特有的功能（例如 Anthropic 的 `cache_control` 提示词缓存控制、OpenAI 的 `reasoning_effort` 推理强度调节）无法通过统一接口直接表达，需要通过 `providerOptions` 字段透传。X-Code CLI 在 `providers/cache-control.ts` 和 `providers/thinking.ts` 中维护了这些供应商特有参数的统一映射（详见 Ch.33 和 Ch.34）。这是选择统一抽象层必然面对的取舍：覆盖 80% 的通用场景几乎零成本，但剩余 20% 的供应商特有能力需要额外的适配代码。

## 7.5 关键术语

以下术语在后续章节中频繁出现，这里统一给出定义：

- `tool_call`——模型输出的工具调用请求，包含工具名称和参数。AI SDK 内部使用驼峰命名 `toolCall`。
- `tool_result`——客户端执行工具后回传的结果，必须按 `tool_call_id` 与对应的请求配对。AI SDK 内部使用 `toolResult`。
- `tool_call_id`——每次工具调用的唯一标识符。客户端回传结果时必须携带与请求相同的 ID，ID 错位会导致模型混淆上下文。AI SDK 内部使用 `toolCallId`。
- `finishReason`——本轮模型输出的结束原因。AI SDK 将各厂商的取值归一化为 6 个枚举值：`stop`（模型主动结束）、`tool-calls`（请求执行工具，循环继续）、`length`（达到 `max_tokens` 截断）、`content-filter`（供应商内容审核拦截）、`error`（SDK 未能明确分类的异常）、`other`（自定义供应商的扩展状态）。其中前 4 个是常见取值，后 2 个在应用层通常归入「终止循环并报错」的分支。
- `inputSchema`——工具参数的 [Zod](https://zod.dev/) schema，模型据此构造调用。X-Code CLI 中每个工具的 `inputSchema` 定义在 `packages/core/src/tools/<name>.ts` 中。
- `execute`——工具的实际执行函数。AI SDK 在收到模型的工具调用请求后会自动调用 `execute`。但在 X-Code CLI 中，只有不涉及副作用或权限确认的只读工具才提供 `execute`，由 SDK 自动执行；写操作和状态变更类工具故意不提供 `execute`，由 Agent Loop 手动派发——这样做是为了在执行前接入权限检查、循环检测和状态写入。具体哪些工具属于哪一类，详见 Ch.08 和 Ch.12。

## 7.6 协议层的异步约束与工具派发策略

模型在一次响应中可以同时发出多个 `tool_call`（例如「我想同时读取 3 个文件」），客户端可以并行执行。但协议层有三条硬约束必须遵守：

（1）每个 `tool_result` 必须按 `tool_call_id` 与对应的请求配对。少一个结果，模型会拒绝整轮请求（返回 HTTP 400）。

（2）只有当本轮所有 `tool_call` 都收到了对应的 `tool_result` 之后，才能进入下一轮调用。这意味着客户端即使并行执行了多个工具，也必须等全部完成后再将结果一起传回。

（3）工具定义（`tools` 数组）必须每轮重新发送。模型不会缓存上一轮的工具列表。

在这些约束下，X-Code CLI 的派发策略做了一个明确的区分：只读工具由 AI SDK 在流式阶段自动执行，已执行的调用在后续手动派发阶段被过滤跳过，避免重复执行；写操作和状态变更类工具由 Agent Loop 逐个串行派发，原因在于它们会修改文件系统、向终端输出流式内容或弹出 UI 对话框，并发执行会产生竞态条件。唯一的并发例外是子 Agent（`task`）工具——模型在一轮中连续发出的多个子 Agent 调用会被归入同一批次并行执行。这样做是安全的，因为每个子 Agent 持有完全独立的循环状态（消息历史、权限模式等各自一份），不与父循环共享可变状态。派发策略的完整实现详见 Ch.08。

## 7.7 协议外的工程考量

协议本身只规定了「如何调用工具」和「如何传回结果」。但生产级 Agent 必须处理一系列协议之外的问题：

- 工具执行结果可能非常大（例如读取一个万行文件），直接传回会占满上下文窗口。需要截断机制将结果压缩到合理范围（详见 Ch.17）。
- 工具执行可能失败（文件不存在、命令报错、网络超时）。错误信息必须以结构化的方式传回，让模型能够据此决定重试、换一个工具、或向用户说明情况。
- 模型可能对同一工具用同一参数反复调用——例如反复读取同一个文件却不做任何修改。这种无意义的死循环（Doom Loop）需要循环护栏机制来检测和中断（详见 Ch.32）。
- 多轮对话中消息数组持续膨胀，最终会超出模型的上下文窗口限制。需要上下文压缩机制在不丢失关键信息的前提下缩减消息量（详见 Ch.31）。

这些问题都不在协议层面，是上层 Agent 的责任。Ch.08 实现 Agent Loop 时会逐一处理。

## 7.8 小结

本章建立了关于工具调用协议的心智模型。大语言模型是纯文本生成器，没有副作用执行能力，因此当模型决定需要调用工具时，它只能输出请求并停下来，等待客户端执行工具并将结果传回。这种往返构成了 Agent Loop 必须是循环的根本原因。在协议层面，每轮消息持续追加、工具定义每轮重发、结束原因（`finishReason`）驱动控制流转——这三处是不变量。各厂商（Anthropic、OpenAI、Google）的字段命名不同但骨架一致，Vercel AI SDK 通过 `streamText` 加 `tool()` 将差异抽象为统一接口，代价是供应商特有功能需要通过 `providerOptions` 透传。在工具派发层面，X-Code CLI 区分了三类工具：只读工具由 AI SDK 自动执行、写操作和状态类工具由 Agent Loop 手动串行派发、子 Agent 工具归批后并行分发。下一章（Ch.08）将进入 Agent Loop 的完整实现，把本章讨论的协议骨架和派发策略落地为可运行的代码。


