"""Textual modal dialogs used by the team manager TUI."""
from __future__ import annotations

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
