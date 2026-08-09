# 方案 B 实施路线：热重启 + 会话续跑

> 目标：工作流跑到中途账号余额耗尽时，自动换号并让成员**带着原有上下文**继续，
> 而不是丢任务或伪造成功。

## 0. 目标与边界

**能达到**：断点 3–10 秒（kill + respawn + resume），成员对话历史完整保留，
无需人工介入，leader 侧只看到一条"该成员已切号续跑"的事件。

**达不到**（必须在设计时接受，别对外承诺"零中断"）：

- 进程一定要重启。base_url 在两个 CLI 里都是启动时固化的，没有运行时通道。
- 正在流式输出的那一轮会被打断。resume 恢复的是**已落盘的**会话历史，
  最后一轮未完成的助手输出会丢失或截断，agent 需要重新组织那一轮的表达。
- 工具执行中途被 kill 的副作用不回滚（写了一半的文件仍然是写了一半）。

因此方案 B 的定位是**过渡方案**：在本地网关（方案 A）就绪前把损失从
"整条工作流中断 + 静默伪造成功"降到"某成员卡顿几秒"。

---

## 1. 现状盘点：已有什么，缺什么

### 1.1 已经具备（可直接复用）

| 能力 | 位置 |
|---|---|
| kill 窗口 → 重建 → 重发任务的完整恢复管道 | `mult_agent_mcp.py:6181` `_recover_and_send()` |
| 崩溃检测 + 自动恢复触发 | `mult_agent_mcp.py:1432-1445`（`state == "dead"` 分支） |
| 恢复次数限流 | `member["recovery_count"]` vs `team["monitor_max_recoveries"]`（默认 3） |
| 未完成任务持久化与重发 | `last_task` / `last_task_completed` / `last_context` |
| 后台监控循环（默认 30s） | `mult_agent_mcp.py:1520` `_monitor_team_loop()` |
| 多账号 profile 存储 | `agent_users` typed profile（`common/tmux_utils.py:1189-1197`） |
| 每成员私有配置隔离 | `CLAUDE_CONFIG_DIR`（`tmux_utils.py:1611`）+ `--settings`（`:1521`） |

**关键结论**：方案 B 不需要新建恢复机制，只需要在既有恢复管道上加三样东西 ——
**触发条件**（余额识别）、**换号决策**（profile 池）、**上下文保留**（resume）。

### 1.2 缺失项

1. 余额/配额识别 —— `_classify_terminal_output()`（`mult_agent_mcp.py:789`）
   只有 approval / busy / idle / dead / unknown 五态，无配额概念。
2. profile 池与故障转移顺序 —— 现在是"一个成员绑一个 profile"的静态映射。
3. 会话 id 的记录与 resume 参数 —— 所有 spawn 路径都是全新会话。

### 1.3 现存缺陷（**先修，与方案 B 独立**）

余额耗尽时 CLI 报错后回到 `❯` 提示符 → 被分类为 `idle` →
`mult_agent_mcp.py:1463` 的 `mark_idle_done` 把**根本没执行的任务标记为完成**，
并调用 `_finalize_agent_completion()` 写入压缩上下文。leader 读到的是"成功"。

这是数据正确性问题，优先级高于换号本身。修法见阶段 1。

---

## 2. 关键技术验证结果

本节结论均为本机实测（`claude --help` / `codex --help` /
`~/.codex/sessions` / `~/.claude`），非推测。未验证项已单独标注。

### 2.1 Claude Code：会话 id 可控 ✅

`--session-id <uuid>` 可在启动时**指定**会话 id（必须合法 UUID），
`-r/--resume <id>` 精确恢复，`--fork-session` 可在恢复时另起新 id。

> ⚠️ **不要用 `-c/--continue`**。它的语义是"当前目录最近一次对话"，
> 而本项目所有成员共享同一个 `team_dir` 作为 cwd
> （`_tmux_spawn_member` 里 `cmd.extend(["-c", team_dir] + ...)`），
> 并发场景下多个成员会互相抢到对方的会话。必须走 `--session-id` 自管 UUID。

**会话文件位置**：`~/.claude/projects/<cwd-slug>/`。
私有 `CLAUDE_CONFIG_DIR` 里的 `projects` 是**软链回真实 `~/.claude`** 的
（`_link_claude_home_assets()` 只排除 `settings.json` / `settings.local.json`，
见 `tmux_utils.py:1559`）。

这一点很关键：`_agent_user_config_dir_path()` 的路径含 profile_key
（`tmux_utils.py:1562-1570`），**换 profile 就会换 CLAUDE_CONFIG_DIR**。
幸好 `projects` 是软链，会话历史落在共享的真实目录里，
所以跨 profile resume 可行。但这是个**隐式依赖**，必须在阶段 3 加断言校验
（软链有可能被 Claude 的"写临时文件 + rename"替换成真实文件，见 `:1584` 注释）。

### 2.2 Codex：会话 id 不可指定，但可用 CODEX_HOME 绕开 ✅

`codex resume <SESSION_ID>` / `resume --last` / `fork` 均存在，
但**没有 `--session-id` 之类的启动时指定参数**。

会话文件：`~/.codex/sessions/YYYY/MM/DD/rollout-<ISO时间>-<uuid>.jsonl`，
首行 `session_meta` 含 `session_id` / `cwd` / `model_provider` / `git`。

反查 session id 有两条路：

- **（不推荐）按时间戳 + cwd 反查**：所有成员 cwd 相同，并发 spawn 时会串号。
- **（推荐）每成员私有 `CODEX_HOME`**：sessions 目录随之私有化，
  `codex resume --last` 天然就是"该成员上一个会话"，无歧义。

`CODEX_HOME` 是真实变量（codex 自身 system prompt 里明确提到
`$CODEX_HOME`，见任一 rollout 文件首行 `base_instructions`）。

这条路顺带解决下面 2.3 的问题，因此**方案 B 的 codex 侧应当直接上
CODEX_HOME**，而不是先做时间戳反查再改。

实现时照抄 claude 侧的成熟模式（`_agent_user_config_dir_path` +
`_link_claude_home_assets`）：私有目录放自己的 `config.toml`，
其余资产（`auth.json`、`skills`、`rules`、`history.jsonl`…）软链回真实
`~/.codex`；**`sessions/` 不软链**，保持私有。

### 2.3 顺带发现：codex 的 base_url 接管当前可能是空转 ⚠️

`_agent_user_env_prefix_for_team()`（`tmux_utils.py:1336`）给 codex 注入
`OPENAI_BASE_URL`。但本机 `~/.codex/config.toml` 是：

```toml
model_provider = "wanapi"
[model_providers.wanapi]
base_url = "https://api.wanapis.com/v1"
wire_api = "responses"
```

据我理解 `OPENAI_BASE_URL` 只影响内置 `openai` provider，
不覆盖自定义 provider 在 toml 里的 `base_url`。若成立，
**codex 成员的 base_url 接管一直没生效，全部走 wanapi**。

同理 `CODEX_MODEL`（`:1340`）大概率也没被 codex 读取 ——
只是 model 实际由 `--model` flag 生效（`codex_command()`，`:617`），
把问题掩盖了。

> **这条尚未实测坐实**，是整个方案 B 里唯一可能推翻 codex 侧设计的点。
> 阶段 0 必须先验证（验证方法见 §6）。
> 好消息是：无论结论如何，私有 `CODEX_HOME` + 自写 `config.toml` 都是正解。

---

## 3. 数据结构变更

均为 `teams_data.json` 增量字段，无破坏性改动，缺省行为与现在一致。

### 3.1 成员级

```jsonc
"members": {
  "coder-1": {
    // —— 会话续跑 ——
    "session_id": "550e8400-e29b-41d4-a716-446655440000",  // claude: 自生成 UUID
    "session_resumable": true,          // 首次 spawn 后置 true
    "codex_home": "/home/zwc/.mult_agent_mcp/.codex_home/team__member",

    // —— 换号 ——
    "agent_user": "acct-a",             // 已有字段，语义不变（当前激活）
    "agent_user_failover_history": [
      {"from": "acct-a", "to": "acct-b", "ts": "...", "reason": "quota_exhausted"}
    ],
    "quota_switch_count": 0             // 独立于 recovery_count 的限流计数
  }
}
```

### 3.2 团队级

```jsonc
{
  "agent_user_pool": {
    "claude": ["acct-a", "acct-b", "acct-c"],   // 按优先级排序
    "codex":  ["cx-a", "cx-b"]
  },
  "quota_failover_enabled": true,
  "max_quota_switches": 5,              // 单轮总任务的换号上限
  "agent_user_health": {
    "acct-a": {"state": "exhausted", "since": "...", "cooldown_until": "..."}
  }
}
```

**沿用 `_effective_agent_user_registry()` 的全局+团队合并语义**，
池里存的只是 key，profile 本体仍在全局 registry。

> `quota_switch_count` 必须与 `recovery_count` **分开计数**。
> 否则连续换 3 个号就会撞上 `monitor_max_recoveries=3` 的崩溃恢复上限，
> 换号能力被误杀。

---

## 4. 分阶段实施

### 阶段 0：验证 codex 注入通道（阻塞项，0.5 天）

见 §6。结论决定阶段 4 的形态。**先做这个**，别先写代码。

### 阶段 1：止血 —— 修 idle 误判 + 加配额识别（1 天，独立价值）

**改 `_classify_terminal_output()`（`mult_agent_mcp.py:789`）**，
在 `approval` 判定之后、`_tail_looks_like_shell_prompt` 之前插入配额检测：

```python
QUOTA_MARKERS = (
    "insufficient balance", "insufficient_quota", "quota exceeded",
    "exceeded your current quota", "billing", "payment required",
    "余额不足", "额度不足", "欠费",
    "402",                      # 谨慎：需与上下文组合，避免误伤
)
```

返回新状态 `"quota"`。

**改 `_scan_member_terminal()`（`:1426` 起）**，新增 `elif state == "quota"` 分支：

- `member["blocked_reason"] = "quota"`，记 `last_blocked_ts`
- **绝不执行 `mark_idle_done`** —— 这是修掉伪造成功的核心
- 阶段 1 只记录 + 上报 leader；换号在阶段 3 接上

**风险**：中转站的错误文案五花八门，纯文本匹配必然有漏网。
阶段 1 的目标不是识别率 100%，而是**宁可漏判为 unknown，也不能误判为 idle**。
建议同时把"tail 出现 `error` / `failed` 且随后回到 `❯`"归为 `unknown` 而非 `idle`，
从根上堵住误判成功这条路。

> 这也是方案 A 明显更优的地方：网关能直接读 HTTP 状态码和错误体，
> 不用猜终端文字。阶段 1 的匹配表在方案 A 落地后可以整体退役。

**测试**：`tests/` 下加 `test_quota_classification.py`，
用真实终端 dump 片段做样例（正例：各中转站余额错误；
反例：代码里出现 "quota" 字样的正常输出、跑测试时打印的 "402"）。

### 阶段 2：会话 id 落地（1.5 天）

目标：**先让 resume 能用，暂不接换号**。这样阶段 2 可独立验证、独立回滚。

**Claude 侧**：

1. `claude_agent_args()`（`common/tmux_utils.py:584`）加 `session_id` /
   `resume` 两个 kwarg：首次启动发 `--session-id <uuid>`，
   恢复时发 `--resume <uuid>`。
2. 成员首次 spawn 时生成 UUID 存入 `member["session_id"]`。
3. **四处 spawn 点全部要改**（漏一处就静默退回全新会话，且不报错）：
   - `common/tmux_utils.py:681` `tmux_spawn_member()`
   - `mult_agent_mcp.py:2052` `_tmux_spawn_member()`
   - `tui/tui_screens.py:561`（leader）、`:613`（成员）、`:1194`（恢复）

   > 项目里 claude 侧已经用 `claude_agent_user_launch()` 把四处收敛成单一入口
   > （`tmux_utils.py:1663` 的注释明确说了"此前每处都各自拼装，
   > 任何一处漏掉 settings_path 就会静默退回默认配置且不报错"）。
   > **session_id 应当走同样的收敛策略**，新增一个
   > `claude_session_args(team, member) -> list[str]` 单一入口，
   > 而不是在四处各写一遍。

**Codex 侧**：实现 `build_agent_user_codex_home()`，
结构对照 `build_agent_user_claude_config_dir()`（`tmux_utils.py:1611`）。
恢复时命令改为 `codex resume --last`（私有 CODEX_HOME 下无歧义）。

**验收**：手工 kill 一个成员窗口 → 触发恢复 → 新窗口里 agent 记得之前的对话。

### 阶段 3：故障转移决策（1 天）

新增 `_select_failover_profile(team_name, member_name, reason) -> str | None`：

1. 读 `agent_user_pool[atype]`，跳过 `agent_user_health` 里标记 exhausted
   且未过 cooldown 的
2. 跳过当前 profile
3. 返回下一个；池空则返回 `None`（→ 保持阻塞并告警 leader，不要静默降级）

在 `_scan_member_terminal()` 的 `quota` 分支接上：
标记旧 profile exhausted → 选新 profile → 写 `member["agent_user"]` →
`quota_switch_count += 1` → 调 `_recover_and_send()`。

**`_recover_and_send()` 需要改造**（`mult_agent_mcp.py:6181`）：

- 加 `reason: str = "crash"` 参数
- `reason == "quota_switch"` 时：走 `quota_switch_count` 限流而非 `recovery_count`
- 重建后带 resume 参数（阶段 2 已就绪）
- **不重发 `last_task`** —— resume 已经带回上下文，重发会让 agent 从头再做一遍。
  改为发一句简短提示：「已切换至账号 B，请从中断处继续」。

  > 这是与现有崩溃恢复语义最大的区别。现在的
  > `leader_launch_member_terminal()`（`:5491-5502`）无条件重发
  > `[任务上下文] + [子任务]`，因为全新会话确实什么都不知道。
  > resume 路径下必须区别对待，否则重复劳动 + 上下文翻倍。

### 阶段 4：codex 通道补齐（1 天，依赖阶段 0 结论）

若阶段 0 确认 `OPENAI_BASE_URL` 对自定义 provider 无效：
在私有 `CODEX_HOME/config.toml` 里直接写 `model_provider` +
`[model_providers.X]`，彻底取代 `OPENAI_BASE_URL` 注入。

保留 `-c model_providers.X.base_url=...` 作为不启用 CODEX_HOME 时的兜底。

### 阶段 5：可观测性（0.5 天）

- `leader_check_member_status` 增加 `blocked_reason: quota` 与
  当前 `agent_user` 展示
- TUI 成员表的 Agent 用户列标注 exhausted 状态
- `agent_user_failover_history` 写入共享上下文 `results.jsonl`，
  让 leader 能读到换号事件

---

## 5. 风险清单

| 风险 | 影响 | 缓解 |
|---|---|---|
| **换号后 model 名不兼容** | 新上游不认旧 model 名，一启动就报错，且此时 resume 已消耗一次 | profile 自带 `anthropic_model`/`codex_model`，换号时一并切换（`resolve_agent_model` 已按 profile 解析，天然支持）；池内建议只放同名 model 的号 |
| **`projects` 软链被替换成真实文件** | 跨 profile resume 静默失败，退回全新会话 | 阶段 2 spawn 前断言 `projects` 是软链且指向真实 `~/.claude`，不成立则告警而非静默 |
| 配额文案漏判 | 卡住不换号 | 兜底：`blocked_reason` 为空但 `busy` 持续超 N 个周期 → 按疑似配额处理 |
| 配额文案误判 | 正常任务被误 kill | 只在 tail **最后几行**匹配 + 要求同时回到提示符；宁漏勿误 |
| 并发换号打架 | 多成员同时耗尽，同时抢下一个号 | 复用 `member_spawn_lock`（`tmux_utils.py:248`）+ 团队级换号串行化 |
| resume 丢最后一轮 | agent 重复或跳过一小段 | 提示语明确「上一轮可能未完成，请先自查再继续」 |
| 换号风暴 | 池里所有号被快速烧完 | `max_quota_switches` + 每号 cooldown；触顶后停止并告警，不无限重试 |

---

## 6. 阶段 0 验证清单（只读，不改仓库）

1. **`OPENAI_BASE_URL` 是否覆盖自定义 provider**
   起一个 codex：`env OPENAI_BASE_URL=http://127.0.0.1:9/v1 codex`，
   发一句话。若仍能正常响应 → 说明 env 未生效（走的还是 wanapi），
   §2.3 的判断成立。

2. **`CODEX_HOME` 是否被识别**
   `env CODEX_HOME=/tmp/cx-test codex` 启动后，
   检查 `/tmp/cx-test/` 下是否生成 `sessions/` 等结构。

3. **codex 是否 mid-session 重读 config.toml**
   起 codex → 改 `~/.codex/config.toml` 的 model → `/status` 看是否变化。
   （预期不变，用于坐实"base_url 完全锁死"这一前提。）

4. **claude `--session-id` + `--resume` 跨 profile 是否连续**
   用 profile A 起一个带 `--session-id <uuid>` 的会话，说一句可辨识的话；
   退出后用 profile B（不同 `CLAUDE_CONFIG_DIR`）`--resume <uuid>`，
   确认能记起那句话。**这条直接决定阶段 3 是否成立。**

5. **`apiKeyHelper` 的 TTL 变量名与刷新语义**
   （仅在只换 key、不换 base_url 的场景下用得上，属于 B-lite 优化项，非阻塞。）

---

## 7. 工作量与建议顺序

| 阶段 | 内容 | 预估 | 可独立交付 |
|---|---|---|---|
| 0 | 验证（§6 第 1–4 条） | 0.5d | — |
| 1 | 配额识别 + 修 idle 误判 | 1d | ✅ 强烈建议先做 |
| 2 | 会话 id + resume | 1.5d | ✅ |
| 3 | 故障转移决策 | 1d | ✅ |
| 4 | codex CODEX_HOME | 1d | ✅ |
| 5 | 可观测性 | 0.5d | ✅ |

合计约 5.5 人天。

**建议**：阶段 1 立刻做 —— 它修的是"余额耗尽被记成任务成功"这个数据正确性
问题，不依赖任何后续阶段，且方案 A 落地后仍有价值（作为网关不可用时的兜底）。

阶段 2–4 是方案 B 的主体，约 3.5 天。如果本地网关（方案 A）已排期且
在两周内能落地，这 3.5 天更划算的用法是直接投入方案 A ——
方案 A 不需要 §5 里的任何一条风险缓解（不重启就没有 resume 问题、
没有会话 id 问题、没有跨 profile 软链问题），且 claude/codex 一视同仁。

方案 B 的真正适用场景是：**网关短期内做不了，或者需要一个不依赖单点的兜底路径。**

