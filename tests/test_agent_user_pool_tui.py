"""
Agent 用户切换池弹窗 — 点选顺序 + Provider 防呆锁 TUI 测试
=========================================================

覆盖 AgentUserPoolDialog（TeamDetailScreen 按 4 进入）的核心验收点：
  1. 点选 B→A→C 后保存 → team["agent_user_pool"] == ["B","A","C"]
     （点选顺序即切换顺序，本需求验收点）；
  2. 取消 A 再点 A → 顺序变为 ["B","C","A"]（重选移到末尾）；
  3. 顺序摘要 Label 随点选实时更新（切换顺序的核心可见性）；
  4. 已有池 ["C","A"] 打开后 selected 顺序还原为 ["C","A"]；
  5. 空 profile 空态提示；保存空选择 → 清空池并 pop cursor；
  6. 低高度 (100,20) 下表单可滚动、保存按钮不被裁切；
  7. Provider 防呆锁：首个勾选 profile 的 resolved agent_type 锁住池，
     异 type 置灰（disabled 物理不可点 + dim markup）且点击无效；
     取消全选 → 锁释放全部恢复；锁生效期间点选顺序仍正确；
     legacy（无 agent_type）作为独立类别参与同一规则。

实现依据（agentuser-tui-recon.md B4 实测 + 本测试实测）：
  - textual 8.2.8 的 SelectionList.selected 返回点选顺序（dict 插入序），
    取消再点选把该项移到末尾 → 直接用 SelectionList 即实现顺序语义；
  - Selection 必须从 textual.widgets._selection_list 导入；
  - 池序还原：initial_state 按列表固有顺序选中，弹窗 on_mount 按已存池
    顺序逐个 toggle，selected 即还原为池序；
  - 行点击：pilot.click("#agent_user_pool_list", offset=(5, row+1)) ——
    OptionList 点击映射对 modal 内列表存在固定 +1 行偏移（实测 y=1 命中
    row0），测试统一用 row+1 抵消，断言全部基于 selected 状态而非像素。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实
teams_data.json。渲染用生产真实 CSS（TeamManagerApp.CSS + 弹窗自身 CSS）。
"""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Button, Label
from textual.widgets._selection_list import SelectionList

from common import data_layer
from common.data_layer import load_data, save_data
from tui.tui_dialogs import AgentUserPoolDialog
from tui.tui_screens import TeamManagerApp

_TEAM = "demo"

# 列表显示顺序 = registry 插入顺序 A,B,C,D,E（SelectionList 行索引 0..4）。
# A/B/C 同 provider（claude）用于点选顺序测试；D=codex、E=legacy 用于防呆锁。
_PROFILE_DATA = {
    "agent_users": {
        "A": {
            "agent_type": "claude", "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-test",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        },
        "B": {
            "agent_type": "claude", "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-b",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-sonnet-5",
        },
        "C": {
            "agent_type": "claude", "takeover_enabled": False,
            "anthropic_api_key": "",
            "anthropic_model": "claude-haiku-4-5",
        },
        "D": {
            "agent_type": "codex", "takeover_enabled": True,
            "openai_api_key": "sk-fake",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-4o",
        },
        "E": {},  # legacy：无 agent_type，独立类别（防呆锁同规则拦截）
    },
    "teams": {_TEAM: {"members": {}}},
}


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS，不启动 TeamManagerApp 主流程/定时器。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


@asynccontextmanager
async def _pool_pilot(width: int = 100, height: int = 34):
    """挂载 AgentUserPoolDialog，等待布局稳定。"""
    app = _make_test_app()
    dialog = AgentUserPoolDialog(_TEAM)
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        await pilot.pause()
        await pilot.pause(0.2)
        yield pilot, dialog, pilot.app.screen


async def _click_row(pilot, row: int) -> None:
    """点击 SelectionList 第 row 行（勾选/取消勾选切换）。

    点击映射存在固定 +1 行偏移（实测 offset y=1 命中 row0），row+1 抵消。
    """
    await pilot.click("#agent_user_pool_list", offset=(5, row + 1))
    await pilot.pause()
    await pilot.pause(0.1)


def _summary_text(screen) -> str:
    return screen.query_one("#agent_user_pool_summary", Label).render().plain


def _selected_keys(screen) -> list:
    return list(screen.query_one("#agent_user_pool_list", SelectionList).selected)


def _team() -> dict:
    return load_data()["teams"][_TEAM]


class _AgentUserPoolBase(IsolatedAsyncioTestCase):
    """数据隔离基类（临时 teams_data.json，不触碰真实数据）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_PROFILE_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()


class AgentUserPoolOrderTests(_AgentUserPoolBase):
    """点选顺序 = 切换顺序（本需求验收点）。"""

    async def test_click_order_saved_as_switching_order(self):
        """点选 B→A→C 后保存 → agent_user_pool == ["B","A","C"]，cursor=0。"""
        async with _pool_pilot() as (pilot, _dialog, _screen):
            await _click_row(pilot, 1)  # B
            await _click_row(pilot, 0)  # A
            await _click_row(pilot, 2)  # C
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_team().get("agent_user_pool"), ["B", "A", "C"])
            self.assertEqual(_team().get("agent_user_pool_cursor"), 0)

    async def test_reselect_moves_profile_to_end(self):
        """取消 A 再点 A → 顺序变为 ["B","C","A"]（重选移到末尾）。"""
        async with _pool_pilot() as (pilot, _dialog, _screen):
            await _click_row(pilot, 1)  # B
            await _click_row(pilot, 0)  # A
            await _click_row(pilot, 2)  # C
            await _click_row(pilot, 0)  # 取消 A
            await _click_row(pilot, 0)  # 再点 A → 移到末尾
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_team().get("agent_user_pool"), ["B", "C", "A"])

    async def test_save_unchanged_pool_keeps_cursor(self):
        """池内容不变时保存 → 不重置已有 cursor。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["A", "C"]
        data["teams"][_TEAM]["agent_user_pool_cursor"] = 1
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, _screen):
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_team().get("agent_user_pool"), ["A", "C"])
            self.assertEqual(_team().get("agent_user_pool_cursor"), 1)


class AgentUserPoolSummaryTests(_AgentUserPoolBase):
    """顺序摘要行随点选实时更新（核心可见性）。"""

    async def test_summary_updates_with_clicks(self):
        async with _pool_pilot() as (pilot, _dialog, screen):
            self.assertEqual(_summary_text(screen), "切换顺序: （未勾选）")
            await _click_row(pilot, 1)  # B
            self.assertEqual(_summary_text(screen), "切换顺序: 1. B")
            await _click_row(pilot, 0)  # A
            self.assertEqual(_summary_text(screen), "切换顺序: 1. B → 2. A")
            await _click_row(pilot, 2)  # C
            self.assertEqual(
                _summary_text(screen), "切换顺序: 1. B → 2. A → 3. C")
            await _click_row(pilot, 0)  # 取消 A → 摘要实时收起
            self.assertEqual(_summary_text(screen), "切换顺序: 1. B → 2. C")
            await _click_row(pilot, 0)  # 再点 A → 移到末尾
            self.assertEqual(
                _summary_text(screen), "切换顺序: 1. B → 2. C → 3. A")


class AgentUserPoolRestoreTests(_AgentUserPoolBase):
    """已有池的打开还原与列表渲染。"""

    async def test_stored_pool_order_restored(self):
        """初始池 ["C","A"] 打开后 selected 顺序仍为 ["C","A"]。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["C", "A"]
        data["teams"][_TEAM]["agent_user_pool_cursor"] = 2
        save_data(data)
        async with _pool_pilot() as (_pilot, _dialog, screen):
            self.assertEqual(_selected_keys(screen), ["C", "A"])
            self.assertEqual(
                _summary_text(screen), "切换顺序: 1. C → 2. A")
            current = screen.query_one("#agent_user_pool_current", Label)
            self.assertEqual(current.render().plain, "当前顺序: C → A")

    async def test_stored_mixed_pool_restores_only_first_type(self):
        """历史脏池（混 type）打开后只还原与首项同 type 的键，异 type 置灰。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["C", "D", "A"]  # claude,codex,claude
        save_data(data)
        async with _pool_pilot() as (_pilot, _dialog, screen):
            self.assertEqual(_selected_keys(screen), ["C", "A"])
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            self.assertTrue(sel.get_option_at_index(3).disabled)  # D 置灰

    async def test_rows_show_provider_badge_and_masked_api_key(self):
        """列表行带 provider badge；api_key 只显示已配置/未配置，无明文。"""
        async with _pool_pilot() as (_pilot, _dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            rows = [str(sel.get_option_at_index(i).prompt) for i in range(5)]
            self.assertIn("🤖Claude", rows[0])  # A claude
            self.assertIn("🤖Claude", rows[1])  # B claude
            self.assertIn("🤖Claude", rows[2])  # C claude
            self.assertIn("🔵Codex", rows[3])   # D codex
            self.assertIn("⚪旧版", rows[4])     # E legacy
            self.assertIn("API 已配置", rows[0])
            self.assertIn("API 已配置", rows[1])
            self.assertIn("API 未配置", rows[2])
            self.assertIn("API 已配置", rows[3])
            self.assertIn("API 未配置", rows[4])
            for row in rows:
                self.assertNotIn("sk-", row, f"api_key 明文泄露: {row}")


class AgentUserPoolLockTests(_AgentUserPoolBase):
    """Provider 防呆锁：首勾 type 锁池，异 type 置灰不可点，全空释放。"""

    async def _lock_hint(self, screen) -> Label:
        return screen.query_one("#agent_user_pool_lock", Label)

    async def test_first_claude_disables_codex_and_legacy(self):
        """首选 claude → codex/legacy 行 disabled + dim；点击无效；同 type 可继续勾。"""
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            await _click_row(pilot, 0)  # A (claude) → lock=claude
            self.assertTrue(sel.get_option_at_index(3).disabled, "codex 行应置灰")
            self.assertTrue(sel.get_option_at_index(4).disabled, "legacy 行应置灰")
            self.assertIn("[dim]", str(sel.get_option_at_index(3).prompt))
            self.assertIn("[dim]", str(sel.get_option_at_index(4).prompt))
            hint = await self._lock_hint(screen)
            self.assertTrue(hint.display)
            self.assertIn("claude", hint.render().plain)
            # 点击 codex 行无效（物理 disabled，事件不触发 toggle）
            await _click_row(pilot, 3)
            self.assertEqual(_selected_keys(screen), ["A"])
            # 同 provider 仍可勾选
            await _click_row(pilot, 1)  # B (claude)
            self.assertEqual(_selected_keys(screen), ["A", "B"])

    async def test_lock_released_when_all_deselected(self):
        """按 t 解锁（all 模式）后全部取消 → 锁释放，异 type 恢复可选。

        2026-08-09 同步修改：团队池新增初始锁（default 模式锁恒在，全空
        不释放 —— 该语义由 test_provider_guard_regression 的
        test_deselect_all_keeps_initial_lock 覆盖）；本用例验证切到
        all 模式后回落动态锁：全空 → 无锁，全部恢复。
        """
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            await pilot.press("t")  # → all 模式：显示全部 provider
            await _click_row(pilot, 0)  # A (claude) → 动态锁=claude
            self.assertTrue(sel.get_option_at_index(3).disabled)
            await _click_row(pilot, 0)  # 取消 A → 全空 → 锁释放
            self.assertFalse(sel.get_option_at_index(3).disabled, "锁释放后 codex 应可选")
            self.assertNotIn("[dim]", str(sel.get_option_at_index(3).prompt))
            hint = await self._lock_hint(screen)
            self.assertNotIn("首勾锁", hint.render().plain, "全空时不应有动态锁提示")
            await _click_row(pilot, 3)  # D (codex) 现在可勾选
            self.assertEqual(_selected_keys(screen), ["D"])

    async def test_lock_keeps_click_order_correct(self):
        """锁生效期间点选顺序仍正确：被锁行点击不改变已选顺序，落盘顺序无损。"""
        async with _pool_pilot() as (pilot, _dialog, screen):
            await _click_row(pilot, 2)  # C (claude) → lock=claude
            await _click_row(pilot, 0)  # A
            await _click_row(pilot, 3)  # D — 被锁拦截，应无任何效果
            await _click_row(pilot, 1)  # B
            self.assertEqual(
                _selected_keys(screen), ["C", "A", "B"],
                "锁拦截不应打乱点选顺序")
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_team().get("agent_user_pool"), ["C", "A", "B"])

    async def test_legacy_first_locks_everything(self):
        """按 t 解锁（all 模式）后首选 legacy（无 agent_type）→ 所有 typed profile 置灰。

        2026-08-09 同步修改：团队池初始锁（default 模式，按 team.default_agent
        锁 claude）会先置灰 legacy 行，点不到 —— legacy 独立类别语义改为
        在 all 模式下验证（同规则无特例）。
        """
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            await pilot.press("t")  # → all 模式：显示全部 provider
            await _click_row(pilot, 4)  # E (legacy) → lock=旧版
            for i in range(4):
                self.assertTrue(
                    sel.get_option_at_index(i).disabled,
                    f"legacy 锁下 typed profile 行 {i} 应置灰")
            hint = await self._lock_hint(screen)
            self.assertTrue(hint.display)
            self.assertIn("旧版", hint.render().plain)
            await _click_row(pilot, 0)  # 点击无效
            self.assertEqual(_selected_keys(screen), ["E"])


class AgentUserPoolEmptyTests(_AgentUserPoolBase):
    """空 profile 空态与清空语义。"""

    async def test_empty_profiles_shows_hint(self):
        """无任何 profile → 空态提示可见；保存空选择幂等。"""
        save_data({"agent_users": {}, "teams": {_TEAM: {"members": {}}}})
        async with _pool_pilot() as (pilot, _dialog, screen):
            hint = screen.query_one("#agent_user_pool_empty", Label)
            self.assertTrue(hint.display, "无 profile 时应显示空态提示")
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            self.assertEqual(sel.option_count, 0)
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertNotIn("agent_user_pool", _team())
            self.assertNotIn("agent_user_pool_cursor", _team())

    async def test_save_empty_selection_clears_pool_and_cursor(self):
        """已存池 ["A","B"]+cursor 全取消后保存 → 两键均被 pop。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["A", "B"]
        data["teams"][_TEAM]["agent_user_pool_cursor"] = 1
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, screen):
            await _click_row(pilot, 0)  # 取消 A
            await _click_row(pilot, 1)  # 取消 B
            self.assertEqual(_selected_keys(screen), [])
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertNotIn("agent_user_pool", _team())
            self.assertNotIn("agent_user_pool_cursor", _team())
            result = screen.query_one("#agent_user_pool_result", Label)
            self.assertIn("已清空", result.render().plain)


class AgentUserPoolLowHeightTests(_AgentUserPoolBase):
    """低高度视口：表单可滚动、保存按钮可达不裁切。"""

    async def test_low_height_scrollable_not_clipped(self):
        async with _pool_pilot(100, 20) as (pilot, _dialog, screen):
            form = screen.query_one(".dialog-form")
            self.assertGreaterEqual(form.max_scroll_y, 0)
            if form.max_scroll_y > 0:
                form.scroll_to(y=form.max_scroll_y)
                await pilot.pause()
                await pilot.pause(0.2)
            btn = screen.query_one("#btn_save_pool", Button)
            self.assertTrue(btn.visible, "低高度滚动后保存按钮应可见")
            r = btn.region
            self.assertGreaterEqual(r.y, 0, f"保存按钮越出上边界: {r}")
            self.assertLessEqual(r.bottom, 20, f"保存按钮越出下边界: {r}")
            # 保存不 dismiss（与 TeamDefaultAgentUserDialog 一致），点后应给出反馈
            await pilot.click("#btn_save_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            result = screen.query_one("#agent_user_pool_result", Label)
            self.assertTrue(
                result.render().plain, "低高度滚动后保存按钮应可点击并给出反馈")


if __name__ == "__main__":
    unittest.main()
