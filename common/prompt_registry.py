"""prompt_registry —— 成员/leader 身份 prompt 单一渲染源 + Claude/Codex 启动注入组装。

依据 docs/prompt_migration_fact_check.md（只读事实基线）的已确认技术路线：

  - **Claude Code**：唯一可靠 system 通道 = ``--append-system-prompt-file <path>``
    （/compact 免疫，每次启动含 resume 必带）。身份必须经 CLI 参数进 system 层，
    而不是只作为首条 user 消息（compact 摘要会摘除首条 user 消息 → 身份遗忘根因）。
  - **Codex**：无任何用户可控 system-prompt 通道；唯一自动装载持久指令文件 =
    AGENTS.md（每次启动含 resume 从磁盘重读）。身份固化落点 = 团队工作区
    AGENTS.md **角色中立段**（不写死具体成员/角色，防多角色串线 B2）。
  - **模板**：``prompts/leader.ts`` / ``prompts/members.ts`` 是**运行时权威模板源**
    （经 ``common.prompt_template`` 纯 Python 解析，无 Node/TS runtime）。本模块经
    ``prompt_template.render_template`` 从 .ts 渲染通道函数（``@channel system`` 段走
    真实 system 通道）；模板缺失/坏模板回退内建 Python 内联文本（A4：不静默丢身份、
    不输出空串），并在 stderr + 共享上下文 results.jsonl 记录
    ``prompt_template_parse_error`` 诊断事件。字段契约对齐 members.ts 的
    ``MemberPromptVars``（team/member_name/role/agent/mode/leader/leaderType/
    teamDir/shareDir/task/recoverySection）。
  - **不采用 [system] 伪标签**（fact-check §8 裁决）：文件正文为普通指令文本，
    持久性由注入通道（append flag / AGENTS.md 自动重载）决定，非内容标记。

数据访问：经 ``common.data_layer`` 读取数据文件（尊重测试隔离 set_data_file），
模块顶部不 import ``mult_agent_mcp``（避免循环依赖）；leader 身份文本惰性复用
``mult_agent_mcp._leader_system_prompt``（调用时解析，此时主模块已加载完成）。
"""

import atexit
import json
import os
import sys
import tempfile
import threading
from pathlib import Path

from common import data_layer
from common import prompt_template as _pt

# 已创建的临时身份文件路径（供 atexit 清理，防残留注入面 R3）。
_identity_files: set[str] = set()
_identity_lock = threading.Lock()

# 确定性默认身份文件：builder 未显式传身份文件时使用（直接调用/单测兜底）。
_DEFAULT_IDENTITY_PATH = os.path.join(tempfile.gettempdir(), "mcp_identity_default.md")
_DEFAULT_IDENTITY_TEXT = (
    "你是 Multi-Agent MCP 团队的成员（身份文件占位默认值）。\n"
    "生产 spawn 会经 prompt_registry.claude_identity_file() 注入真实团队/成员身份；\n"
    "此默认文件仅在直接调用启动参数构造器（如单元测试）时被引用。"
)


def _load_data() -> dict:
    try:
        path = data_layer.get_data_file()
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _team_and_member(team_name: str, member_name: str) -> tuple[dict, dict]:
    data = _load_data()
    team = data.get("teams", {}).get(team_name, {})
    member = team.get("members", {}).get(member_name, {})
    return team, member


# ---------------------------------------------------------------------------
# prompts/*.ts 运行时权威源接线（解析层 common/prompt_template，无 Node）
# ---------------------------------------------------------------------------

def _prompts_dir() -> Path:
    """prompts 模板目录解析 hook（registry 侧）。

    经 ``prompt_template._prompts_dir()`` 解析（模块相对 __file__ + env 逃生阀）；
    测试可 patch 本属性注入临时模板（tester D 组契约 hook）。
    """
    return _pt._prompts_dir()


def _record_fallback(team_name: str, ts_name: str, fn_name: str, err: Exception) -> None:
    """模板解析/渲染失败诊断：stderr 一行 + 共享上下文 results.jsonl 事件（best-effort）。

    spawn 路径不因模板问题崩溃：任何异常在记录前即被吞掉（纯观测，无副作用）。
    """
    msg = f"{ts_name}.ts {fn_name} 渲染失败，回退内建文本: {err}"
    try:
        print(f"[prompt_registry] {msg}", file=sys.stderr)
    except Exception:
        pass
    try:
        data = _load_data()
        ctx = (data.get("teams", {}).get(team_name, {}) or {}).get("context_dir")
        if not ctx:
            return
        import datetime
        entry = {
            "event": "prompt_template_parse_error",
            "file": f"{ts_name}.ts",
            "channel": fn_name,
            "err": str(err),
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
        }
        os.makedirs(ctx, exist_ok=True)
        with open(os.path.join(ctx, "results.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _render_ts_or_none(ts_name: str, fn_name: str, vars_: dict, team_name: str,
                       *, require_system: bool = False) -> str | None:
    """尝试从 ``prompts/{ts_name}.ts`` 渲染通道函数；失败记录诊断并返回 None。

    - ``require_system=True``（leader 分支）：仅当目标函数已标注 ``@channel system``
      才渲染，否则静默返回 None——未迁移模板不被当作 system 渲染，避免解析失败噪音；
    - 任何失败（缺失/语法错/占位符越界）→ ``_record_fallback`` + 返回 None，
      调用方安全回退内建文本，spawn 路径永不因模板问题崩溃。
    """
    try:
        if require_system:
            parsed = _pt.load_parsed(ts_name, prompts_dir=_prompts_dir())
            fn = parsed.functions.get(fn_name)
            if fn is None or fn.channel != "system":
                return None
        return _pt.render_template(ts_name, fn_name, vars_, prompts_dir=_prompts_dir())
    except Exception as e:
        _record_fallback(team_name, ts_name, fn_name, e)
        return None


def render_channel(ts_name: str, fn_name: str, vars_: dict, team_name: str) -> str | None:
    """渲染 ``prompts/{ts_name}.ts`` 的指定通道函数（**user 通道模板**专用）。

    供 mult_agent_mcp / tui 在 initial / recovery / task / leader-initial 等
    user 通道接线：prompts/*.ts 为运行时可编辑权威源，渲染失败返回 None（已记录
    stderr / results.jsonl 诊断），调用方安全回退既有 Python 内建文本（A4）。

    注意：本函数不校验 ``@channel``（调用方显式指定要渲染的函数）；请勿把
    ``@channel system`` 的成员/leader 身份函数当作 user 消息渲染（system 身份
    应走 ``claude_identity_file`` / ``ensure_codex_agents_md`` 真 system 通道）。
    """
    return _render_ts_or_none(ts_name, fn_name, vars_, team_name)


# ---------------------------------------------------------------------------
# 成员静态身份段（纯文本，对齐 members.ts memberSystemPrompt 字段契约）
# ---------------------------------------------------------------------------

def _member_identity_vars(team: dict, member: dict, team_name: str, member_name: str) -> dict:
    role = member.get("role") or "member"
    agent = member.get("agent") or team.get("default_agent") or "claude"
    leader = team.get("leader") or "direct"
    leader_type = team.get("leader_type") or "direct"
    mode = member.get("mode") or "manual"
    return {
        "teamName": team_name,
        "memberName": member_name,
        "role": role,
        "agent": agent,
        "mode": mode,
        "leader": leader,
        "leaderType": leader_type,
        "teamDir": team.get("workspace_dir") or "",
        "shareDir": team.get("context_dir") or "",
        # 动态段默认空：静态 system 通道不引用（C4），但兼容测试模板透传占位。
        "task": "",
        "recoverySection": "",
    }


def _render_member_identity_inline(team: dict, member: dict,
                                   team_name: str, member_name: str) -> str:
    """内建回退：members.ts 缺失/坏模板时使用的 Python 内联身份文本（与 .ts 静态段逐字一致）。"""
    role = member.get("role") or "member"
    agent = member.get("agent") or team.get("default_agent") or "claude"
    leader = team.get("leader") or "direct"
    leader_type = team.get("leader_type") or "direct"
    mode = member.get("mode") or "manual"
    team_dir = team.get("workspace_dir") or ""
    share_dir = team.get("context_dir") or ""
    lines = [
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的成员。",
        f"你的团队成员身份绑定: team='{team_name}', member_name='{member_name}', role='{role}', agent='{agent}'。",
        f"团队成员表中名为 '{member_name}' 的成员记录就是你本人；不要冒用其他成员或 leader 的身份。",
        f"**注意** 你不是 leader：团队 leader 是 '{leader}' ({leader_type})，由它负责分配任务与协调；",
        "不要把 leader 的成员记录当作可分配对象，也不要向 leader 分配子任务，也不要把自己当作 leader 去调度其他成员。",
        "leader 记录在团队成员表中与普通成员并列，但它是协调者，不是你可指挥的平级成员。",
        f"模式: {mode}; Leader: {leader} ({leader_type})",
        f"共享工作目录: {team_dir}",
        f"共享上下文区: {share_dir}",
        "常用工具: member_report_result, member_read_shared, member_send_message, "
        "member_acquire_file_lock, member_release_file_lock, member_submit_patch。",
        "只读取完成当前任务必需的文件；信息不足时先向 leader 提问。",
    ]
    return "\n".join(lines)


def _append_delivery_contract(text: str) -> str:
    """交付合约 + 顺序义务：惰性复用 mult_agent_mcp 单一措辞源，保证与
    _build_member_initial_context / members.ts 模板逐字一致（防漂移）。"""
    try:
        from mult_agent_mcp import _member_delivery_contract
        delivery = _member_delivery_contract()
    except Exception:
        delivery = ""
    if delivery:
        return text + "\n\n" + delivery
    return text


def render_member_identity(team_name: str, member_name: str) -> str:
    """渲染成员静态身份段（Claude append 文件正文）。

    权威模板源：prompts/members.ts 的 memberSystemPrompt（@channel system，无动态
    字段）；解析失败（缺文件/坏模板/占位符越界）回退内建内联文本（A4：不静默丢身份、
    不输出空串），并记录 stderr / results.jsonl 诊断事件。交付合约惰性复用
    mult_agent_mcp 单一措辞源追加（防漂移）；动态恢复段 (recoverySection) 不在此——
    恢复上下文由服务端每次启动重渲染注入。
    """
    team, member = _team_and_member(team_name, member_name)
    text = _render_ts_or_none(
        "members", "memberSystemPrompt",
        _member_identity_vars(team, member, team_name, member_name), team_name)
    if text is None:
        text = _render_member_identity_inline(team, member, team_name, member_name)
    return _append_delivery_contract(text)


# ---------------------------------------------------------------------------
# Claude：--append-system-prompt-file 身份文件（R3 临时文件生命周期）
# ---------------------------------------------------------------------------

def write_identity_file(text: str, *, prefix: str = "mcp_identity_") -> str:
    """写身份文本到临时文件并登记清理，返回路径。

    mkstemp 创建 0600 私有文件（防共享区可写注入面）；atexit 清理防残留。
    """
    fd, path = tempfile.mkstemp(prefix=prefix, suffix=".md", text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception:
        try:
            os.unlink(path)
        except OSError:
            pass
        raise
    with _identity_lock:
        _identity_files.add(path)
    return path


def _render_leader_system(team_name: str) -> str:
    """渲染 leader 系统提示（Claude append 文件正文）。

    权威模板源：prompts/leader.ts 的 leaderSystemPrompt（@channel system，静态无
    teammates/task/recoverySection——修复原实现把动态 recovery 冻结进 system 文件的
    缺陷）。leader.ts 未迁移（函数缺失或非 @channel system）时静默回退
    mult_agent_mcp._leader_system_prompt 单一来源，行为不变。
    """
    team, _member = _team_and_member(team_name, "")
    leader_name = team.get("leader") or "direct"
    leader_member = team.get("members", {}).get(leader_name) or {}
    vars_ = {
        "teamName": team_name,
        "leaderMemberName": leader_name,
        "leaderRole": leader_member.get("role") or "leader",
        "leaderAgent": leader_member.get("agent") or team.get("default_agent") or "claude",
        "defaultAgent": team.get("default_agent") or "claude",
        "teamDir": team.get("workspace_dir") or "",
        "shareDir": team.get("context_dir") or "",
    }
    text = _render_ts_or_none("leader", "leaderSystemPrompt", vars_, team_name,
                              require_system=True)
    if text is None:
        from mult_agent_mcp import _leader_system_prompt
        text = _leader_system_prompt(team_name)
    return text


def claude_identity_file(team_name: str, member_name: str, *, leader: bool = False) -> str:
    """渲染身份文本并写入临时文件，返回 ``--append-system-prompt-file`` 的路径。

    ``leader=True`` 时渲染 leader 系统提示（优先 leader.ts ``leaderSystemPrompt``，
    未迁移回退 mult_agent_mcp 单一来源）；否则渲染成员静态身份段。任何渲染异常
    回退确定性默认路径（文件恒存在，不因身份渲染失败阻塞 spawn）。
    """
    try:
        if leader:
            text = _render_leader_system(team_name)
        else:
            text = render_member_identity(team_name, member_name)
        return write_identity_file(text)
    except Exception:
        return default_claude_identity_path()


def default_claude_identity_path() -> str:
    """确定性默认身份文件路径（builder 未显式传身份文件时用）。

    仅作为直接调用启动参数构造器（如单元测试）的兜底，保证 argv 恒携带 append
    flag；生产 spawn 点必须经 ``claude_identity_file()`` 传入真实身份文件。
    首次访问时写入团队中立的占位文本，避免指向不存在文件导致 CLI 启动报错。
    """
    try:
        if not os.path.exists(_DEFAULT_IDENTITY_PATH):
            os.makedirs(os.path.dirname(_DEFAULT_IDENTITY_PATH), exist_ok=True)
            with open(_DEFAULT_IDENTITY_PATH, "w", encoding="utf-8") as f:
                f.write(_DEFAULT_IDENTITY_TEXT)
    except OSError:
        pass
    return _DEFAULT_IDENTITY_PATH


# ---------------------------------------------------------------------------
# Codex：团队工作区 AGENTS.md 角色中立身份段（唯一自动装载持久指令文件）
# ---------------------------------------------------------------------------

def _codex_agents_md_inline(team_name: str, share_dir: str) -> str:
    """内建回退：members.ts codexAgentsSection 缺失/坏模板时的 Python 内联文本（角色中立）。"""
    lines = [
        "# Multi-Agent MCP 团队约束",
        "",
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的成员（团队协作环境）。",
        f"本目录是团队共享工作目录；共享上下文区: {share_dir}",
        "",
        "协作规则:",
        "- 使用 MCP 工具与团队成员协作：member_report_result 回报结果、member_read_shared 读取共享上下文、member_send_message 与成员/leader 通信。",
        "- 具体角色/成员身份由 leader 派单消息与成员上下文注入；本文件仅承载团队中立的协作约束，不绑定具体成员。",
        "- 任务完成后第一个动作必须是 member_report_result 回报；回报后按约定执行 /compact。",
        "- 只读取完成当前任务必需的文件；信息不足时先向 leader 提问。",
    ]
    return "\n".join(lines)


def codex_agents_md(team_name: str) -> str:
    """渲染 Codex 团队 AGENTS.md 角色中立身份段。

    权威模板源：prompts/members.ts 的 codexAgentsSection（@channel system，角色中立）；
    解析失败回退内建内联文本。事实基线（fact-check §2.2）：Codex 无 system-prompt 通道，
    AGENTS.md 是唯一自动装载持久指令文件。只放团队级固定身份与中性协作约束，
    **不放具体成员/角色**（共享文件多角色串线面 B2）；抗 compact/resume（磁盘自动重载）。
    """
    team, _member = _team_and_member(team_name, "")
    share_dir = team.get("context_dir") or "由 leader 提供"
    text = _render_ts_or_none("members", "codexAgentsSection",
                              {"teamName": team_name, "shareDir": share_dir}, team_name)
    if text is None:
        text = _codex_agents_md_inline(team_name, share_dir)
    return text


def ensure_codex_agents_md(team_name: str, team_dir: str) -> str:
    """确保团队显式 workspace_dir 下存在 AGENTS.md（团队中立身份段），返回文件路径。

    安全规则（fact-check §7 B3，reviewer P1）：AGENTS.md 只写入团队**显式
    workspace_dir** 指向的目录——Codex 从启动 cwd（``-C team_dir``）发现 AGENTS.md，
    写入落点必须与发现语义一致。以下两种情况 **fail-closed 不写入**（返回 ""）：
      1) 团队无 workspace_dir：``_team_dir`` 会回落项目根，写入即污染用户仓库根；
      2) workspace_dir 恰为项目根：写入同样污染用户仓库（该目录下用户正常
         Codex 会话会自动装载团队身份 → B3 角色串线/污染）。
    fail-closed 后 Codex 成员退回事实基线 §8 档 3（首条消息注入身份），不抗
    compact 但零污染。

    幂等：文件已含本团队标识块则不重复追加；**保留用户已有 AGENTS.md 内容**
    （在文件尾部追加，不覆盖、不破坏用户自有内容）。
    """
    team, _member = _team_and_member(team_name, "")
    ws = (team.get("workspace_dir") or "").strip()
    if not ws:
        # 无显式 workspace → 生效 cwd 是项目根回落 → fail-closed，零写入
        return ""
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.abspath(ws) == os.path.abspath(project_root):
        # 显式指向项目根 → 仍污染用户仓库 → fail-closed
        return ""
    marker = f"Multi-Agent MCP 团队 '{team_name}'"
    path = os.path.join(ws, "AGENTS.md")
    existing = ""
    try:
        with open(path, encoding="utf-8") as f:
            existing = f.read()
    except OSError:
        existing = ""
    if marker in existing:
        return path
    block = codex_agents_md(team_name)
    try:
        os.makedirs(ws, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            f.write("\n" + block + "\n")
    except OSError:
        return ""
    return path


# ---------------------------------------------------------------------------
# 清理
# ---------------------------------------------------------------------------

def _cleanup_identity_files() -> None:
    with _identity_lock:
        for path in list(_identity_files):
            try:
                os.unlink(path)
            except OSError:
                pass
        _identity_files.clear()


atexit.register(_cleanup_identity_files)
