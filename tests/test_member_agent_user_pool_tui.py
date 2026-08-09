"""
成员级 Agent 用户池弹窗 — 点选顺序 + provider 单向过滤 TUI 测试
=============================================================

覆盖 MemberAgentUserPoolDialog（EditMemberDialog 内按 "5" / 按钮进入）：
  1. 点选顺序即切换顺序：B→A→C 保存 → member["agent_user_pool"]==["B","A","C"]
     （点选顺序 = 切换顺序，与团队池同语义）；
  2. provider 由 resolve_pool_atype(team, member) 单向决定：claude 成员 → 池内
     只能 claude / legacy+url 匹配 profile（数据层 _profile_matches_atype 同源
     语义），异类行置灰（disabled + dim + 原因标注）且点击无效；
  3. 取消全选保存 = 取消激活（pop agent_user_pool / cursor，回落团队池）；
     get_agent_user_pool 验证"成员池激活后团队池完全不参与"（用户裁定语义）；
  4. disable_option_at_index 原地置灰**不重建列表**：置灰行存在期间点选插入序
     保持正确、option_count 不变（重建会丢 SelectionList._selected 插入序）；
  5. 成员 CLI 为自定义命令（resolve_pool_atype → "other"）→ 不过滤：
     数据层 _profile_matches_atype 对 "other" 返回 True，全部行可选
     （旧"全部置灰"是"一个都选不了"的缺陷语义）；自动换号由
     select_failover_candidate 的 pool-other-agent 安全阀拒绝。

实现依据（照 tests/test_agent_user_pool_tui.py，agentuser-tui-recon.md B4）：
  - textual 8.2.8 的 SelectionList.selected 返回点选顺序（dict 插入序）；
  - Selection 必须从 textual.widgets._selection_list 导入；
  - 池序还原：on_mount 按已存池顺序逐个 toggle（跳过已删除/不匹配 key）；
  - 行点击 offset=(5, row+1) 的 +1 偏移同团队池测试（点击映射固定偏移）。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
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
from common.tmux_utils import (
    get_agent_user_pool,
    member_pool_is_activated,
    resolve_pool_atype,
)
from tui.tui_dialogs import EditMemberDialog, MemberAgentUserPoolDialog
from tui.tui_screens import TeamManagerApp

_TEAM = "demo"
_MEMBER = "m1"

# 列表显示顺序 = registry 插入顺序 A,B,C,D,E,F（SelectionList 行索引 0..5）。
# A/B/C 同 provider（claude）用于点选顺序测试；D=codex 异类置灰；
# E=legacy 带 claude base_url（数据层语义下匹配 claude，可选）；
# F=legacy 空（不匹配 claude，置灰）。
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
        "E": {"anthropic_base_url": "https://api.anthropic.com"},  # legacy+url
        "F": {},  # legacy 空
    },
    "teams": {_TEAM: {"members": {_MEMBER: {"agent": "claude"}}}},
}

# claude 成员下：匹配行 0,1,2,4（A,B,C,E）；不匹配行 3,5（D,F）
_MISMATCH_ROWS = (3, 5)


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS，不启动 TeamManagerApp 主流程/定时器。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


@asynccontextmanager
async def _pool_pilot(width: int = 100, height: int = 34):
    """挂载 MemberAgentUserPoolDialog，等待布局稳定。"""
    app = _make_test_app()
    dialog = MemberAgentUserPoolDialog(_TEAM, _MEMBER)
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        await pilot.pause()
        await pilot.pause(0.2)
        yield pilot, dialog, pilot.app.screen


async def _click_row(pilot, row: int) -> None:
    """点击 SelectionList 第 row 行（勾选/取消勾选切换）。

    点击映射存在固定 +1 行偏移（实测 offset y=1 命中 row0），row+1 抵消。
    """
    await pilot.click("#member_pool_list", offset=(5, row + 1))
    await pilot.pause()
    await pilot.pause(0.1)


def _summary_text(screen) -> str:
    return screen.query_one("#member_pool_summary", Label).render().plain


def _selected_keys(screen) -> list:
    return list(screen.query_one("#member_pool_list", SelectionList).selected)


def _team() -> dict:
    return load_data()["teams"][_TEAM]


def _member() -> dict:
    return _team()["members"][_MEMBER]


class _MemberPoolBase(IsolatedAsyncioTestCase):
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


class MemberPoolOrderTests(_MemberPoolBase):
    """点选顺序 = 切换顺序（与团队池同语义，写成员级字段）。"""

    async def test_click_order_saved_as_switching_order(self):
        """点选 B→A→C 后保存 → member["agent_user_pool"]==["B","A","C"]，cursor=0。"""
        async with _pool_pilot() as (pilot, _dialog, screen):
            await _click_row(pilot, 1)  # B
            await _click_row(pilot, 0)  # A
            await _click_row(pilot, 2)  # C
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_member().get("agent_user_pool"), ["B", "A", "C"])
            self.assertEqual(_member().get("agent_user_pool_cursor"), 0)
            result = screen.query_one("#member_pool_result", Label)
            self.assertIn("已为 m1 写入", result.render().plain)

    async def test_reselect_moves_profile_to_end(self):
        """取消 A 再点 A → 顺序变为 ["B","C","A"]（重选移到末尾）。"""
        async with _pool_pilot() as (pilot, _dialog, screen):
            await _click_row(pilot, 1)  # B
            await _click_row(pilot, 0)  # A
            await _click_row(pilot, 2)  # C
            await _click_row(pilot, 0)  # 取消 A
            await _click_row(pilot, 0)  # 再点 A → 移到末尾
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_member().get("agent_user_pool"), ["B", "C", "A"])

    async def test_save_unchanged_pool_keeps_content_cursor_zeroed(self):
        """池内容不变时保存 → 内容保持；cursor 归零（成员池数据层无条件归零，
        与团队池弹窗的"仅变化时重置"不同，此处以数据层语义为准）。"""
        data = load_data()
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool"] = ["A", "C"]
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool_cursor"] = 1
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, screen):
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_member().get("agent_user_pool"), ["A", "C"])
            self.assertEqual(_member().get("agent_user_pool_cursor"), 0)


class MemberPoolProviderFilterTests(_MemberPoolBase):
    """provider 由 resolve_pool_atype 单向决定：异类置灰不可选。"""

    async def test_mismatched_rows_greyed_with_reason_and_click_blocked(self):
        """claude 成员 → D(codex)/F(legacy空) 置灰+原因标注；点击无效；
        E(legacy+url) 按数据层语义匹配 claude，可选。"""
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#member_pool_list", SelectionList)
            self.assertEqual(sel.option_count, 6)
            for i in _MISMATCH_ROWS:
                self.assertTrue(sel.get_option_at_index(i).disabled, f"行 {i} 应置灰")
                prompt = str(sel.get_option_at_index(i).prompt)
                self.assertIn("[dim]", prompt)
                self.assertIn("不匹配", prompt)
            for i in (0, 1, 2, 4):
                self.assertFalse(
                    sel.get_option_at_index(i).disabled, f"行 {i} 应可选")
            await _click_row(pilot, 3)  # D — 被 disabled 拦截
            self.assertEqual(_selected_keys(screen), [])
            await _click_row(pilot, 4)  # E — legacy+url 匹配 claude
            self.assertEqual(_selected_keys(screen), ["E"])

    async def test_codex_member_only_codex_selectable(self):
        """成员 agent=codex → resolve_pool_atype="codex"，仅 D 可选。"""
        data = load_data()
        data["teams"][_TEAM]["members"][_MEMBER]["agent"] = "codex"
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#member_pool_list", SelectionList)
            for i in (0, 1, 2, 4, 5):
                self.assertTrue(sel.get_option_at_index(i).disabled)
            self.assertFalse(sel.get_option_at_index(3).disabled)
            await _click_row(pilot, 3)  # D
            self.assertEqual(_selected_keys(screen), ["D"])
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_member().get("agent_user_pool"), ["D"])

    async def test_resolve_pool_atype_falls_back_to_team_default(self):
        """成员未配 agent → resolve_pool_atype 落到 team.default_agent。"""
        data = load_data()
        del data["teams"][_TEAM]["members"][_MEMBER]["agent"]
        data["teams"][_TEAM]["default_agent"] = "codex"
        save_data(data)
        team = _team()
        member = _member()
        self.assertEqual(resolve_pool_atype(team, member), "codex")


class MemberPoolEffectivePoolTests(_MemberPoolBase):
    """当前生效的池可见性 + 成员池激活后团队池完全不参与。"""

    async def test_status_shows_effective_pool_and_no_fallback_semantics(self):
        """未配置 → 显示团队池；保存成员池 → 状态切换，警示"完全不参与/耗尽"。"""
        async with _pool_pilot() as (pilot, _dialog, screen):
            status = screen.query_one("#member_pool_status", Label)
            self.assertIn("团队池", status.render().plain)
            warning = screen.query_one("#member_pool_warning", Label)
            warning_text = warning.render().plain
            self.assertIn("完全不参与", warning_text)
            self.assertIn("耗尽", warning_text)
            await _click_row(pilot, 2)  # C
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertIn("成员池", status.render().plain)
            self.assertIn("不参与", status.render().plain)

    async def test_team_pool_excluded_when_member_pool_activated(self):
        """成员池激活后 get_agent_user_pool 只用成员池（即使团队池也有内容）。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["A", "B"]
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool"] = ["C"]
        save_data(data)
        team = _team()
        member = _member()
        self.assertTrue(member_pool_is_activated(member))
        self.assertEqual(get_agent_user_pool(team, member, "claude"), ["C"])
        self.assertNotEqual(
            get_agent_user_pool(team, member, "claude"),
            team.get("agent_user_pool"),
            "激活后绝不能回落团队池")

    async def test_not_activated_uses_team_pool(self):
        """成员池未配置 → get_agent_user_pool 回落团队池。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["A", "B"]
        save_data(data)
        team = _team()
        member = _member()
        self.assertFalse(member_pool_is_activated(member))
        self.assertEqual(get_agent_user_pool(team, member, "claude"), ["A", "B"])


class MemberPoolDeactivateTests(_MemberPoolBase):
    """取消全选 = 取消激活（pop 两键，回落团队池）。"""

    async def test_deselect_all_deactivates(self):
        """已存池 ["A","B"]+cursor 全取消后保存 → 两键被 pop，回落团队池。"""
        data = load_data()
        data["teams"][_TEAM]["agent_user_pool"] = ["A"]  # registry 内 key
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool"] = ["A", "B"]
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool_cursor"] = 1
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, screen):
            await _click_row(pilot, 0)  # 取消 A
            await _click_row(pilot, 1)  # 取消 B
            self.assertEqual(_selected_keys(screen), [])
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertNotIn("agent_user_pool", _member())
            self.assertNotIn("agent_user_pool_cursor", _member())
            result = screen.query_one("#member_pool_result", Label)
            self.assertIn("已取消", result.render().plain)
            status = screen.query_one("#member_pool_status", Label)
            self.assertIn("团队池", status.render().plain)
            self.assertEqual(get_agent_user_pool(_team(), _member(), "claude"), ["A"])


class MemberPoolRestoreTests(_MemberPoolBase):
    """已有成员池的打开还原（按池序 toggle，selected 还原为池序）。"""

    async def test_stored_pool_order_restored(self):
        """初始池 ["C","A"] 打开后 selected 顺序仍为 ["C","A"]。"""
        data = load_data()
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool"] = ["C", "A"]
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool_cursor"] = 2
        save_data(data)
        async with _pool_pilot() as (_pilot, _dialog, screen):
            self.assertEqual(_selected_keys(screen), ["C", "A"])
            self.assertEqual(_summary_text(screen), "切换顺序: 1. C → 2. A")
            current = screen.query_one("#member_pool_current", Label)
            self.assertEqual(current.render().plain, "当前顺序: C → A")

    async def test_restore_skips_deleted_and_mismatched_keys(self):
        """脏池含已删除/异类 key → 只还原有效子集（保序）。"""
        data = load_data()
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool"] = [
            "A", "D", "GHOST", "F", "B"]
        save_data(data)
        async with _pool_pilot() as (_pilot, _dialog, screen):
            self.assertEqual(_selected_keys(screen), ["A", "B"])


class MemberPoolNoRebuildTests(_MemberPoolBase):
    """disable_option_at_index 原地置灰不重建列表 → 点选插入序保真。"""

    async def test_disable_path_preserves_insertion_order_and_count(self):
        """置灰行存在期间反复勾选/取消，selected 插入序始终正确，行数不变。"""
        data = load_data()
        data["teams"][_TEAM]["members"][_MEMBER]["agent_user_pool"] = ["A"]
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#member_pool_list", SelectionList)
            self.assertEqual(sel.option_count, 6)
            self.assertEqual(_selected_keys(screen), ["A"])
            await _click_row(pilot, 1)  # B
            self.assertEqual(_selected_keys(screen), ["A", "B"])
            await _click_row(pilot, 0)  # 取消 A
            self.assertEqual(_selected_keys(screen), ["B"])
            await _click_row(pilot, 0)  # 再点 A → 移到末尾
            self.assertEqual(_selected_keys(screen), ["B", "A"])
            await _click_row(pilot, 3)  # D — 置灰拦截，不应有任何副作用
            self.assertEqual(_selected_keys(screen), ["B", "A"])
            self.assertEqual(sel.option_count, 6, "原地置灰不应重建/复制行")
            self.assertTrue(sel.get_option_at_index(3).disabled)
            self.assertEqual(_summary_text(screen), "切换顺序: 1. B → 2. A")
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertEqual(_member().get("agent_user_pool"), ["B", "A"])


class MemberPoolRowLabelTests(_MemberPoolBase):
    """列表行 badge + API 掩码（无明文）。"""

    async def test_rows_show_badge_and_masked_api_key(self):
        async with _pool_pilot() as (_pilot, _dialog, screen):
            sel = screen.query_one("#member_pool_list", SelectionList)
            rows = [str(sel.get_option_at_index(i).prompt) for i in range(6)]
            self.assertIn("🤖Claude", rows[0])  # A claude
            self.assertIn("🔵Codex", rows[3])   # D codex（置灰）
            self.assertIn("🤖Claude", rows[4])  # E legacy+url → 数据层推断 claude
            self.assertIn("⚪旧版", rows[5])     # F legacy 空壳 → 无法推断 → 旧版
            self.assertIn("API 已配置", rows[0])
            self.assertIn("API 未配置", rows[2])
            self.assertIn("不匹配", rows[3])
            for row in rows:
                self.assertNotIn("sk-", row, f"api_key 明文泄露: {row}")


class MemberPoolCustomAgentTests(_MemberPoolBase):
    """成员 CLI 自定义命令 → resolve_pool_atype="other" → 不过滤，全部行可选。

    2026-08-09 同步修改：数据层 _profile_matches_atype 对 "other" 返回 True
    不过滤 —— 自定义 agent 成员也能看到/选择全部 profile（旧断言"全部行
    置灰"编码的是"一个都选不了"的缺陷语义）；自动换号由
    select_failover_candidate 的 pool-other-agent 安全阀拒绝，UI 不再重复堵。
    """

    async def test_custom_agent_all_rows_enabled(self):
        data = load_data()
        data["teams"][_TEAM]["members"][_MEMBER]["agent"] = "custom"
        save_data(data)
        async with _pool_pilot() as (pilot, _dialog, screen):
            sel = screen.query_one("#member_pool_list", SelectionList)
            for i in range(6):
                self.assertFalse(
                    sel.get_option_at_index(i).disabled, f"行 {i} 不应置灰")
            atype_line = screen.query_one("#member_pool_atype", Label)
            self.assertIn("自定义命令", atype_line.render().plain)
            self.assertIn("自动换号将被拒绝", atype_line.render().plain)
            self.assertEqual(_selected_keys(screen), [])
            await pilot.click("#btn_save_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertNotIn("agent_user_pool", _member())


class MemberPoolEditEntryTests(_MemberPoolBase):
    """成员编辑页入口：按钮可见可点、绑定 "5" 存在、弹窗可回到表单。"""

    def test_binding_5_and_entry_button_present(self):
        dialog = EditMemberDialog(_MEMBER, "coder", "claude", team_name=_TEAM)
        keys = [b.key for b in dialog.BINDINGS]
        self.assertIn("5", keys, "成员用户池应绑定 '5'（团队池 '4' 的下一级）")

    async def test_button_opens_pool_dialog_and_close_returns(self):
        @asynccontextmanager
        async def _edit_pilot():
            app = _make_test_app()
            dlg = EditMemberDialog(_MEMBER, "coder", "claude", team_name=_TEAM)
            async with app.run_test(size=(100, 34)) as pilot:
                await pilot.app.push_screen(dlg)
                await pilot.pause()
                await pilot.pause(0.2)
                yield pilot, dlg

        async with _edit_pilot() as (pilot, dlg):
            btn = dlg.query_one("#btn_member_pool", Button)
            self.assertTrue(btn.visible, "成员用户池按钮应可见")
            await pilot.click("#btn_member_pool")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertIsInstance(
                pilot.app.screen, MemberAgentUserPoolDialog,
                "点击按钮应推开成员池弹窗")
            await pilot.click("#btn_close")
            await pilot.pause()
            await pilot.pause(0.2)
            self.assertIs(pilot.app.screen, dlg, "关闭弹窗应回到成员编辑表单")


if __name__ == "__main__":
    unittest.main()
