"""RC2 注入门控的基线情形(已按 RC2 后语义同步)。

G1: leader_type=tmux、从未调过 leader_sleep(leader_state=active,
    report_wakeup_enabled 默认 True) → RC2 后终端 idle 即注入唤醒
G2: 调过 leader_sleep 且已超时唤醒(sleep_until 已 pop、state=active、
    enabled 仍 True——leader_sleep 强制过) → RC2 后仍注入,不依赖 resting
正常态: 调过 leader_sleep 且仍 resting、终端 idle → 注入成功, injected=True,
    且落盘 leader_state 转 active、leader_wakeup_reason="report"

RC2 门控:_notify_leader_of_report 注入条件 = report_wakeup_enabled + 终端 idle
(任何 leader_state),去掉了 enabled 与 resting 两道门;冷却期(60s)内跳过注入
(reason="report-cooldown")但回报已先入 leader_pending_reports,信息不丢。

隔离模式与 test_leader_sleep.py 一致: unittest+mock 全量覆写模块全局路径,
不写真实 ~/.mult_agent_mcp/。
"""
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class LeaderSleepGapProbe(unittest.TestCase):
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
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "MULT_AGENT_MCP_CONTEXT_DIR")
        }
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
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
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _team(self, **overrides):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            "members": {
                "lead": {"role": "leader", "agent": "claude",
                         "tmux_window_id": "@1", "tmux_session": "team_sess"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    def _report_entry(self):
        return {
            "member": "alice", "result": "完成登录模块", "timestamp": "2026-08-09T12:00:00",
            "compressed_context_path": "", "artifact_path": "",
        }

    def _inject_mocks(self, terminal_idle=True):
        sent = []
        def fake_send(session, target, message, **kw):
            sent.append(message)
            return 0, ""
        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=False),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=terminal_idle),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_send_context_to_member", side_effect=fake_send),
            mock.patch.object(mcp, "_target_is_claude_tmux_leader", return_value=False),
        ]
        return mocks, sent

    # ------------------------------------------------------------------
    # G1 / G2 / 正常态
    # ------------------------------------------------------------------

    def test_g1_active_never_slept_no_inject(self):
        """G1: tmux leader、从未 leader_sleep(active, report_wakeup_enabled 默认 True) → 回报即注入(RC2 后语义)

        RC2 后语义: _notify_leader_of_report 门控 = report_wakeup_enabled + 终端 idle,
        去掉了 enabled 与 resting 两道门 → leader 从未 sleep、state=active 时,
        终端 idle 即被成员回报唤醒注入(这是 RC2 打开的核心缺口)。
        """
        self._team()  # 默认 active,无 leader_wakeup_config → report_wakeup_enabled 默认 True
        mocks, sent = self._inject_mocks()
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry())
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], "RC2 后 active + idle 应注入唤醒")
        self.assertNotEqual(sent, [], "G1 应注入回报摘要到 leader 终端")
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(t.get("leader_wakeup_reason"), "report")

    def test_g2_woken_state_active_no_inject(self):
        """G2: leader_sleep 后已超时唤醒(until 已 pop、active、enabled 仍 True) → 回报仍注入(RC2 后语义)

        RC2 后语义: 注入不依赖 resting,超时唤醒后 state=active 也照样注入;
        sleep_until 已 pop(无残留),不会误触 wakeup_timeout 双注入。
        """
        self._team(
            leader_state="active",
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
            # 无 leader_sleep_until —— 模拟已 pop
        )
        mocks, sent = self._inject_mocks()
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry())
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], "RC2 后 active(enabled 仍 True)+ idle 应注入")
        self.assertNotEqual(sent, [], "G2 应注入回报摘要到 leader 终端")
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")

    def test_normal_resting_idle_injects(self):
        """正常态: leader_sleep 后仍 resting、终端 idle → 注入成功, 置 active"""
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() + timedelta(seconds=300)).isoformat(),
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry())
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"])
        self.assertEqual(len(sent), 1, "正常态应注入 1 条")
        self.assertIn("Leader activation: a member reported a result.", sent[0])
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(t["leader_wakeup_reason"], "report")
        self.assertNotIn("leader_sleep_until", t)  # 若缺失 pop 则此处转红


if __name__ == "__main__":
    unittest.main()
