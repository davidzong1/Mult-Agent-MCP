from fastmcp import FastMCP
import json
import os
import shutil
import subprocess
import threading
import time

from member_status import format_member_activity_status

mcp = FastMCP("mult agent mcp")
TEAM_DATA_LOCK = threading.RLock()
FILE_LOCK_MUTEX = threading.Lock()
AUTHORIZATION_MUTEX = threading.Lock()
TEAM_MONITOR_THREADS: dict[str, threading.Thread] = {}
TEAM_MONITOR_STOP_EVENTS: dict[str, threading.Event] = {}
MCP_SERVER_NAME = "mult-agent-mcp"
DELETED_LEGACY_TEAMS_KEY = "_deleted_legacy_teams"

# ============================================================
# 数据层
# ============================================================
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))

# ---- 持久化根目录 ----
def _mcp_home() -> str:
    env = os.environ.get("MULT_AGENT_MCP_HOME", "").strip()
    if env:
        return os.path.abspath(os.path.expanduser(env))
    return os.path.join(os.path.expanduser("~"), ".mult_agent_mcp")

MCP_HOME = _mcp_home()

# ---- 路径常量 ----
DATA_FILE = os.path.join(MCP_HOME, "teams_data.json")
TEAM_WORKSPACES_DIR = os.path.join(PROJECT_DIR, ".team_workspaces")
SHARE_CONTEXT_DIR = os.path.join(MCP_HOME, "contexts")
SHARE_WORKSPACE_DIR = os.path.join(PROJECT_DIR, "share_work_space")
CLAUDE_GLOBAL_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude.json")
CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__leader_*",
    "mcp__mult_agent_mcp__leader_*",
]
CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__member_*",
    "mcp__mult_agent_mcp__member_*",
]

# ---- 旧路径（向后兼容迁移用） ----
_OLD_DATA_FILE = os.path.join(PROJECT_DIR, "teams_data.json")
_OLD_SHARE_CONTEXT_DIR = os.path.join(PROJECT_DIR, "share_context_space")


def _migrate_if_needed() -> None:
    """Merge legacy PROJECT_DIR data into the canonical MCP home data file."""
    if not os.path.exists(_OLD_DATA_FILE):
        return

    os.makedirs(MCP_HOME, exist_ok=True)

    if not os.path.exists(DATA_FILE):
        shutil.copy2(_OLD_DATA_FILE, DATA_FILE)

    try:
        with open(_OLD_DATA_FILE, "r", encoding="utf-8") as f:
            legacy_data = json.load(f)
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    changed = False
    deleted_legacy_teams = data.get(DELETED_LEGACY_TEAMS_KEY, {})
    if not isinstance(deleted_legacy_teams, dict):
        deleted_legacy_teams = {}

    for team_name, legacy_team in legacy_data.get("teams", {}).items():
        if team_name in deleted_legacy_teams:
            continue
        teams = data.setdefault("teams", {})
        if team_name not in teams:
            teams[team_name] = legacy_team
            changed = True
            continue

        team = teams[team_name]
        for key, value in legacy_team.items():
            if key == "members":
                members = team.setdefault("members", {})
                for member_name, legacy_member in value.items():
                    if member_name not in members:
                        members[member_name] = legacy_member
                        changed = True
                    else:
                        for member_key, member_value in legacy_member.items():
                            if member_key not in members[member_name]:
                                members[member_name][member_key] = member_value
                                changed = True
            elif key not in team:
                team[key] = value
                changed = True

    for team_name, team in data.get("teams", {}).items():
        old_context = team.get("context_dir", "")
        if old_context and old_context.startswith(_OLD_SHARE_CONTEXT_DIR):
            team["context_dir"] = os.path.join(SHARE_CONTEXT_DIR, team_name)
            changed = True

    if changed:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    if os.path.isdir(_OLD_SHARE_CONTEXT_DIR):
        os.makedirs(SHARE_CONTEXT_DIR, exist_ok=True)
        for item in os.listdir(_OLD_SHARE_CONTEXT_DIR):
            src = os.path.join(_OLD_SHARE_CONTEXT_DIR, item)
            dst = os.path.join(SHARE_CONTEXT_DIR, item)
            if os.path.isdir(src) and not os.path.exists(dst):
                try:
                    shutil.copytree(src, dst)
                except Exception:
                    pass


# 模块加载时自动执行迁移（幂等）
_migrate_if_needed()
os.makedirs(MCP_HOME, exist_ok=True)
os.makedirs(SHARE_CONTEXT_DIR, exist_ok=True)


def _is_internal_team_workspace(path: str) -> bool:
    try:
        root = os.path.abspath(TEAM_WORKSPACES_DIR)
        candidate = os.path.abspath(path)
        return candidate == root or candidate.startswith(root + os.sep)
    except OSError:
        return False


def _is_internal_context(path: str, context_root: str) -> bool:
    """检查 path 是否位于 context_root 下，防误删用户自定义上下文目录。"""
    try:
        root = os.path.abspath(context_root)
        candidate = os.path.abspath(path)
        return candidate == root or candidate.startswith(root + os.sep)
    except OSError:
        return False


def _default_workspace_dir() -> str:
    """
    Prefer the directory that existed before Codex/agent launch.
    When the TUI starts a leader/member, this intentionally falls back to PROJECT_DIR
    (the directory containing team_manger.py).
    """
    for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD"):
        candidate = os.environ.get(key, "").strip()
        if candidate and os.path.isdir(candidate) and not _is_internal_team_workspace(candidate):
            return os.path.abspath(candidate)
    return PROJECT_DIR


def _team_info(team: str) -> dict:
    return _load().get("teams", {}).get(team, {})


def _context_base_dir() -> str:
    return os.environ.get("MULT_AGENT_MCP_CONTEXT_DIR", SHARE_CONTEXT_DIR)


def _share_dir(team: str) -> str:
    """团队共享上下文区路径（兼容旧函数名）。"""
    team_info = _team_info(team)
    d = team_info.get("context_dir") or os.path.join(_context_base_dir(), team)
    os.makedirs(d, exist_ok=True)
    return d


def _load() -> dict:
    with TEAM_DATA_LOCK:
        if not os.path.exists(DATA_FILE):
            return {"teams": {}}
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)


def _save(data: dict) -> None:
    with TEAM_DATA_LOCK:
        os.makedirs(os.path.dirname(DATA_FILE), exist_ok=True)
        tmp_file = f"{DATA_FILE}.tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        os.replace(tmp_file, DATA_FILE)


def _update_team_data(team_name: str, updater):
    """Apply a targeted team update while holding the data lock."""
    with TEAM_DATA_LOCK:
        data = _load()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return None
        result = updater(team)
        _save(data)
        return result


def _mark_legacy_team_deleted(data: dict, team_name: str) -> None:
    deleted = data.setdefault(DELETED_LEGACY_TEAMS_KEY, {})
    if isinstance(deleted, dict):
        deleted[team_name] = True


def _session(team: str) -> str:
    return f"mcp_{team}"


def _team_dir(team: str) -> str:
    team_info = _team_info(team)
    d = team_info.get("workspace_dir") or _default_workspace_dir()
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
    tmux_path = _find_tmux()
    if not tmux_path:
        return -1, "", "tmux 未安装，请执行 sudo apt install tmux"
    try:
        r = subprocess.run([tmux_path] + cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "tmux 未安装"
    except subprocess.TimeoutExpired:
        return -1, "", "tmux 命令超时"


def _find_tmux() -> str | None:
    """查找 tmux 可执行文件路径，避免 MCP 服务进程 PATH 不完整导致误判。"""
    if not hasattr(_find_tmux, "_cache"):
        _find_tmux._cache = shutil.which("tmux")  # type: ignore[attr-defined]
        if not _find_tmux._cache:
            for p in ("/usr/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux"):
                if os.path.exists(p):
                    _find_tmux._cache = p  # type: ignore[attr-defined]
                    break
    return _find_tmux._cache  # type: ignore[attr-defined]


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
    return _member_window_target(team, window) is not None


def _tmux_target(session: str, window: str) -> str:
    return window if window.startswith("@") else f"{session}:{window}"


def _tmux_window_records(session: str) -> list[dict[str, str]]:
    rc, out, _ = _tmux([
        "list-windows",
        "-t",
        session,
        "-F",
        "#{session_id}\t#{session_created}\t#{window_id}\t#{window_name}",
    ])
    if rc != 0 or not out:
        return []
    records = []
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


def _remember_member_window_id(team_name: str, member_name: str, session: str, window_name: str | None = None) -> str:
    records = _tmux_window_records(session)
    preferred_name = window_name or member_name
    record = next((r for r in records if r["name"] == preferred_name), None)
    if record is None and window_name and window_name != member_name:
        record = next((r for r in records if r["name"] == member_name), None)
    if record is None:
        return ""

    def update(latest_team: dict) -> str:
        member = latest_team.get("members", {}).get(member_name)
        if not member:
            return ""
        member["tmux_window_id"] = record["id"]
        member["tmux_window_name"] = record["name"]
        member["tmux_session"] = session
        member["tmux_session_id"] = record.get("session_id", "")
        member["tmux_session_created"] = record.get("session_created", "")
        return record["id"]

    return _update_team_data(team_name, update) or ""


def _member_window_target(team_name: str, member_name: str) -> str | None:
    session = _find_any_session(team_name)
    if not session:
        return None
    records = _tmux_window_records(session)
    if not records:
        return member_name

    member = _team_info(team_name).get("members", {}).get(member_name, {})
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
        _remember_member_window_id(team_name, member_name, session, member_name)
        return by_name["id"]
    return None


def _send_keys(session: str, window: str, text: str, *, send_enter: bool = True, literal_keys: bool = False) -> tuple[int, str]:
    """向 tmux 窗口发送按键。

    Args:
        session: tmux session 名
        window: tmux window 名
        text: 要发送的文本
        send_enter: 是否在文本后追加 Enter 键（默认 True）
        literal_keys: True=将 text 作为字面按键序列逐字发送（不带 -l），适合单键 'y'/'n'/'a'
                      注意：使用 literal_keys 时 text 将直接作为 tmux send-keys 参数（不带 -l flag），
                      因此像 "C-c"、"Escape" 等特殊键名会被 tmux 直接解释
    """
    target = _tmux_target(session, window)
    if literal_keys:
        rc, _, err = _tmux(["send-keys", "-t", target] + list(text))
    else:
        rc, _, err = _tmux(["send-keys", "-t", target, "-l", text])
    if rc != 0:
        return rc, err
    if send_enter:
        rc, _, err = _tmux(["send-keys", "-t", target, "Enter"])
    return rc, err if rc != 0 else ""


def _confirm_prompt_submission(session: str, window: str, delay: float = 0.35) -> tuple[int, str]:
    """Send a follow-up Enter for CLIs that receive text before their input loop is ready."""
    if delay > 0:
        time.sleep(delay)
    rc, _, err = _tmux(["send-keys", "-t", _tmux_target(session, window), "Enter"])
    return rc, err if rc != 0 else ""


def _inject_claude_leader_prompt(session: str, leader: str, prompt: str) -> tuple[int, str]:
    """Inject the team initialization prompt into a Claude leader terminal.

    Unlike Codex (which accepts a prompt as a CLI argument), Claude Code
    receives its initial task via tmux send-keys.  This helper wraps the
    two-step injection — send the prompt text, then a follow-up Enter to
    ensure the CLI's input loop picks it up — with success checks so the
    caller gets a single pass/fail signal.

    Returns:
        (0, "") on success, or (rc, error_message) on failure.
    """
    rc, err = _send_keys(session, leader, prompt)
    if rc != 0:
        return rc, f"send_keys failed: {err}"
    rc, err = _confirm_prompt_submission(session, leader)
    if rc != 0:
        return rc, f"confirm failed: {err}"
    return 0, ""


def _authorization_choice_key(choice: str) -> str | None:
    normalized = (choice or "yes").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "": "1",
        "1": "1",
        "yes": "1",
        "y": "1",
        "approve": "1",
        "allow": "1",
        "once": "1",
        "2": "2",
        "session": "2",
        "remember": "2",
        "allow_session": "2",
        "yes_session": "2",
        "dont_ask_again": "2",
        "don't_ask_again": "2",
        "3": "3",
    }
    if normalized == "enter":
        return None
    return aliases.get(normalized)


def _send_authorization_choice(session: str, window: str, choice_key: str | None) -> tuple[int, str]:
    target = _tmux_target(session, window)
    keys = ["Enter"] if choice_key is None else [choice_key, "Enter"]
    last_rc = 0
    last_err = ""
    with AUTHORIZATION_MUTEX:
        for attempt in range(2):
            last_rc, _, last_err = _tmux(["send-keys", "-t", target, *keys])
            if last_rc == 0:
                time.sleep(0.12)
                return 0, ""
            if attempt == 0:
                time.sleep(0.1)
    return last_rc, last_err


def _capture_window(session: str, window: str, lines: int = 80) -> tuple[int, str, str]:
    line_count = max(10, min(int(lines), 500))
    return _tmux(["capture-pane", "-t", _tmux_target(session, window), "-p", "-S", f"-{line_count}"])


def _classify_terminal_output(output: str) -> str:
    text = output or ""
    lower = text.lower()
    tail = "\n".join(text.splitlines()[-16:]).lower()
    approval_markers = (
        "requires approval",
        "do you want to proceed",
        "do you want to allow",
        "do you want to create",
        "do you want to edit",
        "do you want to run",
        "this command requires approval",
        "❯ 1. yes",
    )
    if any(marker in lower for marker in approval_markers):
        return "approval"

    busy_markers = (
        "thinking",
        "running",
        "reading",
        "searching",
        "editing",
        "writing",
        "executing",
        "in progress",
        "◼",
    )
    idle_markers = (
        "manual mode on",
        "⏸",
        "❯",
        "brewed for",
        "baked for",
        "tokens",
    )
    if any(marker in tail for marker in busy_markers):
        return "busy"
    if any(marker in tail for marker in idle_markers):
        return "idle"
    return "unknown"


LEADER_WAKEUP_DEFAULT_CONFIG = {
    "enabled": False,
    "idle_threshold": 4,
    "approval_alert": True,
    "auto_authorize_first": True,
    "cooldown_cycles": 6,
    "max_wakeups_per_session": 10,
}


def _leader_wakeup_config(team: dict) -> dict:
    cfg = dict(LEADER_WAKEUP_DEFAULT_CONFIG)
    stored = team.get("leader_wakeup_config")
    if isinstance(stored, dict):
        cfg.update(stored)
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["approval_alert"] = bool(cfg.get("approval_alert", True))
    cfg["auto_authorize_first"] = bool(cfg.get("auto_authorize_first", True))
    cfg["idle_threshold"] = max(1, min(int(cfg.get("idle_threshold", 4)), 20))
    cfg["cooldown_cycles"] = max(0, min(int(cfg.get("cooldown_cycles", 6)), 100))
    cfg["max_wakeups_per_session"] = max(1, min(int(cfg.get("max_wakeups_per_session", 10)), 1000))
    return cfg


def _classify_leader_terminal_output(output: str) -> str:
    """Classify only the leader terminal tail to avoid historical text false positives."""
    text = output or ""
    tail = "\n".join(text.splitlines()[-5:]).lower()
    approval_markers = (
        "requires approval",
        "do you want to proceed",
        "do you want to allow",
        "do you want to create",
        "do you want to edit",
        "do you want to run",
        "this command requires approval",
        "❯ 1. yes",
    )
    if any(marker in tail for marker in approval_markers):
        return "approval"

    busy_markers = (
        "thinking",
        "running",
        "reading",
        "searching",
        "editing",
        "writing",
        "executing",
        "in progress",
        "◼",
    )
    idle_markers = (
        "manual mode on",
        "⏸",
        "❯",
        "brewed for",
        "baked for",
        "tokens",
    )
    if any(marker in tail for marker in busy_markers):
        return "busy"
    if any(marker in tail for marker in idle_markers):
        return "idle"
    return "unknown"


def _scan_leader_terminal(team_name: str, lines: int = 120) -> dict:
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    cfg = _leader_wakeup_config(team)
    if not cfg["enabled"]:
        return {"leader": team.get("leader", ""), "state": "disabled", "action": "disabled"}

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    if ltype != "tmux":
        def update_direct(latest_team: dict) -> dict:
            latest_team["leader_state"] = "active"
            latest_team["leader_wakeup_unavailable_reason"] = "direct_leader"
            latest_team["leader_idle_streak"] = 0
            return {"leader": latest_team.get("leader", ""), "state": "direct", "action": "direct-leader"}

        return _update_team_data(team_name, update_direct) or {"leader": leader, "state": "direct", "action": "direct-leader"}

    session = _find_any_session(team_name)
    if not leader or not session:
        def update_no_session(latest_team: dict) -> dict:
            latest_team["leader_idle_streak"] = 0
            return {"leader": latest_team.get("leader", ""), "state": "dead", "action": "no-session"}

        return _update_team_data(team_name, update_no_session) or {"leader": leader, "state": "dead", "action": "no-session"}
    leader_target = _member_window_target(team_name, leader)
    if not leader_target:
        def update_missing(latest_team: dict) -> dict:
            latest_team["leader_idle_streak"] = 0
            return {"leader": latest_team.get("leader", ""), "state": "dead", "action": "window-missing"}

        return _update_team_data(team_name, update_missing) or {"leader": leader, "state": "dead", "action": "window-missing"}

    rc, out, err = _capture_window(session, leader_target, lines)
    if rc != 0:
        return {"leader": leader, "state": "error", "action": err}

    state = _classify_leader_terminal_output(out)
    now = datetime.datetime.now().isoformat()

    def update_observed(latest_team: dict) -> dict:
        if state == "idle":
            latest_team["leader_idle_streak"] = int(latest_team.get("leader_idle_streak", 0)) + 1
        else:
            latest_team["leader_idle_streak"] = 0
            if latest_team.get("leader_state") == "resting" and state in {"busy", "approval"}:
                latest_team["leader_state"] = "active"
        latest_team["leader_last_observed_state"] = state
        latest_team["leader_last_status_check_ts"] = now
        return {
            "leader": latest_team.get("leader", leader),
            "state": state,
            "idle_streak": latest_team.get("leader_idle_streak", 0),
            "action": "observed",
        }

    return _update_team_data(team_name, update_observed) or {
        "leader": leader,
        "state": state,
        "idle_streak": 0,
        "action": "observed",
    }


def _member_has_active_task(member: dict) -> bool:
    return bool(member.get("last_task")) and not member.get("last_task_completed", True)


def _approval_members_requiring_leader(team: dict, member_results: list[dict]) -> list[str]:
    cfg = _leader_wakeup_config(team)
    if not cfg["approval_alert"]:
        return []
    members = team.get("members", {})
    blocked = []
    for item in member_results:
        if item.get("state") != "approval":
            continue
        action = item.get("action", "")
        if action.startswith("auto-authorized"):
            continue
        name = item.get("member", "")
        member = members.get(name, {})
        mode = _member_mode(member)
        if cfg["auto_authorize_first"] and (member.get("auto_authorize") or mode == "auto"):
            if not action.startswith("authorize-failed"):
                continue
        blocked.append(name)
    return blocked


def _evaluate_leader_wakeup_conditions(team_name: str, member_results: list[dict]) -> dict:
    with TEAM_DATA_LOCK:
        data = _load()
        team = data.get("teams", {}).get(team_name, {})
        cfg = _leader_wakeup_config(team)
        if not cfg["enabled"] or team.get("leader_type") != "tmux":
            return {"action": "none"}

        cooldown = int(team.get("leader_wakeup_cooldown_remaining", 0))
        if cooldown > 0:
            team["leader_wakeup_cooldown_remaining"] = cooldown - 1
            _save(data)

        leader_state = team.get("leader_state", "active")
        members = team.get("members", {})
        leader = team.get("leader", "")
        active_members = [
            name for name, member in members.items()
            if name != leader and _member_has_active_task(member)
        ]
        approval_members = _approval_members_requiring_leader(team, member_results)

        if leader_state == "resting" and approval_members:
            return {"action": "wakeup_approval", "approval_members": approval_members}
        if leader_state == "resting" and not active_members:
            return {"action": "wakeup_all_done"}

        idle_streak = int(team.get("leader_idle_streak", 0))
        if (
            leader_state != "resting"
            and cooldown <= 0
            and idle_streak >= cfg["idle_threshold"]
            and active_members
        ):
            return {"action": "enter_resting", "active_members": active_members}
        return {"action": "none"}


def _leader_terminal_is_idle(team_name: str, team: dict) -> bool:
    leader = team.get("leader", "")
    if team.get("leader_type") != "tmux" or not leader:
        return False
    session = _find_any_session(team_name)
    leader_target = _member_window_target(team_name, leader)
    if not session or not leader_target:
        return False
    rc, out, _ = _capture_window(session, leader_target, 40)
    return rc == 0 and _classify_leader_terminal_output(out) == "idle"


def _build_leader_wakeup_message(team_name: str, reason: str, details: dict) -> str:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    leader = team.get("leader", "")
    status_lines = []
    for name, member in members.items():
        if name == leader:
            continue
        observed = member.get("last_observed_state") or "unknown"
        task_state = "unfinished" if _member_has_active_task(member) else "done"
        status_lines.append(f"- {name}: {observed}, {task_state}")
    if not status_lines:
        status_lines.append("- no non-leader members")

    if reason == "approval":
        blocked = ", ".join(details.get("approval_members", [])) or "unknown"
        headline = "[system] Leader wakeup: a member is waiting for authorization."
        extra = f"Authorization needed: {blocked}."
    else:
        headline = "[system] Leader wakeup: all tracked member tasks appear complete."
        extra = "Review the shared context and finish the team handoff."

    return "\n".join([
        headline,
        f"Team: {team_name}",
        extra,
        "Member snapshot:",
        *status_lines,
    ])


def _execute_leader_wakeup_action(team_name: str, action_info: dict) -> dict:
    import datetime

    action = action_info.get("action", "none")
    if action == "none":
        return {"action": "none"}

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    cfg = _leader_wakeup_config(team)
    if not cfg["enabled"] or team.get("leader_type") != "tmux":
        return {"action": "none"}

    now = datetime.datetime.now().isoformat()
    if action == "enter_resting":
        def update_resting(latest_team: dict) -> dict:
            latest_team["leader_state"] = "resting"
            latest_team["leader_resting_since"] = now
            latest_team["leader_last_action"] = "enter_resting"
            return {"action": "enter_resting"}

        return _update_team_data(team_name, update_resting) or {"action": "none"}

    if action in {"wakeup_all_done", "wakeup_approval"}:
        wakeups = int(team.get("leader_wakeup_count", 0))
        if wakeups >= cfg["max_wakeups_per_session"]:
            def update_limit(latest_team: dict) -> dict:
                latest_team["leader_last_action"] = "wakeup-limit"
                return {"action": "wakeup-limit"}

            return _update_team_data(team_name, update_limit) or {"action": "none"}

        should_inject = _leader_terminal_is_idle(team_name, team)
        reason = "approval" if action == "wakeup_approval" else "all_done"

        def update_wakeup(latest_team: dict) -> dict:
            latest_cfg = _leader_wakeup_config(latest_team)
            latest_wakeups = int(latest_team.get("leader_wakeup_count", 0))
            if latest_wakeups >= latest_cfg["max_wakeups_per_session"]:
                latest_team["leader_last_action"] = "wakeup-limit"
                return {"action": "wakeup-limit"}
            latest_team["leader_state"] = "active"
            latest_team["leader_idle_streak"] = 0
            latest_team["leader_wakeup_reason"] = reason
            latest_team["leader_wakeup_count"] = latest_wakeups + 1
            latest_team["leader_wakeup_cooldown_remaining"] = latest_cfg["cooldown_cycles"]
            latest_team["leader_last_wakeup_ts"] = now
            latest_team.pop("leader_resting_since", None)
            return {"action": action, "wakeup_count": latest_wakeups + 1}

        update_result = _update_team_data(team_name, update_wakeup) or {"action": "none"}
        if update_result.get("action") == "wakeup-limit":
            return update_result

        if not should_inject:
            return {"action": action, "injected": False}
        session = _find_any_session(team_name)
        latest_team = _team_info(team_name)
        leader = latest_team.get("leader", "")
        leader_target = _member_window_target(team_name, leader) if leader else None
        if not session or not leader_target:
            return {"action": action, "injected": False}
        message = _build_leader_wakeup_message(team_name, reason, action_info)
        rc, err = _send_keys(session, leader_target, message)
        return {"action": action, "injected": rc == 0, "error": err}

    return {"action": "none"}


def _monitor_team_wakeup_once(
    team_name: str,
    *,
    auto_authorize_choice: str = "",
    mark_idle_done: bool = True,
    lines: int = 120,
) -> dict:
    leader_result = _scan_leader_terminal(team_name, lines=lines)
    member_results = _monitor_team_once(
        team_name,
        auto_authorize_choice=auto_authorize_choice,
        mark_idle_done=mark_idle_done,
        lines=lines,
    )
    action_info = _evaluate_leader_wakeup_conditions(team_name, member_results)
    executed = _execute_leader_wakeup_action(team_name, action_info)
    return {
        "leader": leader_result,
        "members": member_results,
        "action": executed,
    }


def _scan_member_terminal(
    team_name: str,
    member_name: str,
    *,
    lines: int = 120,
    auto_authorize_choice: str = "",
    mark_idle_done: bool = True,
) -> dict:
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    member = members.get(member_name)
    if not member:
        return {"member": member_name, "state": "missing", "action": "missing"}

    session = _find_any_session(team_name)
    if not session:
        member["last_observed_state"] = "dead"
        member["last_status_check_ts"] = datetime.datetime.now().isoformat()
        _save(data)
        return {"member": member_name, "state": "dead", "action": "no-session"}

    member_target = _member_window_target(team_name, member_name)
    if not member_target:
        member["last_observed_state"] = "dead"
        member["last_status_check_ts"] = datetime.datetime.now().isoformat()
        _save(data)
        if member.get("last_task") and not member.get("last_task_completed", True):
            if member.get("recovery_count", 0) >= int(team.get("monitor_max_recoveries", 3)):
                return {"member": member_name, "state": "dead", "action": "recovery-limit"}
            ok, msg = _recover_and_send(team_name, member_name, session)
            return {"member": member_name, "state": "dead", "action": "recovered" if ok else f"recover-failed:{msg}"}
        return {"member": member_name, "state": "dead", "action": "window-missing"}

    rc, out, err = _capture_window(session, member_target, lines)
    if rc != 0:
        return {"member": member_name, "state": "error", "action": err}

    state = _classify_terminal_output(out)
    now = datetime.datetime.now().isoformat()
    member["last_observed_state"] = state
    member["last_status_check_ts"] = now
    action = "observed"

    if state == "approval":
        member["blocked_reason"] = "approval"
        member["last_blocked_ts"] = now
        mode = _member_mode(member)
        if auto_authorize_choice or member.get("auto_authorize") or mode == "auto":
            choice = auto_authorize_choice or member.get("auto_authorize_choice") or "session"
            choice_key = _authorization_choice_key(choice)
            if choice_key is not None or choice.strip().lower() == "enter":
                arc, aerr = _send_authorization_choice(session, member_target, choice_key)
                action = f"auto-authorized:{choice}" if arc == 0 else f"authorize-failed:{aerr}"
                if arc == 0:
                    member["last_observed_state"] = "busy"
                    state = "busy"
                    member.pop("blocked_reason", None)
    elif state == "idle":
        member.pop("blocked_reason", None)
        if mark_idle_done and member.get("last_task") and not member.get("last_task_completed", True):
            member["last_task_completed"] = True
            member["last_completed_by_monitor_ts"] = now
            action = "marked-complete"
    elif state == "busy":
        member.pop("blocked_reason", None)

    _save(data)
    return {"member": member_name, "state": state, "action": action}


def _monitor_team_once(
    team_name: str,
    *,
    auto_authorize_choice: str = "",
    mark_idle_done: bool = True,
    lines: int = 120,
) -> list[dict]:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    results = []
    for name in members:
        if ltype == "tmux" and name == leader:
            continue
        results.append(
            _scan_member_terminal(
                team_name,
                name,
                lines=lines,
                auto_authorize_choice=auto_authorize_choice,
                mark_idle_done=mark_idle_done,
            )
        )
        time.sleep(0.03)
    return results


def _monitor_team_loop(team_name: str, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        data = _load()
        team = data.get("teams", {}).get(team_name, {})
        if not team or not team.get("terminals_active"):
            return
        interval = max(5, int(team.get("monitor_interval_seconds", 30)))
        choice = team.get("monitor_auto_authorize_choice", "")
        try:
            _monitor_team_wakeup_once(
                team_name,
                auto_authorize_choice=choice,
                mark_idle_done=team.get("monitor_mark_idle_done", True),
            )
        except Exception:
            pass
        stop_event.wait(interval)


def _start_team_monitor(team_name: str) -> None:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    if not team.get("monitor_enabled", True):
        return
    thread = TEAM_MONITOR_THREADS.get(team_name)
    if thread and thread.is_alive():
        return
    stop_event = threading.Event()
    TEAM_MONITOR_STOP_EVENTS[team_name] = stop_event
    thread = threading.Thread(
        target=_monitor_team_loop,
        args=(team_name, stop_event),
        name=f"mcp-monitor-{team_name}",
        daemon=True,
    )
    TEAM_MONITOR_THREADS[team_name] = thread
    thread.start()


def _stop_team_monitor(team_name: str) -> None:
    event = TEAM_MONITOR_STOP_EVENTS.pop(team_name, None)
    thread = TEAM_MONITOR_THREADS.pop(team_name, None)
    if event:
        event.set()
    if thread and thread.is_alive():
        thread.join(timeout=2.0)


def _kill_session(team: str) -> None:
    session = _find_any_session(team)
    if session:
        _tmux(["kill-session", "-t", session])


def _get_server_port() -> int:
    return int(os.environ.get("FASTMCP_PORT", "8000"))


def _server_url() -> str:
    return f"http://localhost:{_get_server_port()}/mcp"


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


def _resolve_team_name_from_session(session: str) -> str:
    team_name = session.removeprefix("mcp_")
    if "_" not in team_name:
        return team_name
    data = _load()
    for tname in data.get("teams", {}):
        if session == f"mcp_{tname}" or session.startswith(f"mcp_{tname}_"):
            return tname
    return team_name


def _normalize_member_mode(mode: str) -> str:
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


def _member_mode(member_info: dict) -> str:
    return _normalize_member_mode(member_info.get("work_mode") or member_info.get("mode") or "manual") or "manual"


def _claude_agent_args(
    agent_cmd: str,
    mode: str,
    *,
    dangerously_skip_permissions: bool = False,
    allowed_tools: list[str] | None = None,
) -> list[str]:
    args = [agent_cmd]
    normalized = _normalize_member_mode(mode)
    if dangerously_skip_permissions:
        args.append("--dangerously-skip-permissions")
    elif normalized in {"auto", "plan"}:
        args.extend(["--permission-mode", normalized])
    if allowed_tools:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    return args


def _codex_mode_args(mode: str) -> list[str]:
    normalized = _normalize_member_mode(mode)
    if normalized == "auto":
        return ["--ask-for-approval", "never"]
    if normalized == "plan":
        return ["--ask-for-approval", "on-request"]
    return []


def _mode_task_prefix(member_info: dict) -> str:
    mode = _member_mode(member_info)
    agent = member_info.get("agent", "")
    if mode == "plan":
        if _is_codex(agent):
            return (
                "[成员模式: plan]\n"
                "先只分析和给出计划，不要修改文件、运行需要授权的命令或执行破坏性操作；"
                "等待 leader 明确批准后再实施。\n"
            )
        return (
            "[成员模式: plan]\n"
            "先只分析和给出计划，不要修改文件或运行需要授权的命令；等待 leader 批准后再实施。\n"
        )
    if mode == "auto":
        return "[成员模式: auto]\n在已授权范围内自主推进；遇到审批提示时等待 leader 监控处理。\n"
    return ""


def _build_member_initial_context(team_name: str, member_name: str) -> str:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    role = member.get("role", "member")
    leader = team.get("leader", "")
    leader_type = team.get("leader_type", "")
    mode = _member_mode(member)

    lines = [
        "=" * 50,
        f"[系统] 你已加入 Multi-Agent MCP 团队 '{team_name}'",
        "",
        f"你的成员名: {member_name}",
        f"你的角色: {role}",
        f"你的模式: {mode}",
        f"团队 Leader: {leader or 'direct'} ({leader_type or 'direct'})",
        f"共享工作目录: {_team_dir(team_name)}",
        f"共享上下文区: {_share_dir(team_name)}",
        "",
        "可用协作工具:",
        "  member_read_shared       - 查看团队共享上下文",
        "  member_report_result     - 完成任务后回传结果，并让状态退出 working",
        "  member_list_shared_files - 列出共享上下文文件",
        "  member_send_message      - 向 leader 或成员发送消息",
        "  member_acquire_file_lock - 修改文件前申请锁",
        "  member_release_file_lock - 释放文件锁",
        "  member_submit_patch      - 以 patch 形式提交改动",
        "",
        "完成任务后必须调用 member_report_result；leader 也会定期监控终端状态并收敛 working/approval 状态。",
        "=" * 50,
    ]
    return "\n".join(lines)


def _tmux_spawn_member(
    session: str,
    member_name: str,
    agent: str,
    team_dir: str,
    *,
    new_session: bool = False,
    window_name: str | None = None,
    dangerously_skip_permissions: bool = False,
) -> tuple[int, str, str]:
    """启动成员 tmux 窗口，统一处理 workspace 与 agent 类型差异。

    对于 claude 成员，自动写入 .claude/settings.json 预配置权限以减少审批阻塞。
    """
    name = window_name or member_name
    if new_session:
        cmd = ["new-session", "-d", "-s", session, "-n", name]
    else:
        cmd = ["new-window", "-t", session, "-n", name]

    team_name = _resolve_team_name_from_session(session)
    member_info = _load().get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
    mode = _member_mode(member_info)

    if _is_codex(agent):
        cmd.extend(_codex_command(agent, team_dir, member_mode=mode))
    else:
        # Claude / 其他 agent: 预配置权限 + 从共享工作目录启动
        _write_claude_permissions(team_name, dangerously_skip=dangerously_skip_permissions)

        agent_args = _claude_agent_args(
            agent,
            mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
        )
        cmd.extend(["-c", team_dir] + agent_args)

    result = _tmux(cmd)
    if result[0] == 0:
        _remember_member_window_id(team_name, member_name, session, name)
    return result


def _codex_command(agent_cmd: str, team_dir: str, prompt: str = "", member_mode: str = "") -> list[str]:
    cmd = [agent_cmd, "-C", team_dir]
    cmd.extend(_codex_mode_args(member_mode))
    if prompt:
        cmd.append(prompt)
    return cmd


def _leader_system_prompt(team_name: str, task: str = "") -> str:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    leader = team.get("leader", "")
    teammates = [
        f"{name}(role={info.get('role') or 'member'}, agent={info.get('agent') or team.get('default_agent', 'claude')})"
        for name, info in members.items()
        if name != leader
    ]
    lines = [
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的 leader。",
        "必须使用本项目 MCP 工具协调已有团队成员，不要使用 Codex 内置 spawn_agent / sub-agent 代替团队成员。",
        "开始后先调用 leader_list_team 查看成员，再用 leader_assign_subtask、leader_broadcast 等 leader_* 工具分配任务。",
        f"团队共享工作目录: {_team_dir(team_name)}",
        f"团队共享上下文区: {_share_dir(team_name)}",
    ]
    if teammates:
        lines.append("已有成员: " + "; ".join(teammates))
    if task.strip():
        lines.extend(["", "总任务:", task.strip()])
    return "\n".join(lines)


# ============================================================
# MCP 配置生成
# ============================================================

def _claude_mcp_json_path(team_name: str) -> str:
    """Claude 的 MCP 配置文件路径"""
    team_dir = _team_dir(team_name)
    claude_dir = os.path.join(team_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    return os.path.join(claude_dir, "mcp.json")


def _expected_claude_mcp_config() -> dict:
    """Return the expected Claude Code MCP config for the running server."""
    return {"mcpServers": {MCP_SERVER_NAME: _expected_claude_mcp_server_config()}}


def _expected_claude_mcp_server_config() -> dict:
    """Return the expected single-server Claude Code MCP entry."""
    return {
        "type": "http",
        "url": _server_url(),
    }


def _validate_claude_mcp_server_config(server: object) -> tuple[bool, str]:
    if not isinstance(server, dict):
        return False, "server 配置缺失"
    expected_url = _server_url()
    current_type = server.get("type")
    current_url = server.get("url")
    if current_type != "http":
        return False, f"type 不匹配（当前 {current_type!r}，应为 'http'）"
    if current_url != expected_url:
        return False, f"URL 不匹配（当前 {current_url or '空'}，应为 {expected_url}）"
    return True, "ok"


def _claude_global_config_path() -> str:
    """Claude Code 全局配置文件 (~/.claude.json) 路径"""
    return CLAUDE_GLOBAL_CONFIG_PATH


def _claude_project_entry(data: dict, team_dir: str | None) -> dict | None:
    if not team_dir:
        return None
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None
    return projects.get(os.path.abspath(team_dir))


def _claude_global_mcp_status(
    config_path: str | None = None,
    team_dir: str | None = None,
) -> tuple[bool, str]:
    """Check whether ~/.claude.json has a same-name server overriding project config."""
    path = config_path or _claude_global_config_path()
    if not os.path.exists(path):
        return True, "全局 Claude 配置不存在"

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"全局 Claude 配置无法解析: {e}"

    servers = data.get("mcpServers")
    found = False
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        found = True
        ok, message = _validate_claude_mcp_server_config(servers.get(MCP_SERVER_NAME))
        if not ok:
            return False, f"全局 Claude MCP 配置冲突: {message}"

    project_entry = _claude_project_entry(data, team_dir)
    project_servers = project_entry.get("mcpServers") if isinstance(project_entry, dict) else None
    if isinstance(project_servers, dict) and MCP_SERVER_NAME in project_servers:
        found = True
        ok, message = _validate_claude_mcp_server_config(project_servers.get(MCP_SERVER_NAME))
        if not ok:
            return False, f"项目 Claude MCP 配置冲突: {message}"

    if found:
        return True, "全局 Claude MCP 配置已匹配"
    return True, "未发现全局同名 MCP 配置"


def _repair_claude_global_mcp_if_conflicting(
    config_path: str | None = None,
    team_dir: str | None = None,
) -> tuple[bool, str]:
    """Repair a stale global Claude MCP server that would override .claude/mcp.json."""
    path = config_path or _claude_global_config_path()
    if not os.path.exists(path):
        return True, "全局 Claude 配置不存在"

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"全局 Claude 配置无法解析: {e}"

    changed = False
    messages: list[str] = []
    servers = data.get("mcpServers")
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        ok, message = _validate_claude_mcp_server_config(servers.get(MCP_SERVER_NAME))
        if not ok:
            servers[MCP_SERVER_NAME] = _expected_claude_mcp_server_config()
            changed = True
            messages.append(f"全局 Claude MCP 配置: {message}")

    project_entry = _claude_project_entry(data, team_dir)
    project_servers = project_entry.get("mcpServers") if isinstance(project_entry, dict) else None
    if isinstance(project_servers, dict) and MCP_SERVER_NAME in project_servers:
        ok, message = _validate_claude_mcp_server_config(project_servers.get(MCP_SERVER_NAME))
        if not ok:
            project_servers[MCP_SERVER_NAME] = _expected_claude_mcp_server_config()
            changed = True
            messages.append(f"项目 Claude MCP 配置: {message}")

    if not changed:
        return True, "全局 Claude MCP 配置已匹配"

    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    os.replace(tmp_path, path)
    return True, "已修复 " + "；".join(messages)


def _sync_global_claude_mcp_config(team_name: str = "") -> str:
    """Backward-compatible wrapper for repairing global Claude MCP conflicts."""
    team_dir = _team_dir(team_name) if team_name else None
    ok, message = _repair_claude_global_mcp_if_conflicting(team_dir=team_dir)
    if not ok:
        return f"❌ {message}"
    return f"✅ {message}"


def _write_claude_mcp(team_name: str) -> str:
    """为 Claude Code 写入 .claude/mcp.json，并修复全局同名旧配置。"""
    mcp_json_path = _claude_mcp_json_path(team_name)
    with open(mcp_json_path, "w", encoding="utf-8") as f:
        json.dump(_expected_claude_mcp_config(), f, indent=2, ensure_ascii=False)
    ok, message = _repair_claude_global_mcp_if_conflicting(team_dir=_team_dir(team_name))
    if not ok:
        raise RuntimeError(message)
    return mcp_json_path


def _claude_mcp_status(team_name: str) -> tuple[bool, str]:
    """Validate that Claude Code will load the current streamable-http MCP URL."""
    mcp_json_path = _claude_mcp_json_path(team_name)
    if not os.path.exists(mcp_json_path):
        return False, "未配置"

    try:
        with open(mcp_json_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"配置文件无法解析: {e}"

    server = data.get("mcpServers", {}).get(MCP_SERVER_NAME)
    if not isinstance(server, dict):
        if "teamMCP" in data:
            return False, "旧 teamMCP 配置格式，需要迁移到 mcpServers"
        return False, f"缺少 mcpServers.{MCP_SERVER_NAME}"

    ok, message = _validate_claude_mcp_server_config(server)
    if not ok:
        return False, message

    ok, message = _claude_global_mcp_status(team_dir=_team_dir(team_name))
    if not ok:
        return False, message

    return True, mcp_json_path


def _claude_mcp_configured(team_name: str) -> bool:
    ok, _ = _claude_mcp_status(team_name)
    return ok


def _claude_settings_json_path(team_name: str) -> str:
    """Claude Code 的 settings.json 路径（权限预配置）"""
    team_dir = _team_dir(team_name)
    claude_dir = os.path.join(team_dir, ".claude")
    os.makedirs(claude_dir, exist_ok=True)
    return os.path.join(claude_dir, "settings.json")


def _write_claude_permissions(
    team_name: str,
    *,
    dangerously_skip: bool = False,
    allow_patterns: list[str] | None = None,
    additional_dirs: list[str] | None = None,
) -> str:
    """为团队的 Claude Code 成员预配置权限策略。

    写入 .claude/settings.json 以减少成员首次执行 Edit/Write/Bash 时的审批阻塞。

    Args:
        team_name: 团队名称
        dangerously_skip: 跳过所有权限检查（生产环境中慎用）
        allow_patterns: 额外允许的工具模式列表，如 ["Bash(git:*)", "Edit(*.py)"]
        additional_dirs: 额外允许访问的目录列表
    """
    settings_path = _claude_settings_json_path(team_name)
    team_dir = _team_dir(team_name)

    permissions_config: dict = {}

    if dangerously_skip:
        permissions_config["allow-dangerously-skip-permissions"] = True
    else:
        allow: list[str] = list(allow_patterns or [])
        # 默认允许团队工作目录内的 Edit/Write 操作
        allow.extend([
            f"Edit({team_dir}/*)",
            f"Write({team_dir}/*)",
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
    return settings_path


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


def _codex_mcp_url(server_name: str = "mult-agent-mcp") -> str:
    config_path = _codex_config_path()
    if not os.path.exists(config_path):
        return ""
    with open(config_path, "r") as f:
        lines = f.readlines()

    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[mcp_servers.{server_name}]":
            in_section = True
            continue
        if in_section and stripped.startswith("["):
            return ""
        if in_section and stripped.startswith("url"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    return ""


def _write_codex_mcp_config(server_name: str, url: str) -> None:
    config_path = _codex_config_path()
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    lines = []
    if os.path.exists(config_path):
        with open(config_path, "r") as f:
            lines = f.readlines()

    header = f"[mcp_servers.{server_name}]"
    result = []
    in_section = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            if not replaced:
                result.extend([f"\n{header}\n", f'url = "{url}"\n'])
                replaced = True
            in_section = True
            continue
        if in_section and stripped.startswith("["):
            in_section = False
            result.append(line)
            continue
        if in_section:
            continue
        result.append(line)

    if not replaced:
        if result and result[-1].strip():
            result.append("\n")
        result.extend([f"{header}\n", f'url = "{url}"\n'])

    with open(config_path, "w") as f:
        f.writelines(result)


def _ensure_codex_mcp(server_name: str = "mult-agent-mcp") -> str:
    """
    确保 codex 全局配置中注册了此 MCP server。
    优先通过 codex mcp add CLI，失败则直接编辑配置文件。
    返回状态字符串。
    """
    url = _server_url()

    if _codex_mcp_registered(server_name):
        current_url = _codex_mcp_url(server_name)
        if current_url == url:
            return "already_configured"
        try:
            _write_codex_mcp_config(server_name, url)
            return f"✅ codex MCP 已修正 URL: {current_url or '空'} → {url}"
        except Exception as e:
            return f"❌ codex MCP URL 修正失败: {e}\n💡 请手动执行: codex mcp remove {server_name} && codex mcp add {server_name} --url {url}"

    # 方式 1: codex mcp add CLI
    rc, _, _ = _run([
        "codex", "mcp", "add", server_name,
        "--url", url,
    ], timeout=15)
    if rc == 0:
        return "✅ codex MCP 已通过 CLI 注册。"

    # 方式 2: 直接写入 ~/.codex/config.toml
    config_path = _codex_config_path()
    try:
        _write_codex_mcp_config(server_name, url)
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
    - claude: 为团队共享工作目录写入 .claude/mcp.json
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
        "workspace_dir": _default_workspace_dir(),
        "context_dir": os.path.join(_context_base_dir(), team_name),
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
    """删除整个团队及其终端、共享上下文和团队工作区。"""
    data = _load()
    if team_name not in data.get("teams", {}):
        return f"❌ 团队 '{team_name}' 不存在。"

    team = data["teams"][team_name]

    # 停止后台监控线程
    _stop_team_monitor(team_name)

    # 销毁 tmux session
    _kill_session(team_name)

    # 删除团队数据
    del data["teams"][team_name]
    _mark_legacy_team_deleted(data, team_name)
    _save(data)

    # 清理磁盘上的团队产物（仅限本工具管理的目录）
    import shutil as _shutil
    cleanup_msgs: list[str] = []
    context_dir = os.path.abspath(os.path.expanduser(team.get("context_dir") or os.path.join(_context_base_dir(), team_name)))
    context_root = os.path.abspath(os.path.expanduser(_context_base_dir()))
    if os.path.isdir(context_dir) and context_dir != context_root and _is_internal_context(context_dir, context_root):
        try:
            _shutil.rmtree(context_dir)
            cleanup_msgs.append(f"🧹 已删除共享上下文: {context_dir}")
        except OSError as e:
            cleanup_msgs.append(f"⚠️ 共享上下文删除失败: {e}")
    elif os.path.isdir(context_dir):
        cleanup_msgs.append(f"⚠️ 跳过非托管共享上下文: {context_dir}")

    workspace_dir_raw = team.get("workspace_dir", "")
    workspace_dir = os.path.abspath(os.path.expanduser(workspace_dir_raw)) if workspace_dir_raw else ""
    workspace_root = os.path.abspath(os.path.expanduser(TEAM_WORKSPACES_DIR))
    if workspace_dir and os.path.isdir(workspace_dir) and workspace_dir != workspace_root and _is_internal_team_workspace(workspace_dir):
        try:
            _shutil.rmtree(workspace_dir)
            cleanup_msgs.append(f"🧹 已删除团队工作区: {workspace_dir}")
        except OSError as e:
            cleanup_msgs.append(f"⚠️ 团队工作区删除失败: {e}")
    elif workspace_dir and os.path.isdir(workspace_dir):
        cleanup_msgs.append(f"ℹ️ 保留用户工作目录: {workspace_dir}")

    legacy_workspace = os.path.abspath(os.path.join(TEAM_WORKSPACES_DIR, team_name))
    if (
        os.path.isdir(legacy_workspace)
        and legacy_workspace != workspace_root
        and _is_internal_team_workspace(legacy_workspace)
    ):
        try:
            _shutil.rmtree(legacy_workspace)
            cleanup_msgs.append(f"🧹 已删除遗留团队工作区: {legacy_workspace}")
        except OSError as e:
            cleanup_msgs.append(f"⚠️ 遗留团队工作区删除失败: {e}")

    suffix = ("\n" + "\n".join(cleanup_msgs)) if cleanup_msgs else ""
    return f"✅ 团队 '{team_name}' 已删除。{suffix}"


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

    session = _find_any_session(team_name) or _session(team_name)
    member_target = _member_window_target(team_name, member_name) if session else None

    del team["members"][member_name]

    if team.get("leader") == member_name:
        team["leader"] = ""
        team["leader_type"] = ""

    _save(data)

    if session and member_target:
        _tmux(["kill-window", "-t", _tmux_target(session, member_target)])

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
        claude_ok, claude_status = _claude_mcp_status(team_name)
        if claude_ok:
            lines.append(f"   Claude MCP: ✅ {claude_status}")
        elif os.path.exists(claude_mcp):
            lines.append(f"   Claude MCP: ⚠️ {claude_status}（{claude_mcp}）→ 请重新配置 Claude MCP 或执行 launch_team_terminals")
        else:
            lines.append("   Claude MCP: ❌ 未配置（将在 launch 时自动生成）")
    else:
        lines.append(f"   Claude: 无 claude agent 成员")

    # Codex 检查
    if has_codex:
        codex_url = _codex_mcp_url()
        codex_ok = codex_url == _server_url()
        if codex_ok:
            lines.append(f"   Codex MCP: ✅ 已注册（{codex_url}）")
        elif codex_url:
            lines.append(f"   Codex MCP: ⚠️ URL 不匹配（当前 {codex_url}，应为 {_server_url()}）→ 请执行 setup_codex_mcp")
        else:
            lines.append(f"   Codex MCP: ❌ 未注册 → 请执行 setup_codex_mcp")
    else:
        lines.append(f"   Codex: 无 codex agent 成员")

    lines.append(f"\n💡 启动终端时会自动配置所需 MCP。")
    return "\n".join(lines)


@mcp.tool
def get_server_config() -> str:
    """查看 MCP 服务器配置（Claude + Codex 双格式）。"""
    url = _server_url()

    return "\n".join([
        "📋 **MCP 服务器配置**",
        "",
        "### Claude Code（.claude/mcp.json）",
        "```json",
        json.dumps({
            "mcpServers": {
                "mult-agent-mcp": {
                    "type": "http",
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
    启动团队终端（共享上下文模式）。

    所有成员共享真实工作目录、共享上下文区和 MCP 连接：
    - claude 成员: 从团队 workspace_dir 启动，自动加载 .claude/mcp.json
    - codex 成员: 通过全局 codex config 连接 MCP
    - 共享上下文区: share_context_space/{team}/ 供所有成员交换上下文

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

    # 准备真实共享工作目录和共享上下文区
    team_dir = _team_dir(team_name)
    share_dir = _share_dir(team_name)

    # 为所有成员统一配置 MCP（预配置，各成员窗口启动时自动加载）
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    is_direct = (ltype == "direct")
    mcp_setup_lines = [
        "🔧 共享上下文模式: 所有成员共享工作目录 + 共享上下文区 + MCP 连接",
        f"   📁 工作目录: {team_dir}",
        f"   📂 共享上下文区: {share_dir}",
    ]

    # ================================================================
    # direct 模式: 你是 leader，只创建成员终端
    # ================================================================
    if is_direct:
        created = []

        non_leader_members = [
            (n, i) for n, i in members.items()
            if not _is_direct_leader_member(team, n)
        ]
        if not non_leader_members:
            rc, _, err = _tmux(["new-session", "-d", "-s", session, "-n", "members", "-c", team_dir])
            if rc != 0:
                return f"❌ 创建终端失败: {err}"
        else:
            first_name, first_info = non_leader_members[0]
            first_agent = first_info.get("agent", "claude")
            rc, _, err = _tmux_spawn_member(
                session, first_name, first_agent, team_dir, new_session=True,
            )
            if rc != 0:
                return f"❌ 创建终端失败: {err}"
            created.append((first_name, first_agent))

            for name, info in non_leader_members[1:]:
                agent = info.get("agent", "claude")
                rc, _, err = _tmux_spawn_member(session, name, agent, team_dir)
                if rc == 0:
                    created.append((name, agent))
                time.sleep(0.1)

        team["terminals_active"] = True
        _save(data)
        _start_team_monitor(team_name)

        time.sleep(2)
        context_failures = []
        for name, _agent in created:
            target = _member_window_target(team_name, name) or name
            rc, err = _send_keys(session, target, _build_member_initial_context(team_name, name))
            if rc != 0:
                context_failures.append(f"{name}: {err}")

        task_note = ""
        if task.strip():
            task_note = (
                f"\n📋 总任务:\n   > {task}\n"
                f"\n💡 使用 leader_assign_subtask 分配给成员。\n"
                f"💡 所有成员共享工作目录 ({team_dir})，上下文沉淀到 {share_dir}。"
            )

        agent_summary = ", ".join(
            f"{n}({_agent_type(a)}[MCP])" for n, a in created
        )
        return "\n".join([
            f"🚀 **{team_name}** 终端已启动！（直接控制 + 共享上下文模式）",
            f"   session: {session}",
            f"   👑 Leader: **你（当前会话）**",
            f"   👥 成员 ({len(created)}): {agent_summary}",
            "\n".join(mcp_setup_lines),
            task_note,
            ("\n⚠️ 初始上下文发送失败: " + "; ".join(context_failures)) if context_failures else "",
        ])

    # ================================================================
    # tmux 模式: leader 窗口 + 成员窗口（共享上下文）
    # ================================================================
    leader_agent = members[leader].get("agent", "claude")
    leader_atype = _agent_type(leader_agent)

    mcp_setup_lines.insert(0, f"🔧 Leader agent: {leader_agent} [{leader_atype}]")

    leader_prompt = _leader_system_prompt(team_name, task)
    leader_mode = _member_mode(members.get(leader, {}))
    if _is_codex(leader_agent):
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            *_codex_command(leader_agent, team_dir, leader_prompt, member_mode=leader_mode),
        ])
    else:
        _write_claude_permissions(team_name)
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", team_dir,
            *_claude_agent_args(
                leader_agent,
                leader_mode,
                allowed_tools=CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS,
            ),
        ])

    if rc != 0:
        return f"❌ 创建 leader 终端失败: {err}"
    created = [(leader, leader_agent, f"👑[{leader_atype}][MCP]")]

    # 成员窗口: 从共享工作目录启动
    for name, info in members.items():
        if name == leader:
            continue
        member_agent = info.get("agent", "claude")
        rc, _, err = _tmux_spawn_member(session, name, member_agent, team_dir)
        if rc == 0:
            created.append((name, member_agent, f"[{_agent_type(member_agent)}][MCP]"))
        time.sleep(0.1)

    team["terminals_active"] = True
    _save(data)
    _start_team_monitor(team_name)

    time.sleep(2)
    context_failures = []
    for name, info in members.items():
        if name == leader:
            continue
        target = _member_window_target(team_name, name) or name
        rc, err = _send_keys(session, target, _build_member_initial_context(team_name, name))
        if rc != 0:
            context_failures.append(f"{name}: {err}")

    # 发送总任务给 leader
    task_result = ""
    if not _is_codex(leader_agent):
        rc, err2 = _inject_claude_leader_prompt(session, leader, leader_prompt)
        if rc != 0:
            return f"❌ 向 Claude leader 注入团队提示失败: {err2}"
        if task.strip():
            task_result = f"\n📋 总任务已随 leader 初始提示发送给 '{leader}' ✅"
    elif task.strip():
        task_result = f"\n📋 总任务已随 Codex leader 初始提示发送给 '{leader}' ✅"

    agent_summary = ", ".join(f"{n}({t})" for n, _, t in created)
    other_count = len(created) - 1

    return "\n".join([
        f"🚀 **{team_name}** 终端已启动！（共享上下文模式）",
        f"   session: {session}",
        f"   窗口: {agent_summary}",
        f"   👑 Leader: {leader} [{leader_atype}]（已连接 MCP）",
        f"   👥 成员: {other_count} 人（已连接 MCP）",
        "",
        "\n".join(mcp_setup_lines),
        "",
        "💡 所有成员共享真实工作目录，文件操作互相可见",
        "💡 成员可使用 member_report_result 回传结果并生成压缩上下文",
        f"💡 共享上下文区: {share_dir}",
        task_result,
        ("\n⚠️ 初始上下文发送失败: " + "; ".join(context_failures)) if context_failures else "",
    ])


@mcp.tool
def kill_team_terminals(team_name: str) -> str:
    """销毁团队所有终端。"""
    _stop_team_monitor(team_name)
    session = _find_any_session(team_name)
    if session:
        _tmux(["kill-session", "-t", session])

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

    status_counts: dict[str, int] = {
        "working": 0,
        "approval": 0,
        "recovering": 0,
        "idle": 0,
        "sleep": 0,
        "dead": 0,
        "leader": 0,
    }
    for name in members:
        alive = name in alive_windows
        status_label, status_bucket = format_member_activity_status(members[name], alive)
        status_counts[status_bucket] = status_counts.get(status_bucket, 0) + 1

        role = members[name].get("role", "")
        agent = members[name].get("agent", "claude")
        atype = _agent_type(agent)
        mode = _member_mode(members[name])
        observed = members[name].get("last_observed_state", "")
        is_ldr = " 👑Leader" if (name == leader and ltype == "tmux") else ""
        role_str = f" [{role}]" if role else ""
        mode_str = f" mode={mode}" if mode != "manual" else ""
        observed_str = f" observed={observed}" if observed else ""

        lines.append(f"  {status_label} **{name}**{is_ldr}{role_str}  {agent}[{atype}]{mode_str}{observed_str}")

    lines.append(
        "\n📊 "
        f"working:{status_counts['working']} "
        f"approval:{status_counts['approval']} "
        f"recovering:{status_counts['recovering']} "
        f"idle:{status_counts['idle']} "
        f"sleep:{status_counts['sleep']} "
        f"dead:{status_counts['dead']} "
        f"/ 总计 {len(members)}"
    )

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

    if (ltype == "tmux" and member_name == leader) or _is_direct_leader_member(team, member_name):
        return f"⚠️ '{member_name}' 是你自己（leader）。请直接在当前终端执行。"

    # 持久化任务（恢复时自动重发）
    full_msg = subtask
    if context.strip():
        full_msg = f"[上下文] {context}\n[子任务] {subtask}"
    mode_prefix = _mode_task_prefix(members[member_name])
    if mode_prefix:
        full_msg = mode_prefix + full_msg
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
    member_target = _member_window_target(team_name, member_name)
    if not member_target:
        ok, err_msg = _recover_and_send(team_name, member_name, session)
        if not ok:
            return f"❌ 成员终端已死且恢复失败: {err_msg}"
        recovery_msg = f"🔄 成员 '{member_name}' 已自动恢复（含上下文）\n"
        member_target = _member_window_target(team_name, member_name) or member_name

    rc, err = _send_keys(session, member_target, full_msg)
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
        if (ltype == "tmux" and name == leader) or _is_direct_leader_member(team, name):
            continue

        # 自动恢复死掉的成员窗口
        member_target = _member_window_target(team_name, name)
        if not member_target:
            extra_message = _mode_task_prefix(members[name]) + message
            ok, err_msg = _recover_and_send(team_name, name, session, extra_message=extra_message)
            if ok:
                recovered.append(name)
                results.append(f"  ✅ {name} (已恢复+广播)")
            else:
                results.append(f"  ❌ {name} (恢复失败: {err_msg})")
            time.sleep(0.3)
            continue

        full_msg = _mode_task_prefix(members[name]) + message
        rc, _ = _send_keys(session, member_target, full_msg)
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
def leader_authorize_member(team_name: str, member_name: str, choice: str = "yes") -> str:
    """
    [Leader] 对成员终端中的 CLI 授权提示发送确认选项。

    适用于成员卡在 Claude/Codex 的文件修改或命令执行 approval prompt 时。
    choice 支持:
      - yes/approve/allow/1: 选择第 1 项（通常为本次允许）
      - session/remember/dont_ask_again/2: 选择第 2 项（通常为本会话记住）
      - 3: 选择第 3 项（具体含义以成员终端提示为准）
      - enter: 只按 Enter，使用当前高亮选项

    Args:
        team_name: 团队名称
        member_name: 需要授权的成员名称
        choice: 授权选项或精确数字
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if not team.get("terminals_active"):
        return "❌ 终端未启动，无法授权。"

    members = team.get("members", {})
    if member_name not in members:
        return f"❌ 成员 '{member_name}' 不存在。可用 leader_list_team 查看。"
    if _is_direct_leader_member(team, member_name):
        return f"⚠️ '{member_name}' 是你自己（leader），无需通过 member 授权入口操作。"

    session = _find_any_session(team_name)
    if not session:
        return "❌ 未找到运行中的终端 session。"

    member_target = _member_window_target(team_name, member_name)
    if not member_target:
        return f"❌ 成员 '{member_name}' 的终端窗口不存在，无法授权。"

    choice_key = _authorization_choice_key(choice)
    if choice_key is None and (choice or "").strip().lower() != "enter":
        return (
            f"❌ 无效授权选项: {choice!r}\n"
            "可用: yes/1, session/2, 3, enter。若提示选项不同，请直接传精确数字。"
        )

    rc, err = _send_authorization_choice(session, member_target, choice_key)
    if rc != 0:
        return f"❌ 授权按键发送失败: {err}"

    label = "当前高亮项" if choice_key is None else f"第 {choice_key} 项"
    return f"✅ 已向成员 '{member_name}' 发送授权选择：{label}。"


@mcp.tool
def leader_read_member_terminal(team_name: str, member_name: str, lines: int = 80) -> str:
    """
    [Leader] 读取成员终端最近输出，便于判断其是否卡在授权提示。

    Args:
        team_name: 团队名称
        member_name: 成员名称
        lines: 读取最近多少行，范围 10-500
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    if not team.get("terminals_active"):
        return "❌ 终端未启动，无法读取。"

    members = team.get("members", {})
    if member_name not in members:
        return f"❌ 成员 '{member_name}' 不存在。可用 leader_list_team 查看。"

    session = _find_any_session(team_name)
    if not session:
        return "❌ 未找到运行中的终端 session。"

    member_target = _member_window_target(team_name, member_name)
    if not member_target:
        return f"❌ 成员 '{member_name}' 的终端窗口不存在。"

    rc, out, err = _capture_window(session, member_target, lines)
    if rc != 0:
        return f"❌ 读取成员终端失败: {err}"

    return f"📟 **{member_name}** 最近终端输出:\n\n{out or '(无输出)'}"


@mcp.tool
def leader_monitor_members(
    team_name: str,
    *,
    auto_authorize_choice: str = "",
    mark_idle_done: bool = True,
    lines: int = 120,
) -> str:
    """
    [Leader] 扫描所有成员终端，识别 approval/busy/idle/dead 状态并更新成员状态。

    - approval: 标记成员被授权提示阻塞；若成员为 auto 模式或传入 auto_authorize_choice，则自动发送授权选择
    - idle: 若成员有未完成任务，自动标记完成，使其退出 working
    - dead: 标记终端死亡，等待 leader 分配任务时自动恢复

    Args:
        team_name: 团队名称
        auto_authorize_choice: 可选，统一自动授权选项，如 session/yes/enter。为空时只自动处理 auto 成员。
        mark_idle_done: 发现空闲成员时是否将未完成任务标记完成
        lines: 每个成员读取的终端行数
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    if not team.get("terminals_active"):
        return "❌ 终端未启动，无法监控。"

    results = _monitor_team_once(
        team_name,
        auto_authorize_choice=auto_authorize_choice,
        mark_idle_done=mark_idle_done,
        lines=lines,
    )
    counts: dict[str, int] = {}
    lines_out = [f"🩺 **{team_name}** 成员状态巡检:"]
    for item in results:
        state = item.get("state", "unknown")
        counts[state] = counts.get(state, 0) + 1
        lines_out.append(f"  • {item.get('member')}: {state} ({item.get('action')})")
    summary = " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "无成员"
    lines_out.append(f"\n📊 {summary}")
    return "\n".join(lines_out)


@mcp.tool
def leader_configure_wakeup(
    team_name: str,
    enabled: bool = True,
    idle_threshold: int = 4,
    approval_alert: bool = True,
    auto_authorize_first: bool = True,
    cooldown_cycles: int = 6,
    max_wakeups_per_session: int = 10,
) -> str:
    """
    [Leader] 配置 tmux leader 的自动休息/唤醒策略。

    默认关闭；显式启用后，现有团队监控线程会在 tmux leader 空闲且成员仍工作时
    标记 leader_state=resting，并在所有成员完成或成员卡授权时注入提示唤醒 leader。
    direct leader 没有可注入终端，因此只保存配置并提示不可用。

    Args:
        team_name: 团队名称
        enabled: 是否启用自动休息/唤醒
        idle_threshold: leader 连续 idle 观测次数阈值，默认 4
        approval_alert: 成员卡授权时是否唤醒
        auto_authorize_first: 是否先让 auto_authorize 处理授权
        cooldown_cycles: 每次唤醒后的冷却周期数
        max_wakeups_per_session: 单次服务会话最多唤醒次数
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    cfg = dict(LEADER_WAKEUP_DEFAULT_CONFIG)
    cfg.update({
        "enabled": bool(enabled),
        "idle_threshold": max(1, min(int(idle_threshold), 20)),
        "approval_alert": bool(approval_alert),
        "auto_authorize_first": bool(auto_authorize_first),
        "cooldown_cycles": max(0, min(int(cooldown_cycles), 100)),
        "max_wakeups_per_session": max(1, min(int(max_wakeups_per_session), 1000)),
    })
    team["leader_wakeup_config"] = cfg
    if not enabled:
        team["leader_state"] = "active"
        team["leader_idle_streak"] = 0
    team["monitor_enabled"] = True
    team.setdefault("monitor_interval_seconds", 30)
    team.setdefault("monitor_mark_idle_done", True)
    _save(data)

    if enabled and team.get("terminals_active"):
        _start_team_monitor(team_name)

    ltype = team.get("leader_type", "")
    if ltype != "tmux":
        return (
            f"✅ 已保存 {team_name} leader wakeup 配置，但当前 leader_type={ltype or '未设置'}。\n"
            "⚠️ direct/未设置 leader 没有可注入终端，自动唤醒不会实际触发；切换为 tmux leader 后生效。"
        )

    state = "启用" if enabled else "关闭"
    return (
        f"✅ {team_name} leader wakeup 已{state}。\n"
        f"   idle_threshold={cfg['idle_threshold']} approval_alert={cfg['approval_alert']} "
        f"auto_authorize_first={cfg['auto_authorize_first']} cooldown_cycles={cfg['cooldown_cycles']} "
        f"max_wakeups_per_session={cfg['max_wakeups_per_session']}"
    )


@mcp.tool
def leader_set_member_mode(
    team_name: str,
    member_name: str = "",
    mode: str = "manual",
    auto_authorize: bool = True,
) -> str:
    """
    [Leader] 设置成员运行模式，减少 Claude/Codex 授权卡顿。

    mode:
      - manual: 默认模式，不额外放宽审批
      - auto: Claude 启动加 --permission-mode auto；Codex 启动加 --ask-for-approval never；
              leader 监控发现 approval 时自动选择 session
      - plan: Claude 启动加 --permission-mode plan；Codex 启动保留 on-request，并在任务前注入先计划不执行的约束

    Args:
        team_name: 团队名称
        member_name: 成员名；为空或 "*" 表示所有非 leader 成员
        mode: manual/auto/plan
        auto_authorize: auto 模式下是否允许 leader 监控自动授权
    """
    normalized = _normalize_member_mode(mode)
    if not normalized:
        return "❌ 无效模式。可用: manual, auto, plan。"

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    targets = []
    if not member_name or member_name == "*":
        targets = [
            name for name in members
            if not (ltype == "tmux" and name == leader)
        ]
    elif member_name in members:
        targets = [member_name]
    else:
        return f"❌ 成员 '{member_name}' 不存在。"

    for name in targets:
        info = members[name]
        info["work_mode"] = normalized
        if normalized == "auto":
            info["auto_authorize"] = bool(auto_authorize)
            info["auto_authorize_choice"] = "session"
        else:
            info["auto_authorize"] = False
            info.pop("auto_authorize_choice", None)

    team["monitor_enabled"] = True
    team.setdefault("monitor_interval_seconds", 30)
    team.setdefault("monitor_mark_idle_done", True)
    _save(data)
    _start_team_monitor(team_name)

    target_text = ", ".join(targets) if targets else "无"
    return (
        f"✅ 已设置 {team_name} 成员模式: {target_text} → {normalized}\n"
        "💡 已运行终端的 CLI 启动参数需重启/恢复后完全生效；任务文本约束和 leader 监控立即生效。"
    )


@mcp.tool
def leader_grant_member_autonomy(
    team_name: str,
    member_name: str = "",
    relaunch: bool = False,
) -> str:
    """
    [Leader] 授予成员自动执行权限，减少 Claude/Codex 频繁审批阻塞。

    行为:
      - Claude 成员: 设置为 auto 模式，后续启动使用 --permission-mode auto；
        leader 监控遇到 approval prompt 时自动选择 session。
      - Codex 成员: 设置为 auto 模式，后续启动使用 --ask-for-approval never，
        相当于一次性授予当前成员无审批执行权限。
      - 其他 agent: 记录为 auto，并依赖 leader 监控自动处理 approval prompt。

    Args:
        team_name: 团队名称
        member_name: 成员名；为空或 "*" 表示所有非 tmux leader 成员
        relaunch: 是否立即重启目标成员终端，使 CLI 启动参数立即生效。
                  默认 False，避免中断正在执行的成员任务。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    if not members:
        return f"❌ 团队 '{team_name}' 没有成员。"

    if not member_name or member_name == "*":
        targets = [
            name for name in members
            if not (ltype == "tmux" and name == leader)
        ]
    elif member_name in members:
        if ltype == "tmux" and member_name == leader:
            return f"❌ '{member_name}' 是 tmux leader，不应授予 member 自动权限。"
        targets = [member_name]
    else:
        return f"❌ 成员 '{member_name}' 不存在。"

    if not targets:
        return "⚠️ 没有可授权的非 leader 成员。"

    import datetime
    ts = datetime.datetime.now().isoformat()
    claude_targets: list[str] = []
    codex_targets: list[str] = []
    other_targets: list[str] = []

    for name in targets:
        info = members[name]
        agent = info.get("agent", "claude")
        atype = _agent_type(agent)
        info["work_mode"] = "auto"
        info["auto_authorize"] = True
        info["auto_authorize_choice"] = "session"
        info["autonomy_granted"] = True
        info["autonomy_granted_ts"] = ts
        if atype == "claude":
            info["autonomy_policy"] = "claude_permission_mode_auto"
            claude_targets.append(name)
        elif atype == "codex":
            info["autonomy_policy"] = "codex_ask_for_approval_never"
            codex_targets.append(name)
        else:
            info["autonomy_policy"] = "monitor_auto_authorize_session"
            other_targets.append(name)

    team["monitor_enabled"] = True
    team.setdefault("monitor_interval_seconds", 30)
    team.setdefault("monitor_mark_idle_done", True)
    _save(data)

    # Ensure future launches load the right MCP/permission config.
    if claude_targets:
        _write_claude_mcp(team_name)
        _write_claude_permissions(team_name)
    if codex_targets:
        _ensure_codex_mcp()
    _start_team_monitor(team_name)

    relaunch_lines: list[str] = []
    if relaunch:
        if not team.get("terminals_active"):
            relaunch_lines.append("⚠️ 终端未启动，已保存授权；下次启动生效。")
        else:
            session = _find_any_session(team_name)
            if not session:
                relaunch_lines.append("⚠️ 未找到运行中的终端 session，已保存授权；下次启动生效。")
            else:
                team_dir = _team_dir(team_name)
                for name in targets:
                    agent = members[name].get("agent", "claude")
                    target = _member_window_target(team_name, name)
                    if target:
                        _tmux(["kill-window", "-t", _tmux_target(session, target)])
                        time.sleep(0.1)
                    rc, _, err = _tmux_spawn_member(session, name, agent, team_dir)
                    if rc != 0:
                        relaunch_lines.append(f"❌ {name}: 重启失败: {err}")
                        continue
                    time.sleep(1.0)
                    ctx = _build_recovery_context(team_name, name)
                    target = _member_window_target(team_name, name) or name
                    src, serr = _send_keys(session, target, ctx)
                    suffix = "" if src == 0 else f"（恢复上下文发送失败: {serr}）"
                    relaunch_lines.append(f"🔄 {name}: 已重启并加载 auto 权限{suffix}")

    policy_lines = [
        f"✅ 已授予 {team_name} 自动权限: {', '.join(targets)}",
    ]
    if claude_targets:
        policy_lines.append(f"  • Claude auto: {', '.join(claude_targets)} → --permission-mode auto")
    if codex_targets:
        policy_lines.append(f"  • Codex full approval: {', '.join(codex_targets)} → --ask-for-approval never")
    if other_targets:
        policy_lines.append(f"  • Other auto-authorize: {', '.join(other_targets)} → monitor session approval")

    if relaunch_lines:
        policy_lines.extend(relaunch_lines)
    else:
        policy_lines.append("💡 已运行终端需 relaunch=True 或后续恢复/重启后，CLI 启动参数才完全生效；leader 监控自动授权立即生效。")

    return "\n".join(policy_lines)


@mcp.tool
def leader_configure_member_permissions(
    team_name: str,
    *,
    dangerously_skip: bool = False,
    allow_patterns: str = "",
    additional_dirs: str = "",
) -> str:
    """
    [Leader] 为团队 Claude Code 成员预配置权限策略，减少审批阻塞。

    写入团队工作目录下 .claude/settings.json，所有从该目录启动的 claude 成员自动继承。

    使用方式:
      - dangerously_skip=True: 跳过所有权限检查（仅限受信任的 sandbox 环境）
      - allow_patterns: 逗号分隔的额外工具模式，如 "Bash(npm:*),Read(/data/*)"
      - additional_dirs: 逗号分隔的额外目录，自动对每个目录添加 Edit/Write 白名单

    Args:
        team_name: 团队名称
        dangerously_skip: 跳过全部权限检查（默认 False）
        allow_patterns: 逗号分隔的允许工具模式
        additional_dirs: 逗号分隔的额外目录
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    patterns = [p.strip() for p in allow_patterns.split(",") if p.strip()] if allow_patterns else None
    dirs = [d.strip() for d in additional_dirs.split(",") if d.strip()] if additional_dirs else None

    path = _write_claude_permissions(
        team_name,
        dangerously_skip=dangerously_skip,
        allow_patterns=patterns,
        additional_dirs=dirs,
    )

    default_rule_count = 3 + len(CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS)
    mode = "🔓 跳过全部权限检查" if dangerously_skip else f"📋 已添加 {len(patterns or []) + default_rule_count} 条白名单规则"
    return (
        f"✅ {team_name} Claude Code 权限已配置 ({mode})\n"
        f"📄 {path}\n\n"
        "💡 下次 launch_team_terminals / leader_launch_member_terminal 启动的成员自动生效。\n"
        "💡 已运行的成员需要 re-launch 才能加载新权限。"
    )


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

    session = _find_any_session(team_name)
    if not session:
        team["terminals_active"] = False
        _save(data)
        return f"⚠️ 成员已记录，但未找到运行中的终端 session。"

    team_dir = _team_dir(team_name)
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()
    rc, _, err = _tmux_spawn_member(session, member_name, actual_agent, team_dir)
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

    session = _find_any_session(team_name)
    member_target = _member_window_target(team_name, member_name) if session else None

    del team["members"][member_name]
    _save(data)

    if session and member_target:
        _tmux(["kill-window", "-t", _tmux_target(session, member_target)])

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
    if _is_direct_leader_member(team, member_name):
        return f"⚠️ '{member_name}' 是当前 direct leader，不应作为 member 终端启动。"

    session = _find_any_session(team_name)
    if not session:
        return f"❌ 未找到运行中的终端 session。"

    agent = members[member_name].get("agent", "claude")
    atype = _agent_type(agent)
    team_dir = _team_dir(team_name)

    # 确保 MCP 配置就绪
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    rc, _, err = _tmux_spawn_member(session, member_name, agent, team_dir)
    if rc != 0:
        return f"❌ 创建终端失败: {err}"
    member_target = _member_window_target(team_name, member_name) or member_name

    # 等待进程就绪
    time.sleep(1.5)

    # ---- 发送恢复上下文 + 上次未完成任务 ----
    last_task = members[member_name].get("last_task", "")
    task_completed = members[member_name].get("last_task_completed", True)
    extra_lines = []

    # 始终发送恢复上下文（让成员知道团队信息和工作目录）
    recovery_ctx = _build_recovery_context(team_name, member_name)
    _send_keys(session, member_target, recovery_ctx)

    if last_task and not task_completed:
        # 任务未完成，在恢复上下文后追加任务重发
        time.sleep(0.3)
        last_context = members[member_name].get("last_context", "")
        full_msg = last_task
        if last_context:
            full_msg = f"[任务上下文] {last_context}\n[子任务] {last_task}"
        rc2, err2 = _send_keys(session, member_target, full_msg)
        if rc2 == 0:
            extra_lines.append(f"🔄 已自动重发未完成任务: {last_task[:60]}...")
        else:
            extra_lines.append(f"⚠️ 任务重发失败: {err2}")
    elif last_task and task_completed:
        extra_lines.append(f"✅ 上次任务已完成，不再重发: {last_task[:40]}...")

    result = f"✅ 成员 '{member_name}' 终端已启动（agent={agent}[{atype}], 共享上下文，含恢复上下文）。"
    if extra_lines:
        result += "\n" + "\n".join(extra_lines)
    return result


# ============================================================
# 成员协作工具（所有连接 MCP 的成员均可调用）
# ============================================================


def _safe_name(value: str) -> str:
    cleaned = []
    for ch in value:
        if ch.isalnum() or ch in ("-", "_", "."):
            cleaned.append(ch)
        else:
            cleaned.append("_")
    return "".join(cleaned).strip("_") or "unknown"


def _compact_text(text: str, limit: int = 1200) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return f"{text[:half]} ... {text[-half:]}"


def _write_member_compressed_context(
    team_name: str,
    member_name: str,
    result: str,
    artifact_path: str,
    compressed_context: str = "",
) -> str:
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {}) if member_name else {}
    context_dir = os.path.join(_share_dir(team_name), "member_contexts")
    os.makedirs(context_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_member = _safe_name(member_name or "unknown")
    context_file = os.path.join(context_dir, f"{ts}_{safe_member}.md")
    summary = compressed_context.strip() or _compact_text(result)
    last_task = _compact_text(member.get("last_task", ""), 500)
    last_context = _compact_text(member.get("last_context", ""), 500)

    lines = [
        f"# Compressed Context: {member_name or 'unknown'}",
        "",
        f"- team: {team_name}",
        f"- member: {member_name or 'unknown'}",
        f"- timestamp: {datetime.datetime.now().isoformat()}",
        f"- artifact_path: {artifact_path or '(none)'}",
        "",
        "## Task",
        last_task or "(not recorded)",
        "",
        "## Input Context",
        last_context or "(not recorded)",
        "",
        "## Outcome Summary",
        summary or "(empty)",
        "",
    ]
    with open(context_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return os.path.relpath(context_file, _share_dir(team_name))


def _build_recovery_context(team_name: str, member_name: str) -> str:
    """构建成员终端恢复时的结构化上下文消息。

    包含团队信息、工作目录、共享上下文区位置、上次未完成任务、
    以及可用 MCP 工具提示，帮助恢复后的成员快速重新定位。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})

    team_dir = _team_dir(team_name)
    share_dir = _share_dir(team_name)
    role = member.get("role", "member")
    last_task = member.get("last_task", "")
    last_context = member.get("last_context", "")
    recovery_count = member.get("recovery_count", 0)

    lines = [
        "=" * 50,
        f"[系统] 终端恢复通知 (第{recovery_count + 1}次恢复)",
        "",
        f"团队: {team_name}",
        f"角色: {role}",
        f"共享工作目录: {team_dir}",
        f"共享上下文区: {share_dir}",
    ]

    if last_task:
        lines.append(f"上次未完成任务: {last_task}")
    if last_context:
        lines.append(f"任务上下文: {last_context}")

    lines.extend([
        "",
        "💡 可用 MCP 工具:",
        "   member_read_shared       - 查看团队共享上下文区最新结果",
        "   member_report_result     - 回传任务结果",
        "   member_list_shared_files - 列出共享文件",
        "   member_send_message      - 向其他成员发送消息",
        "   member_acquire_file_lock / member_release_file_lock - 文件锁",
        "",
        "💡 请基于以上上下文继续工作，或等待 leader 分配新任务。",
        "=" * 50,
    ])
    return "\n".join(lines)


def _record_recovery_event(team_name: str, member_name: str, had_task: bool) -> None:
    """在共享上下文区 results.jsonl 中记录终端恢复事件。"""
    import datetime
    share_dir = _share_dir(team_name)
    results_file = os.path.join(share_dir, "results.jsonl")
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "member": member_name,
        "event": "terminal_recovery",
        "had_unfinished_task": had_task,
    }
    try:
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _save_death_context_snapshot(team_name: str, member_name: str) -> str:
    """在 member_contexts/ 下保存成员死亡前的上下文快照，供 leader 事后审查。"""
    import datetime
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})

    context_dir = os.path.join(_share_dir(team_name), "member_contexts")
    os.makedirs(context_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = _safe_name(member_name)
    snapshot_file = os.path.join(context_dir, f"{ts}_{safe_name}_recovery.md")

    lines = [
        f"# Recovery Snapshot: {member_name}",
        "",
        f"- team: {team_name}",
        f"- member: {member_name}",
        f"- timestamp: {datetime.datetime.now().isoformat()}",
        f"- event: terminal_died",
        "",
        "## Member State at Death",
        f"- role: {member.get('role', '')}",
        f"- agent: {member.get('agent', '')}",
        f"- last_task: {member.get('last_task', '')}",
        f"- last_context: {member.get('last_context', '')}",
        f"- last_task_completed: {member.get('last_task_completed', True)}",
        f"- recovery_count: {member.get('recovery_count', 0)}",
        "",
    ]
    with open(snapshot_file, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return os.path.relpath(snapshot_file, _share_dir(team_name))


def _recover_and_send(
    team_name: str,
    member_name: str,
    session: str,
    extra_message: str = "",
) -> tuple[bool, str]:
    """统一恢复入口：重建成员终端窗口，发送恢复上下文和可选额外消息。

    流程：保存死亡快照 → 更新恢复计数 → 重建窗口 → 发送恢复上下文 → 发送额外消息 → 记录事件。

    Returns:
        (success, message): success 为 True 表示恢复成功，message 为错误信息（成功时为空字符串）
    """
    import datetime
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    member = members.get(member_name, {})

    if not member:
        return False, f"成员 '{member_name}' 不存在"

    agent = member.get("agent", "claude")
    team_dir = _team_dir(team_name)

    # 确保 MCP 配置就绪
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    # 保存死亡前上下文快照
    had_task = bool(member.get("last_task", "")) and not member.get("last_task_completed", True)
    try:
        _save_death_context_snapshot(team_name, member_name)
    except Exception:
        pass

    # 更新恢复计数和时间戳
    member["recovery_count"] = member.get("recovery_count", 0) + 1
    member["last_recovery_ts"] = datetime.datetime.now().isoformat()
    member["last_terminal_death_ts"] = datetime.datetime.now().isoformat()
    _save(data)

    # 重建终端窗口
    rc, _, err = _tmux_spawn_member(session, member_name, agent, team_dir)
    if rc != 0:
        return False, f"终端重建失败: {err}"
    member_target = _member_window_target(team_name, member_name) or member_name

    # 等待进程就绪
    time.sleep(1.5)

    # 发送恢复上下文
    recovery_ctx = _build_recovery_context(team_name, member_name)
    _send_keys(session, member_target, recovery_ctx)

    # 发送额外消息（如广播内容或新任务）
    if extra_message.strip():
        time.sleep(0.2)
        _send_keys(session, member_target, extra_message)

    # 记录恢复事件到共享上下文区
    try:
        _record_recovery_event(team_name, member_name, had_task)
    except Exception:
        pass

    return True, ""


def _build_recovery_message_tui(team: dict, member_name: str, info: dict, team_name: str) -> str:
    """TUI 侧的恢复消息构建（与 MCP 侧 _build_recovery_context 保持格式一致）。

    此函数供 team_manger.py 导入使用，避免在 TUI 侧重复实现。
    """
    import datetime as _datetime
    team_dir = team.get("workspace_dir", "")
    share_dir = team.get("context_dir", "")
    role = info.get("role", "member")
    last_task = info.get("last_task", "")
    last_context = info.get("last_context", "")
    recovery_count = info.get("recovery_count", 0)

    lines = [
        "=" * 50,
        f"[系统] 终端恢复通知 (第{recovery_count + 1}次恢复)",
        "",
        f"团队: {team_name}",
        f"角色: {role}",
        f"共享工作目录: {team_dir}",
        f"共享上下文区: {share_dir}",
    ]

    if last_task:
        lines.append(f"上次未完成任务: {last_task}")
    if last_context:
        lines.append(f"任务上下文: {last_context}")

    lines.extend([
        "",
        "💡 可用 MCP 工具:",
        "   member_read_shared       - 查看团队共享上下文区最新结果",
        "   member_report_result     - 回传任务结果",
        "   member_list_shared_files - 列出共享文件",
        "   member_send_message      - 向其他成员发送消息",
        "",
        "💡 请基于以上上下文继续工作，或等待 leader 分配新任务。",
        "=" * 50,
    ])
    return "\n".join(lines)


@mcp.tool
def member_report_result(
    team_name: str,
    result: str,
    artifact_path: str = "",
    member_name: str = "",
    compressed_context: str = "",
) -> str:
    """
    [成员] 将任务结果回传给 leader 或其他成员。
    结果会写入共享上下文区的 results.jsonl，供所有成员读取。
    同时为本次任务生成一份压缩上下文，便于 leader 快速了解成员工作。
    提供 member_name 时会将该成员的终端退出进入休眠状态，
    等待 leader 下发新任务时自动唤醒。

    Args:
        team_name: 团队名称
        result: 任务结果摘要
        artifact_path: 可选，产出文件在共享上下文区内的路径
        member_name: 可选，上报结果的成员名称（用于标记任务完成并休眠）
        compressed_context: 可选，成员主动提供的压缩上下文；为空时根据 result/任务记录自动生成
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
            if not _is_leader(team, member_name):
                session = _find_any_session(team_name)
                member_target = _member_window_target(team_name, member_name) if session else None
                if session and member_target:
                    _tmux(["kill-window", "-t", _tmux_target(session, member_target)])
                    sleep_msg = f"\n😴 成员 '{member_name}' 已进入休眠，等待新任务唤醒"

    share_dir = _share_dir(team_name)
    results_file = os.path.join(share_dir, "results.jsonl")
    compressed_context_path = ""
    try:
        compressed_context_path = _write_member_compressed_context(
            team_name, member_name or "unknown", result, artifact_path, compressed_context
        )
    except Exception as e:
        compressed_context_path = f"生成失败: {e}"

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "member": member_name or "unknown",
        "result": result,
        "artifact_path": artifact_path,
        "compressed_context_path": compressed_context_path,
    }
    try:
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        return (
            f"✅ 结果已记录到共享上下文区{task_msg}{sleep_msg}\n"
            f"📄 {results_file}\n"
            f"🧾 压缩上下文: {compressed_context_path}\n"
            f"💡 其他成员可调用 member_read_shared 查看。"
        )
    except Exception as e:
        return f"❌ 写入失败: {e}"


def _is_leader(team: dict, member_name: str) -> bool:
    """判断成员是否为团队 leader"""
    return team.get("leader") == member_name and team.get("leader_type") == "tmux"


def _is_direct_leader_member(team: dict, member_name: str) -> bool:
    """Return True when a member record represents the current direct leader."""
    return team.get("leader_type") == "direct" and bool(team.get("leader")) and team.get("leader") == member_name


@mcp.tool
def member_read_shared(team_name: str) -> str:
    """
    [成员] 读取共享上下文区中的最新结果。
    返回 results.jsonl 中最近 10 条记录。

    Args:
        team_name: 团队名称
    """
    share_dir = _share_dir(team_name)
    results_file = os.path.join(share_dir, "results.jsonl")

    if not os.path.exists(results_file):
        return "📭 共享上下文区暂无结果。"

    try:
        with open(results_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        recent = lines[-10:]
        entries = [json.loads(line) for line in recent]

        out = [f"📋 **{team_name}** 共享上下文区最新结果 ({len(entries)} 条):"]
        for i, e in enumerate(entries, 1):
            ts = e.get("timestamp", "")[:19]
            result_text = e.get("result", "")
            artifact = e.get("artifact_path", "")
            compressed_context_path = e.get("compressed_context_path", "")
            line = f"  {i}. [{ts}] {result_text}"
            if artifact:
                line += f"\n     📎 {artifact}"
            if compressed_context_path:
                line += f"\n     🧾 {compressed_context_path}"
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

    target = _member_window_target(team_name, actual_target)
    if not target:
        return f"❌ 成员 '{actual_target}' 的终端窗口不存在。"

    full_msg = f"[来自其他成员的消息] {message}"
    rc, err = _send_keys(session, target, full_msg)
    if rc != 0:
        return f"❌ 发送失败: {err}"

    return f"✅ 消息已发送给 '{actual_target}'"


@mcp.tool
def member_list_shared_files(team_name: str) -> str:
    """
    [成员] 列出共享上下文区中的所有文件。

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
        return f"📭 共享上下文区为空\n📂 {share_dir}"

    lines = [f"📂 **{team_name}** 共享上下文区文件:", f"   {share_dir}", ""]
    for rel, size in files:
        if size < 1024:
            size_str = f"{size}B"
        elif size < 1024 * 1024:
            size_str = f"{size / 1024:.1f}KB"
        else:
            size_str = f"{size / (1024 * 1024):.1f}MB"
        lines.append(f"   📄 {rel} ({size_str})")
    return "\n".join(lines)


def _locks_file(team_name: str) -> str:
    return os.path.join(_share_dir(team_name), "file_locks.json")


def _load_file_locks(team_name: str) -> dict:
    path = _locks_file(team_name)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            locks = json.load(f)
    except Exception:
        return {}

    now = time.time()
    active = {
        key: value for key, value in locks.items()
        if float(value.get("expires_at", 0)) > now
    }
    if active != locks:
        _save_file_locks(team_name, active)
    return active


def _save_file_locks(team_name: str, locks: dict) -> None:
    with open(_locks_file(team_name), "w", encoding="utf-8") as f:
        json.dump(locks, f, indent=2, ensure_ascii=False)


def _lock_key(team_name: str, file_path: str) -> str:
    workspace = os.path.abspath(_team_dir(team_name))
    candidate = os.path.abspath(file_path if os.path.isabs(file_path) else os.path.join(workspace, file_path))
    try:
        return os.path.relpath(candidate, workspace)
    except ValueError:
        return candidate


@mcp.tool
def member_acquire_file_lock(
    team_name: str,
    member_name: str,
    file_path: str,
    purpose: str = "",
    ttl_seconds: int = 1800,
) -> str:
    """
    [成员] 申请文件修改锁，降低多个 coder 同时覆盖同一文件的风险。

    Args:
        team_name: 团队名称
        member_name: 申请锁的成员名称
        file_path: 相对共享工作目录的文件路径，或绝对路径
        purpose: 修改目的
        ttl_seconds: 锁有效期，默认 30 分钟
    """
    import datetime

    if ttl_seconds < 60:
        ttl_seconds = 60
    if ttl_seconds > 24 * 3600:
        ttl_seconds = 24 * 3600

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    if member_name not in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 不存在。"

    key = _lock_key(team_name, file_path)
    with FILE_LOCK_MUTEX:
        locks = _load_file_locks(team_name)
        existing = locks.get(key)
        if existing and existing.get("member") != member_name:
            expires = datetime.datetime.fromtimestamp(existing["expires_at"]).isoformat()
            return (
                f"🔒 文件已被 {existing.get('member')} 锁定: {key}\n"
                f"用途: {existing.get('purpose') or '(未说明)'}\n"
                f"过期: {expires}\n"
                "请先协调，或提交 patch 到共享上下文区等待合并。"
            )

        now = time.time()
        locks[key] = {
            "member": member_name,
            "purpose": purpose,
            "created_at": datetime.datetime.now().isoformat(),
            "expires_at": now + ttl_seconds,
        }
        _save_file_locks(team_name, locks)
    return f"✅ 已获得文件锁: {key}（{ttl_seconds}s）"


@mcp.tool
def member_release_file_lock(team_name: str, member_name: str, file_path: str) -> str:
    """
    [成员] 释放自己持有的文件修改锁。
    """
    key = _lock_key(team_name, file_path)
    with FILE_LOCK_MUTEX:
        locks = _load_file_locks(team_name)
        existing = locks.get(key)
        if not existing:
            return f"⚠️ 文件未锁定: {key}"
        if existing.get("member") != member_name:
            return f"❌ 文件锁属于 {existing.get('member')}，{member_name} 无法释放。"
        del locks[key]
        _save_file_locks(team_name, locks)
    return f"✅ 已释放文件锁: {key}"


@mcp.tool
def member_list_file_locks(team_name: str) -> str:
    """
    [成员] 查看共享工作目录中的活跃文件锁。
    """
    import datetime

    locks = _load_file_locks(team_name)
    if not locks:
        return "📭 当前没有活跃文件锁。"
    lines = [f"🔐 **{team_name}** 活跃文件锁:"]
    for path, info in sorted(locks.items()):
        expires = datetime.datetime.fromtimestamp(info["expires_at"]).isoformat()
        lines.append(
            f"  • {path} ← {info.get('member')}，过期 {expires}，用途: {info.get('purpose') or '(未说明)'}"
        )
    return "\n".join(lines)


@mcp.tool
def member_submit_patch(
    team_name: str,
    member_name: str,
    summary: str,
    patch: str,
    base_ref: str = "",
) -> str:
    """
    [成员] 将代码修改以 patch 形式提交到共享上下文区，供 leader 或文件锁持有人合并。
    适合多人同时需要修改同一文件时避免直接覆盖。
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    if member_name not in team.get("members", {}):
        return f"❌ 成员 '{member_name}' 不存在。"

    patch_dir = os.path.join(_share_dir(team_name), "patches")
    os.makedirs(patch_dir, exist_ok=True)
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_member = _safe_name(member_name)
    patch_name = f"{ts}_{safe_member}.patch"
    meta_name = f"{ts}_{safe_member}.json"
    patch_path = os.path.join(patch_dir, patch_name)
    meta_path = os.path.join(patch_dir, meta_name)

    with open(patch_path, "w", encoding="utf-8") as f:
        f.write(patch)
        if patch and not patch.endswith("\n"):
            f.write("\n")
    metadata = {
        "timestamp": datetime.datetime.now().isoformat(),
        "team": team_name,
        "member": member_name,
        "summary": summary,
        "base_ref": base_ref,
        "patch": os.path.relpath(patch_path, _share_dir(team_name)),
    }
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    return (
        "✅ patch 已提交到共享上下文区。\n"
        f"📄 {metadata['patch']}\n"
        f"🧾 {os.path.relpath(meta_path, _share_dir(team_name))}"
    )


def main():
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
