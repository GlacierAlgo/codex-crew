# Project instructions

## Commander-worker-judger loop

- Before selecting an execution loop, read `~/.codex/loops/index.md` and load
  only the matching loop files.
- Before starting or operating the three-agent tmux loop, read
  `~/.codex/loops/three-agent-dev/loop.md` completely.
- Treat `~/.codex/loops/three-agent-dev/manifest.toml` as the sole runtime
  authority and the package-local role Markdown as the sole editable role
  instruction sources. Generated profile TOML files are derived adapters.
- Treat the `~/.codex/loops` directory symlink target as the source of truth
  for role boundaries, dispatch, completion detection, review, retry, and
  final reporting. Maintain canonical content only in repository `loops/`;
  never replace the symlink with copied files.
- Do not use `tmux capture-pane` to decide that a Codex turn completed.
- Only the worker mutates the shared worktree during a delegated slice; the
  judger remains read-only, acts as a black-box adversarial user with only the
  root `README.md` as product knowledge, and the commander owns the final
  decision.
- Commander、Worker 与 Judger 的沟通默认使用专业中文句法；code identifier、
  CLI、config key、schema/contract field、file path、error text 与标准 technical
  term 保留英文原名。固定 message-contract label 保持英文，其内容使用中文。
- 除非用户明确要求，不追加逐段或整篇英文翻译。
