# 第 40 章 插件 marketplace 与分发

上一章讲完了插件从安装到运行的完整生命周期。但那一章从头到尾都假设用户已经拿到了插件的名字和来源，直接执行 `xc plugin install` 即可。现在的问题是：用户从哪里发现有哪些插件可以安装？给定一个插件名字之后，CLI 又如何找到它的 git 源？Marketplace 这一层把「哪些插件值得安装」的发现职责跟「安装」的执行职责分开。Marketplace 本身是一份按 git 仓库分发的插件目录（`marketplace.json`）：X-Code CLI 不自己运营 marketplace，用户订阅别人维护的 marketplace 仓库，CLI 在本地缓存索引并组合查询。默认预置的 `anthropic-marketplace` 指向 `github:anthropics/claude-plugins-official`，里面收录了 200 多个真实插件。本章从一次端到端的订阅 → 安装流程入手，再逐个展开 marketplace 的四个设计决定。本章将分为如下几个部分进行介绍。

- 40.1 节以官方 `anthropic-marketplace` 为例，走完从订阅到安装的端到端流程。
- 40.2 节讨论 marketplace 身份的两层命名（本地别名与上游 name）以及保留名冒充防御。
- 40.3 节讲解 `marketplace.json` 里 插件条目（plugin entry）的 4 种主流来源（source）形态（加上 4 种少见形态共 8 种），以及归一化层如何把它们统一成一种内部表示。
- 40.4 节说明 sha 锁定（pinning）的安全动机、校验逻辑和 7-40 字符 hex 格式验证。
- 40.5 节讨论拉取 marketplace 索引的两条路径：HTTPS 直链与 git 浅克隆（shallow clone）。
- 40.6 节给出本章小结。

> AI 知识背景：插件市场（marketplace）/ 扩展注册表（extension registry）
>
> 让用户发现并安装第三方扩展的「插件目录」在工具生态里有很多先例：npm registry（中心化、Node.js 包）、PyPI（中心化、Python 包）、VS Code Marketplace（中心化、VS Code 扩展）、Homebrew tap（分布式、每个 tap 是一个 git 仓库）。这些机制大致分两类。
>
> 中心化注册表（npm、PyPI、VS Code Marketplace）有官方运营方维护服务、托管所有包元数据。优点是一致性高、易搜索；缺点是要发包流程、依赖第三方服务可用性。
>
> 分布式目录（Homebrew tap、Chrome 企业策略 等）让用户订阅不同来源的目录文件。优点是没有中心运营方、用户能选自己信任的源；缺点是多个目录的内容会有重复或冲突。
>
> X-Code CLI 选第二种。Marketplace 是别人 GitHub 仓库里的一份 `marketplace.json`，用户订阅一个或多个 marketplace 仓库，CLI 在本地缓存它们的索引并组合查询。这跟 Claude Code 的 plugin marketplace 设计一致，方便接收对方生态里现成的 marketplace（最大的一份是 `anthropics/claude-plugins-official`，含 200 多个官方与社区插件）。

## 40.1 一个完整的例子：从订阅到安装

新装的 CLI 启动后，默认订阅列表里已经有一份 `anthropic-marketplace`，这是 CLI 启动时自动预置的。先看用户视角能做什么：

```text
> /plugin marketplace list
⎿ Subscribed marketplaces (1):
    anthropic-marketplace  github:anthropics/claude-plugins-official [official]
```

`[official]` 标签表示这个 marketplace 用的是保留名、source 已经被校验过指向了 Anthropic 官方组织（详见 40.2 节）。`source` 字段是 `github:anthropics/claude-plugins-official`，这是 git 短形态，相当于 `https://github.com/anthropics/claude-plugins-official.git`。

订阅了不代表已经拉取过索引。刚启动时本地缓存可能还是空的（或上次启动留下的旧版本）。`refresh` 命令拉取最新索引：

```text
> /plugin marketplace refresh anthropic-marketplace
⎿ Refreshing 1 marketplace …
    ✓ anthropic-marketplace — 203 plugins
```

CLI 这步做的事：把 `github:anthropics/claude-plugins-official` 解析成实际的 git URL，执行 `git clone --depth 1` 拉到一个临时目录，从 `.claude-plugin/marketplace.json` 读取内容，解析校验后缓存到 `~/.x-code/plugins/marketplaces/anthropic-marketplace/marketplace.json`（`~` 在 Windows 上对应 `C:\Users\<用户名>\`），最后清理临时目录。

索引拉取完成后可以查看目录内容：

```text
> /plugin marketplace info anthropic-marketplace
⎿ anthropic-marketplace (anthropic-marketplace)
  Upstream name: claude-plugins-official
  Directory of popular Claude Code extensions ...
  Owner: Anthropic

  203 plugins:
    42crunch-api-security-testing (security)
      API security testing for OpenAPI specifications
    amplitude (productivity)
      Amplitude product analytics integration
    asana (productivity)
      Asana task management
    ...
    linear (productivity)
      Linear issue tracking integration. Create issues, manage projects, …
    ...
```

第一行是 marketplace 的显示名，优先用 `marketplace.json` 顶层声明的 `displayName`，没声明就回退到用户的本地别名。`anthropic-marketplace` 这份 marketplace 没写 `displayName`，所以显示名跟括号里都是同一个本地别名。第二行 `Upstream name: claude-plugins-official` 才是 `marketplace.json` 顶层 `name` 字段值，也就是 marketplace 自己上报的 name。本地订阅别名跟上游 `marketplace.json` 自己声明的 name 是两个东西，可以不同。本地别名是插件 ID 的一部分（`linear@anthropic-marketplace`），上游 name 只在 info 命令里展示。这个解耦设计解决了一个具体问题（详见 40.2 节）。

假设用户在列表里看到了 `linear`，想用它来管理 Linear 项目事务，然后执行安装命令：

```text
> xc plugin install linear@anthropic-marketplace
Installing from git: https://github.com/anthropics/claude-plugins-official.git ...

About to install: linear@anthropic-marketplace v0.0.0
  Linear issue tracking integration. Create issues, manage projects, ...
  Source:      git: https://github.com/anthropics/claude-plugins-official.git
  Marketplace: anthropic-marketplace [reserved/official]
  Author:      Linear

Proceed with install? [y/N] y
Installed linear@anthropic-marketplace v0.0.0
Cache: ~/.x-code/plugins/cache/anthropic-marketplace/linear/0.0.0
```

安装器收到 `linear@anthropic-marketplace` 这个 ID 后，先按 `@` 拆成两部分：前半段 `linear` 是插件名，后半段 `anthropic-marketplace` 是本地 marketplace 别名。接下来，安装器用别名找到本地缓存的索引文件 `~/.x-code/plugins/marketplaces/anthropic-marketplace/marketplace.json`，在 `plugins[]` 数组里搜索 `name: "linear"` 的条目。找到条目后，安装器从它的 `source` 字段读取插件代码的来源——Linear 这个条目用的是相对路径字符串 `"./external_plugins/linear"`，意思是 marketplace 仓库自身的 `external_plugins/linear/` 子目录。安装器把这个相对路径拼上 marketplace 仓库的克隆地址，转成 `git clone` 能用的 URL，最终把插件代码克隆到 `~/.x-code/plugins/cache/anthropic-marketplace/linear/0.0.0/`。克隆完成后的 consent 确认、userConfig 读取和 `/plugin refresh` 重建注册表等后续流程，Ch.39 已经讲过，这里不再重复。

以上流程涉及几个 marketplace 层面的设计决定：marketplace 如何标识自己？条目的 source 字段为什么有多种格式？sha 字段起什么作用？索引从哪里拉取？默认订阅如何预置？让我们在下面的内容里逐节展开。

## 40.2 决定一：marketplace 身份与冒充防御

订阅一份 marketplace 的命令是这样的：

```text
/plugin marketplace add anthropic-marketplace github:anthropics/claude-plugins-official
```

注意命令里出现了两个名字。一个是用户自己起的本地名 `anthropic-marketplace`；另一个是 marketplace 在自己 `marketplace.json` 里声明的 `name`，值是 `claude-plugins-official`（跟上游 GitHub 仓库同名）。两个名字可以完全不同。A 用户可以把这份 marketplace 订阅成 `anthropic-marketplace`，B 用户可以叫它 `claude-official`，两人的插件 ID 分别是 `linear@anthropic-marketplace` 和 `linear@claude-official`。允许用户自定义别名带来了灵活性，但也引出两个需要解决的问题。

（1）CLI 内部用哪个名字作为 marketplace 的唯一标识？X-Code CLI 选的是用户给的本地名，而非 `marketplace.json` 里上游自己声明的 `name`。缓存路径、插件 ID（如 `linear@anthropic-marketplace`）、命令输出都跟着本地名走。这样做的好处是，即使上游 marketplace 作者将来修改了 `marketplace.json` 里的 `name` 字段，用户本地的缓存路径和已安装插件的 ID 也不会受到影响。上游 `name` 只在 `/plugin marketplace info` 的输出里作为参考展示一行 `Upstream name: claude-plugins-official`。

（2）本地名由用户自己起，这就带来一个安全隐患。假设某篇技术博客或论坛帖子里贴了一条命令：`/plugin marketplace add anthropic-marketplace github:evil-user/fake-plugins`。用户一看名字是 `anthropic-marketplace`，以为是官方源，直接复制执行。但这条命令实际指向的是攻击者的仓库 `evil-user/fake-plugins`，里面的 `linear` 插件可能被注入了恶意代码。此后用户执行 `xc plugin install linear@anthropic-marketplace`，拿到的就是攻击者的版本，而非 Anthropic 官方收录的插件。这是典型的身份冒充攻击（Impersonation）——名字看着像官方，实际指向完全不同的来源。

X-Code CLI 的防御方式是把特定名字钉死到对应的 GitHub 组织：`anthropic-marketplace` 和 `claude-plugins` 必须指向 `anthropics` 组织。如果 source 与保留名不匹配，`xc plugin marketplace add` 在写入订阅之前就会拒绝，错误信息告诉用户「这个名字保留给 Anthropic 官方」。通过校验的保留名在 `/plugin marketplace list` 输出里带 `[official]` 标签，让用户一眼区分官方与第三方。

这套机制只针对几个用户可能默认信任的保留名。非保留名字不受限制——用户订阅任意第三方 marketplace 是正当操作，CLI 不做拦截。

## 40.3 决定二：marketplace.json 里的 4 种来源形态

上一节解决了「marketplace 怎么标识自己」。下一个问题是 marketplace.json 里每个插件条目怎么标识「插件代码从哪来」，也就是条目的 `source` 字段。`source` 字段的职责是告诉安装器去哪里拿插件代码，一个 git URL 或一个相对路径似乎就够了。但 `anthropics/claude-plugins-official` 里有四种不同的写法：

```jsonc
// 形态 1：相对路径字符串（marketplace 自己 repo 的 subdir，最常见）
{ "name": "agent-sdk-dev", "source": "./plugins/agent-sdk-dev" }

// 形态 2：{source:'url', url, sha} — 外部仓库整库 clone
{ "name": "amplitude", "source": { "source": "url", "url": "https://github.com/amplitude/mcp-marketplace.git", "sha": "5ddccc36..." } }

// 形态 3：{source:'git-subdir', url, path, ref?, sha?} — 外部仓库 + subdir
{ "name": "42crunch-api-security-testing", "source": {
    "source": "git-subdir",
    "url":    "https://github.com/42Crunch-AI/claude-plugins.git",
    "path":   "plugins/api-security-testing",
    "ref":    "v1.5.5",
    "sha":    "a175b24f..."
} }

// 形态 4：{source:'github', repo:"owner/repo", commit, sha} — GitHub 专用形态
{ "name": "fullstory", "source": { "source": "github", "repo": "fullstorydev/fullstory-skills", "commit": "1ec5865...", "sha": "384555c3..." } }
```

四种形态对应三类来源：marketplace 自己仓库的子目录（形态 1）、外部 git 仓库（形态 2 / 3）、外部 GitHub 仓库的简写（形态 4）。字段命名也不一致：形态 2 的辨别字段叫 `source: 'url'`，形态 4 叫 `source: 'github'`；同样表示 commit 锁定的字段，形态 3 用 `ref`，形态 4 又改成了 `commit`。

这些格式不是 X-Code CLI 自己设计的，而是从 Claude Code 的历史规范继承过来的。除了上面 4 种主流形态，生态中还流通着几种少见的写法：直接给 git URL 字符串（不带 `github:` 前缀）、`{ source: 'git' }` 形态（与形态 3 几乎相同，仅辨别字段不同）、以及 `{ source: 'local', path }` 指向本机绝对路径。加起来共 8 种线上格式（Wire Format，指数据在文件里实际存储的物理形态）。`anthropic-marketplace` 实际只用前 4 种，其余是 Claude Code 生态的历史写法和本地开发路径。

X-Code CLI 要接收对方生态的存量 marketplace，就必须兼容所有已在生产环境流通的写法。但让安装器直接分辨 8 种格式并不合理——安装器本身已经承担了克隆、同意确认、用户配置、sha 校验等职责。因此，marketplace 子系统在安装器之前加了一层归一化处理（Normalization）：把 8 种线上格式统一转成三种内部表示——git 来源（URL + 可选子目录 / ref / sha）、GitHub 来源（owner + repo）、本地路径。安装器只需要处理归一化后的三种类型，不需要知道作者用了哪种原始格式。

归一化过程中有一个需要注意的边界。形态 1 的相对路径（如 `./plugins/foo`）是相对于 marketplace 仓库自身的，归一化时调用方会把 marketplace 的克隆地址传进来，用于拼接完整路径。但如果 marketplace.json 是通过 HTTPS 直接拉取的（详见 40.5 节），就没有克隆地址可用，此时相对路径类型的插件条目无法安装。归一化层检测到克隆地址缺失时直接抛错，错误信息告诉用户「相对路径只对 git 克隆下来的 marketplace 有效」。

## 40.4 决定三：sha 锁定与完整性校验

`anthropics/claude-plugins-official` 里的 153 个远程 source（git-subdir、url、github 三种形态）全部都声明了 `sha` 字段，主要是因为安全考虑。

考虑一个攻击场景：marketplace 作者宣布插件 X 的 source 是 `https://github.com/x-org/plugin-x.git`、`ref: "v1.0"`。今天用户安装时 git clone 拉到的是 v1.0 标签对应的提交。明天攻击者拿到了 x-org 的推送权限，把 v1.0 标签强制推送（force-push）到一个含恶意代码的提交。后天另一个用户安装这个插件，git clone 拉到的就是恶意提交：同样的 `ref: "v1.0"`、同样的指令、不同的内容。

防御办法是把信任锚点从「ref 名字」推到「提交哈希（commit hash）」上。marketplace 作者在 marketplace.json 里多写一个 `sha` 字段：

```jsonc
{
  "source": "git-subdir",
  "url":    "https://github.com/42Crunch-AI/claude-plugins.git",
  "path":   "plugins/api-security-testing",
  "ref":    "v1.5.5",
  "sha":    "a175b24f7b34852b70c78c21545cce8037eb3112"
}
```

安装器克隆完之后执行 `git rev-parse HEAD` 拿到实际的提交哈希，跟 marketplace.json 声明的 `sha` 对比，不匹配则失败：

> 源码：`packages/core/src/plugins/installer.ts`（`fetchToTemp` sha check 段）

```ts
if (source.expectedSha) {
  try {
    const result = await execa('git', ['rev-parse', 'HEAD'], { cwd: tempDir, stdio: 'pipe', signal })
    const actualSha = result.stdout.trim().toLowerCase()
    const expected = source.expectedSha.toLowerCase()
    if (!actualSha.startsWith(expected)) {
      await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {})
      throw new InstallError(
        `sha integrity check failed for ${cloneUrl}${source.ref ? `@${source.ref}` : ''}: ` +
          `marketplace.json declared sha=${expected}, actual HEAD=${actualSha}. ` +
          `The upstream ref may have been force-pushed or the repo compromised. ` +
          `Contact the marketplace author or pin to a different version.`,
      )
    }
  } catch (err) {
    if (err instanceof InstallError) throw err
    await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {})
    throw new InstallError(
      `failed to verify sha for ${cloneUrl}: ${err instanceof Error ? err.message : String(err)}`,
    )
  }
}
```

上面代码中用 `startsWith` 而非全等比较是有意的。marketplace.json 里 `sha` 字段允许是短 sha（最短 7 字符，匹配 `git checkout <short>` 的语义）也允许是完整 40 字符。归一化层在解析 source 时已经做过一道格式验证：

```ts
const rawSha = typeof o.sha === 'string' ? o.sha.trim().toLowerCase() : undefined
const expectedSha = rawSha && /^[0-9a-f]{7,40}$/.test(rawSha) ? rawSha : undefined
```

只接受 7-40 字符的纯 hex，其他形态（包含非 hex、长度不对）一律静默丢弃。如果 marketplace 作者把 sha 字段写错（误输入了空格或漏了几位），不会因为「sha 不匹配」拒绝安装，而是当作 sha 没声明、跳过校验。因此，sha 校验的行为分两种情况：字段存在且格式合法时严格校验、不匹配直接拒绝安装；字段缺失或格式不合法时跳过校验、向后兼容。

校验失败时安装器直接拒绝安装，而不是给一个警告后继续。sha 不匹配按定义只能是两种情况：marketplace.json 写错了（作者的 bug），或者上游 ref 被强制推送、仓库被攻陷。两种都不应该让用户把未经审查的代码留在磁盘上。与其安装之后让用户去排查「这个插件最近行为不太对」，不如直接拒绝，把错误信息指向 marketplace 作者。

## 40.5 决定四：拉取索引的两条路径

40.1 节的例子里，`/plugin marketplace add` 命令的第二个参数是 marketplace 订阅的 source——它决定了 CLI 从哪里拉取 `marketplace.json` 索引文件（注意区分 40.3 节讨论的插件条目 source，两者是不同的字段）。X-Code CLI 根据这个订阅 source 的格式自动选择两条拉取路径中的一条。

第一条是 HTTPS 直链：如果 source 是一个以 `.json` 结尾的 HTTPS URL（如 `https://example.com/marketplace.json`），CLI 直接发起 HTTP GET 请求下载这份文件。这条路径不需要 git 命令、网络成本低，但有一个限制——marketplace.json 里使用相对路径的插件条目（如 `source: "./plugins/foo"`）在没有仓库上下文的情况下无法解析。

第二条是 git 浅克隆：其他所有格式（`github:owner/repo`、`git@` SSH 地址、不以 `.json` 结尾的 HTTPS URL）都走这条路径。CLI 执行 `git clone --depth 1` 把 marketplace 仓库拉到临时目录，从约定路径读取索引文件。这条路径支持相对路径条目，代价是要求本地安装了 git，速度也比单文件下载慢一些。

> 源码：`packages/core/src/plugins/marketplace.ts`（`fetchMarketplace`）

```ts
const isHttp = /^https?:\/\//i.test(entry.source) && /\.json($|\?)/i.test(entry.source)
const rawJson = isHttp
  ? await fetchHttpJson(entry.source, opts.signal)
  : await fetchViaShallowClone(entry.source, opts.signal)
```

代码里的 `isHttp` 判断对应上面描述的第一条路径：source 同时满足以 `http://` / `https://` 开头且以 `.json` 结尾两个条件时走 HTTPS 直链，否则走 git 浅克隆。

浅克隆路径有两个值得注意的细节。第一个是索引文件的探测位置，先试 `.claude-plugin/marketplace.json`（Claude Code 约定路径，也是 `anthropics/claude-plugins-official` 实际放的位置），找不到再退一步试根目录 `marketplace.json`：

```ts
const candidates = [
  path.join(tempDir, '.claude-plugin', 'marketplace.json'),
  path.join(tempDir, 'marketplace.json'),
]
for (const candidate of candidates) {
  try {
    return await fs.readFile(candidate, 'utf-8')
  } catch {
    // try the next candidate
  }
}
```

`.claude-plugin/marketplace.json` 排前面是因为这是 Claude Code 官方约定；根目录那条是给非标准目录布局的 marketplace 作为备选。两个都找不到才抛错。

第二个是克隆之后总是删掉临时目录（不管成功失败）：

```ts
try {
  await execa('git', ['clone', '--depth', '1', cloneUrl, tempDir], ...)
  // ... 读 marketplace.json
} finally {
  await fs.rm(tempDir, { recursive: true, force: true }).catch(() => {})
}
```

这样做的原因是：CLI 只需要 marketplace.json 的文本内容，marketplace 仓库本身的代码不需要保留在磁盘上。每次刷新都会重新克隆，如果不清理临时目录，多次刷新后磁盘上会积累大量无用目录。

此外，`fetchMarketplace` 还接受一个 `maxAgeMs` 参数，用于控制本地缓存的有效期——如果缓存尚未过期，就跳过网络请求直接读取本地文件。CLI 启动时会自动调用一次 `fetchMarketplace` 并传入 `maxAgeMs`，避免每次启动都触发网络请求；而用户显式执行 `/plugin marketplace refresh` 时不传这个参数，强制走网络拉取最新索引。

## 40.6 小结

Marketplace 是分布式插件目录：一份 git 仓库里的 `marketplace.json` 列出可安装的插件清单，用户订阅一个或多个 marketplace 仓库，CLI 在本地缓存索引并组合查询。本地订阅别名与上游 name 是两个独立标识，几个保留名绑定到对应的 GitHub 组织以防止冒充。插件条目的 source 字段在真实生态中有 8 种线上格式，归一化层把它们统一成三种内部表示，安装器只看归一后的类型。

远程 source 可声明 sha 字段，安装器克隆完拿实际提交哈希做前缀匹配，不匹配则拒绝安装；sha 缺失时跳过校验，向后兼容存量。拉取索引根据 source 格式自动选择 HTTPS 直链或 git 浅克隆两条路径，读完即删临时目录。
