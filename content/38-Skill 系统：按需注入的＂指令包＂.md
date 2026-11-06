# 第 38 章 Skill 系统：按需注入的「指令包」

上一章的 MCP 是给工具集留的扩展口，server 暴露的是可调用的函数，模型按场景选用。本章讲的 skill 走另一条扩展路径，扩展的不是工具集，而是模型在当前任务里读到的指令。一份 `SKILL.md` 写一段 Markdown，内容可以是前端组件的审美约束、提交前的自检清单、Conventional Commits 风格的 PR 标题规范这类风格 / 流程 / 偏好型的提示词；用户在会话里通过 `/<skillname>` 触发，或让模型自己判断当前任务该不该激活。激活后这段 Markdown 进入当前会话的消息历史（用户触发时附在用户消息里、模型触发时作为 `activateSkill` 工具的返回值），模型据此调整后续行为。

整套机制的产品代码集中在 `packages/core/src/skills/` 目录下，由加载器、注册表、开关设置三个模块组成，加上一个不到 40 行的激活工具供模型调用，以及 CLI 一侧的 slash 命令路由。本章将分为如下几个部分进行介绍。

- 38.1 节以 Anthropic 公开的 frontend-design skill 为例，走完安装、两种激活路径的完整流程。
- 38.2 节讨论为什么已有五条扩展路径（内置工具、slash 命令、sub-agent、MCP、AGENTS.md）还需要 skill，以及 skill 与它们的分工。
- 38.3 节说明 SKILL.md 的 frontmatter 为什么只有 `name` + `description` 两个字段。
- 38.4 节展开双激活路径的实现：模型自决的 `activateSkill` 工具与用户显式的 `/<skillname>`。
- 38.5 节讲解注册表的启动构建、会话冻结与 `/skill refresh` 热加载机制。
- 38.6 节讨论 disable 为什么走独立的 `settings.json` 而非删除文件。
- 38.7 节说明 `/skill install` 为什么必须走原始字节下载而非 `webFetch`。
- 38.8 节讨论纯 prompt skill 在长会话里被 lost in the middle 衰减的真实限制及缓解方案。
- 38.9 节给出本章小结。

> AI 知识背景：什么是 skill
>
> skill 在 AI Coding Agent 圈子里有几种相近的含义。本章使用的定义是 2025 年陆续被主流 CLI 客户端采纳的形态：一段用户自己写的、用 Markdown 文件管理的、面向 LLM 的角色化指令，配带一份极简元数据（名字 + 一句描述）让模型识别「该不该把这段指令拉进当前对话」。文件按 scope 分两层组织：用户级（`~/.x-code/skills/`，`~` 在 Windows 上对应 `C:\Users\<用户名>\`）负责跨项目通用的 skill、项目级（`<仓库根目录>/.x-code/skills/`）负责仓库级别的 skill，CLI 启动时一次性扫描，运行时由模型或用户触发激活。激活的效果是把 Markdown 正文插入消息历史（模型触发走 `tool_result`、用户触发走 user 消息前置；下文展开），模型读完按指令调整接下来的工具调用和措辞。
>
> 形态上，skill 和 VS Code Snippet 颇为接近。相同点是都把一份用户预先写好的小片段、用一个短触发名挂到编辑器 / CLI 上，由用户输入触发；都是文件驱动（VS Code 的 `*.code-snippets`、X-Code 的 `SKILL.md`）；都按作用域分层组织（VS Code 有 user / workspace 两层，X-Code 有 user / project 两层）。不同点是 snippet 属于字面字符串展开，按 tab stop 插入到光标处，行为完全确定；skill 是写给 LLM 看的指令，模型读完仍有判断空间，最终行为受模型遵循度影响。snippet 是 deterministic 文本替换；skill 是 prompt 工程的载体。
>
> <!-- analogy: skill-vscode-snippet -->

## 38.1 一次完整的例子：安装 frontend-design skill

前面定义里已经说清 skill 的本质是用户写一段 Markdown，但实际工作流里多数用户不会从零写，更常见的做法是从某个公开的 skill 仓库安装一份现成的。Anthropic 在 [`anthropics/skills`](https://github.com/anthropics/skills) 公开了十几个官方 skill（文档生成、UI 设计、API helper、MCP server 模板等），社区也陆续贡献了更多。第一次接触 skill 的读者最好直接装一份现成的 skill，把完整闭环跑通，再考虑要不要自己写。

以 `frontend-design` 为例。这是 Anthropic 公开的最有代表性的 skill 之一，专门约束模型写前端代码时跳出 AI 生成 UI 的同质化倾向（灰底紫渐变、Inter 字体、圆角卡片堆叠这种千篇一律的形态），改成有明确审美方向的设计。它的 frontmatter 如下：

> 源码：[`anthropics/skills/skills/frontend-design/SKILL.md`](https://github.com/anthropics/skills/blob/main/skills/frontend-design/SKILL.md)（YAML frontmatter 摘录）

```yaml
---
name: frontend-design
description: Create distinctive, production-grade frontend interfaces with high
  design quality. Use this skill when the user asks to build web components,
  pages, artifacts, posters, or applications (examples include websites, landing
  pages, dashboards, React components, HTML/CSS layouts, or when styling/
  beautifying any web UI). Generates creative, polished code and UI design that
  avoids generic AI aesthetics.
---
```

正文是一篇一百多行的设计约束手册，主要包括以下几项约束：先确立一个鲜明的概念方向（minimalist / maximalist / retro / brutalist 等）再开始实现、避开 AI 设计的陈词滥调（紫色渐变 / Inter 字体 / cookie-cutter 布局）、选有性格的字体和有主色 + sharp accent 的色板、动效服务于设计意图而非装饰。

### 安装

X-Code CLI 提供 `/skill install <url>` 命令，参数是一个 SKILL.md 的原始字节 URL（GitHub 上需要用 `raw.githubusercontent.com` 域名；`github.com` 域名下的 URL 是经过 HTML 渲染的页面，会破坏 YAML frontmatter）：

```text
> /skill install https://raw.githubusercontent.com/anthropics/skills/main/skills/frontend-design/SKILL.md
  ⎿  Skill frontend-design installed to ~/.x-code/skills/frontend-design/SKILL.md
     Run `/skill refresh` to use /frontend-design now, or restart xc.

> /skill refresh
  ⎿  Reloaded skills — added: frontend-design.
     Note: next message rebuilds the system prompt, so prompt-cache will miss once.
```

执行 `/skill list` 确认已加载：

```text
> /skill list
  ⎿  Loaded skills (1):
     - [on]  frontend-design (user): Create distinctive, production-grade frontend
       interfaces with high design quality. Use this skill when the user asks to build
       web components, pages, artifacts, posters, or applications (examples include
       websites, landing pages, dashboards, React components, HTML/CSS layouts, or
       when styling/beautifying any web UI). Generates creative, polished code and
       UI design that avoids generic AI aesthetics.
```

`[on]` 表示已启用、`user` 表示这份文件来自用户级 scope（项目级 scope 在 `<仓库根目录>/.x-code/skills/<name>/SKILL.md`）。description 是一段在 SKILL.md frontmatter 里写好的字面文本，CLI 不做任何修改。这段 description 除了在 `/skill list` 里展示给用户，更重要的用途是面向模型：它会出现在每一轮发给模型的系统提示词里（38.4 节会看到的 `## Available Skills` 段落），让模型知道当前有哪些 skill 可用、各自适用什么场景，从而在合适的时机自动激活。下面分别看两种激活路径的形态。

### 路径 A：用户显式触发

直接在输入框输入 `/frontend-design`，加上具体诉求：

```text
> /frontend-design 帮我写一个看板的 dashboard 首页,主要展示团队过去 30 天的关键指标
```

CLI 把这条输入翻译成一条带 skill 前置的 user 消息送入 `agentLoop`：

```text
<activated_skill name="frontend-design">
# Frontend Design

Establish aesthetic direction first. Choose a bold conceptual angle...
...(SKILL.md 正文全文,见 anthropics/skills 仓库)
</activated_skill>

帮我写一个看板的 dashboard 首页,主要展示团队过去 30 天的关键指标
```

模型看到 `<activated_skill>` 块后，按 frontend-design 的指令工作：先确立一个鲜明的设计方向（不是默认的中性卡片 + 灰边，可能是 brutalist 大色块 + 等宽字体或杂志感的不对称 layout）、选有性格的字体（不再默认 Inter）、配有主色 + sharp accent 的色板、动效服务于设计意图。整个会话里这段 Markdown 一直保留在消息历史中，模型后续每一步都受这份 prompt 约束。

上面这条 user 消息里的 `<activated_skill>` 块省略了 footer，实际注入到模型的内容在 SKILL.md 正文之后还跟着两段元信息（skill 目录的绝对路径 + 目录里非 SKILL.md 的文件清单），格式见 38.4 节「envelope 长什么样」的详解。

### 路径 B：模型自决

用户没有显式调用 skill，但表达了相关诉求：

```text
> 帮我重做这个产品官网的 hero section,现在太普通了
```

由于 skill 信息已经在 system prompt 的 `## Available Skills` 一节里告诉了模型：

```text
## Available Skills
Use the activateSkill tool to inject a skill's instructions when the task matches its description:
- frontend-design: Create distinctive, production-grade frontend interfaces with high
  design quality. Use this skill when the user asks to build web components, pages,
  artifacts, posters, or applications (examples include websites, landing pages,
  dashboards, React components, HTML/CSS layouts, or when styling/beautifying any web
  UI). Generates creative, polished code and UI design that avoids generic AI aesthetics.
```

模型读到用户说「hero section + 重做 + 太普通」，匹配上 frontend-design 的 description，决定先激活 skill：

```text
● activateSkill(name: "frontend-design")
  ⎿  <activated_skill name="frontend-design">
     # Frontend Design
     ... +180 lines
     </activated_skill>

  好的,我先确定一个明确的设计方向再开始动手,避开 AI 默认的那种千篇一律的布局......
```

`activateSkill` 工具返回的 `<activated_skill>` 块进入 `tool_result`，模型紧接着发起 `readFile` 等具体动作。两条路径注入的消息类型不同（路径 A 是 user 消息前置、路径 B 是 `tool_result`），但 `<activated_skill>...</activated_skill>` 这段 XML 体在两种封装里字节相同，模型靠这个标签识别「这段是 skill 指令、不是普通的对话或读文件结果」。两条路径的差异只在激活决策者：一边是用户、一边是模型。

下面分别讨论支撑这两条路径的几个关键决定。

## 38.2 决定一：为什么已有五条扩展路径还需要 skill

X-Code CLI 已经有内置工具、slash 命令、sub-agent、MCP 工具、AGENTS.md 五条扩展路径（前几章分别讲过），skill 是第六条。读者自然会问：已有的路径够不够用？下面这张对照表把六条路径按四个维度排开，skill 和其他五条的差异也就能看出来了：

<!-- check:ignore=punctuation -->

| 路径          | 是谁写的                | 触发方                      | 进入哪份上下文                              | 改完才生效需要 |
| ------------- | ----------------------- | --------------------------- | ------------------------------------------- | -------------- |
| 内置工具      | CLI 作者                | 模型自决（`readFile` 等）   | 当前对话（tool_result）                     | 重启           |
| slash 命令    | CLI 作者                | 用户显式（`/compact` 等）   | 多数直接走 CLI 代码不进对话；少数（`/init`）以 user 消息形式提交 | 重启 |
| sub-agent     | CLI 作者 + 用户         | 模型自决（`task` 工具）     | 新开一份独立的子 loop（详见 Ch.19）         | 重启           |
| MCP 工具      | server 作者（用户配置接入） | 模型自决（调用 MCP 工具）   | 当前对话（tool_result）                     | `/mcp refresh` 或重启 |
| AGENTS.md     | 用户                    | 总是注入                    | 每一轮 system prompt 都带                   | 下一条 user 消息（prompt-cache miss 一次） |
| skill         | 用户                    | 用户显式 + 模型自决         | 当前对话（user 消息或 tool_result）         | `/skill refresh` 或重启 |

表里最值得关注是以下三列：是谁写的、谁来触发、进入哪份上下文。

是谁写的：skill 和 AGENTS.md 一样由用户编写 Markdown 文件。内置工具和 slash 命令由 CLI 作者打包在源码里，用户无法在不修改源码的前提下进行修改；MCP 工具由 server 作者实现并远程提供，用户只能通过配置接入。skill 的编写权完全在用户手里，随时可以修改，不依赖 CLI 发版。

触发方：skill 是六条路径里唯一同时支持用户显式触发（`/<skillname>`）和模型自决（`activateSkill` 工具）的形态。slash 命令只能用户触发；sub-agent 和 MCP 工具只能模型触发；AGENTS.md 每一轮自动注入，不存在「触发」概念。两条路径互为补充：用户清楚知道当前任务需要哪个 skill 时直接触发，不确定时交给模型根据 description 自行判断。

进入哪份上下文：skill 注入到当前对话的消息历史里，和 sub-agent 形成对比。sub-agent 新开一个独立 loop，中间产物隔离在子对话中不影响主对话；skill 留在当前 loop，指令持续影响后续每一步推理。两者是相反的取舍——sub-agent 牺牲共享上下文换取隔离，skill 牺牲隔离换取共享上下文。

skill 和上一章的 MCP 分工也不重叠。MCP 扩展的是模型能调用的函数库（`find_issues`、`search_repos` 这类可执行的动作），skill 扩展的是模型读到的行为指令（「写前端代码时避开默认的灰底紫渐变」这类风格和流程约束）。举个例子：想让模型连通 Sentry 后端拿 issue 数据，应该接入一个 MCP server；想让模型写前端时遵循一套审美标准，应该写一份 skill。两者解决的问题类型不同，各有各的适用场景。

和 AGENTS.md 的区别在 token 经济性上。AGENTS.md 每一轮 system prompt 都带，即便用户只是问一句无关的问题也要付那份 token 成本；skill 只在被激活后才占消息历史 token，没人激活就是零成本。代价是 skill 依赖 description 做匹配——如果 description 写得不够准确，模型判断不出当前任务需要这个 skill，该激活时没有激活，用户甚至不知道自己装了一个本该生效的 skill。

总结一下 skill 的定位：用户编写、两种方式触发、按需注入当前对话、不占未激活时的 token。

## 38.3 决定二：SKILL.md 的 frontmatter 只有两个字段

X-Code 的 SKILL.md frontmatter schema 由两个必填字段定义：

> 源码：`packages/core/src/skills/loader.ts`

```ts
const frontmatterSchema = z.object({
  name: z.string().min(1),
  description: z.string().min(1),
})
```

`name` 是 skill 的唯一标识。用户在输入框敲 `/frontend-design` 触发、模型调 `activateSkill({ name: "frontend-design" })` 自决激活、loader 按 `skills/frontend-design/` 目录名扫描——三处用的都是这个 name 值，任何一处不匹配 skill 就找不到。`description` 是写给模型看的一句话说明：38.1 节已经看到 `/skill list` 的输出里有一长段 description 文本，这段文本同样会出现在每一轮发给模型的系统提示词里。模型每一轮推理都会读到这份列表，判断当前任务是否匹配某个 skill 的描述；匹配上了就考虑调用激活工具，匹配不上就跳过。一份合法 SKILL.md 的最短形态是：

```markdown
---
name: my-skill
description: One-line description shown to the model
---

The Markdown body the model reads after activation...
```

SKILL.md 在 X-Code 里只做一件事：提供一段激活后注入消息历史的 Markdown 指令。body 是模型读到的指令正文，frontmatter 里 `name` 和 `description` 两个字段分别负责标识和匹配。后面看到的所有 skill 行为（激活、disable、三层覆盖 project > plugin > user，详见 38.6 节）都不依赖 frontmatter 提供额外信息。

### 兼容其他工具的 SKILL.md

其他同类工具也采用相同的 SKILL.md 格式（YAML frontmatter + Markdown body），用户可以直接把别的生态里的 skill 文件拿来用。这些文件的 frontmatter 里如果有其他 X-Code 不识别的字段（比如 `allowed-tools`、`model`、各种 hook 声明），loader 也能正常处理：

> 源码：`packages/core/src/skills/loader.ts`（`parseFrontmatter` + zod 校验）

```ts
const frontmatterSchema = z.object({
  name: z.string().min(1),
  description: z.string().min(1),
})
// ...
const result = frontmatterSchema.safeParse(parsed.data)
```

zod 的 `z.object({...})` 默认行为允许未声明的字段通过校验，X-Code 只取出 `name` 和 `description`，其他字段静默忽略。因此一份带十几个额外字段的 SKILL.md 在 X-Code 里也能正常加载，body 照常注入，模型照常按指令工作。额外字段在原生态里的能力（比如硬权限约束）在 X-Code 里不会生效，但 skill 的核心功能不受影响。

## 38.4 决定三：双激活路径，模型自决与用户显式都保留

X-Code CLI 同时保留了两条激活路径：模型自决（通过 `activateSkill` 工具）和用户显式（通过 `/<skillname>`）。两条路径互为补充——用户明确知道当前任务需要哪个 skill 时直接触发，省去等模型判断的延迟；用户不确定时交给模型根据 description 自行匹配，装了几十个 skill 之后不需要记住每一条的名字。

### 模型自决：activateSkill 工具

只有当 `SkillRegistry` 里至少有一条激活的 skill 时，工具集才会注册 `activateSkill`：

> 源码：`packages/core/src/agent/loop.ts`（`buildTools` 摘录）

```ts
if (options.skillRegistry && options.skillRegistry.names().length > 0) {
  tools.activateSkill = createActivateSkillTool(options.skillRegistry)
}
```

工具本身只做一件事，按 `name` 查 registry、把 skill 内容交给 `wrapActivatedSkill()` 包成 XML envelope（XML 信封，指外面套一对 `<activated_skill>...</activated_skill>` 标签把指令体包起来）返回：

> 源码：`packages/core/src/tools/activate-skill.ts`

```ts
return tool({
  description: `Activate a skill to inject its instructions into the conversation. Available skills: ${nameList}. Call this when the current task clearly matches one of those skill descriptions.`,
  inputSchema: z.object({
    name: z.string().describe('Name of the skill to activate'),
  }),
  execute: async ({ name }) => {
    const skill = registry.get(name)
    if (!skill) {
      const available = registry.names()
      return available.length > 0
       ? `Skill "${name}" not found. Available: ${available.join(', ')}`
        : `Skill "${name}" not found. No skills are currently loaded.`
    }
    return wrapActivatedSkill(skill)
  },
})
```

工具描述里把当前所有可激活的 skill 名拼成一份列表。这意味着模型在两个地方都能看到 skill 信息：一处是系统提示词的可激活列表（带 description），另一处是工具自身的描述（只有名字）。两处冗余让模型在决定激活哪个 skill 时有两次确认机会，降低了拼错名字或漏看的概率。

### 用户显式：/\<skillname\> [arg]

CLI 一侧在 slash 命令的默认分支做捕获：如果命令名不是任何已知子命令（`/help` / `/model` / `/skill` / `/mcp` ...），但在注册表里查到一条同名 skill，就走 skill 路径：

> 源码：`packages/cli/src/ui/components/App.tsx`（默认分支摘录）

```ts
default: {
  const skill = options.skillRegistry?.get(command)
  if (skill) {
    if (arg) {
      // Skill + immediate request
      echoCommand(text)
      await submit(`${wrapActivatedSkill(skill)}\n\n${arg}`, { silent: true })
    } else {
      // No follow-up yet — store as pending
      pendingSkillRef.current = skill
      addCommandMessage(text, `Skill **${skill.name}** loaded. Type your request.`)
    }
    return
  }
  addCommandMessage(text, `Unknown command: /${command}. Type /help for available commands.`)
  return
}
```

`activateSkill` 工具和这条 default 分支都调同一个 `wrapActivatedSkill()`。两条路径的消息封装类型不同（user 消息 vs `tool_result`），但 envelope 里包的内容相同，模型识别 skill 靠 XML 标签，不需要为两种来源各学一套解读方式。

### envelope 长什么样：body + 目录路径 + 文件清单

`wrapActivatedSkill()` 把 SKILL.md 正文 + 一段 footer 一起包进 `<activated_skill>` 标签：

> 源码：`packages/core/src/skills/registry.ts`

```ts
export function formatSkillActivationBody(skill: SkillDefinition): string {
  const lines: string[] = [skill.content.trim(), '']
  lines.push(`Base directory for this skill: ${skill.dir}`)
  lines.push(
    'Relative paths in this skill (e.g., scripts/foo.sh, references/api.md) are resolved against the base directory above.',
  )
  if (skill.files.length > 0) {
    lines.push('', 'Files in this skill directory:')
    const shown = skill.files.slice(0, MAX_RENDERED_FILES)
    for (const f of shown) lines.push(`- ${f}`)
    if (skill.files.length > MAX_RENDERED_FILES) {
      lines.push(`- ... and ${skill.files.length - MAX_RENDERED_FILES} more file(s) not shown`)
    }
  }
  return lines.join('\n')
}

export function wrapActivatedSkill(skill: SkillDefinition): string {
  return `<activated_skill name="${skill.name}">\n${formatSkillActivationBody(skill)}\n</activated_skill>`
}
```

footer 多出来这两段元信息，是为了解决两个真实问题。

Base directory：SKILL.md 正文里如果写「实现完之后跑 `scripts/preflight.sh`」，模型需要知道这个相对路径相对的基准在哪。把目录绝对路径直接注入给模型，省去先调 `pwd` / `Glob` 探一遍才能调 `shell` 工具的来回。尤其在 Windows 上，绝对路径例如 `C:\Users\user\.x-code\skills\frontend-design`，盘符、用户名段都因机器而异，不显式注入模型只能反复 `pwd` / `Glob` 现查。

Files in this skill directory：把目录里非 SKILL.md 的文件一并枚举给模型。这条 footer 不读文件内容、不执行任何脚本，只是把 `references/aesthetics.md` / `scripts/preflight.sh` 这种文件名贴给模型。模型如果需要查询文档或代码，可以直接读文件；如果需要执行脚本，可以直接通过 shell 工具运行。文件列表最多枚举 50 条，扫描时跳过 `node_modules`、`.git` 等重目录和以 `.` 开头的隐藏条目，避免大型 skill 目录把激活消息撑爆。

目录路径和文件清单在启动扫描时就已填好，激活时直接使用、不做额外磁盘读取。`/skill refresh` 会重新扫描一遍，新增或删除的文件名都会跟着更新。

footer 注入的是指路牌而不是动作：告诉模型 skill 目录在哪、里面有哪些文件，但具体哪个文件该读、哪个脚本该执行、什么时候执行，都交给 SKILL.md 正文用自然语言交代，模型通过 `readFile` / `shell` 工具自行调用。这种边界让 CLI 不需要预判每个 skill 的运行节奏，模型按 SKILL.md 里写的条件（「实现完之后跑」「如果用户没改 test 文件就跑 verify」）自行决定即可。

### 没有第二轮请求的 pendingSkill 模式

用户输入 `/frontend-design` 不带参数时，CLI 面临一个选择：应该立刻发起一次推理，还是等用户后续输入？如果直接 submit `<activated_skill>...</activated_skill>` 这一段（不带任何具体诉求），模型会困惑，因为用户给了一段风格指令但没说要做什么，往往会发起一轮无意义的推理，浪费一次模型 round-trip。

设计上选了另一条路：先暂存，等用户的下一条真实消息再合并发出。

> 源码：`packages/cli/src/ui/components/App.tsx`（`handleSubmit` 中 pendingSkill 处理）

```ts
// Prepend any pending skill context to the user's message, then clear it.
const pendingSkill = pendingSkillRef.current
if (pendingSkill) {
  pendingSkillRef.current = null
  await submit(`${wrapActivatedSkill(pendingSkill)}\n\n${text}`, { silent: true })
  return
}
await submit(text)
```

`pendingSkillRef` 是一个 React ref，类型是 `SkillDefinition | null`。用户输入 `/frontend-design` 之后它暂存整份 skill 定义（含 `dir` + `files`），UI 上只显示一行 `Skill frontend-design loaded. Type your request.`，没有向模型发出任何请求。等用户输入下一条完整诉求时，`handleSubmit` 把 pendingSkill 走同一个 `wrapActivatedSkill()` 包好、前置拼到用户消息里、清空 ref、一次性发出。模型看到的格式跟「带参 `/<skillname>`」完全一致。


## 38.5 决定四：注册表启动时构建，显式 refresh 热加载

X-Code CLI 在启动时会构建一次注册表，会话期间冻结，用户需要更新时通过 `/skill refresh` 显式触发重建。

skill 信息会嵌入系统提示词的两个位置——可激活列表和激活工具描述——两者都属于 prompt cache 的一部分（详见 Ch.33），在会话第一轮构建后不再重算。每次刷新 skill 信息都会破坏一次 prompt cache、产生一份重建成本，因此不能在用户无感知的情况下频繁触发。但完全不提供会话内的刷新手段也不行，如果用户在编辑器里改 SKILL.md、想立刻试效果，那得每次都要重启 CLI，非常的麻烦。`/skill refresh` 把刷新时机交给用户自己决定：默认冻结保住缓存，需要时显式触发一次重建。

> 没有任何 skill 时，system prompt 里只显示一段安装指引，模型知道如何帮用户安装 skill 但不会尝试激活。装了至少一份 skill 后，完整的可激活列表才出现在 system prompt 中。

CLI 启动时构造一份 `SkillRegistry` 实例，装进 `AgentOptions` 对象：

> 源码：`packages/cli/src/index.ts`（启动期）

```ts
const subAgentRegistry = await createSubAgentRegistry()
const skillRegistry = await createSkillRegistry()
// ...
const options: AgentOptions = {
  // ...
  subAgentRegistry,
  skillRegistry,
  mcpRegistry: mcpLoadResult.registry,
  // ...
}
```

`/skill refresh` 命令做的事情很简单：重新读一遍硬盘上的 skill 文件 + settings，把新内容更新到这份内存里的 `SkillRegistry` 上：

> 源码：`packages/core/src/skills/registry.ts`

```ts
export async function reloadSkillRegistry(registry: SkillRegistry): Promise<SkillReloadSummary> {
  const [skills, disabled] = await Promise.all([loadSkills(), loadDisabledSkillsSet()])
  return registry.reload(skills, disabled)
}
```

CLI 一侧 `handleSkill` 的 refresh 分支接收到 `reloadSkillRegistry()` 的返回值时，会做四件事：

> 源码：`packages/cli/src/ui/commands/skill.ts`（`handleSkill` refresh 分支摘录）

```ts
const summary = await reloadSkillRegistry(options.skillRegistry)
// 1) 清空 system prompt 缓存
invalidateSystemPromptCache()
// 2) 清掉可能已经悬空的 pending skill
const pending = pendingSkillRef.current
if (pending && !options.skillRegistry.get(pending.name)) {
  pendingSkillRef.current = null
}
// 3) 告诉 React tab 补全数据变了
bumpSkillRegistryVersion()
// 4) 拼 added / removed / changed / unchanged 四类
const summaryParts: string[] = []
if (summary.added.length) summaryParts.push(`added: ${summary.added.join(', ')}`)
if (summary.removed.length) summaryParts.push(`removed: ${summary.removed.join(', ')}`)
if (summary.changed.length) summaryParts.push(`changed: ${summary.changed.join(', ')}`)
if (summary.unchanged.length) summaryParts.push(`unchanged: ${summary.unchanged.join(', ')}`)
if (summaryParts.length === 0) summaryParts.push('no skills found')
// 5) 提示用户下一条消息会付 prompt-cache 重建的成本
const lines = [`Reloaded skills — ${summaryParts.join('; ')}.`]
lines.push('Note: next message rebuilds the system prompt, so prompt-cache will miss once.')
addCommandMessage(text, lines.join('\n'))
```

这四步各自解决一个问题。

（1）清空 system prompt 缓存。skill 列表嵌在 system prompt 和激活工具描述两处，两者在会话第一轮就被缓存（详见 Ch.33）。不清缓存的话，下一轮发给模型的仍是旧版 skill 列表。

（2）校验暂存的 pending skill。用户之前输入 `/frontend-design` 不带参数时，skill 内容会暂存在内存里等待下一条消息合并发出。如果 refresh 恰好把这个 skill 移除或禁用了，暂存内容就过时了，必须清掉，否则下一条消息会静默带上已删除的 skill 指令。

（3）通知 tab 补全列表重算。注册表是原地更新的，对象引用没变，React 的 `useMemo` 不会察觉内部数据已经换过。这里递增一个版本号触发重算，让补全菜单反映新的 skill 列表。

（4）提示用户下一条消息会触发缓存重建。system prompt 缓存清空后，下一轮需要完整重建 prompt cache，首字延迟和 token 成本比平时稍高。CLI 在命令输出末尾加一行提示，让用户对这次额外开销有预期，与 `/mcp refresh` 的做法一致。

### 跟 sub-agent / MCP 的同源设计

这套「启动加载 → 会话冻结 → 显式 refresh 重建」的流程在 X-Code 里不是 skill 独占，sub-agent（Ch.19）和 MCP（Ch.37）都用同一套模式。共同的根本约束是 prompt cache：system prompt 里的内容在会话中途变了一次，缓存就会失效。因此三者都选「启动时构造一次、会话内不变、变更通过显式命令重建」，把缓存重建的成本暴露给用户而非悄悄发生。

三者目前的差别只在于 sub-agent 还没做自己的 refresh 命令。

## 38.6 决定五：disable 走独立 settings.json，不动 SKILL.md 文件本身

X-Code CLI 把「禁用」和「卸载」分开处理：禁用只在 settings 文件里加一条记录，SKILL.md 原封不动留在磁盘上，重新启用时改一下 settings 即可。如果通过删文件来实现禁用，那成本就太高了。

禁用和启用都通过 `/skill` 子命令完成：

```text
> /skill disable frontend-design
  ⎿  Disabled skill frontend-design in user settings (~/.x-code/settings.json).
     Run `/skill refresh` to apply now, or restart xc.

> /skill enable frontend-design
  ⎿  Enabled skill frontend-design in user settings (~/.x-code/settings.json).
     Run `/skill refresh` to apply now, or restart xc.
```

禁用记录写在 settings 文件的 `disabledSkills` 数组里，分用户级（`~/.x-code/settings.json`）和项目级（`<repo>/.x-code/settings.local.json`）两份，两份走并集合并——任一 scope 列了某个 skill，它就算禁用。禁用后模型彻底看不到这个 skill：系统提示词的可激活列表和激活工具描述里都会跳过已禁用的条目。`/skill list` 仍然会列出所有条目（包括已禁用的），每条带 `[on]` / `[off]` 标记，让用户知道哪些是关着的。

`/skill uninstall <name>` 是另一类操作：直接删除 `<skills-dir>/<name>/` 整个目录，同时清掉两份 settings 里可能残留的禁用条目，避免下次安装同名 skill 时被旧记录拦截。

### plugin 来源的 skill

除了用户手写的 skill，插件也能贡献 skill。用户执行 `/plugin install <id>` 装一个插件，插件的 manifest 里如果声明了 skills 目录，该目录下的 SKILL.md 会被折进同一份注册表，和手写的 skill 激活方式完全一致。

三类 skill 在注册表里按 project > plugin > user 的顺序合并，同名时后者覆盖前者。项目级优先是因为团队约定的优先级最高；plugin 位于中间，既能覆盖用户的默认值，又允许项目级进一步覆盖。插件贡献的 skill 生命周期归插件子系统管：如果用户对这类 skill 执行 `/skill uninstall`，CLI 会拦截并提示改用 `/plugin uninstall`。插件系统详见 Ch.39。

## 38.7 决定六：/skill install 必须走原始字节下载

安装 skill 时，无论是用户执行 `/skill install <url>` 还是模型通过 shell 工具下载，都必须走原始字节下载（`fetch`、`curl`、`Invoke-WebRequest`），不能用 `webFetch + writeFile` 组合。原因是 `webFetch` 内部会对响应做 HTML / Markdown 渲染，YAML frontmatter 那对 `---` 分隔符会被渲染器丢弃或重新排版，导致写入磁盘的 SKILL.md 解析失败。系统提示词里有一条专门的指令禁止模型走 `webFetch` 路径。

下载完成后，CLI 从 frontmatter 里提取 `name` 字段，据此决定写到 `~/.x-code/skills/<name>/SKILL.md`。目录名、frontmatter 里的 `name` 字段、`/<skillname>` 触发时的命令名三者必须一致——加载器按目录名扫描、注册表按 `name` 去重、CLI 按 `name` 注册 slash 命令，任何一处不匹配都会导致 skill 找不到。

## 38.8 长会话里的 lost in the middle

skill 激活后，Markdown 指令作为一条消息留在对话历史里，后续模型的每一轮推理都能读到它。但消息历史是不断增长的：每一次工具调用、每一次模型回复都在继续追加内容。在多轮对话之后，早期注入的 skill 指令有可能被推到了上下文的中段位置，离模型当前正在处理的末尾越来越远。

Ch.31 介绍过 Lost in the Middle 现象：LLM 对上下文中段内容的注意力显著低于开头和结尾，呈 U 型衰减。这一性质对 skill 有直接影响。skill 激活后，指令作为一条消息固定在对话历史的某个位置，不会随对话推进而移动。三五轮内 skill 块还在末尾附近，模型对它的注意力命中率高；但在多轮对话之后，skill 指令的位置已经被推到中段、被几十条工具调用结果包夹，模型读到这条指令的概率开始下降。具体表现是模型「该按 skill 风格写代码却偶尔忘了」——不是模型故意不遵守，是它对这条指令的注意力在衰减。

产品代码层面没有针对这条限制的自动修复机制，因为重新注入一遍 SKILL.md 意味着破坏 prompt cache 并额外消耗一轮 token。不过用户可以在使用方式上做一层补偿：在 skill 目录里放一个脚本，让模型在关键节点（如交付前）主动调用，把原本依赖注意力的约束转换为可执行的检查。下面是一个具体例子。

### 例：为 frontend-design skill 增加一个交付前自检脚本

frontend-design 的 SKILL.md 内容为设计规则：例如「先确立设计方向」「避开紫色渐变」「动效服务于设计意图」等等。这些约束在长会话里很容易偏离方向：设计规则本身难以用脚本量化，但是仍存在一类可以用代码来验证的失败模式（代码能否通过 build、是否存在 lint 错误、a11y 静态检查有没有违例），让脚本承担这部分，把「交付前的最低基线」从模型注意力的模糊匹配换成显式校验点。

升级到「prompt + 脚本」形态分两步。

第一步，在 skill 目录里多放一个文件 `~/.x-code/skills/frontend-design/scripts/preflight.sh`：

```bash
#!/usr/bin/env bash
# 实现完之后跑: lint + build + a11y 静态检查必须都通过
set -e
echo "=== Lint ==="
npm run lint
echo "=== Build ==="
npm run build
echo "=== A11y (axe on built artifact) ==="
npx @axe-core/cli dist/index.html --exit
echo "ALL CHECKS PASSED"
```

第二步，在 SKILL.md 正文里加一段显式调用指令：

```markdown
After implementing the change, run this script before declaring done:

  bash scripts/preflight.sh

(The "Base directory for this skill" injected with this skill tells you the
absolute path to prepend.) If any check fails (set -e exits non-zero), fix
the issues and re-run. Only report completion after seeing "ALL CHECKS
PASSED" at the end of the output.
```

CLI 一侧的 loader 没有任何变化，SKILL.md 仍是纯 Markdown，只是 body 里多了一段引用脚本相对路径的说明文字。绝对路径不需要 SKILL.md 自己写死，激活时注入的 footer 里的 `Base directory for this skill: <abs path>` 一行已经告诉了模型怎么把 `scripts/preflight.sh` 拼成可执行的绝对路径，模型通过 `shell` 工具执行脚本即可。注意 Anthropic 提供的 frontend-design SKILL.md 原本不包含这段脚本调用，在用户级 skill 目录里直接编辑装好的 `~/.x-code/skills/frontend-design/SKILL.md`、将这段内容追加进去即可（同时把 `preflight.sh` 放进 `scripts/` 子目录），下次 `/skill refresh` 之后这段新增内容就会进 system prompt，新增的脚本文件名也会被 footer 自动枚举进 `Files in this skill directory` 一段。

这样改完之后，无论会话进行到第 30 轮还是第 50 轮，到了「实现完成、准备提交」这个节点，模型按 SKILL.md 指令执行一次 `preflight.sh`，就会立刻得到与上下文位置无关的验证结果：lint 错误数、build 是否失败、a11y 违例条数，通过则输出 `ALL CHECKS PASSED`、失败则输出具体的报错堆栈。「设计规则是否得到贯彻」仍交给模型注意力去把握，但「代码能编译通过、能符合 a11y 标准」这条最低基线被锚定到显式的命令输出。`ALL CHECKS PASSED` 或具体的 `error: ...` 永远落在 tool_result 末尾、始终位于模型当前注意力的 recency 区，不会被 lost in the middle 衰减。

## 38.9 小结

skill 是 X-Code CLI 的第六条扩展路径，和前五条的区别在于：它扩展的是模型读到的指令，而非模型能调用的工具。一份 SKILL.md 的 frontmatter 只保留 `name` 和 `description` 两个字段，`name` 同时充当命令名、目录名和工具参数的统一标识，`description` 写进系统提示词的可激活列表让模型判断何时激活。激活有两条路径，用户显式触发时 skill 内容前置在 user 消息里，模型自决触发时 skill 内容作为 `activateSkill` 的工具返回值进入对话，两条路径共享同一个格式器，注入的 XML 体字节相同。

注册表在 CLI 启动时一次性构建，会话内默认冻结，需要热加载时用 `/skill refresh` 显式触发。禁用机制走独立的 `settings.json` 而非删除文件，避免用户丢失精心编写的 SKILL.md。插件贡献的 skill 合并进同一注册表，优先级为 project > plugin > user。安装必须走原始字节下载，经过 HTML 渲染的 URL 会破坏 YAML frontmatter。

纯 prompt skill 在长会话里会受 lost in the middle 衰减影响，指令被推到上下文中段后模型的遵循度下降。缓解方案是在 skill 目录里放一个检查脚本，让模型在关键节点（如交付前）主动执行，把可机器验证的基线从注意力的模糊匹配转成末尾处的命令输出。
