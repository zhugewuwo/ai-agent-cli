# 第 13 章 文件工具：readFile、writeFile 与 edit

文件读写是 Agent 最高频的动作——所有代码任务都离不开它们。X-Code CLI 把这部分拆成三个工具：`readFile` 读文件、`writeFile` 写新文件、`edit` 替换老文件中的一段字符串。三个工具看起来非常基础，但每个都包含若干不显眼的设计决策，理解这些决策才能看清工具层为什么是这套架构。全章沿一个具体场景把三个工具走一遍，再逐一讨论关键设计决策，分为如下几个部分进行介绍。

- 13.1 节用一个完整的例子展示三个文件工具的协作过程。
- 13.2 节讨论 `readFile` 返回的行号格式与双层截断策略。
- 13.3 节讲解 `edit` 工具的精确字符串匹配与唯一性校验机制。
- 13.4 节划清 `writeFile` 与 `edit` 的分工边界。
- 13.5 节解释「先读后写」为什么是软约束而非强校验。
- 13.6 节介绍图片与 PDF 如何通过 content parts 走多模态视觉通道。
- 13.7 节给出本章小结。

## 13.1 一个完整的例子：给 utils.ts 加一个函数

假设用户输入：

> 给 `src/utils.ts` 加一个 `formatDate(date: Date, fmt: string): string` 函数，跟里面已有的工具函数风格保持一致

模型不会直接动手——它先调 `readFile` 查看现状：

```jsonc
{ "filePath": "/abs/path/src/utils.ts" }
```

工具结果是带行号的文本（每行 `行号 + Tab + 内容`）：

```
1	import { Buffer } from 'node:buffer'
2
3	export function trimTrailingNewlines(s: string): string {
4	  return s.replace(/\n+$/, '')
5	}
6
7	export function isBufferLike(v: unknown): v is Buffer {
8	  return Buffer.isBuffer(v)
9	}
10
11	export const NOOP = () => undefined
```

行号让模型在后续推理时能稳定地引用位置——「第 5 行后面插入」既精确又便于诊断。模型读完看清了风格（命名驼峰、单层 `export function`、无类型别名）后，面临一个选择：把整个 `utils.ts` 的文件内容重写一遍、把新函数嵌入其中？还是只发「差异」那一段？

答案是后者——这种「老文件改一小段」的场景应该用 `edit`：

```jsonc
{
  "filePath": "/abs/path/src/utils.ts",
  "oldString": "export const NOOP = () => undefined",
  "newString": "export const NOOP = () => undefined\n\nexport function formatDate(date: Date, fmt: string): string {\n  // ... 实现\n}",
}
```

`edit` 工具内部做的事简单又严格：把整个文件读进来，确认 `oldString` 在文件里只出现 1 次，找到位置后把那段字符串替换成 `newString`，写回磁盘。返回结果只有一句：

```
File edited: /abs/path/src/utils.ts
```

为什么不用 `writeFile` 整文件覆写？原因有两个。第一，模型需要把原文件 11 行原样复述一遍才能写——多产生的输出 token 全是无意义的复述，而且容易在复述过程中出现差异（多一个空格、改了缩进）。第二，git diff 看到的是「删 11 行 + 加 14 行」而不是「加 4 行」，代码评审完全分不清哪些是有意改动。`edit` 只发改动的那段，工具结果干净、git diff 也干净。

接下来用户继续：

> 再创建一个独立的 `src/format/date.ts` 模块，把这个函数搬过去

这次情况反过来——`src/format/date.ts` 还不存在，`edit` 找不到任何 `oldString` 可替换。这种「文件尚未存在」的场景只能用 `writeFile` 整段写出去：

```jsonc
{
  "filePath": "/abs/path/src/format/date.ts",
  "content": "export function formatDate(date: Date, fmt: string): string {\n  // ...\n}\n",
}
```

`src/format/` 目录不存在也不会报错——`writeFile` 内部先 `mkdir -p` 把中间目录建好再写。返回：

```
File created: /abs/path/src/format/date.ts (3 lines)
```

三个工具的返回消息各自不同：`writeFile` 新建文件时返回 `File created: ...`；`writeFile` 覆盖已有文件时返回 `File written: ...`；`edit` 做定向替换时返回 `File edited: ...`。这些区别让模型和用户都能一眼分清当前执行的是哪种操作。整个流程演示了三个工具的典型分工：读用 `readFile`、改老文件用 `edit`、建新文件用 `writeFile`。下面分别讨论几个不显眼但关键的设计决策。

## 13.2 行号格式与双层截断

`readFile` 输出的核心格式只有一句话：每行前面加 `行号 + Tab` 前缀。这个看似简单的格式带来两个具体的好处。第一，模型能稳定引用位置——后续推理里可以说「第 42 行的那个 `verify()` 调用没做 audience 校验」。没有行号的话模型只能说「那个 verify 调用」，当文件中存在多个 verify 时，父 agent 和用户都分不清指的是哪一个。第二，`edit` 的 `oldString` 可以直接照抄——模型在 `edit` 时不应包含行号前缀（描述里明确写了 "Never include line number prefixes in oldString or newString"），但行号本身让它知道要复制哪几行的内容。

与此同时，`readFile` 给输出加了两层上限来避免一次读取占满 context window。

> 源码：`packages/core/src/tools/read-file.ts`

```ts
const LARGE_FILE_LINE_THRESHOLD = 2000 // 默认头部截断的行数阈值
const MAX_READ_BYTES = 256 * 1024      // 256 KB 字节上限
```

第一层是行数上限：默认只读前 2000 行。模型不带 `offset` / `limit` 调 `readFile` 读一个 5000 行的文件时，工具只返回前 2000 行，末尾追加一段提示：

```
[readFile: showing first 2000/5000 lines. Call readFile again with
 offset/limit to view other ranges, or use grep to find specific
 symbols. For whole-file analysis of very large files, consider
 delegating to a sub-agent via the task tool — each sub-agent reads
 in isolated context and returns only a summary.]
```

这段提示不只是给用户看——它的措辞像给模型的下一步指引。模型读到这条提示后通常会自己决定是用 grep 找具体目标、用 offset 继续读、还是直接通过 task 子 agent（详见 Ch.19）做整文件分析。把恢复路径写在截断信息里、让模型自行选择下一步，比让 CLI 替模型决定更稳定。

第二层是字节上限：每次返回不超过 256 KB。这一层针对的是模型显式传了 `limit` 的情况——比如 `limit: 90000`——光靠行数限制无法防住。256 KB 是按 token 反算的：英文大约对应 25K tokens，是行业里 Read 类工具的常见上限；CJK 内容更紧凑（约 75 KB），但仍然给主对话留了至少 90% 的 context。超出这个字节预算时，工具按整行截断（不切半行），末尾同样追加提示告知模型如何获取下一段。

为什么要分两层？第一层（行数）是正常使用的体验上限：模型浏览一个文件不应该被一万行的日志淹没。第二层（字节）是模型显式要求读取大量内容时的安全阀——防止一个 `limit: 100000` 把多 MB 的 yaml/json 整段写入上下文。两层各自覆盖不同的场景，模型触及任何一层都能收到清晰的下一步建议。

## 13.3 edit 的精确字符串匹配与唯一性校验

`edit` 的工作方式可以用一句话概括——在文件中找到 `oldString`，替换成 `newString`。但这里有一个关键的设计选择：`oldString` 必须在文件里恰好出现 1 次，否则报错。

> 源码：`packages/core/src/agent/tool-execution.ts::executeWriteTool`

<!-- check:ignore=punctuation -->
```ts
if (!replaceAll) {
  const count = countOccurrences(content, oldString)
  // 找不到
  if (count === 0) return toolErrorString(`old_string not found in ${filePath}`)
  // 找到多个
  if (count > 1)
    return toolErrorString(
      `old_string is not unique in ${filePath} (found ${count} occurrences). ` +
        `Provide more context or set replaceAll: true.`,
    )
}
```

两条错误路径都显式告诉模型如何自纠：找不到 → `oldString` 写错了；找到多个 → 要么补更多上下文让它唯一、要么设置 `replaceAll: true` 全部替换。模型读完错误信息几乎不需要额外思考下一步该做什么。

为什么这么严格？反过来看——如果允许模糊匹配（Fuzzy Match，即忽略空白差异、缩进差异等），会发生什么：模型生成的 `oldString` 缩进少一格，工具「好心」地匹配到另一段看上去差不多的内容，但那段其实不是模型本来想改的位置。表面替换成功，实际改错了文件；git diff 里看起来是个合理的改动，只是改的地方错了，缺陷极难发现。

精确匹配则相反——逐字节比较，连一个空格、一个 Tab、一个换行差异都不放过。任何不一致都立即失败、报错。「看似成功实则改错」这条最隐蔽的缺陷路径被直接消灭。这是一个典型的取舍：精确匹配会导致模型偶尔因为空白字符差异而失败（需要多一次 `readFile` 调用来获取准确内容），但这个代价远小于模糊匹配带来的「改错位置却不报错」的风险。工具描述里也有专门一句话提示模型避开最常见的失败——"Never include line number prefixes in oldString or newString"。把已知容易出错的地方写在工具描述里、写在错误信息里，模型即使失败一次也能很快恢复。

`replaceAll: true` 是给「重命名」这种确实需要全局替换的场景保留的：把变量 `oldName` 改成 `newName`，全文件 5 处都改——一次调用完成。这是一个显式选择——必须传 `replaceAll: true` 才进入全局替换模式，默认行为永远是「必须唯一」。这个分流避免了「以为只改一处、结果改了全部」的意外。

## 13.4 writeFile 与 edit 的分工边界

两个工具表面都是「改文件」，但用途严格分开：

| 场景               | 工具        | 原因                                                      |
| ------------------ | ----------- | --------------------------------------------------------- |
| 创建新文件         | `writeFile` | 文件不存在，没有可替换的 `oldString`，必须整段写          |
| 改老文件的一小段   | `edit`      | 只发改动的那段（diff），节省 token、保留 git diff 可读性  |
| 完全重写老文件     | `writeFile` | 改动太大，逐段 edit 反而碎；整段覆盖一次完成              |
| 老文件追加几行末尾 | `edit`      | 可以 `edit` 文件最后一段非空内容、把 `newString` 续在末尾 |

这条边界不是工具运行时强制的——`writeFile` 可以覆盖任何已存在的文件、`edit` 也可以替换任意大的字符串。分工规则由系统提示词引导：

> Prefer edit (string replacement) over writeFile when modifying existing files — it's safer and costs fewer tokens

> 源码：`packages/core/src/agent/system-prompt.ts`

为什么这条引导规则重要？假设一种最糟的情况：模型想修一个函数的小缺陷，但调了 `writeFile` 把整个 1000 行的文件重写一遍。这会导致三个问题。第一，token 浪费——模型需要把 1000 行原样在 `content` 里复述，输出 token 大几十倍。第二，复述出错的风险——模型抄文件原内容时可能漏一行、缩进错一格、改了个空格——而这些「无意识改动」和本意要修的缺陷混在一起，代码评审时很难区分。第三，git diff 失去可读性——原本应该是「只改了 5 行」的小 PR，现在 diff 显示「删了 1000 行 + 加了 1000 行」，评审者完全看不出哪些是有意改动。`edit` 不存在这些问题——它只发改动的那一段，git diff 干净利落。因此系统提示词引导模型「改老文件用 edit、建新文件才用 writeFile」。

`writeFile` 还有一个细节值得提及——自动创建中间目录：

<!-- check:ignore=punctuation -->
```ts
await fs.mkdir(path.dirname(filePath), { recursive: true })
await fs.writeFile(filePath, content, { encoding: 'utf-8', signal })
```

模型说要写 `src/format/date.ts`、但 `src/format/` 还不存在时，`writeFile` 不会报错，先把中间目录建好再写。这样模型不需要先调 shell 执行 `mkdir`、再调 writeFile，省一轮工具调用。

## 13.5 「先读后写」为什么是软约束

`writeFile` 和 `edit` 的工具描述里都包含类似的措辞：

> writeFile: "If this is an existing file, you MUST use the readFile tool first ... This tool will fail if you did not read the file first."
>
> edit: "You must use readFile at least once in the conversation before editing. This tool will error if you attempt an edit without reading the file."

读起来像运行时强制，但实际打开执行逻辑一看——没有任何强校验。工具层根本不维护「哪些文件被 readFile 过」这种状态。"will fail" 这句承诺是写给模型看的——只存在于工具描述和系统提示词中（"ALWAYS read a file before modifying it"），靠模型自觉遵守。

这个选择背后有两条权衡。第一，实测软约束已经足够有效。这里有一个微妙但关键的原因——软约束之所以管用，并不只是因为模型「听话」，而是因为先读后写有一条自我强制的路径。模型如果不读文件就直接调 `edit`，它写不出有效的 `oldString`。要让 `oldString` 在文件里恰好出现 1 次，模型必须知道文件长什么样；凭空猜测的 `oldString` 几乎必然触发 `old_string not found` 错误，模型下一步还是得调 `readFile`。同理，`writeFile` 改老文件时如果模型不读就直接整段覆写，等于在拿「自己脑补的内容」覆盖真实文件——即使真的发生，git diff 一眼能看出来，最坏情况是被发现而不是悄无声息地出错。因此系统提示词的 "ALWAYS read first" 加上工具描述的 "will fail" 加上 `edit` 的精确匹配——三层叠在一起，模型不读直接动手的代价大、读了再动手的成本小。模型自己会算这笔账。实测下来主流模型遵守率在 90% 以上，剩余不到 10% 的情况也基本会被 `edit` 工具的 `not found` 错误纠正回来。失败一次的代价（多一次 `readFile` 调用）远小于维护一套强校验机制的代价。

第二，强校验的实现复杂度不低。要维护「这次会话里哪些路径被 readFile 过」这份状态，还需要处理一系列边界情况：文件被 readFile 后又被外部修改（IDE 编辑了或 git pull 拉了新版本），这时模型按旧内容生成的 `oldString` 就失效了，需要做 mtime 追踪；同一文件读了又改、改了又读，状态机要正确；`/clear` 后这份状态要不要清空？`/resume` 加载历史会话时要不要恢复？这些都是真实的工程问题。X-Code CLI 当前的选择是不引入这套状态——保持工具的无状态特性（每次调用只看参数就够），靠系统提示词加 `edit` 的精确匹配自然约束。

不过，`edit` 工具内部自己会执行一次 `fs.readFile`（拿到当前文件内容才能做替换）——这一步发生在工具运行期间，是工具的实现细节。它与「模型在调 `edit` 之前是否调过 `readFile` 工具」是两回事：后者影响的是模型有没有看过文件内容、能不能写出准确的 `oldString`；前者只是工具自己获取文件原文用于执行替换操作。

## 13.6 图片与 PDF：content parts 走多模态视觉通道

`readFile` 工具描述里有一句容易忽略的话：

> This tool can read images (PNG, JPG, etc.) and PDFs — their content is presented inline.

图 13-1 展示了 `readFile` 处理不同文件类型时的完整分支路径：


![image.png](https://p3-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/031e25b132804348869f5c5b38829dbd~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1091&h=741&s=68048&e=png&b=ffffff)

> 图 13-1 readFile 对不同文件类型的处理路径——文本走行号截断，图片和 PDF 走 content parts 并由供应商兼容层按需降级

模型对一个 `.png` 文件调 `readFile` 时，工具内部会把文件读成 buffer 后做 `buffer.toString('base64')`——图片数据本身必须编码为 base64 字符串，因为二进制数据无法直接走 JSON 通道。但关键不在 base64 本身，而在这段 base64 被装到哪种「part」里。

> 源码：`packages/core/src/tools/read-file.ts`

<!-- check:ignore=punctuation -->
```ts
if (kind === 'image') {
  const buffer = await fs.readFile(filePath)
  return {
    type: 'content',
    value: [
      { type: 'text', text: `Loaded image: ${filePath}` },
      {
        type: 'image-data', // 关键：part 类型是 image-data 而不是 text
        data: buffer.toString('base64'),
        mediaType: mediaTypeFor(filePath), // 'image/png' / 'image/jpeg' / ...
      },
    ],
  }
}
```

差异要在 AI SDK 的 part 协议层面才能看清楚——同一段 base64 字符串，装到不同 part 里，下游供应商的解读完全不同：

| 装在哪种 part     | 供应商看到的内容                                      | token 计费                                   |
| ----------------- | ----------------------------------------------------- | -------------------------------------------- |
| `text` part       | 一长串 base64 字母数字（约 270 KB），当作普通文本处理 | 按文本 token 计——一张 200 KB 的图约 70K tokens |
| `image-data` part | 真正的图片——供应商调视觉子模型解读像素                | 按图像 token 计——一张图通常几百到几千 token  |

base64 内容是一样的，但前者供应商把它当文本读——模型只看到一长串字母数字，根本不知道是图片；后者供应商识别 part 类型后调用多模态视觉通道——模型真正「看到」图里的内容，token 还便宜十几倍。X-Code CLI 选择的是后者。

但这就引出下一个问题：不是所有供应商都接受 `image-data` part——DeepSeek、自定义 OpenAI 兼容端点这些纯文本供应商收到 `image-data` 会直接 400 报错。X-Code CLI 的解决方式是在工具层和供应商之间加一层适配——工具层不管当前用什么模型，`readFile` 看到图片永远输出 `image-data` part，从不判断当前供应商能否消化。降级由下游的供应商兼容层（Provider-Compat）接管：每次发 API 请求前，兼容层检查当前供应商的能力表，不支持图片的就用本地 OCR 把图片替换为提取出的文字，不支持 PDF 的则替换为占位文本。OCR 是降级方案——质量不如多模态原生看图，但避免了「图片直接发出去 → API 400 → 整轮对话中断」的最坏情况。兼容层的完整降级策略详见 Ch.09，两条图像入口（工具路径 vs 用户附件路径）的降级差异详见 Ch.35。

为什么这么分层？因为工具层不应该知道当前用什么模型——`readFile` 是个无状态工具，输入文件路径、输出 AI SDK 协议结构。当前会话用的是 Claude 还是 DeepSeek 是供应商兼容层的事。这种「工具只管输出标准结构，适配交给兼容层」的边界非常干净——新增供应商时只需要修改 capabilities 表和兼容层，不动任何工具代码。

`classifyFile` 决定一个文件归 text / image / pdf 哪一类——先看扩展名（`.png` / `.jpg` / `.pdf`），扩展名陌生时拿前几个字节比对 magic bytes（PDF 头是 `%PDF-`、PNG 头是 `\x89PNG`）。这种双层识别让模型对扩展名错误的文件（比如下载后扩展名被去掉的图片）也能正确分流。

X-Code CLI 还有另一条入口：用户直接在输入框里写一个文件路径（`@path` 或裸绝对路径），由 `extractFileReferences` 在提交消息时识别，不走 `readFile` 工具。这条路径的图像降级不走 OCR，而是借一家配了 key 的多模态模型生成 caption——完整的优先级顺序、caption 缓存和两条路径不对称的设计取舍详见 Ch.35。

## 13.7 小结

本章围绕一个「给 utils.ts 加函数」的场景走完了三个文件工具的典型分工，并讨论了五个关键设计决策。`readFile` 输出带行号前缀的文本，让模型能稳定引用位置，同时通过行数 2000 和字节 256 KB 两层上限防止一次读取占满 context window，截断信息里直接内嵌下一步建议让模型自行选择恢复路径。`edit` 采用精确字符串匹配加唯一性校验，任何空白差异都立即失败、`oldString` 必须恰好出现 1 次，从根本上消除了模糊匹配「改错位置却不报错」这条最隐蔽的缺陷路径。`writeFile` 与 `edit` 的分工不是工具运行时强制的，而是通过系统提示词引导模型遵守——「改老文件用 edit、建新文件才用 writeFile」——目的是节省 token 并保持 git diff 的可读性。「先读后写」同样是软约束：工具层不维护「哪些路径读过」的状态，靠系统提示词加工具描述加 edit 精确匹配三层叠加自然约束，强校验的实现复杂度远超收益。图片和 PDF 通过 `image-data` / `file-data` content parts 走多模态视觉通道，不支持多模态的供应商由兼容层自动降级为本地 OCR 文字，工具层与供应商能力完全解耦。
