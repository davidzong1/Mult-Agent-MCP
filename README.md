# Multi-Agent MCP

基于 [FastMCP](https://gofastmcp.com) 构建的多智能体团队协作 MCP 服务器。通过 tmux 终端窗口为每个 agent 成员创建独立的 CLI 会话，由 Leader 统一接收总任务、拆分子任务并分配给各成员。

**支持 Claude Code + OpenAI Codex CLI 混合 agent 协同工作。**

## 架构概览

```
┌──────────────────────────────────────────────────────────────────┐
│  你的 VS Code / 主会话                                            │
│                                                                   │
│  用自然语言管理团队、启动终端、分配总任务                             │
│       ↓                                                           │
│  MCP Server (FastMCP)  ←── teams_data.json (持久化)               │
│       ↓                                                           │
│  ┌─ tmux session: mcp_dev_team ─────────────────────────────────┐ │
│  │  window: 👑alice (leader · codex)  ← 自动连接 MCP            │ │
│  │  window: bob   (coder  · claude)  ← 纯文本接收子任务         │ │
│  │  window: carol (tester · codex)   ← 纯文本接收子任务         │ │
│  └──────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────────┘
```

## 环境要求

| 依赖 | 版本/说明 |
|------|----------|
| Python | ≥ 3.10 |
| FastMCP | ≥ 2.0 (`pip install fastmcp`) |
| tmux | `sudo apt install tmux` / `brew install tmux` |
| Claude Code | （可选）`npm install -g @anthropic-ai/claude-code` |
| Codex CLI | （可选）`npm install -g @openai/codex` |

## 安装

### 1. 克隆项目

```bash
git clone <your-repo-url> mult_agent_mcp
cd mult_agent_mcp
./install.sh
```

### 2. 安装依赖

```bash
pip install fastmcp
```

### 3. 确保 tmux 可用

```bash
tmux -V
```

未安装时：

```bash
# Ubuntu / Debian
sudo apt install tmux
pip install textual

# macOS
brew install tmux
```

### 4. 安装至少一个 Agent CLI

```bash
# Claude Code
npm install -g @anthropic-ai/claude-code

# Codex CLI（需要 Node.js ≥ 22，ChatGPT Plus/Pro 账号或 API Key）
npm install -g @openai/codex
```

### 5. 配置 MCP 连接

**Claude Code** — 在 `.claude/mcp.json` 中添加：

```json
{
  "mcpServers": {
    "mult-agent-mcp": {
      "type": "http",
      "url": "http://localhost:8000/mcp"
    }
  }
}
```

**Codex CLI** — 终端执行：

```bash
codex mcp add mult-agent-mcp --url http://localhost:8000/mcp
```

或让 MCP server 自动配置（见下文 `setup_codex_mcp`）。

> 💡 leader 终端在 `launch_team_terminals` 时会**自动配置** MCP（Claude → 写入 `.claude/mcp.json`，Codex → 注册到 `~/.codex/config.toml`）。

### 6. 启动 MCP 服务器

```bash
cd mult_agent_mcp
python mult_agent_mcp.py
# 或指定端口
FASTMCP_PORT=8000 python mult_agent_mcp.py
```

---

## Team Manager TUI（团队管理可视化界面）

无需自然语言命令，通过**终端图形界面**直接管理团队和成员。

### 启动

```bash
cd mult_agent_mcp
pip install textual         # 首次需要安装
python team_manger.py
```

### 主界面

```
┌─ Multi-Agent MCP — Team Manager ─────────────────────────────┐
│  📋 共 2 个团队                                                │
│                                                               │
│  ┌───────────────────────────────────────────────────────┐    │
│  │ 团队名称   │ 成员数 │ 默认Agent │ Leader    │ 终端状态 │    │
│  │ dream_team │   3   │  claude   │ alice(tmux)│ 🟢     │    │
│  │ dev_team   │   2   │  codex    │ —          │ ⚫     │    │
│  └───────────────────────────────────────────────────────┘    │
│                                                               │
│  A 添加团队 | Enter 查看详情 | D 删除团队 | L 接管Leader | Q 退出│
└───────────────────────────────────────────────────────────────┘
```

### 团队详情页（选中团队按 Enter）

```
┌─ Team Manager ───────────────────────────────────────────────┐
│  📋 dream_team  终端: 🟢 运行中                                 │
│                                                               │
│  ┌───────────────────────────────────────────────┐            │
│  │ 名称   │ 角色   │ Agent │ Leader  │           │            │
│  │ alice  │ leader │ codex │ 👑      │  ← 当前行  │            │
│  │ bob    │ coder  │ claude│         │            │            │
│  │ carol  │ tester │ codex │         │            │            │
│  └───────────────────────────────────────────────┘            │
│                                                               │
│  3 个成员 | Leader: alice (tmux)                               │
│                                                               │
│  A 添加成员 | R 移除成员 | E 编辑成员 | L 指定Leader | Esc 返回  │
└───────────────────────────────────────────────────────────────┘
```

### 操作快捷键

#### 主界面

| 按键 | 功能 |
|------|------|
| `A` | 弹出创建团队表单（名称/描述/默认Agent） |
| `Enter` | 进入团队详情页 |
| `D` | 删除选中团队（有确认） |
| `L` | 直接接管 Leader（当前会话） |
| `Q` | 退出 |

#### 详情页

| 按键 | 功能 |
|------|------|
| `A` | 弹出添加成员表单（名称/角色/Agent） |
| `R` | 移除选中成员（Leader 不可直接移除） |
| `E` | 编辑成员的角色和 Agent |
| `L` | 将选中成员指定为 Leader |
| `T` | 启动团队 tmux 终端 |
| `K` | 关闭团队 tmux 终端 |
| `0` | 打开 Leader 终端；TUI 在 tmux 内运行时会分屏进入，否则打开新终端窗口 |
| `Esc` | 返回主界面 |

详情页成员状态列会显示稳定文字状态：`working` 表示窗口存活且有未完成任务，`idle` 表示窗口存活但无进行中任务，`sleep` 表示任务完成后休眠，`dead` 表示未启动或异常退出。

### 数据兼容

TUI 直接读写 MCP Server 的 `teams_data.json`，两者完全互通：

```
TUI 添加团队 ─→ teams_data.json ←─ MCP Server team_create
TUI 添加成员 ─→ teams_data.json ←─ MCP Server add_member
TUI 指定Leader ─→ teams_data.json ←─ MCP Server set_leader
```

可以用 TUI 创建团队，然后在 Claude Code 中 `launch_team_terminals` 启动终端；也可以反过来。

---

## 快速开始

所有操作在 Claude Code / Codex 的**聊天框中用自然语言**完成。

### 第一步：创建混合 agent 团队

```
> 创建一个团队叫 dream_team，默认 agent 用 claude。
  添加三个成员：
  alice（role=leader, agent=codex），
  bob（role=coder, agent=claude），
  carol（role=tester, agent=codex）
```

等价于：

```python
team_create("dream_team", "混合 agent 团队", default_agent="claude")
add_member("dream_team", "alice", role="leader", agent="codex")
add_member("dream_team", "bob",   role="coder",  agent="claude")
add_member("dream_team", "carol", role="tester",  agent="codex")
set_leader("dream_team", "alice")
```

### 第二步：启动终端

```
> 启动 dream_team 的终端，任务是"实现用户登录系统"
```

MCP server 自动完成：
- alice (codex leader)：注册 MCP 到 `~/.codex/config.toml`，启动 codex 窗口
- bob (claude coder)：启动 claude 窗口
- carol (codex tester)：启动 codex 窗口
- 总任务文本发送到 alice 的终端

### 第三步：Leader 拆分与分配

Leader (alice) 在自己的终端中：

```
> 查看团队
→ leader_list_team("dream_team")
→ bob(coder · claude), carol(tester · codex)

> 让 bob 实现登录页面，让 carol 写测试用例
→ leader_assign_subtask("dream_team", "bob", "实现登录页面...")
→ leader_assign_subtask("dream_team", "carol", "写登录测试用例...")
```

### 第四步：收尾

```
> 查看终端状态
→ terminal_status("dream_team")

> 关闭所有终端
→ kill_team_terminals("dream_team")
```

---

## 两种 Leader 模式

### tmux 模式（默认）

Leader 在独立 tmux 窗口中运行，自动连接 MCP server。

```
你 ─→ MCP ─→ launch_team_terminals ─→ [alice tmux] ─→ leader_* ─→ [bob] [carol]
```

### Direct 模式

你直接在**当前会话**充当 leader。

```python
claim_leader("dream_team")
# 如果旧 leader 存活 → 自动降级为普通成员
# 如果旧 leader 已死 → 直接接管

# 直接在当前会话分配任务
leader_assign_subtask("dream_team", "bob", "实现登录页面")
leader_broadcast("dream_team", "进度更新：前端完成 30%")

# 释放
unclaim_leader("dream_team", restore_member="alice")
```

---

## 混合 Agent 协同（Claude + Codex）

### 核心设计

每个成员有独立的 `agent` 字段，值为任意 CLI 命令。MCP server 根据 agent 类型自动适配：

| Agent 类型 | 识别规则 | MCP 配置方式 | Leader 启动 |
|-----------|---------|-------------|------------|
| `claude` | 包含 "claude" | 写入 `<workspace_dir>/.claude/mcp.json` | 从团队共享工作目录启动 |
| `codex` | 包含 "codex" | 注册到 `~/.codex/config.toml` | 用 `-C` 从团队共享工作目录启动，并注入团队 Leader 提示 |
| 其他 | 不匹配以上 | 两种都尝试 | 从团队共享工作目录启动 |

`workspace_dir` 默认避开内部 `.team_workspaces/<team>` 隔离目录，优先使用进入 agent 前的工作目录；通过 `team_manger.py` 启动时使用 `team_manger.py` 所在目录。成员交换报告、补丁、文件锁和压缩上下文时使用 `share_context_space/<team>` 共享上下文区。

### 混合团队示例

```python
# 创建团队
team_create("mixed_team", default_agent="claude")

# Claude 作为 leader + Codex 作为成员
add_member("mixed_team", "alice", role="leader", agent="claude")
add_member("mixed_team", "bob",   role="coder",  agent="codex")
add_member("mixed_team", "carol", role="tester",  agent="codex")
set_leader("mixed_team", "alice")

# 或者反过来——Codex 作为 leader
add_member("mixed_team", "dave", role="backend_dev", agent="claude")
member_set_agent("mixed_team", "alice", "codex")  # 切换 leader 类型
launch_team_terminals("mixed_team", task="实现微服务架构")

# 检查 agent 配置状态
check_agent_setup("mixed_team")
→ Claude MCP: ✅
→ Codex MCP: ✅
```

### Codex MCP 管理工具

| 工具 | 说明 |
|------|------|
| `setup_codex_mcp()` | 手动注册 Codex MCP（等效于 `codex mcp add`） |
| `remove_codex_mcp()` | 移除 Codex MCP |
| `check_agent_setup(team_name)` | 检查团队中所有 agent 的 MCP 配置状态 |

```bash
# 也可以手动在终端执行
codex mcp add mult-agent-mcp --url http://localhost:8000/mcp
codex mcp list
```

### 任务传递原理

无论 member 是 Claude 还是 Codex，子任务通过 `tmux send-keys` 以**纯文本**发送到对应窗口。成员终端收到文本后，像有用户在终端打字并按回车一样，agent CLI 自动解析并执行。

```
leader_assign_subtask("team", "bob", "实现登录页面")
          │
          ▼
tmux send-keys -t mcp_team:bob "实现登录页面" Enter
          │
          ▼
bob 的终端窗口收到文本 → claude 或 codex 开始执行
```

如果成员卡在文件修改或命令执行的授权提示中，Leader 可先读取成员终端，再向该成员终端发送受控确认选项：

```python
leader_read_member_terminal("team", "bob")         # 查看成员是否停在 approval prompt
leader_authorize_member("team", "bob", "yes")      # 选择第 1 项，通常为本次允许
leader_authorize_member("team", "bob", "session")  # 选择第 2 项，通常为本会话记住
leader_authorize_member("team", "bob", "3")        # 选择第 3 项，具体含义以终端提示为准
```

---

## 完整工具参考

### 用户端工具（团队管理）

| 工具 | 参数 | 说明 |
|------|------|------|
| `team_create` | `team_name`, `description?`, `default_agent?` | 创建团队，可指定默认 agent |
| `list_teams` | — | 列出所有团队 |
| `delete_team` | `team_name` | 删除团队及终端 |
| `team_set_default_agent` | `team_name`, `agent` | 修改团队默认 agent |

### 用户端工具（成员管理）

| 工具 | 参数 | 说明 |
|------|------|------|
| `add_member` | `team_name`, `member_name`, `role?`, `model?`, `agent?` | 添加成员。agent 为空时继承团队默认值 |
| `remove_member` | `team_name`, `member_name` | 移除成员 |
| `list_members` | `team_name` | 列出成员（含 agent 类型） |
| `set_leader` | `team_name`, `member_name` | 指定 tmux leader |
| `member_set_agent` | `team_name`, `member_name`, `agent` | 修改成员 agent（claude/codex/自定义） |

### 用户端工具（Agent 配置）

| 工具 | 参数 | 说明 |
|------|------|------|
| `setup_codex_mcp` | `server_name?` | 注册 Codex MCP 到 `~/.codex/config.toml` |
| `remove_codex_mcp` | `server_name?` | 移除 Codex MCP |
| `check_agent_setup` | `team_name` | 检查团队所有 agent 的 MCP 配置状态 |
| `get_server_config` | — | 查看 MCP 配置（Claude + Codex 双格式） |

### 用户端工具（Leader 切换）

| 工具 | 参数 | 说明 |
|------|------|------|
| `claim_leader` | `team_name` | 将当前会话注册为 leader |
| `unclaim_leader` | `team_name`, `restore_member?` | 释放 leader |

### 用户端工具（终端管理）

| 工具 | 参数 | 说明 |
|------|------|------|
| `launch_team_terminals` | `team_name`, `task?` | 启动所有成员的 tmux 终端 |
| `kill_team_terminals` | `team_name` | 关闭所有终端 |
| `terminal_status` | `team_name` | 查看终端状态（含 agent 类型） |

### Leader 端工具

| 工具 | 参数 | 说明 |
|------|------|------|
| `leader_list_team` | `team_name` | 查看团队面板 |
| `leader_assign_subtask` | `team_name`, `member_name`, `subtask`, `context?` | 分配子任务（支持 claude + codex 成员） |
| `leader_broadcast` | `team_name`, `message` | 广播消息给全员 |
| `leader_authorize_member` | `team_name`, `member_name`, `choice?` | 对成员终端中的授权提示发送确认选项 |
| `leader_read_member_terminal` | `team_name`, `member_name`, `lines?` | 读取成员终端最近输出，定位授权卡点 |
| `leader_monitor_members` | `team_name`, `auto_authorize_choice?`, `mark_idle_done?`, `lines?` | 巡检成员终端，识别 approval/busy/idle/dead，并让空闲成员退出 working |
| `leader_set_member_mode` | `team_name`, `member_name?`, `mode`, `auto_authorize?` | 设置成员 `manual`/`auto`/`plan` 模式；Claude 映射 permission-mode，Codex 映射 approval policy |
| `leader_add_member` | `team_name`, `member_name`, `role?`, `agent?` | 动态添加成员 + 创建终端 |
| `leader_remove_member` | `team_name`, `member_name` | 移除成员 + 关闭终端 |
| `leader_redefine_member` | `team_name`, `member_name`, `role?`, `agent?` | 修改成员角色/agent |
| `leader_launch_member_terminal` | `team_name`, `member_name` | 启动成员终端 |

### 成员协作工具

| 工具 | 参数 | 说明 |
|------|------|------|
| `member_report_result` | `team_name`, `result`, `artifact_path?`, `member_name?`, `compressed_context?` | 回传结果并生成压缩上下文 |
| `member_read_shared` | `team_name` | 读取共享上下文区最近结果 |
| `member_list_shared_files` | `team_name` | 列出共享上下文区文件 |
| `member_acquire_file_lock` | `team_name`, `member_name`, `file_path`, `purpose?`, `ttl_seconds?` | 申请文件修改锁 |
| `member_release_file_lock` | `team_name`, `member_name`, `file_path` | 释放自己的文件锁 |
| `member_list_file_locks` | `team_name` | 查看活跃文件锁 |
| `member_submit_patch` | `team_name`, `member_name`, `summary`, `patch`, `base_ref?` | 将修改以 patch 提交到共享上下文区 |

多人需要修改同一文件时，优先由成员申请 `member_acquire_file_lock`；未拿到锁的成员应提交 `member_submit_patch`，由 leader 或锁持有人合并，避免直接覆盖其他成员改动。

---

## claim_leader 接管逻辑

```
claim_leader("team")
        │
        ├── 已经是 direct leader
        │   → 提示"你已经是 leader"
        │
        ├── 之前无 leader
        │   → 🆕 设为直接控制模式
        │
        ├── 有 tmux leader 且终端存活
        │   → 🔄 原 leader 降级为普通成员（窗口保留）
        │   → ✅ 你接管为 leader
        │
        └── 有 tmux leader 但终端已死
            → 💀 直接接管，清理死引用
```

---

## 数据存储

```json
{
  "teams": {
    "dream_team": {
      "description": "混合 agent 团队",
      "leader": "alice",
      "leader_type": "tmux",
      "default_agent": "claude",
      "terminals_active": true,
      "members": {
        "alice": {"role": "leader", "model": "", "agent": "codex"},
        "bob":   {"role": "coder",  "model": "", "agent": "claude"},
        "carol": {"role": "tester", "model": "", "agent": "codex"}
      }
    }
  }
}
```

| 字段 | 说明 |
|------|------|
| `leader` | 被指定为 leader 的成员名 |
| `leader_type` | `"tmux"` / `"direct"` / `""` |
| `default_agent` | 团队默认 agent，新成员继承 |
| `workspace_dir` | 团队共享工作目录 |
| `context_dir` | 团队共享上下文区 |
| `terminals_active` | tmux session 是否在运行 |
| `members[].agent` | 成员 CLI 命令（`claude` / `codex` / 自定义） |
| `members[].role` | 成员角色标识 |
| `members[].model` | 成员使用的模型（可选） |

MCP 配置位置：
- Claude: `<workspace_dir>/.claude/mcp.json`
- Codex: `~/.codex/config.toml`（全局，所有 codex agent 共享）

---

## tmux 操作速查

```bash
# 查看团队终端
tmux attach -t mcp_dev_team

# 在 tmux 中: Ctrl+B 然后按数字键 → 切换窗口
# 在 tmux 中: Ctrl+B 然后按 W → 列出所有窗口
# 在 tmux 中: Ctrl+B 然后按 D → 脱离（不关闭）

tmux list-sessions              # 列出所有 session
tmux list-windows -t mcp_dev_team  # 列出某 session 的窗口
```

---

## 常见问题

**Q: leader 的终端如何获得 `leader_*` 工具？**

A: `launch_team_terminals` 根据 leader 的 agent 类型自动配置 MCP：
- Claude leader → 写入 `<workspace_dir>/.claude/mcp.json`，claude 启动在此目录自动加载
- Codex leader → 注册到 `~/.codex/config.toml`，用 `-C` 进入共享工作目录，并收到要求使用 `leader_*` 工具协调已有成员的初始提示

**Q: Codex 和 Claude 成员之间如何协同？**

A: 成员终端通过 `tmux send-keys` 接收纯文本指令——与具体 CLI 无关。Leader 把 "实现登录页面" 发给 bob（claude）和 "写测试" 发给 carol（codex），两者都在自己终端中收到文本并按各自方式执行。

**Q: Codex 作为 leader 时的限制？**

A: Codex 需要先完成 MCP 注册才有 tool calling 能力。`launch_team_terminals` 会自动处理，或手动执行 `setup_codex_mcp()`。Codex 的 MCP 是全局注册（`~/.codex/config.toml`），同一台机器上所有 codex 实例共享；Codex leader 启动时会收到明确提示，要求使用 `leader_list_team` / `leader_assign_subtask` 等团队 MCP 工具，而不是用 Codex 内置 spawn 代替团队成员。

**Q: 如何在 direct 模式下分配差异化任务？**

A: 先用 `claim_leader` 接管，然后多次调用 `leader_assign_subtask`，每次指定不同 `member_name` 和 `subtask`。

**Q: tmux leader 终端被关闭了怎么办？**

A: 调用 `claim_leader("team_name")`，系统检测到旧 leader 已死，自动接管。之后可重新 `launch_team_terminals`。

**Q: 如何从 Claude leader 切换到 Codex leader？**

A: `member_set_agent("team", "alice", "codex")` 然后重新 `launch_team_terminals`。tmux 模式下旧窗口会被重建，新窗口启动 codex 并自动配置 MCP。
