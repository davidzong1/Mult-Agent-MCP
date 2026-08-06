"""
Agent 用户管理/编辑弹窗 — default 按钮可辨识底色回归测试
================================================================

根因（补充独立结论）
--------------------
default 变体按钮 `background: $surface`，与 `.dialog-form` 的 `$surface`
背景完全同色 → 按钮几何区域连续但**视觉为空**（用户截图"色块缺失"之一）。
primary/error 变体（新建/删除/保存）本身是语义色，不受影响。

修复（作用域限定，保留 primary/error 语义）
--------------------------------------------
- manage 弹窗（#agent_user_actions）：带 agent-btn-default 类的 default
  按钮底色 = $panel（区别于表单 $surface），hover/focus/active 用派生色。
  见 TeamManagerApp.CSS。
- edit 弹窗（#agent_user_edit_actions）：取消按钮底色 = $panel，
  保存(primary) 显式回退 $primary。见 AgentUserEditDialog.CSS。

本测试下沉到 cell/segment 层（compositor.get_style_at）：断言 default 按钮
**实际渲染的单元格背景** ≠ 表单背景，覆盖 normal / hover / focus 与窄宽度，
并校验 primary/error 语义色完整填充按钮。

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实
teams_data.json；用真实 CSS（TeamManagerApp.CSS + 弹窗自身 CSS）。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.geometry import Region
from textual.widgets import Button

from common import data_layer
from tui.tui_dialogs import AgentUserEditDialog, AgentUserManageDialog
from tui.tui_screens import TeamManagerApp

MANAGE_DEFAULT = ["btn_edit", "btn_rename", "btn_close"]
MANAGE_PRIMARY = ["btn_new"]
MANAGE_ERROR = ["btn_delete"]
EDIT_DEFAULT = ["btn_cancel"]
EDIT_PRIMARY = ["btn_save"]

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
    """背景色归一化为可比较 (r,g,b) 元组或 None。"""
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


class _DefaultBgBase(IsolatedAsyncioTestCase):
    """数据隔离基类：临时 teams_data.json + 真实 CSS + compositor 采样。"""

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

    async def _settled(self, pilot):
        """强制重合成并返回 (screen, compositor)。"""
        screen = pilot.app.screen
        screen.refresh()
        await pilot.pause()
        await pilot.pause(0.2)
        return screen, screen._compositor

    async def _button_regions(self, pilot, ids):
        """返回 {id: 按钮在屏幕上的合成区域}。"""
        screen, comp = await self._settled(pilot)
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

    def _form_bg(self, screen):
        """表单背景色（.dialog-form 解析后的 $surface）。"""
        return _bgkey(screen.query_one(".dialog-form").visual_style.background)

    def _cell_bgs(self, comp, region: Region):
        """region 内每个单元格的实际渲染背景色。"""
        return [
            _bgkey(comp.get_style_at(x, y).bgcolor)
            for y in range(region.y, region.bottom)
            for x in range(region.x, region.right)
        ]

    def _assert_cells_differ_from_form(self, comp, region, form_bg, label):
        """区域每个单元格背景均非 None、且与表单背景不同（可辨识）。"""
        bad = []
        for y in range(region.y, region.bottom):
            for x in range(region.x, region.right):
                bg = _bgkey(comp.get_style_at(x, y).bgcolor)
                if bg is None or bg == form_bg:
                    bad.append((x, y, bg))
        self.assertFalse(
            bad,
            f"{label} 存在背景 None 或与表单同色($surface)的单元格（视觉为空）: "
            f"{bad[:8]} region={region} form_bg={form_bg}",
        )

    def _assert_cells_filled_with(self, comp, region, color, label):
        """区域每个单元格背景均为指定色（语义色完整填充，无同色缺口）。"""
        bad = []
        for y in range(region.y, region.bottom):
            for x in range(region.x, region.right):
                bg = _bgkey(comp.get_style_at(x, y).bgcolor)
                if bg is None or bg != color:
                    bad.append((x, y, bg))
        self.assertFalse(
            bad,
            f"{label} 存在非 {color} 的单元格（语义色未完整填充）: {bad[:8]}",
        )


class ManageDefaultCellsDifferFromForm(_DefaultBgBase):
    """管理弹窗 default 按钮：normal 与窄宽度下 cell 背景 ≠ 表单背景。"""

    async def test_manage_default_cells_differ_from_form_wide(self):
        """(100,30)：编辑/重命名/关闭 每个单元格背景都区别于表单 $surface。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            regions = await self._button_regions(pilot, MANAGE_DEFAULT)
            self.assertEqual(len(regions), 3, f"应挂载 3 个 default 按钮: {regions}")
            for bid, reg in regions.items():
                self._assert_cells_differ_from_form(comp, reg, form_bg, f"[{bid}]")

    async def test_manage_default_cells_differ_from_form_narrow(self):
        """(45,30)/(40,30) 折行：每行 default 按钮 cell 背景仍区别于表单。"""
        for w in (45, 40):
            app = _make_test_app()
            async with app.run_test(size=(w, 30)) as pilot:
                await pilot.app.push_screen(AgentUserManageDialog())
                await pilot.pause(0.3)
                screen, comp = await self._settled(pilot)
                form_bg = self._form_bg(screen)
                regions = await self._button_regions(pilot, MANAGE_DEFAULT)
                self.assertGreaterEqual(len(regions), 3, f"{w} 列 default 按钮数异常")
                for bid, reg in regions.items():
                    self._assert_cells_differ_from_form(
                        comp, reg, form_bg, f"{w} 列 [{bid}]")


class ManageStateCells(_DefaultBgBase):
    """管理弹窗 default 按钮：hover / focus 状态下 cell 背景仍可辨识。"""

    async def test_manage_hover_cells_differ_from_form(self):
        """hover 编辑按钮 → cell 背景仍区别于表单（hover 状态可见）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            await pilot.hover("#btn_edit")
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            reg = (await self._button_regions(pilot, ["btn_edit"]))["btn_edit"]
            self._assert_cells_differ_from_form(
                comp, reg, form_bg, "hover[btn_edit]")

    async def test_manage_focus_cells_differ_from_form(self):
        """focus 重命名按钮 → cell 背景仍区别于表单（focus 状态可见）。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen = pilot.app.screen
            screen.query_one("#btn_rename", Button).focus()
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            reg = (await self._button_regions(pilot, ["btn_rename"]))["btn_rename"]
            self._assert_cells_differ_from_form(
                comp, reg, form_bg, "focus[btn_rename]")

    async def test_manage_hover_state_changes_from_base(self):
        """hover 前后 default 按钮 cell 背景不同 → 状态反馈真实可见。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen, comp = await self._settled(pilot)
            reg = (await self._button_regions(pilot, ["btn_edit"]))["btn_edit"]
            base = self._cell_bgs(comp, reg)
            await pilot.hover("#btn_edit")
            screen, comp = await self._settled(pilot)
            hover = self._cell_bgs(comp, reg)
            self.assertNotEqual(
                base, hover,
                "hover 前后 cell 背景应不同（否则 hover 无可见反馈）",
            )


class ManageSemanticsPreserved(_DefaultBgBase):
    """管理弹窗 primary/error 语义保留：语义色完整填充、与 default 互异。"""

    async def test_manage_primary_error_semantic_fill(self):
        """(100,30)：新建(primary)蓝、删除(error)红 完整填充按钮区域，
        且与 default 底色互异。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            screen, comp = await self._settled(pilot)
            regions = await self._button_regions(
                pilot, MANAGE_PRIMARY + MANAGE_ERROR + MANAGE_DEFAULT)
            # 语义色 = 按钮自身解析后的 visual background
            for bid in MANAGE_PRIMARY + MANAGE_ERROR:
                b = screen.query_one(f"#{bid}", Button)
                color = _bgkey(b.visual_style.background)
                self.assertIsNotNone(color, f"{bid} 语义色解析失败")
                self._assert_cells_filled_with(
                    comp, regions[bid], color, f"[{bid}] 语义填充")
            defaults = {
                _bgkey(screen.query_one(f"#{bid}", Button).visual_style.background)
                for bid in MANAGE_DEFAULT
            }
            p = _bgkey(screen.query_one("#btn_new", Button).visual_style.background)
            e = _bgkey(screen.query_one("#btn_delete", Button).visual_style.background)
            self.assertNotIn(p, defaults, "primary 底色不应与 default 相同")
            self.assertNotIn(e, defaults, "error 底色不应与 default 相同")
            self.assertNotEqual(p, e, "primary 与 error 语义色应互异")


class EditDefaultCellsDifferFromForm(_DefaultBgBase):
    """编辑弹窗：取消(default) cell 背景 ≠ 表单；保存(primary) 语义保留。"""

    async def test_edit_cancel_cells_differ_from_form(self):
        """(100,30)：取消按钮 cell 背景区别于表单 $surface。"""
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
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            regions = await self._button_regions(pilot, EDIT_DEFAULT)
            self.assertEqual(len(regions), 1, f"取消按钮缺失: {regions}")
            self._assert_cells_differ_from_form(
                comp, regions["btn_cancel"], form_bg, "edit[btn_cancel]")

    async def test_edit_narrow_cancel_cells_differ_from_form(self):
        """(40,34)：窄宽度折行后取消按钮 cell 背景仍区别于表单。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog(
            user_key="claude_p", agent_type="claude", takeover_enabled=True,
            anthropic_api_key="sk-ant-test",
            anthropic_base_url="https://api.anthropic.com",
            anthropic_model="claude-opus-5",
        )
        async with app.run_test(size=(40, 34)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            regions = await self._button_regions(pilot, EDIT_DEFAULT)
            self.assertIn("btn_cancel", regions, "窄宽下取消按钮缺失")
            self._assert_cells_differ_from_form(
                comp, regions["btn_cancel"], form_bg, "edit 窄宽[btn_cancel]")

    async def test_edit_hover_cancel_cells_differ_from_form(self):
        """hover 取消按钮 → cell 背景仍区别于表单（hover 状态可见）。"""
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
            await pilot.hover("#btn_cancel")
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            reg = (await self._button_regions(pilot, ["btn_cancel"]))["btn_cancel"]
            self._assert_cells_differ_from_form(
                comp, reg, form_bg, "edit hover[btn_cancel]")

    async def test_edit_focus_cancel_cells_differ_from_form(self):
        """focus 取消按钮 → cell 背景仍区别于表单（focus 状态可见）。"""
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
            screen.query_one("#btn_cancel", Button).focus()
            screen, comp = await self._settled(pilot)
            form_bg = self._form_bg(screen)
            reg = (await self._button_regions(pilot, ["btn_cancel"]))["btn_cancel"]
            self._assert_cells_differ_from_form(
                comp, reg, form_bg, "edit focus[btn_cancel]")

    async def test_edit_save_primary_semantic_fill(self):
        """(100,30)：保存(primary) 按钮语义色完整填充、区别于表单。"""
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
            screen, comp = await self._settled(pilot)
            save = screen.query_one("#btn_save", Button)
            color = _bgkey(save.visual_style.background)
            self.assertIsNotNone(color, "保存(primary) 语义色解析失败")
            regions = await self._button_regions(pilot, EDIT_PRIMARY)
            self.assertIn("btn_save", regions, "保存按钮缺失")
            self._assert_cells_filled_with(
                comp, regions["btn_save"], color, "edit[btn_save] 语义填充")
            form_bg = self._form_bg(screen)
            self.assertNotEqual(color, form_bg, "保存(primary) 应区别于表单背景")


if __name__ == "__main__":
    unittest.main()
