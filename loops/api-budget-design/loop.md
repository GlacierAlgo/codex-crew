# API-budget design and comparison loop

## Purpose

本 loop 仅在用户明确选择 `api-budget-design`，或明确要求比较 `N=3/4/5/6` 四种
API-budget 设计时启用。它在一个 tmux window 中启动五个相互隔离的 Codex TUI：四个
designer 对同一个 system new-build 或 migration 请求独立给出设计，Coordinator 是唯一
user-facing communication thread，负责多轮派发、机械校验、比较、metrics 与用户下一步。

designer 的唯一实验变量是各 role profile 固定的数值 budget `N`：恰好 `N` 个 deep
modules，且恰好 `N` 个顶层 public APIs。五个 role 使用相同 `gpt-5.6-sol` model、
`high` reasoning effort 与 Fast service tier。

## Runtime authority

- schema v2 `manifest.toml` 是 ordered roles、exactly one `communication_role`、runtime
  profiles、model、reasoning effort、service tier 与五列 `even-horizontal` layout 的唯一
  runtime authority。role order 固定为 `coordinator, designer_3, designer_4, designer_5,
  designer_6`，communication role 是 `coordinator`。
- `coordinator.md` 与四份 `designer_*.md` 是唯一 editable instruction sources；generated
  profile TOML 只是派生 adapter。
- 四个 designer native `thread_id` 彼此独立，不读取其他 designer 的上下文或输出。
  Coordinator 使用自己的 exact thread，接收用户 request 并控制四个 sub-threads。
- tmux 只提供五列等宽可视布局；dispatch、completion 与 final 都只使用 explicit Unix
  endpoint、exact native `thread_id` 和 exact `turn_id`。

## Shared experiment contract

- Coordinator 将用户原始 design request byte-identical 地只发送给四个 designer exact
  threads；四次 request bytes 必须一致，request 中不要另行注入 budget 或 runtime handoff。
- 每个 designer 必须同时产出恰好 `N` 个 counted deep modules 与恰好 `N` 个顶层
  public APIs。任一 count 不匹配的 final 均不合规，不得进入方案比较。
- 设计减少 API 暴露；每个 public API 都必须有具体 caller 和稳定 contract，不创建
  convenience alias 或 filler API。
- module 内避免多余 `abstract class`、abstract function 和只有一个实现的 abstraction。
- 不复用的逻辑不独立成 function。
- subsystem 局部语义优先在相邻上下行顺序表达，减少为控制流而进行的函数跳转，使未来
  agent developer 可以沿主要路径自上而下阅读。
- 所有 user-facing design output 必须使用中文句法；comparison output 遵循相同要求。
  technical identifier、API、module、contract、CLI、schema、file path、error text 与标准
  technical term 保留 English 原名，并自然混排在中文句法中。不要附加逐段或整篇
  English translation。
- 五个 role 均不修改 target worktree。

## Designer output contract

每份设计严格按相同 headings 输出：

1. `Assumptions`
2. `Module map / Deep modules (exactly N)`：恰好 `N` 个编号 counted modules；逐项给出
   responsibility、simple interface、hidden complexity 与无环依赖方向
3. `Public APIs (exactly N)`：恰好 `N` 个编号 contracts；逐项给出 caller、input、
   output、failures 与 side effects
4. `Main sequential flows`：用主要 new-build/runtime/migration 场景展示自上而下的顺序路径
5. `New-build path` 或 `Migration path`：按请求类型给出最小交付或迁移顺序与兼容边界
6. `Discarded abstractions and tradeoffs`
7. `Budget audit`：明确报告 `deep_modules=N/N` 与 `public_apis=N/N`，其中每个 `N` 替换为
   当前 role 的数值 budget

Supporting public schemas 归属于对应 API contract，不作为额外 public API。Internal helper
不进入 public API count；只有承载独立复杂责任的 module 才进入 deep-module count。

## Automatic handoff and Coordinator lifecycle

所有 identity bootstrap turns COMMIT 后，launcher 自动向 exact Coordinator thread 发送
runtime handoff，并等待 authoritative completed final 后才返回成功。handoff 是 cohort
membership/runtime control envelope，包含 loop/project/session/window/endpoint、communication
role 与 ordered role -> exact pane/thread/bootstrap-turn mappings；不得转发给 designers，
也不改变 native thread identity。Coordinator 保存 mappings 后，authoritative final 第一行
必须严格等于 `runtime_handoff=ready`；substring 或附加前缀/后缀均不合格。handoff 同时
提供 exact external `codex-crew crew close --window-id @N` command。

One-click startup 的 mapping 顺序为：

```text
coordinator -> COORDINATOR_THREAD
designer_3 -> THREAD_3
designer_4 -> THREAD_4
designer_5 -> THREAD_5
designer_6 -> THREAD_6
```

用户只与 Coordinator pane/thread 交互。收到一轮 design request 后，Coordinator：

1. 记录 round wall start；读取四个 target threads 的 status、latest cumulative token
   baseline 与 native goal，并设置明确 goal。用户未给 token budget 时不得臆造。
2. 通过 existing exact `crew send`/`wait`/`final` 把 byte-identical request 分发给四个
   designers；active turn 不重复 send，所有操作保留 exact `turn_id` precondition。
3. 收齐四份 authoritative final `agentMessage`，机械计算 exact module/API counts 与 audit；
   mismatch proposal 标记 noncompliant，不补写或修复，只比较合规方案并 recommendation。
4. 读取每个 thread latest cumulative observation，以 latest minus baseline 计算 round token
   delta；报告 model breakdown 与 total，`cachedInputTokens` 作为 input subset 不重复相加。
5. 独立报告 goal `status`、`tokensUsed`、可选 `tokenBudget`、`timeUsedSeconds`，以及
   Coordinator-observed round wall elapsed；goal-visible tokens 与 model breakdown 不混加。
6. 汇总 comparison/acceptance state、retries、remaining blockers，并询问用户继续、补充/
   修正，还是结束并回收。未收到用户下一步前不得开启新一轮。

同一 Coordinator thread 跨轮持续存在。用户选择结束并回收时，Coordinator 只在当前
final 输出 handoff 中的 exact external close command；不得在自己的 active turn 同步执行。
用户等待 final 后从 crew window 外另一 shell 执行。close archive threads 可恢复，且保留
shared tmux session/App Server。Completion authority 是 `turn/completed`，final authority
是 final-phase `agentMessage`；不新增 fanout、batch、aggregator API 或 binding database。
