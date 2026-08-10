"""direct leader 待收回报提醒（MCP 装饰器层搭便车）N1-N7 验证。

提醒搭便车在 @mcp.tool 装饰器层（_mcp_tool_with_nudge 替换 mcp.tool），
对任何 MCP 客户端一视同仁。触发：direct leader + leader_pending_reports
非空 + 节流（条数 >= 3 或最老一条距今 >= 5 分钟）。排除：leader_activate、
leader_get_recovery_context、非 str 返回值、取不到 team_name、任何异常。

隔离模式与 test_leader_sleep_gap_probe.py 一致：unittest + 模块全局路径
覆写，不写真实 ~/.mult_agent_mcp/。
"""
import datetime
import os
import tempfile
import unittest
from pathlib import Path

import mult_agent_mcp as mcp


def _report(member="alice", ts=None, **extra):
    entry = {
        "member": member,
        "result": "完成登录模块",
        "timestamp": ts or datetime.datetime.now().isoformat(),
        "compressed_context_path": "",
        "artifact_path": "",
    }
    entry.update(extra)
    return entry


class DirectLeaderNudgeTests(unittest.TestCase):
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

    def _team(self, leader_type="direct", reports=()):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": leader_type,
            "leader_state": "active",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
            "leader_pending_reports": list(reports),
        }
        mcp._save({"teams": {"team": team}})
        return team

    # ------------------------------------------------------------------
    # N1-N7
    # ------------------------------------------------------------------

    def test_n1_direct_3_reports_nudged(self):
        """N1: direct leader + 3 条待收 → 任意带 team_name 的工具末尾出现提示"""
        self._team(reports=[_report(), _report(member="bob"), _report(member="carol")])
        r = mcp.list_members(team_name="team")  # kwargs 路径取 team_name
        self.assertIn("⏰ 待收回报提醒", r)
        self.assertIn("3 条成员回报待确认", r)
        self.assertIn("leader_activate('team')", r)
        self.assertTrue(r.startswith("👥"), "工具原文必须保留在提示之前")

    def test_n2_tmux_not_nudged(self):
        """N2: tmux leader + 3 条待收 → 不出现"""
        self._team(leader_type="tmux",
                   reports=[_report(), _report(member="bob"), _report(member="carol")])
        r = mcp.list_members("team")  # 位置参数路径
        self.assertNotIn("⏰ 待收回报提醒", r)
        self.assertIn("alice", r)

    def test_n3_one_fresh_not_nudged(self):
        """N3: direct + 1 条且刚回报（未超 5 分钟）→ 节流生效，不出现"""
        self._team(reports=[_report()])
        r = mcp.list_members("team")
        self.assertNotIn("⏰ 待收回报提醒", r)

    def test_n4_one_old_nudged(self):
        """N4: direct + 1 条但已超 5 分钟 → 时间条件生效，出现"""
        ts = (datetime.datetime.now() - datetime.timedelta(seconds=302)).isoformat()
        self._team(reports=[_report(ts=ts)])
        r = mcp.list_members("team")
        self.assertIn("⏰ 待收回报提醒", r)
        self.assertIn("1 条成员回报待确认", r)
        self.assertIn("已等 5分", r)

    def test_n5_excluded_tools_not_nudged(self):
        """N5: leader_activate 与 leader_get_recovery_context 返回值不被追加"""
        old = (datetime.datetime.now() - datetime.timedelta(minutes=10)).isoformat()
        self._team(reports=[_report(ts=old), _report(ts=old), _report(ts=old)])
        a = mcp.leader_activate("team")
        self.assertIn("已激活", a)
        self.assertNotIn("⏰ 待收回报提醒", a)
        # activate 已消费清空 pending，再造一份测 recovery context
        self._team(reports=[_report(ts=old), _report(ts=old), _report(ts=old)])
        c = mcp.leader_get_recovery_context("team")
        self.assertIsInstance(c, str)
        self.assertNotIn("⏰ 待收回报提醒", c)

    def test_n6_non_str_and_exception_unchanged(self):
        """N6: 工具返回非 str 或抛异常时，包装不改变其行为"""
        # 非 str 返回值：原样透传，不追加
        def dict_fn(team_name):
            return {"team": team_name}

        wrapped = mcp._mcp_tool_with_nudge(dict_fn)
        self.assertEqual(wrapped("team"), {"team": "team"})

        # 抛异常：包装不吞异常
        def boom(team_name):
            raise RuntimeError("boom!")

        wrapped2 = mcp._mcp_tool_with_nudge(boom)
        with self.assertRaises(RuntimeError):
            wrapped2("team")

        # 无 team_name 参数的工具：即使返回 str 也不追加
        def no_team(x: int = 1):
            return "plain-result"

        wrapped3 = mcp._mcp_tool_with_nudge(no_team)
        self.assertEqual(wrapped3(), "plain-result")

    def test_n7_monitor_inferred_counted(self):
        """N7: 含 monitor 推断回报时，提示标注该类数量（复用 event 常量）"""
        old = (datetime.datetime.now() - datetime.timedelta(minutes=10)).isoformat()
        self._team(reports=[
            _report(ts=old, member="alice"),
            _report(ts=old, member="bob", event=mcp.MONITOR_INFERRED_EVENT),
            _report(ts=old, member="carol", event=mcp.MONITOR_INFERRED_EVENT),
        ])
        r = mcp.list_members("team")
        self.assertIn("3 条成员回报待确认", r)
        self.assertIn("含 2 条 monitor 推断", r)

    def test_n1_positional_team_name_also_works(self):
        """N1 补丁：位置参数传 team_name 同样触发（覆盖 args[0] 路径）"""
        self._team(reports=[_report(), _report(member="bob"), _report(member="carol")])
        r = mcp.list_members("team")
        self.assertIn("⏰ 待收回报提醒", r)

    # ------------------------------------------------------------------
    # E1-E4（收尾 1/3：查询类工具节流豁免 + 取参加固）
    # ------------------------------------------------------------------

    def test_e1_query_tool_exempts_throttle(self):
        """E1: direct + 1 条刚回报（未达任何节流条件）+ 查进度工具 → 豁免生效，出现提醒"""
        self._team(reports=[_report()])
        r = mcp.leader_check_member_status(team_name="team")
        self.assertIn("⏰ 待收回报提醒", r)
        self.assertIn("1 条成员回报待确认", r)

    def test_e2_non_exempt_tool_still_throttled(self):
        """E2: 同条件下非豁免工具（配置类）→ 节流仍然生效，不出现"""
        self._team(reports=[_report()])
        r = mcp.team_get_default_agent(team_name="team")
        self.assertNotIn("⏰ 待收回报提醒", r)

    def test_e3_first_param_not_team_name_ignored(self):
        """E3: 首参非 team_name 的工具位置调用 → 不把位置值当团队名，不追加、不抛异常"""
        # 触发条件全满足（3 条 pending），若误把 args[0] 当团队名就会误报
        self._team(reports=[_report(), _report(member="bob"), _report(member="carol")])

        def fake(server_name):
            return f"configured {server_name}"

        wrapped = mcp._mcp_tool_with_nudge(fake)
        r = wrapped("team")  # 位置值 "team" 不能被当作团队名
        self.assertEqual(r, "configured team")
        self.assertNotIn("⏰ 待收回报提醒", r)

    def test_e4_first_param_team_name_positional_still_works(self):
        """E4: 首参确为 team_name 的工具位置调用 → 加固未堵死正常路径"""
        self._team(reports=[_report(), _report(member="bob"), _report(member="carol")])

        def pos(team_name):
            return f"members of {team_name}"

        wrapped = mcp._mcp_tool_with_nudge(pos)
        r = wrapped("team")
        self.assertIn("⏰ 待收回报提醒", r)
        self.assertTrue(r.startswith("members of team"))


if __name__ == "__main__":
    unittest.main()
