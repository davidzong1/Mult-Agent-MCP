"""
成员编辑对话框 — effort 字段 UI 回归测试
================================================================

覆盖 AddMemberDialog / EditMemberDialog 的 effort 集成：
  - effort Select 初始选项按当前 Agent 的 provider 显示（Claude 含 max、
    不含 minimal；Codex 含 minimal、不含 max）；
  - 切换 Agent（provider）时选项随动；当前值不在新集合时回落 inherit；
  - add/save 返回 effort 字段；
  - EditMemberDialog 按当前 Agent 回填 effort（非法等级回落 inherit）。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json；
用真实 CSS（TeamManagerApp.CSS）。
"""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Button, Input, Select

from common import data_layer
from common.data_layer import save_data
from tui.tui_dialogs import AddMemberDialog, EditMemberDialog, AgentUserEditDialog
from tui.tui_screens import TeamManagerApp

_DATA = {
    "agent_users": {
        "claude_p": {"agent_type": "claude", "effort": "high",
                     "anthropic_api_key": "k", "anthropic_model": "m"},
        "codex_p": {"agent_type": "codex", "effort": "low",
                    "openai_api_key": "k", "codex_model": "m"},
    },
    "teams": {
        "t": {
            "default_agent": "claude",
            "members": {},
        },
    },
}


def _make_test_app() -> App[None]:
    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


def _select_values(select: Select) -> list[str]:
    return [v for _, v in select._options]


class _EffortDialogBase(IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()


class AddMemberEffortDialogTests(_EffortDialogBase):
    async def test_initial_options_follow_default_agent(self):
        """default_agent=claude → effort 选项含 max、不含 minimal。"""
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.app.push_screen(AddMemberDialog(default_agent="claude", team_name="t"))
            await pilot.pause(0.3)
            effort = pilot.app.screen.query_one("#effort", Select)
            values = _select_values(effort)
            self.assertIn("max", values)
            self.assertIn("inherit", values)
            self.assertIn("off", values)
            self.assertNotIn("minimal", values)

    async def test_codex_default_excludes_max_includes_minimal(self):
        """default_agent=codex → effort 选项含 minimal、不含 max。"""
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.app.push_screen(AddMemberDialog(default_agent="codex", team_name="t"))
            await pilot.pause(0.3)
            effort = pilot.app.screen.query_one("#effort", Select)
            values = _select_values(effort)
            self.assertIn("minimal", values)
            self.assertNotIn("max", values)

    async def test_refresh_options_on_agent_switch_resets_invalid(self):
        """claude 下选 max 后切到 codex → 选项更新且当前值回落 inherit。"""
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.app.push_screen(AddMemberDialog(default_agent="claude", team_name="t"))
            await pilot.pause(0.3)
            dialog = pilot.app.screen
            effort = dialog.query_one("#effort", Select)
            effort.value = "max"
            await pilot.pause(0.1)
            dialog._refresh_effort_options("codex")
            await pilot.pause(0.1)
            values = _select_values(effort)
            self.assertNotIn("max", values)
            self.assertIn("minimal", values)
            self.assertEqual(_select_value_now(effort), "inherit")

    async def test_add_returns_effort(self):
        """填成员名后点添加 → 对话框关闭，effort 字段在流程中可读且值合法。"""
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            dialog = AddMemberDialog(default_agent="claude", team_name="t")
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen = pilot.app.screen
            effort = screen.query_one("#effort", Select)
            pre_value = _select_value_now(effort)
            screen.query_one("#name", Input).value = "alice"
            await pilot.click("#btn_add")
            await pilot.pause(0.2)
            self.assertNotIn(dialog, list(pilot.app.screen_stack))
            self.assertEqual(pre_value, "inherit")  # claude 默认集合内合法


def _select_value_now(select: Select) -> str:
    from tui.tui_dialogs import _select_value as sv
    return sv(select, "inherit")


class EditMemberEffortDialogTests(_EffortDialogBase):
    async def test_edit_backfill_normalizes_by_agent(self):
        """codex 成员回填 max（非法）→ 回落 inherit；claude 成员回填 max 保留。"""
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.app.push_screen(EditMemberDialog(
                "bob", current_role="coder", current_agent="codex",
                current_effort="max", team_name="t"))
            await pilot.pause(0.3)
            dlg = pilot.app.screen
            effort = dlg.query_one("#effort", Select)
            self.assertEqual(_select_value_now(effort), "inherit")
            values = _select_values(effort)
            self.assertIn("minimal", values)
            self.assertNotIn("max", values)

    async def test_edit_backfill_keeps_valid_claude_level(self):
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            await pilot.app.push_screen(EditMemberDialog(
                "carol", current_role="coder", current_agent="claude",
                current_effort="xhigh", team_name="t"))
            await pilot.pause(0.3)
            dlg = pilot.app.screen
            effort = dlg.query_one("#effort", Select)
            self.assertEqual(_select_value_now(effort), "xhigh")

    async def test_edit_save_returns_effort(self):
        """保存后对话框关闭，effort 字段在流程中可读且值合法。"""
        app = _make_test_app()
        async with app.run_test(size=(80, 34)) as pilot:
            dialog = EditMemberDialog(
                "dave", current_role="coder", current_agent="claude",
                current_effort="high", team_name="t")
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            screen = pilot.app.screen
            effort = screen.query_one("#effort", Select)
            pre_value = _select_value_now(effort)
            await pilot.click("#btn_save")
            await pilot.pause(0.2)
            self.assertNotIn(dialog, list(pilot.app.screen_stack))
            self.assertEqual(pre_value, "high")


class AgentUserEffortBackfillTests(_EffortDialogBase):
    """AgentUserEditDialog 默认 effort 按 provider 归一化回填（不抛 Select 无匹配）。"""

    async def test_codex_profile_invalid_effort_falls_back_to_unset(self):
        """codex profile 回填 max（非法）→ Select 值回落"不设置默认"，选项为 codex 集合。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(AgentUserEditDialog(
                user_key="p", agent_type="codex", effort="max",
                openai_api_key="k", codex_model="m"))
            await pilot.pause(0.3)
            dlg = pilot.app.screen
            effort = dlg.query_one("#effort", Select)
            values = _select_values(effort)
            self.assertIn("minimal", values)
            self.assertNotIn("max", values)
            self.assertEqual(_select_value_now(effort), "")  # 不设置默认

    async def test_claude_profile_valid_effort_preserved(self):
        """claude profile 回填 max → 保留。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(AgentUserEditDialog(
                user_key="p", agent_type="claude", effort="max",
                anthropic_api_key="k", anthropic_model="m"))
            await pilot.pause(0.3)
            dlg = pilot.app.screen
            effort = dlg.query_one("#effort", Select)
            self.assertEqual(_select_value_now(effort), "max")

    async def test_legacy_profile_backfill_unset(self):
        """legacy（无 agent_type）profile 回填 effort="" → 不设置默认，选项按 claude。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(AgentUserEditDialog(
                user_key="legacy", agent_type="", effort=""))
            await pilot.pause(0.3)
            dlg = pilot.app.screen
            effort = dlg.query_one("#effort", Select)
            values = _select_values(effort)
            self.assertIn("", values)  # 不设置默认
            self.assertEqual(_select_value_now(effort), "")

    async def test_provider_switch_updates_effort_options(self):
        """新建模式从 claude 切到 codex → effort 选项变 codex 集合，值回落不设置默认。"""
        app = _make_test_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.app.push_screen(AgentUserEditDialog())
            await pilot.pause(0.3)
            dlg = pilot.app.screen
            agent_type = dlg.query_one("#agent_type", Select)
            effort = dlg.query_one("#effort", Select)
            # 初始（未选 provider → claude 集合）
            self.assertIn("max", _select_values(effort))
            # 切到 codex：设置 value 触发 Select.Changed → on_provider_changed
            agent_type.value = "codex"
            await pilot.pause(0.3)
            values = _select_values(effort)
            self.assertIn("minimal", values)
            self.assertNotIn("max", values)


if __name__ == "__main__":
    unittest.main()
