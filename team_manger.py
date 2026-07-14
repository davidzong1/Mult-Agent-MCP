"""
Multi-Agent MCP — Team Manager TUI
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
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
)

# ============================================================
# 路径与常量
# ============================================================

PROJECT_DIR = Path(__file__).parent
DEFAULT_DATA_FILE = PROJECT_DIR / "teams_data.json"
SERVER_SCRIPT = PROJECT_DIR / "mult_agent_mcp.py"
SERVER_PID_FILE = PROJECT_DIR / ".mcp_server.pid"
SERVER_LOG_FILE = PROJECT_DIR / ".mcp_server.log"
TEAM_WORKSPACES_DIR = PROJECT_DIR / ".team_workspaces"
SHARE_WORKSPACE_DIR = PROJECT_DIR / "share_work_space"
CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"
MCP_SERVER_NAME_CONF = "mult-agent-mcp"

AGENT_CHOICES = [
    ("claude · Claude Code", "claude"),
    ("codex  · Codex CLI", "codex"),
    ("custom · 自定义命令", "custom"),
]


# ============================================================
# 工具函数
# ============================================================

def load_data(path: Path = DEFAULT_DATA_FILE) -> dict:
    if not path.exists():
        return {"teams": {}}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict, path: Path = DEFAULT_DATA_FILE) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def _server_url() -> str:
    port = os.environ.get("FASTMCP_PORT", "8000")
    return f"http://localhost:{port}/mcp"


def _run(cmd: list[str], timeout: int = 15) -> tuple[int, str, str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "命令未找到"
    except subprocess.TimeoutExpired:
        return -1, "", "命令超时"


# ============================================================
# MCP Server 进程管理（守护进程模式）
# ============================================================

def _read_pidfile() -> int | None:
    """读取 PID 文件，返回 PID 或 None"""
    if not SERVER_PID_FILE.exists():
        return None
    try:
        pid = int(SERVER_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        SERVER_PID_FILE.unlink(missing_ok=True)
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    """用 kill(pid, 0) 检测进程是否存活"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _spawn_mcp() -> tuple[int, str | None]:
    """
    用 subprocess.Popen 启动 MCP 守护进程。
    stdout/stderr 重定向到日志文件用于故障诊断。
    返回 (pid, None) 成功, (0, err_msg) 失败。
    """
    if not SERVER_SCRIPT.exists():
        return 0, f"脚本不存在: {SERVER_SCRIPT}"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"    # 确保子进程日志即时写入，不作缓冲
    env.setdefault("FASTMCP_PORT", "8000")
    log_fp = open(SERVER_LOG_FILE, "a")
    import datetime
    log_fp.write(f"\n--- MCP spawned at {datetime.datetime.now()} ---\n")
    log_fp.flush()

    try:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT)],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            env=env,
            start_new_session=True,
        )
        log_fp.close()
        return proc.pid, None
    except FileNotFoundError:
        log_fp.close()
        return 0, f"Python解释器不存在: {sys.executable}"
    except PermissionError:
        log_fp.close()
        return 0, f"权限不足: {SERVER_SCRIPT}"
    except Exception as e:
        log_fp.close()
        return 0, f"启动异常: {e}"


def _find_mcp_processes() -> list[int]:
    """
    通过 PID 文件获取守护进程 PID，用 kill(0) 验证存活。
    如果 PID 文件中存储的进程已死，自动清理并重新扫描。
    """
    pid = _read_pidfile()
    if pid is not None:
        if _pid_alive(pid):
            return [pid]
        else:
            # PID 过期，清理
            SERVER_PID_FILE.unlink(missing_ok=True)
    return []


def mcp_server_status() -> tuple[bool, str]:
    pids = _find_mcp_processes()
    if pids:
        port = os.environ.get("FASTMCP_PORT", "8000")
        return True, f"🟢 运行中 (PID: {', '.join(map(str, pids))}, 端口: {port})"
    return False, "⚫ 未启动"


def start_mcp_server() -> tuple[bool, str]:
    """启动 MCP Server 为守护进程，PID 写入文件"""
    pid = _read_pidfile()
    if pid is not None and _pid_alive(pid):
        port = os.environ.get("FASTMCP_PORT", "8000")
        return True, f"已在运行 (PID: {pid}, 端口: {port})"

    if pid is not None:
        SERVER_PID_FILE.unlink(missing_ok=True)

    new_pid, err = _spawn_mcp()
    if err is not None:
        return False, f"❌ 守护进程启动失败: {err}"

    SERVER_PID_FILE.write_text(str(new_pid))

    # 渐进式等待：先等 0.5s，再 1s，最多等 2s 确认进程存活
    import time
    for delay in (0.5, 1.0):
        time.sleep(delay)
        if _pid_alive(new_pid):
            return True, f"✅ 守护进程已启动 (PID: {new_pid})"

    # 进程已死，尝试读取日志定位原因
    tail = ""
    if SERVER_LOG_FILE.exists():
        try:
            lines = SERVER_LOG_FILE.read_text().splitlines()
            tail = "\n".join(lines[-5:])  # 最后 5 行
        except Exception:
            pass
    SERVER_PID_FILE.unlink(missing_ok=True)
    return False, f"❌ 进程启动后退出 (PID: {new_pid})\n日志尾部:\n{tail}"


def stop_mcp_server() -> tuple[bool, str]:
    """通过 PID 文件找到守护进程并 kill"""
    pid = _read_pidfile()
    if pid is None:
        return True, "MCP Server 未在运行"

    if not _pid_alive(pid):
        SERVER_PID_FILE.unlink(missing_ok=True)
        return True, "MCP Server 进程已不存在（PID 文件已清理）"

    # 先尝试优雅退出 SIGTERM
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        SERVER_PID_FILE.unlink(missing_ok=True)
        return True, "进程已不存在"

    # 等待进程退出，最多 3 秒
    import time
    for _ in range(30):
        time.sleep(0.1)
        if not _pid_alive(pid):
            SERVER_PID_FILE.unlink(missing_ok=True)
            return True, f"✅ 守护进程已停止 (PID: {pid})"

    # SIGTERM 超时，强杀
    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
    except OSError:
        pass

    SERVER_PID_FILE.unlink(missing_ok=True)
    if not _pid_alive(pid):
        return True, f"✅ 守护进程已强制停止 (PID: {pid})"
    return False, f"❌ 无法停止进程 (PID: {pid})"


def restart_mcp_server() -> tuple[bool, str]:
    stop_mcp_server()
    import time; time.sleep(0.5)
    return start_mcp_server()


# ============================================================
# tmux 终端管理（从 TUI 直接启动团队终端）
# ============================================================

def _tmux_session(team_name: str) -> str:
    return f"mcp_{team_name}"


def _find_tmux_session(team_name: str) -> str | None:
    """
    查找团队的实际 tmux session 名称，支持两种命名格式：
      1. mcp_{team}           (MCP server 创建，无时间戳)
      2. mcp_{team}_HHMMSS    (TUI 创建，带时间戳)
    如果有多个匹配项，优先返回精确匹配，其次返回最新的。
    """
    rc, out, _ = _tmux_run(["list-sessions", "-F", "#{session_name}"])
    if rc != 0:
        return None

    sessions = out.split("\n")
    exact = f"mcp_{team_name}"
    prefix = f"mcp_{team_name}_"

    match = None
    for name in sessions:
        name = name.strip()
        if name == exact:
            return exact  # 精确匹配优先
        if name.startswith(prefix):
            match = name  # 最后一个匹配即为最新

    return match


def _find_tmux() -> str | None:
    """查找 tmux 可执行文件路径，缓存结果"""
    if not hasattr(_find_tmux, "_cache"):
        _find_tmux._cache = shutil.which("tmux")  # type: ignore[attr-defined]
        if not _find_tmux._cache:
            for p in ["/usr/bin/tmux", "/usr/local/bin/tmux", "/opt/homebrew/bin/tmux"]:
                if Path(p).exists():
                    _find_tmux._cache = p
                    break
    return _find_tmux._cache  # type: ignore[attr-defined]


def _tmux_run(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """执行 tmux 命令，自动在命令前加上 tmux 的绝对路径"""
    tmux_path = _find_tmux()
    if not tmux_path:
        return -1, "", "tmux 未安装，请执行 sudo apt install tmux"
    try:
        full_cmd = [tmux_path] + cmd
        r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", "tmux 未安装"
    except subprocess.TimeoutExpired:
        return -1, "", "超时"


def tmux_session_alive(team_name: str) -> bool:
    return _find_tmux_session(team_name) is not None


def get_member_terminal_status(team_name: str) -> dict[str, bool]:
    """
    返回团队中每个成员的 tmux 窗口存活状态。
    返回: {member_name: True/False, ...}
    如果没有找到 session 或成员列表为空，返回空 dict。
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    members = team.get("members", {})
    if not members:
        return {}

    session = _find_tmux_session(team_name)
    if not session:
        return {name: False for name in members}

    rc, out, _ = _tmux_run([
        "list-windows", "-t", session, "-F", "#{window_name}",
    ])
    if rc != 0:
        return {name: False for name in members}

    alive_windows = set(out.split("\n")) if out else set()
    return {name: name in alive_windows for name in members}


def launch_terminals(team_name: str) -> tuple[bool, str]:
    """
    为团队创建 tmux session，每个成员一个窗口。
    所有成员共享工作目录和 MCP 连接，实现上下文完全共享：
    - 统一工作目录: .team_workspaces/{team}/
    - MCP 配置: claude 成员从 team workspace 启动以继承 .claude/mcp.json
              codex 成员通过全局 codex config 连接 MCP
    - 共享文件区: share_work_space/{team}/ 供所有成员读写

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

    # 检查 tmux
    rc, _, _ = _tmux_run(["-V"])
    if rc != 0:
        return False, "tmux 未安装，请执行 sudo apt install tmux"

    # 生成带时间戳的 session 名，永不冲突
    import datetime
    session = _tmux_session(f"{team_name}_{datetime.datetime.now().strftime('%H%M%S')}")

    # 重置状态
    team["terminals_active"] = False
    save_data(data)

    import time

    # ---- 准备共享工作目录 ----
    team_workspace = TEAM_WORKSPACES_DIR / team_name
    team_workspace.mkdir(parents=True, exist_ok=True)
    share_dir = SHARE_WORKSPACE_DIR / team_name
    share_dir.mkdir(parents=True, exist_ok=True)

    # ---- 为所有成员统一配置 MCP ----
    # Claude: 写入 .claude/mcp.json 到 team workspace（所有从该目录启动的成员自动加载）
    claude_msg = ""
    if any(("claude" in (members.get(n, {}).get("agent") or team.get("default_agent", "claude")).lower())
           for n in members):
        _, claude_msg = configure_claude_mcp(team_name)
    # Codex: 全局注册（所有 codex 成员共享）
    codex_msg = ""
    if any(("codex" in (members.get(n, {}).get("agent") or team.get("default_agent", "claude")).lower())
           for n in members):
        _, codex_msg = configure_codex_mcp()

    mcp_msgs = ["上下文共享模式: 所有成员共享工作目录 + MCP 连接"]
    if claude_msg:
        mcp_msgs.append(f"  Claude: {claude_msg}")
    if codex_msg:
        mcp_msgs.append(f"  Codex: {codex_msg}")
    mcp_msgs.append(f"  📁 工作目录: {team_workspace}")
    mcp_msgs.append(f"  📂 共享区: {share_dir}")

    # ---- Leader 窗口 ----
    leader_data = members.get(leader, {})
    leader_agent_name = leader_data.get("agent") or team.get("default_agent") or "claude"
    leader_agent_path = shutil.which(leader_agent_name) or leader_agent_name

    if "codex" in leader_agent_name.lower():
        # Codex leader: 不使用 -c（codex 用全局 config）
        rc, _, err = _tmux_run([
            "new-session", "-d", "-s", session,
            "-n", leader,
            leader_agent_path,
        ])
    else:
        # Claude / 其他: 从 team workspace 启动
        rc, _, err = _tmux_run([
            "new-session", "-d", "-s", session,
            "-n", leader,
            "-c", str(team_workspace),
            leader_agent_path,
        ])

    if rc != 0:
        return False, f"创建 leader 终端失败: {err}"
    created = [f"👑{leader}"]

    # ---- 成员窗口: 统一从 team workspace 启动，共享 MCP 和文件 ----
    for name, info in members.items():
        if name == leader:
            continue
        member_agent_name = info.get("agent") or team.get("default_agent") or "claude"
        member_agent_path = shutil.which(member_agent_name) or member_agent_name

        if "codex" in member_agent_name.lower():
            # Codex 成员: 不使用 -c
            _tmux_run([
                "new-window", "-t", session, "-n", name,
                member_agent_path,
            ])
        else:
            # Claude / 其他成员: 从共享工作目录启动
            _tmux_run([
                "new-window", "-t", session, "-n", name,
                "-c", str(team_workspace),
                member_agent_path,
            ])

        created.append(name)
        time.sleep(0.08)

    # 更新数据
    team["terminals_active"] = True
    save_data(data)

    total = len(created)
    return True, (
        f"🚀 终端已启动！（上下文共享模式）\n"
        f"   session: {session}\n"
        f"   窗口({total}): {' | '.join(created)}\n"
        f"   {' | '.join(mcp_msgs)}\n\n"
        f"进入 leader 终端:\n"
        f"   tmux attach -t {session}\n\n"
        f"💡 所有成员共享工作目录 + MCP 连接，可通过共享区交换文件\n"
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


def open_leader_terminal(team_name: str) -> tuple[bool, str]:
    """
    在系统默认终端中打开 tmux attach，直接进入 leader 窗口。
    优先使用 gnome-terminal，fallback 到 xterm 或仅提示命令。
    """
    session = _find_tmux_session(team_name)
    if not session:
        return False, "终端未启动，请先 launch"

    tmux = _find_tmux() or "tmux"

    if shutil.which("gnome-terminal"):
        subprocess.Popen(
            ["gnome-terminal", "--", tmux, "attach", "-t", session],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, f"已在新窗口打开 {session}"

    if shutil.which("xterm"):
        subprocess.Popen(
            ["xterm", "-e", f"{tmux} attach -t {session}"],
            start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return True, f"已在新 xterm 窗口打开 {session}"

    # 仅给出命令
    cmd = f"{tmux} attach -t {session}"
    return True, f"请在另一个终端执行:\n  {cmd}"


# ============================================================
# MCP 客户端配置（Claude + Codex 自动配置）
# ============================================================

def _claude_mcp_configured(team_name: str) -> bool:
    mcp_json = TEAM_WORKSPACES_DIR / team_name / ".claude" / "mcp.json"
    return mcp_json.exists()


def _all_teams_claude_status() -> dict[str, bool]:
    return {name: _claude_mcp_configured(name) for name in load_data().get("teams", {})}


def configure_claude_mcp(team_name: str) -> tuple[bool, str]:
    claude_dir = TEAM_WORKSPACES_DIR / team_name / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    mcp_json = claude_dir / "mcp.json"
    config = {
        "teamMCP": {
            MCP_SERVER_NAME_CONF: {"type": "sse", "url": _server_url()}
        }
    }
    try:
        mcp_json.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        return True, f"✅ {team_name} → {mcp_json}"
    except Exception as e:
        return False, f"❌ 写入失败: {e}"


def configure_all_claude_mcp() -> list[tuple[str, bool, str]]:
    results = []
    for team_name in load_data().get("teams", {}):
        results.append((team_name, *configure_claude_mcp(team_name)))
    return results


def _codex_mcp_configured() -> bool:
    if not CODEX_CONFIG_PATH.exists():
        return False
    return f"[mcp_servers.{MCP_SERVER_NAME_CONF}]" in CODEX_CONFIG_PATH.read_text(encoding="utf-8")


def configure_codex_mcp() -> tuple[bool, str]:
    if _codex_mcp_configured():
        return True, "Codex MCP 已注册（无需重复）"

    # 方式 1: CLI
    rc, _, _ = _run(["codex", "mcp", "add", MCP_SERVER_NAME_CONF, "--url", _server_url()], timeout=15)
    if rc == 0:
        return True, "✅ 已通过 CLI 注册"

    # 方式 2: 直接写配置文件
    section = (
        f"\n[mcp_servers.{MCP_SERVER_NAME_CONF}]\n"
        f'type = "sse"\n'
        f'url = "{_server_url()}"\n'
    )
    try:
        with open(CODEX_CONFIG_PATH, "a", encoding="utf-8") as f:
            f.write(section)
        return True, f"✅ 已写入 ~/.codex/config.toml"
    except Exception as e:
        return False, f"❌ 配置失败: {e}\n💡 手动: codex mcp add {MCP_SERVER_NAME_CONF} --url {_server_url()}"


# ============================================================
# 对话框组件
# ============================================================

class MessageBox(ModalScreen[None]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Label(f"  {self._message}  "),
            Button("确定", variant="primary", id="msg_ok"),
            classes="dialog-box",
        )

    @on(Button.Pressed, "#msg_ok")
    def dismiss_msg(self) -> None:
        self.dismiss(None)


class ConfirmBox(ModalScreen[bool]):
    def __init__(self, message: str) -> None:
        super().__init__()
        self._message = message

    def compose(self) -> ComposeResult:
        yield Container(
            Label(self._message),
            Horizontal(
                Button("确认", variant="error", id="btn_yes"),
                Button("取消", variant="default", id="btn_no"),
                classes="dialog-buttons",
            ),
            classes="dialog-box",
        )

    @on(Button.Pressed, "#btn_yes")
    def on_yes(self) -> None:
        self.dismiss(True)

    @on(Button.Pressed, "#btn_no")
    def on_no(self) -> None:
        self.dismiss(False)


class FormField(Horizontal):
    def __init__(self, label: str, widget: Input | Select[tuple[str, str]]) -> None:
        super().__init__()
        self._label = label
        self._widget = widget

    def compose(self) -> ComposeResult:
        yield Label(self._label, classes="field-label")
        yield self._widget


# ============================================================
# MCP 服务管理对话框
# ============================================================

class McpStatusDialog(ModalScreen[None]):
    BINDINGS = [
        Binding("escape", "close_dialog", "关闭"),
    ]
    def compose(self) -> ComposeResult:
        running, status_text = mcp_server_status()
        btn_label = "🛑 停止服务" if running else "🚀 启动服务"

        yield Container(
            Label("[bold]MCP Server 管理[/bold]", classes="dialog-title"),
            Label(status_text, id="mcp_status_label"),
            Label("", id="mcp_action_result"),
            Horizontal(
                Button(btn_label, variant="primary", id="btn_toggle"),
                Button("🔄 重启服务", variant="default", id="btn_restart"),
                Button("关闭", variant="default", id="btn_close"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_toggle")
    def toggle(self) -> None:
        running, _ = mcp_server_status()
        if running:
            _, msg = stop_mcp_server()
        else:
            _, msg = start_mcp_server()
        self._refresh_buttons()
        self.query_one("#mcp_action_result", Label).update(msg)

    @on(Button.Pressed, "#btn_restart")
    @work
    async def restart(self) -> None:
        self.query_one("#mcp_action_result", Label).update("🔄 正在重启...")
        _, msg = restart_mcp_server()
        self._refresh_buttons()
        self.query_one("#mcp_action_result", Label).update(msg)

    @on(Button.Pressed, "#btn_close")
    def close_dialog(self) -> None:
        self.dismiss(None)

    def _refresh_buttons(self) -> None:
        running, status_text = mcp_server_status()
        self.query_one("#mcp_status_label", Label).update(status_text)
        self.query_one("#btn_toggle", Button).label = (
            "🛑 停止服务" if running else "🚀 启动服务"
        )


# ============================================================
# Agent MCP 配置对话框
# ============================================================

class AgentMcpConfigDialog(ModalScreen[None]):
    """一键为 Claude Code / Codex CLI 配置 MCP 连接"""

    BINDINGS = [
        Binding("escape", "close_dialog", "关闭"),
    ]

    def compose(self) -> ComposeResult:
        teams = load_data().get("teams", {})
        codex_icon = "✅" if _codex_mcp_configured() else "❌"

        rows = [Label(f"  {codex_icon}  [bold]Codex CLI[/bold] (全局)")]
        for name in teams:
            icon = "✅" if _claude_mcp_configured(name) else "❌"
            rows.append(Label(f"  {icon}  [bold]Claude Code[/bold] → {name}"))
        if not teams:
            rows.append(Label("  📭 暂无团队"))

        yield Container(
            Label("[bold]Agent MCP 配置[/bold]", classes="dialog-title"),
            Label("为 Claude Code / Codex CLI 配置 MCP 连接", id="config_desc"),
            Vertical(*rows, Label(f"  [dim]{_server_url()}[/dim]"), id="mcp_config_status"),
            Label("", id="config_action_result"),
            Horizontal(
                Button("🔧 配置所有", variant="primary", id="btn_config_all"),
                Button("📄 Claude", variant="default", id="btn_config_claude"),
                Button("📄 Codex", variant="default", id="btn_config_codex"),
                classes="dialog-buttons",
            ),
            Horizontal(
                Button("关闭", variant="default", id="btn_close"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_config_all")
    @work
    async def config_all(self) -> None:
        msgs = []
        for name in load_data().get("teams", {}):
            ok, msg = configure_claude_mcp(name)
            msgs.append(f"  {'✅' if ok else '❌'} Claude({name})")
        ok, msg = configure_codex_mcp()
        msgs.append(f"  {'✅' if ok else '❌'} Codex: {msg}")
        self.query_one("#config_action_result", Label).update("\n".join(msgs) or "  ⚠️ 无团队")
        self._refresh_status()

    @on(Button.Pressed, "#btn_config_claude")
    @work
    async def config_claude(self) -> None:
        msgs = []
        for name in load_data().get("teams", {}):
            ok, _ = configure_claude_mcp(name)
            msgs.append(f"  {'✅' if ok else '❌'} {name}")
        self.query_one("#config_action_result", Label).update("\n".join(msgs) or "  📭 无团队")
        self._refresh_status()

    @on(Button.Pressed, "#btn_config_codex")
    @work
    async def config_codex(self) -> None:
        ok, msg = configure_codex_mcp()
        self.query_one("#config_action_result", Label).update(f"  {'✅' if ok else '❌'} {msg}")
        self._refresh_status()

    @on(Button.Pressed, "#btn_close")
    def close_dialog(self) -> None:
        self.dismiss(None)

    def _refresh_status(self) -> None:
        status = self.query_one("#mcp_config_status", Vertical)
        status.remove_children()
        teams = load_data().get("teams", {})
        codex_icon = "✅" if _codex_mcp_configured() else "❌"
        status.mount(Label(f"  {codex_icon}  [bold]Codex CLI[/bold] (全局)"))
        for name in teams:
            icon = "✅" if _claude_mcp_configured(name) else "❌"
            status.mount(Label(f"  {icon}  [bold]Claude Code[/bold] → {name}"))
        if not teams:
            status.mount(Label("  📭 暂无团队"))
        status.mount(Label(f"  [dim]{_server_url()}[/dim]"))


# ============================================================
# 表单对话框
# ============================================================

class CreateTeamDialog(ModalScreen[dict | None]):
    def compose(self) -> ComposeResult:
        agent_options = [(label, value) for label, value in AGENT_CHOICES]
        yield Container(
            Label("[bold]创建新团队[/bold]", classes="dialog-title"),
            FormField("团队名称", Input(placeholder="如 dev_team", id="name")),
            FormField("描述", Input(placeholder="选填", id="desc")),
            FormField("默认 Agent", Select(agent_options, id="agent", value="claude")),
            Horizontal(
                Button("创建", variant="primary", id="btn_create"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_create")
    def create(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.app.push_screen(MessageBox("团队名称不能为空"))
            return
        desc = self.query_one("#desc", Input).value.strip()
        agent = self.query_one("#agent", Select).value
        self.dismiss({"name": name, "description": desc, "default_agent": agent})

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class AddMemberDialog(ModalScreen[dict | None]):
    def compose(self) -> ComposeResult:
        agent_options = [(label, value) for label, value in AGENT_CHOICES]
        yield Container(
            Label("[bold]添加成员[/bold]", classes="dialog-title"),
            FormField("成员名称", Input(placeholder="如 alice", id="name")),
            FormField("角色", Input(placeholder="如 coder / tester / reviewer", id="role")),
            FormField("Agent", Select(agent_options, id="agent", value="claude")),
            Horizontal(
                Button("添加", variant="primary", id="btn_add"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_add")
    def add(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.app.push_screen(MessageBox("成员名称不能为空"))
            return
        role = self.query_one("#role", Input).value.strip()
        agent = self.query_one("#agent", Select).value
        self.dismiss({"name": name, "role": role, "agent": agent})

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class EditMemberDialog(ModalScreen[dict | None]):
    def __init__(self, member_name: str, current_role: str, current_agent: str) -> None:
        super().__init__()
        self._member_name = member_name
        self._role = current_role
        self._agent = current_agent

    def compose(self) -> ComposeResult:
        agent_options = [(label, value) for label, value in AGENT_CHOICES]
        yield Container(
            Label(f"[bold]编辑 {self._member_name}[/bold]", classes="dialog-title"),
            FormField("角色", Input(value=self._role, placeholder="角色", id="role")),
            FormField("Agent", Select(agent_options, id="agent", value=self._agent)),
            Horizontal(
                Button("保存", variant="primary", id="btn_save"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        self.dismiss({
            "role": self.query_one("#role", Input).value.strip(),
            "agent": self.query_one("#agent", Select).value,
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


# ============================================================
# 团队详情 Screen
# ============================================================

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
        # 每 5 秒自动刷新终端状态
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
            # 跳过 leader
            if name == team.get("leader", "") and team.get("leader_type") == "tmux":
                continue

            # 窗口存活，跳过
            if name in alive_windows:
                continue

            # 窗口不存在：判断是休眠还是异常
            task_completed = info.get("last_task_completed", True)
            has_task = bool(info.get("last_task", ""))

            if has_task and task_completed:
                # 成员完成任务后主动休眠，不打扰
                continue

            # --- 异常退出（无任务 或 任务未完成就挂了）→ 自动恢复 ---
            member_agent_name = info.get("agent") or team.get("default_agent") or "claude"
            member_agent_path = shutil.which(member_agent_name) or member_agent_name
            team_workspace = TEAM_WORKSPACES_DIR / self._team_name
            team_workspace.mkdir(parents=True, exist_ok=True)

            configure_claude_mcp(self._team_name)
            configure_codex_mcp()

            if "codex" in member_agent_name.lower():
                rc2, _, _ = _tmux_run([
                    "new-window", "-t", session, "-n", name,
                    member_agent_path,
                ])
            else:
                rc2, _, _ = _tmux_run([
                    "new-window", "-t", session, "-n", name,
                    "-c", str(team_workspace),
                    member_agent_path,
                ])

            if rc2 != 0:
                continue

            _time.sleep(0.5)

            # 重发未完成的任务
            if has_task and not task_completed:
                last_context = info.get("last_context", "")
                full_msg = info["last_task"]
                if last_context:
                    full_msg = f"[上下文] {last_context}\n[子任务] {full_msg}"
                _tmux_run(["send-keys", "-t", f"{session}:{name}", "-l", full_msg])
                _tmux_run(["send-keys", "-t", f"{session}:{name}", "Enter"])
                self.notify(f"🔄 成员 '{name}' 已恢复并重发任务", timeout=3)
            else:
                self.notify(f"🔄 成员 '{name}' 已自动恢复", timeout=3)

    def _refresh(self) -> None:
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})

        leader = team.get("leader", "")
        default_agent = team.get("default_agent", "claude")
        terminals = "🟢 运行中" if team.get("terminals_active") else "⚫ 未启动"
        desc = team.get("description", "")
        claude_ok = "✅" if _claude_mcp_configured(self._team_name) else "⚠️"
        codex_ok = "✅" if _codex_mcp_configured() else "⚠️"

        # 获取成员终端存活状态用于窗口计数
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

        for name, info in members.items():
            role = info.get("role", "")
            agent = info.get("agent", default_agent)
            is_ldr = "👑" if name == leader else ""
            # 状态: 🟢运行中  😴休眠(任务已完成)  ⚫未启动/异常退出
            if member_status.get(name):
                status_icon = "🟢"
            elif info.get("last_task") and info.get("last_task_completed", True):
                status_icon = "😴"
            else:
                status_icon = "⚫"
            dt.add_row(name, role, agent, is_ldr, status_icon, key=name)

        ltype = team.get("leader_type", "")
        # 统计各状态成员数
        sleeping_count = sum(
            1 for n, i in members.items()
            if not member_status.get(n) and i.get("last_task") and i.get("last_task_completed", True)
        )
        status_parts = [f"{len(members)} 个成员"]
        if total_count > 0:
            status_parts.append(f"🟢{alive_count} 😴{sleeping_count} ⚫{total_count - alive_count - sleeping_count}")
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


# ============================================================
# 主界面 Screen
# ============================================================

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
            terminal = "🟢" if info.get("terminals_active") else "⚫"
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

        del data["teams"][team_name]
        save_data(data)
        self._refresh()

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


# ============================================================
# 主 App
# ============================================================

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
