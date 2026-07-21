"""
Multi-Agent MCP — 共享 Tmux 工具函数
====================================

供 MCP Server 与 TUI 共用的 tmux 操作底层函数。
使用绝对路径查找 tmux 可执行文件，避免 PATH 不完整。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from common.data_layer import load_data, save_data
from common.leader_recovery import build_leader_recovery_section

AUTHORIZATION_MUTEX = threading.Lock()
CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__member_*",
    "mcp__mult_agent_mcp__member_*",
]


# ============================================================
# tmux 路径查找（缓存）
# ============================================================

def find_tmux() -> str | None:
    """查找 tmux 可执行文件路径，避免 MCP 服务进程 PATH 不完整导致误判。"""
    if not hasattr(find_tmux, "_cache"):
        find_tmux._cache = shutil.which("tmux")  # type: ignore[attr-defined]
        if not find_tmux._cache:
            for p in ("/usr/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux"):
                if os.path.exists(p):
                    find_tmux._cache = p  # type: ignore[attr-defined]
                    break
    return find_tmux._cache  # type: ignore[attr-defined]


def tmux_run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """执行 tmux 命令，返回 (returncode, stdout, stderr)。"""
    tmux_path = find_tmux()
    if not tmux_path:
        return -1, "", "tmux 未安装，请执行 sudo apt install tmux"
    try:
        r = subprocess.run(
            [tmux_path] + cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "tmux 未安装"
    except subprocess.TimeoutExpired:
        return -1, "", "tmux 命令超时"


def run_command(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    """执行任意命令，返回 (returncode, stdout, stderr)。"""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "命令未找到"
    except subprocess.TimeoutExpired:
        return -1, "", "命令超时"


# ============================================================
# session 命名与查找
# ============================================================

def tmux_session_name(team: str) -> str:
    """返回 MCP server 格式的 session 名: mcp_{team}"""
    return f"mcp_{team}"


def find_tmux_session(team: str) -> str | None:
    """
    查找团队的 tmux session，支持两种命名格式：
      1. mcp_{team}           (MCP server 创建，无时间戳)
      2. mcp_{team}_HHMMSS    (TUI 创建，带时间戳)
    如果有多个匹配项，优先返回精确匹配（无时间戳），其次返回最新的。
    """
    session = tmux_session_name(team)
    candidates: list[str] = []
    rc, _, _ = tmux_run(["has-session", "-t", session])
    if rc == 0:
        candidates.append(session)

    rc, out, _ = tmux_run(["list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        prefix = f"mcp_{team}_"
        for name in out.split("\n"):
            if name.startswith(prefix) and name not in candidates:
                candidates.append(name)

    if not candidates:
        return None

    members = load_data().get("teams", {}).get(team, {}).get("members", {})
    if members:
        scored = [(_session_member_match_count(team, candidate, members), candidate) for candidate in candidates]
        best_score, best_session = max(scored, key=lambda item: item[0])
        if best_score > 0:
            return best_session

    if session in candidates:
        return session
    return candidates[-1]


def _session_member_match_count(team: str, session: str, members: dict) -> int:
    records = tmux_window_records(session)
    if not records:
        return 0
    names = {r["name"] for r in records}
    ids = {r["id"] for r in records}
    current_session_id = records[0].get("session_id", "")
    current_session_created = records[0].get("session_created", "")
    score = 0
    for member_name, member in members.items():
        stored_id = member.get("tmux_window_id", "")
        stored_session = member.get("tmux_session", "")
        stored_session_id = member.get("tmux_session_id", "")
        stored_session_created = member.get("tmux_session_created", "")
        if (
            stored_id
            and stored_id in ids
            and stored_session == session
            and stored_session_id == current_session_id
            and stored_session_created == current_session_created
        ):
            score += 1
        elif member_name in names:
            score += 1
    return score


def tmux_session_alive(team: str) -> bool:
    """检查团队是否有存活的 tmux session。"""
    return find_tmux_session(team) is not None


def tmux_target(session: str, window: str) -> str:
    return window if window.startswith("@") else f"{session}:{window}"


def tmux_window_records(session: str) -> list[dict[str, str]]:
    rc, out, _ = tmux_run([
        "list-windows",
        "-t",
        session,
        "-F",
        "#{session_id}\t#{session_created}\t#{window_id}\t#{window_name}",
    ])
    if rc != 0 or not out:
        return []
    records: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) >= 4:
            session_id, session_created, window_id, name = parts
        else:
            session_id = ""
            session_created = ""
            window_id, _, name = line.partition("\t")
        if window_id:
            records.append({
                "id": window_id,
                "name": name,
                "session_id": session_id,
                "session_created": session_created,
            })
    return records


def remember_member_window_id(team_name: str, member_name: str, session: str, window_name: str | None = None) -> str:
    records = tmux_window_records(session)
    preferred_name = window_name or member_name
    record = next((r for r in records if r["name"] == preferred_name), None)
    if record is None and window_name and window_name != member_name:
        record = next((r for r in records if r["name"] == member_name), None)
    if record is None:
        return ""

    data = load_data()
    member = data.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name)
    if not member:
        return ""
    member["tmux_window_id"] = record["id"]
    member["tmux_window_name"] = record["name"]
    member["tmux_session"] = session
    member["tmux_session_id"] = record.get("session_id", "")
    member["tmux_session_created"] = record.get("session_created", "")
    save_data(data)
    return record["id"]


def member_window_target(team_name: str, member_name: str) -> str | None:
    session = find_tmux_session(team_name)
    if not session:
        return None
    records = tmux_window_records(session)
    if not records:
        return None

    member = load_data().get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
    stored_id = member.get("tmux_window_id", "")
    stored_session = member.get("tmux_session", "")
    stored_session_id = member.get("tmux_session_id", "")
    stored_session_created = member.get("tmux_session_created", "")
    current_session_id = records[0].get("session_id", "")
    current_session_created = records[0].get("session_created", "")
    same_session_instance = (
        stored_session == session
        and bool(stored_session_id)
        and bool(stored_session_created)
        and stored_session_id == current_session_id
        and stored_session_created == current_session_created
    )
    if stored_id and same_session_instance and any(r["id"] == stored_id for r in records):
        return stored_id

    by_name = next((r for r in records if r["name"] == member_name), None)
    if by_name:
        remember_member_window_id(team_name, member_name, session, member_name)
        return by_name["id"]
    return None


def sync_team_terminal_state(team_name: str) -> bool:
    """Reconcile persisted terminals_active with the actual tmux session state."""
    alive = find_tmux_session(team_name) is not None
    data = load_data()
    team = data.get("teams", {}).get(team_name)
    if team is not None and bool(team.get("terminals_active")) != alive:
        team["terminals_active"] = alive
        save_data(data)
    return alive


def tmux_window_exists(team: str, window: str) -> bool:
    """检查指定窗口是否存在于团队的 tmux session 中。"""
    return member_window_target(team, window) is not None


def get_member_terminal_status(team_name: str) -> dict[str, bool]:
    """
    返回团队中每个成员的 tmux 窗口存活状态。
    返回: {member_name: True/False, ...}
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    if not members:
        return {}

    session = find_tmux_session(team_name)
    if not session:
        sync_team_terminal_state(team_name)
        return {name: False for name in members}

    records = tmux_window_records(session)
    if not records:
        sync_team_terminal_state(team_name)
        return {name: False for name in members}

    sync_team_terminal_state(team_name)
    return {name: member_window_target(team_name, name) is not None for name in members}


# ============================================================
# session / 窗口操作
# ============================================================

def send_keys(
    session: str,
    window: str,
    text: str,
    *,
    send_enter: bool = True,
    literal_keys: bool = False,
) -> tuple[int, str]:
    """向 tmux 窗口发送按键。

    Args:
        session: tmux session 名
        window: tmux window 名
        text: 要发送的文本
        send_enter: 是否在文本后追加 Enter 键
        literal_keys: True=将 text 作为字面按键序列逐字发送
    """
    target = tmux_target(session, window)
    if literal_keys:
        rc, _, err = tmux_run(["send-keys", "-t", target] + list(text))
    else:
        rc, _, err = tmux_run(["send-keys", "-t", target, "-l", text])
    if rc != 0:
        return rc, err
    if send_enter:
        rc, _, err = tmux_run(["send-keys", "-t", target, "Enter"])
    return rc, err if rc != 0 else ""


def send_authorization_choice(session: str, window: str, choice_key: str | None) -> tuple[int, str]:
    """向成员终端发送授权按键选择。"""
    target = tmux_target(session, window)
    keys = ["Enter"] if choice_key is None else [choice_key, "Enter"]
    last_rc = 0
    last_err = ""
    with AUTHORIZATION_MUTEX:
        for attempt in range(2):
            last_rc, _, last_err = tmux_run(["send-keys", "-t", target, *keys])
            if last_rc == 0:
                time.sleep(0.12)
                return 0, ""
            if attempt == 0:
                time.sleep(0.1)
    return last_rc, last_err


def authorization_choice_key(choice: str) -> str | None:
    """解析授权选项字符串为数字键。"""
    normalized = (choice or "yes").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "1",
        "1": "1", "yes": "1", "y": "1", "approve": "1", "allow": "1", "once": "1",
        "2": "2", "session": "2", "remember": "2", "allow_session": "2",
        "yes_session": "2", "dont_ask_again": "2", "don't_ask_again": "2",
        "3": "3",
    }
    if normalized == "enter":
        return None
    return aliases.get(normalized)


def capture_window(session: str, window: str, lines: int = 80) -> tuple[int, str, str]:
    """捕获 tmux 窗口最近 N 行输出。"""
    line_count = max(10, min(int(lines), 500))
    return tmux_run(["capture-pane", "-t", tmux_target(session, window), "-p", "-S", f"-{line_count}"])


def kill_session(team: str) -> None:
    """销毁团队的 tmux session。"""
    session = find_tmux_session(team)
    if session:
        tmux_run(["kill-session", "-t", session])


# ============================================================
# TUI 辅助函数
# ============================================================

def current_tmux_session() -> str | None:
    """返回 TUI 当前所在 tmux session；不在 tmux 中则返回 None。"""
    if not os.environ.get("TMUX"):
        return None
    rc, out, _ = tmux_run(["display-message", "-p", "#{session_name}"])
    if rc != 0:
        return None
    return out.strip() or None


# ============================================================
# Agent 类型检测
# ============================================================

def agent_type(agent_cmd: str) -> str:
    """根据 agent 启动命令识别 agent 类型: 'claude' | 'codex' | 'other'"""
    cmd = agent_cmd.lower().strip()
    if "codex" in cmd:
        return "codex"
    if "claude" in cmd:
        return "claude"
    return "other"


def is_codex(agent_cmd: str) -> bool:
    return agent_type(agent_cmd) == "codex"


def is_claude(agent_cmd: str) -> bool:
    return agent_type(agent_cmd) == "claude"


def normalize_member_mode(mode: str) -> str:
    normalized = (mode or "manual").strip().lower().replace("-", "_")
    aliases = {
        "": "manual",
        "default": "manual",
        "manual": "manual",
        "ask": "manual",
        "auto": "auto",
        "accept": "auto",
        "accept_edits": "auto",
        "never": "auto",
        "plan": "plan",
        "planning": "plan",
        "readonly": "plan",
        "read_only": "plan",
    }
    return aliases.get(normalized, "")


def member_mode(member_info: dict) -> str:
    return normalize_member_mode(member_info.get("work_mode") or member_info.get("mode") or "manual") or "manual"


# ============================================================
# Agent 启动命令构造
# ============================================================

def codex_mode_args(mode: str) -> list[str]:
    normalized = normalize_member_mode(mode)
    if normalized == "auto":
        return ["--ask-for-approval", "never"]
    if normalized == "plan":
        return ["--ask-for-approval", "on-request"]
    return []


def claude_agent_args(
    agent_cmd: str,
    mode: str,
    *,
    dangerously_skip_permissions: bool = False,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    args = [agent_cmd]
    normalized = normalize_member_mode(mode)
    if dangerously_skip_permissions:
        args.append("--dangerously-skip-permissions")
    elif normalized in {"auto", "plan"}:
        args.extend(["--permission-mode", normalized])
    if allowed_tools:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    return args


def codex_command(agent_cmd: str, team_dir: str, prompt: str = "", member_mode: str = "") -> list[str]:
    """构造 codex 成员启动命令。"""
    cmd = [agent_cmd, "-C", team_dir]
    cmd.extend(codex_mode_args(member_mode))
    if prompt:
        cmd.append(prompt)
    return cmd


def leader_system_prompt(team_name: str, task: str = "") -> str:
    """生成 tmux leader 的初始系统提示。"""
    from common.config import default_workspace_dir, context_base_dir

    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    leader = team.get("leader", "")
    leader_info = members.get(leader, {}) if leader else {}
    leader_role = leader_info.get("role") or "leader"
    leader_agent = leader_info.get("agent") or team.get("default_agent", "claude")
    default_member_agent = (team.get("default_agent") or "claude").strip() or "claude"
    teammates = [
        f"{name}(role={info.get('role') or 'member'}, agent={info.get('agent') or team.get('default_agent', 'claude')})"
        for name, info in members.items()
        if name != leader
    ]

    team_dir = team.get("workspace_dir") or default_workspace_dir()
    share_dir = team.get("context_dir") or str(context_base_dir() / team_name)

    lines = [
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的 leader。",
        f"你的团队成员身份: member_name='{leader or '(未设置)'}', role='{leader_role}', agent='{leader_agent}'。",
        f"leader_list_team 中名为 '{leader or '(未设置)'}' 且标记为 leader 的成员记录就是你本人，不是外部成员。",
        "不要把自己的 leader 成员记录当作可分配对象；不要向自己分配子任务，也不要为了排除自己而剔除 leader 身份。",
        f"创建新成员时默认必须使用团队 default_agent='{default_member_agent}'；不要把你自己的 agent='{leader_agent}' 当作新成员默认 agent。",
        "只有用户明确要求覆盖 agent 时，才在 add_member/leader_add_member 中设置 use_explicit_agent=True。",
        "必须使用本项目 MCP 工具协调已有团队成员，不要使用 Codex 内置 spawn_agent / sub-agent 代替团队成员。",
        "开始后先调用 leader_list_team 查看成员，再用 leader_assign_subtask、leader_broadcast 等 leader_* 工具分配任务。",
        f"团队共享工作目录: {team_dir}",
        f"团队共享上下文区: {share_dir}",
    ]
    if teammates:
        lines.append("已有可分配成员（不包含你）: " + "; ".join(teammates))
    else:
        lines.append("已有可分配成员（不包含你）: 暂无。")
    if task.strip():
        lines.extend(["", "总任务:", task.strip()])
    lines.extend(build_leader_recovery_section(team_name, team, team_dir, share_dir))
    return "\n".join(lines)


def tmux_spawn_member(
    session: str,
    member_name: str,
    agent: str,
    team_dir: str,
    *,
    new_session: bool = False,
    window_name: str | None = None,
    dangerously_skip_permissions: bool = False,
    team_name_for_permissions: str = "",
) -> tuple[int, str, str]:
    """启动成员 tmux 窗口，统一处理 workspace 与 agent 类型差异。

    对于 claude 成员，自动写入 .claude/settings.json 预配置权限以减少审批阻塞。
    """
    name = window_name or member_name
    if new_session:
        cmd = ["new-session", "-d", "-s", session, "-n", name]
    else:
        cmd = ["new-window", "-t", session, "-n", name]

    member_info = {}
    if team_name_for_permissions:
        data = load_data()
        member_info = data.get("teams", {}).get(team_name_for_permissions, {}).get("members", {}).get(member_name, {})
    mode = member_mode(member_info)

    if is_codex(agent):
        cmd.extend(codex_command(agent, team_dir, member_mode=mode))
    else:
        # Claude / 其他 agent: 预配置权限 + 从共享工作目录启动
        if team_name_for_permissions:
            _write_claude_permissions_internal(
                team_name_for_permissions,
                str(Path(team_dir)),
                dangerously_skip=dangerously_skip_permissions,
            )

        agent_args = claude_agent_args(
            agent,
            mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
        )
        cmd.extend(["-c", team_dir] + agent_args)

    return tmux_run(cmd)


# ---- 内部权限写入辅助 ----

def _write_claude_permissions_internal(
    team_name: str,
    team_dir_str: str,
    *,
    dangerously_skip: bool = False,
    allow_patterns: list[str] | None = None,
    additional_dirs: list[str] | None = None,
) -> str:
    """为团队的 Claude Code 成员预配置权限策略（内部函数，写入 .claude/settings.json）。"""
    import json

    claude_dir = Path(team_dir_str) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    settings_path = claude_dir / "settings.json"

    permissions_config: dict = {}

    if dangerously_skip:
        permissions_config["allow-dangerously-skip-permissions"] = True
    else:
        allow: list[str] = list(allow_patterns or [])
        allow.extend([
            f"Edit({team_dir_str}/*)",
            f"Write({team_dir_str}/*)",
            "Bash(git:*)",
            *CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS,
        ])
        if additional_dirs:
            for d in additional_dirs:
                allow.extend([
                    f"Edit({d}/*)",
                    f"Write({d}/*)",
                ])
        permissions_config["allow"] = allow

    settings = {"permissions": permissions_config}
    with open(settings_path, "w", encoding="utf-8") as f:
        json.dump(settings, f, indent=2, ensure_ascii=False)
    return str(settings_path)
