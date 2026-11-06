# 第 35 章 文件附件与视觉降级

用户在 DeepSeek（一个完全不支持视觉的模型）下输入「帮我看下 `@D:\screenshots\login.png` 这个登录页有什么 UX 问题」。DeepSeek 无法处理图片，直接发送请求会被 API 拒绝。但用户既没有切换供应商也没有安装额外工具，模型仍然给出了基于这张图的具体 UX 建议。中间发生的事是：文件摄入（File Ingest）管线在消息拼装阶段解析出 `@` 路径引用的是一张图片，检测到当前供应商不支持视觉，于是从用户已配 API key 的多模态供应商中挑选一个（如 Gemini-2.5-Flash）充当视觉子 Agent，让它把图片描述成结构化的文字图注（包含可见文本、UI 元素、布局、配色），再把图注作为纯文本拼到原用户消息里交给 DeepSeek。DeepSeek 看到的从来不是图，而是另一家多模态模型替它「看过一遍」后写下的文字。

这条路径在 X-Code CLI 内部叫视觉降级（Vision Fallback）。更广义地说，file-ingest 管线负责把用户附加的各类文件（代码、PDF、Office 文档、图片）按供应商能力分流到不同的处理路径，最终统一拼成模型可以理解的消息内容。整个过程发生在消息拼装阶段，不走工具调用协议，也不走权限检查。本章将分为如下几个部分进行介绍。

- 35.1 节介绍两种附件语法（`@path` 和裸绝对路径）以及文件引用的提取过程。
- 35.2 节梳理文件类型分类策略和各类型的处理路径。
- 35.3 节讲解视觉降级子 Agent 的完整实现：供应商选择、图注生成和优先级顺序。
- 35.4 节讨论图注缓存机制和本地 OCR 托底策略。
- 35.5 节分析视觉降级的能力上限和文件附件不走权限检查的设计取舍。
- 35.6 节给出本章小结。

## 35.1 附件语法与文件引用提取

X-Code CLI 支持两种附件语法。`@path` 是显式附加，在路径前加 `@` 前缀；裸绝对路径是隐式识别，只要输入中出现带扩展名的绝对路径（Windows 盘符或 POSIX 根路径），管线自动将其视为文件引用。两种语法可以混用，也可以在一条消息中附加多个文件。

```bash
# @ 语法（显式附加）
> 解释 @D:\code\app\src\main.ts 中的 main 函数

# 裸绝对路径（需带扩展名）
> 总结 /home/me/report.pdf 的核心内容

# 多个附件
> 对比 @D:\v1.png 和 @D:\v2.png 的差异
```

输入 `@` 时，ChatInput 实时弹出文件补全菜单，随后续输入逐字筛选候选项：

```text
 > 解释一下 @compone█

   @packages/cli/src/ui/components/                ← 目录末尾带 /
   @packages/cli/src/ui/components/ChatInput.tsx   ← 高亮当前选中
   @packages/cli/src/ui/components/Dialog.tsx
   @packages/cli/src/ui/components/Spinner.tsx
   @packages/core/src/agent/sub-agents/components.ts
   ...
```

上下箭头切换候选项，Enter 或 Tab 选中并将完整的 `@<rel-path>` 替换进输入框（目录补 `/`，文件不补，方便继续输入子路径），Esc 关闭菜单。补全候选用 `scoreAndRank` 排序后取前 50 条（`MAX_AT_RESULTS = 50`）。无匹配时显示 `No matches`，按 Enter 仍将原始文本发出，方便提到 npm 包名或尚未创建的文件。

用户提交消息后，`extractFileReferences` 从输入文本中抽出所有文件引用：

> 源码：`packages/core/src/agent/file-ingest.ts`

<!-- check:ignore=punctuation -->

```ts
export function extractFileReferences(input: string): FileReference[] {
  const refs = new Map<string, FileReference>()
  // @path: 必须在行首或前面是空白，避免误匹配 @user@host 这类 token
  const atRegex = /(?:^|\s)@((?:[A-Za-z]:[\\/]|[\\/])[^\s]+|[^\s@][^\s]*)/g
  for (const m of input.matchAll(atRegex)) { /* ... */ }
  // 裸绝对路径: Windows 盘符 / POSIX 根 + 至少一个分隔符 + 扩展名
  const bareRegex = /(?:^|\s)((?:[A-Za-z]:[\\/]|\/)[^\s]*\.[A-Za-z0-9]{1,8})/g
  for (const m of input.matchAll(bareRegex)) { /* ... */ }
  return [...refs.values()]
}
```

每个文件引用经过解析后，按类型转换成文本、图片或文件三种消息片段之一，最终拼进用户消息中，用户原文在前、附件内容在后。

## 35.2 文件类型分类与处理路径

`classifyFile` 按扩展名和 magic bytes 将文件分为五类：

<!-- check:ignore=punctuation -->

| 类型       | 检测依据              | 示例扩展名                                           |
| ---------- | --------------------- | ---------------------------------------------------- |
| text/代码  | 60+ 扩展名白名单      | `.ts`、`.py`、`.go`、`.md`、`.json`、`.yml` ...      |
| image      | 扩展名 + magic bytes  | `.png`、`.jpg`、`.gif`、`.webp`、`.bmp`              |
| pdf        | 扩展名 `.pdf`         | 含文本层与扫描型进一步分流                           |
| office     | 扩展名                | `.docx`、`.xlsx`、`.pptx`、`.odt`、`.ods`、`.odp`   |
| unknown    | 以上都不匹配          | 未识别的扩展名报错                                   |

分类策略是扩展名优先：先在四张白名单（text、image、office、pdf）里查后缀，命中直接返回。只有当扩展名缺失或不在白名单时，才动态 `import('file-type')`（一个按文件头字节识别真实类型的库）按 magic bytes 进行二次判断。`file-type` SDK 体积不小，能不加载就不加载。这套顺序在常规场景下省去一次磁盘读取，扩展名被篡改的边界情况仍有 magic bytes 作为二次检测。各类型的处理路径如下。

（1）文本和代码文件。直接读取文件内容（UTF-8 编码），作为 TextPart 内联（带行号，与 readFile 工具一致）。

（2）PDF 文件。分两步处理。含可提取文本层的 PDF 先走本地 `pdf-parse`（一个纯 JS 的 PDF 文本提取库）抽出文本，节省 token 的同时保留主体内容。扫描型 PDF（仅图片层）在多模态供应商下作为 FilePart 原生发送，在 DeepSeek 等纯文本供应商下走本地 PDF 栅格化再经 tesseract OCR 转为文本。判断方式是先尝试 `pdf-parse` 抽取文本，得到的文本量与页数比例过低则判定为扫描型。

（3）Office 文件。各格式使用独立的解析库：docx 用 `mammoth`（把 docx 转成纯文本的库）抽取文本并保留段落结构和列表层级，xlsx 用 `xlsx`（Excel/CSV 通用解析库）读所有工作表转 CSV，pptx、odt、ods、odp 用 `officeparser`（多种 Office 格式的统一解析器）抽取文本。抽取结果统一作为 TextPart 注入，模型看到的是纯文本，不保留视觉格式。

（4）图片文件。按供应商的视觉能力（`capabilities.ts` 中的 `image: true/false` 标记）分流：多模态供应商直接以 ImagePart 原生发送；纯文本供应商（如 DeepSeek）走视觉降级路径，由 35.3 节详细讨论。

## 35.3 视觉降级子 Agent

> AI 知识背景：多模态模型如何处理图像
>
> 多模态 LLM（也叫 VLM，Vision-Language Model）内部采用「视觉编码器（Vision Encoder）+ 投影层（Projector）+ LLM 主体」三段式架构。（1）视觉编码器通常是 CLIP 类预训练的图像编码器（[OpenAI 2021](https://arxiv.org/abs/2103.00020)），把图像切成 patch，每个 patch 编码成一个高维向量。（2）投影层是一个小型网络（几层全连接或 cross-attention），把图像向量投影到 LLM 能消化的维度。（3）LLM 主体把投影后的图像向量当作「特殊 token」，和文本 token 拼成同一个序列，正常做注意力计算。图像在 LLM 看来就是一段特殊的 token，与文本 token 并行处理。这也解释了为什么图注（让多模态模型把图描述成文字）能近似替代多模态：图注模型先把图像内容翻译成自然语言描述，目标 LLM 再读这段文字。代价是目标模型无法直接访问原始图像，无法回答需要看图才能回答的追问（如「左上角按钮是什么颜色」），但语义信息得以保留。

> 源码：`packages/core/src/agent/vision-fallback.ts`

纯文本供应商不支持视觉，X-Code CLI 在文件摄入阶段就处理掉这个差异。`ingestFile` 检测到当前供应商不支持图片时，调用 `pickVisionProvider()` 从用户已配 API key 的多模态供应商中按优先级挑选一个，再用 `captionImage()` 让它把图片描述成结构化文字图注，最后作为 TextPart 注入消息体。整条链路如下：

<!-- check:ignore=code-block-size -->

```text
用户输入: 帮我看下 @login.png 这个登录页有什么 UX 问题
       │
       ▼  extractFileReferences  →  FileReference { path: 'login.png' }
       │
       ▼  classifyFile  →  type: 'image'
       │
       ▼  ingestFile: 当前供应商 (DeepSeek) supportsImage? → false
       │
       ▼  pickVisionProvider()  按 VISION_PRIORITY 找用户已配 API key 的多模态供应商
       │     └─ 命中: google (Gemini-2.5-Flash)
       │
       ▼  captionCache 查 ${modelId}:${size}:${first64bytes-b64}
       │     ├─ hit  → 复用上次图注
       │     └─ miss → captionImage() 调 Gemini 生成结构化描述
       │
       ▼  TextPart { text: "Login page with email/password fields, ..." }
       │
       ▼  拼进用户消息 content（用户原文字在前，图注在后）
       │
       ▼  DeepSeek 收到的请求：纯文本，看到图注但看不到图
       │
       ▼  终端：⎿  Captioned image via google:gemini-2.5-flash
```



`captionImage` 的实现如下：

<!-- check:ignore=punctuation -->
<!-- check:ignore=code-source -->

```ts
const { text } = await generateText({
  model,
  messages: [{
    role: 'user',
    content: [
      { type: 'text', text:
        'Describe this image in detail so a text-only AI can act on it. ' +
        'Include: (1) visible text transcribed verbatim, ' +
        '(2) UI elements, layout, and visual hierarchy, ' +
        '(3) colors, icons, shapes, and other visual details, ' +
        '(4) inferred purpose or context. ...' },
      { type: 'image', image: buffer, mediaType: mediaTypeFor(filePath) },
    ],
  }],
})
```

Prompt 要求视觉模型输出结构化描述，包含四个维度：逐字转录的可见文本、UI 元素和布局层级、颜色和图标等视觉细节、推断的用途或上下文。这四个维度覆盖了纯文本 LLM 在后续对话中可能需要的主要信息。

## 35.4 图注缓存与本地 OCR

同一张图在会话内可能重复出现（例如用户多次引用同一截图），每次都调用视觉模型会浪费 token 并增加延迟。`captionCache` 用 LRU 策略缓存图注结果：

<!-- check:ignore=code-source -->

```ts
const captionCache = new LruCache<string>({ maxEntries: 50 })

async function cacheKey(filePath: string, providerModelId: string): Promise<string> {
  const buffer = await fs.readFile(filePath)
  return `${providerModelId}:${buffer.length}:${buffer.subarray(0, 64).toString('base64')}`
}
```

缓存键用三段式结构 `${providerModelId}:${fileSize}:${first64BytesBase64}`：providerModelId 区分不同视觉模型的图注（同一张图给 Gemini 和给 GLM 的描述不一样，不能复用），fileSize 加前 64 字节的 Base64 作为内容指纹（与 OCR 路径共用同一套廉价指纹策略）。LRU 上限 50 条，同一张图在会话内重复出现时不会重复调用视觉模型。

走视觉降级时终端打印一行反馈，让用户知道使用了哪个辅助供应商：

```text
⎿  Captioned image via google:gemini-2.5-flash
```

如果视觉子 Agent 调用失败（网络错误、API 限流等），终端打印失败原因后自动回退到本地 OCR：

```text
⎿  Vision sub-agent (google:gemini-2.5-flash) failed: 429 Too Many Requests — falling back to OCR
```

如果用户一个视觉供应商都没有配置（DeepSeek 单 API key 用户最常见的情况），管线直接走本地 OCR，不打印额外提示。OCR 使用 [`tesseract.js`](https://github.com/naptha/tesseract.js)（Google Tesseract OCR 引擎的 JS 版本，纯本地运行不联网），仅能提取图中文字，无法描述布局或颜色。模型权重首次使用时自动下载到 `~/.x-code/tessdata/`（约 7.6 MB）。OCR 的输出与视觉降级的图注效果差距显著，但比完全无法读取图片内容强，至少模型能看到图中的文字。

除了文件摄入阶段的主动降级之外，X-Code CLI 在请求发送前还有一条被动安全网。每次调用模型之前，系统会扫描历史消息，检查是否残留了当前供应商无法处理的图片或二进制文件。这种残留最常见的场景是用户中途从多模态供应商（如 Gemini）切换到纯文本供应商（如 DeepSeek），早期对话里的图片内容在切换后仍然留在消息历史中。如果直接发送，API 会因为包含不支持的二进制内容而拒绝请求。安全网的处理方式是将这些残留的图片替换成本地 OCR 提取的文字。这条路径与 35.3 节的视觉降级互不依赖：视觉降级在消息拼装时处理新附件，安全网在请求发送前清理历史残留。

## 35.5 能力上限与设计取舍

视觉降级不是真正的多模态，而是图注加文本注入，有三个明确的能力上限。（1）图注是单向描述。目标 LLM（如 DeepSeek）无法对图反复追问，例如「左上角按钮什么颜色」这类需要直接访问像素的问题无法回答。（2）复杂 UI 还原和像素级布局校验场景下，纯文本描述可能丢失细节。（3）图表和流程图等结构化视觉内容，纯文本描述的效果较差。这些场景下，直接用 `/model` 切换到 Claude、Gemini 或 GLM-4V 等多模态模型是更好的选择。

文件摄入不走权限检查，这个设计有三方面理由。第一，读取文件本来就是 always-allow 级别（readFile 工具如此）。第二，用户手动附加的文件视为用户授权读取，与在终端中输入 `cat foo.txt` 等价。第三，在附加流程中强制权限检查会多一道确认打断，降低用户体验。

为什么要做视觉降级？这个机制专门针对只配了 DeepSeek 这类纯文本供应商 API key、没有任何多模态供应商的用户。这类用户想往对话里附加图片但模型本身看不到。视觉降级用一次轻量视觉模型调用加图注缓存，换来 DeepSeek 单 API key 用户的多模态能力补齐。代价是每张新图多一次 LLM 调用（缓存命中后免除），且图注是单向描述（能力上限见上文）。

## 35.6 小结

本章讨论了 X-Code CLI 的文件附件管线与视觉降级机制。`@path` 和裸绝对路径两种语法被 `extractFileReferences` 统一提取，每个文件引用由 `ingestFile` 按类型分流：文本和代码直接内联，PDF 按文本层和扫描型分别走本地解析或 OCR，Office 文档用独立解析库抽取纯文本，图片按供应商视觉能力分流到 ImagePart 原生或视觉降级路径。视觉降级的核心是从用户已配的多模态供应商中按 `google → zhipu → alibaba → openai → anthropic → moonshotai → xai` 优先级选一个充当图注子 Agent，生成结构化文字描述后作为 TextPart 注入消息。图注缓存用三段式键加 LRU 50 避免重复调用，无视觉供应商时回退本地 tesseract OCR（仅能提取文字）。`downgradeBinaryPartsForProvider` 作为独立安全网，在发送请求前清理历史消息中当前供应商无法接受的二进制内容。
