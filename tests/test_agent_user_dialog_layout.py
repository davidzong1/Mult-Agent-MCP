"""
Agent 用户管理/编辑界面 布局回归测试。
================================================================

背景：
  1. AgentUserManageDialog 的操作按钮区（编辑/删除等）在常规终端宽度下
     由于 dialog 固定 width:60 而折行（3+2），第二行右侧留空，视觉上
     "右侧色块缺失/错位"。修复：.agent-user-manage-form 加宽到 width:88，
     使 ≥84 列终端下 5 个操作按钮排成一行（按钮颜色块完整、对齐）。
  2. AgentUserEditDialog 表单字段较多，短终端下保存/关闭按钮被内容挤出
     视口。修复：字段区放入可滚动容器（#edit_fields_scroll, height:1fr），
     按钮行固定在底部始终可达。

本文件用真实 CSS（TeamManagerApp.CSS）挂载对话框，数据经 data_layer
set_data_file 隔离，绝不触碰真实 teams_data.json。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.containers import VerticalScroll
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import AgentUserEditDialog, AgentUserManageDialog
from tui.tui_screens import TeamManagerApp

BUTTON_IDS = ["btn_new", "btn_edit", "btn_rename", "btn_delete", "btn_close"]
EDIT_BUTTON_IDS = ["btn_save", "btn_cancel"]

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
    """最小 App：复用生产真实 CSS（TeamManagerApp.CSS）。"""
    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


class _IsolatedDataTestCase(IsolatedAsyncioTestCase):
    """数据隔离基类：临时 teams_data.json + 真实 CSS。"""

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

    def _buttons(self, screen, ids):
        return {bid: screen.query_one(f"#{bid}", Button) for bid in ids}


class AgentUserManageSingleRowTests(_IsolatedDataTestCase):
    """操作按钮区：常规宽度下 5 按钮单行排列（修复右侧色块缺失/错位）。"""

    async def test_wide_terminal_all_action_buttons_on_one_row(self):
        """100 列 → 5 个操作按钮在同一行（无第二行右侧留空）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            buttons = self._buttons(pilot.app.screen, BUTTON_IDS)
            rows = sorted({b.region.y for b in buttons.values()})
            self.assertEqual(
                len(rows), 1,
                f"100 列下 5 个操作按钮应排成一行，实际行分布 y={rows}\n"
                + self._summary(buttons),
            )

    async def test_wide_terminal_buttons_no_overlap_and_in_viewport(self):
        """100 列 → 按钮两两不重叠、全部在视口内（删除/关闭可见可点）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            buttons = self._buttons(pilot.app.screen, BUTTON_IDS)
            problems = []
            for i in range(len(BUTTON_IDS)):
                for j in range(i + 1, len(BUTTON_IDS)):
                    if buttons[BUTTON_IDS[i]].region.overlaps(buttons[BUTTON_IDS[j]].region):
                        problems.append(f"{BUTTON_IDS[i]} 与 {BUTTON_IDS[j]} 重叠")
            self.assertFalse(problems, self._summary(buttons))
            w, h = 100, 34
            for bid in BUTTON_IDS:
                r = buttons[bid].region
                self.assertTrue(buttons[bid].visible, f"{bid} 不可见")
                self.assertLessEqual(r.right, w,
                                     f"{bid} 越出右边界: {self._summary(buttons)}")
                self.assertLessEqual(r.bottom, h,
                                     f"{bid} 越出下边界: {self._summary(buttons)}")

    async def test_wide_terminal_delete_button_color_block_visible(self):
        """100 列 → 删除（error 红色块）按钮完整可见、位于右侧操作区。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            delete = pilot.app.screen.query_one("#btn_delete", Button)
            self.assertTrue(delete.visible, "删除按钮应可见")
            # 单行时删除按钮与其它按钮同高、宽度完整（≥ 其标签所需）
            self.assertGreaterEqual(delete.region.width, 10,
                                    f"删除按钮宽度不足，色块被裁: {self._summary(
                                        self._buttons(pilot.app.screen, BUTTON_IDS))}")

    def _summary(self, buttons):
        return "\n  " + "\n  ".join(
            f"{bid}: region=({b.region.x},{b.region.y},{b.region.width},"
            f"{b.region.height}) visible={b.visible} label={b.label!r}"
            for bid, b in buttons.items())


class AgentUserEditDialogScrollTests(_IsolatedDataTestCase):
    """编辑界面：内容过高时按钮仍可达（可滚动字段区 + 固定按钮行）。"""

    async def test_short_terminal_save_cancel_still_in_viewport(self):
        """(80, 20) → 保存/关闭按钮完整位于视口内（不被内容遮挡）。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog()
        async with app.run_test(size=(80, 20)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            buttons = self._buttons(pilot.app.screen, EDIT_BUTTON_IDS)
            w, h = 80, 20
            for bid in EDIT_BUTTON_IDS:
                r = buttons[bid].region
                self.assertTrue(buttons[bid].visible,
                                f"{bid} 不可见: {self._summary(buttons)}")
                self.assertGreaterEqual(r.height, 3,
                                        f"{bid} 高度不足被纵向裁剪")
                self.assertGreaterEqual(r.y, 0,
                                        f"{bid} 越出上边界")
                self.assertLessEqual(r.bottom, h,
                                     f"{bid} 越出下边界（被内容挤出视口）: "
                                     f"bottom={r.bottom} h={h} "
                                     f"{self._summary(buttons)}")

    async def test_very_short_terminal_buttons_still_reachable(self):
        """(80, 16) → 按钮仍在视口内（字段区滚动、按钮固定底部）。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog()
        async with app.run_test(size=(80, 16)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            buttons = self._buttons(pilot.app.screen, EDIT_BUTTON_IDS)
            h = 16
            for bid in EDIT_BUTTON_IDS:
                r = buttons[bid].region
                self.assertTrue(buttons[bid].visible, f"{bid} 不可见")
                self.assertLessEqual(r.bottom, h,
                                     f"{bid} 在 {h} 行终端被挤出视口")
                self.assertGreaterEqual(r.y, 0)

    async def test_fields_are_scrollable_when_content_overflows(self):
        """(80, 16) → 字段区为可滚动容器且内容确实溢出（可滚动）。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog()
        async with app.run_test(size=(80, 16)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            scroll = pilot.app.screen.query_one("#edit_fields_scroll", VerticalScroll)
            self.assertIsInstance(scroll, VerticalScroll,
                                  "字段区应为可滚动容器")
            self.assertGreater(
                scroll.max_scroll_y, 0,
                "内容溢出时字段区应可纵向滚动（否则按钮会被挤出视口）",
            )
            # 按钮在滚动容器之外（固定底部），与滚动区独立
            save_btn = pilot.app.screen.query_one("#btn_save", Button)
            self.assertTrue(
                save_btn.region.y >= scroll.region.bottom - 1,
                f"保存按钮应在滚动字段区下方固定可见: save.y={save_btn.region.y} "
                f"scroll.bottom={scroll.region.bottom}",
            )

    async def test_tall_terminal_no_need_scroll_and_buttons_at_bottom(self):
        """(80, 30) 新建路径（无 provider 字段组）→ 字段全部可见、无滚动；
        按钮在底部可达（无回归）。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            scroll = pilot.app.screen.query_one("#edit_fields_scroll", VerticalScroll)
            self.assertEqual(
                scroll.max_scroll_y, 0,
                "高度充足且字段较少时字段区不应滚动",
            )
            buttons = self._buttons(pilot.app.screen, EDIT_BUTTON_IDS)
            for bid in EDIT_BUTTON_IDS:
                self.assertTrue(buttons[bid].visible, f"{bid} 不可见")
                self.assertLessEqual(buttons[bid].region.bottom, 30,
                                     f"{bid} 应在视口内")

    def _summary(self, buttons):
        return "\n  " + "\n  ".join(
            f"{bid}: region=({b.region.x},{b.region.y},{b.region.width},"
            f"{b.region.height}) visible={b.visible} label={b.label!r}"
            for bid, b in buttons.items())


if __name__ == "__main__":
    unittest.main()
