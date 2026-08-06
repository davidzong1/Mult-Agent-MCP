"""
测试工作流中断自动恢复功能：
  A. 成员任务续跑  - member_get_my_task
  B. leader 激活/回报 - leader_activate + leader_pending_reports
  C. 成员回报触发 leader 激活/重建 - member_report_result 集成
  D. 纯函数层（common.leader_recovery） - member_pending_task / pending reports

遵循现有 test_completion_compact.py 模式: unittest + mock, 隔离全局状态。
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp

from common.leader_recovery import (
    member_pending_task,
    pending_leader_reports,
    append_leader_pending_report,
    build_leader_pending_reports_section,
    leader_has_unfinished_work,
    build_leader_recovery_section,
    MAX_PENDING_REPORTS,
)


class TestMemberTaskResume(unittest.TestCase):
    """成员任务续跑: member_get_my_task"""

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

    def _setup_team(self, *, completed=True, with_task=True):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        task = "实现工作流中断自动恢复" if with_task else ""
        context_txt = "需要支持成员续跑与 leader 激活" if with_task else ""
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {
                            "role": "coder", "agent": "claude",
                            "last_task": task,
                            "last_context": context_txt,
                            "last_task_completed": completed,
                        },
                    },
                }
            }
        })
        return workspace, context

    # ------------------------------------------------------------------
    # member_get_my_task
    # ------------------------------------------------------------------

    def test_resume_unfinished_task(self):
        """未完成任务 → 返回任务/上下文，并记录续跑时间戳"""
        self._setup_team(completed=False)
        result = mcp.member_get_my_task("team", "alice")
        self.assertIn("未完成任务", result)
        self.assertIn("实现工作流中断自动恢复", result)
        self.assertIn("需要支持成员续跑与 leader 激活", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_resume_count"], 1)
        self.assertTrue(member.get("last_resume_ts"))
        self.assertEqual(member["last_observed_state"], "busy")

    def test_resume_then_second_call_increments_count(self):
        """连续续跑 → last_resume_count 递增"""
        self._setup_team(completed=False)
        mcp.member_get_my_task("team", "alice")
        mcp.member_get_my_task("team", "alice")
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_resume_count"], 2)

    def test_task_already_completed(self):
        """任务已完成 → 提示无需续跑，不记录续跑"""
        self._setup_team(completed=True)
        result = mcp.member_get_my_task("team", "alice")
        self.assertIn("已完成", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertNotIn("last_resume_count", member)

    def test_no_task(self):
        """无任务 → 待命提示"""
        self._setup_team(completed=True, with_task=False)
        result = mcp.member_get_my_task("team", "alice")
        self.assertIn("没有未完成任务", result)

    def test_unknown_team(self):
        self.assertIn("不存在", mcp.member_get_my_task("nope", "alice"))

    def test_unknown_member(self):
        self._setup_team()
        self.assertIn("不存在", mcp.member_get_my_task("team", "ghost"))


class TestLeaderActivate(unittest.TestCase):
    """leader 激活/回报: leader_activate + leader_pending_reports"""

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

    def _setup_team(self, *, leader_state="active", reports=None):
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
            "leader_state": leader_state,
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        if reports is not None:
            team["leader_pending_reports"] = reports
        mcp._save({"teams": {"team": team}})
        return workspace, context

    # ------------------------------------------------------------------
    # leader_activate
    # ------------------------------------------------------------------

    def test_activate_with_pending_reports_clears_queue(self):
        """leader_activate 列出并清空待处理回报"""
        self._setup_team(reports=[{"member": "alice", "result": "完成登录", "timestamp": "2026-08-06T10:00:00"}])
        result = mcp.leader_activate("team")
        self.assertIn("成员回报 1 条", result)
        self.assertIn("完成登录", result)
        self.assertEqual(mcp._load()["teams"]["team"].get("leader_pending_reports"), [])
        self.assertEqual(mcp._load()["teams"]["team"]["leader_state"], "active")

    def test_activate_no_reports(self):
        """无待处理回报 → 提示空"""
        self._setup_team()
        result = mcp.leader_activate("team")
        self.assertIn("没有待处理的成员回报", result)

    def test_activate_wakes_from_resting(self):
        """从 resting 唤醒 → 提示唤醒"""
        self._setup_team(leader_state="resting")
        result = mcp.leader_activate("team")
        self.assertIn("从休息中唤醒", result)

    def test_activate_lists_unfinished_work(self):
        """存在未完成成员任务 → 列出"""
        self._setup_team()
        result = mcp.leader_activate("team")
        self.assertIn("未完成工作", result)
        self.assertIn("alice", result)

    def test_activate_unknown_team(self):
        self.assertIn("不存在", mcp.leader_activate("nope"))


class TestMemberReportLeaderActivation(unittest.TestCase):
    """成员回报 → leader 激活/重建 集成"""

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
        self.old_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux_window_exists": mcp._tmux_window_exists,
            "_tmux": mcp._tmux,
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
        for key, value in self.old_funcs.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def _setup_team(self, *, leader_type="tmux", leader_state="resting",
                    wakeup_enabled=True, with_leader_terminal=True):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        members = {
            "lead": {"role": "leader", "agent": "claude"},
            "alice": {"role": "coder", "agent": "claude",
                      "last_task": "完成登录模块", "last_task_completed": False,
                      "tmux_window_id": "@7", "tmux_session": "mcp_team",
                      "tmux_session_id": "$1", "tmux_session_created": "1000"},
        }
        if with_leader_terminal:
            members["lead"].update(tmux_window_id="@1", tmux_session="mcp_team",
                                   tmux_session_id="$1", tmux_session_created="1000")
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": leader_type,
            "leader_state": leader_state,
            "leader_last_task": "总任务：完成登录系统",
            "leader_last_task_completed": False,
            "leader_wakeup_config": {"enabled": wakeup_enabled},
            "members": members,
        }
        mcp._save({"teams": {"team": team}})
        return workspace, context

    @staticmethod
    def _alive_tmux():
        def fake(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\talice", ""
            return 0, "", ""
        return fake

    def test_report_injects_wakeup_when_leader_live_resting(self):
        """leader 存活且 resting → 注入回报摘要并激活"""
        self._setup_team()
        with mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True), \
             mock.patch.object(mcp, "_send_context_to_member", return_value=(0, "")) as send, \
             mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            result = mcp.member_report_result("team", "完成登录", member_name="alice")
        self.assertIn("已唤醒 leader 并注入本次回报", result)
        send.assert_called_once()
        self.assertEqual(mcp._load()["teams"]["team"]["leader_state"], "active")
        # 回报已持久化
        reports = mcp._load()["teams"]["team"].get("leader_pending_reports", [])
        self.assertTrue(any(r.get("result") == "完成登录" for r in reports))

    def test_report_revives_dead_leader(self):
        """leader 终端已死 → member_report_result 触发幂等重建并恢复未完成总任务"""
        self._setup_team()
        with mock.patch.object(mcp, "_leader_window_is_dead", return_value=True), \
             mock.patch.object(mcp, "_maybe_revive_leader", return_value=(True, "revived")) as revive, \
             mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
            result = mcp.member_report_result("team", "完成登录", member_name="alice")
        self.assertIn("已自动恢复", result)  # 独立 revival 闭环的提示
        revive.assert_called_once_with("team", reason="member_report")
        # 回报已持久化，等待重建后的 leader 读取
        reports = mcp._load()["teams"]["team"].get("leader_pending_reports", [])
        self.assertTrue(any(r.get("result") == "完成登录" for r in reports))

    def test_report_queues_when_revival_denied(self):
        """leader 已死但重建被限流/关闭 → 回报记入待处理列表"""
        self._setup_team()
        with mock.patch.object(mcp, "_leader_window_is_dead", return_value=True), \
             mock.patch.object(mcp, "_maybe_revive_leader", return_value=(False, "rate-limited")), \
             mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
            result = mcp.member_report_result("team", "完成登录", member_name="alice")
        self.assertIn("记入 leader 待处理列表", result)
        self.assertNotIn("已自动恢复", result)
        reports = mcp._load()["teams"]["team"].get("leader_pending_reports", [])
        self.assertTrue(any(r.get("result") == "完成登录" for r in reports))

    def test_direct_leader_no_terminal_operation(self):
        """direct leader → 不做终端操作，回报记入待处理列表"""
        self._setup_team(leader_type="direct", with_leader_terminal=False)
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
            result = mcp.member_report_result("team", "完成登录", member_name="alice")
        self.assertIn("记入 leader 待处理列表", result)
        reports = mcp._load()["teams"]["team"].get("leader_pending_reports", [])
        self.assertTrue(any(r.get("result") == "完成登录" for r in reports))


class TestLeaderRecoveryPureHelpers(unittest.TestCase):
    """common.leader_recovery 纯函数层"""

    def test_member_pending_task_returns_snapshot(self):
        team = {
            "leader": "lead",
            "members": {
                "lead": {"role": "leader", "agent": "codex"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "实现续跑", "last_context": "上下文",
                          "last_task_completed": False},
            },
        }
        snap = member_pending_task(team, "alice")
        self.assertEqual(snap["member_name"], "alice")
        self.assertEqual(snap["task"], "实现续跑")
        self.assertEqual(snap["context"], "上下文")
        self.assertEqual(snap["agent"], "claude")
        self.assertEqual(snap["team_leader"], "lead")

    def test_member_pending_task_none_when_completed(self):
        team = {"leader": "lead", "members": {"alice": {"last_task": "x", "last_task_completed": True}}}
        self.assertIsNone(member_pending_task(team, "alice"))

    def test_member_pending_task_none_when_missing(self):
        team = {"leader": "lead", "members": {}}
        self.assertIsNone(member_pending_task(team, "ghost"))
        self.assertIsNone(member_pending_task(team, "alice"))

    def test_append_pending_report_bounded(self):
        team = {"leader_pending_reports": []}
        for i in range(MAX_PENDING_REPORTS + 5):
            append_leader_pending_report(team, {"member": f"m{i}", "result": "r"})
        reports = pending_leader_reports(team)
        self.assertEqual(len(reports), MAX_PENDING_REPORTS)
        self.assertEqual(reports[0]["member"], f"m{5}")

    def test_pending_reports_are_unfinished_work(self):
        team = {"leader": "lead", "members": {}, "leader_pending_reports": [{"member": "a", "result": "r"}]}
        self.assertTrue(leader_has_unfinished_work(team))

    def test_reports_section_built(self):
        team = {"leader": "lead", "members": {},
                "leader_pending_reports": [{"member": "alice", "result": "完成登录", "timestamp": "2026-08-06T10:00:00"}]}
        lines = build_leader_pending_reports_section("team", team)
        text = "\n".join(lines)
        self.assertIn("成员回报待处理", text)
        self.assertIn("alice", text)
        self.assertIn("leader_activate", text)

    def test_recovery_section_includes_pending_reports(self):
        team = {"leader": "lead", "members": {},
                "leader_pending_reports": [{"member": "alice", "result": "完成登录"}]}
        lines = build_leader_recovery_section("team", team, "/tmp/w", "/tmp/s")
        text = "\n".join(lines)
        self.assertIn("检测到未完成团队工作", text)  # pending reports ⇒ resume 分支
        self.assertIn("完成登录", text)


if __name__ == "__main__":
    unittest.main()
