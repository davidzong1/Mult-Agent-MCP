"""Textual modal dialogs used by the team manager TUI."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile as _tempfile

from textual import on, work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select

from common.config import server_url as _server_url
from common.data_layer import load_data, team_workspace_dir
from common.mcp_config import (
    claude_mcp_configured as _common_claude_mcp_configured,
    codex_mcp_registered as _codex_mcp_configured,
    configure_claude_mcp as _common_configure_claude_mcp,
    configure_codex_mcp,
)
from common.mcp_daemon import (
    mcp_server_status,
    start_mcp_server,
    stop_mcp_server,
    restart_mcp_server,
)

AGENT_CHOICES = [
    ("claude · Claude Code", "claude"),
    ("codex  · Codex CLI", "codex"),
    ("custom · 自定义命令", "custom"),
]

PROXY_MODE_CHOICES = [
    ("继承团队", "inherit"),
    ("启用代理", "enabled"),
    ("禁用代理", "disabled"),
]

PROXY_ENABLED_CHOICES = [
    ("禁用", "disabled"),
    ("启用", "enabled"),
]

PROXY_ACTION_CHOICES = [
    ("启用", "enabled"),
    ("禁用", "disabled"),
    ("全部启用", "all_enabled"),
    ("全部禁用", "all_disabled"),
]

def _claude_mcp_configured(team_name: str) -> bool:
    return _common_claude_mcp_configured(team_workspace_dir(team_name))

def configure_claude_mcp(team_name: str) -> tuple[bool, str]:
    try:
        return True, _common_configure_claude_mcp(team_name, team_workspace_dir(team_name))
    except Exception as e:
        return False, f"❌ Claude MCP 配置失败: {e}"

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
    @work
    async def toggle(self) -> None:
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
        proxy_enabled_options = [(label, value) for label, value in PROXY_ENABLED_CHOICES]
        yield Container(
            Label("[bold]创建新团队[/bold]", classes="dialog-title"),
            FormField("团队名称", Input(placeholder="如 dev_team", id="name")),
            FormField("描述", Input(placeholder="选填", id="desc")),
            FormField("默认 Agent", Select(agent_options, id="agent", value="claude")),
            FormField("代理", Select(proxy_enabled_options, id="proxy_enabled", value="disabled")),
            FormField("代理主机", Input(placeholder="127.0.0.1", id="proxy_host")),
            FormField("代理端口", Input(placeholder="7890", id="proxy_port")),
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
        proxy_enabled = self.query_one("#proxy_enabled", Select).value == "enabled"
        proxy_host = self.query_one("#proxy_host", Input).value.strip() or "127.0.0.1"
        proxy_port_str = self.query_one("#proxy_port", Input).value.strip() or "7890"
        try:
            proxy_port = int(proxy_port_str)
        except ValueError:
            proxy_port = 7890
        self.dismiss({
            "name": name,
            "description": desc,
            "default_agent": agent,
            "proxy": {
                "enabled": proxy_enabled,
                "host": proxy_host,
                "port": proxy_port,
            },
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class AddMemberDialog(ModalScreen[dict | None]):
    def __init__(self, default_agent: str = "claude") -> None:
        super().__init__()
        self._default_agent = default_agent or "claude"

    def compose(self) -> ComposeResult:
        agent_options = [(label, value) for label, value in AGENT_CHOICES]
        proxy_options = [(label, value) for label, value in PROXY_MODE_CHOICES]
        yield Container(
            Label("[bold]添加成员[/bold]", classes="dialog-title"),
            FormField("成员名称", Input(placeholder="如 alice", id="name")),
            FormField("角色", Input(placeholder="如 coder / tester / reviewer", id="role")),
            FormField("Agent", Select(agent_options, id="agent", value=self._default_agent)),
            FormField("代理模式", Select(proxy_options, id="proxy_mode", value="inherit")),
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
        proxy_mode = self.query_one("#proxy_mode", Select).value
        self.dismiss({
            "name": name, "role": role, "agent": agent, "proxy_mode": proxy_mode,
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class EditMemberDialog(ModalScreen[dict | None]):
    def __init__(self, member_name: str, current_role: str, current_agent: str, current_proxy_mode: str = "inherit") -> None:
        super().__init__()
        self._member_name = member_name
        self._role = current_role
        self._agent = current_agent
        self._proxy_mode = current_proxy_mode or "inherit"

    def compose(self) -> ComposeResult:
        agent_options = [(label, value) for label, value in AGENT_CHOICES]
        proxy_options = [(label, value) for label, value in PROXY_MODE_CHOICES]
        yield Container(
            Label(f"[bold]编辑 {self._member_name}[/bold]", classes="dialog-title"),
            FormField("角色", Input(value=self._role, placeholder="角色", id="role")),
            FormField("Agent", Select(agent_options, id="agent", value=self._agent)),
            FormField("代理模式", Select(proxy_options, id="proxy_mode", value=self._proxy_mode)),
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
            "proxy_mode": self.query_one("#proxy_mode", Select).value,
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class TeamProxyDialog(ModalScreen[dict | None]):
    """编辑团队代理配置"""

    def __init__(
        self,
        team_name: str,
        current_proxy: dict | None = None,
        current_member: str = "",
    ) -> None:
        super().__init__()
        self._team_name = team_name
        self._current_member = current_member
        proxy = current_proxy or {}
        self._proxy_host = proxy.get("host", "127.0.0.1")
        self._proxy_port = str(proxy.get("port", 7890))

    def compose(self) -> ComposeResult:
        proxy_action_options = [(label, value) for label, value in PROXY_ACTION_CHOICES]
        target = self._current_member or "未选择成员"
        yield Container(
            Label(f"[bold]{self._team_name} 代理配置[/bold]", classes="dialog-title"),
            Label(f"当前成员: {target}", classes="dialog-hint"),
            FormField("代理", Select(proxy_action_options, id="proxy_action", value="enabled")),
            FormField("代理主机", Input(value=self._proxy_host, placeholder="127.0.0.1", id="proxy_host")),
            FormField("代理端口", Input(value=self._proxy_port, placeholder="7890", id="proxy_port")),
            Horizontal(
                Button("保存", variant="primary", id="btn_save"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        proxy_action = self.query_one("#proxy_action", Select).value
        proxy_host = self.query_one("#proxy_host", Input).value.strip() or "127.0.0.1"
        proxy_port_str = self.query_one("#proxy_port", Input).value.strip() or "7890"
        try:
            proxy_port = int(proxy_port_str)
        except ValueError:
            proxy_port = 7890
        self.dismiss({
            "action": proxy_action,
            "host": proxy_host,
            "port": proxy_port,
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


# ============================================================
# 上下文文件管理对话框
# ============================================================

class ContextErrorDialog(ModalScreen[None]):
    """显示上下文文件操作错误。"""

    BINDINGS = [
        Binding("escape", "dismiss_dialog", "关闭"),
    ]

    def __init__(self, rel_path: str, error: str) -> None:
        super().__init__()
        self._rel_path = rel_path
        self._error = error

    def compose(self) -> ComposeResult:
        yield Container(
            Label(f"[bold]错误[/bold] — {self._rel_path}", classes="dialog-title"),
            Label(f"  {self._error}"),
            Horizontal(
                Button("确定", variant="primary", id="btn_ok"),
                classes="dialog-buttons",
            ),
            classes="dialog-box",
        )

    @on(Button.Pressed, "#btn_ok")
    def action_dismiss_dialog(self) -> None:
        self.dismiss(None)


class ContextConfirmDeleteDialog(ModalScreen[bool]):
    """确认删除上下文文件对话框。"""

    BINDINGS = [
        Binding("escape", "dismiss_cancel", "取消"),
    ]

    def __init__(self, rel_path: str) -> None:
        super().__init__()
        self._rel_path = rel_path

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]确认删除[/bold]", classes="dialog-title"),
            Label(f"  确定要删除 [bold]{self._rel_path}[/bold] 吗？\n  此操作不可撤销。"),
            Horizontal(
                Button("删除", variant="error", id="btn_yes"),
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

    def action_dismiss_cancel(self) -> None:
        self.dismiss(False)


class ContextConfirmDeleteAllDialog(ModalScreen[bool]):
    """确认删除全部未锁定上下文文件。"""

    BINDINGS = [
        Binding("escape", "dismiss_cancel", "取消"),
    ]

    def __init__(self, delete_count: int, locked_count: int) -> None:
        super().__init__()
        self._delete_count = delete_count
        self._locked_count = locked_count

    def compose(self) -> ComposeResult:
        locked_note = (
            f"\n  将保留 {self._locked_count} 个上锁文件。"
            if self._locked_count else ""
        )
        yield Container(
            Label("[bold]确认清空上下文[/bold]", classes="dialog-title"),
            Label(
                f"  将删除 {self._delete_count} 个未锁定文件。"
                f"{locked_note}\n  此操作不可撤销。"
            ),
            Horizontal(
                Button("删除全部", variant="error", id="btn_yes"),
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

    def action_dismiss_cancel(self) -> None:
        self.dismiss(False)


# ---- 常量 ----

_MAX_CONTEXT_FILE_BYTES = 5 * 1024 * 1024  # 5 MB，查看/编辑上限


# ---- 纯函数（无 DOM 依赖，可独立测试） ----

def _atomic_write_text(full_path: Path, content: str) -> None:
    """同目录临时文件 + os.replace 原子写入。

    Raises OSError on failure.
    """
    full_path.parent.mkdir(parents=True, exist_ok=True)
    dir_fd = os.open(str(full_path.parent), os.O_RDONLY)
    tmp_path = ""
    try:
        with _tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=str(full_path.parent),
            prefix=f".{full_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tf:
            tf.write(content)
            tmp_path = tf.name
        os.chmod(tmp_path, 0o644)
        os.replace(tmp_path, str(full_path))
    finally:
        os.close(dir_fd)
        try:
            os.unlink(tmp_path)
        except (OSError, NameError):
            pass


class ContextFileViewer(ModalScreen[None]):
    """查看上下文文件内容。"""

    BINDINGS = [
        Binding("escape", "close_viewer", "关闭"),
    ]

    def __init__(self, rel_path: str, full_path: Path) -> None:
        super().__init__()
        self._rel_path = rel_path
        self._full_path = full_path
        self._error: str | None = None
        self._content = ""
        self._read_content()

    def _read_content(self) -> None:
        try:
            stat = self._full_path.stat()
            if stat.st_size > _MAX_CONTEXT_FILE_BYTES:
                size_mb = stat.st_size / (1024 * 1024)
                self._error = (
                    f"文件过大 ({size_mb:.1f} MB)，上限 {_MAX_CONTEXT_FILE_BYTES // 1048576} MB。"
                    f"\n请使用终端编辑器打开: {self._full_path}"
                )
                return
            self._content = self._full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self._error = "无法显示: 文件包含非 UTF-8 内容"
        except OSError as e:
            self._error = f"读取失败: {e}"

    def compose(self) -> ComposeResult:
        from textual.widgets import TextArea

        if self._error:
            yield Container(
                Label(f"[bold]⚠️ {self._rel_path}[/bold]", classes="dialog-title"),
                Label(f"  {self._error}"),
                Horizontal(
                    Button("关闭", variant="primary", id="btn_close"),
                    classes="dialog-buttons",
                ),
                classes="context-viewer",
            )
        else:
            yield Container(
                Label(f"[bold]查看: {self._rel_path}[/bold]", classes="dialog-title"),
                TextArea(self._content, read_only=True, id="context_file_content"),
                Horizontal(
                    Button("关闭", variant="primary", id="btn_close"),
                    classes="dialog-buttons",
                ),
                classes="context-viewer",
            )

    @on(Button.Pressed, "#btn_close")
    def close_viewer(self) -> None:
        self.dismiss(None)

    def action_close_viewer(self) -> None:
        self.dismiss(None)


class ContextFileEditor(ModalScreen[bool]):
    """编辑上下文文件内容。

    并发安全: 打开时记录 stat (mtime_ns + size)，保存前比较，冲突则拒绝覆盖。
    原子写入: 同目录临时文件 + os.replace（通过 _atomic_write_text）。
    """

    BINDINGS = [
        Binding("ctrl+s", "save", "保存"),
        Binding("escape", "cancel_edit", "取消"),
    ]

    def __init__(self, rel_path: str, full_path: Path) -> None:
        super().__init__()
        self._rel_path = rel_path
        self._full_path = full_path
        self._original_content = ""
        self._open_mtime_ns: int | None = None
        self._open_size: int | None = None
        self._read_error: str | None = None
        self._read_content()

    def _read_content(self) -> None:
        try:
            if self._full_path.exists():
                stat = self._full_path.stat()
                if stat.st_size > _MAX_CONTEXT_FILE_BYTES:
                    size_mb = stat.st_size / (1024 * 1024)
                    self._read_error = (
                        f"文件过大 ({size_mb:.1f} MB)，上限 {_MAX_CONTEXT_FILE_BYTES // 1048576} MB。"
                        f"\n请使用终端编辑器打开: {self._full_path}"
                    )
                    return
                self._open_mtime_ns = stat.st_mtime_ns
                self._open_size = stat.st_size
                self._original_content = self._full_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            self._read_error = "无法编辑: 文件包含非 UTF-8 内容"
        except OSError as e:
            self._read_error = f"读取失败: {e}"

    def compose(self) -> ComposeResult:
        from textual.widgets import TextArea

        if self._read_error:
            yield Container(
                Label(f"[bold]⚠️ {self._rel_path}[/bold]", classes="dialog-title"),
                Label(f"  {self._read_error}"),
                Horizontal(
                    Button("关闭", variant="primary", id="btn_close"),
                    classes="dialog-buttons",
                ),
                classes="context-editor-dialog",
            )
        else:
            yield Container(
                Label(
                    f"[bold]编辑: {self._rel_path}[/bold]  [dim](Ctrl+S 保存, Esc 取消)[/dim]",
                    classes="dialog-title",
                ),
                TextArea(self._original_content, id="context_file_content"),
                Horizontal(
                    Button("保存", variant="primary", id="btn_save"),
                    Button("取消", variant="default", id="btn_cancel"),
                    classes="dialog-buttons",
                ),
                classes="context-editor-dialog",
            )

    @on(Button.Pressed, "#btn_close")
    def close_readonly(self) -> None:
        self.dismiss(False)

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        from textual.widgets import TextArea

        if self._read_error:
            self.app.push_screen(
                ContextErrorDialog(self._rel_path, f"无法保存: {self._read_error}")
            )
            return

        try:
            text_area = self.query_one("#context_file_content", TextArea)
            new_content = text_area.text

            # 并发冲突检测: 比较打开时与保存前的 mtime_ns + size
            if self._open_mtime_ns is not None:
                try:
                    current_stat = self._full_path.stat()
                    if (current_stat.st_mtime_ns != self._open_mtime_ns
                            or current_stat.st_size != self._open_size):
                        self.app.push_screen(
                            ContextErrorDialog(
                                self._rel_path,
                                "文件已被外部修改或删除(并发冲突)，请关闭后重新打开编辑。"
                                "\n你的修改未保存，请复制后重试。",
                            )
                        )
                        return
                except (OSError, FileNotFoundError):
                    # 文件被外部删除——同样视为冲突
                    self.app.push_screen(
                        ContextErrorDialog(
                            self._rel_path,
                            "文件已被外部删除(并发冲突)，请关闭后重新打开编辑。"
                            "\n你的修改未保存，请复制后重试。",
                        )
                    )
                    return

            _atomic_write_text(self._full_path, new_content)
            self.dismiss(True)
        except OSError as e:
            self.app.push_screen(
                ContextErrorDialog(self._rel_path, f"保存失败: {e}")
            )

    @on(Button.Pressed, "#btn_cancel")
    def cancel_edit(self) -> None:
        if self._read_error:
            self.dismiss(False)
            return

        from textual.widgets import TextArea
        text_area = self.query_one("#context_file_content", TextArea)
        if text_area.text != self._original_content:
            self.app.push_screen(
                ContextConfirmDiscardDialog(self._rel_path, self)
            )
        else:
            self.dismiss(False)

    def action_save(self) -> None:
        self.save()

    def action_cancel_edit(self) -> None:
        self.cancel_edit()


class ContextConfirmDiscardDialog(ModalScreen[bool]):
    """确认放弃未保存的编辑。"""

    BINDINGS = [
        Binding("escape", "dismiss_continue", "继续编辑"),
    ]

    def __init__(self, rel_path: str, parent_editor: ContextFileEditor) -> None:
        super().__init__()
        self._rel_path = rel_path
        self._parent_editor = parent_editor

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]放弃修改?[/bold]", classes="dialog-title"),
            Label(f"  文件 [bold]{self._rel_path}[/bold] 有未保存的修改。\n  确定要放弃吗？"),
            Horizontal(
                Button("放弃修改", variant="error", id="btn_discard"),
                Button("继续编辑", variant="primary", id="btn_continue"),
                classes="dialog-buttons",
            ),
            classes="dialog-box",
        )

    @on(Button.Pressed, "#btn_discard")
    def on_discard(self) -> None:
        self.dismiss(True)
        self._parent_editor.dismiss(False)

    @on(Button.Pressed, "#btn_continue")
    def on_continue(self) -> None:
        self.dismiss(False)

    def action_dismiss_continue(self) -> None:
        self.dismiss(False)


class NewContextFileDialog(ModalScreen[str | None]):
    """新建上下文文件对话框。"""

    BINDINGS = [
        Binding("escape", "dismiss_cancel", "取消"),
    ]

    def __init__(self, root: Path) -> None:
        super().__init__()
        self._root = root

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]新建上下文文件[/bold]", classes="dialog-title"),
            Label("  输入相对路径 (支持子目录, 如 subdir/notes.md):"),
            FormField(
                "文件路径",
                Input(placeholder="如 subdir/notes.md", id="new_file_path"),
            ),
            Horizontal(
                Button("创建", variant="primary", id="btn_create"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="context-dialog",
        )

    @on(Button.Pressed, "#btn_create")
    def create(self) -> None:
        from common.data_layer import validate_context_path as _vcp

        rel_path = self.query_one("#new_file_path", Input).value.strip()
        full_resolved, err = _vcp(self._root, rel_path)
        if err:
            self.app.push_screen(ContextErrorDialog(rel_path, err))
            return

        if full_resolved.is_dir():
            self.app.push_screen(
                ContextErrorDialog(rel_path, "该路径已存在且为目录")
            )
            return

        if full_resolved.exists():
            self.app.push_screen(
                ContextErrorDialog(rel_path, "文件已存在,请使用编辑功能修改")
            )
            return

        # 路径验证通过；文件在编辑器保存时才创建，避免取消后留下空文件
        self.dismiss(rel_path)

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    def action_dismiss_cancel(self) -> None:
        self.dismiss(None)
