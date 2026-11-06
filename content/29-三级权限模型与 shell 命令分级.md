# 第 29 章 三级权限模型与 shell 命令分级

Agent CLI 的「危险」集中在三个工具：`writeFile` / `edit` / `shell`。权限模型是用户与 AI 之间的信任边界，既不能每次都问（频繁打断会让用户厌烦），也不能默认放行（破坏性命令有失控风险）。X-Code CLI 的做法是按「工具类别 + shell 命令内容」双重判定，把所有工具调用分到 always-allow / ask / deny 三档，shell 内部还有一层动态分级。三档的区别不在于能否执行，而在于用户看到的确认对话框不同：always-allow 无对话框，ask 弹普通确认框，deny 弹带红色 `[dangerous]` 警告的确认框。权限模型在思路上很像 Unix 的 `sudo` + 文件权限位组合。相同点是按「操作风险等级」分档放行，把高危操作单独抬出来要二次确认；不同点是 Unix 权限按「用户身份 + 文件属主」静态判定，权限位写在 inode 里，而 Agent CLI 的判定主体是模型而不是用户，规则按「工具种类 + shell 命令内容」动态算出来。同一个 `shell` 工具里 `ls` 走 always-allow、`git commit` 走 ask、`rm -rf` 走 deny。本章将分为如下几个部分进行介绍。<!-- analogy: permission-sudo-filemode -->

- 29.1 节介绍三档权限模型的设计思路与各工具的静态映射。
- 29.2 节讲解 shell 命令的动态分级：如何拆子命令、按破坏性和只读性分档。
- 29.3 节梳理 checkPermission 的四层判定流程。
- 29.4 节讨论「don't ask again」规则的匹配策略与前缀提取逻辑。
- 29.5 节说明 --trust 模式的适用场景与安全保障。
- 29.6 节给出本章小结。

## 29.1 三档权限：always-allow / ask / deny

三档不是简单的「yes / no / 再问一次」，而是按「风险等级 + 操作可逆性」分档。`always-allow` 自动放行，零打断。只读工具（`readFile` / `glob` / `grep` / `listDir`）和外部只读 API（`webSearch` / `webFetch`）不修改任何文件或系统状态，所以不需要弹窗确认；`askUser` 也归入这一档，因为它不产生文件或系统层面的副作用，且功能本身就是向用户提问，提问前再加一层权限确认只会造成重复打断。`ask` 弹对话框等用户确认，写文件、执行普通 shell 命令默认走这一档。`deny` 同样弹对话框，但对话框带红色 `[dangerous]` 警告标签。`rm -rf /` / `dd if=…` 这类破坏性命令仍然需要用户明确批准才能执行，与 ask 的区别在于视觉上的危险提示更醒目，让用户不会误点通过。

每个工具静态映射到一个权限档，唯独 `shell` 是动态判定（按命令内容分级，详见 29.2 节）。下表列出了权限表登记的 10 个工具与各自的权限档。

<!-- check:ignore=punctuation -->

| 工具       | 权限档        | 备注                  |
| ---------- | ------------- | --------------------- |
| readFile   | always-allow  | 只读                  |
| glob       | always-allow  | 只读                  |
| grep       | always-allow  | 只读                  |
| listDir    | always-allow  | 只读                  |
| webSearch  | always-allow  | 外部只读              |
| webFetch   | always-allow  | 外部只读              |
| askUser    | always-allow  | 交互                  |
| edit       | ask           | 改文件                |
| writeFile  | ask           | 写文件                |
| shell      | 动态判定       | 按命令拆分子命令分级  |

10 个工具里 7 个固定 always-allow，真正会经过 `checkPermission` 判定的只有 `writeFile` / `edit` / `shell` 三个。`todoWrite` / `exitPlanMode` 不在权限表里，因为它们只改内存状态，不产生外部副作用，连权限检查都不走。`enterPlanMode` 虽然也不在权限表里，但它在自己的 handler 中直接调用 `onAskPermission` 弹出 Yes / No 对话框，走的是一条独立的确认路径。

## 29.2 shell 命令的动态分级

29.1 节的表里，除 shell 外每个工具都固定在某一档。shell 不同：如果一律走 `ask`，`ls` / `cat` 这种只读命令每次都弹确认，过度打断用户；如果一律走 `always-allow`，`rm -rf /` 这种破坏性命令就会直接通过。因此 shell 按命令内容动态判定，分三步。首先，按 `&&` / `||` / `;` / `|` 把复合命令拆成子命令数组。然后检查破坏性：任一子命令命中破坏性正则，整条命令判 deny，防止模型用 `ls && rm -rf .` 这种「无害 + 破坏」组合规避检测。如果没有破坏性子命令，再检查只读性：所有子命令都命中只读白名单才判 always-allow。两项都不满足的命令落到 ask，弹对话框让用户判断。

> 源码：`packages/core/src/permissions/index.ts::evaluateShellPermission`

```ts
function evaluateShellPermission(command: string): PermissionLevel {
  const subCommands = splitShellCommands(command)
  if (subCommands.some(isDestructive)) return 'deny'
  if (subCommands.every(isReadOnly)) return 'always-allow'
  return 'ask'
}
```

走一遍真实命令。模型生成 `ls -la && rm -rf ./tmp`，判定过程如下：

<!-- check:ignore=punctuation -->

```text
  cmd = 'ls -la && rm -rf ./tmp'
              │
              ▼   拆子命令
        ['ls -la', 'rm -rf ./tmp']
              │
              ▼   逐个检查破坏性
        'ls -la'        -> 否
        'rm -rf ./tmp'  -> 是 (命中破坏性正则)
              │
              ▼   任一破坏性 -> 整条命令 deny
```

关键不是「对单条命令的判决」，而是「对整条命令链的判决」。只要链上有任意一节是破坏性的，整条命令标记为 deny，对话框会显示 `[dangerous]` 警告。破坏性判定使用正则数组匹配，涵盖文件销毁（`rm -rf`、`sudo …`、`mkfs`、`dd if=…`）、Git 破坏（`git push --force`、`reset --hard`、`clean -f`）、远程拉取直接执行（`curl … | sh`）、系统控制（`shutdown`、`reboot`）、数据库销毁（`DROP DATABASE`、`TRUNCATE TABLE`）、容器清理（`docker system prune`）、包发布（`npm publish`）等类别。只读判定使用精确白名单，子命令开头匹配 `ls` / `cat` / `pwd` / `find` / `git status` / `git log` 等才算只读，且所有子命令都得是只读，整条才放行。

为什么破坏性用正则、只读用精确白名单？两者方向不同。破坏性是否定逻辑，模型可能写出 `rm -rf` 的各种变体（带 `sudo`、带相对路径、放进 `bash -c` 里），要用正则覆盖，任一命中即拒。只读是肯定逻辑，要绝对确定无副作用才放行，白名单越严越安全。没在白名单里的命令走 ask 让用户判断。两种判定的方向都是偏保守：宁可多弹一次确认框，也不能让一条未审查的破坏性命令静默通过。

## 29.3 四层判定：checkPermission

所有工具调用在执行前都经过 `checkPermission` 统一入口，按优先级从高到低短路判定。

（1）always-allow 或 trust 模式 → 直接放行。只读工具天然无副作用，trust 是用户主动选的全局放行（详见 29.5 节）。这一层同时覆盖了 deny 级别的命令。在 trust 模式下，即使是 `rm -rf` 也会直接通过，因为用户已经通过命令行参数明确表示信任模型的判断。

（2）acceptEdits 模式 + 写工具 + 路径安全 → 直接放行。仅适用于 `writeFile` / `edit`，目标路径必须在项目目录内且不是敏感文件（`.bashrc`、`.git/`、`.env`、`.ssh/` 等）。路径不满足条件则落到下一层（`permissionMode` 的三档语义详见 Ch.18）。

（3）会话规则匹配 → 命中已保存的 AllowRule 时自动放行，不弹对话框（29.4 节细讲）。

（4）ask 路径 → 弹对话框等用户响应。deny 级别的命令也走这一层，区别在于对话框会显示红色 `[dangerous]` 标签，提醒用户该命令属于破坏性操作。用户选择 always 时生成 AllowRule 存入会话，选择 yes 本次放行，选择 no 拒绝。

为什么不把 deny 做成硬拦截？早期版本确实如此，deny 级命令直接返回 false，trust 模式也无法覆盖。但实际使用中发现，用户有时明确需要执行破坏性命令（如清理构建缓存的 `rm -rf dist/`），硬拦截迫使用户退出 Agent 手动执行，工作流被打断。改为弹对话框后，决策权回到用户手中：看到 `[dangerous]` 标签后可以选择批准或拒绝，而系统提示词也相应调整为「破坏性命令按用户要求执行，权限系统会显示 [dangerous] 警告并要求确认」。这种设计的 trade-off 是：安全性从「系统级硬保障」降为「用户级软保障」，如果用户不看对话框直接点批准，破坏性命令就会执行。红色警告标签和醒目的 `[dangerous]` 文字是对抗这种风险的视觉手段。

## 29.4 「don't ask again」规则记忆

用户在权限对话框选择「Yes, don't ask again for: `<label>`」后，审批决策被转化为一条 AllowRule 保存到内存。后续同类操作命中规则时自动放行，不再弹对话框。AllowRule 有三种匹配策略。

（1）prefix（前缀匹配）：适用于能提取命令前缀的 shell 命令。例如 `git commit -m "fix"` 提取出前缀 `git commit`，后续所有以 `git commit` 开头的命令自动放行。

（2）exact（精确匹配）：前缀提取失败时的回退。命令原文作为匹配依据，只有完全相同的命令才能命中。例如 `node index.js`，其中 `index.js` 是文件名而非子命令，提取函数无法从中归纳出可复用的前缀，因此回退到 exact。

（3）tool（工具全放行）：`writeFile` / `edit` 的规则类型，本会话内所有同工具操作免确认。

前缀提取是规则系统最精细的环节。提取函数需要处理三类情况：跳过命令的全局参数（如 `git -C /tmp commit` 跳过 `-C /tmp` 得到 `git commit`）、剥离白名单内的环境变量前缀（如 `NODE_ENV=prod npm run dev` 剥离 `NODE_ENV=prod` 得到 `npm run`）、识别 wrapper 命令（如 `sudo` / `bash -c` / `env` / `time` 等 21 个）并直接放弃提取。白名单内的环境变量只收行为无关的语言环境和日志开关（`NODE_ENV` / `CI` / `DEBUG` / `LANG` 等），`PATH` / `NODE_OPTIONS` / `http_proxy` 等能改变进程行为的变量一律不剥离，前缀提取因此失败，回退到 exact。这些处理的方向都是「宁可不提取也不能错误放行」，用户最多额外确认一次，远好过让一条已批准的规则吸收掉一个未审查的副作用。

shell 规则跨会话持久化到 `.x-code/local/permissions.json`，格式为 `"shell:git commit:*"` 这样的字符串数组，启动时读入内存。writeFile / edit 的规则仅当前会话有效，因为写文件的影响因项目而异，跨会话自动放行风险高于 shell 前缀规则。

## 29.5 --trust 模式

`--trust` / `-t` 命令行参数让 CLI 跳过所有权限确认，包括 deny 级别的破坏性命令。适用场景包括：用户对项目熟悉、不想每个 edit 都点确认；批量任务（一次让 Agent 做几十个文件改动）；CI 自动化（无人值守）。在 29.3 节的四层判定中，trust 模式在第一层就短路返回 true，所有工具调用（无论 always-allow、ask 还是 deny）都直接放行。这意味着 trust 模式下模型可以执行任何操作，包括 `rm -rf` 这类破坏性命令，安全保障完全依赖系统提示词对模型行为的约束。系统提示词要求模型「破坏性命令按用户要求执行」，即模型不会主动发起破坏性操作，只在用户明确要求时才生成这类命令。trust 模式的设计假设是：开启该模式的用户理解其含义，并愿意承担对应的风险。

## 29.6 小结

本章讨论了 X-Code CLI 的三级权限模型。always-allow 消除只读操作的打断，ask 让用户审查写操作的范围，deny 对破坏性命令弹出带 `[dangerous]` 警告的确认框。三档按「风险 + 可逆性」分，deny 不是硬拦截而是醒目警告，最终决策权在用户手中。shell 命令在此基础上做动态分级：按分隔符拆子命令后，任一破坏性 → 整条 deny，全只读 → 自动放行，其余 → ask。checkPermission 四层短路判定把这些机制串联起来，「don't ask again」规则通过前缀、精确和工具三种匹配策略记忆用户的审批决策。--trust 模式在此之上提供全局放行，所有级别（包括 deny）均直接通过。
