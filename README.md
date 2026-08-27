# codex-crew

`codex-crew` 按 repository-owned loop manifest 在一个 tmux window 中启动多个独立
Codex TUI。tmux 只提供 manifest-defined 等宽可视布局；所有派发、等待、final 与 goal
操作都直接使用 App Server native `thread_id`。默认 `three-agent-dev` 仍启动
Commander、Worker、Judger 三列。

## Global ergonomic entrypoint

以 editable tool 安装 repository-owned CLI（把示例路径替换为本 checkout 的绝对路径）：

```bash
uv tool install --editable /absolute/path/to/codex-crew
```

全局启动 contract 是 `crew LOOP_ID [PROJECT_DIR]`：

```bash
crew three-agent-dev
crew api-budget-design /path/to/project --json
```

`PROJECT_DIR` 省略时使用命令 invocation cwd 的 resolved path；提供时显式覆盖它。
`--session`、`--window-name` 与 `--json` 直接传入现有 one-click startup。默认 tmux
window name 是 `crew-<loop-id>-<project-slug>`；显式 `--window-name` 优先。

`LOOP_ID` completion 每次从本 repository 的 `loops/` registry 动态读取，目录参数使用
shell path completion。持久启用 zsh completion 的一种方式是在 `~/.zshrc` 中加入：

```zsh
eval "$(crew --show-completion zsh)"
```

重新打开 shell，或 source 该配置后，`crew <TAB>` 会列出当前已注册的
`three-agent-dev` 与 `api-budget-design`。`--show-completion` 同时支持 `bash` 和 `fish`。
安装与 shell 配置由用户显式执行；startup command 本身不会修改 shell config。

## One-click startup

要求 Python 3.11+、`uv`、`tmux` 与已完成登录的 `codex`。在本 repository root 一次启动：

```bash
./bin/codex-crew up /path/to/project --json
```

`PROJECT_DIR` 省略时默认为当前目录；`--session` 默认是 `default`，`--loop` 默认是
`three-agent-dev`。现有 `codex-crew up` 保持兼容；全局 `crew LOOP_ID [PROJECT_DIR]`
调用同一个 `up_crew` transaction。startup 在一次调用内严格依次完成：

1. 从所选 schema v2 `loops/<loop-id>/manifest.toml` 加载 ordered roles、exactly one
   `communication_role` 与其他 runtime authority；
2. 按 manifest role count 幂等生成并检查 repo-derived profile adapters；
3. 复用 exact tmux session，只有不存在时才运行
   `tmux new-session -d -s SESSION -c PROJECT_DIR`；
4. 复用 ready 的 repo-owned App Server，否则 detached 启动并有界等待 readiness；
5. 调用 native launcher，创建一个 window、manifest-defined 等宽 panes，并等待全部
   identity bootstrap turns COMMIT；
6. 向 exact communication thread 自动发送 runtime handoff，并等待 authoritative
   completed final；
7. 只有 handoff 第一行 exact `runtime_handoff=ready` 后，才 atomic persist ignored lifecycle
   record 并返回 `CrewLaunch`。

任一 gate 失败都会非零退出，后续 gate 不会执行，也不会返回 partial launch JSON。
`up` 不会杀已有 tmux session/window，也不会为失败的 App Server readiness 杀未知 live
PID。

成功结果包含：

- `window_id`、`window_index` 与 `pane_mapping`，只用于打开和查看布局；
- `thread_mapping`，把所选 manifest 的每个 role 映射到精确 native `thread_id`；默认
  mapping keys 是 `commander`、`worker`、`judger`；
- 每个 pane 已 COMMIT 的 `bootstrap_turn_id` 与 marker；
- 全部 TUI 共用的 explicit repo Unix endpoint；
- `communication_role`、`communication_thread_id`、`communication_pane_id`，以及 completed
  `handoff_turn_id` / `handoff_status`；
- `lifecycle_record_path` 与 exact `close_command`。

## Loop package public surface

本文中的 **loop package** 是 repository-owned artifact：`loops/<loop-id>/` 目录及其中的
`manifest.toml`、`loop.md` 和 role instruction Markdown；`package` 不是 CLI subcommand，
因此不存在 `codex-crew loop package`。公开 CLI surface 只有以下三个 verbs：

```bash
# 发现 packages，并查看 roles、runtime profiles、communication_role 与 layout
./bin/codex-crew loop list
./bin/codex-crew loop list --json

# 从指定 package authority 安装 repo-derived profile adapters
./bin/codex-crew loop install api-budget-design

# 校验 package sources 与已安装 adapters 一致
./bin/codex-crew loop check api-budget-design
```

因此，“package/list/install/check”表示“canonical loop package artifact，以及用于发现、安装、
校验它的 `loop list`、`loop install LOOP_ID`、`loop check LOOP_ID`”，不是四个并列的 CLI
subcommands。`api-budget-design` 的 public role order、profiles、`communication_role` 与
`split-plan` contract 可由 `loop list --json` 直接观察；`install`/`check` 的成功输出则列出
同一 ordered role/profile mapping。

## One communication thread per loop

每个 manifest 必须且只能声明一个 `communication_role`。启动成功后，用户只与 JSON 中的
`communication_thread_id` 对应 TUI（也就是 `communication_pane_id`）交互；其他 roles 是
由它控制的 sub-threads，不直接承担用户沟通。两个当前 loop 都使用 Commander。

Automatic runtime handoff 在 launch 返回前完成。它包含 loop/project/session、exact
window metadata、explicit endpoint 与 ordered role -> exact pane/thread/bootstrap-turn
mappings，只是 cohort membership/runtime control envelope；不改变 native identity，也不
转发给 sub-threads。communication role 保存该 envelope 后，其 authoritative final 第一行
必须严格等于 `runtime_handoff=ready`；substring、附加前缀或后缀均不合格。wrong 或 missing
readiness marker 都使 launch fail closed、非零退出并保留 window 诊断，不返回 partial
success。

同一 communication thread 跨轮持续存在。每轮它：

1. 收到用户 task/request 后，先读取目标 threads 的 authoritative status 与 native goal，
   设置明确 round goal；用户未提供 token budget 时不创建 budget；
2. 使用 explicit endpoint + exact `thread_id` 和 `crew send`/`wait`/`final` 派发并收集，
   active turn 不重复 send；
3. 每个 target 完成后读取 native goal；必须报告 goal `status`、`tokensUsed`、可选
   `tokenBudget`、`timeUsedSeconds`，以及 communication role 观察到的 round wall elapsed；
4. model token observation 是 optional：仅当对应 exact `crew wait` 返回 `token_usage` 时，
   才将其标为该 wait 实际观察到的 cumulative value；缺失必须披露但不阻塞 round。不得将
   未观察 baseline 当作 zero，也不得虚构 round delta 或 model total。展示 optional breakdown
   时，`cachedInputTokens` 是 input subset，不重复相加；goal-visible tokens 与 model
   observation 不混加；
5. 汇总结果、验收/比较状态、retries 与 remaining blockers，然后询问用户继续、补充/
   修正，还是结束并回收。未收到用户下一步前不得开始新一轮。

## Recoverable crew teardown

用户选择结束并回收时，communication role 只在自己的 authoritative final 中显示 handoff
提供的 exact external command，不在当前 active turn 内同步执行：

```bash
codex-crew crew close --window-id @7
```

必须等待该 final 完成，再从 crew window 之外的另一 shell 执行。`--window-id` 只接受
exact tmux ID；CLI 只读取 repository-owned `.codex-crew/runtime/crew-lifecycle/` 下对应的
managed lifecycle schema v2 record。Record 是 teardown ownership manifest，不是 thread locator 或 binding
database；`send`、`goal`、`wait`、`final` 等普通 control APIs 不接受 record path/window ID。

Close transaction 按以下顺序 fail closed：

1. 读取并验证 exact record；对所有尚未归档 threads 做 read-only active preflight。任一
   active/running turn 会在任何 teardown mutation 前阻止 close，并报告 role/thread/turn；
   close 不 interrupt。
2. Window reclaim 使用 `pending → started → complete` phase。首次执行先验证 live exact
   window 的 session、window ID、name/index 与完整 pane ID set，再在 destructive kill 前
   atomic checkpoint `started`，只执行 `tmux kill-window -t @N`，成功后 checkpoint
   `complete`。不会 kill tmux session、App Server 或其他 windows。
3. Retry 看到 `started` 时：若 exact window 仍存在，必须重新验证 exact metadata/panes 后
   才能重新 kill；若 tmux 明确报告该 exact window 不存在，reconcile 为 `complete`。一般
   inspection error、错误 window metadata 或 pane mismatch 绝不当作 absent。
4. 通过 record 内 explicit endpoint + exact native thread IDs，以 sub-threads first、
   communication role last 调用 `thread/archive`；每成功一个就 atomic checkpoint。
5. 中途失败保留 record/progress 并列出 remaining roles。Retry 跳过已完成 window/archive
   stages；若 archive 已成功但 checkpoint 未落盘，则用 archived listing evidence reconcile。
6. 全部 threads archived 后才删除 managed record，并输出 reclaimed window 与 exact
   role/thread mapping。

默认语义只有可恢复 archive，没有永久 delete flag；shared tmux session 与 App Server 保留。
OpenAI Docs 说明 built-in `/archive` 只归档当前 session 并退出 TUI，transcript 仍保留，
也可用 `codex archive <SESSION>` / `codex unarchive <SESSION>` 管理 saved session；这些单
session commands 不能替代 cohort teardown。[Codex developer commands](https://developers.openai.com/codex/cli/slash-commands)
同时说明 `/delete` / `codex delete` 永久删除 transcript，本项目不使用。

OpenAI 当前已将 [custom prompts](https://developers.openai.com/codex/custom-prompts) 标记为
deprecated，并推荐 [skills](https://developers.openai.com/codex/skills)。Custom prompt 可形成
`/prompts:name`，但只会扩展并发送一条消息，不能成为 teardown authority；本项目不安装
该 prompt。若未来需要 inside-Codex ergonomic entrypoint，应实现 skill 且仅准备/展示
external close command，transaction 仍由 lifecycle CLI 执行。

## Four-way API-budget design with Commander

`api-budget-design` 是 optional loop，只在用户明确选择它，或明确要求比较 `N=3/4/5/6`
四种 API-budget 设计时使用。它用同一 `gpt-5.6-sol` model、`high` reasoning effort 与
Fast service tier 启动五个隔离 roles：`commander`、`worker_3`、`worker_4`、
`worker_5`、`worker_6`。Commander 是唯一 communication role；四个 Workers 是
sub-threads。Worker 的唯一实验变量是 `N`：恰好 `N` 个 counted deep modules，且恰好
`N` 个顶层 public APIs。

```bash
./bin/codex-crew up /path/to/project --loop api-budget-design --json
```

成功 JSON 的 `thread_mapping` 固定包含：

```text
commander -> exact native communication thread_id
worker_3 -> exact native thread_id for N=3
worker_4 -> exact native thread_id for N=4
worker_5 -> exact native thread_id for N=5
worker_6 -> exact native thread_id for N=6
```

manifest 的 ordered split plan 创建三列近似等宽的 `[1,2,2]` geometry：左列
`commander` full height；中列上/下为 `worker_3`/`worker_4`；右列上/下为
`worker_5`/`worker_6`，即“左1/中23/右45”。split plan 使用 target role、
`horizontal`/`vertical` direction 与 percentage 静态定义，不经过会重排 geometry 的
`select-layout`。

用户启动后只在 Commander pane/thread 输入原始 new-build 或 migration request。
Commander 自己把 byte-identical request 分发给四个 Worker threads，不注入 `N` 或
runtime handoff；operator 不再手工构造 comparison input。四个 Worker contexts/output
互相隔离。每份 final 使用相同
output contract：`Assumptions`、`Module map / Deep modules (exactly N)`、
`Public APIs (exactly N)`、
`Main sequential flows`、`New-build path` 或 `Migration path`、
`Discarded abstractions and tradeoffs`、`Budget audit`。最后一节必须报告
`deep_modules=N/N` 与 `public_apis=N/N`，并将每个 `N` 替换为当前 role 的数值 budget。
任一 module/API count 不匹配的 Worker final 均不合规，不得作为 recommendation
candidate。

Language contract：四个 Worker 的所有 user-facing design output 必须使用中文句法；
Commander 的 comparison output 遵循相同要求。
technical identifier、API、module、contract、CLI、schema、file path、error text 与标准
technical term 保留 English 原名，并自然混排在中文句法中。不要附加逐段或整篇
English translation。

Commander 收齐四份 authoritative final `agentMessage` 后，机械计算每份 final 的顶层
numbered module/API entries，并核对 numeric
`deep_modules=N/N` 与 `public_apis=N/N` audit；只比较合规方案。最终 comparison 清楚列出
每个合规 `N` 强制产生的结构决策、取舍、风险与 recommendation，不补写或修复 Worker
方案，也不修改 target worktree。它随后报告本轮 metrics 并等待用户下一步。

## Repository and runtime boundary

Canonical launcher、startup lifecycle、manifest、role instructions 与 profile generator 全部
位于 tracked repository：`bin/`、`codex_crew/`、`loops/`。运行时只产生以下 ignored
内容：

```text
<codex-crew-repo>/.codex-crew/generated/<loop-id>/*.config.toml
<codex-crew-repo>/.codex-crew/runtime/app-server.sock
<codex-crew-repo>/.codex-crew/runtime/app-server.pid
<codex-crew-repo>/.codex-crew/runtime/app-server.log
<codex-crew-repo>/.codex-crew/runtime/crew-lifecycle/window-<N>.json
```

`.gitignore` 覆盖整个 `.codex-crew/`。`$CODEX_HOME` 继续拥有 Codex auth；`up` 不复制、
移动或读取 auth，只在 `$CODEX_HOME` 放置指向上述 generated adapter 的 namespaced
symlink。

App Server lifecycle 只清理由 exact repo runtime path 和 dead PID 共同证明的 stale PID/
Unix socket。endpoint 不 ready 且 PID 仍 live、PID file 无效、socket 没有 owned PID，或
runtime artifact 类型冲突时一律 fail closed，并在错误中给出 endpoint、PID/log path 等
诊断位置。

所有 high-level `crew` 与 `codex-crew up` targets 共享 repository-owned explicit endpoint
`unix://<codex-crew-repo>/.codex-crew/runtime/app-server.sock`；它不是 bare `unix://`。
Bare `unix://` default 只属于 low-level diagnostic/control endpoint resolution，不会被
high-level startup 传播给 Codex TUI。

## Runtime model

- 两个 repository-owned manifests 的全部 roles 都显式使用 `gpt-5.6-sol`、`high`
  reasoning effort 与 `service_tier = "fast"`；generated profile 不提供隐式 default。
- 所选 manifest 的每个 TUI 都以各自的 `--profile`、`--strict-config`、`--yolo`、`--remote`、
  `-C TARGET` 和唯一 bootstrap prompt 启动，不预创建 thread，也不执行
  `codex resume`。
- launcher 通过 App Server `thread/list` 与 `thread/read`，覆盖 `cli`、`vscode` 两种
  interactive source，按启动前后 thread 集合、唯一 marker、role 和 exact `cwd` 做
  有界关联；不维护 binding database。
- 只有 bootstrap turn 已 `completed`，且 authoritative `final_answer`
  `agentMessage` 第一行严格为对应的 `role=<role>`，native identity 才 COMMIT。
- 全部 identities COMMIT 后，launcher 使用相同 native control path 向 manifest
  communication thread 发送 runtime handoff；handoff 的 `turn/completed` 与 final-phase
  `agentMessage` 分别是 completion/final authority。
- exact readiness 后 atomic lifecycle persist 才是 successful launch 的最终 commit gate；
  persist failure 保留 window 诊断且不返回 partial success。
- launch 返回的 native `thread_id` 是后续控制身份。`codex://THREAD_ID` 只可作为
  Codex App 的导航 projection，不是 CLI 或 wire address。
- tmux 不承载 role message、completion state 或 controller metadata。禁止通过
  `send-keys`、paste buffer、`capture-pane`、prompt 外观或沉默判断结果。

## Low-level diagnostics

`launch` 与 `app-server check` 保留用于显式 external endpoint 或逐层诊断，不是默认
startup procedure。例如：

```bash
./bin/codex-crew loop install three-agent-dev
./bin/codex-crew loop check three-agent-dev
tmux new-session -d -s diagnostic -c /path/to/project
codex app-server --listen unix:///tmp/codex-crew-diagnostic.sock
./bin/codex-crew app-server check unix:///tmp/codex-crew-diagnostic.sock
./bin/codex-crew launch /path/to/project \
  --session diagnostic \
  --app-server unix:///tmp/codex-crew-diagnostic.sock \
  --json
```

低层 `launch` 假定 profile、tmux session 和 endpoint 已由 caller 准备，不接管它们的
lifecycle。`app-server check` 只做连接和 protocol readiness 检查，不创建 thread。

## Native launch contract

Launch 没有 partial-success 状态。任一 role 在 deadline 内缺失、bootstrap
`failed`/`interrupted`/缺少或 wrong identity final，或 communication handoff 未完成且无
authoritative final 时，command 都以非零状态退出。错误包含 exact tmux window ID 与
affected role/thread；已创建的可视 window 保留供诊断，launcher 不回退、不猜测 identity。

生产默认 committed-identity deadline 为 120 秒：launch 最多等待 120 秒，让 manifest
定义的全部 profiled `high` reasoning、Fast service tier identity turns 完成并满足 COMMIT
gate。120 秒后仍未全部 COMMIT 才按上述规则非零退出；这不是单次 App Server request
timeout。handoff wait 另有 120 秒 bound。

启动命令的固定形态为：

```text
codex --profile PROFILE --strict-config --yolo \
  --remote ENDPOINT -C TARGET UNIQUE_BOOTSTRAP_PROMPT
```

## Native thread control

以下命令只需要 endpoint 与 launch 返回的 native `thread_id`，不读取 tmux 或
SQLite：

```bash
ENDPOINT=unix:///absolute/path/to/codex-crew/.codex-crew/runtime/app-server.sock
WORKER_THREAD=01...

uv run codex-crew crew status \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" --json

uv run codex-crew crew send \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --message-file task.md --json
```

`send` 返回 exact `turn_id`。等待并读取 authoritative final：

```bash
TURN_ID=01...

uv run codex-crew crew wait \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --turn-id "$TURN_ID" --timeout 120 --json

uv run codex-crew crew final \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --turn-id "$TURN_ID" --json
```

若 status 显示已有 active turn，不能再次 `send`。只有给出 exact active turn
precondition 才能 steer：

```bash
uv run codex-crew crew steer \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --expected-turn-id "$TURN_ID" --message '补充完整输入' --json
```

Goal 同样直接属于 native thread：

```bash
uv run codex-crew crew goal get \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" --json
uv run codex-crew crew goal set \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --objective '完成当前 slice' --token-budget 40000 --json
uv run codex-crew crew goal clear \
  --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" --json
```

`--token-budget` 是可选参数；communication role 只有在用户明确给出预算时才使用它。

Runtime invariants：

- `send` 在 `turn/start` 前读取 authoritative turns；对 `-32001` 只做有界 retry。
- 若 `turn/start` response 丢失，仅用启动前 turn 集合与 exact user text 关联新增
  turn；不能唯一证明时返回 `dispatch_unknown`，不重复派发。
- `steer` 必须匹配唯一 authoritative active `turn_id`。
- `wait` 先 read；active 时只用 bare `thread/resume {threadId}` 为当前 controller
  connection 订阅 native events，再 read 一次关闭 completion-before-subscribe race。
- `turn/completed` 和 final `agentMessage` item 是 completion/final authority；没有
  shared Stop snapshot fallback。
- Lifecycle record 只能由 `crew close --window-id` 解析；它不能替代 native thread ID。

## Default Commander–Worker–Judger loop

默认 `three-agent-dev` 的角色固定为 Commander、Worker、Judger；Commander 是唯一
communication role：

- Commander 保持 source-read-only，从 automatic handoff 保存 exact mappings，按用户请求
  派发一个 bounded Worker slice，并用 exact turn wait/final 读取结果。
- Worker 是唯一可修改 shared worktree 的角色，运行最小相关验证并按固定 contract
  回报。
- Judger 保持 source-read-only，只读取本 README，通过这里记录的 public commands
  做 black-box acceptance；它不读取 source、diff、tests 或内部 state。

Worker final：

```text
Result: complete | blocked
Changed: paths and behavior
Verification: command and result
Risks: remaining uncertainty
```

Judger final：

```text
Verdict: PASS | FAIL
Blockers: reproducible user-visible failures, or none
Checks: public commands/interactions executed and observed outcomes
Evidence: exit status, stdout/stderr, responses/UI behavior, and public artifacts
```

Commander 只有在独立 Judger 返回 fresh `PASS` 后才接受 slice。随后它按统一 lifecycle
报告 required per-thread native goal token/time、round wall elapsed、retries 与 blockers，询问用户
继续、补充/修正，还是结束并回收，然后等待下一步。

## Independent Stop snapshots

Stop Hook SQLite 是独立的 direct observation feature，不参与 crew target resolution、
dispatch、wait 或 final。它保留以下命令：

```bash
uv run codex-crew --db /path/snapshots.sqlite3 init-db
uv run codex-crew --db /path/snapshots.sqlite3 hook-config
uv run codex-crew --db /path/snapshots.sqlite3 latest --json
uv run codex-crew --db /path/snapshots.sqlite3 final --session-id SESSION --turn-id TURN
uv run codex-crew --db /path/snapshots.sqlite3 summary --json
```

Hook 只写 `turn_stop_snapshot`，不查询或修改 tmux，也不保存 crew binding。

## Focused verification

```bash
uv run python -m unittest tests.test_startup tests.test_cli
uv run python -m unittest tests.test_launcher
```

静态 migration guard：

```bash
rg -n 'binding.*window|run_tmux_' codex_crew
rg -n 'send-keys|paste-buffer|load-buffer|capture-pane|set-option' codex_crew
```

两条命令都应无匹配。
