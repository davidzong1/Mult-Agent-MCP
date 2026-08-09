"""
#mcp_config_status 嵌套滚动缺口 —— 隔离 UI 回归（最小补漏）
================================================================

覆盖清单②：AgentMcpConfigDialog 的 `#mcp_config_status`（max-height:18、
overflow-y:hidden）在团队数量多、状态行超过 18 行时，行内容被裁剪且自身
无法滚动（清单②"嵌套滚动缺口"）。外层 `.dialog-form` 已统一 max-height:100%
+ overflow-y:auto（coder 主实现），故底部主按钮仍可达——本测试钉住这个兜底
行为，避免未来外层滚动被误删后缺口扩大为"按钮不可达"。

钉住的行为契约
----------------
1. 大量团队（25 个）时 `#mcp_config_status` 行内容超过 max-height:18 →
   该容器自身 max_scroll_y>0 但 overflow-y:hidden（裁剪、不可内部滚动）——
   这是已知缺口，钉住现状；若未来补 overflow-y:auto 需更新本断言。
2. 低视口（h=18/24）下 AgentMcpConfigDialog 底部主按钮 btn_config_all
   仍完整位于视口内（外层 .dialog-form 滚动兜底）——不因缺口导致按钮不可达。
3. 高视口（h=67）下按钮完整可见（无回归）。

隔离性
--------
- data_layer.set_data_file → temp teams_data；绝不触碰真实 teams_data.json。
- _codex_mcp_configured / _claude_mcp_configured mock（避免读真实 MCP 配置）。
- 不发起 tmux / MCP daemon 外部进程调用。
"""

import sys
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

from textual.app import App
from textual.containers import Vertical
from textual.widgets import Button, Label

from common import data_layer
from common.data_layer import save_data
from tui.tui_dialogs import AgentMcpConfigDialog
from tui.tui_screens import TeamManagerApp

MANY_TEAMS = {f"t{i:02d}": {"members": {}} for i in range(25)}


def _make_test_app() -> App[None]:
    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


def _in_viewport(region, viewport_height: int) -> bool:
    return region.y >= 0 and region.bottom <= viewport_height


class McpConfigStatusNestedScrollTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data({"teams": MANY_TEAMS})

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    @asynccontextmanager
    async def _open_dialog(self, size):
        app = _make_test_app()
        async with app.run_test(size=size) as pilot:
            with (
                mock.patch("tui.tui_dialogs._codex_mcp_configured", return_value=True),
                mock.patch("tui.tui_dialogs._claude_mcp_configured", return_value=True),
            ):
                dialog = AgentMcpConfigDialog()
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)
            yield pilot, dialog

    # ---------- 1. 嵌套滚动缺口已补：状态列表内部可滚动 ----------

    async def test_mcp_config_status_internally_scrollable(self):
        """25 团队时 #mcp_config_status 行内容超过 max-height:18 →
        容器 max_scroll_y>0 且 overflow-y:auto（内部可滚动，不再裁剪）。"""
        async with self._open_dialog((100, 24)) as (pilot, dialog):
            status = dialog.query_one("#mcp_config_status", Vertical)
            row_count = len(status.query(Label))
            self.assertGreater(row_count, 18, "前置条件：状态行应超过 max-height:18")
            # max_height 为 Scalar(18.0 cells)（Textual 计算值）
            mh = status.styles.max_height
            self.assertEqual(float(mh.value), 18.0, "max-height 应为 18 行")
            self.assertEqual(str(status.styles.overflow_y), "auto",
                             "状态列表应 overflow-y:auto（内部可滚动，不裁剪）")
            self.assertGreater(getattr(status, "max_scroll_y", 0) or 0, 0,
                               "行内容应产生溢出量")
            # 实际内部滚动生效：scroll_end 后 scroll_y 变化
            before = status.scroll_y
            status.scroll_end(animate=False)
            await pilot.pause(0.2)
            self.assertGreater(status.scroll_y, before,
                               "状态列表 scroll_end 后应内部滚动（缺口已补）")

    # ---------- 2. 外层 .dialog-form 兜底：低视口按钮仍可达 ----------

    async def test_low_height_primary_button_reachable_via_outer_scroll(self):
        """低视口（18/24 行）：即使 #mcp_config_status 内部裁剪，
        外层 .dialog-form 滚动兜底 → 底部主按钮完整可见、可聚焦、可点击。"""
        for height in (18, 24):
            with self.subTest(viewport_height=height):
                async with self._open_dialog((100, height)) as (pilot, dialog):
                    btn = dialog.query_one("#btn_config_all", Button)
                    self.assertGreaterEqual(btn.region.y, 0)
                    self.assertLessEqual(
                        btn.region.bottom, height,
                        f"低视口 {height} 行下主按钮被挤出行外（外层兜底滚动失效）: {btn.region}",
                    )
                    self.assertTrue(btn.can_focus)
                    btn.focus()
                    await pilot.pause(0.1)
                    self.assertIs(pilot.app.screen.focused, btn)

    # ---------- 3. 高视口无回归 ----------

    async def test_wide_height_button_fully_visible(self):
        """高视口（67 行 ≈ 1920x1080）：主按钮完整可见（布局无回归）。"""
        async with self._open_dialog((120, 67)) as (pilot, dialog):
            btn = dialog.query_one("#btn_config_all", Button)
            self.assertTrue(_in_viewport(btn.region, 67), f"按钮越出视口: {btn.region}")


if __name__ == "__main__":
    unittest.main()
