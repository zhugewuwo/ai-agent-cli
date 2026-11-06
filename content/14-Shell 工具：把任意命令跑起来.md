# 第 14 章 Shell 工具：跨平台命令执行与失败信号传递

读完第 12 和 13 章，读者已经了解工具定义的三段结构以及文件工具的实现细节。本章讨论的 `shell` 在工具系统中地位特殊：它是唯一一个可以执行任意操作系统命令的工具，能力边界远大于其他所有工具——执行测试、构建项目、查看 git 历史、安装依赖，这些操作都只能通过 `shell` 完成。与 Ch.12 介绍的工具定义结构不同，`shell` 没有 `execute` 字段，不会由 AI SDK 自动执行，而是由 CLI 在 `processToolCalls` 阶段接管，原因是 shell 命令需要权限审批、跨平台 shell 检测和流式输出等会话级能力，这些超出了 `execute` 函数仅接收模型参数的能力范围。`shell` 工具只接收两个参数：`command`（要执行的命令字符串）和可选的 `timeout`（超时毫秒数，默认 30000）。围绕命令执行的完整路径，本章分为如下几个部分进行介绍。权限审批的完整机制留在 Ch.29 讨论。

- 14.1 节用一条 `pnpm test` 的完整执行过程串起全章的知识点。
- 14.2 节解释 description 为什么要列出专用工具对照表。
- 14.3 节讨论跨平台 shell 检测策略：bash 优先还是 PowerShell 优先。
- 14.4 节拆解 PowerShell 的编码通道与三层包装。
- 14.5 节介绍命令输出的流式推送与 50ms 进度节流。
- 14.6 节讲解命令失败时信号的四层传递机制。
- 14.7 节讨论复合命令拆分与权限预分级。
- 14.8 节给出本章小结。

## 14.1 完整示例：一条 `pnpm test` 的全过程

假设用户输入以下请求：

> 我刚改完登录模块的几个文件，帮我执行一下 `pnpm test` 看看有没有失败的用例

模型读完上下文后决定调用 `shell` 工具：

```jsonc
{ "command": "pnpm test" }
```

X-Code CLI 收到这个工具调用后依次完成以下步骤。首先调用 `getShellProvider()` 检测当前平台的 shell 环境，从 bash、zsh、PowerShell 三者中选择一个（14.3 节详述）。然后用 `execa` 启动子进程执行命令——如果选中的是 bash，命令形式为 `bash -c "pnpm test"`；如果是 PowerShell，则走专门的 base64 编码路径（14.4 节详述）。子进程启动后，stdout 和 stderr 的每一段输出都通过回调函数实时推送给 UI，用户可以看到测试用例逐个执行的过程。命令结束后，CLI 收集完整的 stdout、stderr 和退出码，拼装成结构化的工具结果返回给模型：

```text
PASS  src/auth/jwt.test.ts
PASS  src/auth/session.test.ts
FAIL  src/routes/login.test.ts
  ● POST /login › returns 200 on valid credentials
    expected 200 but got 401
    at Object.<anonymous> (src/routes/login.test.ts:23:12)

Test Suites: 1 failed, 2 passed, 3 total
Tests:       1 failed, 12 passed, 13 total

Exit code 1
```

末尾的 `Exit code 1` 不是 jest 自己输出的，而是 X-Code CLI 检测到退出码不为零后主动追加的一行文本。这样做的目的是让模型在读取工具结果时能立刻判断命令是否成功，不需要自己去解析 stderr 的内容——14.6 节会详细讨论这个设计及其背后的原因。同时这次工具调用的结果被标记为 `isError: true`，UI 端的工具结果面板用红色高亮显示，用户扫一眼就能识别失败的命令。如果命令成功执行且没有任何输出（stdout 和 stderr 均为空），CLI 会返回字符串 `"Done"` 作为工具结果，避免模型收到空响应后无法判断命令是否已经执行完毕。模型读到上面这段输出后，就知道哪个测试失败、在第几行、具体的错误信息是什么，接下来可以调用 `readFile` 查看对应代码段、用 `edit` 修复问题、再调用一次 `shell` 重新执行测试。整个过程演示了 shell 工具的核心动作链。图 14-1 展示了从模型发出工具调用到工具结果写回消息历史的完整执行管线，后续各节逐一展开管线中每个阶段的设计决定。


![image.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/cf9f0fd3e98c4477bf2e96289e874781~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1300&h=133&s=18910&e=png&b=ffffff)

> 图 14-1 shell 工具执行管线——从工具调用到结果回流

## 14.2 为什么 description 列出专用工具对照表

`shell` 工具的 description 中有一段醒目的禁忌列表，明确告诉模型不要用 `shell` 执行某些命令，并给出对应的专用替代工具。

> 源码：`packages/core/src/tools/shell.ts`

<!-- check:ignore=punctuation -->
```
IMPORTANT: Avoid using this tool to run grep, rg, cat, head, tail, sed, or
awk commands. Instead, use the appropriate dedicated tool — they provide a
better user experience:
- File search: Use glob (NOT find or ls)
- Content search: Use grep tool (NOT grep/rg command)
- Read files: Use readFile (NOT cat/head/tail)
- Edit files: Use edit (NOT sed/awk)
- Write files: Use writeFile (NOT echo >/cat <<EOF)
```

这段文字是提示词级（Prompt-level）引导，不是运行时拦截——`shell` 工具本身仍然能够执行 `grep`、`cat` 或 `find`，工具层不会主动拒绝这些命令。模型读到 description 中的禁忌列表后，大多数情况下会自觉选择专用工具。之所以要这样引导而非放任模型自行选择，有以下四方面原因。（1）截断精度：专用工具按工具语义做截断处理（详见 Ch.17），例如 `readFile` 默认限制前 2000 行加 256KB 字节上限，而 `shell` 执行 `cat` 读取大文件时可能一次返回数兆字节的原始输出。（2）权限分级：专用只读工具默认设为 always-allow（自动允许），而 `shell` 执行 `cat` 时权限层无法判断意图，需要整体走交互审批流程。（3）跨平台兼容性：`grep` 在 Windows 默认没有安装、`find` 的语义与 GNU find 不同，专用 `grep` 工具底层调用的是预编译的 ripgrep 二进制文件，跨平台行为一致。（4）UI 呈现：专用工具的结果会按 `file:line:content` 格式结构化呈现，shell 输出则只是未经处理的 stdout 文本。

description 中显式列出了每个命令与对应专用工具的映射关系（`cat` → `readFile`、`find` → `glob`、`sed` → `edit`），而非笼统地说「请使用专用工具」。这种显式映射的效果优于抽象指示：模型在选择工具时不需要额外推理「应该用哪个替代」，看到想执行 `cat` 就直接联想到 `readFile`，对应关系一目了然。这是工具 description 设计的一条通用经验——禁止某个行为时，同时提供具体的替代方案，比单纯的「不要做 X」更有效。与此同时，`shell` 仍然保留了执行这些命令的能力，这种「提示词引导加保留灵活性」的组合让模型在绝大多数场景中使用专用工具，同时在特殊场景（例如需要执行 `find -exec` 这种 `glob` 做不到的操作）下仍然可以通过 `shell` 完成。

## 14.3 跨平台 shell 检测：bash 优先还是 PowerShell 优先

在讨论具体的检测逻辑之前，需要先区分两个容易混淆的层次。第一层是终端层（TTY），它决定 CLI 能否启动。X-Code CLI 是一个 TUI 应用，启动时需要拦截键盘输入、进入 raw mode、监听窗口尺寸变化，这些操作要求终端向 Node.js 暴露交互式终端设备（Teletypewriter，即 TTY）；不支持 TTY 的环境（如 CI 流水线中的非交互式终端）无法运行 CLI。第二层是 Shell 提供者（Shell Provider），它决定命令如何执行——CLI 已经启动之后，模型调用 `shell` 工具时，需要选择用 bash 还是 PowerShell 来启动子进程。本节只讨论第二层，第一层的终端兼容性归入终端基础相关章节。

`getShellProvider()` 函数负责第二层的检测。它在每次执行 shell 命令时被调用，根据操作系统类型（`os.platform()`）和用户的默认 shell 设置（`process.env.SHELL`）返回对应的 Shell Provider。函数本身没有做显式缓存，但这两个值在进程生命周期内不会变化，因此每次调用的返回结果都是一致的。

> 源码：`packages/core/src/tools/shell-provider.ts`

<!-- check:ignore=punctuation -->
```ts
export function getShellProvider(): ShellProvider {
  if (os.platform() === 'win32') {
    // Git Bash / MSYS2 / Cygwin 把 SHELL 设成 Unix 风格路径，
    // 检测到就用 bash 启动子进程，让 Unix 工具生态正常工作
    const shell = process.env.SHELL
    if (shell && /\b(bash|zsh)$/i.test(shell)) {
      return createPosixProvider(shell, shell.endsWith('zsh') ? 'zsh' : 'bash')
    }
    // 未检测到 bash/zsh，降级到 PowerShell
    return createPowerShellProvider('powershell.exe')
  }
  // macOS / Linux：使用 $SHELL 环境变量，默认 bash
  const userShell = process.env.SHELL ?? '/bin/bash'
  return createPosixProvider(userShell, userShell.endsWith('zsh') ? 'zsh' : 'bash')
}
```

返回的 `ShellProvider` 接口包含两个字段：`type` 标识当前 shell 类型（`'bash'`、`'zsh'` 或 `'powershell'` 三种取值），`spawn()` 方法负责启动子进程执行命令。Windows 路径的检测逻辑值得展开讨论。Git Bash、MSYS2 和 Cygwin 这类工具在 Windows 上运行时，会把 `$SHELL` 环境变量设成 Unix 风格路径（如 `/usr/bin/bash`）。`getShellProvider` 用正则 `/\b(bash|zsh)$/i` 检测到这个值后，直接用对应的 bash 或 zsh 可执行文件启动子进程，让模型生成的 bash 命令可以原样执行。只有在 `$SHELL` 未设置或不匹配时才降级到 `powershell.exe`。这种「bash 优先」策略的原因在于模型的训练数据分布：大量编程教程和技术问答使用 bash 语法，模型在训练过程中见到的命令绝大多数是 `for f in *.ts; do ...`、`$(...)` 命令替换、管道和 heredoc 等 bash 习惯用法。如果 CLI 直接降级到 PowerShell，模型按 bash 语法生成的命令会频繁执行失败，触发不必要的重试循环。Windows 开发者最常见的使用场景是从 VSCode 的 Git Bash profile 启动 CLI，此时 `$SHELL` 已经被设成 `/usr/bin/bash`，shell-provider 会直接选择 bash 路径。

macOS 和 Linux 不存在这种跨语法分歧。macOS 10.15（Catalina）之后默认 shell 是 zsh，更早的版本以及大部分 Linux 发行版默认是 bash。bash 和 zsh 都属于 POSIX shell，命令替换 `$(...)` 、管道、重定向、`for ... do ... done`、heredoc 等核心语法完全一致。少数差异（zsh 默认开启的 `EXTENDED_GLOB`、bash 4.x 才支持的关联数组）只在非常特殊的用法中才会暴露，模型生成的日常命令几乎不会触发。因此 `getShellProvider` 在 Unix 路径上不做区分，统一走 `createPosixProvider`，仅在 `type` 字段上记为 `'bash'` 或 `'zsh'`。这个 `type` 值会通过系统提示词注入给模型——`system-prompt.ts` 中的 `{shell}` 占位符在运行时被替换为实际的 shell 类型字符串。模型读到 `Shell: bash` 后就知道应该生成 bash 语法的命令，读到 `Shell: powershell` 则生成 PowerShell 语法，避免了因猜错 shell 类型而生成无法执行的命令。

这里有一个有意的设计取舍：X-Code CLI 不强制要求用户安装 WSL（Windows Subsystem for Linux）。WSL 是完整的 Linux 子系统，安装成本高、磁盘占用大，且跨文件系统路径换算（`C:\foo` 与 `/mnt/c/foo` 的映射）会引入额外的复杂度。依赖 `$SHELL` 环境变量做自适应检测已经足够：用户以什么方式打开终端，shell-provider 就选择对应的 shell，无需额外配置。

## 14.4 PowerShell 的编码通道与三层包装

如果 `getShellProvider` 选择了 PowerShell 路径，命令执行的复杂度会显著上升。PowerShell 的引号和转义规则与 bash 有本质区别：bash 中反引号是命令替换，PowerShell 中反引号是转义符；`$()` 在两个 shell 中都存在但语义不同；`echo "Hello $name"` 在 bash 中会展开 `$name` 变量，在 PowerShell 中如果该变量未定义会被静默替换为空字符串，导致命令含义被无声改写。除了语法差异本身，命令字符串从 Node.js 传到 PowerShell 的过程中还需要穿过多层解析。在 Windows 上，Node.js 启动子进程时，操作系统会按 Windows 命令行规则处理一次参数中的引号和特殊字符；如果经由 `cmd.exe` 中转，`cmd.exe` 还会按自己的规则（`%` 变量展开、`^` 转义）再解析一次；最终 PowerShell 收到命令行字符串后再按 PowerShell 语法做第三次解析。三层解析器各有一套引号和转义规则，要让一条包含 `$`、引号或反引号的命令在三层传递中都保持原意，手动添加转义几乎不可能做到正确。

X-Code CLI 采用 PowerShell 官方提供的 `-EncodedCommand` 参数来绕过这个问题。

> 源码：`packages/core/src/tools/shell-provider.ts::createPowerShellProvider`

<!-- check:ignore=punctuation -->
```ts
function encodePowerShellCommand(psCommand: string): string {
  // 将命令字符串按 UTF-16LE 编码后转为 base64
  return Buffer.from(psCommand, 'utf16le').toString('base64')
}

// 启动子进程时使用 -EncodedCommand 传递编码后的命令
return execa(executable, [
  '-NoProfile', '-NonInteractive',
  '-EncodedCommand', encodePowerShellCommand(wrapped)
], { /* ... */ })
```

整个命令字符串先按 UTF-16LE 编码、再转为 base64、然后通过 `-EncodedCommand` 参数传给 PowerShell。base64 输出的字符集只包含 `[A-Za-z0-9+/=]`，这些字符不会被任何外层引号或转义层误解。无论 cmd.exe、Node 的 Windows argv 序列化还是 PowerShell 自己的解析器，都不会修改 base64 字符串中的任何字符。命令在整个传输路径上字节稳定，PowerShell 收到后解码还原成原始命令字符串再执行。这种方式的代价是调试可读性下降——日志中看到的是一串 base64 而非原始命令——但比起在三层解析器之间手写转义规则，base64 编码通道在正确性上要可靠得多。

`createPowerShellProvider` 在 base64 编码之前，还会在用户命令外面包裹前缀和后缀，每一段处理一个独立的平台兼容问题。

> 源码：`packages/core/src/tools/shell-provider.ts::createPowerShellProvider`

<!-- check:ignore=punctuation -->
```ts
const wrapped = [
  '[Console]::OutputEncoding = [System.Text.Encoding]::UTF8',
  "$ProgressPreference = 'SilentlyContinue'",
  command,
  '$__ec = if ($null -ne $LASTEXITCODE) { $LASTEXITCODE } elseif ($?) { 0 } else { 1 }',
  'exit $__ec',
].join('\n')
```

第一行 `[Console]::OutputEncoding = UTF8` 解决的是编码问题。PowerShell 5.1 在中文 Windows 系统上默认以 GBK 编码输出，而 X-Code CLI 按 UTF-8 解码，不做这个设定会读到乱码。第二行 `$ProgressPreference = 'SilentlyContinue'` 解决的是噪声问题。PowerShell 首次加载某些模块时会在 stderr 输出 CLIXML 格式的进度记录，这些内容会被当作工具结果的一部分发送给模型，形成无意义的噪声，关闭这个偏好设置可以消除它们。

最后两行解决的是退出码传播问题，这是整段包装中影响最大、也最容易遗漏的部分。PowerShell 不会自动将子命令的退出码传播为自身进程的退出码：原生可执行文件的退出码记录在 `$LASTEXITCODE` 变量中，cmdlet 管道的成功与否记录在 `$?` 自动变量中，两个来源各自独立。PowerShell 不负责将它们统一汇总到进程退出码上，最终返回给调用方的 process exit code 经常不反映底层命令的真实状态。如果不做显式处理，会出现子命令失败但 PowerShell 进程退出码为零的情况——X-Code CLI 看到 `exitCode === 0` 后不会追加 `Exit code N`，也不会标记 `isError`，模型会把一次失败的命令当作成功，继续按错误的前提推理。这是一个静默腐蚀模型决策质量的缺陷：命令的输出文本中仍然包含失败信息，但退出码信号丢失后模型不一定会去仔细解析输出内容，尤其是在输出较长的情况下。包装层通过 `$__ec` 变量优先取 `$LASTEXITCODE`（原生可执行文件场景），若不存在则根据 `$?` 取 0 或 1（cmdlet 场景），最后用 `exit $__ec` 确保退出码被正确传播。

总结来看，PowerShell 路径的实现复杂度远超 bash 路径——bash 只需一行 `execa(shell, ['-c', command])` 即可完成命令执行，PowerShell 需要 base64 编码加三段包装。这种不对称是 Windows 平台的现实约束，编写跨平台 CLI 工具时无法回避。

## 14.5 流式输出与 50ms 进度节流

`pnpm test` 这类命令可能运行数分钟。如果等命令完全结束后才展示结果，用户在等待期间没有任何反馈，体验会非常差。因此 shell 工具的执行采用流式输出：子进程的 stdout 和 stderr 每产生一段数据就立刻推送给 UI，用户可以实时看到测试用例逐个执行的过程。

> 源码：`packages/core/src/agent/tool-execution.ts::executeShell`

<!-- check:ignore=punctuation -->
```ts
const proc = getShellProvider().spawn(command, { timeout, signal })

let lastProgressTime = 0
const PROGRESS_THROTTLE_MS = 50

const onChunk = (chunk: Buffer) => {
  const s = chunk.toString()
  callbacks.onShellOutput(s) // 完整输出实时推送给 UI

  // 进度提示节流：50ms 最多更新一次
  const now = Date.now()
  if (now - lastProgressTime < PROGRESS_THROTTLE_MS) return
  const lines = s.split(/\r?\n/).filter((l) => l.trim().length > 0)
  const last = lines[lines.length - 1]
  if (last) {
    lastProgressTime = now
    const trimmed = last.length > 120 ? last.slice(0, 117) + '...' : last
    reportProgress(toolCallId, trimmed)
  }
}

proc.stdout?.on('data', onChunk)
proc.stderr?.on('data', onChunk)

const result = await proc // 等待命令结束
```

这段代码中有两条独立的输出通路。第一条是完整输出流：`onShellOutput(s)` 把子进程的每一段输出原封不动地推送给 UI 的 Shell 输出组件，用户在终端历史区域可以看到所有内容逐行累积。第二条是进度提示流：工具执行期间，进度指示器旁边会显示一行灰色文字，内容是当前 chunk 中最后一行非空输出（截断到 120 字符），让用户在不滚动终端的情况下也能感知命令正在做什么。提取最后一行的方式是将 chunk 按换行符拆分后过滤空行，取最末一行。

进度提示设置了 50 毫秒的节流间隔。原因是某些命令会在极短时间内输出大量文本——典型的场景包括 PowerShell 的 `Format-Table` 渲染表格、`pnpm install` 下载依赖时的进度更新等，这些命令可能在 1 毫秒内输出十几行。如果每一行都触发一次 `setState` 导致 UI 组件重新渲染，用户会看到明显的文字闪烁。50 毫秒的节流将进度提示的更新频率限制在每秒最多 20 次——频率足够高，用户仍然感觉是实时更新；频率又足够低，不会出现视觉上的闪烁或抖动。需要明确的一点是，节流只影响进度提示的显示频率，不影响模型最终看到的输出内容。模型在命令结束后读到的是完整的 stdout 和 stderr 拼接结果，没有任何字节被丢弃，节流是纯粹的 UI 层优化。

## 14.6 失败信号的四层传递

命令执行结束后，X-Code CLI 不仅把 stdout 和 stderr 拼接起来返回给模型，还要把「命令成功还是失败」这个信号传递清楚。这个过程由四层机制共同完成。

> 源码：`packages/core/src/agent/tool-execution.ts::executeShell`

第一层是退出码显式追加到输出末尾。

<!-- check:ignore=punctuation -->
```ts
const output = [stdout, stderr].filter(Boolean).join('\n').trim()
if (result.exitCode !== 0 || isMaxBuffer) {
  const suffix = isMaxBuffer ? ' (output exceeded buffer limit)' : ''
  const text = output
    ? `${output}\nExit code ${result.exitCode}${suffix}`
    : `Exit code ${result.exitCode}${suffix}`
  return { output: text, isError: true }
}
return { output: output || 'Done', isError: false }
```

之所以要在 output 末尾主动追加 `Exit code N`，是因为 stdout 和 stderr 本身并不总是包含清晰的失败信号。`tsc` 编译失败时 stderr 可能为空（错误信息全在 stdout 中）、`git push` 失败时 stderr 的内容顺序不稳定、`jest` 失败时有时还会在某处输出 "Tests passed" 引起误判。固定在末尾追加退出码让模型只需检查最后一行就能判断成败，比要求模型自行解析 stderr 内容要可靠得多。当 `isMaxBuffer` 为 true 时（即子进程因输出超限被终止），还会在退出码行后追加 ` (output exceeded buffer limit)` 标记，把「被缓冲上限强制终止」和「命令自身返回非零退出码」这两种失败原因区分开。

第二层是 `isError: true` 标记。CLI 在返回工具结果时同时设置这个标志，UI 端据此把整个工具结果面板染为红色。用户扫一眼就能识别哪些 shell 调用失败了，不需要阅读完整的输出文本才能得出判断。

第三层是 PowerShell 错误块折叠（Error Block Folding）。PowerShell 遇到引号错误或语法错误时，会输出以 `At line:X char:Y` 开头的多行诊断信息块，每个块通常包含 5 到 10 行内容。如果模型在重试循环中多次执行同一条错误命令，这些重复的错误块会在输出中快速累积，占据大量 token 却不提供新的诊断信息。`foldShellErrorNoise` 函数在将输出返回给模型之前，对 stdout 和 stderr 各执行一次折叠：识别以 `At line:` 标记开头的 PowerShell 错误块，将每个多行块折叠成一行摘要（格式为 `At line:X char:Y [PS parse error — details folded]`），让模型在有限的上下文窗口中能够聚焦在真正有用的诊断信号上。

> 源码：`packages/core/src/utils/shell-error.ts::foldShellErrorNoise`

第四层是 20MB 缓冲上限。某些命令会产生巨量输出——`cat` 读取大日志文件、`npm ls` 打印完整依赖树、`find /` 遍历整个文件系统等。`execa` 的 `maxBuffer` 设为 20MB（`MAX_SHELL_BUFFER = 20 * 1024 * 1024`），超出后子进程会被 SIGTERM 信号终止，已经收到的部分输出仍然可用。CLI 检测到 `isMaxBuffer === true` 后，对 stdout 和 stderr 分别截断到前 30000 个字符，末尾追加 `[stdout truncated — exceeded buffer limit]` 或对应的 stderr 标记。

<!-- check:ignore=punctuation -->
```ts
if (isMaxBuffer) {
  const INLINE_CAP = 30_000
  if (stdout.length > INLINE_CAP)
    stdout = stdout.slice(0, INLINE_CAP) + '\n... [stdout truncated — exceeded buffer limit]'
  if (stderr.length > INLINE_CAP)
    stderr = stderr.slice(0, INLINE_CAP) + '\n... [stderr truncated — exceeded buffer limit]'
}
```

20MB 是按实际工作流中合理输出量的上限设定的：大部分 build 和 test 命令输出几 KB 到几 MB，20MB 给确实需要大输出的命令留出了足够空间，同时能防止 `yes` 或 `find /` 这类意外失控的命令耗尽进程内存。

## 14.7 复合命令拆分与权限预分级

模型可能在一条命令中包含多个操作，例如 `git diff && rm -rf ./tmp` 或 `pnpm test || npm install`。如果对整条复合命令做整体的权限判定，可能出现以下情况：用户看到 `pnpm test || npm install`，认为「执行测试，失败则安装依赖」的逻辑合理于是批准执行；但如果测试确实失败了，`npm install` 随即触发，可能修改 lockfile、安装了不该安装的依赖。前半段的合理性掩盖了后半段的风险。

X-Code CLI 的解决方式是把复合命令拆分为独立的子命令，分别做权限分级判定。

> 源码：`packages/core/src/tools/shell-utils.ts::splitShellCommands`

<!-- check:ignore=punctuation -->
```ts
splitShellCommands('git diff && rm -rf ./tmp')
// → ['git diff', 'rm -rf ./tmp']
```

`splitShellCommands` 按 `&&`、`||`、`;`、`|` 四种操作符拆分命令，但在拆分过程中会跳过两类边界内的分隔符。第一类是引号：`echo "a; b"` 中的分号是字符串字面量的一部分，不是命令分隔符，拆分器通过跟踪单引号和双引号的开合状态来跳过这些位置。第二类是花括号：PowerShell 的哈希字面量和脚本块使用 `@{N='Directory';E={$_.Name}}` 这样的结构，其中的分号是字段分隔符而非语句分隔符。拆分器通过维护一个花括号深度计数器来识别这些嵌套结构，深度大于零时不在分隔符位置切分。POSIX shell 的 `{ ... ; }` 花括号分组也被这条规则覆盖，效果是保持花括号内的完整内容不被切断。

拆分后的每个子命令独立接受分类判定：只要任何一个子命令命中破坏性模式列表（`DESTRUCTIVE_PATTERNS`），整条复合命令被标记为 deny 级，对话框会显示红色 `[dangerous]` 警告；只有所有子命令都命中只读命令列表（`READ_ONLY_COMMANDS`）时，才会自动放行；其余情况走普通交互审批。回到前面的例子——`git diff && rm -rf ./tmp` 拆分后，`git diff` 属于只读、`rm -rf ./tmp` 命中破坏性模式，整条命令被标记为危险并弹出带警告的确认框，避免了用户被前半段只读操作误导而忽视后半段破坏性命令的情况。完整的分级规则、`READ_ONLY_COMMANDS` 全名单、`DESTRUCTIVE_PATTERNS` 全条目以及 shell 权限缓存机制将在 Ch.29 展开。

`splitShellCommands` 有一个有意的设计局限：它不识别 `$()` 子表达式和反引号命令替换的边界。以 `echo $(git diff | wc -l)` 为例，这条命令的意图是「统计 diff 的行数并打印」，`|` 管道符位于 `$()` 内部，语义上属于子表达式的一部分。但拆分器无法感知 `$()` 的边界，看到 `|` 就切分，产生两个不完整的分片：`echo $(git diff` 和 `wc -l)`。

这种切分虽然不正确，但不影响安全判定。原因在于权限分级以每个分片的起始命令名为依据：`echo` 和 `wc` 都不是破坏性命令，判定结果与不拆分时相同。切错影响的只是「分给谁判定」这个内部分配问题，不会让原本安全的命令变成危险命令，也不会让原本危险的命令逃过拦截。

为什么不修复这个局限？因为修复它需要引入一个完整的 shell 语法解析器，而且必须同时支持 bash 和 PowerShell 两套语法——两者在子表达式方面的规则完全不同（bash 用 `$()`，PowerShell 用 `$()`、`@()` 和 `$()`）。模型生成包含嵌套命令替换的命令频率很低，为这类极少出现的边界情况引入完整解析器性价比不高。

## 14.8 小结

`shell` 工具是整个工具系统中能力最强也风险最高的入口，它的实现围绕三个核心挑战展开。跨平台 shell 检测方面，`getShellProvider` 在 Windows 上优先检测 `$SHELL` 环境变量以识别 Git Bash 或 MSYS2 环境，只有在未发现 POSIX shell 时才降级到 PowerShell，这样模型按 bash 习惯生成的命令在大多数 Windows 开发环境中可以直接执行。PowerShell 路径的复杂度远高于 bash——命令通过 UTF-16LE 编码和 base64 转换后经 `-EncodedCommand` 传递，绕过了 cmd.exe、Node argv 和 PowerShell 解析器三层引号转义的问题；编码之前还要在命令外层包裹三段前缀后缀，分别处理输出编码（强制 UTF-8）、进度噪声（关闭 CLIXML 输出）和退出码传播（显式取 `$LASTEXITCODE` 或 `$?`）三个独立的平台兼容问题。其中退出码传播最容易遗漏，因为不加这段包装命令也能正常执行，只是失败信号会静默丢失。

流式输出方面，子进程的 stdout 和 stderr 实时推送给 UI，但进度指示器以 50 毫秒为间隔节流更新，在实时感和视觉稳定性之间取得平衡，节流不影响模型最终读到的完整输出。失败信号通过四层机制逐级加强：退出码显式追加到输出末尾让模型一行就能判断成败、`isError` 标记让 UI 标红、PowerShell 错误块折叠防止重复诊断信息占据上下文窗口、20MB 缓冲上限防止失控输出耗尽进程内存。复合命令拆分是权限预分级的前提——`splitShellCommands` 按操作符切分但跳过引号和花括号内的字符，让每个子命令独立接受只读或破坏性判定，避免复合命令中的危险部分被前面的安全部分掩盖。
