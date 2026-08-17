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
from common import classifier_fallback
from common import prompt_registry
from common.data_layer import (
    get_data_file,
    load_data,
    load_data_locked,
    save_data,
    save_data_locked,
    team_context_dir,
)
from common.leader_recovery import build_leader_recovery_section

try:
    import fcntl
    _HAVE_FCNTL = True
except ImportError:  # 非 POSIX 平台降级为仅进程内互斥
    _HAVE_FCNTL = False

AUTHORIZATION_MUTEX = threading.Lock()
# Agent 用户池等数据写入的进程内互斥锁（RLock 允许 load_data_locked 重入）
TEAM_DATA_LOCK = threading.RLock()
CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__member_*",
    "mcp__mult_agent_mcp__member_*",
]
# 所有 Claude 终端（leader 与普通成员）共享的 Bash/Edit 自主执行放行。
# Claude-as-leader 没有上级替它确认授权，若 Bash/Edit 弹 approval 会永久卡死；
# 普通成员同样放开（用户明确授权）。
#
# 【F1 修复 2026-08-12（leader 批准）】基座 = **精选、可审计安全 pattern**，与
# classifier_fallback.CLAUDE_FALLBACK_BASH_PATTERNS 一致（git:/pwd:/ls:/cat:/echo:
# /wc:/head:/tail:/grep:/which:/whoami:/date:/python3 -m pytest/unittest/compileall）。
# **不得保留裸 Bash**（等价 Bash(*)，真机 2.1.228 实证可创建 /tmp 外文件，无条件
# 放行全部 shell 含 workspace 外危险命令 rm/sudo/curl/wget...）——非安全 Bash 必须
# 进入正常审批（auto 下 monitor auto-authorize，manual 下人工批准），绝不无条件放行。
#
# **裸 Edit 已移除**（无路径不匹配文件权限，no-op）；workspace 内 Edit/Write 由
# scoped ``Edit(<ws>/*)`` 规则承载（G1 真机实证 Edit(path) 规则覆盖 Write 新建），
# 由 settings writer 显式写 + claude_terminal_allow_tools 无条件注入 argv 层。
# MCP 前缀（leader_* / member_*）不在此列，由各层独立拼接，严格隔离。
CLAUDE_BASH_EDIT_ALLOW_PATTERNS = list(classifier_fallback.CLAUDE_FALLBACK_BASH_PATTERNS)


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


def exact_session_target(session: str) -> str:
    """tmux 精确 session 目标（`=name`）。

    tmux 的 target-session 解析默认做**前缀匹配**：只有 `mcp_team_215956` 存在时，
    `has-session -t mcp_team` 仍返回 0、`attach -t mcp_team` 会连进那个 session、
    `kill-session -t mcp_team` 会杀掉它。团队 session 有 `mcp_{team}` 与
    `mcp_{team}_{HHMMSS}` 两种命名且会并存，前缀匹配会让"已杀的 session"看起来
    还活着（重连循环因此不 break，被送回兄弟 session）。凡是按名字精确定位某个
    session 的场合都必须用本函数。
    """
    return f"={session}"


def find_all_tmux_sessions(team: str) -> list[str]:
    """团队名下**全部**存活 session（精确名 + 带时间戳名），以 list-sessions 为准。

    一个团队可能同时拥有多个 session：MCP server 建 `mcp_{team}`（含
    `_ensure_team_session` 的中断重建），TUI 建 `mcp_{team}_{HHMMSS}`。
    "关闭所有终端"必须遍历全部，只杀一个会留下活着的兄弟。

    精确名的存在性只认 list-sessions 的真实输出，**不能用 `has-session` 的返回码**
    —— 那是前缀匹配，只有 `mcp_{team}_HHMMSS` 时会误判精确名存在，从而把一个
    根本不存在的短名当候选返回（MCP 侧 `_find_any_session` 已修，这份副本此前漏修）。
    """
    rc, out, _ = tmux_run(["list-sessions", "-F", "#{session_name}"])
    if rc != 0 or not out:
        return []
    names = out.split("\n")
    session = tmux_session_name(team)
    prefix = f"{session}_"
    sessions: list[str] = []
    if session in names:
        sessions.append(session)
    for name in names:
        if name.startswith(prefix) and name not in sessions:
            sessions.append(name)
    return sessions


def find_tmux_session(team: str) -> str | None:
    """
    查找团队的 tmux session，支持两种命名格式：
      1. mcp_{team}           (MCP server 创建，无时间戳)
      2. mcp_{team}_HHMMSS    (TUI 创建，带时间戳)
    如果有多个匹配项，优先返回精确匹配（无时间戳），其次返回最新的。
    """
    session = tmux_session_name(team)
    candidates = find_all_tmux_sessions(team)

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


BASE_WINDOW_NAME = "__base"


def drop_base_window(session: str, run=None) -> bool:
    """脚手架窗口用完即撤：session 内已有真实窗口时删掉 `__base`。

    `__base` 是个**没有任何 CLI 的空壳**，只为"先有 session 才能 new-window"而建：
      · TUI direct 分支（成员窗要有个 session 落脚）；
      · MCP `_ensure_team_session`（session 意外死亡后重建，随后把成员/leader 窗接进来）。
    两处都只创建、从不回收，于是它作为窗口 0 长期霸占 session —— 用户 attach 进去
    正对着一个 bash 提示符，看起来像"agent 没起来 / leader 消失了"。

    硬约束：**只有存在至少一个非 `__base` 窗口时才 kill**。tmux 杀掉最后一个窗口
    会连 session 一起带走，脚手架回收绝不能反过来把刚建好的 session 干掉。

    幂等、best-effort：没有 `__base`、session 不存在、tmux 报错都只返回 False，
    绝不抛异常打断调用方的主流程（spawn / 恢复 / 换号）。

    Args:
        session: 目标 session 名。
        run: 注入的 tmux runner（默认 `tmux_run`）；MCP 侧传自己的 `_tmux`，
             便于两边各自 mock（同 `_window_records_with` 的注入约定）。

    Returns:
        True 表示确实删掉了一个 `__base` 窗口。
    """
    runner = run or tmux_run
    try:
        records = _window_records_with(session, runner)
    except Exception:
        return False
    base = [r for r in records if r.get("name") == BASE_WINDOW_NAME]
    others = [r for r in records if r.get("name") != BASE_WINDOW_NAME]
    if not base or not others:
        return False
    dropped = False
    for record in base:
        # 按 window_id（@N）定位：window_id 全局唯一，不受同名窗口/前缀匹配影响。
        rc, _, _ = runner(["kill-window", "-t", record["id"]])
        dropped = dropped or rc == 0
    return dropped


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
    effort: str = "",
    append_system_prompt_file: str = "",
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
    # 成员级 effort 覆盖：Claude Code 原生 --effort（low/medium/high/xhigh/max）
    normalized_effort = normalize_effort(effort, "claude")
    if normalized_effort in CLAUDE_EFFORT_LEVELS:
        args.extend(["--effort", normalized_effort])
    # 身份进 system 层（fact-check §8）：--append-system-prompt-file 是 Claude 唯一
    # 可靠通道（/compact 免疫，每次启动含 resume 必带）。生产 spawn 点传入真实
    # 身份文件；未显式传入回落确定性默认路径，保证与 MCP 版 _claude_agent_args
    # 逐字一致（双 builder 同步防回漂，TUI 6 spawn 点走本副本）。
    if not append_system_prompt_file:
        append_system_prompt_file = prompt_registry.default_claude_identity_path()
    args.extend(["--append-system-prompt-file", append_system_prompt_file])
    return args


def codex_command(agent_cmd: str, team_dir: str, prompt: str = "", member_mode: str = "", *, model: str = "", effort: str = "") -> list[str]:
    """构造 codex 成员启动命令。

    effort 经 `-c model_reasoning_effort="<level>"` 注入：Codex CLI 通过
    -c/--config 覆盖 config.toml 的 model_reasoning_effort（全局/成员级
    均可用，本机 Codex 已接受该配置）。effort 归一化后为受限枚举
    （low/medium/high/xhigh/max），无 shell 元字符，引号只是形式保证。
    """
    cmd = [agent_cmd, "-C", team_dir]
    cmd.extend(codex_mode_args(member_mode))
    if model:
        cmd.extend(["--model", model])
    normalized_effort = normalize_effort(effort, "codex")
    if normalized_effort in CODEX_EFFORT_LEVELS:
        cmd.extend(["-c", f'model_reasoning_effort="{normalized_effort}"'])
    if prompt:
        cmd.append(prompt)
    return cmd


def leader_system_prompt(team_name: str, task: str = "") -> str:
    """生成 tmux leader 的初始系统提示（单一来源委托 mult_agent_mcp）。

    mult_agent_mcp._leader_system_prompt 已接线 prompts/leader.ts
    leaderInitialContext（@channel initial）权威源，渲染失败回退内建 Python
    内联文本。此处不再保留独立 Python 副本，消除 TUI/MCP/tmux_utils 三份
    平行动定义的漂移（audit §7 缺口3）。
    """
    from mult_agent_mcp import _leader_system_prompt as _mcp_leader_system_prompt
    return _mcp_leader_system_prompt(team_name, task)


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
    allowed_tools: list[str] | None = None,
) -> tuple[int, str, str]:
    """启动成员 tmux 窗口，统一处理 workspace 与 agent 类型差异。

    对于 claude 成员，自动写入 .claude/settings.json 预配置权限以减少审批阻塞。
    ``allowed_tools`` 仅 claude agent 生效（--allowedTools）；默认补上成员
    Bash/Edit + member MCP 放行，显式传入时（如 leader 复活）以调用方为准。
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

    # 成员级 effort 覆盖：三态解析（显式级别 / 继承 Agent 用户默认 / 关闭）
    resolved_effort = resolve_member_effort(team_name, member_name, atype)

    if is_codex(agent):
        # Codex 无 system-prompt 通道：身份固化到唯一自动装载持久指令文件
        # AGENTS.md（团队中立段，抗 compact/resume，防多角色串线 B2）。
        prompt_registry.ensure_codex_agents_md(team_name, team_dir)
        cmd.extend(agent_user_prefix + proxy_prefix + codex_command(agent, team_dir, member_mode=mode, model=resolved_model, effort=resolved_effort))
    else:
        # Claude / 其他 agent: 预配置权限 + 从共享工作目录启动
        if team_name_for_permissions:
            # F2：共享 settings 用团队 union 有效模式（任一 claude 成员映射原生 plan
            # → plan），不按单成员 mode 写——消除混合团队随 spawn 顺序 last-writer-wins
            # 与按成员串权；每 Agent 精确豁免仍由下方 --allowedTools argv 承载。
            team_data = load_data().get("teams", {}).get(team_name_for_permissions, {}) or {}
            _write_claude_permissions_internal(
                team_name_for_permissions,
                str(Path(team_dir)),
                dangerously_skip=dangerously_skip_permissions,
                mode=classifier_fallback.team_classifier_effective_mode(
                    team_data.get("members") or {}
                ),
            )

        # 私有 settings 目录权限收紧失败时 fail closed，返回可见错误而非继续
        try:
            au_prefix, claude_settings_path = claude_agent_user_launch(team_name, member_name)
        except RuntimeError as e:
            return -1, "", str(e)

        # --allowedTools：模式限定 fallback（plan/auto 追加精选安全窄规则，
        # 其他模式原样 → 不外溢；窄规则绕过分类器，outage 下安全命令不硬阻断）。
        resolved_tools = allowed_tools if allowed_tools is not None else [
            *CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS, *CLAUDE_BASH_EDIT_ALLOW_PATTERNS
        ]
        # 身份进 system 层（fact-check §8）：--append-system-prompt-file 单点接线。
        # leader/成员按 member_name==团队 leader 判定渲染，角色不得混淆。
        team_leader = (load_data().get("teams", {}).get(team_name, {}) or {}).get("leader") if team_name else ""
        identity_path = prompt_registry.claude_identity_file(
            team_name, member_name, leader=(member_name == team_leader)
        )
        agent_args = claude_agent_args(
            agent,
            mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
            allowed_tools=classifier_fallback.claude_terminal_allow_tools(
                mode, team_dir, resolved_tools
            ),
            model=resolved_model,
            settings_path=claude_settings_path,
            effort=resolved_effort,
            append_system_prompt_file=identity_path,
        )
        cmd.extend(["-c", team_dir] + merge_env_prefixes(au_prefix, proxy_prefix) + agent_args)

    return tmux_run(cmd)


# ---- 内部权限写入辅助 ----

def _write_claude_permissions_internal(
    team_name: str,
    team_dir_str: str,
    *,
    dangerously_skip: bool = False,
    allow_patterns: list[str] | None = None,
    additional_dirs: list[str] | None = None,
    mode: str = "",
) -> str:
    """为团队的 Claude Code 成员预配置权限策略（内部函数，写入 .claude/settings.json）。

    ``mode``：成员模式（auto/plan/manual）。仅 plan/auto 追加分类器 fallback
    精选安全 allow（``classifier_fallback``）；其余模式追加空 → 与既有完全一致
    （fallback 不外溢）。
    """
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
        # 共享 settings.json 被从该工作目录启动的 所有 Claude 进程（leader+成员）加载，
        # 因此不得含 member_* / leader_* 角色 MCP 规则——否则 leader 会串权拿到 member_*。
        # 成员 MCP 权限仅通过 CLI --allowedTools 注入（tmux_spawn_member 的 claude 分支）。
        allow.extend([
            f"Edit({team_dir_str}/*)",
            *CLAUDE_BASH_EDIT_ALLOW_PATTERNS,
        ])
        if additional_dirs:
            for d in additional_dirs:
                allow.append(f"Edit({d}/*)")
        # 分类器 fallback：仅 plan/auto 追加精选安全 allow（成员模式直接入参，
        # 不转 native——auto 转 acceptEdits 会被模式门误判非目标）。危险命令不放行。
        allow.extend(classifier_fallback.classifier_fallback_allow_patterns(team_dir_str, mode))
        # 去重（保序）：与 cfg/MCP/TUI writer 统一 —— F1 后基座已含精选安全 Bash，
        # plan fallback 追加内容与基座重叠；去重保证三个 writer 输出确定、一致
        # （此前本 writer 缺去重 → plan 模式下 32 条（16 条重复）而 cfg/MCP 16 条，
        # 跨 writer 不一致）。重复规则在 Claude 端无额外效果，仅污染审计与文件。
        permissions_config["allow"] = list(dict.fromkeys(allow))

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


# ---- 成员级 effort 覆盖 ----

# 成员可单独管理 effort，且与 Agent 用户默认 effort 清晰区分（三态语义）：
#   - 显式级别（Claude: low/medium/high/xhigh/max；Codex: minimal/low/medium/high/xhigh）
#     → 覆盖 Agent 用户默认；
#   - "inherit"（默认 / 缺失）→ 继承 Agent 用户默认 effort（profile.effort）；
#   - "off" → 显式关闭：即使 Agent 用户有默认 effort 也不注入。
#
# effort 等级按 provider 分离并校验（避免让 Codex 接受 max、或让 Claude 接受
# minimal）：
#   - Claude Code 原生 --effort <low|medium|high|xhigh|max>（本机 --help 已确认）；
#   - Codex CLI 通过 -c model_reasoning_effort="<minimal|low|medium|high|xhigh>"
#     覆盖 config.toml（无独立 effort flag，本机 Codex 已接受该配置）。
CLAUDE_EFFORT_LEVELS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")
CODEX_EFFORT_LEVELS: tuple[str, ...] = ("minimal", "low", "medium", "high", "xhigh")
# 兼容别名：历史上 EFFORT_LEVELS 即 Claude 集合；未指定 provider 时默认走 Claude。
EFFORT_LEVELS: tuple[str, ...] = CLAUDE_EFFORT_LEVELS
EFFORT_INHERIT = "inherit"
EFFORT_OFF = "off"
# 可与 effort 互转的别名（用于 UI 文案/兼容旧值）；归一化后按 provider 集合过滤。
_EFFORT_ALIASES: dict[str, str] = {
    "极低": "minimal",
    "低": "low", "中": "medium", "高": "high", "极高": "xhigh", "最高": "max",
    "inherit": EFFORT_INHERIT, "继承": EFFORT_INHERIT, "默认": EFFORT_INHERIT,
    "off": EFFORT_OFF, "关闭": EFFORT_OFF, "关": EFFORT_OFF, "none": EFFORT_OFF,
}


def effort_levels_for(agent_type: str = "") -> tuple[str, ...]:
    """返回某 agent 类型的 effort 级别集合（未识别/自定义 agent 用 Claude 集合）。"""
    atype = (agent_type or "").strip().lower()
    if atype == "codex":
        return CODEX_EFFORT_LEVELS
    return CLAUDE_EFFORT_LEVELS


def normalize_effort(value: object, agent_type: str = "") -> str:
    """归一化 effort 输入；按 provider 级别集合校验。

    返回小写级别（当前 provider 允许）/ "inherit" / "off"；无效返回 ""。
    无效值按未设置处理（与 resolve_member_effort 的继承路径兼容）。
    """
    if value is None:
        return ""
    v = str(value).strip().lower()
    if v in (EFFORT_INHERIT, EFFORT_OFF):
        return v
    levels = effort_levels_for(agent_type)
    if v in levels:
        return v
    alias = _EFFORT_ALIASES.get(v, "")
    # 别名也可能映射到三态关键字（如 "none"→"off"、"默认"→"inherit"），
    # 它们不受 provider 级别集合过滤（否则会被 levels 校验吞掉返回 ""）。
    if alias in (EFFORT_INHERIT, EFFORT_OFF):
        return alias
    return alias if alias in levels else ""


def resolve_member_effort(
    team_name: str,
    member_name: str = "",
    agent_kind: str = "",
) -> str:
    """解析成员最终 effort 字符串（空串 = 不注入）。

    成员级 effort 覆盖的三态语义：
      1. 成员显式级别（按成员 agent 的 provider 集合校验）→ 用之（覆盖默认）；
      2. 成员 "off" → 显式关闭：即使 Agent 用户 profile 有默认 effort 也不注入；
      3. 成员缺失 / "" / "inherit" → 继承 Agent 用户默认 effort（profile.effort）；
         成员未显式指定 agent_user 时回退到 team.default_agent_user
         （与 resolve_agent_model 的默认回退语义一致）。

    注意：effort 是推理级别（非凭据），继承**不**施加 takeover_enabled 门控——
    只要 profile 类型与成员 agent 匹配即继承（与 model 不同：model 受接管门控）。
    profile 类型不匹配 / legacy profile（无 agent_type）/ profile 不存在 → 不继承。
    effort 等级按成员 agent 的 provider 校验（Claude 与 Codex 等级集不同，
    见 CLAUDE_EFFORT_LEVELS / CODEX_EFFORT_LEVELS）。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})

    members = team.get("members", {})
    member_info = members.get(member_name, {}) if member_name else {}
    agent = (member_info.get("agent") or team.get("default_agent") or "claude").strip()
    # 参数名 agent_kind 而非 agent_type，避免遮蔽同名函数 agent_type(agent)
    atype = (agent_kind or agent_type(agent)).strip().lower()
    levels = effort_levels_for(atype)

    member_effort = normalize_effort(member_info.get("effort"), atype)

    # 显式级别 → 覆盖 Agent 用户默认（不依赖 agent_users：即使团队无 profile
    # 也能给成员单独设置 effort）
    if member_effort in levels:
        return member_effort
    # 显式关闭 → 不注入（即使 Agent 用户有默认）
    if member_effort == EFFORT_OFF:
        return ""

    # 继承路径（成员缺失 / "" / "inherit"）—— 此时才需要 agent_users
    agent_users = _effective_agent_user_registry(data, team)
    if not agent_users:
        return ""

    # 继承路径（成员缺失 / "" / "inherit"）
    user_key = member_info.get("agent_user", "")
    if user_key == AGENT_USER_NONE:
        return ""  # 显式不接管：跳过 default_agent_user 回退
    if not user_key:
        user_key = team.get("default_agent_user", "")
        if not user_key:
            return ""
    user_config = agent_users.get(user_key, {})
    profile_agent_type = (user_config.get("agent_type") or "").strip().lower()
    if not profile_agent_type:
        return ""  # legacy profile 无类型，无法确认 effort 归属
    if atype != profile_agent_type:
        return ""  # 类型不匹配：effort 只对匹配类型的 agent 生效
    profile_effort = normalize_effort(user_config.get("effort"), atype)
    return profile_effort if profile_effort in levels else ""


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
        ["env", "ANTHROPIC_AUTH_TOKEN=sk-ant-xxx", "ANTHROPIC_BASE_URL=https://api.anthropic.com",
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
    clear_claude_parent_credentials = False
    if profile_agent_type == "claude":
        # API_KEY / BASE_URL 在 full_takeover（显式开启或回退默认）时注入
        if full_takeover:
            api_key = (user_config.get("anthropic_api_key") or "").strip()
            if api_key and _validate_env_value(api_key):
                # Claude CLI rejects simultaneous non-empty AUTH_TOKEN/API_KEY.
                # Use the Bearer channel consistently with the private settings path.
                env_vars.append(f"ANTHROPIC_AUTH_TOKEN={api_key}")
                clear_claude_parent_credentials = True
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
        prefix = ["env"]
        if clear_claude_parent_credentials:
            prefix.extend(["-u", "ANTHROPIC_API_KEY", "-u", "ANTHROPIC_AUTH_TOKEN"])
        return prefix + env_vars
    return []


# ---- 每终端 Claude --settings 覆盖（高于 user/project settings） ----

# 影响 Claude provider 选择的 ANTHROPIC_* 变量全集。生成 --settings 文件时，
# 仅显式处理这些变量：当前 profile 提供的字段用真实值，其余置 ""
# （空串 → Claude 视为未设置，覆盖用户级 ~/.claude/settings.json 中遗留的
# AUTH_TOKEN / DEFAULT_* 模型等）。**不得**清理 OPENAI_*/CODEX_* —— 那是
# Claude 子进程（Bash/MCP 工具）可能用到的另一 provider 环境，无理由清除会
# 污染其子进程。
#
# ⚠️ ANTHROPIC_SMALL_FAST_MODEL 是例外，**不能**跟着 DEFAULT_* 一起置空：
# DEFAULT_OPUS/SONNET/HAIKU 只是别名映射，置空后回落到 ANTHROPIC_MODEL 即正确；
# 而 SMALL_FAST 是权限分类器（auto mode 判定工具是否安全）等辅助调用的独立模型槽。
# 置空后它回落到主模型 + 接管后的第三方 ANTHROPIC_BASE_URL，于是每次工具权限判定
# 都打到第三方端点——provider 一抖，整个终端所有工具报
# "<model> is temporarily unavailable, so auto mode cannot determine the safety of X"，
# 只剩只读操作可用（2026-08-10 全员锁死事故）。见 _SMALL_FAST_MODEL_ENV。
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

# 分类器/辅助调用使用的小模型槽。接管第三方 base_url 时必须给它一个该 provider
# 上**确实存在**的模型名，否则 auto mode 无法判定工具安全性。
_SMALL_FAST_MODEL_ENV = "ANTHROPIC_SMALL_FAST_MODEL"



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


def _set_claude_credential(env: dict, key: str) -> None:
    """把 profile 凭据写入 Claude 唯一的 Bearer 认证通道。

    Claude CLI 在 ``ANTHROPIC_AUTH_TOKEN`` 与 ``ANTHROPIC_API_KEY`` 同时非空时
    会直接发出认证冲突警告，并且不同版本对优先级处理不一致。Agent User 的
    输入只有一个 API Key 字段，统一按 Bearer token 使用，兼容现有第三方中转站。
    ``claude_agent_user_launch`` 会在进程启动前清理父环境中的两个旧变量，避免
    监管用户的凭据参与认证。
    """
    env["ANTHROPIC_AUTH_TOKEN"] = key
    # 明确以空值覆盖用户级/项目级 settings 中可能残留的 API_KEY。
    # Claude CLI 将空字符串视为未设置，因此不会触发双通道警告；但如果完全
    # 删除该字段，下层 settings 的旧 API_KEY 可能在合并时重新生效。
    env["ANTHROPIC_API_KEY"] = ""


def _claude_takeover_env(team_name: str, member_name: str = "") -> tuple[dict, str]:
    """解析某成员生效的 Claude 接管 env 块与 profile key。

    这是 --settings 覆盖（build_agent_user_claude_settings）与私有
    CLAUDE_CONFIG_DIR（build_agent_user_claude_config_dir）**共用**的单一
    判定入口——两条注入通道必须给出完全一致的 env，否则会出现"base_url 走
    A 通道、凭据走 B 通道"的撕裂状态。

    未接管（系统默认 / __none__ / takeover 关闭 / 类型不匹配 / 非 claude
    typed profile）时返回 ({}, "")，由调用方回落系统默认。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    agent_users = _effective_agent_user_registry(data, team)
    if not agent_users:
        return {}, ""

    member_info = team.get("members", {}).get(member_name, {}) if member_name else {}
    user_key = member_info.get("agent_user", "")

    if user_key == AGENT_USER_NONE:
        return {}, ""  # 显式不接管 → 系统默认

    is_default_fallback = False
    if not user_key:
        user_key = team.get("default_agent_user", "")
        if not user_key:
            return {}, ""  # 系统默认
        is_default_fallback = True

    user_config = agent_users.get(user_key, {})
    takeover_enabled = bool(user_config.get("takeover_enabled"))
    profile_agent_type = (user_config.get("agent_type") or "").strip().lower()

    agent = (member_info.get("agent") or team.get("default_agent") or "claude").strip()

    # 显式选择 + takeover 关闭 → 系统默认（不覆盖）
    if not is_default_fallback and not takeover_enabled:
        return {}, ""

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
            return {}, ""
        if agent_type(agent) != "claude":
            return {}, ""  # 类型不匹配 → 系统默认
        if is_default_fallback or takeover_enabled:
            if api_key and validate_agent_user_env_value(api_key, "ANTHROPIC_API_KEY") == "":
                _set_claude_credential(env, api_key)
            if base_url and validate_agent_user_url(base_url) == "":
                env["ANTHROPIC_BASE_URL"] = base_url
        if model and validate_agent_user_env_value(model, "ANTHROPIC_MODEL") == "":
            env["ANTHROPIC_MODEL"] = model
        _apply_small_fast_model(env, user_config, model)
    else:
        # legacy profile（无 agent_type）：仅注入 ANTHROPIC_BASE_URL（受 takeover 门控），
        # 同样走 --settings 私有文件，避免 base_url 进入命令行。
        if profile_agent_type:
            return {}, ""  # 非 claude 类型（codex 等）由进程级 env 前缀处理
        if not takeover_enabled:
            return {}, ""
        if agent_type(agent) != "claude":
            return {}, ""
        if base_url and validate_agent_user_url(base_url) == "":
            env["ANTHROPIC_BASE_URL"] = base_url
        # legacy 同样把 BASE_URL 指向第三方，SMALL_FAST 置空会让分类器打到那里；
        # profile 无 model 字段可用时退回 anthropic_model（legacy 通常也有）。
        _apply_small_fast_model(env, user_config, model)

    if _SMALL_FAST_MODEL_ENV in env and not env[_SMALL_FAST_MODEL_ENV]:
        # 没有任何可用小模型候选 → 保持"不设置"，让系统默认生效。
        # 绝不下发空串：那会把分类器顶到主模型 + 第三方 base_url（本次事故根因）。
        env.pop(_SMALL_FAST_MODEL_ENV, None)

    if "ANTHROPIC_AUTH_TOKEN" in env and not env["ANTHROPIC_AUTH_TOKEN"]:
        # profile 没提供可用凭据 → 绝不能把 AUTH_TOKEN/API_KEY 置空下发。
        # 本机可能根本没有 OAuth 登录态（~/.claude/.credentials.json 不存在），
        # 用户级 settings 的 AUTH_TOKEN 就是唯一凭据；清空它 = 终端直接
        # "Not logged in, Please run /login"。此时保持这两个变量"不设置"，
        # 让系统默认凭据继续生效，只接管 base_url/model。
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        env.pop("ANTHROPIC_API_KEY", None)

    return env, user_key


def _apply_small_fast_model(env: dict, user_config: dict, model: str) -> None:
    """为第三方便携 base_url 分配可用的分类器小模型。

    优先级（都要求通过环境值校验，非法值按未提供处理）:
      1. profile 显式 `anthropic_small_fast_model`（可选新字段）
      2. profile 的 `anthropic_model`（同一 provider 上必然存在的模型）
    两者都不可得时保留系统默认（不置空、不注入），让 Claude 按主模型回退。
    """
    explicit = (user_config.get("anthropic_small_fast_model") or "").strip()
    for candidate in (explicit, model):
        if candidate and validate_agent_user_env_value(candidate, "ANTHROPIC_SMALL_FAST_MODEL") == "":
            env[_SMALL_FAST_MODEL_ENV] = candidate
            return


def build_agent_user_claude_settings(team_name: str, member_name: str = "") -> str:
    """为 claude 成员构建"每终端独立"的私有 --settings 覆盖文件。

    根因：用户级 ~/.claude/settings.json 的 env 块会覆盖普通进程 env，且遗留
    ANTHROPIC_AUTH_TOKEN 优先于 ANTHROPIC_API_KEY，只有 --model(CLI 参数) 不受
    影响——因此出现"仅 model 生效、ANTHROPIC_BASE_URL/key 未接管"。本函数生成
    一个优先级高于 user/project settings 的 --settings 文件，其 env 块显式设置
    profile 的凭据 / ANTHROPIC_BASE_URL / ANTHROPIC_MODEL，并把
    ANTHROPIC_DEFAULT_* 等置空，实现多 base_url / 多 key 并发隔离。

    返回 --settings 文件路径；当成员未接管（系统默认 / __none__ / takeover 关闭
    / 类型不匹配 / 非 claude typed profile）时返回 ""，让用户级系统默认生效。
    绝不写入团队共享 .claude/settings.json；文件 0600 原子写入私有位置。
    """
    env, user_key = _claude_takeover_env(team_name, member_name)
    if not env:
        return ""

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


# ---- 方案B：每成员私有 CLAUDE_CONFIG_DIR（从根上绕过 cc-switch） ----

# 私有 config dir 中**不**链接回真实 ~/.claude 的条目。settings.json 是
# cc-switch 改写的那一份（正是要绕开的目标）；settings.local.json 优先级更高，
# 同样必须由我们独占，否则接管仍可能被压。
_CLAUDE_HOME_UNLINKED: frozenset[str] = frozenset({"settings.json", "settings.local.json"})


def _agent_user_config_dir_path(team_name: str, member_name: str, profile_key: str) -> Path:
    """私有 CLAUDE_CONFIG_DIR 路径（与 --settings 文件同样按 team+member+profile 隔离）。"""
    base = get_data_file().parent
    parts = (
        _sanitize_settings_component(team_name),
        _sanitize_settings_component(member_name),
        _sanitize_settings_component(profile_key),
    )
    return base / ".agent_user_home" / "__".join(parts)


def _link_claude_home_assets(config_dir: Path) -> None:
    """把真实 ~/.claude 的非 settings 资产 symlink 进私有 config dir。

    CLAUDE_CONFIG_DIR 换掉的是**整个**配置根，不只是 settings.json。若不链接：
      - ~/.claude.json 缺失 → 每个成员终端都会重跑 onboarding / 信任对话框，
        且 customApiKeyResponses（已批准的 API key）丢失；
      - .credentials.json 缺失 → OAuth 登录态丢失（本机当前无此文件，
        但用户一旦 /login 过就必须保留，否则又回到 "Not logged in"）；
      - plugins / skills / projects / sessions 等个性化资产全部丢失。

    因此除 settings.json / settings.local.json 外全部软链回真实目录，
    只让 provider 选择由我们独占。已存在的条目不覆盖（Claude 可能用
    "写临时文件 + rename" 把某个 symlink 替换成真实文件，此时保留其自有状态）。
    """
    real_home = Path.home() / ".claude"
    if real_home.is_dir():
        for item in real_home.iterdir():
            if item.name in _CLAUDE_HOME_UNLINKED:
                continue
            link = config_dir / item.name
            if link.exists() or link.is_symlink():
                continue
            try:
                link.symlink_to(item)
            except OSError:
                pass  # 单个资产链接失败不影响接管本身

    # ~/.claude.json 位于 HOME 根而非 ~/.claude 下，但同样受 CLAUDE_CONFIG_DIR
    # 重定向；onboarding 状态与已批准 key 都在里面，必须一并链接。
    real_global = Path.home() / ".claude.json"
    link_global = config_dir / ".claude.json"
    if real_global.exists() and not (link_global.exists() or link_global.is_symlink()):
        try:
            link_global.symlink_to(real_global)
        except OSError:
            pass


def build_agent_user_claude_config_dir(team_name: str, member_name: str = "") -> str:
    """方案B：为接管的 claude 成员生成私有 CLAUDE_CONFIG_DIR，返回目录路径。

    与 --settings 的区别是**层级不同**，不是"覆盖"而是"不读"：
      - --settings 仍会读 cc-switch 的 ~/.claude/settings.json，只是优先级压过它；
        属于同一层的竞争，cc-switch 随时改写仍会影响新启动的终端。
      - CLAUDE_CONFIG_DIR 在读取 settings **之前**就决定了配置根目录，
        cc-switch 的 ~/.claude/settings.json 根本不在读取路径上。

    两者叠加使用（本函数 + build_agent_user_claude_settings）：即使某一条通道
    在未来的 Claude 版本上语义变化，另一条仍能保住接管，避免静默回落默认配置。

    未接管时返回 ""。目录 0700、settings.json 0600；路径含 shell 元字符时
    返回 "" 而不下发（不构造出可能被 shell 拆开的命令）。
    """
    env, user_key = _claude_takeover_env(team_name, member_name)
    if not env:
        return ""

    config_dir = _agent_user_config_dir_path(team_name, member_name, user_key)
    try:
        config_dir.mkdir(parents=True, exist_ok=True)
        config_dir.chmod(0o700)
        atomic_json_write(config_dir / "settings.json", {"env": env})
    except OSError as e:
        raise RuntimeError(
            f"无法创建私密 Claude 配置目录 {config_dir}（fail closed，拒绝写入凭据）: {e}"
        ) from e

    _link_claude_home_assets(config_dir)

    # 该路径要作为 `env CLAUDE_CONFIG_DIR=<path>` 进入 tmux 命令行；含空格或
    # shell 元字符会被拆成多个参数，宁可不注入也不要构造出错误命令。
    if not _validate_env_value(str(config_dir)):
        return ""
    return str(config_dir)


def merge_env_prefixes(*prefixes: list[str]) -> list[str]:
    """合并多个 `["env", "K=V", ...]` 前缀为单个 env 调用。

    直接拼接会得到 `env A=1 env B=2 cmd`（能跑但嵌套 env 进程），合并后是
    `env A=1 B=2 cmd`，命令更短也更容易在 `ps` / tmux 里读懂。
    """
    kvs: list[str] = []
    for prefix in prefixes:
        if not prefix:
            continue
        kvs.extend(prefix[1:] if prefix[0] == "env" else prefix)
    return ["env"] + kvs if kvs else []


def claude_agent_user_launch(team_name: str, member_name: str = "") -> tuple[list[str], str]:
    """claude 成员的接管启动参数：返回 (env 命令前缀, --settings 路径)。

    单一入口，供 4 处 spawn 点复用——此前每处都各自拼装，任何一处漏掉
    settings_path 就会静默退回 cc-switch 的默认配置且不报错。

    注意 env 前缀里只有配置目录和 ``env -u`` 清理指令，凭据一律走 settings
    文件，绝不进入命令行，否则 ``ps`` / tmux 会话里就能看到 key。只有 profile
    确实提供了凭据时才清理父环境；无 key 的 profile 仍保留系统默认登录态。
    """
    takeover_env, _ = _claude_takeover_env(team_name, member_name)
    settings_path = build_agent_user_claude_settings(team_name, member_name)
    config_dir = build_agent_user_claude_config_dir(team_name, member_name)
    prefix: list[str] = []
    if config_dir:
        prefix = ["env"]
        # settings.json 负责注入 profile 的 AUTH_TOKEN；先移除父进程中监管用户
        # 可能留下的两个变量，避免 API_KEY 与 AUTH_TOKEN 同时存在，或旧 token
        # 覆盖 profile token。无 profile key 时不执行清理，保留系统默认认证。
        if takeover_env.get("ANTHROPIC_AUTH_TOKEN"):
            prefix.extend(["-u", "ANTHROPIC_API_KEY", "-u", "ANTHROPIC_AUTH_TOKEN"])
        prefix.append(f"CLAUDE_CONFIG_DIR={config_dir}")
    return prefix, settings_path


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


# ============================================================
# Agent 用户池 — 有序切换池（plan-b §3.2，顺序由 TUI 点选决定）
# ============================================================

QUOTA_FAILOVER_DEFAULT_CONFIG = {
    "enabled": False,
    "confirm_cycles": 2,
    "wrap": True,
    "max_switches": 6,
}


def _profile_resolved_atype(profile: dict) -> str:
    """解析 profile 自身的 provider 类型（不含成员侧 CLI 类型）。

    Returns: 'claude' | 'codex' | ''（无法确定）
      - typed（有 agent_type）：声明值直接决定；声明非 claude/codex（如
        "other"）→ 无法确定 —— 自定义 provider 无法校验，拒绝进团队池。
      - legacy（无 agent_type）：按该 provider 的三组字段
        （base_url / api_key / model）兜底推断。旧判定只看 base_url：
        （a）只填了 api_key/model 的 profile 会被两边同时拒掉；
        （b）同时填了 claude+codex 两组字段的 profile 会两边同时匹配，
        codex 成员照样能选 claude 号 —— 正是用户报的防呆漏洞。
        补强后：任一组字段非空即视为该 provider；**两边都像（混填两组字段）
        或都不像（空壳）→ 无法确定** —— 宁可拒绝进池让用户补 agent_type，
        也不放一个换过去可能空转的号。
    """
    if not isinstance(profile, dict):
        return ""
    declared = (profile.get("agent_type") or "").strip().lower()
    if declared:
        return declared if declared in ("claude", "codex") else ""
    claude_like = any(
        isinstance(profile.get(k), str) and bool(profile.get(k).strip())
        for k in ("anthropic_base_url", "anthropic_api_key", "anthropic_model")
    )
    codex_like = any(
        isinstance(profile.get(k), str) and bool(profile.get(k).strip())
        for k in ("openai_base_url", "openai_api_key", "codex_model")
    )
    if claude_like == codex_like:  # 都像（混填）/ 都不像（空壳）→ 无法确定
        return ""
    return "claude" if claude_like else "codex"


def _profile_matches_atype(profile: dict, atype: str) -> bool:
    """profile 能否真正为 atype 类型的 CLI 注入凭证（跨 provider 换号防呆）。

    门槛与实际注入逻辑逐条对齐，杜绝"写入成功但注入为空"的静默空转：
      - typed profile（有 agent_type）：类型必须相等 —— 同
        _agent_user_env_prefix_for_team:1322 / resolve_agent_model:1062 /
        resolve_member_effort:1187 三处的判定；不等时那三处一律返回空，
        换过去等于什么都没换、立刻再撞配额。
      - legacy profile（无 agent_type）：由 _profile_resolved_atype 按
        base_url / api_key / model 三组字段兜底推断（同 :1308-1320 回退
        注入分支）；两边都像 / 都不像 → 无法确定，两类 CLI 都不过。
      - atype 为空（无法确定 CLI 类型）→ 不过滤，保持既有行为。
      - atype 为 "other"（自定义 agent 命令）：命令串不含 claude/codex
        关键字，provider 无法确定 → **不过滤**（池可见可选，修复"自定义
        成员池一个都不能选"的静默全 False）。自动换号安全阀在
        select_failover_candidate 的 "other" 分支拒绝 —— 自定义 agent 的
        注入侧（_agent_user_env_prefix_for_team）对 "other" 一律返回空，
        机器自动换号必然静默空转，必须由人确认。
    """
    if not atype:
        return True
    if not isinstance(profile, dict):
        return False
    if atype == "other":
        return True
    declared = (profile.get("agent_type") or "").strip().lower()
    if declared:
        return declared == atype
    return _profile_resolved_atype(profile) == atype


def member_pool_is_activated(member: dict) -> bool:
    """成员池是否已激活 = 原始 agent_user_pool 存在且为非空 list。

    判定用【原始】字段而非净化结果：一旦操作者手工选过成员池，团队池就
    完全不参与，即使这些 key 后来被删/被类型过滤清空也不回落团队池
    （用户裁定：哪怕成员池配额全部用完也不切回 team 池）——回落会让
    "我只配了这几个号"的预期失效，且排障时无法判断实际走了哪个池。
    """
    if not isinstance(member, dict):
        return False
    pool = member.get("agent_user_pool")
    return isinstance(pool, list) and len(pool) > 0


def _effective_agent_user_pool(
    data: dict, team: dict, raw_pool: object = None, atype: str = ""
) -> list[str]:
    """净化后的有效 agent 用户池（保序）。

    净化规则（读路径容错，不抛错）：
      - 非 list / 缺失 → []
      - 丢弃不在 _effective_agent_user_registry 中的 key
        （全局 registry + 团队旧数据合并，复用既有合并语义）
      - 丢弃 AGENT_USER_NONE 哨兵（:891，绝不能进池）
      - atype 非空时丢弃 provider 不匹配的 profile（_profile_matches_atype）
      - 保序去重（重复 key 只保留首次出现位置）

    raw_pool 为 None 时取 team["agent_user_pool"]（团队池）；调用方传入成员池
    原始列表即可复用同一套净化。
    """
    if not isinstance(team, dict):
        return []
    pool = team.get("agent_user_pool") if raw_pool is None else raw_pool
    if not isinstance(pool, list):
        return []
    registry = _effective_agent_user_registry(data, team)
    seen: set[str] = set()
    cleaned: list[str] = []
    for key in pool:
        if not isinstance(key, str) or not key:
            continue
        if key == AGENT_USER_NONE:
            continue
        if key not in registry:
            continue
        if key in seen:
            continue
        if not _profile_matches_atype(registry.get(key) or {}, atype):
            continue
        seen.add(key)
        cleaned.append(key)
    return cleaned


def resolve_pool_atype(team: dict, member: dict) -> str:
    """池过滤应使用的 provider 类型（决定换号候选是否真能生效）。

    三级链，与 resolve_agent_model:1060 的既有约定完全一致：
        member["agent"] → team["default_agent"] → "claude"

    ⚠️ 必须由 member["agent"] 优先，不能无条件用团队默认：跑哪个 CLI 由
    member["agent"] 决定，agent_user 只注入 env、从不改变 CLI。若让团队默认
    覆盖成员自身 agent，"codex 成员 + 团队默认 claude" 会被筛出 claude 池，
    换过去三处注入全部返回空 → 静默退回本机配置（刚耗尽的那套）→ 立刻再撞
    配额，直到 max_switches 烧完。成员未配 agent（空）时才落团队默认。
    """
    if not isinstance(member, dict):
        member = {}
    if not isinstance(team, dict):
        team = {}
    agent = (member.get("agent") or team.get("default_agent") or "claude").strip()
    return agent_type(agent)


def get_agent_user_pool(team: dict, member: dict | None = None, atype: str = "") -> list[str]:
    """读取净化后的 agent 用户池（点选顺序 = 切换顺序）。

    池归属（用户裁定：成员池激活后团队池完全不参与）：
      - member 传入且 member_pool_is_activated(member) → 只用成员池，
        耗尽也不回落团队池；
      - 否则用团队池。

    atype 非空时按 provider 过滤（跨 provider 的 profile 换过去必然空转）。
    返回 [keyA, ...]；非 list / 缺失时返回 []。仅读操作，不写盘。
    """
    data = load_data()
    if member is not None and member_pool_is_activated(member):
        return _effective_agent_user_pool(
            data, team, raw_pool=member.get("agent_user_pool"), atype=atype
        )
    return _effective_agent_user_pool(data, team, atype=atype)


def set_agent_user_pool(team_name: str, keys: list[str]) -> tuple[bool, str]:
    """写入团队 agent 用户池（MCP 侧，持锁）。

    - 校验每个 key 均存在于 _effective_agent_user_registry（全局 + 团队旧数据
      合并）且非 AGENT_USER_NONE；保序去重后整体写入。
    - 校验失败返回 (False, 原因)，绝不部分写入。
    - 空列表 = 清空池，同时移除 cursor（agent_user_pool_cursor）。
    - 非空新池将 cursor 归零（重建池后从 0 开始切换）。
    - **团队池内部 provider 一致性校验**（数据层兜底，与 TUI 防呆锁同语义，
      MCP 工具直接写也绕不过）：池内所有 key 的 resolved type
      （_profile_resolved_atype）必须一致 —— 团队池是给多个 provider 的成员
      共用的，池内混号会让 codex 成员换到 claude 号（或反之），注入为空、
      静默空转。无法确定 provider 的 profile（无 agent_type 且 legacy 字段
      不唯一/缺失）一并拒绝，让操作者补 agent_type。
    - **不强制**匹配 team.default_agent（成员可各自覆盖 agent，异类成员由
      select_failover_candidate 的 atype 过滤自然挡掉）—— 但池类型与团队
      默认不一致时在返回消息里提示，让操作者知情。
    """
    if not isinstance(keys, list):
        return False, "keys 必须是列表"
    with TEAM_DATA_LOCK:
        data = load_data_locked(TEAM_DATA_LOCK)
        team = data.get("teams", {}).get(team_name)
        if not isinstance(team, dict):
            return False, f"团队不存在: {team_name}"
        registry = _effective_agent_user_registry(data, team)
        deduped: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if not isinstance(key, str):
                return False, f"池成员必须是字符串: {key!r}"
            if not key:
                return False, "池成员不能为空字符串"
            if key == AGENT_USER_NONE:
                return False, f"AGENT_USER_NONE 哨兵不能进入池: {key}"
            if key not in registry:
                return False, f"profile 不在生效 registry 中: {key}"
            if key in seen:
                continue  # 保序去重
            seen.add(key)
            deduped.append(key)
        if not deduped:
            team.pop("agent_user_pool", None)
            team.pop("agent_user_pool_cursor", None)
            save_data_locked(data, TEAM_DATA_LOCK)
            return True, "已清空 agent 用户池"
        # ── 团队池内部 provider 一致性（数据层兜底，绕不过 TUI 防呆锁）──
        pool_type = ""
        for key in deduped:
            rt = _profile_resolved_atype(registry.get(key) or {})
            if not rt:
                return False, (
                    f"profile '{key}' 无法确定 provider（无 agent_type 且 legacy "
                    f"字段不唯一/缺失）—— 请补 agent_type 字段后再写入团队池"
                )
            if pool_type and rt != pool_type:
                return False, (
                    f"团队池必须内部同 provider: '{key}' 为 {rt} 类型,"
                    f"池内其他 profile 为 {pool_type} 类型 —— 混号会让异类成员"
                    f"换到跨 provider 的号上、注入为空静默空转"
                )
            pool_type = rt
        team["agent_user_pool"] = deduped
        team["agent_user_pool_cursor"] = 0
        save_data_locked(data, TEAM_DATA_LOCK)
        msg = f"已写入 {len(deduped)} 个 agent 用户（{pool_type} 类型,按点选顺序）"
        # 团队默认 agent 提示（不强制匹配：成员可各自覆盖 agent）
        default_raw = (team.get("default_agent") or "").strip()
        default_eff = default_raw or "claude"
        if agent_type(default_eff) != pool_type:
            shown = f"'{default_raw}'" if default_raw else "未设置（启动默认 claude）"
            msg += (
                f"; 提示:池为 {pool_type} 类型,团队默认 agent {shown}"
                f"（{agent_type(default_eff)}）—— 默认 agent 的成员将无法使用此池"
            )
        return True, msg


def set_member_agent_user_pool(
    team_name: str, member_name: str, keys: list[str]
) -> tuple[bool, str]:
    """写入成员级 agent 用户池（优先于团队池，激活后团队池完全不参与）。

    校验与 set_agent_user_pool 一致（registry 存在性 + 非哨兵 + 保序去重，
    失败不部分写入），额外多一道 provider 强校验：池内 key 必须与该成员的
    CLI 类型（resolve_pool_atype）匹配。这是数据层强校验，任何调用方（含
    MCP 工具直接写）都绕不过 —— 只靠 TUI 置灰挡不住。

    空列表 = 取消激活，删除 agent_user_pool 字段后回落团队池。
    """
    if not isinstance(keys, list):
        return False, "keys 必须是列表"
    with TEAM_DATA_LOCK:
        data = load_data_locked(TEAM_DATA_LOCK)
        team = data.get("teams", {}).get(team_name)
        if not isinstance(team, dict):
            return False, f"团队不存在: {team_name}"
        member = (team.get("members") or {}).get(member_name)
        if not isinstance(member, dict):
            return False, f"成员不存在: {member_name}"
        registry = _effective_agent_user_registry(data, team)
        atype = resolve_pool_atype(team, member)
        deduped: list[str] = []
        seen: set[str] = set()
        for key in keys:
            if not isinstance(key, str) or not key:
                return False, f"池成员必须是非空字符串: {key!r}"
            if key == AGENT_USER_NONE:
                return False, f"AGENT_USER_NONE 哨兵不能进入池: {key}"
            if key not in registry:
                return False, f"profile 不在生效 registry 中: {key}"
            if not _profile_matches_atype(registry.get(key) or {}, atype):
                return False, (
                    f"profile '{key}' 与成员 '{member_name}' 的 CLI 类型 "
                    f"'{atype}' 不匹配 —— 换过去无法注入凭证（静默空转）"
                )
            if key in seen:
                continue
            seen.add(key)
            deduped.append(key)
        if not deduped:
            member.pop("agent_user_pool", None)
            member.pop("agent_user_pool_cursor", None)
            save_data_locked(data, TEAM_DATA_LOCK)
            return True, f"已取消 {member_name} 的成员池（回落团队池）"
        member["agent_user_pool"] = deduped
        member["agent_user_pool_cursor"] = 0
        save_data_locked(data, TEAM_DATA_LOCK)
        return True, (
            f"已为 {member_name} 写入 {len(deduped)} 个 agent 用户"
            f"（{atype} 类型，按点选顺序；团队池不再参与）"
        )


def next_agent_user_in_pool(
    team: dict, current: str | None, member: dict | None = None, atype: str = ""
) -> str | None:
    """返回池中 current 的后继（quota failover 切换目标）。

    - current 不在池中（含 None/空串）→ 返回池首。
    - 到池尾：wrap=True（quota_failover_config 配置）→ 回池首；wrap=False → None。
    - 空池 → None。
    - 池长 1：无论 wrap 一律返回 None —— 无处可换，避免原地空转
      （单元素池切换自身无意义，调用方应停止切换）。

    member/atype 见 get_agent_user_pool：成员池激活则独占，atype 过滤跨
    provider 候选。需要区分「池本身为空」与「被类型过滤清空」的调用方请用
    select_failover_candidate（本函数只回 None，无法分辨失败原因）。
    """
    pool = get_agent_user_pool(team, member=member, atype=atype)
    if not pool or len(pool) == 1:
        return None
    if current in pool:
        idx = pool.index(current)
        if idx + 1 < len(pool):
            return pool[idx + 1]
        if quota_failover_config(team)["wrap"]:
            return pool[0]
        return None
    return pool[0]


def select_failover_candidate(team: dict, member: dict) -> tuple[str | None, str]:
    """选出换号目标并说明失败原因（quota failover 单一决策入口）。

    Returns:
        (key, reason)。key 非 None 时 reason 为 ""；key 为 None 时 reason ∈
          - "pool-empty"          池（成员池或团队池）本身为空
          - "pool-single"         池里只有 1 个号：无处可换（切换到自身等于原地
                                  空转）。与 pool-empty 分开报，因为运维处置不同
                                  —— 报"池空"会让人去查是不是漏配了池，实际是
                                  配了但只配了一个，要补第二个号才有意义
          - "pool-type-mismatch"  池非空，但按成员 CLI 类型过滤后无可用候选
                                  —— 必须保持阻塞并告警，绝不静默降级：
                                  换过去三处注入全返回空，等于原地空转
          - "pool-other-agent"    成员为自定义 agent（atype="other"，无法
                                  确定 provider）：池虽可见可选（_profile
                                  _matches_atype 不过滤），但自动换号的注入
                                  侧对 "other" 一律返回空 —— 机器换号必然
                                  静默空转，拒绝自动换号、由人确认
          - "pool-exhausted"      wrap=False 且已到池尾

    ⚠️ 类型不匹配单独成因，是因为它与"池空"的运维处置完全不同：池空要加号，
    类型不匹配是配错了（如 codex 成员配了纯 claude 池），要改配置；自定义
    agent 是既无法确认、自动换号也必然空注入，要人确认。
    """
    atype = resolve_pool_atype(team, member)
    raw = get_agent_user_pool(team, member=member)          # 不过滤类型
    if atype == "other":
        # 自定义 agent 安全阀：_profile_matches_atype 对 "other" 不过滤
        # （池可见可选），但三处注入对 "other" 全部返回空 —— 机器自动换号
        # 等于什么都没换、立刻再撞配额。拒绝并明确提示，绝不静默空转。
        if raw:
            return None, "pool-other-agent"
        return None, "pool-empty"
    typed = get_agent_user_pool(team, member=member, atype=atype)  # 过滤后
    if raw and not typed:
        return None, "pool-type-mismatch"
    if not typed:
        return None, "pool-empty"
    if len(typed) == 1:
        return None, "pool-single"
    # typed 非空且 >1：next_agent_user_in_pool 必然返回非 None —— 当 current 在
    # typed 中时返回后继，不在时返回池首（永不为 None，池长已 >1）。唯一"无处
    # 可换"是池首就是 current 自身（current 不在池中却等于池首不可能），
    # 即 wrap=False 且 current 恰为池尾元素。直接判池首，比依赖
    # next_agent_user_in_pool 的 None 更可靠（它只在池空/长1 时回 None）。
    current = member.get("agent_user") or ""
    nxt = next_agent_user_in_pool(team, current, member=member, atype=atype)
    if nxt is None or nxt == current:
        return None, "pool-exhausted"
    return nxt, ""


def quota_failover_config(team: dict) -> dict:
    """读取团队 quota failover 配置（defaults 合并 + 类型强制 + 钳制）。

    模板：mult_agent_mcp._leader_wakeup_config（defaults dict + isinstance 合并 +
    逐字段强制；confirm_cycles 钳制 1..10、max_switches 钳制 1..50）。
    """
    cfg = dict(QUOTA_FAILOVER_DEFAULT_CONFIG)
    stored = team.get("quota_failover")
    if isinstance(stored, dict):
        cfg.update(stored)
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["confirm_cycles"] = max(1, min(int(cfg.get("confirm_cycles", 2)), 10))
    cfg["wrap"] = bool(cfg.get("wrap", True))
    cfg["max_switches"] = max(1, min(int(cfg.get("max_switches", 6)), 50))
    return cfg


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
