"""
P0/P1 补测：并发证据原子性 / ACK 持久化字段 / quota 恢复消息
============================================================

聚焦 leader 验收阻断项 ③ 中 test_checkpoint_gate_isolation.py（reviewer）
与 test_leader_checkpoint.py（coder）**未覆盖**的补强面：

  1. leader_assign_subtask 并发更新（_update_team_data 原子路径）不覆盖
     既有 checkpoint.evidence —— 分配/回报互不覆盖的 TOCTOU 关闭回归。
  2. ACK 正确 epoch 后持久化 ack 字段（epoch/acked_ts/acked_by）。
  3. quota 恢复消息内容：读取成员 last_task、提示续跑入口（refactor ②
     verify-then-continue 落盘前的数据不变量）。

隔离：临时 teams_data + mock tmux，零触生产（conftest 环境级兜底）。
"""

import unittest
from unittest import mock

import mult_agent_mcp as mcp


class _IsolatedTestCase(unittest.TestCase):
    """与 test_leader_checkpoint.py 相同的隔离基类（temp teams_data + mock）。"""

    def setUp(self):
        import os
        import tempfile
        from pathlib import Path
        from common import data_layer

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
                "MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR",
            )
        }
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
        import os
        from common import data_layer
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = None
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _team(self, *, leader_task="", members=None):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "direct",
            "members": {"lead": {"role": "leader", "agent": "claude"}},
        }
        for name, info in (members or {}).items():
            team["members"][name] = info
        if leader_task:
            team["leader_last_task"] = leader_task
            team["leader_last_task_completed"] = False
        mcp._save({"teams": {"team": team}})
        return team

    def _record_task_start(self, task):
        team = mcp._load()["teams"]["team"]
        mcp._record_leader_task_start(team, task)
        mcp._save({"teams": {"team": team}})

    def _cp(self) -> dict:
        return mcp._load().get("teams", {}).get("team", {}).get("leader_checkpoint", {})

    def _report(self, result: str):
        """按真实调用点走 member_report_result（追加 checkpoint.evidence）。"""
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    return mcp.member_report_result("team", result, member_name="alice")

    def _assign(self, task: str):
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    return mcp.leader_assign_subtask("team", "alice", task)


class ConcurrentEvidenceAtomicityTests(_IsolatedTestCase):
    """③-3：leader_assign_subtask 并发更新（_update_team_data 原子路径）
    不覆盖既有 checkpoint.evidence（TOCTOU 关闭回归）。"""

    def test_assign_after_report_preserves_evidence(self):
        """先回报（evidence=1 条）→ 再分配：evidence 不被覆盖，仅追加分工。"""
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        self._report("evidence one")
        cp = self._cp()
        self.assertEqual(cp["source"], "report")
        ev_before = len(cp["evidence"])
        self.assertEqual(cp["evidence"][-1]["result"], "evidence one")

        r = self._assign("implement checkpoint module")
        self.assertIn("已分配", r)

        cp = self._cp()
        self.assertEqual(len(cp["evidence"]), ev_before, "证据保留（原子路径不丢 evidence）")
        self.assertEqual(cp["evidence"][-1]["result"], "evidence one")
        self.assertEqual(cp["assignments"]["alice"]["task"], "implement checkpoint module")
        self.assertEqual(cp["source"], "assign")

    def test_report_after_assign_preserves_assignments(self):
        """先分配（分工=1）→ 再回报：assignments 不被覆盖，evidence 追加。"""
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        r = self._assign("implement checkpoint module")
        self.assertIn("已分配", r)
        cp = self._cp()
        self.assertEqual(cp["assignments"]["alice"]["task"], "implement checkpoint module")

        self._report("evidence after assign")

        cp = self._cp()
        self.assertEqual(cp["assignments"]["alice"]["task"], "implement checkpoint module", "分工不被覆盖")
        self.assertEqual(cp["evidence"][-1]["result"], "evidence after assign", "证据追加")
        self.assertEqual(cp["source"], "report")


class AckPersistenceTests(_IsolatedTestCase):
    """③-2：ACK 正确 epoch 后持久化 ack 字段（epoch/acked_ts/acked_by）。"""

    def test_ack_persists_epoch_and_acked_by(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        # 制造 HIGH 漂移（goal 已记录但 leader_last_task 被清空）
        data = mcp._load()
        data["teams"]["team"].pop("leader_last_task", None)
        mcp._save(data)

        r = mcp.leader_ack_checkpoint("team")
        self.assertIn("已确认", r)
        data = mcp._load()["teams"]["team"]
        ack = data["leader_checkpoint_ack"]
        self.assertEqual(ack["epoch"], data["leader_checkpoint"]["epoch"])
        self.assertTrue(ack["acked_ts"])
        self.assertEqual(ack["acked_by"], "lead")

    def test_ack_epoch_mismatch_rejected_no_write(self):
        """显式传旧 ack_epoch 与当前不一致 → 拒绝确认，不写 ack。"""
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set("team", decisions="A")  # 推进到 epoch 2
        self.assertEqual(self._cp()["epoch"], 2)
        r = mcp.leader_ack_checkpoint("team", ack_epoch=1)
        self.assertIn("未确认", r)
        self.assertNotIn("leader_checkpoint_ack", mcp._load()["teams"]["team"])


class QuotaRecoveryMessageTests(_IsolatedTestCase):
    """③-4：quota 恢复消息读取成员任务、提示续跑入口；无 checkpoint 时诚实
    回落 last_task（refactor ② 落盘前的数据不变量）。"""

    def test_recovery_message_includes_task_and_resume_hint(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        data = mcp._load()
        m = data["teams"]["team"]["members"]["alice"]
        m["last_task"] = "implement checkpoint module"
        m["last_task_completed"] = False
        mcp._save(data)

        msg = mcp._build_recovery_context("team", "alice")
        self.assertIn("implement checkpoint module", msg)
        self.assertIn("member_get_my_task", msg)  # 续跑入口提示存在


if __name__ == "__main__":
    unittest.main()
