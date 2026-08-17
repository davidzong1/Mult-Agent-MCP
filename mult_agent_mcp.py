import functools
import inspect
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
    undelivered_pending_reports,
    mark_pending_reports_delivered,
    build_leader_pending_reports_section,
    report_origin_prefix,
    claim_keeps_tmux_leader,
    MONITOR_INFERRED_EVENT,
    LEADER_CHECKPOINT_VERSION,
    MAX_CHECKPOINT_EVIDENCE,
    empty_leader_checkpoint,
    leader_checkpoint,
    checkpoint_epoch,
    leader_checkpoint_drift,
    leader_checkpoint_high_drift,
    build_leader_checkpoint_section,
    build_leader_checkpoint_drift_section,
)
from common.tmux_utils import (
    get_agent_user_env_prefix,
    get_agent_user_pool,
    get_proxy_env_prefix,
    member_proxy_enabled,
    member_proxy_mode,
    member_spawn_lock,
    member_window_state as common_member_window_state,
    migrate_agent_users_global_file,
    next_agent_user_in_pool,
    select_failover_candidate,
    resolve_pool_atype,
    member_pool_is_activated,
    set_member_agent_user_pool,
    quota_failover_config,
    resolve_agent_model,
    resolve_member_effort,
    normalize_effort,
    CLAUDE_EFFORT_LEVELS,
    CODEX_EFFORT_LEVELS,
    build_agent_user_claude_settings,
    build_agent_user_claude_config_dir,
    claude_agent_user_launch,
    merge_env_prefixes,
    drop_base_window,
    CLAUDE_BASH_EDIT_ALLOW_PATTERNS,
)
from common.atomic_write import atomic_json_write
from common.data_layer import get_data_file, DATA_FILE as _DATA_LAYER_DATA_FILE
from common import checkpoint
from common import session_resume
from common import classifier_fallback
from common import prompt_registry
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

# F3 层2（2026-08-12）：分类器 unavailable 签名**注入排除护栏**。
# 本仓无原生 auto 发射点（auto→acceptEdits，永不传 --permission-mode auto），
# 成员终端出现 "temporarily unavailable ... safety" 签名几乎只来自**注入引用**
# （leader 广播/回报/任务转述成员的报错文本，可能无引号逐字引用——refactor 复核
# 实证：leader 天然会无引号转述成员报错 → 引号排除护栏（层1）漏网 → 误判
# classifier_unavailable → 成员永不 idle → 任务悬挂）。为消除该误判，记录"最近
# 注入过含签名 payload"的成员，注入后 N 秒内 monitor 分类跳过 classifier_unavailable
# 分支（busy/idle/dead/quota 语义不变）。窗口到期自动恢复全量检测。
# 安全性：护栏期漏检真实签名概率≈0（本仓从不产生原生 auto 签名；若某成员真实
# 进入分类器故障，签名是持续存在的工具 result，会在窗口过后被检测到）。
SIG_INJECTION_SUPPRESS_SECONDS = 240
# 键 = (team_name, member_name) 复合键：跨团队同名成员零污染（2026-08-12 最终门
# 3a）。裸 member_name 键下，团队A alice 注入会污染团队B alice 的抑制判定 →
# 团队B 真实分类器签名被误抑（unknown）。复合键保证抑制精确到 (团队, 成员)。
_SIG_INJECTION_SUPPRESS_UNTIL: dict[tuple[str, str], float] = {}
_SIG_INJECTION_SUPPRESS_LOCK = threading.Lock()


def _sig_injection_mark_suppressed(team_name: str, member_name: str) -> None:
    """记录 (team, member) 最近被注入过含分类器签名文本，置抑制到期时间（单调时钟）。"""
    with _SIG_INJECTION_SUPPRESS_LOCK:
        _SIG_INJECTION_SUPPRESS_UNTIL[(team_name, member_name)] = (
            time.monotonic() + SIG_INJECTION_SUPPRESS_SECONDS
        )


def _sig_injection_suppressed(team_name: str, member_name: str) -> bool:
    """成员当前是否处于注入抑制窗口（now < suppress_until）。

    键为 (team_name, member_name) 复合键——注入者与观测者必须同一团队才会命中，
    跨团队同名成员互相零污染。
    """
    with _SIG_INJECTION_SUPPRESS_LOCK:
        until = _SIG_INJECTION_SUPPRESS_UNTIL.get((team_name, member_name))
        if until is None:
            return False
        if time.monotonic() < until:
            return True
        _SIG_INJECTION_SUPPRESS_UNTIL.pop((team_name, member_name), None)
        return False


def _resolve_member_from_window(session: str, window: str) -> tuple[str, str] | None:
    """把注入目标窗口解析回 (team_name, member_name)（用于 F3 层2 抑制标记）。

    ``window`` 可能是裸成员名、ACTIVE generation 窗口名（``{name}__g{N}``）或
    ``session:window`` 目标串。遍历当前数据中所有团队的成员，取 ``_member_window_target``
    与 ``window`` 匹配（支持前缀/后缀 generation 与 session 前缀）。找不到返回 None。

    **3a（2026-08-12 最终门）**：返回 ``(team_name, member_name)`` 复合键，而非
    裸成员名——抑制记录精确到团队，跨团队同名成员零污染。
    """
    if not window:
        return None
    try:
        data = _load()
    except Exception:
        return None
    norm = str(window).strip()
    for team_name, team in (data.get("teams") or {}).items():
        for name in (team.get("members") or {}):
            try:
                tgt = _member_window_target(team_name, name) or name
            except Exception:
                tgt = name
            tgt_bare = str(tgt).split(":")[-1].strip()
            if norm == tgt_bare or norm.endswith(":" + tgt_bare) or norm.startswith(tgt_bare):
                return (team_name, name)
    return None

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
# 模块自身 DATA_FILE 的导入时初值。_data_file_path() 用它判断 DATA_FILE 是否被
# 外部（如测试）显式修改：被修改时模块自己的路径设置最具体，优先于 data_layer 覆盖。
_DEFAULT_DATA_FILE = DATA_FILE
TEAM_WORKSPACES_DIR = os.path.join(PROJECT_DIR, ".team_workspaces")
SHARE_CONTEXT_DIR = os.path.join(MCP_HOME, "contexts")
SHARE_WORKSPACE_DIR = os.path.join(PROJECT_DIR, "share_work_space")
CLAUDE_GLOBAL_CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".claude.json")
CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__leader_*",
    "mcp__mult_agent_mcp__leader_*",
]
# leader / 普通成员 的完整 --allowedTools 由 common.mcp_config 统一维护
# （MCP 前缀严格隔离 + 共享 Bash/Edit），本模块直接复用，避免双份漂移。
from common.mcp_config import (  # noqa: E402
    CLAUDE_LEADER_TOOL_ALLOW_PATTERNS,
    CLAUDE_MEMBER_TOOL_ALLOW_PATTERNS,
)

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


def _data_file_path() -> Path:
    """解析当前生效的数据文件路径，优先级（最具体者先）：

    1. 模块自身 DATA_FILE 被显式修改（!= 导入时初值）—— 模块自己的路径设置
       最具体，直接生效（兼容只改 mcp.DATA_FILE 的旧测试写法，如
       test_file_permissions 三条路径分别验证 0600）；
    2. data_layer.set_data_file() 覆盖 —— 一条覆盖隔离全仓（测试环境级隔离）；
    3. 模块级 DATA_FILE（生产默认，与 data_layer 同值，行为不变）。
    """
    if DATA_FILE != _DEFAULT_DATA_FILE:
        return Path(DATA_FILE)
    path = get_data_file()
    if path != _DATA_LAYER_DATA_FILE:
        return path  # data_layer 覆盖生效（测试）
    return Path(DATA_FILE)


def _load() -> dict:
    with TEAM_DATA_LOCK:
        path = _data_file_path()
        if not path.exists():
            return {"teams": {}}
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)


def _save(data: dict) -> None:
    with TEAM_DATA_LOCK:
        atomic_json_write(_data_file_path(), data)


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


# ============================================================
# leader_checkpoint 写入原语（team 级结构化进度快照）
# ============================================================

def _leader_checkpoint_upsert(
    team: dict,
    patch: dict,
    *,
    source: str,
    updated_by: str = "leader",
) -> dict:
    """将 patch 合并进 team['leader_checkpoint'] 并单调递增 epoch。

    调用方必须已持有 TEAM_DATA_LOCK（所有调用点都在 _update_team_data 的
    updater 内，或锁内的 load→mutate→save 序列里）。epoch 即版本计数器：
    每次写入 +1，旧写入可通过 expected_epoch 被 _update_leader_checkpoint 拒绝。
    """
    import datetime

    cp = team.get("leader_checkpoint")
    if not isinstance(cp, dict):
        cp = empty_leader_checkpoint()
    else:
        cp = dict(cp)
    new_epoch = checkpoint_epoch(cp) + 1
    cp["epoch"] = new_epoch
    cp["version"] = LEADER_CHECKPOINT_VERSION
    for k, v in patch.items():
        cp[k] = v
    cp["updated_ts"] = datetime.datetime.now().isoformat()
    cp["source"] = source
    cp["updated_by"] = updated_by
    team["leader_checkpoint"] = cp
    return cp


def _update_leader_checkpoint(
    team_name: str,
    patch: dict,
    *,
    source: str,
    updated_by: str = "leader",
    expected_epoch: int | None = None,
) -> dict:
    """原子写入 leader_checkpoint；拒绝旧 epoch（乐观并发防护）。

    走 _update_team_data（锁内 read-modify-write + atomic_json_write）。
    expected_epoch 非 None 时：若持久化 epoch 与之不同则拒绝写入，返回当前
    epoch 供调用方基于最新快照重试或中止。
    返回 {"rejected": bool, "epoch": int, ...}。
    """
    def updater(team: dict) -> dict:
        cp = team.get("leader_checkpoint")
        cur_epoch = checkpoint_epoch(cp) if isinstance(cp, dict) else 0
        if expected_epoch is not None and cur_epoch != int(expected_epoch):
            return {
                "rejected": True,
                "epoch": cur_epoch,
                "expected_epoch": int(expected_epoch),
            }
        updated = _leader_checkpoint_upsert(team, patch, source=source, updated_by=updated_by)
        return {"rejected": False, "epoch": updated["epoch"]}

    result = _update_team_data(team_name, updater)
    if result is None:
        return {"rejected": True, "error": f"团队 '{team_name}' 不存在"}
    return result


def _checkpoint_split_lines(value: str) -> list[str]:
    """把换行分隔的多值字段拆成去空行列表（leader_checkpoint_set 用）。"""
    return [ln.strip() for ln in (value or "").splitlines() if ln.strip()]


def _record_leader_checkpoint_assignment(
    team: dict,
    member_name: str,
    task: str,
    status: str = "assigned",
) -> None:
    """把一次成员分配写入 checkpoint.assignments（无 checkpoint 时为 no-op）。"""
    if not isinstance(team.get("leader_checkpoint"), dict):
        return
    assignments = dict(team.get("leader_checkpoint", {}).get("assignments") or {})
    assignments[member_name] = {
        "task": (task or "").strip(),
        "status": status,
    }
    _leader_checkpoint_upsert(team, {"assignments": assignments}, source="assign")


def _checkpoint_gate_block(team: dict, team_name: str) -> str:
    """硬门：HIGH 漂移未确认时返回拒绝原因（非空即阻止分配/广播）。

    这是"drift 不得仅 prompt 提示"的硬性闸门——prompt 渲染只是可见性，
    真正的防线在这里：leader_assign_subtask / leader_assign_task_to_relevant /
    leader_broadcast 都会在进入前调用，HIGH 漂移且当前 checkpoint 未被
    leader_ack_checkpoint 确认时直接拒绝执行。

    ack 语义：leader_checkpoint_ack.epoch 必须等于当前 checkpoint.epoch 才算
    "已确认当前方向"；任何新写入（报告/分配等）都会 bump epoch，HIGH 漂移
    持续存在时需再次确认（防止旧确认覆盖新状态）。
    """
    high = leader_checkpoint_high_drift(team)
    if not high:
        return ""
    cp = leader_checkpoint(team)
    ack = team.get("leader_checkpoint_ack")
    if isinstance(ack, dict) and ack.get("epoch") == cp.get("epoch"):
        return ""  # 当前 checkpoint 已被确认
    detail = "；".join(high)
    return (
        f"⛔ leader_checkpoint 高优先级漂移未确认，已拒绝执行：{detail}\n"
        f"  请先调用 leader_ack_checkpoint('{team_name}') 确认当前方向后再重试。"
    )


# ============================================================
# member_outbox：批量 ACK/广播的有界、自动推进、可观测消息队列
# ============================================================
# P0 task1：leader 向全部成员批量发 ACK 时，leader_broadcast 进门被
# _checkpoint_gate_block 整批拒绝（HIGH drift 未确认即零成员联系），调用方只能
# 逐个 member_send_message。这里提供一条"入队即成功、锁外自动投递"的批量通道：
#   - 有界：MEMBER_OUTBOX_MAX 满则显式拒绝，绝不静默丢消息；
#   - message_id 幂等：同 id 重复入队跳过，重试不双发；
#   - per-target FIFO：同一成员只有队首可 sending，跨成员窗口并行（线程安全，
#     _send_keys 多行 buffer 名含 {pid}_{tid}）；
#   - 受 gate 约束：held_reason="checkpoint_gate" 的消息仅在硬门放行后投递
#     （drift 保护保留，见验收#5）；
#   - 重试上限后显式 failed（可观测），无静默丢失。

MEMBER_OUTBOX_MAX = 100          # 每团队活跃(queued/sending)队列上限（有界）
OUTBOX_HISTORY_MAX = 20          # 终态(delivered/failed)历史保留条数（可观测又不占容量）
OUTBOX_RETRY_MAX = 3             # 单条消息投递最大重试次数
OUTBOX_SEND_WORKERS = 4          # 跨成员窗口并行投递线程数
OUTBOX_SENDING_STALE_SECONDS = 60  # sending 状态超时即视为崩溃残留，重置后重试


def _outbox_entries(team: dict) -> list[dict]:
    q = team.get("member_outbox")
    if not isinstance(q, list):
        return []
    return [e for e in q if isinstance(e, dict)]


def _prune_outbox_history(q: list[dict]) -> list[dict]:
    """裁剪最旧终态(delivered/failed)条目到有界历史（F1）。

    终态条目不占活跃容量但保留可观测历史；持续广播不会因历史堆积饿死活跃团队。
    调用方须在锁内（或 _update_team_data 的 updater 内）。"""
    terminal_positions = [
        i for i, e in enumerate(q) if e.get("state") in ("delivered", "failed")
    ]
    excess = len(terminal_positions) - OUTBOX_HISTORY_MAX
    if excess <= 0:
        return q
    remove = set(terminal_positions[:excess])
    return [e for i, e in enumerate(q) if i not in remove]


def _next_outbox_message_id(kind: str) -> str:
    import datetime
    import uuid

    return f"{kind}_{datetime.datetime.now().strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _enqueue_outbox_messages(
    team_name: str,
    targets: list[str],
    payload: str,
    kind: str,
    *,
    held_reason: str = "",
    message_ids: dict | None = None,
) -> dict:
    """锁内为每个 target 入队一条 outbox 消息（有界 + 幂等）。

    有界：队列满则对该 target 显式拒绝（"queue-full"），绝不静默丢弃。
    幂等：message_ids 提供的 id（或既有队列中已存在同 id）重复入队时跳过。
    调用方负责过滤 leader 成员；targets 中不存在的成员不会入队。
    返回 {"enqueued", "rejected", "message_ids"}；团队不存在返回空结果。
    """
    import datetime

    message_ids = message_ids or {}

    def updater(latest_team: dict) -> dict:
        q = list(_outbox_entries(latest_team))
        existing = {e.get("message_id") for e in q}
        # F1 容量：终态(delivered/failed)条目不占活跃容量，但保留有界历史
        # （可观测），入队时裁剪最旧终态，避免持续广播 100 次后饿死活跃团队。
        q = _prune_outbox_history(q)
        # 活跃容量只计 queued/sending（在途未决）；终态不计。
        active = sum(1 for e in q if e.get("state") in ("queued", "sending"))
        out: dict = {"enqueued": [], "rejected": [], "message_ids": {}}
        for name in targets:
            mid = message_ids.get(name) or _next_outbox_message_id(kind)
            if mid in existing or any(mid == x for x in out["message_ids"].values()):
                out["rejected"].append(f"{name}:dup({mid})")
                continue
            if active >= MEMBER_OUTBOX_MAX:
                out["rejected"].append(f"{name}:queue-full")
                continue
            entry = {
                "message_id": mid,
                "target_member": name,
                "payload": payload,
                "kind": kind,
                "state": "queued",
                "held_reason": held_reason,
                "retries": 0,
                "last_error": "",
                "created_ts": datetime.datetime.now().isoformat(),
                "delivered_ts": "",
            }
            q.append(entry)
            active += 1
            out["enqueued"].append(name)
            out["message_ids"][name] = mid
        latest_team["member_outbox"] = q
        return out

    return _update_team_data(team_name, updater) or {
        "enqueued": [], "rejected": [], "message_ids": {},
    }


def _deliver_outbox_entry(team_name: str, entry: dict) -> tuple[bool, str]:
    """锁外投递单条 outbox 消息（与 _send_message_to_members 同口径）。

    _send_keys / _recover_and_send 可能耗时数秒，必须在锁外执行。
    返回 (ok, error)；任何异常降级为 (False, reason)，不抛。
    """
    name = entry.get("target_member") or ""
    payload = entry.get("payload") or ""
    try:
        data = _load()
        team = data.get("teams", {}).get(team_name, {})
        members = team.get("members", {})
        member = members.get(name, {})
        session = _find_any_session(team_name)
        if not session:
            return False, "no-session"
        if _is_leader_member(team, name):
            return False, "leader-skip"
        full_msg = _mode_task_prefix(member) + payload
        member_target = _member_window_target(team_name, name)
        if not member_target:
            ok, err_msg = _recover_and_send(
                team_name, name, session, extra_message=full_msg
            )
            return (True, "") if ok else (False, err_msg)
        rc, err = _send_keys(session, member_target, full_msg)
        return (True, "") if rc == 0 else (False, err)
    except Exception as e:  # 单条失败绝不让整个推进崩
        return False, f"exception: {e}"


def _outbox_gate_blocked(team: dict, team_name: str) -> bool:
    """outbox 是否有 gate-held 消息且硬门未放行（仅对 held 消息的投递判据）。"""
    if not any(
        e.get("held_reason") == "checkpoint_gate" for e in _outbox_entries(team)
    ):
        return False
    return bool(_checkpoint_gate_block(team, team_name))


def _advance_member_outbox_once(team_name: str) -> dict:
    """自动推进 member_outbox：把 gate 已放行的 queued 消息投递给成员。

    关键约束（refactor-claude 评审）：
      - 锁外发送、锁内记账 —— _send_keys 可能耗时数秒，绝不持 TEAM_DATA_LOCK；
      - per-target FIFO —— 同一成员只有队首可 sending，跨成员窗口并行；
      - gate-held 消息仅在硬门放行后投递（drift 保护保留）；
      - 重试上限后显式 failed（无静默丢消息）；任何异常降级为不推进。
    挂载于 _monitor_team_wakeup_once（巡检路径，同 _retry_deferred_report_injection）。
    返回 {"delivered", "retrying", "failed", "held"}。
    """
    import concurrent.futures
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return {"delivered": [], "retrying": [], "failed": [], "held": []}
    q = _outbox_entries(team)
    if not q:
        return {"delivered": [], "retrying": [], "failed": [], "held": []}
    if not _find_any_session(team_name):
        return {"delivered": [], "retrying": [], "failed": [], "held": []}

    gate_blocked = _outbox_gate_blocked(team, team_name)

    # 崩溃恢复：sending 状态若停留在 sending_started_ts 超过阈值（进程在发送
    # 中途崩溃 / 发送线程被 kill），重置回 queued 允许重试，绝不永久卡死。
    now = datetime.datetime.now()

    def stale_sending_reset(latest_team: dict) -> dict:
        fresh_q = _outbox_entries(latest_team)
        reset = []
        for e in fresh_q:
            if e.get("state") != "sending":
                continue
            started = e.get("sending_started_ts") or ""
            try:
                stale = (
                    not started
                    or (now - datetime.datetime.fromisoformat(started)).total_seconds()
                    > OUTBOX_SENDING_STALE_SECONDS
                )
            except (ValueError, TypeError):
                stale = True
            if stale:
                e["state"] = "queued"
                e["last_error"] = "crashed-during-send (reset to retry)"
                e.pop("sending_started_ts", None)
                reset.append(e.get("target_member") or e.get("message_id"))
        return {"reset": reset}

    _update_team_data(team_name, stale_sending_reset)

    # stale 重置后重读最新队列，保证 F2 的 in-flight 判定基于 fresh 状态。
    team = _load().get("teams", {}).get(team_name, {})
    q = _outbox_entries(team)

    # 每个 target 只取头部 queued 且（无 held 或 gate 已放行）的消息。
    # F2 并发防护：若该 target 已有 in-flight 非终态(sending) 条目，整个 target
    # 跳过 —— 否则并发推进（巡检 15s + leader batch/flush/ack 可同时触发）会把
    # 同成员下一条 queued 选为队首并发发送，破坏 per-target FIFO 顺序。
    heads: dict[str, dict] = {}
    for e in q:
        if e.get("state") != "queued":
            continue
        if e.get("held_reason") == "checkpoint_gate" and gate_blocked:
            continue
        t = e.get("target_member") or ""
        if not t or t in heads:
            continue
        if any(
            x.get("target_member") == t and x.get("state") == "sending"
            for x in q
        ):
            continue
        heads[t] = e
    if not heads:
        held = [
            e.get("target_member") or ""
            for e in q
            if e.get("held_reason") == "checkpoint_gate"
            and e.get("state") != "delivered"
        ]
        return {"delivered": [], "retrying": [], "failed": [], "held": held}

    # ---- 锁内记账：选中的队首置 sending（防并发推进双发同一 entry）----
    def mark_sending(latest_team: dict) -> dict:
        fresh_q = _outbox_entries(latest_team)
        by_id = {e.get("message_id"): e for e in fresh_q}
        marked = []
        for e in heads.values():
            fresh = by_id.get(e.get("message_id"))
            if fresh and fresh.get("state") == "queued":
                fresh["state"] = "sending"
                fresh["sending_started_ts"] = datetime.datetime.now().isoformat()
                fresh["last_error"] = ""
                marked.append(fresh)
        return {"marked": marked}

    mark_result = _update_team_data(team_name, mark_sending) or {"marked": []}
    to_send = mark_result.get("marked") or []
    if not to_send:
        held = [
            e.get("target_member") or ""
            for e in q
            if e.get("held_reason") == "checkpoint_gate"
            and e.get("state") != "delivered"
        ]
        return {"delivered": [], "retrying": [], "failed": [], "held": held}

    # ---- 锁外并行发送（不同成员窗口），同窗口天然单条（per-target FIFO）----
    results: dict[str, tuple[bool, str]] = {}
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=OUTBOX_SEND_WORKERS
    ) as ex:
        fut_map = {ex.submit(_deliver_outbox_entry, team_name, e): e for e in to_send}
        for fut in concurrent.futures.as_completed(fut_map):
            try:
                ok, err = fut.result()
            except Exception as e:  # 单条异常不让整个推进崩
                ok, err = False, f"exception: {e}"
            results[fut_map[fut].get("message_id")] = (ok, err)

    # ---- 锁内记账：delivered / 重试 / 显式 failed ----
    def record(latest_team: dict) -> dict:
        fresh_q = _outbox_entries(latest_team)
        by_id = {e.get("message_id"): e for e in fresh_q}
        out: dict = {"delivered": [], "retrying": [], "failed": []}
        for mid, (ok, err) in results.items():
            fresh = by_id.get(mid)
            if not fresh:
                continue
            if ok:
                fresh["state"] = "delivered"
                fresh["delivered_ts"] = datetime.datetime.now().isoformat()
                fresh["last_error"] = ""
                out["delivered"].append(fresh.get("target_member") or mid)
            else:
                fresh["retries"] = (fresh.get("retries") or 0) + 1
                if fresh["retries"] >= OUTBOX_RETRY_MAX:
                    fresh["state"] = "failed"
                    fresh["last_error"] = err
                    out["failed"].append(fresh.get("target_member") or mid)
                else:
                    fresh["state"] = "queued"  # 允许下次巡检重试
                    fresh["last_error"] = err
                    out["retrying"].append(fresh.get("target_member") or mid)
        # F1：每次投递记账后裁剪终态历史到有界（终态不占活跃容量）。
        latest_team["member_outbox"] = _prune_outbox_history(fresh_q)
        return out

    rec = _update_team_data(team_name, record) or {
        "delivered": [], "retrying": [], "failed": [],
    }
    rec["held"] = [
        e.get("target_member") or ""
        for e in _outbox_entries(_load().get("teams", {}).get(team_name, {}))
        if e.get("held_reason") == "checkpoint_gate"
        and e.get("state") != "delivered"
    ]
    return rec


def _build_outbox_status(team: dict) -> list[str]:
    """渲染 outbox 队列状态（可观测性：逐成员 queued/sending/delivered/failed）。"""
    q = _outbox_entries(team)
    if not q:
        return ["📭 member_outbox 队列为空。"]
    by_member: dict[str, dict] = {}
    for e in q:
        t = e.get("target_member") or "?"
        s = by_member.setdefault(t, {"queued": 0, "sending": 0, "delivered": 0, "failed": 0})
        state = e.get("state")
        if state in s:
            s[state] += 1
    lines = [f"📬 member_outbox 队列（共 {len(q)} 条，上限 {MEMBER_OUTBOX_MAX}）:"]
    for t, s in sorted(by_member.items()):
        held = any(
            e.get("held_reason") == "checkpoint_gate"
            for e in q if e.get("target_member") == t
        )
        held_txt = " | held(gate)" if held else ""
        lines.append(
            f"  {t}: queued={s['queued']} sending={s['sending']} "
            f"delivered={s['delivered']} failed={s['failed']}{held_txt}"
        )
    newest = q[-1] if q else {}
    oldest = q[0] if q else {}
    lines.append(f"  最早: {oldest.get('created_ts', '')[:19]} {oldest.get('state', '')}")
    lines.append(f"  最新: {newest.get('created_ts', '')[:19]} {newest.get('state', '')}")
    return lines


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

    精确匹配以 list-sessions 的真实输出为准：tmux 的 target 解析（has-session -t）
    会对 session 名做前缀匹配，`mcp_team` 会误命中 `mcp_team_215956`，从而把带时间戳
    session 记录成无时间戳名。因此只有 list-sessions 中确实存在精确名时才视为 exact。
    """
    session = _session(team)
    candidates: list[str] = []
    rc, out, _ = _tmux(["list-sessions", "-F", "#{session_name}"])
    if rc == 0:
        names = out.split("\n")
        if session in names:
            candidates.append(session)
        prefix = f"mcp_{team}_"
        for name in names:
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


def _member_generation(member: dict) -> int:
    """成员当前 ACTIVE 终端 generation（未迁移 = 1，首窗为裸名 {member}）。

    磁盘上 terminal_generation 可能被写坏/浮点化，安全归一化（同 checkpoint_epoch）。
    """
    try:
        return int(member.get("terminal_generation", 1) or 1)
    except (TypeError, ValueError):
        return 1


def _active_generation_window_name(member_name: str, member: dict) -> str | None:
    """成员 ACTIVE 窗口名：已发生 generation 迁移后为 {member}__g{gen}。

    legacy 成员（未迁移，首窗为裸名 {member}）返回 None → 走既有解析。
    terminal_generation 由 _quota_generation_migrate 成功提升后 >=2；1 表示从未迁移。
    """
    gen = _member_generation(member)
    if gen >= 2:
        return f"{member_name}__g{gen}"
    return None


def _member_window_target(team_name: str, member_name: str) -> str | None:
    session = _find_any_session(team_name)
    if not session:
        return None
    records = _tmux_window_records(session)
    if not records:
        return member_name

    member = _team_info(team_name).get("members", {}).get(member_name, {})
    # P2：已发生 generation 迁移 → 只路由 ACTIVE 窗口（{member}__g{N}），
    # 绝不回退到 DRAINING 旧窗或裸名；ACTIVE 窗口缺失视为 dead（交由恢复
    # 重建 ACTIVE，而不是把指令打进非权威旧窗）。
    active_name = _active_generation_window_name(member_name, member)
    if active_name:
        by_name = next((r for r in records if r["name"] == active_name), None)
        if by_name:
            _remember_member_window_id(team_name, member_name, session, active_name)
            return by_name["id"]
        return None
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
    # P2：leader 也可能发生 generation 迁移（tmux leader 换号），ACTIVE 窗口
    # 名为 {leader}__g{N}，优先解析，避免唤醒/注入打进 DRAINING 旧窗。
    leader_member = _team_info(team_name).get("members", {}).get(leader_name, {})
    active_name = _active_generation_window_name(leader_name, leader_member)
    if active_name:
        by_name = next((r for r in records if r["name"] == active_name), None)
        if by_name:
            return by_name["id"]
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
    # F3 层2：注入前若 payload 含分类器 unavailable 签名（leader 转述成员报错文本，
    # 可能无引号逐字引用），标记目标成员抑制 classifier_unavailable 检测 240s——
    # 避免 monitor 把"注入的引用文本"误判为成员真实进入分类器故障 → 永不 idle。
    # 安全性：本仓无原生 auto 发射点，抑制窗口内漏检真实签名概率≈0（见模块注释）。
    if text and classifier_fallback.detect_classifier_unavailable(text):
        resolved = _resolve_member_from_window(session, window)
        if resolved:
            _sig_injection_mark_suppressed(*resolved)
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


# codex 提交确认的渲染沉降时间(秒): 让终端刷新、codex 处理排队的输入后再捕获
# 证据。这不是"掩盖竞态的盲目延时"——修复机制是下面的证据检查 + 条件补 Enter,
# 延时只保证捕获能看到输入框的最终状态(否则会在 codex 处理完输入前就抓拍)。
CODEX_CONFIRM_SETTLE_SECONDS = 0.35


def _message_residue_in_input_box(output: str, message: str) -> bool:
    """True 当 `message` 文本仍残留在 CLI 的输入框(未提交)。

    输入框区域 = 最后一个 `›`/`❯` 前缀行(输入框提示符)到末尾的整段(含长文本
    换行的续行), 以底部 footer 为界; 带编号的授权选项(`❯ 1. Yes`)不是输入框。
    消息若已提交, 其文本会回显在输入框**上方**的对话输出里——那些行在最后一个
    `›` 行之上, 不计入输入框区域, 因此不会误报残留。
    """
    if not message:
        return False
    marker = message.strip().splitlines()[0][:40].strip()
    if not marker:
        return False
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    box_idx = None
    for i in range(len(lines) - 1, -1, -1):
        s = lines[i].strip()
        if (s.startswith("›") or s.startswith("❯")) and not re.match(
            r"^[❯›]\s*\d+[\.\)]\s*\S", s
        ):
            box_idx = i
            break
    if box_idx is None:
        return False
    return any(marker in (ln or "") for ln in lines[box_idx:])


def _confirm_codex_leader_submission(
    session: str,
    window: str,
    message: str,
    *,
    settle: float = CODEX_CONFIRM_SETTLE_SECONDS,
) -> tuple[int, str]:
    """面向 codex leader 注入的"基于证据"的提交确认。

    codex 的输入循环在渲染/状态切换窗口可能吞掉 ``_send_keys`` 追加的尾随
    Enter —— 消息于是停在输入框未提交, 而调用方仍会报告 injected。Claude 路径
    用盲补 Enter(``_confirm_prompt_submission``)兜底; 对 codex 盲补 Enter 不安全
    (可能双提交, 或误提交占位提示文本), 因此: 短暂沉降后捕获 pane, 只有当消息
    **确实仍残留在输入框**时才补一次 Enter 提交; 输入框已清空/回到占位(已提交)
    则不多发。补 Enter 后仍残留 → 返回失败, 调用方应视为未注入(回报仍留在
    leader_pending_reports, leader_activate 可见, 信息不丢)。

    Returns:
        (0, "") 消息已离开输入框(已提交或本就未落入); (rc, err) 无法确认提交。
    """
    import time

    def _box_clean() -> tuple[bool, int, str]:
        if settle > 0:
            time.sleep(settle)
        rc, out, err = _capture_window(session, window, 60)
        if rc != 0:
            return False, rc, f"codex submit verify capture failed: {err}"
        return not _message_residue_in_input_box(out, message), 0, ""

    clean, rc, err = _box_clean()
    if rc != 0:
        return rc, err
    if clean:
        return 0, ""  # 输入框无残留 → 已提交(或消息未落入), 不多发 Enter
    # 首次 Enter 被吞 → 补一次 Enter 提交输入框中的消息
    rc, err = _send_keys(session, window, "", send_enter=True)
    if rc != 0:
        return rc, f"codex submit confirm Enter failed: {err}"
    clean, rc, err = _box_clean()
    if rc != 0:
        return rc, err
    if not clean:
        return -1, "codex input box still holds the message after confirm Enter"
    return 0, ""


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


def _target_is_codex_tmux_leader(team: dict, member_name: str) -> bool:
    if team.get("leader_type") != "tmux" or team.get("leader") != member_name:
        return False
    member = team.get("members", {}).get(member_name, {})
    agent = member.get("agent") or team.get("default_agent") or "claude"
    return _is_codex(agent)


def _send_context_to_member(
    session: str,
    target: str,
    text: str,
    *,
    confirm_submission: bool = False,
    confirm_codex_submission: bool = False,
) -> tuple[int, str]:
    rc, err = _send_keys(session, target, text)
    if rc != 0:
        return rc, err
    if confirm_codex_submission:
        # codex leader: 基于证据的提交确认(消息残留输入框才补 Enter), 不盲发
        return _confirm_codex_leader_submission(session, target, text)
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


# Live tool-execution detection for Claude Code / Codex TUIs.
#
# While a tool (Bash/Edit/...) is executing, both CLIs render a live status
# line carrying an elapsed counter:
#   ✢ Waddling… (42s · ↓ 5.3k tokens)   # Claude Code
#   ◦ Working (0s • esc to interrupt)   # Codex
# Claude Code draws the input-box prompt (❯) and a static footer (e.g.
# "⏵⏵ accept edits on …") BELOW that status line, so the last non-empty line
# can be a footer while a tool is still running.  A spinner/working line with
# an elapsed counter is therefore the reliable discriminator between "mid-tool"
# and "idle at prompt"; the elapsed counter alone (residual "took (5s)" in
# command output) is not enough — the line must also be a spinner/working line.
_LIVE_TOOL_ELAPSED_RE = re.compile(r"\(\s*\d+[smhd]\b")
# ⚠️ 本词表只在**同一行还带耗时计数**（_LIVE_TOOL_ELAPSED_RE）时才生效，这是
# 加入 "■"/"thinking" 的安全前提：codex 用 "■" 同时渲染活动行与系统提示
#     ■ Working (12s • esc to interrupt)     ← 活动
#     ■ '/compact' is disabled while a task is in progress.   ← 提示，无耗时
# 只有前者带 "(12s"，后者永远匹配不上，所以不会把一条静态提示钉成 busy。
# 码位注意：codex 的方块是 ■ U+25A0，与 Claude 的 ◼ U+25FC **不是同一个字符**，
# 两个都要在表里（曾经只有 U+25FC，导致 codex 活动行全漏）。
_LIVE_TOOL_MARKERS = (
    "✢", "✻", "✽", "✼", "✾", "❀", "❁", "❂", "❃",
    "◼", "◻", "◦", "◧", "◨", "◴", "◷", "◵", "◶", "▣", "◐",
    "■",
    "working", "waddling", "thinking",
)


# Claude Code 底部会常驻渲染一块**任务清单**（静态待办文本，不是活动状态指示）：
#     4 tasks (0 done, 1 in progress, 3 open)
#     ◼ 正在做的事
#     ◻ 待办的事
# 其中 ``◼`` 与旧版 Claude 的"停止"指示符同形、清单头又含 "in progress"，两者都在
# busy_markers 里 —— 于是**只要屏幕上挂着任务清单，成员就永远被判 busy**。实测
# （2026-08-15 生产团队取样）：coder-claude / refactor-claude 停在 ``❯`` 提示符、
# _tail_shows_live_tool 为假（确实没在跑），却因清单里一行 ``◼ 阅读…`` 被判 busy。
# 后果不止"状态显示不对"：busy 在 quota **之前**判定，配额耗尽因此永远不会被评估，
# 换号链路整条失效（生产事故的第三道闸门）。
# 判定 busy 时一律先剔除清单行；真正的流式仍由 _tail_shows_live_tool
# （esc to interrupt / 耗时计数 + 标记）独立兜底，不受影响。
_TASK_LIST_HEADER_RE = re.compile(r"^\s*\d+\s+tasks?\s*\(", re.IGNORECASE)
_TASK_LIST_ITEM_RE = re.compile(r"^\s*[◼◻▪▫]\s+\S")
# 清单**上下文**证据（三者任一即可认定屏幕上确实挂着任务清单）：
#   - 清单头 "4 tasks (0 done, 1 in progress, 3 open)"
#   - "未开始项" ◻ / ▫ —— 停止指示符只会用实心 ◼，绝不会用空心
#   - 底部 footer 的 "ctrl+t to hide tasks"（实测 refactor-claude 的窗口里
#     清单头与 ◻ 都已滚出取样窗口，只剩这一条证据）
_TASK_LIST_OPEN_ITEM_RE = re.compile(r"^\s*[◻▫]\s+\S")
_TASK_LIST_FOOTER_RE = re.compile(r"hide\s+tasks", re.IGNORECASE)

# codex 的系统提示行与"进行中"同形碰撞（与上面的任务清单 ◼ 同一类缺陷）：
#     ■ '/compact' is disabled while a task is in progress.
# 这行含 "in progress"、行首是 ■，两个都在 busy_markers 里 —— 成员被
# /compact 提示过一次之后就**永久判 busy**，既不会被判完成，也让 leader 的
# wakeup_all_done 永不成立。它是一条静态提示（无耗时计数），真正的流式仍由
# _tail_shows_live_tool 独立兜底，剔除它不会放过正在跑的成员。
_CODEX_NOTICE_RE = re.compile(
    r"^\s*[■◼]\s*['\"`]?/?\w[\w\- ]*['\"`]?\s+is\s+(?:disabled|not\s+available)\b",
    re.IGNORECASE,
)


def _drop_codex_notice_lines(lines: list[str]) -> list[str]:
    """剔除 codex 的静态系统提示行（``■ '<cmd>' is disabled …``）。"""
    return [ln for ln in lines if not _CODEX_NOTICE_RE.match(ln)]


def _has_task_list_block(lines: list[str]) -> bool:
    """屏幕上是否确实挂着任务清单块。

    没有上下文证据时**绝不**把 ``◼ 文本`` 当清单项 —— 否则真正的停止指示符
    （旧版 Claude 的 ``◼ 处理中``）会被误剔除，busy 判定被放宽成 idle，
    monitor 就会给正在跑的成员合成回报、标记完成（伪造成功）。
    """
    for ln in lines:
        if (_TASK_LIST_HEADER_RE.match(ln)
                or _TASK_LIST_OPEN_ITEM_RE.match(ln)
                or _TASK_LIST_FOOTER_RE.search(ln)):
            return True
    return False


def _is_task_list_line(ln: str) -> bool:
    """该行是否属于 Claude 任务清单块（清单头或清单条目）。"""
    return bool(_TASK_LIST_HEADER_RE.match(ln) or _TASK_LIST_ITEM_RE.match(ln))


def _drop_task_list_lines(lines: list[str], context: list[str] | None = None) -> list[str]:
    """剔除任务清单行（供 busy 判定使用，不影响其它判定的取样窗口）。

    context 给出判断"是否存在清单块"的完整取样（默认与 lines 相同）。
    _is_claude_ready_prompt 只把提示符**以上**的行传进来，而 footer 证据在
    提示符**以下**，故必须显式传入完整 context，否则证据看不见。
    """
    ctx = lines if context is None else context
    if not _has_task_list_block(ctx):
        return list(lines)
    return [ln for ln in lines if not _is_task_list_line(ln)]


def _tail_shows_live_tool(lines: list[str]) -> bool:
    """Return True when a recent non-empty line is a live tool-execution
    indicator.

    Claude can scroll the spinner out of the captured tail while retaining an
    ``esc to interrupt`` footer.  That footer is itself a live-state signal;
    checking it also protects member monitoring from marking an active task
    complete when no spinner row remains visible.
    """
    if any("esc to interrupt" in ln.lower() for ln in lines[-3:]):
        return True
    for ln in lines[-8:]:
        if not _LIVE_TOOL_ELAPSED_RE.search(ln):
            continue
        if any(m in ln.lower() for m in _LIVE_TOOL_MARKERS):
            return True
    return False


# =====================================================================
# 配额/余额耗尽识别（阶段1 止血）
# ---------------------------------------------------------------------
# 约束（leader 裁定，与讨论产物冲突时以此为准，见 docs/plan-b-hot-restart-resume.md
# §4 阶段1）：
#   - quota 关键词是【必要条件】；HTTP 状态码/供应商域名只是佐证，不能单独定案。
#     纯 429 rate limiting（无 quota 词）不算 quota —— 限流会自愈，换号只会抖动。
#   - 词表剔除裸 "billing"（CLI 启动横幅 "API Usage Billing" 每个新 spawn 必现）
#     与裸 "quota"，只保留组合词。
#   - 只扫最后 16 个非空行（与 idle_markers 的 tail 窗口一致）；命中行必须整行
#     是错误形态（_QUOTA_ERROR_LINE_RE）；行级否决 G3-G7（diff / grep path:line /
#     命令回显 / markdown 围栏 / pytest 上下文）与白名单（disk quota exceeded =
#     EDQUOT、402 downloading = npm/uvx 装包 —— 真错误但非账号配额）。
_QUOTA_STRONG_RE = re.compile(
    r"(?:\binsufficient\s+balance\b|\binsufficient_quota\b|"
    r"\bquota\s+exceeded\b|\bexceeded\s+your\s+current\s+quota\b|"
    r"\bpayment\s+required\b|"
    r"余额不足|额度不足|欠费|配额不足)",        # 中文：Python unicode \w 自带边界，勿加 \b
    re.IGNORECASE,
)
_QUOTA_WEAK_RE = re.compile(
    r"(?<!\d)402(?!\d)|billing\s+hard\s+limit|billing\s+details",
    re.IGNORECASE,
)
_QUOTA_WHITELIST_RE = re.compile(
    r"\b(?:disk\s+quota\s+exceeded|402\s+downloading)\b",
    re.IGNORECASE,
)
# 错误行结构：**段首**以错误形态开头（含 JSON 错误体），杜绝子串误伤。
# 锚定的是"段首"而非"整行行首"——见 _quota_error_line_form 的装饰前缀剥离与
# 分段拆分；正则本身保持不变，仍是防子串误伤的主力。
_QUOTA_ERROR_LINE_RE = re.compile(
    r"^\s*(?:\{.*\"error\"|api[\s\-]?error|error|failed|✗|❌|error\s*code)",
    re.IGNORECASE,
)
# 装饰前缀：真实 CLI 在错误文本前渲染的修饰符（转录区 ⎿、告警 ⚠、错误框边线
# 与框线字符 U+2500-U+257F、项目符号、引用符）。
# ⚠️ 绝不含 ✗ / ❌ —— 那两个是错误 token 本身（_QUOTA_ERROR_LINE_RE 的分支），
# 当装饰剥离会让 "✗ 余额不足" 直接失配，正例 P5/P7 全线崩。
_QUOTA_LINE_DECOR_RE = re.compile("^[\\s─-╿⎿⚠●○•▪▸►>›]+")
# 分段分隔符：底部状态区/错误框把多段信息拼进同一行时的连接符。
# 只收全角与框线分隔符，**不收 ASCII "|"** —— markdown 表格行用它，收了会把
# 表格单元格当作独立段落判定。
_QUOTA_SEGMENT_SPLIT_RE = re.compile("[·•│┃｜]")
# 佐证状态码：4xx/5xx 全覆盖（含 429）。纯 429 无 quota 词仍不能定案——
# 关键词是必要条件（裁定1），429 只够嫌疑（suspect→unknown），不会判 quota。
_QUOTA_STATUS_RE = re.compile(r"(?<!\d)(?:4\d\d|5\d\d)(?!\d)")
_QUOTA_DOMAIN_RE = re.compile(
    r"\b(?:openai\.com|anthropic\.com|api\.deepseek\.com|api\.moonshot\.cn|"
    r"dashscope\.aliyuncs\.com|api\.z\.ai|api\.gptapi|claude\.ai)\b",
    re.IGNORECASE,
)
# 行级否决 G3-G7（命中行不参与 quota 判定）
_QUOTA_G3_DIFF_RE = re.compile(r"^\s*[+-]|^\s*@@")             # diff 变更行 / hunk 头
_QUOTA_G4_GREP_RE = re.compile(r"^\s*[^\s:]+\.\w{1,8}:\d+(?::\d+)?[: ]")  # grep path:line
_QUOTA_G5_ECHO_RE = re.compile(r"^\s*[$❯]\s")                  # 命令/输入回显
_QUOTA_G6_FENCE_RE = re.compile(r"^\s*(?:```+|~~~+)")          # markdown 围栏（状态化计数）
_QUOTA_G7_PYTEST_RE = re.compile(
    r"::test_|PASSED|FAILED|Traceback|^\s*[E>]\s|^={4,}"
)


def _quota_line_vetoed(ln: str) -> bool:
    """行级否决 G3-G7：命中任意一条则该行不参与 quota 判定。"""
    return bool(
        _QUOTA_G3_DIFF_RE.match(ln)
        or _QUOTA_G4_GREP_RE.match(ln)
        or _QUOTA_G5_ECHO_RE.match(ln)
        or _QUOTA_G7_PYTEST_RE.search(ln)
    )


def _quota_error_line_form(ln: str) -> bool:
    """该行是否为"错误输出形态"（行首门；放宽装饰前缀与分段拼接）。

    旧实现直接用 ``_QUOTA_ERROR_LINE_RE.match(ln)`` 锚定**整行行首**，但真实
    CLI 从不保证错误独占行首：

      - 转录区/告警/错误框会在错误文本前加装饰符（``⎿`` ``⚠`` ``│`` 及框线）；
      - 底部状态区把多段信息用 ``·`` 拼进同一行，例如实测的
        ``Please run /login·API Error:403 用户额度不足,剩余额度:¥0.00000000`` ——
        ``API Error`` 被挤到行中间，``^`` 锚定一律失配。强词 ``额度不足`` 明明
        在词表里，却只降级成 evidence → suspect → unknown：**成员静默卡死，
        既不换号也不告警**（本函数的修复动机，见 docs/plan-b §1.5.3）。

    修法：先剥离装饰前缀，再按分段分隔符拆开**逐段**判定，任一段成立即算。

    仍然**不做全行 search** —— 自然语言复述（"我现在要实现余额不足的识别逻辑"、
    反例 N8/N9）里的关键词不会出现在段首，段首锚定连同 G3-G7 行级否决仍是
    防误判的主力；放宽的只是"错误 token 必须在第 0 列"这一条过紧约束。
    """
    for seg in _QUOTA_SEGMENT_SPLIT_RE.split(ln):
        if _QUOTA_ERROR_LINE_RE.match(_QUOTA_LINE_DECOR_RE.sub("", seg)):
            return True
    return False


def _quota_terminal_at_rest(lines: list[str], tail16: list[str]) -> bool:
    """终端是否处于静止态（配额定案的必要条件之一）。

    静止门的作用是"别拿流式中途的半截帧定案"，但旧实现只认**最后一行**里的
    shell 提示符或 ``❯``，两个真实形态因此被漏掉：

      1. Claude TUI 在输入提示符**下方**常驻 footer/模式行（``⏸ manual mode on``、
         ``⏵⏵ accept edits``、token 计数），末行不是 ``❯`` → 判"未静止" →
         真配额错误永远停在 suspect。改为复用 _is_claude_ready_prompt 在**底部
         zone**（尾 5 行）判定：该函数本就处理"下方是静态 footer""上方有 spinner
         则不算就绪"两种情况，是现成且已验证的静止原语，绝不另写一套。
      2. CLI 已中止并要求 ``/login``（_detect_auth_state 命中错误形态行）——
         能渲染出这行说明本轮已经结束、不再流式输出，本身就是静止信号。
         中转站额度耗尽正是以 ``Please run /login·API Error:403 用户额度不足``
         这种"认证提示 + 配额错误"同屏形态出现的（实测语料），漏掉这条会让
         最典型的换号场景永远定不了案。

    ⚠️ 不放宽成"尾 5 行里出现过 ❯ 就算静止"：错误之后 agent 自行重试
    （边界例 B2：``❯`` 上方随后又起 spinner）必须继续算流式中，
    _is_claude_ready_prompt 的 spinner/footer 判定正好挡住这种情况。
    """
    last = tail16[-1].strip()
    if re.match(r"^[\w@~/:. \+\-\[\]()=]*[$#]\s*$", last) or "❯" in last:
        return True
    if _is_claude_ready_prompt(tail16[-5:]):
        return True
    # 实机取样补充（2026-08-15，本团队真实窗口）：Claude 的底部是三行结构
    #     ❯
    #     ────────────────────────────
    #       ⏸ manual mode on · ? for shortcuts · ← for agents
    # ``❯`` 与模式行之间**隔着一条分隔线**，于是 "❯" in last 不成立，
    # _is_claude_ready_prompt 也因为"❯ 的下一行不是状态行"而返回 False ——
    # 上面两条都盖不住这个最常见的真实布局。底部出现 CLI 静态状态栏
    # （_is_cli_status_line，既有原语；含 codex 的 `<model> <effort> · <cwd>`）
    # 本身就说明 CLI 已回到常驻界面。
    # 安全性：真正流式中的帧在两个分类器里都先被 _tail_shows_live_tool /
    # busy_markers 拦下，根本到不了 _detect_quota，所以这条不会放过流式半截帧。
    if any(_is_cli_status_line(ln) for ln in tail16[-3:]):
        return True
    return _detect_auth_state(lines)


def _detect_quota(lines: list[str]) -> str | None:
    """在终端非空行中识别配额/余额耗尽。

    返回三态：
      "quota"   —— 错误形态行命中强词，或弱词（402/billing 组合）同行有
                   4xx/5xx 状态码/供应商域名佐证，且终端处于静止
                   （见 _quota_terminal_at_rest 的三种静止信号）；
      "suspect" —— 有 quota 证据但不够格（非错误形态 / 未静止 / 双周期未确认等）。
                   调用方必须返回 unknown，绝不返回 idle（防 mark_idle_done 伪造成功）；
      None      —— 无任何 quota 证据。
    """
    if not lines:
        return None
    tail16 = lines[-16:]
    end_at_prompt = _quota_terminal_at_rest(lines, tail16)
    # G6：围栏状态跨全文统计（围栏可能开在 tail16 之前）
    in_fence = False
    fence_state = []
    for ln in lines:
        if _QUOTA_G6_FENCE_RE.match(ln):
            in_fence = not in_fence
        fence_state.append(in_fence)
    evidence = False
    for i, ln in enumerate(tail16):
        if fence_state[len(lines) - len(tail16) + i] or _QUOTA_WHITELIST_RE.search(ln):
            continue
        if _quota_line_vetoed(ln):
            continue
        if not _quota_error_line_form(ln):
            # 非错误形态行即使含 quota 词也只是证据（文档/代码/自然语言复述），
            # 不能定案 —— 这就是"代码里出现 quota 字样"反例的过滤层
            if _QUOTA_STRONG_RE.search(ln) or _QUOTA_WEAK_RE.search(ln):
                evidence = True
            continue
        if _QUOTA_STRONG_RE.search(ln):
            if end_at_prompt:
                return "quota"
            evidence = True
        if _QUOTA_WEAK_RE.search(ln) and (
            _QUOTA_STATUS_RE.search(ln) or _QUOTA_DOMAIN_RE.search(ln)
        ):
            if end_at_prompt:
                return "quota"
            evidence = True
        if _QUOTA_STATUS_RE.search(ln):
            # 错误形态行 + HTTP 状态码 → 配额嫌疑（如纯 429 限流），
            # 不能定案但足以阻止 idle 伪造成功
            evidence = True
    return "suspect" if evidence else None


# 认证态（"Not logged in, Please run /login" 等）：账号级登录失效，与 quota 分开
# 判定 —— 换号换的是第三方 profile 账号，而登录态是 CLI 自身凭据层，机器级失效
# 换号无法修复；"登录"字样也绝不应累计 quota_hits / 触发 failover。只认错误形态
# 行（与 _detect_quota 同窗口、同行级否决、同白名单），文档/代码正文里的 login
# 字样不误伤。
_AUTH_STATE_RE = re.compile(
    r"\b(?:not\s+logged\s+in|please\s+run\s+/login|login\s+(?:required|failed|again)|"
    r"authentication\s+(?:failed|required|error)|unauthorized\b|"
    r"未登录|请先登录|登录已过期|认证失败|登录失败)\b",
    re.IGNORECASE,
)


def _detect_auth_state(lines: list[str]) -> bool:
    """错误形态行命中认证关键词 → True（与 _detect_quota 同构的窗口/否决/围栏）。

    认证态是账号级硬阻断：CLI 无凭据无法执行任何动作，漏判会落 idle →
    mark_idle_done 伪造成功；误判（代码/文档正文）会卡住真忙成员，故只认
    错误形态行 + 认证关键词，行级否决 G3-G7 与围栏区间一律不参与。
    """
    if not lines:
        return False
    tail16 = lines[-16:]
    # G6：围栏状态跨全文统计（与 _detect_quota 同构，围栏可能开在 tail16 之前）
    in_fence = False
    fence_state = []
    for ln in lines:
        if _QUOTA_G6_FENCE_RE.match(ln):
            in_fence = not in_fence
        fence_state.append(in_fence)
    for i, ln in enumerate(tail16):
        if fence_state[len(lines) - len(tail16) + i]:
            continue
        if _QUOTA_WHITELIST_RE.search(ln) or _quota_line_vetoed(ln):
            continue
        if _quota_error_line_form(ln) and _AUTH_STATE_RE.search(ln):
            return True
    return False


def _classify_terminal_output(output: str, *, native_mode: str = "", suppress_classifier: bool = False) -> str:
    text = output or ""
    tail = "\n".join(text.splitlines()[-16:]).lower()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "unknown"
    approval_markers = (
        "requires approval",
        "do you want to proceed",
        "do you want to allow",
        "do you want to create",
        "do you want to edit",
        "do you want to run",
        "this command requires approval",
        "do you want to use this api key",
        "❯ 1. yes",
    )
    # 权限提示只认底部活动区（尾 5 行），与 _classify_leader_terminal_output 的
    # zone 语义一致：滚动到上方的命令输出残留（如 grep 回显的 "Do you want to
    # proceed? = yes"）不是真实权限提示，不再全文匹配 —— 否则 auto 模式会在
    # 残留授权文案上误发 Enter。
    zone = lines[-5:]
    zone_lower = "\n".join(zone).lower()
    last = zone[-1].lower()
    choice_in_zone = any(
        re.match(r"^[❯>\*]?\s*\d+[\.\)]\s*\S", ln) for ln in zone
    )
    if any(marker in zone_lower for marker in approval_markers) and (
        any(marker in last for marker in approval_markers) or choice_in_zone
    ):
        return "approval"

    # 执行中的工具（Claude ✢/◼ spinner + 耗时计数、Codex ◦ Working）必须判 busy，
    # 不能被底部常驻的 `❯`/tokens 状态行误判 idle（否则 monitor mark_idle_done 会把
    # 正在跑 Bash/Edit 的成员提前标记完成）。live-tool 优先否决一切，包括 quota。
    if _tail_shows_live_tool(lines):
        return "busy"

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
    # 剔除任务清单行后再匹配：清单是静态待办文本，``◼`` / "in progress" 都不是
    # 活动状态（见 _is_task_list_line）。codex 的 ``■ '<cmd>' is disabled …``
    # 系统提示同理（见 _CODEX_NOTICE_RE）。真正流式已由上面的 live-tool 判定兜底。
    busy_tail = "\n".join(
        _drop_codex_notice_lines(_drop_task_list_lines(text.splitlines()[-16:]))
    ).lower()
    if any(marker in busy_tail for marker in busy_markers):
        return "busy"

    # 配额/余额耗尽（阶段1 止血）：在两个 busy 之后、dead/idle 之前判定。
    # suspect（有配额证据但不够格）→ unknown，绝不 idle —— 否则 mark_idle_done
    # 会把根本没执行的任务标记为完成（docs/plan-b-hot-restart-resume.md §1.3）。
    q = _detect_quota(lines)
    if q == "quota":
        return "quota"
    if q == "suspect":
        return "unknown"

    # 认证态（"Not logged in, Please run /login"）：账号级登录失效，与 quota 分开
    # —— 不累计 quota_hits、不触发 failover（换号只换第三方 profile 账号，CLI
    # 自身凭据层仍断）。判 auth → 调用方标记独立阻塞告警；绝不落 idle（否则
    # mark_idle_done 伪造成功）。
    if _detect_auth_state(lines):
        return "auth"

    # 分类器暂时不可用（原生 auto 分类器故障）：需判定的工具被硬阻断，终端把 deny
    # 作为 tool result 返回、模型继续。必须判 classifier_unavailable —— 绝不 idle →
    # 绝不 mark_idle_done（不丢 checkpoint/session 上下文，2026-08-10 残留层）。
    # 放在 quota 之后、dead 之前：配额错误是"账号级"更可行动，先定案。检测无条件
    # （2026-08-11：签名是原生 auto 专用、自证消息，与 assumed 原生模式无关）——
    # 出现签名一律 classifier_unavailable；allow 仍 plan-only（common/classifier_fallback）。
    # F3 层2（2026-08-12）：``suppress_classifier=True``（成员处于注入抑制窗口）时
    # 跳过该分支——注入的引用文本不是成员真实进入分类器故障，让普通观测判定
    # （busy/idle/dead/quota 语义不变）。
    if (not suppress_classifier
            and classifier_fallback.classifier_detection_applies(native_mode)
            and classifier_fallback.detect_classifier_unavailable(text)):
        return "classifier_unavailable"

    if _tail_looks_like_shell_prompt(text):
        return "dead"

    # 就绪提示符判据（与 leader 侧共用 _is_claude_ready_prompt，绝不各写一套）：
    # 覆盖 codex 的 ``›`` 输入框 + ``<model> <effort> · <cwd>`` footer —— 下面的
    # idle_markers 只有 Claude 的 ``❯`` 和模式词，codex 成员因此一律落 unknown。
    # 它比裸字符匹配**更严**（要求上方无 spinner、下方是静态 footer），所以只会
    # 把原本 unknown 的 codex 静止帧补成 idle，不会放宽既有 Claude 语义。
    if _is_claude_ready_prompt(lines[-5:]):
        return "idle"

    idle_markers = (
        "manual mode on",
        "auto mode on",
        "⏸",
        "❯",
        "brewed for",
        "baked for",
        "tokens",
    )
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
    # 成员回报是否注入唤醒 leader（逃生阀）。默认 True：事件驱动回报注入有明确
    # 因果，与轮询 enabled 默认 False 刻意不同——用户关掉轮询不影响回报唤醒。
    "report_wakeup_enabled": True,
}

# 回报注入冷却（秒）：距上次成功注入（leader_last_wakeup_ts，轮询与回报两路径
# 共用）未达该间隔即跳过新的回报注入。RC2 去掉 resting 门后，resting 门原有的
# 天然限流（唤醒即置 active）消失，一轮巡检里多个成员同时被判完成会对 leader
# 终端连击注入 N 段文本；冷却把注入频率限制为 1 次/60s。被跳过的回报已在
# _record_report_and_notify_leader 内先写入 leader_pending_reports，信息不丢，
# 仅"打扰终端"动作被节流（leader_activate 仍可见全部回报）。
# 不复用 cooldown_cycles：那是轮询周期数（以巡检轮次为单位），与事件驱动的
# 秒级语义混用会让两条路径互相干扰。
REPORT_WAKEUP_COOLDOWN_SECONDS = 60


def _leader_wakeup_config(team: dict) -> dict:
    cfg = dict(LEADER_WAKEUP_DEFAULT_CONFIG)
    stored = team.get("leader_wakeup_config")
    if isinstance(stored, dict):
        cfg.update(stored)
    cfg["enabled"] = bool(cfg.get("enabled", False))
    cfg["report_wakeup_enabled"] = bool(cfg.get("report_wakeup_enabled", True))
    cfg["approval_alert"] = bool(cfg.get("approval_alert", True))
    cfg["auto_authorize_first"] = bool(cfg.get("auto_authorize_first", True))
    cfg["idle_threshold"] = max(1, min(int(cfg.get("idle_threshold", 4)), 20))
    cfg["cooldown_cycles"] = max(0, min(int(cfg.get("cooldown_cycles", 6)), 100))
    cfg["max_wakeups_per_session"] = max(1, min(int(cfg.get("max_wakeups_per_session", 10)), 1000))
    return cfg


# Braille spinner frames rendered by Claude/Codex while actively processing.
_LEADER_SPINNER_CHARS = frozenset(
    "⠁⠂⠄⠈⠐⠠⡀⡄⡆⡇⠃⠅⠇⠉⠙⠹⠸⠼⠴⠦⠧⠇⠏⠋⠓⠒⠐⠓⠉"
)


def _is_claude_status_line(line: str) -> bool:
    """True if a line is Claude's bottom mode/status bar (not command output)."""
    low = (line or "").lower()
    return any(
        marker in low
        for marker in (
            "manual mode",
            "auto mode",
            "auto-accept edits",
            "accept edits",
            "⏸",
            "token count",
            "tokens",
            "brewed for",
            "baked for",
        )
    )


# Codex CLI 的底部常驻行（实机取样 2026-08-16，真实 codex leader 窗口）：
#     ›                                          ← 输入框提示符 (U+203A)
#       gpt-5.6-sol high · /tmp/tmpqx.../workspace  ← 模型[+档位] · 工作目录
# 也可能渲染快捷键提示行或上下文余量：
#       ⏎ send   ⌃J newline   ⌃T transcript   ⌃C quit
#       87% context left
# 这些**一条都不沾** _is_claude_status_line 的词表（manual mode / tokens / …），
# 于是 _is_claude_ready_prompt 的"下方是静态 footer"判据对 codex 全部失败，
# codex 终端一律判 unknown → _leader_terminal_is_idle 恒为 False →
# 超时唤醒 / 回报注入 / 授权唤醒**四条注入链路同时失效**，codex leader 一旦
# leader_sleep 就再也醒不过来（本次修复的根因）。
#
# ⚠️ 模型行的正则刻意收紧到"`·` 两侧带空格且右侧是路径样式"：中转站配额错误
# 那一行 `Please run /login·API Error:403 用户额度不足…` 也含 `·`，若放宽成
# "含 · 即状态行"，配额错误帧会被当成静止 footer → 反向制造 fake-idle。
_CODEX_FOOTER_MODEL_RE = re.compile(
    r"^\s*[\w.\-]+(?:\s+(?:minimal|low|medium|high|xhigh))?\s+·\s+[~/.]\S*"
)
_CODEX_FOOTER_MARKERS = (
    "⏎ send",
    "⌃j newline",
    "⌃t transcript",
    "⌃c quit",
    "context left",
)


def _is_codex_status_line(line: str) -> bool:
    """True if a line is Codex CLI's bottom status/footer row."""
    s = line or ""
    if _CODEX_FOOTER_MODEL_RE.match(s):
        return True
    low = s.lower()
    return any(marker in low for marker in _CODEX_FOOTER_MARKERS)


def _is_cli_status_line(line: str) -> bool:
    """True if a line is either CLI's bottom static status/footer row.

    两侧分类器共用的单一原语：新增一种 CLI 只在这里加一次，绝不在 leader /
    成员两处各写一套（codex 漏检正是"只按 Claude 写了一套"的直接后果）。
    """
    return _is_claude_status_line(line) or _is_codex_status_line(line)


def _is_claude_ready_prompt(lines: list[str]) -> bool:
    """True when the bottom rows show a live CLI input prompt (``❯``/``›``).

    A bare prompt (or ``❯ <typed text>``, but not the approval option line
    ``❯ 1. Yes``) counts as READY when:
      - no live-processing signal (Stop ``◼`` / braille spinner) renders above
        it in the zone — a mid-tool terminal keeps its prompt on screen while
        the tool status sits above, and that is NOT idle; and
      - the row directly above is not a shell sub-prompt (``$``/``>``/``#``)
        that would make the ``❯`` the stdout tail of a running command; and
      - the row below (if any) is the CLI's static footer/mode line
        (Claude 的模式行 **或** codex 的 ``<model> <effort> · <cwd>`` /
        快捷键提示行 —— 见 _is_cli_status_line）。
    """
    for i, ln in enumerate(lines):
        s = ln.strip()
        is_prompt = s in ("❯", "›") or (
            (s.startswith("❯ ") or s.startswith("› "))
            and not re.match(r"^[❯›]\s*\d+[\.\)]\s*\S", s)
        )
        if not is_prompt:
            continue
        above = "\n".join(_drop_task_list_lines(lines[:i], context=lines)).lower()
        if "◼" in above or any(ch in above for ch in _LEADER_SPINNER_CHARS):
            # A live tool is rendering above this "❯" — it is command stdout.
            # Task-list rows are dropped first: their ``◼``/``◻`` glyphs are the
            # in-progress/open markers of a static todo block, not a spinner.
            continue
        # A bare shell transcript line ($ cmd / > cmd / # cmd) above the "❯"
        # means the terminal is showing a raw shell prompt, not Claude's TUI;
        # the "❯" is then the stdout tail of a running sub-command.  Tool-block
        # lines are prefixed with "│/┌/└" so they never match here.
        if any(re.match(r"^\s*[$#>]", ln) for ln in lines[:i]):
            continue
        prev = lines[i - 1].strip().lower() if i > 0 else ""
        if prev.endswith(("$", ">", "#")):
            # Shell sub-prompt directly above ⇒ this "❯" is command output.
            continue
        below = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if not below or _is_cli_status_line(below):
            return True
    return False


def _classify_leader_terminal_output(output: str, *, native_mode: str = "", suppress_classifier: bool = False) -> str:
    """Classify the leader terminal from its bottom activity zone.

    Claude Code renders a live status line, then the input-box prompt (``❯``),
    then a static footer (e.g. "⏵⏵ accept edits on …") — so the LAST non-empty
    line is often the footer, not the prompt.  Priority (highest first):
      1. a real permission choice (numbered line / approval phrase at bottom)
      2. a live tool indicator (spinner + elapsed counter)        -> busy
      3. a bare shell prompt (CLI crashed/exited)                 -> dead
      4. a busy marker on the live bottom line                    -> busy
      5. a recent prompt (❯ / ›) in the bottom zone, with a static
         footer below/above it                                    -> idle
    Command output that scrolled above the prompt/footer is history and must
    not pin the leader busy/approval — that would disable enter_resting (sleep)
    or fire spurious wakeup_approval; conversely a mid-tool terminal must never
    be seen as idle (its prompt+footer are still on screen).
    """
    text = output or ""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "unknown"
    zone = lines[-5:]
    zone_lower = "\n".join(zone).lower()
    last = lines[-1].lower()

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
    # A real permission prompt is the most recent interaction: a numbered choice
    # ("❯ 1. Yes") renders in the bottom zone — possibly with Claude's static
    # footer ("auto mode on") BELOW it — so the choice line is searched across
    # the whole zone, not just the last line.  Approval words that only appear
    # in scrolled command output (above the live zone) are not a prompt.
    choice_in_zone = any(
        re.match(r"^[❯>\*]?\s*\d+[\.\)]\s*\S", ln) for ln in zone
    )
    if any(marker in zone_lower for marker in approval_markers) and (
        any(marker in last for marker in approval_markers) or choice_in_zone
    ):
        return "approval"

    # 执行中的工具：Claude 的实时状态行（✢ Waddling… (42s)）位于输入框/静态
    # footer 之上，单看 `last` 会命中 footer/prompt 而误判 idle。
    # live-tool 优先否决 dead/quota：正在跑工具的终端既不是崩溃也不是配额耗尽。
    if _tail_shows_live_tool(lines):
        return "busy"

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
    # Busy is decided by the live bottom line (spinner / status / stop button).
    # A busy word in older command output above the prompt is history.
    # 任务清单行不参与：``◼`` 是清单的"进行中"标记，与停止指示符同形（与成员侧
    # _classify_terminal_output 同规则，两侧绝不各写一套）。
    if not (_has_task_list_block(zone) and _is_task_list_line(last)) and any(
        marker in last for marker in busy_markers
    ):
        return "busy"

    # 配额/余额耗尽（与成员共用 _detect_quota，绝不另写一套并行逻辑 —— 否则
    # 今天这个 bug 会被复制到 leader 侧）：在两个 busy 之后、dead/idle 之前判定。
    # 顺序与 _classify_terminal_output 严格一致：quota 先于 dead，因为 CLI 报
    # 配额错误后回到自己的 ❯ 输入框并未崩溃，先判 dead 会把它错当进程退出。
    # suspect（有证据但不够格）→ unknown，绝不 idle —— 否则 leader_idle_streak
    # 会累加，配额耗尽被当成"闲着"进而 enter_resting（成员侧阶段1 已修掉的同
    # 一个 fake-idle 缺陷）。
    q = _detect_quota(lines)
    if q == "quota":
        return "quota"
    if q == "suspect":
        return "unknown"

    # 认证态：与成员侧同构 —— 不累加 leader_idle_streak（否则认证断了被当成
    # "闲着"进而 enter_resting），独立标记阻塞告警，不换号。
    if _detect_auth_state(lines):
        return "auth"

    # 分类器暂时不可用（原生 auto 分类器故障）：与成员侧同构 —— 判
    # classifier_unavailable，绝不 idle（否则 leader_idle_streak 累加 → 误
    # enter_resting，leader 在分类器故障期"睡着"）。检测无条件（2026-08-11：签名
    # 是原生 auto 专用、自证消息，与 assumed 原生模式无关）——出现签名一律
    # classifier_unavailable；allow 仍 plan-only（common/classifier_fallback）。
    if (not suppress_classifier
            and classifier_fallback.classifier_detection_applies(native_mode)
            and classifier_fallback.detect_classifier_unavailable(text)):
        return "classifier_unavailable"

    if _tail_looks_like_shell_prompt(text):
        return "dead"

    # Idle = the LIVE input prompt (❯/›) is the bottom element, optionally with
    # a static footer/mode line below it.  A "❯" that is actually the tail of a
    # running command's stdout (a live spinner/Stop line or a shell sub-prompt
    # directly above it) is rejected, so an executing Bash/Edit is never
    # misjudged idle — otherwise wakeup injection would type into a running tool.
    if _is_claude_ready_prompt(lines[-5:]):
        return "idle"
    idle_markers = (
        "manual mode on",
        "auto mode on",
        "⏸",
        "brewed for",
        "baked for",
        "tokens",
    )
    if any(marker in last for marker in idle_markers):
        return "idle"
    return "unknown"


def _scan_leader_terminal(team_name: str, lines: int = 120) -> dict:
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    wakeup_cfg = _leader_wakeup_config(team)
    quota_cfg = quota_failover_config(team)
    # wakeup 与 quota 两个关注点分离：wakeup 只管 idle_streak/resting 判定；
    # 配额识别由 quota_failover_config 决定 —— leader 被 _monitor_team_once
    # 显式跳过，只有这条扫描路径能触达 leader 终端，若仍挂在 wakeup 门控下，
    # 配额耗尽会被永远漏检。两者皆关时才保留 "disabled" 早返回（维持零额外
    # tmux capture 开销，默认配置行为一字不变）。
    if not wakeup_cfg["enabled"] and not quota_cfg["enabled"]:
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

    state = _classify_leader_terminal_output(
        out,
        native_mode=classifier_fallback.claude_native_permission_mode(
            _member_mode(team.get("members", {}).get(leader, {}))
        ),
        # F3 层2：leader 处于注入抑制窗口时跳过 classifier_unavailable 分支（同上）。
        # 3a：抑制键 (team, member) 复合——仅同团队注入命中，跨团队同名零污染。
        suppress_classifier=_sig_injection_suppressed(team_name, leader),
    )
    now = datetime.datetime.now().isoformat()

    if state == "quota":
        # ---- leader 配额/余额耗尽（fake-idle 缺陷本体修复）----
        # 与成员侧 _scan_member_terminal 的 quota 分支同构，但用团队级
        # leader_quota_hits 独立计数（leader 没有成员 quota_hits 字段）。
        # 硬约束：配额态绝不累加 leader_idle_streak、绝不 enter_resting ——
        # 若落入下方 update_observed 的 idle 累加路径，配额耗尽会被当成
        # "闲着"进而休息。任何非 quota 状态在分支派发前清零计数。
        confirm = quota_cfg["confirm_cycles"]

        def update_quota(latest_team: dict) -> dict:
            # 双周期确认：连续 confirm 个监控周期稳定命中才确认（防瞬时伪影/
            # 滚动残留单帧误判换号）。未达阈值返回 unknown，绝不 idle。
            hits = int(latest_team.get("leader_quota_hits", 0) or 0) + 1
            latest_team["leader_quota_hits"] = hits
            # 硬约束 1：配额态永不累加 idle_streak → 永不能满足
            # _evaluate_leader_wakeup_conditions 的 enter_resting 条件。
            latest_team["leader_idle_streak"] = 0
            latest_team["leader_last_status_check_ts"] = now
            if hits < confirm:
                latest_team["leader_last_observed_state"] = "unknown"
                return {
                    "leader": latest_team.get("leader", leader),
                    "state": "unknown",
                    "idle_streak": 0,
                    "action": f"quota-suspect:{hits}/{confirm}",
                }
            # 确认达成：标记阻塞（quota_failover.enabled=False 默认只记录不换号）
            latest_team["leader_last_observed_state"] = "quota"
            leader_info = latest_team.get("members", {}).get(latest_team.get("leader", ""), {})
            if not isinstance(leader_info, dict):
                leader_info = {}
            leader_info["blocked_reason"] = "quota"
            leader_info["last_blocked_ts"] = now
            if not quota_cfg["enabled"]:
                return {
                    "leader": latest_team.get("leader", leader),
                    "state": "quota",
                    "idle_streak": 0,
                    "action": "quota-confirmed",
                }
            if int(leader_info.get("quota_switch_count", 0) or 0) >= quota_cfg["max_switches"]:
                # 换号上限：停止换号并保持阻塞告警（防换号风暴烧光池）
                return {
                    "leader": latest_team.get("leader", leader),
                    "state": "quota",
                    "idle_streak": 0,
                    "action": "quota-switch-limit",
                }
            nxt, fail_reason = _select_failover_profile(latest_team, leader_info)
            if nxt is None:
                # 保持阻塞，不静默降级。pool-type-mismatch 是配错了（如 codex
                # leader 配纯 claude 池）——换过去三处注入全返回空、原地空转，
                # 必须单独告警而不是混进"池空"。
                leader_info["blocked_reason"] = (
                    "quota-type-mismatch"
                    if fail_reason == "pool-type-mismatch"
                    else "quota"
                )
                return {
                    "leader": latest_team.get("leader", leader),
                    "state": "quota",
                    "idle_streak": 0,
                    "action": f"quota-pool-{fail_reason.replace('pool-', '')}",
                }
            # 记录换号历史 + 更新池游标（先落盘，再在锁外重建终端）
            prev_user = leader_info.get("agent_user") or ""
            history = leader_info.get("agent_user_failover_history") or []
            history.append({
                "from": leader_info.get("agent_user") or "",
                "to": nxt,
                "ts": now,
                "reason": "quota_exhausted",
            })
            leader_info["agent_user_failover_history"] = history
            pool = get_agent_user_pool(
                latest_team, member=leader_info,
                atype=resolve_pool_atype(latest_team, leader_info),
            )
            if nxt in pool:
                if member_pool_is_activated(leader_info):
                    leader_info["agent_user_pool_cursor"] = pool.index(nxt)
                else:
                    latest_team["agent_user_pool_cursor"] = pool.index(nxt)
            leader_info["agent_user"] = nxt
            return {
                "leader": latest_team.get("leader", leader),
                "state": "quota",
                "idle_streak": 0,
                "action": "switch",
                "to": nxt,
                "from_user": prev_user,
            }

        result = _update_team_data(team_name, update_quota) or {
            "leader": leader, "state": state, "action": "observed"
        }
        if result.get("action") != "switch":
            return result

        # ---- 换号（在锁外执行：_recover_and_send 内部自行 load/save）----
        nxt = result["to"]
        # 杀旧窗口已收敛进 _recover_and_send（reason="quota_switch" 分支统一处理）：
        # 配额耗尽时 CLI 仍存活（窗口在），不杀窗会让 _tmux_spawn_member 返回
        # "window already exists"（rc=0），恢复文本打进旧进程、新账号 env 永不
        # 生效。此处不再重复 kill —— 成员侧走同一函数，两边行为一致。
        ok, msg = _recover_and_send(
            team_name, leader, session, reason="quota_switch",
            previous_agent_user=result.get("from_user", ""),
            extra_message=_leader_system_prompt(team_name, team.get("leader_last_task", "")),
        )

        def update_switched(latest_team: dict) -> dict:
            # 重载最新数据合并 _recover_and_send 写入的计数；换号后清零计数，
            # 新账号重新走识别流程，若再次耗尽重新累积
            latest_team["leader_idle_streak"] = 0
            latest_team["leader_quota_hits"] = 0
            latest_team["leader_last_status_check_ts"] = now
            li = latest_team.get("members", {}).get(latest_team.get("leader", ""), {})
            if not isinstance(li, dict):
                li = {}
            if ok:
                # 换号成功：解除阻塞，leader 进入重建激活态（全新进程不存在 resting）
                li.pop("blocked_reason", None)
                latest_team["leader_state"] = "active"
                latest_team["leader_last_observed_state"] = "recovering"
                return {
                    "leader": latest_team.get("leader", leader),
                    "state": "quota",
                    "idle_streak": 0,
                    "action": f"quota-switched:{nxt}",
                }
            # 换号失败：保留阻塞（新号已记录，重建失败也计一次切换）
            return {
                "leader": latest_team.get("leader", leader),
                "state": "quota",
                "idle_streak": 0,
                "action": f"quota-switch-failed:{msg}",
            }

        return _update_team_data(team_name, update_switched) or {
            "leader": leader, "state": "quota", "action": f"quota-switch-failed:{msg}"
        }

    if state == "auth":
        # 认证态：账号级登录失效，与 quota 分开 —— 不累计 leader_quota_hits、
        # 不换号、绝不 enter_resting（否则"认证断了"被当成闲着进而休眠）。
        # 标记独立阻塞告警，供终端状态展示 / leader_activate 可见。
        def update_auth(latest_team: dict) -> dict:
            latest_team["leader_quota_hits"] = 0
            latest_team["leader_idle_streak"] = 0
            latest_team["leader_last_status_check_ts"] = now
            latest_team["leader_last_observed_state"] = "auth"
            li = latest_team.get("members", {}).get(latest_team.get("leader", leader), {})
            if isinstance(li, dict):
                li["blocked_reason"] = "auth"
                li["last_blocked_ts"] = now
            return {
                "leader": latest_team.get("leader", leader),
                "state": "auth",
                "idle_streak": 0,
                "action": "auth-state",
            }

        return _update_team_data(team_name, update_auth) or {
            "leader": leader, "state": "auth", "action": "auth-state",
        }

    # 任何非 quota 状态 → 配额计数清零（分支派发之前，与成员侧一致：
    # 成员重试/自愈即放弃确认，防抖动换号）
    def update_observed(latest_team: dict) -> dict:
        latest_team["leader_quota_hits"] = 0
        if state == "idle":
            latest_team["leader_idle_streak"] = int(latest_team.get("leader_idle_streak", 0)) + 1
        else:
            latest_team["leader_idle_streak"] = 0
            if latest_team.get("leader_state") == "resting" and state in {"busy", "approval"}:
                latest_team["leader_state"] = "active"
        # 分类器 fallback 审计（leader 侧，观察式进出/恢复）：classifier_unavailable
        # 不累加 idle_streak（上方 state != "idle" 已保证）→ 分类器故障期 leader
        # 绝不 enter_resting；进出各记一条审计供复核。模式切换由 leader_set_member_mode
        # + 重新 spawn 完成，本路径只保留上下文、不重启不 compact。
        prev_leader_state = latest_team.get("leader_last_observed_state")
        leader_info = latest_team.get("members", {}).get(latest_team.get("leader", leader), {})
        if state == "classifier_unavailable" and prev_leader_state != "classifier_unavailable":
            classifier_fallback.record_classifier_fallback_event(
                _share_dir(team_name), team_name=team_name, scope="leader",
                member=latest_team.get("leader", leader),
                mode=_member_mode(leader_info), state="entered",
                note="leader 终端 Claude 分类器暂时不可用；已保留 checkpoint/session，支持恢复后重试。",
            )
        elif prev_leader_state == "classifier_unavailable" and state != "classifier_unavailable":
            classifier_fallback.record_classifier_fallback_event(
                _share_dir(team_name), team_name=team_name, scope="leader",
                member=latest_team.get("leader", leader),
                mode=_member_mode(leader_info), state="recovered",
            )
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
        # leader_sleep 主动休眠的超时唤醒：成员回报/授权优先，超时兜底。
        sleep_until = team.get("leader_sleep_until")
        if leader_state == "resting" and sleep_until:
            import datetime

            if datetime.datetime.now().isoformat() >= sleep_until:
                return {"action": "wakeup_timeout"}

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
        headline = "[唤醒通知] Leader wakeup: a member is waiting for authorization."
        extra = f"Authorization needed: {blocked}."
    elif reason == "report":
        report = details.get("report") or {}
        reporter = report.get("member") or "unknown"
        result = _compact_text(report.get("result") or "", 300)
        headline = "[唤醒通知] Leader activation: a member reported a result."
        extra = f"Report from {reporter}: {result}"
        if report.get("artifact_path"):
            extra += f" | artifact: {report['artifact_path']}"
        extra += (
            "\n查看共享上下文: member_read_shared 或读取 member_contexts/ 下的压缩上下文。"
        )
    elif reason == "pending_reports":
        # 巡检兜底补投用：汇总 leader_pending_reports 全部滞留回报（被回报注入
        # 冷却挡下、冷却过期后由 _retry_deferred_report_injection 补投）。
        pending = details.get("pending_reports") or []
        headline = "[唤醒通知] Leader activation: member reports are waiting."
        report_lines = []
        for i, report in enumerate(pending, 1):
            reporter = report.get("member") or "unknown"
            result = _compact_text(report.get("result") or "", 300)
            ts = (report.get("timestamp") or "")[:19]
            line = f"  {i}. [{ts}] {reporter}: {result}"
            if report.get("artifact_path"):
                line += f" | artifact: {report['artifact_path']}"
            report_lines.append(line)
        if not report_lines:
            report_lines.append("  - (empty)")
        extra = "\n".join(report_lines) + (
            "\n查看共享上下文: member_read_shared 或读取 member_contexts/ 下的压缩上下文。"
        )
    elif reason == "timeout":
        headline = "[唤醒通知] Leader wakeup: sleep timeout reached."
        extra = (
            "审查所有成员的任务状态，识别是否存在阻塞、超时或依赖问题；"
            "如有必要，主动向相关成员发起询问。"
        )
    else:
        headline = "[唤醒通知] Leader wakeup: all tracked member tasks appear complete."
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

    if action in {"wakeup_all_done", "wakeup_approval", "wakeup_timeout"}:
        # timeout 是 leader 主动 leader_sleep 的预期唤醒，不应被
        # max_wakeups_per_session 限额挡停（否则 leader 会困在 resting）。
        if action != "wakeup_timeout":
            wakeups = int(team.get("leader_wakeup_count", 0))
            if wakeups >= cfg["max_wakeups_per_session"]:
                def update_limit(latest_team: dict) -> dict:
                    latest_team["leader_last_action"] = "wakeup-limit"
                    return {"action": "wakeup-limit"}

                return _update_team_data(team_name, update_limit) or {"action": "none"}

        should_inject = _leader_terminal_is_idle(team_name, team)
        reason = {
            "wakeup_approval": "approval",
            "wakeup_all_done": "all_done",
            "wakeup_timeout": "timeout",
        }[action]

        # ---- 未送达绝不消费唤醒（2026-08-16 修复）----------------------------
        # 旧实现无条件先跑 update_wakeup（置 active + pop leader_sleep_until +
        # 计数 +1），再 `if not should_inject: return`。于是终端不空闲的那一刻，
        # 这次唤醒被**不可逆地消费**：state 已 active → resting 分支不再成立、
        # sleep_until 已删 → 超时分支也不再成立，下一轮 _evaluate 直接返回
        # {"action": "none"}，leader 终端一个字都没收到却永远等不到第二次。
        # 实测（codex leader，_leader_terminal_is_idle 因终端识别缺陷恒为 False）：
        # 一轮之后 state=active / sleep_until=None / wakeup_count=1 / 注入 0 次。
        # 现在改为**投递成功才推进状态**：未送达只记录延迟证据，保持 resting +
        # sleep_until 原样，下一轮巡检自然重试。对 Claude leader 同样是修复
        # ——唤醒时刻恰好在跑工具，旧实现同样会白丢这次唤醒。
        def _defer(defer_reason: str, err: str = "") -> dict:
            def update_defer(latest_team: dict) -> dict:
                latest_team["leader_wakeup_deferred_reason"] = defer_reason
                latest_team["leader_wakeup_deferred_ts"] = now
                latest_team["leader_wakeup_deferred_count"] = (
                    int(latest_team.get("leader_wakeup_deferred_count", 0)) + 1
                )
                latest_team["leader_last_action"] = f"wakeup-deferred:{reason}"
                return {"action": action, "injected": False, "deferred": True,
                        "reason": defer_reason, **({"error": err} if err else {})}

            return _update_team_data(team_name, update_defer) or {
                "action": action, "injected": False, "deferred": True,
                "reason": defer_reason,
            }

        if not should_inject:
            return _defer("leader-not-idle")

        session = _find_any_session(team_name)
        pre_team = _team_info(team_name)
        pre_leader = pre_team.get("leader", "")
        pre_target = _member_window_target(team_name, pre_leader) if pre_leader else None
        if not session or not pre_target:
            return _defer("no-leader-target")

        # ---- 先投递、成功了才推进状态 ----------------------------------------
        # 顺序刻意与旧实现相反：旧实现先 update_wakeup 再发送，注入失败
        # （尤其 codex 提交确认 rc=-1）时状态已经 active、sleep_until 已删，
        # 这次唤醒同样被白白消费。现在任何失败都走 _defer，resting+sleep_until
        # 原样保留，下轮巡检重试。
        message = _build_leader_wakeup_message(team_name, reason, action_info)
        rc, err = _send_context_to_member(
            session,
            pre_target,
            message,
            confirm_submission=_target_is_claude_tmux_leader(pre_team, pre_leader),
            confirm_codex_submission=_target_is_codex_tmux_leader(pre_team, pre_leader),
        )
        if rc != 0:
            # 注入失败：状态一律不推进（保持 resting + sleep_until，下轮重试），
            # 也不写冷却时间戳 —— 失败不被掩盖，pending/后续 retry 仍可达。
            return _defer("inject-failed", err)

        # 真实注入成功后才推进状态并写冷却时间戳（与 _notify_leader_of_report /
        # _retry_deferred_report_injection 一致：ts 是"最后一次成功注入"的时间，
        # 供 _report_wakeup_cooldown_passed 节流后续注入，防连击）。
        def update_wakeup(latest_team: dict) -> dict:
            latest_cfg = _leader_wakeup_config(latest_team)
            latest_wakeups = int(latest_team.get("leader_wakeup_count", 0))
            latest_team["leader_state"] = "active"
            latest_team["leader_idle_streak"] = 0
            latest_team["leader_wakeup_reason"] = reason
            latest_team["leader_wakeup_count"] = latest_wakeups + 1
            latest_team["leader_wakeup_cooldown_remaining"] = latest_cfg["cooldown_cycles"]
            latest_team["leader_last_wakeup_ts"] = datetime.datetime.now().isoformat()
            latest_team.pop("leader_resting_since", None)
            latest_team.pop("leader_sleep_until", None)
            latest_team.pop("leader_wakeup_deferred_reason", None)
            return {"action": action, "injected": True, "error": err,
                    "wakeup_count": latest_wakeups + 1}

        return _update_team_data(team_name, update_wakeup) or {
            "action": action,
            "injected": False,
            "error": "update-failed",
        }

    return {"action": "none"}


def _report_wakeup_cooldown_passed(team: dict) -> bool:
    """回报注入冷却检查：距上次成功注入是否已达 REPORT_WAKEUP_COOLDOWN_SECONDS。

    容错（宁可多注入一次，不可因解析失败永久静默）：字段缺失、格式非法、
    时区不匹配（TypeError）、或时钟回拨导致负差值时，一律放行注入。
    """
    import datetime

    ts = team.get("leader_last_wakeup_ts")
    if not ts:
        return True
    try:
        last = datetime.datetime.fromisoformat(ts)
        elapsed = (datetime.datetime.now() - last).total_seconds()
    except (TypeError, ValueError):
        return True
    if elapsed < 0:
        return True  # 时钟回拨：放行，避免负差值把冷却期算成无限长
    return elapsed >= REPORT_WAKEUP_COOLDOWN_SECONDS


def _notify_leader_of_report(team_name: str, entry: dict) -> dict:
    """Member-report → leader activation (回报激活机制).

    当成员调用 member_report_result 回报结果时:
      - tmux leader 终端存活: report_wakeup_enabled 开启 + 空闲 + 距上次注入
        已过冷却(REPORT_WAKEUP_COOLDOWN_SECONDS)时注入回报摘要并标记 active,
        立即激活 leader(任何 leader_state)；否则只持久化回报。
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

    # ---- leader 终端存活：回报注入开关 + 空闲才注入（任何 leader_state） ----
    if session and not _leader_window_is_dead(team_name, team, session):
        if (
            cfg["report_wakeup_enabled"]
            and _leader_terminal_is_idle(team_name, team)
        ):
            # 冷却：距上次成功注入（leader_last_wakeup_ts，:1696 注入成功后写）
            # 未达 REPORT_WAKEUP_COOLDOWN_SECONDS 时跳过注入。RC2 去 resting 门后
            # 一轮巡检多个成员同时完成会对 leader 终端连击注入；被跳过的回报已在
            # _record_report_and_notify_leader 内先写入 leader_pending_reports，
            # 信息不丢，仅"打扰终端"动作被节流（leader_activate 仍可见全部回报）。
            if not _report_wakeup_cooldown_passed(team):
                return {"injected": False, "leader": leader, "reason": "report-cooldown"}
            leader_target = _member_window_target(team_name, leader) or leader
            message = _build_leader_wakeup_message(team_name, "report", {"report": entry})
            rc, err = _send_context_to_member(
                session,
                leader_target,
                message,
                confirm_submission=_target_is_claude_tmux_leader(team, leader),
                confirm_codex_submission=_target_is_codex_tmux_leader(team, leader),
            )
            if rc != 0:
                return {"injected": False, "leader": leader, "error": err}

            def update_wakeup(latest_team: dict) -> dict:
                latest_team["leader_state"] = "active"
                latest_team["leader_idle_streak"] = 0
                latest_team["leader_wakeup_reason"] = "report"
                latest_team["leader_last_wakeup_ts"] = datetime.datetime.now().isoformat()
                # §5-2：置 active 的同时清理休眠时间戳，避免下轮 _evaluate 把残留
                # 过期 sleep_until 误判为 wakeup_timeout 导致双注入（与 :1615/:5073 一致）
                latest_team.pop("leader_sleep_until", None)
                # S3：注入成功=已投递（delivered），未 ACK 的报告不再被巡检重放
                # （竞态 B 根治）；leader_activate drain 仍是最终 ACK。
                mark_pending_reports_delivered(latest_team, [entry.get("report_id")])
                return {"injected": True, "leader": leader}

            return _update_team_data(team_name, update_wakeup) or {
                "injected": False,
                "leader": leader,
                "reason": "update-failed",
            }
        return {"injected": False, "leader": leader, "reason": "leader-live"}

    # ---- leader 终端已死：由 member_report_result 的独立 revival 闭环处理 ----
    return {"injected": False, "leader": leader, "reason": "leader-dead"}


def _retry_deferred_report_injection(team_name: str) -> dict:
    """巡检兜底：冷却过期后补投滞留的成员回报（direct leader 的 tmux 侧对应物）。

    REPORT_WAKEUP_COOLDOWN_SECONDS 挡下的回报没有自动重试——多成员同一轮完成时
    只有第一份被注入，其余永久停在 leader_pending_reports，直到新回报事件或
    leader 主动 leader_activate。本函数在每次巡检调用一次（_monitor_team_wakeup_once
    内、_execute_leader_wakeup_action 之后）：同时满足
        tmux leader + 未投递 pending 非空 + report_wakeup_enabled + 终端存活且空闲
        + 冷却已过
    才补投 **未投递(delivered=False)** 的回报清单，并照常更新 leader_last_wakeup_ts
    ——兜底自身也受冷却约束，否则每轮巡检都注入，等于把冷却废掉。

    S3 只重放未投递：已注入成功（delivered=True）的报告不再每 60s 重放——leader
    看"成员回报待处理"是未投递的，已投递未 ACK 的只提示 leader_activate 收讫，
    杜绝"未 ACK ≠ 未发送"的误判（竞态 B 根治）。

    与 _notify_leader_of_report 共用同一批判据（_leader_terminal_is_idle /
    _leader_window_is_dead / _report_wakeup_cooldown_passed），不另写一套；
    dead 分支不注入（revival 闭环由巡检尾部 _maybe_revive_leader 单独处理）；
    被跳过的回报始终完整留在 leader_pending_reports（RC1 底线，只有
    leader_activate 消费清空）。direct leader 由 MCP 装饰器层 nudge 覆盖，
    不在此路径（避免双重打扰）。

    返回 {"injected", "leader", "reason"}；任何失败降级为不注入，不抛异常
    （巡检线程外层只有裸 except 兜底）。
    """
    import datetime

    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    if not team:
        return {"injected": False, "reason": "no-team"}
    if team.get("leader_type") != "tmux":
        return {"injected": False, "reason": "not-tmux-leader"}
    reports = undelivered_pending_reports(team)
    if not reports:
        return {"injected": False, "reason": "no-pending"}
    cfg = _leader_wakeup_config(team)
    if not cfg["report_wakeup_enabled"]:
        return {"injected": False, "reason": "report-disabled"}
    session = _find_any_session(team_name)
    if not session or _leader_window_is_dead(team_name, team, session):
        return {"injected": False, "reason": "leader-dead"}
    if not _leader_terminal_is_idle(team_name, team):
        return {"injected": False, "reason": "leader-live"}
    if not _report_wakeup_cooldown_passed(team):
        return {"injected": False, "reason": "report-cooldown"}

    leader = team.get("leader", "")
    leader_target = _member_window_target(team_name, leader) if leader else None
    if not leader_target:
        return {"injected": False, "reason": "no-leader-target"}
    message = _build_leader_wakeup_message(
        team_name, "pending_reports", {"pending_reports": reports}
    )
    rc, err = _send_context_to_member(
        session,
        leader_target,
        message,
        confirm_submission=_target_is_claude_tmux_leader(team, leader),
        confirm_codex_submission=_target_is_codex_tmux_leader(team, leader),
    )
    if rc != 0:
        return {"injected": False, "leader": leader, "error": err}

    def update_wakeup(latest_team: dict) -> dict:
        latest_team["leader_state"] = "active"
        latest_team["leader_idle_streak"] = 0
        latest_team["leader_wakeup_reason"] = "report"
        latest_team["leader_last_wakeup_ts"] = datetime.datetime.now().isoformat()
        # 与 _notify_leader_of_report 一致：置 active 的同时清理休眠时间戳，
        # 避免下轮 _evaluate 把残留过期 sleep_until 误判为 wakeup_timeout 双注入
        latest_team.pop("leader_sleep_until", None)
        # S3：本次实际注入的报告标 delivered（已投递未 ACK 不再被下轮巡检重放）
        mark_pending_reports_delivered(
            latest_team, [r.get("report_id") for r in reports]
        )
        return {"injected": True, "leader": leader}

    return _update_team_data(team_name, update_wakeup) or {
        "injected": False,
        "reason": "update-failed",
    }


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
    # 兜底补投：被回报注入冷却挡下的滞留回报，冷却过期后在巡检路径补投一次。
    # 必须放在 _execute_leader_wakeup_action 之后——轮询路径本轮若已注入，
    # 其刚写入的 leader_last_wakeup_ts 会被兜底的冷却检查挡住，一轮内天然不双发。
    reinjected = _retry_deferred_report_injection(team_name)
    # P0 task1：批量消息队列自动推进（gate-held 消息在硬门放行后自动投递，
    # 无需 leader 人工再次调用；与报告兜底补投同巡检路径）。
    outbox_advance = _advance_member_outbox_once(team_name)
    # 中断闭环：巡检时若 leader 终端已死则自动重建（幂等，活跃 leader 不受影响）
    revived, revive_msg = _maybe_revive_leader(team_name, reason="patrol")
    return {
        "leader": leader_result,
        "members": member_results,
        "action": executed,
        "report_reinjection": reinjected,
        "outbox_advance": outbox_advance,
        "leader_revived": revived,
        "leader_revive_msg": revive_msg,
    }


def _apply_member_scan_fields(
    team_name: str,
    member_name: str,
    fields: dict,
    pop_fields: tuple = (),
    team_fields: dict | None = None,
) -> bool:
    """锁内定向更新 monitor 观测字段 —— 绝不整份 stale teams_data 覆写并发写入。

    P1 竞态 A1/A2/A3 根治：_scan_member_terminal 开头加载的是 stale 快照，旧实现
    末尾用整份 ``_save(data)`` 会把并发 member_report_result 在锁内落盘的亲笔字段
    （last_task_completed / last_report_* / last_report_key / pending）整体回退。
    本原语在 ``_update_team_data`` 锁内 fresh load → apply monitor 字段 → save，
    只写 monitor 自己负责的观测字段（状态/时间戳/配额计数/阻塞），并发亲笔回报
    字段天然保留。返回成员是否存在。
    """
    def _updater(latest_team: dict) -> dict:
        members = latest_team.get("members", {})
        mem = members.get(member_name)
        if not mem:
            return {"applied": False}
        for k, v in fields.items():
            mem[k] = v
        for k in pop_fields:
            mem.pop(k, None)
        if team_fields:
            for k, v in team_fields.items():
                latest_team[k] = v
        return {"applied": True}

    result = _update_team_data(team_name, _updater)
    return bool(result and result.get("applied"))


def _monitor_idle_autocomplete_fresh_check(
    team_name: str,
    member_name: str,
) -> tuple[bool, dict]:
    """锁内 fresh 判定 idle 自动完成（P1 A2 根因）。

    monitor 判 idle 用的是开头加载的 stale 快照；并发亲笔回报可能已在 fresh 状态
    落盘 last_task_completed / last_report_* / pending。因此"是否自动完成+合成回报"
    必须基于 fresh 数据：已权威完成（或有同任务亲笔 member_report pending）→ 不合成、
    不重复标记，保留亲笔 report_id/pending。本函数只读判定、不写任何字段。
    返回 (是否本次自动完成, fresh 成员快照)。
    """
    def _decide(latest_team: dict) -> dict:
        mem = latest_team.get("members", {}).get(member_name) or {}
        last_task = mem.get("last_task") or ""
        proceed = bool(last_task) and not mem.get("last_task_completed", True)
        if proceed:
            # 纵深防御：同任务已有亲笔 member_report pending → 视为已权威回报，跳过合成
            pending = latest_team.get("leader_pending_reports") or []
            if any(
                r.get("event") == "member_report"
                and r.get("member") == member_name
                and r.get("report_task") == last_task
                for r in pending
            ):
                proceed = False
        return {"proceed": proceed, "member": dict(mem)}

    res = _update_team_data(team_name, _decide) or {}
    return bool(res.get("proceed")), res.get("member") or {}


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

    # P2：每个监控周期先回收超过 TTL 的 DRAINING 旧窗（只回收 DRAINING，ACTIVE 不碰）。
    # 旧窗回收后不再被任何路由/计数触达 —— 与 _member_window_target 的 ACTIVE 路由配合，
    # 保证 monitor 只观察权威窗口。回收会改写 terminal_windows → 重载，避免本函数
    # 用旧引用覆盖回收结果（DRAINING 记录回弹）。
    _reclaim_member_draining_windows(team_name, member_name)
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    member = members.get(member_name)
    if not member:
        return {"member": member_name, "state": "missing", "action": "missing"}

    session = _find_any_session(team_name)
    if not session:
        _apply_member_scan_fields(
            team_name, member_name,
            {"last_observed_state": "dead",
             "last_status_check_ts": datetime.datetime.now().isoformat()},
        )
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
        _apply_member_scan_fields(
            team_name, member_name,
            {"last_observed_state": "dead",
             "last_status_check_ts": datetime.datetime.now().isoformat()},
        )
        if member.get("last_task") and not member.get("last_task_completed", True):
            if member.get("recovery_count", 0) >= int(team.get("monitor_max_recoveries", 3)):
                return {"member": member_name, "state": "dead", "action": "recovery-limit"}
            ok, msg = _recover_and_send(team_name, member_name, session)
            return {"member": member_name, "state": "dead", "action": "recovered" if ok else f"recover-failed:{msg}"}
        return {"member": member_name, "state": "dead", "action": "window-missing"}

    rc, out, err = _capture_window(session, member_target, lines)
    if rc != 0:
        return {"member": member_name, "state": "error", "action": err}

    state = _classify_terminal_output(
        out,
        native_mode=classifier_fallback.claude_native_permission_mode(_member_mode(member)),
        # F3 层2：成员处于注入抑制窗口（leader 刚转述过含分类器签名的文本）时跳过
        # classifier_unavailable 分支——注入的引用文本不是真实分类器故障，让普通
        # 观测判定（busy/idle/dead/quota 语义不变，绝不因此误判 idle 丢上下文）。
        # 3a：抑制键 (team, member) 复合——仅同团队注入命中，跨团队同名零污染。
        suppress_classifier=_sig_injection_suppressed(team_name, member_name),
    )
    now = datetime.datetime.now().isoformat()
    prev_state = member.get("last_observed_state") or "unknown"
    # 分类器 fallback 恢复（观察式）：签名从捕获窗口消失 → 审计 recovered。
    # 必须在本函数重写 last_observed_state 之前判定 prev。
    recovered_from_classifier = (
        prev_state == "classifier_unavailable"
        and state != "classifier_unavailable"
    )
    if recovered_from_classifier:
        classifier_fallback.record_classifier_fallback_event(
            _share_dir(team_name), team_name=team_name, scope="member",
            member=member_name, mode=_member_mode(member), state="recovered",
        )
    action = "classifier-recovered" if recovered_from_classifier else "observed"

    # ── monitor 观测字段（统一在函数末尾锁内定向落盘）────────────────────
    # last_task_completed / last_report_* / last_report_key / pending / results.jsonl
    # 是亲笔回报权威字段，由 member_report_result 锁内写入；monitor 只更新自己负责的
    # 观测字段（状态/时间戳/配额计数/阻塞），绝不整份 stale 覆写 —— P1 竞态 A1/A2/A3。
    scan_fields: dict = {
        "last_observed_state": state,
        "last_status_check_ts": now,
    }
    if state != "quota":
        scan_fields["quota_hits"] = 0
        # 可观测性：有配额证据但不够格定案（suspect）时留痕。suspect 在
        # _classify_terminal_output 里一律降级为 unknown，与"普通说不清"在数据层
        # 完全无法区分 —— 生产事故里正是这个盲区让中转站额度耗尽静默卡死：
        # 不计数、不写 blocked_reason、不告警，运维只能看到一个 unknown。
        # 只记时间戳（不参与 quota_hits，定案仍要求连续的确定帧），供排障区分
        # "从没识别到配额证据" 与 "识别到了但没敢定案"。
        if state == "unknown" and _detect_quota(
            [ln for ln in (out or "").splitlines() if ln.strip()]
        ) == "suspect":
            scan_fields["last_quota_suspect_ts"] = now
            if action == "observed":
                action = "quota-suspect-unconfirmed"
    pop_fields: set = set()
    # blocked_reason 终态：blocked 非 None → 设置；否则若 blocked_clear → 清除。
    blocked: str | None = None
    blocked_clear: bool = recovered_from_classifier  # 分类器恢复：清旧阻塞

    if state == "dead":
        # 进程已退出掉到 shell 提示符（崩溃/OOM/手动退出），但 tmux 窗口仍存活：
        # 若有未完成任务，先清理旧窗口再重建，避免同名窗口被复用为 <name>(1)。
        blocked = "crashed"
        scan_fields["blocked_reason"] = "crashed"
        scan_fields["last_blocked_ts"] = now
        if member.get("last_task") and not member.get("last_task_completed", True):
            if member.get("recovery_count", 0) >= int(team.get("monitor_max_recoveries", 3)):
                _apply_member_scan_fields(team_name, member_name, scan_fields, tuple(pop_fields))
                return {"member": member_name, "state": "dead", "action": "recovery-limit"}
            _tmux(["kill-window", "-t", _tmux_target(session, member_target)])
            time.sleep(0.3)
            ok, msg = _recover_and_send(team_name, member_name, session)
            _apply_member_scan_fields(team_name, member_name, scan_fields, tuple(pop_fields))
            return {"member": member_name, "state": "dead", "action": "recovered" if ok else f"recover-failed:{msg}"}
        # 无未完成任务：仅清除崩溃阻塞，不落盘 crashed
        scan_fields.pop("blocked_reason", None)
        scan_fields.pop("last_blocked_ts", None)
        blocked = None
        blocked_clear = True
    elif state == "classifier_unavailable":
        # Claude 分类器暂时不可用（plan/auto）：Bash/Write/Edit 被硬阻断，但
        # 不是 approval、不是 dead、不是 idle。绝不放行任何命令（硬阻断是原生
        # 安全行为，本轮**不绕过** classifier）；只标记阻塞 + 审计 entered，
        # 保留 last_task / session 上下文，等分类器恢复后成员重试（观察式恢复，
        # 见上方 recovered_from_classifier）。支持 leader 切换模式后重新 spawn。
        blocked = "classifier_unavailable"
        if prev_state != "classifier_unavailable":
            classifier_fallback.record_classifier_fallback_event(
                _share_dir(team_name), team_name=team_name, scope="member",
                member=member_name, mode=_member_mode(member), state="entered",
                note="Claude 分类器暂时不可用，Bash/Write/Edit 被硬阻断；已保留任务上下文，支持恢复后重试 / leader 切换模式。",
            )
            action = "classifier-unavailable-entered"
        # 绝不 mark_idle_done、绝不 pop last_task、绝不 compact —— 失败不丢
        # checkpoint/session 上下文（_finalize_agent_completion 不触发）。
    elif state == "approval":
        blocked = "approval"
        mode = _member_mode(member)
        if auto_authorize_choice or member.get("auto_authorize") or mode == "auto":
            choice = auto_authorize_choice or member.get("auto_authorize_choice") or "session"
            choice_key = _authorization_choice_key(choice)
            if choice_key is not None or choice.strip().lower() == "enter":
                arc, aerr = _send_authorization_choice(session, member_target, choice_key)
                action = f"auto-authorized:{choice}" if arc == 0 else f"authorize-failed:{aerr}"
                if arc == 0:
                    scan_fields["last_observed_state"] = "busy"
                    state = "busy"
                    blocked = None
                    blocked_clear = True
    elif state == "quota":
        # 余额/配额耗尽：连续 confirm_cycles 个监控周期稳定命中才确认（防瞬时
        # 伪影/滚动残留单帧误判换号）。未达阈值返回 unknown（绝不 idle → 绝不
        # mark_idle_done），只累计计数。
        #
        # ⚠️ 阶段3 诚实标注：CLI 会话 resume 未实现（阶段2 未做）——换号后是
        # 全新会话，对话上下文不保留；但成员任务 checkpoint 已接线，换号恢复
        # 消息携带 verify-then-continue 续跑依据（见 _recover_and_send docstring）。
        quota_cfg = quota_failover_config(team)
        confirm = quota_cfg["confirm_cycles"]
        hits = int(member.get("quota_hits", 0) or 0) + 1
        scan_fields["quota_hits"] = hits
        if hits >= confirm:
            blocked = "quota"
            action = "quota-confirmed"
            # 阶段3：确认后按池顺序换号。默认 enabled=False → 只记录不换号
            # （保持既有 blocked_reason="quota" 行为，默认行为不变）。
            if quota_cfg["enabled"]:
                if int(member.get("quota_switch_count", 0) or 0) >= quota_cfg["max_switches"]:
                    # 换号上限：停止换号并保持阻塞告警，不无限重试（防换号风暴烧光池）
                    action = "quota-switch-limit"
                else:
                    nxt, fail_reason = _select_failover_profile(team, member)
                    if nxt is None:
                        # 保持阻塞，不静默降级。三种失败原因运维处置不同：
                        #   pool-type-mismatch 是配错了（如 codex 成员配纯
                        #   claude 池）——换过去三处注入全返回空、原地空转，
                        #   必须单独告警而不是混进"池空"。
                        action = f"quota-pool-{fail_reason.replace('pool-', '')}"
                        blocked = (
                            "quota-type-mismatch"
                            if fail_reason == "pool-type-mismatch"
                            else "quota"
                        )
                    else:
                        # 记录换号历史 + 更新池游标（阶段3 决策，见 plan-b §3.2）
                        prev_user = member.get("agent_user") or ""
                        history = member.get("agent_user_failover_history") or []
                        history.append({
                            "from": member.get("agent_user") or "",
                            "to": nxt,
                            "ts": now,
                            "reason": "quota_exhausted",
                        })
                        scan_fields["agent_user_failover_history"] = history
                        # 游标写回池的归属方（成员池激活时写成员，否则写团队）
                        pool = get_agent_user_pool(
                            team, member=member,
                            atype=resolve_pool_atype(team, member),
                        )
                        team_fields: dict = {}
                        if nxt in pool:
                            if member_pool_is_activated(member):
                                scan_fields["agent_user_pool_cursor"] = pool.index(nxt)
                            else:
                                team_fields["agent_user_pool_cursor"] = pool.index(nxt)
                        scan_fields["agent_user"] = nxt
                        # 先把切换决策锁内定向落盘，再调 _recover_and_send（其内部
                        # 独立 load/save 递增 quota_switch_count）——两者都基于 fresh
                        # 读，不会互相覆写（旧代码此处用 stale _save(data) 整份覆写，
                        # 会回退并发 member_report_result 的亲笔字段）。
                        scan_fields["blocked_reason"] = "quota"
                        scan_fields["last_blocked_ts"] = now
                        _apply_member_scan_fields(
                            team_name, member_name, scan_fields, tuple(pop_fields),
                            team_fields=team_fields,
                        )
                        ok, msg = _recover_and_send(
                            team_name, member_name, session, reason="quota_switch",
                            previous_agent_user=prev_user,
                        )
                        # 换号后清零计数：新账号重新走识别流程，若再次耗尽重新累积
                        scan_fields["quota_hits"] = 0
                        if ok:
                            # 换号成功：解除该次阻塞，成员进入重建期
                            scan_fields["last_observed_state"] = "recovering"
                            blocked = None
                            blocked_clear = True
                            action = f"quota-switched:{nxt}"
                        else:
                            action = f"quota-switch-failed:{msg}"
        else:
            state = "unknown"
            action = f"quota-suspect:{hits}/{confirm}"
    elif state == "auth":
        # 认证态：账号级登录失效（"Not logged in, Please run /login"）。
        # 与 quota 分开：不累计 quota_hits（上方 state != "quota" 分支已清零）、
        # 不触发 failover —— 换号只换第三方 profile 账号，CLI 自身凭据层仍断，
        # 换过去必然原地再撞。标记独立阻塞告警；绝不 mark_idle_done（任务未执行）。
        blocked = "auth"
        action = "auth-state"
    elif state == "idle":
        blocked_clear = True
        if mark_idle_done and member.get("last_task") and not member.get("last_task_completed", True):
            # 锁内 fresh 判定（A2 根因）：并发亲笔回报已在 fresh 落盘完成 →
            # 跳过合成回报与重复完成标记，保留亲笔 report_id/pending。
            proceed, fresh_member = _monitor_idle_autocomplete_fresh_check(
                team_name, member_name,
            )
            if proceed:
                action = "marked-complete"
                scan_fields["last_task_completed"] = True
                scan_fields["last_observed_state"] = "idle"
                scan_fields["last_completed_by_monitor_ts"] = now
                synthetic_result = _build_monitor_completion_result(fresh_member)
                # 回报必须先于 /compact 落到 leader 可见处（results.jsonl +
                # leader_pending_reports + leader 唤醒）——否则成员终端被我们注入的
                # /compact 清空后回报永远到不了 leader，leader 永不激活（P0 主根因）。
                # 补回报失败绝不阻断 monitor 扫描：本地就地 try，不依赖外层裸 except。
                # event 用 monitor_inferred_completion 与亲笔 member_report 区分
                # （区分度形态与"先催一轮再 mark 完成"待 reviewer 裁决，事件字段先占位）。
                try:
                    _record_report_and_notify_leader(
                        team_name, member_name, synthetic_result,
                        event=MONITOR_INFERRED_EVENT,
                    )
                except Exception:
                    pass
                _finalize_agent_completion(
                    team_name, member_name, synthetic_result,
                    is_leader=False,
                )
                # compact_sent_by_monitor audit marker（锁内定向）：一个显式的
                # member_report_result 允许权威 /compact 一次，普通重复回报仍幂等。
                def _mark_audit(latest_team: dict) -> dict:
                    lm = latest_team.get("members", {}).get(member_name, {})
                    if lm.get("compact_sent"):
                        lm["compact_sent_by_monitor"] = True
                    return {"saved": True}

                _update_team_data(team_name, _mark_audit)
    elif state == "busy":
        blocked_clear = True

    # ── 统一锁内定向落盘 monitor 观测字段（绝不整份 stale 覆写）──────────
    if blocked is not None:
        scan_fields["blocked_reason"] = blocked
        scan_fields["last_blocked_ts"] = now
    elif blocked_clear:
        pop_fields.add("blocked_reason")
    _apply_member_scan_fields(team_name, member_name, scan_fields, tuple(pop_fields))
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
        # P4b：monitor 前刷新 codex 首启回填——有标记未回填时 discover 真实 session_id。
        # leader 由 spawn 路径（_revive_leader_terminal_locked → _tmux_spawn_member）刷新。
        _codex_session_backfill(team_name, name)
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


# TUI launch_terminals 只写 terminals_active=True、不调用 _start_team_monitor
# （tui 不 import mult_agent_mcp；monitor 单宿主于 MCP server 进程，避免双扫）。
# 本周期 sweep 保证仅经 TUI 启动的团队也能得到 classifier_unavailable 检测/审计、
# wakeup/resting、idle/done 半环 —— 即 TUI vs CLI 启动链路差异 2 的根治。
MONITOR_SWEEP_INTERVAL_SECONDS = 15


def _ensure_team_monitors_once() -> int:
    """为 ``terminals_active`` 且尚无运行中 monitor 的团队启动 monitor，返回启动数。

    幂等：``_start_team_monitor`` 内部检查 ``TEAM_MONITOR_THREADS`` 线程存活，
    重复调用不双启。非活跃团队（``terminals_active`` False）不启动 —— 与
    ``_monitor_team_loop`` 的退出语义一致（loop 在 terminals_active 为 False 时
    返回，sweep 不会为已杀终端的团队无限重启 monitor）。
    """
    data = _load()
    started = 0
    for team_name, team in (data.get("teams") or {}).items():
        if team.get("terminals_active"):
            _start_team_monitor(team_name)
            started += 1
    return started


def _ensure_team_monitors_loop(stop_event: threading.Event) -> None:
    """周期巡检：每个间隔执行一次 ``_ensure_team_monitors_once``。

    TUI 启动可随时发生（不触发任何 MCP 工具），故必须周期扫描而非只扫一次。
    单次失败不中断循环（best-effort，与 monitor 主循环一致）。
    """
    while not stop_event.is_set():
        try:
            _ensure_team_monitors_once()
        except Exception:
            pass
        stop_event.wait(MONITOR_SWEEP_INTERVAL_SECONDS)


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
    if observed in {"busy", "approval", "recovering", "classifier_unavailable"}:
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
    resume_argv: list[str] | None = None,
    append_system_prompt_file: str = "",
) -> list[str]:
    """Build CLI args for a Claude Code member.

    Member mode → CLI --permission-mode mapping:
      auto   → acceptEdits  (auto-approve Edit/Write; Bash prompts → monitor authorizes)
      plan   → plan         (read-only; no modifications)
      manual → (no flag)    (all tools prompt for approval)

    We use "acceptEdits" instead of "auto" because "auto" hard-denies tools
    not in the allow list (→ "bash auto mode denied"), while "acceptEdits"
    generates prompts that the leader monitor can auto-authorize.

    ``append_system_prompt_file``：身份进 system 层的 `--append-system-prompt-file`
    文件路径（fact-check §8 已确认技术路线，/compact 免疫，每次启动含 resume 必带）。
    生产 spawn 点经 ``prompt_registry.claude_identity_file()`` 传入真实身份文件；
    未显式传入（直接调用/单测）回落确定性默认路径，保证 argv 恒携带该 flag。
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
    # P0：身份进 system 层——Claude 唯一可靠通道 --append-system-prompt-file
    # （/compact 免疫，每次启动含 resume 必带）。生产 spawn 点已传入真实身份
    # 文件；此处回落确定性默认路径，保证任何调用路径 argv 都携带该 flag。
    if not append_system_prompt_file:
        append_system_prompt_file = prompt_registry.default_claude_identity_path()
    args.extend(["--append-system-prompt-file", append_system_prompt_file])
    # P4：session resume（开启时）——精确 --resume <id> 恢复原会话，或
    # --session-id <id> 把新会话绑定为稳定 id（未来可恢复）。关闭时恒 None，
    # 不追加任何参数，spawn 行为与既有完全一致。
    if resume_argv:
        args.extend(resume_argv)
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


def _member_report_first_rule() -> str:
    """成员"先回报再 compact"顺序义务（统一措辞，所有成员可见 prompt 面共用）。

    系统自身注入的 /compact 一定在回报之后（_finalize_agent_completion 位于
    member_report_result 末尾）；成员自身上下文压力触发的 auto-compact 会把
    回报义务连同上下文一起清空。此义务注入派单（assign_subtask/broadcast）、
    终端恢复、任务续跑四处，避免与成员身份绑定段落重复。
    """
    return (
        "⚠️ 顺序义务：任务完成后的第一个动作必须是调用 member_report_result 回报，"
        "在此之前不要执行 /compact；若上下文即将耗尽，先回报再继续。"
    )


def _member_delivery_contract() -> str:
    return "\n".join([
        "[交付格式]",
        "完成后调用 member_report_result，result 仅包含:",
        "1. 结论",
        "2. 修改文件",
        "3. 验证/测试",
        "4. 风险/阻塞",
        "compressed_context <= 200 字；不要复述过程日志。",
        "",
        _member_report_first_rule(),
    ])


def _build_member_task_payload(subtask: str, context: str = "", reason: str = "") -> tuple[str, str]:
    task_text = subtask.strip()
    compact_context = _compact_text(context, 700) if context.strip() else ""
    # P0：任务派单框架走 prompts/members.ts memberTaskPayload（@channel task，
    # user 通道 send-keys）；动态段（子任务/必要上下文/分配原因）拼进 ${v.task}。
    # 渲染失败回退内建 Python 内联文本（A4：不静默丢交付合约、不输出空串）。
    body = task_text
    if compact_context:
        body += "\n\n[必要上下文] " + compact_context
    if reason:
        body += "\n\n[分配原因] " + _compact_text(reason, 180)
    text = prompt_registry.render_channel("members", "memberTaskPayload",
                                          {"task": body}, "")
    if text is not None:
        return text, compact_context
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
    # P0：成员首启上下文走 prompts/members.ts memberInitialContext（@channel
    # initial，user 通道 send-keys/argv），prompts/*.ts 为运行时可编辑权威源；
    # 渲染失败回退内建 Python 内联文本（A4：不静默丢身份、不输出空串）。
    vars_ = {
        "teamName": team_name,
        "memberName": member_name,
        "role": member.get("role") or "member",
        "agent": _member_agent(team, member),
        "mode": _member_mode(member),
        "leader": team.get("leader") or "direct",
        "leaderType": team.get("leader_type") or "direct",
        "teamDir": _team_dir(team_name),
        "shareDir": _share_dir(team_name),
        "task": "",
        "recoverySection": "",
    }
    text = prompt_registry.render_channel("members", "memberInitialContext", vars_, team_name)
    if text is not None:
        return text
    role = member.get("role", "member")
    agent = _member_agent(team, member)
    leader = team.get("leader", "")
    leader_type = team.get("leader_type", "")
    mode = _member_mode(member)

    lines = [
        f"[成员上下文] Multi-Agent MCP 成员上下文: team='{team_name}'",
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


# ---------------------------------------------------------------------------
# P4：稳定 session_id + 三态 resume 计划（feature flag 默认关闭，关闭时零行为变化）
# ---------------------------------------------------------------------------

def _member_claude_config_home(team_name: str, member_name: str) -> str:
    """成员 claude 会话转录根：接管时用私有 CLAUDE_CONFIG_DIR，否则 ~/.claude。

    与 claude_agent_user_launch 的 env CLAUDE_CONFIG_DIR=<dir> 一致——resume 校验
    必须落在成员实际写入转录的配置根，而不是默认 ~/.claude（接管成员会写进
    .agent_user_home 私有目录）。未接管时返回 ~/.claude（默认转录根）。
    """
    try:
        config_dir = build_agent_user_claude_config_dir(team_name, member_name)
        if config_dir:
            return config_dir
    except RuntimeError:
        pass
    return os.path.expanduser("~/.claude")


def _member_codex_home(team_name: str, member_name: str) -> str:
    """成员 codex 会话根：优先回填标记记录的 spawn 时 CODEX_HOME，否则 env/~/.codex。

    codex 会话写盘的 CODEX_HOME 在 spawn 时已定格（可能被调用方 env 覆盖）；
    resume/回填必须对准记录的那个根，而不是恢复时的进程 env（可能已不同）。
    """
    data = _load()
    member = data.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
    if isinstance(member, dict):
        bf = member.get("session_backfill")
        if isinstance(bf, dict) and bf.get("codex_home"):
            return str(bf["codex_home"])
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _record_session_backfill_marker(team_name: str, member_name: str, *, spawn_ts: float) -> None:
    """记录 codex 首启回填标记：spawn 时间 / 工作目录 / 私有 CODEX_HOME（P4b）。

    managed codex 首启**不自造 uuid 当真实会话 id**——只记录"这次 spawn 发生在
    何时/何目录/哪个 CODEX_HOME"，真实 session_id 之后由 ``_codex_session_backfill``
    discover 扫描实际写盘的 rollout 后回填。direct/claim leader 无管理终端 →
    保持 checkpoint-only 边界。已有标记（首次 spawn）不覆盖，保证时间窗指向首次
    会话而非后续恢复 spawn。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    agent = _member_agent(team, member)
    if not _is_codex(agent) or not session_resume.resume_enabled():
        return
    if not isinstance(member, dict) or member.get("session_id"):
        return  # 非 codex / 功能关闭 / 已回填真实 id
    if member_name == team.get("leader", "") and team.get("leader_type") == "direct":
        return  # direct leader 只 checkpoint，无管理终端可回填
    marker = {
        "spawn_ts": spawn_ts,
        "cwd": _team_dir(team_name),
        "codex_home": os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex"),
    }

    def updater(latest_team: dict) -> dict:
        m = latest_team.get("members", {}).get(member_name)
        if isinstance(m, dict) and not m.get("session_id"):
            old = m.get("session_backfill") if isinstance(m.get("session_backfill"), dict) else {}
            if not old.get("spawn_ts"):
                m["session_backfill"] = marker
        return {"ok": True}

    _update_team_data(team_name, updater)


def _expire_session_backfill(team_name: str, member_name: str) -> None:
    """时间窗确定性缺失后过期回填标记，停止重复扫描（P4b）。

    超过 spawn 时间窗仍未 discover 到真实会话 → 该次 spawn 不会再有新 rollout，
    标记已无意义；移除后 monitor 不再逐 tick 扫描，恢复路径自然 checkpoint-only。
    """
    def updater(latest_team: dict) -> dict:
        m = latest_team.get("members", {}).get(member_name)
        if isinstance(m, dict) and isinstance(m.get("session_backfill"), dict):
            m.pop("session_backfill", None)
        return {"ok": True}

    _update_team_data(team_name, updater)


def _codex_session_backfill(team_name: str, member_name: str, *, window_seconds: float = 300.0) -> None:
    """monitor / recovery / spawn 前刷新：发现 codex 首启真实 session 并**原子写**回填。

    P4b：managed codex 首启不自造 uuid；spawn 时 ``_record_session_backfill_marker``
    记录 spawn_ts/cwd/codex_home，此后在 monitor、recovery、spawn 前调用本函数：
    discover 扫描"新产生"rollout，仅在时间窗 + cwd 匹配 + 候选唯一时回填真实
    session_id（先经 ``resolve_codex_session`` 确认可定位再落盘）；歧义/缺失一律
    不写（保持未回填，恢复路径自然 checkpoint fallback）。leader 同时原子写
    ``leader_checkpoint.session_id``（先 checkpoint 再 resume）。direct/claim
    leader 无管理终端 → 保持 checkpoint-only 边界。
    """
    if not session_resume.resume_enabled():
        return
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    if not isinstance(member, dict) or member.get("session_id"):
        return  # 已回填真实 id → 无需重复扫描
    if member_name == team.get("leader", "") and team.get("leader_type") == "direct":
        return  # direct leader 只 checkpoint
    bf = member.get("session_backfill")
    if not isinstance(bf, dict) or not bf.get("spawn_ts"):
        return  # 无标记 → 非 codex 首启或功能关闭
    codex_home = str(bf.get("codex_home") or _member_codex_home(team_name, member_name))
    cwd = str(bf.get("cwd") or _team_dir(team_name))
    disc = session_resume.discover_codex_session(
        spawn_ts=float(bf["spawn_ts"]),
        workspace_dir=cwd,
        codex_home=codex_home,
        window_seconds=window_seconds,
    )
    if not disc["ok"]:
        # 确定性缺失（时间窗已过仍未发现）→ 过期标记，停止重复扫描
        upper = float(bf["spawn_ts"]) + window_seconds + session_resume._DISCOVER_CLOCK_SKEW
        if time.time() > upper:
            _expire_session_backfill(team_name, member_name)
        return
    # belt-and-suspenders：真实 id 必须能经 resolve 定位（禁回填不可解析的 id）
    check = session_resume.resolve_codex_session(disc["session_id"], codex_home)
    if not check["ok"]:
        return
    real_sid = check["session_id"]

    def updater(latest_team: dict) -> dict:
        m = latest_team.get("members", {}).get(member_name)
        if isinstance(m, dict):
            m["session_id"] = real_sid
            m["session_backfill"] = {
                "spawn_ts": bf["spawn_ts"], "cwd": cwd,
                "codex_home": codex_home, "resolved": True,
            }
        if member_name == latest_team.get("leader", ""):
            _leader_checkpoint_upsert(
                latest_team, {"session_id": real_sid}, source="session_backfill",
            )
        return {"ok": True}

    _update_team_data(team_name, updater)


def _member_session_id(team_name: str, member_name: str, team_dir: str, *, for_agent: str = "") -> str:
    """成员稳定 session_id：已持久化优先，Claude 初次生成 uuid4 并回写持久化。

    稳定性契约：session_id 是 uuid4，一旦生成便持久化在 member["session_id"]
    （leader 亦可落在 leader_checkpoint），此后换号/恢复/复活一律读回同一 id——
    绝不现场重算/猜测（真实 CLI 只认已持久化会话，B1/B3）。leader 优先复用
    leader_checkpoint 中 session_id（"先 checkpoint 再 resume"）。幂等（值不变仅
    回写缺失字段）。team_dir 参数保留以兼容调用点，uuid 不依赖 workspace。

    ``for_agent``：codex 首启**不自造 uuid 当真实会话 id**（P4b）——codex 无已
    持久化真实 id 时返回空串，真实 id 由 ``_codex_session_backfill`` discover 回填；
    Claude/未知 agent 保持既有行为（首次生成 uuid4 并持久化）。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    sid = member.get("session_id") if isinstance(member, dict) else ""
    if not sid and member_name == team.get("leader", ""):
        # leader 重启/切换恢复：复用 leader checkpoint 中 session_id（P4 要求）
        cp = team.get("leader_checkpoint") if isinstance(team, dict) else None
        if isinstance(cp, dict):
            sid = cp.get("session_id") or ""
    if not sid:
        if _is_codex(for_agent) and session_resume.resume_enabled():
            # P4b：resume 开启时 codex 首启**不自造 uuid 当真实会话 id**——真实 id
            # 由 _codex_session_backfill discover 回填；返回空串走 checkpoint/bind。
            return ""
        # 初次 spawn：生成 uuid4 并持久化（Claude 会话 id 是 uuid，非派生哈希；
        # resume 关闭时 codex 亦保持 P4 既有行为，零变化）
        sid = session_resume.new_session_id()

    def updater(latest_team: dict) -> dict:
        m = latest_team.get("members", {}).get(member_name)
        if isinstance(m, dict) and m.get("session_id") != sid:
            m["session_id"] = sid
        return {"ok": True}

    _update_team_data(team_name, updater)
    return sid


def _session_resume_plan(team_name: str, member_name: str, agent: str, team_dir: str, *, force_checkpoint_only: bool = False) -> dict | None:
    """三态 session resume 计划；功能关闭或强制 checkpoint 时返回 None（零变化）。

    开启时返回（任何一步校验失败都回落更保守一档，绝不断言可恢复）:
      {"kind": "resume", "session_id": sid, "argv": ["--resume", sid] | ["resume", sid]}
          transcript / codex session 精确定位成功 → 恢复原会话
      {"kind": "bind",   "session_id": sid, "argv": ["--session-id", sid] | []}
          claude 转录缺失 → 绑定新会话为已持久化 uuid（未来可 resume）；
          codex 无 --session-id → argv 为空（原样启动，恢复上下文仍注入）
    关闭 / force_checkpoint_only → None → 调用方不追加任何 argv、不写任何数据，
    与既有行为完全一致；force_checkpoint_only 供 P2 generation 跨凭证迁移用
    （新账号窗口不得原生 resume 旧账号会话，只走 checkpoint 续跑）。

    ``session_id`` 恒为**已持久化**的 uuid（member.session_id / leader checkpoint），
    绝不现场生成猜测；codex 首启未回填真实 id 时 sid 为空 → 原样启动（bind，
    argv=[]，只 checkpoint，禁 --last）。安全闸：argv 由模块精确构造器产出，绝不含
    --last/-l/--continue/-c；resume 只认精确 transcript/session 路径，且接入
    reject_sensitive_paths——credentials/settings 等敏感路径一律不构造 resume。
    """
    if force_checkpoint_only or not session_resume.resume_enabled():
        return None
    sid = _member_session_id(team_name, member_name, team_dir, for_agent=agent)
    if _is_codex(agent):
        codex_home = _member_codex_home(team_name, member_name)
        if session_resume.reject_sensitive_paths([codex_home]):
            return None
        if not sid:
            # codex 首启尚未回填真实 id → 原样启动，只 checkpoint（不自造 uuid）
            return {"kind": "bind", "session_id": "", "argv": []}
        check = session_resume.resolve_codex_session(sid, codex_home)
        if check["ok"]:
            # resume 必须用真实 session uuid（只认 rollout-*.jsonl 证据）
            real_sid = check["session_id"]
            return {"kind": "resume", "session_id": real_sid, "argv": session_resume.codex_resume_argv(real_sid)}
        return {"kind": "bind", "session_id": sid, "argv": []}
    if _is_claude(agent):
        claude_home = _member_claude_config_home(team_name, member_name)
        if session_resume.reject_sensitive_paths([claude_home]):
            return None
        if session_resume.validate_transcript(sid, team_dir, claude_home)["ok"]:
            return {"kind": "resume", "session_id": sid, "argv": session_resume.claude_resume_argv(sid)}
        return {"kind": "bind", "session_id": sid, "argv": session_resume.claude_session_id_argv(sid)}
    return None


def _merge_spawned_session_ids(data: dict, team_name: str, member_names: list[str]) -> None:
    """launch_team_terminals spawn 后把已持久化 session_id 合并回内存 data。

    _tmux_spawn_member 内部已通过 _update_team_data 持久化 session_id（uuid4，
    只读回，不可重算），但 launch_team_terminals 随后会 _save(data)（load 时的旧
    引用）覆盖磁盘；此处从最新磁盘读回各成员 session_id 合并进内存 data，保证
    初次 spawn 的持久化不被旧引用冲掉。功能关闭时成员无 session_id → 零写入。
    """
    if not member_names:
        return
    fresh = _load().get("teams", {}).get(team_name, {}).get("members", {})
    team = data.get("teams", {}).get(team_name, {})
    for name in member_names:
        m = team.get("members", {}).get(name)
        if isinstance(m, dict):
            sid = (fresh.get(name) or {}).get("session_id", "") if isinstance(fresh, dict) else ""
            if sid:
                m["session_id"] = sid


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
    allowed_tools: list[str] | None = None,
    resume_disabled: bool = False,
) -> tuple[int, str, str]:
    """启动成员 tmux 窗口，统一处理 workspace 与 agent 类型差异。

    对于 claude 成员，自动写入 .claude/settings.json 预配置权限以减少审批阻塞。
    ``prompt`` 仅对 codex agent 生效（作为 CLI 位置参数传入）；claude agent 的
    初始提示由调用方在启动后通过 send-keys 注入。
    ``allowed_tools`` 仅 claude agent 生效（--allowedTools），用于 leader 复活时
    补齐 leader 自身 Bash/Edit/MCP 工具的自动放行（普通成员保持 None）。
    ``resume_disabled``：True 时强制不接 session resume（P2 generation 跨凭证
    迁移用——新账号窗口不得原生 resume 旧账号会话，只走 checkpoint 续跑）。
    """
    name = window_name or member_name
    if new_session:
        cmd = ["new-session", "-d", "-s", session, "-n", name]
    else:
        cmd = ["new-window", "-t", session, "-n", name]

    team_name = _resolve_team_name_from_session(session)
    team = _load().get("teams", {}).get(team_name, {})
    member_info = team.get("members", {}).get(member_name, {})
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

    # P4：session resume 计划（默认关闭/强制 checkpoint → None，不追加任何参数）。
    # P4b：spawn 前先刷新 codex 首启回填——若该 codex 成员有回填标记且尚未回填真实
    # session_id，这里 discover 扫描"新产生"rollout 并原子写；P2 跨凭证迁移
    # （resume_disabled=True）保持 checkpoint-only，不触发回填。
    if not resume_disabled:
        _codex_session_backfill(team_name, member_name)
    resume_plan = _session_resume_plan(team_name, member_name, agent, team_dir, force_checkpoint_only=resume_disabled)
    resume_argv = resume_plan["argv"] if resume_plan else None

    if _is_codex(agent):
        # Codex 无 system-prompt 通道：身份固化到唯一自动装载持久指令文件
        # AGENTS.md（团队中立段，抗 compact/resume，防多角色串线 B2）。
        prompt_registry.ensure_codex_agents_md(team_name, team_dir)
        cmd.extend(agent_user_prefix + proxy_prefix + _codex_command(agent, team_dir, prompt=prompt, member_mode=mode, model=resolved_model, effort=resolved_effort, resume_argv=resume_argv))
    else:
        # Claude / 其他 agent: 预配置权限 + 从共享工作目录启动
        # G2 修复：共享 settings.json 被工作目录下所有 Claude 进程加载，只能承载
        # 一个模式；若按当前 spawn 成员自己的 mode 写，混合团队会随 spawn 顺序
        # last-writer-wins 翻转（leader plan + member auto / 反向）。改为团队 union
        # 有效模式（任一 claude 成员映射原生 plan → plan），确定性、不按 leader
        # 串权。每 Agent 精确豁免仍由下方 --allowedTools argv 承载。
        _write_claude_permissions(
            team_name,
            dangerously_skip=dangerously_skip_permissions,
            mode=classifier_fallback.team_classifier_effective_mode(team.get("members") or {}),
        )

        # 私有 settings 目录权限收紧失败时 fail closed，返回可见错误而非无锁继续
        try:
            au_prefix, claude_settings_path = claude_agent_user_launch(team_name, member_name)
        except RuntimeError as e:
            return -1, "", str(e)

        # --allowedTools：模式限定 fallback（plan/auto 追加精选安全窄规则，其他
        # 模式原样 → 不外溢）。_tmux_spawn_member 是 MCP 侧成员与 managed leader
        # （含 leader 复活）共用的统一 spawn 点，故在此单点做模式限定。
        resolved_tools = allowed_tools if allowed_tools is not None else CLAUDE_MEMBER_TOOL_ALLOW_PATTERNS
        # 身份进 system 层（fact-check §8）：--append-system-prompt-file 单点接线。
        # 本 spawn 点是成员与 managed leader 复活统一入口，按 member_name==leader
        # 判定渲染 leader/成员身份（角色不得混淆）。
        is_leader_spawn = bool(team) and (member_name == team.get("leader"))
        identity_path = prompt_registry.claude_identity_file(
            team_name, member_name, leader=is_leader_spawn
        )
        agent_args = _claude_agent_args(
            agent,
            mode,
            dangerously_skip_permissions=dangerously_skip_permissions,
            allowed_tools=classifier_fallback.claude_terminal_allow_tools(
                mode, team_dir, resolved_tools
            ),
            model=resolved_model,
            settings_path=claude_settings_path,
            effort=resolved_effort,
            resume_argv=resume_argv,
            append_system_prompt_file=identity_path,
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
                spawn_ts = time.time()
                result = _tmux(cmd)
                if result[0] == 0:
                    _remember_member_window_id(team_name, member_name, session, name)
                    # P4b：codex 首启记录回填标记（spawn_ts/cwd/CODEX_HOME）。
                    # P2 跨凭证迁移（resume_disabled=True）保持 checkpoint-only，不记录。
                    if not resume_disabled:
                        _record_session_backfill_marker(team_name, member_name, spawn_ts=spawn_ts)
                return result
        except (OSError, RuntimeError) as e:
            # 跨进程锁 fail closed：锁不可用时不得无锁创建，转为可见错误
            return -1, "", f"无法获取跨进程成员 spawn 锁: {e}"


def _codex_command(agent_cmd: str, team_dir: str, prompt: str = "", member_mode: str = "", *, model: str = "", effort: str = "", resume_argv: list[str] | None = None) -> list[str]:
    """构造 codex 成员启动命令。

    effort 经 `-c model_reasoning_effort="<level>"` 注入：Codex CLI 通过
    -c/--config 覆盖 config.toml 的 model_reasoning_effort（本机 Codex 已
    接受该配置）。effort 归一化后为受限枚举，无 shell 元字符。
    """
    if resume_argv:
        # P4：codex 精确 resume——codex -C dir resume <id>；mode/model/effort/prompt
        # 由原会话承载（恢复上下文另行 send-keys 注入，见 _recover_and_send）。
        return [agent_cmd, "-C", team_dir] + list(resume_argv)
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


def _record_leader_task_start(team: dict, task: str, context: str = "", *, team_name: str = "") -> None:
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

    # ---- leader_checkpoint：新任务重置为结构化基线；同任务重入保留已有字段 ----
    # epoch 单调递增由 _leader_checkpoint_upsert 保证；goal 与已有 checkpoint 不一致
    # 视为新任务 → 清空边界/决策/计划/分工/依赖/剩余/证据/下一步，仅保留审计头。
    existing_goal = str((team.get("leader_checkpoint") or {}).get("goal") or "").strip()
    patch: dict = {"goal": clean_task, "status": "active"}
    if existing_goal != clean_task:
        patch.update({
            "boundaries": [],
            "decisions": [],
            "plan": [],
            "assignments": {},
            "dependencies": [],
            "deadline": "",
            "remaining": [],
            "evidence": [],
            "next_actions": [],
        })
    # P4：leader 稳定 session_id 记入 checkpoint——重启/切换恢复复用同一 id
    # （"先 checkpoint 再 resume"：恢复 prompt 先渲染 checkpoint，CLI 再 --resume）。
    # P4b：codex leader 未回填真实 id 前记空（真实 id 由 _codex_session_backfill 回填）。
    if team_name:
        leader = team.get("leader") or ""
        leader_agent = _member_agent(team, team.get("members", {}).get(leader, {}))
        patch["session_id"] = _member_session_id(
            team_name, leader, _team_dir(team_name), for_agent=leader_agent,
        )
    _leader_checkpoint_upsert(team, patch, source="task_start")


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


def leader_duty_prompt() -> str:
    """Leader 职责与工作流程约束（对 leader 本人）。

    作为独立 section 注入 `_leader_system_prompt`，约束 leader 只做
    「方向盘」式规划/调度/推进：分配后调用 MCP 休眠工具静默等待，
    靠成员回报与超时唤醒驱动进度推进，收尾闭环后不再动作。
    """
    return "\n".join([
        "你是一个团队领导者（Leader Agent），核心职责是统筹全局、把控任务方向，而不是直接执行具体工作步骤。",
        "",
        "【工作流程与规则】",
        "1. 任务拆解与对齐",
        "   - 接到任务后，先将目标拆解成可执行的子任务。",
        "   - 在分配前，与所有成员完成“颗粒度对齐”（即确保成员对目标、边界、协作方式达成一致理解），并让每个人明确自己的职责与交付标准。",
        "",
        "2. 分配与 MCP 休眠",
        "   - 根据成员能力与当前任务需求，合理分配子任务，清晰说明期望结果和截止节点。",
        "   - 分配完成后，立即调用 MCP 提供的休眠工具进入休眠，最长休眠时间设置为 600 秒。",
        "   - 休眠期间，你不得执行任何操作或主动发言，但系统会在以下任一情况发生时自动唤醒你：",
        "     a) 收到任何成员的消息（尤其是“任务完成”回报）；",
        "     b) 休眠达到 600 秒超时。",
        "   - 唤醒后你立即激活，进入进度推进环节。",
        "",
        "3. 激活后的推进与介入",
        "   - 每次激活时，你需审视当前整体进度：",
        "     - 若因成员回报而激活：评估该子任务完成情况，记录结果，并判断是否还有其他子任务需要继续。",
        "     - 若因超时而激活：主动检查所有成员的任务状态，必要时向相关成员发起询问，识别是否存在阻塞或依赖问题。",
        "   - 当发现冲突、依赖阻塞、进度滞后等需要协调的情况时，你只进行决策和调度，不亲自执行具体工作。",
        "   - 如果全部子任务尚未完成，根据最新状态对剩余工作进行重新指派或微调，然后再次调用 MCP 休眠工具进入休眠（最长 600 秒），等待下一次唤醒。",
        "   - 如果全部子任务均已完成，则立即转入收尾阶段。",
        "",
        "4. 收尾与闭环",
        "   - 汇总所有成员的输出成果，对照最初目标进行验证。",
        "   - 确认目标达成后，进行最终交付或输出总结结论。",
        "   - 形成完整的任务闭环，此后不再主动休眠或执行任何与该任务相关的操作。",
        "",
        "【核心原则】",
        "你是任务的“方向盘”，不是“发动机”。你的价值体现在规划、调度和推进，而不是亲自下场。MCP 休眠工具是你管理节奏的手段，等待回报与超时检查是你掌控进度的方式。",
    ])


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
    team_dir = _team_dir(team_name)
    share_dir = _share_dir(team_name)
    # P0：leader 首启/恢复完整上下文走 prompts/leader.ts leaderInitialContext
    # （@channel initial，user 通道 send-keys/argv）——Codex leader 的 argv prompt
    # 与 Claude leader 的 send-keys 首启消息同源，不依赖 AGENTS.md 是否可写（团队
    # workspace == 项目根时 ensure_codex_agents_md fail-closed，argv 是唯一载体）。
    # 渲染失败回退内建 Python 内联文本（A4：不静默丢身份、不输出空串）。
    vars_ = {
        "teamName": team_name,
        "leaderMemberName": leader or "(未设置)",
        "leaderRole": leader_role,
        "leaderAgent": leader_agent,
        "defaultAgent": _default_member_agent(team),
        "teammates": "; ".join(teammates) if teammates else "暂无。",
        "task": task or "",
        "teamDir": team_dir,
        "shareDir": share_dir,
        "recoverySection": "\n".join(build_leader_recovery_section(
            team_name, team, team_dir, share_dir)),
    }
    text = prompt_registry.render_channel("leader", "leaderInitialContext", vars_, team_name)
    if text is not None:
        return text
    lines = [
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的 leader。",
        f"你的团队成员身份: member_name='{leader or '(未设置)'}', role='{leader_role}', agent='{leader_agent}'。",
        f"leader_list_team 中名为 '{leader or '(未设置)'}' 且标记为 leader 的成员记录就是你本人，不是外部成员。",
        "**注意** 不要把自己的 leader 成员记录当作可分配对象；不要向自己分配子任务，也不要为了排除自己而剔除 leader 身份。",
        f"创建新成员时默认必须使用团队 default_agent='{_default_member_agent(team)}'；不要把你自己的 agent='{leader_agent}' 当作新成员默认 agent。",
        "只有用户明确要求覆盖 agent 时，才在 add_member/leader_add_member 中设置 use_explicit_agent=True。",
        "必须使用本项目 MCP 工具协调已有团队成员，不要使用 Codex 内置 spawn_agent / sub-agent 代替团队成员。",
        "开始后先调用 leader_list_team 查看成员，再用 leader_select_task_members 分析需要参与的角色。",
        "分配任务优先使用 leader_assign_task_to_relevant 或 leader_broadcast_to_relevant；只有确需全员同步时才使用 leader_broadcast。",
        "讨论/分析类任务使用 leader_start_discussion 强制开启讨论模式，并用 leader_discussion_next_round 收敛，最多 3 轮。",
        "监控成员完成情况优先用 leader_check_member_status（纯数据层，零终端读取）；阅读成员产出用 member_read_shared 或 member_read_file 读共享上下文 member_contexts/ 下的压缩上下文，不要轮询 leader_read_member_terminal（终端 dump 最耗 token）。",
        f"团队共享工作目录: {team_dir}",
        f"团队共享上下文区: {share_dir}",
    ]
    lines.extend(["", leader_duty_prompt()])
    if teammates:
        lines.append("")
        lines.append("已有可分配成员（不包含你）: " + "; ".join(teammates))
    else:
        lines.append("")
        lines.append("已有可分配成员（不包含你）: 暂无。")
    if task.strip():
        lines.extend(["", "总任务:", task.strip()])
    lines.extend(build_leader_recovery_section(team_name, team, team_dir, share_dir))
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
    mode: str = "",
) -> str:
    """为团队的 Claude Code 成员预配置权限策略。

    写入 .claude/settings.json 以减少成员首次执行 Edit/Write/Bash 时的审批阻塞。

    Args:
        team_name: 团队名称
        dangerously_skip: 跳过所有权限检查（生产环境中慎用）
        allow_patterns: 额外允许的工具模式列表，如 ["Bash(git:*)", "Edit(*.py)"]
        additional_dirs: 额外允许访问的目录列表
        mode: 成员模式（auto/plan/manual 语义）。经
            ``claude_native_permission_mode`` 转成 Claude 原生模式后仅当 ∈
            {plan, auto}（成员 plan / planning / readonly）才追加分类器 fallback
            精选 allow（见 common/classifier_fallback）。成员 auto → 原生
            acceptEdits，**实证不调用分类器** → 非目标 → 追加空 → settings 与
            既有完全一致（fallback 不外溢）。manual / default / "" 同样非目标。
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
        # F1（2026-08-12）：基座已收敛为精选安全 Bash pattern（含 git:，不再硬编码），
        # 这里只补 scoped Edit(ws/*)，避免与基座重复（去重见下）。
        allow.extend([
            f"Edit({team_dir}/*)",
            *CLAUDE_BASH_EDIT_ALLOW_PATTERNS,
        ])
        if additional_dirs:
            for d in additional_dirs:
                allow.append(f"Edit({d}/*)")
        # 分类器 fallback：仅**映射到原生 plan** 的模式（成员 plan/planning/
        # readonly）追加精选安全 allow。成员 auto → 原生 acceptEdits（实证不调用
        # 分类器）→ 非目标，不外溢。classifier_fallback_allow_patterns 内部自行
        # 做 claude_native_permission_mode 转换。
        allow.extend(classifier_fallback.classifier_fallback_allow_patterns(team_dir, mode))
        # 去重（保序）：F1 后基座已含精选安全 Bash，plan fallback 追加的内容可能
        # 与基座重复；去重保持 settings 确定、干净（重复规则在 Claude 端无额外效果）。
        permissions_config["allow"] = list(dict.fromkeys(allow))

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
# direct leader 待收回报提醒（MCP 装饰器层旁路）
# ============================================================
# leader 可能是 Claude Code 或 Codex（或任意 MCP 客户端），所以提醒搭
# 便车在 @mcp.tool 装饰器层，对任何客户端一视同仁，而非在 66 个工具
# 体内逐个追加。触发：direct leader（非 tmux）+ 有 leader_pending_reports
# + 达到节流条件（条数 >= DIRECT_LEADER_NUDGE_MIN_COUNT 或最老一条距今
# >= DIRECT_LEADER_NUDGE_MAX_AGE_SECONDS，任一成立）。排除：leader_activate
# （调用即消费清空 pending，追加自相矛盾）、leader_get_recovery_context
# （已通过 build_leader_pending_reports_section 渲染完整清单，重复即噪音）、
# 非 str 返回值、取不到 team_name。提示是旁路装饰：任何异常都原样返回
# result，绝不让工具本身失败。

DIRECT_LEADER_NUDGE_MIN_COUNT = 3
DIRECT_LEADER_NUDGE_MAX_AGE_SECONDS = 300
_NUDGE_EXCLUDED_TOOLS = frozenset({"leader_activate", "leader_get_recovery_context"})
# 查询/查进度类工具豁免节流：leader 主动调它们本身就表明在等结果，
# 哪怕只有 1 条 pending 也应告知。判据是"调用意图明显在查进度"——
# leader_check_member_status / leader_list_team / leader_monitor_members
# 是直接看成员任务状态，leader_read_member_terminal 是深度排查进度，
# terminal_status / member_terminal_status 是看终端存活，member_read_shared
# 是直接读共享区最新结果。派单/配置类不放进来（那会成为噪音）。
_NUDGE_ALWAYS_TOOLS = frozenset({
    "leader_check_member_status",
    "leader_list_team",
    "leader_monitor_members",
    "leader_read_member_terminal",
    "member_terminal_status",
    "terminal_status",
    "member_read_shared",
})


def _fmt_waited(seconds: int) -> str:
    """把秒数格式化为紧凑人话时长（"X小时Y分"/"X分Y秒"/"X秒"）。"""
    if seconds >= 3600:
        return f"{seconds // 3600}小时{(seconds % 3600) // 60}分"
    if seconds >= 60:
        return f"{seconds // 60}分{seconds % 60}秒"
    return f"{seconds}秒"


def _pending_reports_nudge(team_name: str, always: bool = False) -> str:
    """direct leader 待收回报提醒串；无待收或未达节流条件时返回 ""。

    always=True 时跳过条数/时长节流判断（查询类工具豁免），只要
    pending 非空就提醒。纯函数：只读数据、不修改。任何异常都返回 ""
    （提示是旁路，绝不能让工具本身失败）。
    """
    try:
        import datetime

        team = _load().get("teams", {}).get(team_name)
        if not team:
            return ""
        if team.get("leader_type") == "tmux":
            return ""  # tmux leader 走终端注入，不该重复打扰
        reports = pending_leader_reports(team)
        if not reports:
            return ""
        now = datetime.datetime.now()
        oldest_ts = None
        for report in reports:
            ts = report.get("timestamp") or ""
            try:
                parsed = datetime.datetime.fromisoformat(ts)
            except (ValueError, TypeError):
                continue
            if oldest_ts is None or parsed < oldest_ts:
                oldest_ts = parsed
        if oldest_ts is not None:
            age_seconds = max(0, int((now - oldest_ts).total_seconds()))
        else:
            age_seconds = 0
        if (not always and len(reports) < DIRECT_LEADER_NUDGE_MIN_COUNT
                and age_seconds < DIRECT_LEADER_NUDGE_MAX_AGE_SECONDS):
            return ""
        monitor_count = sum(
            1 for r in reports if r.get("event") == MONITOR_INFERRED_EVENT
        )
        monitor_text = f"（含 {monitor_count} 条 monitor 推断）" if monitor_count else ""
        return (
            f"\n\n⏰ 待收回报提醒：有 {len(reports)} 条成员回报待确认"
            f"{monitor_text}，最老一条已等 {_fmt_waited(age_seconds)}。"
            f"请调用 leader_activate('{team_name}') 查看并确认。"
        )
    except Exception:
        return ""


_mcp_tool_orig = mcp.tool


def _mcp_tool_with_nudge(func):
    """包装 FastMCP.tool：工具返回字符串时按条件追加待收回报提醒。

    functools.wraps 必须保留 __name__/__doc__ —— FastMCP 靠它们生成
    工具名与描述。仅当 result 是 str 且团队满足触发条件时才追加；
    其余情况（非 str、无 team_name、排除名单、任何异常）原样返回。
    """
    # 装饰时取一次首参名并闭包捕获：args[0] 回退只在首参确实名为
    # team_name 时成立，避免把任意位置值误当团队名；签名解析失败
    # 保守处理（不回退，只认 kwargs）。不在每次调用时算——66 个工具
    # × 每次调用的无谓开销。
    try:
        first_param_is_team_name = (
            next(iter(inspect.signature(func).parameters), None) == "team_name"
        )
    except (ValueError, TypeError):
        first_param_is_team_name = False

    @functools.wraps(func)
    def _wrapped(*args, **kwargs):
        result = func(*args, **kwargs)
        if not isinstance(result, str):
            return result
        try:
            if func.__name__ in _NUDGE_EXCLUDED_TOOLS:
                return result
            team_name = kwargs.get("team_name")
            if not team_name and first_param_is_team_name and args:
                team_name = args[0]
            if not team_name:
                return result
            nudge = _pending_reports_nudge(
                str(team_name), always=func.__name__ in _NUDGE_ALWAYS_TOOLS)
            return result + nudge if nudge else result
        except Exception:
            return result

    return _mcp_tool_orig(_wrapped)


mcp.tool = _mcp_tool_with_nudge


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
    - 如果已有受管 tmux leader 且终端存活: 同名 claim 保持 tmux 语义，不覆盖为 direct
    - 如果已有 tmux leader 且终端存活但非受管: 降级为普通成员，当前会话接管
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

        # 【受管 tmux leader 保持语义】同名 claim 不得把受管且存活的 tmux leader
        # 覆盖为 direct（元数据撕裂：leader_type=direct 但 leader 仍指向带活窗口
        # 的成员名）。真正外部接管仅在旧 leader 终端已关闭 / 非受管时发生。
        if claim_keeps_tmux_leader(
            team, session_alive=session_alive, window_alive=window_alive
        ):
            return (
                f"✅ 受管 tmux leader '{old_leader}' 终端存活，同名 claim 保持 tmux 语义，未覆盖为 direct。\n\n"
                "   如需外部接管：请先关闭该 leader 终端（或对其 unclaim_leader），再 claim_leader。"
            )

        if window_alive:
            if old_leader in team.get("members", {}):
                team["members"][old_leader]["role"] = "member"
                lines.append(f"🔄 原 tmux leader '{old_leader}' 终端存活，已降级为普通成员（窗口保留）。")
            else:
                lines.append(f"🔄 原 tmux leader '{old_leader}' 终端存活，直接接管（非受管成员记录）。")
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
        _record_leader_task_start(team, task, team_name=team_name)
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

        _merge_spawned_session_ids(data, team_name, [n for n, _a in created])
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
    # P4：leader 首启也绑定稳定 session_id（换号/重启恢复复用同一 id）。
    # 走原始 _tmux(new-session) 旁路（未收敛到 _tmux_spawn_member），故在此显式接线。
    # P4b：spawn 前先刷新 codex leader 首启回填（有标记未回填时 discover 真实 id）。
    _codex_session_backfill(team_name, leader)
    leader_spawn_ts = time.time()
    leader_resume_plan = _session_resume_plan(team_name, leader, leader_agent, team_dir)
    leader_resume_argv = leader_resume_plan["argv"] if leader_resume_plan else None
    if _is_codex(leader_agent):
        # Codex leader 同样固化身份到 AGENTS.md（角色中立段）
        prompt_registry.ensure_codex_agents_md(team_name, team_dir)
        proxy_prefix = get_proxy_env_prefix(team_name, leader)
        agent_user_prefix = get_agent_user_env_prefix(team_name, leader, leader_atype)
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            *agent_user_prefix,
            *proxy_prefix,
            *_codex_command(leader_agent, team_dir, leader_prompt, member_mode=leader_mode, model=leader_model, effort=leader_effort, resume_argv=leader_resume_argv),
        ])
    else:
        # G2：共享 settings 用团队 union 有效模式（任一 claude 成员映射原生 plan →
        # plan），不按 leader 单一模式写——混合团队（leader auto + member plan）的
        # plan 成员 settings 层也被覆盖，且不随 spawn 顺序翻转。每 Agent 精确豁免
        # 仍由各自 --allowedTools argv 承载（下方 _claude_agent_args）。
        _write_claude_permissions(
            team_name,
            mode=classifier_fallback.team_classifier_effective_mode(members),
        )
        proxy_prefix = get_proxy_env_prefix(team_name, leader)
        # 私有 settings 目录权限收紧失败时 fail closed，返回可见错误
        try:
            leader_au_prefix, leader_settings_path = claude_agent_user_launch(team_name, leader)
        except RuntimeError as e:
            return f"❌ 创建 leader 终端失败: {e}"
        # leader 身份进 system 层（--append-system-prompt-file）
        leader_identity_path = prompt_registry.claude_identity_file(
            team_name, leader, leader=True
        )
        rc, _, err = _tmux([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", team_dir,
            *merge_env_prefixes(leader_au_prefix, proxy_prefix),
            *_claude_agent_args(
                leader_agent,
                leader_mode,
                allowed_tools=classifier_fallback.claude_terminal_allow_tools(
                    leader_mode, team_dir, CLAUDE_LEADER_TOOL_ALLOW_PATTERNS
                ),
                model=leader_model,
                settings_path=leader_settings_path,
                effort=leader_effort,
                resume_argv=leader_resume_argv,
                append_system_prompt_file=leader_identity_path,
            ),
        ])

    if rc != 0:
        return f"❌ 创建 leader 终端失败: {err}"
    # P4b：codex leader 首启记录回填标记（spawn_ts/cwd/CODEX_HOME），真实 id 由
    # 后续 monitor / 复活刷新 discover 回填。claude leader 无需（--session-id 已绑定）。
    _record_session_backfill_marker(team_name, leader, spawn_ts=leader_spawn_ts)
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

    _merge_spawned_session_ids(data, team_name, [n for n, _i, _tag in created])
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

    # 硬门：HIGH 漂移未确认时拒绝分配（drift 不只是 prompt 提示，而是硬性闸门）
    gate_err = _checkpoint_gate_block(team, team_name)
    if gate_err:
        return gate_err

    # 持久化任务（恢复时自动重发）—— 原子路径：member 任务字段 + checkpoint 分工
    # 都在 _update_team_data 锁内 read-modify-write，关闭与并发回报/分配的 TOCTOU。
    full_msg, compact_context = _build_member_task_payload(subtask, context)
    mode_prefix = _mode_task_prefix(members[member_name])
    if mode_prefix:
        full_msg = mode_prefix + full_msg

    def _persist_assignment(latest_team: dict) -> dict:
        latest_members = latest_team.get("members", {})
        m = latest_members.get(member_name)
        if not m:
            return {"ok": False}
        m["last_task"] = subtask
        m["last_context"] = compact_context
        m["last_task_completed"] = False
        m.pop("compact_sent", None)  # 新任务重置，允许下一次 /compact
        _record_leader_checkpoint_assignment(latest_team, member_name, subtask)
        _touch_leader_activity(latest_team)
        return {"ok": True}

    persist_result = _update_team_data(team_name, _persist_assignment)
    if persist_result is None or not persist_result.get("ok"):
        return f"❌ 持久化任务失败（成员 '{member_name}' 可能已被移除）。"

    session = _find_any_session(team_name)
    if not session:
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

    # 硬门（P0 task1 门语义）：HIGH 漂移未确认时不再"整批 return 拒绝"——
    # 改为一律入队 member_outbox 并 held(checkpoint_gate)，leader_ack_checkpoint
    # 放行后由巡检/leader_flush_outbox 自动投递。drift 保护保留（消息不会在
    # 漂移未确认时被送出），但批量广播不再因单个硬门整批失败或要求手工逐个。
    targets = [
        name for name in members
        if not ((ltype == "tmux" and name == leader) or _is_direct_leader_member(team, name))
    ]
    if not targets:
        return "⚠️ 没有可广播的成员终端。"
    gate_err = _checkpoint_gate_block(team, team_name)
    if gate_err:
        result = _enqueue_outbox_messages(
            team_name, targets, message, kind="broadcast", held_reason="checkpoint_gate"
        )
        enqueued = result.get("enqueued") or []
        rejected = result.get("rejected") or []
        lines = [
            f"⏸ 广播已入队延后投递 {len(enqueued)}/{len(targets)} 人（HIGH 漂移未确认，"
            f"消息被 gate 保持，不会在漂移下送出）。",
            "   请调用 leader_ack_checkpoint 确认方向后，由系统自动投递，"
            "或 leader_flush_outbox 立即投递。无需逐个 member_send_message。",
        ]
        if rejected:
            lines.append(f"   ⚠️ 拒绝: {'; '.join(rejected)}（队列满或重复 id）")
        return "\n".join(lines)

    recovered = []
    results = []
    for name in targets:
        # 自动恢复死掉的成员窗口
        member_target = _member_window_target(team_name, name)
        if not member_target:
            extra_message = f"{_mode_task_prefix(members[name])}{message}\n{_member_report_first_rule()}"
            ok, err_msg = _recover_and_send(team_name, name, session, extra_message=extra_message)
            if ok:
                recovered.append(name)
                results.append(f"  ✅ {name} (已恢复+广播)")
            else:
                results.append(f"  ❌ {name} (恢复失败: {err_msg})")
            time.sleep(0.3)
            continue

        full_msg = f"{_mode_task_prefix(members[name])}{message}\n{_member_report_first_rule()}"
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
def leader_batch_ack(team_name: str, message: str, member_names: str = "") -> str:
    """
    [Leader] 批量发送确认/ACK 消息给所有（或指定）成员，自动并行投递。

    ⚠️ 命名澄清（与 checkpoint ACK 区分）：本工具是"向成员批量发送消息
    （消息级 ACK/通知）"，与 leader_ack_checkpoint（确认 checkpoint 方向的
    硬门放行）是两回事。本工具不受硬门拦截；checkpoint 方向确认仍走
    leader_ack_checkpoint。

    P0 task1：一次调用为每个目标成员入队一条消息（member_outbox），由系统自动
    并行投递，无需逐个 member_send_message。ACK/通知语义，不受 checkpoint 硬门
    拦截（不会因 HIGH drift 整批失败）；批量通道有界、幂等、可观测，无静默丢消息。

    Args:
        team_name: 团队名称
        message: 要发送的 ACK/确认内容
        member_names: 可选，逗号分隔的目标成员；为空=全部非 leader 成员
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    if not team.get("terminals_active"):
        return f"❌ 终端未启动。"

    members = team.get("members", {})
    missing: list[str] = []
    if member_names.strip():
        raw = [n.strip() for n in member_names.split(",") if n.strip()]
        targets = []
        for n in raw:
            if n not in members:
                missing.append(n)
            elif not _is_leader_member(team, n):
                targets.append(n)
    else:
        targets = [
            n for n in members
            if not _is_leader_member(team, n)
        ]
    if not targets:
        return "⚠️ 没有可发送的目标成员。"

    result = _enqueue_outbox_messages(team_name, targets, message, kind="ack")
    enqueued = result.get("enqueued") or []
    message_ids = result.get("message_ids") or {}
    rejected = result.get("rejected") or []

    # 立即同步推进一次（尽快送达；失败由巡检路径自动重试兜底）。
    advance = _advance_member_outbox_once(team_name)

    lines = [
        f"✅ 批量 ACK 已入队 {len(enqueued)}/{len(targets)} 人，自动投递。",
        f"   message_ids: {', '.join(f'{n}={message_ids.get(n)}' for n in enqueued) or '(空)'}",
    ]
    if advance.get("delivered"):
        lines.append(f"   已送达: {', '.join(advance['delivered'])}")
    if advance.get("retrying"):
        lines.append(f"   投递中/将重试: {', '.join(advance['retrying'])}")
    if advance.get("failed"):
        lines.append(f"   ❌ 投递失败: {', '.join(advance['failed'])}（详见 leader_outbox_status）")
    if rejected:
        lines.append(f"   ⚠️ 拒绝: {'; '.join(rejected)}（队列满或重复 id，无静默丢弃）")
    if missing:
        lines.append(f"   ⚠️ 未知成员: {', '.join(missing)}")
    lines.append("   可调用 leader_outbox_status 查看队列状态。")
    return "\n".join(lines)


@mcp.tool
def leader_outbox_status(team_name: str) -> str:
    """
    [Leader] 查看批量消息队列（member_outbox）状态，可观测性。

    返回逐成员 queued/sending/delivered/failed 计数、gate-held 标记、队列最早/
    最新条目时间；失败消息含 retries/last_error（无静默丢消息）。

    Args:
        team_name: 团队名称
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    return "\n".join(_build_outbox_status(team))


@mcp.tool
def leader_flush_outbox(team_name: str) -> str:
    """
    [Leader] 手动触发一次批量消息队列（member_outbox）投递推进。

    正常情况下巡检路径会自动推进；本工具供 leader 在 ACK 硬门放行后立即投递
    gate-held 的批量消息，无需等待下一次巡检。

    Args:
        team_name: 团队名称
    """
    advance = _advance_member_outbox_once(team_name)
    lines = [
        "📨 outbox 推进完成。",
    ]
    if advance.get("delivered"):
        lines.append(f"   已送达: {', '.join(advance['delivered'])}")
    if advance.get("retrying"):
        lines.append(f"   将重试: {', '.join(advance['retrying'])}")
    if advance.get("failed"):
        lines.append(f"   ❌ 失败: {', '.join(advance['failed'])}（重试超限）")
    if advance.get("held"):
        lines.append(f"   ⏸ 仍被 gate 保持: {', '.join(advance['held'])}（请先 leader_ack_checkpoint）")
    if not any(advance.values()):
        lines.append("   （队列为空或无需推进）")
    return "\n".join(lines)


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

    # 硬门（P0 task1 门语义，同 leader_broadcast）：HIGH 漂移未确认时不再
    # "整批 return 拒绝"，改为入队 member_outbox 并 held(checkpoint_gate)，
    # leader_ack_checkpoint 放行后自动投递；drift 保护保留。
    gate_err = _checkpoint_gate_block(team, team_name)
    if gate_err:
        result = _enqueue_outbox_messages(
            team_name, targets, message, kind="broadcast", held_reason="checkpoint_gate"
        )
        enqueued = result.get("enqueued") or []
        rejected = result.get("rejected") or []
        lines = [
            f"⏸ 定向广播已入队延后投递 {len(enqueued)}/{len(targets)} 人"
            "（HIGH 漂移未确认，消息被 gate 保持）。",
            "   请调用 leader_ack_checkpoint 确认方向后自动投递，"
            "或 leader_flush_outbox 立即投递。",
        ]
        if rejected:
            lines.append(f"   ⚠️ 拒绝: {'; '.join(rejected)}（队列满或重复 id）")
        return "\n".join(lines)

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

    # 硬门：HIGH 漂移未确认时拒绝按角色自动分配（防止漂移下盲目重派）
    gate_err = _checkpoint_gate_block(team, team_name)
    if gate_err:
        return gate_err

    targets = selection.get("selected", [])
    if not targets:
        return "⚠️ 未选择相关成员，未分配任务。请传 required_roles 或使用 leader_assign_subtask 显式指定成员。"

    payload_task = subtask.strip() or task
    reason = f"由 leader_assign_task_to_relevant 根据任务选择: {_compact_text(task, 240)}"
    payload, compact_context = _build_member_task_payload(payload_task, reason=reason)
    sent, failures = _send_message_to_members(team_name, team, targets, payload)

    # 原子持久化：member 任务字段 + checkpoint 分工在 _update_team_data 锁内
    # read-modify-write，关闭与并发回报/分配的 TOCTOU（同 leader_assign_subtask）。
    def _persist_sent(latest_team: dict) -> dict:
        latest_members = latest_team.get("members", {})
        persisted = []
        for name in sent:
            member = latest_members.get(name)
            if not member:
                continue
            member["last_task"] = payload_task
            member["last_context"] = compact_context or reason
            member["last_task_completed"] = False
            member.pop("compact_sent", None)  # 新任务重置，允许下一次 /compact
            _record_leader_checkpoint_assignment(latest_team, name, payload_task)
            persisted.append(name)
        _touch_leader_activity(latest_team)
        return {"persisted": persisted}

    persist_result = _update_team_data(team_name, _persist_sent)
    if persist_result is None:
        return f"❌ 团队 '{team_name}' 不存在。"

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

        # P1/S4 可见性：数据层暴露"已回报/未收到回报"事实（含 event 区分），
        # leader 判定只用数据层，不依赖成员对话窗/终端残留（终端残留不影响事实状态）。
        report_ts = (member.get("last_report_ts") or "")[:16]
        report_summary = _compact_text(member.get("last_report_summary") or "", 50)
        report_task = member.get("last_report_task") or ""
        cur_task = (member.get("last_task") or "").strip()
        report_event = member.get("last_report_event") or ""
        if report_ts and report_task == cur_task:
            origin = "⚠️[monitor 推断]" if report_event == MONITOR_INFERRED_EVENT else ""
            report_text = f"已回报 {report_ts} {origin}" + (f"：{report_summary}" if report_summary else "")
        elif report_ts:
            report_text = f"上次回报 {report_ts}（对应旧任务，当前任务未回报）"
        else:
            report_text = "未收到回报"

        lines = [
            f"• **{name}** [{state}] {status_text}",
            f"   状态检查: {ts or '—'}",
            f"   回报: {report_text}",
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
    # P1 消费可见性：巡检后提示待处理回报数，Codex leader 知道该 leader_activate
    # 消费（ACK）；不在此消费——activate 是唯一消费点，避免误清。
    pending = pending_leader_reports(team)
    if pending:
        undelivered = [r for r in pending if not r.get("delivered")]
        suffix = f"（含 {len(undelivered)} 条未投递）" if undelivered else ""
        lines_out.append(
            f"\n📥 待处理成员回报 {len(pending)} 条{suffix} → "
            f"用 leader_activate('{team_name}') 查看确认（会清空）。"
        )
    summary = " ".join(f"{k}:{v}" for k, v in sorted(counts.items())) or "无成员"
    lines_out.append(f"\n📊 {summary}")
    return "\n".join(lines_out)


@mcp.tool
def leader_checkpoint_set(
    team_name: str,
    goal: str = "",
    boundaries: str = "",
    decisions: str = "",
    plan: str = "",
    dependencies: str = "",
    deadline: str = "",
    remaining: str = "",
    next_actions: str = "",
    expected_epoch: int = 0,
) -> str:
    """
    [Leader] 写入/更新团队 leader_checkpoint 的结构化字段（单调 epoch 递增）。

    这是 leader 跨重启/切换承接总体方向的主要写入点：目标/边界/决策/计划/
    依赖/截止/剩余/下一步按需提供，多值字段（boundaries/decisions/plan/
    dependencies/remaining/next_actions）支持换行分隔（每行一项）。
    assignments/evidence 由系统写入点（分配/回报/完成）自动维护，不在此设置。

    幂等与旧 epoch 防护：epoch 每次写入单调 +1。传 expected_epoch 时若与
    当前 epoch 不一致则拒绝写入并返回当前 epoch——用于"基于快照再写"场景，
    防止旧上下文覆盖新 checkpoint。

    Args:
        team_name: 团队名称
        goal: 总体目标
        boundaries: 边界（换行分隔）
        decisions: 已决策（换行分隔）
        plan: 执行计划（换行分隔）
        dependencies: 依赖（换行分隔）
        deadline: 截止时间/要求
        remaining: 剩余工作（换行分隔）
        next_actions: 下一步动作（换行分隔）
        expected_epoch: 可选乐观锁基准 epoch；>0 时校验，不匹配则拒绝
    """
    patch: dict = {}
    if (goal or "").strip():
        patch["goal"] = goal.strip()
    multi_fields = {
        "boundaries": boundaries,
        "decisions": decisions,
        "plan": plan,
        "dependencies": dependencies,
        "remaining": remaining,
        "next_actions": next_actions,
    }
    for key, raw in multi_fields.items():
        items = _checkpoint_split_lines(raw)
        if items:
            patch[key] = items
    if (deadline or "").strip():
        patch["deadline"] = deadline.strip()
    if not patch:
        return "⚠️ 未提供任何结构化字段（goal/boundaries/decisions/plan/dependencies/deadline/remaining/next_actions）。"

    result = _update_leader_checkpoint(
        team_name,
        patch,
        source="leader_checkpoint_set",
        updated_by="leader",
        expected_epoch=int(expected_epoch) if expected_epoch > 0 else None,
    )
    if result.get("rejected"):
        if result.get("error"):
            return f"❌ {result['error']}"
        return (
            f"⚠️ 旧 epoch 拒绝：当前 epoch={result.get('epoch')}，期望 {result.get('expected_epoch')}。"
            "请先基于最新 checkpoint 重试（leader_get_recovery_context 查看当前状态）。"
        )
    cp = _load().get("teams", {}).get(team_name, {}).get("leader_checkpoint") or {}
    return (
        f"✅ leader_checkpoint 已更新（epoch={cp.get('epoch')}, version={cp.get('version')}, "
        f"source=leader_checkpoint_set）。"
    )


@mcp.tool
def leader_ack_checkpoint(team_name: str, ack_epoch: int = 0) -> str:
    """
    [Leader] 确认当前 leader_checkpoint 方向（硬门放行）。

    当检测到 HIGH 漂移（checkpoint.goal 与 leader_last_task 冲突 / 分工与
    成员 last_task 矛盾）时，分配/广播会被硬门拒绝；调用本工具确认当前
    方向后放行。记录 leader_checkpoint_ack = {epoch, acked_ts, acked_by}，
    epoch 必须等于当前 checkpoint.epoch 才算"已确认"。

    ack_epoch 默认 0 = 确认当前最新 checkpoint（无需手填数字）；也可显式传
    旧 epoch 校验——若与当前不一致返回提示（旧确认不覆盖新状态）。

    Args:
        team_name: 团队名称
        ack_epoch: 要确认的 checkpoint epoch（默认 0 = 当前最新）
    """
    import datetime

    def updater(latest_team: dict) -> dict:
        cp = latest_team.get("leader_checkpoint")
        cur_epoch = checkpoint_epoch(cp) if isinstance(cp, dict) else 0
        target = int(ack_epoch) if ack_epoch > 0 else cur_epoch
        if target != cur_epoch:
            return {"acked": False, "cur_epoch": cur_epoch, "target": target}
        latest_team["leader_checkpoint_ack"] = {
            "epoch": cur_epoch,
            "acked_ts": datetime.datetime.now().isoformat(),
            "acked_by": latest_team.get("leader") or "leader",
        }
        return {"acked": True, "epoch": cur_epoch}

    result = _update_team_data(team_name, updater)
    if result is None:
        return f"❌ 团队 '{team_name}' 不存在。"
    if not result.get("acked"):
        return (
            f"⚠️ 未确认：当前 checkpoint epoch={result.get('cur_epoch')}，"
            f"目标 ack_epoch={result.get('target')} 已过期。请基于最新状态重试（ack_epoch 留空默认确认最新）。"
        )
    # P0 task1：ACK 放行后立即推进一次 member_outbox——gate-held 的批量广播
    # 消息自动投递，不依赖 leader 人工再次调用。
    advance = _advance_member_outbox_once(team_name)
    extra = ""
    if advance.get("delivered"):
        extra = f" 已自动投递 outbox: {', '.join(advance['delivered'])}"
    return (
        f"✅ 已确认 leader_checkpoint (epoch={result.get('epoch')})，"
        f"分配/广播硬门已放行。{extra}"
    )


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
        latest_team.pop("leader_sleep_until", None)
        reports = pending_leader_reports(latest_team)
        latest_team["leader_pending_reports"] = []
        # P1 ACK 证据：持久化本次消费的 report_id 清单，供验收/审计证明 leader
        # 已 ACK（消费），且不依赖成员对话窗/终端残留。
        latest_team["leader_last_ack"] = {
            "ts": now,
            "count": len(reports),
            "report_ids": [r.get("report_id") or "" for r in reports],
        }
        # S3/S4 语义：pending 非空=leader 有待 ACK 的回报（active）；activate 消费
        # 清空后若再无未完成工作，leader_work_state 归 idle（team 可进待机）。
        if not leader_has_unfinished_work(latest_team):
            latest_team["leader_work_state"] = "idle"
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
    # 恢复时优先渲染 leader_checkpoint；检测到明显漂移时禁止自动再分配。
    lines.extend(build_leader_checkpoint_section(team))
    lines.extend(build_leader_checkpoint_drift_section(team))

    if reports:
        lines.append(f"\n📥 成员回报 {len(reports)} 条(已确认):")
        for i, report in enumerate(reports, 1):
            member = report.get("member") or "unknown"
            result = _compact_text(report.get("result") or "", 200)
            ts = (report.get("timestamp") or "")[:19]
            line = f"  {i}. [{ts}] {report_origin_prefix(report)}{member}: {result}"
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


# =====================================================================
# leader_sleep 的"延时等待"实现（2026-08-16）
# ---------------------------------------------------------------------
# 语义裁定（与旧实现的根本区别）：leader_sleep 是**延时等待**——工具调用本身
# 就是那段等待，阻塞到有事发生或到点后带摘要返回，agent 在**同一回合**继续。
# 旧实现是"打个标记 + 要求 agent 立刻结束回合，等系统往终端注入唤醒"，一旦
# 注入链路失效（codex 终端识别缺陷即是），leader 就永远醒不过来 = 真休眠。
#
# 注入兜底不拆：仍然置 leader_state=resting + leader_sleep_until，万一客户端
# 中断了这次工具调用，巡检照样会在回报/授权/超时时注入唤醒。
#
# 切片：MCP 客户端对单次工具调用有超时（分钟级），所以一次阻塞不超过
# LEADER_SLEEP_MAX_BLOCK_SECONDS；未到 max_seconds 就返回"继续等待"提示由
# agent 再调一次。剩余时长由 leader_sleep_until 记账，切片不会拉长总等待。
LEADER_SLEEP_MAX_BLOCK_SECONDS = 240
LEADER_SLEEP_POLL_SECONDS = 1.0


def _leader_sleep_block_ceiling(team: dict) -> float:
    """单次阻塞上限（秒）：团队级 ``leader_sleep_block_seconds`` 覆盖模块默认。

    留成可配置量而不是写死常量，一是不同 MCP 客户端的工具调用超时不一样，
    二是给测试一个不靠 sleep 真等的确定性缝（置 0 即"求值一次事件后立刻按
    切片返回"，事件判定路径与生产完全一致）。
    """
    raw = team.get("leader_sleep_block_seconds", LEADER_SLEEP_MAX_BLOCK_SECONDS)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float(LEADER_SLEEP_MAX_BLOCK_SECONDS)
    return max(0.0, min(value, 3600.0))


def _team_has_active_member_tasks(team: dict) -> bool:
    """是否还有非 leader 成员挂着未完成任务。"""
    leader = team.get("leader", "")
    return any(
        _member_has_active_task(member)
        for name, member in (team.get("members") or {}).items()
        if name != leader
    )


def _members_blocked_on_approval(team: dict) -> list[str]:
    """从**持久化状态**读出真正卡在授权、需要 leader 处理的成员。

    只认 last_observed_state 与 blocked_reason 同时为 "approval"：monitor 自动
    授权成功后会把成员改写成 busy 并清掉 blocked_reason（见 _scan_member_terminal
    的 approval 分支），所以这条判据天然排除了"正在被自动授权"的成员，与
    _approval_members_requiring_leader 的语义一致，但零终端读取。
    """
    if not _leader_wakeup_config(team)["approval_alert"]:
        return []
    leader = team.get("leader", "")
    return [
        name
        for name, member in (team.get("members") or {}).items()
        if name != leader
        and member.get("last_observed_state") == "approval"
        and member.get("blocked_reason") == "approval"
    ]


def _leader_sleep_wait(
    team_name: str,
    *,
    until_dt,
    budget_seconds: float,
    baseline_ids: set,
    had_active: bool,
) -> dict:
    """阻塞轮询直到出现终止事件，返回 {"event": ..., ...}。

    事件优先级与 _evaluate_leader_wakeup_conditions 对齐：外部激活 > 新回报 >
    卡授权 > 全部完成 > 到点；单次阻塞上限单列（block_ceiling，非终止事件）。

    只读 `_team_info`（数据层），不做任何 tmux capture —— 每秒一次终端 dump
    既贵又会与 monitor 抢 tmux。全程不持 TEAM_DATA_LOCK。
    """
    import datetime

    ceiling_at = time.monotonic() + max(0.0, budget_seconds)
    while True:
        team = _team_info(team_name) or {}

        # a) 外部激活：注入兜底唤醒 / 别人调了 leader_activate
        if team.get("leader_state") != "resting":
            return {
                "event": "external_wakeup",
                "reason": team.get("leader_wakeup_reason") or "activated",
            }

        # b) 新回报（baseline 之外的才算，避免拿休眠前的旧回报立刻返回）
        fresh = [
            r for r in pending_leader_reports(team)
            if r.get("report_id") and r.get("report_id") not in baseline_ids
        ]
        if fresh:
            return {"event": "report", "reports": fresh}

        # c) 成员卡授权
        blocked = _members_blocked_on_approval(team)
        if blocked:
            return {"event": "approval", "members": blocked}

        # d) 全部完成 —— 必须带"曾有过工作"守卫：没派过活时全员本来就没任务，
        #    否则 leader 一睡下就立刻被"全部完成"叫醒，等待形同虚设。
        if had_active and not _team_has_active_member_tasks(team):
            return {"event": "all_done"}

        # e) 到达 max_seconds（真正的超时）
        if datetime.datetime.now() >= until_dt:
            return {"event": "deadline"}

        # f) 到达单次阻塞上限（切片，非终止）
        if time.monotonic() >= ceiling_at:
            return {"event": "block_ceiling"}

        time.sleep(LEADER_SLEEP_POLL_SECONDS)


def _leader_sleep_block(team_name: str, *, until_iso: str, max_seconds: int) -> str:
    """执行一段延时等待并渲染返回文案（leader_sleep 的主体）。"""
    import datetime

    team = _team_info(team_name) or {}
    leader_type = team.get("leader_type", "")
    baseline_ids = {
        r.get("report_id") for r in pending_leader_reports(team) if r.get("report_id")
    }
    had_active = _team_has_active_member_tasks(team)

    started = datetime.datetime.now()
    try:
        until_dt = datetime.datetime.fromisoformat(until_iso)
    except (TypeError, ValueError):
        until_dt = started + datetime.timedelta(seconds=max_seconds)
    budget = min(
        max(0.0, (until_dt - started).total_seconds()), _leader_sleep_block_ceiling(team)
    )

    event = _leader_sleep_wait(
        team_name,
        until_dt=until_dt,
        budget_seconds=budget,
        baseline_ids=baseline_ids,
        had_active=had_active,
    )
    kind = event["event"]

    # 累计等待时长按 leader_sleep_started_ts 计（跨切片累加，不只是本片）
    fresh_team = _team_info(team_name) or {}
    try:
        origin = datetime.datetime.fromisoformat(fresh_team.get("leader_sleep_started_ts") or "")
    except (TypeError, ValueError):
        origin = started
    waited = max(0, int((datetime.datetime.now() - origin).total_seconds()))

    # direct / 非 tmux leader：注入兜底不可用，但"等待"这件事本身已由工具完成，
    # 不再需要事后 leader_activate 才能知道发生了什么（旧实现只能靠它）。
    direct_note = (
        ""
        if leader_type == "tmux"
        else (
            f"\n（leader_type={leader_type or '未设置'}：无注入终端，兜底唤醒不可用；"
            "但本次等待已由工具本身完成，无需再调 leader_activate —— "
            "如需回看历史回报仍可调用它。）"
        )
    )

    # ---- 切片返回：不推进状态，注入兜底继续武装，让 agent 再调一次 ----
    if kind == "block_ceiling":
        def update_slice(latest_team: dict) -> dict:
            latest_team["leader_sleep_slices"] = int(latest_team.get("leader_sleep_slices", 0)) + 1
            latest_team["leader_sleep_last_slice_ts"] = datetime.datetime.now().isoformat()
            return {"ok": True}

        _update_team_data(team_name, update_slice)
        left = max(10, int((until_dt - datetime.datetime.now()).total_seconds()))
        ceiling = int(_leader_sleep_block_ceiling(team))
        return (
            f"⏳ 已等待 {waited}s / {max_seconds}s，期间无成员回报、无人卡授权、任务未全部完成。\n"
            f"（单次工具调用最长阻塞 {ceiling}s，这是客户端超时保护，不是等待结束）\n"
            f"➡️ 请**立即再次调用** leader_sleep(team_name=\"{team_name}\", max_seconds={left}) 接着等待。\n"
            "不要结束回合，也不要用 shell `sleep` / `time.sleep` / 轮询自己造延时。"
            + direct_note
        )

    # ---- 终止事件：置 active、解除休眠记账、把本次呈现的回报标为已投递 ----
    reports = event.get("reports") or []
    wake_reason = {
        "report": "report",
        "approval": "approval",
        "all_done": "all_done",
        "deadline": "timeout",
        "external_wakeup": event.get("reason") or "activated",
    }[kind]

    def update_wake(latest_team: dict) -> dict:
        latest_team["leader_state"] = "active"
        latest_team["leader_idle_streak"] = 0
        latest_team["leader_last_action"] = f"leader_sleep:{kind}"
        latest_team.pop("leader_sleep_until", None)
        latest_team.pop("leader_resting_since", None)
        if kind != "external_wakeup":
            # external_wakeup 的 reason 由唤醒方写入，不覆盖
            latest_team["leader_wakeup_reason"] = wake_reason
        if reports:
            # 本次已把回报内容直接返回给 leader，等同投递成功：标 delivered，
            # 避免巡检兜底再往终端注入同一批（leader_activate 仍是最终 ACK）。
            mark_pending_reports_delivered(
                latest_team, [r.get("report_id") for r in reports]
            )
        return {"ok": True}

    _update_team_data(team_name, update_wake)

    lines = []
    if kind == "report":
        lines.append(f"📥 等待 {waited}s 后收到 {len(reports)} 条成员回报：")
        for i, report in enumerate(reports, 1):
            member = report.get("member") or "unknown"
            ts = (report.get("timestamp") or "")[:19]
            line = f"  {i}. [{ts}] {report_origin_prefix(report)}{member}: " \
                   f"{_compact_text(report.get('result') or '', 300)}"
            if report.get("artifact_path"):
                line += f" | artifact: {report['artifact_path']}"
            lines.append(line)
    elif kind == "approval":
        lines.append(
            f"🔐 等待 {waited}s 后发现成员卡在授权提示："
            f"{', '.join(event.get('members') or []) or 'unknown'}。"
        )
        lines.append("  用 leader_authorize_member 发送授权选项，或改成员为 auto 模式。")
    elif kind == "all_done":
        lines.append(f"✅ 等待 {waited}s 后，所有成员的在办任务均已完成。")
    elif kind == "external_wakeup":
        lines.append(f"🔔 等待 {waited}s 后 leader 已被激活（{wake_reason}）。")
    else:  # deadline
        lines.append(f"⏰ 已等满 {max_seconds}s，期间没有新的成员回报或授权阻塞。")
        lines.append("  请检查成员状态，识别是否存在阻塞、超时或依赖问题。")

    lines.append("")
    lines.append("➡️ 现在**在同一回合内继续**：评估进度 → 决定继续分配、追问阻塞，或转入收尾。")
    lines.append("   仍需等待时再次调用 leader_sleep；不要用 shell `sleep` / `time.sleep` 自己造延时。")
    lines.append(
        "[token 高效] 查成员状态用 leader_check_member_status（纯数据层）；"
        "读成果用 member_read_shared；不要轮询 leader_read_member_terminal。"
    )
    return "\n".join(lines) + direct_note


@mcp.tool
def leader_sleep(team_name: str, max_seconds: int = 120) -> str:
    """
    [Leader] 延时等待：阻塞到"有成员回报 / 有人卡授权 / 全部完成 / 到点"，
    然后带着这段时间发生了什么的摘要返回。

    这是**延时等待**，不是结束回合去休眠：工具调用本身就是那段等待，返回后你
    直接在同一回合里继续处理返回内容（评估回报、检查阻塞、继续分配或收尾）。
    因此**不要**在调用后停止思考、也不要用 shell `sleep` / `time.sleep` /
    轮询自己造延时——等待由本工具完成。

    单次调用最长阻塞 240 秒（避开 MCP 客户端的工具调用超时）。若 max_seconds
    更大，返回会明确告知"已等 X/Y 秒"，你**再调一次**本工具接着等即可，剩余
    时长由系统记账，切片不会让总等待变长。

    同时仍会置 leader_state=resting 并登记休眠截止时间，保留"注入唤醒"兜底：
    万一客户端把这次工具调用中断，系统仍会在成员回报/授权/超时时向 tmux
    leader 终端注入唤醒提示。

    Args:
        team_name: 团队名称
        max_seconds: 最长等待秒数，默认 120，范围 10~3600。
    """
    import datetime

    team = _team_info(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"
    leader = team.get("leader", "")
    if not leader:
        return "❌ 团队未指定 leader，无法休眠。"

    max_seconds = max(10, min(int(max_seconds), 3600))
    now = datetime.datetime.now()
    now_iso = now.isoformat()
    until_iso = (now + datetime.timedelta(seconds=max_seconds)).isoformat()

    def update_sleep(latest_team: dict) -> dict:
        latest_team["leader_state"] = "resting"
        latest_team["leader_sleep_until"] = until_iso
        latest_team["leader_sleep_max_seconds"] = max_seconds
        latest_team["leader_sleep_started_ts"] = now_iso
        latest_team["leader_idle_streak"] = 0
        latest_team["leader_wakeup_reason"] = ""
        latest_team["leader_last_action"] = "leader_sleep"
        # 主动休眠要求唤醒闭环可用：确保 wakeup enabled + monitor 运行
        cfg = _leader_wakeup_config(latest_team)
        cfg["enabled"] = True
        latest_team["leader_wakeup_config"] = cfg
        latest_team["monitor_enabled"] = True
        return {"action": "sleep", "until": until_iso}

    result = _update_team_data(team_name, update_sleep) or {"action": "none"}
    if result.get("action") != "sleep":
        return "❌ leader 进入休眠失败。"

    latest = _load().get("teams", {}).get(team_name, {})
    if latest.get("terminals_active"):
        _start_team_monitor(team_name)

    return _leader_sleep_block(team_name, until_iso=until_iso, max_seconds=max_seconds)


def leader_sleep_continue(team_name: str, until_iso: str, max_seconds: int) -> str:
    """续等入口（内部/测试用）：不重置截止时间，直接接着阻塞剩余时长。"""
    return _leader_sleep_block(team_name, until_iso=until_iso, max_seconds=max_seconds)


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
        # leader_checkpoint 收口：标记 completed、清空剩余工作与下一步、追加完成证据
        # （无 checkpoint 的旧团队 no-op，保持向后兼容）。
        if isinstance(latest_team.get("leader_checkpoint"), dict):
            evidence = list(latest_team.get("leader_checkpoint", {}).get("evidence") or [])
            evidence.append({
                "timestamp": now,
                "member": latest_team.get("leader") or "leader",
                "event": "leader_task_completed",
                "result": (summary or "").strip() or "leader marked team task complete",
            })
            _leader_checkpoint_upsert(
                latest_team,
                {
                    "status": "completed",
                    "remaining": [],
                    "next_actions": [],
                    "evidence": evidence[-MAX_CHECKPOINT_EVIDENCE:],
                },
                source="complete",
            )
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
    report_wakeup_enabled: bool = True,
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
        report_wakeup_enabled: 成员回报是否注入唤醒 leader,默认 True;
            设为 False 即完全关闭回报注入(逃生阀),不影响 enabled 轮询开关
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
        "report_wakeup_enabled": bool(report_wakeup_enabled),
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
        # grant_autonomy 针对的是 auto 模式成员（→ Claude acceptEdits，非分类器
        # fallback 目标）；批量改写共享 settings 不携带单一 mode 上下文，显式
        # mode="" 保持不追加 fallback 精选 allow（fallback 不外溢到批量授权）。
        # plan 成员若需 fallback，由 spawn 路径（_tmux_spawn_member 传该成员
        # mode）或 leader_configure_member_permissions 落盘。
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

    # F1 后 settings writer = scoped Edit(ws/*) + 基座安全集（去重），故 +1 而非 +2
    default_rule_count = 1 + len(CLAUDE_BASH_EDIT_ALLOW_PATTERNS)
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

            # 只回写 compact_sent 到 fresh 快照（锁内原子），避免 stale 整份
            # 覆写并发成员回报（竞态 A 收尾侧：盲 _save(data) 会清掉并发 append
            # 的 pending / 完成标记，见 test_b5b_concurrent_finalize_no_pending_loss）。
            def _mark_compact_sent(latest_team: dict) -> dict:
                if is_leader:
                    latest_team["leader_compact_sent"] = now
                else:
                    if agent_name in latest_team.get("members", {}):
                        latest_team["members"][agent_name]["compact_sent"] = now
                return {"saved": True}

            _update_team_data(team_name, _mark_compact_sent)
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


def _build_member_checkpoint_section(team_name: str, member_name: str) -> list[str]:
    """渲染成员任务 checkpoint 恢复段（续跑依据），无 checkpoint 返回空列表。

    有 checkpoint 时，恢复消息携带结构化进度指针与 verify-then-continue
    续跑指令（已完成步骤不重做、产物哈希先核对再续跑），而不是只重发空白
    last_task —— 满足"有 checkpoint 不得空白重做"。读取持 TEAM_DATA_LOCK，
    与 leader 数据路径互斥；verify 契约以 checkpoint.epoch 为 expected_epoch，
    旧上下文覆盖新进度被拒（防旧上下文覆盖 P0）。

    无 checkpoint / 非法时诚实回落现状：不渲染任何 checkpoint 行（调用方仍按
    既有逻辑重发 last_task 从头做）；仅当磁盘 checkpoint 非法时给出可见降级
    提示，避免恢复进程在脏数据上盲目续跑。
    """
    cp, errors = checkpoint.load_checkpoint(
        TEAM_DATA_LOCK, team_name=team_name, member_name=member_name,
    )
    if cp is None:
        if errors:
            return [
                "",
                "⚠️ 成员任务 checkpoint 非法，已回落为空白重发: "
                + "; ".join(errors[:3]),
            ]
        return []
    lines = [
        "",
        "📌 成员任务 Checkpoint（恢复续跑依据，勿空白重做）:",
    ]
    lines.extend(checkpoint.checkpoint_to_lines(cp))
    lines.extend([
        "续跑规则(verify-then-continue):",
        f"  - 校验: 本 checkpoint 仍为最新(epoch={cp.get('epoch')}, "
        f"writer={cp.get('writer')}, task_id={cp.get('task_id')})，"
        "且产物哈希与共享工作目录核对一致;",
        "  - 继续: 从 current_step / 续跑指令推进，completed_steps 已完成步骤不重做;",
        "  - 失败(epoch 已被新进度覆盖 / 产物漂移): 以最新 checkpoint 或 leader "
        "新指令为准，不得用过期上下文覆盖新进度。",
    ])
    return lines


def _build_recovery_context(team_name: str, member_name: str, *, generation: int = 0) -> str:
    """构建成员终端恢复时的结构化上下文消息。

    静态头（[恢复通知]/团队/身份/目录/上次任务）走 prompts/members.ts
    memberRecoveryContext（@channel recovery，user 通道）权威源；动态段
    （恢复次数/generation/session/任务上下文/checkpoint/工具清单/顺序义务）
    经 recoverySection 占位注入。generation 覆盖：_quota_generation_migrate
    在 commit 前调用（数据仍是旧 generation），须显式传新窗的 next_gen 标注
    窗口身份。渲染失败回退内建 Python 内联文本（A4：不静默丢上下文）。
    """
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})

    team_dir = _team_dir(team_name)
    share_dir = _share_dir(team_name)
    role = member.get("role") or "member"
    agent = _member_agent(team, member)
    last_task = member.get("last_task", "")
    last_context = member.get("last_context", "")
    recovery_count = member.get("recovery_count", 0)
    gen = generation or _member_generation(member)

    # 动态尾段（模板静态框架之外的部分，注入 recoverySection）
    tail = [f"========== 第{recovery_count + 1}次恢复 =========="]
    # P2：标注窗口 generation（换号后新窗识别身份 + 回报门控依据）
    if gen >= 2:
        tail.append(f"当前终端窗口 generation: g{gen}（换号后的 ACTIVE 新窗口）。")
        tail.append("回报时请传 generation 参数匹配此值，供 leader 识别权威窗口（旧窗口回报会被门控拒绝）。")
    # P4：CLI 会话恢复提示（开启时成员按 --resume <id> 恢复原对话；
    # 下方 checkpoint 仍是 verify-then-continue 续跑依据，不依赖会话恢复）。
    # P4b：codex 首启未回填真实 id 前不渲染该行（只有真实 id 才可 --resume）。
    if session_resume.resume_enabled():
        sid = _member_session_id(team_name, member_name, team_dir, for_agent=agent)
        if sid:
            tail.append(f"CLI 会话 session_id: {sid}（开启时按 --resume 恢复对话）")
    if last_context:
        tail.append(f"任务上下文: {last_context}")
    # 成员任务 checkpoint：有结构化进度时恢复续跑依据优先（已完成步骤不重做），
    # 无 checkpoint 时不渲染任何行（诚实回落现状重发 last_task 从头做）。
    tail.extend(_build_member_checkpoint_section(team_name, member_name))
    tail.extend([
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
        _member_report_first_rule(),
        "",
        "💡 请基于以上上下文继续工作，或等待 leader 分配新任务。",
    ])

    vars_ = {
        "teamName": team_name,
        "memberName": member_name,
        "role": role,
        "agent": agent,
        "teamDir": team_dir,
        "shareDir": share_dir,
        "task": last_task or "",
        "recoverySection": "\n".join(tail),
    }
    text = prompt_registry.render_channel("members", "memberRecoveryContext", vars_, team_name)
    if text is not None:
        return text

    lines = [
        "=" * 50,
        f"[恢复通知] 终端恢复通知 (第{recovery_count + 1}次恢复)",
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
    if gen >= 2:
        lines.append(f"当前终端窗口 generation: g{gen}（换号后的 ACTIVE 新窗口）。")
        lines.append("回报时请传 generation 参数匹配此值，供 leader 识别权威窗口（旧窗口回报会被门控拒绝）。")
    if session_resume.resume_enabled():
        sid = _member_session_id(team_name, member_name, team_dir, for_agent=agent)
        if sid:
            lines.append(f"CLI 会话 session_id: {sid}（开启时按 --resume 恢复对话）")
    if last_task:
        lines.append(f"上次未完成任务: {last_task}")
    if last_context:
        lines.append(f"任务上下文: {last_context}")
    lines.extend(_build_member_checkpoint_section(team_name, member_name))
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
        _member_report_first_rule(),
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


def _select_failover_profile(team: dict, member: dict) -> tuple[str | None, str]:
    """选出下一个换号目标（阶段3，按池顺序），并给出失败原因。

    直接复用 common.tmux_utils.select_failover_candidate，不重复实现池遍历：
      - 成员池激活（member["agent_user_pool"] 非空）→ 只用成员池，
        耗尽也不回落团队池（用户裁定）
      - provider 过滤：候选必须能为该成员的 CLI 注入凭证，否则换过去空转
      - 返回 (None, reason)，reason ∈ pool-empty / pool-type-mismatch /
        pool-exhausted —— 调用方据此保持阻塞并区分告警，绝不静默降级

    ⚠️ 阶段3 诚实标注：会话 resume 未实现（阶段2 未做），换号后是全新会话、
    重发 last_task 从头做——调用方勿以为对话上下文被保留。
    """
    return select_failover_candidate(team, member)


def _recover_and_send(
    team_name: str,
    member_name: str,
    session: str,
    extra_message: str = "",
    reason: str = "crash",
    previous_agent_user: str = "",
) -> tuple[bool, str]:
    """统一恢复入口：重建成员终端窗口，发送恢复上下文和可选额外消息。

    流程：保存死亡快照 → 更新恢复计数 → 重建窗口 → 发送恢复上下文 → 发送额外消息 → 记录事件。

    reason 限流分流：
      - "crash"（默认）：递增 recovery_count，受 monitor_max_recoveries 约束。
      - "quota_switch"：递增 quota_switch_count（独立计数，与 recovery_count 分开），
        不受 monitor_max_recoveries 约束 —— 否则连换 3 个号就会撞上崩溃恢复上限
        （docs/plan-b §3.2：混用会让换号能力被误杀）。换号上限由
        quota_failover_config.max_switches 在调用方（_scan_member_terminal quota 分支）检查。

    P2 事务式换号（generation_migrate 开启时）：quota_switch 不再先 kill 旧窗，
    而是 spawn 新窗 {member}__g{N+1} 成功后原子提升 ACTIVE、旧窗记 DRAINING；
    失败回滚 agent_user 并保持旧 ACTIVE 与 checkpoint（见 _quota_generation_migrate）。
    previous_agent_user 供迁移失败回滚还原旧账号；feature flag 关闭时行为不变。

    ⚠️ 阶段3 诚实标注：CLI 会话 resume 仍未实现（阶段2 未做），quota 换号后是
    全新会话，对话上下文不保留。但成员任务 checkpoint 已接线：换号/恢复前读取
    最新成员 checkpoint（持 TEAM_DATA_LOCK），恢复消息携带结构化进度指针与
    verify-then-continue 续跑指令（completed_steps 不重做、产物哈希先核对），
    有 checkpoint 时不再空白重发 last_task；无 checkpoint 才回落重发 last_task
    从头做。其余 reason（crash 等）行为不变，与既有调用点完全向后兼容。

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

    # P2：已迁移成员重建窗口时用 ACTIVE generation 窗口名（{member}__g{N}），
    # 保持路由指向当前 ACTIVE；legacy 成员 window_name=None → 裸名，行为不变。
    spawn_window_name = _active_generation_window_name(member_name, member)

    # 确保 MCP 配置就绪
    _write_claude_mcp(team_name)
    _ensure_codex_mcp()

    # 保存死亡前上下文快照
    had_task = bool(member.get("last_task", "")) and not member.get("last_task_completed", True)
    try:
        _save_death_context_snapshot(team_name, member_name)
    except Exception:
        pass

    # 更新恢复计数和时间戳（quota 换号走独立计数，与崩溃恢复互不污染：
    # 连换 N 个号不递增 recovery_count，不会撞上 monitor_max_recoveries）
    if reason == "quota_switch":
        member["quota_switch_count"] = member.get("quota_switch_count", 0) + 1
    else:
        member["recovery_count"] = member.get("recovery_count", 0) + 1
    member["last_recovery_ts"] = datetime.datetime.now().isoformat()
    member["last_terminal_death_ts"] = datetime.datetime.now().isoformat()
    _save(data)

    # 重建终端窗口。
    #
    # quota_switch 默认先杀旧窗口。配额耗尽时 CLI 只是报错回到提示符，**窗口
    # 仍然活着** —— 此时 _tmux_spawn_member 命中 "window already exists" 分支并
    # 返回 rc=0（非 0 才算失败），于是下面的 rc != 0 检查放行，恢复上下文被发进
    # 旧窗口。结果：member["agent_user"] 已写成新 key，但进程从未重启、env 从未
    # 重新注入，仍用刚耗尽的旧凭证 —— 换号 100% 空转，且
    # agent_user_failover_history 记录得一切正常，运维完全看不出来。
    #
    # P2 事务式换号（generation_migrate 开启）：不 kill 旧窗，spawn 新窗
    # {member}__g{N+1}，成功后原子提升 ACTIVE、旧窗 DRAINING（见
    # _quota_generation_migrate）；失败兜底才走下方 kill/recreate 旧行为。
    if reason == "quota_switch":
        if _quota_generation_migrate_enabled(team):
            ok_mig, mig_msg = _quota_generation_migrate(
                team_name, member_name, session, previous_agent_user,
            )
            if ok_mig:
                return True, ""
            # 失败兜底：迁移已回滚 agent_user=previous；兜底前恢复为 nxt
            # （顶部快照的 agent_user 即调用方写入的新账号），走 kill/recreate。
            _apply_agent_user(team_name, member_name, member.get("agent_user", ""))
            time.sleep(0.3)
        stale_target = _member_window_target(team_name, member_name) or member_name
        _tmux(["kill-window", "-t", _tmux_target(session, stale_target)])
        time.sleep(0.3)     # 等 tmux 回收窗口，避免 spawn 撞上同名残留
    # P2 跨凭证迁移同规则：quota 换号是换账号，不得原生 resume 旧账号会话
    # （generation_migrate 路径已在 _quota_generation_migrate 传 resume_disabled=True，
    # 此处 kill/recreate 默认路径必须同样禁用 resume），只走 checkpoint 续跑；
    # crash 恢复（reason="crash"）仍保留 resume 以恢复对话上下文。
    rc, _, err = _tmux_spawn_member(
        session, member_name, agent, team_dir,
        window_name=spawn_window_name,
        resume_disabled=(reason == "quota_switch"),
    )
    if rc != 0:
        return False, f"终端重建失败: {err}"
    if reason == "quota_switch" and "already exists" in (err or ""):
        # 杀窗后仍报已存在 → 换号必然空转，宁可失败也不要静默假成功
        return False, f"换号需重启进程，但旧窗口未能回收: {err}"
    # 脚手架用完即撤：这条路径常常发生在 session 被 _ensure_team_session 重建之后
    # （换号先杀旧窗、整个 session 中断恢复），那次重建会留下一个没有任何 CLI 的
    # __base 空壳。成员窗已经接进来了，空壳必须撤掉，否则它常驻为窗口 0，用户
    # attach 进去只看到 bash 提示符。
    drop_base_window(session, _tmux)
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


# ============================================================
# P2：事务式 quota 换号 + 终端 generation
# ============================================================

# DRAINING 旧窗默认回收 TTL（秒）；terminal_windows 记录上限（有界，保最新）
QUOTA_GENERATION_DRAINING_TTL_SECONDS = 300
MAX_TERMINAL_WINDOWS = 8


def _quota_generation_migrate_enabled(team: dict) -> bool:
    """feature flag：team['quota_failover']['generation_migrate']（默认 False）。

    False = 保持既有 kill/recreate 换号行为一字不变；True = 事务式 generation
    迁移（不 kill 旧窗，spawn 新窗后原子提升 ACTIVE）。配置在团队数据里直接
    写入 quota_failover.generation_migrate，无需额外工具。
    """
    stored = team.get("quota_failover")
    if isinstance(stored, dict):
        return bool(stored.get("generation_migrate", False))
    return False


def _draining_ttl_seconds(team: dict) -> int:
    ttl = QUOTA_GENERATION_DRAINING_TTL_SECONDS
    stored = team.get("quota_failover")
    if isinstance(stored, dict) and isinstance(stored.get("draining_ttl_seconds"), int):
        ttl = max(30, min(stored["draining_ttl_seconds"], 86400))
    return ttl


def _apply_agent_user(team_name: str, member_name: str, agent_user: str) -> None:
    """原子写回成员 agent_user（迁移失败兜底前恢复 nxt 用）。空值不动。"""
    if not agent_user:
        return

    def updater(latest_team: dict) -> dict:
        m = latest_team.get("members", {}).get(member_name)
        if isinstance(m, dict):
            m["agent_user"] = agent_user
        return {"ok": True}

    _update_team_data(team_name, updater)


def _revert_agent_user(team_name: str, member_name: str, previous_agent_user: str) -> None:
    """迁移失败回滚：agent_user 还原为 previous（保持与旧 ACTIVE 窗口一致）。

    previous 为空（未传/首换）时不动 —— 宁可不回滚也不写空号。
    """
    if not previous_agent_user:
        return
    _apply_agent_user(team_name, member_name, previous_agent_user)


def _promote_generation(
    team_name: str,
    member_name: str,
    new_window: str,
    next_gen: int,
    cur_gen: int,
    agent_user: str,
    previous_agent_user: str = "",
) -> tuple[bool, str]:
    """COMMIT 阶段：原子提升新窗为 ACTIVE、旧 ACTIVE 记 DRAINING+TTL。

    有界：terminal_windows 最多保留 MAX_TERMINAL_WINDOWS 条（保最新）。
    checkpoint 不动（换号不触碰成员任务进度）。cur_gen==1 的 legacy 首窗为
    裸名 {member}（不在 terminal_windows 中），显式补 DRAINING 记录供 TTL 回收，
    避免裸名旧窗泄漏。
    """
    import datetime
    now = datetime.datetime.now()
    ttl = _draining_ttl_seconds(_load().get("teams", {}).get(team_name, {}))

    def updater(latest_team: dict) -> dict:
        m = latest_team.get("members", {}).get(member_name)
        if not isinstance(m, dict):
            return {"ok": False, "error": f"成员 '{member_name}' 不存在"}
        windows = [dict(w) for w in (m.get("terminal_windows") or []) if isinstance(w, dict)]
        for w in windows:
            if w.get("generation") == cur_gen and w.get("status") == "ACTIVE":
                w["status"] = "DRAINING"
                w["drained_ts"] = now.isoformat()
                w["ttl_until"] = (now + datetime.timedelta(seconds=ttl)).isoformat()
        # legacy 首窗（裸名 {member}）不在列表 → 显式补 DRAINING 记录
        if cur_gen == 1 and not any(w.get("generation") == 1 and w.get("name") == member_name for w in windows):
            windows.append({
                "name": member_name,
                "generation": 1,
                "status": "DRAINING",
                "agent_user": previous_agent_user,
                "drained_ts": now.isoformat(),
                "ttl_until": (now + datetime.timedelta(seconds=ttl)).isoformat(),
            })
        existing = next((w for w in windows if w.get("name") == new_window), None)
        if existing:
            existing["status"] = "ACTIVE"
            existing["generation"] = next_gen
            existing["agent_user"] = agent_user
        else:
            windows.append({
                "name": new_window,
                "generation": next_gen,
                "status": "ACTIVE",
                "agent_user": agent_user,
                "created_ts": now.isoformat(),
            })
        m["terminal_windows"] = windows[-MAX_TERMINAL_WINDOWS:]
        m["terminal_generation"] = next_gen
        m["quota_hits"] = 0
        m.pop("blocked_reason", None)
        return {"ok": True}

    result = _update_team_data(team_name, updater)
    if result is None:
        return False, f"团队 '{team_name}' 不存在"
    return bool(result.get("ok")), str(result.get("error") or "")


def _quota_generation_migrate(
    team_name: str,
    member_name: str,
    session: str,
    previous_agent_user: str,
) -> tuple[bool, str]:
    """事务式 generation 迁移：不先 kill 旧窗，spawn 新窗 {member}__g{N+1}。

    成功：原子提升 ACTIVE（_promote_generation），旧窗记 DRAINING+TTL，
    agent_user 保持新账号（调用方已设为 nxt），checkpoint 不动。
    失败（spawn/接续/commit）：kill 新窗 + 回滚 agent_user=previous，保持旧
    ACTIVE 与原 checkpoint —— 不产生半迁移状态，旧窗仍是唯一权威。

    返回 (ok, msg)；ok=False 时调用方可选择 kill/recreate 兜底（_recover_and_send）。
    """
    import datetime
    data = _load()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    agent = _member_agent(team, member)
    team_dir = _team_dir(team_name)

    cur_gen = _member_generation(member)
    next_gen = cur_gen + 1
    new_win = f"{member_name}__g{next_gen}"

    # 1. spawn 新窗（新账号 env 由 member['agent_user']=nxt 注入；不 kill 旧窗）
    rc, _, err = _tmux_spawn_member(session, member_name, agent, team_dir, window_name=new_win, resume_disabled=True)
    if rc != 0:
        _revert_agent_user(team_name, member_name, previous_agent_user)
        return False, f"新账号窗口创建失败: {err}"

    # 2. 新窗已存在且 live（上次失败残留）→ 复用前确认进程存活；dead 则重建
    if "already exists" in (err or ""):
        target = new_win
        records = _tmux_window_records(session)
        rec = next((r for r in records if r["name"] == new_win), None)
        if rec:
            target = rec["id"]
        arc, aout, _aerr = _capture_window(session, target, 40)
        if arc == 0 and _classify_terminal_output(aout) == "dead":
            try:
                _tmux(["kill-window", "-t", _tmux_target(session, target)])
            except Exception:
                pass
            time.sleep(0.3)
            rc, _, err = _tmux_spawn_member(session, member_name, agent, team_dir, window_name=new_win, resume_disabled=True)
            if rc != 0:
                _revert_agent_user(team_name, member_name, previous_agent_user)
                return False, f"新账号窗口重建失败: {err}"

    # 3. 发送恢复上下文（接续；失败 → 清理新窗 + 回滚，旧 ACTIVE 不动）
    recovery_ctx = _build_recovery_context(team_name, member_name, generation=next_gen)
    snd_rc, snd_err = _send_keys(session, new_win, recovery_ctx)
    if snd_rc != 0:
        try:
            _tmux(["kill-window", "-t", _tmux_target(session, new_win)])
        except Exception:
            pass
        _revert_agent_user(team_name, member_name, previous_agent_user)
        return False, f"新窗口恢复上下文发送失败: {snd_err}"

    # 4. COMMIT：原子提升 ACTIVE（checkpoint 不动）
    ok, commit_err = _promote_generation(
        team_name, member_name, new_win, next_gen, cur_gen,
        agent_user=member.get("agent_user", ""),
        previous_agent_user=previous_agent_user,
    )
    if not ok:
        try:
            _tmux(["kill-window", "-t", _tmux_target(session, new_win)])
        except Exception:
            pass
        _revert_agent_user(team_name, member_name, previous_agent_user)
        return False, f"generation 提升失败: {commit_err}"
    return True, ""


def _reclaim_member_draining_windows(team_name: str, member_name: str) -> int:
    """回收超过 TTL 的 DRAINING 旧窗（有界：记录上限 MAX_TERMINAL_WINDOWS）。

    仅 kill DRAINING 且过期的窗口；ACTIVE 永不回收。返回回收数。
    """
    import datetime
    data = _load()
    member = data.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name)
    windows = member.get("terminal_windows") if isinstance(member, dict) else None
    if not windows:
        return 0
    session = _find_any_session(team_name)
    now = datetime.datetime.now()
    to_kill = []
    for w in windows:
        if not isinstance(w, dict) or w.get("status") != "DRAINING":
            continue
        ttl = w.get("ttl_until") or ""
        expired = False
        if ttl:
            try:
                expired = datetime.datetime.fromisoformat(ttl) <= now
            except ValueError:
                expired = True
        else:
            expired = True
        if expired:
            name = w.get("name")
            if name:
                to_kill.append(name)
    if session:
        for name in to_kill:
            try:
                _tmux(["kill-window", "-t", _tmux_target(session, name)])
            except Exception:
                pass
    if to_kill:
        killed = set(to_kill)

        def updater(latest_team: dict) -> dict:
            m = latest_team.get("members", {}).get(member_name)
            if isinstance(m, dict):
                ws = m.get("terminal_windows") or []
                m["terminal_windows"] = [w for w in ws if w.get("name") not in killed][-MAX_TERMINAL_WINDOWS:]
            return {"ok": True}

        _update_team_data(team_name, updater)
    return len(to_kill)


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
            allowed_tools=CLAUDE_LEADER_TOOL_ALLOW_PATTERNS,
        )
    except (OSError, RuntimeError) as e:
        return False, f"leader spawn lock unavailable: {e}"
    if rc != 0:
        return False, f"leader window spawn failed: {err}"
    if err and "already exists" in err:
        # 旧窗口未被清除，禁止向可能已死的窗口注入提示
        return False, "leader window already exists (stale), skip injection"

    # 脚手架用完即撤：上面若走了 _ensure_team_session 重建 session，会留下一个
    # 没有 CLI 的 __base 空壳；leader 窗已经建好，空壳必须撤掉。
    drop_base_window(session, _tmux)

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
        f"[恢复通知] 终端恢复通知 (第{recovery_count + 1}次恢复)",
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
        "",
        _member_report_first_rule(),
    ])
    return "\n".join(lines)


def _report_dedup_key(member_name: str, last_task: str, event: str, result: str) -> str:
    """稳定去重键：同成员+同当前任务+同事件+同结果 → 同键（重复回报幂等）。"""
    return f"{member_name or ''}|{(last_task or '')[:40]}|{event}|{result[:80]}"


def _make_report_id(member_name: str, dedup_key: str) -> str:
    """持久化 report_id：对同 (成员,任务,事件,结果) 稳定复现；跨成员/跨任务不撞。

    作为 leader 消费/ACK 的持久化证据标识——交付(delivered)与 ACK(activate)
    均引用此 id，验收不以 idle/terminal classifier 判完成，只认持久化 report_id。
    """
    import hashlib

    return f"{member_name or 'unknown'}:{hashlib.sha1(dedup_key.encode('utf-8')).hexdigest()[:16]}"


def _record_report_and_notify_leader(
    team_name: str,
    member_name: str,
    result: str,
    artifact_path: str = "",
    compressed_context_path: str = "",
    event: str = "member_report",
    generation: int = 0,
    mark_member_complete: str = "",
) -> tuple[str, dict, str, str, dict]:
    """写入 results.jsonl + 记录 leader 待处理回报 + 激活/唤醒 leader。

    member_report_result(event="member_report") 与 monitor idle 自动完成路径
    (event="monitor_inferred_completion") 共用。顺序固定为 记录 → 通知，
    且必须在 /compact 注入之前调用——成员终端被 /compact 清空后成员再无机会
    亲笔回报，先落盘 leader 才能看到。

    S1 原子完成标记：``mark_member_complete`` 非空时，完成标记
    (last_task_completed / last_observed_state=idle / last_report_*) 与 pending
    append 在同一 ``_update_team_data`` 锁内原子写入——崩溃/失败在锁内时成员
    保持"进行中"，绝不出现"已完成但无报告"的假完成态（竞态 A 根治）。

    S2 幂等：report_id 对同 (成员,任务,事件,结果) 稳定复现，重复回报被本函数
    (last_report_key) 与 append_leader_pending_report(report_id) 双层跳过；成员
    亲笔回报(member_report)会替换同任务下 monitor 推断(monitor_inferred_completion)
    的合成回报（后者权威低，防双报）。

    S3 delivered：注入成功由 _notify_leader_of_report 标 delivered；未 ACK 的报告
    不再被巡检重放（竞态 B 根治）。

    并发安全：leader_pending_reports 的修改走 _update_team_data 的
    read-modify-write 原语（锁内 load → append → save），绝不"先 _load 再
    _save 整份"覆盖并发写入；results.jsonl 是追加式日志，天然并发安全。

    异常隔离：写记录/通知的任何失败都降级为提示字符串，不向上抛——补回报
    失败绝不能阻断调用方（尤其 monitor 扫描线程，其外层 _monitor_team_loop
    只有裸 except 兜底）。

    返回 (results_file, entry, write_error, report_notice, mark_info)。
    mark_info = {"marked": bool, "duplicate": bool} —— marked=是否在同一锁内标记
    完成；duplicate=是否为重复回报被幂等跳过。
    """
    import datetime

    # ---- 1. 构建 results.jsonl 记录（report_id 在锁内生成后回填；锁内 best-effort 落盘，先于 /compact） ----
    results_file = os.path.join(_share_dir(team_name), "results.jsonl")
    entry = {
        "timestamp": datetime.datetime.now().isoformat(),
        "member": member_name or "unknown",
        "result": result,
        "artifact_path": artifact_path,
        "compressed_context_path": compressed_context_path,
    }
    if generation > 0:
        entry["generation"] = generation
    write_error = ""

    # ---- 2. 记录 leader 待处理回报 + 激活/唤醒 leader ----
    # 成员回报即 leader 激活信号：tmux resting leader 立即注入唤醒；
    # direct/其他情况回报持久化到 leader_pending_reports，leader 重新进入时用 leader_activate 确认。
    report_notice = ""
    mark_info = {"marked": False, "duplicate": False}
    report_entry = None  # 在锁内用最新成员状态构建（含 report_task / report_id）
    try:
        def _append_report_entry(latest_team: dict) -> dict:
            nonlocal report_entry, write_error
            members = latest_team.get("members", {})
            member = members.get(member_name) if member_name else None
            last_task = (member.get("last_task", "") or "") if member else ""
            key = _report_dedup_key(member_name, last_task, event, result)
            report_entry = {
                "timestamp": entry["timestamp"],
                "member": member_name or "unknown",
                "event": event,
                "result": _compact_text(result, 500),
                "artifact_path": artifact_path,
                "report_task": last_task,
                "report_id": _make_report_id(member_name, key),
            }
            # 回填 results.jsonl 记录的 report_id（持久化证据链与 pending 一致）
            entry["report_id"] = report_entry["report_id"]
            if member:
                # monitor 推断是低权威合成回报：若同成员同任务已有亲笔 member_report
                # pending，直接跳过 —— 防"亲笔先落盘、monitor 后判 idle"反向顺序下
                # monitor 合成回报覆盖/重复亲笔报告（P1 A2 纵深防御；正向顺序由下方
                # member_report 替换 monitor_inferred 处理）。
                if event == MONITOR_INFERRED_EVENT and last_task:
                    existing_pending = latest_team.get("leader_pending_reports") or []
                    if any(
                        r.get("event") == "member_report"
                        and r.get("member") == member_name
                        and r.get("report_task") == last_task
                        for r in existing_pending
                    ):
                        mark_info["duplicate"] = True
                        return {"appended": False, "duplicate": True}
                # S2 幂等：同成员+同任务+同事件+同结果 → 重复回报跳过（leader 只提醒一次）
                if member.get("last_report_key") == key:
                    mark_info["duplicate"] = True
                    return {"appended": False, "duplicate": True}
                member["last_report_key"] = key
                member["last_report_id"] = report_entry["report_id"]
                member["last_report_ts"] = report_entry["timestamp"]
                member["last_report_summary"] = _compact_text(result, 120)
                member["last_report_task"] = last_task
                member["last_report_event"] = event
                # S1 原子完成标记：与 pending append 同锁写入；回报未落 pending 绝不标完成
                if mark_member_complete and last_task.strip():
                    member["last_task_completed"] = True
                    member["last_observed_state"] = "idle"
                    member["last_completed_ts"] = report_entry["timestamp"]
                    mark_info["marked"] = True
                # 成员亲笔回报权威：替换同任务下 monitor 推断的合成回报（防双报）
                if event == "member_report":
                    pending = latest_team.get("leader_pending_reports") or []
                    for i in range(len(pending) - 1, -1, -1):
                        r = pending[i]
                        if (
                            r.get("event") == MONITOR_INFERRED_EVENT
                            and r.get("member") == member_name
                            and r.get("report_task") == last_task
                        ):
                            pending.pop(i)
            # 写 results.jsonl（带 report_id；记录先于 pending append 且先于 /compact）。
            # best-effort：写失败只置 write_error，绝不阻断 pending 交付/完成标记。
            if not write_error:
                try:
                    with open(results_file, "a", encoding="utf-8") as f:
                        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
                except Exception as e:
                    write_error = f"⚠️ 写入 results.jsonl 失败: {e}"
            append_leader_pending_report(latest_team, report_entry)
            # leader_checkpoint 证据记录：与 pending 回报同锁原子追加，
            # 供恢复时渲染"最近证据"（无 checkpoint 时 no-op）。
            # 防御：checkpoint 证据写入绝不能阻断 P0 关键路径的 pending 回报
            # append（monitor 路径外层 try/except 是兜底，这里就地隔离更稳）。
            try:
                if isinstance(latest_team.get("leader_checkpoint"), dict):
                    evidence = list(latest_team.get("leader_checkpoint", {}).get("evidence") or [])
                    evidence.append({
                        "timestamp": entry["timestamp"],
                        "member": member_name or "unknown",
                        "event": event,
                        "result": _compact_text(result, 300),
                    })
                    _leader_checkpoint_upsert(
                        latest_team,
                        {"evidence": evidence[-MAX_CHECKPOINT_EVIDENCE:]},
                        source="report",
                        updated_by=member_name or "member",
                    )
            except Exception:
                pass  # 证据失败仅丢证据，不丢回报
            return {"appended": True, "duplicate": False}

        _update_team_data(team_name, _append_report_entry)
        if mark_info["duplicate"]:
            report_notice = "\n🔄 重复回报（同任务同结果）已幂等跳过，不重复提醒 leader。"
        else:
            if report_entry is None:  # 团队缺失等异常兜底：构建最小 report_entry
                report_entry = {
                    "timestamp": entry["timestamp"],
                    "member": member_name or "unknown",
                    "event": event,
                    "result": _compact_text(result, 500),
                    "artifact_path": artifact_path,
                    "report_task": "",
                    "report_id": _make_report_id(member_name, _report_dedup_key(member_name, "", event, result)),
                }
            wake = _notify_leader_of_report(team_name, report_entry)
            if wake.get("injected"):
                report_notice = "\n🔔 已唤醒 leader 并注入本次回报。"
            elif wake.get("leader"):
                report_notice = "\n🔔 本次回报已记入 leader 待处理列表；leader 重新进入后用 leader_activate 查看确认。"
    except Exception as e:
        report_notice = f"\n⚠️ 记录 leader 回报失败: {e}"

    return results_file, entry, write_error, report_notice, mark_info


@mcp.tool
def member_report_result(
    team_name: str,
    result: str,
    artifact_path: str = "",
    member_name: str = "",
    compressed_context: str = "",
    generation: int = 0,
) -> str:
    """
    [成员] 将任务结果回传给 leader 或其他成员。
    结果会写入共享上下文区的 results.jsonl，供所有成员读取。
    同时为本次任务生成一份压缩上下文，便于 leader 快速了解成员工作。
    提供 member_name 时会标记该成员任务完成并保持终端空闲，
    等待 leader 下发新任务。
    回报完成后系统会自动向你的终端注入 /compact（收尾在 _finalize_agent_completion），
    所以不需要、也不应该在回报前自行执行 /compact。

    P2 generation 回报门控：换号后旧窗口（DRAINING/非 ACTIVE）的成员若持旧
    generation 回报，会被门控拒绝——防止 stale 窗口把过期结论当权威结果写入。
    恢复消息会告知当前窗口 generation，ACTIVE 新窗回报传匹配值即可通过。

    Args:
        team_name: 团队名称
        result: 任务结果摘要
        artifact_path: 可选，产出文件在共享上下文区内的路径
        member_name: 可选，上报结果的成员名称（用于标记任务完成并休眠）
        compressed_context: 可选，成员主动提供的压缩上下文；为空时根据 result/任务记录自动生成
        generation: 可选，发起回报的窗口 generation；>0 时与成员当前 ACTIVE
            generation 不一致则拒绝（旧窗口门控）；0=不校验（向后兼容）
    """
    data = _load()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return f"❌ 团队 '{team_name}' 不存在。"

    # P2 generation 回报门控：旧窗口（generation 落后于当前 ACTIVE）回报被拒。
    # 门控发生在任何数据写入之前 —— 被拒回报绝不落 results.jsonl / pending。
    if generation > 0 and member_name:
        cur_gen = _member_generation(
            data.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
        )
        if cur_gen != generation:
            return (
                f"⛔ 回报门控：generation={generation} 已不是当前 ACTIVE 窗口"
                f"（当前 g{cur_gen}）。旧窗口回报被拒绝；请用 ACTIVE 窗口（新账号会话）回报。"
            )

    task_msg = ""
    idle_msg = ""

    # ---- 1. 生成压缩上下文（先生成路径，供 results.jsonl 记录） ----
    pre_path = ""
    try:
        pre_path = _write_member_compressed_context(
            team_name, member_name or "unknown", result, artifact_path, compressed_context
        )
    except Exception as e:
        pre_path = f"生成失败: {e}"

    # ---- 2. 写入 results.jsonl + 记录 leader 待处理回报（记录必须在 /compact 之前） ----
    # S1 原子完成标记：mark_member_complete=member_name 使完成标记(last_task_completed
    # / idle / last_report_*) 与 pending append 在同一 _update_team_data 锁内写入。
    # 回报未持久化绝不标记完成——崩溃/失败时成员保持"进行中"，杜绝"已完成但无报告"竞态。
    # 与 monitor idle 自动完成路径共用 _record_report_and_notify_leader：
    # 写 results.jsonl → 锁内 append pending + 原子完成标记 → _notify_leader_of_report。
    results_file, _entry, write_error, report_notice, mark_info = _record_report_and_notify_leader(
        team_name,
        member_name,
        result,
        artifact_path=artifact_path,
        compressed_context_path=pre_path,
        event="member_report",
        generation=generation,
        mark_member_complete=member_name,
    )
    marked = mark_info.get("marked", False)
    if member_name and marked:
        task_msg = f"\n✅ 成员 '{member_name}' 的任务已标记为完成"
        idle_msg = f"\n🟢 成员 '{member_name}' 终端保持空闲，等待新任务"

    # 锁内原子收尾：compact_sent_by_monitor 消费 + leader_work_state 同步。
    # 必须用 _update_team_data（锁内 fresh read-modify-write）——并发成员回报时，
    # 盲 _save(data) 会用 stale 快照覆写刚 append 的回报（B5b 并发竞态）。
    def _finalize_member_state(latest_team: dict) -> dict:
        # Monitor may have inferred completion before the member had a chance to
        # submit its authoritative result. Permit exactly one explicit report to
        # deliver /compact, while preserving normal duplicate-report idempotency.
        if member_name:
            latest_member = latest_team.get("members", {}).get(member_name, {})
            if latest_member.pop("compact_sent_by_monitor", False):
                latest_member.pop("compact_sent", None)
        if not leader_has_unfinished_work(latest_team):
            latest_team["leader_work_state"] = "idle"
        else:
            # A partial member report must keep the persisted team in active state;
            # otherwise a re-entered leader can incorrectly enter standby while
            # sibling tasks are still unfinished.
            _touch_leader_activity(latest_team)
        return {"saved": True}

    _update_team_data(team_name, _finalize_member_state)

    # 重读最新团队状态（完成标记与收尾已在锁内写入；供下方 revive 判断 leader_type）
    data = _load()
    team = data.get("teams", {}).get(team_name, team)

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
        confirm_codex_submission=_target_is_codex_tmux_leader(team, actual_target),
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
    # TUI 只写 terminals_active 不启动 monitor；周期 sweep 保证仅经 TUI 启动的
    # 团队也能得到 classifier 检测/审计/wakeup 半环（monitor 单宿主于本进程）。
    _MONITOR_SWEEP_STOP = threading.Event()
    threading.Thread(
        target=_ensure_team_monitors_loop,
        args=(_MONITOR_SWEEP_STOP,),
        name="mcp-monitor-sweep",
        daemon=True,
    ).start()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
