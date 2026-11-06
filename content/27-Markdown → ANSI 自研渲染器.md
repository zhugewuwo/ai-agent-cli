# 第 27 章 Markdown → ANSI 自研渲染器

模型的输出中包含大量 Markdown 标记：标题、代码块、列表、表格、行内 code。在终端中渲染这些 Markdown 不能使用 [marked](https://marked.js.org/) 自带的 HTML renderer，因为终端不是浏览器，无法解释 HTML 标签。社区提供的 marked-terminal 等 terminal renderer 也不合适，因为颜色和缩进都是硬编码的，无法与产品的主题色统一。X-Code CLI 选择自己实现一个 Markdown → ANSI 渲染器（`render-markdown.ts`，当前约 400 行，外加主题色配置 `theme.ts` 约 200 行）。本章以一个具体的模型回复为线索，分为如下几个部分进行介绍。

- 27.1 节介绍整体设计思路：复用 `marked.lexer` 做解析，自研 renderer 输出 ANSI 转义序列。
- 27.2 节讨论主题色映射策略：三色窄色板如何覆盖非代码 token 的全部样式需求。
- 27.3 节讲解表格渲染的对齐算法：如何正确处理 CJK 字符占两列宽的问题。
- 27.4 节讨论流式场景下不完整 Markdown 的容错处理。
- 27.5 节介绍实际运行中的特殊处理：禁用删除线 tokenizer、列表嵌套缩进、OSC 8 超链接降级。
- 27.6 节给出本章小结。

假设用户输入「帮我把 `formatDate` 改成支持时区参数」，模型回复了这样一段 Markdown：

<!-- check:ignore=code-block-size -->

````markdown
## 修改方案

`formatDate` 目前只处理 UTC，改为：

```ts
function formatDate(date: Date, tz?: string): string {
  return dayjs(date).tz(tz ?? 'UTC').format('YYYY-MM-DD')
}
```

- 安装 `dayjs`
- 替换 `src/utils.ts` 中的实现

> 注意：单元测试需要同步修改。

详见 [dayjs 文档](https://day.js.org)
````

这段文本包含 h2 标题、行内 code、代码块、无序列表、引用块和超链接六种 token 类型。渲染器把它转换成 ANSI 转义序列后，终端上显示的效果如下（颜色以文字标注）：

<!-- check:ignore=code-block-size -->

```text
修改方案                                        ← h2: bold（不上色）

formatDate 目前只处理 UTC，改为：               ← codespan 蓝紫色 #99ccff

function formatDate(date: Date, tz?: string)    ← code: 逐行语法高亮（keyword/type/string 分色）
  return dayjs(date).tz(tz ?? 'UTC')...

• 安装 dayjs                                    ← • 蓝紫色，dayjs 蓝紫色
• 替换 src/utils.ts 中的实现                    ← • 蓝紫色，src/utils.ts 蓝紫色

▎ 注意：单元测试需要同步修改。                  ← 灰色 ▎ dim + 内容 italic

dayjs 文档                                      ← 浅蓝 #93a5ff + 下划线 + OSC 8 超链接
```

从原始 Markdown 到终端上的彩色文本，中间经过了哪些步骤？每种 token 的颜色为什么这样选？后续各节将围绕这段回复逐一展开。

## 27.1 整体思路：复用 lexer，自研 renderer

示例中的那段 Markdown 交给渲染器后，经历了怎样的转换流程？自研不意味着从零造轮子。`marked` 的 lexer 是一个稳定且仍在积极维护的 Markdown 解析器，X-Code CLI 把它当作「Markdown → AST」的前端，只复用解析能力；renderer 部分则完全自己实现，根据 token 类型输出 ANSI 转义序列。整条流水线如下：

<!-- check:ignore=code-block-size -->

```text
   原始 markdown 字符串
          │
          │ 先快速检测："这串文本里有没有 markdown 标记"
          ▼
   ┌─────────────────────────────────┐
   │ 没有 markdown 标记 → 直接返回原文 │   ← 纯文本短回复的常见场景
   └─────────────────────────────────┘
          │
          │ 有 markdown 标记
          ▼
   marked.lexer(text)
          │
          ▼
   Token AST（marked 自带类型）:
   [
     { type: 'heading', depth: 1, tokens: [...] },
     { type: 'paragraph', tokens: [...] },
     { type: 'code', lang: 'ts', text: '...' },
     { type: 'list', items: [...] },
     ...
   ]
          │
          │ tokens.map(formatToken)
          ▼
   带 ANSI 转义序列的字符串
   "\x1b[1m\x1b[3m\x1b[4m标题\x1b[0m\n..."
          │
          ▼
   process.stdout.write → 终端显示渲染后的样式
```

> 源码：`packages/cli/src/ui/render-markdown.ts`

<!-- check:ignore=punctuation -->
```ts
import { type Token, type Tokens, marked } from 'marked'
import { Chalk } from 'chalk'
const c = new Chalk({ level: 3 }) // 强制 24-bit 真彩色

// 快速路径：检测原文里有没有任何 markdown 标记
const MD_SYNTAX_RE = /[#*`|[>_~\-]|\n\n|^\d+\. |\n\d+\. /
function hasMarkdownSyntax(s: string): boolean {
  return MD_SYNTAX_RE.test(s.length > 500 ? s.slice(0, 500) : s)
}

export function renderMarkdown(text: string): string {
  if (!text) return ''
  configureMarked() // 一次性禁用删除线 tokenizer（详见后文）
  try {
    // 纯文本短回复跳过 lexer，避免无谓解析
    if (!hasMarkdownSyntax(text)) return text + '\n'
    const tokens = marked.lexer(text) as Token[]
    return tokens.map((t) => formatToken(t, 0, null, null)).join('')
  } catch {
    return text // lexer 偶尔抛错（不完整 markdown）→ 容错返回原文
  }
}
```

主入口先走快速路径：如果文本中不含任何 markdown 标记，就直接 `text + '\n'` 返回；命中 markdown 标记的输入才进入 lexer 解析，再逐个 token 调用 `formatToken` 转换为 ANSI 序列并拼接为最终输出。以示例中的回复为例，`marked.lexer` 会把 `## 修改方案` 解析成 `{ type: 'heading', depth: 2 }` token，`` `formatDate` `` 解析成 `{ type: 'codespan' }` token，代码块解析成 `{ type: 'code', lang: 'ts' }` token。每个 token 进入 `formatToken` 后，按 type 分支输出对应的 ANSI 转义序列。 <!-- check:ignore=punctuation -->

`formatToken` 是一个 ~200 行的大 switch，没有分发到独立的 `renderHeading` / `renderCodeBlock` 等函数，每个 case 直接内联处理，需要递归时再调 `formatToken` 自己。以 heading 和 codespan 两个 case 为例：

<!-- check:ignore=code-source,punctuation -->

```ts
function formatToken(
  token: Token,
  listDepth = 0,
  orderedListNumber: number | null = null,
  parent: Token | null = null,
): string {
  switch (token.type) {
    case 'heading': {
      const h = token as Tokens.Heading
      const content = (h.tokens ?? []).map((t) => formatToken(t, 0, null, null)).join('')
      if (h.depth === 1) return c.bold.italic.underline(content) + EOL + EOL
      return c.bold(content) + EOL + EOL  // 示例中的 “## 修改方案” 走这条分支
    }
    case 'codespan':
      return c.hex(CODE_INLINE)((token as Tokens.Codespan).text ?? '')
      // 示例中的 `formatDate` → 蓝紫色文字
    // ... blockquote / code / list / link / table 等十余种 case 见源码
  }
}
```

`listDepth` / `orderedListNumber` / `parent` 三个递归参数让嵌套结构能正确处理“列表项里嵌段落”、“段落里嵌 codespan”、“链接内部的 text token 不要再次着色”这些场景。

## 27.2 主题色映射

> 源码：`packages/cli/src/ui/render-markdown.ts` + `packages/cli/src/ui/theme.ts`

回到示例：`## 修改方案` 显示为白色加粗，`` `formatDate` `` 显示为蓝紫色，`dayjs 文档` 链接显示为浅蓝加下划线——这些颜色从哪里来？配色策略上 X-Code CLI 选了一种低饱和度的窄色板，只用三种颜色：蓝紫色（`BLUE_PURPLE = #99ccff`）、浅蓝（`SPINNER_BLUE = #93a5ff`）和深灰（`#999999`）。这三色覆盖了标题、行内 code、列表前缀、链接、引用块、表格框线等非代码 token 的全部样式需求。代码块的语法高亮使用独立的配色系统（`syntax-highlight.ts` 提供 keyword、string、comment 等多种 token 分色），不在这三色范围内。 <!-- check:ignore=punctuation -->

| 元素                     | 样式                                       | 颜色                          |
| ------------------------ | ------------------------------------------ | ----------------------------- |
| h1                       | `bold + italic + underline`                | 默认前景色（不上色）          |
| h2 / h3+                 | `bold`                                     | 默认前景色（不上色）          |
| 代码块（fenced）         | 语法高亮逐行（`highlightLine` 分语言着色） | 由 `syntax-highlight.ts` 决定 |
| 行内 code (`codespan`)   | 前景色                                     | `BLUE_PURPLE = #99ccff`       |
| 列表项前缀（`-` / `N.`） | 前景色                                     | `BLUE_PURPLE = #99ccff`       |
| 引用块（`>`）            | 行首 `▎` 深灰 dim + 内容 italic            | dim 灰条 + 默认前景           |
| 链接                     | 前景色 + underline + OSC 8 超链接          | `SPINNER_BLUE = #93a5ff`      |
| `hr` (`---`)             | `─` × 20 深灰                              | `#999999`                     |
| 表格边框                 | dim 深灰；表头 `bold`                      | `#999999`                     |

这份配色表有几个设计选择。h1 和 h2 都不使用品牌色，只用 `bold+italic+underline` 和 `bold` 的字重组合来区分层级，颜色留给真正需要视觉突出的内联元素（行内 code、链接、列表前缀）。代码块也没有背景色，`fenced code` 通过 `highlightLine` 逐行做语法高亮（按语言识别 keyword / string / comment），而不是整段刷一个底色，这样老终端不支持背景色变化时也能正常显示。行内 code 使用蓝紫色（`CODE_INLINE = BLUE_PURPLE`），与列表项前缀同色以形成视觉一致性。链接使用 `SPINNER_BLUE` 着色加下划线，并包裹 OSC 8 hyperlink 转义序列。支持 OSC 8 的现代终端会把整段文本渲染成可点击链接，不支持的老终端会忽略 `\x1b]8;;href\x1b\\` 转义序列，只保留下划线文字，自然降级。整套配色使用 24-bit 真彩色，主流终端（iTerm2 / Windows Terminal / Alacritty / WezTerm）都支持，老终端会降级到 256 色。

## 27.3 表格渲染

示例中的回复恰好没有表格，但模型经常输出 Markdown 表格来比较方案或列出参数。表格在终端里需要特别处理，原因在于 ANSI 颜色码不计入字符宽度，但 CJK 字符占两列宽，对齐算法不能简单用 `string.length`。

下面的例子展示了按 `string.length` 算宽度时的对齐错位（中文字符算 1 字符宽，但终端渲染占 2 列）：

```text
错误做法（按 string.length 算）：
   ┌────────────┬──────────┬──────┐
   │ 列名       │ 中文列   │ 数字 │   ← string.length 把"中文列"算成 3 字符
   │ hello      │ 世界    │ 42   │   ← 但终端实际占 6 列，右边线整体走位
   │ abc        │ 中文    │ 999  │
   └────────────┴──────────┴──────┘

正确做法（visualWidth：CJK 占 2 列）：
   ┌────────────┬──────────┬──────┐
   │ 列名       │ 中文列    │ 数字 │   ← "中文列"算成 6 列宽，padding 按 6 算
   │ hello      │ 世界     │ 42   │   ← 右边线对齐
   │ abc        │ 中文     │ 999  │
   └────────────┴──────────┴──────┘
```

实现思路用两个工具函数配合：

<!-- check:ignore=code-source,punctuation -->

```ts
// 算"可见宽度"：先去掉 ANSI 转义序列、再按 CJK 占 2 列算总宽
function displayWidthOf(tokens?: Token[]): number {
  const content = (tokens ?? []).map((t) => formatToken(t, 0, null, null)).join('')
  return visualWidth(stripAnsi(content)) // visualWidth 来自 text-width.ts
}

// 按目标列宽 + 对齐方向 padding
function padAligned(
  content: string,
  displayWidth: number,
  targetWidth: number,
  align: 'left' | 'center' | 'right' | null | undefined,
): string {
  const padding = Math.max(0, targetWidth - displayWidth)
  if (align === 'center') {
    const leftPad = Math.floor(padding / 2)
    return ' '.repeat(leftPad) + content + ' '.repeat(padding - leftPad)
  }
  if (align === 'right') return ' '.repeat(padding) + content
  return content + ' '.repeat(padding) // 默认左对齐
}
```

`stripAnsi` 去掉 ANSI 转义序列；`visualWidth`（项目自己维护的 `text-width.ts`，跟 cell buffer 的 `charWidth()` 共享同一份 CJK Unicode 表）算 CJK 实际占的 2 列；`padAligned` 支持 markdown 表格的左 / 中 / 右对齐符号（`:---:` / `---:`）。

表格框线用 Unicode box-drawing 字符（`┌ ┐ └ ┘ ├ ┤ ┬ ┴ ┼ │ ─`）由 `#999999` dim 灰着色；表头额外加粗；列宽按“表头 + 所有行同一列”取最大值。整套不依赖外部 markdown-table 类库。

## 27.4 流式场景下不完整 Markdown 的容错

回到示例：如果模型回复的那段 Markdown 是流式到达的，渲染器可能只收到 `` ```ts\nfunction fo `` 就需要渲染，此时代码块还没闭合。流式输出时 `renderMarkdown` 拿到不完整 Markdown 的情况并不罕见。下面是一个典型场景： <!-- check:ignore=punctuation -->

````text
流式中途收到的 buffer 文本：

   这是一段说明，下面是代码：

   ```ts
   function fo
   （文本到此为止，stream 还在继续）
````

代码块只开了 ` ``` ` 还没闭合，`marked.lexer` 解析到这里会抛错。整条容错链路：

<!-- check:ignore=code-block-size -->

````text
   renderMarkdown(text)
          │
          ▼
   try:
      marked.lexer(text) ──→ 抛错（code fence 未闭合 / 表格未闭合 等）
          │
          ▼
   catch:
      return text  ← 直接返回原文，UI 显示纯文本（不带样式）
          │
          ▼
   ChatInput 把这条 streamingChunk 消息写到终端历史区
   用户看到纯文本片段，但 UI 不会崩溃

   ── 下一段 stream 到达 ──
          │
          ▼
   新的 buffer = 上一次 + 后续 delta
       "...```ts\nfunction foo() {}\n```\n"
          │
          ▼
   findSafeBoundary 这次会在 fence 闭合后 \n 处切
          │
          ▼
   renderMarkdown 拿到完整片段，正常渲染
          │
          ▼
   UI 显示彩色代码块（追加在上一段纯文本后面）
````

容错逻辑就是前文 `renderMarkdown` 入口里的那个 try/catch：`marked.lexer` 抛错时直接返回原文。这条 catch 路径在实际运行中很少触发，原因在于 `findSafeBoundary`（Ch.26）已经保证了「切点不在未闭合结构里」，正常情况下 `renderMarkdown` 拿到的永远是 marked 能完整解析的片段。catch 路径主要覆盖两类极端情况：安全边界检测遗漏的 markdown 结构（例如某些复杂嵌套写法）、或 `marked.lexer` 自身的偶发 bug。渲染失败时直接返回原文，UI 显示的是不带样式的纯文本而不是崩溃。下一段 stream 通常会把缺失的结构补全，重渲一次就回到样式正常。

## 27.5 实际运行中的特殊处理

### 禁用删除线 tokenizer

Markdown 默认把 `~~text~~` 解析为删除线，但模型经常输出 `~/code/project`（路径）或 `~5 个错误`（约等于 N 的写法），前两个 `~` 会被错误地当成删除线起始，把后续文字纳入 strikethrough 渲染。X-Code CLI 的做法是整体禁用 `del` tokenizer：

```ts
// packages/cli/src/ui/render-markdown.ts:37
marked.use({
  tokenizer: {
    del() {
      return undefined as any
    }, // 禁用删除线 tokenizer
  },
})
```

代价是用户真正想用删除线时不会被渲染。但在实际场景中，「路径 / 约等于」的误判频率远高于「真正想用删除线」，这个权衡是值得的。

### 列表项：嵌套缩进 + bullet 字符按层级切换

回到示例：`- 安装 dayjs` 和 `- 替换 src/utils.ts 中的实现` 两行无序列表，经过渲染后变成了蓝紫色的 `•` 前缀加缩进。marked 默认把嵌套列表当 nested div 处理，缩进控制有限。产品代码按层级 + 2 空格手动缩进；bullet 字符走 `terminal-glyphs.ts` 的 `GLYPH_LIST_BULLET`（`•`，ConHost 上自动降级为 `-`）；ordered 列表按嵌套深度切换”数字 → 字母 → 罗马数字”。简化后的关键逻辑：

> 源码：`render-markdown.ts`（`GLYPH_LIST_BULLET` 由 `terminal-glyphs.ts` 导出）

<!-- check:ignore=punctuation -->
```ts
case 'list_item':
  return (token.tokens ?? [])
    .map((t) => `${'  '.repeat(listDepth)}${formatToken(t, listDepth + 1, orderedListNumber, token)}`)
    .join('')

case 'text': {
  // ... 在 list_item 上下文里追加 bullet
  if (parent?.type === 'list_item') {
    const marker = orderedListNumber === null
      ? c.hex(BLUE_PURPLE)(GLYPH_LIST_BULLET)                       // 无序：• (ConHost: -)
      : c.hex(BLUE_PURPLE)(`${getListNumber(listDepth, orderedListNumber)}.`)  // 有序：1. / a. / i.
    // ... 后面拼内容
  }
}
```

下面用一个示意图完整展示无序列表和有序列表在各层级的渲染结果。左侧是模型输出的 markdown 原文，右侧是终端实际显示的效果（颜色用文字标注）：

```text
markdown 原文                          终端渲染结果（缩进 + bullet/编号）
─────────────────────────              ─────────────────────────────────────
- 第一项                               • 第一项                    ← 第 1 层，无序: •
  - 嵌套项 A                             • 嵌套项 A                ← 第 2 层，无序: •
    - 更深一层                               • 更深一层            ← 第 3 层，无序: •
- 第二项                               • 第二项                    ← 第 1 层

1. 步骤一                              1. 步骤一                   ← 第 1 层，有序: 数字
   1. 子步骤                               a. 子步骤              ← 第 2 层，有序: 字母
      1. 细节                                  i. 细节            ← 第 3 层，有序: 罗马数字
2. 步骤二                              2. 步骤二                   ← 第 1 层

缩进规则：每层 +2 空格（'  '.repeat(listDepth)）
bullet 字符：无序统一用 • (ConHost 降级为 -)
编号样式：第 1 层 → 数字  |  第 2 层 → 字母  |  第 3 层 → 罗马数字  |  第 4 层+ → 回退数字
全部着色：BLUE_PURPLE (#99ccff)
```

按嵌套深度切换 ordered 列表样式的好处是：模型经常在同一段里写出三层嵌套列表，统一用 “1. 2. 3.” 从外到内重复会让读者难以判断当前在哪一层。数字、字母、罗马数字三种样式在视觉上形成足够的区分度，读者扫一眼前缀就能定位自己在第几层。

### 链接：OSC 8 超链接与降级

示例中的 `[dayjs 文档](https://day.js.org)` 渲染后在终端上显示为浅蓝色下划线文字，但这个链接能不能点击，取决于终端是否支持 OSC 8 协议。X-Code CLI 的做法是把「显示文字 + 跳转目标」封装进一段 OSC 8 转义序列，由终端自己决定如何呈现。同一段 ANSI 字节在两种终端上的行为如下：

```text
markdown 输入：
   [Anthropic](https://anthropic.com)
        │
        ▼
渲染后的 ANSI 字节流（同一份输出，不同的终端有不同的行为）：
   \x1b]8;;https://anthropic.com\x1b\\<underline + SPINNER_BLUE>Anthropic</...>\x1b]8;;\x1b\\

┌──────────────────────────────────────────────────────┐    ┌──────────────────────────────────────────────────┐
│ 支持 OSC 8 的现代终端                                 │    │ 不支持 OSC 8 的老终端                              │
│ (iTerm2 / WezTerm / Windows Terminal / Kitty 等)      │    │                                                  │
│                                                       │    │                                                  │
│  ✓ 识别 \x1b]8;;…\x1b\\ 序列                          │    │  ✗ 不识别 \x1b]8;;…\x1b\\，直接丢弃              │
│  ✓ 把中间的"Anthropic"渲染成可点击链接                │    │  ✗ 保留中间的"Anthropic"文字 + 下划线 + 蓝色      │
│                                                       │    │                                                  │
│  屏幕：Anthropic (带下划线 + 蓝色 + 鼠标变手型)        │    │  屏幕：Anthropic (带下划线 + 蓝色，但不能点击)    │
│  行为：Ctrl+Click 跳转到 https://anthropic.com        │    │  行为：靠下划线 + 颜色提示这是链接，URL 不可见     │
└──────────────────────────────────────────────────────┘    └──────────────────────────────────────────────────┘
```

实现代码：

<!-- check:ignore=code-source,punctuation -->

```ts
case 'link': {
  const l = token as Tokens.Link
  if (l.href?.startsWith('mailto:')) {
    return l.href.replace(/^mailto:/, '')  // mailto 直接退成裸邮箱
  }
  const linkText = (l.tokens ?? []).map((t) => formatToken(t, 0, null, l as Token)).join('')
  const href = l.href ?? ''
  const plain = stripAnsi(linkText)
  // 显示文字和 href 相同时（[https://x](https://x) 这种）只显示一份，避免冗余
  const styled = c.hex(LINK).underline(plain && plain !== href ? linkText : href)
  return href ? `\x1b]8;;${href}\x1b\\${styled}\x1b]8;;\x1b\\` : styled
}
```

这段代码首先解决的是链接去重问题：当显示文字与 URL 相同时（`plain !== href` 为假），只显示 URL 本体，避免 `[https://x.com](https://x.com)` 这种 markdown 渲染成重复的 `https://x.com (https://x.com)`。OSC 8 降级则是由终端自身完成的：支持 OSC 8 的终端识别这对转义序列，把整段文字渲染成可点击的超链接；不支持的终端忽略 `\x1b]8;;…\x1b\\` 转义，只保留中间的着色加下划线文字，与上面示意图中描述的行为一致。`mailto:` 链接做了特殊处理：直接退化成邮件地址裸文字，不加 OSC 8 包装也不加颜色，原因在于多数终端不支持 mailto 协议的 OSC 8 跳转。

## 27.6 小结

本章讨论了 X-Code CLI 的 Markdown → ANSI 自研渲染器。整体设计思路是复用 `marked.lexer` 做解析，自己实现 renderer。`formatToken` 是一个单一大 switch，每个 token 类型内联处理，需要递归时通过 `listDepth` / `orderedListNumber` / `parent` 三个参数下传上下文。配色策略选择低饱和度窄色板（蓝紫 + 浅蓝 + 深灰三色），标题靠字重分层而不上色，颜色留给行内 code、链接、列表前缀等需要视觉突出的内联元素。表格对齐算法使用 `stripAnsi` + `visualWidth` 按可见宽度计算，正确处理 CJK 字符占两列宽的问题。流式场景下的容错依赖外层 try/catch，`findSafeBoundary`（Ch.26）保证正常切点不在未闭合结构里，catch 路径极少触发，触发时直接返回原文，UI 不会崩溃。整个渲染器约 400 行代码加 200 行主题配置，体量不大但完全可控。
