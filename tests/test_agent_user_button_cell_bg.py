"""
Agent 用户管理/编辑按钮 — cell/segment 背景连续性复现测试
================================================================

目标
----
用户截图显示编辑/删除按钮"背景色块缺口（黑色）"。此前布局测试只检查
region 边界（不重叠、在视口内）。本文件下沉到像素/segment 层：用
Textual compositor 的 `get_style_at(x, y)` 逐格采样每个按钮合成后的
单元格背景，确认黑色缺口的来源，并给出可复现断言。

结论（Textual 8.2.8，truecolor headless，真实 TeamManagerApp.CSS）
------------------------------------------------------------------
1. 按钮自身完全连续：
   - 每个按钮 compositor 区域内的每个单元格都有非 None 背景，且不是
     纯黑 (0,0,0)；render_strips 顶部 `▔`/底部 `▁` 行同样被按钮背景覆盖。
   - 同一网格行的按钮 edge-to-edge 邻接（`grid-gutter: 0 0` + margin:0
     生效），按钮之间无水平缝隙。
   - 依次聚焦 编辑/删除/新建/重命名/关闭 后，所有按钮单元格覆盖依然完整，
     focus 只会对聚焦按钮背景加 tint，不产生缺口。
2. 纯黑单元格的唯一来源 = ScrollBar：
   - 短终端（manage 表单 ≤ ~24 行、edit 表单 ≤ ~22 行）下，表单
     `overflow-y: auto` 让 Textual 在表单右缘挂出垂直 ScrollBar，其轨道/
     箭头以纯黑 (0,0,0) 渲染，紧贴最右操作按钮（80x22 时直接挨着
     btn_delete；100x24 时挨着 btn_close）。这就是截图中"黑色缺口"的来源。
   - 断言：屏幕上所有纯黑单元格均落在 ScrollBar 部件的区域内，且不在任何
     按钮区域内。
3. ANSI 模式风险：
   - `app.ansi_color=True`（native_ansi_color）时，按钮套用
     `Button:ansi.-style-default` → `background: ansi_default`（终端默认色，
     通常为黑）。若用户终端触发 ANSI 模式，彩色块整体变黑。
     `Button:ansi` 由 `app.native_ansi_color` 决定（app.py:544/1551）。

4. 修复方向（已落地，本文件据此锁定断言）：
   - default 变体按钮（编辑/重命名/关闭、取消）改用可辨识底色
     `background: $panel`（区别于 .dialog-form 的 $surface），
     并给完整状态样式：hover=$panel-lighten-1、focus=$panel-darken-1(+提示)。
     primary/error 变体语义保留（新建=蓝、删除=红）。
   - 断言下沉到**实际合成 cell 背景**（compositor.get_style_at），
     覆盖 base/hover/focus/窄宽；见
     AgentUserDefaultButtonDistinguishableTests / AgentUserEditDistinguishableTests。

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实
teams_data.json；用真实 CSS（TeamManagerApp.CSS + 弹窗自身 CSS）。

注：`compositor._visible_widgets` / `compositor.get_style_at` 为内部 API，
在 Textual 8.2.8 稳定；升级 Textual 需核对。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.geometry import Region
from textual.scrollbar import ScrollBar
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import AgentUserEditDialog, AgentUserManageDialog
from tui.tui_screens import TeamManagerApp

MANAGE_BUTTONS = ["btn_new", "btn_edit", "btn_rename", "btn_delete", "btn_close"]
EDIT_BUTTONS = ["btn_save", "btn_cancel"]
# default 变体按钮（修复后必须与表单背景可辨识）
MANAGE_DEFAULT_BUTTONS = ["btn_edit", "btn_rename", "btn_close"]
EDIT_DEFAULT_BUTTONS = ["btn_cancel"]

# default 按钮与表单背景($surface)的最小欧氏距离：低于此值视为"视觉上同色/消失"。
# 当前实现（truecolor, textual-dark）：base $panel≈32、hover $panel-lighten-1≈59、
# manage focus≈21、edit focus≈45，均远高于此阈值。
MIN_DIST_FROM_FORM = 12

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


def _bgkey(bg):
    """把背景色归一化为可比较的 (r,g,b) 元组或 None。"""
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


def _dist(a, b) -> float:
    """两个 (r,g,b) key 的欧氏距离；任一为 None 视为极大（不可比）。"""
    if a is None or b is None:
        return 999.0
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2 + (a[2] - b[2]) ** 2) ** 0.5


PURE_BLACK = (0, 0, 0)


class _CellBgBase(IsolatedAsyncioTestCase):
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

    # ---- compositor helpers ----

    async def _settled_screen(self, pilot):
        """强制重合成并返回 (screen, compositor)。"""
        screen = pilot.app.screen
        screen.refresh()
        await pilot.pause()
        await pilot.pause(0.2)
        return screen, screen._compositor

    async def _button_regions(self, pilot, ids):
        """返回 {id: 按钮在屏幕上的合成区域}（compositor map 为准）。"""
        screen, comp = await self._settled_screen(pilot)
        out = {}
        for bid in ids:
            try:
                b = screen.query_one(f"#{bid}", Button)
            except Exception:
                continue
            entry = comp._visible_widgets.get(b)
            if entry:
                out[bid] = entry[0]
        return out

    def _cells_in_region(self, comp, region: Region):
        """region 内每个单元格的 bgkey（行优先）。"""
        rows = []
        for y in range(region.y, region.bottom):
            row = [_bgkey(comp.get_style_at(x, y).bgcolor)
                   for x in range(region.x, region.right)]
            rows.append(row)
        return rows

    def _assert_cells_fully_painted(self, comp, region: Region, label: str) -> None:
        """区域内的每个单元格都有背景且非纯黑（无 unpainted/黑色缺口）。"""
        bad = []
        for y in range(region.y, region.bottom):
            for x in range(region.x, region.right):
                bg = _bgkey(comp.get_style_at(x, y).bgcolor)
                if bg is None or bg == PURE_BLACK:
                    bad.append((x, y, bg))
        self.assertFalse(
            bad,
            f"{label} 区域存在无背景/纯黑单元格（黑色缺口）: "
            f"{bad[:10]} region={region}",
        )

    def _content_row_bgs(self, comp, region: Region):
        """按钮内容行（垂直中间行，避开边框行）每个单元格的 bgkey。"""
        y = region.y + region.height // 2
        return [_bgkey(comp.get_style_at(x, y).bgcolor)
                for x in range(region.x, region.right)]

    def _assert_content_row_distinct(self, comp, button, form_bg, label: str) -> None:
        """按钮内容行每个合成 cell 背景都与表单背景不同且可辨识（距离 ≥ 阈值）。

        这是"修复后 default 按钮可辨识"的核心断言：无论 base/hover/focus、
        宽/窄，按钮的实际合成 cell 背景都必须与表单背景拉开距离 —— 把
        "default 按钮与表单同色 → 视觉空缺口"的回归锁死。
        """
        entry = comp._visible_widgets.get(button)
        self.assertIsNotNone(entry, f"{label}: 按钮未在合成器中渲染")
        region = entry[0]
        bad = []
        for bg in self._content_row_bgs(comp, region):
            if bg is None:
                bad.append((None, "unpainted"))
                continue
            d = _dist(bg, form_bg)
            if d < MIN_DIST_FROM_FORM:
                bad.append((bg, round(d, 1)))
        self.assertFalse(
            bad,
            f"{label} 内容行存在与表单背景不可辨识的 cell: {bad[:8]} "
            f"region={region}",
        )


class AgentUserManageCellContinuityTests(_CellBgBase):
    """管理弹窗：按钮区域 cell 背景连续、同排邻接、focus 不破坏覆盖。"""

    async def test_wide_terminal_all_button_cells_fully_painted(self):
        """(100,30) 5 个按钮区域无任何 bg=None / 纯黑单元格。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen, comp = await self._settled_screen(pilot)
            regions = await self._button_regions(pilot, MANAGE_BUTTONS)
            self.assertEqual(len(regions), 5,
                             f"应挂载 5 个按钮，实际 {list(regions)}")
            for bid, reg in regions.items():
                self._assert_cells_fully_painted(comp, reg, f"[{bid}]")

    async def test_wide_terminal_buttons_contiguous_in_row(self):
        """(100,30) 同一行按钮 edge-to-edge（next.x == prev.right，零缝隙）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            regions = await self._button_regions(pilot, MANAGE_BUTTONS)
            items = sorted(regions.items(), key=lambda kv: (kv[1].y, kv[1].x))
            gaps = []
            for (a, ra), (b, rb) in zip(items, items[1:]):
                if ra.y == rb.y:  # 同一行
                    if rb.x != ra.right:
                        gaps.append(f"{a}(right={ra.right}) → {b}(x={rb.x})")
            self.assertFalse(gaps, f"同排按钮之间存在缝隙: {gaps}")

    async def test_narrow_wrap_rows_still_contiguous_and_painted(self):
        """(80,24)/(70,22) 折行后：每行内按钮仍邻接、区域仍全覆盖。"""
        for w, h in [(80, 24), (70, 22)]:
            app = _make_test_app()
            async with app.run_test(size=(w, h)) as pilot:
                await pilot.app.push_screen(AgentUserManageDialog())
                await pilot.pause(0.3)
                screen, comp = await self._settled_screen(pilot)
                regions = await self._button_regions(pilot, MANAGE_BUTTONS)
                items = sorted(regions.items(), key=lambda kv: (kv[1].y, kv[1].x))
                gaps = []
                for (a, ra), (b, rb) in zip(items, items[1:]):
                    if ra.y == rb.y and rb.x != ra.right:
                        gaps.append(f"{a}→{b}")
                self.assertFalse(gaps, f"{w}x{h} 折行后同排仍有缝隙: {gaps}")
                for bid, reg in regions.items():
                    self._assert_cells_fully_painted(comp, reg, f"{w}x{h} [{bid}]")

    async def test_focus_each_button_preserves_cell_coverage(self):
        """依次聚焦 编辑/删除/新建/重命名/关闭 → 全部按钮仍全覆盖、聚焦不黑。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            for bid in ["btn_edit", "btn_delete", "btn_new", "btn_rename", "btn_close"]:
                screen = pilot.app.screen
                target = screen.query_one(f"#{bid}", Button)
                target.focus()
                await pilot.pause()
                await pilot.pause(0.2)
                screen, comp = await self._settled_screen(pilot)
                regions = await self._button_regions(pilot, MANAGE_BUTTONS)
                for other, reg in regions.items():
                    self._assert_cells_fully_painted(
                        comp, reg, f"focus={bid} [{other}]")
                # 聚焦按钮自身背景必须仍是主题色（非 ansi_default/纯黑）
                bg = _bgkey(target.visual_style.background)
                self.assertIsNotNone(bg, f"focus={bid} 背景为 None")
                self.assertNotEqual(bg, PURE_BLACK, f"focus={bid} 背景纯黑")


class AgentUserDefaultButtonDistinguishableTests(_CellBgBase):
    """核心断言：default 变体按钮**实际合成 cell 背景**与表单背景可辨识。

    覆盖 base / hover / focus / 窄宽，以及 primary/error 语义保留。
    """

    async def _assert_default_buttons_distinct(self, pilot, label: str):
        """当前状态下断言管理弹窗全部 default 按钮内容行与表单可辨识。"""
        screen, comp = await self._settled_screen(pilot)
        form_bg = _bgkey(screen.query_one(".dialog-form").visual_style.background)
        for bid in MANAGE_DEFAULT_BUTTONS:
            b = screen.query_one(f"#{bid}", Button)
            self._assert_content_row_distinct(
                comp, b, form_bg, f"{label} [{bid}]")

    async def test_wide_base_default_buttons_distinguishable(self):
        """(100,30) base 态：编辑/重命名/关闭 的实际 cell 背景与表单可辨识。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            await self._assert_default_buttons_distinct(pilot, "wide base")

    async def test_narrow_wrap_default_buttons_distinguishable(self):
        """(80,24)/(70,22) 折行后：default 按钮实际 cell 背景仍与表单可辨识。"""
        for w, h in [(80, 24), (70, 22)]:
            app = _make_test_app()
            async with app.run_test(size=(w, h)) as pilot:
                await pilot.app.push_screen(AgentUserManageDialog())
                await pilot.pause(0.3)
                form = pilot.app.screen.query_one(".dialog-form")
                if form.max_scroll_y > 0:
                    form.scroll_to(y=form.max_scroll_y)
                    await pilot.pause(0.2)
                await self._assert_default_buttons_distinct(pilot, f"{w}x{h} base")

    async def test_hover_keeps_default_buttons_distinguishable(self):
        """hover 每个 default 按钮：hover 态实际 cell 背景仍与表单可辨识。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            for bid in MANAGE_DEFAULT_BUTTONS:
                b = pilot.app.screen.query_one(f"#{bid}", Button)
                await pilot.hover(b)
                await pilot.pause()
                await self._assert_default_buttons_distinct(pilot, f"hover {bid}")

    async def test_focus_keeps_default_buttons_distinguishable(self):
        """focus 每个 default 按钮：focus 态实际 cell 背景仍与表单可辨识。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            for bid in MANAGE_DEFAULT_BUTTONS:
                pilot.app.screen.query_one(f"#{bid}", Button).focus()
                await pilot.pause()
                await pilot.pause(0.2)
                await self._assert_default_buttons_distinct(pilot, f"focus {bid}")

    async def test_primary_error_semantics_preserved(self):
        """新建=primary 蓝、删除=error 红：与 default 底色不同、与表单可辨识。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen, comp = await self._settled_screen(pilot)
            form_bg = _bgkey(screen.query_one(".dialog-form").visual_style.background)

            def center_bg(bid):
                b = screen.query_one(f"#{bid}", Button)
                reg = comp._visible_widgets[b][0]
                return _bgkey(comp.get_style_at(
                    reg.x + reg.width // 2, reg.y + reg.height // 2).bgcolor)

            new_bg = center_bg("btn_new")
            del_bg = center_bg("btn_delete")
            edit_bg = center_bg("btn_edit")
            # primary/error 语义：与 default 底色明显区分（颜色块不同）
            self.assertNotEqual(
                new_bg, edit_bg, "新建按钮应保持 primary 蓝色，而非 default 底色")
            self.assertNotEqual(
                del_bg, edit_bg, "删除按钮应保持 error 红色，而非 default 底色")
            # 三者均与表单背景可辨识
            for lbl, bg in [("新建", new_bg), ("删除", del_bg), ("编辑", edit_bg)]:
                self.assertGreaterEqual(
                    _dist(bg, form_bg), MIN_DIST_FROM_FORM,
                    f"{lbl} 按钮与表单背景不可辨识: {bg} vs form {form_bg}")


class AgentUserEditDistinguishableTests(_CellBgBase):
    """编辑弹窗：取消(default) 可辨识、保存(primary) 语义保留。"""

    def _edit_dialog(self) -> AgentUserEditDialog:
        return AgentUserEditDialog(
            user_key="claude_p", agent_type="claude", takeover_enabled=True,
            anthropic_api_key="sk-ant-test",
            anthropic_base_url="https://api.anthropic.com",
            anthropic_model="claude-opus-5",
        )

    async def test_edit_cancel_default_distinguishable_base_hover_focus(self):
        """取消按钮 base/hover/focus 内容行 cell 背景均与表单可辨识。"""
        app = _make_test_app()
        dialog = self._edit_dialog()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen, comp = await self._settled_screen(pilot)
            form_bg = _bgkey(screen.query_one(".dialog-form").visual_style.background)
            cancel = screen.query_one("#btn_cancel", Button)
            self._assert_content_row_distinct(comp, cancel, form_bg, "edit cancel base")
            # hover
            await pilot.hover(cancel)
            await pilot.pause()
            screen, comp = await self._settled_screen(pilot)
            self._assert_content_row_distinct(comp, cancel, form_bg, "edit cancel hover")
            # focus（聚焦后 focus 态与 hover 态均须可辨识）
            cancel.focus()
            await pilot.pause()
            await pilot.pause(0.2)
            screen, comp = await self._settled_screen(pilot)
            self._assert_content_row_distinct(comp, cancel, form_bg, "edit cancel focus")

    async def test_edit_save_primary_semantics_preserved(self):
        """保存按钮保持 primary 蓝色，与取消(default) 底色区分、与表单可辨识。"""
        app = _make_test_app()
        dialog = self._edit_dialog()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen, comp = await self._settled_screen(pilot)
            form_bg = _bgkey(screen.query_one(".dialog-form").visual_style.background)
            save = screen.query_one("#btn_save", Button)
            cancel = screen.query_one("#btn_cancel", Button)
            reg_s = comp._visible_widgets[save][0]
            reg_c = comp._visible_widgets[cancel][0]
            save_bg = _bgkey(comp.get_style_at(
                reg_s.x + reg_s.width // 2, reg_s.y + reg_s.height // 2).bgcolor)
            cancel_bg = _bgkey(comp.get_style_at(
                reg_c.x + reg_c.width // 2, reg_c.y + reg_c.height // 2).bgcolor)
            self.assertNotEqual(
                save_bg, cancel_bg,
                "保存按钮应保持 primary 蓝色，而非 default 底色")
            self.assertGreaterEqual(
                _dist(save_bg, form_bg), MIN_DIST_FROM_FORM,
                f"保存按钮与表单背景不可辨识: {save_bg} vs {form_bg}")


class AgentUserBlackGapSourceTests(_CellBgBase):
    """黑色缺口来源确认：纯黑单元格都来自 ScrollBar，不在按钮上；ANSI 模式黑化。"""

    async def test_default_variant_buttons_have_distinct_background(self):
        """default 变体按钮背景必须区别于 .dialog-form 的 $surface（可辨识）。

        修复前编辑/重命名/关闭背景 = $surface 与表单同色，视觉上不可见
        （用户截图"黑色缺口"来源之一）；修复后使用 $panel 底色，与表单
        可区分。primary/error 保留变体色。
        """
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            form_bg = _bgkey(
                screen.query_one(".dialog-form").visual_style.background)
            for bid in ["btn_edit", "btn_rename", "btn_close"]:
                b = screen.query_one(f"#{bid}", Button)
                self.assertNotEqual(
                    _bgkey(b.visual_style.background), form_bg,
                    f"{bid} default 变体背景应区别于表单 $surface（可辨识），"
                    f"实际 {b.visual_style.background} vs 表单 {form_bg}",
                )
            for bid in ["btn_new", "btn_delete"]:
                b = screen.query_one(f"#{bid}", Button)
                self.assertNotEqual(
                    _bgkey(b.visual_style.background), form_bg,
                    f"{bid} primary/error 变体背景应区别于表单背景（可见）",
                )

    async def test_short_height_black_cells_are_scrollbar_not_button(self):
        """(80,22)/(100,24) 出现纯黑单元格；每个都落在 ScrollBar 区域内、
        且不在任何按钮区域内 —— 证明缺口来自 overflow-y:auto 的滚动条。"""
        for w, h in [(80, 22), (100, 24)]:
            app = _make_test_app()
            async with app.run_test(size=(w, h)) as pilot:
                await pilot.app.push_screen(AgentUserManageDialog())
                await pilot.pause(0.3)
                screen, comp = await self._settled_screen(pilot)
                button_regions = list((await self._button_regions(pilot, MANAGE_BUTTONS)).values())
                black = []
                for y in range(h):
                    for x in range(w):
                        bg = _bgkey(comp.get_style_at(x, y).bgcolor)
                        if bg == PURE_BLACK:
                            black.append((x, y))
                self.assertGreater(
                    len(black), 0,
                    f"{w}x{h} 应复现纯黑缺口（滚动条），未出现",
                )
                outside = []
                for (x, y) in black:
                    try:
                        widget, _reg = comp.get_widget_at(x, y)
                    except Exception:
                        widget = None
                    if not isinstance(widget, ScrollBar):
                        outside.append((x, y, type(widget).__name__))
                    if any(r.contains(x, y) for r in button_regions):
                        outside.append((x, y, "INSIDE_BUTTON"))
                self.assertFalse(
                    outside,
                    f"{w}x{h} 存在不在 ScrollBar 内/落在按钮内的纯黑单元格: "
                    f"{outside[:10]}",
                )

    async def test_ansi_mode_buttons_switch_to_ansi_default_background(self):
        """native_ansi_color=True 时按钮背景变为 ansi_default（终端黑）；
        关闭时为主题真彩色 —— 说明 ANSI 终端会整体黑化彩色块。"""
        for ansi, expect_ansi_default in [(False, False), (True, True)]:
            app = _make_test_app()
            app.ansi_color = ansi
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(AgentUserManageDialog())
                await pilot.pause(0.3)
                edit = pilot.app.screen.query_one("#btn_edit", Button)
                bg = edit.visual_style.background
                ansi_val = getattr(bg, "ansi", None)
                if expect_ansi_default:
                    self.assertEqual(
                        ansi_val, -1,
                        f"ansi_color=True 时 btn_edit 应为 ansi_default，实际 {bg}",
                    )
                else:
                    self.assertIsNone(
                        ansi_val,
                        f"ansi_color=False 时 btn_edit 应为真彩色，实际 {bg}",
                    )
                self.assertEqual(pilot.app.native_ansi_color, ansi)


class AgentUserEditCellContinuityTests(_CellBgBase):
    """编辑弹窗：保存/取消按钮区域 cell 连续、focus 不破坏。"""

    async def test_edit_save_cancel_cells_fully_painted(self):
        """(100,30) 保存/取消区域无 bg=None / 纯黑单元格。"""
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
            screen, comp = await self._settled_screen(pilot)
            regions = await self._button_regions(pilot, EDIT_BUTTONS)
            self.assertEqual(len(regions), 2, f"应挂载 2 个按钮，实际 {list(regions)}")
            for bid, reg in regions.items():
                self._assert_cells_fully_painted(comp, reg, f"edit [{bid}]")

    async def test_edit_focus_preserves_coverage(self):
        """聚焦保存/取消后覆盖依然完整。"""
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
            for bid in ["btn_save", "btn_cancel"]:
                screen = pilot.app.screen
                screen.query_one(f"#{bid}", Button).focus()
                await pilot.pause()
                await pilot.pause(0.2)
                screen, comp = await self._settled_screen(pilot)
                for other, reg in (await self._button_regions(pilot, EDIT_BUTTONS)).items():
                    self._assert_cells_fully_painted(
                        comp, reg, f"edit focus={bid} [{other}]")


if __name__ == "__main__":
    unittest.main()
