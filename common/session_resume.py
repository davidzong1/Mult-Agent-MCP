"""
Multi-Agent MCP — 稳定 session 恢复基础适配器（P3 基础层，P4 接线，P4b 回填）
==============================================================================

在 P1 成员任务 checkpoint 落地后，CLI 会话 resume 被构造并**已接入**生产
spawn/恢复链路（P4/P4b）；feature flag **默认关闭**，关闭时零行为变化
（P0–P3 一字不变，见 docs/p4-session-resume-wiring.md）。本模块做 CLI 参数
构造、文件隔离校验与真实会话发现：

  - 不触真实凭证 / API：所有路径都接受注入的 claude_home / codex_home，
    测试全部用临时目录，绝不读真实 ~/.claude 或 ~/.codex。
  - 禁止 --last / -c 等模糊 cwd 恢复：恢复必须携带显式 session_id。
  - **Claude**：session_id 为 uuid4，由调用方初次 spawn 生成并持久化
    （member/leader checkpoint），首启 `--session-id` 绑定、恢复 `--resume`
    精确 id，转录白名单校验（B1/B3）。
  - **Codex（managed member / managed leader）**：首启**不自造 uuid**——
    真实会话 id 只能来自实际写盘的 rollout 证据；`discover_codex_session`
    扫描私有 CODEX_HOME/sessions/**/rollout-*.jsonl 的**首行 session_meta**，
    按 cwd + spawn 时间窗 + 唯一候选 discover 真实 UUID，调用方（P4b
    `_codex_session_backfill`）原子回填 member.session_id / leader checkpoint
    后才可 `codex resume <真实id>`。缺失/歧义/错误 cwd/超窗过期 → 只 checkpoint。
  - **direct / claim leader**（无管理终端）明确 **checkpoint-only**：不记录
    回填标记、不 discover、不 resume。**P2 跨用户 generation** 仍 checkpoint-only
    （resume_disabled=True 时不回填、不记录标记）。
  - 禁止复制 credentials / settings：resume 路径接入 reject_sensitive_paths，
    session 恢复只允许转录/会话元数据。
  - resume 不可用时返回结构化 fallback（use_task_checkpoint=True），
    调用方据此改用 P1 task checkpoint 续跑，而不是空白重做。

feature flag：模块常量 SESSION_RESUME_ENABLED 默认 False；环境变量
MULT_AGENT_MCP_SESSION_RESUME=1 可显式开启（供测试 / 接线验证）。
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

# ---------------------------------------------------------------------------
# feature flag（默认关闭；P3 只交付参数构造层，不改变任何既有行为）
# ---------------------------------------------------------------------------

SESSION_RESUME_ENABLED = False

#: 环境变量开关名（测试与后续接线用，值 "1"/"true"/"True" 视为开启）
RESUME_FLAG_ENV = "MULT_AGENT_MCP_SESSION_RESUME"


def resume_enabled() -> bool:
    """是否启用 session resume。默认关闭；环境变量显式开启。"""
    if os.environ.get(RESUME_FLAG_ENV) in ("1", "true", "True"):
        return True
    return SESSION_RESUME_ENABLED


# ---------------------------------------------------------------------------
# 稳定 session_id：uuid4 生成；"稳定"由调用方持久化保证（不依赖模糊 cwd）
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    """生成一个新的 Claude/Codex session UUID（uuid4）。

    Claude 的 --session-id / --resume 使用标准 UUID 作为会话标识。真实 CLI 审计
    否决了"确定性哈希 id"方案：哈希串不是合法会话 id，且转录文件名对不上
    （B1）。session_id 由调用方在初次 spawn 时生成并**持久化**
    （member["session_id"] / leader checkpoint），此后换号/恢复/复活复用同一 id；
    本函数只负责生成，不负责记忆。
    """
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Claude：显式 --session-id / --resume 参数构造 + 转录白名单校验
# ---------------------------------------------------------------------------

#: Claude 模糊恢复参数——任何 resume 路径都禁止出现（必须显式 session_id）
CLAUDE_FORBIDDEN_RESUME_ARGS = ("--last", "-l", "--continue", "-c")

#: 禁止随 session 复制的敏感文件名/目录名（凭证、设置、认证）
SENSITIVE_RESUME_NAMES = {
    "credentials",
    "settings",
    "settings.json",
    ".claude.json",
    "config.json",
    "config.toml",
    "auth.json",
    ".codex",
    ".credentials",
}


def claude_session_id_argv(session_id: str) -> list[str]:
    """新会话显式绑定：--session-id <id>（为将来拉起新会话并打固定标记用）。"""
    return ["--session-id", session_id]


def claude_resume_argv(session_id: str) -> list[str]:
    """Claude 显式恢复参数：--resume <session_id>。

    精确 id 恢复，绝不退化为 --last / --continue（模糊最近会话）。
    """
    return ["--resume", session_id]


def reject_forbidden_resume_args(argv: list[str]) -> str | None:
    """拒绝 --last / -l / --continue / -c 等模糊恢复参数。

    返回错误原因字符串（有禁止参数时）或 None（合法）。调用方在拼装
    恢复命令前必须先过此闸，防止任何路径退化为模糊 cwd 恢复。
    """
    for bad in CLAUDE_FORBIDDEN_RESUME_ARGS:
        if bad in argv:
            return f"禁止使用模糊恢复参数 {bad}（恢复必须显式 --resume <session_id>）"
    return None


def encode_project_dir(workspace_dir: str) -> str:
    """Claude 转录目录编码：绝对路径 -> projects 下目录名。

    与 Claude Code 约定一致（路径分隔符替换为 '-'），**保留首尾 '-'**：绝对路径
    以 '/' 开头 → 首个空段变成前导 '-'（如 /a/b/c → -a-b-c）。真实 CLI 审计
    （W1）发现之前 `.strip("-")` 会剥掉前导 '-'，导致 transcript 路径与
    Claude 实际写入的 `~/.claude/projects/-a-b-c/<id>.jsonl` 对不上、--resume
    找不到文件。Windows 盘符段（C:）保留字母，冒号移除。
    """
    normalized = str(workspace_dir).replace("\\", "/")
    return normalized.replace("/", "-").replace(":", "")


def transcript_path_for(
    session_id: str, workspace_dir: str, claude_home: str = ""
) -> Path:
    """成员 session 转录的预期路径：<claude_home>/projects/<编码>/<id>.jsonl。

    claude_home 可注入（默认 ~/.claude），测试用临时目录，不触真实配置。
    """
    home = Path(claude_home or os.path.expanduser("~/.claude"))
    return home / "projects" / encode_project_dir(workspace_dir) / f"{session_id}.jsonl"


def validate_transcript(
    session_id: str, workspace_dir: str, claude_home: str = ""
) -> dict:
    """转录白名单校验：session 必须精确存在于本成员 workspace 的 project 目录。

    这是防跨团队 / 防模糊 cwd 恢复的关键闸：只认 <workspace 编码>/<session_id>.jsonl
    这一条精确路径，绝不扫描"最近会话"或跨目录猜测。返回结构化结果：

        {"ok": True,  "path": str, "session_id": str, "workspace": str}
        {"ok": False, "reason": str, "path": str}

    未找到 / 找到但不可读都返回 ok=False，调用方据此走结构化 fallback。
    """
    path = transcript_path_for(session_id, workspace_dir, claude_home)
    if not path.is_file():
        return {
            "ok": False,
            "reason": f"session 转录不存在或未授权: {path}（禁止模糊 cwd 恢复）",
            "path": str(path),
        }
    return {
        "ok": True,
        "path": str(path),
        "session_id": session_id,
        "workspace": workspace_dir,
    }


# ---------------------------------------------------------------------------
# Codex：精确 resume 命令 + 私有 CODEX_HOME session 定位
# ---------------------------------------------------------------------------

#: Codex 模糊恢复参数——同样禁止
CODEX_FORBIDDEN_RESUME_ARGS = ("--last", "-l")


def codex_resume_argv(session_id: str) -> list[str]:
    """Codex 精确恢复命令：codex resume <session_id>。

    使用精确 id 恢复，禁止 codex resume 交互选择器 / codex --last 模糊恢复。
    """
    return ["resume", session_id]


def codex_sessions_dir(codex_home: str = "") -> Path:
    """私有 CODEX_HOME session 根目录。

    codex_home 可注入（默认 $CODEX_HOME 或 ~/.codex），测试用临时目录。
    """
    home = Path(
        codex_home
        or os.environ.get("CODEX_HOME")
        or os.path.expanduser("~/.codex")
    )
    return home / "sessions"


#: rollout 文件名末尾的真实 session uuid（本机 codex 实测：
#:   sessions/<year>/<month>/<day>/rollout-<ts>-<uuid>.jsonl）
_ROLLOUT_UUID_RE = re.compile(
    r"-([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})\.jsonl$"
)


def _codex_session_identity(rollout: Path) -> dict:
    """从单个 rollout 文件解析真实 session 标识（session_meta 优先，文件名 uuid 兜底）。

    返回 {"session_id": str, "name": str, "path": str}。session_meta.json 存在时
    取其 session_id/id 与 title/name；否则从 rollout 文件名末段解析真实 uuid。
    解析不到任何标识返回全空（调用方跳过该文件）。
    """
    meta = rollout.parent / "session_meta.json"
    if meta.is_file():
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            m = {}
        if isinstance(m, dict):
            sid = str(m.get("session_id") or m.get("id") or "").strip()
            name = str(m.get("title") or m.get("name") or "").strip()
            if sid:
                return {"session_id": sid, "name": name, "path": str(rollout)}
    match = _ROLLOUT_UUID_RE.search(rollout.name)
    sid = match.group(1).lower() if match else ""
    return {"session_id": sid, "name": "", "path": str(rollout)}


def _codex_rollout_paths(codex_home: str = "") -> list[Path]:
    """递归枚举私有 CODEX_HOME/sessions/**/rollout-*.jsonl 路径（真实布局证据）。

    只认 rollout 文件——凭空 mkdir 的 sessions/<id> "假目录"一律不在扫描范围内。
    """
    sessions_root = codex_sessions_dir(codex_home)
    if not sessions_root.is_dir():
        return []
    return sorted(sessions_root.rglob("rollout-*.jsonl"))


def scan_codex_sessions(codex_home: str = "") -> list[dict]:
    """递归扫描私有 CODEX_HOME/sessions/**/rollout-*.jsonl，返回真实 session 标识。

    真实 Codex 布局（本机实测）是日期分层：
        ~/.codex/sessions/<year>/<month>/<day>/rollout-<ts>-<uuid>.jsonl
    （部分版本为 sessions/<uuid>/session_meta.json + rollout-*.jsonl）。本函数递归
    扫描所有 rollout-*.jsonl 证据，从 session_meta / 文件名解析真实 uuid/名称。
    只认 rollout 证据——凭空 mkdir 的 sessions/<id> "假目录"一律不认。
    """
    found: list[dict] = []
    for rollout in _codex_rollout_paths(codex_home):
        identity = _codex_session_identity(rollout)
        if identity.get("session_id") or identity.get("name"):
            found.append(identity)
    return found


# ---------------------------------------------------------------------------
# P4b：首启真实 session 回填——discover 扫描"新产生"rollout，解析 session_meta
# payload 的真实 id 与 cwd，仅在时间窗 + cwd 匹配 + 候选唯一时返回真实 id。
# ---------------------------------------------------------------------------

#: discover 时间窗下界的时钟偏差容忍（秒）——rollout mtime 略早于 spawn_ts
#: 仍视为"该次 spawn 新产生"（文件系统 mtime 与我们的时钟可能有秒级偏差）
_DISCOVER_CLOCK_SKEW = 5.0

#: 默认 discover 时间窗上界（秒）：成员首启后应在此窗口内写盘第一条 rollout
_DISCOVER_WINDOW_DEFAULT = 300.0


def _parse_rollout_session_meta(rollout: Path, max_lines: int = 20) -> dict | None:
    """读取 rollout 首部 session_meta 事件的 payload（真实 id / cwd / 线程来源）。

    真实 Codex rollout 首条事件恒为 type=session_meta，其 payload 带真实
    ``session_id``（resumable 会话 UUID）、``id``（本条 rollout 线程身份，
    本机实测恒等于文件名末段 uuid）与 ``cwd``（会话实际工作目录）。本函数只读
    rollout 前若干行，不读取消息正文（保护上下文隐私）。解析不到返回 None。
    """
    try:
        with open(rollout, encoding="utf-8", errors="replace") as f:
            for _ in range(max_lines):
                line = f.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "session_meta":
                    continue
                payload = obj.get("payload")
                if not isinstance(payload, dict):
                    return None
                return {
                    "session_id": str(payload.get("session_id") or "").strip(),
                    "rollout_id": str(payload.get("id") or "").strip(),
                    "cwd": str(payload.get("cwd") or "").strip(),
                    "thread_source": str(payload.get("thread_source") or "").strip(),
                }
            return None
    except OSError:
        return None


def _cwd_matches(cwd: str, workspace_dir: str) -> bool:
    """cwd 与期望工作目录精确匹配（normpath 优先，realpath 兜底符号链接）。

    严格拒绝不同目录——cwd 是 discover 防止"扫到别的成员/团队会话"的关键闸。
    任一为空 → 不匹配（无法确认 cwd 的证据一律不算候选）。
    """
    if not cwd or not workspace_dir:
        return False
    try:
        a = os.path.normpath(cwd)
        b = os.path.normpath(workspace_dir)
        if a == b:
            return True
        # 符号链接兜底：realpath 相同仍视为同一工作目录（绝不接受不同目录）
        return os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def discover_codex_session(
    spawn_ts: float,
    workspace_dir: str,
    codex_home: str = "",
    window_seconds: float = _DISCOVER_WINDOW_DEFAULT,
) -> dict:
    """在首启时间窗内发现 Codex 真实 session 并回填（P4b）。

    首启 managed codex 时**不要自造 uuid 当真实会话 id**——真实 id 只能来自
    codex 实际写盘的 rollout 证据。本函数递归扫描私有
    ``CODEX_HOME/sessions/**/rollout-*.jsonl``，解析各 rollout 首部
    session_meta 事件的 payload（真实 ``session_id`` / ``cwd``），**仅在以下
    条件同时成立**时返回真实 id：

      1. 时间窗：rollout mtime 落在 ``[spawn_ts - 偏差容忍, spawn_ts + window_seconds]``；
      2. cwd 匹配：payload.cwd 与 ``workspace_dir`` 精确匹配（见 ``_cwd_matches``）；
      3. 候选唯一：按真实 ``session_id`` 去重后恰好一个。

    歧义（多个不同 session）/ 缺失（无匹配）/ cwd 无法确认 → 结构化 fallback，
    调用方只做 checkpoint 续跑（**禁 --last 模糊恢复**）。返回：

        {"ok": True,  "session_id": 真实uuid, "path": rollout, "cwd": str, "spawned_ts": float}
        {"ok": False, "reason": str, "candidates": [...]}

    时间窗上界默认 300s：成员首启后应在此窗口内产生第一条 rollout 记录；
    超过该窗仍未发现 → 确定性缺失，调用方应停止重复扫描（过期标记）。
    """
    lower = spawn_ts - _DISCOVER_CLOCK_SKEW
    upper = spawn_ts + float(window_seconds)
    matches: list[dict] = []
    for rollout in _codex_rollout_paths(codex_home):
        try:
            mtime = rollout.stat().st_mtime
        except OSError:
            continue
        if not (lower <= mtime <= upper):
            continue
        meta = _parse_rollout_session_meta(rollout)
        if not meta or not meta["session_id"]:
            continue
        if not _cwd_matches(meta["cwd"], workspace_dir):
            continue
        matches.append({
            "session_id": meta["session_id"],
            "rollout_id": meta["rollout_id"],
            "path": str(rollout),
            "cwd": meta["cwd"],
            "thread_source": meta["thread_source"],
            "mtime": mtime,
        })
    if not matches:
        return {
            "ok": False,
            "reason": (
                "时间窗内未发现匹配 cwd 的真实 codex 会话"
                "（无实际 ID 只 checkpoint 续跑）"
            ),
            "candidates": [],
        }
    # 按真实 session_id 去重（subagent 线程共享父 session_id → 同一可恢复会话）
    unique: dict[str, dict] = {}
    for m in matches:
        unique.setdefault(m["session_id"], m)
    candidates = list(unique.values())
    if len(candidates) > 1:
        return {
            "ok": False,
            "reason": (
                f"发现 {len(candidates)} 个候选 codex 会话（歧义，只 checkpoint 续跑）"
            ),
            "candidates": candidates,
        }
    c = candidates[0]
    return {
        "ok": True,
        "session_id": c["session_id"],
        "path": c["path"],
        "cwd": c["cwd"],
        "spawned_ts": c["mtime"],
    }


def resolve_codex_session(session_id: str, codex_home: str = "") -> dict:
    """按精确 id / 名称在**真实** Codex session 布局中定位会话。

    只认递归扫描到的 rollout-*.jsonl 证据（session_meta 或文件名真实 uuid），
    绝不认"凭空 mkdir 的 sessions/<id> 假目录"（P4 最终硬门）。禁止模糊 cwd 恢复：
    必须精确匹配真实 uuid 或名称。返回结构化结果：

        {"ok": True,  "path": str, "session_id": str}   # session_id 为真实 uuid
        {"ok": False, "reason": str, "path": str}

    未找到匹配（无实际 ID）→ 调用方只做 checkpoint fallback。
    """
    if not (session_id or "").strip():
        return {"ok": False, "reason": "缺少 codex session_id（无实际 ID 只 checkpoint fallback）", "path": ""}
    target = str(session_id).strip().lower()
    for identity in scan_codex_sessions(codex_home):
        sid = (identity.get("session_id") or "").strip().lower()
        name = (identity.get("name") or "").strip().lower()
        if sid and sid == target:
            return {"ok": True, "path": identity["path"], "session_id": identity["session_id"]}
        if name and name == target:
            return {"ok": True, "path": identity["path"], "session_id": identity["session_id"]}
    sessions_root = codex_sessions_dir(codex_home)
    return {
        "ok": False,
        "reason": f"未找到匹配真实 codex 会话: {sessions_root}（禁止模糊 cwd 恢复，无实际 ID 只 checkpoint fallback）",
        "path": str(sessions_root),
    }


# ---------------------------------------------------------------------------
# 禁止复制 credentials / settings
# ---------------------------------------------------------------------------

def is_sensitive_path(path) -> bool:
    """路径任一段命中敏感名（credentials/settings/.claude.json 等）即视为敏感。"""
    parts = Path(path).parts
    return any(p in SENSITIVE_RESUME_NAMES for p in parts)


def reject_sensitive_paths(paths) -> str | None:
    """批量拒绝敏感路径：任何一条命中即返回错误原因，全部安全返回 None。

    session 恢复只允许转录/会话元数据文件；credentials / settings /
    auth 一律不得随 session 复制（防凭证泄露与配置漂移）。
    """
    for p in paths:
        if is_sensitive_path(p):
            return f"禁止复制敏感路径 {p}（credentials/settings 不得随 session 复制）"
    return None


# ---------------------------------------------------------------------------
# 结构化 fallback：resume 不可用时让调用方改用 task checkpoint
# ---------------------------------------------------------------------------

def _fallback(reason: str) -> dict:
    return {
        "available": False,
        "agent": "",
        "argv": [],
        "session_id": "",
        "fallback": {
            "reason": reason,
            "use_task_checkpoint": True,
            "message": f"{reason}；恢复不可用，请改用成员任务 checkpoint 续跑"
            "（勿空白重做，见恢复消息的 verify-then-continue 规则）。",
        },
    }


def build_resume_command(
    *,
    team_name: str,
    member_name: str,
    agent: str,
    workspace_dir: str,
    claude_home: str = "",
    codex_home: str = "",
    session_id: str = "",
) -> dict:
    """构造显式 resume 命令；不可用时返回结构化 fallback。

    返回（available=True）：
        {"available": True, "agent": "claude"|"codex", "argv": [...],
         "session_id": str, "fallback": None}

    返回（available=False）：
        {"available": False, "agent": "", "argv": [], "session_id": "",
         "fallback": {"reason": str, "use_task_checkpoint": True, "message": str}}

    ``session_id`` 必须是调用方**已持久化**的会话 id（初次 spawn 用
    ``new_session_id()`` 生成并保存，恢复时读取同一值）——resume 只能针对实际
    存在过的会话，绝不生成/猜测（B3）。完整校验链：feature flag → 已持久化
    session_id → 转录/私有 CODEX_HOME 精确定位 → 敏感路径闸 → 模糊参数闸。
    任何一步不满足都不构造命令，而是交回结构化 fallback（调用方使用 P1 task
    checkpoint 续跑，而非空白重做）。
    """
    if not resume_enabled():
        return _fallback("session resume 功能默认关闭（feature flag 未启用）")

    if not session_id:
        return _fallback("缺少已持久化 session_id（初次 spawn 需先 new_session_id 生成并持久化）")

    if agent == "claude":
        argv = claude_resume_argv(session_id)
        check = validate_transcript(session_id, workspace_dir, claude_home)
        if not check["ok"]:
            return _fallback(check["reason"])
        sensitive = reject_sensitive_paths([claude_home])
        if sensitive:
            return _fallback(sensitive)
    elif agent == "codex":
        check = resolve_codex_session(session_id, codex_home)
        if not check["ok"]:
            return _fallback(check["reason"])
        sensitive = reject_sensitive_paths([codex_home])
        if sensitive:
            return _fallback(sensitive)
        # resume 必须用真实 session uuid（可能由名称匹配解析而来），不是输入别名
        real_sid = check["session_id"]
        argv = codex_resume_argv(real_sid)
        session_id = real_sid
    else:
        return _fallback(f"不支持的 agent: {agent!r}（仅支持 claude / codex）")

    forbidden = reject_forbidden_resume_args(argv)
    if forbidden:
        return _fallback(forbidden)

    return {
        "available": True,
        "agent": agent,
        "argv": argv,
        "session_id": session_id,
        "fallback": None,
    }
