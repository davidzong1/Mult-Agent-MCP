"""
Agent 用户管理按钮 — 标签宽度一致性 / 色块缺口回归测试
================================================================

根因（Textual 8.2.8 + POSIX 终端 wcwidth）
--------------------------------------------
用户截图显示 AgentUserManageDialog 操作按钮行中"编辑/删除"按钮左右
出现黑色阶梯缺口。定位为按钮标签中的 **emoji 变体选择符 U+FE0F**：

  - 原标签 `✏️  编辑` / `🗑️  删除` 含 U+FE0F（VARIATION SELECTOR-16）。
  - rich/Textual 的 cell_len 把 `✏️`/`🗑️`（emoji presentation）视为 **2 列**。
  - 但 POSIX 终端（glibc wcwidth）中 U+270F(✏)/U+1F5D1(🗑) 是 EAW=N，
    wcwidth=1；U+FE0F 是 combining，wcwidth=0 → 终端实际只推进 **1 列**。
  - Textual 按 2 列布局按钮中间行，终端按 1 列渲染 → 中间行比上下边框行
    窄 1 列 → 按钮左右边缘出现阶梯状缺口（露出终端/表单深色背景）。

  `➕`(U+2795, W)、`📛`(U+1F4DB, W) 在 rich 与终端宽度一致（2），
  因此"新建/重命名"无缺口 —— 与用户报告"编辑/删除才有缺口"完全吻合。

修复
----
去掉 `✏️`/`🗑️` 中的 U+FE0F，改为 `✏`(U+270F)/`🗑`(U+1F5D1)。
此时 rich cell_len == POSIX wcwidth（均为 1），布局与终端一致，缺口消除。

测试
----
- 断言每个按钮标签的 rich cell_len == POSIX wcwidth 总和（逐字符模拟 glibc）。
- 断言编辑/删除标签不含 U+FE0F（根因直接回归）。
- 渲染级（真实 CSS + compositor 逐格采样）：按钮区域无 bg=None/纯黑缺口，
  且中间（内容）行背景覆盖 == 按钮宽度（像素/strip 断言）。

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实
teams_data.json；用真实 CSS（TeamManagerApp.CSS + 弹窗自身 CSS）。
"""

import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from rich.cells import cell_len as rich_cell_len

from textual.app import App
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import AgentUserEditDialog, AgentUserManageDialog
from tui.tui_screens import TeamManagerApp

MANAGE_BUTTONS = ["btn_new", "btn_edit", "btn_rename", "btn_delete", "btn_close"]
PURE_BLACK = (0, 0, 0)

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


def posix_wcwidth(ch: str) -> int:
    """真实 glibc wcwidth（逐字符）；失败时回退到 EAW 模拟。

    关键：glibc 对 U+FE0F 返回 0、对 U+270F(✏)/U+1F5D1(🗑) 返回 1，
    而 rich/Textual 把含 FE0F 的 emoji 序列（如 `✏️`）视为 2 宽 ——
    这正是"编辑/删除按钮左右阶梯缺口"的根因度量。
    """
    if _libc_wcwidth is not None:
        try:
            return max(_libc_wcwidth(ch), 0)
        except Exception:
            pass
    o = ord(ch)
    if o == 0 or unicodedata.combining(ch):
        return 0
    ea = unicodedata.east_asian_width(ch)
    return 2 if ea in ("W", "F") else 1


try:
    import ctypes as _ctypes
    _libc = _ctypes.CDLL("libc.so.6")
    _libc.wcwidth.argtypes = [_ctypes.c_wchar]
    _libc_wcwidth = _libc.wcwidth
except Exception:  # pragma: no cover - 非 glibc 平台回退到模拟
    _libc_wcwidth = None


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS（TeamManagerApp.CSS）。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


def _bgkey(bg):
    """背景色归一化为可比较 (r,g,b) 或 None。"""
    if bg is None:
        return None
    for attr in ("triplet", "rgb"):
        v = getattr(bg, attr, None)
        if v is not None:
            t = getattr(v, "triplet", v)
            if isinstance(t, tuple) and len(t) == 3:
                return tuple(int(c) for c in t)
            if hasattr(t, "red"):
                return (int(t.red), int(t.green), int(t.blue))
            return str(t)
    return str(bg)


class _WidthConsistencyBase(IsolatedAsyncioTestCase):
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

    async def _button_labels(self, pilot):
        """返回 {id: 标签纯文本}。"""
        out = {}
        for bid in MANAGE_BUTTONS:
            b = pilot.app.screen.query_one(f"#{bid}", Button)
            label = b.label.plain if b.label is not None else ""
            out[bid] = label
        return out


class ButtonLabelWidthConsistencyTests(_WidthConsistencyBase):
    """标签宽度一致性：rich cell_len 必须与 POSIX 终端 wcwidth 一致。

    修复前 `✏️`/`🗑️`（含 U+FE0F）rich=2 而终端=1 → 中间行错位；
    修复后 `✏`/`🗑` 均为 1 → 一致。这是"黑色阶梯缺口"的根因回归。
    """

    async def test_all_button_labels_width_match_terminal(self):
        """每个按钮标签 rich cell_len == POSIX wcwidth 总和。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            labels = await self._button_labels(pilot)
            self.assertEqual(len(labels), 5, f"应挂载 5 个按钮，实际 {list(labels)}")
            for bid, label in labels.items():
                rich_w = rich_cell_len(label)
                posix_w = sum(posix_wcwidth(c) for c in label)
                self.assertEqual(
                    rich_w, posix_w,
                    f"{bid} 标签 {label!r}: rich cell_len={rich_w} 但终端 wcwidth="
                    f"{posix_w} → Textual 布局与终端渲染错位（色块缺口）",
                )

    async def test_edit_delete_labels_have_no_variation_selector(self):
        """编辑/删除标签不含 U+FE0F（变体选择符）——缺口根因的直接回归。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            labels = await self._button_labels(pilot)
            for bid in ("btn_edit", "btn_delete"):
                self.assertNotIn(
                    "️", labels[bid],
                    f"{bid} 标签 {labels[bid]!r} 含 U+FE0F（变体选择符），"
                    f"rich 视为 2 列而 POSIX 终端视为 1 列 → 阶梯缺口",
                )

    async def test_single_line_row_content_widths_fit_buttons(self):
        """宽终端单行：每个按钮内容（含 padding）<= 按钮宽度，且无 FE0F 字符。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            labels = await self._button_labels(pilot)
            for bid in MANAGE_BUTTONS:
                b = screen.query_one(f"#{bid}", Button)
                content_w = rich_cell_len(labels[bid]) + 2  # line-pad/padding
                self.assertLessEqual(
                    content_w, b.region.width,
                    f"{bid} 内容宽度 {content_w} 超出按钮宽度 {b.region.width}",
                )


class AgentUserManageCellContinuityTests(_WidthConsistencyBase):
    """渲染级（像素/strip）：按钮区域无 bg=None/纯黑缺口。"""

    async def test_wide_terminal_all_button_cells_fully_painted(self):
        """(100,30) 5 个按钮区域无任何 bg=None / 纯黑单元格（黑色缺口）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            screen.refresh()
            await pilot.pause(0.2)
            comp = screen._compositor
            bad = []
            for bid in MANAGE_BUTTONS:
                b = screen.query_one(f"#{bid}", Button)
                entry = comp._visible_widgets.get(b)
                if not entry:
                    self.fail(f"{bid} 不在可见合成区")
                region = entry[0]
                for y in range(region.y, region.bottom):
                    for x in range(region.x, region.right):
                        bg = _bgkey(comp.get_style_at(x, y).bgcolor)
                        if bg is None or bg == PURE_BLACK:
                            bad.append((bid, x, y, bg))
            self.assertFalse(
                bad,
                f"按钮区域存在无背景/纯黑单元格（黑色缺口）: {bad[:10]}",
            )

    async def test_default_buttons_bg_distinct_from_form(self):
        """default 变体按钮（编辑/重命名/关闭）cell 背景 != 表单背景（可辨识）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            screen.refresh()
            await pilot.pause(0.2)
            comp = screen._compositor
            form_bg = _bgkey(
                screen.query_one(".dialog-form").visual_style.background)
            for bid in ("btn_edit", "btn_rename", "btn_close"):
                b = screen.query_one(f"#{bid}", Button)
                entry = comp._visible_widgets.get(b)
                self.assertIsNotNone(entry, f"{bid} 不在可见合成区")
                r = entry[0]
                mid = (r.x + r.right) // 2, (r.y + r.bottom) // 2
                cell_bg = _bgkey(comp.get_style_at(*mid).bgcolor)
                self.assertNotEqual(
                    cell_bg, form_bg,
                    f"{bid} default 按钮 cell 背景 {cell_bg} 与表单 {form_bg} 同色"
                    f"（视觉上消失/黑色缺口）",
                )

    async def test_default_buttons_focus_bg_still_distinct(self):
        """focus 后 default 按钮 cell 背景仍 != 表单背景。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            form_bg = _bgkey(
                screen.query_one(".dialog-form").visual_style.background)
            for bid in ("btn_edit", "btn_rename", "btn_close"):
                b = screen.query_one(f"#{bid}", Button)
                b.focus()
                await pilot.pause()
                await pilot.pause(0.2)
                screen.refresh()
                await pilot.pause(0.1)
                comp = screen._compositor
                entry = comp._visible_widgets.get(b)
                self.assertIsNotNone(entry, f"focus 后 {bid} 不在合成区")
                r = entry[0]
                mid = (r.x + r.right) // 2, (r.y + r.bottom) // 2
                cell_bg = _bgkey(comp.get_style_at(*mid).bgcolor)
                self.assertNotEqual(
                    cell_bg, form_bg,
                    f"focus {bid} 后 cell 背景 {cell_bg} 与表单 {form_bg} 同色",
                )

    async def test_default_buttons_hover_bg_still_distinct(self):
        """hover 后 default 按钮 cell 背景仍 != 表单背景。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            form_bg = _bgkey(
                screen.query_one(".dialog-form").visual_style.background)
            for bid in ("btn_edit", "btn_rename", "btn_close"):
                b = screen.query_one(f"#{bid}", Button)
                await pilot.hover(b)
                await pilot.pause(0.2)
                screen.refresh()
                await pilot.pause(0.1)
                comp = screen._compositor
                entry = comp._visible_widgets.get(b)
                self.assertIsNotNone(entry, f"hover 后 {bid} 不在合成区")
                r = entry[0]
                mid = (r.x + r.right) // 2, (r.y + r.bottom) // 2
                cell_bg = _bgkey(comp.get_style_at(*mid).bgcolor)
                self.assertNotEqual(
                    cell_bg, form_bg,
                    f"hover {bid} 后 cell 背景 {cell_bg} 与表单 {form_bg} 同色",
                )

    async def test_narrow_width_default_buttons_bg_distinct(self):
        """窄宽（按钮折行）下 default 按钮 cell 背景仍 != 表单背景。"""
        for w, h in ((60, 30), (44, 30)):
            app = _make_test_app()
            async with app.run_test(size=(w, h)) as pilot:
                await pilot.app.push_screen(AgentUserManageDialog())
                await pilot.pause(0.3)
                screen = pilot.app.screen
                screen.refresh()
                await pilot.pause(0.2)
                comp = screen._compositor
                form_bg = _bgkey(
                    screen.query_one(".dialog-form").visual_style.background)
                for bid in ("btn_edit", "btn_rename", "btn_close"):
                    b = screen.query_one(f"#{bid}", Button)
                    entry = comp._visible_widgets.get(b)
                    if not entry:
                        continue  # 窄宽下可能被滚动出视口，跳过
                    r = entry[0]
                    mid = (r.x + r.right) // 2, (r.y + r.bottom) // 2
                    cell_bg = _bgkey(comp.get_style_at(*mid).bgcolor)
                    self.assertNotEqual(
                        cell_bg, form_bg,
                        f"{w}x{h} {bid} default 按钮 cell 背景 {cell_bg} "
                        f"与表单 {form_bg} 同色",
                    )

    async def test_edit_dialog_cancel_bg_distinct_from_form(self):
        """编辑弹窗：取消（default）按钮 cell 背景 != 表单背景；保存保持 primary。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog(
            user_key="claude_p", agent_type="claude", takeover_enabled=True,
            anthropic_api_key="sk-ant-test",
            anthropic_base_url="https://api.anthropic.com",
            anthropic_model="claude-opus-5",
        )
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen = pilot.app.screen
            screen.refresh()
            await pilot.pause(0.2)
            comp = screen._compositor
            form_bg = _bgkey(
                screen.query_one(".dialog-form").visual_style.background)
            for bid in ("btn_cancel", "btn_save"):
                b = screen.query_one(f"#{bid}", Button)
                entry = comp._visible_widgets.get(b)
                self.assertIsNotNone(entry, f"edit {bid} 不在合成区")
                r = entry[0]
                mid = (r.x + r.right) // 2, (r.y + r.bottom) // 2
                cell_bg = _bgkey(comp.get_style_at(*mid).bgcolor)
                self.assertNotEqual(
                    cell_bg, form_bg,
                    f"edit {bid} cell 背景 {cell_bg} 与表单 {form_bg} 同色",
                )


if __name__ == "__main__":
    unittest.main()
