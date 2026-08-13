"""claim_leader tmux 语义保持 —— 元数据撕裂修复回归测试。

背景（reviewer/refactor 根因，2026-08-12）：
  claim_leader / TUI action_claim_leader 此前把**受管且存活的 tmux leader**
  无条件降级为普通成员并覆盖 ``leader_type="direct"``，却不清理原 tmux 窗口
  → 元数据撕裂：``leader_type='direct'`` 但 ``leader`` 仍指向一个带活 tmux
  窗口的成员名，``_is_direct_leader_member`` 纯名字匹配误判、``leader_list_team``
  误显示 DIRECT-LEADER、回报注入/唤醒门控全被 direct 拦截（回报滞留）。

修复：
  ``common.leader_recovery.claim_keeps_tmux_leader(team, session_alive,
  window_alive)`` —— 受管（members[leader].role=='leader'）+ 存活（session +
  window alive）同时成立 → 同名 claim **保持 tmux 语义**（不降级、不覆盖
  direct）。MCP ``claim_leader``（mult_agent_mcp.py:5204）与 TUI
  ``action_claim_leader``（tui_screens.py:2245）共用同一判定，防两处漂移。

本文件覆盖 coder 在 tests/test_leader_member_recovery.py（纯函数 + 两条
claim 行为）之外的**集成闭环**维度：
  A. 保持 tmux 后回报注入仍走 tmux 路径（_notify_leader_of_report 不早退）
  B. 展示/通知语义：leader_list_team 显示 tmux 存活 + 👑LEADER，不误显
     DIRECT-LEADER / 直接控制；_is_direct_leader_member 判 False；保持消息
     不含"已接管/降级"
  C. 重启场景：受管 leader 重启重新进入（窗口存活）→ 保持 tmux 且
     leader_recovery_count 不递增（不误 reentry）；死窗接管才递增
  D. 死窗口/无窗口处理：受管 leader 窗口死亡 → direct 接管 + 消息"已关闭"
  E. TUI/MCP 共用同一 claim_keeps_tmux_leader（防漂移）
  F. 纯函数补充边界（leader_type='direct' / 空 team / leader 不在成员表）

隔离：与既有测试一致，setUp 将 mcp 全局路径重定向到临时目录，不触碰真实
teams_data.json / cppipc-dds 状态。
"""

import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class _IsolatedMcp(unittest.TestCase):
    """重定向 mcp 全局路径到临时目录的基类（同 test_leader_wakeup_injection）。"""

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
            for key in (
                "MULT_AGENT_MCP_WORKSPACE", "MULT_AGENT_MCP_CONTEXT_DIR",
                "MULT_AGENT_MCP_HOME",
            )
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

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _team(self, **overrides):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            # 有未完成任务 → leader_has_unfinished_work()=True → reentry 会递增计数
            "leader_last_task": "总任务",
            "leader_last_task_completed": False,
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    def _claim(self, session_alive=True, window_alive=True):
        """mock tmux 存活面后调用 claim_leader。"""
        with mock.patch.object(mcp, "_tmux_session_alive", return_value=session_alive):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=window_alive):
                return mcp.claim_leader("team")

    def _report_inject_mocks(self):
        """回报注入闭环的 mock 面（复用 test_leader_wakeup_injection V 系列）。"""
        sent = []

        def fake_send(session, target, message, **kw):
            sent.append(message)
            return 0, ""

        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=False),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_send_context_to_member", side_effect=fake_send),
            mock.patch.object(mcp, "_target_is_claude_tmux_leader", return_value=False),
        ]
        for m in mocks:
            m.start()
        return mocks, sent


class TestClaimKeepsTmuxIntegration(_IsolatedMcp):
    """MCP claim_leader 保持 tmux 语义的集成闭环。"""

    def test_claim_managed_alive_keeps_tmux(self):
        """受管+存活 tmux leader：claim 保持 tmux，返回消息不含接管/降级。"""
        self._team()
        result = self._claim(session_alive=True, window_alive=True)

        team = mcp._load()["teams"]["team"]
        self.assertIn("保持 tmux 语义", result)
        self.assertIn("未覆盖为 direct", result)
        self.assertNotIn("已接管", result)
        self.assertNotIn("降级", result)
        self.assertEqual(team["leader_type"], "tmux")            # 未被覆盖为 direct
        self.assertEqual(team["leader"], "lead")
        self.assertEqual(team["members"]["lead"]["role"], "leader")  # 未降级

    def test_claim_managed_alive_keeps_tmux_no_direct_member_flag(self):
        """保持 tmux 后 _is_direct_leader_member 判 False（消除纯名字误判）。"""
        self._team()
        self._claim(session_alive=True, window_alive=True)
        team = mcp._load()["teams"]["team"]
        self.assertFalse(mcp._is_direct_leader_member(team, "lead"))

    def test_claim_managed_alive_keeps_tmux_list_team_not_direct(self):
        """保持 tmux 后 leader_list_team 显示 tmux 存活 + 👑LEADER，不误显 direct。"""
        self._team()
        self._claim(session_alive=True, window_alive=True)
        with mock.patch.object(mcp, "_tmux_session_alive", return_value=True):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                panel = mcp.leader_list_team("team")

        self.assertIn("(tmux 🟢存活)", panel)          # tmux 存活展示
        self.assertIn("👑LEADER", panel)               # 受管 tmux leader 标记
        self.assertNotIn("DIRECT-LEADER", panel)       # 不误显示 direct
        self.assertNotIn("直接控制", panel)            # 不误显示"当前会话直接控制"

    def test_claim_managed_alive_report_inject_still_tmux(self):
        """【回报注入回归】保持 tmux 后回报注入仍走 tmux 路径，不被 direct 门控拦截。"""
        self._team()
        self._claim(session_alive=True, window_alive=True)

        mocks, sent = self._report_inject_mocks()
        try:
            result = mcp._notify_leader_of_report(
                "team", {"member": "alice", "result": "登录完成",
                         "timestamp": "2026-08-12T12:00:00"}
            )
        finally:
            for m in reversed(mocks):
                m.stop()

        self.assertTrue(result["injected"], f"保持 tmux 后应注入, got {result}")
        self.assertEqual(len(sent), 1)
        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_state"], "active")
        self.assertEqual(team["leader_wakeup_reason"], "report")

    def test_claim_managed_alive_no_reentry_count_increment(self):
        """【重启场景】受管 leader 重启重新进入（窗口存活）：保持 tmux 且计数不递增。"""
        self._team(leader_recovery_count=2)
        self._claim(session_alive=True, window_alive=True)
        team = mcp._load()["teams"]["team"]
        # 保持 tmux 的 claim 走早 return，不调用 _record_leader_reentry
        self.assertEqual(team["leader_recovery_count"], 2)

    def test_claim_dead_window_takes_over_direct_and_counts(self):
        """【死窗口】受管 leader 窗口已死：direct 接管 + 计数递增 + 消息"已关闭"。"""
        self._team()
        result = self._claim(session_alive=True, window_alive=False)

        team = mcp._load()["teams"]["team"]
        self.assertIn("已关闭，直接接管", result)
        self.assertIn("已接管", result)
        self.assertEqual(team["leader_type"], "direct")
        self.assertEqual(team["leader"], "lead")                      # 保留原 leader 名
        self.assertEqual(team["leader_recovery_count"], 1)            # reentry 递增

    def test_claim_unmanaged_alive_demotes_to_member_direct(self):
        """【非受管】存活 tmux 终端但 role≠leader：降级 + direct（外部接管保留）。"""
        self._team()
        team = mcp._load()["teams"]["team"]
        team["members"]["lead"]["role"] = "member"
        mcp._save({"teams": {"team": team}})

        result = self._claim(session_alive=True, window_alive=True)

        team = mcp._load()["teams"]["team"]
        self.assertIn("已降级为普通成员", result)
        self.assertIn("已接管", result)
        self.assertEqual(team["leader_type"], "direct")
        self.assertEqual(team["members"]["lead"]["role"], "member")
        # 外部接管后展示为"当前会话 · 直接控制"
        with mock.patch.object(mcp, "_tmux_session_alive", return_value=True):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                panel = mcp.leader_list_team("team")
        self.assertIn("直接控制", panel)

    def test_claim_no_leader_sets_direct(self):
        """【无窗口/无 leader】之前无 leader：设为 direct 控制模式。"""
        self._team(leader="", leader_type="", leader_last_task="")
        result = self._claim(session_alive=False, window_alive=False)
        team = mcp._load()["teams"]["team"]
        self.assertIn("之前无 leader", result)
        self.assertEqual(team["leader_type"], "direct")
        self.assertEqual(team["leader"], "you")


class TestClaimKeepsTmuxPredicateEdges(_IsolatedMcp):
    """claim_keeps_tmux_leader 纯函数补充边界（coder 已覆盖 5 条，补其余）。"""

    def _mk(self, **overrides):
        base = {
            "leader_type": "tmux",
            "leader": "lead",
            "members": {"lead": {"role": "leader"}},
        }
        base.update(overrides)
        return base

    def test_false_for_direct_type(self):
        """leader_type='direct' 时保持语义不适用（direct reentry 走原路径）。"""
        team = self._mk(leader_type="direct")
        self.assertFalse(mcp.claim_keeps_tmux_leader(
            team, session_alive=True, window_alive=True))

    def test_false_for_empty_team(self):
        self.assertFalse(mcp.claim_keeps_tmux_leader(
            None, session_alive=True, window_alive=True))
        self.assertFalse(mcp.claim_keeps_tmux_leader(
            {}, session_alive=True, window_alive=True))

    def test_false_when_leader_not_in_members(self):
        """leader 名不在成员表 → 非受管，允许外部接管。"""
        team = self._mk(members={"alice": {"role": "coder"}})
        self.assertFalse(mcp.claim_keeps_tmux_leader(
            team, session_alive=True, window_alive=True))

    def test_false_when_session_or_window_dead(self):
        """session 或 window 任一死亡 → 不保持（死窗走 direct 接管）。"""
        team = self._mk()
        self.assertFalse(mcp.claim_keeps_tmux_leader(
            team, session_alive=False, window_alive=True))
        self.assertFalse(mcp.claim_keeps_tmux_leader(
            team, session_alive=True, window_alive=False))


class TestClaimKeepsTmuxTuiSharesPredicate(unittest.TestCase):
    """TUI / MCP 共用同一 claim_keeps_tmux_leader（防两处语义漂移）。"""

    def test_tui_mcp_share_same_predicate(self):
        import tui.tui_screens as tui_screens
        self.assertIs(
            tui_screens.claim_keeps_tmux_leader,
            mcp.claim_keeps_tmux_leader,
            "TUI action_claim_leader 与 MCP claim_leader 必须共用同一受管判定",
        )


if __name__ == "__main__":
    unittest.main()
