"""
Multi-Agent MCP — 共享 Tmux 工具函数
====================================

供 MCP Server 与 TUI 共用的 tmux 操作底层函数。
使用绝对路径查找 tmux 可执行文件，避免 PATH 不完整。
"""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
import warnings
from pathlib import Path
from urllib.parse import urlsplit

from common.atomic_write import atomic_json_write
from common.data_layer import get_data_file, load_data, save_data, team_context_dir
from common.leader_recovery import build_leader_recovery_section

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # 非 POSIX 平台降级为仅进程内互斥
    _HAVE_FCNTL = False

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


def _window_records_with(session: str, run) -> list[dict[str, str]]:
    """使用注入的 tmux runner 列出窗口记录（便于 MCP/TUI 测试注入各自 mock）。"""
    rc, out, _ = run([
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


def member_spawn_lock_path(team_name: str, member_name: str) -> Path:
    """跨进程成员 spawn 锁文件路径。

    放在团队共享上下文目录（context_dir）下的隐藏目录 .member_spawn_locks/，
    该目录由 MCP Server 与 TUI（多进程）共享，从而保证同一成员在不同进程
    创建/恢复终端时使用同一把锁。
    """
    ctx = team_context_dir(team_name)
    safe_member = re.sub(r"[^A-Za-z0-9_.-]", "_", member_name or "member")
    return ctx / ".member_spawn_locks" / f"{safe_member}.lock"


@contextlib.contextmanager
def member_spawn_lock(team_name: str, member_name: str):
    """跨进程成员终端 spawn 锁（fcntl.flock，进程间互斥）。

    覆盖"检查窗口是否存在 + 创建窗口(new-window/new-session)"临界区，
    防止 MCP / TUI / monitor 等不同进程并发为同一成员重复创建终端。
    **Fail closed**：fcntl 不可用或锁文件无法创建时抛出明确异常，
    由调用方转为可见错误，绝不无锁继续（否则跨进程幂等保证失效）。

    用法（MCP 与 TUI 的创建/恢复路径都必须包裹此锁）：
        with member_spawn_lock(team_name, member_name):
            state, detail = member_window_state(team_name, member_name, session)
            if state == "live":
                ... 复用现有窗口，不创建 ...
            if state == "unknown":
                ... 返回可见错误，不盲目创建 ...
            tmux_run(["new-window", "-t", session, "-n", member_name, ...])

    说明：send-keys 重发应在锁外执行，不占住临界区。
    """
    path = member_spawn_lock_path(team_name, member_name)
    if not _HAVE_FCNTL:
        raise RuntimeError("当前平台不支持 fcntl，无法提供跨进程成员 spawn 锁（fail closed）")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise RuntimeError(f"无法创建/打开成员 spawn 锁文件 {path}: {e}（fail closed）") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def member_window_state(
    team_name: str,
    member_name: str,
    session: str,
    *,
    window_name: str | None = None,
    new_session: bool = False,
    run_tmux=None,
) -> tuple[str, str]:
    """成员窗口存在性三态判定（MCP/TUI 共享），使用调用方传入的 session，不重解析。

    返回 (state, detail)：
      - ('live', target):    确认存在存活窗口（按持久化 id 或窗口名匹配）
      - ('absent', ''):      确认不存在该成员窗口
      - ('unknown', reason): 无法确认（session 存活但 list-windows 查询失败/为空）

    保守规则：活 tmux session 不可能 0 窗口；has-session 成功但 records 为空，
    说明查询失败/异常，一律按 unknown 处理（不得盲目 new-window，防止瞬时失败
    时恰好重复创建），无论成员是否已有持久化窗口记录。
    """
    run = run_tmux or tmux_run
    rc, _, _ = run(["has-session", "-t", session])
    if rc != 0:
        return "absent", ""
    if new_session:
        return "live", session

    records = _window_records_with(session, run)
    if not records:
        return "unknown", "session 存活但 list-windows 查询失败/返回空"

    name = window_name or member_name
    member = load_data().get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
    stored_id = member.get("tmux_window_id", "")
    stored_session = member.get("tmux_session", "")
    stored_session_id = member.get("tmux_session_id", "")
    stored_session_created = member.get("tmux_session_created", "")
    current_session_id = records[0].get("session_id", "")
    current_session_created = records[0].get("session_created", "")
    same_instance = (
        stored_session == session
        and bool(stored_session_id)
        and bool(stored_session_created)
        and stored_session_id == current_session_id
        and stored_session_created == current_session_created
    )
    if stored_id and same_instance and any(r["id"] == stored_id for r in records):
        return "live", stored_id
    by_name = next((r for r in records if r["name"] == name), None)
    if by_name:
        return "live", by_name["id"]
    return "absent", ""


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
    model: str = "",
    settings_path: str = "",
) -> list[str]:
    args = [agent_cmd]
    normalized = normalize_member_mode(mode)
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
    return args


def codex_command(agent_cmd: str, team_dir: str, prompt: str = "", member_mode: str = "", *, model: str = "") -> list[str]:
    """构造 codex 成员启动命令。"""
    cmd = [agent_cmd, "-C", team_dir]
    cmd.extend(codex_mode_args(member_mode))
    if model:
        cmd.extend(["--model", model])
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

    # 代理前缀：env http_proxy=URL ...
    team_name = team_name_for_permissions or session.removeprefix("mcp_")
    proxy_prefix = get_proxy_env_prefix(team_name, member_name)

    # Agent User 环境变量前缀：仅在接管开关开启时注入（临时接管系统默认 agent 用户）
    atype = agent_type(agent)
    agent_user_prefix = get_agent_user_env_prefix(team_name, member_name, atype)

    # 解析 model 用于显式 --model CLI flag（绕过 env var 对特殊字符的脆弱性）
    resolved_model = resolve_agent_model(team_name, member_name)

    if is_codex(agent):
        cmd.extend(agent_user_prefix + proxy_prefix + codex_command(agent, team_dir, member_mode=mode, model=resolved_model))
    else:
        # Claude / 其他 agent: 预配置权限 + 从共享工作目录启动
        if team_name_for_permissions:
            _write_claude_permissions_internal(
                team_name_for_permissions,
                str(Path(team_dir)),
                dangerously_skip=dangerously_skip_permissions,
            )

        # 私有 settings 目录权限收紧失败时 fail closed，返回可见错误而非继续
        try:
            claude_settings_path = build_agent_user_claude_settings(team_name, member_name)
        except RuntimeError as e:
            return -1, "", str(e)

        agent_args = claude_agent_args(
            agent,
            mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
            model=resolved_model,
            settings_path=claude_settings_path,
        )
        cmd.extend(["-c", team_dir] + proxy_prefix + agent_args)

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
        # 只用 Edit(path) 规则：Claude Code v2.1.210+ 只按 Edit/Read 匹配文件权限，
        # Write(path) 规则被接受但永不生效，还会在启动时打印告警。
        allow.extend([
            f"Edit({team_dir_str}/*)",
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
    return str(settings_path)


# ============================================================
# 代理配置 — env 命令前缀注入
# ============================================================

def _proxy_enabled_from_mode(mode: str) -> bool | None:
    normalized = (mode or "").strip().lower()
    if normalized in {"enabled", "enable", "on", "true", "yes", "1"}:
        return True
    if normalized in {"disabled", "disable", "off", "false", "no", "0"}:
        return False
    return None


def member_proxy_mode(member_info: dict) -> str:
    """Return a normalized member proxy mode, preserving legacy proxy_enabled."""
    mode = (member_info.get("proxy_mode") or "").strip().lower()
    if mode in {"inherit", "enabled", "disabled"}:
        return mode
    if "proxy_enabled" in member_info:
        return "enabled" if bool(member_info.get("proxy_enabled")) else "disabled"
    return "inherit"


def member_proxy_enabled(team: dict, member_name: str = "", member_info: dict | None = None) -> bool:
    """Resolve effective proxy state for a member.

    Priority: member proxy_mode/proxy_enabled override > team proxy.enabled.
    """
    if member_info is None and member_name:
        member_info = team.get("members", {}).get(member_name, {})
    override = _proxy_enabled_from_mode(member_proxy_mode(member_info or {}))
    if override is not None:
        return override
    return bool(team.get("proxy", {}).get("enabled"))


def proxy_env_prefix_for_team(team: dict, member_name: str = "", member_info: dict | None = None) -> list[str]:
    """Build proxy env prefix from an already loaded team dict."""
    if not member_proxy_enabled(team, member_name, member_info):
        return []

    proxy_config = team.get("proxy", {})
    host = proxy_config.get("host", "127.0.0.1")
    port = proxy_config.get("port", 7890)
    proxy_url = f"http://{host}:{port}"

    return [
        "env",
        f"http_proxy={proxy_url}",
        f"https_proxy={proxy_url}",
        f"HTTP_PROXY={proxy_url}",
        f"HTTPS_PROXY={proxy_url}",
    ]


def get_proxy_env_prefix(team_name: str, member_name: str = "") -> list[str]:
    """读取团队/成员代理配置，返回 env 命令前缀列表。

    代理配置存储在 team["proxy"] 中，格式：
        {"enabled": true, "host": "127.0.0.1", "port": 7890}

    成员可通过 member["proxy_mode"] 单独覆盖是否启用代理：
        - "enabled":  强制启用（即使团队默认关闭）
        - "disabled": 强制禁用（即使团队默认开启）
        - "inherit"/未设置: 继承团队 team["proxy"]["enabled"] 默认值

    兼容旧字段 member["proxy_enabled"]。
    优先级: 成员覆盖 > 团队 proxy.enabled 默认

    返回示例:
        ["env", "http_proxy=URL", "https_proxy=URL", "HTTP_PROXY=URL", "HTTPS_PROXY=URL"]

    使用 env 命令前缀而非 tmux -e 标志，确保环境变量直接设置在进程环境中。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    return proxy_env_prefix_for_team(team, member_name)


# ============================================================
# Agent User 配置 — 临时接管系统默认 agent 用户
# ============================================================

# Sentinel for "explicitly don't take over any agent user profile".
# Members with agent_user == AGENT_USER_NONE skip both per-member profile
# and team.default_agent_user fallback — no env vars are injected.
# Must never be used as a real profile key or leak into env injection.
AGENT_USER_NONE = "__none__"

# Shell-dangerous characters that must never appear in a URL destined for env var injection.
# Includes: control chars (0x00-0x1f, 0x7f), space/tab, quotes, backticks, $, ;, &, |, <, >,
# parentheses, braces, backslashes.
_SHELL_DANGEROUS_RE = re.compile(r'[\x00-\x1f\x7f \t`\'"$;&|<>(){}\\]')

# Percent-encoded linefeed / carriage-return — equivalent to \n, \r and never legitimate
_PCT_ENCODED_CRLF_RE = re.compile(r'%0[dD]|%0[aA]', re.IGNORECASE)


def validate_agent_user_url(url: str) -> str:
    """Validate an agent user base URL for safe env var injection.

    Uses urllib.parse.urlsplit for structural validation and rejects any
    shell-dangerous characters (control chars, quotes, backticks, $, ;, &,
    |, <, >, parentheses, backslashes, etc.).

    Returns empty string on success, or a human-readable error message on failure.
    Designed for reuse by both TUI save dialogs (save-time validation) and
    terminal launch helpers (injection-time safety check).

    Allowed: http://host:port/path or https://host:port/path with no shell metacharacters.
    Rejected: whitespace, control chars, quotes, backticks, $, ;, &, |, <, >,
              parentheses, backslashes, newlines, and any non-http/https scheme.
    """
    if not url or not url.strip():
        return "URL 不能为空"

    # --- Shell-dangerous character check (must happen first, before any parse) ---
    if _SHELL_DANGEROUS_RE.search(url):
        return "URL 包含禁止的 shell 特殊字符（如 空格 $ ; & | < > ` ' \" 等）"

    if "\n" in url or "\r" in url:
        return "URL 包含换行符"

    # Percent-encoded CR/LF are equivalent to raw \n/\r — reject
    if _PCT_ENCODED_CRLF_RE.search(url):
        return "URL 包含编码的换行符"

    # --- Structural parse ---
    try:
        parsed = urlsplit(url)
    except (ValueError, AttributeError):
        return "URL 格式无效，无法解析"

    scheme = (parsed.scheme or "").lower()
    if scheme not in ("http", "https"):
        return f"不支持的协议: {scheme or '(空)'}，仅允许 http/https"

    hostname = parsed.hostname
    if not hostname:
        return "URL 缺少主机名 (hostname)"

    # netloc with @ is suspicious (userinfo injection); reject
    if "@" in (parsed.netloc or ""):
        return "URL 不允许包含 @ (userinfo)"

    # Port validation — parsed.port raises ValueError for out-of-range values
    try:
        port = parsed.port
    except ValueError:
        return "端口号无效（超出范围）"
    if port is not None:
        if not (1 <= port <= 65535):
            return f"端口号无效: {port}"

    # Re-validate the whole URL string contains no surprises after parse
    reconstituted = parsed.geturl()
    if reconstituted != url:
        # urlsplit normalized something — this is suspicious; reject
        return "URL 格式不规范，请使用标准格式如 https://host:port/path"

    return ""


# Backward-compatible internal alias used by injection helpers
def _validate_url_safe(url: str) -> bool:
    """Check if URL is safe for env var injection. Returns bool (legacy interface)."""
    return validate_agent_user_url(url) == ""


# Internal bool alias — delegates to validate_agent_user_env_value
def _validate_env_value(value: str) -> bool:
    """Check if value is safe for env var injection. Returns bool (legacy interface)."""
    return validate_agent_user_env_value(value) == ""


def validate_agent_user_env_value(value: str, field_name: str = "值") -> str:
    """Public entry point for validating any non-URL agent user env value.

    Covers API keys and model names — opaque tokens that don't have URL
    structure but MUST be free of shell metacharacters. Wraps the internal
    ``_validate_env_value`` with a user-facing error message plus a
    reasonable length cap (512 chars).

    Returns empty string on success, or a human-readable error message on failure.
    Designed for reuse by TUI save dialogs (show MessageBox) and terminal
    launch helpers (silently skip injection for that field).
    """
    if not value:
        return ""
    if _SHELL_DANGEROUS_RE.search(value):
        return f"{field_name} 包含禁止的 shell 特殊字符"
    if _PCT_ENCODED_CRLF_RE.search(value):
        return f"{field_name} 包含编码的换行符"
    if "\n" in value or "\r" in value:
        return f"{field_name} 包含换行符"
    if len(value) > 512:
        return f"{field_name} 长度超过 512 字符"
    return ""


def resolve_agent_model(team_name: str, member_name: str = "") -> str:
    """Resolve the model name from a member's agent_user profile.

    Looks up member.agent_user (or team.default_agent_user fallback) and
    returns the provider-specific model field when the profile type matches
    the member's agent type.

    **Default fallback semantics**: When falling back to team.default_agent_user
    (member.agent_user is empty), the profile is fully taken over — MODEL is
    always returned regardless of takeover_enabled, and the env prefix path
    injects API_KEY/BASE_URL consistently (the user's intent in setting a
    default profile is to use its full configuration).

    Returns the model string (e.g. "claude-sonnet-5-20251001") or empty
    string when no profile applies.
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    agent_users = _effective_agent_user_registry(data, team)
    if not agent_users:
        return ""

    members = team.get("members", {})
    member_info = members.get(member_name, {}) if member_name else {}
    user_key = member_info.get("agent_user", "")

    if user_key == AGENT_USER_NONE:
        return ""

    is_default_fallback = False
    if not user_key:
        user_key = team.get("default_agent_user", "")
        if not user_key:
            return ""
        is_default_fallback = True

    user_config = agent_users.get(user_key, {})
    # When falling back to team default, MODEL is always returned — the
    # default profile carries the user's intended model regardless of
    # takeover_enabled (which gates security-sensitive API_KEY/BASE_URL).
    if not is_default_fallback and not user_config.get("takeover_enabled"):
        return ""

    profile_agent_type = (user_config.get("agent_type") or "").strip().lower()
    if not profile_agent_type:
        return ""  # legacy profiles don't carry model

    agent = (member_info.get("agent") or team.get("default_agent") or "claude").strip()
    atype = agent_type(agent)
    if atype != profile_agent_type:
        return ""

    if profile_agent_type == "claude":
        return (user_config.get("anthropic_model") or "").strip()
    if profile_agent_type == "codex":
        return (user_config.get("codex_model") or "").strip()
    return ""


def get_agent_user_env_prefix(team_name: str, member_name: str = "", agent_type: str = "") -> list[str]:
    """读取团队/成员 agent 用户配置，返回接管 env 命令前缀列表。

    Agent 用户配置存储在 team["agent_users"] 中，支持两种格式：

    **新 typed profile**（有 agent_type 字段）：
        {"<user_key>": {
            "agent_type": "claude" | "codex",
            "takeover_enabled": bool,
            # Claude 专属:
            "anthropic_api_key": "...", "anthropic_base_url": "...", "anthropic_model": "...",
            # Codex 专属:
            "openai_api_key": "...", "openai_base_url": "...", "codex_model": "...",
        }}

    **旧 legacy profile**（无 agent_type 字段，向后兼容）：
        {"<user_key>": {
            "anthropic_base_url": "...", "openai_base_url": "...", "takeover_enabled": bool,
        }}

    成员可通过 member["agent_user"] 指定使用的 agent 用户 key。

    当 takeover_enabled=True 时：
      - typed profile: 仅当 profile.agent_type 与 agent_type 参数匹配时注入对应
        provider 的全部字段（API_KEY + BASE_URL + MODEL），空字段跳过
      - legacy profile: 回退到旧行为，仅注入与 agent_type 参数匹配的 BASE_URL
      - agent_type 为空或其他 → 返回空列表 []

    回退到 team.default_agent_user 时视为完整接管该默认 profile：
      MODEL + API_KEY/BASE_URL 均注入（与 resolve_agent_model 的 MODEL 语义一致）。

    显式关闭接管、未配置、profile 不存在、或类型不匹配时返回空列表 []。

    返回示例（typed claude profile + claude agent，接管开启）:
        ["env", "ANTHROPIC_API_KEY=sk-ant-xxx", "ANTHROPIC_BASE_URL=https://api.anthropic.com",
         "ANTHROPIC_MODEL=claude-opus-5"]

    安全性:
      - BASE_URL 字段使用 validate_agent_user_url（结构化 URL 校验）
      - API_KEY / MODEL 字段使用 validate_agent_user_env_value（shell 元字符 + 长度校验）
      - 使用 env 前缀方式，环境变量仅在当前进程生效，不持久污染
      - API Key 绝不出现在日志、错误信息或展示输出中

    终端层可直接将此列表拼接到 tmux 启动命令中，与 get_proxy_env_prefix 叠加使用。
    """
    if not agent_type:
        return []
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    # 全局 registry 优先，团队旧数据兼容合并（混合数据不丢失未迁移 profiles）
    team_view = dict(team)
    team_view["agent_users"] = _effective_agent_user_registry(data, team)
    return _agent_user_env_prefix_for_team(team_view, member_name, agent_type)


def _agent_user_env_prefix_for_team(team: dict, member_name: str = "", agent_type: str = "") -> list[str]:
    """从已加载的 team dict 构建 agent 用户 env 前缀（内部函数）。

    支持两种 profile 格式：
      - 新 typed profile（有 agent_type 字段）：类型匹配时注入 provider 专属的全部字段
      - 旧 legacy profile（无 agent_type 字段）：回退到仅注入 BASE_URL 的旧行为

    **Default fallback semantics**: 当成员未显式设置 agent_user 而回退到
    team.default_agent_user 时，视为完整接管该默认 profile——MODEL 与
    API_KEY / BASE_URL 均不受 takeover_enabled 约束（设为默认即意图使用
    其完整配置，与 resolve_agent_model 的 MODEL 语义保持一致）。
    当成员显式选择了 profile 时，所有字段均受 takeover_enabled 约束。

    注意：此进程级 env 前缀对 Claude 只是补充——用户级 ~/.claude/settings.json
    的 env 会覆盖普通进程 env，真正让 Claude 的 BASE_URL/key 接管生效的是
    build_agent_user_claude_settings 生成的每终端 --settings 覆盖
    （优先级高于 user/project settings）。Codex 无 settings-env 覆盖机制，
    进程级 env 前缀是其接管注入的主要通道。
    """
    if not agent_type:
        return []

    agent_users = team.get("agent_users", {})
    if not agent_users:
        return []

    members = team.get("members", {})
    member_info = members.get(member_name, {}) if member_name else {}
    user_key = member_info.get("agent_user", "")

    if user_key == AGENT_USER_NONE:
        # 显式不接管：跳过 default_agent_user 回退，不注入任何 env
        return []

    is_default_fallback = False
    if not user_key:
        # 成员未指定 agent_user：回退到团队默认 profile
        user_key = team.get("default_agent_user", "")
        if not user_key:
            return []
        is_default_fallback = True

    user_config = agent_users.get(user_key, {})
    takeover_enabled = bool(user_config.get("takeover_enabled"))

    # 当成员显式选择了 profile 且接管关闭时，全部字段都不注入。
    if not is_default_fallback and not takeover_enabled:
        return []

    # 完整接管判定：
    #   - 显式选择：必须 takeover_enabled=True 才注入（安全敏感）
    #   - 回退团队默认：用户意图即使用该默认 profile 的完整配置，与
    #     MODEL 保持一致 → API_KEY/BASE_URL 同样注入（不受 takeover 门控）
    full_takeover = is_default_fallback or takeover_enabled

    atype = agent_type.lower()
    profile_agent_type = (user_config.get("agent_type") or "").strip().lower()

    # ── 旧 legacy profile（无 agent_type 字段）：无 MODEL 可注入 ──
    # BASE_URL 始终受 takeover_enabled 门控（安全敏感），无 takeover → 空
    if not profile_agent_type:
        if not takeover_enabled:
            return []
        if atype == "claude":
            base_url = (user_config.get("anthropic_base_url") or "").strip()
            if base_url and _validate_url_safe(base_url):
                return ["env", f"ANTHROPIC_BASE_URL={base_url}"]
        elif atype == "codex":
            base_url = (user_config.get("openai_base_url") or "").strip()
            if base_url and _validate_url_safe(base_url):
                return ["env", f"OPENAI_BASE_URL={base_url}"]
        return []

    # ── 新 typed profile：类型必须匹配 ──
    if atype != profile_agent_type:
        return []

    env_vars: list[str] = []
    if profile_agent_type == "claude":
        # API_KEY / BASE_URL 在 full_takeover（显式开启或回退默认）时注入
        if full_takeover:
            api_key = (user_config.get("anthropic_api_key") or "").strip()
            if api_key and _validate_env_value(api_key):
                env_vars.append(f"ANTHROPIC_API_KEY={api_key}")
            base_url = (user_config.get("anthropic_base_url") or "").strip()
            if base_url and _validate_url_safe(base_url):
                env_vars.append(f"ANTHROPIC_BASE_URL={base_url}")
        # MODEL 与完整接管保持一致（显式开启或回退默认时注入）
        model = (user_config.get("anthropic_model") or "").strip()
        if model and _validate_env_value(model):
            env_vars.append(f"ANTHROPIC_MODEL={model}")
    elif profile_agent_type == "codex":
        if full_takeover:
            api_key = (user_config.get("openai_api_key") or "").strip()
            if api_key and _validate_env_value(api_key):
                env_vars.append(f"OPENAI_API_KEY={api_key}")
            base_url = (user_config.get("openai_base_url") or "").strip()
            if base_url and _validate_url_safe(base_url):
                env_vars.append(f"OPENAI_BASE_URL={base_url}")
        model = (user_config.get("codex_model") or "").strip()
        if model and _validate_env_value(model):
            env_vars.append(f"CODEX_MODEL={model}")

    if env_vars:
        return ["env"] + env_vars
    return []


# ---- 每终端 Claude --settings 覆盖（高于 user/project settings） ----

# 影响 Claude provider 选择的 ANTHROPIC_* 变量全集。生成 --settings 文件时，
# 仅显式处理这些变量：当前 profile 提供的字段用真实值，其余置 ""
# （空串 → Claude 视为未设置，覆盖用户级 ~/.claude/settings.json 中遗留的
# AUTH_TOKEN / DEFAULT_* 模型等）。**不得**清理 OPENAI_*/CODEX_* —— 那是
# Claude 子进程（Bash/MCP 工具）可能用到的另一 provider 环境，无理由清除会
# 污染其子进程。
_CLAUDE_AGENT_USER_ENV_VARS: tuple[str, ...] = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "ANTHROPIC_REASONING_MODEL",
)


def _sanitize_settings_component(value: str) -> str:
    """文件名组件消毒：非法字符替换为 `_`，并附 8 位哈希避免消毒碰撞。

    不同原始串可能消毒出相同 base（如 "a/b" 与 "a_b" 都 → "a_b"），
    追加原始串哈希后缀保证不碰撞（跨团队同名成员 / legacy 同名异配置均隔离）。
    """
    import hashlib

    base = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "") or "empty"
    digest = hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:8]
    return f"{base}__{digest}"


def _agent_user_settings_path(team_name: str, member_name: str, profile_key: str) -> Path:
    """每终端独立的私有 --settings 文件路径（数据文件同目录的隐藏子目录）。

    文件名含 team + member + profile key（各自带哈希后缀避免消毒碰撞）：
      - 跨团队同名成员 / 同名异配置 profile 互不覆盖；
      - 成员切换 profile 时生成不同文件，已运行终端持有的旧文件不被覆盖，
        保证多 profile 并发隔离。
    目录权限 0700，文件 0600（atomic_json_write），位于私有位置，
    绝不上团队共享 .claude/settings.json。
    """
    base = get_data_file().parent
    parts = (
        _sanitize_settings_component(team_name),
        _sanitize_settings_component(member_name),
        _sanitize_settings_component(profile_key),
    )
    return base / ".agent_user_settings" / f"{'__'.join(parts)}.json"


def _ensure_settings_dir(path: Path) -> None:
    """确保 settings 文件所在目录存在且为 0700；权限无法收紧时 **fail closed**。

    凭据目录权限不可靠时绝不能继续写 secret —— 直接抛 OSError，由
    build_agent_user_claude_settings 转为可见的 RuntimeError，spawn 路径转为
    可见错误，绝不静默写入不安全的目录。
    """
    d = path.parent
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError as e:
        raise OSError(
            f"无法将私密 Claude settings 目录权限收紧为 0700: {d}（fail closed，拒绝写入凭据）"
        ) from e


def build_agent_user_claude_settings(team_name: str, member_name: str = "") -> str:
    """为 claude 成员构建"每终端独立"的私有 --settings 覆盖文件。

    根因：用户级 ~/.claude/settings.json 的 env 块会覆盖普通进程 env，且遗留
    ANTHROPIC_AUTH_TOKEN 优先于 ANTHROPIC_API_KEY，只有 --model(CLI 参数) 不受
    影响——因此出现"仅 model 生效、ANTHROPIC_BASE_URL/key 未接管"。本函数生成
    一个优先级高于 user/project settings 的 --settings 文件，其 env 块显式设置
    profile 的 ANTHROPIC_API_KEY / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL，并把
    AUTH_TOKEN / ANTHROPIC_DEFAULT_* 等置空，实现多 base_url / 多 key 并发隔离。

    返回 --settings 文件路径；当成员未接管（系统默认 / __none__ / takeover 关闭
    / 类型不匹配 / 非 claude typed profile）时返回 ""，让用户级系统默认生效。
    绝不写入团队共享 .claude/settings.json；文件 0600 原子写入私有位置。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    agent_users = _effective_agent_user_registry(data, team)
    if not agent_users:
        return ""

    member_info = team.get("members", {}).get(member_name, {}) if member_name else {}
    user_key = member_info.get("agent_user", "")

    if user_key == AGENT_USER_NONE:
        return ""  # 显式不接管 → 系统默认

    is_default_fallback = False
    if not user_key:
        user_key = team.get("default_agent_user", "")
        if not user_key:
            return ""  # 系统默认
        is_default_fallback = True

    user_config = agent_users.get(user_key, {})
    takeover_enabled = bool(user_config.get("takeover_enabled"))
    profile_agent_type = (user_config.get("agent_type") or "").strip().lower()

    agent = (member_info.get("agent") or team.get("default_agent") or "claude").strip()

    # 显式选择 + takeover 关闭 → 系统默认（不覆盖）
    if not is_default_fallback and not takeover_enabled:
        return ""

    # 仅处理 ANTHROPIC_*（影响 Claude provider 选择）；值必须复用现有校验
    # （validate_agent_user_url / validate_agent_user_env_value），与
    # _agent_user_env_prefix_for_team 的防线一致，非法值按空处理（不注入）。
    env = {var: "" for var in _CLAUDE_AGENT_USER_ENV_VARS}
    base_url = (user_config.get("anthropic_base_url") or "").strip()
    model = (user_config.get("anthropic_model") or "").strip()
    api_key = (user_config.get("anthropic_api_key") or "").strip()

    if profile_agent_type == "claude":
        # typed claude：接管时 key/base_url 仅在完整接管下注入，model 始终注入
        if not is_default_fallback and not takeover_enabled:
            return ""
        if agent_type(agent) != "claude":
            return ""  # 类型不匹配 → 系统默认
        if is_default_fallback or takeover_enabled:
            if api_key and validate_agent_user_env_value(api_key, "ANTHROPIC_API_KEY") == "":
                env["ANTHROPIC_API_KEY"] = api_key
            if base_url and validate_agent_user_url(base_url) == "":
                env["ANTHROPIC_BASE_URL"] = base_url
        if model and validate_agent_user_env_value(model, "ANTHROPIC_MODEL") == "":
            env["ANTHROPIC_MODEL"] = model
    else:
        # legacy profile（无 agent_type）：仅注入 ANTHROPIC_BASE_URL（受 takeover 门控），
        # 同样走 --settings 私有文件，避免 base_url 进入命令行。
        if profile_agent_type:
            return ""  # 非 claude 类型（codex 等）由进程级 env 前缀处理
        if not takeover_enabled:
            return ""
        if agent_type(agent) != "claude":
            return ""
        if base_url and validate_agent_user_url(base_url) == "":
            env["ANTHROPIC_BASE_URL"] = base_url

    path = _agent_user_settings_path(team_name, member_name, user_key)
    try:
        _ensure_settings_dir(path)
        # 只写 Claude 官方 settings 根键 env；绝不附加自定义根字段（如 _agent_user_key）：
        # Claude Code 对 settings 有 schema 校验，未知根字段可能使整个文件 invalid/被忽略，
        # 导致真实终端仍只有 model 生效。profile 归属由文件名末尾的 hashed profile 分量
        # 表达，purge 据此精确清理，无需内嵌自定义字段。
        atomic_json_write(path, {"env": env})
    except OSError as e:
        raise RuntimeError(
            f"无法创建私密 Claude settings 文件 {path}（fail closed，拒绝写入凭据）: {e}"
        ) from e
    return str(path)


def _settings_filename_matches(fname: str, team_comp: str, member_comp: str,
                               profile_comp: str) -> bool:
    """文件名分量精确匹配（分量本身可含 `__`，故用前缀 + 分隔符而非 split）。

    文件名格式: <sanitize(team)>__<sanitize(member)>__<sanitize(profile)>.json。
    按“分量值 + '__'”前缀精确比对 team/member，profile 分量用文件名末尾的
    精确 hashed 分量匹配。避免原始子串宽匹配误伤跨团队同名成员。
    """
    name = fname[:-len(".json")] if fname.endswith(".json") else fname
    if team_comp:
        prefix = team_comp + "__"
        if not name.startswith(prefix):
            return False
        name = name[len(prefix):]
    if member_comp:
        prefix = member_comp + "__"
        if not name.startswith(prefix):
            return False
        name = name[len(prefix):]
    if profile_comp:
        # profile 分量 = 文件名最后一个分量（含哈希后缀）；settings JSON 不含
        # 自定义字段，归属只能由文件名分量表达。3 分量格式下末分量即 profile。
        if not (name == profile_comp or name.endswith("__" + profile_comp)):
            return False
    return True


def purge_agent_user_settings(profile_key: str, team_name: str = "", member_name: str = "") -> tuple[int, list[str]]:
    """清理引用 profile_key 的私有 settings 文件（profile 删除/重命名后旧凭据残留）。

    profile 删除/重命名后，旧 settings 文件可能仍含该 profile 的 key/base_url；
    本函数扫描 .agent_user_settings/ 下文件，按文件名末尾的精确 hashed profile
    分量匹配并删除（settings JSON 只含 Claude 官方 env 键，不含自定义归属字段）。

    边界语义（精确限定，不做宽泛删除）：
      - 仅作用于数据文件旁的 .agent_user_settings/ 目录，绝不触碰其他路径；
      - 只删除文件名 profile 分量 == _sanitize_settings_component(profile_key)
        的文件；不匹配的文件（含旧格式/任意命名）宁可保留也不误删；
      - 可选按 team/member 缩小范围：按文件名分量（含哈希后缀）精确匹配，
        不做原始子串宽匹配，避免跨团队同名成员被误删/漏删。

    返回 (removed, failed)：
      - removed: 成功删除的文件数；
      - failed:  已确认归属本 profile 但删除失败的文件路径列表 —— 可恢复错误
                 显式上报，不静默吞掉；调用方（sweep/TUI）可据此提示，避免
                 “删除了 profile 但旧凭据无说明地残留”。
    幂等；目录/文件不存在时返回 (0, [])。
    """
    if not profile_key:
        return 0, []
    base = get_data_file().parent
    d = base / ".agent_user_settings"
    if not d.exists():
        return 0, []
    team_comp = _sanitize_settings_component(team_name) if team_name else ""
    member_comp = _sanitize_settings_component(member_name) if member_name else ""
    profile_comp = _sanitize_settings_component(profile_key)
    removed = 0
    failed: list[str] = []
    for f in sorted(d.glob("*.json")):
        if not _settings_filename_matches(f.name, team_comp, member_comp, profile_comp):
            continue
        try:
            f.unlink()
            removed += 1
        except OSError:
            failed.append(str(f))  # 可恢复错误显式上报，不静默吞掉
    return removed, failed


def get_agent_user_config(team_name: str, member_name: str = "") -> dict | None:
    """获取成员当前生效的 agent 用户配置（供查询/展示用）。

    返回 None 表示使用系统默认。
    返回 dict 包含 user_key, agent_type, takeover_enabled 及 provider 字段。
    API Key 字段仅返回"已配置"或"未配置"，绝不泄露原文。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    agent_users = _effective_agent_user_registry(data, team)
    if not agent_users:
        return None

    members = team.get("members", {})
    member_info = members.get(member_name, {}) if member_name else {}
    user_key = member_info.get("agent_user", "")

    if user_key == AGENT_USER_NONE:
        # 显式不接管：不应用任何 profile
        return None

    if not user_key:
        # 成员未指定 agent_user：回退到团队默认 profile
        user_key = team.get("default_agent_user", "")
    if not user_key or user_key not in agent_users:
        return None

    user_config = agent_users[user_key]
    result = {
        "user_key": user_key,
        "agent_type": user_config.get("agent_type", ""),
        "takeover_enabled": bool(user_config.get("takeover_enabled")),
        "anthropic_base_url": user_config.get("anthropic_base_url", ""),
        "openai_base_url": user_config.get("openai_base_url", ""),
        "anthropic_model": user_config.get("anthropic_model", ""),
        "codex_model": user_config.get("codex_model", ""),
    }
    # API Key 掩码：仅返回"已配置/未配置"，绝不出现在日志或展示中
    for key_field in ("anthropic_api_key", "openai_api_key"):
        result[key_field] = "已配置" if user_config.get(key_field) else "未配置"
    return result


def list_agent_users(team_name: str) -> dict[str, dict]:
    """列出指定团队的 agent 用户 profiles（全局 registry 优先）。

    返回 { user_key: {...profile...}, ... }，其中：
      - 全局 registry data['agent_users'] 为 post-migration 的 source of truth；
      - 兼容未迁移的团队旧数据 team['agent_users']（合并展示，键冲突时团队
        旧数据优先），保证混合数据下不会因全局已有一项而忽略未迁移 profiles。
    未配置时返回空 dict。
    注意：返回原始数据；调用方负责掩码 API key 等敏感字段。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    return _effective_agent_user_registry(data, team)


# ============================================================
# Agent User 全局 registry — 迁移 / 合并读取 / 引用 sweep
# ============================================================

def _effective_agent_user_registry(data: dict, team: dict) -> dict:
    """返回某团队生效的 agent-user profile registry（全局 + 团队旧数据合并）。

    兼容混合/未迁移数据：不能因全局 registry 已有一项就忽略未迁移的团队
    team["agent_users"]。合并规则：团队旧数据优先（键冲突时团队自己的 profile
    胜出），这与迁移 R3 的语义一致（冲突的团队 profile 被改名为 key__N，
    且仅将本团队引用指向它），从而保证迁移前后读取结果一致。
    """
    global_registry = data.get("agent_users")
    if not isinstance(global_registry, dict):
        global_registry = {}
    legacy = team.get("agent_users")
    if not isinstance(legacy, dict) or not legacy:
        return global_registry
    merged = dict(global_registry)
    merged.update(legacy)  # 团队旧数据优先
    return merged


def _next_available_key(registry: dict, base: str) -> str:
    """稳定分配 key__2、key__3…（首个冲突从 __2 递增）。"""
    i = 2
    while f"{base}__{i}" in registry:
        i += 1
    return f"{base}__{i}"


def _find_existing_variant_with_cfg(registry: dict, base: str, cfg: dict) -> str | None:
    """在已有变体 key__N 中查找与 cfg 相同的那个；找不到返回 None。

    复用已有同配置变体，避免同一份配置被重复分配多个 key（跨团队/跨批次
    迁移保持幂等，不会生成多余变体）。
    """
    i = 2
    while True:
        candidate = f"{base}__{i}"
        if candidate not in registry:
            return None  # 首个缺失变体之后不可能再有同名变体
        if registry[candidate] == cfg:
            return candidate
        i += 1


def _sync_team_refs(data: dict, team_name: str, old_key: str, new_key: str) -> None:
    """仅同步 team_name 团队的 default/member 引用（R3：其他团队不动）。"""
    team = data["teams"][team_name]
    if team.get("default_agent_user") == old_key:
        team["default_agent_user"] = new_key
    for member in team.get("members", {}).values():
        if member.get("agent_user") == old_key:
            member["agent_user"] = new_key


def migrate_agent_users_global(data: dict) -> dict:
    """结构性迁移：team["agent_users"] → 全局 data["agent_users"] registry（R1-R5）。

    - R1 普通迁移：  key ∉ 全局 → M[key] = cfg，团队内引用不变
    - R2 同名合并：  key ∈ 全局 且 cfg 相同 → 不重复写（去重）
    - R3 同名冲突：  key ∈ 全局 且 cfg 不同 → 先复用已有同 cfg 变体（key__N），
                    否则稳定生成 key__2/key__3…；仅同步"发生冲突的团队"的
                    default/member 引用；两份配置零丢失
    - R4 不接管保护：key == AGENT_USER_NONE 绝不被迁移/合并/重命名
    - R5 幂等：      再次执行结果完全一致（迁移后清除团队级存储 → no-op）

    稳定遍历：团队按名排序、profile 按 key 排序（可复现）。
    返回新 dict，不改输入。不得丢任何 profile/凭据。
    """
    migrated = copy.deepcopy(data)
    registry = migrated.get("agent_users")
    if not isinstance(registry, dict):
        registry = {}
        migrated["agent_users"] = registry
    for team_name in sorted(migrated.get("teams", {})):
        team = migrated["teams"][team_name]
        legacy = team.get("agent_users")
        if not isinstance(legacy, dict) or not legacy:
            continue
        for key in sorted(legacy):
            if key == AGENT_USER_NONE:
                continue  # R4 不接管绝不被迁移
            cfg = legacy[key]
            if key in registry:
                if registry[key] == cfg:
                    continue  # R2 同名同配置：合并（不重复写）
                # R3 同名不同配置：复用已有同 cfg 变体，否则稳定分配唯一新 key
                variant = _find_existing_variant_with_cfg(registry, key, cfg)
                if variant is None:
                    variant = _next_available_key(registry, key)
                    registry[variant] = cfg
                _sync_team_refs(migrated, team_name, key, variant)
            else:
                registry[key] = cfg  # R1 普通迁移
        team.pop("agent_users", None)  # 迁移后清除团队级存储（幂等）
    return migrated


def agent_user_migration_lock_path(data_file: Path) -> Path:
    """迁移锁文件路径（与数据文件同目录，供 TUI/MCP 跨进程共享同一把锁）。"""
    path = Path(data_file)
    return path.parent / f".{path.name}.agent_users.lock"


@contextlib.contextmanager
def agent_user_migration_lock(data_file: Path):
    """跨进程 agent 用户迁移临界区锁（fcntl.flock）。

    **Fail closed**：fcntl 不可用或锁文件无法打开时抛出明确异常，调用方
    不得在无锁情况下继续迁移（否则与并发启动的 TUI/MCP 竞争覆盖）。
    """
    path = agent_user_migration_lock_path(data_file)
    if not _HAVE_FCNTL:
        raise RuntimeError("当前平台不支持 fcntl，无法提供跨进程迁移锁（fail closed）")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise RuntimeError(f"无法创建/打开 agent 用户迁移锁文件 {path}: {e}（fail closed）") from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def migrate_agent_users_global_file(data_file: Path | None = None) -> dict:
    """在数据文件上执行 agent 用户全局迁移（跨进程安全、0600 原子写、幂等）。

    - data_file 为空时使用 data_layer.get_data_file()（TUI/MCP 生产默认路径）。
    - 在跨进程 flock 临界区内执行 load → migrate → save（0600 原子写）。
    - 失败关闭：无法获得跨进程锁时抛 RuntimeError，由调用方决定降级
      （读路径仍兼容旧数据），绝不无锁迁移。
    - 重复运行完全幂等（二次为 no-op，不写盘）。
    """
    path = Path(data_file) if data_file is not None else get_data_file()
    with agent_user_migration_lock(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {"teams": {}}
        migrated = migrate_agent_users_global(data)
        if migrated != data:
            atomic_json_write(path, migrated)
        return migrated


# ---- 全局 rename / delete 引用 sweep（可被 TUI / MCP 公共调用） ----

def agent_user_ref_count(data: dict, key: str) -> tuple[int, int]:
    """统计所有团队对 key 的引用数。返回 (受影响团队数, 受影响成员数)。"""
    teams = 0
    members = 0
    for team in data.get("teams", {}).values():
        t_hit = False
        if team.get("default_agent_user") == key:
            t_hit = True
        for member_info in team.get("members", {}).values():
            if member_info.get("agent_user") == key:
                members += 1
                t_hit = True
        if t_hit:
            teams += 1
    return teams, members


def agent_user_rename_sweep(data: dict, old_key: str, new_key: str) -> tuple[int, int]:
    """全局 rename 后 sweep 所有团队的 default/member 引用，并同步旧团队级存储。

    - team.default_agent_user == old_key → new_key
    - member.agent_user == old_key        → new_key（含跨团队）
    - 旧 team['agent_users'] 中的 old_key 一并改名（兼容未迁移数据）
    - 清理旧 key 的私有 settings 残留（旧凭据不再有效，下次 spawn 按新 key 重建）
    返回 (受影响团队数, 受影响成员数)。
    """
    teams = 0
    members = 0
    for team in data.get("teams", {}).values():
        t_hit = False
        if team.get("default_agent_user") == old_key:
            team["default_agent_user"] = new_key
            t_hit = True
        for member_info in team.get("members", {}).values():
            if member_info.get("agent_user") == old_key:
                member_info["agent_user"] = new_key
                members += 1
                t_hit = True
        legacy = team.get("agent_users")
        if isinstance(legacy, dict) and old_key in legacy:
            legacy[new_key] = legacy.pop(old_key)
            t_hit = True
        if t_hit:
            teams += 1
    # 旧 key 的私有 --settings 残留一并清理（不随 rename 无限残留旧凭据）；
    # 删除失败的可恢复错误显式留痕，不静默吞掉
    _purge_with_warning(old_key, "rename")
    return teams, members


def agent_user_delete_sweep(data: dict, key: str) -> tuple[int, int]:
    """全局 delete 后 sweep 所有团队的 default/member 引用与旧团队级存储。

    - team.default_agent_user == key → 清除
    - member.agent_user == key        → 清除（回退团队默认），不强制写 AGENT_USER_NONE
    - 旧 team['agent_users'] 中的 key 一并移除（兼容未迁移数据）
    - 清理该 profile 的私有 settings 残留（旧凭据随删除一并清理，避免无限残留）
    返回 (受影响团队数, 受影响成员数)。
    """
    teams = 0
    members = 0
    for team in data.get("teams", {}).values():
        t_hit = False
        if team.get("default_agent_user") == key:
            team.pop("default_agent_user", None)
            t_hit = True
        for member_info in team.get("members", {}).values():
            if member_info.get("agent_user") == key:
                member_info.pop("agent_user", None)
                members += 1
                t_hit = True
        legacy = team.get("agent_users")
        if isinstance(legacy, dict) and key in legacy:
            legacy.pop(key, None)
            if not legacy:
                team.pop("agent_users", None)
            t_hit = True
        if t_hit:
            teams += 1
    # 被删 profile 的私有 --settings 残留一并清理（旧凭据不再有效）；
    # 删除失败的可恢复错误显式留痕，不静默吞掉
    _purge_with_warning(key, "delete")
    return teams, members


def _purge_with_warning(profile_key: str, op: str) -> tuple[int, list[str]]:
    """执行私有 settings 清理，并在失败时用 warnings 显式留痕。

    purge_agent_user_settings 的可恢复错误（已匹配但删除失败）必须可被看到，
    不能静默吞掉——否则用户删除 profile 后旧凭据无说明地残留。
    返回 (removed, failed) 供调用方进一步处理。
    """
    removed, failed = purge_agent_user_settings(profile_key)
    if failed:
        warnings.warn(
            f"Agent 用户 {op} 后清理私有 settings 失败 {len(failed)} 个文件"
            f"（旧凭据可能残留）: {', '.join(failed)}",
            RuntimeWarning,
            stacklevel=2,
        )
    return removed, failed
