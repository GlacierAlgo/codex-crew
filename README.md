# codex-crew

`codex-crew` 从 repository-owned loop package 在 tmux 中启动多个独立 Codex
session，并使用 Codex `Stop` Hook，在每个 turn 停止时把完整 final、累计 token
分项和可选 goal 快照写入本地 SQLite。

运行依赖 Python 3.11+、[uv](https://docs.astral.sh/uv/) 与 Click。真实 launch 还需要
`codex` CLI、一个已存在的 tmux session，以及 target project root 中供 black-box
Judger 阅读的 `README.md`。不需要 Redis 或常驻服务。

## Operator-only clean-clone quickstart

本节以及后续 profile materialization、symlink 和 Stop Hook setup procedure 仅供
人类/operator 在 Commander、Worker、Judger session 启动前执行。运行中的 AI loop
不负责也不得执行 `uv sync`、`codex-crew loop install` 或
`codex-crew loop check`，不得 materialize profiles、建立或修改 runtime symlink、
配置 Hook，亦不得修改自身 runtime setup。

在 clean clone 根目录安装 locked environment：

```bash
uv sync
uv run codex-crew --help
```

列出并验证 repository 中的 loop packages：

```bash
uv run codex-crew loop list
```

当前输出为：

```text
three-agent-dev	roles=commander,worker,judger	layout=even-horizontal
```

`loops/<loop-id>/manifest.toml` 是唯一 runtime authority，声明 ordered roles、每个
role 的 package-local Markdown、namespaced runtime profile、model、reasoning
effort 和 tmux layout。Repository Markdown + manifest 是唯一 editable source。

安装并检查默认 loop：

```bash
uv run codex-crew loop install three-agent-dev
uv run codex-crew loop check three-agent-dev
```

两条命令也可省略默认 ID。install 把 deterministic derived TOML 写入当前 clone 的
ignored `.codex-crew/generated/<loop-id>/`，并在 `$CODEX_HOME`（默认
`~/.codex`）中建立 namespaced `*.config.toml` symlink；不会维护第二份可编辑 copy。
install 可重复运行且输出稳定，check 会同时验证 source、adapter 内容与 symlink
目标。

用临时 Codex home 做隔离验证时，可使用环境变量或 documented option：

```bash
CREW_TMP=$(mktemp -d)
CODEX_HOME="$CREW_TMP/codex-home" uv run codex-crew loop install
uv run codex-crew loop check --codex-home "$CREW_TMP/codex-home"
```

install 在写任何文件前完成全部 preflight。若任一同名 `$CODEX_HOME` target 是
regular file、指向其他位置的 symlink，或任一 generated target 不是
`codex-crew` managed adapter，command 会非零退出并保留所有冲突内容，不会覆盖。
修正或移走明确的冲突后再重试；不要手工编辑 generated adapter。

## 安装 Stop Hook

初始化数据库并生成当前 checkout 对应的 Hook 配置：

```bash
uv run codex-crew init-db
uv run codex-crew hook-config
```

将第二条命令输出的 `Stop` 配置合并到 `$CODEX_HOME/hooks.json`。本项目也提供了
可直接参考的 [`examples/hooks.json`](examples/hooks.json)。Codex 启动后使用
`/hooks` 检查并信任这条 Hook。

默认数据库位于：

```text
~/.local/state/codex-crew/snapshots.sqlite3
```

可以用 `CODEX_CREW_DB` 或全局 `--db` 参数覆盖。

## Launch

确保 profile 已 install/check，且 tmux session `default` 已存在，然后启动 target：

```bash
uv run codex-crew launch /path/to/project
```

`launch TARGET` 默认选择 `three-agent-dev`。显式 loop、session 或 window name：

```bash
uv run codex-crew launch /path/to/project \
  --loop three-agent-dev \
  --session research \
  --window-name crew-factor
```

launcher 从 manifest 读取 ordered roles、runtime profiles、model、reasoning effort
和 `even-horizontal` layout。当前 package 形成 Commander、Worker、Judger 从左到右
三列等宽 pane，每个 pane 通过 `-C` 使用同一个 target root。`--json` 输出
`loop_id`、`layout`、ordered `panes` 和稳定 `pane_mapping`。

window 保存 `@codex_crew_loop`、`@codex_crew_project` 和按 manifest 派生的
`@codex_<role>_pane`；pane 保存 `@codex_role`。创建或记录 mapping 任一步失败时，
只回滚本次 `new-window` 返回的精确 `window_id`，不修改已有 window。

## 无 side effect 的 public launch probe

`launch` 只从 `PATH` 解析 `codex` 和 `tmux`。下面的 public fake-executable
boundary 可供 black-box 验证：它执行真实 CLI/manifest/profile preflight 和 mapping
逻辑，但 fake `tmux` 不创建 session/window，fake `codex` 不启动 TUI。

```bash
CREW_TMP=$(mktemp -d)
mkdir -p "$CREW_TMP/fake-bin"

cat >"$CREW_TMP/fake-bin/codex" <<'SH'
#!/bin/sh
printf '%s\n' 'codex fake 1.0'
SH

cat >"$CREW_TMP/fake-bin/tmux" <<'SH'
#!/bin/sh
operation=$1
shift
case "$operation" in
  has-session|select-layout|set-option|kill-window)
    exit 0
    ;;
  new-window)
    printf '@fake-window\t%%fake-commander\t1\n'
    ;;
  split-window)
    target=
    while [ "$#" -gt 0 ]; do
      if [ "$1" = "-t" ]; then
        shift
        target=$1
        break
      fi
      shift
    done
    case "$target" in
      %fake-commander) printf '%%fake-worker\n' ;;
      %fake-worker) printf '%%fake-judger\n' ;;
      *) printf 'unexpected split target: %s\n' "$target" >&2; exit 64 ;;
    esac
    ;;
  *)
    printf 'unexpected tmux operation: %s\n' "$operation" >&2
    exit 64
    ;;
esac
SH

chmod +x "$CREW_TMP/fake-bin/codex" "$CREW_TMP/fake-bin/tmux"
CODEX_HOME="$CREW_TMP/codex-home" uv run codex-crew loop install
CODEX_HOME="$CREW_TMP/codex-home" uv run codex-crew loop check
PATH="$CREW_TMP/fake-bin:$PATH" \
  CODEX_HOME="$CREW_TMP/codex-home" \
  uv run codex-crew launch "$PWD" --loop three-agent-dev --json
```

成功 probe 退出 0，JSON 的 `pane_mapping` 为
`commander=%fake-commander`、`worker=%fake-worker`、`judger=%fake-judger`。
将 `--loop` 改为未知 ID 会在调用 fake executables 前非零退出。

## 查询与 completion contract

```bash
# 最近 Stop 快照，不输出可能很长的 final
uv run codex-crew latest

# 包含完整字段和 final 的 JSON
uv run codex-crew latest --json

# 某个 session/turn 的完整 final
uv run codex-crew final --session-id SESSION_ID --turn-id TURN_ID

# 每个 session 只取最新累计快照后再汇总
uv run codex-crew summary
```

成功写库后，Hook 设置当前 pane 的 user options：

- `@codex_status=complete`
- `@codex_session_id`
- `@codex_turn_id`
- `@codex_snapshot_at`
- `@codex_crew_db`

例如：

```bash
tmux show-options -pv -t %3 @codex_status
tmux show-options -pv -t %3 @codex_session_id
```

## 统计与可靠性边界

- token 列是该 session 截至 Stop 时刻的累计值，不是单 turn 增量。
- `summary` 对每个 session 只取最新行；单 turn 用量应用相邻快照做差。
- `cached_input_tokens` 是 `input_tokens` 的分项。
- `reasoning_output_tokens` 是 `output_tokens` 的分项，不能重复相加。
- `goal_tokens_used` 与模型 usage 是两个独立口径。
- Goal objective 不超过 40 个 Unicode 字符时原样保存；超过时保存前 20 个字符、
  `...` 和最后 20 个字符。

官方 Hook 输入不直接携带 token usage 和 goal，当前实现从 `transcript_path` 的
JSONL 做兼容性解析。transcript 格式不是稳定 Hook API，因此解析失败时对应字段写
`NULL`，Hook 自身始终返回成功，不会阻止 Codex 完成 turn。

完整 final 可能包含敏感信息。默认数据库文件权限收紧为仅当前用户可读写；备份、
同步或上传前仍应自行检查内容。更完整的字段与设计约束见
[`DESIGN.md`](DESIGN.md)。三 Agent 协作遵循 [`loops/index.md`](loops/index.md)
routing，并读取 [`loops/three-agent-dev/loop.md`](loops/three-agent-dev/loop.md)。
