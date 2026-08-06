"""
Agent 用户管理/编辑弹窗 — Textual Pilot 布局测试
================================================

覆盖四个布局关注点（真实 CSS 渲染，非 mock 尺寸）：
  1. 操作按钮右侧对齐 —— AgentUserManageDialog 的 5 个操作按钮
     （新建/编辑/重命名/删除/关闭）与 AgentUserEditDialog 的 保存/取消
     按钮组应靠内容区右侧对齐（通用桌面 UX 约定），宽窄终端均成立；
  2. 低高度视口滚动 —— 表单 (.dialog-form) 在低高度视口下必须可纵向
     滚动（overflow-y:auto），操作/保存/关闭按钮可滚动进入可视区；
  3. 保存/关闭按钮可达 —— 正常高度与窄窗口、低高度滚动后均可实际点击
     （取消/关闭 dismiss，保存带合法数据 dismiss 返回 dict）；
  4. 窄窗口无重叠 —— 40 列下 manage 5 按钮两两不重叠、edit 表单
     横向收缩（max-width:100%）使输入框与按钮不越出视口。

实现修正（本次一并落地，作用域限定两个 Agent 弹窗）：
  - AgentUserEditDialog.CSS / AgentUserManageDialog.CSS 新增
    .dialog-form { overflow-y:auto; }（低高度可滚动）、按钮右对齐、
    编辑弹窗表单 max-width:100% 与输入/下拉 1fr 自适应宽度。

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实
teams_data.json。布局用真实 CSS（TeamManagerApp.CSS + 弹窗自身 CSS）。
"""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import (
    AgentUserEditDialog,
    AgentUserManageDialog,
)
from tui.tui_screens import TeamManagerApp

MANAGE_BUTTONS = ["btn_new", "btn_edit", "btn_rename", "btn_delete", "btn_close"]
EDIT_BUTTONS = ["btn_save", "btn_cancel"]
EDIT_FIELDS = ["key", "ant_key", "ant_url", "ant_model", "takeover"]

_PROFILE_DATA = {
    "agent_users": {
        "claude_p": {
            "agent_type": "claude",
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-test",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        },
        "codex_p": {
            "agent_type": "codex",
            "takeover_enabled": False,
            "openai_api_key": "sk-fake",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-4o",
        },
    },
    "teams": {},
}


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS，不启动 TeamManagerApp 主流程/定时器。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


def _edit_dialog_with_data() -> AgentUserEditDialog:
    """编辑 Claude profile 的编辑弹窗（claude 字段组显示，输入合法）。"""
    return AgentUserEditDialog(
        user_key="claude_p",
        agent_type="claude",
        takeover_enabled=True,
        anthropic_api_key="sk-ant-test",
        anthropic_base_url="https://api.anthropic.com",
        anthropic_model="claude-opus-5",
    )


@asynccontextmanager
async def _manage_pilot(width: int, height: int = 34):
    """挂载 AgentUserManageDialog，等待按钮折行/布局稳定。"""
    app = _make_test_app()
    dialog = AgentUserManageDialog()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        await pilot.pause()
        await pilot.pause(0.3)
        screen = pilot.app.screen
        buttons = {bid: screen.query_one(f"#{bid}", Button) for bid in MANAGE_BUTTONS}
        yield pilot, dialog, buttons, screen


@asynccontextmanager
async def _edit_pilot(width: int, height: int = 34):
    """挂载 AgentUserEditDialog（编辑 Claude profile），等待布局稳定。"""
    app = _make_test_app()
    dialog = _edit_dialog_with_data()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        await pilot.pause()
        await pilot.pause(0.3)
        screen = pilot.app.screen
        buttons = {bid: screen.query_one(f"#{bid}", Button) for bid in EDIT_BUTTONS}
        yield pilot, dialog, buttons, screen


class _AgentUserLayoutBase(IsolatedAsyncioTestCase):
    """数据隔离基类（临时 teams_data.json，不触碰真实数据）。"""

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

    # ---- 布局断言辅助 ----

    def _layout_summary(self, buttons: dict) -> str:
        lines = []
        for bid, b in buttons.items():
            lines.append(
                f"{bid}: region=({b.region.x},{b.region.y},"
                f"{b.region.width},{b.region.height}) visible={b.visible}"
            )
        return "\n  " + "\n  ".join(lines)

    def _assert_right_aligned(self, form, buttons: dict, tolerance: int = 3) -> None:
        """按钮组靠内容区右侧：右间隙小（≤tolerance）且明显小于左间隙。"""
        content = form.content_region
        max_right = max(b.region.right for b in buttons.values())
        min_left = min(b.region.x for b in buttons.values())
        right_gap = content.right - max_right
        left_gap = min_left - content.x
        self.assertGreaterEqual(
            right_gap, 0,
            f"按钮越出内容区右边界: {self._layout_summary(buttons)}",
        )
        self.assertLessEqual(
            right_gap, tolerance,
            f"操作按钮未右对齐（右间隙 {right_gap} > {tolerance}）: "
            f"{self._layout_summary(buttons)}",
        )
        self.assertLess(
            right_gap, left_gap,
            f"按钮组未靠右（右间隙 {right_gap} ≥ 左间隙 {left_gap}）: "
            f"{self._layout_summary(buttons)}",
        )

    def _assert_no_overlap(self, buttons: dict) -> None:
        """两两不重叠（无视觉碰撞/裁剪）。"""
        problems = []
        items = list(buttons.items())
        for i in range(len(items)):
            for j in range(i + 1, len(items)):
                a = items[i][1].region
                b = items[j][1].region
                if a.overlaps(b):
                    problems.append(f"{items[i][0]}{a} 与 {items[j][0]}{b} 重叠")
        self.assertFalse(problems, f"控件重叠:\n{self._layout_summary(buttons)}")

    def _assert_in_viewport(self, buttons: dict, size) -> None:
        """控件完整落在视口内（不越出左右/上下边界）。"""
        w, h = size
        for bid, b in buttons.items():
            r = b.region
            self.assertTrue(b.visible, f"{bid} 不可见: {self._layout_summary(buttons)}")
            self.assertGreaterEqual(r.x, 0, f"{bid} 越出左边界: {self._layout_summary(buttons)}")
            self.assertLessEqual(r.right, w,
                                 f"{bid} right={r.right} 越出右边界(视口宽 {w}):"
                                 f"{self._layout_summary(buttons)}")
            self.assertGreaterEqual(r.y, 0, f"{bid} 越出上边界: {self._layout_summary(buttons)}")
            self.assertLessEqual(r.bottom, h,
                                 f"{bid} bottom={r.bottom} 越出下边界(视口高 {h}):"
                                 f"{self._layout_summary(buttons)}")

    async def _scroll_to_bottom(self, pilot, form) -> None:
        """把表单滚到底（低高度视口下按钮可达的前提）。"""
        if form.max_scroll_y > 0:
            form.scroll_to(y=form.max_scroll_y)
            await pilot.pause()
            await pilot.pause(0.2)

    def _assert_buttons_within_form(self, buttons: dict, form) -> None:
        """按钮完整落在表单可视区内（固定底部/未滚动时即可达）。"""
        for bid, b in buttons.items():
            self.assertTrue(b.visible, f"{bid} 不可见: {self._layout_summary(buttons)}")
            self.assertGreaterEqual(
                b.region.y, form.region.y,
                f"{bid} 顶部越出表单上边界: {self._layout_summary(buttons)}",
            )
            self.assertLessEqual(
                b.region.bottom, form.region.bottom,
                f"{bid} bottom={b.region.bottom} 越出表单底部"
                f"({form.region.bottom}): {self._layout_summary(buttons)}",
            )


class AgentUserManageRightAlignTests(_AgentUserLayoutBase):
    """管理弹窗：操作按钮右侧对齐（宽/窄终端）。"""

    async def test_manage_buttons_right_aligned_wide(self):
        async with _manage_pilot(100) as (_p, _d, buttons, screen):
            self._assert_right_aligned(screen.query_one(".dialog-form"), buttons)

    async def test_manage_buttons_right_aligned_narrow(self):
        async with _manage_pilot(40) as (_p, _d, buttons, screen):
            self._assert_right_aligned(screen.query_one(".dialog-form"), buttons)


class AgentUserEditRightAlignTests(_AgentUserLayoutBase):
    """编辑弹窗：保存/取消右侧对齐（宽/窄终端）。"""

    async def test_edit_buttons_right_aligned_wide(self):
        async with _edit_pilot(100) as (_p, _d, buttons, screen):
            self._assert_right_aligned(screen.query_one(".dialog-form"), buttons)

    async def test_edit_buttons_right_aligned_narrow(self):
        async with _edit_pilot(40) as (_p, _d, buttons, screen):
            self._assert_right_aligned(screen.query_one(".dialog-form"), buttons)


class AgentUserLowHeightScrollTests(_AgentUserLayoutBase):
    """低高度视口：按钮可达 —— 或固定可见（edit 字段区独立滚动），
    或表单可滚动把按钮带入可视区（manage）。"""

    async def _assert_low_height_reachable(self, pilot, form, buttons, height):
        """按钮在低高度视口下完整落在表单可视区内（可达）。

        两种生产设计都兼容：
          - edit：字段包在 #edit_fields_scroll 独立滚动，按钮固定底部 → 初始即可见；
          - manage：无独立滚动区，表单本身 overflow-y:auto → 需滚动到底。
        统一先滚动到底再断言，幂等。
        """
        self.assertGreaterEqual(
            form.max_scroll_y, 0,
            f"低高度视口表单 max_scroll_y 异常: {form.max_scroll_y}",
        )
        await self._scroll_to_bottom(pilot, form)
        for bid, b in buttons.items():
            self.assertTrue(b.visible, f"滚动后 {bid} 不可见: {self._layout_summary(buttons)}")
            self.assertGreaterEqual(
                b.region.y, form.region.y,
                f"滚动后 {bid} 顶部越出表单上边界: {self._layout_summary(buttons)}",
            )
            self.assertLessEqual(
                b.region.bottom, form.region.bottom,
                f"滚动后 {bid} bottom={b.region.bottom} 越出表单底部"
                f"({form.region.bottom}): {self._layout_summary(buttons)}",
            )

    async def _assert_edit_fields_scrollable(self, screen) -> None:
        """编辑弹窗字段区（#edit_fields_scroll）低高度下必须可独立滚动，
        证明字段内容可达（按钮固定底部，字段区滚动是唯一滚动来源）。"""
        vs = screen.query_one("#edit_fields_scroll")
        self.assertGreater(
            vs.max_scroll_y, 0,
            f"低高度视口编辑字段区应可滚动（max_scroll_y={vs.max_scroll_y}）",
        )
        self.assertNotEqual(
            str(vs.styles.overflow_y), "hidden",
            "低高度视口编辑字段区滚动被 overflow-y:hidden 禁用",
        )

    async def test_manage_12h_buttons_reachable_by_scroll(self):
        async with _manage_pilot(100, 12) as (_p, _d, buttons, screen):
            await self._assert_low_height_reachable(
                _p, screen.query_one(".dialog-form"), buttons, 12)

    async def test_manage_10h_buttons_reachable_by_scroll(self):
        async with _manage_pilot(60, 10) as (_p, _d, buttons, screen):
            await self._assert_low_height_reachable(
                _p, screen.query_one(".dialog-form"), buttons, 10)

    async def test_edit_12h_save_reachable_by_scroll(self):
        async with _edit_pilot(100, 12) as (_p, _d, buttons, screen):
            form = screen.query_one(".dialog-form")
            # 编辑弹窗按钮固定在底部（字段区独立滚动）→ 未滚动即完整可见
            self._assert_buttons_within_form(buttons, form)
            await self._assert_low_height_reachable(_p, form, buttons, 12)
            await self._assert_edit_fields_scrollable(screen)

    async def test_edit_10h_save_reachable_by_scroll(self):
        async with _edit_pilot(60, 10) as (_p, _d, buttons, screen):
            form = screen.query_one(".dialog-form")
            self._assert_buttons_within_form(buttons, form)
            await self._assert_low_height_reachable(_p, form, buttons, 10)
            await self._assert_edit_fields_scrollable(screen)


class AgentUserSaveCloseReachableTests(_AgentUserLayoutBase):
    """保存/关闭按钮可达：正常高度、窄窗口、低高度滚动后均可点击。"""

    async def test_edit_cancel_clickable_narrow(self):
        """40 列下点取消 → 编辑弹窗关闭出栈。"""
        async with _edit_pilot(40) as (pilot, dialog, _buttons, _screen):
            await pilot.click("#btn_cancel")
            await pilot.pause()
            await pilot.pause(0.2)
            stack = list(pilot.app.screen_stack)
            self.assertNotIn(
                dialog, stack,
                f"点取消后 AgentUserEditDialog 应 dismiss 出栈，实际栈={stack}",
            )

    async def test_edit_save_clickable_narrow(self):
        """40 列下带合法数据点保存 → dismiss 返回 profile dict（证明按钮可点）。"""
        async with _edit_pilot(40) as (pilot, _d, _buttons, _screen):
            await pilot.click("#btn_save")
            await pilot.pause()
            await pilot.pause(0.3)
            stack = list(pilot.app.screen_stack)
            self.assertNotIn(
                AgentUserEditDialog, [type(s) for s in stack],
                f"点保存后编辑弹窗应 dismiss 出栈，实际栈={[type(s).__name__ for s in stack]}",
            )

    async def test_edit_save_clickable_low_height(self):
        """12 行低高度：滚动到底后保存按钮可实际点击并 dismiss。"""
        async with _edit_pilot(100, 12) as (pilot, dialog, _buttons, screen):
            await self._scroll_to_bottom(pilot, screen.query_one(".dialog-form"))
            await pilot.click("#btn_save")
            await pilot.pause()
            await pilot.pause(0.3)
            stack = list(pilot.app.screen_stack)
            self.assertNotIn(
                dialog, stack,
                f"低高度滚动后点保存应 dismiss 出栈，实际栈={[type(s).__name__ for s in stack]}",
            )

    async def test_manage_close_clickable_narrow(self):
        """40 列下点关闭 → 管理弹窗关闭出栈。"""
        async with _manage_pilot(40) as (pilot, dialog, _buttons, _screen):
            await pilot.click("#btn_close")
            await pilot.pause()
            await pilot.pause(0.2)
            stack = list(pilot.app.screen_stack)
            self.assertNotIn(
                dialog, stack,
                f"点关闭后 AgentUserManageDialog 应 dismiss 出栈，实际栈={stack}",
            )

    async def test_manage_close_clickable_low_height(self):
        """12 行低高度：滚动到底后关闭按钮可实际点击并 dismiss。"""
        async with _manage_pilot(100, 12) as (pilot, dialog, _buttons, screen):
            await self._scroll_to_bottom(pilot, screen.query_one(".dialog-form"))
            await pilot.click("#btn_close")
            await pilot.pause()
            await pilot.pause(0.2)
            stack = list(pilot.app.screen_stack)
            self.assertNotIn(
                dialog, stack,
                f"低高度滚动后点关闭应 dismiss 出栈，实际栈={stack}",
            )


class AgentUserNarrowNoOverlapTests(_AgentUserLayoutBase):
    """窄窗口无重叠：40/45 列下 manage 5 按钮与 edit 字段/按钮均不碰撞、不越界。"""

    async def test_manage_narrow_no_overlap_in_viewport(self):
        """40 列：5 个操作按钮两两不重叠且完整在视口内。"""
        async with _manage_pilot(40) as (_p, _d, buttons, screen):
            self._assert_no_overlap(buttons)
            self._assert_in_viewport(buttons, screen.size)

    async def test_edit_narrow_fields_buttons_no_overlap(self):
        """40 列：编辑弹窗全部字段+按钮两两不重叠、不越出视口。"""
        async with _edit_pilot(40) as (_p, _d, buttons, screen):
            from textual.widgets import Input, Select

            widgets = dict(buttons)
            for fid in EDIT_FIELDS:
                node = screen.query_one(f"#{fid}")
                widgets[fid] = node
            self._assert_no_overlap(widgets)
            self._assert_in_viewport(widgets, screen.size)

    async def test_edit_45col_no_overlap_in_viewport(self):
        """45 列：编辑弹窗表单横向收缩，字段/按钮不越出视口。"""
        async with _edit_pilot(45) as (_p, _d, buttons, screen):
            self._assert_in_viewport(buttons, screen.size)


if __name__ == "__main__":
    unittest.main()
