# 第 5 章 CLI 启动流程

上一章交代了 X-Code CLI 的双包结构和模块地图，读者已经知道 core 和 cli 各自负责什么。本章进入 cli 包的第一个运行时话题：从用户在终端输入 `xc` 到屏幕上出现交互界面，中间究竟发生了什么？这个过程并不复杂，`main()` 函数（`packages/cli/src/index.ts`）按固定顺序做四组事情，每一组只解决一类问题，前一组的输出是后一组的输入。之所以需要理解这条流水线，是因为后续章节讨论的每一个运行时机制（模型切换、权限模式、会话恢复、上下文压缩等）都需要在启动阶段完成初始化，读者需要知道它们各自在流水线的哪个位置被装配进来。本章将分为如下几个部分进行介绍。

*   5.1 节给出四步流水线的总览，用一张流程图展示全局结构。
*   5.2 节讲参数与环境——Node 版本检查、环境变量加载、命令行参数解析、管道输入读取。
*   5.3 节讲模型与供应商解析——模型标识符的四级优先级链、供应商探测顺序、三种切换模型的方式。
*   5.4 节讲 AgentOptions 的构建——将前面步骤的产物汇总成一个配置对象，传给 Agent Loop。
*   5.5 节讲 UI 的启动——启动横幅的直写策略、Ink 挂载与 ChatInput 接管、useAgent 桥接。
*   5.6 节讲非交互模式（`--print`）的独立路径。
*   5.7 节给出本章小结。

## 5.1 四步流水线总览

`main()` 的内部结构是一条四步流水线，如图 5-1 所示。第一步解析命令行参数并加载环境变量，确定运行环境是否满足要求以及用户给了什么输入；第二步根据参数和环境变量解析出要使用的模型与供应商；第三步将前两步的产物汇总成一个 `AgentOptions` 对象；第四步用这个对象启动终端界面。非交互模式（`--print`）在第三步之后分叉，走完全独立的路径，不进入 Ink 渲染。


![image.png](https://p9-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/b324a44ac9f24915bc629147f0f8ac1c~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=475&h=889&s=42779&e=png&b=ffffff)

> 图 5-1 main() 的四步流水线与 --print 分叉

四步之间是严格的顺序依赖：步骤 2 需要步骤 1 解析出的 `--model` 参数才能确定模型标识符，步骤 3 需要步骤 2 创建的供应商注册表才能组装配置对象，步骤 4 需要步骤 3 的 `AgentOptions` 才能初始化 Agent 状态。任何一步失败（例如 Node 版本过低、没有任何供应商配置了 API Key），流水线会在该步骤直接终止并输出错误提示，不会继续往下执行。

## 5.2 参数与环境

`main()` 开头依次完成四件事：检查 Node 版本、加载环境变量文件、解析命令行参数、读取管道输入。这四件事的共同目标是确定运行环境是否满足要求，以及用户通过命令行和管道传入了什么信息。

### 5.2.1 Node 版本检查

`checkNodeVersion` 要求 Node 版本不低于 20.19.0，不满足则直接调用 `process.exit(1)` 终止进程并提示用户升级。版本比较的实现是手写的三段数字比较（major、minor、patch），将 `process.versions.node` 按 `.` 拆分后逐段对比，没有引入 `semver` 这类第三方依赖。选择 20.19.0 作为下限，原因在于 `.env` 加载功能依赖 Node 20.12 引入的内置 `process.loadEnvFile` 接口，同时更高的小版本在 ESM 模块解析等基础功能上更为稳定。提前在入口处检查版本，比让低版本 Node 运行到可选链（`?.`）或顶层 `await` 处报出难以理解的语法错误要友好得多。

### 5.2.2 环境变量加载

`loadEnvFile` 从当前工作目录（`process.cwd()`）开始逐级向上搜索 `.env` 文件，找到第一个即停止，不会合并多层 `.env` 的内容。加载时已存在的环境变量不会被覆盖，也就是说通过命令行 `export` 设置的变量优先级高于 `.env` 文件中的同名变量。底层调用的是 Node 20.12 引入的内置接口 `process.loadEnvFile(envPath)`，不需要引入 `dotenv` 等第三方依赖。

这个实现有一个值得说明的设计选择：向上搜索时没有在 `.git` 目录处设定边界。理论上 `.git` 目录标志着仓库根，继续向上可能读到仓库外的 `.env` 文件。实际中这个问题很少触发，原因在于绝大多数项目的 `.env` 文件就放在项目根目录，第一次匹配就会返回。如果后续实践中发现这个行为产生困扰，加一行 `.git` 边界检测即可修复。当前版本选择保持实现的简洁性，将这条检测留作已知的可选改进项。

### 5.2.3 命令行参数解析

X-Code CLI 通过 yargs 解析命令行参数。完整的参数列表如表 5-1 所示。

| 参数                | 短别名  | 说明                                           |
| ----------------- | ---- | -------------------------------------------- |
| `--model <id>`    | `-m` | 指定模型（如 `sonnet`、`deepseek`、`openai:gpt-4.1`） |
| `--trust` | `-t` | 信任模式：跳过所有权限确认（含破坏性命令） |
| `--print`         | `-p` | 非交互模式：输出后退出                                  |
| `--max-turns <n>` | —    | Agent 循环轮次上限（可选）                             |
| `--plan`          | —    | 启动即进入计划模式（Plan Mode）                         |
| `--continue`      | `-c` | 恢复本项目最近一次会话                                  |
| `--resume [<id>]` | `-r` | 恢复指定会话（不带值时弹出选择器）                            |

> 表 5-1 X-Code CLI 的命令行参数

这里有几个参数设计上的取舍。`--plan` 没有短别名，原因在于 `-p` 已被 `--print` 占用。`--max-turns` 在交互模式下没有默认值，因为交互模式下用户按 Esc 就能随时终止当前轮，设一个固定的轮次上限没有实际意义。这个参数主要服务于 `--print` 非交互模式和自动化测试场景：在没有用户按 Esc 的环境中，轮次上限充当安全网，防止 Agent 在死循环中无限消耗 Token。X-Code CLI 没有提供 `--config <path>` 参数让用户自定义配置文件路径，配置固定在 `~/.x-code/config.json`（`~` 代表用户主目录，Windows 上对应 `C:\Users\<用户名>`）。原因在于增加路径选项带来的灵活性与它引入的配置复杂度不成正比，实际需要自定义路径的场景极少。`--continue` 与 `--resume` 的会话恢复语义详见 Ch.36。

### 5.2.4 管道输入读取

`readStdin` 通过检测 `process.stdin.isTTY` 来判断当前的标准输入来源。TTY（Teletype）原本指物理终端机，在现代系统中引申为「程序是否连接着一个真实的终端」。当用户直接在终端输入 `xc` 时，`process.stdin.isTTY` 为 `true`；当通过管道传入数据时（例如 `cat foo.md | xc -p "总结这份文档"`），该值为 `false`，因为此时标准输入接收的是另一个进程的输出而非终端的键盘输入。

检测到管道输入后，`readStdin` 将管道内容完整读出，后续会与命令行中的 prompt 参数拼接成完整的用户消息。这是支持 `cat foo.md | xc -p "总结这份文档"` 这类管道用法的基础。在这个例子中，`foo.md` 的文件内容通过管道进入标准输入，`"总结这份文档"` 作为命令行参数传入，两者拼接后构成一条完整的用户指令发送给模型。

## 5.3 模型与供应商解析

步骤 1 完成后，`main()` 已经知道用户传了什么参数、环境变量里有哪些 API Key。步骤 2 的任务是根据这些信息确定要使用的模型，并创建供应商注册表。相关源码位于 `packages/core/src/config/index.ts` 和 `packages/core/src/providers/registry.ts`，简化后的调用序列如下：

<!-- check:ignore=punctuation -->

```ts
const availableProviders = getAvailableProviders() // 检测哪些供应商配了 API Key，全无则退出并提示
const modelId = resolveModelId(argv.model)         // 按四级优先级链解析模型标识符
const providerRegistry = createModelRegistry()     // 将所有有 API Key 的供应商注册进查询表
const subAgentRegistry = await createSubAgentRegistry() // 扫描 ~/.x-code/agents/ + 项目本地 .x-code/agents/
```

这四行代码对应了步骤 2 的全部工作。`getAvailableProviders` 是前置门禁：如果没有任何供应商配置了 API Key，流水线在此终止并输出配置指引。后三行各自完成一项独立的初始化，产物都会在步骤 3 中装入 `AgentOptions`。

### 5.3.1 模型标识符的四级优先级

`resolveModelId` 按以下顺序逐级查找模型标识符，命中即返回：（1）`--model` 参数显式指定；（2）`~/.x-code/config.json` 中的 `model` 字段；（3）`X_CODE_MODEL` 环境变量；（4）按供应商探测顺序，取第一个已配置 API Key 的供应商的默认模型。

把 `config.json` 的优先级排在环境变量之前，这是一个有意的设计选择。典型场景如下：用户在 `~/.bashrc` 中设置了 `export X_CODE_MODEL=deepseek:deepseek-v4-flash`，之后在 CLI 运行时通过 `/model` 命令切换到了 Claude Sonnet，`/model` 的选择会写入 `config.json`。如果环境变量的优先级更高，下次启动 CLI 时 `~/.bashrc` 中的设置会将模型静默覆盖回 DeepSeek，导致用户在 UI 中做的选择没有实际效果。将 `config.json` 排在环境变量之前，确保「用户在 UI 中做的最近一次选择」能够跨重启保持。这条规则的代价是：如果用户希望通过环境变量临时覆盖，需要在命令行直接传 `--model` 参数，而非依赖 `X_CODE_MODEL`。

### 5.3.2 供应商探测顺序

当用户没有通过上述四级中的前三级显式指定模型时，`PROVIDER_DETECTION_ORDER` 决定使用哪家供应商的默认模型。当前的探测顺序为：DeepSeek → Anthropic → OpenAI → Alibaba → Google → xAI → Zhipu → Moonshot。系统依次检查每家供应商的 API Key 环境变量是否已配置，第一个命中的供应商即为默认供应商，其默认模型作为会话使用的模型。DeepSeek 排在首位的设计取舍、各家供应商的注册机制、以及自定义 OpenAI 兼容端点的处理方式详见 Ch.09。

### 5.3.3 三种切换模型的方式

用户在使用过程中可以通过三种方式切换模型。

第一种是 `/model` 交互式选择器。进入 CLI 后输入 `/model` 命令，弹出交互式列表，列出 8 家供应商共 21 个精选模型，通过方向键选择，如图 5-2 所示。选定的模型会写入 `~/.x-code/config.json`，下次启动时自动使用，不需要重新选择。这种方式最为常用，因为选择会持久化。


![model.png](https://p3-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/c92ac369fdef46fc9e016122897dcd35~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=765&h=264&s=37613&e=png&b=272822)

> 图 5-2 /model 交互式选择器界面

第二种是 `xc --model <别名>` 在启动时指定。X-Code CLI 提供了 10 个短别名，覆盖各家供应商的主力模型，如表 5-2 所示。短别名的设计目的是让用户在命令行中用最少的字符切换到常用模型，无需记忆完整的 `<provider>:<model>` 格式。

| 别名                          | 对应模型                              |
| --------------------------- | --------------------------------- |
| `sonnet` / `opus` / `haiku` | Claude Sonnet / Opus / Haiku      |
| `gpt4`                      | GPT-4.1                           |
| `gemini`                    | Gemini 2.5 Pro                    |
| `deepseek` / `deepseek-pro` | DeepSeek V4 Flash / V4 Pro        |
| `qwen` / `glm` / `kimi`     | Qwen Max / GLM-4 Plus / Kimi K2.5 |

> 表 5-2 模型短别名与对应模型

第三种是 `xc --model <provider>:<model>` 完整标识符。短别名没有覆盖的模型（如 OpenAI o3、Qwen3 Coder Plus 等）通过这种方式指定，标识符直接传给 AI SDK 解析，不经过别名映射。

## 5.4 构建 AgentOptions

步骤 2 完成后，模型标识符和供应商注册表都已就绪。实际的 `main()` 在步骤 2 和步骤 3 之间还有一系列扩展生态的初始化工作：加载插件（Plugin）、注册 MCP 服务器、扫描技能（Skill）目录、处理 `--continue` / `--resume` 会话恢复等。这些初始化产生的注册表也会装进 `AgentOptions`，相关细节分别在 Ch.37（MCP）、Ch.38（Skill）、Ch.39（Plugin 与 Hook）中讨论。步骤 3 将前面所有步骤的产物汇总成一个 `AgentOptions` 对象，作为后续 Agent Loop（Ch.08）的完整配置输入。以下代码展示了其中与启动流程直接相关的核心字段：

<!-- check:ignore=punctuation -->

```ts
const options: AgentOptions = {
  modelId,          // 步骤 2 解析出的模型标识符，如 "anthropic:claude-sonnet-4-6"
  trustMode,        // --trust 参数，默认 false
  printMode,        // --print 参数，默认 false
  maxTurns,         // --max-turns 参数，未指定时为 undefined（agentLoop 视为无上限）
  thinking,         // 从 ~/.x-code/config.json 读取的思考模式开关
  permissionMode,   // --plan 启动时为 'plan'，否则为 'default'
  modelRegistry,    // 供应商注册表
  subAgentRegistry, // 子 Agent 注册表
  // skillRegistry, mcpRegistry, pluginRegistry, hookBus 等扩展生态字段此处省略
}
```

这个对象中有三个字段在后续章节中会反复出现，这里先说明其设计意图。

`permissionMode` 记录当前会话使用哪种权限策略，取值为 `'default'`、`'plan'` 或 `'acceptEdits'`，三种模式的含义详见 Ch.29。这个字段只存在于内存中，不持久化到磁盘。启动时由 `--plan` 参数决定初始值，运行中用户可以通过 `/plan` 命令临时切换，但切换结果不会写入配置文件，下次启动时权限模式会回到 `--plan` 参数决定的初始值。这样设计的原因在于权限模式是一个会话级的安全决策——如果某次会话中用户临时放宽了权限（例如切到 `acceptEdits`），这个决定不应当默默延续到下一次会话，否则用户可能在无意中以宽松权限启动了一个处理敏感项目的会话。

`modelRegistry` 是启动时创建的供应商注册表，包含所有已配置 API Key 的供应商。它的存在使得 `/model` 命令能够在不重启 CLI 的前提下切换到另一个供应商的模型——切换时只需从注册表中取出另一个供应商的模型实例，不需要重新初始化整个供应商适配层。

`subAgentRegistry` 在启动时构建一次，扫描 `~/.x-code/agents/` 和项目本地的 `.x-code/agents/` 目录，将发现的所有自定义子 Agent（Sub-Agent）定义注册进来，之后整个会话期间冻结不再更新。这意味着如果用户在 CLI 运行期间向 `.x-code/agents/` 目录中添加了新的 Agent 定义文件，需要重启 CLI 才能生效。之所以在运行期间冻结而非动态更新，原因在于 `task` 工具的描述字符串中嵌入了可用 Agent 列表，这段字符串作为系统提示词的一部分需要保持字节稳定以命中供应商的提示词缓存（Prompt Cache）机制（详见 Ch.08、Ch.19）。如果运行中动态更新 Agent 列表，`task` 工具的描述字符串就会改变，导致缓存失效，每轮请求都需要重新计算系统提示词的 Token，增加延迟和费用。

## 5.5 启动 UI

前三步完成了所有准备工作，第四步将交互界面呈现给用户。`main()` 调用 `startApp`（`packages/cli/src/app.tsx`），内部顺序完成两件事：先将启动横幅直接输出到屏幕，再通过 Ink 挂载交互组件。简化后的代码如下：

<!-- check:ignore=punctuation -->

```ts
// packages/cli/src/app.tsx 简化版
export function startApp(model, options, ...) {
  printHeader(options.modelId)              // 1. 启动横幅直写 stdout，不经过 Ink
  const { waitUntilExit } = render(<App />) // 2. 挂载 Ink，启动交互界面
  return waitUntilExit                      //    返回 Promise，main() await 它直到 Ink 卸载
}
```

### 5.5.1 启动横幅为什么不走 Ink

`printHeader`（`packages/cli/src/ui/components/AppHeader.tsx`）将一个 ASCII Logo、当前版本号、模型名称和操作提示拼成多行字符串，通过 `process.stdout.write` 直接写到屏幕上，不经过 Ink 的渲染管线。选择直写而非交给 Ink 渲染，原因在于横幅是一次性的静态内容，启动后不会再变化。如果将它交给 Ink，Ink 的动态渲染区域从一开始就会包含这段内容，后续 ChatInput 接管终端底部时需要额外处理位置协调，增加了不必要的复杂性。直写标准输出是更简单直接的方案。

实现上，`printHeader` 根据终端宽度自适应选择 Logo 样式：宽度不低于 52 列时使用完整大 Logo，不低于 30 列时使用紧凑 Logo，更窄的终端只输出纯文字版本。颜色使用 chalk 库并强制设为 level 3（24 位真彩色），始终输出 24 位色彩码而非根据终端能力自动降级。这一选择基于当前主流终端模拟器均已原生支持真彩色的判断。对于不支持真彩色的老旧终端，横幅颜色可能显示不正确，但影响范围仅限于启动横幅的装饰性色彩，不影响后续的功能使用。

### 5.5.2 Ink 挂载与 ChatInput 接管

横幅输出后，`render(<App />)` 启动 Ink。Ink 是一个用 React 心智模型编写终端 UI 的库，它将 React 元素转换为 ANSI 转义序列输出到标准输出。但 X-Code CLI 对 Ink 的使用方式并不典型：`<App>` 组件（`packages/cli/src/ui/components/App.tsx`）直接返回 `<ChatInput>`，没有任何 `<Box>` 或 `<Text>` 子元素包裹。

原因在于 Ink 默认的渲染管线（Yoga 布局引擎加 log-update 重绘机制）在三类场景下存在肉眼可见的界面抖动：CJK 宽字符的宽度计算偏差、输入法（IME）组合输入状态下的光标跳动、以及模型流式输出时大段文本的增量追加。这些问题在所有测试过的终端模拟器上均能复现。因此 X-Code CLI 的方案是保留 Ink 作为生命周期容器（负责组件挂载与卸载、标准输入的原始按键事件分发、Ctrl+C 信号捕获），将实际的屏幕渲染交给 ChatInput 自行管理。ChatInput 绕过 Ink 的渲染管线，用一套基于单元格缓冲区（Cell Buffer）的自研机制直接管理终端输出，从根源上消除了上述三类抖动。这套渲染机制的完整实现详见 Ch.23 与 Ch.24。

### 5.5.3 useAgent 桥接与退出流程

`<App>` 组件内部调用了一个 `useAgent` Hook（`packages/cli/src/ui/hooks/use-agent.ts`）来桥接 UI 层与 core 层。这个 Hook 持有所有 Agent 运行时状态——消息历史、活跃工具调用、权限请求队列、Token 用量、当前模型标识符等——并向 ChatInput 暴露 `submit` 方法用于发送用户消息。`useAgent` 是 UI 与 Agent 引擎之间的唯一连接点，它的具体实现将在 Ch.08 讨论 Agent Loop 时展开。

挂载完成后，`main()` 通过 `await waitUntilExit()` 将控制权交给 Ink，此后一直处于等待状态。当用户输入 `/exit`、连续按两次 Ctrl+C、或者 Ink 因其他原因卸载时，`waitUntilExit()` 返回，`main()` 随后调用 `gracefulShutdown(0)` 执行清理并退出进程。

## 5.6 非交互模式

`--print` 参数（短别名 `-p`）触发一条完全独立的执行路径（`packages/cli/src/print.ts`），不进入步骤 4 的 Ink 渲染，如图 5-1 所示的右侧分支。管道输入的读取在步骤 1 中已经完成（5.2.4 节），`main()` 将命令行 prompt 与管道内容拼接成完整的用户消息后，将其作为参数传给 `runPrintMode`。`runPrintMode` 发起单次 `agentLoop` 调用，模型的文本回复通过 `onTextDelta` 回调直接写到标准输出。执行完成后返回退出码（成功为 0、出错为非 0），主入口据此调用 `process.exit(code)` 退出。

在这条路径中，工具相关的回调处理与交互模式截然不同。`onToolCall`、`onToolProgress`、`onToolResult` 全部设为空操作，不向屏幕输出工具调用细节，因为非交互场景下用户关心的只是最终的文本输出。`onAskPermission` 永远返回拒绝（`'no'`），因为非交互模式下没有用户在终端前等待确认。如果需要在 `--print` 模式下允许写操作，用户需要同时传入 `--trust` 参数。`--trust` 在权限检查层直接跳过确认流程，根本不会触发 `onAskPermission` 回调，因此写操作能够顺利执行。

`--print` 模式的典型应用场景包括脚本调用、CI 流水线嵌入、以及批量任务处理。例如 `cat foo.md | xc -p "总结这份文档"` 将文件内容通过管道传入，模型的回复直接输出到标准输出，可以继续通过管道传给下游命令处理。`--trust` 与 `--print` 的组合让 Agent 在所有操作上不受阻塞（包括破坏性命令），前提是用户对任务内容有充分把握。

## 5.7 小结

本章梳理了 X-Code CLI 从启动到准备接受用户输入的完整流程。`main()` 按照参数与环境、模型与供应商解析、构建 AgentOptions、启动 UI 四个步骤依次执行，每一步只解决一类问题，前一步的输出是后一步的输入。模型标识符通过四级优先级链解析（`--model` 参数 > `config.json` > 环境变量 > 供应商探测），`config.json` 排在环境变量之前是为了确保 `/model` 选择器的选择能跨重启保持。UI 启动分为两步：启动横幅通过 `process.stdout.write` 直写以避开 Ink 的渲染管线，随后 `render(<App />)` 挂载 Ink，但真正的屏幕渲染由 ChatInput 的 Cell Buffer 机制接管（Ch.23-24）。非交互模式（`--print`）在 AgentOptions 构建完成后走完全独立的路径，不进入 Ink 渲染，适用于脚本调用和 CI 嵌入。完整启动到准备接收用户输入约 300ms 量级，主要瓶颈在 Node.js 冷启动本身，具体的耗时拆解与构建优化方案详见附录一。

下一章（Ch.06）将从启动流程转入实操——指导读者将 X-Code CLI 源码克隆到本地并运行起来，为后续章节「打开源码对照阅读」做好准备。
