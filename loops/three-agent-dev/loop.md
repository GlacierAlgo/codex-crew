# Commander–worker–judger loop

## Runtime authority

- `manifest.toml` 是 ordered roles、runtime profiles、model、reasoning effort 和
  tmux layout 的 runtime authority。
- `loop.md` 定义协作流程，三个 package-local role Markdown 定义角色边界。
- Agent 必须先读取 loop registry，再按 trigger 只加载本 manual。registry、manifest
  或 manual 异常时必须报告 blocker，不得静默降级为 single-agent acceptance。
- `codex-crew` CLI、runtime profiles 和 Stop Hook 由 operator 在 session 启动前外部
  准备。执行中的 Commander、Worker、Judger 不安装、物化或修改自身 runtime。

## Problem

Independent Codex TUI sessions share a project through tmux. Terminal text is
not a reliable completion API: `capture-pane` only sees the rendered grid and
can miss or truncate the final answer. `codex-crew` uses the Codex `Stop` Hook
to persist semantic completion, final text, token usage, and goal state in
SQLite, then exposes the result through tmux pane options and a CLI.

## Roles

- **Commander:** owns requirements, task slicing, pane mapping, dispatch,
  retries, and the final answer. It does not accept work without a Judger pass.
- **Worker:** owns one bounded implementation slice at a time, changes the
  shared worktree, runs focused verification, and returns evidence.
- **Judger:** stays read-only and acts as a black-box adversarial user. Its only
  product knowledge is the root `README.md`; it exercises documented public
  entry points and returns `PASS` or `FAIL` from observable runtime behavior.

The launcher records `@codex_role` on each pane and stable derived pane options
on the window; `session_id` independently identifies Codex sessions in Stop
snapshots.

## One loop

1. Commander defines one Worker slice with scope, constraints, acceptance
   criteria, and the smallest relevant verification command.
2. Before dispatch, mark the Worker pane non-complete:

```bash
tmux set-option -p -t %2 @codex_status running
```

3. Send the complete task to the Worker, then submit Enter. Use literal paste
   for text; a second Enter is a transport retry, not a new consultation.
4. Poll completion without reading rendered terminal content:

```bash
tmux show-options -p -v -t %2 @codex_status
```

5. When it is `complete`, resolve the exact result:

```bash
SID=$(tmux show-options -p -v -t %2 @codex_session_id)
TID=$(tmux show-options -p -v -t %2 @codex_turn_id)
codex-crew final --session-id "$SID" --turn-id "$TID"
```

6. Commander sends the Judger the original slice, acceptance criteria, Worker
   claim, and documented public entry points. Do not send source paths, diffs,
   implementation details, or Worker test output as acceptance evidence. Mark
   the Judger pane as `running` before dispatch.
7. Read the Judger result through its session and turn IDs in the same way.
8. On `PASS`, Commander accepts the slice. On `FAIL`, Commander sends only the
   blockers back to the Worker, then submits the revised result for rejudging.
9. Repeat until `PASS` or a genuine user/external blocker prevents progress.

## Communication language

- Commander、Worker 与 Judger 的 user-facing communication、角色间 handoff、
  progress update 和 final response 默认使用简洁、专业的中文句法。
- Code identifier、CLI command、config key、schema/contract field、file path、
  error text 与标准 technical term 保留英文原名。
- 固定 message-contract label 保持英文，其内容与解释使用中文。
- 除非用户明确要求，不追加逐段或整篇 English translation。

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
- Judger never fixes its own findings; it returns them to Commander.
- Judger reads only the root `README.md` for product knowledge and does not
  inspect source, diffs, Git history, tests, fixtures, or internal state.
- Judger never runs automated test suites or imports internal modules. It
  validates through the documented public product interface.
- Worker tests and claims are context, not Judger acceptance evidence.
- Commander never infers success from silence, prompt appearance, or pane text.
- Set `running` before every dispatch because `complete` describes the last Stop.
- Missing `@codex_session_id` means that pane has not completed its first turn.
- Token columns are cumulative per session; sum only each session's latest row.
- Goal-visible tokens and model token breakdowns remain separate accounting.

## Finish

Commander reports accepted scope, Worker and Judger session/turn IDs, focused
verification, rejudge count, final verdict, and any unresolved blocker. Use
`codex-crew summary` for the latest cross-session token totals.
