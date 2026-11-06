# 第 15 章 代码库探索：glob、grep 与 listDir

模型在动手修改代码之前，需要先定位目标：哪个文件定义了目标函数？哪些文件调用了它？项目目录的结构是什么样的？X-Code CLI 为这三类需求各提供了一个专用工具：

（1）`glob`——按文件名模式匹配文件路径

（2）`grep`——按正则表达式搜索文件内容中的匹配行

（3）`listDir`——列出指定目录的直接子项

三个工具的实现代码都不算长，`glob` 和 `grep` 各不到 100 行，共用 `@vscode/ripgrep` 提供的 [ripgrep](https://github.com/BurntSushi/ripgrep) 二进制执行底层搜索；`listDir` 只有 30 行，直接调用 Node.js 内置的 `fs.readdir`。本章的重点不在实现代码本身，而在于这些「看似现成的 Unix 工具」被封装为面向大语言模型的工具接口时做出的一系列设计选择：默认参数如何选定、输出上限设为多少、错误信号如何传递、description 里需要写哪些禁令。这些选择直接影响模型在「搜索 → 决策 → 修改」链路上的准确性和效率。本章分为如下几个部分进行介绍。

- 15.1 节用一个贯穿示例展示三件工具在一次搜索中如何协作。
- 15.2 节讨论工具粒度的设计选择：三种意图为什么对应三个独立工具。
- 15.3 节介绍 ripgrep 共用与路径解析缓存。
- 15.4 节拆解 `glob` 的修改时间倒序排列与 catch-all 特殊处理。
- 15.5 节讲解 `grep` 的三层截断与退出码语义翻译。
- 15.6 节解释 `grep` description 中的禁用指令及其背后的原因。
- 15.7 节介绍 `listDir` 的绝对路径策略与极简输出格式。
- 15.8 节讨论工具执行过程中的进度上报机制。
- 15.9 节给出本章小结。

## 15.1 贯穿示例：从一次搜索看三件工具如何协作

假设用户在终端输入：

> 帮我看看项目里所有用到 `formatDate` 的地方，看看能不能在不破坏旧调用的前提下加一个时区参数

模型读完请求后，第一步不是动手改代码，而是先确认「哪些地方用到了 `formatDate`」。它发出一个 `grep` 工具调用：

```jsonc
{
  "name": "grep",
  "args": { "pattern": "formatDate", "glob": "*.ts" }
}
```

X-Code CLI 收到调用后启动 ripgrep 子进程执行搜索，将匹配结果截断到 250 行上限后返回模型：

```text
src/utils.ts:42:export function formatDate(d: Date): string {
src/utils.ts:43:  return d.toISOString().slice(0, 10)
src/components/Header.tsx:8:import { formatDate } from '../utils'
src/components/Header.tsx:31:  <span>{formatDate(post.createdAt)}</span>
src/api/posts.ts:15:import { formatDate } from '../utils'
src/api/posts.ts:42:    publishedAt: formatDate(p.publishedAt),
src/api/posts.ts:78:  date: formatDate(comment.date),
```

如果匹配总数超出上限，末尾还会追加一行提示：

```text
... [42 more lines not shown — at least 49 total matches, capped at 250.
     Narrow your pattern or use glob to reduce results.]
```

模型读完这段结果就掌握了三个关键信息：`formatDate` 定义在 `utils.ts:42`，调用方分布在 `Header.tsx` 和 `posts.ts`，目前没有任何调用方传入时区参数。下一步它会调用 `readFile` 读取 `src/utils.ts` 的完整定义，再规划如何添加新参数以及是否需要修改现有调用方。如果模型还想了解项目的整体结构，可能会在 `grep` 之前先调用 `listDir` 查看 `src/` 目录下有哪些子目录，或者先用 `glob "src/**/*.ts"` 估算 TypeScript 文件的总数再决定是否需要按目录拆分搜索。三个工具往往组合使用，下面逐节展开它们各自的关键设计选择。

## 15.2 工具粒度：三种意图对应三个独立工具

三种搜索能力也可以合成一个「全能搜索工具」，例如 `search({ files?, content?, dir? })`，通过参数组合覆盖所有场景。X-Code CLI 没有选择这条路，而是有意将其拆成三个独立工具。原因在于模型的决策路径在这三种场景下完全不同：使用 `glob` 时，模型已经知道想找什么类型的文件（「所有 `.ts` 文件」或「所有测试文件」），不关心文件内容；使用 `grep` 时，模型已经知道想找什么内容（「所有用到 `formatDate` 的地方」），不关心文件名模式；而 `listDir` 的典型使用场景是模型对项目结构一无所知，想先看一眼目录再决定下一步行动。

如果将三者合成一个工具，模型在每次调用前都必须先在心智中完成一次场景分类——「我现在到底想做哪件事」，然后再去填写对应的参数组合。这种额外的认知负担会导致模型在简单查询中选错参数组合。三个工具分开之后，模型选择哪个工具本身就已经表达了它的意图，参数填写出错的概率大幅下降。这项设计原则和 Ch.20 todoWrite 中「给大语言模型设计工具应当合粗」的判断看似矛盾，实际上遵循同一项准则：todoWrite 之所以合粗，是因为「维护一份清单」属于单一意图，拆细只会让模型在多次调用之间维持状态时出错；而搜索工具之所以拆细，是因为「找文件」「搜内容」「看目录」是三种独立意图，合并反而强迫模型做无意义的分类。简言之，单一意图合粗，多种意图拆细。

## 15.3 ripgrep 共用与路径解析缓存

三个工具的底层实现各自依赖一个成熟方案：

| 工具 | 底层实现 | 选择原因 |
|---|---|---|
| `glob` | ripgrep 的 `rg --files --glob <pattern> --sortr=modified` | ripgrep 已是 `grep` 工具的依赖，复用不增加包体积；Rust 实现的目录遍历配合内置 `.gitignore` 过滤，远快于 Node.js 的 glob 库 |
| `grep` | ripgrep 的 `rg <pattern> <path>` | Rust 多线程实现，比 Node.js 单线程 `fs` + `RegExp` 方案快几个数量级；内置 `.gitignore` 解析、Unicode 正则支持和并行目录遍历 |
| `listDir` | Node.js 内置 `fs.readdir` | 功能简单，无需引入外部依赖 |

`@vscode/ripgrep` 是 Microsoft 维护的 npm 包，它将 ripgrep 二进制按平台预编译好（macOS、Linux、Windows 各有 x64 和 arm64 版本），通过 `require('@vscode/ripgrep').rgPath` 即可拿到当前平台的可执行文件路径，不需要用户预先通过系统包管理器安装 ripgrep。图 15-1 展示了三个工具的调用路径：`glob` 和 `grep` 在 `getRipgrepPath()` 处汇合，共用同一个 ripgrep 子进程；`listDir` 走独立的 `fs.readdir` 路径，不依赖 ripgrep。


![image.png](https://p9-juejin.byteimg.com/tos-cn-i-k3u1fbpfcp/4facc1d44bb34c4081416905ffbc1c9a~tplv-k3u1fbpfcp-jj-mark:0:0:0:0:q75.image#?w=1134&h=267&s=25366&e=png&b=ffffff)

> 图 15-1 三个搜索工具的调用路径——glob 和 grep 共用 ripgrep，listDir 走 fs.readdir

`glob` 和 `grep` 共用一个路径解析辅助函数 `getRipgrepPath`，定义在 `tools/utils.ts` 中。`tools/` 目录下文件名与工具名一一对应（`glob.ts`、`grep.ts`、`shell.ts` 各定义一个工具），单函数级别的公共辅助则集中放入 `utils.ts`，避免与工具文件并列时被读者误认为也是一个工具定义：

> 源码：`packages/core/src/tools/utils.ts`

<!-- check:ignore=punctuation -->
```ts
// X-Code CLI 是 ESM 包（"type": "module"），
// 全局 require 不存在，需要 createRequire 加载 CJS 模块
const _require = createRequire(import.meta.url)

let _rgPath: string | null = null

export function getRipgrepPath(): string {
  if (_rgPath) return _rgPath
  try {
    const rg = _require('@vscode/ripgrep') as { rgPath: string }
    _rgPath = rg.rgPath
  } catch {
    // 包未安装时降级为系统 PATH 中的 rg
    _rgPath = 'rg'
  }
  return _rgPath
}
```

这段代码有两个细节值得说明。第一，项目采用 ESM 模块规范（`package.json` 中 `"type": "module"`），运行时不存在全局 `require` 函数，因此需要通过 `createRequire(import.meta.url)` 手动构造一个 `require`，才能加载 `@vscode/ripgrep` 这类只提供 CommonJS 导出的包。第二，`_rgPath` 变量实现了单进程级别的缓存：首次调用时解析路径并缓存结果，后续调用直接返回，避免重复执行 `require` 解析。`catch` 分支是一道降级措施——如果 `@vscode/ripgrep` 包因安装失败等原因不可用，回退到系统 PATH 中的 `rg` 命令，让开发者在本地开发和测试时仍能正常使用搜索工具（前提是系统中已安装 ripgrep）。

## 15.4 glob：修改时间倒序与 catch-all 特殊处理

回到 15.1 的示例。如果模型在调用 `grep` 之前想先了解项目里有多少 TypeScript 文件，它会发出一个 `glob` 调用：

```jsonc
{
  "name": "glob",
  "args": { "pattern": "src/**/*.ts" }
}
```

X-Code CLI 返回按修改时间倒序排列的绝对路径列表：

```text
/Users/dev/proj/src/utils.ts
/Users/dev/proj/src/api/posts.ts
/Users/dev/proj/src/components/Header.tsx
... (共 47 个文件)
```

模型一眼就能看到最近改过的文件排在最前面，决定下一步对哪些文件调用 `grep` 做内容搜索。X-Code CLI 内部组装 ripgrep 参数的逻辑如下：

> 源码：`packages/core/src/tools/glob.ts`

```ts
const isCatchAll = /^(\*\*\/?\*?|\*)$/.test(pattern.trim())
const args = ['--files', '--sortr=modified', '--hidden', '--glob', '!.git']
if (!isCatchAll) {
  args.push('--glob', pattern)
}
const { stdout } = await execFileAsync(getRipgrepPath(), args, {
  cwd: searchDir,
  maxBuffer: RG_MAX_BUFFER,
  timeout: 30000,
})
```

每个参数各管一件事。`--files` 告诉 ripgrep 只列出文件路径而不搜索内容，关闭了默认的内容搜索行为。`--sortr=modified` 按修改时间（mtime）倒序输出，最近改过的文件排在最前面（注意是 `sortr` 带 `r` 表示 reverse，不带 `r` 的 `--sort=modified` 是正序）。`--hidden` 将以 `.` 开头的文件也纳入搜索范围，让模型能找到 `.eslintrc`、`.prettierrc`、`.env.example` 等配置文件，因为 ripgrep 默认会跳过 dotfiles。`--glob '!.git'` 显式排除 `.git/` 元数据目录，这条排除规则不是多余的安全措施，而是必要的：`.gitignore` 中通常不会列出 `.git/`（git 自身管理该目录），而 `--hidden` 一旦开启就会让 ripgrep 进入 `.git/objects/` 遍历内部文件，不过滤的话结果中会出现成千上万条无意义的 hash 路径。最后一个 `--glob <pattern>` 是用户传入的文件名模式，仅在非 catch-all 模式时追加。

### 为什么 catch-all 模式需要特殊处理

当用户传入的 pattern 是 `**/*`、`**` 或 `*` 这类通配全量模式（Catch-all Pattern）时，代码不会把它传给 ripgrep 的 `--glob` 参数。这是因为 ripgrep 的 `--glob` 采用白名单语义：一旦显式指定了 `--glob` 参数，ripgrep 就将其理解为「只要这些文件」，并绕过 `.gitignore` 的过滤规则。对于 `**/*.ts` 这样的限定模式，白名单行为符合预期；但当 pattern 是 `**/*` 时，等于告诉 ripgrep「所有文件都要」，`.gitignore` 的过滤被完全绕过，结果中会涌入 `node_modules/` 下的数万条文件路径、`dist/` 中的各种构建产物以及 `*.tsbuildinfo` 等缓存文件。这种行为不会触发任何错误提示，只是悄悄返回大量无关路径，将模型的上下文窗口塞满噪声。因此代码在检测到 catch-all 模式时选择不传 `--glob` 参数，让 ripgrep 的默认文件遍历行为接管——默认行为会严格遵守 `.gitignore` 规则。

### 为什么修改时间倒序对模型更友好

假设模型搜索 `**/*.ts`，匹配到 500 个文件，但工具的输出上限是 200 条。此时排序方式决定了哪些文件被保留、哪些被截断。如果按字典序排列，前 200 条可能全是 `src/aaa.ts`、`src/abc.ts` 等字母靠前的文件名，用户最近修改过的、与当前任务最相关的文件反而排在 300 位之后被截掉。如果按修改时间倒序排列，前 200 条是用户最近改动过的文件，与当前对话上下文最相关的概率最高；被截掉的则是放置数月未动的稳定模块。修改时间倒序让模型在截断场景下优先看到最有可能需要关注的文件，而不是碰巧名字靠前的无关文件。

工具的 description 字符串中也明确写道 `Returns absolute file paths sorted by modification time, most recent first`，模型会按字面意思推理——「前几条是最近修改的文件」。如果代码实际给出字典序，模型在截断场景下就会做出错误判断。工具描述对模型做出的承诺必须与代码的实际行为严格一致。此外需要说明的是，`--sortr` 会强制 ripgrep 以单线程模式运行以保证排序的确定性，多线程加速主要体现在不需要排序的 `grep` 路径上。

### `.gitignore` 由 ripgrep 默认处理

在非 catch-all 路径上，ripgrep 自动尊重项目的 `.gitignore` 文件，不需要传递额外参数。X-Code CLI 没有在代码中内置一份硬编码的忽略列表（如 `node_modules`、`dist` 等），原因在于用户的 `.gitignore` 通常已经覆盖了这些路径，由 ripgrep 统一处理即可。如果工具层再内置一份固定列表，就会出现两套忽略规则并存的问题：monorepo 子包使用 `build/` 而非 `dist/` 时，硬编码的 `dist` 规则无法命中；某些项目将 `dist` 标记为需要追踪的产物时，内置规则又会错误地将其忽略。唯一的硬编码排除是前面提到的 `--glob '!.git'`，这条规则无法依赖用户配置来覆盖，因为 git 不会把 `.git/` 写进 `.gitignore`。

让 `.gitignore` 加上唯一一条硬编码的 `.git` 排除共同构成忽略规则的来源，行为便是可预测的：用户在终端中通过 `git status` 看到的忽略规则，与 `glob` 工具所遵守的忽略规则基本一致（只多出一条 `.git/` 排除）。

### 路径转绝对值与截断提示

ripgrep 返回的是相对路径（相对于 `cwd` 参数指定的搜索目录），`glob` 工具在拿到结果后逐条检查：已经是绝对路径的原样保留，相对路径则通过 `path.join(searchDir, p)` 拼接为绝对路径再返回。绝对路径让模型在跨目录任务中引用文件时不会因为工作目录切换而导致路径失效。文件总数超过上限时，工具截断结果并附带引导提示：

```ts
const MAX_GLOB_RESULTS = 200
// ...
if (truncated) {
  return `${result}\n\n... [${absolutes.length - MAX_GLOB_RESULTS} more files not shown — ${absolutes.length} total matches, capped at ${MAX_GLOB_RESULTS}. Use a more specific pattern to narrow results.]`
}
```

`Use a more specific pattern to narrow results` 是写给模型读的引导语。模型看到这句话就知道下一步应当把 pattern 写得更精确（例如从 `**/*.ts` 改为 `src/auth/**/*.ts`），而不是重试同样的调用或放弃搜索。截断不是终点，而是一个「换条路再来」的信号。

## 15.5 grep：三层截断与退出码语义翻译

回到 15.1 的示例。模型调用 `grep` 搜索 `formatDate` 时，`grep` 工具内部设置了三道独立的限制，各自防范一种失控场景：

> 源码：`packages/core/src/tools/grep.ts`

```ts
const DEFAULT_HEAD_LIMIT = 250
const MAX_COLUMNS = 500
const RG_MAX_BUFFER = 20 * 1024 * 1024
```

（1）行数上限（`DEFAULT_HEAD_LIMIT = 250`）。常见的 `grep` 失控场景是 pattern 写得过于宽泛，例如搜索 `import` 字样可能命中数千行。250 行的上限让模型既能看清结果的分布规律，又不至于一次性接收几十 KB 的 token。截断时拼接的提示语 `Narrow your pattern or use glob to reduce results` 直接告诉模型下一步该如何调整：要么把 pattern 写得更精确，要么通过 `glob` 参数限制搜索的文件类型。

（2）单行列数上限（`MAX_COLUMNS = 500`）。传给 ripgrep 的 `--max-columns 500` 配合 `--max-columns-preview` 会把长行（如压缩后的 JS bundle 或单行嵌套的 JSON）截断到 500 字符，并在截断处标注省略信息。这样模型读到的每一行都保持结构化的 `file:line:content` 格式，不会被一行数十 KB 的 minified 代码淹没整个工具结果。

（3）ripgrep stdout 缓冲上限（`RG_MAX_BUFFER = 20 * 1024 * 1024`）。这是传给 `execFile` 的 `maxBuffer` 参数，与 Ch.14 shell 工具的 `MAX_SHELL_BUFFER` 属于同一数量级。超出此限制时 ripgrep 子进程会被终止并抛出异常，而不是无限吞噬内存。

除了三层截断之外，还有一个容易被忽略的语义翻译：

```ts
catch (err) {
  if (err && typeof err === 'object' && 'code' in err && err.code === 1) {
    return 'No matches found.'
  }
  return formatToolError('searching', err)
}
```

ripgrep 在没有找到任何匹配时返回退出码 1，而 `execFile` 看到非零退出码会抛出 `Error`。如果直接把这个异常透传给模型，模型会将「没找到匹配」解读为工具执行出错，可能开始无意义的重试或切换到其他工具。这里专门拦截 `code === 1` 的情况，将其转换为 `No matches found.` 这一正常结果返回，在语义上将「无匹配」和「执行出错」区分开来。模型读到 `No matches found.` 就能理解这是空结果而非故障，不会触发重试逻辑。`glob` 工具也采用了完全相同的处理方式，返回 `No files found matching the pattern.`。

## 15.6 grep description 的禁用指令

`grep` 工具的 description 开头就写了一条明确的禁用指令：

> 源码：`packages/core/src/tools/grep.ts`

<!-- check:ignore=punctuation -->
```
A powerful search tool built on ripgrep.

Usage:
- ALWAYS use this grep tool for content search tasks. NEVER invoke grep or
  rg as a shell command — this tool has been optimized for correct
  permissions and access.
```

中文译义：基于 ripgrep 的搜索工具。用法——搜索内容时永远使用 `grep` 工具，永远不要通过 shell 执行 `grep` 或 `rg` 命令，本工具已针对权限和访问路径做了优化。

这条指令和 Ch.14 shell 工具 description 中的禁令互为对照：shell 工具那边说「不要用 shell 执行 grep」，`grep` 工具这边说「不要走 shell 执行 rg」。两端都做引导，模型在选择工具时无论从哪一端出发都能被引向正确路径。之所以需要在 `grep` 工具自身的 description 中再写一遍，是因为模型的训练数据中包含大量 `rg pattern .` 和 `grep -r pattern dir` 的用法，默认倾向是通过 shell 工具执行命令行调用。如果禁令只写在 shell 工具的 description 中，模型可能根本不会进入选择 shell 工具的决策路径，它直接就生成了 grep 命令交给 shell 执行。将禁令同时写在 `grep` 工具的 description 中，相当于在搜索入口处建立了第二道引导：模型一旦考虑到 `grep` 工具，看到这段说明就知道这是内容搜索的正式入口。

description 中还指出了 ripgrep 正则语法的一个常见陷阱：

<!-- check:ignore=punctuation -->
```
- Pattern syntax: Uses ripgrep — literal braces need escaping
  (use interface\{\} to find interface{} in Go code).
```

ripgrep 使用 Rust 的 regex crate 实现正则表达式，其中 `{` 和 `}` 是量词元字符（`{n,m}` 语法）。如果要在 Go 代码中搜索 `interface{}` 字面量，必须转义为 `interface\{\}`。这种边角细节不写在 description 中，模型很容易在搜索 Go 代码时全部返回空结果却不知道原因。

## 15.7 listDir：绝对路径与极简输出

回到 15.1 的示例。如果模型对项目结构一无所知，想先看看 `src/` 下有哪些子目录再决定搜索范围，它会发出一个 `listDir` 调用：

```jsonc
{
  "name": "listDir",
  "args": { "dirPath": "/Users/dev/proj/src" }
}
```

返回结果极简——只有名称和类型后缀：

```text
api/
components/
utils.ts
index.ts
```

模型看到 `api/` 和 `components/` 两个子目录后，就知道下一步可以用 `grep` 分别搜索这两个目录下的 `formatDate` 调用。`listDir` 是三个工具中最简单的，schema 只有一个字段：

> 源码：`packages/core/src/tools/list-dir.ts`

```ts
inputSchema: z.object({
  dirPath: z.string().describe('Absolute path to the directory'),
})
```

execute 函数直接调用 `fs.readdir(dirPath, { withFileTypes: true })`，每个条目输出文件名加类型后缀（目录加 `/`），空目录返回 `(empty directory)`：

```text
src/
README.md
package.json
tsconfig.json
.gitignore
```

这个工具有两个看似不起眼但有意为之的设计选择。

（1）只接受绝对路径。schema 描述中写明 `Absolute path to the directory`。`glob` 工具支持可选的 `cwd` 参数加相对路径，`listDir` 为什么不这样设计？原因在于相对路径的语义在大语言模型的工具调用场景中不够稳定。模型在多轮对话中可能记错「当前目录」是什么（尤其在执行过 `cd` 类 shell 命令之后），相对路径的解析基准也随之变得模糊。绝对路径让每次调用的语义独立且可重现：参数中写明 `/Users/foo/proj/src` 就是这个目录，与对话历史无关。

（2）输出不包含文件大小信息。结果中只有文件名和 `/` 后缀，没有 KB、字节数等元数据。这是因为 `listDir` 的定位是「快速浏览目录结构」，大小信息在绝大多数任务场景中属于噪声——用户问「看下 `src` 下有什么」，模型的回答中夹杂一堆数字反而干扰主要信息的传递。真正需要查看文件大小的场景（例如找出最大的日志文件）通过 shell 工具执行 `du` 或 `ls -lh` 更为直接。工具的输出形态决定了它适合什么场景，`listDir` 的极简输出将其定位明确限定在「探索式浏览」，而非「文件元数据查询」。

## 15.8 进度上报

三个工具的 execute 函数开头都调用一次 `reportProgress`：

```ts
// glob
reportProgress(toolCallId, `Matching ${pattern}`)
// grep
reportProgress(toolCallId, `Searching for /${pattern}/`)
// listDir
reportProgress(toolCallId, `Listing ${dirPath}`)
```

这条调用的作用是在工具结果尚未返回之前，让终端界面实时显示一行当前工具正在执行的操作。读者在终端中看到的工具调用下方那行 `⎿ ⠋ Searching for /formatDate/` 就是它推送上去的。两条边界需要说明：进度消息只面向用户展示，模型不会读到这些中间状态，它最终接收的只是工具的返回结果；`toolCallId` 是每条上报的标识符，同一轮中模型并行调用多个工具时，各工具的进度消息凭借不同的 `toolCallId` 各自显示、互不干扰。至于 `reportProgress` 内部如何将消息推送到终端界面（注册表机制、清除时机、shell 工具和子 Agent 工具是否共用同一条通路），属于流式渲染层面的话题，详见 Ch.26。本节只需要理解一点：这条调用是给用户的实时反馈，不影响工具的语义行为。

## 15.9 小结

本章围绕 `glob`、`grep` 和 `listDir` 三个代码库探索工具，讨论了它们在工具粒度、底层实现选择、输出格式和错误处理等方面的设计决策。三个工具拆分而非合并，遵循的是「单一意图合粗、多种意图拆细」的准则——找文件、搜内容和查看目录是三种独立的模型意图，合成一个工具只会增加参数选择时的认知负担。`glob` 和 `grep` 共用 `@vscode/ripgrep` 提供的 ripgrep 二进制，通过 `getRipgrepPath` 实现单进程级别的缓存和降级回退。`glob` 选择修改时间倒序输出，使得截断场景下保留的是与当前任务最相关的近期文件；对 catch-all 模式的特殊处理则避免了 `.gitignore` 被绕过导致的噪声泛滥。`grep` 的三层截断（250 行、500 列、20 MB 缓冲）各自防范一种失控场景，退出码 1 到 `No matches found.` 的语义翻译则将「无匹配」和「执行出错」在模型层面区分开来。`listDir` 只接受绝对路径以保证每次调用的语义独立性，极简的输出格式将其定位明确限定在探索式浏览而非元数据查询。下一章将讨论网络工具 `webSearch` 和 `webFetch`，它们让模型能够在对话过程中实时获取外部信息。
