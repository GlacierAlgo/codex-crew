# Loop Registry

`codex-crew/loops/` 是 reusable execution loops 的 canonical collection。
`$CODEX_HOME/loops`（默认 `~/.codex/loops`）必须是指向本 directory 的
symlink。

## Selection

1. 开始任务前先读取本 registry，根据 `Trigger` 选择 loop。
2. `required`：trigger 匹配时必须加载；多个匹配项可以叠加。
3. `optional`：仅在用户、project instructions 或上层 loop 明确选择时加载。
4. `exclusive`：同一任务最多选择一个；若多个 trigger 同时匹配且没有更具体的
   project routing，必须先澄清，不得猜测。
5. 只读取被选中 package 的 `loop.md`，不默认加载其他 package/manual。
6. 每个 package 的 `manifest.toml` 是唯一 runtime authority；package-local role
   Markdown 是唯一 editable role instruction source。
7. 新增、重命名或删除 loop 时，必须在同一变更中更新本 registry。

## Registered loops

| ID | Mode | Trigger | Source |
| --- | --- | --- | --- |
| `three-agent-dev` | `required` | 任何可能修改 worktree 的 development task | [`three-agent-dev/loop.md`](three-agent-dev/loop.md) |
