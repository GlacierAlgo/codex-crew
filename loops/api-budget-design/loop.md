# API-budget design loop

## Purpose

本 loop 仅在用户明确选择 `api-budget-design`，或明确要求比较 `N=3/4/5/6` 四种
API-budget 设计时启用。它在一个 tmux window 中启动四个相互隔离的 Codex TUI；四个
role 对同一个 system new-build 或 migration 请求独立给出可比较设计。

唯一实验变量是各 role profile 固定的数值 budget `N`：最多 `N` 个 deep modules，且
恰好 `N` 个顶层 public APIs。四个 role 使用相同 `gpt-5.6-sol` model、`high` reasoning
effort、Fast service tier、共同约束、输入和输出合同。

## Runtime authority

- `manifest.toml` 是 ordered roles、runtime profiles、model、reasoning effort、
  service tier 与四列 `even-horizontal` layout 的唯一 runtime authority。
- `designer_3.md`、`designer_4.md`、`designer_5.md`、`designer_6.md` 是各 role 的唯一
  editable instruction source；generated profile TOML 只是派生 adapter。
- 四个 native `thread_id` 彼此独立。role 不读取其他 role 的上下文或输出，也不协作或
  聚合结果。
- tmux 只提供四列等宽可视布局；dispatch、completion 与 final 都只使用 explicit Unix
  endpoint、exact native `thread_id` 和 exact `turn_id`。

## Shared experiment contract

- Operator 将同一个未修改的 UTF-8 `--message-file` 分别发送给四个 exact threads；
  request bytes 必须一致，request 中不要另行注入 budget。
- 设计减少 API 暴露；每个 public API 都必须有具体 caller 和稳定 contract，不创建
  convenience alias 或 filler API。
- module 内避免多余 `abstract class`、abstract function 和只有一个实现的 abstraction。
- 不复用的逻辑不独立成 function。
- subsystem 局部语义优先在相邻上下行顺序表达，减少为控制流而进行的函数跳转，使未来
  agent developer 可以沿主要路径自上而下阅读。
- 所有 user-facing design output 必须使用中文句法；technical identifier、API、module、contract、CLI、schema、file path、error text 与标准 technical term 保留 English 原名，并自然混排在中文句法中。
- 不要附加逐段或整篇 English translation。
- 四个 role 只产出设计，不修改 target worktree。

## Output contract

每份设计严格按相同 headings 输出：

1. `Assumptions`
2. `Module map / Deep modules (K <= N)`：列出每个 module 的责任、simple interface、
   hidden complexity 与无环依赖方向
3. `Public APIs (exactly N)`：恰好 `N` 个编号 contract；逐项给出 caller、input、output、
   failures 与 side effects
4. `Main sequential flows`：用主要 new-build/runtime/migration 场景展示自上而下的顺序路径
5. `New-build path` 或 `Migration path`：按请求类型给出最小交付或迁移顺序与兼容边界
6. `Discarded abstractions and tradeoffs`
7. `Budget audit`：明确报告 `deep_modules=K/N` 与 `public_apis=N/N`

Supporting public schemas 归属于对应 API contract，不作为额外 public API。Internal helper
不进入 public API count；只有承载独立复杂责任的 module 才进入 deep-module count。

## Native dispatch

One-click startup 返回 shared endpoint 和以下 exact mappings：

```text
designer_3 -> THREAD_3
designer_4 -> THREAD_4
designer_5 -> THREAD_5
designer_6 -> THREAD_6
```

不要新增 fanout、batch 或 aggregator API。对同一个 `design-request.md`，分别调用现有
`crew send`，保存每次返回的 exact `turn_id`，再对每路独立调用 `crew wait` 与
`crew final`。Completion authority 是 `turn/completed`；设计文本 authority 是对应 turn
的 final-phase `agentMessage`。
