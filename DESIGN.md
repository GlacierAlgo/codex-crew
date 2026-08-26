# 设计说明

## 目标

本项目承担三条紧密关联的路径：发现、验证并物化 repository-owned loop package；
在现有 tmux session 中按 manifest 确定性启动 ordered role panes；Codex `Stop`
Hook 触发后，将这一 turn 的最终消息以及“截至此时”的 session usage/goal 快照
可靠地写入本地 SQLite，并让 Commander 判断 turn 已经完成。

不承担模型计费定价、跨机器队列、完整可观测平台或 Redis 事件总线。

## Loop package contract

`loops/<loop-id>/` 是自包含 package，至少包含 `manifest.toml`、`loop.md` 和 manifest
引用的 role Markdown。`manifest.toml` 是唯一 runtime authority，声明：

- stable loop ID 与 package-local manual；
- ordered roles 与每个 role 的 package-local instructions；
- namespaced runtime profile ID、model、reasoning effort；
- tmux layout、column count 与 equal-width constraint。

discovery/validation 只使用标准库 `tomllib`。package ID 必须匹配 directory；manual
和 role instructions 必须解析为 package 内的 Markdown；role/profile 必须唯一；
`columns` 必须等于 ordered role count。当前 kernel 支持并强制
`even-horizontal` + `equal_width=true`，因此新增合规 loop 以新增 package 为主。

Repository Markdown + manifest 是唯一 editable authority。`loop install` 把确定性
TOML adapter 写入 ignored `.codex-crew/generated/<loop-id>/`，再从
`$CODEX_HOME/<runtime-profile>.config.toml` 建 symlink。所有 generated target 和
runtime target 在任何写入前统一 preflight；regular file、错误 symlink、非 managed
adapter 等冲突一律 fail closed。`loop check` 校验 source、exact adapter bytes 与
symlink target。

## Launcher contract

`codex-crew launch TARGET` 默认 loop 为 `three-agent-dev`、默认 tmux session 为
`default`；`--loop` 可显式选择其他 discovered package：

- 创建一个 detached window，不切换当前 client。
- ordered roles、runtime profiles、model、reasoning effort 与 layout 全部来自选中
  manifest；launcher 不保留 role/profile policy 常量。
- 每个进程使用 manifest 的 namespaced runtime profile，并以 `-C TARGET` 共享
  同一个 working root。
- window options 持久化 target、loop ID 与动态派生的 `@codex_<role>_pane`；每个
  pane 使用 `@codex_role` 声明 role。
- CLI 输出 ordered pane metadata 与 generic `pane_mapping`，不把公共结果绑定到
  固定 role fields。
- 所有 read-only preflight 在创建 window 前完成；创建后的任何失败都只按返回的
  `window_id` 回滚本次 window。

## 数据流

```text
Codex Stop Hook stdin
        │
        ├── session_id / turn_id / model / last_assistant_message
        │
        └── transcript_path
                 │
                 ├── 最新 token_count.total_token_usage
                 └── 最新 thread_goal_updated.goal
        │
        ▼
幂等 upsert: (session_id, turn_id)
        │
        ├── SQLite WAL
        └── tmux pane user options
```

官方 OpenAI 文档说明，`Stop` Hook 提供 `turn_id`、`stop_hook_active` 和
`last_assistant_message`，并继承 `session_id`、`transcript_path`、`model` 等公共
字段：<https://developers.openai.com/codex/hooks/#stop>。

App Server 的 goal 对象提供 `objective`、`status`、`tokenBudget`、`tokensUsed` 和
`timeUsedSeconds`：<https://developers.openai.com/codex/app-server/#manage-a-thread-goal>。

## 表结构

唯一表为 `turn_stop_snapshot`：

| 字段组 | 字段 |
| --- | --- |
| 标识 | `session_id`, `turn_id`, `asof_at` |
| 上下文 | `model`, `final_text` |
| 模型累计 usage | `input_tokens`, `cached_input_tokens`, `cache_write_input_tokens`, `output_tokens`, `reasoning_output_tokens` |
| Goal 快照 | `goal_objective_excerpt`, `goal_created_at`, `goal_status`, `goal_token_budget`, `goal_tokens_used`, `goal_time_used_seconds` |

Goal 不存在或 transcript 无法解析时，相关列为 `NULL`。不保存冗余的
`total_tokens`；展示时计算 `input_tokens + output_tokens`。

`goal_created_at` 用于区分同一 session 中先后替换的不同 goal。官方行为是：提供
新的 objective 会替换 goal 并重置 usage accounting。

## Objective 缩略规则

按 Unicode code point 计数：

- 长度小于等于 40：原样保存。
- 长度大于 40：前 20 + `...` + 后 20。
- 缩略后的最大长度为 43。

因此不会从 UTF-8 字节中间切断中文。字段名使用 `goal_objective_excerpt`，避免误以为
它始终保存完整 objective。

## 累计快照语义

usage 列记录 session 累计计数。若 session A 有三个 Stop，只能取最新一行参与
跨 session 汇总。每 turn 用量是当前快照减去上一快照。

`goal_tokens_used` 单独保留，命名为 goal-visible usage；它不能代替 input/cache/
output usage，也不能用于推断未纳入该 goal 的独立 pane 消耗。

## 并发与失败策略

- SQLite 使用 WAL 和 5 秒 busy timeout，允许多个 pane 短事务并发写入。
- `(session_id, turn_id)` 主键使重复 Stop 投递成为幂等更新。
- transcript 中的未知、损坏记录被忽略。
- 任何读取或写入错误只写 stderr，并让 Hook 输出 `{}`、退出 0。
- 写库成功后才把 tmux `@codex_status` 设置为 `complete`；失败时设置为
  `capture_error`。
- 不把完整 final 放进 tmux option，避免大小、转义和终端渲染问题。

## 兼容性边界

Codex 官方说明 transcript 格式不是稳定 Hook API。解析逻辑因此集中在
`codex_crew/transcript.py`，同时兼容当前 rollout 的 snake_case 事件以及 App Server
camelCase notification。未来格式变化只需修改这一模块；SQLite 字段契约保持不变。
