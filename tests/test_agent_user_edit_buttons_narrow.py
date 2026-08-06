"""
AgentUserEditDialog 保存/取消 按钮 — 窄宽度换行回归测试。
================================================================

背景（补充修复）：
  并行修复已解决 Agent 用户编辑弹窗的纵向溢出（#edit_fields_scroll 独立
  滚动、按钮固定底部）与表单横向自适应（max-width:100%、输入 1fr）。
  但底部 保存/取消 按钮仍是 Horizontal（不换行）：终端宽度 ≤36 列时
  两个 16 宽按钮并排放不下，右侧"取消"被横向裁剪出视口。

  本修复把按钮行改为 Grid(#agent_user_edit_actions) + _reflow_action_buttons：
    - 宽度放得下 → 2 列并排（与 Horizontal 观感一致，右对齐）;
    - 放不下      → 1 列竖排，两个按钮都完整可见。

数据隔离：data_layer.set_data_file 指向临时文件，真实 CSS（TeamManagerApp.CSS）。
"""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import AgentUserEditDialog
from tui.tui_screens import TeamManagerApp

EDIT_BUTTONS = ["btn_save", "btn_cancel"]


def _make_test_app() -> App[None]:
    """最小 App：复用生产真实 CSS（TeamManagerApp.CSS）。"""
    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


def _edit_dialog() -> AgentUserEditDialog:
    """编辑 Claude profile（字段组可见，表单较高，模拟真实编辑场景）。"""
    return AgentUserEditDialog(
        user_key="claude_p",
        agent_type="claude",
        takeover_enabled=True,
        anthropic_api_key="sk-ant-test",
        anthropic_base_url="https://api.anthropic.com",
        anthropic_model="claude-opus-5",
    )


@asynccontextmanager
async def _edit_pilot(width: int, height: int = 34):
    """挂载 AgentUserEditDialog，等字段可见性切换与按钮折行稳定。"""
    app = _make_test_app()
    dialog = _edit_dialog()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        await pilot.pause()
        await pilot.pause(0.3)
        screen = pilot.app.screen
        buttons = {bid: screen.query_one(f"#{bid}", Button) for bid in EDIT_BUTTONS}
        yield pilot, dialog, buttons


def _summary(buttons) -> str:
    return "\n  " + "\n  ".join(
        f"{bid}: region=({b.region.x},{b.region.y},{b.region.width},"
        f"{b.region.height}) visible={b.visible}"
        for bid, b in buttons.items())


class AgentUserEditButtonsNarrowTests(IsolatedAsyncioTestCase):
    """窄宽度下保存/取消按钮完整可见、不横向裁剪、不重叠。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        from common.data_layer import save_data
        save_data({"agent_users": {}, "teams": {}})

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    async def test_narrow_columns_buttons_in_viewport_no_overlap(self):
        """26 / 30 / 34 / 40 列：保存/取消均完整在视口内且两两不重叠。"""
        for w in (26, 30, 34, 40):
            async with _edit_pilot(w) as (_p, _d, buttons):
                for bid, b in buttons.items():
                    self.assertTrue(b.visible,
                                    f"{bid}@{w} 列不可见{_summary(buttons)}")
                    self.assertGreaterEqual(b.region.x, 0,
                                            f"{bid}@{w} 越出左边界{_summary(buttons)}")
                    self.assertLessEqual(b.region.right, w,
                                         f"{bid}@{w} right={b.region.right} 越出右边界"
                                         f"{_summary(buttons)}")
                a, c = buttons["btn_save"].region, buttons["btn_cancel"].region
                self.assertFalse(
                    a.overlaps(c),
                    f"{w} 列下保存/取消重叠{_summary(buttons)}",
                )

    async def test_very_narrow_stacks_to_one_column(self):
        """28 列：放不下两列 → 保存/取消竖排（同 x，y 不同）。"""
        async with _edit_pilot(28) as (_p, _d, buttons):
            s, c = buttons["btn_save"].region, buttons["btn_cancel"].region
            self.assertEqual(s.x, c.x, f"窄宽度应竖排对齐{_summary(buttons)}")
            self.assertNotEqual(s.y, c.y, f"窄宽度应分两行{_summary(buttons)}")

    async def test_wide_enough_stays_two_columns(self):
        """40 列：两个按钮并排（同 y，x 不同，右侧对齐不越界）。"""
        async with _edit_pilot(40) as (_p, _d, buttons):
            s, c = buttons["btn_save"].region, buttons["btn_cancel"].region
            self.assertEqual(s.y, c.y, f"宽宽度应并排{_summary(buttons)}")
            self.assertLess(s.x, c.x, f"保存应在取消左侧{_summary(buttons)}")
            self.assertLessEqual(c.right, 40, f"取消越出视口{_summary(buttons)}")

    async def test_resize_wide_to_narrow_reflows(self):
        """40 → 28 列：resize 后从并排切换为竖排，且仍完整可见。"""
        async with _edit_pilot(40) as (pilot, _d, buttons):
            s0, c0 = buttons["btn_save"].region, buttons["btn_cancel"].region
            self.assertEqual(s0.y, c0.y, "40 列初始应并排")
            await pilot.resize_terminal(28, 34)
            await pilot.pause()
            await pilot.pause(0.3)
            s1, c1 = buttons["btn_save"].region, buttons["btn_cancel"].region
            self.assertEqual(s1.x, c1.x, "resize 到 28 列应竖排")
            self.assertLessEqual(c1.right, 28, "resize 后取消应完整可见")


if __name__ == "__main__":
    unittest.main()
