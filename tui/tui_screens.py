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
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import Screen
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
from common.data_layer import (
    team_workspace_dir,
    team_context_dir,
    cleanup_team_artifacts,
    mark_legacy_team_deleted,
)
from common.tmux_utils import (
    find_tmux as _find_tmux,
    tmux_run as _tmux_run,
    run_command as _run,
    tmux_session_name as _tmux_session,
    find_tmux_session as _find_tmux_session,
    tmux_session_alive,
    get_member_terminal_status,
    remember_member_window_id,
    sync_team_terminal_state,
    current_tmux_session as _current_tmux_session,
    codex_command as _codex_command,
    claude_agent_args as _claude_agent_args,
    member_mode as _member_mode,
    leader_system_prompt as _leader_system_prompt,
    send_keys as _send_keys,
    agent_type,
    is_claude as _is_claude,
    is_codex as _is_codex,
)
from common.mcp_config import (
    claude_mcp_configured as _common_claude_mcp_configured,
    configure_claude_mcp as _common_configure_claude_mcp,
    codex_mcp_registered as _codex_mcp_configured,
    configure_codex_mcp,
    write_claude_mcp,
    write_claude_permissions,
    CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS,
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

def load_data(path: Path = DEFAULT_DATA_FILE) -> dict:
    if not path.exists() and path == DEFAULT_DATA_FILE and _OLD_DATA_FILE.exists():
        _migrate_data_to_mcp_home()

    if not path.exists():
        return {"teams": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)

def save_data(data: dict, path: Path = DEFAULT_DATA_FILE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _migrate_data_to_mcp_home() -> None:
    """将旧 PROJECT_DIR/teams_data.json 迁移到 ~/.mult_agent_mcp/。"""
    import shutil as _shutil

    if not _OLD_DATA_FILE.exists():
        return
    if DEFAULT_DATA_FILE.exists():
        return

    MCP_HOME.mkdir(parents=True, exist_ok=True)
    _shutil.copy2(str(_OLD_DATA_FILE), str(DEFAULT_DATA_FILE))

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
        with open(DEFAULT_DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

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

    leader_data = members.get(leader, {})
    leader_agent_name = leader_data.get("agent") or team.get("default_agent") or "claude"
    leader_agent_path = shutil.which(leader_agent_name) or leader_agent_name

    if "codex" in leader_agent_name.lower():
        rc, _, err = _tmux_run([
            "new-session", "-d", "-s", session,
            "-n", leader,
            *_codex_command(
                leader_agent_path,
                team_workspace,
                _leader_system_prompt(team_name),
                member_mode=_member_mode(leader_data),
            ),
        ])
    else:
        rc, _, err = _tmux_run([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", str(team_workspace),
            *_claude_agent_args(
                leader_agent_path,
                _member_mode(leader_data),
                allowed_tools=CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS,
            ),
        ])

    if rc != 0:
        return False, f"创建 leader 终端失败: {err}"
    remember_member_window_id(team_name, leader, session, leader)
    created = [f"👑{leader}"]

    for name, info in members.items():
        if name == leader:
            continue
        member_agent_name = info.get("agent") or team.get("default_agent") or "claude"
        member_agent_path = shutil.which(member_agent_name) or member_agent_name

        if "codex" in member_agent_name.lower():
            member_rc, _, _ = _tmux_run([
                "new-window", "-t", session, "-n", name,
                *_codex_command(
                    member_agent_path,
                    team_workspace,
                    member_mode=_member_mode(info),
                ),
            ])
        else:
            member_rc, _, _ = _tmux_run([
                "new-window", "-t", session, "-n", name,
                "-c", str(team_workspace),
                *_claude_agent_args(member_agent_path, _member_mode(info)),
            ])

        if member_rc == 0:
            remember_member_window_id(team_name, name, session, name)
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
    session = _find_tmux_session(team_name)
    if not session:
        return False, "未找到运行中的终端"

    rc, _, err = _tmux_run(["kill-session", "-t", session])
    if rc != 0:
        return False, f"关闭失败: {err}"

    data = load_data()
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
    return True, "\n".join(close_msgs + cleanup_msgs)


def open_leader_terminal(team_name: str) -> tuple[bool, str]:
    """
    打开团队 leader 终端。
    TUI 自身在 tmux 内运行时，优先在当前 tmux 中分屏 attach；
    否则使用系统图形终端，fallback 到提示命令。
    """
    session = _find_tmux_session(team_name)
    if not session:
        return False, "终端未启动，请先 launch"

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

from tui.tui_dialogs import (
    MessageBox, ConfirmBox, FormField, McpStatusDialog, AgentMcpConfigDialog,
    CreateTeamDialog, AddMemberDialog, EditMemberDialog,
)

class TeamDetailScreen(Screen[None]):
    BINDINGS = [
        Binding("a", "add_member", "添加成员"),
        Binding("r", "remove_member", "移除成员"),
        Binding("e", "edit_member", "编辑成员"),
        Binding("l", "set_leader", "指定Leader"),
        Binding("t", "launch_terminals", "启动终端"),
        Binding("k", "kill_terminals", "关闭终端"),
        Binding("0", "open_leader", "打开Leader窗口"),
        Binding("1", "mcp_manage", "MCP服务"),
        Binding("2", "mcp_config", "MCP配置"),
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
        yield Footer()

    def on_mount(self) -> None:
        dt = self.query_one("#member_table", DataTable)
        dt.add_columns("名称", "角色", "Agent", "Leader", "状态")
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
            if name == team.get("leader", "") and team.get("leader_type") == "tmux":
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

            if "codex" in member_agent_name.lower():
                rc2, _, _ = _tmux_run([
                    "new-window", "-t", session, "-n", name,
                    *_codex_command(
                        member_agent_path,
                        team_workspace,
                        member_mode=_member_mode(info),
                    ),
                ])
            else:
                rc2, _, _ = _tmux_run([
                    "new-window", "-t", session, "-n", name,
                    "-c", str(team_workspace),
                    *_claude_agent_args(member_agent_path, _member_mode(info)),
                ])

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
        terminal_alive = sync_team_terminal_state(self._team_name)
        team["terminals_active"] = terminal_alive
        terminals = "🟢 运行中" if terminal_alive else "⚫ 未启动"
        desc = team.get("description", "")
        claude_ok = "✅" if _claude_mcp_configured(self._team_name) else "⚠️"
        codex_ok = "✅" if _codex_mcp_configured() else "⚠️"

        member_status = get_member_terminal_status(self._team_name)
        alive_count = sum(1 for v in member_status.values() if v)
        total_count = len(member_status)
        window_info = f"({alive_count}/{total_count}窗口)" if total_count > 0 else ""

        info = self.query_one("#team_info", Static)
        info.update(
            f"📋 [bold]{self._team_name}[/bold]  终端:{terminals}{window_info}"
            f"  Claude MCP:{claude_ok}  Codex MCP:{codex_ok}"
            f"{'   ' + desc if desc else ''}"
        )

        dt = self.query_one("#member_table", DataTable)
        dt.clear()
        members = team.get("members", {})

        if not members:
            self.query_one("#status_bar", Static).update(
                "A 添加成员 | R 移除 | E 编辑 | L 指定Leader | 1 服务 | 2 配置 | Esc/Ctrl+Q 返回"
            )
            return

        activity_counts: dict[str, int] = {"working": 0, "idle": 0, "sleep": 0, "dead": 0}
        for name, info in members.items():
            role = info.get("role", "")
            agent = info.get("agent", default_agent)
            is_ldr = "👑" if name == leader else ""
            status_info = dict(info)
            if name == leader:
                status_info["role"] = "leader"
            status_label, status_bucket = format_member_activity_status(
                status_info,
                member_status.get(name, False),
            )
            activity_counts[status_bucket] = activity_counts.get(status_bucket, 0) + 1
            dt.add_row(name, role, agent, is_ldr, status_label, key=name)

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
        result = await self.app.push_screen_wait(AddMemberDialog())
        if result is None:
            return

        data = load_data()
        team = data.setdefault("teams", {}).setdefault(self._team_name, {})
        members = team.setdefault("members", {})

        if result["name"] in members:
            await self.app.push_screen_wait(MessageBox(f"成员 '{result['name']}' 已存在"))
            return

        members[result["name"]] = {
            "role": result["role"], "model": "", "agent": result["agent"],
        }
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
        ))
        if result is None:
            return

        member["role"] = result["role"]
        member["agent"] = result["agent"]
        save_data(data)
        self._refresh()

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

class MainScreen(Screen[None]):
    BINDINGS = [
        Binding("a", "add_team", "添加团队"),
        Binding("d", "delete_team", "删除团队"),
        Binding("enter,space", "view_team", "查看详情"),
        Binding("l", "claim_leader", "接管Leader"),
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
        yield Footer()

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
            self.query_one("#hint", Static).update("A 添加团队 | 1 服务 | 2 配置 | Q 退出")
            return

        count = 0
        for name, info in teams.items():
            terminal_alive = sync_team_terminal_state(name)
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
            "A 添加团队 | Enter/Space 查看详情 | D 删除 | L 接管Leader | 1 服务 | 2 配置 | Q 退出"
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
    .field-label {
        width: 14;
        text-align: right;
        padding-right: 1;
        content-align: center middle;
    }
    FormField {
        height: 3;
        align: left middle;
    }
    FormField Input,
    FormField Select {
        width: 35;
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
        height: 1;
        color: $text-muted;
        margin-top: 1;
    }
    #hint {
        height: 1;
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
        self.push_screen(MainScreen())

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

if __name__ == "__main__":
    app = TeamManagerApp()
    app.run()
