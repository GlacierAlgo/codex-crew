# Commander–worker–judger loop

## Runtime authority

- `manifest.toml` 是 ordered roles、runtime profiles、model、reasoning effort、
  service tier 与 `even-horizontal` tmux layout 的唯一 runtime authority。三个 roles
  固定使用 `gpt-5.6-sol`、`high` reasoning effort 与 Fast service tier。
- 本 manual 定义协作流程；三个 package-local role Markdown 定义角色边界。
- Launcher 在一个 tmux window 中启动三个 fresh profiled Codex TUI。tmux 只负责可视
  布局，不参与 dispatch、completion 或 identity resolution。
- Shared App Server 是唯一 crew communication transport。One-click startup 使用
  `unix://<codex-crew-repo>/.codex-crew/runtime/app-server.sock`；launch result 中的 exact
  endpoint 与 native `thread_id` 共同构成后续操作参数。
- Launcher discovery 覆盖 `cli` 与 `vscode` interactive sources。只有 identity
  bootstrap turn `completed` 且 final 第一行严格为 `role=<role>` 才返回三个 IDs；
  production deadline 为 120 秒；missing/failed role 使 launch 非零退出并保留
  visual window 供诊断。

## Repository-owned startup

Operator 从 `codex-crew` repository root 只需调用：

```bash
./bin/codex-crew up /path/to/project --json
```

该入口按 manifest 依次 materialize/check repo-derived profiles、复用或创建 exact tmux
session、复用或启动 repo-owned Unix App Server，再调用 native launcher。PID、log、socket
只在 ignored `.codex-crew/runtime/`；canonical config、launcher 与 role sources 始终在
repository。已经启动的 Commander、Worker、Judger 不运行 `up` 或修改自身 runtime
setup。

## Roles

- **Commander:** owns requirements, slicing, native thread mapping, dispatch, retries,
  acceptance flow, and final answer. It stays source-read-only.
- **Worker:** owns one bounded implementation slice, is the only role that changes the shared
  worktree, runs focused verification, and returns evidence.
- **Judger:** stays read-only and acts as a black-box adversarial user. Its only product knowledge
  is root `README.md`; it returns `PASS` or `FAIL` from public runtime behavior.

## Native dispatch

从 launcher JSON 保存 endpoint 与三个 exact IDs：

```bash
ENDPOINT=unix:///absolute/path/to/codex-crew/.codex-crew/runtime/app-server.sock
WORKER_THREAD=01...
JUDGER_THREAD=01...
```

派发一个完整 Worker slice：

```bash
codex-crew crew status --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" --json
codex-crew crew send --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --message-file worker-task.md --json
```

用返回的 exact `turn_id` 等待并读取 authoritative final：

```bash
codex-crew crew wait --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --turn-id "$TURN_ID" --timeout 120 --json
codex-crew crew final --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --turn-id "$TURN_ID" --json
```

如果存在 active turn，不得再次 send。只有 exact active precondition 才可追加一次完整
steer input：

```bash
codex-crew crew steer --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --expected-turn-id "$TURN_ID" --message-file steer.md --json
```

Goal 也直接使用相同 native identity：

```bash
codex-crew crew goal get --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" --json
codex-crew crew goal set --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" \
  --objective "$OBJECTIVE" --json
codex-crew crew goal clear --endpoint "$ENDPOINT" --thread-id "$WORKER_THREAD" --json
```

禁止 shared `send-keys`、paste buffer、TUI stdin、`capture-pane`、prompt appearance、
terminal silence 或 Stop snapshot fallback。`codex://THREAD_ID` 仅是 Codex App navigation
projection，不是 wire scheme。

## One acceptance loop

1. Commander 定义一个 Worker slice：scope、constraints、acceptance criteria 与最小相关
   verification。
2. Commander 用 Worker native `thread_id` send，随后 wait exact `turn_id` 并读取 final。
3. Commander 把 original slice、acceptance criteria、Worker claim 与 README public entry
   points 发给 Judger；不发送 source paths、diff、implementation detail 或 Worker test
   output 作为 acceptance evidence。
4. Commander 用 Judger native `thread_id` send/wait/final。
5. `PASS` 后接受；`FAIL` 时只把 reproducible blockers 发回 Worker，再 fresh rejudge。
6. 重复直到 `PASS` 或真实 user/external blocker。

## Communication language

- Commander、Worker、Judger 的 user-facing communication、handoff、progress update 与
  final response 默认使用简洁、专业的中文句法。
- Code identifier、CLI command、config key、schema/contract field、file path、error text 与
  标准 technical term 保留英文原名。
- 固定 message-contract label 保持英文；除非用户明确要求，不追加英文全文翻译。

## Message contracts

Worker final:

```text
Result: complete | blocked
Changed: paths and behavior
Verification: command and result
Risks: remaining uncertainty
```

Judger final:

```text
Verdict: PASS | FAIL
Blockers: reproducible user-visible failures, or none
Checks: public commands/interactions executed and observed outcomes
Evidence: exit status, stdout/stderr, responses/UI behavior, and public artifacts
```

## Invariants

- Only one Worker turn mutates the shared worktree at a time.
- Judger never fixes findings and never reads beyond root README for product knowledge.
- Commander never accepts without a fresh Judger `PASS`.
- Role order is Commander、Worker、Judger; manifest remains
  profile/model/effort/service-tier/layout authority.
- Every operation uses one explicit Unix endpoint plus one exact native `thread_id`.
- `send` preserves one complete message; `steer` preserves exact turn precondition.
- Completion comes from `turn/completed`; final comes from authoritative final `agentMessage`.
- tmux is visual layout only and carries no shared role message or lifecycle state.
- Token columns are cumulative per native thread; sum only each thread's latest observation.
- Goal-visible tokens and model token breakdowns remain separate accounting.

## Finish

Commander reports accepted scope, Worker/Judger native thread and turn IDs, focused Worker
verification, Judger black-box checks, rejudge count, final verdict, and unresolved blockers.
