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

1. 从所选 `loops/<loop-id>/manifest.toml` 加载该 loop 的唯一 runtime authority；
2. 按 manifest role count 幂等生成并检查 repo-derived profile adapters；
3. 复用 exact tmux session，只有不存在时才运行
   `tmux new-session -d -s SESSION -c PROJECT_DIR`；
4. 复用 ready 的 repo-owned App Server，否则 detached 启动并有界等待 readiness；
5. 调用 native launcher，创建一个 window、manifest-defined 等宽 panes，并等待全部
   identity bootstrap turns COMMIT。

任一 gate 失败都会非零退出，后续 gate 不会执行，也不会返回 partial launch JSON。
`up` 不会杀已有 tmux session/window，也不会为失败的 App Server readiness 杀未知 live
PID。

成功结果包含：

- `window_id`、`window_index` 与 `pane_mapping`，只用于打开和查看布局；
- `thread_mapping`，把所选 manifest 的每个 role 映射到精确 native `thread_id`；默认
  mapping keys 是 `commander`、`worker`、`judger`；
- 每个 pane 已 COMMIT 的 `bootstrap_turn_id` 与 marker；
- 全部 TUI 共用的 explicit repo Unix endpoint。

## Four-way API-budget design

`api-budget-design` 是 optional loop，只在用户明确选择它，或明确要求比较 `N=3/4/5/6`
四种 API-budget 设计时使用。它用同一 `gpt-5.6-sol` model、`high` reasoning effort、
Fast service tier 和共同设计合同启动四个相互隔离的 designer；唯一实验变量是 role
profile 中的 `N`：最多 `N` 个 deep modules，且恰好 `N` 个顶层 public APIs。

```bash
./bin/codex-crew up /path/to/project --loop api-budget-design --json
```

成功 JSON 的 `thread_mapping` 固定包含：

```text
designer_3 -> exact native thread_id for N=3
designer_4 -> exact native thread_id for N=4
designer_5 -> exact native thread_id for N=5
designer_6 -> exact native thread_id for N=6
```

把同一个 new-build 或 migration 请求保存为 `design-request.md`。四次 dispatch 必须读取
这一个未修改文件，使 request bytes 一致；不要在 request 中注入 `N`，也不要新增 fanout、
batch 或 aggregator API。将 startup JSON 中的值填入以下变量，并分别保存四次 `send`
返回的 exact `turn_id`：

```bash
ENDPOINT='unix:///absolute/path/to/codex-crew/.codex-crew/runtime/app-server.sock'
DESIGNER_3_THREAD='01...'
DESIGNER_4_THREAD='01...'
DESIGNER_5_THREAD='01...'
DESIGNER_6_THREAD='01...'
REQUEST_FILE='design-request.md'

./bin/codex-crew crew send --endpoint "$ENDPOINT" --thread-id "$DESIGNER_3_THREAD" --message-file "$REQUEST_FILE" --json
./bin/codex-crew crew send --endpoint "$ENDPOINT" --thread-id "$DESIGNER_4_THREAD" --message-file "$REQUEST_FILE" --json
./bin/codex-crew crew send --endpoint "$ENDPOINT" --thread-id "$DESIGNER_5_THREAD" --message-file "$REQUEST_FILE" --json
./bin/codex-crew crew send --endpoint "$ENDPOINT" --thread-id "$DESIGNER_6_THREAD" --message-file "$REQUEST_FILE" --json
```

例如把四个返回值分别记为 `TURN_3`、`TURN_4`、`TURN_5`、`TURN_6`，再逐路读取 native
completion 与 authoritative final：

```bash
TURN_3='01...'
TURN_4='01...'
TURN_5='01...'
TURN_6='01...'

./bin/codex-crew crew wait --endpoint "$ENDPOINT" --thread-id "$DESIGNER_3_THREAD" --turn-id "$TURN_3" --timeout 120 --json
./bin/codex-crew crew wait --endpoint "$ENDPOINT" --thread-id "$DESIGNER_4_THREAD" --turn-id "$TURN_4" --timeout 120 --json
./bin/codex-crew crew wait --endpoint "$ENDPOINT" --thread-id "$DESIGNER_5_THREAD" --turn-id "$TURN_5" --timeout 120 --json
./bin/codex-crew crew wait --endpoint "$ENDPOINT" --thread-id "$DESIGNER_6_THREAD" --turn-id "$TURN_6" --timeout 120 --json

./bin/codex-crew crew final --endpoint "$ENDPOINT" --thread-id "$DESIGNER_3_THREAD" --turn-id "$TURN_3" --json
./bin/codex-crew crew final --endpoint "$ENDPOINT" --thread-id "$DESIGNER_4_THREAD" --turn-id "$TURN_4" --json
./bin/codex-crew crew final --endpoint "$ENDPOINT" --thread-id "$DESIGNER_5_THREAD" --turn-id "$TURN_5" --json
./bin/codex-crew crew final --endpoint "$ENDPOINT" --thread-id "$DESIGNER_6_THREAD" --turn-id "$TURN_6" --json
```

四路 thread、context 和 output 完全隔离，不读取彼此结果。每份 final 使用相同 output
contract：`Assumptions`、`Module map / Deep modules (K <= N)`、
`Public APIs (exactly N)`、
`Main sequential flows`、`New-build path` 或 `Migration path`、
`Discarded abstractions and tradeoffs`、`Budget audit`。最后一节必须报告
`deep_modules=K/N` 与 `public_apis=N/N`。

Language contract：四个 Designer 的所有 user-facing design output 必须使用中文句法；
technical identifier、API、module、contract、CLI、schema、file path、error text 与标准
technical term 保留 English 原名，并自然混排在中文句法中。不要附加逐段或整篇
English translation。

## Repository and runtime boundary

Canonical launcher、startup lifecycle、manifest、role instructions 与 profile generator 全部
位于 tracked repository：`bin/`、`codex_crew/`、`loops/`。运行时只产生以下 ignored
内容：

```text
<codex-crew-repo>/.codex-crew/generated/<loop-id>/*.config.toml
<codex-crew-repo>/.codex-crew/runtime/app-server.sock
<codex-crew-repo>/.codex-crew/runtime/app-server.pid
<codex-crew-repo>/.codex-crew/runtime/app-server.log
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

Launch 没有 partial-success 状态。任一 role 在 deadline 内缺失，或 bootstrap
`failed`、`interrupted`、缺少/wrong identity final 时，command 以非零状态退出；错误
包含 exact tmux window ID 与 missing/failed role。已创建的可视 window会保留供诊断，
launcher 不回退、不猜测 identity。

生产默认 committed-identity deadline 为 120 秒：launch 最多等待 120 秒，让 manifest
定义的全部 profiled `high` reasoning、Fast service tier identity turns 完成并满足 COMMIT
gate。120 秒后仍未全部 COMMIT 才按上述规则非零退出；这不是单次 App Server request
timeout。

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

Runtime invariants：

- `send` 在 `turn/start` 前读取 authoritative turns；对 `-32001` 只做有界 retry。
- 若 `turn/start` response 丢失，仅用启动前 turn 集合与 exact user text 关联新增
  turn；不能唯一证明时返回 `dispatch_unknown`，不重复派发。
- `steer` 必须匹配唯一 authoritative active `turn_id`。
- `wait` 先 read；active 时只用 bare `thread/resume {threadId}` 为当前 controller
  connection 订阅 native events，再 read 一次关闭 completion-before-subscribe race。
- `turn/completed` 和 final `agentMessage` item 是 completion/final authority；没有
  shared Stop snapshot fallback。

## Default Commander–Worker–Judger loop

默认 `three-agent-dev` 的角色固定为 Commander、Worker、Judger：

- Commander 保持 source-read-only，按 `thread_mapping` 派发一个 bounded Worker
  slice，并用 exact turn wait/final 读取结果。
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

Commander 只有在独立 Judger 返回 `PASS` 后才接受 slice。

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
