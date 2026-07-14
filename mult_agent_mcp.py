from fastmcp import FastMCP
import json
import os
import subprocess
import time

mcp = FastMCP("mult agent mcp")

# ============================================================
# 数据层
# ============================================================
DATA_FILE = os.path.join(os.path.dirname(__file__), "teams_data.json")
SHARE_WORKSPACE_DIR = os.path.join(os.path.dirname(__file__), "share_work_space")


def _share_dir(team: str) -> str:
    """团队共享文件区路径"""
    d = os.path.join(SHARE_WORKSPACE_DIR, team)
    os.makedirs(d, exist_ok=True)
    return d


def _load() -> dict:
    if not os.path.exists(DATA_FILE):
        return {"teams": {}}
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def _save(data: dict) -> None:
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _session(team: str) -> str:
    return f"mcp_{team}"


def _team_dir(team: str) -> str:
    d = os.path.join(os.path.dirname(__file__), ".team_workspaces", team)
    os.makedirs(d, exist_ok=True)
    return d


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """执行命令，返回 (returncode, stdout, stderr)"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "命令未找到"
    except subprocess.TimeoutExpired:
        return -1, "", "命令超时"


def _tmux(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "tmux 未安装"
    except subprocess.TimeoutExpired:
        return -1, "", "tmux 命令超时"


def _find_any_session(team: str) -> str | None:
    """
    查找团队的 tmux session，支持两种命名格式：
      1. mcp_{team}           (MCP server 创建，无时间戳)
      2. mcp_{team}_HHMMSS    (TUI 创建，带时间戳)
    如果有多个匹配项，优先返回精确匹配，其次返回最新的。
    """
    # 先尝试 MCP server 格式: mcp_{team}
    session = _session(team)
    rc, _, _ = _tmux(["has-session", "-t", session])
    if rc == 0:
        return session
    # 再尝试 TUI 格式: mcp_{team}_{timestamp}
    rc, out, _ = _tmux(["list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        prefix = f"mcp_{team}_"
        for name in out.split("\n"):
            if name.startswith(prefix):
                return name  # 返回最新匹配项
    return None


def _tmux_session_alive(team: str) -> bool:
    return _find_any_session(team) is not None


def _tmux_window_exists(team: str, window: str) -> bool:
    session = _find_any_session(team)
    if not session:
        return False
    rc, out, _ = _tmux(["list-windows", "-t", session, "-F", "#{window_name}"])
    if rc != 0:
        return False
    return window in out.split("\n")


def _send_keys(session: str, window: str, text: str) -> tuple[int, str]:
    rc, _, err = _tmux(["send-keys", "-t", f"{session}:{window}", "-l", text])
    if rc != 0:
        return rc, err
    rc, _, err = _tmux(["send-keys", "-t", f"{session}:{window}", "Enter"])
    return rc, err if rc != 0 else ""


def _kill_session(team: str) -> None:
    _tmux(["kill-session", "-t", _session(team)])


def _get_server_port() -> int:
    return int(os.environ.get("FASTMCP_PORT", "8000"))


# ============================================================
# Agent 类型识别
# ============================================================

def _agent_type(agent_cmd: str) -> str:
    """根据 agent 启动命令识别 agent 类型: 'claude' | 'codex' | 'other'"""
    cmd = agent_cmd.lower().strip()
    if "codex" in cmd:
        return "codex"
    if "claude" in cmd:
        return "claude"
    return "other"


def _is_codex(agent_cmd: str) -> bool:
    return _agent_type(agent_cmd) == "codex"


def _is_claude(agent_cmd: str) -> bool:
    return _agent_type(agent_cmd) == "claude"


# ============================================================
# MCP 配置生成
# ============================================================

def _claude_mcp_json_path(team_name: str) -> str:
    """Claude 的 MCP 配置文件路径"""
    team_dir = _team_dir(team_name)
    claude_dir = os.path.join(team_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    return os.path.join(claude_dir, "mcp.json")


def _write_claude_mcp(team_name: str) -> str:
    """为 Claude Code 写入 .claude/mcp.json"""
    port = _get_server_port()
    mcp_json_path = _claude_mcp_json_path(team_name)
    config = {
        "mcpServers": {
            "mult-agent-mcp": {
                "type": "sse",
                "url": f"http://localhost:{port}/sse",
            }
        }
    }
    with open(mcp_json_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    return mcp_json_path


def _codex_config_path() -> str:
    return os.path.expanduser("~/.codex/config.toml")


def _codex_mcp_registered(server_name: str = "mult-agent-mcp") -> bool:
    """检查 codex 的 config.toml 中是否已注册此 MCP server"""
    config_path = _codex_config_path()
    if not os.path.exists(config_path):
        return False
    with open(config_path, "r") as f:
        content = f.read()
    return f"[mcp_servers.{server_name}]" in content


def _ensure_codex_mcp(server_name: str = "mult-agent-mcp") -> str:
    """
    确保 codex 全局配置中注册了此 MCP server。
    优先通过 codex mcp add CLI，失败则直接编辑配置文件。
    返回状态字符串。
    """
    port = _get_server_port()
    url = f"http://localhost:{port}/sse"

    # 已注册则跳过
    if _codex_mcp_registered(server_name):
        return "already_configured"

    # 方式 1: codex mcp add CLI
    rc, _, _ = _run([
        "codex", "mcp", "add", server_name,
        "--url", url,
    ], timeout=15)
    if rc == 0:
        return "✅ codex MCP 已通过 CLI 注册。"

    # 方式 2: 直接写入 ~/.codex/config.toml
    config_path = _codex_config_path()
    section = (
        f"\n[mcp_servers.{server_name}]\n"
        f'type = "sse"\n'
        f'url = "{url}"\n'
    )
    try:
        with open(config_path, "a") as f:
            f.write(section)
        return f"✅ codex MCP 已写入 {config_path}"
    except Exception as e:
        return f"❌ codex MCP 配置失败: {e}\n💡 请手动执行: codex mcp add {server_name} --url {url}"


def _remove_codex_mcp(server_name: str = "mult-agent-mcp") -> str:
    """从 codex 配置中移除 MCP server"""
    if not _codex_mcp_registered(server_name):
        return "not_registered"

    # 方式 1: codex mcp remove CLI
    rc, _, _ = _run(["codex", "mcp", "remove", server_name], timeout=10)
    if rc == 0:
        return "✅ codex MCP 已通过 CLI 移除。"

    # 方式 2: 直接编辑
    config_path = _codex_config_path()
    try:
        with open(config_path, "r") as f:
            lines = f.readlines()

        in_section = False
        result = []
        for line in lines:
            if line.strip() == f"[mcp_servers.{server_name}]":
                in_section = True
                continue
            if in_section:
                if line.strip().startswith("[") and line.strip() != f"[mcp_servers.{server_name}]":
                    in_section = False
                    result.append(line)
                continue
            result.append(line)

        with open(config_path, "w") as f:
            f.writelines(result)
        return f"✅ codex MCP 已从配置中移除。"
    except Exception as e:
        return f"❌ 移除失败: {e}\n💡 请手动执行: codex mcp remove {server_name}"


def _ensure_agent_mcp(team_name: str, agent_cmd: str) -> str:
    """
    根据 agent 类型确保 MCP 配置已就绪。
    - claude: 为 team workspace 写入 .claude/mcp.json
    - codex: 确保全局 codex config 中已注册
    - other: 尝试两种方式
    返回配置摘要。
    """
    atype = _agent_type(agent_cmd)
    results = []

    if atype == "claude":
        path = _write_claude_mcp(team_name)
        results.append(f"📄 Claude MCP → {path}")
    elif atype == "codex":
        status = _ensure_codex_mcp()
        if status == "already_configured":
            results.append("📄 Codex MCP → 已注册（全局配置）")
        else:
            results.append(f"📄 Codex MCP → {status}")
    else:
        # 未知 agent，两种都尝试
        _write_claude_mcp(team_name)
        _ensure_codex_mcp()
        results.append("📄 已同时尝试 Claude 和 Codex MCP 配置。")

    return "\n".join(results)


# ============================================================
# 团队管理
# ============================================================

@mcp.tool
def team_create(
    team_name: str,
    description: str = "",
    default_agent: str = "claude",
) -> str:
    """
    创建一个新的 agent 团队。

    Args:
        team_name: 团队名称（唯一标识）
        description: 团队描述
        default_agent: 团队默认 agent，新成员继承此设置。可选: claude, codex, 或任意命令
    """
    data = _load()
    if team_name in data["teams"]:
        return f"❌ 团队 '{team_name}' 已存在。"

    data["teams"][team_name] = {
        "description": description,
        "leader": "",
        "leader_type": "",
        "default_agent": default_agent,
        "terminals_active": False,
        "members": {},
    }
    _save(data)
    atype = _agent_type(default_agent)
    return (
        f"✅ 团队 '{team_name}' 创建成功（默认 agent: {default_agent} [{atype}]）。\n"
        f"💡 下一步: add_member → set_leader → claim_leader 或 launch_team_terminals"
    )


@mcp.tool
def team_set_default_agent(team_name: str, agent: str) -> str:
    """
    修改团队的默认 agent。已存在的成员不受影响。

    Args:
        team_name: 团队名称
        agent: 新默认 agent（如 claude, codex）
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    team["default_agent"] = agent
    _save(data)
    return f"✅ 团队 '{team_name}' 默认 agent → '{agent}'。"


@mcp.tool
def list_teams() -> str:
    """列出所有已创建的团队。"""
    data = _load()
    teams = data.get("teams", {})
    if not teams:
        return "📭 当前没有任何团队。"

    lines = ["📋 **团队列表**:"]
    for name, info in teams.items():
        mc = len(info.get("members", {}))
        leader = info.get("leader", "")
        ltype = info.get("leader_type", "")
        default_agent = info.get("default_agent", "claude")
        status = "🟢" if info.get("terminals_active") else "⚫"

        if ltype == "direct":
            ldr = " leader=你(直接)"
        elif leader:
            ldr = f" leader={leader}(tmux)"
        else:
            ldr = ""
        lines.append(f"  • {status} **{name}** ({mc} 人, agent={default_agent}{ldr})")
    return "\n".join(lines)


@mcp.tool
def delete_team(team_name: str) -> str:
    """删除整个团队及其终端。"""
    data = _load()
    if team_name not in data.get("teams", {}):
        return f"❌ 团队 '{team_name}' 不存在。"

    _kill_session(team_name)
    del data["teams"][team_name]
    _save(data)
    return f"✅ 团队 '{team_name}' 已删除。"


# ============================================================
# 成员管理
# ============================================================

@mcp.tool
def add_member(
    team_name: str,
    member_name: str,
    role: str = "",
    model: str = "",
    agent: str = "",
) -> str:
    """
    向团队添加成员。

    Args:
        team_name: 团队名称
        member_name: 成员名称（团队内唯一）
        role: 角色标识（如 leader, coder, reviewer, tester）
        model: 模型名
        agent: 终端启动命令。空字符串 = 继承团队默认 agent。可指定 claude/codex/任意命令
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if member_name in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 已存在。"

    actual_agent = agent if agent else team.get("default_agent", "claude")
    atype = _agent_type(actual_agent)

    team["members"][member_name] = {
        "role": role,
        "model": model,
        "agent": actual_agent,
    }
    _save(data)
    return f"✅ 成员 '{member_name}' 已加入 '{team_name}'（agent={actual_agent} [{atype}], role={role or '无'}）。"


@mcp.tool
def remove_member(team_name: str, member_name: str) -> str:
    """从团队中移除成员。运行中的 tmux leader 需先接管。"""
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if member_name not in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 不存在。"

    ltype = team.get("leader_type", "")

    if team.get("leader") == member_name and ltype == "tmux":
        if _tmux_session_alive(team_name) and _tmux_window_exists(team_name, member_name):
            return f"❌ '{member_name}' 是正在运行的 tmux leader，无法移除。\n💡 请先用 claim_leader 接管。"

    del team["members"][member_name]

    if team.get("leader") == member_name:
        team["leader"] = ""
        team["leader_type"] = ""

    _save(data)

    session = _session(team_name)
    _tmux(["kill-window", "-t", f"{session}:{member_name}"])

    return f"✅ 成员 '{member_name}' 已移除。"


@mcp.tool
def list_members(team_name: str) -> str:
    """列出团队成员（含 agent 类型）。"""
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    if not members:
        return "📭 暂无成员。"

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    default_agent = team.get("default_agent", "claude")
    lines = [
        f"👥 **{team_name}** ({len(members)} 人)  [默认 agent: {default_agent}]"
    ]

    if ltype == "direct":
        lines.append(f"   👑 Leader: **你（当前会话）** ← 直接控制")

    for i, (name, info) in enumerate(members.items(), 1):
        role = info.get("role", "")
        agent = info.get("agent", "claude")
        atype = _agent_type(agent)
        is_ldr = " 👑LEADER" if (name == leader and ltype == "tmux") else ""
        extras = [f"{agent}[{atype}]"]
        if role:
            extras.insert(0, role)
        lines.append(f"  {i}. **{name}**{is_ldr} ({', '.join(extras)})")
    return "\n".join(lines)


@mcp.tool
def set_leader(team_name: str, member_name: str) -> str:
    """
    指定团队的 leader（tmux 模式）。

    Args:
        team_name: 团队名称
        member_name: 要设为 leader 的成员名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if member_name not in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 不存在，请先 add_member。"

    agent = team["members"][member_name].get("agent", "claude")
    atype = _agent_type(agent)

    team["leader"] = member_name
    team["leader_type"] = "tmux"
    team["members"][member_name]["role"] = "leader"
    _save(data)
    return f"✅ '{member_name}' 已被设为 '{team_name}' 的 tmux leader（agent: {agent} [{atype}]）。"


@mcp.tool
def member_set_agent(team_name: str, member_name: str, agent: str) -> str:
    """设置单个成员的 agent（claude / codex / 自定义命令）。"""
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if member_name not in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 不存在。"

    team["members"][member_name]["agent"] = agent
    _save(data)
    atype = _agent_type(agent)
    return f"✅ '{member_name}' agent → '{agent}' [{atype}]。"


# ============================================================
# claim / unclaim leader
# ============================================================

@mcp.tool
def claim_leader(team_name: str) -> str:
    """
    将当前终端（本 Claude Code / Codex 会话）注册为团队的 leader。

    接管行为:
    - 如果不存在 leader: 直接将当前会话设为 leader
    - 如果已有 tmux leader 且终端存活: 将该 tmux leader 降级为普通成员，当前会话接管
    - 如果前 leader 是已关闭的 tmux 窗口: 直接接管 leader 身份

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    old_leader = team.get("leader", "")
    old_type = team.get("leader_type", "")
    lines = []

    if old_type == "direct":
        return f"✅ 你已经是 '{team_name}' 的 leader（直接控制模式）。"

    if old_leader and old_type == "tmux":
        session_alive = _tmux_session_alive(team_name)
        window_alive = session_alive and _tmux_window_exists(team_name, old_leader)

        if window_alive:
            team["members"][old_leader]["role"] = "member"
            lines.append(f"🔄 原 tmux leader '{old_leader}' 终端存活，已降级为普通成员（窗口保留）。")
        else:
            lines.append(f"💀 原 tmux leader '{old_leader}' 终端已关闭，直接接管。")
    elif not old_leader:
        lines.append(f"🆕 '{team_name}' 之前无 leader，设为直接控制模式。")

    team["leader_type"] = "direct"
    if not team["leader"]:
        team["leader"] = old_leader if old_leader else "you"
    _save(data)

    lines += [
        "",
        f"✅ 你已接管 **{team_name}** 的 leader！",
        "",
        "💡 现在可在当前会话中直接调用:",
        "   leader_list_team     - 查看团队面板",
        "   leader_assign_subtask - 分配子任务给成员",
        "   leader_broadcast     - 广播消息",
        "   leader_add_member    - 动态添加成员",
        "   leader_remove_member - 移除成员",
        "   leader_redefine_member - 修改成员角色/agent",
        "   leader_launch_member_terminal - 启动成员终端",
    ]
    return "\n".join(lines)


@mcp.tool
def unclaim_leader(team_name: str, restore_member: str = "") -> str:
    """
    放弃 leader 身份。

    Args:
        team_name: 团队名称
        restore_member: 可选，恢复为 tmux leader 的成员名
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if team.get("leader_type") != "direct":
        return f"❌ 当前 leader 不是直接控制模式，无需 unclaim。"

    team["leader_type"] = ""
    team["leader"] = ""

    if restore_member:
        if restore_member not in team.get("members", {}):
            return f"❌ 成员 '{restore_member}' 不存在。"
        team["leader"] = restore_member
        team["leader_type"] = "tmux"
        team["members"][restore_member]["role"] = "leader"
        _save(data)
        return f"✅ 已释放 leader，'{restore_member}' 恢复为 tmux leader。"

    _save(data)
    return f"✅ 已释放 leader，'{team_name}' 暂无 leader。"


# ============================================================
# Agent MCP 配置工具（用户端）
# ============================================================

@mcp.tool
def setup_codex_mcp(server_name: str = "mult-agent-mcp") -> str:
    """
    注册当前 MCP 服务器到 Codex 的全局配置中。
    使 Codex agent 能够调用 leader_* 等团队协作工具。

    此操作修改 ~/.codex/config.toml（仅添加，不影响已有配置）。

    Args:
        server_name: MCP server 名称，默认 mult-agent-mcp
    """
    if _codex_mcp_registered(server_name):
        return f"✅ Codex MCP '{server_name}' 已注册，无需重复操作。"

    result = _ensure_codex_mcp(server_name)
    return result


@mcp.tool
def remove_codex_mcp(server_name: str = "mult-agent-mcp") -> str:
    """
    从 Codex 配置中移除当前 MCP 服务器。

    Args:
        server_name: MCP server 名称，默认 mult-agent-mcp
    """
    result = _remove_codex_mcp(server_name)
    if result == "not_registered":
        return f"⚠️ Codex MCP '{server_name}' 未注册，无需移除。"
    return result


@mcp.tool
def check_agent_setup(team_name: str) -> str:
    """
    检查团队中各 agent 的 MCP 配置状态。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    lines = [f"🔍 **{team_name}** agent 状态检查"]

    has_claude = False
    has_codex = False
    for name, info in members.items():
        agent = info.get("agent", "claude")
        if _is_claude(agent):
            has_claude = True
        if _is_codex(agent):
            has_codex = True

    # Claude 检查
    if has_claude:
        claude_mcp = _claude_mcp_json_path(team_name)
        claude_ok = os.path.exists(claude_mcp)
        lines.append(f"   Claude MCP: {'✅ ' + claude_mcp if claude_ok else '❌ 未配置（将在 launch 时自动生成）'}")
    else:
        lines.append(f"   Claude: 无 claude agent 成员")

    # Codex 检查
    if has_codex:
        codex_ok = _codex_mcp_registered()
        lines.append(f"   Codex MCP: {'✅ 已注册（全局配置）' if codex_ok else '❌ 未注册 → 请执行 setup_codex_mcp'}")
    else:
        lines.append(f"   Codex: 无 codex agent 成员")

    lines.append(f"\n💡 启动终端时会自动配置所需 MCP。")
    return "\n".join(lines)


@mcp.tool
def get_server_config() -> str:
    """查看 MCP 服务器配置（Claude + Codex 双格式）。"""
    port = _get_server_port()
    url = f"http://localhost:{port}/sse"

    return "\n".join([
        "📋 **MCP 服务器配置**",
        "",
        "### Claude Code（.claude/mcp.json）",
        "```json",
        json.dumps({
            "mcpServers": {
                "mult-agent-mcp": {
                    "type": "sse",
                    "url": url,
                }
            }
        }, indent=2, ensure_ascii=False),
        "```",
        "",
        "### Codex CLI（终端命令）",
        f"```bash",
        f"codex mcp add mult-agent-mcp --url {url}",
        f"```",
        "",
        "### Codex（~/.codex/config.toml）",
        "```toml",
        "[mcp_servers.mult-agent-mcp]",
        'type = "sse"',
        f'url = "{url}"',
        "```",
        "",
        "💡 leader 终端启动时自动配置，无需手动操作。",
    ])


# ============================================================
# 启动终端
# ============================================================

@mcp.tool
def launch_team_terminals(team_name: str, task: str = "") -> str:
    """
    启动团队终端（上下文共享模式）。

    所有成员共享工作目录和 MCP 连接：
    - claude 成员: 从 .team_workspaces/{team}/ 启动，自动加载 .claude/mcp.json
    - codex 成员: 通过全局 codex config 连接 MCP
    - 共享文件区: share_work_space/{team}/ 供所有成员交换文件

    每个成员窗口都可以通过 MCP 工具互相通信。

    Args:
        team_name: 团队名称
        task: 总任务描述
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")

    if not leader and ltype != "direct":
        return f"❌ 请先用 set_leader 指定 leader，或用 claim_leader 接管。"

    members = team.get("members", {})
    if not members:
        return f"❌ 请先用 add_member 添加成员。"

    rc, _, err = _tmux(["-V"])
    if rc != 0:
        return f"❌ tmux 未安装: {err}"

    session = _session(team_name)

    rc, _, _ = _tmux(["has-session", "-t", session])
    if rc == 0:
        _kill_session(team_name)
        time.sleep(0.3)

    # 准备共享工作目录
    team_dir = _team_dir(team_name)
    share_dir = _share_dir(team_name)

    # 为所有成员统一配置 MCP（预配置，各成员窗口启动时自动加载）
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    is_direct = (ltype == "direct")
    mcp_setup_lines = [
        "🔧 上下文共享模式: 所有成员共享工作目录 + MCP 连接",
        f"   📁 工作目录: {team_dir}",
        f"   📂 共享区: {share_dir}",
    ]

    # ================================================================
    # direct 模式: 你是 leader，只创建成员终端
    # ================================================================
    if is_direct:
        created = []

        non_leader_members = [(n, i) for n, i in members.items()]
        if not non_leader_members:
            first_name = leader
            first_agent = members[leader].get("agent", "claude")
            if _is_codex(first_agent):
                cmd = ["new-session", "-d", "-s", session, "-n", first_name + "(m)", first_agent]
            else:
                cmd = ["new-session", "-d", "-s", session, "-n", first_name + "(m)", "-c", team_dir, first_agent]
            rc, _, err = _tmux(cmd)
            if rc != 0:
                return f"❌ 创建终端失败: {err}"
            created.append((first_name, first_agent))
        else:
            first_name, first_info = non_leader_members[0]
            first_agent = first_info.get("agent", "claude")
            if _is_codex(first_agent):
                cmd = ["new-session", "-d", "-s", session, "-n", first_name, first_agent]
            else:
                cmd = ["new-session", "-d", "-s", session, "-n", first_name, "-c", team_dir, first_agent]
            rc, _, err = _tmux(cmd)
            if rc != 0:
                return f"❌ 创建终端失败: {err}"
            created.append((first_name, first_agent))

            for name, info in non_leader_members[1:]:
                agent = info.get("agent", "claude")
                if _is_codex(agent):
                    rc, _, err = _tmux(["new-window", "-t", session, "-n", name, agent])
                else:
                    rc, _, err = _tmux(["new-window", "-t", session, "-n", name, "-c", team_dir, agent])
                if rc == 0:
                    created.append((name, agent))
                time.sleep(0.1)

        team["terminals_active"] = True
        _save(data)

        time.sleep(2)

        task_note = ""
        if task.strip():
            task_note = (
                f"\n📋 总任务:\n   > {task}\n"
                f"\n💡 使用 leader_assign_subtask 分配给成员。\n"
                f"💡 所有成员共享工作目录 ({team_dir})，文件操作互相可见。"
            )

        agent_summary = ", ".join(
            f"{n}({_agent_type(a)}[MCP])" for n, a in created
        )
        return "\n".join([
            f"🚀 **{team_name}** 终端已启动！（直接控制 + 上下文共享模式）",
            f"   session: {session}",
            f"   👑 Leader: **你（当前会话）**",
            f"   👥 成员 ({len(created)}): {agent_summary}",
            "\n".join(mcp_setup_lines),
            task_note,
        ])

    # ================================================================
    # tmux 模式: leader 窗口 + 成员窗口（共享上下文）
    # ================================================================
    leader_agent = members[leader].get("agent", "claude")
    leader_atype = _agent_type(leader_agent)

    mcp_setup_lines.insert(0, f"🔧 Leader agent: {leader_agent} [{leader_atype}]")

    if _is_codex(leader_agent):
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            leader_agent,
        ])
    else:
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", team_dir,
            leader_agent,
        ])

    if rc != 0:
        return f"❌ 创建 leader 终端失败: {err}"
    created = [(leader, leader_agent, f"👑[{leader_atype}][MCP]")]

    # 成员窗口: 从共享工作目录启动
    for name, info in members.items():
        if name == leader:
            continue
        member_agent = info.get("agent", "claude")
        if _is_codex(member_agent):
            rc, _, err = _tmux(["new-window", "-t", session, "-n", name, member_agent])
        else:
            rc, _, err = _tmux(["new-window", "-t", session, "-n", name, "-c", team_dir, member_agent])
        if rc == 0:
            created.append((name, member_agent, f"[{_agent_type(member_agent)}][MCP]"))
        time.sleep(0.1)

    team["terminals_active"] = True
    _save(data)

    time.sleep(2)

    # 发送总任务给 leader
    task_result = ""
    if task.strip():
        rc, err2 = _send_keys(session, leader, task)
        task_result = (
            f"\n📋 总任务已发送给 leader '{leader}' ✅"
            if rc == 0
            else f"\n⚠️ 发送失败: {err2}"
        )

    agent_summary = ", ".join(f"{n}({t})" for n, _, t in created)
    other_count = len(created) - 1

    return "\n".join([
        f"🚀 **{team_name}** 终端已启动！（上下文共享模式）",
        f"   session: {session}",
        f"   窗口: {agent_summary}",
        f"   👑 Leader: {leader} [{leader_atype}]（已连接 MCP）",
        f"   👥 成员: {other_count} 人（已连接 MCP）",
        "",
        "\n".join(mcp_setup_lines),
        "",
        "💡 所有成员共享工作目录，文件操作互相可见",
        "💡 成员可使用 member_report_result 回传结果",
        f"💡 共享文件区: {share_dir}",
        task_result,
    ])


@mcp.tool
def kill_team_terminals(team_name: str) -> str:
    """销毁团队所有终端。"""
    _kill_session(team_name)

    data = _load()
    if team_name in data.get("teams", {}):
        team = data["teams"][team_name]
        team["terminals_active"] = False
        _save(data)
    return f"✅ 团队 '{team_name}' 终端已关闭。"


@mcp.tool
def terminal_status(team_name: str) -> str:
    """查看终端运行状态（含 agent 类型信息）。"""
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    ltype = team.get("leader_type", "")
    members = team.get("members", {})
    session = _find_any_session(team_name)
    if not session:
        if team.get("terminals_active"):
            team["terminals_active"] = False
            _save(data)
        lines = []
        if ltype == "direct":
            lines.append("👑 Leader 模式: **直接控制**（当前会话）")
        lines.append("⚫ 终端未运行。")
        return "\n".join(lines)

    rc, out, _ = _tmux(["list-windows", "-t", session])

    lines = []
    if ltype == "direct":
        lines.append("👑 Leader 模式: **直接控制**（当前会话）")

    if rc != 0:
        if team.get("terminals_active"):
            team["terminals_active"] = False
            _save(data)
        lines.append(f"⚫ 终端未运行。")
        return "\n".join(lines)

    lines += [f"🟢 **{team_name}** 终端运行中", f"   session: {session}"]

    alive_count = 0
    for w in out.split("\n"):
        parts = w.strip().split(None, 1)
        if parts:
            win_name = parts[0]
            agent_info = ""
            in_members = win_name in members
            if in_members:
                agent = members[win_name].get("agent", "")
                if agent:
                    agent_info = f" [{agent} · {_agent_type(agent)}]"
                alive_count += 1
            marker = "👤" if in_members else "❓"
            lines.append(f"   {marker} {w.strip()}{agent_info}")

    total_members = len(members)
    if total_members > 0:
        lines.append(f"\n📊 成员窗口存活: {alive_count}/{total_members}")
    return "\n".join(lines)


@mcp.tool
def member_terminal_status(team_name: str) -> str:
    """
    查看每个成员的终端窗口存活状态。
    返回每个成员是否在 tmux 中有对应的存活窗口。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    if not members:
        return "📭 该团队暂无成员。"

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    session = _find_any_session(team_name)

    alive_windows = set()
    if session:
        rc, out, _ = _tmux(["list-windows", "-t", session, "-F", "#{window_name}"])
        if rc == 0 and out:
            alive_windows = set(out.split("\n"))

    lines = [f"👥 **{team_name}** 成员终端状态:"]

    alive_count = 0
    sleeping_count = 0
    for name in members:
        alive = name in alive_windows
        if alive:
            alive_count += 1

        if alive:
            icon = "🟢"
        elif members[name].get("last_task") and members[name].get("last_task_completed", True):
            icon = "😴"
            sleeping_count += 1
        else:
            icon = "⚫"

        role = members[name].get("role", "")
        agent = members[name].get("agent", "claude")
        atype = _agent_type(agent)
        is_ldr = " 👑Leader" if (name == leader and ltype == "tmux") else ""
        role_str = f" [{role}]" if role else ""

        lines.append(f"  {icon} **{name}**{is_ldr}{role_str}  {agent}[{atype}]")

    lines.append(f"\n📊 🟢{alive_count} 😴{sleeping_count} ⚫{len(members) - alive_count - sleeping_count} / 总计 {len(members)}")

    if ltype == "direct":
        lines.append("👑 Leader 模式: 直接控制（当前会话）")

    return "\n".join(lines)


# ============================================================
# Leader 端工具
# ============================================================

@mcp.tool
def leader_list_team(team_name: str) -> str:
    """
    [Leader] 查看团队完整信息。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    members = team.get("members", {})
    terminals = "🟢 运行中" if team.get("terminals_active") else "⚫ 未启动"

    if ltype == "direct":
        leader_str = "你（当前会话 · 直接控制）"
    elif leader:
        leader_alive = _tmux_session_alive(team_name) and _tmux_window_exists(team_name, leader)
        leader_str = f"{leader} (tmux {'🟢存活' if leader_alive else '💀已死'})"
    else:
        leader_str = "未设置"

    lines = [
        f"📋 **{team_name}** 团队面板  [{terminals}]",
        f"   👑 Leader: {leader_str}",
        f"   👥 成员 ({len(members)} 人):",
    ]
    for name, info in members.items():
        role = info.get("role", "")
        agent = info.get("agent", "claude")
        atype = _agent_type(agent)
        is_ldr = " 👑LEADER" if (name == leader and ltype == "tmux") else ""
        role_str = f" [{role}]" if role else ""
        lines.append(f"     • {name}{is_ldr}{role_str}  {agent}[{atype}]")
    return "\n".join(lines)


@mcp.tool
def leader_assign_subtask(
    team_name: str,
    member_name: str,
    subtask: str,
    context: str = "",
) -> str:
    """
    [Leader] 向指定成员分配子任务。

    通过 tmux send-keys 将子任务文本发送到成员终端。
    如果成员终端已退出，自动重新拉起并发送任务（自动恢复）。
    任务会持久化到成员数据中，恢复后自动重发。

    Args:
        team_name: 团队名称
        member_name: 目标成员名称
        subtask: 子任务描述
        context: 可选上下文
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if not team.get("terminals_active"):
        return f"❌ 终端未启动，请先 launch_team_terminals。"

    members = team.get("members", {})
    if member_name not in members:
        return f"❌ 成员 '{member_name}' 不存在。可用 leader_list_team 查看。"

    ltype = team.get("leader_type", "")
    leader = team.get("leader", "")

    if ltype == "tmux" and member_name == leader:
        return f"⚠️ '{member_name}' 是你自己（tmux leader）。请直接在当前终端执行。"

    # 持久化任务（恢复时自动重发）
    full_msg = subtask
    if context.strip():
        full_msg = f"[上下文] {context}\n[子任务] {subtask}"
    members[member_name]["last_task"] = subtask
    members[member_name]["last_context"] = context
    members[member_name]["last_task_completed"] = False
    _save(data)

    session = _find_any_session(team_name)
    if not session:
        _save(data)
        return f"❌ 未找到运行中的终端 session。"

    # ---- 自动恢复：成员窗口不存在时先拉起 ----
    recovery_msg = ""
    if not _tmux_window_exists(team_name, member_name):
        agent = members[member_name].get("agent", "claude")
        team_dir = _team_dir(team_name)
        _write_claude_mcp(team_name)
        _ensure_codex_mcp()

        if _is_codex(agent):
            rc, _, err = _tmux(["new-window", "-t", session, "-n", member_name, agent])
        else:
            rc, _, err = _tmux(["new-window", "-t", session, "-n", member_name, "-c", team_dir, agent])

        if rc != 0:
            return f"❌ 成员终端已死且恢复失败: {err}"
        # 等待新进程就绪
        time.sleep(1.5)
        recovery_msg = f"🔄 成员 '{member_name}' 已自动恢复\n"

    rc, err = _send_keys(session, member_name, full_msg)
    if rc != 0:
        return f"❌ 发送失败: {err}{' (已恢复)' if recovery_msg else ''}"

    member_agent = members[member_name].get("agent", "claude")
    atype = _agent_type(member_agent)
    return f"{recovery_msg}✅ 子任务已分配给 '{member_name}' [{atype}] → {subtask[:60]}..."


@mcp.tool
def leader_broadcast(team_name: str, message: str) -> str:
    """
    [Leader] 向所有非 leader 成员广播消息。
    对于已退出的成员自动拉起终端再发送。

    Args:
        team_name: 团队名称
        message: 广播内容
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if not team.get("terminals_active"):
        return f"❌ 终端未启动。"

    ltype = team.get("leader_type", "")
    leader = team.get("leader", "")
    members = team.get("members", {})
    session = _find_any_session(team_name)
    if not session:
        return "❌ 未找到运行中的终端 session。"

    recovered = []
    results = []
    for name in members:
        if ltype == "tmux" and name == leader:
            continue

        # 自动恢复死掉的成员窗口
        if not _tmux_window_exists(team_name, name):
            agent = members[name].get("agent", "claude")
            team_dir = _team_dir(team_name)
            _write_claude_mcp(team_name)
            _ensure_codex_mcp()
            if _is_codex(agent):
                rc_r, _, _ = _tmux(["new-window", "-t", session, "-n", name, agent])
            else:
                rc_r, _, _ = _tmux(["new-window", "-t", session, "-n", name, "-c", team_dir, agent])
            if rc_r == 0:
                recovered.append(name)
                time.sleep(0.3)
            else:
                results.append(f"  ❌ {name} (恢复失败)")
                continue

        rc, _ = _send_keys(session, name, message)
        results.append(f"  {'✅' if rc == 0 else '❌'} {name}")
        time.sleep(0.05)

    if not results and not recovered:
        return "⚠️ 没有可广播的成员终端。"

    extra = ""
    if recovered:
        extra = f"\n🔄 自动恢复: {', '.join(recovered)}"

    count = sum(1 for r in results if "✅" in r)
    return f"📣 已广播至 {count}/{len(results)} 人:{extra}\n" + "\n".join(results)


@mcp.tool
def leader_add_member(
    team_name: str,
    member_name: str,
    role: str = "",
    agent: str = "",
) -> str:
    """
    [Leader] 动态添加成员 + 创建终端窗口。

    成员 agent 为空时继承团队默认 agent。

    Args:
        team_name: 团队名称
        member_name: 新成员名称
        role: 角色
        agent: 启动命令（claude/codex/自定义，空=继承默认）
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if member_name in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 已存在。"

    if not team.get("terminals_active"):
        return f"❌ 终端未启动。"

    actual_agent = agent if agent else team.get("default_agent", "claude")
    atype = _agent_type(actual_agent)

    team["members"][member_name] = {
        "role": role,
        "model": "",
        "agent": actual_agent,
        "last_task": "",
        "last_context": "",
        "last_task_completed": True,
    }
    _save(data)

    session = _session(team_name)
    rc, _, err = _tmux([
        "new-window", "-t", session, "-n", member_name, actual_agent,
    ])
    if rc != 0:
        return f"⚠️ 成员已记录但终端创建失败: {err}"

    return f"✅ 新成员 '{member_name}' 已加入（role={role}, agent={actual_agent}[{atype}]），终端已启动。"


@mcp.tool
def leader_remove_member(team_name: str, member_name: str) -> str:
    """
    [Leader] 移除成员 + 关闭其终端窗口。

    Args:
        team_name: 团队名称
        member_name: 要移除的成员
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")

    if ltype == "tmux" and member_name == leader:
        return f"❌ '{member_name}' 是 tmux leader，不能移除。请先用 claim_leader 接管。"

    if member_name not in team.get("members", {}):
        return f"❌ 成员不存在。"

    del team["members"][member_name]
    _save(data)

    session = _session(team_name)
    _tmux(["kill-window", "-t", f"{session}:{member_name}"])

    return f"✅ 成员 '{member_name}' 已移除。"


@mcp.tool
def leader_redefine_member(
    team_name: str,
    member_name: str,
    role: str = "",
    agent: str = "",
) -> str:
    """
    [Leader] 修改成员角色 和 / 或 agent。

    Args:
        team_name: 团队名称
        member_name: 成员名称
        role: 新角色（空=不改）
        agent: 新 agent claude/codex/自定义（空=不改）
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if member_name not in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 不存在。"

    m = team["members"][member_name]
    changes = []
    if role:
        m["role"] = role
        changes.append(f"role → {role}")
    if agent:
        m["agent"] = agent
        changes.append(f"agent → {agent}[{_agent_type(agent)}]")

    if not changes:
        return "⚠️ 未提供任何修改项。"

    _save(data)
    return f"✅ 成员 '{member_name}' 已更新: {', '.join(changes)}。"


@mcp.tool
def leader_launch_member_terminal(team_name: str, member_name: str) -> str:
    """
    [Leader] 为已有成员单独启动终端窗口。
    成员从共享工作目录启动，自动加载 MCP 配置。
    如果成员有上次未完成的任务（last_task），自动重新发送。

    Args:
        team_name: 团队名称
        member_name: 成员名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if not team.get("terminals_active"):
        return f"❌ 主 session 未启动。"

    members = team.get("members", {})
    if member_name not in members:
        return f"❌ 成员 '{member_name}' 不存在。"

    session = _find_any_session(team_name)
    if not session:
        return f"❌ 未找到运行中的终端 session。"

    agent = members[member_name].get("agent", "claude")
    atype = _agent_type(agent)
    team_dir = _team_dir(team_name)

    # 确保 MCP 配置就绪
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    if _is_codex(agent):
        rc, _, err = _tmux(["new-window", "-t", session, "-n", member_name, agent])
    else:
        rc, _, err = _tmux(["new-window", "-t", session, "-n", member_name, "-c", team_dir, agent])
    if rc != 0:
        return f"❌ 创建终端失败: {err}"

    # 等待进程就绪
    time.sleep(1.5)

    # ---- 自动恢复上次未完成任务 ----
    last_task = members[member_name].get("last_task", "")
    task_completed = members[member_name].get("last_task_completed", True)
    extra_lines = []
    if last_task and not task_completed:
        last_context = members[member_name].get("last_context", "")
        full_msg = last_task
        if last_context:
            full_msg = f"[上下文] {last_context}\n[子任务] {last_task}"
        rc2, err2 = _send_keys(session, member_name, full_msg)
        if rc2 == 0:
            extra_lines.append(f"🔄 已自动重发未完成任务: {last_task[:60]}...")
        else:
            extra_lines.append(f"⚠️ 任务重发失败: {err2}")
    elif last_task and task_completed:
        extra_lines.append(f"✅ 上次任务已完成，不再重发: {last_task[:40]}...")

    result = f"✅ 成员 '{member_name}' 终端已启动（agent={agent}[{atype}], 共享上下文）。"
    if extra_lines:
        result += "\n" + "\n".join(extra_lines)
    return result


# ============================================================
# 成员协作工具（所有连接 MCP 的成员均可调用）
# ============================================================

@mcp.tool
def member_report_result(
    team_name: str,
    result: str,
    artifact_path: str = "",
    member_name: str = "",
) -> str:
    """
    [成员] 将任务结果回传给 leader 或其他成员。
    结果会写入共享区的 result.jsonl，供所有成员读取。
    提供 member_name 时会将该成员的终端退出进入休眠状态，
    等待 leader 下发新任务时自动唤醒。

    Args:
        team_name: 团队名称
        result: 任务结果摘要
        artifact_path: 可选，产出文件在共享区内的路径
        member_name: 可选，上报结果的成员名称（用于标记任务完成并休眠）
    """
    import datetime
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    # 标记任务完成
    task_msg = ""
    sleep_msg = ""
    if member_name:
        members = team.get("members", {})
        if member_name in members:
            if members[member_name].get("last_task"):
                members[member_name]["last_task_completed"] = True
                _save(data)
                task_msg = f"\n✅ 成员 '{member_name}' 的任务已标记为完成"

            # 成员完成任务后自动退出终端进入休眠
            # 下次 leader 分配新任务时会自动唤醒
            if not _is_leader(team, member_name) and _tmux_window_exists(team_name, member_name):
                _tmux(["kill-window", "-t", f"{_find_any_session(team_name)}:{member_name}"])
                sleep_msg = f"\n😴 成员 '{member_name}' 已进入休眠，等待新任务唤醒"

    share_dir = _share_dir(team_name)
    results_file = os.path.join(share_dir, "results.jsonl")

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "member": member_name or "unknown",
        "result": result,
        "artifact_path": artifact_path,
    }
    try:
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return f"✅ 结果已记录到共享区{task_msg}{sleep_msg}\n📄 {results_file}\n💡 其他成员可调用 member_read_shared 查看。"
    except Exception as e:
        return f"❌ 写入失败: {e}"


def _is_leader(team: dict, member_name: str) -> bool:
    """判断成员是否为团队 leader"""
    return team.get("leader") == member_name and team.get("leader_type") == "tmux"


@mcp.tool
def member_read_shared(team_name: str) -> str:
    """
    [成员] 读取共享区中的最新结果。
    返回 results.jsonl 中最近 10 条记录。

    Args:
        team_name: 团队名称
    """
    share_dir = _share_dir(team_name)
    results_file = os.path.join(share_dir, "results.jsonl")

    if not os.path.exists(results_file):
        return "📭 共享区暂无结果。"

    try:
        with open(results_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        recent = lines[-10:]
        entries = [json.loads(line) for line in recent]

        out = [f"📋 **{team_name}** 共享区最新结果 ({len(entries)} 条):"]
        for i, e in enumerate(entries, 1):
            ts = e.get("timestamp", "")[:19]
            result_text = e.get("result", "")
            artifact = e.get("artifact_path", "")
            line = f"  {i}. [{ts}] {result_text}"
            if artifact:
                line += f"\n     📎 {artifact}"
            out.append(line)
        return "\n".join(out)
    except Exception as e:
        return f"❌ 读取失败: {e}"


@mcp.tool
def member_send_message(
    team_name: str,
    target_member: str,
    message: str,
) -> str:
    """
    [成员] 向团队中另一个成员发送消息。
    通过 tmux send-keys 将消息文本注入目标成员的终端。

    Args:
        team_name: 团队名称
        target_member: 目标成员名称（或 "leader" 发送给 leader）
        message: 消息内容
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if not team.get("terminals_active"):
        return f"❌ 终端未启动，无法发送消息。"

    members = team.get("members", {})
    if target_member not in members and target_member != "leader":
        return f"❌ 成员 '{target_member}' 不存在。"

    # 解析目标：如果 target_member 是 "leader"，取实际的 leader 名
    actual_target = target_member
    if target_member == "leader":
        leader = team.get("leader", "")
        if not leader:
            return "❌ 未指定 leader。"
        actual_target = leader

    session = _find_any_session(team_name)
    if not session:
        return "❌ 未找到运行中的终端 session。"

    if not _tmux_window_exists(team_name, actual_target):
        return f"❌ 成员 '{actual_target}' 的终端窗口不存在。"

    full_msg = f"[来自其他成员的消息] {message}"
    rc, err = _send_keys(session, actual_target, full_msg)
    if rc != 0:
        return f"❌ 发送失败: {err}"

    return f"✅ 消息已发送给 '{actual_target}'"


@mcp.tool
def member_list_shared_files(team_name: str) -> str:
    """
    [成员] 列出共享区中的所有文件。

    Args:
        team_name: 团队名称
    """
    share_dir = _share_dir(team_name)

    try:
        files = []
        for root, _dirs, filenames in os.walk(share_dir):
            for fname in filenames:
                fpath = os.path.join(root, fname)
                rel = os.path.relpath(fpath, share_dir)
                size = os.path.getsize(fpath)
                files.append((rel, size))
    except Exception as e:
        return f"❌ 列出文件失败: {e}"

    if not files:
        return f"📭 共享区为空\n📂 {share_dir}"

    lines = [f"📂 **{team_name}** 共享区文件:", f"   {share_dir}", ""]
    for rel, size in files:
        if size < 1024:
            size_str = f"{size}B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f}KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f}MB"
        lines.append(f"   📄 {rel} ({size_str})")
    return "\n".join(lines)


def main():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
