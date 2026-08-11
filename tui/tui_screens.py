"""
Multi-Agent MCP — Team Manager TUI Screens
===================================
基于 textual 的终端团队管理工具。

功能:
  - 可视化创建团队、管理成员、指定 Leader
  - 管理 MCP Server 的启动/停止/重启
  - 一键自动配置 Claude Code 与 Codex CLI 的 MCP 连接
  - 数据自动同步到 teams_data.json，与 MCP Server 完全兼容

用法:
    python team_manger.py

快捷键:
    全局:    1 MCP服务   2 MCP配置   3 重启MCP
    主界面:  A 添加团队   D 删除团队   Enter 查看详情   Q 退出
    详情页:  A 添加成员   R 移除成员   E 编辑成员   L 指定Leader   Esc/Ctrl+Q 返回

模块结构 (task3 重构):
  工具函数从 common/ 模块导入（config, data_layer, tmux_utils, mcp_config, mcp_daemon）。
  TUI 类保留在本地（后续渐进迁移到 tui/ 子目录）。
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.events import Resize
from textual.screen import Screen
from textual.worker import Worker, WorkerState

# work: textual.work 的包装，只改一个默认值 exit_on_error=False。
# Textual 默认让 worker 异常终止整个 App —— 一次保存失败就丢掉全部会话。
from tui.tui_worker import work
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    Static,
)

from member_status import format_member_activity_status

from common.config import (
    server_url as _server_url,
    default_workspace_dir as _default_workspace_dir,
)
from common.leader_recovery import build_leader_recovery_section, leader_has_unfinished_work
from common.data_layer import (
    team_workspace_dir,
    team_context_dir,
    validate_context_path as _validate_context_path,
    cleanup_team_artifacts,
    mark_legacy_team_deleted,
    get_data_file,
)
from common.atomic_write import atomic_json_write
from common import classifier_fallback
from common import prompt_registry
from common.tmux_utils import (
    find_tmux as _find_tmux,
    tmux_run as _tmux_run,
    run_command as _run,
    tmux_session_name as _tmux_session,
    find_tmux_session as _find_tmux_session,
    member_window_target as _member_window_target,
    tmux_session_alive,
    get_member_terminal_status,
    current_tmux_session as _current_tmux_session,
    codex_command as _codex_command,
    claude_agent_args as _claude_agent_args,
    member_mode as _member_mode,
    send_keys as _send_keys,
    agent_type,
    resolve_agent_model,
    resolve_member_effort,
    is_claude as _is_claude,
    is_codex as _is_codex,
    get_proxy_env_prefix,
    get_agent_user_env_prefix,
    build_agent_user_claude_settings,
    claude_agent_user_launch,
    merge_env_prefixes,
    member_proxy_enabled,
    member_proxy_mode,
    list_agent_users as _list_agent_users,
    member_spawn_lock as _member_spawn_lock,
    member_window_state as _member_window_state,
    migrate_agent_users_global_file as _migrate_agent_users_global_file,
    AGENT_USER_NONE,
)
from common.mcp_config import (
    claude_mcp_configured as _common_claude_mcp_configured,
    configure_claude_mcp as _common_configure_claude_mcp,
    codex_mcp_registered as _codex_mcp_configured,
    configure_codex_mcp,
    write_claude_mcp,
    write_claude_permissions,
    CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS,
    CLAUDE_LEADER_TOOL_ALLOW_PATTERNS,
    CLAUDE_MEMBER_TOOL_ALLOW_PATTERNS,
    MCP_SERVER_NAME as MCP_SERVER_NAME_CONF,
)
from common.mcp_daemon import (
    mcp_server_status,
    start_mcp_server,
    stop_mcp_server,
    restart_mcp_server,
)

def _build_tui_recovery_message(team: dict, member_name: str, info: dict, team_name: str) -> str:
    """构建 TUI 侧成员终端恢复时的结构化上下文消息（与 MCP 侧格式一致）。"""
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
        "   member_report_result     - 回传任务结果",
        "   member_list_shared_files - 列出共享文件",
        "   member_send_message      - 向其他成员发送消息",
        "",
        "💡 请基于以上上下文继续工作，或等待 leader 分配新任务。",
        "=" * 50,
    ])
    return "\n".join(lines)

PROJECT_DIR = Path(__file__).resolve().parent.parent

def _mcp_home() -> Path:
    env = os.environ.get("MULT_AGENT_MCP_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".mult_agent_mcp"

MCP_HOME = _mcp_home()
MCP_HOME.mkdir(parents=True, exist_ok=True)

DEFAULT_DATA_FILE = MCP_HOME / "teams_data.json"
SERVER_SCRIPT = PROJECT_DIR / "mult_agent_mcp.py"        # 必须在项目根目录
SERVER_PID_FILE = MCP_HOME / "mcp_server.pid"
SERVER_LOG_FILE = MCP_HOME / "mcp_server.log"
TEAM_WORKSPACES_DIR = PROJECT_DIR / ".team_workspaces"
SHARE_CONTEXT_DIR = MCP_HOME / "contexts"
SHARE_WORKSPACE_DIR = PROJECT_DIR / "share_work_space"
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
MCP_SERVER_NAME_CONF = "mult-agent-mcp"

_OLD_DATA_FILE = PROJECT_DIR / "teams_data.json"
_OLD_SHARE_CONTEXT_DIR = PROJECT_DIR / "share_context_space"

AGENT_CHOICES = [
    ("claude · Claude Code", "claude"),
    ("codex  · Codex CLI", "codex"),
    ("custom · 自定义命令", "custom"),
]

def load_data(path: Path | None = None) -> dict:
    # path 默认 None：函数体内动态解析，使 data_layer.set_data_file() 的测试覆盖生效
    # （默认参数在导入时求值会绑定真实路径，导致测试写入真实数据文件）
    path = Path(path) if path else get_data_file()
    if not path.exists() and path == DEFAULT_DATA_FILE and _OLD_DATA_FILE.exists():
        _migrate_data_to_mcp_home()

    if not path.exists():
        return {"teams": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_data(data: dict, path: Path | None = None) -> None:
    atomic_json_write(Path(path) if path else get_data_file(), data)


def _tmux_window_records(session: str) -> list[dict[str, str]]:
    rc, out, _ = _tmux_run([
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


def _remember_member_window_id(team_name: str, member_name: str, session: str, window_name: str | None = None) -> str:
    records = _tmux_window_records(session)
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


def _sync_team_terminal_state(team_name: str) -> bool:
    alive = _find_tmux_session(team_name) is not None
    data = load_data()
    team = data.get("teams", {}).get(team_name)
    if team is not None and bool(team.get("terminals_active")) != alive:
        team["terminals_active"] = alive
        save_data(data)
    return alive


def _leader_system_prompt(team_name: str, task: str = "") -> str:
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

    team_dir = team.get("workspace_dir") or str(Path(_default_workspace_dir()).resolve())
    share_dir = team.get("context_dir") or str((SHARE_CONTEXT_DIR / team_name).resolve())
    lines = [
        f"你是 Multi-Agent MCP 团队 '{team_name}' 的 leader。",
        f"你的团队成员身份: member_name='{leader or '(未设置)'}', role='{leader_role}', agent='{leader_agent}'。",
        f"leader_list_team 中名为 '{leader or '(未设置)'}' 且标记为 leader 的成员记录就是你本人，不是外部成员。",
        "**注意** 不要把自己的 leader 成员记录当作可分配对象；不要向自己分配子任务，也不要为了排除自己而剔除 leader 身份。",
        f"创建新成员时默认必须使用团队 default_agent='{default_member_agent}'；不要把你自己的 agent='{leader_agent}' 当作新成员默认 agent。",
        "只有用户明确要求覆盖 agent 时，才在 add_member/leader_add_member 中设置 use_explicit_agent=True。",
        "必须使用本项目 MCP 工具协调已有团队成员，不要使用 Codex 内置 spawn_agent / sub-agent 代替团队成员。",
        "开始后先调用 leader_list_team 查看成员，再用 leader_assign_subtask、leader_broadcast 等 leader_* 工具分配任务。",
        f"团队共享工作目录: {team_dir}",
        f"团队共享上下文区: {share_dir}",
    ]
    # 复用 MCP 侧同一份职责约束，避免两份拷贝漂移
    from mult_agent_mcp import leader_duty_prompt

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


def _record_leader_reentry(team: dict) -> None:
    import datetime

    if leader_has_unfinished_work(team):
        team["leader_recovery_count"] = int(team.get("leader_recovery_count", 0)) + 1
        team["leader_last_reentry_ts"] = datetime.datetime.now().isoformat()
        team["leader_work_state"] = "active"
    else:
        team["leader_work_state"] = "idle"


def _remove_team_from_legacy_data_file(team_name: str) -> None:
    if not _OLD_DATA_FILE.exists():
        return
    try:
        with open(_OLD_DATA_FILE, encoding="utf-8") as f:
            data = json.load(f)
        teams = data.get("teams", {})
        if team_name not in teams:
            return
        del teams[team_name]
        deleted = data.setdefault("_deleted_legacy_teams", {})
        if isinstance(deleted, dict):
            deleted[team_name] = True
        atomic_json_write(_OLD_DATA_FILE, data)
    except Exception:
        pass


def _migrate_data_to_mcp_home() -> None:
    """将旧 PROJECT_DIR/teams_data.json 迁移到 ~/.mult_agent_mcp/。"""
    import shutil as _shutil

    if not _OLD_DATA_FILE.exists():
        return
    if DEFAULT_DATA_FILE.exists():
        return

    MCP_HOME.mkdir(parents=True, exist_ok=True)
    # 读取旧数据，用 0600 原子写入新位置（不进 copy2 保留宽松权限）
    try:
        with open(_OLD_DATA_FILE, "r", encoding="utf-8") as f:
            seed = json.load(f)
    except Exception:
        seed = {"teams": {}}
    atomic_json_write(DEFAULT_DATA_FILE, seed)

    try:
        with open(DEFAULT_DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return

    changed = False
    old_base = str(_OLD_SHARE_CONTEXT_DIR)
    for team_name, team in data.get("teams", {}).items():
        old_ctx = team.get("context_dir", "")
        if old_ctx and old_ctx.startswith(old_base):
            team["context_dir"] = str(SHARE_CONTEXT_DIR / team_name)
            changed = True

    if changed:
        atomic_json_write(DEFAULT_DATA_FILE, data)

    if _OLD_SHARE_CONTEXT_DIR.is_dir():
        SHARE_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        for item in _OLD_SHARE_CONTEXT_DIR.iterdir():
            dst = SHARE_CONTEXT_DIR / item.name
            if item.is_dir() and not dst.exists():
                try:
                    _shutil.copytree(str(item), str(dst))
                except Exception:
                    pass

def _team_workspace(team_name: str) -> Path:
    """团队工作目录（优先使用 teams_data.json 中的配置）。"""
    configured = load_data().get("teams", {}).get(team_name, {}).get("workspace_dir")
    return Path(configured).expanduser().resolve() if configured else Path(_default_workspace_dir()).resolve()

def _team_context_dir(team_name: str) -> Path:
    """团队共享上下文目录（优先使用 teams_data.json 中的配置）。"""
    configured = load_data().get("teams", {}).get(team_name, {}).get("context_dir")
    return Path(configured).expanduser().resolve() if configured else (SHARE_CONTEXT_DIR / team_name).resolve()

def _claude_mcp_configured(team_name: str) -> bool:
    return _common_claude_mcp_configured(_team_workspace(team_name))

def configure_claude_mcp(team_name: str) -> tuple[bool, str]:
    try:
        return True, _common_configure_claude_mcp(team_name, _team_workspace(team_name))
    except Exception as e:
        return False, f"❌ Claude MCP 配置失败: {e}"

def configure_all_claude_mcp() -> list[tuple[str, bool, str]]:
    return [
        (name, *configure_claude_mcp(name))
        for name in load_data().get("teams", {})
    ]

def _all_teams_claude_status() -> dict[str, bool]:
    """检查所有团队的 Claude MCP 配置状态。"""
    return {name: _claude_mcp_configured(name) for name in load_data().get("teams", {})}

def tmux_spawn(command: str, title: str = "") -> tuple[bool, str]:
    """
    在当前 TUI 所在 tmux session 中分屏执行命令。
    split-window 默认会切到新 pane，适合远程连接场景直接查看。
    """
    current_session = _current_tmux_session()
    if not current_session:
        return False, "当前 TUI 不在 tmux 中"

    keep_open_command = (
        f"{command}; "
        "status=$?; "
        "printf '\\n[tmux_spawn] 命令已结束，退出码: %s。按 Ctrl+D 关闭此窗格。\\n' \"$status\"; "
        "exec ${SHELL:-/bin/sh}"
    )
    rc, _, err = _tmux_run(["split-window", "-h", keep_open_command])
    if rc != 0:
        return False, f"tmux 分屏失败: {err}"

    if title:
        _tmux_run(["select-pane", "-T", title])
    return True, f"已在当前 tmux session '{current_session}' 中分屏打开"

def _reattaching_tmux_attach_command(tmux: str, session: str) -> str:
    quoted_tmux = shlex.quote(tmux)
    quoted_session = shlex.quote(session)
    return (
        "trap 'exit 0' INT TERM; "
        f"while {quoted_tmux} has-session -t {quoted_session} 2>/dev/null; do "
        f"env -u TMUX {quoted_tmux} attach -t {quoted_session}; "
        "status=$?; "
        f"{quoted_tmux} has-session -t {quoted_session} 2>/dev/null || break; "
        "printf '\\n[tmux_spawn] 已从团队终端脱离或 attach 返回(%s)，2 秒后重新进入。按 Ctrl+C 停止自动重连。\\n' \"$status\"; "
        "sleep 2; "
        "done"
    )

def _confirm_prompt_submission(session: str, window: str, delay: float = 0.35) -> tuple[int, str]:
    """Send a follow-up Enter for CLIs that receive text before their input loop is ready."""
    if delay > 0:
        import time
        time.sleep(delay)
    rc, _, err = _tmux_run(["send-keys", "-t", f"{session}:{window}", "Enter"])
    return rc, err if rc != 0 else ""


def _inject_claude_leader_prompt(session: str, leader: str, team_name: str) -> tuple[int, str]:
    """向 Claude leader 终端注入团队提示，等待 CLI 初始化完成以避免竞态。

    与 MCP Server 侧行为一致：先等待 2 秒确保 Claude CLI 启动完毕，
    再通过 send_keys 发送 leader_system_prompt，最后按 Enter 提交。

    返回 (rc, err_msg)，rc=0 表示成功。
    """
    import time
    # 等待 Claude CLI 完成初始化（对齐 MCP Server 侧 time.sleep(2)）
    time.sleep(2.0)
    rc, err = _send_keys(session, leader, _leader_system_prompt(team_name))
    if rc != 0:
        return rc, f"向 Claude leader 注入团队提示失败: {err}"
    rc, err = _confirm_prompt_submission(session, leader)
    if rc != 0:
        return rc, f"向 Claude leader 确认团队提示失败: {err}"
    return 0, ""


def _leader_terminal_restart_blocked(team_name: str, team: dict) -> bool:
    """Return whether a live leader window must be protected from restart."""
    leader = team.get("leader", "")
    return bool(
        leader
        and leader_has_unfinished_work(team)
        and _member_window_target(team_name, leader)
    )


def launch_terminals(team_name: str) -> tuple[bool, str]:
    """
    为团队创建 tmux session，每个成员一个窗口。
    所有成员共享真实工作目录、共享上下文区和 MCP 连接：
    - 统一工作目录: workspace_dir（TUI 默认 team_manger.py 所在目录）
    - MCP 配置: claude 成员从共享工作目录启动以继承 .claude/mcp.json
              codex 成员通过全局 codex config 连接 MCP
    - 共享上下文区: share_context_space/{team}/ 供所有成员读写

    与 MCP server 的 launch_team_terminals 行为完全一致。
    返回 (成功, 信息)。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return False, f"团队 '{team_name}' 不存在"

    # 任务进行中保护仍在线的 leader；leader 已离线时允许恢复启动。
    if _leader_terminal_restart_blocked(team_name, team):
        return False, (
            "任务进行中，禁止重启 leader 终端。\n"
            "请等待所有成员和 leader 任务完成后重试。\n"
            "💡 如需单独重启成员终端，可在成员终端列表中选择后再试。"
        )

    leader = team.get("leader", "")
    members = team.get("members", {})
    if not members:
        return False, "请先添加成员"
    if not leader:
        return False, "请先在详情页按 L 指定 Leader"

    rc, _, _ = _tmux_run(["-V"])
    if rc != 0:
        return False, "tmux 未安装，请执行 sudo apt install tmux"

    import datetime
    session = _tmux_session(f"{team_name}_{datetime.datetime.now().strftime('%H%M%S')}")

    _record_leader_reentry(team)
    team["terminals_active"] = False
    save_data(data)

    import time

    team_workspace = _team_workspace(team_name)
    team_workspace.mkdir(parents=True, exist_ok=True)
    share_dir = _team_context_dir(team_name)
    share_dir.mkdir(parents=True, exist_ok=True)
    team["workspace_dir"] = str(team_workspace)
    team["context_dir"] = str(share_dir)

    claude_msg = ""
    has_claude = any(("claude" in (members.get(n, {}).get("agent") or team.get("default_agent", "claude")).lower())
                     for n in members)
    if has_claude:
        _, claude_msg = configure_claude_mcp(team_name)
        write_claude_permissions(team_workspace)
    codex_msg = ""
    if any(("codex" in (members.get(n, {}).get("agent") or team.get("default_agent", "claude")).lower())
           for n in members):
        _, codex_msg = configure_codex_mcp()

    mcp_msgs = ["共享上下文模式: 所有成员共享工作目录 + 共享上下文区 + MCP 连接"]
    if claude_msg:
        mcp_msgs.append(f"  Claude: {claude_msg}")
    if codex_msg:
        mcp_msgs.append(f"  Codex: {codex_msg}")
    mcp_msgs.append(f"  📁 工作目录: {team_workspace}")
    mcp_msgs.append(f"  📂 共享上下文区: {share_dir}")

    proxy_prefix = get_proxy_env_prefix(team_name, leader)
    leader_data = members.get(leader, {})
    leader_agent_name = leader_data.get("agent") or team.get("default_agent") or "claude"
    leader_agent_path = shutil.which(leader_agent_name) or leader_agent_name

    leader_agent_type = agent_type(leader_agent_name)
    leader_agent_user_prefix = get_agent_user_env_prefix(team_name, leader, leader_agent_type)
    leader_model = resolve_agent_model(team_name, leader)
    leader_effort = resolve_member_effort(team_name, leader, leader_agent_type)

    if _is_codex(leader_agent_name):
        # Codex 无 system-prompt 通道：身份固化到唯一自动装载持久指令文件 AGENTS.md
        prompt_registry.ensure_codex_agents_md(team_name, str(team_workspace))
        rc, _, err = _tmux_run([
            "new-session", "-d", "-s", session,
            "-n", leader,
            *leader_agent_user_prefix,
            *proxy_prefix,
            *_codex_command(
                leader_agent_path,
                team_workspace,
                _leader_system_prompt(team_name),
                member_mode=_member_mode(leader_data),
                model=leader_model,
                effort=leader_effort,
            ),
        ])
    else:
        try:
            leader_au_prefix, leader_settings_path = claude_agent_user_launch(team_name, leader)
        except RuntimeError as e:
            return False, f"创建 leader 终端失败: {e}"
        # leader 身份进 system 层（--append-system-prompt-file）
        leader_identity_path = prompt_registry.claude_identity_file(team_name, leader, leader=True)
        rc, _, err = _tmux_run([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", str(team_workspace),
            *merge_env_prefixes(leader_au_prefix, proxy_prefix),
            *_claude_agent_args(
                leader_agent_path,
                _member_mode(leader_data),
                allowed_tools=classifier_fallback.claude_terminal_allow_tools(
                    _member_mode(leader_data), str(team_workspace),
                    CLAUDE_LEADER_TOOL_ALLOW_PATTERNS,
                ),
                model=leader_model,
                settings_path=leader_settings_path,
                effort=leader_effort,
                append_system_prompt_file=leader_identity_path,
            ),
        ])

    if rc != 0:
        return False, f"创建 leader 终端失败: {err}"
    _remember_member_window_id(team_name, leader, session, leader)
    created = [f"👑{leader}"]

    for name, info in members.items():
        if name == leader:
            continue
        member_agent_name = info.get("agent") or team.get("default_agent") or "claude"
        member_agent_path = shutil.which(member_agent_name) or member_agent_name

        member_proxy_prefix = get_proxy_env_prefix(team_name, name)
        member_agent_type = agent_type(member_agent_name)
        member_agent_user_prefix = get_agent_user_env_prefix(team_name, name, member_agent_type)
        member_model = resolve_agent_model(team_name, name)
        member_effort = resolve_member_effort(team_name, name, member_agent_type)

        # 跨进程 spawn 锁：与 MCP _tmux_spawn_member 共享同一锁，"检查窗口存在 +
        # 创建窗口"在同一临界区，防止并发重复创建同一成员终端。
        try:
            with _member_spawn_lock(team_name, name):
                state, _detail = _member_window_state(team_name, name, session)
                if state == "live":
                    # 窗口已存在（可能由 MCP 并发创建）→ 复用，不重复创建
                    member_rc = 0
                elif state == "unknown":
                    # 无法确认存在性 → 不盲目创建，转可见错误
                    self.notify(
                        f"⚠️ 成员 '{name}' 终端状态未知（{_detail}），跳过创建",
                        timeout=4,
                    )
                    member_rc = 1
                elif _is_codex(member_agent_name):
                    prompt_registry.ensure_codex_agents_md(team_name, str(team_workspace))
                    member_rc, _, _ = _tmux_run([
                        "new-window", "-t", session, "-n", name,
                        *member_agent_user_prefix,
                        *member_proxy_prefix,
                        *_codex_command(
                            member_agent_path,
                            team_workspace,
                            member_mode=_member_mode(info),
                            model=member_model,
                            effort=member_effort,
                        ),
                    ])
                else:
                    member_au_prefix, member_settings_path = claude_agent_user_launch(team_name, name)
                    member_identity_path = prompt_registry.claude_identity_file(team_name, name)
                    member_rc, _, _ = _tmux_run([
                        "new-window", "-t", session, "-n", name,
                        "-c", str(team_workspace),
                        *merge_env_prefixes(member_au_prefix, member_proxy_prefix),
                        *_claude_agent_args(
                            member_agent_path,
                            _member_mode(info),
                            allowed_tools=classifier_fallback.claude_terminal_allow_tools(
                                _member_mode(info), str(team_workspace),
                                CLAUDE_MEMBER_TOOL_ALLOW_PATTERNS,
                            ),
                            model=member_model,
                            settings_path=member_settings_path,
                            effort=member_effort,
                            append_system_prompt_file=member_identity_path,
                        ),
                    ])
        except (RuntimeError, OSError) as lock_err:
            # fail closed：锁不可用 → 可见错误，不无锁继续创建
            self.notify(f"⚠️ 成员 '{name}' 无法获取 spawn 锁: {lock_err}", timeout=4)
            member_rc = 1

        if member_rc == 0:
            _remember_member_window_id(team_name, name, session, name)
            created.append(name)
        time.sleep(0.08)

    team["terminals_active"] = True
    save_data(data)

    if not _is_codex(leader_agent_name):
        rc, err = _inject_claude_leader_prompt(session, leader, team_name)
        if rc != 0:
            return False, err

    total = len(created)
    return True, (
        f"🚀 终端已启动！（共享上下文模式）\n"
        f"   session: {session}\n"
        f"   窗口({total}): {' | '.join(created)}\n"
        f"   {' | '.join(mcp_msgs)}\n\n"
        f"进入 leader 终端:\n"
        f"   tmux attach -t {session}\n\n"
        f"💡 所有成员共享真实工作目录 + MCP 连接，可通过共享上下文区交换上下文\n"
        f"💡 tmux 快捷键: Ctrl+B 数字键(切换窗口)  W(列表)  D(脱离)"
    )

def kill_terminals(team_name: str) -> tuple[bool, str]:
    """销毁团队 tmux session（可能带唯一后缀）"""
    data = load_data()
    team = data.get("teams", {}).get(team_name)
    if team and _leader_terminal_restart_blocked(team_name, team):
        return False, "任务进行中，禁止关闭 leader 终端。普通成员终端仍可单独重启。"

    session = _find_tmux_session(team_name)
    if not session:
        return False, "未找到运行中的终端"

    rc, _, err = _tmux_run(["kill-session", "-t", session])
    if rc != 0:
        return False, f"关闭失败: {err}"

    if team_name in data.get("teams", {}):
        data["teams"][team_name]["terminals_active"] = False
        save_data(data)
    return True, "终端已关闭"


def delete_team_record_and_artifacts(team_name: str) -> tuple[bool, str]:
    """删除团队记录及本工具托管的团队产物。"""
    data = load_data()
    team = data.get("teams", {}).get(team_name)
    if not team:
        return False, f"团队 '{team_name}' 不存在"

    close_msgs: list[str] = []
    if team.get("terminals_active") or _find_tmux_session(team_name):
        ok, msg = kill_terminals(team_name)
        if ok:
            close_msgs.append(msg)
        elif _find_tmux_session(team_name):
            return False, f"删除中止：终端仍在运行且关闭失败: {msg}"
        else:
            close_msgs.append("终端状态已过期，未发现运行中的终端")
        data = load_data()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return True, "\n".join(close_msgs)

    cleanup_msgs = cleanup_team_artifacts(team_name, team)
    del data["teams"][team_name]
    mark_legacy_team_deleted(data, team_name)
    save_data(data)
    _remove_team_from_legacy_data_file(team_name)
    return True, "\n".join(close_msgs + cleanup_msgs)


def open_leader_terminal(team_name: str) -> tuple[bool, str]:
    """
    打开团队 leader 终端。
    进入终端前自动检测并启动 MCP server（若未运行），
    确保 leader 进入后 MCP 工具立即可用。
    TUI 自身在 tmux 内运行时，优先在当前 tmux 中分屏 attach；
    否则使用系统图形终端，fallback 到提示命令。
    """
    session = _find_tmux_session(team_name)
    if not session:
        return False, "终端未启动，请先 launch"

    ok, mcp_msg = _ensure_mcp_server_running()
    if not ok:
        return False, f"MCP Server 启动失败: {mcp_msg}\n请检查 MCP 服务状态后再试"

    tmux = _find_tmux() or "tmux"
    data = load_data()
    leader = data.get("teams", {}).get(team_name, {}).get("leader", "")
    if leader:
        _tmux_run(["select-window", "-t", f"{session}:{leader}"])

    if _current_tmux_session():
        command = _reattaching_tmux_attach_command(tmux, session)
        ok, msg = tmux_spawn(command, title=f"{team_name}:leader")
        if ok:
            return True, f"{msg}，已进入 {session}"
        return False, msg

    if shutil.which("gnome-terminal"):
        subprocess.Popen(
            ["gnome-terminal", "--", tmux, "attach", "-t", session],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, f"已在新窗口打开 {session}"

    if shutil.which("xterm"):
        subprocess.Popen(
            ["xterm", "-e", tmux, "attach", "-t", session],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, f"已在新 xterm 窗口打开 {session}"

    cmd = f"{tmux} attach -t {session}"
    return True, f"请在另一个终端执行:\n  {cmd}"


def _ensure_mcp_server_running() -> tuple[bool, str]:
    """确保 MCP Server 正在运行；未运行时自动启动。

    供 TUI 在打开 Leader 终端前调用，确保成员能通过 MCP 通信。
    返回 (ok, msg)：ok=True 表示 MCP 已就绪，ok=False 表示启动失败。
    """
    running, status = mcp_server_status()
    if running:
        return True, status
    return start_mcp_server()


from tui.tui_dialogs import (
    MessageBox, ConfirmBox, FormField, McpStatusDialog, AgentMcpConfigDialog,
    CreateTeamDialog, AddMemberDialog, EditMemberDialog, TeamProxyDialog,
    ContextErrorDialog, ContextConfirmDeleteDialog, ContextConfirmDeleteAllDialog,
    ContextFileViewer, ContextFileEditor, NewContextFileDialog,
    AgentUserManageDialog, TeamDefaultAgentUserDialog, AgentUserPoolDialog,
)

def apply_proxy_action(team: dict, action: str, member_name: str, host: str, port: int) -> str:
    """Apply the TUI proxy action to either the selected member or the team default."""
    if action not in {"enabled", "disabled", "all_enabled", "all_disabled"}:
        raise ValueError(f"未知代理操作: {action}")

    proxy = team.setdefault("proxy", {})
    proxy["host"] = host
    proxy["port"] = port

    if action in {"all_enabled", "all_disabled"}:
        proxy["enabled"] = action == "all_enabled"
        state = "启用" if proxy["enabled"] else "禁用"
        return f"✅ 已全部{state}代理"

    if not member_name:
        raise ValueError("请先选择成员")
    members = team.setdefault("members", {})
    member = members.get(member_name)
    if member is None:
        raise ValueError(f"成员 '{member_name}' 不存在")

    enabled = action == "enabled"
    member["proxy_mode"] = "enabled" if enabled else "disabled"
    member["proxy_enabled"] = enabled
    state = "启用" if enabled else "禁用"
    return f"✅ 已为 '{member_name}' {state}代理"


# ============================================================
# 上下文文件管理 — 安全验证与文件操作
# ============================================================

def _context_root_dir(team_name: str) -> Path:
    """返回团队共享上下文的根目录。"""
    return team_context_dir(team_name)


def _list_context_files(root: Path) -> list[dict]:
    """递归列出上下文目录下的所有普通实体文件。

    返回按相对路径排序的文件列表,每个元素包含:
      rel_path: 相对 root 的路径
      size: 文件大小(字节)
      mtime: 修改时间(ISO 格式字符串)
      error: 错误信息或 None(二进制文件标记为 "非文本文件")
      readable: 是否可查看/编辑(非 UTF-8 为 False)

    跳过目录和所有符号链接(包括根内与越界),不跳过二进制/非 UTF-8 文件。
    """
    results: list[dict] = []
    if not root.exists() or not root.is_dir():
        return results

    for entry in sorted(root.rglob("*"), key=lambda p: str(p.relative_to(root))):
        try:
            rel = str(entry.relative_to(root))
        except (ValueError, OSError):
            continue

        if entry.is_dir():
            continue
        if entry.is_symlink():
            continue  # 跳过所有符号链接(包括根内),防止 delete 误删 resolve 后的真实目标
        if not entry.is_file():
            continue

        error = None
        try:
            stat = entry.stat()
            size = stat.st_size
            mtime = stat.st_mtime
        except OSError as e:
            error = str(e)
            size = 0
            mtime = 0.0

        # 检测是否为文本文件——二进制文件不跳过,只标记
        readable = True
        if error is None:
            try:
                with open(entry, "rb") as f:
                    chunk = f.read(8192)
                chunk.decode("utf-8")
            except UnicodeDecodeError:
                error = "非UTF-8内容"
                readable = False
            except OSError:
                pass

        from datetime import datetime, timezone
        mtime_str = datetime.fromtimestamp(mtime, tz=timezone.utc).strftime("%Y-%m-%d %H:%M") if mtime > 0 else "?"

        results.append({
            "rel_path": rel,
            "size": size,
            "mtime": mtime_str,
            "error": error,
            "readable": readable,
        })

    return results


_CONTEXT_LOCK_OWNER = "tui"
_CONTEXT_LOCK_TTL_SECONDS = 1800


def _context_locks_path(root: Path) -> Path:
    return root / "file_locks.json"


def _write_context_file_locks(root: Path, locks: dict) -> None:
    """原子写入与 MCP 文件锁工具共享的 file_locks.json。"""
    root.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".file_locks.", suffix=".tmp", dir=root)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(locks, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, _context_locks_path(root))
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def _load_context_file_locks(root: Path) -> dict:
    """读取活跃锁；锁文件损坏时抛错，避免误删本应受保护的文件。"""
    path = _context_locks_path(root)
    if not path.exists():
        return {}
    with open(path, encoding="utf-8") as f:
        locks = json.load(f)
    if not isinstance(locks, dict):
        raise ValueError("file_locks.json 格式无效")

    now = time.time()
    active = {
        key: value for key, value in locks.items()
        if isinstance(value, dict) and float(value.get("expires_at", 0)) > now
    }
    if active != locks:
        _write_context_file_locks(root, active)
    return active


def _context_file_lock_key(team_name: str, full_path: Path) -> str:
    """生成与 MCP `_lock_key` 一致的、相对团队 workspace 的锁键。"""
    workspace = os.path.abspath(team_workspace_dir(team_name))
    candidate = os.path.abspath(full_path)
    try:
        return os.path.relpath(candidate, workspace)
    except ValueError:
        return candidate


def _acquire_context_file_lock(team_name: str, root: Path, full_path: Path) -> tuple[bool, str]:
    locks = _load_context_file_locks(root)
    key = _context_file_lock_key(team_name, full_path)
    existing = locks.get(key)
    if existing and existing.get("member") != _CONTEXT_LOCK_OWNER:
        return False, f"文件已被 {existing.get('member') or '其他成员'} 锁定"

    import datetime
    now = time.time()
    locks[key] = {
        "member": _CONTEXT_LOCK_OWNER,
        "purpose": "TUI 上下文文件操作",
        "created_at": datetime.datetime.now().isoformat(),
        "expires_at": now + _CONTEXT_LOCK_TTL_SECONDS,
    }
    _write_context_file_locks(root, locks)
    return True, ""


def _release_context_file_lock(team_name: str, root: Path, full_path: Path) -> tuple[bool, str]:
    locks = _load_context_file_locks(root)
    key = _context_file_lock_key(team_name, full_path)
    existing = locks.get(key)
    if not existing:
        return False, "文件未锁定"
    if existing.get("member") != _CONTEXT_LOCK_OWNER:
        return False, f"文件锁属于 {existing.get('member') or '其他成员'}，TUI 无法释放"
    del locks[key]
    _write_context_file_locks(root, locks)
    return True, ""


def _delete_unlocked_context_files(team_name: str, root: Path) -> tuple[int, int, list[str]]:
    """删除上下文中的未锁定普通文件，并返回 deleted/skipped/errors。"""
    locks = _load_context_file_locks(root)
    deleted = 0
    skipped = 0
    errors: list[str] = []

    for entry in _list_context_files(root):
        rel = entry["rel_path"]
        key = _context_file_lock_key(team_name, root / rel)
        if key in locks:
            skipped += 1
            continue
        if rel == "file_locks.json" and locks:
            continue

        lexical = root / rel
        try:
            if lexical.is_symlink() or not lexical.is_file():
                continue
            lexical.unlink()
            deleted += 1
        except OSError as e:
            errors.append(f"{rel}: {e}")

    directories = sorted(
        (p for p in root.rglob("*") if p.is_dir() and not p.is_symlink()),
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for directory in directories:
        try:
            directory.rmdir()
        except OSError:
            pass

    return deleted, skipped, errors


class WrappingFooter(Footer):
    """Footer that wraps binding hints into rows when terminal width is limited."""

    DEFAULT_CSS = """
    WrappingFooter {
        layout: grid;
        grid-columns: auto;
        grid-rows: 1;
        height: auto;
        scrollbar-size: 0 0;
    }
    WrappingFooter FooterKey.-command-palette {
        dock: none;
        padding-right: 0;
        border-left: none;
    }
    """

    def _update_grid_columns(self) -> None:
        children = list(self.children)
        child_count = len(children)
        if child_count == 0:
            return
        available_width = max(1, self.content_size.width)
        item_widths = [
            max(1, child.get_content_width(self.content_size, self.app.size))
            for child in children
        ]
        fitting_columns = 1
        for columns in range(1, child_count + 1):
            column_widths = [0] * columns
            for index, item_width in enumerate(item_widths):
                column = index % columns
                column_widths[column] = max(column_widths[column], item_width)
            if sum(column_widths) <= available_width:
                fitting_columns = columns
        self.styles.grid_size_columns = fitting_columns

    def on_mount(self) -> None:
        super().on_mount()
        self.call_after_refresh(self._update_grid_columns)

    def on_resize(self, _event: Resize) -> None:
        self.call_after_refresh(self._update_grid_columns)

    def bindings_changed(self, screen: Screen) -> None:
        super().bindings_changed(screen)
        if self.is_attached:
            self.call_after_refresh(self._update_grid_columns)


class TeamDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("a", "add_member", "添加成员"),
        Binding("r", "remove_member", "移除成员"),
        Binding("e", "edit_member", "编辑成员"),
        Binding("l", "set_leader", "指定Leader"),
        Binding("t", "launch_terminals", "启动终端"),
        Binding("k", "kill_terminals", "关闭终端"),
        Binding("p", "edit_proxy", "代理配置"),
        Binding("u", "team_default_agent_user", "默认Agent用户"),
        Binding("m", "context_manage", "上下文"),
        Binding("0", "open_leader", "打开Leader窗口"),
        Binding("1", "mcp_manage", "MCP服务"),
        Binding("2", "mcp_config", "MCP配置"),
        Binding("4", "agent_user_pool", "Agent用户池"),
        Binding("q", "quit", "退出"),
        Binding("escape,ctrl+q", "go_back", "返回"),
    ]

    def __init__(self, team_name: str) -> None:
        super().__init__()
        self._team_name = team_name

    @property
    def team_name(self) -> str:
        return self._team_name

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("", id="team_info"),
            DataTable(id="member_table", cursor_type="row"),
            Static("", id="status_bar"),
            classes="detail-container",
        )
        yield WrappingFooter()

    def on_mount(self) -> None:
        dt = self.query_one("#member_table", DataTable)
        dt.add_columns("名称", "角色", "Agent", "Leader", "代理", "Agent用户", "状态")
        dt.show_header = True
        dt.can_focus = False
        self.query_one(Header).can_focus = False
        self.query_one(Footer).can_focus = False
        self.focus()
        self._refresh()
        self.set_interval(5, self._auto_refresh)

    def _auto_refresh(self) -> None:
        """定时刷新终端存活状态，自动恢复死亡的成员"""
        self._refresh()
        self._auto_recover_members()

    def _auto_recover_members(self) -> None:
        """检测死亡的成员终端，仅对异常退出的成员自动恢复（休眠成员不打扰）"""
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        if not team.get("terminals_active"):
            return

        members = team.get("members", {})
        session = _find_tmux_session(self._team_name)
        if not session:
            return

        import time as _time

        rc, out, _ = _tmux_run(["list-windows", "-t", session, "-F", "#{window_name}"])
        if rc != 0:
            return
        alive_windows = set(out.split("\n")) if out else set()

        for name, info in members.items():
            if name == team.get("leader", ""):
                continue

            if name in alive_windows:
                continue

            task_completed = info.get("last_task_completed", True)
            has_task = bool(info.get("last_task", ""))

            if has_task and task_completed:
                continue

            recovery_count = info.get("recovery_count", 0)
            MAX_RECOVERY = 3

            if recovery_count >= MAX_RECOVERY:
                self.notify(
                    f"⚠️ 成员 '{name}' 已恢复 {recovery_count} 次，超过上限，不再自动恢复。请手动检查。",
                    timeout=5,
                )
                continue

            member_agent_name = info.get("agent") or team.get("default_agent") or "claude"
            member_agent_path = shutil.which(member_agent_name) or member_agent_name
            team_workspace = _team_workspace(self._team_name)
            team_workspace.mkdir(parents=True, exist_ok=True)

            configure_claude_mcp(self._team_name)
            configure_codex_mcp()

            proxy_prefix = get_proxy_env_prefix(self._team_name, name)

            member_agent_type = agent_type(member_agent_name)
            member_agent_user_prefix = get_agent_user_env_prefix(self._team_name, name, member_agent_type)
            member_model = resolve_agent_model(self._team_name, name)
            member_effort = resolve_member_effort(self._team_name, name, member_agent_type)

            # 跨进程 spawn 锁：与 MCP 共享，自动恢复同样"检查 + 创建"同一临界区。
            try:
                with _member_spawn_lock(self._team_name, name):
                    state, _detail = _member_window_state(self._team_name, name, session)
                    if state == "live":
                        # 窗口已被（MCP 等）创建 → 不再创建，但下面仍重发任务/恢复消息
                        rc2 = 0
                    elif state == "unknown":
                        self.notify(
                            f"⚠️ 成员 '{name}' 终端状态未知（{_detail}），跳过自动恢复",
                            timeout=4,
                        )
                        continue
                    elif _is_codex(member_agent_name):
                        prompt_registry.ensure_codex_agents_md(self._team_name, str(team_workspace))
                        rc2, _, _ = _tmux_run([
                            "new-window", "-t", session, "-n", name,
                            *member_agent_user_prefix,
                            *proxy_prefix,
                            *_codex_command(
                                member_agent_path,
                                team_workspace,
                                member_mode=_member_mode(info),
                                model=member_model,
                                effort=member_effort,
                            ),
                        ])
                    else:
                        recover_au_prefix, recover_settings_path = claude_agent_user_launch(
                            self._team_name, name)
                        recover_identity_path = prompt_registry.claude_identity_file(self._team_name, name)
                        rc2, _, _ = _tmux_run([
                            "new-window", "-t", session, "-n", name,
                            "-c", str(team_workspace),
                            *merge_env_prefixes(recover_au_prefix, proxy_prefix),
                            *_claude_agent_args(
                                member_agent_path,
                                _member_mode(info),
                                allowed_tools=classifier_fallback.claude_terminal_allow_tools(
                                    _member_mode(info), str(team_workspace),
                                    CLAUDE_MEMBER_TOOL_ALLOW_PATTERNS,
                                ),
                                model=member_model,
                                settings_path=recover_settings_path,
                                effort=member_effort,
                                append_system_prompt_file=recover_identity_path,
                            ),
                        ])
            except (RuntimeError, OSError) as lock_err:
                self.notify(
                    f"⚠️ 成员 '{name}' 无法获取 spawn 锁: {lock_err}，跳过自动恢复",
                    timeout=4,
                )
                continue

            if rc2 != 0:
                continue

            import datetime as _dt
            info["recovery_count"] = recovery_count + 1
            info["last_recovery_ts"] = _dt.datetime.now().isoformat()
            info["last_terminal_death_ts"] = _dt.datetime.now().isoformat()
            save_data(data)

            _time.sleep(0.5)

            recovery_ctx = _build_tui_recovery_message(team, name, info, self._team_name)
            _tmux_run(["send-keys", "-t", f"{session}:{name}", "-l", recovery_ctx])
            _tmux_run(["send-keys", "-t", f"{session}:{name}", "Enter"])

            if has_task and not task_completed:
                _time.sleep(0.3)
                last_context = info.get("last_context", "")
                full_msg = info["last_task"]
                if last_context:
                    full_msg = f"[任务上下文] {last_context}\n[子任务] {full_msg}"
                _tmux_run(["send-keys", "-t", f"{session}:{name}", "-l", full_msg])
                _tmux_run(["send-keys", "-t", f"{session}:{name}", "Enter"])
                self.notify(
                    f"🔄 成员 '{name}' 已恢复并重发任务 (第{info['recovery_count']}次)",
                    timeout=3,
                )
            else:
                self.notify(
                    f"🔄 成员 '{name}' 已自动恢复 (第{info['recovery_count']}次)",
                    timeout=3,
                )

    def _refresh(self) -> None:
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})

        leader = team.get("leader", "")
        default_agent = team.get("default_agent", "claude")
        terminal_alive = _sync_team_terminal_state(self._team_name)
        team["terminals_active"] = terminal_alive
        terminals = "🟢 运行中" if terminal_alive else "⚫ 未启动"
        desc = team.get("description", "")
        claude_ok = "✅" if _claude_mcp_configured(self._team_name) else "⚠️"
        codex_ok = "✅" if _codex_mcp_configured() else "⚠️"

        # 代理状态
        proxy_config = team.get("proxy", {})
        if proxy_config.get("enabled"):
            proxy_info = f"🔀代理:{proxy_config.get('host','127.0.0.1')}:{proxy_config.get('port',7890)}"
        else:
            proxy_info = "🔀代理:关"

        member_status = get_member_terminal_status(self._team_name)
        alive_count = sum(1 for v in member_status.values() if v)
        total_count = len(member_status)
        window_info = f"({alive_count}/{total_count}窗口)" if total_count > 0 else ""

        # Agent 用户切换池状态（勾选顺序即切换顺序；无池时不显示）
        pool = team.get("agent_user_pool", [])
        pool_info = ""
        if isinstance(pool, list) and pool:
            pool_info = "  池:" + "→".join(str(k) for k in pool)

        info = self.query_one("#team_info", Static)
        info.update(
            f"📋 [bold]{self._team_name}[/bold]  终端:{terminals}{window_info}"
            f"  Claude MCP:{claude_ok}  Codex MCP:{codex_ok}  {proxy_info}"
            f"{pool_info}"
            f"{'   ' + desc if desc else ''}"
        )

        dt = self.query_one("#member_table", DataTable)
        dt.clear()
        members = team.get("members", {})

        if not members:
            self.query_one("#status_bar", Static).update(
                "A 添加成员 | R 移除 | E 编辑 | L 指定Leader | P 代理 | U Agent用户 | 4 用户池 | 1 服务 | 2 配置 | Esc/Ctrl+Q 返回"
            )
            return

        activity_counts: dict[str, int] = {"working": 0, "idle": 0, "sleep": 0, "dead": 0}
        # 全局-aware 读：全局 data['agent_users'] + 该团队未迁移旧数据合并，
        # 保证迁移后成员表的 provider 标签不丢失。
        profiles = _list_agent_users(self._team_name)
        for name, info in members.items():
            role = info.get("role", "")
            agent = info.get("agent", default_agent)
            is_ldr = "👑" if name == leader else ""
            proxy_mode = member_proxy_mode(info)
            effective_proxy = member_proxy_enabled(team, name, info)
            if proxy_mode == "inherit":
                proxy_label = "继承开" if effective_proxy else "继承关"
            else:
                proxy_label = "强制开" if effective_proxy else "强制关"
            status_info = dict(info)
            if name == leader:
                status_info["role"] = "leader"
            status_label, status_bucket = format_member_activity_status(
                status_info,
                member_status.get(name, False),
            )
            activity_counts[status_bucket] = activity_counts.get(status_bucket, 0) + 1
            agent_user_key = info.get("agent_user", "")
            # 显示 profile 名称 + provider 标记；未指定时回退到团队默认
            if agent_user_key == AGENT_USER_NONE:
                agent_user_label = "不接管"
            elif not agent_user_key:
                default_key = team.get("default_agent_user", "")
                if default_key and default_key in profiles:
                    cfg = profiles[default_key]
                    at = (cfg.get("agent_type") or "").lower()
                    if at == "claude":
                        agent_user_label = f"🤖{default_key}(默认)"
                    elif at == "codex":
                        agent_user_label = f"🔵{default_key}(默认)"
                    else:
                        agent_user_label = f"{default_key}(默认)"
                else:
                    agent_user_label = "默认"
            else:
                cfg = profiles.get(agent_user_key, {})
                at = (cfg.get("agent_type") or "").lower()
                if at == "claude":
                    agent_user_label = f"🤖{agent_user_key}"
                elif at == "codex":
                    agent_user_label = f"🔵{agent_user_key}"
                else:
                    agent_user_label = agent_user_key
            dt.add_row(name, role, agent, is_ldr, proxy_label, agent_user_label, status_label, key=name)

        ltype = team.get("leader_type", "")
        status_parts = [f"{len(members)} 个成员"]
        if total_count > 0:
            status_parts.append(
                " ".join(
                    [
                        f"working:{activity_counts['working']}",
                        f"idle:{activity_counts['idle']}",
                        f"sleep:{activity_counts['sleep']}",
                        f"dead:{activity_counts['dead']}",
                    ]
                )
            )
        if leader:
            if ltype == "direct":
                status_parts.append(f"Leader: {leader} (直接控制)")
            else:
                status_parts.append(f"Leader: {leader} (tmux)")
        self.query_one("#status_bar", Static).update(" | ".join(status_parts))

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()

    def _selected_member_name(self) -> str:
        dt = self.query_one("#member_table", DataTable)
        if dt.row_count == 0:
            return ""
        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return ""
        return str(row_key.value) if row_key.value else ""

    @work
    async def action_mcp_manage(self) -> None:
        await self.app.push_screen_wait(McpStatusDialog())
        self._refresh()

    @work
    async def action_mcp_config(self) -> None:
        await self.app.push_screen_wait(AgentMcpConfigDialog())
        self._refresh()

    @work
    async def action_launch_terminals(self) -> None:
        ok, msg = launch_terminals(self._team_name)
        if ok and "进入" in msg:
            await self.app.push_screen_wait(MessageBox(msg))
        else:
            await self.app.push_screen_wait(MessageBox(msg))
        self._refresh()

    @work
    async def action_kill_terminals(self) -> None:
        if not tmux_session_alive(self._team_name):
            await self.app.push_screen_wait(MessageBox("终端未运行"))
            return
        confirmed = await self.app.push_screen_wait(ConfirmBox("确认关闭所有终端窗口？"))
        if not confirmed:
            return
        _, msg = kill_terminals(self._team_name)
        await self.app.push_screen_wait(MessageBox(msg))
        self._refresh()

    @work
    async def action_open_leader(self) -> None:
        if not tmux_session_alive(self._team_name):
            await self.app.push_screen_wait(MessageBox("终端未启动，请先按 T 启动"))
            return

        _, msg = open_leader_terminal(self._team_name)
        await self.app.push_screen_wait(MessageBox(msg))
        self._refresh()

    @work
    async def action_add_member(self) -> None:
        data = load_data()
        team = data.setdefault("teams", {}).setdefault(self._team_name, {})
        default_agent = team.get("default_agent", "claude")
        result = await self.app.push_screen_wait(AddMemberDialog(default_agent=default_agent, team_name=self._team_name))
        if result is None:
            return

        data = load_data()
        team = data.setdefault("teams", {}).setdefault(self._team_name, {})
        members = team.setdefault("members", {})

        if result["name"] in members:
            await self.app.push_screen_wait(MessageBox(f"成员 '{result['name']}' 已存在"))
            return

        member_data = {
            "role": result["role"], "model": "", "agent": result["agent"],
            # `or` 而非 .get 默认值：键一定存在，空值(空选择归一化的结果)才是要兜的
            "proxy_mode": result.get("proxy_mode") or "inherit",
            # 成员级 effort（三态：显式级别 / inherit=继承 Agent 用户默认 / off=关闭）
            "effort": result.get("effort") or "inherit",
        }
        if result.get("agent_user"):
            member_data["agent_user"] = result["agent_user"]
        members[result["name"]] = member_data
        save_data(data)
        self._refresh()

    @work
    async def action_remove_member(self) -> None:
        dt = self.query_one("#member_table", DataTable)
        if dt.row_count == 0:
            await self.app.push_screen_wait(MessageBox("没有可移除的成员"))
            return

        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return
        member_name = str(row_key.value) if row_key.value else ""
        if not member_name:
            return

        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        if team.get("leader") == member_name:
            await self.app.push_screen_wait(
                MessageBox(f"'{member_name}' 是 Leader，请先指定新 Leader 再移除")
            )
            return

        confirmed = await self.app.push_screen_wait(ConfirmBox(f"确认移除 {member_name} ？"))
        if not confirmed:
            return

        del team["members"][member_name]
        save_data(data)
        self._refresh()

    @work
    async def action_edit_member(self) -> None:
        dt = self.query_one("#member_table", DataTable)
        if dt.row_count == 0:
            return

        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return
        member_name = str(row_key.value) if row_key.value else ""
        if not member_name:
            return

        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        member = team.get("members", {}).get(member_name, {})

        result = await self.app.push_screen_wait(EditMemberDialog(
            member_name,
            current_role=member.get("role", ""),
            current_agent=member.get("agent", team.get("default_agent", "claude")),
            current_proxy_mode=member_proxy_mode(member),
            current_agent_user=member.get("agent_user", ""),
            current_effort=member.get("effort", "inherit"),
            team_name=self._team_name,
        ))
        if result is None:
            return

        member["role"] = result["role"]
        member["agent"] = result["agent"]
        # `or` 而非 .get 默认值：键一定存在，空值(空选择归一化的结果)才是要兜的
        member["proxy_mode"] = result.get("proxy_mode") or "inherit"
        member["effort"] = result.get("effort") or "inherit"
        if result.get("agent_user"):
            member["agent_user"] = result["agent_user"]
        else:
            member.pop("agent_user", None)
        save_data(data)
        self._refresh()

    @work
    async def action_edit_proxy(self) -> None:
        """编辑代理配置：当前成员覆盖或团队默认批量切换。"""
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        member_name = self._selected_member_name()
        current_proxy = team.get("proxy", {})
        result = await self.app.push_screen_wait(TeamProxyDialog(
            self._team_name, current_proxy, current_member=member_name,
        ))
        if result is None:
            return

        action = result.get("action", "enabled")
        try:
            msg = apply_proxy_action(
                team,
                action,
                member_name,
                result.get("host", "127.0.0.1"),
                result.get("port", 7890),
            )
        except ValueError as e:
            await self.app.push_screen_wait(MessageBox(str(e)))
            return
        save_data(data)
        await self.app.push_screen_wait(MessageBox(msg))
        self._refresh()

    @work
    async def action_team_default_agent_user(self) -> None:
        """选择团队系统默认 Agent 用户（从全局 profile 列表或「不接管」）。"""
        await self.app.push_screen_wait(TeamDefaultAgentUserDialog(self._team_name))
        self._refresh()

    @work
    async def action_agent_user_pool(self) -> None:
        """配置 Agent 用户切换池（多选 profile，勾选顺序即切换顺序）。"""
        await self.app.push_screen_wait(AgentUserPoolDialog(self._team_name))
        self._refresh()

    @work
    async def action_context_manage(self) -> None:
        """打开上下文文件管理界面"""
        self.app.push_screen(ContextManagementScreen(self._team_name))

    @work
    async def action_set_leader(self) -> None:
        dt = self.query_one("#member_table", DataTable)
        if dt.row_count == 0:
            await self.app.push_screen_wait(MessageBox("没有成员可供指定"))
            return

        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return
        member_name = str(row_key.value) if row_key.value else ""
        if not member_name:
            return

        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        old_leader = team.get("leader", "")

        team["leader"] = member_name
        team["leader_type"] = "tmux"
        team["members"][member_name]["role"] = "leader"
        save_data(data)

        msg = f"✅ '{member_name}' 已被设为 Leader"
        if old_leader and old_leader != member_name:
            msg += f"\n原 Leader '{old_leader}' 已降级"
        await self.app.push_screen_wait(MessageBox(msg))
        self._refresh()


# ============================================================
# 上下文文件管理 Screen
# ============================================================

class ContextManagementScreen(Screen[None]):
    """团队共享上下文文件管理界面。

    快捷键:
      ↑↓        方向键选择文件行
      Enter     查看文件
      E         编辑文件
      A         新建文件
      D         删除文件(需确认)
      L/U       上锁/解锁文件
      X         删除全部未锁定文件
      Q         退出
      Esc       返回上级
    """

    BINDINGS = [
        Binding("space", "view", "查看"),
        Binding("e", "edit", "编辑"),
        Binding("a", "new", "新建"),
        Binding("d", "delete", "删除"),
        Binding("l", "lock", "上锁"),
        Binding("u", "unlock", "解锁"),
        Binding("x", "delete_all_unlocked", "清空未锁定"),
        Binding("q", "quit", "退出"),
        Binding("escape,ctrl+q", "go_back", "返回"),
    ]

    def __init__(self, team_name: str) -> None:
        super().__init__()
        self._team_name = team_name
        self._root = _context_root_dir(team_name)

    @property
    def team_name(self) -> str:
        return self._team_name

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("", id="context_path"),
            DataTable(id="context_file_table", cursor_type="row"),
            Static("", id="context_status_bar"),
            classes="context-container",
        )
        yield WrappingFooter()

    def on_mount(self) -> None:
        dt = self.query_one("#context_file_table", DataTable)
        dt.add_columns("文件 (相对路径)", "大小", "修改时间", "锁定")
        dt.show_header = True
        self.query_one(Header).can_focus = False
        self.query_one(Footer).can_focus = False
        dt.focus()
        self._load_files()

    def _load_files(self) -> None:
        """加载并显示上下文文件列表。"""
        path_label = self.query_one("#context_path", Static)
        status_bar = self.query_one("#context_status_bar", Static)
        dt = self.query_one("#context_file_table", DataTable)
        dt.clear()

        root_str = str(self._root)
        if not self._root.exists() or not self._root.is_dir():
            path_label.update(f"📁 [bold]{self._team_name}[/bold] 上下文: [dim]{root_str}[/dim]  (目录不存在或不可访问)")
            status_bar.update("A 新建 | Esc/Ctrl+Q 返回")
            self._file_entries = {}
            return

        files = _list_context_files(self._root)
        try:
            locks = _load_context_file_locks(self._root)
            self._lock_load_error = ""
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            locks = {}
            self._lock_load_error = str(e)
        path_label.update(f"📁 [bold]{self._team_name}[/bold] 上下文: [dim]{root_str}[/dim]")

        if not files:
            status_bar.update("A 新建 | Esc/Ctrl+Q 返回")
            self._file_entries = {}
            return

        self._file_entries = {f["rel_path"]: f for f in files}

        for f in files:
            rel = f["rel_path"]
            size = f["size"]
            mtime = f["mtime"]
            error = f.get("error")
            readable = f.get("readable", True)
            lock = locks.get(_context_file_lock_key(self._team_name, self._root / rel))
            f["lock"] = lock

            # 可读大小
            if size >= 1024 * 1024:
                size_str = f"{size / (1024 * 1024):.1f} MB"
            elif size >= 1024:
                size_str = f"{size / 1024:.1f} KB"
            else:
                size_str = f"{size} B"

            display_rel = rel
            if not readable:
                display_rel = f"⚠ {rel}"
                size_str = f"{size_str} (非UTF-8)"

            lock_text = f"🔒 {lock.get('member')}" if lock else ""
            dt.add_row(display_rel, size_str, mtime, lock_text, key=rel)

        if self._lock_load_error:
            status_bar.update(
                f"锁记录读取失败: {self._lock_load_error} | 已禁用锁定与删除操作"
            )
        else:
            status_bar.update(
                f"{len(files)} 个文件 | Enter 查看 | E 编辑 | A 新建 | D 删除 | "
                "L 上锁 | U 解锁 | X 清空未锁定 | Esc/Ctrl+Q 返回"
            )

    def _selected_file(self) -> str:
        """获取当前选中的文件相对路径。"""
        dt = self.query_one("#context_file_table", DataTable)
        if dt.row_count == 0:
            return ""
        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return ""
        return str(row_key.value) if row_key.value else ""

    def _selected_entry(self) -> dict | None:
        """获取当前选中文件的完整条目信息,含 readable 标记。"""
        rel = self._selected_file()
        if not rel:
            return None
        return self._file_entries.get(rel)

    def _selected_lock(self) -> dict | None:
        entry = self._selected_entry()
        return entry.get("lock") if entry else None

    def _current_lock(self, rel: str) -> dict | None:
        """重新读取选中文件的活跃锁，避免使用列表加载时的过期快照。"""
        locks = _load_context_file_locks(self._root)
        key = _context_file_lock_key(self._team_name, self._root / rel)
        return locks.get(key)

    def _show_lock_error(self, rel: str, error: str) -> None:
        self.app.push_screen(ContextErrorDialog(rel, error))

    @on(DataTable.RowSelected, "#context_file_table")
    async def _on_row_selected(self, event: DataTable.RowSelected) -> None:
        """Enter 选中行 → 查看文件。event.stop() 阻止冒泡重复触发。"""
        event.stop()
        self._do_view()

    def _do_view(self) -> None:
        """查看当前选中文件（无等待，供 Enter/Space 共用）。"""
        rel = self._selected_file()
        if not rel:
            return
        entry = self._selected_entry()
        if entry and not entry.get("readable", True):
            self.app.push_screen(ContextErrorDialog(rel, "此文件非 UTF-8 文本，无法查看"))
            return
        full = self._get_file_full_path(rel)
        if full is None:
            return
        self.app.push_screen(ContextFileViewer(rel, full))

    @work
    async def action_view(self) -> None:
        self._do_view()

    def _get_file_full_path(self, rel_path: str) -> Path | None:
        """验证并返回文件完整路径,带错误提示。"""
        full, err = _validate_context_path(self._root, rel_path)
        if err:
            self.app.push_screen(ContextErrorDialog(rel_path, err))
            return None
        if not full.exists() or not full.is_file():
            self.app.push_screen(ContextErrorDialog(rel_path, "文件不存在或不可读"))
            return None
        return full

    @work
    async def action_edit(self) -> None:
        rel = self._selected_file()
        if not rel:
            return
        entry = self._selected_entry()
        try:
            lock = self._current_lock(rel)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            self._show_lock_error(rel, f"无法读取锁记录，已取消编辑: {e}")
            return
        if lock and lock.get("member") != _CONTEXT_LOCK_OWNER:
            self._show_lock_error(rel, f"文件已被 {lock.get('member')} 锁定，无法编辑")
            return
        if entry and not entry.get("readable", True):
            self.app.push_screen(ContextErrorDialog(rel, "此文件非 UTF-8 文本，无法编辑"))
            return
        full = self._get_file_full_path(rel)
        if full is None:
            return
        saved = await self.app.push_screen_wait(ContextFileEditor(rel, full))
        if saved:
            self._load_files()
            self.notify(f"✅ 已保存: {rel}")

    @work
    async def action_new(self) -> None:
        created = await self.app.push_screen_wait(NewContextFileDialog(self._root))
        if created is None:
            return
        # 路径已验证，文件在编辑器保存时才创建（取消不留空文件）
        full = self._root / created
        saved = await self.app.push_screen_wait(ContextFileEditor(created, full))
        if saved:
            self._load_files()
            self.notify(f"✅ 已创建: {created}")

    @work
    async def action_delete(self) -> None:
        rel = self._selected_file()
        if not rel:
            return
        try:
            lock = self._current_lock(rel)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            self._show_lock_error(rel, f"无法读取锁记录，已取消删除: {e}")
            return
        if lock:
            self._show_lock_error(rel, f"文件已被 {lock.get('member')} 锁定，无法删除")
            return
        full = self._get_file_full_path(rel)
        if full is None:
            return
        if full.is_dir():
            await self.app.push_screen_wait(
                ContextErrorDialog(rel, "不允许删除目录,请手动在终端操作")
            )
            return

        confirmed = await self.app.push_screen_wait(
            ContextConfirmDeleteDialog(rel)
        )
        if not confirmed:
            return

        try:
            # unlink lexical entry（不用 resolve 后的路径，防止 TOCTOU 下删错目标）
            lexical = self._root / rel
            lexical.unlink()
            self.notify(f"🗑️ 已删除: {rel}")
            self._load_files()
        except OSError as e:
            await self.app.push_screen_wait(
                ContextErrorDialog(rel, f"删除失败: {e}")
            )

    @work
    async def action_lock(self) -> None:
        rel = self._selected_file()
        if not rel:
            return
        full = self._get_file_full_path(rel)
        if full is None:
            return
        try:
            ok, error = _acquire_context_file_lock(self._team_name, self._root, full)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            ok, error = False, str(e)
        if not ok:
            await self.app.push_screen_wait(ContextErrorDialog(rel, error))
            return
        self._load_files()
        self.notify(f"🔒 已上锁 30 分钟: {rel}")

    @work
    async def action_unlock(self) -> None:
        rel = self._selected_file()
        if not rel:
            return
        full = self._get_file_full_path(rel)
        if full is None:
            return
        try:
            ok, error = _release_context_file_lock(self._team_name, self._root, full)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            ok, error = False, str(e)
        if not ok:
            await self.app.push_screen_wait(ContextErrorDialog(rel, error))
            return
        self._load_files()
        self.notify(f"🔓 已解锁: {rel}")

    @work
    async def action_delete_all_unlocked(self) -> None:
        try:
            locks = _load_context_file_locks(self._root)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            await self.app.push_screen_wait(
                ContextErrorDialog("全部删除", f"无法读取锁记录，已取消删除: {e}")
            )
            return

        files = _list_context_files(self._root)
        locked_count = sum(
            1 for entry in files
            if _context_file_lock_key(self._team_name, self._root / entry["rel_path"]) in locks
        )
        delete_count = sum(
            1 for entry in files
            if entry["rel_path"] != "file_locks.json"
            and _context_file_lock_key(self._team_name, self._root / entry["rel_path"]) not in locks
        )
        if delete_count == 0:
            self.notify("没有可删除的未锁定上下文文件")
            return

        confirmed = await self.app.push_screen_wait(
            ContextConfirmDeleteAllDialog(delete_count, locked_count)
        )
        if not confirmed:
            return

        try:
            # helper 内部会在确认后重新读取锁，避免删除确认期间新上锁的文件。
            deleted, skipped, errors = _delete_unlocked_context_files(
                self._team_name, self._root
            )
            self._load_files()
            self.notify(f"已删除 {deleted} 个文件，保留 {skipped} 个上锁文件")
            if errors:
                await self.app.push_screen_wait(
                    ContextErrorDialog("部分文件删除失败", "\n".join(errors[:8]))
                )
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as e:
            await self.app.push_screen_wait(
                ContextErrorDialog("全部删除", f"删除失败: {e}")
            )

    def action_go_back(self) -> None:
        self.app.pop_screen()

    def action_quit(self) -> None:
        self.app.exit()


class MainScreen(Screen[None]):
    BINDINGS = [
        Binding("a", "add_team", "添加团队"),
        Binding("d", "delete_team", "删除团队"),
        Binding("enter,space", "view_team", "查看详情"),
        Binding("l", "claim_leader", "接管Leader"),
        Binding("u", "agent_users", "Agent用户"),
        Binding("1", "mcp_manage", "MCP服务"),
        Binding("2", "mcp_config", "MCP配置"),
        Binding("q", "quit", "退出"),
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Container(
            Static("", id="mcp_status"),
            Static("", id="summary"),
            DataTable(id="team_table", cursor_type="row"),
            Static("", id="hint"),
            classes="main-container",
        )
        yield WrappingFooter()

    def on_mount(self) -> None:
        dt = self.query_one("#team_table", DataTable)
        dt.add_columns("团队名称", "成员数", "默认Agent", "Leader", "终端状态")
        dt.show_header = True
        dt.can_focus = False
        self.query_one(Header).can_focus = False
        self.query_one(Footer).can_focus = False
        self.focus()
        self._refresh()
        self._refresh_mcp_status()
        self.set_interval(15.0, self._refresh_mcp_status)

    def _refresh_mcp_status(self) -> None:
        _, status_text = mcp_server_status()
        codex_ok = _codex_mcp_configured()
        claude_count = sum(1 for v in _all_teams_claude_status().values() if v)
        self.query_one("#mcp_status", Static).update(
            f"Server: {status_text}  |  Codex MCP: {'✅' if codex_ok else '⚠️'}"
            f"  |  Claude MCP: {claude_count} 团队已配置"
        )

    def _refresh(self) -> None:
        dt = self.query_one("#team_table", DataTable)
        dt.clear()

        data = load_data()
        teams = data.get("teams", {})
        claude_status = _all_teams_claude_status()

        if not teams:
            self.query_one("#summary", Static).update("📭 暂无团队")
            self.query_one("#hint", Static).update(
                "A 添加团队 | U Agent用户 | 1 服务 | 2 配置 | Q 退出")
            return

        count = 0
        for name, info in teams.items():
            terminal_alive = _sync_team_terminal_state(name)
            info["terminals_active"] = terminal_alive
            mc = len(info.get("members", {}))
            default_agent = info.get("default_agent", "claude")
            leader = info.get("leader", "")
            ltype = info.get("leader_type", "")

            if ltype == "direct" and leader:
                leader_str = f"{leader}(直接)"
            elif leader:
                leader_str = f"{leader}(tmux)"
            else:
                leader_str = "—"

            mcp_ok = "✓" if claude_status.get(name) else " "
            terminal = "🟢" if terminal_alive else "⚫"
            status = f"{terminal} MCP:{mcp_ok}"

            dt.add_row(name, str(mc), default_agent, leader_str, status, key=name)
            count += 1

        self.query_one("#summary", Static).update(f"📋 共 {count} 个团队")
        self.query_one("#hint", Static).update(
            "A 添加团队 | Enter/Space 查看详情 | D 删除 | L 接管Leader | U Agent用户 | 1 服务 | 2 配置 | Q 退出"
        )

    def action_quit(self) -> None:
        self.app.exit()

    @work
    async def action_mcp_manage(self) -> None:
        await self.app.push_screen_wait(McpStatusDialog())
        self._refresh_mcp_status()

    @work
    async def action_mcp_config(self) -> None:
        await self.app.push_screen_wait(AgentMcpConfigDialog())
        self._refresh()
        self._refresh_mcp_status()

    @work
    async def action_agent_users(self) -> None:
        """顶层管理全局 Agent 用户 profiles（跨团队复用）。"""
        await self.app.push_screen_wait(AgentUserManageDialog())
        self._refresh()

    @work
    async def action_add_team(self) -> None:
        result = await self.app.push_screen_wait(CreateTeamDialog())
        if result is None:
            return

        data = load_data()
        if result["name"] in data.get("teams", {}):
            await self.app.push_screen_wait(MessageBox(f"团队 '{result['name']}' 已存在"))
            return

        data["teams"][result["name"]] = {
            "description": result["description"],
            "leader": "",
            "leader_type": "",
            "default_agent": result["default_agent"],
            "proxy": result.get("proxy", {}),
            "workspace_dir": str(_default_workspace_dir()),
            "context_dir": str((SHARE_CONTEXT_DIR / result["name"]).resolve()),
            "terminals_active": False,
            "members": {},
        }
        save_data(data)
        self._refresh()

    @work
    async def action_delete_team(self) -> None:
        dt = self.query_one("#team_table", DataTable)
        if dt.row_count == 0:
            return

        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return
        team_name = str(row_key.value) if row_key.value else ""
        if not team_name:
            return

        data = load_data()
        team = data.get("teams", {}).get(team_name, {})
        warn = ""
        if team.get("terminals_active"):
            warn = "\n⚠️  终端正在运行"
        if len(team.get("members", {})):
            warn += f"\n⚠️  包含 {len(team['members'])} 个成员"

        confirmed = await self.app.push_screen_wait(ConfirmBox(f"删除 '{team_name}'？{warn}"))
        if not confirmed:
            return

        ok, cleanup_msg = delete_team_record_and_artifacts(team_name)
        if not ok:
            await self.app.push_screen_wait(MessageBox(cleanup_msg))
            self._refresh()
            return

        self._refresh()

        if cleanup_msg:
            self.notify(cleanup_msg, timeout=4)

    def action_view_team(self) -> None:
        dt = self.query_one("#team_table", DataTable)
        if dt.row_count == 0:
            return

        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return
        team_name = str(row_key.value) if row_key.value else ""
        if not team_name:
            return

        self.app.push_screen(TeamDetailScreen(team_name), callback=self._on_detail_closed)

    def _on_detail_closed(self, _result: None) -> None:
        self._refresh()
        self._refresh_mcp_status()

    @work
    async def action_claim_leader(self) -> None:
        dt = self.query_one("#team_table", DataTable)
        if dt.row_count == 0:
            return

        row_key = dt.coordinate_to_cell_key(dt.cursor_coordinate).row_key
        if row_key is None:
            return
        team_name = str(row_key.value) if row_key.value else ""
        if not team_name:
            return

        data = load_data()
        team = data.get("teams", {}).get(team_name, {})
        ltype = team.get("leader_type", "")

        if ltype == "direct":
            _record_leader_reentry(team)
            save_data(data)
            await self.app.push_screen_wait(MessageBox(f"你已经是 '{team_name}' 的 Leader"))
            return

        old_leader = team.get("leader", "")
        if old_leader and ltype == "tmux":
            team["members"][old_leader]["role"] = "member"
            msg = f"🔄 原 Leader '{old_leader}' 已降级。\n✅ 你已接管 '{team_name}'！"
        else:
            msg = f"✅ 你已接管 '{team_name}' 的 Leader！"

        team["leader_type"] = "direct"
        if not team.get("leader"):
            team["leader"] = "you"
        _record_leader_reentry(team)
        save_data(data)

        await self.app.push_screen_wait(MessageBox(msg))
        self._refresh()

class TeamManagerApp(App[None]):
    CSS = """
    .main-container {
        padding: 1 2;
    }
    .detail-container {
        padding: 1 2;
    }
    # ---- 统一纵向滚动（所有子页弹窗 + 全屏页，单点维护） ----
    # 弹窗根容器在超出视口高度时纵向滚动，底部按钮/交互项在 1080P 及更低
    # 终端高度下可滚动到达。不逐页写 max-height 特例：内容低于视口时
    # max-height 不生效（height:auto 保持原宽屏布局、无滚动条）；内容超出
    # 时容器高度封顶视口、overflow-y:auto 内部滚动。新弹窗用统一类名即可
    # 获得该能力（并建议继承 ScrollableModalScreen 基类表明意图）。
    .dialog-form,
    .dialog-box,
    .context-dialog,
    .context-viewer,
    .context-editor-dialog {
        max-height: 100%;
        overflow-y: auto;
    }
    /* 表单弹窗的字段滚动区：包在 VerticalScroll 内（1fr 弹性高度，min-height:1），
       按钮行位于滚动区之外始终可达。VerticalScroll 是接收 PageDown/滚轮的
       可滚动容器（普通 Container overflow-y:auto 不绑定 page_down，键盘被外层
       截断）——这是低高度下底部按钮可达 + PageDown 不被截断的正确结构。
       内容低于视口时 1fr 无多余空间，字段区保持内容高度（宽屏布局不变）。 */
    .dialog-fields-scroll {
        height: 1fr;
        min-height: 1;
        overflow-y: auto;
    }
    /* 全屏页容器安全网：若未来内容超出视口（DataTable 已 1fr 吸收高度），
       容器内滚动而非裁剪，底部状态/提示仍可达。 */
    .main-container,
    .detail-container,
    .context-container {
        overflow-y: auto;
    }

    .dialog-box {
        width: 50;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
        align: center middle;
    }
    .dialog-form {
        width: 60;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    .dialog-title {
        width: 100%;
        text-align: center;
        padding-bottom: 1;
        border-bottom: solid $primary;
        margin-bottom: 1;
    }
    .dialog-buttons {
        width: 100%;
        align: center middle;
        margin-top: 1;
    }
    .dialog-buttons Button {
        margin: 0 1;
    }
    #agent_user_actions {
        layout: grid;
        grid-columns: auto;
        grid-rows: auto;
        grid-gutter: 0 0;
        height: auto;
        /* 对齐（right middle）唯一来源在 AgentUserManageDialog.CSS，
           此处不声明 align，避免 App center 与 dialog right 级联依赖。 */
    }
    #agent_user_actions Button {
        width: 100%;
        margin: 0;
    }
    /* Agent 用户管理弹窗的 default 变体按钮：背景 $panel 区别于
       .dialog-form 的 $surface，消除"几何区域连续但视觉为空"的色块缺失；
       hover/focus/active 均提供可辨识状态（hover 色与编辑弹窗一致）。
       仅命中带 agent-btn-default 类的按钮（只在本弹窗 default 按钮上添加），
       primary/error 变体不在此列，语义颜色保留。
       （编辑弹窗的 default 按钮底色由 AgentUserEditDialog.CSS 处理。） */
    #agent_user_actions Button.agent-btn-default {
        background: $panel;
    }
    #agent_user_actions Button.agent-btn-default:hover {
        background: $panel-lighten-1;
    }
    #agent_user_actions Button.agent-btn-default:focus {
        background: $panel-darken-1;
        text-style: $button-focus-text-style;
    }
    #agent_user_actions Button.agent-btn-default.-active {
        background: $panel;
        tint: $background 30%;
    }
    .agent-user-manage-form {
        width: 88;
        max-width: 100%;
    }
    .agent-user-list {
        height: 10;
        width: 100%;
        margin-top: 1;
        border: solid $primary;
        background: $surface;
    }
    .field-label {
        width: 14;
        text-align: right;
        padding-right: 1;
        content-align: center middle;
    }
    FormField {
        height: 4;
        align: left middle;
    }
    FormField Input,
    FormField Select {
        width: 35;
    }
    #claude_fields {
        height: auto;
        width: 100%;
    }
    #codex_fields {
        height: auto;
        width: 100%;
    }
    #mcp_status {
        height: 1;
        margin-bottom: 1;
    }
    #summary {
        height: 1;
        color: $text-muted;
        margin-bottom: 1;
    }
    #team_info {
        height: 1;
        margin-bottom: 1;
        color: $secondary;
    }
    #status_bar {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    #hint {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    #mcp_status_label {
        width: 100%;
        padding: 1 0;
    }
    #mcp_action_result {
        width: 100%;
        padding: 1 0;
        color: $accent;
    }
    #config_desc {
        width: 100%;
        padding-bottom: 1;
        color: $text-muted;
    }
    #mcp_config_status {
        height: auto;
        max-height: 18;
        overflow-y: auto;
        padding: 1;
        border: solid $primary-background;
        margin-bottom: 1;
    }
    #config_action_result {
        width: 100%;
        min-height: 1;
        padding: 1 0;
        color: $accent;
    }
    DataTable {
        height: 1fr;
        border: solid $primary-background;
    }
    .context-container {
        padding: 1 2;
    }
    #context_path {
        height: 1;
        margin-bottom: 1;
        color: $secondary;
    }
    #context_status_bar {
        height: auto;
        color: $text-muted;
        margin-top: 1;
    }
    .context-viewer {
        width: 70;
        height: 30;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    .context-editor-dialog {
        width: 80;
        height: 30;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
    }
    .context-dialog {
        width: 55;
        padding: 1 2;
        border: thick $primary;
        background: $surface;
        align: center middle;
    }
    #context_file_content {
        height: 1fr;
        overflow-y: auto;
    }
    """

    TITLE = "Multi-Agent MCP — Team Manager"
    SUB_TITLE = "团队管理 TUI"

    BINDINGS = [
        Binding("1", "mcp_manage", "MCP服务"),
        Binding("2", "mcp_config", "MCP配置"),
        Binding("3", "mcp_restart", "重启MCP"),
        Binding("q", "quit", "退出"),
    ]

    def on_mount(self) -> None:
        # agent 用户全局迁移在 CLI 入口 run_team_manager_app() 中执行（app.run() 前），
        # 避免 headless 测试实例化 App 时触达真实 teams_data.json。
        self.push_screen(MainScreen())

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """@work worker 抛异常时提示用户，而不是让整个 TUI 带 traceback 崩掉。

        绝大多数交互动作（编辑成员、保存代理配置、启停终端…）都是 @work
        worker。Textual 默认把 worker 里的未捕获异常上抛，直接终止 App —— 一个
        保存动作的 TypeError 就能让用户丢掉整个会话。这里统一降级为通知。

        注意这是**安全网，不是借口**：具体动作仍应各自校验输入。它的价值在于
        把"崩溃"变成"这一次操作失败"。
        """
        if event.state is not WorkerState.ERROR:
            return
        error = getattr(event.worker, "error", None)
        if error is None:
            return
        name = getattr(event.worker, "name", "") or "操作"
        try:
            self.notify(
                f"{name} 失败: {type(error).__name__}: {error}",
                title="操作失败",
                severity="error",
                timeout=8,
            )
        except Exception:
            # 连通知都发不出（App 正在关闭）就别再抛了，否则又是一次崩溃
            pass

    def action_quit(self) -> None:
        self.exit()

    @work
    async def action_mcp_manage(self) -> None:
        await self.app.push_screen_wait(McpStatusDialog())

    @work
    async def action_mcp_config(self) -> None:
        await self.app.push_screen_wait(AgentMcpConfigDialog())

    @work
    async def action_mcp_restart(self) -> None:
        _, msg = restart_mcp_server()
        for screen in self.screen_stack:
            if isinstance(screen, MainScreen):
                screen._refresh_mcp_status()
        self.notify(msg, timeout=3)

    @work
    async def on_unmount(self) -> None:
        running, _ = mcp_server_status()
        if not running:
            return
        confirmed = await self.app.push_screen_wait(
            ConfirmBox("MCP Server 仍在运行。\n是否在退出前停止？")
        )
        if confirmed:
            stop_mcp_server()

def run_team_manager_app() -> None:
    """CLI 启动入口：先执行 agent 用户全局迁移，再启动 TUI。

    - 迁移在 app.run() 之前执行一次（幂等 / 跨进程锁 / 0600 原子写）。
    - fail closed：迁移失败（如拿不到跨进程锁）不阻塞启动，读路径仍兼容
      旧数据；错误写 stderr 可见。
    - headless 测试实例化 TeamManagerApp 不会触发迁移（on_mount 只 push
      MainScreen），保证测试零真实文件副作用。
    """
    try:
        _migrate_agent_users_global_file()
    except (RuntimeError, OSError) as exc:
        print(
            f"[mult-agent-mcp] agent 用户全局迁移跳过（读路径兼容旧数据）: {exc}",
            file=sys.stderr,
        )
    TeamManagerApp().run()


if __name__ == "__main__":
    run_team_manager_app()
