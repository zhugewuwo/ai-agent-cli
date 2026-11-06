# 第 34 章 思考模式：跨供应商的统一开关

思考模式（Thinking Mode）让模型在回答前先生成一段内部推理（reasoning tokens），复杂任务的质量因此显著提升，代价是额外的 token 消耗和延迟。问题在于 8 家供应商对这一能力的命名、参数格式和默认行为各不相同：Anthropic 用 `thinking: { type: 'enabled', budgetTokens }`，Google 用 `thinkingConfig: { thinkingBudget }`，OpenAI 用 `reasoningEffort`，Alibaba 用 `enableThinking`。Gemini 2.5 Pro 默认开启思考，Claude Sonnet 4.6 默认关闭，DeepSeek V4 也默认关闭。如果让用户直接管理这些原生参数，每次 `/model` 切换供应商都需要重新查阅对应文档才能确认当前状态。X-Code CLI 的做法是把所有差异压平到一个 `/thinking on|off` 开关里，本章讲这层抽象的设计决策与实现。本章将分为如下几个部分进行介绍。

- 34.1 节通过一个跨供应商切换的场景说明统一开关的动机，并补充思考模式的技术背景。
- 34.2 节讲解单一布尔接口的设计：如何把 6 种参数命名和 3 种默认行为压平到 on/off 两档。
- 34.3 节讨论透传容错策略：为什么不按模型维护支持表，以及整个供应商不支持时如何处理。
- 34.4 节分析 `/thinking off` 的局限性：哪些模型在物理上无法关闭思考，以及 `/thinking` 命令的运行时机制。
- 34.5 节给出本章小结。

## 34.1 为什么需要统一开关

8 家供应商对思考模式的默认行为各不相同：Gemini 2.5 Pro 默认开启，DeepSeek V4 和 Claude Sonnet 4.6 默认关闭。当用户用 `/model` 在供应商之间切换时，思考状态会随目标供应商的默认值静默改变，而 CLI 没有任何信号提示这一变化。例如用户刚才在 Gemini 上开着思考模式完成了一轮复杂规划，接着切到 DeepSeek 继续工作，思考模式就在用户不知情的情况下从开变成了关。这就是 `/thinking on|off` 存在的动机：把各家不同的默认行为压平成一个跨供应商一致的开关。用户开启 thinking 后切换供应商，思考模式的语义保持不变，不需要重新设置。

> AI 知识背景：思考模式的来历
>
> 2022 年 Wei 等人发表 [Chain-of-Thought Prompting](https://arxiv.org/abs/2201.11903)（思维链）论文，发现在 prompt 里加一句 `Let's think step by step`，模型在数学和逻辑题上的准确率显著提升，多步推理任务上能从个位数提升到几十个百分点。原因是大模型本身具备推理能力，但默认不会主动写出中间步骤，引导一下即可。2024 年 9 月 OpenAI 发布 [o1 模型](https://openai.com/index/learning-to-reason-with-llms/)，第一个把思维链训练进模型本体的生产级 LLM：通过强化学习教模型在回答前先生成一段长思考（reasoning tokens），思考越长，复杂问题的答案越准。这段思考默认对用户隐藏，只暴露最终答案。后续 o3、DeepSeek-R1、Claude extended thinking、Gemini 2.5 Pro thinking budget、Kimi K2.5、Qwen3 都跟进了「推理模型」范式。核心权衡在于：思考会消耗大量 reasoning token（动辄几千到几万），但换来复杂任务的质量提升，因此 thinking 不是免费午餐，简单查询关闭可以节省 token，复杂规划开启可以提升质量。
>
> 一个直觉化的类比：reasoning token 像模型自己的草稿纸，答题前先在草稿上演算中间步骤，最后只把答案抄到答题卡上交。相同点是中间过程不影响最终答案的形式，但能显著提升复杂题的准确率；不同点是这张草稿对用户默认隐藏，且按 token 单独计费，简单任务开启反而增加不必要的开销。<!-- analogy: scratchpad -->

## 34.2 单一布尔接口：不暴露各家原生参数

8 家供应商加起来有 6 种命名风格和 3 种默认行为，下表列出了各家的参数差异：

<!-- check:ignore=punctuation -->

| 供应商       | 开                                                            | 关                                      |
| ------------ | ------------------------------------------------------------- | --------------------------------------- |
| anthropic    | `thinking: { type: 'enabled', budgetTokens }`                 | `thinking: { type: 'disabled' }`        |
| google       | `thinkingConfig: { thinkingBudget: -1 }`（动态）              | `thinkingConfig: { thinkingBudget: 0 }` |
| openai       | `reasoningEffort: 'high'`（仅 o-series 与 `gpt-5` reasoning） | `reasoningEffort: 'minimal'`            |
| xai          | `reasoningEffort: 'high'`（仅 `grok-3-mini` / `grok-4-mini`） | `reasoningEffort: 'low'`                |
| deepseek     | `thinking: { type: 'enabled' }`（仅 v4）                      | `thinking: { type: 'disabled' }`        |
| moonshotai   | `thinking: { type: 'enabled' }`                               | `thinking: { type: 'disabled' }`        |
| alibaba      | `enableThinking: true`                                        | `enableThinking: false`                 |
| zhipu/custom | （无对应参数，原样发送）                                      | （无对应参数，原样发送）                |

默认值的差异同样显著。Gemini 2.5 Pro 和 Kimi K2.5 默认开启思考，Claude Sonnet 4.6、DeepSeek V4、Qwen Max 默认关闭，用户必须显式传入思考参数才能启用；GPT-4.1、Grok 3、GLM-4-Plus 则完全不支持思考模式。如果让用户直接管理原生参数，每次 `/model` 切换都需要重新查阅对应供应商的文档，而用户不可能记住 8 家的默认值。因此 X-Code CLI 在这一层做了统一：接口只有 `/thinking on` 和 `/thinking off` 两档，把 6 种命名和 3 种默认值压平到一个语义统一的开关。on 状态取每家供应商的最高档：Anthropic 的思考预算设为 8000 token（源码注释标注为经验值，对大多数复杂推理任务足够，同时不会占用过多上下文窗口），Google 使用动态预算（由服务端自行决定思考长度），OpenAI 和 xAI 使用最高推理档位。中间档不暴露给用户。

这样做的代价是失去精细控制，例如 Anthropic 用户想精确指定 `budgetTokens = 16000` 无法通过 CLI 完成，必须修改源码。但在典型使用场景中，「开或关」已经覆盖绝大多数需求，中间档带来的收益不足以抵消接口复杂度的增加。

## 34.3 透传容错：不维护 per-model 支持表

实现这个映射函数时会遇到一个现实问题：同一供应商内部不同模型的支持度不一样。`openai:gpt-4.1` 不支持 reasoning，`openai:o3` 和 `openai:gpt-5` 支持；`xai:grok-3` 不支持，`xai:grok-3-mini` 和 `xai:grok-4-mini` 支持；`deepseek:deepseek-chat` 不支持，`deepseek:deepseek-v4` 支持。也可以给函数加一份「每个供应商下哪些模型支持哪些字段」的白名单，在 case 内按 modelId 二次判断，支持的模型才传字段，不支持的跳过。但白名单有三方面代价。第一，白名单需要随供应商发布新模型而持续更新，三个月不维护就会过期。第二，白名单里没有的新模型该如何处理本身就是一个两难选择：默认开启可能把不支持的字段发送出去，默认关闭又可能漏掉真正支持思考的新模型。第三，每次合并代码都需要先查阅供应商文档来更新表格，维护成本与模型数量成正比。

X-Code CLI 的选择是不维护这张表。同一个供应商家族下，无论传入什么 modelId 都返回同一个字段：

> 源码：`packages/core/src/providers/thinking.ts`

<!-- check:ignore=punctuation -->

```ts
case 'xai':
  // 只有 grok-3-mini / grok-4-mini honour reasoningEffort；grok-3 / grok-4
  // 收到这个字段会被 API 静默丢弃 ── 透传是安全的
  return enabled ? { xai: { reasoningEffort: 'high' } } : { xai: { reasoningEffort: 'low' } }

case 'openai':
  // 只有 o-series 和 gpt-5 用 reasoningEffort；gpt-4.1 同样静默忽略
  return enabled ? { openai: { reasoningEffort: 'high' } } : { openai: { reasoningEffort: 'minimal' } }
```

代码注释把这个设计契约写得很直白：Sending the option is harmless on the ignoring models — the SDK passes it through and the API silently discards it. 也就是说，「哪些模型支持哪些字段」的判断整体外包给了供应商的 API。CLI 层只按供应商维度做统一处理：同一供应商下的所有模型都收到相同的思考参数，不支持的模型由 API 端识别并静默丢弃。这样换回了三个收益。第一，不需要维护白名单，供应商端「未知字段忽略」是 LLM API 普遍的向后兼容契约，CLI 直接借用这个契约。第二，新模型无需改动代码，新发布的支持模型不改代码就能立即启用思考，新发布的不支持模型也不用改代码避错。第三，设计契约明确，源码注释写明透传是有意为之，维护者不会误以为是遗漏白名单的 bug。

还有另一种情况：某些供应商目前完全不支持思考模式，例如 zhipu 当前的模型和 custom 兼容入口。X-Code CLI 的处理方式与上面的透传容错是同一个理念：CLI 层直接返回空对象，SDK 收到后等同于没传这个参数，不会报错。新增供应商的代价仅仅是 switch 多一个 case，调用方无需改动。用户在不支持的供应商上开着 thinking，`/model` 切到支持的供应商后自动生效，不需要先关再开。

两层容错合起来形成一套连贯策略：

<!-- check:ignore=punctuation -->

| 不支持的情况           | 处理层 | 机制                                  |
| ---------------------- | ------ | ------------------------------------- |
| 供应商内部分模型不支持 | API    | 字段照传，API 静默丢弃               |
| 整个供应商不支持       | CLI    | 返回 `{}`，SDK 不传字段              |


下面的示例把两层容错放在一起对比。`openai:o3` 和 `openai:gpt-4.1` 调用 `getThinkingProviderOptions` 返回值完全一样，差别全在 API 端：

<!-- check:ignore=punctuation -->

```ts
getThinkingProviderOptions('openai:o3', true)
// → { openai: { reasoningEffort: 'high' } }      ← 支持模型，API 正常生效

getThinkingProviderOptions('openai:gpt-4.1', true)
// → { openai: { reasoningEffort: 'high' } }      ← 不支持模型，字段照传；API 丢弃，无副作用
```

返回的对象最终会合并到发送请求时的供应商选项中。这里使用「合并」而不是「覆盖」，原因在于同一个供应商下可能还挂着缓存控制等其他字段，思考选项必须与之并列，不能把整个供应商配置替换掉。

## 34.4 关不掉的思考模式

34.2 节说 `on` 是「尽可能让模型思考」，对称地看，读者可能以为 `off` 是「保证不思考」。实际并非如此，供应商端有几个模型在物理上就无法关闭思考，X-Code CLI 这边发出的 `off` 对它们近似无效。

Gemini 2.5 Pro 有一个最低思考预算限制。源码 `providers/thinking.ts` 注释里的说明是：Gemini 2.5 Pro can't be fully turned off (min budget 128) — we still send `thinkingBudget: 0` for OFF and let the SDK clamp; 2.5 Flash and Lite respect 0 as "no thinking". CLI 层照常发 `thinkingBudget: 0`，但 SDK 会把它 clamp 到 128。Pro 的最小思考预算就是 128 token，永远比「完全不思考」多一点点开销。Flash 和 Lite 没有这个下限，传 0 即真正关闭。

Alibaba 的专用推理模型（dedicated reasoning Qwen）也存在类似的情况。源码注释的说明是：Hybrid Qwen ids honour `enableThinking` per-request; the dedicated reasoning ids (qwq-plus, qwen3-\*-thinking-\*) ignore an `enableThinking: false` request and keep thinking on. Alibaba 的模型分为两类：专用推理模型（如 `qwq-plus`、`qwen3-*-thinking-*`）和普通混合模型（如 `qwen-plus`、`qwen-max`）。专用推理模型的思考能力是通过强化学习训练到模型本体中的核心能力，关闭思考会让模型行为偏离训练分布，因此供应商端有意忽略关闭请求，模型照常进行推理。普通混合模型则支持按请求级别切换，关闭思考后模型确实不再生成 reasoning token。这意味着用户在 `qwq-plus` 上执行 `/thinking off` 后，模型仍然会消耗 reasoning token，CLI 层无法改变这一行为。需要严格避免思考开销时，应当通过 `/model` 切换到 `qwen-plus` 等普通混合模型。

`/thinking` 命令本身支持几种别名形式：

（1）无参数：弹出 `SelectOptions` 选择器，显示当前状态，方向键切换。

（2）开启：`/thinking on`、`/thinking true`、`/thinking enable`。

（3）关闭：`/thinking off`、`/thinking false`、`/thinking disable`。

设置持久化到用户级配置文件。运行时切换不需要销毁和重新创建供应商连接，组件通过 React 的 `useRef` 持有当前开关值，每次发请求时实时读取最新状态。这与 Ch.28 权限对话框中的做法一致：用 state 驱动界面重渲染，用 ref 在异步回调中同步访问最新值。

## 34.5 小结

本章讨论了 X-Code CLI 的思考模式统一开关。8 家供应商的 6 种参数命名和 3 种默认行为被压平到一个 `/thinking on|off` 布尔开关，on 取每家最高档，不暴露中间档。容错策略分两层：供应商内部分模型不支持时，参数照传由 API 静默丢弃，不维护按模型区分的白名单；整个供应商不支持时，CLI 层返回空对象，等同于不传参数。`/thinking off` 是尽力而为的信号而非关闭保证——Gemini 2.5 Pro 有 128 token 的最低思考预算，Alibaba 的专用推理模型直接忽略关闭请求。需要严格避免思考开销时，应当通过 `/model` 切换到不强制思考的模型。
