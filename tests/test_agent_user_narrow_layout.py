"""
Agent 用户管理界面 — 窄终端宽度操作按钮换行/可见性回归测试。
================================================================

背景（task4 后续回归）：
  AgentUserManageDialog 的操作按钮行在窄终端下可能横向裁剪（删除/关闭
  按钮被挤出视口）。生产实现：5 个操作按钮始终是同一 Grid 的 children，
  宽度足够 → 单列组（宽屏单行）；不足 → 通过 _reflow_action_buttons 减小
  grid_size_columns 让 Grid 自动生成多行，保证删除等按钮可见、可点击；
  on_resize 时重新计算列数。

本文件覆盖（重点 Textual Pilot）：
  1. 40 / 60 / 100 列下 mount AgentUserManageDialog：
     - 5 个按钮区域两两不重叠（无视觉碰撞）；
     - 删除、关闭按钮在视口内（region 完全在屏幕范围内）且可见；
     - 删除可点击（触发确认框）、关闭可点击（关闭对话框）。
  2. resize 后重新合并 / 拆行：40→100 行数减少（合并）、100→40 行数增加
     （拆行），且重排后仍无重叠、仍在视口内。

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实
teams_data.json。布局用真实 CSS（TeamManagerApp.CSS，含 .dialog-form
width:60 / #agent_user_actions layout:grid）忠实复现生产渲染。
"""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App

from common import data_layer
from tui.tui_dialogs import (
    AgentUserManageDialog,
    ConfirmBox,
)
from tui.tui_screens import TeamManagerApp

BUTTON_IDS = ["btn_new", "btn_edit", "btn_rename", "btn_delete", "btn_close"]

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
        "gpt_p": {
            "agent_type": "codex",
            "takeover_enabled": True,
            "openai_api_key": "sk-fake2",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-5",
        },
    },
    "teams": {},
}


def _layout_summary(buttons: dict) -> str:
    """可读的按钮布局摘要，用于失败报告（region/宽度数据）。"""
    lines = []
    for bid in BUTTON_IDS:
        b = buttons[bid]
        lines.append(f"{bid}: region=({b.region.x},{b.region.y},"
                     f"{b.region.width},{b.region.height}) "
                     f"visible={b.visible} label={b.label!r}")
    rows = sorted({b.region.y for b in buttons.values()})
    lines.append(f"distinct rows(y): {rows}")
    return "\n  " + "\n  ".join(lines)


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS（.dialog-form width:60 等），
    不启动 TeamManagerApp 主流程/定时器。"""
    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


@asynccontextmanager
async def _dialog_pilot(width: int, height: int = 34):
    """用真实 CSS 挂载 AgentUserManageDialog，yield 后保持驱动存活。

    run_test 是 asynccontextmanager：必须在块内完成全部交互（点击/resize）。
    yield (pilot, dialog, buttons, screen_size)。
    """
    from textual.widgets import Button

    app = _make_test_app()
    dialog = AgentUserManageDialog()
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        # 等 on_mount → call_after_refresh(_reflow_action_buttons) 完成重排
        await pilot.pause()
        await pilot.pause(0.3)
        screen = pilot.app.screen
        buttons = {
            bid: screen.query_one(f"#{bid}", Button)
            for bid in BUTTON_IDS
        }
        yield pilot, dialog, buttons, screen.size


class _NarrowLayoutBase(IsolatedAsyncioTestCase):
    """数据隔离 + 挂载 AgentUserManageDialog 的共享基类。"""

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

    def _assert_no_overlap(self, buttons) -> None:
        """5 个按钮区域两两不重叠（无视觉碰撞/裁剪）。"""
        problems = []
        for i in range(len(BUTTON_IDS)):
            for j in range(i + 1, len(BUTTON_IDS)):
                a = buttons[BUTTON_IDS[i]].region
                b = buttons[BUTTON_IDS[j]].region
                if a.overlaps(b):
                    problems.append(f"{BUTTON_IDS[i]}{a} 与 {BUTTON_IDS[j]}{b} 重叠")
        self.assertFalse(
            problems,
            f"列按钮重叠:\n{_layout_summary(buttons)}",
        )

    def _assert_buttons_in_viewport(self, buttons, screen_size) -> None:
        """所有操作按钮均完整可见，且保留 Button 的正常三行高度。"""
        w, h = screen_size
        for bid in BUTTON_IDS:
            r = buttons[bid].region
            self.assertTrue(buttons[bid].visible,
                            f"{bid} 不可见: {_layout_summary(buttons)}")
            self.assertGreaterEqual(
                r.height, 3,
                f"{bid} 高度不足，按钮被纵向裁剪: {_layout_summary(buttons)}",
            )
            self.assertGreaterEqual(r.x, 0,
                                    f"{bid} x 越出左边界: {_layout_summary(buttons)}")
            self.assertLessEqual(r.right, w,
                                 f"{bid} right={r.right} 越出右边界(视口宽 {w}):"
                                 f"{_layout_summary(buttons)}")
            self.assertGreaterEqual(r.y, 0,
                                    f"{bid} y 越出上边界: {_layout_summary(buttons)}")
            self.assertLessEqual(r.bottom, h,
                                 f"{bid} bottom={r.bottom} 越出下边界(视口高 {h}):"
                                 f"{_layout_summary(buttons)}")
        # 纵向完整性：action Grid 的 region 必须覆盖所有按钮行的底部，
        # 否则多行折行时最后一行会被容器纵向裁剪（生产 CSS 曾用 grid-rows:1）。
        from textual.containers import Grid
        grid = buttons[BUTTON_IDS[0]].app.screen.query_one(
            "#agent_user_actions", Grid)
        max_bottom = max(b.region.bottom for b in buttons.values())
        self.assertGreaterEqual(
            grid.region.bottom, max_bottom,
            f"action Grid 底部({grid.region.bottom}) 未覆盖最后一行按钮底部"
            f"({max_bottom}): {_layout_summary(buttons)}",
        )

    def _rows(self, buttons) -> list[int]:
        """各按钮所在行 y（去重）。"""
        return sorted({b.region.y for b in buttons.values()})


class AgentUserManageNarrowLayoutTests(_NarrowLayoutBase):
    """40/60/100 列：5 按钮不重叠、删除/关闭在视口内。"""

    async def test_40_col_no_overlap_delete_close_in_viewport(self):
        async with _dialog_pilot(40) as (_p, _d, buttons, size):
            self._assert_no_overlap(buttons)
            self._assert_buttons_in_viewport(buttons, size)

    async def test_60_col_no_overlap_delete_close_in_viewport(self):
        async with _dialog_pilot(60) as (_p, _d, buttons, size):
            self._assert_no_overlap(buttons)
            self._assert_buttons_in_viewport(buttons, size)

    async def test_100_col_no_overlap_delete_close_in_viewport(self):
        async with _dialog_pilot(100) as (_p, _d, buttons, size):
            self._assert_no_overlap(buttons)
            self._assert_buttons_in_viewport(buttons, size)


class AgentUserManageClickableTests(_NarrowLayoutBase):
    """窄宽度（40 列）下删除/关闭可实际点击。"""

    async def test_delete_clickable_opens_confirm(self):
        """40 列下点删除 → 弹出确认框（证明按钮真正可点，非被裁剪）。"""
        async with _dialog_pilot(40) as (pilot, _d, buttons, _size):
            await pilot.click(buttons["btn_delete"])
            await pilot.pause()
            await pilot.pause(0.3)

            self.assertIsInstance(
                pilot.app.screen, ConfirmBox,
                f"点删除后应弹出 ConfirmBox，实际 screen={type(pilot.app.screen)}",
            )
            # 关闭确认框，避免残留
            await pilot.click("#btn_no")
            await pilot.pause(0.2)

    async def test_close_clickable_dismisses(self):
        """40 列下点关闭 → 对话框关闭（从 screen 栈弹出）。"""
        async with _dialog_pilot(40) as (pilot, dialog, buttons, _size):
            await pilot.click(buttons["btn_close"])
            await pilot.pause()
            await pilot.pause(0.2)

            stack = list(pilot.app.screen_stack)
            self.assertNotIn(
                dialog, stack,
                f"点关闭后 AgentUserManageDialog 应被 dismiss 出栈，实际栈={stack}",
            )


class AgentUserManageResizeTests(_NarrowLayoutBase):
    """resize 后重新合并 / 拆行，且重排后仍无重叠、删除/关闭仍可见。"""

    async def test_resize_40_to_100_remerges_rows(self):
        """40 列（多行）→ resize 到 100 列 → 行数减少（重新合并为更少行）。"""
        async with _dialog_pilot(40) as (pilot, _d, buttons, _size):
            rows_narrow = self._rows(buttons)
            await pilot.resize_terminal(100, 34)
            await pilot.pause()
            await pilot.pause(0.3)
            self._assert_no_overlap(buttons)
            self._assert_buttons_in_viewport(buttons, (100, 34))
            self.assertLess(
                len(self._rows(buttons)), len(rows_narrow),
                f"resize 到 100 列应合并行数; 40 列行={rows_narrow}, "
                f"100 列行={self._rows(buttons)}",
            )

    async def test_resize_100_to_40_resplits_rows(self):
        """100 列（单/少行）→ resize 到 40 列 → 行数增加（拆行）。"""
        async with _dialog_pilot(100) as (pilot, _d, buttons, _size):
            rows_wide = self._rows(buttons)
            await pilot.resize_terminal(40, 34)
            await pilot.pause()
            await pilot.pause(0.3)
            self._assert_no_overlap(buttons)
            self._assert_buttons_in_viewport(buttons, (40, 34))
            self.assertGreater(
                len(self._rows(buttons)), len(rows_wide),
                f"resize 到 40 列应拆行; 100 列行={rows_wide}, "
                f"40 列行={self._rows(buttons)}",
            )

    async def test_resize_roundtrip_recover_no_overlap(self):
        """40→100→40 往返：最终 40 列布局与最初一致且无重叠。"""
        async with _dialog_pilot(40) as (pilot, _d, buttons, _size):
            rows_initial = self._rows(buttons)
            await pilot.resize_terminal(100, 34)
            await pilot.pause(0.3)
            await pilot.resize_terminal(40, 34)
            await pilot.pause()
            await pilot.pause(0.3)
            self._assert_no_overlap(buttons)
            self._assert_buttons_in_viewport(buttons, (40, 34))
            self.assertEqual(
                self._rows(buttons), rows_initial,
                f"往返后应恢复到原 40 列布局; 初始={rows_initial}, "
                f"最终={self._rows(buttons)}",
            )


if __name__ == "__main__":
    unittest.main()
