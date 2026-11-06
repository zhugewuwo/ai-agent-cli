# 第 1 章 开篇示例：50 行代码跑通一个 Agent

后续章节将从工程基础一直讲到扩展生态，完整拆解一个生产级 AI 编程助手（AI Coding Agent）的内部实现。但在进入工程细节之前，本章先用 50 行 TypeScript 代码搭建一个最小可运行的智能体（Agent），让读者直接看到它在终端中的实际形态——如何接收自然语言、如何决定调用哪个工具、如何将结果流式输出给用户。对于初次接触 AI Agent 的读者，一个常见疑问是：实现这样的助手，是否需要训练一个专用的模型？答案是不需要。模型由 OpenAI、Anthropic、Google、DeepSeek 等供应商提供，开发者通过 API 调用即可获得推理能力。让模型从「能回答问题」到「能实际执行操作」，所依赖的是模型外面包裹的一层工程系统。这层系统负责驱动模型多轮调用工具、在终端中流畅渲染输出、在出错时自动恢复、在遇到危险操作时拦截确认。这层系统的质量，直接影响一个 Agent 能否从「能运行」走向「敢交给用户使用」。

本章的任务是用一份 50 行的最小示例搭建骨架，让读者先看见 Agent 在终端中的实际形态，再从这 50 行代码出发，明确它与生产级 Agent 之间的差距，以及后续 39 章各自要补足哪些能力。读者可以将这份「最小 Agent 与生产级 Agent 之间的差距」作为参照，进入后续章节时，每一章要解决什么问题就会清晰许多。本章将分为如下几个部分进行介绍。

*   1.1 节通过一个具体场景说明 Agent 与传统对话的区别——关键变化不是「用自然语言写代码」，而是 Agent 能在用户的真实文件系统上使用工具。
*   1.2 节交代如何获取代码并在本地运行，包含环境准备与一次完整的交互示范。
*   1.3 节对 50 行代码进行逐段解读，将其拆解为导入、工具定义、REPL 循环、Agent 核心驱动四个部分。
*   1.4 节给出一次完整对话的执行时序图，将代码中的每一行与实际运行过程一一对照。
*   1.5 节用两张对照表分别整理这 50 行代码做了什么、没做什么，后者恰好构成后续章节的工程清单。
*   1.6 节将 50 行代码与后续章节的具体机制做映射，说明本书所有内容都是在这份骨架上逐步填入具体实现。
*   1.7 节给出本章小结。

## 1.1 从一个具体场景说起

假设我们正在一个 Node.js 项目中工作，需要查看 `package.json` 里有哪些依赖，再确认项目是否声明了 `main` 字段。如果手工完成，步骤是：打开 `package.json`，阅读文件内容，找到 `dependencies` 和 `main` 字段。操作本身不复杂，但如果将同样的事情交给一个 AI Agent，场景会变成这样：在终端中输入「帮我看看 package.json 里有哪些依赖」，Agent 自动调用 `readFile` 工具读取文件内容，再用自然语言将依赖列表整理给用户。接着问「这个项目的 main 字段是什么」，Agent 无需再次读取文件——上一轮的文件内容仍然保留在对话历史中——直接回答：`main` 字段没有显式声明，npm 会默认回退到 `index.js`。整个过程不需要离开终端，也不需要手工打开文件。

在这个场景中，关键变化并非「用自然语言回答问题」——这件事 ChatGPT 早已能做到。关键变化在于 Agent 能够在用户的真实机器上使用工具：它读取了真实的文件、操作了真实的文件系统、结果来自真实的磁盘数据。模型只是决策中枢，负责判断「现在是否需要调用工具、调用哪个工具、传什么参数」；真正产生效果的是模型背后的工具调用。这个过程的核心机制可以用一句话概括：模型主动决定是否调用工具、调用哪个工具、调用几次、何时停止调用并开始回答。这就是 Agent 的核心循环（Agent Loop）——给模型一组工具，由模型自行决定如何使用，外部系统负责执行工具并将结果回传给模型。后续 39 章要讲清楚的，正是这个「外部系统」的全部实现细节。

先看实际的运行效果。下面这段录屏是 50 行代码在终端里与模型对话、调用工具读取文件的过程：


![demo.gif](https://p6-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/cc89ed3f153642a4b1ccbf8600c43974~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1327&h=628&s=127158&e=gif&f=101&b=282922)

## 1.2 拿到代码与本地运行

本节的示例代码已放在配套仓库 [github.com/woai3c/hello-agent](https://github.com/woai3c/hello-agent) 中。读者按以下步骤将其克隆到本地、安装依赖即可运行。运行环境需要 Node.js 18 或更高版本，以及 npm 包管理器。

```bash
git clone https://github.com/woai3c/hello-agent.git
cd hello-agent
npm install
```

启动前需要一个模型 API Key。本示例使用 DeepSeek，在国内访问稳定、价格较低。在 [platform.deepseek.com](https://platform.deepseek.com) 注册后到「API Keys」页面创建一个 key，按操作系统设置环境变量：

```bash
# macOS / Linux
export DEEPSEEK_API_KEY=sk-...

# Windows PowerShell
$env:DEEPSEEK_API_KEY = "sk-..."
```

设置完毕后执行 `npm start`，终端会出现一个 `你:` 提示符。输入问题即可看到 Agent 的交互效果：

```text
你: 帮我看看 package.json 里有哪些依赖
助手:
  [调用 readFile({"path":"package.json"})]
  [返回 N 字节]
助手: 你的 package.json 里有以下依赖：ai、@ai-sdk/deepseek、zod ...

你: 这个项目的 main 字段是什么？
助手: 你的 package.json 里没有显式声明 main 字段，npm 会默认回退到 index.js。

你: exit
```

在这个交互中，模型主动决定了是否调用 `readFile`、何时调用、调用后何时开始回答。用户没有告诉模型「先读文件再回答」，这个调用策略是模型根据工具描述和用户意图自行选择的。这正是 Agent Loop 最基本的形式。

## 1.3 完整代码逐行解读

`hello-agent.ts` 的完整内容如下，约 50 行。

<!-- check:ignore=punctuation -->

```ts
// hello-agent.ts —— 一个最小可运行的 AI agent
//
// 配套小册《从零打造一个 AI Agent CLI》开篇示例。
// 50 行跑通：streamText + 一个 readFile 工具 + 多轮对话循环。
//
// 依赖：ai @ai-sdk/deepseek zod
// 启动：npx tsx hello-agent.ts

import { deepseek } from '@ai-sdk/deepseek'
import { streamText, stepCountIs, tool } from 'ai'
import { z } from 'zod'
import fs from 'node:fs/promises'
import readline from 'node:readline'

// 给模型一个工具：读取一个文件的全部内容
const readFile = tool({
  description: '读取一个文本文件，返回完整内容',
  inputSchema: z.object({
    path: z.string().describe('要读取的文件路径'),
  }),
  execute: async ({ path }) => {
    return await fs.readFile(path, 'utf-8')
  },
})

async function main() {
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout })
  const ask = (q: string) => new Promise<string>((r) => rl.question(q, r))
  const messages: Array<{ role: 'user' | 'assistant'; content: any }> = []

  for (;;) {
    const input = (await ask('\n你: ')).trim()
    if (!input || input === 'exit') break
    messages.push({ role: 'user', content: input })

    const result = streamText({
      model: deepseek('deepseek-chat'),
      messages,
      tools: { readFile },
      stopWhen: stepCountIs(10), // 最多 10 轮工具调用后停止
    })

    process.stdout.write('助手: ')
    for await (const chunk of result.fullStream) {
      if (chunk.type === 'text-delta') process.stdout.write(chunk.text)
      else if (chunk.type === 'tool-call') process.stdout.write(`\n  [调用 ${chunk.toolName}(${JSON.stringify(chunk.input)})]`)
      else if (chunk.type === 'tool-result') process.stdout.write(`\n  [返回 ${String(chunk.output).length} 字节]\n助手: `)
    }

    const { messages: newMessages } = await result.response
    messages.push(...(newMessages as any))
  }
  rl.close()
}

main().catch(console.error)
```

这段代码按职责可以分为四个部分。

第一部分（前 5 行）是导入语句。`@ai-sdk/deepseek` 是 Vercel AI SDK 为 DeepSeek 模型提供的适配器（Adapter），负责屏蔽不同供应商的 API 差异，对外提供统一接口。`ai` 包是 SDK 主体，导出了三个核心 API：`streamText` 用于向模型发起流式请求，`stepCountIs` 用于设定工具调用轮数上限，`tool` 用于定义工具。`zod` 是 TypeScript 生态中使用最广泛的运行时校验库，这里用来为工具参数定义参数模式（Schema）。选择 Vercel AI SDK 而非直接调用 DeepSeek 的 HTTP API，原因在于 SDK 屏蔽了供应商差异：后续如果要切换到 OpenAI 或 Anthropic，只需替换一行 `model: deepseek(...)` 为 `model: openai(...)`，其余代码不动。这一层抽象在 Ch.09 多供应商支持一节会展开讨论。

第二部分（`tool({...})` 代码段）定义了 Agent 唯一的工具 `readFile`。每个工具包含三个要素：一段给模型阅读的自然语言描述（`description`），一组用 Zod 定义的参数模式（`inputSchema`），以及一个真正执行操作的函数（`execute`）。描述决定了模型在什么场景下会选用该工具——如果描述写得过于笼统，模型可能在不需要读取文件时也调用它；如果写得过于限制，模型可能在需要读取文件时却跳过它。参数模式约束了调用时的参数格式，如果模型传入不合法的参数，SDK 会直接拒绝并触发错误。执行函数决定了调用后实际发生的操作。这三个要素的组合在后文中称为工具使用协议（Tool Use Protocol），Ch.07 会展开讨论。选择这种声明式的定义方式而非让模型直接执行代码，原因在于安全性：模型永远不直接执行代码，所有副作用都由工具的 `execute` 函数代理完成，因此每个可能产生影响的操作都在开发者的控制之下。这一设计的代价是灵活性受限——模型无法执行工具集之外的任何操作，但正是这种限制构成了权限控制（Ch.29）的基础。

第三部分（`main` 函数中 `for` 循环的外层）是一个简单的交互式读取-求值-打印循环（REPL，Read-Eval-Print Loop）。通过 Node.js 的 `readline` 模块读取用户输入，将每条输入追加到 `messages` 数组中，构建对话历史。这个 `messages` 数组是模型每一轮所看到的完整上下文——包含之前所有的用户输入、模型回复、工具调用记录和工具返回结果。每一轮用户输入到来时，`messages` 中的信息量都比上一轮多，模型因此能够「记住」之前的对话内容。这份简单的数组在生产级 Agent 中会演化为一个名为 `LoopState` 的结构（Ch.08），增加对话过长时的自动压缩、消息序列出错时的自动修复等机制，但其本质仍然是「维护一份模型可见的完整上下文」。

第四部分（`streamText({...})` 及其后的 `for await` 循环）是 Agent 的核心驱动。`streamText` 将当前 `messages` 与工具集发送给模型，SDK 在内部驱动一个「模型输出文本 → 模型发出工具调用 → SDK 执行工具 → 工具结果回传给模型 → 模型继续输出」的循环，直到模型决定停止或命中 `stopWhen` 所设定的上限（此处为 10 轮）。`fullStream` 是这个循环的事件流（Event Stream），其中每一段文本、每一次工具调用、每一次工具返回都作为一个数据块（Chunk）依次推出，我们用 `for await` 逐个消费，按 `chunk.type` 分别写到终端。最后，`result.response.messages` 取出 SDK 在本轮中累积的所有新消息——包括模型输出的文本、工具调用记录、工具返回结果——追加到 `messages` 数组中。下一轮用户输入到来时，模型就能看到完整的对话历史。整个流程中，SDK 帮我们封装了「执行工具 → 回传结果 → 再次请求模型」这一循环，我们只需消费 `fullStream` 并维护 `messages`。Ch.08 将展开这层封装，分析 SDK 承担了哪些工作、哪些工作必须由我们自己实现。

## 1.4 一次完整对话的执行轨迹

为了更清晰地展示上述循环的运行过程，本节给出一次完整对话的执行时序。当用户输入「package.json 里有哪些依赖」时，用户、SDK、模型、工具四个角色之间的交互如图 1-1 所示。


![image.png](https://p9-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/eb2ec7e279814303bd7414025f8f9f07~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1004&h=932&s=85740&e=png&b=fffdfd)

> 图 1-1 Hello Agent 一次完整对话的请求/响应时序

将图中的每一步与代码对照来看，一次完整对话的流程如下。

（1）用户输入「package.json 里有哪些依赖」，该输入被追加到 `messages` 数组中。

（2）`streamText` 发起第一次 API 请求，将 `messages` 连同 `tools` 描述一起发送给模型。模型在这次请求中看到了工具的 `description` 和 `inputSchema`，因此知道自己可以调用一个名为 `readFile` 的工具来读取文件。

（3）模型判断需要查看文件内容才能回答这个问题，于是返回一个工具调用指令（`tool_call`）：`readFile({"path": "package.json"})`。SDK 将这个 Chunk 推送到 `fullStream`，主循环将其打印到终端，用户看到 `[调用 readFile({"path":"package.json"})]`。

（4）SDK 调用 `readFile.execute({path: 'package.json'})`，Node.js 读取本地 `package.json` 文件，返回文件全文。

（5）SDK 发起第二次 API 请求。在原有 `messages` 基础上追加了刚才的 `tool_call` 和 `tool_result`（工具返回结果），此时模型已经能看到文件的完整内容。

（6）模型输出一段自然语言回复（「你的 package.json 里有以下依赖……」），以若干 `text-delta` Chunk 的形式流式推回。SDK 将这些 Chunk 推送到 `fullStream`，主循环逐字打印到终端。

（7）模型输出完毕（`finishReason: stop`），SDK 关闭流，主循环跳出 `for await`，等待下一次用户输入。

在整个过程中，我们的代码只编写了一个 `for await` 循环和一次 `messages.push`。「模型调用工具 → 执行工具 → 将结果回传给模型 → 模型继续输出」这一完整循环由 SDK 封装完成。这个过程的本质可以抽象为一个 while 循环，如图 1-2 所示。


![image.png](https://p3-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/3641956b83bc40c5830e49bb04758fac~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1100&h=254&s=33041&e=png&b=ffffff)

> 图 1-2 Agent Loop 的本质——一个 while 循环驱动的「决策 → 执行 → 反馈」闭环

图 1-2 揭示了 Agent Loop 最基本的形态：用户输入追加到 `messages`，由 `streamText` 发给模型；模型如果决定调用工具，SDK 执行工具并将结果追加回 `messages`，然后再次请求模型；模型如果决定用文本回复，流式输出到终端后等待下一轮输入。整个循环的控制权始终在模型手中——模型决定每一步是调用工具还是直接回答，外部系统只负责执行工具和维护 `messages`。这也是为什么这类系统被称为 Agent 而非脚本（Script）：执行路径不是预先编排好的，而是由模型在运行时动态决定。

## 1.5 这 50 行代码做了什么、没做什么

### 1.5.1 已完成的职责

将这 50 行代码按职责整理，可以看出它已经达到了「最小可用」的水平。表 1-1 将代码中的每个关键片段与其承担的职责和后续章节中的对应概念一一列出。

| 关键代码                              | 职责                                                  | 对应概念                            |
| --------------------------------- | --------------------------------------------------- | ------------------------------- |
| `tool({...})`                     | 定义工具：名称、描述、参数 Schema、执行函数                           | 工具使用协议（Tool Use Protocol，Ch.07） |
| `messages: [...]`                 | 维护对话历史，每轮追加用户输入与模型回复                                | Agent Loop 状态（Ch.08）            |
| `streamText({...})`               | 将对话历史与工具集发送给模型，由 SDK 驱动多轮 tool\_call / tool\_result | Agent Loop 主驱动（Ch.08）           |
| `stopWhen: stepCountIs(10)`       | 最多 10 轮工具调用后停止                                      | 粗粒度循环护栏（Loop Guard，Ch.32）       |
| `for await (chunk of fullStream)` | 流式消费模型输出，按类型分发 text-delta / tool-call / tool-result | 流式 Chunk 处理（Ch.10）              |
| `result.response.messages`        | 取出本轮 SDK 累积的所有新消息，追加到对话历史                           | 工具结果回传（Ch.08、Ch.17）             |

> 表 1-1 50 行代码的职责分解

### 1.5.2 缺失的能力：后续章节的工程清单

更重要的是看清这 50 行没有做什么。表 1-2 将缺失的能力、缺失导致的后果与对应的章节并排列出。这份清单恰好就是后续 39 章要逐项补齐的工程内容。

| 缺失能力                     | 后果                                                                                      | 对应章节                                                                     |                            |
| ------------------------ | --------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ | -------------------------- |
| 没有终端 UI                  | 只能使用裸 readline，多行输入、光标移动、@ 文件补全等均不支持                                                    | 第四部分（Ch.23-28 终端 UI）                                                     |                            |
| 没有权限确认                   | 模型若要执行 shell 命令或删除文件，会直接执行，无「请确认这个操作」的环节                                                | Ch.29 三级权限模型                                                             |                            |
| 没有错误恢复                   | 网络波动、模型返回非法工具参数、长输出超出上下文（Context）上限，任意一处出错整个会话都将中断                                      | Ch.11 错误恢复、Ch.17 工具结果处理                                                  |                            |
| 只支持一家供应商                 | 接入 OpenAI、Anthropic、Gemini 等其他供应商均需重写代码，各家的缓存控制（Cache Control）、思考模式（Thinking）等字段差异需单独适配 | Ch.09 多供应商支持                                                             | <!-- check:ignore-line --> |
| 只有一个工具                   | 文件读写、Shell 执行、grep、列目录、网页抓取等常用能力均不具备                                                    | 第三部分（Ch.12-22 工具系统）                                                      |                            |
| 不会上下文压缩                  | 多轮对话后 messages 持续增长，几十轮即可占满 200K 上下文窗口（Context Window）                                  | Ch.31 上下文压缩                                                              |                            |
| 没有提示词缓存                  | 每一轮系统提示词、对话历史都全量重新计算 token，API 费用与延迟均大幅增加                                               | Ch.33 提示词缓存（Prompt Caching）                                              |                            |
| 没有循环护栏                   | 模型一旦陷入「调用失败 → 重试 → 再次失败」的死循环，唯一的防线只有 `stepCountIs(10)` 这个粗粒度上限                          | Ch.32 循环护栏（Loop Guard）                                                   |                            |
| 没有子 Agent、计划模式、知识系统、会话恢复 | 高级模型能力无法启用，复杂任务无法拆分委派，上一次会话的进度无法恢复                                                      | Ch.19-22（子 Agent / todoWrite / Plan Mode）、Ch.34-36（思考模式 / 多模态 / 知识与会话系统） |                            |

> 表 1-2 50 行代码的缺失清单——每一行对应后续章节要补足的一项工程能力

两张表并列来看，可以得出一个贯穿全书的结论：demo 与一个生产级 AI 编程助手之间，缺的不是大语言模型（LLM，Large Language Model）的推理能力，而是工程层面的细节。UI 不抖动、权限不缺失、错误可恢复、token 不溢出、模型不陷入死循环、可切换供应商、可处理复杂任务——这些全部需要模型外面那层工程系统来保障。这也是本书反复强调的一个观点：让模型从「能运行」到「敢使用」，所缺的不是模型能力，而是模型外面的那层工程。

图 1-3 将这些缺失能力按类别归组，直观展示了从 demo 到生产级 Agent 之间需要补足的工程模块全景。


![image.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/9a2f8fd8f89c44588e8bd8540465b086~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1091&h=831&s=62115&e=png&b=fff7f1)

> 图 1-3 从 demo 到生产级 Agent——需要补足的工程模块全景

## 1.6 这份代码就是后续章节的骨架

建议读者将这 50 行代码 `hello-agent.ts` 保留在手边。后续每一章讲到新机制时，都可以回头对照这份骨架，看清新机制替换的是骨架中的哪一部分。本书后续所有内容都是在这 50 行代码上逐步填入具体实现。图 1-4 将 50 行代码中的每个关键部分与后续章节的具体替换做了一一对照。


![image.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/94705c24c8b64b27b60e3db8065ef867~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=730&h=683&s=51963&e=png&b=fef7e7)

> 图 1-4 50 行骨架到生产级的替换映射——后续每一章都在填充这份骨架

从图 1-4 中可以看出，替换是逐步进行的，而非一次性推倒重写。具体来说，这些替换分为如下几个方向。

（1）`streamText` 的 `for await` 循环会演化为自研的 `agentLoop` 加流事件分发（Ch.08、Ch.10），以支持更细粒度的事件处理与 UI 回调。

（2）`messages` 数组会演化为 `LoopState` 结构，增加截断策略与 `tool_call` / `tool_result` 配对修复机制（Ch.08、Ch.17），以应对消息序列在压缩、中断、错误恢复后可能出现的不一致。

（3）`tools: { readFile }` 会扩展为 13 个静态工具加 `task` 动态注入加子 Agent 分发（Ch.12-19），覆盖文件读写、Shell 执行、grep、网页抓取、任务拆分等场景。

（4）`process.stdout.write` 会被替换为基于 cell-level diff 的终端渲染（Ch.24、Ch.25），以消除 CJK 宽字符导致的抖动问题和流式追加导致的闪烁。

（5）`tool({ execute })` 的直接调用会加入「先权限确认、再调用」的流程（Ch.18、Ch.29），将工具按危险程度分为三级，由用户决定是否放行。

（6）`stopWhen: stepCountIs(10)` 会被替换为基于工具调用指纹（Fingerprint）的循环护栏（Loop Guard，Ch.32），通过对工具名加标准化参数计算哈希来检测重复调用，而非仅靠轮数上限。

下一章（Ch.02）将把视角提升一层：用「提示词工程（Prompt Engineering）/ 上下文工程（Context Engineering）/ 驾驭工程（Harness Engineering）」三个层级给后续 38 章的每个机制做一次工程定位。这份 50 行骨架本身落在哪一层、要补的细节又分别落在哪几层，带着这张地图再读后续章节，会更容易看出每个机制要解决的是哪一层的问题。

## 1.7 小结

本章用 50 行 TypeScript 代码实现了一个最小可运行的 Agent，让读者直接看到了 Agent 在终端中的实际形态。这 50 行完成的工作可以归纳为四件事：用 `tool({...})` 声明工具的三要素（描述、参数模式、执行函数），用 `messages` 数组维护对话历史，用 `streamText` 的 `for await` 循环流式消费模型输出，用 `result.response.messages` 将新消息追加回历史。四件事合在一起构成了 Agent Loop 最基本的形态：一个 while 循环驱动的「用户输入 → 模型决策 → 工具执行 → 结果回传 → 模型继续」闭环。

与此同时，本章也列出了这 50 行未能做到的 9 项能力（表 1-2），它们恰好构成了后续 39 章的工程清单。这些缺失中没有一项是 LLM 能力不足导致的，全部是工程层面需要编写的代码。这也验证了一个核心判断：让模型从「能运行」到「敢使用」，所缺的不是模型能力，而是模型外面的那层工程。

本章最后给出的骨架映射（图 1-4）建立了一个可以贯穿全书的参照：后续每一章讲到的机制，都可以回到这份 50 行代码中找到它替换的位置。建议读者在进入下一章之前，将 `hello-agent.ts` 克隆到本地实际运行，对 Agent 的交互形态建立直观感受。带着这份感受再进入后续章节的工程细节，阅读体验会更为连贯。

接下来的 Ch.2-5 是全书的导航层：Ch.2 建立三层架构框架，Ch.3 梳理七类失败模式并将每类失败对应到具体的防御章节，Ch.4 介绍项目结构与技术选型，Ch.5 走通 CLI 的完整启动流程。这四章不写实现代码，它们的作用是帮读者建立全局心智模型——在动手之前先知道每个机制为什么存在、在系统中处于什么位置。从 Ch.7 开始，我们将逐章动手构建 Agent Loop 引擎及其周边机制，届时这四章建立的全局索引会持续发挥作用：遇到任何一个实现细节，都可以回到这几张表中定位它在整个系统里的坐标。
