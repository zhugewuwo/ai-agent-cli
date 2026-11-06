# 第 32 章 循环护栏：doom-loop 检测

模型偶尔会陷入死循环（doom-loop），例如同一个 shell 命令重试 8 次、每次都是同样的引号错误。每次重试都把上一次失败的 stderr 重新写入上下文，几轮下来上下文和 token 预算就被消耗殆尽。循环护栏（Loop Guard）是这种循环的安全网：检测到重复调用后，先注入合成提示让模型自救，自救失败再弹对话框把决策权交给用户。本章将分为如下几个部分进行介绍。

- 32.1 节解释 doom-loop 为什么会发生，以及为什么仅依赖提示词无法解决。
- 32.2 节描述实战中常见的循环场景和共同特征。
- 32.3 节讲解检测算法：SHA-256 指纹、stable-stringify 和 8 槽位滑动窗口。
- 32.4 节讨论软阈值（3 次）和硬阈值（5 次）的双层阻断设计。
- 32.5 节说明检测窗口大小的选择依据和误报规避策略。
- 32.6 节分析循环护栏与 token 经济学和系统提示词的关系。
- 32.7 节给出本章小结。

## 32.1 doom-loop 为什么会发生

模型似乎应该自己意识到错了，但实际上大语言模型（Large Language Model）经常陷入循环。这是几个机制叠加的结果。

（1）自回归推理（Autoregressive Inference）。大语言模型每个 token 的生成都依赖前面所有 token，prompt 里反复出现「调 X 失败 → 再调 X」会在概率分布上强化这个 pattern，模型倾向于继续按这个 pattern 走。

（2）缺乏元认知（Meta-cognition）。模型不知道「我前几轮做过同样的事」。每一轮在模型看来都是全新的，prompt 里的失败历史只是上下文信息，不是行为告警。

（3）错误反馈不够明确。失败的 stderr 经常是 `command not found` 这种通用错误，模型从中推不出「应该完全换思路」。

（4）低温度（Low Temperature）导致采样退化。Agent 场景通常使用低 temperature（0-0.3）保证确定性，但低温度也意味着模型几乎总是采样概率最高的 token。一旦「调 X」的概率在分布里最高，就会持续采样同一结果。

这四者叠加构成了大语言模型在 Agent 场景下的固有失败模式，不是 bug 而是当前模型架构的结构性限制。仅靠系统提示词提醒「不要重复」无法可靠地阻止循环，因此需要外部循环护栏作为强制约束：由运行系统强制识别「同一动作重复 N 次」，注入合成工具结果打破循环。这种「模型行为由外部规则约束」的思路在 Agent 系统里很常见，Tree of Thoughts、Reflexion 等框架也在做类似的事情。

形态上，循环护栏很像分布式系统里的断路器（Circuit Breaker，Hystrix / Resilience4j 等库的核心模式）。相同点是都用「同一调用连续失败 N 次就跳闸」的策略保护下游，防止失败请求不断堆积。不同点是断路器跳闸后通常进入 open 状态拒绝所有请求，等待一段冷却时间后才半开试探；循环护栏没有这种基于时间的「半开试探」，而是按重复次数分软、硬双阈值。3 次只是软阻断（注入提示让模型换思路，不终止整轮），5 次才弹对话框把决策权交给用户。双阈值更适合 Agent 场景，因为模型有概率自己跳出循环，没必要一次失败就强制终止。<!-- analogy: loop-guard-circuit-breaker -->

## 32.2 死循环的典型场景

实战中遇到的循环模式有以下几类：模型对一个 shell 命令的引号格式不熟，尝试后失败，再以完全相同的命令重试（PowerShell 上特别常见）；模型尝试 edit 但 `oldString` 不存在，重试同样的 `oldString`；模型对 grep 的匹配模式写错，重试同样的模式；模型生成的代码有语法错误，反复构建失败但每次「修改」都没有真正改动关键部分。这些场景的共同特征是同一 `toolName` + 同一 `input` 反复出现。如果不阻断，每轮都会浪费 10k+ token（执行命令 + 失败输出 + 模型重读），加速上下文压缩的触发，而且永远拿不到正确答案，因为错误模式没有改变，结果也不会改变。

## 32.3 检测算法：指纹与滑动窗口

> 源码：`packages/core/src/agent/loop-guard.ts`

整体思路一句话概括：给每次工具调用算一个「指纹」，追加到一个滑动窗口；判定时只看窗口内最近 8 次调用的指纹，统计当前指纹出现了多少次。两个核心概念先讲清楚。指纹等于 `SHA-256(toolName + 标准化 input)` 截到 16 个十六进制字符，同一工具加同一参数产生同一指纹。检测窗口取 `state.recentToolCalls` 数组末尾的 N 个条目（N = `LOOP_WINDOW_SIZE` = 8），只有最近 8 次的工具调用参与判定，更早的不算。下面分三步讲解实现。

第一步是计算指纹。每次工具调用进入 `processToolCalls` 时调用 `hashToolCall`：

```ts
export function hashToolCall(toolName: string, input: unknown): string {
  const payload = toolName + '\x00' + stableStringify(input)
  return crypto.createHash('sha256').update(payload).digest('hex').slice(0, 16)
}
```

用 `\x00` 而不是 `:` 作分隔符，是因为 JSON 字符串里不会出现 `\x00`，避免 `toolName` 或 `input` 里恰好有 `:` 时产生伪碰撞。截到 16 个十六进制字符在 8 槽位窗口里碰撞概率几乎可以忽略。

第二步是维护窗口。指纹算完后推进 `state.recentToolCalls`。这个数组的物理上限是 `LOOP_WINDOW_SIZE * 2 = 16`，超过就裁剪最老的条目：

```ts
state.recentToolCalls.push({ toolName, hash })
const cap = LOOP_WINDOW_SIZE * 2
if (state.recentToolCalls.length > cap) {
  state.recentToolCalls.splice(0, state.recentToolCalls.length - cap)
}
```

为什么数组容量留 16 个条目、检测窗口只看最近 8 个？检测窗口固定 8 个槽位，判定时取末尾 8 个条目、统计当前指纹出现的次数，更早的指纹不参与判定。数组容量留双倍是把刚滑出窗口的 8 个 entry 暂时保留。

第三步是统计同指纹出现次数。判定时取 `state.recentToolCalls` 的末尾 `LOOP_WINDOW_SIZE` 个条目：

```ts
const window = state.recentToolCalls.slice(-LOOP_WINDOW_SIZE)
const priorMatches = window.filter((e) => e.hash === currentHash).length
```

`priorMatches + 1`（加 1 是因为当前这次调用本身也算在内）落进哪个阈值就触发哪种动作（详见 32.4 节）。

指纹计算有一个关键前提：同一 input 必须 hash 出同一字符串。`JSON.stringify({a:1, b:2})` 与 `JSON.stringify({b:2, a:1})` 输出不同，直接使用会导致同样的输入因键顺序不同而产生不同的 hash。因此 `hashToolCall` 内部使用 `stableStringify` 将键排序后再序列化：

<!-- check:ignore=code-source -->

```ts
function stableStringify(value: unknown): string {
  if (value === null || typeof value !== 'object') return JSON.stringify(value)
  if (Array.isArray(value)) return '[' + value.map(stableStringify).join(',') + ']'
  const entries = Object.entries(value as Record<string, unknown>)
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
  return '{' + entries.map(([k, v]) =>
    JSON.stringify(k) + ':' + stableStringify(v)
  ).join(',') + '}'
}
```


下面这张图展示了 8 格滑动窗口的心智模型，窗口里同 hash 重复出现 3 次触发软阻断、5 次触发硬退出：

<!-- check:ignore=code-block-size -->

```text
recentToolCalls 滑动窗口（LOOP_WINDOW_SIZE = 8）：

最早                                         最新
 ┌────┬────┬────┬────┬────┬────┬────┬────┐
 │ A  │ B  │ A  │ C  │ A  │ D  │ A  │ A  │   ← 数 hash=A 出现几次
 └────┴────┴────┴────┴────┴────┴────┴────┘
   1         2         3         4    5
                       ▲              ▲
                       │              │
                  软阈值 3          硬阈值 5
                  注入合成结果       弹 askUser 对话框
                  "换个思路"         由用户决定 Pause / Continue
```

每来一个新工具调用就从右边推进窗口、左边滑出最老的一个；判定逻辑只看尾部 8 格里和当前 hash 相同的格子数。中间夹了 B / C / D 这些不同 hash 不影响计数，只有完全相同的 `toolName` + `input` 才算同指纹。

## 32.4 双阈值阻断：软阻断与硬退出

循环护栏采用双阈值设计，`SOFT_LOOP_THRESHOLD = 3` 和 `HARD_LOOP_THRESHOLD = 5`，两者的处理方式不同。判定时先检查硬阈值（`priorMatches + 1 >= 5`），再检查软阈值（`priorMatches + 1 >= 3`），因此 5 次及以上直接走硬退出路径，不会先触发一次软阻断。

同 hash 在窗口内出现 3 次时触发软阻断。此时该工具调用不会真正执行，循环护栏直接注入一条合成的 `tool_result` 返回给模型：

<!-- check:ignore=code-source -->
<!-- check:ignore=punctuation -->

```ts
// soft-block 返回的 message（priorMatches + 1 = 窗口内同 hash 次数 + 本次）
`This exact ${toolName} call (same arguments) has already been attempted `
+ `${priorMatches + 1} times this session with the same result. `
+ 'DO NOT retry it. Change your approach — alter the arguments meaningfully, '
+ 'try a different tool, or ask the user what to do instead.'
```

这条消息的措辞是命令式的（「DO NOT retry it」），目的是在概率分布上压制「继续重试」这个 pattern，给模型一次自己跳出循环的机会。多数死循环在这一步被打断。

如果软阻断之后模型仍然重试，同 hash 达到 5 次时触发硬退出。硬退出不是直接中断整个轮次，而是分两步处理：首先注入一条合成 `tool_result`（与软阻断一样，该工具调用不会真正执行），然后弹一个 `askUser` 对话框让用户在 Pause 和 Continue 之间选择。

<!-- check:ignore=code-source -->

```ts
const answer = await callbacks.onAskUser(
  `The model keeps calling ${toolName} with identical arguments. `
  + 'How do you want to proceed?',
  [
    { label: 'Pause',    description: 'Pause the turn — you can type a new instruction.' },
    { label: 'Continue', description: 'Let the model keep trying; the loop guard stays armed.' },
  ],
)

if (answer.toLowerCase().startsWith('pause')) {
  state.recentToolCalls = []
  deferred.push({
    role: 'user',
    content: '[loop-guard] User paused the loop. '
      + 'Wait for further instructions rather than calling more tools.',
  })
}
```

Pause 和 Continue 两条路径的设计有明确的意图区分。Pause 清空滑动窗口，这是为了让用户用自然语言重新指挥后，循环护栏不会「还记着之前那 5 次」立刻再次触发。同时注入一条带 `[loop-guard]` 前缀的 user 消息，告诉模型等待新指令而不是继续调用工具。这条消息会等到本轮所有 `tool_result` 都处理完毕后再追加到消息历史，避免破坏工具调用和结果的配对关系。Continue 不清空窗口，用户明确放行，但循环护栏保持武装状态，再来同样的调用还会再次触发阈值检查。

为什么不在 5 次时直接终止轮次，而要弹对话框让用户选择？原因在于循环护栏无法确定当前行为一定是错误的。有些场景下模型确实需要对同一工具反复调用（例如用户明确要求「再跑一次同样的命令」），硬终止会误伤这类合法操作。弹对话框让用户看到原因后自行判断，是精度和体验之间的折中。

## 32.5 窗口大小与误报规避

`LOOP_WINDOW_SIZE = 8` 是经验值，权衡两个方向。窗口太小（如 4）会检测不到中间夹了一两次成功调用的循环，例如「调 A 失败 → 调 B 成功 → 又调 A 失败 → 调 C 成功 → 又调 A 失败」，A 实际重复了 3 次但每次中间都隔了一次不同调用。窗口太大（如 16）则会把很久之前的同样调用误判为循环，例如用户主动让模型重试一次旧操作。8 覆盖大部分实战场景：3 次重复加中间几次正常调用、再加软阻断后重试 1-2 次到硬退出，大约就是 8 个槽位。

循环护栏的检测精度建立在「同 hash = 同 toolName + 同 input」这个判定标准上，因此几类正常场景不会触发。模型调同一工具但参数不同（比如对不同路径的 `readFile`）产生不同 hash，不触发。模型先失败、调整参数后再试（如先 grep 一个模式失败，改模式再 grep）同样是不同 hash，不触发。用户主动让模型重试同一命令时确实会触发，但用户能从硬退出对话框中看到原因，主动决定下一步。只有「输入完全相同的反复重试」才被判定为死循环。

## 32.6 与 token 经济学和系统提示词的关系

Ch.30 将循环护栏列为四层 token 防御的第二层。循环护栏的核心价值不是省一两轮的 token，而是在错误模式没有改变时早一步停下来让用户介入——错误模式不改，重试再多次也拿不到正确答案。被循环护栏阻断后留下的残骸（`[loop-guard]` 开头的合成 tool-result）会在 Ch.31 的轻量压缩中被清理掉。

系统提示词里没有特别提及循环护栏，模型不知道有这个机制存在。遇到软阻断时，模型按合成 `tool_result` 里的提示自然换思路；遇到硬退出时，按对话框结果处理。这种「对模型不可见」的容错机制是有意为之。模型应该通过训练数据和提示词引导学会不重复，而不是依赖外部机制告知；把循环护栏写进系统提示词反而增加每轮的 token 消耗（每轮重发）；而且模型对外部机制的认知不可靠，告知了也不一定真的改变行为。让循环护栏在后台默默运行，模型只看到结果就足够了。

## 32.7 小结

本章讨论了 X-Code CLI 的循环护栏机制。死循环是大语言模型的固有失败模式，自回归推理、缺乏元认知、错误反馈不够明确、低温度退化采样四者叠加，仅靠提示词无法可靠阻止。检测核心是 SHA-256 指纹加 8 槽位滑动窗口，双阈值设计给模型自救空间：软阈值（3 次）注入合成 tool-result 提示换思路，硬阈值（5 次）弹对话框把决策权交给用户。循环护栏不写进系统提示词，模型只看到结果，避免每轮重发增加 token 消耗。
