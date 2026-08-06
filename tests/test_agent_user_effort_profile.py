"""
Agent 用户 profile 默认 effort — UI 与持久化测试
================================================

覆盖 coder-claude 负责的 AgentUserEditDialog / AgentUserManageDialog effort 功能：

  1. 共享 effort UI helper：按 provider 分离等级并校验
     （Claude: low/medium/high/xhigh/max；Codex: minimal/low/medium/high/xhigh），
     避免单一集合让 Codex 接受 max、或让 Claude 接受 minimal。
  2. AgentUserEditDialog 初始化：typed claude/codex / legacy / 跨 provider 残留
     effort 均不崩溃，Select 初始值归一到当前 provider 合法集合
     （allow_blank=False 下非法值会抛 InvalidSelectValueError）。
  3. provider 切换：切换 Provider 时"默认推理强度"选项随之刷新。
  4. 保存：dismiss 载荷含按 provider 归一化的 effort。
  5. AgentUserManageDialog 新建/编辑 profile：effort 字段正确持久化到全局
     data['agent_users'] registry。

附：backend 冒烟（只读，不修改核心文件）——resolve_member_effort 空参/Claude/Codex
与 codex_command/claude_agent_args 注入，覆盖 blocker 修复（参数 agent_kind 不再
遮蔽同名函数 agent_type）。

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json；
用真实 CSS（TeamManagerApp.CSS + 弹窗自身 CSS）。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Button, Input, Select

from common import data_layer
from common.tmux_utils import (
    claude_agent_args,
    codex_command,
    resolve_member_effort,
)
from tui.tui_dialogs import (
    AgentUserEditDialog,
    AgentUserManageDialog,
    agent_user_effort_choices_for,
    agent_user_effort_value_for,
    _effort_value_for,
)
from tui.tui_screens import TeamManagerApp

_PROFILE_DATA = {
    "agent_users": {
        "claude_p": {
            "agent_type": "claude",
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-test",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
            "effort": "high",
        },
        "codex_p": {
            "agent_type": "codex",
            "takeover_enabled": False,
            "openai_api_key": "sk-fake",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-4o",
            "effort": "minimal",
        },
    },
    "teams": {},
}


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS，不启动 TeamManagerApp 主流程/定时器。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


class _ProfileEffortBase(IsolatedAsyncioTestCase):
    """数据隔离基类：临时 teams_data.json + 真实 CSS。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        from common.data_layer import save_data
        save_data(dict(_PROFILE_DATA))

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    @staticmethod
    def _effort_values(select: Select) -> list:
        """Select 选项值列表（剔除 Select.NULL 占位）。"""
        return [v for _, v in select._options if isinstance(v, str)]


# ============================================================
# 共享 effort UI helper — 按 provider 分离等级 + 归一化校验
# ============================================================

class AgentUserProfileEffortHelpersTests(unittest.TestCase):
    """共享 helper：Claude/Codex 等级集分离，避免跨 provider 接受非法值。"""

    def test_claude_profile_choices_include_max_not_minimal(self):
        values = {v for _, v in agent_user_effort_choices_for("claude")}
        self.assertEqual(
            values, {"", "low", "medium", "high", "xhigh", "max"},
            "Claude profile 默认 effort 选项应含 max、不含 minimal",
        )

    def test_codex_profile_choices_include_minimal_not_max(self):
        values = {v for _, v in agent_user_effort_choices_for("codex")}
        self.assertEqual(
            values, {"", "minimal", "low", "medium", "high", "xhigh"},
            "Codex profile 默认 effort 选项应含 minimal、不含 max",
        )

    def test_unknown_agent_defaults_to_claude_choices(self):
        values = {v for _, v in agent_user_effort_choices_for("custom")}
        self.assertIn("max", values)
        self.assertNotIn("minimal", values)

    def test_effort_value_normalized_per_provider(self):
        """跨 provider 残留（Claude 存 minimal / Codex 存 max）归一到 ''。"""
        self.assertEqual(agent_user_effort_value_for("claude", "high"), "high")
        self.assertEqual(agent_user_effort_value_for("claude", "max"), "max")
        self.assertEqual(agent_user_effort_value_for("claude", "minimal"), "")
        self.assertEqual(agent_user_effort_value_for("claude", "garbage"), "")
        self.assertEqual(agent_user_effort_value_for("codex", "minimal"), "minimal")
        self.assertEqual(agent_user_effort_value_for("codex", "xhigh"), "xhigh")
        self.assertEqual(agent_user_effort_value_for("codex", "max"), "")
        self.assertEqual(agent_user_effort_value_for("codex", ""), "")

    def test_member_effort_value_falls_back_to_inherit(self):
        """成员级 helper：跨 provider 非法 → inherit（不注入），合法等级保留。"""
        self.assertEqual(_effort_value_for("claude", "max"), "max")
        self.assertEqual(_effort_value_for("codex", "max"), "inherit")
        self.assertEqual(_effort_value_for("claude", "minimal"), "inherit")
        self.assertEqual(_effort_value_for("codex", "minimal"), "minimal")


# ============================================================
# AgentUserEditDialog — 初始化 / provider 切换 / 保存
# ============================================================

class AgentUserEditDialogEffortTests(_ProfileEffortBase):
    """编辑弹窗 effort 字段：选项按 provider 分离、初始值归一化、保存含 effort。"""

    async def test_typed_claude_profile_effort_options_and_value(self):
        """Claude typed profile：选项含 max 不含 minimal；effort 回填。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog(
            user_key="claude_p", agent_type="claude", takeover_enabled=True,
            effort="high",
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            sel = pilot.app.screen.query_one("#effort", Select)
            self.assertEqual(str(sel.value), "high")
            values = self._effort_values(sel)
            self.assertIn("max", values)
            self.assertNotIn("minimal", values)

    async def test_typed_codex_profile_effort_options_and_value(self):
        """Codex typed profile：选项含 minimal 不含 max；effort 回填。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog(
            user_key="codex_p", agent_type="codex", takeover_enabled=False,
            effort="minimal",
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            sel = pilot.app.screen.query_one("#effort", Select)
            self.assertEqual(str(sel.value), "minimal")
            values = self._effort_values(sel)
            self.assertIn("minimal", values)
            self.assertNotIn("max", values)

    async def test_cross_provider_effort_backfilled_no_crash(self):
        """claude profile 存了 codex-only minimal → 构造不崩溃，Select 回退 ''。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog(
            user_key="claude_p", agent_type="claude", takeover_enabled=True,
            effort="minimal",  # 跨 provider 残留
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            sel = pilot.app.screen.query_one("#effort", Select)
            self.assertEqual(str(sel.value), "")

    async def test_legacy_profile_effort_defaults_to_claude(self):
        """旧版 profile（无 agent_type）：effort 选项默认 Claude 集合。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog(
            user_key="legacy_p", agent_type="", takeover_enabled=True,
            effort="max",
        )
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            sel = pilot.app.screen.query_one("#effort", Select)
            values = self._effort_values(sel)
            self.assertIn("max", values)
            self.assertNotIn("minimal", values)
            # legacy 默认走 Claude 集合 → max 保留
            self.assertEqual(str(sel.value), "max")

    async def test_provider_switch_updates_effort_options(self):
        """新建 profile 切换 Provider：选项在 Claude/Codex 集合间正确刷新。"""
        app = _make_test_app()
        dialog = AgentUserEditDialog()  # 新建，provider 可选
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen = pilot.app.screen
            provider = screen.query_one("#agent_type", Select)
            sel = screen.query_one("#effort", Select)
            # 初始（provider 未选）→ Claude 集合
            self.assertIn("max", self._effort_values(sel))
            self.assertNotIn("minimal", self._effort_values(sel))
            # 切到 Codex → 刷新为 Codex 集合（max 消失、minimal 出现）
            provider.value = "codex"
            await pilot.pause()
            values = self._effort_values(sel)
            self.assertIn("minimal", values)
            self.assertNotIn("max", values)
            # 切回 Claude → max 恢复、minimal 消失
            provider.value = "claude"
            await pilot.pause()
            values = self._effort_values(sel)
            self.assertIn("max", values)
            self.assertNotIn("minimal", values)

    async def test_save_payload_includes_effort(self):
        """新建 claude profile：保存载荷含按 provider 归一化的 effort。"""
        app = _make_test_app()
        dialog = _RecordingEditDialog()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen = pilot.app.screen
            screen.query_one("#key", Input).value = "p_eff"
            screen.query_one("#agent_type", Select).value = "claude"
            await pilot.pause()
            screen.query_one("#effort", Select).value = "high"
            await pilot.pause()
            await pilot.click("#btn_save")
            await pilot.pause(0.3)
            self.assertIsNotNone(dialog.recorded, "保存后应 dismiss 返回 dict")
            self.assertEqual(dialog.recorded["effort"], "high")
            self.assertEqual(dialog.recorded["agent_type"], "claude")


class _RecordingEditDialog(AgentUserEditDialog):
    """记录 dismiss 载荷，便于断言保存结果。"""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.recorded = None

    def dismiss(self, result=None, *args, **kwargs):
        if result is not None:
            self.recorded = result
        return super().dismiss(result, *args, **kwargs)


# ============================================================
# AgentUserManageDialog — 新建/编辑 profile 持久化 effort
# ============================================================

class AgentUserManageDialogEffortTests(_ProfileEffortBase):
    """全局管理弹窗：effort 字段经 新建/编辑 正确写入 data['agent_users']。"""

    async def test_manage_new_user_persists_effort(self):
        """新建 profile → effort 持久化到全局 registry。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 42)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            await pilot.click("#btn_new")
            await pilot.pause(0.3)
            screen = pilot.app.screen
            self.assertIsInstance(screen, AgentUserEditDialog)
            screen.query_one("#key", Input).value = "p_eff"
            screen.query_one("#agent_type", Select).value = "claude"
            await pilot.pause()
            screen.query_one("#effort", Select).value = "max"
            await pilot.pause()
            await pilot.click("#btn_save")
            await pilot.pause(0.4)
            data = data_layer.load_data()
            self.assertEqual(data["agent_users"]["p_eff"]["effort"], "max")

    async def test_manage_edit_user_prefills_and_persists_effort(self):
        """编辑 profile：effort 回填；改值保存后持久化。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 42)) as pilot:
            await pilot.app.push_screen(AgentUserManageDialog())
            await pilot.pause(0.3)
            await pilot.click("#btn_edit")  # 首行高亮 = claude_p
            await pilot.pause(0.3)
            screen = pilot.app.screen
            self.assertIsInstance(screen, AgentUserEditDialog)
            sel = screen.query_one("#effort", Select)
            self.assertEqual(str(sel.value), "high")  # claude_p.effort=high 回填
            sel.value = "low"
            await pilot.pause()
            await pilot.click("#btn_save")
            await pilot.pause(0.4)
            data = data_layer.load_data()
            self.assertEqual(data["agent_users"]["claude_p"]["effort"], "low")


# ============================================================
# Backend 冒烟（只读）— resolve_member_effort 空参 / Claude / Codex 注入
# ============================================================

class AgentUserEffortBackendSmokeTests(unittest.TestCase):
    """resolve_member_effort 与命令构造注入（不修改核心文件）。

    覆盖 blocker 修复：resolve_member_effort 参数已改名 agent_kind，不再遮蔽
    同名函数 agent_type(agent)；空 agent_kind 时按成员 agent 解析不抛 TypeError。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    def _save_team(self, team: dict):
        # effort 是 Agent 用户 feature 的一部分：当前 resolve_member_effort 在
        # 团队 agent_users registry 为空时直接返回 ""（设计如此）。为验证解析/
        # 注入逻辑，测试 seed 一个最小 registry（成员显式 effort 与 registry
        # 内容无关，只需非空即可走到显式级别分支）。
        team.setdefault("agent_users", {"dummy": {"agent_type": "claude"}})
        from common.data_layer import save_data
        save_data({"teams": {"t": team}})

    def test_empty_agent_kind_resolves_from_member_agent(self):
        """空 agent_kind：按成员 agent 推断 provider，不因遮蔽抛 TypeError。"""
        self._save_team({
            "default_agent": "claude",
            "members": {"alice": {"role": "coder", "agent": "claude", "effort": "high"}},
        })
        self.assertEqual(resolve_member_effort("t", "alice"), "high")

    def test_claude_member_explicit_effort(self):
        self._save_team({
            "default_agent": "claude",
            "members": {"bob": {"role": "coder", "agent": "claude", "effort": "max"}},
        })
        self.assertEqual(resolve_member_effort("t", "bob", "claude"), "max")

    def test_codex_member_explicit_effort(self):
        self._save_team({
            "default_agent": "codex",
            "members": {"carol": {"role": "tester", "agent": "codex", "effort": "minimal"}},
        })
        self.assertEqual(resolve_member_effort("t", "carol", "codex"), "minimal")

    def test_codex_member_rejects_claude_only_level(self):
        self._save_team({
            "default_agent": "codex",
            "members": {"dave": {"role": "tester", "agent": "codex", "effort": "max"}},
        })
        self.assertEqual(resolve_member_effort("t", "dave", "codex"), "")

    def test_leader_member_effort_resolves(self):
        """leader 启动路径：以 leader 成员名解析，空 agent_kind 不抛错。"""
        self._save_team({
            "default_agent": "claude",
            "leader": "lead",
            "members": {"lead": {"role": "leader", "agent": "claude", "effort": "high"}},
        })
        self.assertEqual(resolve_member_effort("t", "lead"), "high")

    def test_claude_agent_args_injects_effort_flag(self):
        args = claude_agent_args("claude", "manual", model="m", effort="high")
        self.assertIn("--effort", args)
        self.assertIn("high", args)

    def test_codex_command_injects_model_reasoning_effort(self):
        cmd = codex_command("codex", "/tmp/ws", model="gpt-5", effort="high")
        self.assertIn("-c", cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)

    def test_codex_command_skips_invalid_effort(self):
        cmd = codex_command("codex", "/tmp/ws", effort="max")
        self.assertNotIn("model_reasoning_effort", cmd)


if __name__ == "__main__":
    unittest.main()
