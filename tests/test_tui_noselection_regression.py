"""
TUI NoSelection 回归测试。
==========================

task1 修复点：AgentUserEditDialog 新建 profile 时，provider Select 的初始值
必须使用 Select.NULL（NoSelection 哨兵）而非 None。直接传 None 会让 Textual
在 compose 阶段抛出 NoSelection，导致"新建 Agent 用户"对话框崩溃。

现有测试大多用 agent_type="claude"/"codex" 显式构造来"避开 Select(None) 崩溃"，
本文件直接覆盖空 agent_type（= 真实"新建"路径）的构造与校验行为，防止修复回退。

覆盖生产路径：
  - tui.tui_dialogs.AgentUserEditDialog（空 agent_type 构造、provider Select 哨兵）
  - AgentUserManageDialog.btn_new 触发的空 agent_type 编辑弹窗
"""

import unittest
from unittest import mock

from tui.tui_dialogs import AgentUserEditDialog


# 空的 team profile 列表（管理对话框用）
_MOCK_MANAGE_NO_PROFILES: dict = {}


class AgentUserEditDialogNoSelectionTests(unittest.IsolatedAsyncioTestCase):
    """空 agent_type（新建路径）构造不崩 NoSelection，且选择语义正确。"""

    async def test_new_dialog_empty_agent_type_uses_no_selection_sentinel(self):
        """空 agent_type 构造 → provider Select 初始值为 Select.NULL（哨兵），
        而非 None。这是 NoSelection 崩溃的修复点。"""
        from textual.app import App
        from textual.widgets import Select

        app = App()
        dialog = AgentUserEditDialog()  # 空 agent_type + is_new=True
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            # 不崩溃：当前屏幕就是编辑弹窗
            self.assertIs(pilot.app.screen, dialog,
                          "空 agent_type 构造不应抛 NoSelection，屏幕应为编辑弹窗")

            select = pilot.app.screen.query_one("#agent_type", Select)
            self.assertIs(select.value, Select.NULL,
                          "provider Select 初始值必须是 Select.NULL 哨兵而非 None")
            self.assertTrue(select._allow_blank,
                            "新建 profile 的 provider Select 应允许空值")

    async def test_new_dialog_save_without_provider_shows_error_not_crash(self):
        """空 agent_type 未选 provider 直接保存 → 弹出错误提示，且不关闭弹窗
        （不能崩溃、不能以错误值 dismiss）。"""
        from textual.app import App
        from textual.widgets import Input, Select
        from tui.tui_dialogs import MessageBox

        app = App()
        dialog = AgentUserEditDialog()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            # 先填写用户标识（key 校验先于 provider），再保存以触发 provider 校验
            key_input = pilot.app.screen.query_one("#key", Input)
            key_input.value = "profile_x"
            await pilot.pause(0.2)

            # 不选 provider，直接点保存
            await pilot.click("#btn_save")
            await pilot.pause(0.3)

            top = pilot.app.screen
            self.assertIsInstance(top, MessageBox,
                                  "未选 provider 保存应弹出 MessageBox 提示")
            self.assertIn("请选择 Provider", getattr(top, "_message", ""))
            # 编辑弹窗仍保留在栈中（未误关闭）
            self.assertIn(dialog, pilot.app.screen_stack)

    async def test_new_dialog_select_provider_then_fields_visible(self):
        """空 agent_type → 选 Claude → #claude_fields 显示；这是新建路径的
        完整可用性验证（修复后必须仍可正常编辑）。"""
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Select

        app = App()
        dialog = AgentUserEditDialog()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            select = pilot.app.screen.query_one("#agent_type", Select)
            select.value = "claude"
            await pilot.pause(0.2)

            self.assertTrue(
                pilot.app.screen.query_one("#claude_fields", Container).display,
                "选择 Claude 后应显示 Claude 字段组",
            )
            self.assertFalse(
                pilot.app.screen.query_one("#codex_fields", Container).display,
                "选择 Claude 后应隐藏 Codex 字段组",
            )


class AgentUserManageNoSelectionTests(unittest.IsolatedAsyncioTestCase):
    """Manage 对话框 → btn_new 打开空 agent_type 编辑弹窗（真实入口回归）。"""

    async def test_manage_new_opens_empty_agent_type_editor(self):
        """Manage → 新建 → 打开的空编辑弹窗 provider Select 为 Select.NULL。
        覆盖 AgentUserManageDialog 的 btn_new 真实入口。"""
        from textual.app import App
        from textual.widgets import Select
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_NO_PROFILES):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                before = pilot.app.screen
                await pilot.click("#btn_new")
                await pilot.pause(0.4)

                self.assertIsNot(pilot.app.screen, before, "新建应打开编辑弹窗")
                editor = pilot.app.screen
                self.assertIsInstance(editor, AgentUserEditDialog,
                                      "btn_new 应打开 AgentUserEditDialog")
                select = editor.query_one("#agent_type", Select)
                self.assertIs(select.value, Select.NULL,
                              "btn_new 打开的空编辑弹窗 provider 应为 Select.NULL 哨兵")


if __name__ == "__main__":
    unittest.main()
