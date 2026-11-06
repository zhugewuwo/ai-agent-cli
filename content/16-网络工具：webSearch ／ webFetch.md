# 第 16 章 网络工具：webSearch 与 webFetch

代码库内部的搜索由 Ch.15 的三个工具覆盖，但许多任务需要查阅代码库以外的信息：某个第三方库的最新版本特性、Stack Overflow 上的错误排查方案、官方文档的最新写法。X-Code CLI 提供两个网络工具来满足这类需求：

（1）`webSearch`——按关键词搜索，返回候选 URL 列表和内容摘要

（2）`webFetch`——抓取指定 URL 的页面内容，将 HTML 转换为 Markdown 后返回模型

`webSearch` 封装了 Tavily 和 Brave 两家搜索 API，约 120 行代码；`webFetch` 用原生 `fetch` 获取页面后，通过 cheerio 和 turndown 将 HTML 转为 Markdown，加上服务端请求伪造（SSRF）防护、Cloudflare 降级和 LRU 缓存等机制，约 210 行代码。本章的重点在于这些通用的 HTTP 操作被封装为面向大语言模型的工具时做出的一系列选择：多后端切换策略、SSRF 防护、Cloudflare 反爬降级、流式字节上限和内容缓存。这些选择中的每一项都对应着一个真实的生产环境问题。本章分为如下几个部分进行介绍。

- 16.1 节用一个贯穿示例展示两个网络工具的协作过程：查 React 19 的 `useRef` 类型签名变化。
- 16.2 节讨论工具拆分的设计选择：搜索与抓取为什么是两种独立意图。
- 16.3 节讲解 `webSearch` 的 Tavily 优先与 Brave 回退策略。
- 16.4 节介绍 description 如何注入当前年份以引导模型搜索最新结果。
- 16.5 节拆解 `webFetch` 的 HTML → Markdown 转换管线。
- 16.6 节讨论 SSRF 防护的多层 URL 校验机制。
- 16.7 节介绍 Cloudflare 降级、流式读取与 LRU 缓存。
- 16.8 节给出本章小结。

## 16.1 贯穿示例：查 React 19 的 useRef 类型签名变化

假设用户输入：

> 帮我看一下 React 19 官方迁移指南怎么讲 `useRef` 类型签名变化的。我项目里 `useRef<HTMLDivElement>(null)` 升级 React 19 后到处报类型错误。

模型不知道 React 19 的发布博客具体在哪个 URL，第一步是搜索：

```jsonc
{
  "name": "webSearch",
  "args": { "query": "React 19 useRef type signature migration 2026" }
}
```

`webSearch` 调用 [Tavily](https://tavily.com/) API（用户配置了 `TAVILY_API_KEY`），返回 5 条候选结果，每条包含标题、URL 和内容摘要：

```text
### React 19 Upgrade Guide – React
https://react.dev/blog/2024/04/25/react-19-upgrade-guide
React 19 changed the signature of `useRef` to require an argument...

### useRef now requires an initial value · facebook/react · GitHub
https://github.com/facebook/react/pull/26416
RFC: ref() callback cleanup, useRef requires initial...

### React 19 ref types break TypeScript · DefinitelyTyped issue
https://github.com/DefinitelyTyped/DefinitelyTyped/issues/...

（剩下两条略）
```

模型读完 5 条摘要，判断第一条 react.dev 官方博客最相关，于是发出第二个工具调用来抓取页面正文：

```jsonc
{
  "name": "webFetch",
  "args": {
    "url": "https://react.dev/blog/2024/04/25/react-19-upgrade-guide",
    "prompt": "useRef type signature changes"
  }
}
```

`webFetch` 的执行过程依次经过以下步骤：对 URL 做安全校验（长度、协议、私有 IP 检查），查 LRU 缓存（15 分钟内同一 URL 直接命中），缓存未命中时用浏览器 User-Agent 发起 HTTP 请求（超时 15 秒），检查是否被 Cloudflare 拦截（403 + `cf-mitigated` 响应头时会换成如实标注的 CLI UA 重试），流式读取响应体（累计字节超过 10 MB 时立即取消），然后用 cheerio 移除导航和页脚等噪声节点并提取 `<main>` 或 `<article>` 主体内容，用 turndown 将 HTML 转为 Markdown，截断到 100 KB 上限后写入缓存并返回模型。图 16-1 展示了这条管线的完整流程，读者可以在阅读后续各节时回溯对照。


![image.png](https://p1-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/27faa5de140e471ab3c4c64daa97c71e~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=925&h=1008&s=55637&e=png&b=ffffff)

图太长，截了两次：

![image.png](https://p9-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/06c8034734de4c02b4bfb0c146b9364b~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=771&h=867&s=49425&e=png&b=ffffff)

<center>图 16-1 webFetch 执行管线</center>

返回结果（节选）如下： <!-- check:ignore-line -->

````markdown
# Content from https://react.dev/blog/2024/04/25/react-19-upgrade-guide

## React 19 Upgrade Guide

### `useRef` now requires an argument

In TypeScript, `useRef<T>` now requires an initial value...

```ts
// React 18 的写法
const ref = useRef<HTMLDivElement>(null)

// React 19 的写法——类型需要显式包含 null
const ref = useRef<HTMLDivElement | null>(null)
```

---

Extract instruction: useRef type signature changes
````

模型基于这段内容回答用户：在 React 19 中 `useRef<T>(null)` 需要改为 `useRef<T | null>(null)`，并给出批量迁移的正则替换方案。整轮交互演示了网络工具的核心模式——搜索获取候选列表，抓取获取页面正文，模型读取 Markdown 后决定下一步行动。下面各节展开关键设计决定。

## 16.2 工具拆分：搜索与抓取是两种独立意图

搜索和抓取也可以合成一个「网页查询工具」——给关键词，工具自动完成搜索、选 URL、抓取页面、提取要点，一条龙返回结果。X-Code CLI 将其拆成 `webSearch` 和 `webFetch` 两个独立工具，理由和 Ch.15 三个搜索工具的拆分逻辑一致：两件事是不同的意图，模型选择哪个工具本身就表达了它的目的。`webSearch` 的意图是「列出候选」——模型还不知道答案在哪个 URL，需要看 5～10 条候选的标题和摘要先做判断；`webFetch` 的意图是「抓取已知 URL 的全文」——模型已经决定要读哪一页，需要的是页面正文而非搜索结果列表。

如果将两者合成一个工具，模型在每次调用前都必须先区分「我现在该搜索还是该抓取」，然后去填写互斥的参数（`mode: 'search' | 'fetch'`，`query` 和 `url` 二选一），参数填错的可能性随之上升。两个独立的 schema 让每种意图各自对应一组简洁的参数，不存在互斥和歧义。此外，两条路径的下游成本特征也完全不同：`webSearch` 的开销是远程 API 计费（Tavily 和 Brave 各有配额限制），`webFetch` 的开销是网络带宽加上 LRU 缓存（同一 URL 15 分钟内免费复用）。拆开之后两条路径各自管理自己的成本约束，不会互相干扰。

## 16.3 webSearch：Tavily 优先与 Brave 回退

`webSearch` 的执行逻辑很简洁——检测用户配置了哪家 API Key，按固定优先级调用：

> 源码：`packages/core/src/tools/web-search.ts`

<!-- check:ignore=punctuation -->
```ts
execute: async ({ query, maxResults }, { toolCallId }) => {
  const n = maxResults ?? 5
  const hasTavily = !!process.env.TAVILY_API_KEY
  const hasBrave = !!process.env.BRAVE_API_KEY

  if (!hasTavily && !hasBrave) return buildMissingKeyError()

  reportProgress(toolCallId, `Searching: ${query}`)
  try {
    const results = hasTavily
      ? await searchWithTavily(query, n)
      : await searchWithBrave(query, n)
    return formatResults(results)
  } catch (err) {
    return formatToolError(
      `searching (${hasTavily ? 'Tavily' : 'Brave'})`, err
    )
  }
}
```

优先级规则很简单：配置了 `TAVILY_API_KEY` 就用 Tavily，否则用 Brave，两家都没配则返回 `buildMissingKeyError()` 生成的友好错误信息。两家搜索 API 的差异如下表所示：

| 项目 | Tavily | Brave |
|---|---|---|
| 返回内容 | 经过 LLM 优化的清洗摘要 | 通用搜索引擎描述片段 |
| 每条 token 消耗 | 较低（已过滤导航、广告等噪声） | 较高（接近常规搜索引擎描述片段的长度） |
| 调用方式 | `@tavily/core` 官方 SDK（动态 `import`） | 原生 `fetch` 直接调用 REST API |
| 免费额度 | 1000 次/月 | 约 1000 次（$5 免费额度，需绑信用卡，超额扣费） |

Tavily 被设为优先选项的原因在于它是专门为大语言模型用例设计的搜索 API，返回的 `content` 字段已经过滤了站内导航和广告等噪声，模型读取时更直接、token 消耗更少。Brave 是更通用的搜索引擎 API，`description` 字段是常规搜索引擎的描述片段，没有针对大语言模型做特别清洗，但作为回退方案完全足够。X-Code CLI 没有引入 DuckDuckGo 或 Google：DuckDuckGo 没有官方搜索 API，只能抓取 HTML 页面，违反服务条款且容易被封 IP；Google Custom Search API 注册门槛高，需要绑信用卡、创建 Custom Search Engine，免费配额也很小。两家已经足以覆盖目标用户群体。

`@tavily/core` SDK 通过 `await import('@tavily/core')` 动态加载，只有在 Tavily 优先路径上才会触发 import。没有配置 `TAVILY_API_KEY` 的用户在启动时不会加载这个 SDK，不产生额外的包初始化开销。Brave 路径走原生 `fetch`，完全没有外部依赖。

### buildMissingKeyError：按当前 shell 生成配置命令

两家 API Key 都没有配置时，工具不只是返回一条「请配置 Key」的错误消息，而是返回一段包含具体操作步骤的引导信息：

<!-- check:ignore=punctuation -->
```text
Error: WebSearch requires an API key. Two free options (set either one):

  1. Tavily — 1000 searches/month, recommended
     Sign up: https://tavily.com → copy API key from dashboard

  2. Brave  — ~1000 searches/month via $5 free credit (requires credit card, over-usage billed)
     Sign up: https://api.search.brave.com → create API key

Setup (powershell):
  # current session:
  $env:TAVILY_API_KEY = "tvly-xxx"
  $env:BRAVE_API_KEY  = "BSA-xxx"
  # persistent (new shells):
  [Environment]::SetEnvironmentVariable("TAVILY_API_KEY","tvly-xxx","User")
  ...

After setting, restart this shell for the variable to take effect.
```

其中 `Setup (powershell)` 这一段是根据当前 shell 类型动态生成的。`buildMissingKeyError` 内部调用 `getShellProvider().type` 获取当前 shell 类型：PowerShell 环境下生成 `$env:` 和 `[Environment]::SetEnvironmentVariable` 命令；bash 环境下生成 `export` 和 `~/.bashrc` 追加命令；zsh 环境下改为 `~/.zshrc`。模型读到这段错误信息后会原样转发给用户，用户拿到的是直接可以复制粘贴的命令，从「发现错误」到「完成修复」之间没有额外的查阅步骤。这是工具错误信息设计的一条通用经验：错误消息中包含可执行的修复指令，比单纯的「请配置 X」有效得多。

## 16.4 description 注入当前年份

`webSearch` 和 `webFetch` 的 description 都做了同一件事——在工具描述中注入当前年份：

> 源码：`packages/core/src/tools/web-search.ts`

<!-- check:ignore=punctuation -->
```ts
const YEAR = new Date().getFullYear()

description:
  `Search the web for information. ... ` +
  `The current year is ${YEAR} — use it whenever the user asks ` +
  `for recent/latest/current information ` +
  `(e.g. prefer "React 19 release notes ${YEAR}" ` +
  `over "React latest release notes").`,
```

为什么要把年份显式告诉模型？因为模型本身不知道「今年」是哪一年——它的训练数据有截止时间，无法自动更新。如果让模型自由构造搜索查询，它倾向于使用 `React latest release notes` 这种没有年份锚定的表述，而搜索引擎对这类查询返回的往往是累计链接权重更高的旧页面（2023 或 2024 年的文档）。显式告诉模型当前年份，并给出具体的改写示范（prefer `React 19 release notes 2026` over `React latest release notes`），让模型在构造查询时自带时效性关键词，搜索结果的新鲜度随之提升。这是一个成本极低的优化：一行 description 改写，零运行时开销，效果却很明显。`webFetch` 的 description 中也注入了同一年份，让模型在决定是否需要重新抓取某个页面时（例如「之前读过的 react.dev 文档是否有更新」）有时间参考。

## 16.5 webFetch 的 HTML → Markdown 转换管线

`webFetch` 不是直接把 HTML 原文返回模型，而是经过一条三阶转换管线（Pipeline）：

> 源码：`packages/core/src/tools/web-fetch.ts`

<!-- check:ignore=punctuation -->
```ts
const $ = cheerio.load(body)

// 阶段 1：移除导航、广告等噪声节点
$('script, style, nav, footer, header, aside, ' +
  '.sidebar, .nav, .menu, .ads, .advertisement').remove()

// 阶段 2：优先提取主体内容容器，未匹配到时回退到 body
const mainContent =
  $('main, article, .content, .post, #content').first()
const html = mainContent.length
  ? mainContent.html() : $('body').html()

// 阶段 3：turndown 将 HTML 转为 Markdown
let markdown: string = turndown.turndown(html)
```

每个阶段都在提升信号与噪声的比值。阶段 1（清除噪声）移除 `<script>`、`<style>`、`<nav>`、`<footer>`、`<aside>` 等几乎所有网页都存在的固定结构噪声，以及 `.ads`、`.advertisement`、`.sidebar` 等常见的广告和侧边栏 class。阶段 2（主体提取）优先选取 HTML5 语义标签 `<main>` 和 `<article>` 作为内容容器，这两个标签在大多数文档站点中包裹了页面的核心正文；此外还尝试 `.content`、`.post`、`#content` 等社区中广泛使用的约定 class 和 id。如果所有候选容器都未匹配到，才回退到整个 `<body>`。阶段 3（格式转换）使用 turndown 库将 HTML 转为 Markdown，配置为 `atx` 标题风格（`# / ## / ###`）和 `fenced` 代码块风格（` ``` `）。Markdown 比 HTML 的体积通常压缩一半以上，同时保留了标题层级、代码块、列表和链接等语义结构，是面向大语言模型的最佳中间表示格式。

为什么不直接返回 HTML 让模型自行解析？因为即使经过主体提取，HTML 仍然包含大量模型不需要的结构噪声：class 属性、`data-*` 属性、深层嵌套的 `<div>`、内联 SVG 等。Markdown 化去除了这些噪声，让模型能够把上下文窗口的容量集中在真正有信息量的内容上。有一个例外情况：当响应的 `Content-Type` 是 `application/json` 时，`webFetch` 跳过整条 HTML 转换管线，直接将原始 JSON 字符串截断到 100 KB 后返回。JSON 本身已经是结构化数据，再经过 cheerio 和 turndown 处理反而会破坏其结构。

## 16.6 SSRF 防护：多层 URL 校验

`webFetch` 是模型可以触发的任意 HTTP 请求——这意味着模型有可能被诱导（通过提示词注入）向内网地址发送请求，例如 `http://localhost:8080/admin`、`http://169.254.169.254/latest/meta-data/`（AWS、GCP 和 Azure 共用的实例元数据服务地址）或 `http://192.168.1.1/router`。如果不做防护，攻击场景包括通过提示词注入让模型抓取内网地址泄露环境信息、读取云厂商元数据服务获取实例角色凭据、以及通过响应耗时差异实现内网端口扫描。防护逻辑集中在 `validateFetchUrl` 函数中，在请求发出之前执行五层校验：

> 源码：`packages/core/src/tools/web-fetch.ts`

```ts
const PRIVATE_IP_PATTERNS = [
  /^127\./,                        // loopback
  /^10\./,                         // 10.0.0.0/8
  /^172\.(1[6-9]|2\d|3[01])\./,   // 172.16.0.0/12
  /^192\.168\./,                   // 192.168.0.0/16
  /^169\.254\./,                   // link-local (AWS/GCP metadata)
  /^0\./,                          // 0.0.0.0/8
  /^::1$/,                         // IPv6 loopback
  /^fd[0-9a-f]{2}:/i,             // IPv6 ULA
  /^fe80:/i,                       // IPv6 link-local
]

export function validateFetchUrl(url: string): string | null {
  if (url.length > MAX_URL_LENGTH)
    return `URL exceeds ${MAX_URL_LENGTH} character limit`
  let parsed: URL
  try { parsed = new URL(url) } catch { return 'Invalid URL' }
  if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:')
    return `Unsupported protocol: ${parsed.protocol} (only http/https allowed)`
  if (parsed.username || parsed.password)
    return 'URLs with embedded credentials are not allowed'
  const parts = parsed.hostname.split('.')
  if (parts.length < 2)
    return `Hostname "${parsed.hostname}" is not a public domain (must have at least two segments)`
  if (isPrivateHost(parsed.hostname))
    return `Fetching private/internal address "${parsed.hostname}" is blocked for security`
  return null
}
```

五层校验各防范一种攻击向量：

（1）URL 长度不超过 2000 字符（`MAX_URL_LENGTH = 2000`），阻断携带超长攻击载荷的 URL。

（2）协议必须是 `http:` 或 `https:`，拒绝 `file:`、`gopher:`、`ftp:` 等非 Web 协议。

（3）不允许 URL 中嵌入凭据（`http://user:pass@host` 形式），防止模型被诱导在 URL 中泄露用户名和密码。

（4）hostname 至少包含两个以点分隔的段，单段的 `localhost` 或任意单词（可能解析到 hosts 文件中的本地映射）一律拒绝。

（5）私有 IP 黑名单（9 条正则），覆盖 IPv4 的 loopback、三个私有地址段、link-local 段以及 IPv6 的 loopback、ULA 和 link-local 地址。

其中 `169.254.0.0/16`（link-local 段）这条尤其重要。`169.254.169.254` 是 AWS、GCP 和 Azure 共用的实例元数据服务地址，在任何云虚拟机上 fetch 这个地址都能获取当前实例角色的临时凭据。X-Code CLI 如果在云开发环境中运行而漏掉这条黑名单，攻击者可以通过提示词注入让模型读取凭据并泄露到外部。`isPrivateHost` 函数除了匹配 IP 地址之外，还额外拦截三类 hostname：`localhost`、以 `.local` 结尾的 mDNS 局域网名称、以及以 `.internal` 结尾的云厂商内部域名。

这套校验有一个已知的局限：校验在请求发出之前执行，不检查重定向目标。如果一个公开 URL 通过 HTTP 302 重定向到 `127.0.0.1`，`fetch` 配置的 `redirect: 'follow'` 会跟随过去，校验层无法拦截。要彻底堵住这条路径需要改为 `redirect: 'manual'` 并在每一跳都做校验，但这样做会显著增加实现复杂度，当前版本选择接受这个边界。

## 16.7 Cloudflare 降级、流式读取与 LRU 缓存

### Cloudflare 降级

许多网站通过 Cloudflare CDN 做爬虫拦截。当 Cloudflare 决定对一次请求发起质询时，会返回 `403 Forbidden` 状态码并附带一个 `cf-mitigated` 响应头。`webFetch` 检测到这两个条件同时满足时，改用一个如实标注身份的 CLI User-Agent 重试一次： <!-- check:ignore-line -->

<!-- check:ignore=punctuation -->
```ts
const BROWSER_UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) ... Chrome/130 ...'
const FALLBACK_UA =
  `x-code-cli/${VERSION} (+https://github.com/woai3c/x-code-cli)`

let response = await doFetch(url, BROWSER_UA)
if (response.status === 403
    && response.headers.get('cf-mitigated') !== null) {
  response = await doFetch(url, FALLBACK_UA)
}
```

为什么「假装是浏览器」反而被拦截，「如实表明身份」反而能通过？原因在于 Cloudflare 的反爬规则通常包含 TLS 指纹检查。浏览器 UA 暴露了「我自称是 Chrome」的意图，但 Cloudflare 看到的 TLS 握手指纹并不像真实 Chrome（Node.js 的 `fetch` 使用 OpenSSL 默认的 cipher suite 顺序），于是命中「伪装浏览器」的拦截规则被拒绝。反过来，如实标注的 CLI UA 自报家门——「我是一个 CLI 工具，用 Node.js fetch」，Cloudflare 的规则集通常对已识别的爬虫和 CLI 工具放行（许多文档站点欢迎 curl、wget 等自动化工具的访问）。这是一个反直觉但在实践中非常有效的降级策略。`FALLBACK_UA` 中的版本号取自项目的 `VERSION` 常量（从 `version.ts` 导入），而非硬编码的数字，确保版本信息与实际发布保持同步。

### 流式读取与 10 MB 字节上限

`fetch` 返回的 Response 如果用 `.text()` 一次性读取完整响应体，当响应体很大时会耗尽进程内存。尤其在 chunked transfer encoding 下，服务端不发送 `content-length` 头，客户端无法预判响应体大小。`webFetch` 采用流式读取方式，边读取边累计字节数：

> 源码：`packages/core/src/tools/web-fetch.ts`

<!-- check:ignore=punctuation -->
```ts
async function readResponseBody(
  response: Response, maxBytes: number
): Promise<string> {
  const reader = response.body?.getReader()
  if (!reader) return response.text()
  const chunks: Uint8Array[] = []
  let totalBytes = 0
  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    totalBytes += value.byteLength
    if (totalBytes > maxBytes) {
      await reader.cancel()  // 立即取消，不再读取后续数据
      break
    }
    chunks.push(value)
  }
  // 将收集到的 chunks 拼接为不超过 maxBytes 的字节数组后解码
  // ...
}
```

`MAX_HTTP_BYTES = 10 * 1024 * 1024`（10 MB）是任何单次 fetch 的响应体上限，配合三层防御机制共同生效：

（1）`content-length` 头检查。如果服务端声明的响应体大小超过 10 MB，在读取之前直接拒绝。

（2）流式读取累计超过 10 MB 时立即取消。当 `content-length` 缺失或服务端给出不准确的值时，这层限制仍然有效。

（3）Markdown 化后的最终字符上限 `MAX_CONTENT_CHARS = 100_000`。10 MB 的原始 HTML 经过 cheerio 清洗和 turndown 转换后通常压缩到 1～3 MB，再截断到 100 KB（约 25K token，相当于 Sonnet 200K 上下文窗口的 12%）返回模型，单次 fetch 不会占据过大的上下文比例。

### 15 分钟 LRU 缓存

```ts
const CACHE_TTL_MS = 15 * 60 * 1000  // 15 分钟
const CACHE_MAX_ENTRIES = 50          // 最多 50 条
```

缓存参数的选择基于实际使用场景。模型在同一会话中反复读取同一文档页面是很常见的行为——例如先 fetch 一次官方文档了解全貌，写了几行代码后再 fetch 同一页查找具体细节。这种「5 分钟内读 3 次同一页面」的场景下，缓存命中能省去 3 次 HTTP 往返和 3 次 turndown 转换。15 分钟的过期时间让「边写代码边查文档」的工作流全程命中缓存，同时不至于让缓存中保留数小时前的过期内容。50 条的容量上限防止模型连续抓取大量 URL 时将缓存撑到占用过多内存。

缓存的 key 是 URL 的字面量字符串，不做归一化处理。这意味着模型在 URL 末尾多加一个 `?utm_source=xxx` 查询参数会绕过缓存，但反过来也避免了「归一化后命中缓存但实际内容已变化」的不一致问题。

## 16.8 小结

本章围绕 `webSearch` 和 `webFetch` 两个网络工具，讨论了它们从意图拆分到运行时防护的一系列设计决策。搜索与抓取拆成两个独立工具，遵循的是 Ch.15 确立的「不同意图对应不同工具」准则，两条路径的下游成本特征（API 计费与带宽加缓存）也完全不同。`webSearch` 采用 Tavily 优先、Brave 回退的多后端策略，两家 API Key 都未配置时返回按当前 shell 类型生成的具体配置命令，让用户从「发现错误」到「完成修复」之间没有额外的查阅步骤。description 中注入当前年份是一个成本极低但效果明显的优化，让模型在构造搜索查询时自带时效性关键词。`webFetch` 的 HTML → Markdown 三阶管线（噪声清除、主体提取、格式转换）将 HTML 的体积压缩一半以上，同时保留标题层级和代码块等语义结构。SSRF 防护通过五层校验在请求发出之前拦截内网访问，其中 `169.254.0.0/16` 段对云环境尤为重要；重定向目标未做校验是已知的局限。Cloudflare 降级策略利用了一个反直觉的现象——如实标注身份的 CLI UA 比伪装浏览器的 UA 更容易通过反爬规则。流式读取加 10 MB 字节上限防止大响应体耗尽进程内存，15 分钟 LRU 缓存则让同一会话中反复读取同一页面免去重复的网络请求和格式转换开销。下一章将讨论工具结果的处理机制——工具返回的原始数据如何被截断、格式化并写入消息历史。
