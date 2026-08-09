"""
Claude leader 分类器 — 执行中误判 idle 的定向回归测试
================================================================

P2 风险:Claude Code 作为 tmux leader 执行 Bash/Edit 时,若命令 stdout 的
最后一行恰好是 ``❯``(REPL/嵌套 CLI 回显)或 spinner/``◼`` 停在 ``❯`` 输入行
上方,`_classify_leader_terminal_output`(mult_agent_mcp.py) 曾把它判成 idle
→ `_leader_terminal_is_idle` gate 放行 → wakeup 注入打进运行中的工具。

本文件锁定的语义(**执行中永不 idle**):
  - 命令 stdout 尾部回显 ``❯`` 而上方是 shell 子提示($/>)→ 不是 Claude
    prompt → unknown(不判 idle)。
  - ``❯`` 输入行上方存在实时 spinner/``◼`` → 工具在跑 → unknown(不判 idle)。
  - 工具完成回到 Claude prompt(``❯`` 为底部活动元素)→ idle,可进入 resting。
  - 残留 busy/approval 词不阻断 idle(回归,与 test_leader_classifier_claude_tools
    语义一致)。

数据隔离:复用 temp teams_data 隔离 + mock tmux,绝不触碰真实 teams_data.json。
"""

import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer


def _classify(text: str) -> str:
    return mcp._classify_leader_terminal_output(text)


class MidToolNeverIdleTests(unittest.TestCase):
    """执行中 Bash/Edit —— 绝不判 idle(可为 unknown/busy)。"""

    def test_repl_stdout_ending_in_prompt_arrow_is_not_idle(self):
        """REPL 式命令 stdout 尾部 ``❯`` 是命令输出,不是 Claude prompt。"""
        # 上方是 shell 子提示 `nested>`,最后的 ``❯`` 是子 CLI 的回显。
        got = _classify("$ cli\nnested> \n❯")
        self.assertNotEqual(got, "idle", "执行中命令 stdout 尾部 ❯ 不得判 idle")
        self.assertEqual(got, "unknown")

    def test_shell_prompt_echo_then_arrow_is_not_idle(self):
        got = _classify("$ ./install.sh\nInstalling... done\n❯")
        self.assertNotEqual(got, "idle")

    def test_spinner_above_input_prompt_is_not_idle(self):
        """``❯`` 输入行仍在屏,但其上方是实时 spinner → 工具执行中,非 idle。"""
        got = _classify("┌─ Bash ─\n│ pytest [80%]\n⠹ Running Bash\n❯")
        self.assertNotEqual(got, "idle")
        self.assertEqual(got, "unknown")

    def test_stop_button_above_input_prompt_is_not_idle(self):
        got = _classify("│ pytest [90%]\n│ pytest [100%]\n◼ Stop\n❯")
        self.assertNotEqual(got, "idle")
        self.assertEqual(got, "unknown")

    def test_spinner_line_alone_is_busy(self):
        """执行中实时状态行(无 ❯)保持 busy。"""
        self.assertEqual(_classify("⠹ Running Bash (3s)\n◼ Stop"), "busy")
        self.assertEqual(_classify("✢ Waddling… (42s)"), "busy")

    def test_long_stdout_no_prompt_is_unknown(self):
        """长输出且底部无 prompt → unknown(不误判 idle)。"""
        got = _classify("$ pytest -x\ncollecting 5 items\ntest_foo.py .....F")
        self.assertEqual(got, "unknown")


class ReadyPromptStillIdleTests(unittest.TestCase):
    """工具完成回到 Claude prompt —— 必须 idle(可 resting/被 wake)。"""

    def test_post_bash_prompt_is_idle(self):
        self.assertEqual(_classify("✓ 3 files changed, 5 insertions\n✻ Brewed for 5s\n❯"), "idle")

    def test_post_edit_prompt_is_idle(self):
        self.assertEqual(_classify("✓ Applied edit to file.py\n(file.py)\n❯"), "idle")

    def test_prompt_above_footer_is_idle(self):
        self.assertEqual(_classify("✻ Brewed for 5s\n❯\nAuto-accept edits: off"), "idle")
        self.assertEqual(_classify("✻ Brewed for 5s\n❯\nToken count: 12.4k"), "idle")

    def test_residual_busy_word_does_not_break_idle(self):
        self.assertEqual(_classify("collected 12 items\nRunning tests: 12 passed\n❯"), "idle")
        self.assertEqual(_classify("Writing output to build.log\nDone\n❯"), "idle")

    def test_residual_approval_word_does_not_break_idle(self):
        self.assertEqual(_classify("grep result:\nDo you want to proceed? = yes\n❯"), "idle")

    def test_tool_block_close_above_prompt_is_idle(self):
        """已闭合工具块(└─ done)+ 底部 ❯ = 完成,非执行中。"""
        self.assertEqual(_classify("┌─ Edit ─\n│ + new line\n└─ done\n❯"), "idle")


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类 + tmux mock 惯例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "SHARE_WORKSPACE_DIR": mcp.SHARE_WORKSPACE_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
        }
        self.old_funcs = {"_find_any_session": mcp._find_any_session}
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                        "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_funcs.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, leader_state="resting", alice_done=True):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_state": leader_state,
                    "leader_wakeup_config": {
                        "enabled": True,
                        "idle_threshold": 1,
                        "approval_alert": True,
                        "auto_authorize_first": True,
                        "cooldown_cycles": 3,
                        "max_wakeups_per_session": 10,
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {
                            "role": "coder", "agent": "claude",
                            "last_task": "work",
                            "last_task_completed": alice_done,
                        },
                    },
                }
            }
        })


class LeaderIdleGateMidToolTests(_IsolatedTestCase):
    """_leader_terminal_is_idle gate:执行中 false,完成回 prompt true。"""

    def _idle(self, leader_capture: str) -> bool:
        def fake_capture(session, window, lines=40):
            return 0, leader_capture, ""

        self._save_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                with mock.patch.object(mcp, "_member_window_target",
                                       side_effect=lambda t, m: m):
                    return mcp._leader_terminal_is_idle("team", mcp._team_info("team"))

    def test_gate_false_repl_stdout_arrow(self):
        self.assertFalse(self._idle("$ cli\nnested> \n❯"))

    def test_gate_false_spinner_above_prompt(self):
        self.assertFalse(self._idle("┌─ Bash ─\n│ pytest [80%]\n⠹ Running Bash\n❯"))

    def test_gate_false_stop_above_prompt(self):
        self.assertFalse(self._idle("│ pytest [100%]\n◼ Stop\n❯"))

    def test_gate_true_post_tool_prompt(self):
        self.assertTrue(self._idle("✓ Applied edit to file.py\n(file.py)\n❯"))


class WakeupPathMidToolTests(_IsolatedTestCase):
    """全链路:执行中不注入唤醒;完成回 prompt 才注入。"""

    def _wakeup_once(self, leader_capture: str):
        def fake_capture(session, window, lines=120):
            return 0, leader_capture, ""

        sent = []
        self._save_team(leader_state="resting", alice_done=True)
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                with mock.patch.object(mcp, "_send_keys",
                                       side_effect=lambda s, w, text, **kw: sent.append((s, w, text)) or (0, "")):
                    with mock.patch.object(mcp, "_member_window_target",
                                           side_effect=lambda t, m: m):
                        result = mcp._monitor_team_wakeup_once("team", mark_idle_done=True)
        return result, sent

    def test_wakeup_skipped_while_repl_stdout_arrow(self):
        """命令 stdout 尾部 ``❯`` 的执行中 leader:不注入唤醒。"""
        result, sent = self._wakeup_once("$ cli\nnested> \n❯")
        self.assertEqual(sent, [], "执行中(stdout 回显 ❯)不得向 leader 注入唤醒")
        self.assertEqual(result["action"]["action"], "wakeup_all_done")

    def test_wakeup_injected_after_tool_completes_to_prompt(self):
        """工具完成、回到 ❯ prompt:all_done 唤醒注入(可被 wake)。"""
        result, sent = self._wakeup_once("✓ 3 files changed, 5 insertions\n✻ Brewed for 5s\n❯")
        self.assertEqual(len(sent), 1, "工具完成回 prompt 应注入 all_done 唤醒")
        self.assertIn("all tracked member tasks appear complete", sent[0][2])


# =====================================================================
# Reviewer 12:12 REVISE B/C/D —— 真实 Claude pane fixtures
# =====================================================================
# B: 运行时 spinner 用 ✽ U+273D 且底部只剩 "esc to interrupt" footer
# C: 空闲 prompt 的 footer 是 "auto mode on"(auto 模式)
# D: approval choices 位于 footer("auto mode on")上方
REAL_RUNNING_ESC = "✽ Running Bash… (3s)\nesc to interrupt"
REAL_RUNNING_ESC_WADDLE = "✽ Waddling… (42s · ↓ 5.3k tokens)\nesc to interrupt"
REAL_RUNNING_ESC_FOOTER_ONLY = (
    "previous command output\n"
    "❯\n"
    "────────────────────────────────\n"
    "⏵⏵ accept edits on · esc to interrupt · ctrl+t to hide tasks"
)
REAL_IDLE_AUTO = "✻ Brewed for 5s\n❯\nauto mode on"
REAL_IDLE_AUTO_FOOTER_ONLY = "✻ Brewed for 5s\nauto mode on"
REAL_APPROVAL_ABOVE_FOOTER = (
    "This command requires approval\n"
    "Do you want to proceed?\n"
    "❯ 1. Yes\n"
    "  2. Yes, and don't ask again for this command\n"
    "auto mode on"
)


class RealClaudePaneReviewerTests(unittest.TestCase):
    """B/C/D 直接分类断言:真实 Claude pane 不再误判。"""

    def test_b_running_esc_spinner_heavy_asterisk_is_busy(self):
        """✽ U+273D spinner + 仅剩 esc footer:判 busy(不判 idle/unknown)。"""
        self.assertEqual(_classify(REAL_RUNNING_ESC), "busy")
        self.assertEqual(_classify(REAL_RUNNING_ESC_WADDLE), "busy")

    def test_b_running_esc_never_idle(self):
        self.assertNotEqual(_classify(REAL_RUNNING_ESC), "idle")
        self.assertNotEqual(_classify(REAL_RUNNING_ESC_WADDLE), "idle")

    def test_b_running_footer_only_is_busy(self):
        """Spinner 已滚出捕获区时，interrupt footer 仍是运行态权威信号。"""
        self.assertEqual(_classify(REAL_RUNNING_ESC_FOOTER_ONLY), "busy")

    def test_c_idle_auto_mode_footer_is_idle(self):
        """空闲 prompt + auto mode on footer:判 idle(可 resting/被 wake)。"""
        self.assertEqual(_classify(REAL_IDLE_AUTO), "idle")
        self.assertEqual(_classify(REAL_IDLE_AUTO_FOOTER_ONLY), "idle")

    def test_d_approval_choices_above_footer_is_approval(self):
        """approval choices 位于 footer 上方:判 approval,不被 footer 吞掉。"""
        self.assertEqual(_classify(REAL_APPROVAL_ABOVE_FOOTER), "approval")


class RealClaudePaneMemberClassifierTests(unittest.TestCase):
    """B/C/D 对成员分类器同样生效(同源 busy 信号)。"""

    def test_b_member_running_esc_is_busy(self):
        self.assertEqual(mcp._classify_terminal_output(REAL_RUNNING_ESC), "busy")
        self.assertEqual(mcp._classify_terminal_output(REAL_RUNNING_ESC_WADDLE), "busy")
        self.assertEqual(
            mcp._classify_terminal_output(REAL_RUNNING_ESC_FOOTER_ONLY), "busy"
        )

    def test_c_member_idle_auto_mode_is_idle(self):
        self.assertEqual(mcp._classify_terminal_output(REAL_IDLE_AUTO), "idle")

    def test_d_member_approval_above_footer_is_approval(self):
        self.assertEqual(mcp._classify_terminal_output(REAL_APPROVAL_ABOVE_FOOTER), "approval")


class RealClaudePaneGateWakeupTests(_IsolatedTestCase):
    """B/C/D 的 idle gate 与 wakeup 全链路断言。"""

    def _idle(self, leader_capture: str) -> bool:
        def fake_capture(session, window, lines=40):
            return 0, leader_capture, ""

        self._save_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                with mock.patch.object(mcp, "_member_window_target",
                                       side_effect=lambda t, m: m):
                    return mcp._leader_terminal_is_idle("team", mcp._team_info("team"))

    def test_gate_false_running_esc(self):
        """执行中(✽+esc footer)不是 idle gate → 不注入。"""
        self.assertFalse(self._idle(REAL_RUNNING_ESC))

    def test_gate_false_running_footer_only(self):
        """Spinner 滚出后，interrupt footer 仍不得通过 idle gate。"""
        self.assertFalse(self._idle(REAL_RUNNING_ESC_FOOTER_ONLY))

    def test_gate_true_idle_auto_mode(self):
        """空闲(auto mode on)是 idle gate → 可注入唤醒。"""
        self.assertTrue(self._idle(REAL_IDLE_AUTO))

    def test_gate_false_approval_above_footer(self):
        """approval(choices 在 footer 上方)不是 idle gate。"""
        self.assertFalse(self._idle(REAL_APPROVAL_ABOVE_FOOTER))

    def _wakeup_once(self, leader_capture: str):
        def fake_capture(session, window, lines=120):
            return 0, leader_capture, ""

        sent = []
        self._save_team(leader_state="resting", alice_done=True)
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                with mock.patch.object(mcp, "_send_keys",
                                       side_effect=lambda s, w, text, **kw: sent.append((s, w, text)) or (0, "")):
                    with mock.patch.object(mcp, "_member_window_target",
                                           side_effect=lambda t, m: m):
                        result = mcp._monitor_team_wakeup_once("team", mark_idle_done=False)
        return result, sent

    def test_wakeup_skipped_while_running_esc(self):
        """执行中(✽+esc footer):busy 扫描把 leader 翻回 active,不注入唤醒。"""
        result, sent = self._wakeup_once(REAL_RUNNING_ESC)
        self.assertEqual(sent, [], "执行中(✽ Running)不得注入唤醒")
        self.assertEqual(result["action"]["action"], "none")
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["leader_state"], "active",
                         "busy leader 应从 resting 回到 active(不休息不唤醒)")

    def test_wakeup_skipped_with_footer_only_live_signal(self):
        result, sent = self._wakeup_once(REAL_RUNNING_ESC_FOOTER_ONLY)
        self.assertEqual(sent, [], "interrupt footer 存在时不得注入唤醒")
        self.assertEqual(result["action"]["action"], "none")

    def test_wakeup_injected_when_idle_auto_mode(self):
        """空闲(auto mode on):all_done 唤醒注入。"""
        result, sent = self._wakeup_once(REAL_IDLE_AUTO)
        self.assertEqual(len(sent), 1, "空闲(auto mode on)应注入 all_done 唤醒")
        self.assertIn("all tracked member tasks appear complete", sent[0][2])
        self.assertTrue(result["action"].get("injected", False))


if __name__ == "__main__":
    unittest.main()
