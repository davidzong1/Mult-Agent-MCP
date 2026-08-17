"""Textual modal dialogs used by the team manager TUI."""
from __future__ import annotations

import os
from pathlib import Path
import tempfile as _tempfile

from typing import Generic

from textual import on, events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container, Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, ScreenResultType
from textual.widgets import Button, Input, Label, OptionList, Select, Static
from textual.widgets.option_list import Option
# 8.2.8 中 Selection 不再从 textual.widgets 导出（会 ImportError），
# 唯一入口是 _selection_list（见 agentuser-tui-recon.md B4 实测）。
from textual.widgets._selection_list import Selection, SelectionList

# work: 见 tui/tui_worker.py —— exit_on_error=False，worker 异常不终止 App
from tui.tui_worker import work

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
from common.tmux_utils import (
    get_agent_user_config as _get_agent_user_config,
    validate_agent_user_url,
    validate_agent_user_env_value,
    AGENT_USER_NONE,
    list_agent_users as _common_list_agent_users,
    agent_user_ref_count as _agent_user_ref_count,
    agent_user_rename_sweep as _agent_user_rename_sweep,
    agent_user_delete_sweep as _agent_user_delete_sweep,
    purge_agent_user_settings as _purge_agent_user_settings,
    effort_levels_for as _effort_levels_for,
    normalize_effort as _normalize_effort,
    # 成员级 agent 用户池（数据层已实现，UI 只调用不重写）：
    #   resolve_pool_atype      —— 成员池 provider 单向决定（member.agent →
    #                              team.default_agent → "claude"）
    #   member_pool_is_activated—— 原始 agent_user_pool 非空 list（激活判定）
    #   set_member_agent_user_pool —— 写入（registry+哨兵+provider 强校验）
    #   _profile_matches_atype  —— 与数据层同源的过滤语义（typed 必须相等；
    #                              legacy 按是否携带该 atype 的 base_url 判定），
    #                              列表过滤必须用它，否则"UI 可选但保存被拒"
    resolve_pool_atype,
    member_pool_is_activated,
    set_member_agent_user_pool,
    _profile_matches_atype,
    # 团队池初始锁：agent_type(team.default_agent or "claude") 取团队默认
    # agent 的 resolved type（"claude"|"codex"|"other"）。仅用于初始锁推导，
    # 不做任何 profile 过滤语义判断。
    agent_type,
    # profile 类型单一判定来源：_resolve_profile_agent_type 薄委托数据层
    # _profile_resolved_atype（legacy 按 base_url/api_key/model 三组字段推断
    # claude/codex；两边都像或空壳 → ""）。UI 与数据层永不漂移 —— coder
    # 改数据层语义时 UI 自动跟随（2026-08-09 订正：UI 曾把数据层可接受的
    # legacy+url profile 当异类置灰）。
    _profile_resolved_atype,
    # 自动换号总开关：团队 quota_failover.enabled。此前只能手写 teams_data.json
    # —— 用户把池配全了却仍不换号，界面上还没有任何地方显示"它是关的"
    # （生产事故的首要原因）。读取一律走 quota_failover_config（defaults 合并
    # + 钳制），绝不在 UI 侧另写一套默认值。
    quota_failover_config,
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

# 成员级 effort 覆盖（三态）：显式级别 / 继承 Agent 用户默认 / 关闭。
# 等级按 provider 分离并校验（Claude: low/medium/high/xhigh/max；
# Codex: minimal/low/medium/high/xhigh），UI 按当前/所选 Agent 显示可用选项。
_EFFORT_LABELS: dict[str, str] = {
    "minimal": "极低", "low": "低", "medium": "中", "high": "高",
    "xhigh": "极高", "max": "最高",
}
_EFFORT_INHERIT = "inherit"
_EFFORT_OFF = "off"


def effort_choices_for(agent_cmd: str) -> list[tuple[str, str]]:
    """成员级 effort Select 选项：按 agent 的 provider 显示可用等级。

    未知/其他 agent 走 Claude 集合（effort_levels_for 未识别时默认 Claude）。
    """
    levels = _effort_levels_for(agent_cmd)
    return [("继承 Agent 用户默认", _EFFORT_INHERIT), ("关闭", _EFFORT_OFF)] + [
        (_EFFORT_LABELS[lvl], lvl) for lvl in levels
    ]


def _effort_value_for(agent_cmd: str, value: object, default: str = _EFFORT_INHERIT) -> str:
    """把 effort 值按 agent 的 provider 归一化；非法/不在此集合时回退 default。"""
    normalized = _normalize_effort(value, agent_cmd)
    return normalized or default


def agent_user_effort_choices_for(agent_type: str = "") -> list[tuple[str, str]]:
    """Agent 用户 profile 的默认 effort Select 选项：按 provider 显示可用等级。

    仅"不设置默认" + 具体级别（成员 effort=inherit 时继承此默认）。
    未知/其他 agent 走 Claude 集合（与 effort_levels_for 语义一致）。
    """
    levels = _effort_levels_for(agent_type)
    return [("不设置默认", "")] + [(_EFFORT_LABELS[lvl], lvl) for lvl in levels]


def agent_user_effort_value_for(agent_type: str, value: object) -> str:
    """Agent 用户默认 effort 按 provider 归一化；非法/不在该集合时回退 ""。

    Select 用 allow_blank=False，构造期 value 必须命中选项，否则 Textual 抛
    InvalidSelectValueError。历史数据可能含跨 provider 残留（如 claude profile
    存了 minimal）或手改垃圾值，这里统一归一到当前 provider 的合法集合。
    """
    normalized = _normalize_effort(value, agent_type)
    legal = {v for _, v in agent_user_effort_choices_for(agent_type)}
    return normalized if normalized in legal else ""


def _api_key_display(s: str) -> str:
    """掩码显示：已配置/未配置，绝不显示明文。"""
    return "已配置" if s and s.strip() else "未配置"


def _resolve_profile_agent_type(cfg: dict) -> str:
    """解析 profile 的 provider 类型（'claude'|'codex'|''）。

    单一判定来源：薄委托数据层 _profile_resolved_atype —— legacy profile
    （无 agent_type）按 base_url/api_key/model 三组字段兜底推断，不在 UI
    本地重写判定。修复"UI 比数据层更严"：只填 claude 字段的 legacy profile
    数据层推断为 claude 可用，UI 曾算 '' 被默认锁当异类置灰（用户配好了
    却选不了）。coder 改数据层推断逻辑后 UI 自动跟随，不再漂移。
    """
    return _profile_resolved_atype(cfg)


def _agent_type_badge(agent_type: str) -> str:
    """返回 provider 标记文本。"""
    if agent_type == "claude":
        return "🤖Claude"
    if agent_type == "codex":
        return "🔵Codex"
    return "⚪旧版"


def _build_agent_user_options(team_name: str, for_agent_type: str = "", *, include_no_takeover: bool = True) -> list[tuple[str, str]]:
    """构建 agent 用户选择列表 [(label, value), ...]。

    始终包含"系统默认"(空值)。当 for_agent_type 非空时仅列出匹配 provider 或旧版的 profile。
    profile 仅显示用户标识和 Provider（不显示接管状态等详情）。

    Args:
        team_name: 团队名称（用于解析团队默认 profile 的 ⭐ 标记与旧数据回退）
        for_agent_type: 过滤条件，非空时仅列出匹配 provider 或旧版的 profile
        include_no_takeover: 是否包含"不接管"哨兵选项。
            True（默认）用于成员 AddMember/EditMember 选择列表，以及
            TeamDefaultAgentUserDialog 团队默认选择（保证三态语义一致）；
            False 仅用于需要纯净 profile 列表的调用方。
    """
    profiles = _agent_user_profiles(team_name)
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    default_key = team.get("default_agent_user", "")
    default_label = f"系统默认 ({default_key})" if default_key else "系统默认"
    options: list[tuple[str, str]] = [
        (default_label, ""),
    ]
    if include_no_takeover:
        options.append(("不接管", AGENT_USER_NONE))
    for key, cfg in profiles.items():
        at = _resolve_profile_agent_type(cfg)
        badge = _agent_type_badge(at)
        # 过滤：仅当 for_agent_type 匹配或无 type(旧版)时显示
        if for_agent_type and at and at != for_agent_type:
            continue
        prefix = "⭐ " if key == default_key else ""
        options.append((f"{prefix}{badge} {key}", key))
    return options


def _get_profile_agent_type(team_name: str, profile_key: str) -> str:
    """获取指定 profile 的 agent_type，未知返回空串。"""
    profiles = _agent_user_profiles(team_name)
    cfg = profiles.get(profile_key, {})
    return _resolve_profile_agent_type(cfg)


def _sync_agent_user_rename(team: dict, old_key: str, new_key: str) -> None:
    """同步 team 内 default_agent_user 和 member.agent_user 引用从 old_key 到 new_key。

    纯 helper，不涉及 IO；由 edit_user 在 key 变更分支中调用。
    """
    if team.get("default_agent_user") == old_key:
        team["default_agent_user"] = new_key
    for member_info in team.get("members", {}).values():
        if member_info.get("agent_user") == old_key:
            member_info["agent_user"] = new_key


def _normalize_select_value(value, default: str = "") -> str:
    """把 Textual Select 的原始值归一化成字符串；空选择返回 default。

    Select 默认 allow_blank=True，下拉里会多出一行占位项（显示为 "Select"）。
    用户选中它后 ``select.value`` 是 ``Select.NULL``（NoSelection 哨兵），它
    **truthy、不是 str、且不可 JSON 序列化**——三条性质凑在一起特别阴险：

      - truthy    → ``if value:`` / ``value or "default"`` 这类兜底全部失效
      - 非 str    → 拿去做 dict key、渲染富文本会 AttributeError
      - 不可序列化 → 一旦随 dismiss() 流进 teams_data.json，save_data() 抛
                    TypeError，而调用方是 @work worker，异常直接把整个 TUI 打崩

    所以**所有** Select 读取都必须过这里，不能再裸读 ``.value``。
    """
    if value is None or value is Select.NULL:
        return default
    return str(value)


def _select_value(select: "Select", default: str = "") -> str:
    """读取 Select 当前值并归一化为字符串；空选择返回 default。"""
    return _normalize_select_value(select.value, default)


def _ensure_option(options: list[tuple[str, str]], value: str) -> list[tuple[str, str]]:
    """保证 value 命中某个选项；不在列表里就追加一条"自定义"项。

    配合 allow_blank=False 使用：Textual 在构造期要求初始 value 必须命中
    选项，否则抛 InvalidSelectValueError，对话框直接打不开。而成员的 agent
    可能是自定义命令、agent_user 可能指向已删除的 profile —— 这里把原值补进
    选项而不是静默改写用户数据。
    """
    if not value or any(v == value for _, v in options):
        return options
    return [*options, (f"{value} · 自定义", value)]


def _scrub_no_selection(payload):
    """递归把 dismiss 载荷里的 NoSelection 哨兵替换成空串。

    兜底网：即使将来有人新增 Select 却忘了走 _select_value，也不会把不可
    JSON 序列化的哨兵写进 teams_data.json 把 TUI 打崩。
    """
    if payload is Select.NULL:
        return ""
    if isinstance(payload, dict):
        return {k: _scrub_no_selection(v) for k, v in payload.items()}
    if isinstance(payload, list):
        return [_scrub_no_selection(v) for v in payload]
    if isinstance(payload, tuple):
        return tuple(_scrub_no_selection(v) for v in payload)
    return payload


class SelectSafeDismissMixin:
    """给含 Select 的表单对话框统一做 dismiss 载荷清洗（见 _scrub_no_selection）。

    必须排在 ModalScreen 之前继承，才能拦到 dismiss。
    """

    def dismiss(self, *args, **kwargs):
        if args:
            args = (_scrub_no_selection(args[0]),) + args[1:]
        if "result" in kwargs:
            kwargs["result"] = _scrub_no_selection(kwargs["result"])
        return super().dismiss(*args, **kwargs)


def _selected_profile_key(select: "Select") -> str:
    """从管理 Select 读取当前选中的 profile key，空选择归一化为 ''。

    语义（详见 _normalize_select_value 对 NoSelection 的说明）：
      - Select.NULL (无选择)             → ''
      - '' (系统默认)                     → ''
      - profile key (如 'alice')          → 'alice'
      - AGENT_USER_NONE (显式不接管)      → '__none__'
    """
    return _select_value(select, "")


def _agent_user_profiles(team_name: str = "") -> dict:
    """读取 agent 用户 registry（委托 common 全局-aware 读 API）。

    统一走 common.tmux_utils.list_agent_users：全局 data['agent_users']
    优先，并与未迁移团队的 team['agent_users'] 合并（键冲突团队旧数据优先）。
    team_name 为空时用于全局管理视图，返回 post-migration 全局 registry。
    """
    return _common_list_agent_users(team_name)


def _global_profile_options() -> list[tuple[str, str]]:
    """全局 manage 列表选项：仅 profiles（Provider badge + 接管状态），无系统默认/不接管。

    全局管理不再负责某团队设默认；设为团队默认在 TeamDetailScreen 完成。
    每行展示 key、provider、接管状态（takeover_enabled）。
    """
    profiles = _agent_user_profiles()
    return [
        (
            f"{_agent_type_badge(_resolve_profile_agent_type(cfg))} {key}"
            f"  ·  {'接管' if cfg.get('takeover_enabled') else '未接管'}",
            key,
        )
        for key, cfg in profiles.items()
    ]


def _highlighted_profile_key(option_list: "OptionList") -> str:
    """从全局管理 OptionList 读取当前高亮的 profile key，无高亮返回 ''。

    与 _selected_profile_key（Select）对应：OptionList.highlighted 为索引，
    highlighted_option 可能为 None（无行可选），这里统一归一化为空串。
    """
    opt = option_list.highlighted_option
    if opt is None or not opt.id:
        return ""
    return opt.id


def _claude_mcp_configured(team_name: str) -> bool:
    """团队工作目录下是否已写入 Claude MCP 配置。

    注意：这个 def 头曾被一份重复粘贴的 _highlighted_profile_key 覆盖掉，
    导致下方 MCP 状态面板调用它时抛 NameError。
    """
    return _common_claude_mcp_configured(team_workspace_dir(team_name))


def configure_claude_mcp(team_name: str) -> tuple[bool, str]:
    try:
        return True, _common_configure_claude_mcp(team_name, team_workspace_dir(team_name))
    except Exception as e:
        return False, f"❌ Claude MCP 配置失败: {e}"

class ScrollableModalScreen(ModalScreen[ScreenResultType], Generic[ScreenResultType]):
    """统一可纵向滚动的 ModalScreen 基类（所有子页弹窗继承）。

    弹窗根容器在超出视口高度时可纵向滚动，底部按钮/交互项在 1080P 及更低
    终端高度下可滚动到达（创建团队、添加成员等不再被遮挡）。

    高度约束是**全局规则**，不逐页写 max-height 特例，统一由
    TeamManagerApp.CSS 的 .dialog-form/.dialog-box/.context-dialog/
    .context-viewer/.context-editor-dialog 规则承担（单点维护）:
      - 内容低于视口 → max-height 不生效，height:auto 保持原宽屏布局（无滚动条）;
      - 内容超出视口 → 容器高度封顶视口，overflow-y:auto 内部滚动，底部可达。
    focus/Tab 由 Textual 的 scroll_to_region 自动把焦点控件滚入视口，无需手动。
    新弹窗一律继承本基类，即可获得统一滚动能力。
    """


class MessageBox(ScrollableModalScreen[None]):
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


class ConfirmBox(ScrollableModalScreen[bool]):
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

class McpStatusDialog(ScrollableModalScreen[None]):
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

    def action_close_dialog(self) -> None:
        """Escape 键退出。"""
        self.dismiss(None)

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

class AgentMcpConfigDialog(ScrollableModalScreen[None]):
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

    def action_close_dialog(self) -> None:
        """Escape 键退出。"""
        self.dismiss(None)

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

class CreateTeamDialog(SelectSafeDismissMixin, ScrollableModalScreen[dict | None]):
    def compose(self) -> ComposeResult:
        agent_options = [(label, value) for label, value in AGENT_CHOICES]
        proxy_enabled_options = [(label, value) for label, value in PROXY_ENABLED_CHOICES]
        yield Container(
            Label("[bold]创建新团队[/bold]", classes="dialog-title"),
            VerticalScroll(
                FormField("团队名称", Input(placeholder="如 dev_team", id="name")),
                FormField("描述", Input(placeholder="选填", id="desc")),
                FormField("默认 Agent", Select(agent_options, id="agent", value="claude", allow_blank=False)),
                FormField("代理", Select(proxy_enabled_options, id="proxy_enabled", value="disabled", allow_blank=False)),
                FormField("代理主机", Input(placeholder="127.0.0.1", id="proxy_host")),
                FormField("代理端口", Input(placeholder="7890", id="proxy_port")),
                classes="dialog-fields-scroll",
            ),
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
        agent = _select_value(self.query_one("#agent", Select), "claude")
        proxy_enabled = _select_value(self.query_one("#proxy_enabled", Select), "disabled") == "enabled"
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


class AddMemberDialog(SelectSafeDismissMixin, ScrollableModalScreen[dict | None]):
    def __init__(self, default_agent: str = "claude", team_name: str = "") -> None:
        super().__init__()
        self._default_agent = default_agent or "claude"
        self._team_name = team_name

    def compose(self) -> ComposeResult:
        agent_options = _ensure_option(
            [(label, value) for label, value in AGENT_CHOICES], self._default_agent
        )
        proxy_options = [(label, value) for label, value in PROXY_MODE_CHOICES]
        agent_user_options = _build_agent_user_options(self._team_name) if self._team_name else [("系统默认", "")]
        yield Container(
            Label("[bold]添加成员[/bold]", classes="dialog-title"),
            VerticalScroll(
                FormField("成员名称", Input(placeholder="如 alice", id="name")),
                FormField("角色", Input(placeholder="如 coder / tester / reviewer", id="role")),
                FormField("Agent", Select(agent_options, id="agent", value=self._default_agent, allow_blank=False)),
                FormField("代理模式", Select(proxy_options, id="proxy_mode", value="inherit", allow_blank=False)),
                FormField("Agent用户", Select(agent_user_options, id="agent_user", value="", allow_blank=False)),
                FormField("推理强度", Select(effort_choices_for(self._default_agent), id="effort", value="inherit", allow_blank=False)),
                classes="dialog-fields-scroll",
            ),
            Horizontal(
                Button("添加", variant="primary", id="btn_add"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Select.Changed, "#agent_user")
    def on_agent_user_changed(self, event: Select.Changed) -> None:
        """选择 typed profile 时同步 agent 并禁用 Agent Select；清空/不接管/旧版恢复。"""
        profile_key = _normalize_select_value(event.value)
        if not profile_key or not self._team_name:
            # 恢复 Agent Select
            self.query_one("#agent", Select).disabled = False
            return
        if profile_key == AGENT_USER_NONE:
            # 显式不接管：恢复 Agent 选择，不同步 profile
            self.query_one("#agent", Select).disabled = False
            return
        at = _get_profile_agent_type(self._team_name, profile_key)
        if at in ("claude", "codex"):
            self.query_one("#agent", Select).value = at
            self.query_one("#agent", Select).disabled = True
            self._refresh_effort_options(at)
        else:
            self.query_one("#agent", Select).disabled = False

    @on(Select.Changed, "#agent")
    def on_agent_changed(self, event: Select.Changed) -> None:
        """切换 Agent 时更新推理强度选项（Claude/Codex 等级集不同）。"""
        agent = _normalize_select_value(event.value)
        self._refresh_effort_options(agent)

    def _refresh_effort_options(self, agent: str) -> None:
        """按 agent 的 provider 刷新推理强度 Select 选项；当前值非法则回落 inherit。"""
        try:
            effort = self.query_one("#effort", Select)
        except Exception:
            return
        current = _select_value(effort, _EFFORT_INHERIT)
        choices = effort_choices_for(agent)
        if not any(v == current for _, v in choices):
            current = _EFFORT_INHERIT
        effort.set_options(choices)
        effort.value = current

    @on(Button.Pressed, "#btn_add")
    def add(self) -> None:
        name = self.query_one("#name", Input).value.strip()
        if not name:
            self.app.push_screen(MessageBox("成员名称不能为空"))
            return
        role = self.query_one("#role", Input).value.strip()

        # 按 typed profile 强制同步 agent
        agent_user = _select_value(self.query_one("#agent_user", Select), "")
        agent = _select_value(self.query_one("#agent", Select), self._default_agent)
        if agent_user and self._team_name:
            at = _get_profile_agent_type(self._team_name, agent_user)
            if at in ("claude", "codex"):
                agent = at  # typed profile 强制覆盖 agent

        proxy_mode = _select_value(self.query_one("#proxy_mode", Select), "inherit")
        effort = _select_value(self.query_one("#effort", Select), "inherit")
        self.dismiss({
            "name": name, "role": role, "agent": agent, "proxy_mode": proxy_mode,
            "agent_user": agent_user, "effort": effort,
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class EditMemberDialog(SelectSafeDismissMixin, ScrollableModalScreen[dict | None]):
    # 成员级 Agent 用户池入口（"5"）：与团队池 TeamDetailScreen "4" 同级编号，
    # App 级 "3" 是 mcp_restart —— 5 未占用。焦点在 Input 上时按键先被输入框
    # 消费，按钮路径不受影响（#btn_member_pool 始终可点）。
    BINDINGS = [
        Binding("5", "member_user_pool", "成员用户池"),
    ]

    def __init__(self, member_name: str, current_role: str, current_agent: str, current_proxy_mode: str = "inherit", current_agent_user: str = "", current_effort: str = "inherit", team_name: str = "") -> None:
        super().__init__()
        self._member_name = member_name
        self._role = current_role
        # 空 agent 会让 allow_blank=False 的 Select 找不到匹配项而构造失败，
        # 且 "" 本来就不是合法 agent —— 与 AddMemberDialog 保持同一默认。
        self._agent = current_agent or "claude"
        self._proxy_mode = current_proxy_mode or "inherit"
        self._agent_user = current_agent_user
        # 回填按当前 agent 的 provider 归一化：非法等级（如 codex 成员存了 max）回落 inherit
        self._effort = _effort_value_for(self._agent, current_effort)
        self._team_name = team_name

    def compose(self) -> ComposeResult:
        # _ensure_option: 成员的 agent 可能是自定义命令、agent_user 可能指向
        # 已删除的 profile，allow_blank=False 下必须让原值命中选项，否则
        # Textual 构造期抛 InvalidSelectValueError（弹窗直接打不开）。
        agent_options = _ensure_option(
            [(label, value) for label, value in AGENT_CHOICES], self._agent
        )
        proxy_options = [(label, value) for label, value in PROXY_MODE_CHOICES]
        agent_user_options = _build_agent_user_options(self._team_name) if self._team_name else [("系统默认", "")]
        agent_user_options = _ensure_option(agent_user_options, self._agent_user)
        yield Container(
            Label(f"[bold]编辑 {self._member_name}[/bold]", classes="dialog-title"),
            VerticalScroll(
                FormField("角色", Input(value=self._role, placeholder="角色", id="role")),
                FormField("Agent", Select(agent_options, id="agent", value=self._agent, allow_blank=False)),
                FormField("代理模式", Select(proxy_options, id="proxy_mode", value=self._proxy_mode, allow_blank=False)),
                FormField("Agent用户", Select(agent_user_options, id="agent_user", value=self._agent_user, allow_blank=False)),
                FormField("推理强度", Select(effort_choices_for(self._agent), id="effort", value=self._effort, allow_blank=False)),
                classes="dialog-fields-scroll",
            ),
            Horizontal(
                Button("成员用户池", variant="default", id="btn_member_pool",
                       disabled=not bool(self._team_name)),
                Button("保存", variant="primary", id="btn_save"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Select.Changed, "#agent_user")
    def on_agent_user_changed(self, event: Select.Changed) -> None:
        """选择 typed profile 时同步 agent 并禁用 Agent Select；清空/不接管/旧版恢复。"""
        profile_key = _normalize_select_value(event.value)
        if not profile_key or not self._team_name:
            self.query_one("#agent", Select).disabled = False
            return
        if profile_key == AGENT_USER_NONE:
            # 显式不接管：恢复 Agent 选择，不同步 profile
            self.query_one("#agent", Select).disabled = False
            return
        at = _get_profile_agent_type(self._team_name, profile_key)
        if at in ("claude", "codex"):
            self.query_one("#agent", Select).value = at
            self.query_one("#agent", Select).disabled = True
            self._refresh_effort_options(at)
        else:
            self.query_one("#agent", Select).disabled = False

    @on(Select.Changed, "#agent")
    def on_agent_changed(self, event: Select.Changed) -> None:
        """切换 Agent 时更新推理强度选项（Claude/Codex 等级集不同）。"""
        agent = _normalize_select_value(event.value)
        self._refresh_effort_options(agent)

    def _refresh_effort_options(self, agent: str) -> None:
        """按 agent 的 provider 刷新推理强度 Select 选项；当前值非法则回落 inherit。"""
        try:
            effort = self.query_one("#effort", Select)
        except Exception:
            return
        current = _select_value(effort, self._effort)
        choices = effort_choices_for(agent)
        if not any(v == current for _, v in choices):
            current = _EFFORT_INHERIT
        effort.set_options(choices)
        effort.value = current

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        # 所有 Select 一律经 _select_value 归一化：空选择回落到成员原值，
        # 绝不让 NoSelection 哨兵进入 dismiss 载荷 → teams_data.json。
        agent_user = _select_value(self.query_one("#agent_user", Select), self._agent_user)
        agent = _select_value(self.query_one("#agent", Select), self._agent)
        if agent_user and self._team_name:
            at = _get_profile_agent_type(self._team_name, agent_user)
            if at in ("claude", "codex"):
                agent = at  # typed profile 强制覆盖 agent

        self.dismiss({
            "role": self.query_one("#role", Input).value.strip(),
            "agent": agent,
            "proxy_mode": _select_value(self.query_one("#proxy_mode", Select), self._proxy_mode),
            "agent_user": agent_user,
            "effort": _select_value(self.query_one("#effort", Select), self._effort),
        })

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn_member_pool")
    @work
    async def open_member_pool_button(self) -> None:
        """按钮入口：打开成员级 Agent 用户池弹窗（焦点在 Input 时也可用）。"""
        await self.app.push_screen_wait(
            MemberAgentUserPoolDialog(self._team_name, self._member_name))

    @work
    async def action_member_user_pool(self) -> None:
        """成员级 Agent 用户池入口（BINDINGS "5"，与按钮共用同一弹窗）。

        弹窗不 dismiss 本表单：关闭池弹窗后仍可继续编辑成员并保存。
        """
        await self.app.push_screen_wait(
            MemberAgentUserPoolDialog(self._team_name, self._member_name))


class TeamProxyDialog(SelectSafeDismissMixin, ScrollableModalScreen[dict | None]):
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
            VerticalScroll(
                FormField("代理", Select(proxy_action_options, id="proxy_action", value="enabled", allow_blank=False)),
                FormField("代理主机", Input(value=self._proxy_host, placeholder="127.0.0.1", id="proxy_host")),
                FormField("代理端口", Input(value=self._proxy_port, placeholder="7890", id="proxy_port")),
                classes="dialog-fields-scroll",
            ),
            Horizontal(
                Button("保存", variant="primary", id="btn_save"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        proxy_action = _select_value(self.query_one("#proxy_action", Select), "enabled")
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

class ContextErrorDialog(ScrollableModalScreen[None]):
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


class ContextConfirmDeleteDialog(ScrollableModalScreen[bool]):
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


class ContextConfirmDeleteAllDialog(ScrollableModalScreen[bool]):
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


class ContextFileViewer(ScrollableModalScreen[None]):
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


class ContextFileEditor(ScrollableModalScreen[bool]):
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


class ContextConfirmDiscardDialog(ScrollableModalScreen[bool]):
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


class NewContextFileDialog(ScrollableModalScreen[str | None]):
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


# ============================================================
# Agent 用户管理对话框
# ============================================================

class AgentUserEditDialog(SelectSafeDismissMixin, ModalScreen[dict | None]):
    """新增或编辑 agent 用户 profile。

    新增时选择 Claude/Codex 并填写对应 provider 三字段；编辑时 agent_type 不可变。
    API Key 明文显示（个人使用，便于核对）。保存时校验 URL 和 Key/Model 安全。

    返回 dict: {
        "key": str, "agent_type": "claude"|"codex",
        "takeover_enabled": bool,
        "anthropic_api_key": str, "anthropic_base_url": str, "anthropic_model": str,
        "openai_api_key": str, "openai_base_url": str, "codex_model": str,
    }
    """

    PROVIDER_OPTIONS = [
        ("Claude", "claude"),
        ("Codex", "codex"),
    ]

    # Agent 用户编辑弹窗布局修正（作用域限定本弹窗，不影响其他 dialog-buttons 弹窗）:
    #   - 保存/取消右侧对齐（通用 UX 约定：底部操作按钮靠右）;
    #   - 按钮行 height:auto，避免 Horizontal 默认 1fr 在内容较高时把按钮
    #     挤到 1 行高导致溢出表单（其余弹窗内容矮、剩余空间大，无此问题）;
    #   - 表单允许纵向滚动：低高度视口下保存/取消按钮仍可达;
    #   - 表单横向自适应：窄终端不裁剪输入框与按钮;
    #   - 输入/下拉宽度自适应（1fr 收缩），宽屏保持原有 max-width:35 观感。
    CSS = """
    .dialog-form {
        max-width: 100%;
        height: 100%;
        max-height: 100%;
    }
    .dialog-title {
        margin-bottom: 0;
    }
    .dialog-buttons {
        align: right middle;
        height: auto;
    }
    FormField Input,
    FormField Select {
        width: 1fr;
        max-width: 35;
    }
    #edit_fields_scroll {
        height: 1fr;
        min-height: 1;
        overflow-y: auto;
    }
    #agent_user_edit_actions {
        layout: grid;
        grid-columns: auto;
        grid-rows: auto;
        height: auto;
    }
    /* 可辨识底色：default 变体按钮背景区别于 .dialog-form 的 $surface，
       否则取消按钮与表单同色在视觉上"消失"；作用域限定本弹窗，
       保留 primary（保存）语义。default 变体无 `-default` class，
       用基础规则 + 显式覆盖 primary 回变体色。 */
    #agent_user_edit_actions Button {
        background: $panel;
    }
    #agent_user_edit_actions Button:hover {
        background: $panel-lighten-1;
    }
    #agent_user_edit_actions Button.-primary {
        background: $primary;
    }
    #agent_user_edit_actions Button.-primary:hover {
        background: $primary-darken-2;
    }
    /* ansi 终端回退：尊重 Textual ansi 变体（ansi_default），避免真彩色黑化 */
    #agent_user_edit_actions Button:ansi,
    #agent_user_edit_actions Button:ansi:hover,
    #agent_user_edit_actions Button.-primary:ansi {
        background: ansi_default;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
    ]

    def __init__(
        self,
        user_key: str = "",
        agent_type: str = "",
        takeover_enabled: bool = False,
        anthropic_api_key: str = "",
        anthropic_base_url: str = "",
        anthropic_model: str = "",
        openai_api_key: str = "",
        openai_base_url: str = "",
        codex_model: str = "",
        effort: str = "",
    ) -> None:
        super().__init__()
        self._user_key = user_key
        self._agent_type = agent_type
        self._takeover_enabled = takeover_enabled
        self._anthropic_api_key = anthropic_api_key
        self._anthropic_base_url = anthropic_base_url
        self._anthropic_model = anthropic_model
        self._openai_api_key = openai_api_key
        self._openai_base_url = openai_base_url
        self._codex_model = codex_model
        # 初始即按 provider 归一化：历史数据可能含跨 provider 残留（如 claude
        # profile 存了 minimal / codex 存了 max），非法值回退 ""（不设置默认），
        # 保证 allow_blank=False 的 effort Select 构造期必有匹配选项。
        self._effort = agent_user_effort_value_for(self._agent_type, effort)
        self._is_new = not user_key

    @property
    def _provider_editable(self) -> bool:
        """Provider is editable when creating new, or editing a legacy profile."""
        return self._is_new or not self._agent_type

    def compose(self) -> ComposeResult:
        takeover_options = [("关闭", "disabled"), ("开启", "enabled")]
        if self._is_new:
            title = "新建 Agent 用户"
        elif not self._agent_type:
            title = f"编辑旧版 Agent 用户: {self._user_key} (需选择 Provider)"
        else:
            title = f"编辑 Agent 用户: {self._user_key}"

        # Provider 选择：新建 或 旧版(无agent_type) → Select；已有 typed → Static 锁定
        provider_value = self._agent_type or Select.NULL
        if self._provider_editable:
            provider_field = FormField(
                "Provider",
                Select(
                    [(label, val) for label, val in self.PROVIDER_OPTIONS],
                    id="agent_type",
                    value=provider_value,
                    # 只要还没定下 provider（新建 或 旧版 profile），初始值就是
                    # Select.NULL，此时必须 allow_blank=True，否则 Textual 构造期
                    # 抛 InvalidSelectValueError，"编辑旧版 Agent 用户"直接打不开。
                    allow_blank=not self._agent_type,
                ),
            )
        else:
            at_label = _agent_type_badge(self._agent_type)
            provider_field = FormField(
                "Provider",
                Static(f"{at_label} (不可变更，修改类型请新建 profile)", id="agent_type_static"),
            )

        yield Container(
            Label(f"[bold]{title}[/bold]", classes="dialog-title"),
            VerticalScroll(
                FormField(
                    "用户标识",
                    Input(value=self._user_key, placeholder="如 my-api-key", id="key",
                          disabled=not self._is_new),
                ),
                provider_field,
                # Claude 字段组 — 通过 display 切换
                Container(
                    Static("🤖 Claude 配置", id="group_claude_label"),
                    FormField("  API Key", Input(value=self._anthropic_api_key, placeholder="sk-ant-...", id="ant_key")),
                    FormField("  BASE_URL", Input(value=self._anthropic_base_url, placeholder="https://api.anthropic.com", id="ant_url")),
                    FormField("  Model", Input(value=self._anthropic_model, placeholder="claude-sonnet-5-20251001", id="ant_model")),
                    id="claude_fields",
                ),
                # Codex 字段组 — 通过 display 切换
                Container(
                    Static("🔵 Codex 配置", id="group_codex_label"),
                    FormField("  API Key", Input(value=self._openai_api_key, placeholder="sk-...", id="oai_key")),
                    FormField("  BASE_URL", Input(value=self._openai_base_url, placeholder="https://api.openai.com", id="oai_url")),
                    FormField("  Model", Input(value=self._codex_model, placeholder="gpt-4o", id="oai_model")),
                    id="codex_fields",
                ),
                FormField("接管开关", Select(takeover_options, id="takeover", value="enabled" if self._takeover_enabled else "disabled", allow_blank=False)),
                FormField("默认推理强度", Select(agent_user_effort_choices_for(self._agent_type), id="effort", value=agent_user_effort_value_for(self._agent_type, self._effort), allow_blank=False)),
                id="edit_fields_scroll",
            ),
            Grid(
                Button("保存", variant="primary", id="btn_save"),
                Button("取消", variant="default", id="btn_cancel"),
                id="agent_user_edit_actions",
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    def on_mount(self) -> None:
        """设置初始字段可见性，并按可用宽度重排保存/取消按钮。"""
        self._update_field_visibility()
        self.call_after_refresh(self._reflow_action_buttons)

    def on_resize(self, _event: events.Resize) -> None:
        """终端宽度变化时重排保存/取消，避免窄窗口右侧按钮横向裁剪。"""
        self.call_after_refresh(self._reflow_action_buttons)

    def _reflow_action_buttons(self) -> None:
        """窄宽度下把保存/取消折成 1 列（与 manage 弹窗同一换行策略）。

        默认 2 列并排；当可用宽度放不下两个按钮时降为 1 列竖排，
        保证窄窗口下取消按钮不被横向裁剪出视口。
        """
        try:
            grid = self.query_one("#agent_user_edit_actions", Grid)
        except Exception:
            return
        buttons = [
            self.query_one("#btn_save", Button),
            self.query_one("#btn_cancel", Button),
        ]
        available = grid.content_size.width
        if available <= 0:
            available = self.size.width
        widths = [
            max(b.region.width, b.get_content_width(grid.content_size, self.size)) or 1
            for b in buttons
        ]
        grid.styles.grid_size_columns = 1 if sum(widths) > available else 2

    def _update_field_visibility(self) -> None:
        """根据当前 agent_type 切换 Claude/Codex 字段组的 display 属性。"""
        at = self._agent_type.lower() if self._agent_type else ""
        show_claude = at == "claude"
        show_codex = at == "codex"
        try:
            self.query_one("#claude_fields", Container).display = show_claude
        except Exception:
            pass
        try:
            self.query_one("#codex_fields", Container).display = show_codex
        except Exception:
            pass

    @on(Select.Changed, "#agent_type")
    def on_provider_changed(self, event: Select.Changed) -> None:
        """切换 provider 时更新字段可见性 + 默认推理强度选项（按 provider 分离）。"""
        self._agent_type = _normalize_select_value(event.value)
        self._update_field_visibility()
        try:
            sel = self.query_one("#effort", Select)
        except Exception:
            return
        choices = agent_user_effort_choices_for(self._agent_type)
        legal = {v for _, v in choices}
        current = _normalize_select_value(sel.value)
        sel.set_options(choices)
        # set_options 重置 selection；保留仍在合法集合内的当前值，否则回退"不设置默认"
        if current not in legal:
            current = ""
        if current in legal:
            sel.value = current

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        key = self.query_one("#key", Input).value.strip()
        if not key:
            self.app.push_screen(MessageBox("用户标识不能为空"))
            return
        if key == AGENT_USER_NONE:
            self.app.push_screen(MessageBox(f"'{AGENT_USER_NONE}' 是系统保留字，不能用作 profile 标识"))
            return

        # 获取 provider 类型：新建+旧版从 Select 读取，typed 编辑从实例属性
        if self._provider_editable:
            try:
                at = _select_value(self.query_one("#agent_type", Select), "")
            except Exception:
                at = self._agent_type
        else:
            at = self._agent_type

        if at not in ("claude", "codex"):
            self.app.push_screen(MessageBox("请选择 Provider (Claude / Codex)"))
            return

        # 仅读取 + 校验选中 provider 的字段；另一 provider 字段置空
        if at == "claude":
            ant_key = self.query_one("#ant_key", Input).value.strip()
            ant_url = self.query_one("#ant_url", Input).value.strip()
            ant_model = self.query_one("#ant_model", Input).value.strip()
            oai_key = oai_url = oai_model = ""

            if ant_url:
                err = validate_agent_user_url(ant_url)
                if err:
                    self.app.push_screen(MessageBox(f"ANTHROPIC_BASE_URL 无效: {err}"))
                    return
            for kv, name in [(ant_key, "ANTHROPIC_API_KEY"), (ant_model, "ANTHROPIC_MODEL")]:
                err = validate_agent_user_env_value(kv, name)
                if err:
                    self.app.push_screen(MessageBox(err))
                    return
        else:  # codex
            ant_key = ant_url = ant_model = ""
            oai_key = self.query_one("#oai_key", Input).value.strip()
            oai_url = self.query_one("#oai_url", Input).value.strip()
            oai_model = self.query_one("#oai_model", Input).value.strip()

            if oai_url:
                err = validate_agent_user_url(oai_url)
                if err:
                    self.app.push_screen(MessageBox(f"OPENAI_BASE_URL 无效: {err}"))
                    return
            for kv, name in [(oai_key, "OPENAI_API_KEY"), (oai_model, "CODEX_MODEL")]:
                err = validate_agent_user_env_value(kv, name)
                if err:
                    self.app.push_screen(MessageBox(err))
                    return

        takeover = _select_value(self.query_one("#takeover", Select), "disabled") == "enabled"
        effort = _select_value(self.query_one("#effort", Select), "")
        # 按 provider 归一化：跨 provider 残留的非法 effort（如 codex 的 max）
        # 在保存前清除，绝不写入与 profile 类型不匹配的 effort。
        effort = agent_user_effort_value_for(at, effort)
        self.dismiss({
            "key": key,
            "agent_type": at,
            "takeover_enabled": takeover,
            "effort": effort,
            "anthropic_api_key": ant_key,
            "anthropic_base_url": ant_url,
            "anthropic_model": ant_model,
            "openai_api_key": oai_key,
            "openai_base_url": oai_url,
            "codex_model": oai_model,
        })

    def action_cancel(self) -> None:
        """Escape 键退出 Agent 用户编辑（与取消按钮一致）。"""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class AgentUserRenameDialog(ScrollableModalScreen[str | None]):
    """重命名全局 Agent 用户 profile 标识（跨团队引用将同步 sweep）。"""

    BINDINGS = [
        Binding("escape", "cancel", "取消"),
    ]

    def __init__(self, old_key: str, taken_keys: set) -> None:
        super().__init__()
        self._old_key = old_key
        self._taken = taken_keys

    def compose(self) -> ComposeResult:
        yield Container(
            Label("[bold]重命名 Agent 用户[/bold]", classes="dialog-title"),
            Label(f"将 '{self._old_key}' 重命名为："),
            FormField("新标识", Input(placeholder=self._old_key, id="new_key")),
            Label("跨团队引用将被同步更新", id="rename_hint"),
            Horizontal(
                Button("保存", variant="primary", id="btn_save"),
                Button("取消", variant="default", id="btn_cancel"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    @on(Button.Pressed, "#btn_save")
    def save(self) -> None:
        new_key = self.query_one("#new_key", Input).value.strip()
        if not new_key:
            self.app.push_screen(MessageBox("新标识不能为空"))
            return
        if new_key == AGENT_USER_NONE:
            self.app.push_screen(
                MessageBox(f"'{AGENT_USER_NONE}' 是系统保留字，不能用作 profile 标识"))
            return
        if new_key in self._taken:
            self.app.push_screen(MessageBox(f"标识 '{new_key}' 已存在，请改用其他名称"))
            return
        if new_key == self._old_key:
            self.app.push_screen(MessageBox("新标识与当前一致，未变化"))
            return
        self.dismiss(new_key)

    def action_cancel(self) -> None:
        self.dismiss(None)

    @on(Button.Pressed, "#btn_cancel")
    def cancel(self) -> None:
        self.dismiss(None)


class AgentUserManageDialog(ModalScreen[None]):
    """管理全局 agent 用户 profiles（MainScreen 顶层入口）。

    全局 registry 存于 data['agent_users']，跨团队复用。
    支持新增、编辑、重命名、删除；rename/delete 会 sweep 所有团队的
    default_agent_user 与 member.agent_user 引用。团队默认的设置/清除
    移到了 TeamDefaultAgentUserDialog（TeamDetailScreen）。
    """

    BINDINGS = [
        Binding("escape", "close_dialog", "关闭"),
        Binding("q", "close_dialog", "关闭"),
    ]

    # Agent 用户管理弹窗布局修正（作用域限定本弹窗）:
    #   - 操作按钮（新建/编辑/重命名/删除/关闭）右侧对齐;
    #   - 表单允许纵向滚动：低高度视口下操作按钮仍可达。
    CSS = """
    #agent_user_actions {
        align: right middle;
    }
    .dialog-form {
        overflow-y: auto;
    }
    /* default 按钮可辨识底色由 TeamManagerApp.CSS 的 .agent-btn-default
       规则处理（背景 $panel 区别于表单 $surface）；此处补 ansi 终端回退
       （ansi_default），避免真彩色在不支持真彩色的终端黑化。 */
    #agent_user_actions Button:ansi,
    #agent_user_actions Button:ansi:hover {
        background: ansi_default;
    }
    """

    def __init__(self, team_name: str = "") -> None:
        super().__init__()
        self._team_name = team_name  # 保留参数以兼容旧调用；profile 存储为全局

    def compose(self) -> ComposeResult:
        options = _global_profile_options()
        # 空态提示独立于列表：OptionList 始终挂载（id 稳定），
        # 新建首个 profile 后 _refresh_dialog 可直接 query 到并立即刷新。
        empty_hint = Label("暂无 profile，请先新建", id="agent_user_empty")
        empty_hint.display = not bool(options)

        # 操作按钮存入 self，供窄宽度下按可用宽度自动换行（_reflow_action_buttons）。
        # default 变体按钮带 agent-btn-default 类：底色 $panel 与表单 $surface 区分，
        # 避免"色块视觉为空"（primary/error 语义不变，见 TeamManagerApp.CSS）。
        buttons = [
            Button("➕ 新建", variant="primary", id="btn_new"),
            Button("✏  编辑", variant="default", id="btn_edit",
                   classes="agent-btn-default"),
            Button("📛 重命名", variant="default", id="btn_rename",
                   classes="agent-btn-default"),
            Button("🗑  删除", variant="error", id="btn_delete"),
            Button("关闭", variant="default", id="btn_close",
                   classes="agent-btn-default"),
        ]
        self._action_buttons = buttons

        yield Container(
            Label("[bold]Agent 用户管理 (全局)[/bold]", classes="dialog-title"),
            Label("跨团队复用；团队默认请在团队详情页设置。"
                  "仅共享 profile 配置，不共享任何成员终端。", id="agent_user_desc"),
            empty_hint,
            OptionList(
                *[Option(label, id=key) for label, key in options],
                id="agent_user_list",
                classes="agent-user-list",
            ),
            Label("", id="agent_user_result"),
            # 操作按钮始终为同一 Grid 的 children，只在挂载/宽度变化后更新
            # grid_size_columns，让 Grid 自动生成多行；任何宽度下 5 个按钮
            # 都是同一实例，不允许动态 remove/remount 丢控件。
            Grid(*buttons, id="agent_user_actions", classes="dialog-buttons"),
            classes="dialog-form agent-user-manage-form",
        )

    def on_mount(self) -> None:
        """初始高亮第一行，保证编辑/重命名/删除立即可用；随后按可用宽度折行按钮。"""
        self._refresh_dialog()
        self.call_after_refresh(self._reflow_action_buttons)

    def on_resize(self, _event: events.Resize) -> None:
        """终端宽度变化时重排按钮，避免删除等按钮被横向裁剪。"""
        self.call_after_refresh(self._reflow_action_buttons)

    def _reflow_action_buttons(self) -> None:
        """按可用宽度更新 Grid 列数，让操作按钮自动换行。

        - 够宽（所有按钮并排 ≤ 可用宽度）→ 单行（宽屏布局不变）；
        - 不够宽 → 减少列数，Grid 自动生成多行，删除等按钮可见、不被裁剪。
        按钮宽度用其渲染宽度（region.width），回退到内容宽度；始终≥1。
        """
        try:
            grid = self.query_one("#agent_user_actions", Grid)
        except Exception:
            return
        buttons = list(self._action_buttons)
        if not buttons:
            return
        available = grid.content_size.width
        if available <= 0:
            available = self.size.width
        n = len(buttons)
        widths = [
            max(
                b.region.width,
                b.get_content_width(grid.content_size, self.size),
            ) or 1
            for b in buttons
        ]
        fitting_columns = 1
        for columns in range(1, n + 1):
            column_widths = [0] * columns
            for index, item_width in enumerate(widths):
                column = index % columns
                column_widths[column] = max(column_widths[column], item_width)
            if sum(column_widths) <= available:
                fitting_columns = columns
        grid.styles.grid_size_columns = fitting_columns

    def _selected_key(self) -> str:
        """读取当前高亮的 profile key；列表不存在（空）或无高亮返回 ''。"""
        try:
            option_list = self.query_one("#agent_user_list", OptionList)
        except Exception:
            return ""
        return _highlighted_profile_key(option_list)

    def action_close_dialog(self) -> None:
        """Escape / q 键退出，关闭按钮也复用此路径。"""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_new")
    @work
    async def new_user(self) -> None:
        result = await self.app.push_screen_wait(AgentUserEditDialog())
        if result is None:
            return
        data = load_data()
        key = result["key"]
        if key in _agent_user_profiles():
            self.query_one("#agent_user_result", Label).update(
                f"'{key}' 已存在，请改用其他标识")
            return
        data.setdefault("agent_users", {})[key] = {
            "agent_type": result["agent_type"],
            "takeover_enabled": result["takeover_enabled"],
            "effort": result.get("effort", ""),
            "anthropic_api_key": result["anthropic_api_key"],
            "anthropic_base_url": result["anthropic_base_url"],
            "anthropic_model": result["anthropic_model"],
            "openai_api_key": result["openai_api_key"],
            "openai_base_url": result["openai_base_url"],
            "codex_model": result["codex_model"],
        }
        from common.data_layer import save_data
        save_data(data)
        self._refresh_dialog()

    @on(Button.Pressed, "#btn_edit")
    @work
    async def edit_user(self) -> None:
        selected = self._selected_key()
        if not selected:
            self.query_one("#agent_user_result", Label).update(
                "请先选择或新建 profile")
            return
        profiles = _agent_user_profiles()
        cfg = profiles.get(selected, {})
        result = await self.app.push_screen_wait(AgentUserEditDialog(
            user_key=selected,
            agent_type=_resolve_profile_agent_type(cfg),
            takeover_enabled=bool(cfg.get("takeover_enabled")),
            effort=cfg.get("effort", ""),
            anthropic_api_key=cfg.get("anthropic_api_key", ""),
            anthropic_base_url=cfg.get("anthropic_base_url", ""),
            anthropic_model=cfg.get("anthropic_model", ""),
            openai_api_key=cfg.get("openai_api_key", ""),
            openai_base_url=cfg.get("openai_base_url", ""),
            codex_model=cfg.get("codex_model", ""),
        ))
        if result is None:
            return
        # 编辑态 key 不可改；写入全局 registry
        data = load_data()
        data.setdefault("agent_users", {})[result["key"]] = {
            "agent_type": result["agent_type"],
            "takeover_enabled": result["takeover_enabled"],
            "effort": result.get("effort", ""),
            "anthropic_api_key": result["anthropic_api_key"],
            "anthropic_base_url": result["anthropic_base_url"],
            "anthropic_model": result["anthropic_model"],
            "openai_api_key": result["openai_api_key"],
            "openai_base_url": result["openai_base_url"],
            "codex_model": result["codex_model"],
        }
        from common.data_layer import save_data
        save_data(data)
        self._refresh_dialog()

    @on(Button.Pressed, "#btn_rename")
    @work
    async def rename_user(self) -> None:
        selected = self._selected_key()
        if not selected:
            self.query_one("#agent_user_result", Label).update(
                "请先选择或新建 profile")
            return
        profiles = _agent_user_profiles()
        if selected not in profiles:
            self.query_one("#agent_user_result", Label).update(
                f"profile '{selected}' 不存在")
            return
        new_key = await self.app.push_screen_wait(
            AgentUserRenameDialog(old_key=selected, taken_keys=set(profiles)))
        if not new_key:
            return
        data = load_data()
        agent_users = data.setdefault("agent_users", {})
        old_cfg = agent_users.pop(selected, None)
        if old_cfg is None:
            old_cfg = profiles[selected]  # 仅存在于旧团队级数据 → 落库全局
        agent_users[new_key] = old_cfg
        teams_aff, members_aff = _agent_user_rename_sweep(data, selected, new_key)
        # 清理旧 key 的私有 settings 残留（旧 key 凭据不再有效，避免无限残留）
        _removed, _failed = _purge_agent_user_settings(selected)
        from common.data_layer import save_data
        save_data(data)
        _msg = (f"已重命名 '{selected}' → '{new_key}'"
                f"（同步 {teams_aff} 团队 / {members_aff} 成员引用）")
        if _failed:
            _msg += f"\n⚠ 私有 settings 清理失败 {len(_failed)} 个（旧凭据可能残留）"
        self.query_one("#agent_user_result", Label).update(_msg)
        self._refresh_dialog()

    @on(Button.Pressed, "#btn_delete")
    @work
    async def delete_user(self) -> None:
        selected = self._selected_key()
        if not selected:
            self.query_one("#agent_user_result", Label).update(
                "请先选择或新建 profile")
            return
        profiles = _agent_user_profiles()
        if selected not in profiles:
            self.query_one("#agent_user_result", Label).update(
                f"profile '{selected}' 不存在")
            return
        data = load_data()
        teams_aff, members_aff = _agent_user_ref_count(data, selected)
        impact = (f"跨团队清理：{teams_aff} 个团队、{members_aff} 个成员引用将被复位\n"
                  "成员将回退团队默认，团队默认将被清除")
        confirmed = await self.app.push_screen_wait(
            ConfirmBox(f"确认删除全局 Agent 用户 '{selected}'？\n{impact}"))
        if not confirmed:
            return
        data = load_data()
        agent_users = data.get("agent_users") or {}
        agent_users.pop(selected, None)
        if not agent_users:
            data.pop("agent_users", None)
        teams_aff2, members_aff2 = _agent_user_delete_sweep(data, selected)
        # 清理被删 profile 的私有 settings 残留（旧凭据随 profile 删除一并清理）
        _removed, _failed = _purge_agent_user_settings(selected)
        from common.data_layer import save_data
        save_data(data)
        _msg = f"已删除 '{selected}'（复位 {teams_aff2} 团队 / {members_aff2} 成员引用）"
        if _failed:
            _msg += f"\n⚠ 私有 settings 清理失败 {len(_failed)} 个（旧凭据可能残留）"
        self.query_one("#agent_user_result", Label).update(_msg)
        self._refresh_dialog()

    @on(Button.Pressed, "#btn_close")
    def close_dialog(self) -> None:
        self.dismiss(None)

    def _refresh_dialog(self) -> None:
        """重建全局 profile 列表，恢复高亮 / 默认高亮第一行。

        OptionList.set_options 会重置高亮；这里在重建后恢复：
          - 当前高亮仍是合法 profile key → 保持该行高亮；
          - 否则有行时高亮第一行（编辑/删除立即可用），无行时无高亮。
        """
        option_list = self.query_one("#agent_user_list", OptionList)
        current = _highlighted_profile_key(option_list)
        options = _global_profile_options()
        option_list.set_options(
            [Option(label, id=key) for label, key in options])
        keys = [key for _, key in options]
        if current in keys:
            option_list.highlighted = keys.index(current)
        elif keys:
            option_list.highlighted = 0
        else:
            option_list.highlighted = None
        self._sync_empty_hint(empty=not keys)

    def _sync_empty_hint(self, empty: bool) -> None:
        """空态提示随列表是否有行切换；OptionList 始终挂载，永不替换。"""
        try:
            hint = self.query_one("#agent_user_empty", Label)
        except Exception:
            return
        hint.display = empty


class TeamDefaultAgentUserDialog(ScrollableModalScreen[None]):
    """选择团队系统默认 Agent 用户（TeamDetailScreen u 入口）。

    从全局 profile 列表选择设为团队默认；选择「不接管」则清除团队默认。
    三态（无选择 / 普通 profile / 不接管）下均不崩溃且行为正确。
    """

    BINDINGS = [
        Binding("escape", "close_dialog", "关闭"),
        Binding("q", "close_dialog", "关闭"),
    ]

    def __init__(self, team_name: str) -> None:
        super().__init__()
        self._team_name = team_name

    @property
    def _default_key(self) -> str:
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        return team.get("default_agent_user", "")

    def compose(self) -> ComposeResult:
        profiles = _agent_user_profiles(self._team_name)
        options = [("不接管（清除团队默认）", AGENT_USER_NONE)]
        option_values = {AGENT_USER_NONE}
        for key, cfg in profiles.items():
            at = _resolve_profile_agent_type(cfg)
            options.append((f"{_agent_type_badge(at)} {key}", key))
            option_values.add(key)
        current = self._default_key
        current_label = f"当前团队默认: {current}" if current else "当前团队默认: 无"

        yield Container(
            Label(f"[bold]{self._team_name} — 团队默认 Agent 用户[/bold]", classes="dialog-title"),
            Label(current_label, id="team_default_current"),
            Label("选择全局 profile 设为团队默认；'不接管' 清除团队默认", id="team_default_desc"),
            FormField(
                "Agent用户",
                Select(options, id="team_default_select",
                       value=current if current in option_values else Select.NULL)
                if options else Label("暂无 profile"),
            ),
            Label("", id="team_default_result"),
            Horizontal(
                Button("设为默认", variant="primary", id="btn_set_default"),
                Button("关闭", variant="default", id="btn_close"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    def action_close_dialog(self) -> None:
        """Escape / q 键退出，关闭按钮也复用此路径。"""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_set_default")
    @work
    async def set_default(self) -> None:
        """将选中的全局 profile 设为团队默认；「不接管」清除默认。"""
        selected = _selected_profile_key(self.query_one("#team_default_select", Select))
        result = self.query_one("#team_default_result", Label)
        if not selected:
            result.update("请先选择一个 profile")
            return
        data = load_data()
        team = data.setdefault("teams", {}).setdefault(self._team_name, {})
        if selected == AGENT_USER_NONE:
            # 选择「不接管」→ 清除团队默认（幂等），不把 __none__ 写入
            # default_agent_user——它是 profile key 查找键，清空即表示
            # 成员回退时不再注入任何 agent 用户 env。
            team.pop("default_agent_user", None)
            from common.data_layer import save_data
            save_data(data)
            result.update("已设置：团队默认不接管")
            self._refresh()
            return
        # 验证 profile 存在且有 agent_type（typed profile）
        profiles = _agent_user_profiles(self._team_name)
        cfg = profiles.get(selected, {})
        at = _resolve_profile_agent_type(cfg)
        if not at:
            result.update("旧版 profile 无法设为团队默认，请先编辑选择 Provider")
            return
        team["default_agent_user"] = selected
        from common.data_layer import save_data
        save_data(data)
        result.update(f"⭐ '{selected}' 已设为团队默认")
        self._refresh()

    @on(Button.Pressed, "#btn_close")
    def close_dialog(self) -> None:
        self.dismiss(None)

    def _refresh(self) -> None:
        current = self._default_key
        self.query_one("#team_default_current", Label).update(
            f"当前团队默认: {current}" if current else "当前团队默认: 无")


class AgentUserPoolDialog(SelectSafeDismissMixin, ScrollableModalScreen[None]):
    """配置团队 Agent 用户切换池（TeamDetailScreen 4 入口）。

    多选全局 profile，**勾选顺序即切换顺序**：SelectionList.selected 返回
    点选/切换顺序（dict 插入序），取消再勾选会把该项移到末尾，直接作为
    plan-b §3.2 的 agent_user_pool 落盘，无需自维护有序列表。

    落盘语义（供 mult_agent_mcp.py 消费）：
      - 保存: team["agent_user_pool"] = 点选顺序列表；池内容变化时
        team["agent_user_pool_cursor"] = 0（换号游标回第一位）；
      - 清空选择: pop 掉 agent_user_pool 与 agent_user_pool_cursor 两键。

    Provider 防呆锁（防呆漏洞修复）：
      - **初始锁 = 团队默认 agent 的 resolved type**（agent_type(
        team.default_agent or "claude")）—— 打开对话框时异类行已置灰，
        消灭"第一下能点错 provider"的窗口。此前是"首勾锁定"：未勾选时无锁，
        codex 默认团队也能先点 claude，点完锁才建立（用户报的防呆漏洞）。
      - **"t" 键显式解锁**：团队池是给多成员共用的、成员可各自覆盖
        member.agent，不能像成员池那样硬过滤（否则 codex 团队里的 claude
        成员就没池可配）。默认收紧在团队默认类型，按 "t" 切到"显示全部"
        后回落"首个勾选决定"的动态锁；再按 "t" 恢复默认锁定。
    api_key 仅显示"已配置/未配置"，永不显示明文。
    """

    BINDINGS = [
        Binding("escape", "close_dialog", "关闭"),
        Binding("q", "close_dialog", "关闭"),
        Binding("t", "toggle_lock_mode", "显示全部/仅默认"),
        Binding("f", "toggle_failover", "自动换号 开/关"),
    ]

    CSS = """
    #agent_user_pool_list {
        height: 10;
    }
    #agent_user_pool_summary {
        color: $text-muted;
    }
    #agent_user_pool_lock {
        color: $warning;
    }
    /* 开关状态行：id 稳定（textual 不允许改已挂载节点的 id），配色靠 class 切换 */
    #agent_user_pool_failover.off {
        color: $warning;
    }
    #agent_user_pool_failover.on {
        color: $success;
    }
    """

    def __init__(self, team_name: str) -> None:
        super().__init__()
        self._team_name = team_name
        # 初始锁 = 团队默认 agent 的 resolved type（默认收紧防呆，"t" 显式解锁）
        data = load_data()
        team = data.get("teams", {}).get(team_name, {})
        self._default_lock_type = agent_type(team.get("default_agent") or "claude")
        self._lock_mode = "default"  # "default" 仅默认 provider | "all" 显示全部
        self._lock_type: str | None = None
        self._row_locked: dict[int, bool] = {}

    @property
    def _stored_pool(self) -> list[str]:
        """读取已存切换池（仅字符串键，防御脏数据）。"""
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        pool = team.get("agent_user_pool", [])
        if not isinstance(pool, list):
            return []
        return [k for k in pool if isinstance(k, str)]

    @staticmethod
    def _profile_label(key: str, cfg: dict) -> str:
        """列表行 label：provider badge + key + API 配置状态（掩码，无明文）。"""
        at = _resolve_profile_agent_type(cfg)
        if at == "claude":
            api = _api_key_display(cfg.get("anthropic_api_key"))
        elif at == "codex":
            api = _api_key_display(cfg.get("openai_api_key"))
        else:
            api = "未配置"
        return f"{_agent_type_badge(at)} {key}  · API {api}"

    @staticmethod
    def _order_summary_text(selected: list[str]) -> str:
        """切换顺序摘要：1. keyA → 2. keyB → …（本需求的核心可见性）。"""
        if not selected:
            return "切换顺序: （未勾选）"
        return "切换顺序: " + " → ".join(f"{i}. {k}" for i, k in enumerate(selected, 1))

    def _failover_enabled(self) -> bool:
        """团队自动换号总开关（读取走数据层 quota_failover_config，不另设默认）。"""
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        return bool(quota_failover_config(team)["enabled"])

    @staticmethod
    def _failover_status_text(enabled: bool) -> str:
        """开关状态行：关闭时必须明说"配了池也不会换"，否则用户无从判断。"""
        if enabled:
            return "自动换号: ✅ 已启用（识别到配额耗尽后按上面的顺序切换）"
        return ("自动换号: ⚠️ 未启用 —— 即使池已配好也【不会】自动切换，"
                "只会标记阻塞。按 f 开启")

    def compose(self) -> ComposeResult:
        profiles = _agent_user_profiles(self._team_name)
        self._profiles = profiles
        self._option_values = set(profiles.keys())
        selections = [
            Selection(self._profile_label(key, cfg), key)
            for key, cfg in profiles.items()
        ]
        # 空态提示独立于列表：SelectionList 始终挂载（id 稳定），后续新增
        # profile 刷新时可直接 query 到（照 AgentUserManageDialog :1746 约定）。
        empty_hint = Label("暂无 profile，请先新建", id="agent_user_pool_empty")
        empty_hint.display = not bool(selections)

        lock_hint = Label("", id="agent_user_pool_lock")
        lock_hint.display = False

        pool = self._stored_pool
        current_label = f"当前顺序: {' → '.join(pool)}" if pool else "当前顺序: 未配置"
        fo_on = self._failover_enabled()
        failover_label = Label(
            self._failover_status_text(fo_on),
            id="agent_user_pool_failover",
            classes="on" if fo_on else "off",
        )

        yield Container(
            Label(f"[bold]{self._team_name} — Agent 用户切换池[/bold]", classes="dialog-title"),
            Label(current_label, id="agent_user_pool_current"),
            Label("多选全局 profile，勾选顺序即切换顺序；取消再勾选会移到末尾。"
                  "池内必须同 provider（换号只换同类型的号）",
                  id="agent_user_pool_desc"),
            empty_hint,
            SelectionList(*selections, id="agent_user_pool_list"),
            Label(self._order_summary_text(pool), id="agent_user_pool_summary"),
            failover_label,
            lock_hint,
            Label("", id="agent_user_pool_result"),
            Horizontal(
                Button("保存", variant="primary", id="btn_save_pool"),
                Button(
                    "关闭自动换号" if fo_on else "开启自动换号",
                    variant="warning" if fo_on else "success",
                    id="btn_toggle_failover",
                ),
                Button("关闭", variant="default", id="btn_close"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    def on_mount(self) -> None:
        """还原已存池的点选顺序。

        Selection 的 initial_state 由 OptionList 按**列表固有顺序**依次选中
        （_from_options 逐项 _select），无法表达池序；这里统一不设
        initial_state，挂载后按池顺序逐个 toggle，selected 即还原为用户
        上次保存的点选顺序。

        Provider 初始锁同样在此生效：挂载时锁恒为"default"模式（"t" 只能
        在挂载后按），历史脏池（混 type）只还原与**团队默认 agent 类型**
        相同的键，异 type 键置灰不还原 —— 保持池内 provider 一致性且不与
        初始锁冲突。
        """
        sel = self.query_one("#agent_user_pool_list", SelectionList)
        lock_type = self._default_lock_type
        for key in self._stored_pool:
            if key not in self._option_values:
                continue
            if _resolve_profile_agent_type(self._profiles.get(key, {})) != lock_type:
                continue
            sel.toggle(key)
        self._update_provider_lock()
        self._update_order_summary()

    @on(SelectionList.SelectedChanged, "#agent_user_pool_list")
    def _on_pool_selection_changed(self, _event: SelectionList.SelectedChanged) -> None:
        """勾选变化：纠偏异 provider 误选 + 刷新锁状态与顺序摘要。"""
        self._enforce_provider_lock()
        self._update_provider_lock()
        self._update_order_summary()

    def _enforce_provider_lock(self) -> None:
        """防呆兜底：任何路径（含键盘 space 跳过 disabled）选中异 type → 立即 toggle 回去。

        mouse 点击已被 OptionList._on_click 的 disabled 检查物理拦截；此方法
        兜住不走点击路径的合法 API（toggle/键盘），保证锁在 UI 层不可绕过。
        """
        lock = self._lock_type
        if lock is None:
            return
        sel = self.query_one("#agent_user_pool_list", SelectionList)
        for key in list(sel.selected):
            cfg = self._profiles.get(key, {})
            if _resolve_profile_agent_type(cfg) != lock:
                sel.toggle(key)  # 立即取消，保留其余 selected 的顺序
                self.query_one("#agent_user_pool_result", Label).update(
                    f"⛔ {key} 与当前锁定 provider 不同，已取消勾选")

    def _effective_lock(self) -> str | None:
        """当前生效的锁类型。

        - "default" 模式：固定 = 团队默认 agent 的 resolved type（初始锁，
          打开即收紧，消灭"第一下点错 provider"的窗口；全选取消也不释放）；
        - "all" 模式：回落"首个勾选决定"的动态锁；未勾选 → 无锁（全部可选）。
        legacy 的 resolved type 是 ""（独立类别）——判定必须用 is not None。

        默认 agent 是自定义命令时（agent_type → "other"）**不预锁**，退回动态锁：
        _profile_resolved_atype 永不返回 "other"（只有 'claude'|'codex'|''），
        锁成 "other" 会让每一行都判异类置灰 —— 团队池一个都选不了。
        与数据层同源：_profile_matches_atype 对 "other" 也不过滤
        （common/tmux_utils.py），机器换号侧由 select_failover_candidate 的
        pool-other-agent 兜住空转，UI 层不再重复堵。
        """
        if self._lock_mode == "default" and self._default_lock_type != "other":
            return self._default_lock_type
        selected = list(self.query_one(
            "#agent_user_pool_list", SelectionList).selected)
        if not selected:
            return None
        return _resolve_profile_agent_type(self._profiles.get(selected[0], {}))

    def action_toggle_lock_mode(self) -> None:
        """"t"：切换 仅默认 provider / 显示全部。

        团队池是多成员共用、成员可各自覆盖 agent，不能像成员池那样硬过滤
        —— 默认收紧在团队默认类型，显式解锁后回落动态锁。切回"仅默认"时
        立即把已勾选但不符合默认类型的 key 踢掉（_enforce_provider_lock 兜底），
        防呆不因解锁操作破功。
        """
        self._lock_mode = "default" if self._lock_mode == "all" else "all"
        if self._lock_mode == "default":
            self._lock_type = self._default_lock_type
            self._enforce_provider_lock()
        self._update_provider_lock()

    def _update_provider_lock(self) -> None:
        """Provider 防呆锁：锁 = _effective_lock() 的当前类型。

        与锁同 type 的 profile 保持可选；异 type 的置灰（disabled 物理不可点
        + dim prompt 可见），提示行说明当前锁与后果。"default" 模式下锁恒为
        团队默认类型，全选取消**不释放锁**；仅 "t" 切到 "all" 模式才回落
        动态锁（全空 → 无锁，全部恢复）。
        legacy（agent_type 为空串）作为独立类别参与同一规则，无特例分支。
        全部原地改（disable_option_at_index / replace_option_prompt_at_index），
        绝不重建选项列表 —— sel.selected 的插入序（点选顺序）必须原样保留。
        """
        sel = self.query_one("#agent_user_pool_list", SelectionList)
        lock_type = self._effective_lock()
        self._lock_type = lock_type
        for i, (key, cfg) in enumerate(self._profiles.items()):
            at = _resolve_profile_agent_type(cfg)
            blocked = lock_type is not None and at != lock_type
            if blocked == self._row_locked.get(i):
                continue  # 行状态未变，跳过（避免无谓重绘）
            if blocked:
                sel.disable_option_at_index(i)
                sel.replace_option_prompt_at_index(i, self._dim_label(key, cfg))
            else:
                sel.enable_option_at_index(i)
                sel.replace_option_prompt_at_index(i, self._profile_label(key, cfg))
            self._row_locked[i] = blocked
        hint = self.query_one("#agent_user_pool_lock", Label)
        if self._lock_mode == "default" and self._default_lock_type == "other":
            # 自定义命令无法判定 provider → 未预锁（见 _effective_lock），
            # 此处必须如实说明，否则文案与"全部可选"的实际行为矛盾。
            hint.update(
                "⚠️ 团队默认 agent 为自定义命令，无法自动校验 provider —— "
                "请自行确认所选 profile 与成员实际 CLI 匹配"
                "（自动换号会被拒绝，需人工介入）")
            hint.display = True
        elif self._lock_mode == "default":
            lock_label = {"claude": "claude", "codex": "codex"}.get(
                self._default_lock_type, self._default_lock_type)
            hint.update(
                f"🔒 已按团队默认 agent({lock_label})锁定（按 t 显示全部）"
                f" —— 其它 provider 的成员需在成员池单独配置")
            hint.display = True
        else:
            base = ("⚠️ 已显示全部 provider（按 t 恢复仅默认）"
                    " —— 与团队默认 agent 不符的池,仅对显式覆盖了 agent 的成员生效")
            if lock_type is not None:
                # legacy 的 resolved type 是 ""（独立类别）——必须用 is not None 判断
                lock_label = {"claude": "claude", "codex": "codex"}.get(
                    lock_type, "旧版")
                base += f"（当前首勾锁: {lock_label}）"
            hint.update(base)
            hint.display = True

    def _dim_label(self, key: str, cfg: dict) -> str:
        """置灰行 label：dim markup 可见化 + 物理 disabled（见 _update_provider_lock）。"""
        return f"[dim]{self._profile_label(key, cfg)}[/dim]"

    def _update_order_summary(self) -> None:
        sel = self.query_one("#agent_user_pool_list", SelectionList)
        self.query_one("#agent_user_pool_summary", Label).update(
            self._order_summary_text(list(sel.selected)))

    @on(Button.Pressed, "#btn_save_pool")
    @work
    async def save_pool(self) -> None:
        """按点选顺序保存切换池；空选择清空池并移除游标。"""
        sel = self.query_one("#agent_user_pool_list", SelectionList)
        selected = list(sel.selected)
        result = self.query_one("#agent_user_pool_result", Label)
        data = load_data()
        team = data.setdefault("teams", {}).setdefault(self._team_name, {})
        old_pool = team.get("agent_user_pool", [])
        if selected:
            team["agent_user_pool"] = selected
            if old_pool != selected:
                # 池内容变化 → 换号游标重置回第一位（无谓换号的起点安全）
                team["agent_user_pool_cursor"] = 0
            from common.data_layer import save_data
            save_data(data)
            result.update(f"✅ 已保存 {len(selected)} 个 profile（按勾选顺序）")
        else:
            team.pop("agent_user_pool", None)
            team.pop("agent_user_pool_cursor", None)
            from common.data_layer import save_data
            save_data(data)
            result.update("✅ 已清空切换池")
        self._refresh()

    def action_close_dialog(self) -> None:
        """Escape / q 键退出，关闭按钮也复用此路径。"""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_close")
    def close_dialog(self) -> None:
        self.dismiss(None)

    def _refresh(self) -> None:
        pool = self._stored_pool
        self.query_one("#agent_user_pool_current", Label).update(
            f"当前顺序: {' → '.join(pool)}" if pool else "当前顺序: 未配置")
        self._update_order_summary()
        self._refresh_failover_row()

    @on(Button.Pressed, "#btn_toggle_failover")
    def toggle_failover_button(self) -> None:
        self.action_toggle_failover()

    def action_toggle_failover(self) -> None:
        """切换团队 quota_failover.enabled（自动换号总开关）。

        只翻 enabled 一个键，confirm_cycles / wrap / max_switches 一律不动 ——
        它们的默认与钳制由数据层 quota_failover_config 负责，UI 不重复实现。
        """
        from common.data_layer import save_data
        data = load_data()
        team = data.setdefault("teams", {}).setdefault(self._team_name, {})
        stored = team.get("quota_failover")
        if not isinstance(stored, dict):
            stored = {}
        new_state = not bool(quota_failover_config(team)["enabled"])
        stored["enabled"] = new_state
        team["quota_failover"] = stored
        save_data(data)
        result = self.query_one("#agent_user_pool_result", Label)
        if new_state and len(self._stored_pool) < 2:
            # 开了开关但池不足两个号 → 换号仍然无处可去（select_failover_candidate
            # 会返回 pool-empty / pool-single），必须当场说清而不是让用户以为搞定了
            result.update("✅ 已开启自动换号；⚠️ 但池内少于 2 个 profile，仍无处可换")
        else:
            result.update("✅ 已开启自动换号" if new_state else "✅ 已关闭自动换号")
        self._refresh()

    def _refresh_failover_row(self) -> None:
        """同步开关状态行与按钮文案。

        状态行 id 固定为 agent_user_pool_failover —— textual 不允许修改已挂载
        节点的 id（会抛 ValueError），配色靠 on/off class 切换。
        """
        enabled = self._failover_enabled()
        for node in self.query("#agent_user_pool_failover"):
            node.set_class(enabled, "on")
            node.set_class(not enabled, "off")
            node.update(self._failover_status_text(enabled))
        for btn in self.query("#btn_toggle_failover"):
            btn.label = "关闭自动换号" if enabled else "开启自动换号"
            btn.variant = "warning" if enabled else "success"


class MemberAgentUserPoolDialog(SelectSafeDismissMixin, ScrollableModalScreen[None]):
    """配置成员级 Agent 用户切换池（EditMemberDialog 入口）。

    与 AgentUserPoolDialog（团队池）同语义：多选 profile，**勾选顺序即切换
    顺序**（SelectionList.selected 的 dict 插入序直接落盘）。两点关键差异：

      1. provider 由 resolve_pool_atype(team, member) **单向决定**——不是团队池
         那种"首勾锁定"：成员跑哪个 CLI 是既成事实，池内 key 必须与该 CLI
         类型匹配（数据层 set_member_agent_user_pool 强校验同规则，任何调用
         方都绕不过）。列表按该类型**静态**过滤：匹配行可选，异类行在
         on_mount 原地置灰（disable_option_at_index，**绝不重建列表**——重建
         会丢 SelectionList._selected 的插入序即点选顺序）并标注原因。
         过滤语义用数据层同源的 _profile_matches_atype（typed 必须相等；
         legacy 按是否携带该 atype 的 base_url 判定），保证"UI 可选 == 保存
         能过"。
      2. 成员池激活后团队池**完全不参与**（用户裁定：哪怕池内全部耗尽也不
         回落）。弹窗明示当前生效的池与"不回落"语义，避免误以为有兜底。

    落盘: set_member_agent_user_pool(team_name, member_name, keys)
      - 非空列表按点选顺序写入，cursor 归零；
      - 空列表 = 取消激活（pop 掉 agent_user_pool / agent_user_pool_cursor，
        回落团队池）。
    api_key 仅显示"已配置/未配置"，永不显示明文。
    """

    BINDINGS = [
        Binding("escape", "close_dialog", "关闭"),
        Binding("q", "close_dialog", "关闭"),
    ]

    CSS = """
    #member_pool_list {
        height: 10;
    }
    #member_pool_summary {
        color: $text-muted;
    }
    #member_pool_status {
        color: $accent;
    }
    #member_pool_warning {
        color: $warning;
    }
    #member_pool_atype {
        color: $text-muted;
    }
    """

    def __init__(self, team_name: str, member_name: str) -> None:
        super().__init__()
        self._team_name = team_name
        self._member_name = member_name

    def _member(self) -> dict:
        data = load_data()
        team = data.get("teams", {}).get(self._team_name, {})
        return (team.get("members") or {}).get(self._member_name, {})

    def _team(self) -> dict:
        data = load_data()
        return data.get("teams", {}).get(self._team_name, {})

    @property
    def _atype(self) -> str:
        """成员池 provider：resolve_pool_atype 单向决定（member.agent →
        team.default_agent → "claude"），与数据层强校验同一来源。"""
        return resolve_pool_atype(self._team(), self._member())

    @property
    def _stored_pool(self) -> list[str]:
        """读取成员已存切换池（仅字符串键，防御脏数据）。"""
        pool = self._member().get("agent_user_pool", [])
        if not isinstance(pool, list):
            return []
        return [k for k in pool if isinstance(k, str)]

    @staticmethod
    def _profile_label(key: str, cfg: dict) -> str:
        """列表行 label：provider badge + key + API 配置状态（掩码，无明文）。"""
        at = _resolve_profile_agent_type(cfg)
        if at == "claude":
            api = _api_key_display(cfg.get("anthropic_api_key"))
        elif at == "codex":
            api = _api_key_display(cfg.get("openai_api_key"))
        else:
            api = "未配置"
        return f"{_agent_type_badge(at)} {key}  · API {api}"

    def _dim_label(self, key: str, cfg: dict) -> str:
        """异类行 label：dim markup + 原因标注（静态过滤，见 class docstring）。"""
        return (
            f"[dim]{self._profile_label(key, cfg)}"
            f" · 与成员 {self._atype} 不匹配[/dim]"
        )

    @staticmethod
    def _order_summary_text(selected: list[str]) -> str:
        """切换顺序摘要：1. keyA → 2. keyB → …（本需求的核心可见性）。"""
        if not selected:
            return "切换顺序: （未勾选）"
        return "切换顺序: " + " → ".join(f"{i}. {k}" for i, k in enumerate(selected, 1))

    def compose(self) -> ComposeResult:
        profiles = _agent_user_profiles(self._team_name)
        self._profiles = profiles
        self._option_values = set(profiles.keys())
        atype = self._atype
        self._row_matches: dict[int, bool] = {}
        selections = []
        for i, (key, cfg) in enumerate(profiles.items()):
            matches = _profile_matches_atype(cfg, atype)
            self._row_matches[i] = matches
            # 构造期一律纯文本 label：markup（[dim]）由 on_mount 用
            # replace_option_prompt_at_index 原地套（构造期传 markup 会被
            # Selection 解析成富文本，str(prompt) 只剩纯文本，置灰不可见）。
            selections.append(Selection(self._profile_label(key, cfg), key))
        # 空态提示独立于列表：SelectionList 始终挂载（id 稳定），后续新增
        # profile 刷新时可直接 query 到（照 AgentUserPoolDialog 约定）。
        empty_hint = Label("暂无 profile，请先新建", id="member_pool_empty")
        empty_hint.display = not bool(selections)

        member = self._member()
        activated = member_pool_is_activated(member)
        if activated:
            status_text = "当前生效: ✅ 成员池（团队池不参与，耗尽也不回落）"
        else:
            status_text = "当前生效: 团队池（成员池未配置 —— 保存勾选后切换为成员池）"
        atype_label = {"claude": "🤖Claude", "codex": "🔵Codex"}.get(atype, "⚪自定义命令")
        atype_hint = (
            # "other" 已不过滤（_profile_matches_atype 全 True → 全部可选），
            # 如实说明并给用户动作 + 自动换号后果，避免文案与行为矛盾。
            "自定义命令无法判定 provider —— 请自行确认所选 profile "
            "与成员实际 CLI 匹配；自动换号将被拒绝，需人工介入"
            if atype == "other"
            else "池内只能同类 profile，异类行置灰不可选"
        )
        pool = self._stored_pool
        current_label = f"当前顺序: {' → '.join(pool)}" if pool else "当前顺序: 未配置"

        yield Container(
            Label(f"[bold]{self._member_name} — 成员级 Agent 用户切换池[/bold]",
                  classes="dialog-title"),
            Label(status_text, id="member_pool_status"),
            Label(f"成员 CLI: {atype_label}（{atype_hint}）", id="member_pool_atype"),
            Label(current_label, id="member_pool_current"),
            Label("多选 profile，勾选顺序即切换顺序；取消再勾选会移到末尾。",
                  id="member_pool_desc"),
            empty_hint,
            SelectionList(*selections, id="member_pool_list"),
            Label(self._order_summary_text(pool), id="member_pool_summary"),
            Label("⚠️ 成员池激活后团队池完全不参与：哪怕池内 profile 全部耗尽"
                  "也不会切回团队池。", id="member_pool_warning"),
            Label("", id="member_pool_result"),
            Horizontal(
                Button("保存", variant="primary", id="btn_save_member_pool"),
                Button("关闭", variant="default", id="btn_close"),
                classes="dialog-buttons",
            ),
            classes="dialog-form",
        )

    def on_mount(self) -> None:
        """静态 provider 过滤 + 按已存池顺序还原。

        异类行原地 disable_option_at_index（一次性，无动态锁——atype 由成员
        CLI 单向决定，与团队池"首勾锁定"的机制不同），**绝不重建列表**：
        重建会丢 selected 的插入序（点选顺序）。

        还原：Selection 不设 initial_state（OptionList 会按列表固有顺序逐项
        选中，无法表达池序），挂载后按池顺序逐个 toggle，selected 即还原为
        用户上次保存的点选顺序；已删除 key 与不匹配 key 显式跳过。
        """
        sel = self.query_one("#member_pool_list", SelectionList)
        for i, key in enumerate(self._profiles):
            if not self._row_matches[i]:
                sel.disable_option_at_index(i)
                # 原地替换 prompt 套 dim markup（绝不重建列表）：构造期传
                # markup 会被解析成富文本导致 [dim] 不可见，这里在挂载后
                # 原地替换（照 AgentUserPoolDialog._update_provider_lock）。
                sel.replace_option_prompt_at_index(i, self._dim_label(key, self._profiles[key]))
        for key in self._stored_pool:
            if key not in self._option_values:
                continue
            if not _profile_matches_atype(self._profiles.get(key, {}), self._atype):
                continue
            sel.toggle(key)
        self._update_order_summary()

    @on(SelectionList.SelectedChanged, "#member_pool_list")
    def _on_member_pool_selection_changed(
            self, _event: SelectionList.SelectedChanged) -> None:
        """勾选变化：刷新顺序摘要。

        无动态锁——provider 由成员 CLI 单向决定（静态过滤，见 class
        docstring），置灰行已被 OptionList 物理拦截，无需团队池的纠偏逻辑。
        """
        self._update_order_summary()

    def _update_order_summary(self) -> None:
        sel = self.query_one("#member_pool_list", SelectionList)
        self.query_one("#member_pool_summary", Label).update(
            self._order_summary_text(list(sel.selected)))

    @on(Button.Pressed, "#btn_save_member_pool")
    @work
    async def save_member_pool(self) -> None:
        """按点选顺序写入成员池；空选择 = 取消激活（回落团队池）。

        走数据层 set_member_agent_user_pool（registry + 哨兵 + provider 强
        校验，失败不部分写入），UI 过滤与之同源，正常路径不会触发拒绝。
        """
        sel = self.query_one("#member_pool_list", SelectionList)
        result = self.query_one("#member_pool_result", Label)
        ok, msg = set_member_agent_user_pool(
            self._team_name, self._member_name, list(sel.selected))
        result.update(("✅ " if ok else "⛔ ") + msg)
        if ok:
            self._refresh()

    def _refresh(self) -> None:
        """保存后刷新"当前生效"与"当前顺序"（数据层已写盘）。"""
        member = self._member()
        activated = member_pool_is_activated(member)
        status = self.query_one("#member_pool_status", Label)
        status.update(
            "当前生效: ✅ 成员池（团队池不参与，耗尽也不回落）" if activated
            else "当前生效: 团队池（成员池未配置 —— 保存勾选后切换为成员池）")
        pool = self._stored_pool
        self.query_one("#member_pool_current", Label).update(
            f"当前顺序: {' → '.join(pool)}" if pool else "当前顺序: 未配置")
        self._update_order_summary()

    def action_close_dialog(self) -> None:
        """Escape / q 键退出，关闭按钮也复用此路径。"""
        self.dismiss(None)

    @on(Button.Pressed, "#btn_close")
    def close_dialog(self) -> None:
        self.dismiss(None)
