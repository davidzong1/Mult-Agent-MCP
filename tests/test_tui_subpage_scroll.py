"""
TUI 子页面统一纵向滚动 回归测试
================================================================

根因:创建团队 / 添加成员等弹窗根容器 `height:auto`,内容超出视口高度时被
居中裁剪,底部按钮(创建/添加/保存等)在 1080P 及更低终端高度下不可达。

修复(见 tui_dialogs.py + tui_screens.py):
  - 全局 CSS:`.dialog-form`/`.dialog-box`/`.context-dialog`/
    `.context-viewer`/`.context-editor-dialog` 统一 `max-height:100%` +
    `overflow-y:auto`;内容超出视口 → 容器高度封顶视口、内部纵向滚动,底部
    交互项可滚动到达;内容低于视口 → 布局不变(无滚动条)。
  - `ScrollableModalScreen` 基类:新弹窗继承即获得统一滚动能力(17 个既有
    弹窗已 re-base;AgentUserEdit/Manage 保留各自既有滚动 CSS)。

本测试:
  - 低高度(h=18/24):表单可滚动,scroll_end 后底部按钮进入视口;
  - 宽屏(h=60):内容适配视口、无强制滚动条,按钮天然可见(布局不变);
  - focus 自动滚入视口:低高度下聚焦底部按钮即自动滚动到可见;
  - 容器类统一约束:所有被测弹窗根容器 overflow-y=auto。

数据隔离:data_layer.set_data_file 指向临时文件,绝不触碰真实 teams_data.json;
tmux / MCP daemon 调用全部 mock 或避开。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

from textual.app import App
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import (
    ScrollableModalScreen,
    CreateTeamDialog,
    AddMemberDialog,
    EditMemberDialog,
    TeamProxyDialog,
    AgentUserRenameDialog,
    AgentUserManageDialog,
    AgentUserEditDialog,
    TeamDefaultAgentUserDialog,
    NewContextFileDialog,
)
from tui.tui_screens import TeamManagerApp

# 被测弹窗注册表: (标签, 工厂, 底部提交按钮 id)
_DIALOGS = [
    ("CreateTeam", lambda: CreateTeamDialog(), "btn_create"),
    ("AddMember", lambda: AddMemberDialog(team_name="team"), "btn_add"),
    ("EditMember", lambda: EditMemberDialog("alice", "coder", "claude", team_name="team"), "btn_save"),
    ("TeamProxy", lambda: TeamProxyDialog("team", {"enabled": False, "host": "127.0.0.1", "port": 7890}), "btn_save"),
    ("AgentUserRename", lambda: AgentUserRenameDialog("claude_p", []), "btn_save"),
    ("TeamDefaultAgentUser", lambda: TeamDefaultAgentUserDialog("team"), "btn_set_default"),
    ("NewContextFile", lambda: NewContextFileDialog(_TMP_ROOT), "btn_create"),
]

# 两个保留各自既有滚动 CSS 的弹窗(未 re-base,依赖全局 CSS + 自身规则)
_DIALOGS_OWN_CSS = [
    ("AgentUserManage", lambda: AgentUserManageDialog(team_name="team"), "btn_close"),
    ("AgentUserEdit", lambda: AgentUserEditDialog(
        user_key="claude_p", agent_type="claude", takeover_enabled=True,
        anthropic_api_key="sk-ant-test", anthropic_base_url="https://api.anthropic.com",
        anthropic_model="claude-opus-5"), "btn_save"),
]

_TMP_ROOT = "/tmp"  # NewContextFileDialog 仅表单,不读取目录;占位路径

_PROFILE_DATA = {
    "agent_users": {
        "claude_p": {"agent_type": "claude", "takeover_enabled": True,
                     "anthropic_api_key": "sk-ant-test",
                     "anthropic_base_url": "https://api.anthropic.com",
                     "anthropic_model": "claude-opus-5"},
        "codex_p": {"agent_type": "codex", "takeover_enabled": False,
                    "openai_api_key": "sk-fake", "openai_base_url": "https://api.openai.com",
                    "codex_model": "gpt-4o"},
    },
    "teams": {"team": {"leader": "lead", "leader_type": "tmux", "members": {}}},
}


def _make_test_app() -> App[None]:
    """最小 App:复用生产真实 CSS(TeamManagerApp.CSS)。"""
    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    with mock.patch.dict(os.environ):
        os.environ.pop("NO_COLOR", None)
        return _TestApp()


def _form_ancestor(btn) -> "object | None":
    """找到按钮所属的弹窗根容器(带统一滚动类名)。"""
    a = btn.parent
    while a is not None:
        cls = set(a.classes or ())
        if cls & {"dialog-form", "dialog-box", "context-dialog",
                  "context-viewer", "context-editor-dialog"}:
            return a
        a = a.parent
    return None


class _DialogScrollBase(IsolatedAsyncioTestCase):
    """数据隔离基类:临时 teams_data + 真实 CSS。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        from common.data_layer import save_data
        save_data(_PROFILE_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    async def _mount(self, dialog, w, h):
        app = _make_test_app()
        self._test_ctx = app.run_test(size=(w, h))
        pilot = await self._test_ctx.__aenter__()
        await pilot.app.push_screen(dialog)
        await pilot.pause(0.4)
        return pilot, pilot.app.screen

    async def _unmount(self):
        if getattr(self, "_test_ctx", None) is not None:
            await self._test_ctx.__aexit__(None, None, None)
            self._test_ctx = None

    def _button(self, screen, btn_id):
        return screen.query_one(f"#{btn_id}", Button)


class DialogScrollReachabilityTests(_DialogScrollBase):
    """低高度下底部按钮 scroll_end 后可达;容器可滚动。"""

    async def _assert_reachable(self, factory, btn_id, h, label):
        pilot, screen = await self._mount(factory(), 100, h)
        btn = self._button(screen, btn_id)
        sh = screen.size.height
        form = _form_ancestor(btn)
        self.assertIsNotNone(form, f"{label}: 未找到弹窗根容器")
        # 统一约束:容器 overflow-y=auto(全局 CSS)
        self.assertEqual(form.styles.overflow_y, "auto", f"{label}: 容器应 overflow-y:auto")
        # 内容超出视口 → 必须可滚动;不足 → 按钮本就可见
        r0 = btn.region
        vis0 = r0.y >= 0 and r0.bottom <= sh
        form.scroll_end(animate=False)
        await pilot.pause(0.2)
        r1 = btn.region
        vis1 = r1.y >= 0 and r1.bottom <= sh
        self.assertTrue(
            vis1,
            f"{label} h={h}: 底部按钮 {btn_id} scroll_end 后不可达 "
            f"(before={r0} vis={vis0} after={r1})",
        )
        await self._unmount()

    async def test_each_dialog_reachable_h18(self):
        for label, factory, btn_id in _DIALOGS + _DIALOGS_OWN_CSS:
            with self.subTest(dialog=label):
                await self._assert_reachable(factory, btn_id, 18, label)

    async def test_each_dialog_reachable_h24(self):
        for label, factory, btn_id in _DIALOGS + _DIALOGS_OWN_CSS:
            with self.subTest(dialog=label):
                await self._assert_reachable(factory, btn_id, 24, label)


class DialogWideLayoutUnchangedTests(_DialogScrollBase):
    """宽屏(h=60):按钮天然可见,不强制滚动条(布局不变)。"""

    async def test_wide_height_buttons_visible_no_forced_scroll(self):
        for label, factory, btn_id in _DIALOGS + _DIALOGS_OWN_CSS:
            with self.subTest(dialog=label):
                pilot, screen = await self._mount(factory(), 100, 60)
                btn = self._button(screen, btn_id)
                sh = screen.size.height
                r = btn.region
                self.assertTrue(
                    r.y >= 0 and r.bottom <= sh,
                    f"{label} h=60: 按钮 {btn_id} 不可见 {r} (screen_h={sh})",
                )
                form = _form_ancestor(btn)
                if form is not None:
                    # 宽屏下内容适配视口,不产生溢出滚动(除非内容确实超长)
                    self.assertLessEqual(
                        form.virtual_size.height, form.size.height + 1,
                        f"{label}: 宽屏不应强制滚动条",
                    )
                await self._unmount()


class DialogFocusScrollTests(_DialogScrollBase):
    """低高度下聚焦底部按钮 → 自动滚动到可见(focus/Tab 行为)。"""

    async def test_focus_bottom_button_auto_scrolls_into_view(self):
        pilot, screen = await self._mount(CreateTeamDialog(), 100, 18)
        btn = self._button(screen, "btn_cancel")
        btn.focus()
        await pilot.pause(0.3)
        r = btn.region
        self.assertTrue(
            r.y >= 0 and r.bottom <= screen.size.height,
            f"聚焦 btn_cancel 后应自动滚入视口: {r}",
        )
        await self._unmount()


class KeyboardPageDownReachabilityTests(_DialogScrollBase):
    """最终验收:受影响页面须用 VerticalScroll 或等价可接收键盘/滚轮事件的
    滚动容器 —— 真实 PageDown 键在低高度下必须能把底部按钮滚入视口。

    CSS-only(overflow-y:auto 于普通 Container)被 PageDown 命中的 Input 消费,
    scroll_y 不移动 → 键盘不可达。VerticalScroll 等滚动容器会接收 PageDown。
    本测试挂载每个弹窗后发送 page_down,断言底部按钮进入视口。
    """

    async def _assert_pgdn_reachable(self, factory, btn_id, h, label):
        pilot, screen = await self._mount(factory(), 100, h)
        btn = self._button(screen, btn_id)
        sh = screen.size.height
        r0 = btn.region
        vis0 = r0.y >= 0 and r0.bottom <= sh
        await pilot.press("page_down")
        await pilot.pause(0.25)
        r1 = btn.region
        vis1 = r1.y >= 0 and r1.bottom <= sh
        self.assertTrue(
            vis1,
            f"{label} h={h}: PageDown 后底部按钮 {btn_id} 仍不可达 "
            f"(before={r0} vis={vis0} after={r1} screen_h={sh}) — "
            "需 VerticalScroll 或等价可接收键盘事件的滚动容器",
        )
        await self._unmount()

    async def test_page_down_reachable_h18(self):
        for label, factory, btn_id in _DIALOGS + _DIALOGS_OWN_CSS:
            with self.subTest(dialog=label, h=18):
                await self._assert_pgdn_reachable(factory, btn_id, 18, label)

    async def test_page_down_reachable_h24(self):
        for label, factory, btn_id in _DIALOGS + _DIALOGS_OWN_CSS:
            with self.subTest(dialog=label, h=24):
                await self._assert_pgdn_reachable(factory, btn_id, 24, label)


class ScrollableModalScreenBaseTests(unittest.TestCase):
    """re-base 契约:表单类弹窗继承统一基类;保留既有 CSS 的弹窗不受影响。"""

    def test_form_dialogs_inherit_scrollable_base(self):
        for cls in (CreateTeamDialog, AddMemberDialog, EditMemberDialog,
                    TeamProxyDialog, AgentUserRenameDialog,
                    TeamDefaultAgentUserDialog, NewContextFileDialog):
            self.assertTrue(
                issubclass(cls, ScrollableModalScreen),
                f"{cls.__name__} 应继承 ScrollableModalScreen",
            )

    def test_own_css_dialogs_keep_modal_screen_base(self):
        # AgentUserEdit/Manage 有各自既有滚动 CSS,不强制 re-base(不破坏现有)
        self.assertFalse(issubclass(AgentUserEditDialog, ScrollableModalScreen))
        self.assertFalse(issubclass(AgentUserManageDialog, ScrollableModalScreen))


if __name__ == "__main__":
    unittest.main()
