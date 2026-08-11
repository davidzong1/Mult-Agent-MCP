"""prompt_registry —— 成员/leader 身份 prompt 单一渲染源 + Claude/Codex 启动注入组装。

依据 docs/prompt_migration_fact_check.md（只读事实基线）的已确认技术路线：

  - **Claude Code**：唯一可靠 system 通道 = ``--append-system-prompt-file <path>``
    （/compact 免疫，每次启动含 resume 必带）。身份必须经 CLI 参数进 system 层，
    而不是只作为首条 user 消息（compact 摘要会摘除首条 user 消息 → 身份遗忘根因）。
  - **Codex**：无任何用户可控 system-prompt 通道；唯一自动装载持久指令文件 =
    AGENTS.md（每次启动含 resume 从磁盘重读）。身份固化落点 = 团队工作区
    AGENTS.md **角色中立段**（不写死具体成员/角色，防多角色串线 B2）。
  - **模板**：``prompts/leader.ts`` / ``prompts/members.ts`` 是文档模板（非 Python
    运行时载入源，无 TS 加载器）。本模块是 **Python 运行时单一渲染来源**，字段
    契约对齐 members.ts 的 ``MemberPromptVars``（team/member_name/role/agent/
    mode/leader/leaderType/teamDir/shareDir/task/recoverySection）。
  - **不采用 [system] 伪标签**（fact-check §8 裁决）：文件正文为普通指令文本，
    持久性由注入通道（append flag / AGENTS.md 自动重载）决定，非内容标记。

数据访问：经 ``common.data_layer`` 读取数据文件（尊重测试隔离 set_data_file），
模块顶部不 import ``mult_agent_mcp``（避免循环依赖）；leader 身份文本惰性复用
``mult_agent_mcp._leader_system_prompt``（调用时解析，此时主模块已加载完成）。
"""

import atexit
import json
import os
import tempfile
import threading

from common import data_layer

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
# 成员静态身份段（纯文本，对齐 members.ts memberSystemPrompt 字段契约）
# ---------------------------------------------------------------------------

def render_member_identity(team_name: str, member_name: str) -> str:
    """渲染成员静态身份段（Claude append 文件正文）。

    字段契约对齐 prompts/members.ts 的 MemberPromptVars：
    team/member_name/role/agent/mode/leader/leaderType/teamDir/shareDir。
    交付合约/顺序义务惰性复用 mult_agent_mcp 单一措辞源（防漂移）；动态恢复段
    (recoverySection) 不在此——恢复上下文由服务端每次启动重渲染注入。
    """
    team, member = _team_and_member(team_name, member_name)
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
    # 交付合约 + 顺序义务：惰性复用 mult_agent_mcp 单一措辞源，保证与
    # _build_member_initial_context / members.ts 模板逐字一致（防漂移）。
    try:
        from mult_agent_mcp import _member_delivery_contract
        delivery = _member_delivery_contract()
    except Exception:
        delivery = ""
    if delivery:
        lines.extend(["", delivery])
    return "\n".join(lines)


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


def claude_identity_file(team_name: str, member_name: str, *, leader: bool = False) -> str:
    """渲染身份文本并写入临时文件，返回 ``--append-system-prompt-file`` 的路径。

    ``leader=True`` 时渲染 leader 系统提示（惰性复用 mult_agent_mcp 单一来源）；
    否则渲染成员静态身份段。任何渲染异常回退确定性默认路径（文件恒存在，
    不因身份渲染失败阻塞 spawn）。
    """
    try:
        if leader:
            from mult_agent_mcp import _leader_system_prompt
            text = _leader_system_prompt(team_name)
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

def codex_agents_md(team_name: str) -> str:
    """渲染 Codex 团队 AGENTS.md 角色中立身份段。

    事实基线（fact-check §2.2）：Codex 无 system-prompt 通道，AGENTS.md 是唯一
    自动装载持久指令文件。只放团队级固定身份与中性协作约束，**不放具体成员/角色**
    （共享文件多角色串线面 B2）；抗 compact/resume（磁盘自动重载）。
    """
    data = _load_data()
    team = data.get("teams", {}).get(team_name, {})
    share_dir = team.get("context_dir") or "由 leader 提供"
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
