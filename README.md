# codex-crew

`codex-crew` 在一个 tmux window 中启动 Commander、Worker、Judger 三个独立
Codex TUI。tmux 只提供三列等宽的可视布局；所有派发、等待、final 与 goal 操作都
直接使用 App Server native `thread_id`。

## One-click startup

要求 Python 3.11+、`uv`、`tmux` 与已完成登录的 `codex`。在本 repository root 一次启动：

```bash
./bin/codex-crew up /path/to/project --json
```

`PROJECT_DIR` 省略时默认为当前目录；`--session` 默认是 `default`。`up` 在一次调用内
严格依次完成：

1. 从 `loops/three-agent-dev/manifest.toml` 加载唯一 runtime authority；
2. 幂等生成并检查三个 repo-derived profile adapter；
3. 复用 exact tmux session，只有不存在时才运行
   `tmux new-session -d -s SESSION -c PROJECT_DIR`；
4. 复用 ready 的 repo-owned App Server，否则 detached 启动并有界等待 readiness；
5. 调用 native launcher，创建一个 window、三列等宽 pane，并等待三个 identity
   bootstrap turn COMMIT。

任一 gate 失败都会非零退出，后续 gate 不会执行，也不会返回 partial launch JSON。
`up` 不会杀已有 tmux session/window，也不会为失败的 App Server readiness 杀未知 live
PID。

成功结果包含：

- `window_id`、`window_index` 与 `pane_mapping`，只用于打开和查看布局；
- `thread_mapping`，把 `commander`、`worker`、`judger` 映射到精确 native
  `thread_id`；
- 每个 pane 已 COMMIT 的 `bootstrap_turn_id` 与 marker；
- 三个 TUI 共用的 explicit repo Unix endpoint。

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

## Runtime model

- 三个 TUI 都以各自的 `--profile`、`--strict-config`、`--yolo`、`--remote`、
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

生产默认 committed-identity deadline 为 120 秒：launch 最多等待 120 秒，让三个
profiled xhigh identity turns 完成并满足 COMMIT gate。120 秒后仍未全部 COMMIT 才按
上述规则非零退出；这不是单次 App Server request timeout。

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

## Commander–Worker–Judger loop

角色固定为 Commander、Worker、Judger：

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
