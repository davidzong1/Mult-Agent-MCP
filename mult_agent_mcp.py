import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

# ============================================================
# 启动保护：在 socks:// 代理环境下关闭 FastMCP 的 PyPI 版本检查
# ============================================================
# 当环境存在 ALL_PROXY=socks://127.0.0.1:7890/（httpx 不支持 socks scheme）时，
# FastMCP 启动横幅的版本检查会在端口绑定前抛 ValueError：
#   "Unknown scheme for proxy URL URL('socks://127.0.0.1:7890/')"
# 导致 MCP Server 进程起即死。版本检查仅用于横幅提示，属非必要功能。
# 必须在导入 fastmcp（其实例化 settings 单例）之前设置该环境变量，否则
# settings 会以默认值 "stable" 实例化，此处的关闭将不生效。
os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")

from fastmcp import FastMCP

from common.leader_recovery import (
    build_leader_recovery_section,
    leader_has_unfinished_work,
    active_member_tasks,
    member_pending_task,
    pending_leader_reports,
    append_leader_pending_report,
    build_leader_pending_reports_section,
)
from common.tmux_utils import (
    get_agent_user_env_prefix,
    get_proxy_env_prefix,
    member_proxy_enabled,
    member_proxy_mode,
    member_spawn_lock,
    member_window_state as common_member_window_state,
    migrate_agent_users_global_file,
    resolve_agent_model,
    resolve_member_effort,
    normalize_effort,
    CLAUDE_EFFORT_LEVELS,
    CODEX_EFFORT_LEVELS,
    build_agent_user_claude_settings,
    claude_agent_user_launch,
    merge_env_prefixes,
)
from common.atomic_write import atomic_json_write
from member_status import format_member_activity_status

mcp = FastMCP("mult agent mcp")
TEAM_DATA_LOCK = threading.RLock()
FILE_LOCK_MUTEX = threading.Lock()
AUTHORIZATION_MUTEX = threading.Lock()
# 终端创建互斥锁：保护“检查成员窗口是否存在 → 创建窗口”的原子性，
# 防止并发/重试时同一成员被重复拉起多个终端窗口。
TERMINAL_SPAWN_LOCK = threading.Lock()
LEADER_REVIVAL_LOCKS_GUARD = threading.Lock()
LEADER_REVIVAL_LOCKS: dict[str, threading.Lock] = {}
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
        # 读取旧数据，用 0600 原子写入新位置（不进 copy2 保留宽松权限）
        try:
            with open(_OLD_DATA_FILE, "r", encoding="utf-8") as f:
                seed = json.load(f)
        except Exception:
            seed = {"teams": {}}
        atomic_json_write(Path(DATA_FILE), seed)

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
        atomic_json_write(Path(DATA_FILE), data)

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
        atomic_json_write(Path(DATA_FILE), data)


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


def _remove_team_from_legacy_data_file(team_name: str) -> None:
    if not os.path.exists(_OLD_DATA_FILE):
        return
    try:
        with open(_OLD_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        teams = data.get("teams", {})
        if team_name not in teams:
            return
        del teams[team_name]
        deleted = data.setdefault(DELETED_LEGACY_TEAMS_KEY, {})
        if isinstance(deleted, dict):
            deleted[team_name] = True
        atomic_json_write(Path(_OLD_DATA_FILE), data)
    except Exception:
        pass


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


def _tmux_with_input(cmd: list[str], input_text: str, timeout: int = 10) -> tuple[int, str, str]:
    tmux_path = _find_tmux()
    if not tmux_path:
        return -1, "", "tmux 未安装，请执行 sudo apt install tmux"
    try:
        r = subprocess.run(
            [tmux_path] + cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
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
    session = _session(team)
    candidates: list[str] = []
    rc, _, _ = _tmux(["has-session", "-t", session])
    if rc == 0:
        candidates.append(session)

    rc, out, _ = _tmux(["list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        prefix = f"mcp_{team}_"
        for name in out.split("\n"):
            if name.startswith(prefix) and name not in candidates:
                candidates.append(name)

    if not candidates:
        return None

    members = _team_info(team).get("members", {})
    if members:
        scored = [(_session_member_match_count(team, candidate, members), candidate) for candidate in candidates]
        best_score, best_session = max(scored, key=lambda item: item[0])
        if best_score > 0:
            return best_session

    if session in candidates:
        return session
    return candidates[-1]


def _tmux_session_alive(team: str) -> bool:
    return _find_any_session(team) is not None


def _ensure_team_session(team_name: str) -> tuple[str | None, bool]:
    """Return an existing team tmux session, or create a bare one when the team
    is marked active but its session died unexpectedly (interruption recovery).

    A bare `__base` window hosts no CLI; member/leader windows are spawned into
    the session afterwards by the recovery flow.  When the team was intentionally
    stopped (``terminals_active`` False) no session is created.
    """
    session = _find_any_session(team_name)
    if session:
        return session, True
    team = _team_info(team_name)
    if not team or not team.get("terminals_active"):
        return None, False
    team_dir = _team_dir(team_name)
    rc, _, _err = _tmux(["new-session", "-d", "-s", _session(team_name), "-n", "__base", "-c", team_dir])
    if rc != 0:
        return None, False
    return _find_any_session(team_name), True


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


def _session_member_match_count(team_name: str, session: str, members: dict) -> int:
    records = _tmux_window_records(session)
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


def _member_window_state(
    team_name: str,
    member_name: str,
    session: str,
    *,
    window_name: str | None = None,
    new_session: bool = False,
) -> tuple[str, str]:
    """MCP 侧三态判定：委托 common.member_window_state，使用调用方传入的 session。

    返回 ('live', target) / ('absent', '') / ('unknown', reason)。
    通过注入 run_tmux=_tmux 使测试对 mcp._tmux 的 mock 依然生效。
    """
    return common_member_window_state(
        team_name,
        member_name,
        session,
        window_name=window_name,
        new_session=new_session,
        run_tmux=_tmux,
    )


def _leader_window_target(team_name: str, leader_name: str) -> str | None:
    """Find a tmux window target for a leader using by-name matching.

    Unlike _member_window_target, this does NOT depend on the leader being
    in team["members"].  It scans the live tmux session for a window whose
    name matches `leader_name`.

    Security: name matching carries no cross-session risk — the worst case
    is sending /compact text to a wrong terminal, which is harmless (compact
    just compresses context).  When no window matches, nothing is sent at all.

    Returns:
        A window target (e.g. "@0") on success, None if no reachable window.
    """
    session = _find_any_session(team_name)
    if not session:
        return None
    records = _tmux_window_records(session)
    if not records:
        return None
    by_name = next((r for r in records if r["name"] == leader_name), None)
    if by_name:
        return by_name["id"]
    return None


def _leader_terminal_restart_blocked(team_name: str, team: dict) -> bool:
    """Return whether a live leader window must be protected from restart."""
    leader = team.get("leader", "")
    return bool(
        leader
        and leader_has_unfinished_work(team)
        and _member_window_target(team_name, leader)
    )


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
    elif "\n" in text:
        buffer_name = f"mcp_inject_{os.getpid()}_{threading.get_ident()}"
        rc, _, err = _tmux_with_input(["load-buffer", "-b", buffer_name, "-"], text)
        if rc != 0:
            return rc, err
        rc, _, err = _tmux(["paste-buffer", "-b", buffer_name, "-t", target])
        _tmux(["delete-buffer", "-b", buffer_name])
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


def _target_is_claude_tmux_leader(team: dict, member_name: str) -> bool:
    if team.get("leader_type") != "tmux" or team.get("leader") != member_name:
        return False
    member = team.get("members", {}).get(member_name, {})
    agent = member.get("agent") or team.get("default_agent") or "claude"
    return _is_claude(agent)


def _send_context_to_member(
    session: str,
    target: str,
    text: str,
    *,
    confirm_submission: bool = False,
) -> tuple[int, str]:
    rc, err = _send_keys(session, target, text)
    if rc != 0:
        return rc, err
    if not confirm_submission:
        return 0, ""
    rc, err = _confirm_prompt_submission(session, target)
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


def _tail_looks_like_shell_prompt(text: str) -> bool:
    """Detect whether a terminal tail is a bare shell prompt (CLI exited/crashed).

    Used by the interruption closed loop: when a member/leader CLI process drops
    to a shell prompt (crash, OOM, /exit) while the tmux window is still alive,
    the old logic classified the pane as idle/unknown and never recovered it.
    We only match when the very last non-empty line is a short line ending in a
    shell prompt marker ($ / #) and the tail contains no busy/approval markers,
    so normal CLI output and the `❯` input prompt are never misclassified.
    """
    non_empty = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not non_empty:
        return False
    tail = non_empty[-6:]
    joined = "\n".join(tail).lower()
    for marker in (
        "thinking", "running", "reading", "searching", "editing",
        "writing", "executing", "in progress", "◼",
        "requires approval", "do you want to proceed", "do you want to allow",
        "do you want to run", "do you want to edit", "do you want to create",
    ):
        if marker in joined:
            return False
    last = tail[-1].strip()
    if len(last) > 100 or "❯" in last or "$" not in last and "#" not in last:
        return False
    return bool(re.match(r"^[\w@~/:. \+\-\[\]()=]*[$#]\s*$", last))


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

    if _tail_looks_like_shell_prompt(text):
        return "dead"

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

    if _tail_looks_like_shell_prompt(text):
        return "dead"

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
    elif reason == "report":
        report = details.get("report") or {}
        reporter = report.get("member") or "unknown"
        result = _compact_text(report.get("result") or "", 300)
        headline = "[system] Leader activation: a member reported a result."
        extra = f"Report from {reporter}: {result}"
        if report.get("artifact_path"):
            extra += f" | artifact: {report['artifact_path']}"
        extra += (
            "\n查看共享上下文: member_read_shared 或读取 member_contexts/ 下的压缩上下文。"
        )
    else:
        headline = "[system] Leader wakeup: all tracked member tasks appear complete."
        extra = "Review the shared context and finish the team handoff."

    return "\n".join([
        headline,
        f"Team: {team_name}",
        extra,
        "Member snapshot:",
        *status_lines,
        "",
        "[token 高效] 判断成员完成情况优先用 leader_check_member_status（纯数据层，零终端读取）；",
        f"阅读已完成工作用 member_read_shared 或 member_read_file 读 {_share_dir(team_name)}/member_contexts/ 下的压缩上下文；",
        "不要轮询 leader_read_member_terminal（终端 dump 最耗 token）。",
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
        rc, err = _send_context_to_member(
            session,
            leader_target,
            message,
            confirm_submission=_target_is_claude_tmux_leader(latest_team, leader),
        )
        return {"action": action, "injected": rc == 0, "error": err}

    return {"action": "none"}


def _notify_leader_of_report(team_name: str, entry: dict) -> dict:
    """Member-report → leader activation (回报激活机制).

    当成员调用 member_report_result 回报结果时:
      - tmux leader 终端存活: 处于 resting + wakeup 启用 + 空闲时注入回报摘要并标记 active,
        立即激活 leader；否则只持久化回报。
      - tmux leader 终端已死: 不做终端操作（dead-leader 重建由 member_report_result 内
        独立的 leader revival 闭环处理，避免与本激活原语重复触发）。
      - direct / 非 tmux leader: 不做终端操作，回报持久化在 leader_pending_reports，
        leader 重新进入后用 leader_activate 查看确认。

    返回 {injected, leader, reason/error} 供调用方拼接提示。
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    if not team:
        return {"injected": False, "reason": "no-team"}
    leader = team.get("leader", "")
    if not leader:
        return {"injected": False, "reason": "no-leader", "leader": ""}
    if team.get("leader_type") != "tmux":
        return {"injected": False, "leader": leader, "reason": "not-tmux-leader"}

    cfg = _leader_wakeup_config(team)
    session = _find_any_session(team_name)

    # ---- leader 终端存活：resting + wakeup 启用 + 空闲才注入 ----
    if session and not _leader_window_is_dead(team_name, team, session):
        if (
            cfg["enabled"]
            and team.get("leader_state") == "resting"
            and _leader_terminal_is_idle(team_name, team)
        ):
            leader_target = _member_window_target(team_name, leader) or leader
            message = _build_leader_wakeup_message(team_name, "report", {"report": entry})
            rc, err = _send_context_to_member(
                session,
                leader_target,
                message,
                confirm_submission=_target_is_claude_tmux_leader(team, leader),
            )
            if rc != 0:
                return {"injected": False, "leader": leader, "error": err}

            def update_wakeup(latest_team: dict) -> dict:
                latest_team["leader_state"] = "active"
                latest_team["leader_idle_streak"] = 0
                latest_team["leader_wakeup_reason"] = "report"
                latest_team["leader_last_wakeup_ts"] = datetime.datetime.now().isoformat()
                return {"injected": True, "leader": leader}

            return _update_team_data(team_name, update_wakeup) or {
                "injected": False,
                "leader": leader,
                "reason": "update-failed",
            }
        return {"injected": False, "leader": leader, "reason": "leader-live"}

    # ---- leader 终端已死：由 member_report_result 的独立 revival 闭环处理 ----
    return {"injected": False, "leader": leader, "reason": "leader-dead"}


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
    # 中断闭环：巡检时若 leader 终端已死则自动重建（幂等，活跃 leader 不受影响）
    revived, revive_msg = _maybe_revive_leader(team_name, reason="patrol")
    return {
        "leader": leader_result,
        "members": member_results,
        "action": executed,
        "leader_revived": revived,
        "leader_revive_msg": revive_msg,
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
        if member.get("last_task") and not member.get("last_task_completed", True):
            if member.get("recovery_count", 0) >= int(team.get("monitor_max_recoveries", 3)):
                return {"member": member_name, "state": "dead", "action": "recovery-limit"}
            # 整个 tmux session 中断：若团队仍标记 active，则重建 session 后恢复成员
            restored, _ok = _ensure_team_session(team_name)
            if restored:
                ok, msg = _recover_and_send(team_name, member_name, restored)
                return {"member": member_name, "state": "dead", "action": "recovered" if ok else f"recover-failed:{msg}"}
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

    if state == "dead":
        # 进程已退出掉到 shell 提示符（崩溃/OOM/手动退出），但 tmux 窗口仍存活：
        # 若有未完成任务，先清理旧窗口再重建，避免同名窗口被复用为 <name>(1)。
        member["blocked_reason"] = "crashed"
        member["last_blocked_ts"] = now
        if member.get("last_task") and not member.get("last_task_completed", True):
            if member.get("recovery_count", 0) >= int(team.get("monitor_max_recoveries", 3)):
                _save(data)
                return {"member": member_name, "state": "dead", "action": "recovery-limit"}
            _tmux(["kill-window", "-t", _tmux_target(session, member_target)])
            time.sleep(0.3)
            ok, msg = _recover_and_send(team_name, member_name, session)
            _save(data)
            return {"member": member_name, "state": "dead", "action": "recovered" if ok else f"recover-failed:{msg}"}
        member.pop("blocked_reason", None)
    elif state == "approval":
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
            # _finalize_agent_completion does its own load/save internally;
            # save our state first, then reload afterwards to merge its changes
            _save(data)
            synthetic_result = _build_monitor_completion_result(member)
            _finalize_agent_completion(
                team_name, member_name, synthetic_result,
                is_leader=False,
            )
            # Reload to pick up compact_sent timestamp written by finalizer
            data = _load()
            team = data.get("teams", {}).get(team_name, {})
            members = team.get("members", {})
            member = members.get(member_name, {})
            # Keep an audit marker: an explicit member_report_result after a
            # monitor-only completion is allowed one authoritative /compact
            # submission (ordinary duplicate reports remain idempotent).
            if member.get("compact_sent"):
                member["compact_sent_by_monitor"] = True
                _save(data)
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
    results = []
    for name in members:
        if _is_leader_member(team, name):
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


def _default_member_agent(team: dict) -> str:
    return (team.get("default_agent") or "claude").strip() or "claude"


def _member_agent(team: dict, member_info: dict) -> str:
    return (member_info.get("agent") or _default_member_agent(team)).strip() or "claude"


def _resolve_new_member_agent(team: dict, agent: str = "", *, use_explicit_agent: bool = False) -> tuple[str, bool]:
    """Resolve new member agent without letting the current leader agent leak into defaults."""
    default_agent = _default_member_agent(team)
    explicit = (agent or "").strip()
    if use_explicit_agent and explicit:
        return explicit, True
    return default_agent, False


ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "coder": (
        "coder", "code", "coding", "implement", "implementation", "developer", "dev",
        "fix", "bug", "feature", "refactor", "开发", "实现", "编码", "修复", "功能", "重构",
    ),
    "tester": (
        "tester", "test", "tests", "qa", "verify", "verification", "validate",
        "测试", "验证", "验收", "用例",
    ),
    "reviewer": (
        "reviewer", "review", "audit", "risk", "compatibility", "acceptance",
        "评审", "审查", "复核", "风险", "兼容", "验收标准",
    ),
    "analyst": (
        "analyst", "analysis", "analyze", "design", "architecture", "proposal",
        "讨论", "分析", "方案", "设计", "架构",
    ),
    "writer": (
        "writer", "docs", "doc", "documentation", "readme",
        "文档", "说明", "手册",
    ),
}


def _split_csv(value: str) -> list[str]:
    return [part.strip() for part in (value or "").replace("，", ",").split(",") if part.strip()]


def _normalize_role(role: str) -> str:
    return (role or "").strip().lower().replace(" ", "_")


def _is_leader_member(team: dict, member_name: str) -> bool:
    leader = team.get("leader", "")
    return (team.get("leader_type") == "tmux" and member_name == leader) or _is_direct_leader_member(team, member_name)


def _member_role(member: dict) -> str:
    return _normalize_role(member.get("role") or "member")


def _role_matches(required_role: str, member_role: str) -> bool:
    required = _normalize_role(required_role)
    actual = _normalize_role(member_role)
    if not required or not actual:
        return False
    if required == actual:
        return True
    if required in actual or actual in required:
        return True
    return actual in ROLE_KEYWORDS.get(required, ())


def _infer_required_roles(team: dict, task: str, required_roles: str = "") -> list[str]:
    explicit = [_normalize_role(role) for role in _split_csv(required_roles)]
    if explicit:
        return list(dict.fromkeys(explicit))

    text = (task or "").lower()
    roles: list[str] = []
    existing_roles = [
        _member_role(info)
        for name, info in team.get("members", {}).items()
        if not _is_leader_member(team, name)
    ]
    for role in existing_roles:
        if role and role != "member" and role in text:
            roles.append(role)
    for role, keywords in ROLE_KEYWORDS.items():
        if any(keyword.lower() in text for keyword in keywords):
            roles.append(role)
    return list(dict.fromkeys(role for role in roles if role))


def _member_is_busy_for_discussion(member: dict) -> bool:
    observed = (member.get("last_observed_state") or "").lower()
    if observed in {"busy", "approval", "recovering"}:
        return True
    return bool(member.get("last_task")) and not member.get("last_task_completed", True)


def _make_member_name_for_role(team: dict, role: str) -> str:
    safe_role = _safe_name(_normalize_role(role) or "member")
    default_agent = _safe_name(_agent_type(_default_member_agent(team)) or "agent")
    base = f"{safe_role}-{default_agent}"
    members = team.setdefault("members", {})
    if base not in members:
        return base
    index = 2
    while f"{base}-{index}" in members:
        index += 1
    return f"{base}-{index}"


def _ensure_members_for_roles(team_name: str, team: dict, roles: list[str], *, create_missing: bool) -> tuple[list[str], list[str]]:
    members = team.setdefault("members", {})
    selected: list[str] = []
    created: list[str] = []
    for role in roles:
        matches = [
            name for name, info in members.items()
            if not _is_leader_member(team, name) and _role_matches(role, _member_role(info))
        ]
        if matches:
            selected.extend(matches)
            continue
        if not create_missing:
            continue

        member_name = _make_member_name_for_role(team, role)
        actual_agent, _ = _resolve_new_member_agent(team)
        members[member_name] = {
            "role": role,
            "model": "",
            "agent": actual_agent,
            "last_task": "",
            "last_context": "",
            "last_task_completed": True,
        }
        selected.append(member_name)
        created.append(member_name)

    return list(dict.fromkeys(selected)), created


def _send_message_to_members(team_name: str, team: dict, target_members: list[str], message: str) -> tuple[list[str], list[str]]:
    session = _find_any_session(team_name)
    if not session:
        return [], ["未找到运行中的终端 session"]

    sent: list[str] = []
    failures: list[str] = []
    members = team.get("members", {})
    for name in target_members:
        if name not in members:
            failures.append(f"{name}: missing")
            continue
        if _is_leader_member(team, name):
            failures.append(f"{name}: leader-skip")
            continue
        full_msg = _mode_task_prefix(members[name]) + message
        member_target = _member_window_target(team_name, name)
        if not member_target:
            ok, err_msg = _recover_and_send(team_name, name, session, extra_message=full_msg)
            if ok:
                sent.append(name)
            else:
                failures.append(f"{name}: {err_msg}")
            time.sleep(0.3)
            continue
        rc, err = _send_keys(session, member_target, full_msg)
        if rc == 0:
            sent.append(name)
        else:
            failures.append(f"{name}: {err}")
        time.sleep(0.05)
    return sent, failures


def _select_task_members(
    team_name: str,
    task: str,
    *,
    required_roles: str = "",
    create_missing: bool = True,
    fallback_all: bool = False,
) -> dict:
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return {"error": f"❌ 团队 '{team_name}' 不存在。"}

    roles = _infer_required_roles(team, task, required_roles)
    spawn_failures: list[str] = []
    # 角色成员创建 + 保存 在数据锁内原子执行，避免并发创建产生重名成员。
    with TEAM_DATA_LOCK:
        data = _load()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return {"error": f"❌ 团队 '{team_name}' 不存在。"}
        selected, created = _ensure_members_for_roles(team_name, team, roles, create_missing=create_missing)
        if not selected and fallback_all:
            selected = [
                name for name in team.get("members", {})
                if not _is_leader_member(team, name)
            ]
        if created:
            _save(data)
    if created:
        if team.get("terminals_active"):
            session = _find_any_session(team_name)
            if session:
                team_dir = _team_dir(team_name)
                _write_claude_mcp(team_name)
                _ensure_codex_mcp()
                for name in created:
                    rc, _, err = _tmux_spawn_member(session, name, _member_agent(team, team["members"][name]), team_dir)
                    if rc == 0:
                        time.sleep(1.0)
                        target = _member_window_target(team_name, name) or name
                        _send_keys(session, target, _build_member_initial_context(team_name, name))
                    else:
                        spawn_failures.append(f"{name}: {err}")
                    time.sleep(0.1)
    return {
        "team": team,
        "roles": roles,
        "selected": selected,
        "created": created,
        "spawn_failures": spawn_failures,
    }


def _is_discussion_task(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in ("讨论", "分析", "头脑风暴", "brainstorm", "discussion", "discuss", "analyze"))


def _discussion_file(team_name: str) -> str:
    return os.path.join(_share_dir(team_name), "discussion_results.jsonl")


def _discussion_entry(team: dict) -> dict:
    discussion = team.setdefault("discussion", {})
    discussion.setdefault("enabled", False)
    discussion.setdefault("status", "idle")
    discussion.setdefault("round", 0)
    discussion.setdefault("max_rounds", 3)
    discussion.setdefault("participants", [])
    discussion.setdefault("conclusions", {})
    return discussion


def _discussion_summary(team: dict) -> str:
    discussion = _discussion_entry(team)
    conclusions = discussion.get("conclusions", {})
    round_key = str(discussion.get("round", 0))
    current = conclusions.get(round_key, {}) if isinstance(conclusions, dict) else {}
    lines = [
        f"讨论主题: {discussion.get('topic') or '(未设置)'}",
        f"轮次: {discussion.get('round', 0)}/{discussion.get('max_rounds', 3)}",
        f"参与成员: {', '.join(discussion.get('participants', [])) or '无'}",
        "成员最后结论:",
    ]
    if not current:
        lines.append("- 暂无")
    else:
        for member, conclusion in current.items():
            lines.append(f"- {member}: {_compact_text(conclusion, 500)}")
    return "\n".join(lines)


def _write_discussion_final_entry(team_name: str, team: dict) -> None:
    """讨论结束时，将最终结论写入 discussion_results.jsonl 供共享上下文查阅。"""
    import datetime

    discussion = _discussion_entry(team)
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "type": "discussion_ended",
        "session_id": discussion.get("session_id", ""),
        "topic": discussion.get("topic", ""),
        "total_rounds": discussion.get("round", 0),
        "max_rounds": discussion.get("max_rounds", 3),
        "ended_reason": discussion.get("ended_reason", ""),
        "participants": discussion.get("participants", []),
        "conclusions": discussion.get("conclusions", {}),
    }
    try:
        disc_file = _discussion_file(team_name)
        os.makedirs(os.path.dirname(disc_file), exist_ok=True)
        with open(disc_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _claude_agent_args(
    agent_cmd: str,
    mode: str,
    *,
    dangerously_skip_permissions: bool = False,
    allowed_tools: list[str] | None = None,
    model: str = "",
    settings_path: str = "",
    effort: str = "",
) -> list[str]:
    """Build CLI args for a Claude Code member.

    Member mode → CLI --permission-mode mapping:
      auto   → acceptEdits  (auto-approve Edit/Write; Bash prompts → monitor authorizes)
      plan   → plan         (read-only; no modifications)
      manual → (no flag)    (all tools prompt for approval)

    We use "acceptEdits" instead of "auto" because "auto" hard-denies tools
    not in the allow list (→ "bash auto mode denied"), while "acceptEdits"
    generates prompts that the leader monitor can auto-authorize.
    """
    args = [agent_cmd]
    normalized = _normalize_member_mode(mode)
    if dangerously_skip_permissions:
        args.append("--dangerously-skip-permissions")
    elif normalized == "auto":
        args.extend(["--permission-mode", "acceptEdits"])
    elif normalized == "plan":
        args.extend(["--permission-mode", "plan"])
    if allowed_tools:
        args.extend(["--allowedTools", ",".join(allowed_tools)])
    if settings_path:
        # 每终端私有 --settings 覆盖（优先级高于 user/project settings），
        # 让 agent user 的 BASE_URL/key 接管在用户级 settings env 下仍生效。
        args.extend(["--settings", settings_path])
    if model:
        args.extend(["--model", model])
    # 成员级 effort 覆盖：Claude Code 原生 --effort（low/medium/high/xhigh/max）
    normalized_effort = normalize_effort(effort, "claude")
    if normalized_effort in CLAUDE_EFFORT_LEVELS:
        args.extend(["--effort", normalized_effort])
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


def _member_delivery_contract() -> str:
    return "\n".join([
        "[交付格式]",
        "完成后调用 member_report_result，result 仅包含:",
        "1. 结论",
        "2. 修改文件",
        "3. 验证/测试",
        "4. 风险/阻塞",
        "compressed_context <= 200 字；不要复述过程日志。",
    ])


def _build_member_task_payload(subtask: str, context: str = "", reason: str = "") -> tuple[str, str]:
    task_text = subtask.strip()
    compact_context = _compact_text(context, 700) if context.strip() else ""
    lines = ["[子任务]", task_text]
    if compact_context:
        lines.extend(["", "[必要上下文]", compact_context])
    if reason:
        lines.extend(["", "[分配原因]", _compact_text(reason, 180)])
    lines.extend(["", _member_delivery_contract()])
    return "\n".join(lines), compact_context


def _build_member_initial_context(team_name: str, member_name: str) -> str:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    role = member.get("role", "member")
    agent = _member_agent(team, member)
    leader = team.get("leader", "")
    leader_type = team.get("leader_type", "")
    mode = _member_mode(member)

    lines = [
        f"[系统] Multi-Agent MCP 成员上下文: team='{team_name}'",
        f"你的团队成员身份绑定: team='{team_name}', member_name='{member_name}', role='{role}', agent='{agent}'。",
        "团队成员表中同名成员记录就是你本人；不要冒用其他成员或 leader 的身份。",
        f"模式: {mode}; Leader: {leader or 'direct'} ({leader_type or 'direct'})",
        f"共享工作目录: {_team_dir(team_name)}",
        f"共享上下文区: {_share_dir(team_name)}",
        "常用工具: member_report_result, member_read_shared, member_send_message, member_acquire_file_lock, member_release_file_lock, member_submit_patch。",
        "只读取完成当前任务必需的文件；信息不足时先向 leader 提问。",
        _member_delivery_contract(),
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
    prompt: str = "",
) -> tuple[int, str, str]:
    """启动成员 tmux 窗口，统一处理 workspace 与 agent 类型差异。

    对于 claude 成员，自动写入 .claude/settings.json 预配置权限以减少审批阻塞。
    ``prompt`` 仅对 codex agent 生效（作为 CLI 位置参数传入）；claude agent 的
    初始提示由调用方在启动后通过 send-keys 注入。
    """
    name = window_name or member_name
    if new_session:
        cmd = ["new-session", "-d", "-s", session, "-n", name]
    else:
        cmd = ["new-window", "-t", session, "-n", name]

    team_name = _resolve_team_name_from_session(session)
    member_info = _load().get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
    mode = _member_mode(member_info)

    # 代理前缀：env http_proxy=URL ...（成员覆盖优先）
    proxy_prefix = get_proxy_env_prefix(team_name, member_name)

    # Agent User 环境变量前缀：仅在接管开关开启时注入（临时接管系统默认 agent 用户）
    atype = _agent_type(agent)
    agent_user_prefix = get_agent_user_env_prefix(team_name, member_name, atype)

    # 解析 model 用于显式 --model CLI flag（绕过 env var 对特殊字符的脆弱性）
    resolved_model = resolve_agent_model(team_name, member_name)

    # 成员级 effort 覆盖：三态解析（显式级别 / 继承 Agent 用户默认 / 关闭）
    resolved_effort = resolve_member_effort(team_name, member_name, atype)

    if _is_codex(agent):
        cmd.extend(agent_user_prefix + proxy_prefix + _codex_command(agent, team_dir, prompt=prompt, member_mode=mode, model=resolved_model, effort=resolved_effort))
    else:
        # Claude / 其他 agent: 预配置权限 + 从共享工作目录启动
        _write_claude_permissions(team_name, dangerously_skip=dangerously_skip_permissions)

        # 私有 settings 目录权限收紧失败时 fail closed，返回可见错误而非无锁继续
        try:
            au_prefix, claude_settings_path = claude_agent_user_launch(team_name, member_name)
        except RuntimeError as e:
            return -1, "", str(e)

        agent_args = _claude_agent_args(
            agent,
            mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=resolved_model,
            settings_path=claude_settings_path,
            effort=resolved_effort,
        )
        cmd.extend(["-c", team_dir] + merge_env_prefixes(au_prefix, proxy_prefix) + agent_args)

    # 幂等 + 互斥：进程内 TERMINAL_SPAWN_LOCK + 跨进程 flock(member_spawn_lock) 双层保护，
    # "检查窗口状态 + 创建(new-window/new-session)" 在统一临界区内原子执行。
    # 三态判定：确认存活 → 复用；确认缺失 → 创建；无法确认（查询失败）→
    # 返回可见错误而非盲目 new-window，避免瞬时失败时恰好重复创建。
    with TERMINAL_SPAWN_LOCK:
        try:
            with member_spawn_lock(team_name, member_name):
                if new_session:
                    state, _ = _member_window_state(team_name, member_name, session, new_session=True)
                    if state == "live":
                        _remember_member_window_id(team_name, member_name, session, name)
                        return 0, "", "session already exists"
                else:
                    state, detail = _member_window_state(team_name, member_name, session, window_name=name)
                    if state == "live":
                        _remember_member_window_id(team_name, member_name, session, name)
                        return 0, "", "window already exists"
                    if state == "unknown":
                        return -1, "", f"无法确认成员终端状态（{detail}），为避免重复创建已安全停止，请稍后重试"
                result = _tmux(cmd)
                if result[0] == 0:
                    _remember_member_window_id(team_name, member_name, session, name)
                return result
        except (OSError, RuntimeError) as e:
            # 跨进程锁 fail closed：锁不可用时不得无锁创建，转为可见错误
            return -1, "", f"无法获取跨进程成员 spawn 锁: {e}"


def _codex_command(agent_cmd: str, team_dir: str, prompt: str = "", member_mode: str = "", *, model: str = "", effort: str = "") -> list[str]:
    """构造 codex 成员启动命令。

    effort 经 `-c model_reasoning_effort="<level>"` 注入：Codex CLI 通过
    -c/--config 覆盖 config.toml 的 model_reasoning_effort（本机 Codex 已
    接受该配置）。effort 归一化后为受限枚举，无 shell 元字符。
    """
    cmd = [agent_cmd, "-C", team_dir]
    cmd.extend(_codex_mode_args(member_mode))
    if model:
        cmd.extend(["--model", model])
    normalized_effort = normalize_effort(effort, "codex")
    if normalized_effort in CODEX_EFFORT_LEVELS:
        cmd.extend(["-c", f'model_reasoning_effort="{normalized_effort}"'])
    if prompt:
        cmd.append(prompt)
    return cmd


def _touch_leader_activity(team: dict) -> None:
    import datetime

    team["leader_work_state"] = "active"
    team["leader_last_activity_ts"] = datetime.datetime.now().isoformat()


def _record_leader_task_start(team: dict, task: str, context: str = "") -> None:
    import datetime

    clean_task = (task or "").strip()
    if not clean_task:
        return
    now = datetime.datetime.now().isoformat()
    team["leader_last_task"] = clean_task
    team["leader_last_context"] = (context or "").strip()
    team["leader_last_task_completed"] = False
    team["leader_work_state"] = "active"
    team["leader_task_started_ts"] = now
    team["leader_last_activity_ts"] = now
    team["leader_recovery_count"] = 0
    team["leader_revival_count"] = 0  # 新任务重置复活计数，允许新一轮中断恢复
    team.pop("leader_compact_sent", None)  # 新任务重置，允许下一次 /compact
    if _is_discussion_task(clean_task):
        discussion = _discussion_entry(team)
        discussion["enabled"] = True
        discussion["forced_by_task"] = True
        discussion["status"] = "ready"
        discussion["topic"] = clean_task


def _record_leader_reentry(team: dict) -> None:
    import datetime

    if not leader_has_unfinished_work(team):
        team["leader_work_state"] = "idle"
        return
    team["leader_recovery_count"] = int(team.get("leader_recovery_count", 0)) + 1
    team["leader_last_reentry_ts"] = datetime.datetime.now().isoformat()
    team["leader_work_state"] = "active"


def _recent_shared_results(team_name: str, limit: int = 5) -> list[dict]:
    results_file = os.path.join(_share_dir(team_name), "results.jsonl")
    if not os.path.exists(results_file):
        return []
    try:
        with open(results_file, "r", encoding="utf-8") as f:
            lines = f.readlines()[-limit:]
        entries = []
        for line in lines:
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return entries
    except Exception:
        return []


def _build_leader_recovery_context(team_name: str) -> str:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    team_dir = _team_dir(team_name)
    share_dir = _share_dir(team_name)
    lines = [
        f"🧭 **{team_name}** leader 恢复上下文",
        f"   模式: {'继续未完成工作' if leader_has_unfinished_work(team) else '待机'}",
        f"   工作目录: {team_dir}",
        f"   共享上下文: {share_dir}",
    ]
    lines.extend(build_leader_recovery_section(team_name, team, team_dir, share_dir))

    recent = _recent_shared_results(team_name)
    lines.append("")
    lines.append("最近共享结果:")
    if not recent:
        lines.append("  - 暂无 results.jsonl 记录")
    else:
        for entry in recent:
            ts = (entry.get("timestamp") or "")[:19]
            member = entry.get("member") or "unknown"
            result = _compact_text(entry.get("result") or entry.get("event") or "", 300)
            artifact = entry.get("artifact_path") or ""
            line = f"  - [{ts}] {member}: {result or '(empty)'}"
            if artifact:
                line += f" | artifact: {artifact}"
            lines.append(line)
    return "\n".join(lines)


def _leader_system_prompt(team_name: str, task: str = "") -> str:
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    leader = team.get("leader", "")
    leader_info = members.get(leader, {}) if leader else {}
    leader_role = leader_info.get("role") or "leader"
    leader_agent = _member_agent(team, leader_info)
    teammates = [
        f"{name}(role={info.get('role') or 'member'}, agent={_member_agent(team, info)})"
        for name, info in members.items()
        if name != leader
    ]
    lines = [
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的 leader。",
        f"你的团队成员身份: member_name='{leader or '(未设置)'}', role='{leader_role}', agent='{leader_agent}'。",
        f"leader_list_team 中名为 '{leader or '(未设置)'}' 且标记为 leader 的成员记录就是你本人，不是外部成员。",
        "不要把自己的 leader 成员记录当作可分配对象；不要向自己分配子任务，也不要为了排除自己而剔除 leader 身份。",
        f"创建新成员时默认必须使用团队 default_agent='{_default_member_agent(team)}'；不要把你自己的 agent='{leader_agent}' 当作新成员默认 agent。",
        "只有用户明确要求覆盖 agent 时，才在 add_member/leader_add_member 中设置 use_explicit_agent=True。",
        "必须使用本项目 MCP 工具协调已有团队成员，不要使用 Codex 内置 spawn_agent / sub-agent 代替团队成员。",
        "开始后先调用 leader_list_team 查看成员，再用 leader_select_task_members 分析需要参与的角色。",
        "分配任务优先使用 leader_assign_task_to_relevant 或 leader_broadcast_to_relevant；只有确需全员同步时才使用 leader_broadcast。",
        "讨论/分析类任务使用 leader_start_discussion 强制开启讨论模式，并用 leader_discussion_next_round 收敛，最多 3 轮。",
        "监控成员完成情况优先用 leader_check_member_status（纯数据层，零终端读取）；阅读成员产出用 member_read_shared 或 member_read_file 读共享上下文 member_contexts/ 下的压缩上下文，不要轮询 leader_read_member_terminal（终端 dump 最耗 token）。",
        f"团队共享工作目录: {_team_dir(team_name)}",
        f"团队共享上下文区: {_share_dir(team_name)}",
    ]
    if teammates:
        lines.append("已有可分配成员（不包含你）: " + "; ".join(teammates))
    else:
        lines.append("已有可分配成员（不包含你）: 暂无。")
    if task.strip():
        lines.extend(["", "总任务:", task.strip()])
    lines.extend(build_leader_recovery_section(team_name, team, _team_dir(team_name), _share_dir(team_name)))
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
        # 默认允许团队工作目录内的 Edit 操作；只用 Edit(path) 规则：
        # Claude Code v2.1.210+ 只按 Edit/Read 匹配文件权限，Write(path) 规则
        # 被接受但永不生效，还会在启动时打印告警。
        allow.extend([
            f"Edit({team_dir}/*)",
            "Bash(git:*)",
            *CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS,
        ])
        if additional_dirs:
            for d in additional_dirs:
                allow.append(f"Edit({d}/*)")
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
def team_get_default_agent(team_name: str) -> str:
    """
    查看团队默认成员 agent。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    agent = _default_member_agent(team)
    atype = _agent_type(agent)
    return f"🔧 团队 '{team_name}' 默认成员 agent: {agent} [{atype}]。"


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
    _remove_team_from_legacy_data_file(team_name)

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
    use_explicit_agent: bool = False,
) -> str:
    """
    向团队添加成员。

    Args:
        team_name: 团队名称
        member_name: 成员名称（团队内唯一）
        role: 角色标识（如 leader, coder, reviewer, tester）
        model: 模型名
        agent: 可选终端启动命令。默认忽略并继承团队默认 agent
        use_explicit_agent: True 时才使用 agent 覆盖团队默认 agent
    """
    # 检查存在 → 写入 → 保存 在数据锁内原子执行，避免并发创建同名成员。
    with TEAM_DATA_LOCK:
        data = _load()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return f"❌ 团队 '{team_name}' 不存在。"

        if member_name in team.get("members", {}):
            return f"❌ 成员 '{member_name}' 已存在。"

        actual_agent, used_explicit_agent = _resolve_new_member_agent(
            team,
            agent,
            use_explicit_agent=use_explicit_agent,
        )
        atype = _agent_type(actual_agent)

        team["members"][member_name] = {
            "role": role,
            "model": model,
            "agent": actual_agent,
        }
        _save(data)
    source = "显式指定" if used_explicit_agent else "团队默认"
    return f"✅ 成员 '{member_name}' 已加入 '{team_name}'（agent={actual_agent} [{atype}]，来源={source}, role={role or '无'}）。"


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
    proxy_config = team.get("proxy", {})
    proxy_default = "✅" if proxy_config.get("enabled") else "❌"
    lines = [
        f"👥 **{team_name}** ({len(members)} 人)  [默认 agent: {default_agent}]  [代理: {proxy_default}]"
    ]

    if ltype == "direct":
        lines.append(f"   👑 Leader: **你（当前会话）** ← 直接控制")

    for i, (name, info) in enumerate(members.items(), 1):
        role = info.get("role", "")
        agent = _member_agent(team, info)
        atype = _agent_type(agent)
        is_ldr = " 👑LEADER" if (name == leader and ltype == "tmux") else ""
        extras = [f"{agent}[{atype}]"]
        if role:
            extras.insert(0, role)
        proxy_mode = member_proxy_mode(info)
        if proxy_mode != "inherit":
            extras.append("🔒proxy" if proxy_mode == "enabled" else "🚫proxy")
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

    agent = _member_agent(team, team["members"][member_name])
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
        _record_leader_reentry(team)
        _save(data)
        return (
            f"✅ 你已经是 '{team_name}' 的 leader（直接控制模式）。\n\n"
            + _build_leader_recovery_context(team_name)
        )

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
    _record_leader_reentry(team)
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
        "",
        _build_leader_recovery_context(team_name),
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
        agent = _member_agent(team, info)
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

    # 任务进行中保护仍在线的 leader；leader 已离线时允许恢复启动。
    if _leader_terminal_restart_blocked(team_name, team):
        return (
            f"❌ 团队 '{team_name}' 任务进行中，禁止重启/重拉起 leader 终端。\n"
            "   请等待所有成员和 leader 任务完成后重试，或手动 leader_mark_task_complete 标记完成。\n"
            "   💡 如需单独重启成员终端，可使用 leader_launch_member_terminal。"
        )

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

    if task.strip():
        _record_leader_task_start(team, task)
    else:
        _record_leader_reentry(team)
    _save(data)

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
        batch_failures: list[str] = []

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
            first_agent = _member_agent(team, first_info)
            rc, _, err = _tmux_spawn_member(
                session, first_name, first_agent, team_dir, new_session=True,
            )
            if rc != 0:
                return f"❌ 创建终端失败: {err}"
            created.append((first_name, first_agent))

            for name, info in non_leader_members[1:]:
                agent = _member_agent(team, info)
                rc, _, err = _tmux_spawn_member(session, name, agent, team_dir)
                if rc == 0:
                    created.append((name, agent))
                else:
                    batch_failures.append(f"{name}: {err}")
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
        launch_failure_note = ""
        if batch_failures:
            launch_failure_note = "\n⚠️ 成员终端创建失败: " + "; ".join(batch_failures)
        return "\n".join([
            f"🚀 **{team_name}** 终端已启动！（直接控制 + 共享上下文模式）",
            f"   session: {session}",
            f"   👑 Leader: **你（当前会话）**",
            f"   👥 成员 ({len(created)}): {agent_summary}",
            "\n".join(mcp_setup_lines),
            task_note,
            launch_failure_note,
            ("\n⚠️ 初始上下文发送失败: " + "; ".join(context_failures)) if context_failures else "",
        ])

    # ================================================================
    # tmux 模式: leader 窗口 + 成员窗口（共享上下文）
    # ================================================================
    leader_agent = _member_agent(team, members[leader])
    leader_atype = _agent_type(leader_agent)

    mcp_setup_lines.insert(0, f"🔧 Leader agent: {leader_agent} [{leader_atype}]")

    leader_prompt = _leader_system_prompt(team_name, task)
    leader_mode = _member_mode(members.get(leader, {}))
    leader_model = resolve_agent_model(team_name, leader)
    leader_effort = resolve_member_effort(team_name, leader, leader_atype)
    if _is_codex(leader_agent):
        proxy_prefix = get_proxy_env_prefix(team_name, leader)
        agent_user_prefix = get_agent_user_env_prefix(team_name, leader, leader_atype)
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            *agent_user_prefix,
            *proxy_prefix,
            *_codex_command(leader_agent, team_dir, leader_prompt, member_mode=leader_mode, model=leader_model, effort=leader_effort),
        ])
    else:
        _write_claude_permissions(team_name)
        proxy_prefix = get_proxy_env_prefix(team_name, leader)
        # 私有 settings 目录权限收紧失败时 fail closed，返回可见错误
        try:
            leader_au_prefix, leader_settings_path = claude_agent_user_launch(team_name, leader)
        except RuntimeError as e:
            return f"❌ 创建 leader 终端失败: {e}"
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", team_dir,
            *merge_env_prefixes(leader_au_prefix, proxy_prefix),
            *_claude_agent_args(
                leader_agent,
                leader_mode,
                allowed_tools=CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS,
                model=leader_model,
                settings_path=leader_settings_path,
                effort=leader_effort,
            ),
        ])

    if rc != 0:
        return f"❌ 创建 leader 终端失败: {err}"
    created = [(leader, leader_agent, f"👑[{leader_atype}][MCP]")]
    tmux_mode_batch_failures: list[str] = []

    # 成员窗口: 从共享工作目录启动
    for name, info in members.items():
        if name == leader:
            continue
        member_agent = _member_agent(team, info)
        rc, _, err = _tmux_spawn_member(session, name, member_agent, team_dir)
        if rc == 0:
            created.append((name, member_agent, f"[{_agent_type(member_agent)}][MCP]"))
        else:
            tmux_mode_batch_failures.append(f"{name}: {err}")
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
    launch_failure_note = ""
    if tmux_mode_batch_failures:
        launch_failure_note = "\n⚠️ 成员终端创建失败: " + "; ".join(tmux_mode_batch_failures)

    return "\n".join([
        f"🚀 **{team_name}** 终端已启动！（共享上下文模式）",
        f"   session: {session}",
        f"   窗口: {agent_summary}",
        launch_failure_note,
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
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if team and _leader_terminal_restart_blocked(team_name, team):
        return (
            f"❌ 团队 '{team_name}' 任务进行中，禁止关闭 leader 终端。\n"
            "   普通成员终端仍可单独重启。"
        )

    _stop_team_monitor(team_name)
    session = _find_any_session(team_name)
    if session:
        _tmux(["kill-session", "-t", session])

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
        agent = _member_agent(team, members[name])
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

    proxy_config = team.get("proxy", {})
    proxy_default = "✅" if proxy_config.get("enabled") else "❌"

    lines = [
        f"📋 **{team_name}** 团队面板  [{terminals}]",
        f"   👑 Leader: {leader_str}",
        f"   🔧 默认成员 agent: {_default_member_agent(team)}",
        f"   🌐 默认代理: {proxy_default}",
        f"   👥 成员 ({len(members)} 人):",
    ]
    for name, info in members.items():
        role = info.get("role", "")
        agent = _member_agent(team, info)
        atype = _agent_type(agent)
        if name == leader and ltype == "tmux":
            identity = " 👑LEADER ← 你自己"
        elif _is_direct_leader_member(team, name):
            identity = " 👑DIRECT-LEADER ← 你自己"
        else:
            identity = ""

        proxy_tag = ""
        proxy_mode = member_proxy_mode(info)
        if proxy_mode != "inherit":
            proxy_tag = " 🔒proxy" if proxy_mode == "enabled" else " 🚫proxy"

        role_str = f" [{role}]" if role else ""
        lines.append(f"     • {name}{identity}{role_str}  {agent}[{atype}]{proxy_tag}")
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
    full_msg, compact_context = _build_member_task_payload(subtask, context)
    mode_prefix = _mode_task_prefix(members[member_name])
    if mode_prefix:
        full_msg = mode_prefix + full_msg
    members[member_name]["last_task"] = subtask
    members[member_name]["last_context"] = compact_context
    members[member_name]["last_task_completed"] = False
    members[member_name].pop("compact_sent", None)  # 新任务重置，允许下一次 /compact
    _touch_leader_activity(team)
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

    member_agent = _member_agent(team, members[member_name])
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
def leader_select_task_members(
    team_name: str,
    task: str,
    required_roles: str = "",
    create_missing: bool = True,
) -> str:
    """
    [Leader] 分配任务前根据任务内容和成员角色选择参与成员。

    该工具会先推断需要的角色，只选择匹配角色的非 leader 成员；当缺少角色且
    create_missing=True 时，自动按团队 default_agent 创建对应角色的新成员。

    Args:
        team_name: 团队名称
        task: 待分配任务描述
        required_roles: 可选，逗号分隔的显式角色列表；为空时根据 task 推断
        create_missing: 缺少所需角色时是否自动创建成员
    """
    selection = _select_task_members(
        team_name,
        task,
        required_roles=required_roles,
        create_missing=create_missing,
    )
    if selection.get("error"):
        return selection["error"]
    roles = selection.get("roles", [])
    selected = selection.get("selected", [])
    created = selection.get("created", [])
    lines = [
        "🧠 任务参与者分析:",
        f"   任务: {_compact_text(task, 240)}",
        f"   需要角色: {', '.join(roles) if roles else '未能可靠推断'}",
        f"   参与成员: {', '.join(selected) if selected else '无'}",
    ]
    if created:
        lines.append(f"   自动创建: {', '.join(created)}")
    spawn_failures = selection.get("spawn_failures") or []
    if spawn_failures:
        lines.append(f"   ⚠️ 终端创建失败: {'; '.join(spawn_failures)}")
    if not selected:
        lines.append("⚠️ 未选择成员。请传 required_roles，或改用 leader_broadcast 做显式全员广播。")
    return "\n".join(lines)


@mcp.tool
def leader_broadcast_to_relevant(
    team_name: str,
    message: str,
    task: str = "",
    required_roles: str = "",
    create_missing: bool = True,
) -> str:
    """
    [Leader] 仅向当前任务相关成员广播，避免无关成员消耗上下文。

    Args:
        team_name: 团队名称
        message: 广播内容
        task: 用于推断角色的任务描述；为空时使用 message
        required_roles: 可选，逗号分隔的显式角色列表
        create_missing: 缺少所需角色时是否自动创建成员
    """
    selection = _select_task_members(
        team_name,
        task or message,
        required_roles=required_roles,
        create_missing=create_missing,
    )
    if selection.get("error"):
        return selection["error"]
    team = selection["team"]
    if not team.get("terminals_active"):
        return "❌ 终端未启动。"
    targets = selection.get("selected", [])
    if not targets:
        return "⚠️ 未选择相关成员，未发送广播。请传 required_roles 或改用 leader_broadcast。"

    sent, failures = _send_message_to_members(team_name, team, targets, message)
    lines = [
        f"📣 定向广播已发送至 {len(sent)}/{len(targets)} 人。",
        f"   需要角色: {', '.join(selection.get('roles', [])) or '未指定'}",
        f"   目标成员: {', '.join(targets)}",
    ]
    if selection.get("created"):
        lines.append(f"   自动创建: {', '.join(selection['created'])}")
    if failures:
        lines.append("   失败: " + "; ".join(failures))
    return "\n".join(lines)


@mcp.tool
def leader_assign_task_to_relevant(
    team_name: str,
    task: str,
    subtask: str = "",
    required_roles: str = "",
    create_missing: bool = True,
) -> str:
    """
    [Leader] 根据角色选择相关成员，并把任务只分配给这些成员。

    Args:
        team_name: 团队名称
        task: 用于角色推断和记录的任务描述
        subtask: 实际发送给成员的任务；为空时发送 task
        required_roles: 可选，逗号分隔的显式角色列表
        create_missing: 缺少所需角色时是否自动创建成员
    """
    selection = _select_task_members(
        team_name,
        task,
        required_roles=required_roles,
        create_missing=create_missing,
    )
    if selection.get("error"):
        return selection["error"]
    team = selection["team"]
    if not team.get("terminals_active"):
        return "❌ 终端未启动，请先 launch_team_terminals。"
    targets = selection.get("selected", [])
    if not targets:
        return "⚠️ 未选择相关成员，未分配任务。请传 required_roles 或使用 leader_assign_subtask 显式指定成员。"

    payload_task = subtask.strip() or task
    reason = f"由 leader_assign_task_to_relevant 根据任务选择: {_compact_text(task, 240)}"
    payload, compact_context = _build_member_task_payload(payload_task, reason=reason)
    sent, failures = _send_message_to_members(team_name, team, targets, payload)
    data = _load()
    latest_team = data.get("teams", {}).get(team_name, {})
    for name in sent:
        member = latest_team.get("members", {}).get(name)
        if not member:
            continue
        member["last_task"] = payload_task
        member["last_context"] = compact_context or reason
        member["last_task_completed"] = False
        member.pop("compact_sent", None)  # 新任务重置，允许下一次 /compact
    _touch_leader_activity(latest_team)
    _save(data)

    lines = [
        f"✅ 相关任务已分配给 {len(sent)}/{len(targets)} 人。",
        f"   需要角色: {', '.join(selection.get('roles', [])) or '未指定'}",
        f"   目标成员: {', '.join(targets)}",
    ]
    if selection.get("created"):
        lines.append(f"   自动创建: {', '.join(selection['created'])}")
    if failures:
        lines.append("   失败: " + "; ".join(failures))
    return "\n".join(lines)


@mcp.tool
def leader_set_discussion_mode(team_name: str, enabled: bool = True, max_rounds: int = 3) -> str:
    """
    [Leader] 自由开启或关闭讨论模式。

    Args:
        team_name: 团队名称
        enabled: 是否开启讨论模式
        max_rounds: 最大讨论轮次，上限固定为 3
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    discussion = _discussion_entry(team)
    discussion["enabled"] = bool(enabled)
    discussion["max_rounds"] = max(1, min(int(max_rounds), 3))
    if not enabled:
        discussion["status"] = "idle"
        discussion["participants"] = []
    _save(data)
    return f"✅ 讨论模式已{'开启' if enabled else '关闭'}，最大轮次 {discussion['max_rounds']}。"


@mcp.tool
def leader_start_discussion(
    team_name: str,
    topic: str,
    required_roles: str = "",
    participants: str = "",
    max_rounds: int = 3,
) -> str:
    """
    [Leader] 开始讨论模式；讨论分析任务应使用此工具强制开启。

    coding 或执行指令中的 busy 成员会被跳过，不进入讨论。

    Args:
        team_name: 团队名称
        topic: 讨论主题
        required_roles: 可选，逗号分隔角色；为空时根据 topic 推断，仍为空则选择所有空闲成员
        participants: 可选，逗号分隔显式成员列表
        max_rounds: 最大讨论轮次，上限固定为 3
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    if not team.get("terminals_active"):
        return "❌ 终端未启动。"

    members = team.get("members", {})
    explicit_participants = _split_csv(participants)
    if explicit_participants:
        candidates = [
            name for name in explicit_participants
            if name in members and not _is_leader_member(team, name)
        ]
        created: list[str] = []
        roles = []
    else:
        selection = _select_task_members(
            team_name,
            topic,
            required_roles=required_roles,
            create_missing=True,
            fallback_all=True,
        )
        if selection.get("error"):
            return selection["error"]
        team = selection["team"]
        members = team.get("members", {})
        roles = selection.get("roles", [])
        created = selection.get("created", [])
        candidates = selection.get("selected", [])

    # _select_task_members may have persisted newly created members and window ids.
    # Reload before writing discussion state so those updates are not overwritten.
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    members = team.get("members", {})
    candidates = [
        name for name in candidates
        if name in members and not _is_leader_member(team, name)
    ]
    skipped_busy = [name for name in candidates if _member_is_busy_for_discussion(members.get(name, {}))]
    active = [name for name in candidates if name not in skipped_busy]
    if not active:
        return "⚠️ 没有可参与讨论的空闲成员；busy/coding 成员不会进入讨论模式。"

    import datetime
    session_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    discussion = _discussion_entry(team)
    discussion.update({
        "enabled": True,
        "forced_by_task": _is_discussion_task(topic),
        "status": "active",
        "session_id": session_id,
        "topic": topic,
        "round": 1,
        "max_rounds": max(1, min(int(max_rounds), 3)),
        "participants": active,
        "skipped_busy": skipped_busy,
        "conclusions": {"1": {}},
    })
    _touch_leader_activity(team)
    _save(data)

    message = "\n".join([
        "[讨论模式] 请进入团队讨论。",
        f"主题: {topic}",
        f"轮次: 1/{discussion['max_rounds']}",
        "请先独立 thinking，给出你的结论；然后调用 member_report_discussion_conclusion 上报。",
        "你可以调用 member_read_discussion 查看其他成员最后结论。",
    ])
    sent, failures = _send_message_to_members(team_name, team, active, message)

    lines = [
        f"🗣️ 讨论模式已开启: {session_id}",
        f"   主题: {_compact_text(topic, 240)}",
        f"   参与成员: {', '.join(active)}",
        f"   最大轮次: {discussion['max_rounds']}",
    ]
    if roles:
        lines.append(f"   需要角色: {', '.join(roles)}")
    if created:
        lines.append(f"   自动创建: {', '.join(created)}")
    if skipped_busy:
        lines.append(f"   跳过 busy: {', '.join(skipped_busy)}")
    if failures:
        lines.append("   发送失败: " + "; ".join(failures))
    else:
        lines.append(f"   已通知: {', '.join(sent)}")
    return "\n".join(lines)


@mcp.tool
def leader_discussion_next_round(
    team_name: str,
    leader_instruction: str = "",
    consensus_reached: bool = False,
) -> str:
    """
    [Leader] 汇总当前轮成员结论，并决定结束或进入下一轮讨论。

    Args:
        team_name: 团队名称
        leader_instruction: 给下一轮成员 thinking 的补充指令
        consensus_reached: 已获得一致结论时设为 True，立即结束讨论
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    discussion = _discussion_entry(team)
    if not discussion.get("enabled") or discussion.get("status") != "active":
        return "⚠️ 当前没有活跃讨论。"

    current_round = int(discussion.get("round", 1))
    max_rounds = max(1, min(int(discussion.get("max_rounds", 3)), 3))
    summary = _discussion_summary(team)
    if consensus_reached or current_round >= max_rounds:
        discussion["status"] = "ended"
        discussion["enabled"] = False
        discussion["ended_reason"] = "consensus" if consensus_reached else "max_rounds"
        _write_discussion_final_entry(team_name, team)
        _save(data)
        return "\n".join([
            "✅ 讨论模式已结束。",
            f"   原因: {discussion['ended_reason']}",
            summary,
        ])

    members = team.get("members", {})
    active = [
        name for name in discussion.get("participants", [])
        if name in members and not _member_is_busy_for_discussion(members[name])
    ]
    skipped_busy = [
        name for name in discussion.get("participants", [])
        if name in members and _member_is_busy_for_discussion(members[name])
    ]
    if not active:
        discussion["status"] = "ended"
        discussion["enabled"] = False
        discussion["ended_reason"] = "no_idle_participants"
        _write_discussion_final_entry(team_name, team)
        _save(data)
        return "⚠️ 所有讨论成员都处于 busy/coding 状态，讨论模式已结束。"

    next_round = current_round + 1
    discussion["round"] = next_round
    discussion["participants"] = active
    discussion["skipped_busy"] = skipped_busy
    discussion.setdefault("conclusions", {})[str(next_round)] = {}
    _save(data)

    message = "\n".join([
        "[讨论模式] 进入下一轮 thinking。",
        summary,
        "",
        f"Leader 指令: {leader_instruction or '请结合其他成员结论，收敛为更一致的方案。'}",
        f"轮次: {next_round}/{max_rounds}",
        "完成后调用 member_report_discussion_conclusion 上报本轮结论。",
    ])
    sent, failures = _send_message_to_members(team_name, team, active, message)
    lines = [
        f"🔁 已进入讨论第 {next_round}/{max_rounds} 轮。",
        f"   参与成员: {', '.join(active)}",
    ]
    if skipped_busy:
        lines.append(f"   跳过 busy: {', '.join(skipped_busy)}")
    if failures:
        lines.append("   发送失败: " + "; ".join(failures))
    else:
        lines.append(f"   已通知: {', '.join(sent)}")
    return "\n".join(lines)


@mcp.tool
def leader_end_discussion(team_name: str, reason: str = "leader_closed") -> str:
    """
    [Leader] 手动结束讨论模式。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    discussion = _discussion_entry(team)
    discussion["enabled"] = False
    discussion["status"] = "ended"
    discussion["ended_reason"] = reason
    _write_discussion_final_entry(team_name, team)
    _save(data)
    return "✅ 讨论模式已手动结束。\n" + _discussion_summary(team)


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
def leader_read_member_terminal(
    team_name: str,
    member_name: str,
    lines: int = 80,
    verbose: bool = False,
) -> str:
    """
    [Leader] 读取成员终端最近输出，便于判断其是否卡在授权提示。

    ⚡ Token 提示：默认 smart summary 只返回关键几行，按需选择 verbose=True 拿全文。

    Args:
        team_name: 团队名称
        member_name: 成员名称
        lines: verbose=True 时读取的最近行数，范围 10-500（默认 80）
        verbose: True 返回完整终端输出（深度排查用）；默认按状态智能截断：
            approval → 最后 8 行（含授权 prompt）
            busy → 最后 3 行（当前动作）
            idle → 仅状态摘要（不返回原始输出，最省 token）
            unknown → 最后 5 行
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

    # smart summary 只需少量行即可分类；verbose 才拉全量
    capture_lines = lines if verbose else min(lines, 40)
    rc, out, err = _capture_window(session, member_target, capture_lines)
    if rc != 0:
        return f"❌ 读取成员终端失败: {err}"

    text = out or ""
    if verbose or not text.strip():
        return f"📟 **{member_name}** 最近终端输出:\n\n{text or '(无输出)'}"

    state = _classify_terminal_output(text)
    lines_list = text.splitlines()
    if state == "approval":
        tail = lines_list[-8:]
        header = f"⚠️ **{member_name}** 卡在授权提示（最后 8 行）:\n"
    elif state == "busy":
        tail = lines_list[-3:]
        header = f"🔄 **{member_name}** 忙（最后 3 行）:\n"
    elif state == "idle":
        # idle 时终端只有提示符，无有用信息——直接给结论，零原始输出
        return (
            f"✅ **{member_name}** 空闲（idle）。如需了解其已完成的工作，"
            f"优先读 member_contexts/ 下的压缩上下文或 member_read_shared。"
        )
    else:
        tail = lines_list[-5:]
        header = f"❓ **{member_name}** 状态未知（最后 5 行）:\n"
    return header + "\n".join(tail)


@mcp.tool
def leader_check_member_status(team_name: str, member_name: str = "") -> str:
    """
    [Leader] 查看成员任务状态（纯数据层，零终端读取，token 开销最小）。

    后台监控线程每 30 秒更新 last_observed_state / last_task_completed，
    通常无需读取终端即可判断成员完成情况。常规轮询优先使用本工具；
    需要终端细节（如卡在授权提示）时才用 leader_read_member_terminal。

    Args:
        team_name: 团队名称
        member_name: 成员名称；为空时返回全部非 leader 成员
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    members = team.get("members", {})
    leader = team.get("leader", "")

    names = [member_name] if member_name else [n for n in members if n != leader]
    if not names:
        return "(无非 leader 成员)"

    out_lines = []
    for name in names:
        member = members.get(name)
        if not member:
            out_lines.append(f"❌ 成员 '{name}' 不存在。可用 leader_list_team 查看。")
            continue
        state = member.get("last_observed_state") or "unknown"
        done = bool(member.get("last_task_completed", True))
        has_task = bool((member.get("last_task") or "").strip())
        ts = (member.get("last_status_check_ts") or "")[:19]
        task_brief = _compact_text(member.get("last_task") or "", 60)

        if not has_task:
            status_text = "⏸ 无任务"
        elif done:
            status_text = "✅ 完成"
        else:
            status_text = "⏳ 进行中"

        lines = [
            f"• **{name}** [{state}] {status_text}",
            f"   状态检查: {ts or '—'}",
        ]
        if has_task:
            lines.append(f"   任务: {task_brief or '(空)'}")
            if not done:
                lines.append("   → 未完成；如需细节用 leader_read_member_terminal")
        out_lines.append("\n".join(lines))
    return "\n".join(out_lines)


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

    ⚡ Token 提示：常规轮询请用 leader_check_member_status（纯数据层，零终端读取）；
    本工具会触发终端捕获并可能自动授权，仅在确实需要扫描/操作时调用。

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

    # 中断闭环：leader 巡检成员时顺带自检 leader 终端，仅在 leader 曾经有
    # 持久化窗口身份时重建。这样不会把尚未启动过的“占位 leader”误拉起；
    # CLI 崩溃到 shell 仍由后台监控循环/成员回报路径处理。
    leader_name = team.get("leader", "")
    leader_info = team.get("members", {}).get(leader_name, {})
    leader_was_spawned = any(
        leader_info.get(key)
        for key in ("tmux_window_id", "tmux_window_name", "tmux_session", "tmux_session_id")
    )
    revived, revive_msg = (False, "")
    if leader_was_spawned:
        revived, revive_msg = _maybe_revive_leader(
            team_name, reason="leader_patrol", only_missing_window=True
        )

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
    if revived:
        lines_out.append(f"  • [leader] 🔴 终端中断 → 已自动恢复: {revive_msg}")
    summary = " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "无成员"
    lines_out.append(f"\n📊 {summary}")
    return "\n".join(lines_out)


@mcp.tool
def leader_get_recovery_context(team_name: str) -> str:
    """
    [Leader] 获取 leader 重新进入后的恢复上下文。

    如果存在未完成总任务或成员未完成任务，返回继续工作所需的任务快照；
    如果没有未完成工作，返回待机说明和最近共享结果。

    Args:
        team_name: 团队名称
    """
    return _build_leader_recovery_context(team_name)


@mcp.tool
def leader_activate(team_name: str) -> str:
    """
    [Leader] 激活 leader 并查看中断期间待处理的成员回报。

    重新进入(或从休息中被唤醒)后调用:清除 resting 状态并返回当前工作摘要,
    包括 leader 自身未完成任务、未完成成员任务,以及成员在 leader 离开/休息期间
    上报的回报(leader_pending_reports)。调用会消费(清空)这些回报,避免重复提醒。

    direct leader 没有可注入终端,通过本工具主动激活即可收取离线期间的回报。

    Args:
        team_name: 团队名称
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    now = datetime.datetime.now().isoformat()

    def _activate_and_drain(latest_team: dict) -> dict:
        """原子：激活 leader + 取走并清空待处理回报。

        必须在 _update_team_data 的锁内读+清，否则与并发 member_report_result 的
        append 形成 TOCTOU，可能把新上报覆盖丢失（load 快照里没有、save 时清空）。
        """
        was_resting = latest_team.get("leader_state") == "resting"
        latest_team["leader_state"] = "active"
        latest_team["leader_idle_streak"] = 0
        latest_team["leader_activated_ts"] = now
        latest_team["leader_wakeup_reason"] = ""
        reports = pending_leader_reports(latest_team)
        latest_team["leader_pending_reports"] = []
        return {"was_resting": was_resting, "reports": reports}

    result = _update_team_data(team_name, _activate_and_drain) or {}
    was_resting = result.get("was_resting", False)
    reports = result.get("reports") or []
    # 取回清空后的最新团队状态，供下方"未完成工作"摘要使用
    team = _load().get("teams", {}).get(team_name, {})

    lines = [
        f"✅ leader 已激活{'（从休息中唤醒）' if was_resting else ''}。",
        f"   激活时间: {now}",
    ]

    if reports:
        lines.append(f"\n📥 成员回报 {len(reports)} 条(已确认):")
        for i, report in enumerate(reports, 1):
            member = report.get("member") or "unknown"
            result = _compact_text(report.get("result") or "", 200)
            ts = (report.get("timestamp") or "")[:19]
            line = f"  {i}. [{ts}] {member}: {result}"
            if report.get("artifact_path"):
                line += f" | artifact: {report['artifact_path']}"
            lines.append(line)
    else:
        lines.append("\n📭 没有待处理的成员回报。")

    if leader_has_unfinished_work(team):
        lines.append("\n⏳ 未完成工作:")
        leader_task = (team.get("leader_last_task") or "").strip()
        if leader_task and not team.get("leader_last_task_completed", True):
            lines.append(f"  - leader 总任务: {_compact_text(leader_task, 300)}")
        for name, member in active_member_tasks(team):
            task = _compact_text(member.get("last_task") or "", 200)
            lines.append(f"  - 成员 {name}({member.get('role') or 'member'}): {task}")
        lines.append("\n  完整恢复摘要用 leader_get_recovery_context 查看。")
    else:
        lines.append("\n💤 当前没有未完成工作，等待新任务。")

    return "\n".join(lines)


@mcp.tool
def leader_mark_task_complete(
    team_name: str,
    summary: str = "",
    artifact_path: str = "",
) -> str:
    """
    [Leader] 标记当前团队/leader 工作已完成，后续重新进入时进入待机状态。

    Args:
        team_name: 团队名称
        summary: 可选完成摘要，会写入共享 results.jsonl
        artifact_path: 可选产物路径
    """
    import datetime

    current = _team_info(team_name)
    if not current:
        return f"❌ 团队 '{team_name}' 不存在。"

    now = datetime.datetime.now().isoformat()

    def update_complete(latest_team: dict) -> dict:
        latest_team["leader_last_task_completed"] = True
        latest_team["leader_task_completed_ts"] = now
        latest_team["leader_last_activity_ts"] = now
        still_unfinished = leader_has_unfinished_work(latest_team)
        latest_team["leader_work_state"] = "active" if still_unfinished else "idle"
        return {
            "leader": latest_team.get("leader") or "leader",
            "still_unfinished": still_unfinished,
            "work_state": latest_team["leader_work_state"],
        }

    update_result = _update_team_data(team_name, update_complete)
    if update_result is None:
        return f"❌ 团队 '{team_name}' 不存在。"

    # ---- 1. 生成压缩上下文（先生成路径，供 results.jsonl 记录） ----
    pre_path = ""
    try:
        pre_path = _write_leader_compressed_context(team_name, summary, artifact_path)
    except Exception as e:
        pre_path = f"生成失败: {e}"

    # ---- 2. 写入 results.jsonl（记录必须在 /compact 之前） ----
    entry = {
        "timestamp": now,
        "member": update_result.get("leader") or "leader",
        "event": "leader_task_completed",
        "result": summary.strip() or "leader marked team task complete",
        "artifact_path": artifact_path.strip(),
        "compressed_context_path": pre_path,
    }
    write_error = ""
    try:
        results_file = os.path.join(_share_dir(team_name), "results.jsonl")
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        write_error = f"\n⚠️ 写入 results.jsonl 失败: {e}"

    # ---- 3. 统一收尾：发送 /compact ----
    fin = _finalize_agent_completion(
        team_name,
        update_result.get("leader") or "leader",
        summary.strip() or "leader marked team task complete",
        artifact_path=artifact_path,
        is_leader=True,
        compact_path=pre_path,
    )
    compressed_context_path = fin["compact_path"]

    # ---- 构建 /compact 状态消息 ----
    compact_msg = ""
    if fin["compact_sent"]:
        compact_msg = "\n📦 已向 leader 终端注入 /compact"
    elif fin["compact_error"] and fin["compact_error"] != "already sent (idempotent)":
        compact_msg = f"\n⚠️ /compact 注入失败: {fin['compact_error']}"

    base_msg = (
        f"✅ '{team_name}' leader 工作已标记完成。"
        f"\n🧾 压缩上下文: {compressed_context_path}{compact_msg}{write_error}"
    )
    if update_result.get("still_unfinished"):
        return (
            base_msg + "\n"
            "⚠️ 仍检测到未完成成员任务；下次 leader 重新进入时仍会进入恢复续跑模式。"
        )
    return (
        base_msg + "\n"
        "💤 下次 leader 重新进入时将进入待机状态，除非又分配了新的未完成成员任务。"
    )


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
def leader_configure_recovery(
    team_name: str,
    enabled: bool = True,
    min_interval_seconds: int = 60,
    max_revivals: int = 5,
    max_member_recoveries: int = 3,
) -> str:
    """[Leader] 配置工作流中断自动恢复的限流与上限。

    自动恢复默认开启，但仍受保护阈值约束：仅当持久化任务未完成时复活，
    活跃 CLI 不会被重启；连续崩溃达到上限后保留恢复上下文并等待人工处理。

    Args:
        team_name: 团队名称
        enabled: 是否允许自动复活 leader 终端
        min_interval_seconds: 同一团队两次 leader 复活的最小间隔
        max_revivals: 单轮总任务最多复活次数
        max_member_recoveries: 单个成员终端最多自动恢复次数
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    cfg = {
        "enabled": bool(enabled),
        "min_interval_seconds": max(0, min(int(min_interval_seconds), 86400)),
        "max_revivals": max(1, min(int(max_revivals), 100)),
    }
    team["leader_revival_config"] = cfg
    team["monitor_max_recoveries"] = max(0, min(int(max_member_recoveries), 100))
    team["monitor_enabled"] = True
    team.setdefault("monitor_interval_seconds", 30)
    _save(data)
    if team.get("terminals_active"):
        _start_team_monitor(team_name)

    state = "启用" if cfg["enabled"] else "关闭"
    return (
        f"✅ {team_name} 中断自动恢复已{state}。\n"
        f"   leader 间隔={cfg['min_interval_seconds']}s 上限={cfg['max_revivals']}次，"
        f"成员上限={team['monitor_max_recoveries']}次"
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
      - auto: Claude 启动加 --permission-mode acceptEdits；Codex 启动加 --ask-for-approval never；
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
      - Claude 成员: 设置为 auto 模式，后续启动使用 --permission-mode acceptEdits；
        leader 监控遇到 approval prompt 时自动选择 session。
      - Codex 成员: 设置为 auto 模式，后续启动使用 --ask-for-approval never，
        相当于一次性授予当前成员无审批执行权限。
      - 其他 agent: 记录为 auto，并依赖 leader 监控自动处理 approval prompt。

    Args:
        team_name: 团队名称
        member_name: 成员名；为空或 "*" 表示所有非 leader 成员
        relaunch: 是否立即重启目标成员终端，使 CLI 启动参数立即生效。
                  默认 False，避免中断正在执行的成员任务。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    if not members:
        return f"❌ 团队 '{team_name}' 没有成员。"

    if not member_name or member_name == "*":
        targets = [
            name for name in members
            if not _is_leader_member(team, name)
        ]
    elif member_name in members:
        if _is_leader_member(team, member_name):
            return f"❌ '{member_name}' 是 leader，不应授予 member 自动权限。"
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
        agent = _member_agent(team, info)
        atype = _agent_type(agent)
        info["work_mode"] = "auto"
        info["auto_authorize"] = True
        info["auto_authorize_choice"] = "session"
        info["autonomy_granted"] = True
        info["autonomy_granted_ts"] = ts
        if atype == "claude":
            info["autonomy_policy"] = "claude_permission_mode_accept_edits"
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
                    agent = _member_agent(team, members[name])
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
        policy_lines.append(f"  • Claude auto: {', '.join(claude_targets)} → --permission-mode acceptEdits")
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
def leader_configure_proxy(
    team_name: str,
    *,
    enabled: bool = False,
    host: str = "127.0.0.1",
    port: int = 7890,
) -> str:
    """
    [Leader] 配置团队默认代理。新启动的成员终端将自动设置 http_proxy/https_proxy 环境变量。

    可通过 leader_configure_member_proxy 对单个成员单独覆盖是否启用代理。
    已运行的成员终端需要重新启动才能生效。

    Args:
        team_name: 团队名称
        enabled: 是否启用代理（团队默认）
        host: 代理主机（默认 127.0.0.1）
        port: 代理端口（默认 7890）
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    team["proxy"] = {
        "enabled": enabled,
        "host": host,
        "port": port,
    }
    _save(data)

    if enabled:
        proxy_url = f"http://{host}:{port}"
        return (
            f"✅ 团队 '{team_name}' 默认代理已启用。\n"
            f"   http_proxy={proxy_url}\n"
            f"   https_proxy={proxy_url}\n"
            f"💡 新启动的成员终端将自动设置这些环境变量。\n"
            f"💡 使用 leader_configure_member_proxy 对单个成员覆盖。"
        )
    else:
        return f"✅ 团队 '{team_name}' 默认代理已禁用。"


@mcp.tool
def leader_get_proxy_config(team_name: str, member_name: str = "") -> str:
    """
    [Leader] 查看团队的代理配置。

    指定 member_name 时额外展示该成员的代理覆盖状态。
    不指定 member_name 时列出所有有覆盖的成员。

    Args:
        team_name: 团队名称
        member_name: 可选，查看特定成员的代理覆盖状态
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    proxy_config = team.get("proxy", {})

    # 团队成员默认状态
    team_enabled = bool(proxy_config.get("enabled"))
    host = proxy_config.get("host", "127.0.0.1")
    port = proxy_config.get("port", 7890)
    proxy_url = f"http://{host}:{port}"

    lines = [
        f"🔧 团队 '{team_name}' 代理配置:",
        f"   默认: {'✅ 启用' if team_enabled else '❌ 禁用'}",
    ]
    if team_enabled:
        lines.extend([
            f"   http_proxy={proxy_url}",
            f"   https_proxy={proxy_url}",
        ])

    # 查看特定成员
    if member_name:
        member = team.get("members", {}).get(member_name)
        if not member:
            return f"❌ 成员 '{member_name}' 不存在。"
        mode = member_proxy_mode(member)
        effective = member_proxy_enabled(team, member_name, member)
        if mode == "enabled":
            lines.append(f"\n   👤 {member_name}: 强制启用（覆盖团队默认，当前生效: ✅）")
        elif mode == "disabled":
            lines.append(f"\n   👤 {member_name}: 强制禁用（覆盖团队默认，当前生效: ❌）")
        else:
            lines.append(f"\n   👤 {member_name}: 继承团队默认（当前生效: {'✅' if effective else '❌'}）")
        return "\n".join(lines)

    # 列出有覆盖的成员
    overrides = []
    for name, info in team.get("members", {}).items():
        mode = member_proxy_mode(info)
        if mode != "inherit":
            status = "🔒 强制启用" if mode == "enabled" else "🚫 强制禁用"
            overrides.append(f"   👤 {name}: {status}")
    if overrides:
        lines.append(f"\n   成员覆盖 ({len(overrides)}):")
        lines.extend(overrides)
    else:
        lines.append(f"\n   所有成员继承团队默认（无覆盖）")
    lines.append(f"\n💡 使用 leader_configure_member_proxy 对单个成员覆盖。")
    return "\n".join(lines)


@mcp.tool
def leader_configure_member_proxy(
    team_name: str,
    member_name: str = "",
    *,
    proxy_enabled: bool = False,
) -> str:
    """
    [Leader] 为成员单独设置代理开关，覆盖团队默认。

    优先级: 成员 proxy_mode/proxy_enabled > 团队 proxy.enabled

    - proxy_enabled=True:  强制启用代理（即使团队默认关闭）
    - proxy_enabled=False: 强制禁用代理（即使团队默认开启）
    - 清除覆盖: 使用 leader_configure_member_proxy_clear

    配置后新启动的成员终端立即生效；已运行的终端需重启。

    Args:
        team_name: 团队名称
        member_name: 成员名；为空或 "*" 表示所有非 leader 成员
        proxy_enabled: 是否对此成员启用代理
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")

    if not member_name or member_name == "*":
        targets = [
            name for name in members
            if not (ltype == "tmux" and name == leader)
        ]
    elif member_name in members:
        targets = [member_name]
    else:
        return f"❌ 成员 '{member_name}' 不存在。"

    if not targets:
        return "⚠️ 没有可设置的非 leader 成员。"

    mode = "enabled" if proxy_enabled else "disabled"
    for name in targets:
        members[name]["proxy_mode"] = mode
        members[name]["proxy_enabled"] = bool(proxy_enabled)
    _save(data)

    status = "🔒 强制启用" if proxy_enabled else "🚫 强制禁用"
    target_text = ", ".join(targets)
    team_enabled = bool(team.get("proxy", {}).get("enabled"))
    note = ""
    if team_enabled == proxy_enabled:
        note = "（与团队默认一致）"
    return (
        f"✅ 已为 {target_text} 设置代理: {status}{note}\n"
        f"💡 新启动的终端生效；已运行终端需重启。"
    )


@mcp.tool
def leader_clear_member_proxy_override(
    team_name: str,
    member_name: str = "",
) -> str:
    """
    [Leader] 清除成员的代理覆盖，恢复为继承团队默认。

    Args:
        team_name: 团队名称
        member_name: 成员名；为空或 "*" 表示所有非 leader 成员
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    members = team.get("members", {})
    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")

    if not member_name or member_name == "*":
        targets = [
            name for name in members
            if not (ltype == "tmux" and name == leader)
        ]
    elif member_name in members:
        targets = [member_name]
    else:
        return f"❌ 成员 '{member_name}' 不存在。"

    if not targets:
        return "⚠️ 没有可清除的非 leader 成员。"

    cleared = []
    for name in targets:
        if "proxy_enabled" in members[name] or member_proxy_mode(members[name]) != "inherit":
            members[name].pop("proxy_enabled", None)
            members[name].pop("proxy_mode", None)
            cleared.append(name)
    _save(data)

    if not cleared:
        return "⚠️ 所选成员无代理覆盖，无需清除。"

    team_enabled = bool(team.get("proxy", {}).get("enabled"))
    status = "✅ 启用" if team_enabled else "❌ 禁用"
    return (
        f"✅ 已清除 {', '.join(cleared)} 的代理覆盖 → 继承团队默认（{status}）"
    )


@mcp.tool
def leader_add_member(
    team_name: str,
    member_name: str,
    role: str = "",
    agent: str = "",
    use_explicit_agent: bool = False,
) -> str:
    """
    [Leader] 动态添加成员 + 创建终端窗口。

    默认强制继承团队默认 agent；只有 use_explicit_agent=True 时才使用 agent 覆盖。

    Args:
        team_name: 团队名称
        member_name: 新成员名称
        role: 角色
        agent: 可选启动命令（claude/codex/自定义）
        use_explicit_agent: True 时才使用 agent 覆盖团队默认 agent
    """
    # 检查存在 → 写入 → 保存 在数据锁内原子执行，避免并发创建同名成员后各自 spawn。
    with TEAM_DATA_LOCK:
        data = _load()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return f"❌ 团队 '{team_name}' 不存在。"

        if member_name in team.get("members", {}):
            return f"❌ 成员 '{member_name}' 已存在。"

        if not team.get("terminals_active"):
            return f"❌ 终端未启动。"

        actual_agent, used_explicit_agent = _resolve_new_member_agent(
            team,
            agent,
            use_explicit_agent=use_explicit_agent,
        )
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

    source = "显式指定" if used_explicit_agent else "团队默认"
    return f"✅ 新成员 '{member_name}' 已加入（role={role}, agent={actual_agent}[{atype}]，来源={source}），终端已启动。"


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

    if (ltype == "tmux" and member_name == leader) or _is_direct_leader_member(team, member_name):
        return f"❌ '{member_name}' 是 leader，不能移除。请先用 claim_leader 接管。"

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
    if _is_leader_member(team, member_name):
        return f"⚠️ '{member_name}' 是当前 leader，不应作为 member 终端启动。"

    session = _find_any_session(team_name)
    if not session:
        return f"❌ 未找到运行中的终端 session。"

    agent = _member_agent(team, members[member_name])
    atype = _agent_type(agent)
    team_dir = _team_dir(team_name)

    # 确保 MCP 配置就绪
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    # 幂等：成员终端已在运行时直接短路，避免重试产生重复窗口/重复注入上下文。
    state, state_detail = _member_window_state(team_name, member_name, session)
    if state == "live":
        return f"✅ 成员 '{member_name}' 终端已在运行（未重复创建）。"
    if state == "unknown":
        return f"❌ 无法确认成员 '{member_name}' 终端状态（{state_detail}），已安全停止，请稍后重试。"

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


# ---------------------------------------------------------------------------
# Shared-context file-path security validator
# ---------------------------------------------------------------------------

def _safe_share_path(team_name: str, file_path: str, *,
                     allow_missing: bool = False) -> tuple[str, str]:
    """Validate and resolve a file path within the team's share context directory.

    Security checks (in order):
    1. Reject empty paths
    2. Reject absolute paths
    3. Reject .. path-traversal segments
    4. Resolve realpath; reject if it escapes the share dir
    5. Walk each segment to reject symlinks that point outside share dir
    6. Reject non-regular files (directories, devices, etc.)
    7. When allow_missing=False: reject non-existent files

    Returns:
        (resolved_absolute_path, "") on success
        ("", "error message") on failure  -- first element is always "" on error
    """
    share_dir = _share_dir(team_name)
    share_real = os.path.realpath(share_dir)

    # 1. Reject empty
    if not file_path or not file_path.strip():
        return "", "❌ 文件路径不能为空"

    # 2. Reject absolute
    if os.path.isabs(file_path):
        return "", f"❌ 不允许绝对路径: {file_path}"

    # 3. Reject .. segments (string-level, before any filesystem access)
    normalized = file_path.replace("\\", "/")
    segments = [s for s in normalized.split("/") if s and s != "."]
    if ".." in segments:
        return "", f"❌ 禁止 .. 路径穿越: {file_path}"

    # 4. Resolve realpath; reject escape
    candidate = os.path.join(share_dir, file_path)
    try:
        real = os.path.realpath(candidate)
    except (ValueError, OSError) as e:
        return "", f"❌ 路径解析失败: {file_path} ({e})"

    if not (real == share_real or real.startswith(share_real + os.sep)):
        return "", f"❌ 路径越界（逃逸共享目录）: {file_path}"

    # 5. Walk each segment: reject any symlink (not just escapes)
    cumulative = share_dir
    for seg in segments:
        cumulative = os.path.join(cumulative, seg)
        if os.path.islink(cumulative):
            # Check whether the symlink target also escapes
            try:
                seg_real = os.path.realpath(cumulative)
            except (ValueError, OSError):
                seg_real = cumulative
            if not (seg_real == share_real or seg_real.startswith(share_real + os.sep)):
                return "", f"❌ 符号链接 '{seg}' 指向共享目录外，拒绝访问"
            return "", f"❌ 不允许符号链接: {os.path.relpath(cumulative, share_dir)}"

    # 6. File type checks
    rel = os.path.relpath(real, share_dir)

    if not os.path.lexists(real):
        if allow_missing:
            return real, ""
        return "", f"❌ 文件不存在: {rel}"

    if not os.path.isfile(real):
        return "", f"❌ 不是普通文件: {rel}"

    return real, ""


# ---------------------------------------------------------------------------
# Concurrent-write guard: stat-before + stat-after + atomic os.replace
# ---------------------------------------------------------------------------


class _ConcurrentWriteGuard:
    """Atomic file writer with concurrent-modification detection.

    Usage:
        guard = _ConcurrentWriteGuard(target_path)
        with guard:
            with open(guard.tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
            err = guard.check_and_replace()
            if err:
                raise RuntimeError(err)  # tmp path is cleaned in __exit__
    """

    def __init__(self, file_path: str):
        self._file_path = file_path
        self._before = None
        self.tmp_path: str | None = None

    def __enter__(self):
        if os.path.lexists(self._file_path):
            st = os.stat(self._file_path)
            self._before = (st.st_ino, st.st_mtime, st.st_size)
        else:
            self._before = None
        self.tmp_path = f"{self._file_path}.tmp.{os.getpid()}"
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # Clean up temp file on error
        if exc_type is not None and self.tmp_path and os.path.exists(self.tmp_path):
            try:
                os.unlink(self.tmp_path)
            except OSError:
                pass
        return False

    def check_and_replace(self) -> str:
        """Compare stat snapshots and atomically replace.

        Returns "" on success, or an error message string.
        On failure, cleans up the temp file.
        """
        if not self.tmp_path:
            return "❌ 内部错误：临时文件路径未设置"

        exists_now = os.path.lexists(self._file_path)

        if exists_now:
            if self._before is None:
                self._cleanup_tmp()
                return (
                    "⚠️ 并发冲突：文件在写入过程中被其他成员创建，"
                    "拒绝覆盖以免数据丢失。请重新读取后再试。"
                )
            st = os.stat(self._file_path)
            after = (st.st_ino, st.st_mtime, st.st_size)
            if self._before != after:
                self._cleanup_tmp()
                return (
                    "⚠️ 并发冲突：文件在写入过程中被其他成员修改，"
                    "拒绝覆盖以免数据丢失。请重新读取最新内容后再试。"
                )
        elif self._before is not None:
            self._cleanup_tmp()
            return (
                "⚠️ 并发冲突：文件在写入过程中被其他成员删除，"
                "拒绝覆盖以免数据丢失。请重新读取后再试。"
            )

        try:
            os.replace(self.tmp_path, self._file_path)
        except OSError as e:
            self._cleanup_tmp()
            return f"❌ 写入失败 (os.replace): {e}"

        return ""

    def _cleanup_tmp(self):
        """Remove the temp file if it exists."""
        if self.tmp_path and os.path.lexists(self.tmp_path):
            try:
                os.unlink(self.tmp_path)
            except OSError:
                pass


def _compact_text(text: str, limit: int = 1200) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    half = max(1, (limit - 20) // 2)
    return f"{text[:half]} ... {text[-half:]}"


# ---------------------------------------------------------------------------
# Token budget enforcement: PROVABLE upper bound via UTF-8 byte count
# ---------------------------------------------------------------------------
# STRATEGY: Every BPE token encodes ≥1 byte of the final UTF-8 text.
# Therefore len(content.encode('utf-8')) ≤ max_bytes
#        ⇒  token_count(content) ≤ max_bytes.
# This is a mathematically provable upper bound across GPT, Claude, Llama,
# and all other BPE tokenizers.  We use max_bytes = 2000.
# ---------------------------------------------------------------------------

_MAX_COMPLETION_BYTES = 2000


def _token_bound_truncate(text: str, max_bytes: int = _MAX_COMPLETION_BYTES) -> str:
    """Truncate text so its UTF-8 encoding is ≤ max_bytes.

    TOKEN GUARANTEE: In every BPE tokenizer (GPT, Claude, Llama, …), each
    token encodes ≥1 byte of the output.  Therefore:
        len(result.encode('utf-8')) ≤ max_bytes
        ⇒  token_count(result) ≤ max_bytes.
    This is a PROVABLE upper bound, not a heuristic or approximation.

    Preserves both the HEAD (metadata + task sections) and the TAIL (Outcome
    Summary) by taking equal portions from each end, with a truncation marker
    in the middle.  This ensures the most important content at both ends
    survives truncation.
    """
    text = " ".join((text or "").split())
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    marker = f" ... [≤{max_bytes} UTF-8 bytes ⇒ ≤{max_bytes} tokens] ... "
    marker_bytes = len(marker.encode("utf-8"))
    available = max_bytes - marker_bytes
    half = available // 2

    # Binary search for head prefix that fits in half the budget
    lo, hi = 1, len(text)
    head = ""
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[:mid]
        if len(candidate.encode("utf-8")) <= half:
            head = candidate
            lo = mid + 1
        else:
            hi = mid - 1

    # Binary search for tail suffix that fits in remaining budget
    head_bytes = len(head.encode("utf-8"))
    tail_budget = available - head_bytes
    tail = ""
    lo, hi = 1, min(len(text) - len(head), len(text))
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = text[-mid:] if mid < len(text) else text
        if len(candidate.encode("utf-8")) <= tail_budget:
            tail = candidate
            lo = mid + 1
        else:
            hi = mid - 1

    return head + marker + tail


def _enforce_token_budget(content: str, max_bytes: int = _MAX_COMPLETION_BYTES) -> str:
    """Apply provable token-bound truncation.

    Uses UTF-8 byte count as the provable upper bound on token count.
    Returns content guaranteed to be ≤ max_bytes UTF-8 bytes and thus
    ≤ max_bytes tokens.
    """
    return _token_bound_truncate(content, max_bytes)


# ---------------------------------------------------------------------------
# /compact injection
# ---------------------------------------------------------------------------

def _inject_compact(team_name: str, member_name: str) -> tuple[bool, str]:
    """Send and submit /compact to an agent's tmux window if it is alive.

    Slash-command completion can consume the first Enter while leaving /compact
    in the input box.  A short delayed follow-up Enter confirms submission.

    For direct leaders: routes through _leader_window_target, which does a
    pure by-name scan against the live session — no dependency on the leader
    being in team["members"].  Falls back to "direct leader has no terminal
    window" only when no matching window is actually reachable.

    Returns:
        (sent, detail): sent is True when /compact was delivered to a live
        terminal; detail is a human-readable status or empty string on success.
    """
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    if not team:
        return False, "team not found"

    session = _find_any_session(team_name)
    if not session:
        return False, "no tmux session"

    if _is_direct_leader_member(team, member_name):
        target = _leader_window_target(team_name, member_name)
        if not target:
            return False, "direct leader has no terminal window"
    else:
        target = _member_window_target(team_name, member_name)
        if not target:
            return False, "terminal dead"

    rc, err = _send_keys(session, target, "/compact")
    if rc != 0:
        return False, f"send_keys failed: {err}"
    rc, err = _confirm_prompt_submission(session, target)
    if rc != 0:
        return False, f"confirm failed: {err}"

    return True, ""


# ---------------------------------------------------------------------------
# Compressed context writers (member + leader) — unified ≤2000 token guarantee
# ---------------------------------------------------------------------------

def _write_member_compressed_context(
    team_name: str,
    member_name: str,
    result: str,
    artifact_path: str,
    compressed_context: str = "",
) -> str:
    """Write a ≤2000-token compressed-context markdown file for a member.

    Returns the path relative to the team's share directory.
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {}) if member_name else {}
    context_dir = os.path.join(_share_dir(team_name), "member_contexts")
    os.makedirs(context_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_member = _safe_name(member_name or "unknown")
    context_file = os.path.join(context_dir, f"{ts}_{safe_member}.md")

    summary = compressed_context.strip() or _compact_text(result)
    last_task = member.get("last_task", "")
    last_context = member.get("last_context", "")

    # Outcome Summary first: head+tail truncation preserves section order,
    # so the most important info (outcome) must come before task details.
    lines = [
        f"# Compressed Context: {member_name or 'unknown'}",
        "",
        f"- team: {team_name}",
        f"- member: {member_name or 'unknown'}",
        f"- timestamp: {datetime.datetime.now().isoformat()}",
        f"- artifact_path: {artifact_path or '(none)'}",
        "",
        "## Outcome Summary",
        summary or "(empty)",
        "",
        "## Task",
        last_task or "(not recorded)",
        "",
        "## Input Context",
        last_context or "(not recorded)",
        "",
    ]
    content = "\n".join(lines)
    content = _enforce_token_budget(content)

    with open(context_file, "w", encoding="utf-8") as f:
        f.write(content)
    return os.path.relpath(context_file, _share_dir(team_name))


def _write_leader_compressed_context(
    team_name: str,
    summary: str,
    artifact_path: str,
) -> str:
    """Write a ≤2000-token compressed-context markdown file for the leader.

    Returns the path relative to the team's share directory.
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    leader_name = team.get("leader", "leader")

    context_dir = os.path.join(_share_dir(team_name), "member_contexts")
    os.makedirs(context_dir, exist_ok=True)

    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe_name = _safe_name(leader_name)
    context_file = os.path.join(context_dir, f"{ts}_{safe_name}_leader.md")

    leader_task = team.get("leader_last_task", "")
    leader_context = team.get("leader_last_context", "")
    result_summary = summary.strip() or "leader marked team task complete"

    # Completion Summary first: head+tail truncation preserves section order
    lines = [
        f"# Compressed Context: {leader_name} (leader)",
        "",
        f"- team: {team_name}",
        f"- leader: {leader_name}",
        f"- timestamp: {datetime.datetime.now().isoformat()}",
        f"- artifact_path: {artifact_path or '(none)'}",
        "",
        "## Completion Summary",
        result_summary,
        "",
        "## Leader Task",
        leader_task or "(not recorded)",
        "",
        "## Task Context",
        leader_context or "(not recorded)",
        "",
    ]
    content = "\n".join(lines)
    content = _enforce_token_budget(content)

    with open(context_file, "w", encoding="utf-8") as f:
        f.write(content)
    return os.path.relpath(context_file, _share_dir(team_name))


# ---------------------------------------------------------------------------
# Unified agent-completion finalization
# ---------------------------------------------------------------------------

_SENTINEL_NAMES = {"unknown", ""}


def _finalize_agent_completion(
    team_name: str,
    agent_name: str,
    result: str,
    compressed_context: str = "",
    artifact_path: str = "",
    is_leader: bool = False,
    compact_path: str | None = None,
) -> dict:
    """Unified per-agent completion finalization: write context + send /compact.

    Covers three completion paths:
      - member_report_result  (is_leader=False)
      - leader_mark_task_complete (is_leader=True)
      - monitor idle auto-complete (is_leader=False, result is synthetic)

    Idempotency: uses the compact_sent field (member or leader level).
    If compact_sent is set → skip /compact (already sent).
    If compact_sent is missing (previous send failed or first time) → send.

    Sentinel agent names ("unknown", "") skip /compact entirely.

    Context is ALWAYS written (not idempotent — each call produces a new
    timestamped file for audit trail).

    If compact_path is provided, context writing is skipped and the pre-generated
    path is used directly (allows caller to write results.jsonl before /compact).

    Returns:
        {compact_path, compact_sent, compact_error, truncated, agent_exited}
    """
    import datetime

    # ---- 1. Write compressed context (skip if path pre-provided) ----
    if compact_path is not None:
        actual_path = compact_path
    else:
        actual_path = ""
        try:
            if is_leader:
                actual_path = _write_leader_compressed_context(
                    team_name, result, artifact_path
                )
            else:
                actual_path = _write_member_compressed_context(
                    team_name, agent_name, result, artifact_path, compressed_context
                )
        except Exception as e:
            actual_path = f"生成失败: {e}"

    # ---- 2. Check idempotency guards ----
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    compact_already_sent = False
    if is_leader:
        compact_already_sent = bool(team.get("leader_compact_sent"))
    else:
        member = team.get("members", {}).get(agent_name, {})
        compact_already_sent = bool(member.get("compact_sent"))

    # ---- 3. Send /compact (skip sentinel names, already-sent, dead terminals) ----
    compact_sent = False
    compact_error = ""
    agent_exited = False

    if agent_name in _SENTINEL_NAMES:
        compact_error = "sentinel name — skipping /compact"
    elif compact_already_sent:
        compact_error = "already sent (idempotent)"
    else:
        try:
            sent, detail = _inject_compact(team_name, agent_name)
        except Exception as e:
            # /compact 是旁路终端通知：异常降级为错误说明，绝不向调用方
            # (member_report_result / leader_mark_task_complete) 传播。
            sent, detail = False, f"compact injection error: {e}"
        if sent:
            compact_sent = True
            now = datetime.datetime.now().isoformat()
            if is_leader:
                team["leader_compact_sent"] = now
            else:
                members = team.get("members", {})
                if agent_name in members:
                    members[agent_name]["compact_sent"] = now
            _save(data)
        elif detail in ("terminal dead", "no tmux session"):
            agent_exited = True
            compact_error = detail
        else:
            compact_error = detail

    # ---- 4. Detect truncation ----
    truncated = False
    try:
        abs_path = os.path.join(_share_dir(team_name), actual_path)
        if os.path.exists(abs_path):
            with open(abs_path, "r", encoding="utf-8") as f:
                content = f.read()
            truncated = len(content.encode("utf-8")) >= _MAX_COMPLETION_BYTES - 5
    except Exception:
        pass

    return {
        "compact_path": actual_path,
        "compact_sent": compact_sent,
        "compact_error": compact_error,
        "truncated": truncated,
        "agent_exited": agent_exited,
    }


# ---------------------------------------------------------------------------
# Monitor-path context generation (no explicit result from member)
# ---------------------------------------------------------------------------

def _build_monitor_completion_result(member: dict) -> str:
    """Build a synthetic result string for monitor auto-completions."""
    last_task = member.get("last_task", "")
    last_context = member.get("last_context", "")
    parts = ["[monitor auto-detected completion]"]
    if last_task:
        parts.append(f"Task: {_compact_text(last_task, 300)}")
    if last_context:
        parts.append(f"Context: {_compact_text(last_context, 300)}")
    return " | ".join(parts)


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
    agent = _member_agent(team, member)
    last_task = member.get("last_task", "")
    last_context = member.get("last_context", "")
    recovery_count = member.get("recovery_count", 0)

    lines = [
        "=" * 50,
        f"[系统] 终端恢复通知 (第{recovery_count + 1}次恢复)",
        "",
        f"团队: {team_name}",
        f"成员名: {member_name}",
        f"角色: {role}",
        f"agent: {agent}",
        f"你的团队成员身份绑定: team='{team_name}', member_name='{member_name}', role='{role}', agent='{agent}'。",
        "团队成员表中同名成员记录就是你本人；不要冒用其他成员或 leader 的身份。",
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
        "   member_get_my_task       - 查询并续跑自己上次未完成的任务",
        "   member_read_shared       - 查看团队共享上下文区最新结果",
        "   member_read_discussion   - 查看讨论模式中其他成员最后结论",
        "   member_report_discussion_conclusion - 上报讨论模式结论",
        "   member_report_result     - 回传任务结果",
        "   member_check_leader_status - 检查 leader 是否在线（中断时自动触发恢复）",
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

    agent = _member_agent(team, member)
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


def _leader_revival_config(team: dict) -> dict:
    cfg = {
        "enabled": True,
        "min_interval_seconds": 60,
        "max_revivals": 5,
    }
    stored = team.get("leader_revival_config")
    if isinstance(stored, dict):
        cfg.update(stored)
    cfg["enabled"] = bool(cfg.get("enabled", True))
    cfg["min_interval_seconds"] = max(0, int(cfg.get("min_interval_seconds", 60)))
    cfg["max_revivals"] = max(1, int(cfg.get("max_revivals", 5)))
    return cfg


def _leader_revival_allowed(team_name: str) -> bool:
    """Rate-limit + cap leader revival to avoid restart loops."""
    import datetime

    team = _team_info(team_name)
    if team.get("leader_type") != "tmux" or not team.get("terminals_active"):
        return False
    # 已无总任务、成员任务或待处理回报时无需复活，避免空闲团队因普通
    # tmux 清理被后台监控反复拉起。
    if not leader_has_unfinished_work(team):
        return False
    cfg = _leader_revival_config(team)
    if not cfg["enabled"]:
        return False
    if int(team.get("leader_revival_count", 0)) >= cfg["max_revivals"]:
        return False
    last_ts = team.get("leader_last_revival_ts", "")
    if last_ts:
        try:
            last = datetime.datetime.fromisoformat(last_ts)
            if (datetime.datetime.now() - last).total_seconds() < cfg["min_interval_seconds"]:
                return False
        except (ValueError, TypeError):
            pass
    return True


def _leader_window_is_dead(team_name: str, team: dict, session: str) -> bool:
    """A tmux leader is 'down' when its window is missing, or when the window
    still exists but the CLI process crashed to a bare shell prompt.  A live
    CLI (idle/busy/approval) is never considered dead — prevents restarting an
    active leader.
    """
    leader = team.get("leader", "")
    if not leader or not session:
        return True
    target = _member_window_target(team_name, leader)
    if not target:
        return True
    rc, out, _ = _capture_window(session, target, 40)
    if rc != 0:
        return True
    return _classify_leader_terminal_output(out) == "dead"


def _maybe_revive_leader(team_name: str, *, reason: str = "patrol", only_missing_window: bool = False) -> tuple[bool, str]:
    """Central entry of the leader interruption closed loop.

    When a tmux leader's terminal is down (window missing or process crashed to
    shell) and revival is allowed, rebuild the leader window and re-inject the
    recovery prompt so the leader resumes the unfinished overall task.  Returns
    (revived, message); a no-op for direct leaders or when the leader is active.

    ``only_missing_window=True`` skips the liveness capture (cheap patrol path):
    it revives only when the leader window is outright missing, leaving the
    crashed-to-shell case to the background monitor loop / member report path.
    This avoids an extra tmux capture during routine member patrols.
    """
    team = _team_info(team_name)
    if not team or team.get("leader_type") != "tmux":
        return False, ""
    if not team.get("terminals_active"):
        return False, ""
    leader = team.get("leader", "")
    if not leader:
        return False, ""
    if only_missing_window:
        if _member_window_target(team_name, leader):
            return False, ""  # 窗口存在：无论活跃与否都不在巡检路径重启
        return _revive_leader_terminal(team_name, reason=reason)
    session = _find_any_session(team_name)
    if session and not _leader_window_is_dead(team_name, team, session):
        return False, ""
    return _revive_leader_terminal(team_name, reason=reason)


def _leader_revival_lock(team_name: str) -> threading.Lock:
    """Return a process-local per-team lock for the full revive transaction."""
    with LEADER_REVIVAL_LOCKS_GUARD:
        return LEADER_REVIVAL_LOCKS.setdefault(team_name, threading.Lock())


def _revive_leader_terminal(team_name: str, *, reason: str = "patrol") -> tuple[bool, str]:
    """Serialize check/cleanup/spawn for a team's leader revival.

    Best-effort by contract: a revival is a side channel — it must never
    propagate an exception to callers (member_report_result, patrol,
    member_send_message, member_check_leader_status).  Any failure inside the
    locked transaction is converted to ``(False, message)`` so the caller can
    keep reporting its own primary outcome.  All callers already treat a False
    return as "no revival happened", so this is behavior-compatible.
    """
    with _leader_revival_lock(team_name):
        try:
            return _revive_leader_terminal_locked(team_name, reason=reason)
        except Exception as e:
            return False, f"leader revival error: {e}"


def _revive_leader_terminal_locked(team_name: str, *, reason: str = "patrol") -> tuple[bool, str]:
    """Rebuild the leader tmux window and restore the unfinished overall task.

    Safety:
      - Idempotent: skips when the leader window is alive with a working CLI.
      - Locked spawn: reuses ``_tmux_spawn_member`` (TERMINAL_SPAWN_LOCK +
        member_spawn_lock + three-state window check) so concurrent triggers
        (patrol thread + member report) cannot double-spawn the leader.
      - Stale-window cleanup: a crashed-to-shell window is killed first so the
        rebuilt window keeps the canonical name instead of becoming ``leader(1)``.
    """
    import datetime

    # Re-check after acquiring the per-team transaction lock. Another trigger
    # may have revived the leader while this caller was waiting; never kill the
    # newly live window in that case.
    current_team = _team_info(team_name)
    current_session = _find_any_session(team_name)
    if current_session and not _leader_window_is_dead(team_name, current_team, current_session):
        return False, "leader already live"

    if not _leader_revival_allowed(team_name):
        return False, "leader revival disabled or rate-limited"

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    if not team or team.get("leader_type") != "tmux":
        return False, "no tmux leader"
    leader = team.get("leader", "")
    members = team.get("members", {})
    leader_info = members.get(leader, {})
    if not leader or not leader_info:
        return False, "no leader member"
    leader_agent = _member_agent(team, leader_info)
    team_dir = _team_dir(team_name)

    # 确保 MCP 配置就绪
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    session = _find_any_session(team_name)
    if not session:
        session, _ok = _ensure_team_session(team_name)
        if not session or not _ok:
            return False, "cannot create tmux session"
        time.sleep(0.3)

    # 清理已崩溃到 shell 的旧 leader 窗口（窗口缺失时此处为 no-op）
    stale = _member_window_target(team_name, leader)
    if stale:
        _tmux(["kill-window", "-t", _tmux_target(session, stale)])
        time.sleep(0.3)

    # 记录 leader 复活事件（供成员与后续 leader 阅读）
    try:
        share_dir = _share_dir(team_name)
        results_file = os.path.join(share_dir, "results.jsonl")
        entry = {
            "timestamp": datetime.datetime.now().isoformat(),
            "member": leader,
            "event": "leader_revival",
            "reason": reason,
        }
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    # 恢复未完成总任务：重建后用 leader 系统提示注入恢复上下文
    leader_prompt = _leader_system_prompt(team_name, team.get("leader_last_task", ""))

    # _tmux_spawn_member 已经对“检查窗口 + 创建窗口”持有进程内锁和跨进程
    # member spawn 锁；这里不能再套 TERMINAL_SPAWN_LOCK（非可重入锁），否则
    # 恢复路径会在同一线程中自死锁。
    try:
        rc, _, err = _tmux_spawn_member(
            session, leader, leader_agent, team_dir,
            window_name=leader,
            prompt=leader_prompt if _is_codex(leader_agent) else "",
        )
    except (OSError, RuntimeError) as e:
        return False, f"leader spawn lock unavailable: {e}"
    if rc != 0:
        return False, f"leader window spawn failed: {err}"
    if err and "already exists" in err:
        # 旧窗口未被清除，禁止向可能已死的窗口注入提示
        return False, "leader window already exists (stale), skip injection"

    time.sleep(1.5)

    if not _is_codex(leader_agent):
        rc2, err2 = _inject_claude_leader_prompt(session, leader, leader_prompt)
        if rc2 != 0:
            return False, f"leader prompt inject failed: {err2}"

    def update_revival(latest_team: dict) -> dict:
        latest_team["leader_revival_count"] = int(latest_team.get("leader_revival_count", 0)) + 1
        latest_team["leader_last_revival_ts"] = datetime.datetime.now().isoformat()
        latest_team["leader_last_revival_reason"] = reason
        latest_team["leader_state"] = "active"
        latest_team["leader_idle_streak"] = 0
        latest_team["leader_last_observed_state"] = "recovering"
        return {"revival_count": latest_team["leader_revival_count"]}

    _update_team_data(team_name, update_revival)
    return True, f"leader '{leader}' revived (reason={reason})"


def _build_recovery_message_tui(team: dict, member_name: str, info: dict, team_name: str) -> str:
    """TUI 侧的恢复消息构建（与 MCP 侧 _build_recovery_context 保持格式一致）。

    此函数供 team_manger.py 导入使用，避免在 TUI 侧重复实现。
    """
    import datetime as _datetime
    team_dir = team.get("workspace_dir", "")
    share_dir = team.get("context_dir", "")
    role = info.get("role", "member")
    agent = info.get("agent") or team.get("default_agent", "claude")
    last_task = info.get("last_task", "")
    last_context = info.get("last_context", "")
    recovery_count = info.get("recovery_count", 0)

    lines = [
        "=" * 50,
        f"[系统] 终端恢复通知 (第{recovery_count + 1}次恢复)",
        "",
        f"团队: {team_name}",
        f"成员名: {member_name}",
        f"角色: {role}",
        f"agent: {agent}",
        f"你的团队成员身份绑定: team='{team_name}', member_name='{member_name}', role='{role}', agent='{agent}'。",
        "团队成员表中同名成员记录就是你本人；不要冒用其他成员或 leader 的身份。",
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
        "   member_read_discussion   - 查看讨论模式中其他成员最后结论",
        "   member_report_discussion_conclusion - 上报讨论模式结论",
        "   member_report_result     - 回传任务结果",
        "   member_check_leader_status - 检查 leader 是否在线（中断时自动触发恢复）",
        "   member_list_shared_files - 列出共享文件",
        "   member_send_message      - 向其他成员发送消息",
        "",
        "💡 请基于以上上下文继续工作，或等待 leader 分配新任务。",
        "=" * 50,
    ])
    return "\n".join(lines)


@mcp.tool
def member_get_my_task(team_name: str, member_name: str) -> str:
    """
    [成员] 查询并续跑自己上次未完成的任务（成员任务续跑）。

    工作流中断自动恢复的成员侧入口：成员重新进入后调用本工具即可取回自己
    持久化的未完成任务与上下文，继续推进，不依赖 leader 终端注入。
    每次续跑会记录 last_resume_ts / last_resume_count，供 leader 感知成员已恢复。

    - 有未完成任务: 返回任务/上下文/工作目录，并记录续跑时间戳。
    - 任务已完成: 返回完成状态，提示等待新任务。
    - 无任务: 返回待命提示。

    Args:
        team_name: 团队名称
        member_name: 成员名称
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    members = team.get("members", {})
    if member_name not in members:
        return f"❌ 成员 '{member_name}' 不存在。可用 leader_list_team 查看。"
    member = members[member_name]

    pending = member_pending_task(team, member_name)
    if pending is None:
        last_task = (member.get("last_task") or "").strip()
        if last_task:
            return (
                f"✅ 成员 '{member_name}' 上次任务已完成，无需续跑。\n"
                f"   上次任务: {_compact_text(last_task, 300)}\n"
                "   等待 leader 下发新任务，或调用 member_read_shared 查看共享结果。"
            )
        return (
            f"⏸ 成员 '{member_name}' 当前没有未完成任务。\n"
            f"   共享工作目录: {_team_dir(team_name)}\n"
            "   等待 leader 下发任务，或调用 member_read_shared 查看共享结果。"
        )

    # 原子记录续跑事件，避免与 member_report_result/监控完成并发时用旧快照
    # 覆盖最新完成状态。
    now = datetime.datetime.now().isoformat()

    def update_resume(latest_team: dict) -> dict | None:
        latest_pending = member_pending_task(latest_team, member_name)
        if latest_pending is None:
            return None
        latest_member = latest_team.get("members", {}).get(member_name, {})
        latest_member["last_resume_ts"] = now
        latest_member["last_resume_count"] = int(latest_member.get("last_resume_count", 0)) + 1
        latest_member["last_observed_state"] = "busy"
        return {
            "pending": latest_pending,
            "resume_count": latest_member["last_resume_count"],
        }

    resume_update = _update_team_data(team_name, update_resume)
    if not resume_update:
        return (
            f"✅ 成员 '{member_name}' 的任务状态已在并发操作中更新，无需续跑。\n"
            "   请调用 member_read_shared 查看最新结果，或等待 leader 下发新任务。"
        )
    pending = resume_update["pending"]
    resume_count = resume_update["resume_count"]

    lines = [
        f"🔄 成员 '{member_name}' 检测到未完成任务，已记录第 {resume_count} 次续跑。",
        f"   团队: {team_name}",
        f"   角色: {pending['role']}",
        f"   共享工作目录: {_team_dir(team_name)}",
        f"   共享上下文区: {_share_dir(team_name)}",
        f"   未完成任务: {_compact_text(pending['task'], 400)}",
    ]
    if pending["context"]:
        lines.append(f"   任务上下文: {_compact_text(pending['context'], 300)}")
    lines.extend([
        "",
        "💡 请基于以上任务继续推进。完成后调用 member_report_result 回报结果；",
        "   需要参考团队最新结果时调用 member_read_shared。",
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
    提供 member_name 时会标记该成员任务完成并保持终端空闲，
    等待 leader 下发新任务。

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
    idle_msg = ""
    if member_name:
        members = team.get("members", {})
        if member_name in members:
            if members[member_name].get("last_task"):
                members[member_name]["last_task_completed"] = True
                members[member_name]["last_observed_state"] = "idle"
                _save(data)
                task_msg = f"\n✅ 成员 '{member_name}' 的任务已标记为完成"
                idle_msg = f"\n🟢 成员 '{member_name}' 终端保持空闲，等待新任务"

    # Monitor may have inferred completion before the member had a chance to
    # submit its authoritative result. Permit exactly one explicit report to
    # deliver /compact, while preserving normal duplicate-report idempotency.
    if member_name:
        latest = _load()
        latest_member = latest.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
        if latest_member.pop("compact_sent_by_monitor", False):
            latest_member.pop("compact_sent", None)
            _save(latest)
            data = latest
            team = data.get("teams", {}).get(team_name, team)

    if not leader_has_unfinished_work(team):
        team["leader_work_state"] = "idle"
        _save(data)
    else:
        # A partial member report must keep the persisted team in active state;
        # otherwise a re-entered leader can incorrectly enter standby while
        # sibling tasks are still unfinished.
        _touch_leader_activity(team)
        _save(data)

    # ---- 1. 生成压缩上下文（先生成路径，供 results.jsonl 记录） ----
    pre_path = ""
    try:
        pre_path = _write_member_compressed_context(
            team_name, member_name or "unknown", result, artifact_path, compressed_context
        )
    except Exception as e:
        pre_path = f"生成失败: {e}"

    # ---- 2. 写入 results.jsonl（记录必须在 /compact 之前） ----
    share_dir = _share_dir(team_name)
    results_file = os.path.join(share_dir, "results.jsonl")
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "member": member_name or "unknown",
        "result": result,
        "artifact_path": artifact_path,
        "compressed_context_path": pre_path,
    }
    write_error = ""
    try:
        with open(results_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as e:
        write_error = f"⚠️ 写入 results.jsonl 失败: {e}"

    # ---- 2.5 记录 leader 待处理回报 + 激活/唤醒 leader ----
    # 成员回报即 leader 激活信号：tmux resting leader 立即注入唤醒；
    # direct/其他情况回报持久化到 leader_pending_reports，leader 重新进入时用 leader_activate 确认。
    report_notice = ""
    try:
        report_entry = {
            "timestamp": entry["timestamp"],
            "member": member_name or "unknown",
            "event": "member_report",
            "result": _compact_text(result, 500),
            "artifact_path": artifact_path,
        }

        def _append_report_entry(latest_team: dict) -> dict:
            append_leader_pending_report(latest_team, report_entry)
            return {"appended": True}

        _update_team_data(team_name, _append_report_entry)
        wake = _notify_leader_of_report(team_name, report_entry)
        if wake.get("injected"):
            report_notice = "\n🔔 已唤醒 leader 并注入本次回报。"
        elif wake.get("leader"):
            report_notice = "\n🔔 本次回报已记入 leader 待处理列表；leader 重新进入后用 leader_activate 查看确认。"
    except Exception as e:
        report_notice = f"\n⚠️ 记录 leader 回报失败: {e}"

    # ---- 3. 统一收尾：发送 /compact（写记录失败不阻断） ----
    # 安全边界：/compact 注入属于终端通知旁路动作，任何异常都不能让整个上报
    # 失败——结果在 results.jsonl 已持久化，此处把收尾异常降级为提示。
    try:
        fin = _finalize_agent_completion(
            team_name,
            member_name or "unknown",
            result,
            compressed_context=compressed_context,
            artifact_path=artifact_path,
            is_leader=False,
            compact_path=pre_path,
        )
    except Exception as e:
        fin = {
            "compact_path": pre_path,
            "compact_sent": False,
            "compact_error": f"finalize failed: {e}",
            "truncated": False,
            "agent_exited": False,
        }
    compressed_context_path = fin["compact_path"]

    compact_msg = ""
    if fin["compact_sent"]:
        compact_msg = "\n📦 已向成员终端注入 /compact"
    elif fin["compact_error"] and fin["compact_error"] != "already sent (idempotent)":
        if fin["compact_error"] != "direct leader has no terminal window":
            compact_msg = f"\n⚠️ /compact 注入失败: {fin['compact_error']}"

    # 中断闭环：成员回报即"成员回报"信号——若 leader tmux 终端已死则安全重建并恢复总任务
    # 安全边界：leader 通知/恢复是旁路动作，任何失败都不能让整个上报失败；
    # 结果在 results.jsonl 已持久化，此处仅把恢复错误降级为提示。
    revive_note = ""
    if team.get("leader_type") == "tmux":
        try:
            revived, revive_msg = _maybe_revive_leader(team_name, reason="member_report")
            if revived:
                revive_note = f"\n👑 检测到 leader 终端中断，已自动恢复并注入恢复上下文: {revive_msg}"
        except Exception as e:
            revive_note = f"\n⚠️ leader 终端恢复失败（结果已保存，不影响本次上报）: {e}"

    return (
        f"✅ 结果已记录到共享上下文区{task_msg}{idle_msg}\n"
        f"📄 {results_file}\n"
        f"🧾 压缩上下文: {compressed_context_path}{compact_msg}{revive_note}\n"
        + (f"{write_error}\n" if write_error else "")
        + (f"{report_notice}\n" if report_notice else "")
        + "💡 其他成员可调用 member_read_shared 查看。"
    )


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
def member_read_discussion(team_name: str) -> str:
    """
    [成员] 读取当前讨论模式状态和其他成员最后结论。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    discussion = _discussion_entry(team)
    has_data = discussion.get("enabled") or discussion.get("status") in ("active", "ended")
    if not has_data:
        return "📭 当前没有活跃讨论。"
    if discussion.get("status") == "ended":
        return "✅ 讨论已结束。最终结论:\n" + _discussion_summary(team)
    return "🗣️ 当前讨论状态:\n" + _discussion_summary(team)


@mcp.tool
def member_report_discussion_conclusion(
    team_name: str,
    member_name: str,
    conclusion: str,
    round_number: int = 0,
) -> str:
    """
    [成员] 上报讨论模式中的本轮结论，供 leader 和其他成员读取。

    Args:
        team_name: 团队名称
        member_name: 成员名称
        conclusion: 本轮结论
        round_number: 可选轮次；0 表示当前轮
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    members = team.get("members", {})
    if member_name not in members:
        return f"❌ 成员 '{member_name}' 不存在。"
    discussion = _discussion_entry(team)
    if not discussion.get("enabled") or discussion.get("status") != "active":
        return "❌ 当前没有活跃讨论，无法上报讨论结论。"
    if member_name not in discussion.get("participants", []):
        return f"❌ 成员 '{member_name}' 不在当前讨论参与列表中。"

    current_round = int(discussion.get("round", 1))
    target_round = int(round_number) if round_number else current_round
    if target_round != current_round:
        return f"❌ 轮次不匹配：当前轮为 {current_round}，收到 {target_round}。"

    round_key = str(current_round)
    discussion.setdefault("conclusions", {}).setdefault(round_key, {})[member_name] = conclusion
    discussion["last_update_ts"] = datetime.datetime.now().isoformat()
    _save(data)

    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "session_id": discussion.get("session_id", ""),
        "round": current_round,
        "member": member_name,
        "topic": discussion.get("topic", ""),
        "conclusion": conclusion,
    }
    try:
        with open(_discussion_file(team_name), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass

    participant_count = len(discussion.get("participants", []))
    conclusion_count = len(discussion.get("conclusions", {}).get(round_key, {}))
    return (
        f"✅ 第 {current_round} 轮讨论结论已记录 ({conclusion_count}/{participant_count})。\n"
        "💡 其他成员可调用 member_read_discussion 查看。"
    )


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
    if not target and actual_target == team.get("leader", ""):
        # 中断闭环：向 leader 发消息时若其终端已死，先安全重建再发送
        revived, _revive_msg = _maybe_revive_leader(team_name, reason="member_message")
        if revived:
            target = _member_window_target(team_name, actual_target)
    if not target:
        return f"❌ 成员 '{actual_target}' 的终端窗口不存在。"

    full_msg = f"[来自其他成员的消息] {message}"
    rc, err = _send_context_to_member(
        session,
        target,
        full_msg,
        confirm_submission=_target_is_claude_tmux_leader(team, actual_target),
    )
    if rc != 0:
        return f"❌ 发送失败: {err}"

    return f"✅ 消息已发送给 '{actual_target}'"


@mcp.tool
def member_check_leader_status(team_name: str) -> str:
    """
    [成员] 检查 leader 是否在线；若 leader 终端已中断（窗口缺失或进程崩溃到 shell）则自动触发恢复。

    中断闭环的"检测"半环：成员在回报/发消息前可先调用本工具确认 leader 存活。
    检测为死时会自动调用恢复逻辑（幂等、限流），并在共享上下文区记录 leader_revival 事件。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    leader = team.get("leader", "")
    ltype = team.get("leader_type", "")
    if ltype != "tmux":
        return "👑 Leader 模式: direct（当前会话即 leader，无需检查）。"
    if not leader:
        return "❌ 团队未指定 tmux leader。"

    session = _find_any_session(team_name)
    alive = bool(session and not _leader_window_is_dead(team_name, team, session))
    lines = [
        f"👑 Leader: {leader}",
        f"   Leader 类型: {ltype}",
        f"   在线: {'🟢' if alive else '🔴'}",
        f"   最近观测状态: {team.get('leader_last_observed_state', 'unknown')}",
        f"   工作状态: {team.get('leader_work_state', 'unknown')}",
        f"   最近恢复时间: {team.get('leader_last_revival_ts', '无')}",
        f"   恢复次数: {team.get('leader_revival_count', 0)}",
    ]
    if alive:
        lines.append("\n✅ Leader 在线，无需恢复。")
    else:
        revived, msg = _maybe_revive_leader(team_name, reason="member_check")
        if revived:
            lines.append(f"\n🔄 检测到 leader 中断，已自动恢复: {msg}\n💡 可通过 member_read_shared 查看恢复事件。")
        else:
            lines.append(f"\n🔴 Leader 中断，自动恢复未执行（{msg or '可能不在恢复窗口内'}）。\n💡 可等待巡检自动恢复，或直接 member_report_result / member_send_message 触发。")
    return "\n".join(lines)


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


# ---------------------------------------------------------------------------
# Member file read / write / delete tools
# ---------------------------------------------------------------------------


@mcp.tool
def member_read_file(team_name: str, file_path: str) -> str:
    """[成员] 读取共享上下文区中的任意普通文件。

    安全约束：
      - 仅允许相对路径（拒绝绝对路径和 .. 穿越）
      - 拒绝符号链接
      - 拒绝超过 1MB 的大文件（明确报告大小）
      - UTF-8 解码错误会明确报告偏移量和文件名

    Args:
        team_name: 团队名称
        file_path: 相对于共享上下文区的文件路径
    """
    data = _load()
    if team_name not in data.get("teams", {}):
        return f"❌ 团队 '{team_name}' 不存在。"

    abs_path, err = _safe_share_path(team_name, file_path, allow_missing=False)
    if err:
        return err

    share_dir = _share_dir(team_name)
    rel = os.path.relpath(abs_path, share_dir)

    try:
        size = os.path.getsize(abs_path)
    except OSError as e:
        return f"❌ 获取文件大小失败: {rel} ({e})"

    if size > 1_048_576:
        size_mb = size / 1_048_576
        return f"❌ 文件过大（{size_mb:.1f}MB），超过 1MB 限制: {rel}"

    try:
        with open(abs_path, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError as e:
        return f"❌ UTF-8 解码失败: {rel}\n错误位置: 偏移 {e.start}-{e.end}, 原因: {e.reason}"
    except OSError as e:
        return f"❌ 读取失败: {rel} ({e})"

    size_str = f"{size}B" if size < 1024 else f"{size / 1024:.1f}KB"
    header = f"📄 {rel} ({size_str}):\n" + "─" * 40 + "\n"
    return header + content


@mcp.tool
def member_write_file(team_name: str, file_path: str, content: str) -> str:
    """[成员] 写入或覆写共享上下文区中的普通文件。

    使用同目录临时文件 + os.replace 实现原子替换。
    保存前检测并发修改：若文件在 stat 快照后到 os.replace 前
    被其他成员修改/创建/删除，拒绝覆盖并明确报告冲突。

    Args:
        team_name: 团队名称
        file_path: 相对于共享上下文区的文件路径（允许尚不存在的文件）
        content: 要写入的文件内容（UTF-8 编码，最大 5MB）
    """
    data = _load()
    if team_name not in data.get("teams", {}):
        return f"❌ 团队 '{team_name}' 不存在。"

    abs_path, err = _safe_share_path(team_name, file_path, allow_missing=True)
    if err:
        return err

    share_dir = _share_dir(team_name)
    rel = os.path.relpath(abs_path, share_dir)

    if content is None:
        return "❌ content 不能为 None"

    content_bytes = len(content.encode("utf-8"))
    if content_bytes > 5_242_880:
        return f"❌ 内容过大（{content_bytes / 1_048_576:.1f}MB），最大允许 5MB"

    guard = _ConcurrentWriteGuard(abs_path)
    with guard:
        # Write to temp file
        try:
            with open(guard.tmp_path, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
        except OSError as e:
            return f"❌ 写入临时文件失败: {rel} ({e})"

        # Check concurrent changes and atomically replace
        replace_err = guard.check_and_replace()
        if replace_err:
            return replace_err

    return f"✅ 已写入: {rel}（{content_bytes / 1024:.1f}KB）"


@mcp.tool
def member_delete_file(team_name: str, file_path: str, confirm: bool = False) -> str:
    """[成员] 删除共享上下文区中的普通文件（包括 results.jsonl）。

    安全要求：
      - confirm 必须为 True（二次确认），否则拒绝并提示如何确认
      - 仅允许删除普通文件（拒绝目录、符号链接）
      - 无"受保护文件"概念——任何普通文件在 confirm=True 时均可删除

    Args:
        team_name: 团队名称
        file_path: 相对于共享上下文区的文件路径
        confirm: 必须显式设为 true 以确认删除操作
    """
    data = _load()
    if team_name not in data.get("teams", {}):
        return f"❌ 团队 '{team_name}' 不存在。"

    if confirm is not True:
        return (
            f"⚠️ 删除操作需要二次确认。\n"
            f"   目标文件: {file_path}\n"
            f"   请将 confirm=True 传递给 member_delete_file 以确认删除。"
        )

    abs_path, err = _safe_share_path(team_name, file_path, allow_missing=False)
    if err:
        return err

    share_dir = _share_dir(team_name)
    rel = os.path.relpath(abs_path, share_dir)

    try:
        os.unlink(abs_path)
    except OSError as e:
        return f"❌ 删除失败: {rel} ({e})"

    return f"✅ 已删除: {rel}"


def _migrate_agent_users_global_on_startup() -> None:
    """MCP 启动时执行一次 agent 用户全局迁移（幂等，跨进程锁，0600 原子写）。

    迁移在跨进程 flock 临界区内执行，TUI 同时启动也不会竞争覆盖。
    失败关闭：无法获得跨进程锁时抛 RuntimeError，这里捕获后跳过迁移——
    读路径（get_agent_user_env_prefix / resolve_agent_model 等）仍兼容旧数据，
    不阻止 MCP 启动。
    """
    try:
        migrate_agent_users_global_file(Path(DATA_FILE))
    except Exception as e:
        # 迁移失败不阻塞 MCP 启动；下次启动或 TUI 启动时会重试（幂等）
        print(f"[mult-agent-mcp] 跳过 agent 用户全局迁移（{e}）", file=sys.stderr)


def _disable_fastmcp_version_check() -> None:
    """确保 FastMCP 的启动版本检查处于关闭状态（幂等）。

    模块顶部已在导入 fastmcp 前设置 FASTMCP_CHECK_FOR_UPDATES=off；这里再对
    已实例化的 settings 做一次直接修改，覆盖环境变量被显式覆盖为非 off 的
    场景。关闭失败不阻塞启动：版本检查本身是可降级的非必要功能。
    """
    os.environ.setdefault("FASTMCP_CHECK_FOR_UPDATES", "off")
    try:
        import fastmcp

        fastmcp.settings.check_for_updates = "off"
    except Exception:
        pass


def main():
    # 启动保护：关闭 FastMCP 版本检查（socks:// 代理环境下会抛异常导致启动崩溃）
    _disable_fastmcp_version_check()
    # 启动时执行一次 agent 用户全局迁移（幂等；跨进程锁在迁移入口内部）
    _migrate_agent_users_global_on_startup()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
