"""
Claude leader 终端分类 — Bash/Edit 场景回归测试
================================================================

P2 风险:Claude Code 作为 leader 时,Bash/Edit 的输出/残留文本会被
`_classify_leader_terminal_output`(mult_agent_mcp.py:814)误分类:

- 命令输出中含 "running"/"writing"/"thinking" 等 busy 词 → 工具已完成、
  底部已回到 `❯` prompt,仍被钉在 **busy** → leader 永远进不了 resting,
  sleep 控制失效。
- 命令输出中含 "do you want to proceed" 等 approval 词 → 被误判 **approval**
  → 触发虚假 wakeup_approval,wakeup 控制错乱。
- 执行中(底部是 spinner/状态行,不是 prompt)→ 必须保持 **busy**,不能被
  判 idle 而提前进入 resting。

本测试断言修复后的语义:**busy/approval 只看底部“活动行”(input-box 状态行),
不再匹配滚动到上方的命令输出残留;idle 要求底部是 prompt/状态行。**

数据隔离:复用 MultAgentMcpContextTests 的 temp teams_data 隔离套路,
绝不触碰真实 teams_data.json;tmux 相关调用全部 mock。
"""

import json
import os
import re
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer

# 复用生产默认 wakeup 配置的简化副本,避免依赖内部常量
IDLE_FIXTURE = "✻ Brewed for 5s\n❯\n⏸ manual mode on"          # codex/claude 空闲
MID_TOOL_FIXTURE = "Running Bash: git fetch\n...\n◼"             # 执行中 spinner
POST_TOOL_IDLE = "✓ Applied edit to file.py\n(file.py)\n❯"      # 工具完成 + prompt
RESIDUAL_RUNNING = "collected 12 items\nRunning tests: 12 passed\n❯"   # 残留 busy 词
RESIDUAL_WRITING = "Writing output to build.log\nDone\n❯"             # 残留 busy 词
RESIDUAL_APPROVAL = "grep result:\nDo you want to proceed? = yes\n❯"  # 残留 approval 词
REAL_APPROVAL = "This command requires approval\nDo you want to proceed?\n❯ 1. Yes"
CODX_BUSY = "Thinking\n◼ running"
STREAMING = "Downloading deps...\n12%"


def _classify(text: str) -> str:
    return mcp._classify_leader_terminal_output(text)


class LeaderClassifierStateTests(unittest.TestCase):
    """直接单元测试 _classify_leader_terminal_output(无 tmux/数据依赖)。"""

    # ---- 执行中:必须 busy ----
    def test_mid_bash_spinner_is_busy(self):
        self.assertEqual(_classify(MID_TOOL_FIXTURE), "busy")

    def test_mid_tool_thinking_is_busy(self):
        self.assertEqual(_classify("Thinking...\n◼"), "busy")

    def test_mid_codex_running_is_busy(self):
        self.assertEqual(_classify(CODX_BUSY), "busy")

    # ---- 工具完成后 prompt 在底部:必须 idle(残留文本不影响) ----
    def test_post_tool_prompt_is_idle(self):
        self.assertEqual(_classify(POST_TOOL_IDLE), "idle")

    def test_residual_running_word_above_prompt_is_idle(self):
        """历史残留 "Running" 不再把已完成的 leader 钉成 busy。"""
        self.assertEqual(_classify(RESIDUAL_RUNNING), "idle")

    def test_residual_writing_word_above_prompt_is_idle(self):
        self.assertEqual(_classify(RESIDUAL_WRITING), "idle")

    def test_residual_approval_words_above_prompt_is_idle(self):
        """命令输出残留的 approval 词不是真实权限提示。"""
        self.assertEqual(_classify(RESIDUAL_APPROVAL), "idle")

    def test_bare_prompt_is_idle(self):
        self.assertEqual(_classify("❯"), "idle")

    def test_codex_idle_brewed_manual_mode_is_idle(self):
        """Codex 现有空闲行为保持不变。"""
        self.assertEqual(_classify(IDLE_FIXTURE), "idle")

    # ---- 真实权限提示:必须 approval ----
    def test_real_approval_prompt_is_approval(self):
        self.assertEqual(_classify(REAL_APPROVAL), "approval")

    # ---- 无 prompt 的流式输出:unknown(不判 idle,防止误睡) ----
    def test_streaming_output_no_prompt_is_unknown(self):
        self.assertEqual(_classify(STREAMING), "unknown")

    # ---- 崩溃到 shell:必须 dead ----
    def test_bare_shell_prompt_is_dead(self):
        self.assertEqual(_classify("user@host:~/repo$"), "dead")

    def test_empty_output_is_unknown(self):
        self.assertEqual(_classify(""), "unknown")


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类 + tmux mock 惯例(与 test_mult_agent_mcp 一致)。"""

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
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
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
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "work",
                            "last_task_completed": alice_done,
                        },
                    },
                }
            }
        })


class LeaderIdleGateTests(_IsolatedTestCase):
    """_leader_terminal_is_idle gate:残留文本不得阻断注入,执行中不得放行。"""

    def _idle(self, fake_leader_capture: str) -> bool:
        def fake_capture(session, window, lines=40):
            return 0, fake_leader_capture, ""

        self._save_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                with mock.patch.object(mcp, "_member_window_target", side_effect=lambda team_name, member_name: member_name):
                    return mcp._leader_terminal_is_idle("team", mcp._team_info("team"))

    def test_idle_gate_true_post_tool_with_residual_busy_text(self):
        """工具完成、底部回到 prompt,即使上方残留 "Running" 也可唤醒注入。"""
        self.assertTrue(self._idle(RESIDUAL_RUNNING))

    def test_idle_gate_true_with_residual_approval_text(self):
        self.assertTrue(self._idle(RESIDUAL_APPROVAL))

    def test_idle_gate_false_mid_bash(self):
        """执行中(底部 spinner)不是 idle → 不向运行中的工具注入唤醒消息。"""
        self.assertFalse(self._idle(MID_TOOL_FIXTURE))

    def test_idle_gate_false_streaming_no_prompt(self):
        self.assertFalse(self._idle(STREAMING))


class ClaudeLeaderWakeupPathTests(_IsolatedTestCase):
    """_monitor_team_wakeup_once 全链路:all_done / report 唤醒不被残留文本阻断。"""

    def _wakeup_once(self, fake_leader_capture: str, mark_idle_done=True):
        def fake_capture(session, window, lines=120):
            if window == "lead":
                return 0, fake_leader_capture, ""
            # 成员(非 leader)保持普通空闲,隔离被测的 leader 面板文本
            return 0, "✻ Brewed for 5s\n❯\n⏸ manual mode on", ""

        sent = []
        self._save_team(leader_state="resting", alice_done=True)
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text: sent.append((session, window, text)) or (0, "")):
                    with mock.patch.object(mcp, "_member_window_target", side_effect=lambda team_name, member_name: member_name):
                        result = mcp._monitor_team_wakeup_once("team", mark_idle_done=mark_idle_done)
        return result, sent

    def test_all_done_wakeup_not_blocked_by_residual_busy_text(self):
        """leader 空闲、成员全部完成 → 即使 leader 面板残留 "Running" 文本,
        all_done 唤醒消息仍应注入(修复前被误判 busy,注入被 gate 拦下)。"""
        result, sent = self._wakeup_once(RESIDUAL_RUNNING)
        self.assertEqual(result["action"]["action"], "wakeup_all_done")
        self.assertEqual(len(sent), 1, "残留 busy 文本不应阻断 all_done 唤醒注入")
        self.assertIn("all tracked member tasks appear complete", sent[0][2])
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["leader_state"], "active")

    def test_all_done_wakeup_not_blocked_by_residual_approval_text(self):
        result, sent = self._wakeup_once(RESIDUAL_APPROVAL)
        self.assertEqual(result["action"]["action"], "wakeup_all_done")
        self.assertEqual(len(sent), 1)
        self.assertEqual(mcp._load()["teams"]["team"]["leader_state"], "active")

    def test_wakeup_injection_skipped_while_mid_bash(self):
        """执行中(底部 spinner)被识别为 busy → resting 被拉回 active,且不注入
        (避免唤醒消息打进运行中的工具)。"""
        result, sent = self._wakeup_once(MID_TOOL_FIXTURE)
        self.assertEqual(sent, [], "执行中不应向 leader 注入唤醒消息")
        self.assertNotIn(result["action"]["action"], {"wakeup_all_done", "wakeup_approval"})
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["leader_state"], "active",
                         "执行中 leader 应从 resting 回到 active")


LEADER_MCP_PATTERNS = ("mcp__mult-agent-mcp__leader_*", "mcp__mult_agent_mcp__leader_*")
MEMBER_MCP_PATTERNS = ("mcp__mult-agent-mcp__member_*", "mcp__mult_agent_mcp__member_*")


def _tool_names(tools_str: str) -> set[str]:
    """'Bash(git:*),Edit(...),mcp_...' → {'Bash','Edit','mcp_...'}。"""
    return {t.split("(")[0].strip() for t in (tools_str or "").split(",") if t.strip()}


def _allowed_tools_str(cmd: list[str]) -> str:
    if "--allowedTools" not in cmd:
        return ""
    return cmd[cmd.index("--allowedTools") + 1]


class ClaudeTeamAllowedToolsTests(_IsolatedTestCase):
    """Claude 团队终端 --allowedTools 主验收(四路径 + 角色 MCP 白名单不串权)。

    最新要求:所有 Claude 团队终端均允许 Bash/Edit;leader 仍仅 leader_* MCP 工具,
    普通成员仍仅 member_* MCP 工具。覆盖:
      - MCP 主启动 (launch_team_terminals 的 leader new-session)
      - TUI 启动   (tui_screens.launch_terminals 的 leader/member)
      - 普通成员创建/恢复 (_tmux_spawn_member 的 member new-window)
      - leader revival (_revive_leader_terminal_locked 的 leader spawn)
    """

    def _assert_role_tools(self, cmd, role, label):
        self.assertIn("--allowedTools", cmd, f"{label}: 命令应含 --allowedTools")
        tools = _tool_names(_allowed_tools_str(cmd))
        self.assertIn("Bash", tools, f"{label}: 缺 Bash")
        self.assertIn("Edit", tools, f"{label}: 缺 Edit")
        # 精确放开 Bash/Edit 即可；不额外放开读写/搜索类工具
        for over in ("Read", "Write", "Glob", "Grep"):
            self.assertNotIn(over, tools, f"{label}: 不应额外放开 {over}")
        patterns = LEADER_MCP_PATTERNS if role == "leader" else MEMBER_MCP_PATTERNS
        for p in patterns:
            self.assertIn(p, tools, f"{label}: 缺角色 MCP 白名单 {p}")
        forbidden = "member" if role == "leader" else "leader"
        for t in tools:
            self.assertNotIn(forbidden, t, f"{label}: 角色 MCP 白名单串权 -> {t}")

    # ---- 路径1: MCP 主启动 ----
    def test_mcp_main_launch_claude_leader_allowed_tools(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text, **kw: (0, "")):
                            with mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda session, leader, prompt: (0, "")):
                                with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                                    with mock.patch.object(mcp, "claude_agent_user_launch", return_value=(["A=1"], str(workspace / "settings.json"))):
                                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                                            result = mcp.launch_team_terminals("team", task="t")
        self.assertIn("终端已启动", result)
        leader_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-session")
        self._assert_role_tools(leader_cmd, "leader", "MCP 主启动 leader")

    # ---- 路径2: 普通成员创建/恢复 ----
    def test_tmux_spawn_member_claude_allowed_tools(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "mcp_team": {
                    "workspace_dir": str(workspace),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                # 非空但无 alice 的窗口 → alice 判定 absent → 触发 spawn(而非 unknown fail-closed)
                return 0, "$1\t1000\t@1\totherwin", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "claude_agent_user_launch", return_value=(["A=1"], str(workspace / "settings.json"))):
                    rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))
        self.assertEqual(rc, 0, f"成员 spawn 失败: {err}")
        member_cmd = next(cmd for cmd in calls if cmd and cmd[0] == "new-window")
        self._assert_role_tools(member_cmd, "member", "成员创建/恢复 _tmux_spawn_member")

    # ---- 路径3: TUI 启动 ----
    def test_tui_launch_claude_leader_and_member_allowed_tools(self):
        from tui import tui_screens as tui

        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "leader": "lead",
            "leader_type": "tmux",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude"},
            },
        }
        tmux_calls = []

        def fake_tmux_run(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(tui, "_tmux_run", side_effect=fake_tmux_run):
            with mock.patch.object(tui, "load_data", return_value={"teams": {"team": team}}):
                with mock.patch.object(tui, "save_data", side_effect=lambda data, path=None: None):
                    with mock.patch.object(tui, "_leader_terminal_restart_blocked", return_value=False):
                        with mock.patch.object(tui, "configure_claude_mcp", return_value=(True, "ok")):
                            with mock.patch.object(tui, "configure_codex_mcp", return_value=(True, "ok")):
                                with mock.patch.object(tui, "write_claude_permissions", return_value=None):
                                    with mock.patch.object(tui, "claude_agent_user_launch", return_value=(["A=1"], str(workspace / "settings.json"))):
                                        with mock.patch.object(tui, "_member_window_state", return_value=("missing", "")):
                                            with mock.patch.object(tui, "_member_spawn_lock"):
                                                with mock.patch.object(tui.time, "sleep", return_value=None):
                                                    with mock.patch.object(tui, "_send_keys", side_effect=lambda session, window, text, **kw: (0, "")):
                                                        ok, msg = tui.launch_terminals("team")
        self.assertTrue(ok, msg)
        leader_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-session")
        member_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-window")
        self._assert_role_tools(leader_cmd, "leader", "TUI 启动 leader")
        self._assert_role_tools(member_cmd, "member", "TUI 启动 member")

    # ---- 路径4: leader revival ----
    def test_leader_revival_spawn_claude_allowed_tools(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_wakeup_config": {"enabled": True},
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        })
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "", ""
            if cmd[0] == "kill-window":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
                with mock.patch.object(mcp, "_leader_revival_allowed", return_value=True):
                    with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                        with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                                with mock.patch.object(mcp, "claude_agent_user_launch", return_value=(["A=1"], str(workspace / "settings.json"))):
                                    with mock.patch.object(mcp, "_member_window_target", return_value=None):
                                        with mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda session, leader, prompt: (0, "")):
                                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                                revived, msg = mcp._revive_leader_terminal_locked("team", reason="test")
        self.assertTrue(revived, msg)
        leader_cmd = next(cmd for cmd in calls if cmd and cmd[0] in {"new-session", "new-window"})
        self._assert_role_tools(leader_cmd, "leader", "leader revival spawn")

    # ---- 路径: TUI member recovery ----
    def test_tui_member_recovery_claude_allowed_tools(self):
        from tui.tui_screens import TeamDetailScreen

        workspace = self.root / "workspace"
        workspace.mkdir()
        team = {
            "workspace_dir": str(workspace),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "recover me", "last_task_completed": False},
            },
        }
        tmux_calls = []

        def fake_tmux_run(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "list-windows":
                return 0, "", ""
            if cmd[0] == "new-window":
                return 0, "", ""
            return 0, "", ""

        screen = TeamDetailScreen("team")
        with mock.patch.object(type(screen), "notify", lambda self, *a, **k: None):
            with mock.patch("tui.tui_screens._find_tmux_session", return_value="mcp_team"):
                with mock.patch("tui.tui_screens.load_data", return_value={"teams": {"team": team}}):
                    with mock.patch("tui.tui_screens._tmux_run", side_effect=fake_tmux_run):
                        with mock.patch("tui.tui_screens._member_spawn_lock"):
                            with mock.patch("tui.tui_screens._member_window_state", return_value=("missing", "")):
                                with mock.patch("tui.tui_screens.configure_claude_mcp", return_value=(True, "ok")):
                                    with mock.patch("tui.tui_screens.configure_codex_mcp", return_value=(True, "ok")):
                                        with mock.patch("tui.tui_screens.claude_agent_user_launch", return_value=(["A=1"], str(workspace / "settings.json"))):
                                            with mock.patch("tui.tui_screens._send_keys", side_effect=lambda session, window, text, **kw: (0, "")):
                                                screen._auto_recover_members()
        member_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-window")
        self._assert_role_tools(member_cmd, "member", "TUI member recovery")

    # ---- Codex 命令不受影响 ----
    def test_codex_leader_command_not_affected(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "codex"},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text, **kw: (0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team", task="t")
        self.assertIn("终端已启动", result)
        leader_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-session")
        self.assertNotIn("--allowedTools", leader_cmd,
                         "Codex leader 命令不应有 --allowedTools")
        self.assertNotIn("--permission-mode", leader_cmd,
                         "Codex leader 命令不应有 --permission-mode")

    def test_codex_member_command_not_affected(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "codex"},
                    },
                }
            }
        })
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                # 非空但无 alice 的窗口 → absent → 触发 spawn
                return 0, "$1\t1000\t@1\totherwin", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
                rc, _, _ = mcp._tmux_spawn_member("mcp_team", "alice", "codex", str(workspace))
        self.assertEqual(rc, 0)
        member_cmd = next(cmd for cmd in calls if cmd and cmd[0] == "new-window")
        self.assertNotIn("--allowedTools", member_cmd,
                         "Codex 成员命令不应有 --allowedTools")

    def test_shared_settings_have_no_role_mcp_patterns(self):
        """共享 .claude/settings.json 只含工作区 Edit + Bash 规则，不得含 member_*/leader_*。

        Reviewer A:共享 settings 被 leader 与成员共同加载，若含 member_* 会让 leader
        串权。三个 writer(mult_agent_mcp / common.mcp_config / common.tmux_utils)
        必须一致:角色 MCP 权限仅通过各成员 CLI --allowedTools 注入。
        """
        from common import mcp_config as cfg
        from common import tmux_utils as ctu

        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "mcp_team": {
                    "workspace_dir": str(workspace),
                    "members": {},
                }
            }
        })

        writers = [
            ("mult_agent_mcp", mcp._write_claude_permissions("mcp_team")),
            ("common.mcp_config", str(cfg.write_claude_permissions(str(workspace)))),
            ("common.tmux_utils", ctu._write_claude_permissions_internal("mcp_team", str(workspace))),
        ]
        for label, settings_path in writers:
            with open(settings_path) as f:
                allow = json.load(f)["permissions"]["allow"]
            self.assertTrue(any(r.startswith("Edit(") for r in allow),
                            f"{label}: 应保留工作区 Edit 规则")
            self.assertTrue(any(r == "Bash" or r.startswith("Bash(") for r in allow),
                            f"{label}: 应保留 Bash 规则")
            self.assertTrue(any(r == "Edit" for r in allow),
                            f"{label}: 应保留裸 Edit(CLAUDE_BASH_EDIT_ALLOW_PATTERNS)")
            for r in allow:
                self.assertNotIn("member_*", r, f"{label}: 共享 settings 含 member_* -> {r}")
                self.assertNotIn("leader_*", r, f"{label}: 共享 settings 含 leader_* -> {r}")


if __name__ == "__main__":
    unittest.main()
