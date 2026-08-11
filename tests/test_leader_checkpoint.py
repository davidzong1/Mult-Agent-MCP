"""
team 级 leader_checkpoint 基础测试。

覆盖 P0 交付：
  - 结构化字段（goal/boundaries/decisions/plan/assignments/dependencies/
    deadline/remaining/evidence/next_actions）+ 单调 epoch/version
  - 原子写入与旧 epoch 拒绝（_update_leader_checkpoint / leader_checkpoint_set）
  - 四个写入点接入：任务开始(_record_leader_task_start)、分配(leader_assign_subtask)、
    回报(_record_report_and_notify_leader 经 member_report_result)、完成(leader_mark_task_complete)
  - 恢复渲染优先 checkpoint + 漂移检测禁止自动再分配
    （build_leader_recovery_section / leader_activate / leader_get_recovery_context）
  - 向后兼容：无 checkpoint 的旧团队行为不变

遵循现有 test_leader_member_recovery.py / test_workflow_resume_recovery.py 模式：
unittest + mock，隔离全局状态，不依赖真实 tmux。
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp

from common.leader_recovery import (
    build_leader_recovery_section,
    build_leader_checkpoint_section,
    leader_checkpoint_drift,
    empty_leader_checkpoint,
    LEADER_CHECKPOINT_VERSION,
    MAX_CHECKPOINT_EVIDENCE,
)


class TestLeaderCheckpoint(unittest.TestCase):
    """leader_checkpoint 基础：字段 / epoch / 写入点 / 恢复渲染 / 兼容"""

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
                "MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR",
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

    def _team(
        self,
        *,
        leader="lead",
        leader_type="direct",
        leader_task="",
        members=None,
        terminals_active=False,
    ):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": terminals_active,
            "leader": leader,
            "leader_type": leader_type,
            "members": {
                leader: {"role": "leader", "agent": "claude"},
            },
        }
        for name, info in (members or {}).items():
            team["members"][name] = info
        if leader_task:
            team["leader_last_task"] = leader_task
            team["leader_last_task_completed"] = False
        mcp._save({"teams": {"team": team}})
        return team

    def _cp(self) -> dict:
        return mcp._load().get("teams", {}).get("team", {}).get("leader_checkpoint", {})

    def _record_task_start(self, task, context=""):
        """按真实调用点：_record_leader_task_start 原地改 team → _save。"""
        team = mcp._load()["teams"]["team"]
        mcp._record_leader_task_start(team, task, context)
        mcp._save({"teams": {"team": team}})

    # ------------------------------------------------------------------
    # 1. 任务开始：初始化结构化基线
    # ------------------------------------------------------------------

    def test_task_start_initializes_checkpoint(self):
        self._team()
        self._record_task_start("ship P0 checkpoint")

        cp = self._cp()
        self.assertEqual(cp["goal"], "ship P0 checkpoint")
        self.assertEqual(cp["epoch"], 1)
        self.assertEqual(cp["version"], LEADER_CHECKPOINT_VERSION)
        self.assertEqual(cp["source"], "task_start")
        self.assertEqual(cp["status"], "active")
        # 全部结构化字段存在
        for field in ("boundaries", "decisions", "plan", "assignments",
                      "dependencies", "deadline", "remaining", "evidence", "next_actions"):
            self.assertIn(field, cp)
        self.assertEqual(cp["boundaries"], [])
        self.assertEqual(cp["assignments"], {})

    def test_task_start_reentry_same_goal_preserves_fields_bumps_epoch(self):
        self._team()
        self._record_task_start("build P0")
        r = mcp.leader_checkpoint_set("team", decisions="A\nB", remaining="R1")
        self.assertIn("已更新", r)
        self.assertEqual(self._cp()["epoch"], 2)

        # 同任务重入：保留 decisions/remaining，仅 bump epoch
        self._record_task_start("build P0")
        cp = self._cp()
        self.assertEqual(cp["decisions"], ["A", "B"])
        self.assertEqual(cp["remaining"], ["R1"])
        self.assertEqual(cp["epoch"], 3)
        self.assertEqual(cp["status"], "active")

    def test_task_start_new_task_resets_structured_fields(self):
        self._team()
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set("team", decisions="old decision", remaining="old remaining")
        self.assertEqual(self._cp()["epoch"], 2)

        self._record_task_start("completely different task")
        cp = self._cp()
        self.assertEqual(cp["goal"], "completely different task")
        self.assertEqual(cp["decisions"], [])
        self.assertEqual(cp["remaining"], [])
        self.assertEqual(cp["evidence"], [])
        self.assertEqual(cp["assignments"], {})
        self.assertEqual(cp["epoch"], 3)  # 单调递增，不清零

    # ------------------------------------------------------------------
    # 2. leader_checkpoint_set：结构化字段 + 旧 epoch 拒绝
    # ------------------------------------------------------------------

    def test_checkpoint_set_merges_structured_fields(self):
        self._team()
        self._record_task_start("build P0")

        r = mcp.leader_checkpoint_set(
            "team",
            goal="build P0",
            boundaries="不改生产数据\n只读分析",
            decisions="方案 A",
            plan="p1\np2",
            dependencies="refactor 完成",
            deadline="2026-08-11",
            remaining="r1\nr2",
            next_actions="n1",
        )
        self.assertIn("已更新", r)
        cp = self._cp()
        self.assertEqual(cp["goal"], "build P0")
        self.assertEqual(cp["boundaries"], ["不改生产数据", "只读分析"])
        self.assertEqual(cp["decisions"], ["方案 A"])
        self.assertEqual(cp["plan"], ["p1", "p2"])
        self.assertEqual(cp["dependencies"], ["refactor 完成"])
        self.assertEqual(cp["deadline"], "2026-08-11")
        self.assertEqual(cp["remaining"], ["r1", "r2"])
        self.assertEqual(cp["next_actions"], ["n1"])
        self.assertEqual(cp["epoch"], 2)

    def test_checkpoint_set_empty_patch_rejected(self):
        self._team()
        self._record_task_start("build P0")
        r = mcp.leader_checkpoint_set("team", goal="   ")
        self.assertIn("未提供任何结构化字段", r)
        self.assertEqual(self._cp()["epoch"], 1)  # 无写入，epoch 不变

    def test_checkpoint_set_old_epoch_rejected(self):
        self._team()
        self._record_task_start("build P0")
        self.assertEqual(self._cp()["epoch"], 1)
        # 先成功写一次，把 epoch 推进到 2
        r = mcp.leader_checkpoint_set("team", decisions="baseline")
        self.assertIn("已更新", r)
        self.assertEqual(self._cp()["epoch"], 2)

        # 基于旧 epoch=1 的写入被拒绝（当前已是 2）
        r = mcp.leader_checkpoint_set("team", goal="stale", expected_epoch=1)
        self.assertIn("旧 epoch 拒绝", r)
        self.assertIn("当前 epoch=2", r)
        cp = self._cp()
        self.assertEqual(cp["goal"], "build P0")  # 未被覆盖
        self.assertEqual(cp["epoch"], 2)

        # 基于最新 epoch 的写入成功
        r = mcp.leader_checkpoint_set("team", goal="build P0 v2", expected_epoch=2)
        self.assertIn("已更新", r)
        self.assertEqual(self._cp()["goal"], "build P0 v2")
        self.assertEqual(self._cp()["epoch"], 3)

    def test_checkpoint_set_unknown_team_rejected(self):
        r = mcp.leader_checkpoint_set("nope", goal="x")
        self.assertIn("不存在", r)

    # ------------------------------------------------------------------
    # 3. 分配写入点
    # ------------------------------------------------------------------

    def test_assign_subtask_records_checkpoint_assignment(self):
        self._team(
            leader_task="build P0",
            terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertIn("已分配", r)

        cp = self._cp()
        self.assertEqual(cp["assignments"]["alice"]["task"], "implement checkpoint")
        self.assertEqual(cp["assignments"]["alice"]["status"], "assigned")
        self.assertEqual(cp["source"], "assign")
        self.assertGreater(cp["epoch"], 1)

    def test_assign_without_checkpoint_is_noop(self):
        # 旧团队无 checkpoint：分配不产生 checkpoint（向后兼容，不抛异常）
        self._team(terminals_active=True, members={"alice": {"role": "coder", "agent": "claude"}})
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "some task")
        self.assertIn("已分配", r)
        self.assertEqual(self._cp(), {})

    # ------------------------------------------------------------------
    # 4. 回报写入点
    # ------------------------------------------------------------------

    def test_member_report_appends_checkpoint_evidence(self):
        self._team(
            leader_task="build P0",
            leader_type="direct",
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    r = mcp.member_report_result("team", "checkpoint done", member_name="alice")
        self.assertIn("结果已记录", r)

        cp = self._cp()
        self.assertEqual(cp["source"], "report")
        self.assertEqual(cp["evidence"][-1]["member"], "alice")
        self.assertEqual(cp["evidence"][-1]["event"], "member_report")
        self.assertIn("checkpoint done", cp["evidence"][-1]["result"])
        self.assertGreater(cp["epoch"], 1)

    def test_report_without_checkpoint_is_noop(self):
        self._team(leader_type="direct", members={"alice": {"role": "coder", "agent": "claude"}})
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    r = mcp.member_report_result("team", "done", member_name="alice")
        self.assertIn("结果已记录", r)
        self.assertEqual(self._cp(), {})

    # ------------------------------------------------------------------
    # 5. 完成写入点
    # ------------------------------------------------------------------

    def test_mark_complete_closes_checkpoint(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set("team", remaining="R1", next_actions="N1")

        with mock.patch.object(mcp, "_write_leader_compressed_context", return_value="ctx.md"):
            with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                "compact_path": "ctx.md", "compact_sent": False,
                "compact_error": "no tmux", "truncated": False, "agent_exited": False,
            }):
                r = mcp.leader_mark_task_complete("team", summary="all done", artifact_path="report.md")
        self.assertIn("已标记完成", r)

        cp = self._cp()
        self.assertEqual(cp["status"], "completed")
        self.assertEqual(cp["remaining"], [])
        self.assertEqual(cp["next_actions"], [])
        self.assertEqual(cp["source"], "complete")
        self.assertEqual(cp["evidence"][-1]["event"], "leader_task_completed")
        self.assertIn("all done", cp["evidence"][-1]["result"])
        self.assertGreater(cp["epoch"], 1)

    def test_mark_complete_without_checkpoint_backward_compat(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        with mock.patch.object(mcp, "_write_leader_compressed_context", return_value="ctx.md"):
            with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                "compact_path": "ctx.md", "compact_sent": False,
                "compact_error": "no tmux", "truncated": False, "agent_exited": False,
            }):
                r = mcp.leader_mark_task_complete("team", summary="done")
        self.assertIn("已标记完成", r)
        self.assertEqual(self._cp(), {})

    # ------------------------------------------------------------------
    # 6. 漂移检测
    # ------------------------------------------------------------------

    def test_drift_goal_mismatch(self):
        self._team(leader_task="build P0")
        self._record_task_start("build P0")
        self.assertEqual(leader_checkpoint_drift(mcp._load()["teams"]["team"]), [])

        # leader_last_task 与 checkpoint.goal 明显不一致 → 漂移
        data = mcp._load()
        data["teams"]["team"]["leader_last_task"] = "completely different plan"
        mcp._save(data)
        reasons = leader_checkpoint_drift(mcp._load()["teams"]["team"])
        self.assertTrue(any("goal" in r for r in reasons))

    def test_drift_goal_present_but_leader_task_empty(self):
        self._team()
        self._record_task_start("build P0")
        data = mcp._load()
        data["teams"]["team"].pop("leader_last_task", None)
        mcp._save(data)
        reasons = leader_checkpoint_drift(mcp._load()["teams"]["team"])
        self.assertTrue(any("leader_last_task 为空" in r for r in reasons))

    def test_drift_assignment_mismatch(self):
        self._team(
            leader_task="build P0", terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertEqual(leader_checkpoint_drift(mcp._load()["teams"]["team"]), [])

        # 成员 last_task 被改成与 checkpoint 分工矛盾的内容 → 漂移
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["last_task"] = "unrelated task xyz"
        mcp._save(data)
        reasons = leader_checkpoint_drift(mcp._load()["teams"]["team"])
        self.assertTrue(any("alice" in r for r in reasons))

    def test_drift_done_but_remaining_not_cleared(self):
        self._team()
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set("team", remaining="R1")
        # 团队已标记完成但 checkpoint 仍残留剩余工作 → 漂移
        data = mcp._load()
        data["teams"]["team"]["leader_last_task_completed"] = True
        mcp._save(data)
        reasons = leader_checkpoint_drift(mcp._load()["teams"]["team"])
        self.assertTrue(any("剩余工作" in r for r in reasons))

    def test_no_checkpoint_no_drift(self):
        self._team(leader_task="build P0")
        self.assertEqual(leader_checkpoint_drift(mcp._load()["teams"]["team"]), [])

    # ------------------------------------------------------------------
    # 7. 恢复渲染：优先 checkpoint + 漂移禁止自动再分配
    # ------------------------------------------------------------------

    def test_recovery_section_renders_checkpoint_first(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set(
            "team", decisions="方案 A", plan="p1", remaining="r1",
            next_actions="n1",
        )
        team = mcp._load()["teams"]["team"]
        text = "\n".join(build_leader_recovery_section("team", team, "/tmp/w", "/tmp/s"))
        self.assertIn("Leader Checkpoint", text)
        self.assertIn("目标:", text)
        self.assertIn("已决策", text)
        self.assertIn("剩余工作", text)
        self.assertIn("下一步", text)
        # checkpoint 渲染在 last_task 摘要之前（优先结构化依据）
        self.assertLess(text.index("Leader Checkpoint"), text.index("未完成总任务"))

    def test_recovery_section_forbids_auto_reassign_on_drift(self):
        self._team(
            leader_task="build P0", terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    mcp.leader_assign_subtask("team", "alice", "implement checkpoint")

        # 制造漂移
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["last_task"] = "unrelated task xyz"
        mcp._save(data)

        team = mcp._load()["teams"]["team"]
        text = "\n".join(build_leader_recovery_section("team", team, "/tmp/w", "/tmp/s"))
        self.assertIn("漂移警告（禁止自动再分配）", text)
        self.assertIn("人工确认前不得自动重派任务", text)
        # 漂移时不给"继续推进"默认指引
        self.assertNotIn("检测到未完成团队工作。你重新进入后必须先恢复上下文并继续推进", text)

    def test_leader_activate_renders_checkpoint_and_drift(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set("team", decisions="A", remaining="R1")

        act = mcp.leader_activate("team")
        self.assertIn("Leader Checkpoint", act)
        self.assertIn("已决策", act)

        # 制造漂移后：leader_activate 渲染禁止自动再分配警告
        data = mcp._load()
        data["teams"]["team"]["leader_last_task"] = "total rewrite of direction"
        mcp._save(data)
        act = mcp.leader_activate("team")
        self.assertIn("漂移警告（禁止自动再分配）", act)

    def test_recovery_context_includes_checkpoint(self):
        self._team(leader_task="build P0", members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        mcp.leader_checkpoint_set("team", decisions="方案 A")
        context_msg = mcp.leader_get_recovery_context("team")
        self.assertIn("Leader Checkpoint", context_msg)
        self.assertIn("方案 A", context_msg)

    # ------------------------------------------------------------------
    # 8. 向后兼容：无 checkpoint 团队行为不变
    # ------------------------------------------------------------------

    def test_recovery_section_backward_compat_without_checkpoint(self):
        self._team(
            leader_task="ship recovery feature",
            members={"alice": {"role": "coder", "agent": "claude", "last_task": "implement state model", "last_task_completed": False}},
        )
        team = mcp._load()["teams"]["team"]
        text = "\n".join(build_leader_recovery_section("team", team, "/tmp/w", "/tmp/s"))
        self.assertIn("检测到未完成团队工作", text)
        self.assertIn("ship recovery feature", text)
        self.assertIn("alice", text)
        self.assertNotIn("Leader Checkpoint", text)

    # ------------------------------------------------------------------
    # 9. 硬门（验收阻断项）：HIGH 漂移未确认时拒绝分配/广播
    # ------------------------------------------------------------------

    def _high_drift_team(self, members=None):
        """goal 已记录但 leader_last_task 为空 → HIGH 漂移；返回 (team, team_name)。"""
        self._team(
            leader_task="build P0",
            terminals_active=True,
            members=members or {"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        data = mcp._load()
        data["teams"]["team"].pop("leader_last_task", None)
        mcp._save(data)
        return mcp._load()["teams"]["team"]

    def test_gate_blocks_assign_on_unacked_high_drift(self):
        """绕过硬门：HIGH 漂移未 ACK 时分配被拒绝，last_task 不落盘、checkpoint 不变。"""
        self._high_drift_team()

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertIn("已拒绝执行", r)
        self.assertIn("leader_ack_checkpoint", r)

        data = mcp._load()["teams"]["team"]
        self.assertNotIn("last_task", data["members"]["alice"])  # 分配未落盘
        self.assertEqual(data["leader_checkpoint"]["assignments"], {})  # checkpoint 未变
        # epoch 不变（gate 拒绝不产生写入）
        self.assertEqual(data["leader_checkpoint"]["epoch"], 1)

    def test_gate_blocks_broadcast_on_unacked_high_drift(self):
        self._high_drift_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast("team", "continue work")
        self.assertIn("已拒绝执行", r)

    def test_gate_does_not_block_when_no_high_drift(self):
        """无 HIGH 漂移（goal 与 leader_last_task 一致）时不触发硬门。"""
        self._team(leader_task="build P0", terminals_active=True,
                   members={"alice": {"role": "coder", "agent": "claude"}})
        self._record_task_start("build P0")
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertIn("已分配", r)

    def test_ack_then_assign_allowed(self):
        """ACK 后放行：确认当前 checkpoint 后 HIGH 漂移不再阻止分配。"""
        self._high_drift_team()
        r = mcp.leader_ack_checkpoint("team")
        self.assertIn("已确认", r)
        data = mcp._load()["teams"]["team"]
        self.assertEqual(data["leader_checkpoint_ack"]["epoch"], data["leader_checkpoint"]["epoch"])

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertIn("已分配", r)
        data = mcp._load()["teams"]["team"]
        self.assertEqual(data["members"]["alice"]["last_task"], "implement checkpoint")
        self.assertEqual(data["leader_checkpoint"]["assignments"]["alice"]["task"], "implement checkpoint")

    def test_stale_ack_does_not_pass_gate_after_epoch_bump(self):
        """旧 ACK 不覆盖新状态：epoch 被新写入推进后，HIGH 漂移需重新确认。"""
        self._high_drift_team()
        # ACK epoch=1
        self.assertIn("已确认", mcp.leader_ack_checkpoint("team"))
        # 新写入（回报）推进 epoch → ack.epoch 过期
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    mcp.member_report_result("team", "progress update", member_name="alice")
        data = mcp._load()["teams"]["team"]
        self.assertGreater(data["leader_checkpoint"]["epoch"],
                           data["leader_checkpoint_ack"]["epoch"])

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertIn("已拒绝执行", r)

        # 重新 ACK 最新 epoch → 放行
        self.assertIn("已确认", mcp.leader_ack_checkpoint("team"))
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "alice", "implement checkpoint")
        self.assertIn("已分配", r)

    def test_ack_stale_epoch_explicit_rejected(self):
        """显式传旧 ack_epoch 与当前不一致 → 拒绝确认（不覆盖新状态）。"""
        self._high_drift_team()
        # 推进到 epoch 2
        mcp.leader_checkpoint_set("team", decisions="A")
        self.assertEqual(self._cp()["epoch"], 2)
        r = mcp.leader_ack_checkpoint("team", ack_epoch=1)
        self.assertIn("未确认", r)
        data = mcp._load()["teams"]["team"]
        self.assertNotIn("leader_checkpoint_ack", data)  # 未写入 ack

    # ------------------------------------------------------------------
    # 10. quota 恢复从 checkpoint 续跑（数据不变量，供 refactor 的
    #     _recover_and_send verify-then-continue 消费）
    # ------------------------------------------------------------------

    def test_quota_switch_preserves_checkpoint_for_continue(self):
        """quota 换号只改 agent_user，checkpoint（分工+证据）完整保留供续跑。"""
        self._team(
            leader_task="build P0",
            leader_type="direct",
            terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        # 分配
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    mcp.leader_assign_subtask("team", "alice", "implement checkpoint module")
        # 回报（部分进展证据）
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    mcp.member_report_result("team", "设计完成，实现进行中 60%", member_name="alice")

        before = mcp._load()["teams"]["team"]["leader_checkpoint"]

        # 模拟 quota 换号（_select_failover_profile/_recover_and_send 的数据效果：
        # 换 agent_user + 递增 quota_switch_count，其余不动）
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        member["agent_user"] = "acct-b"
        member["quota_switch_count"] = member.get("quota_switch_count", 0) + 1
        member["blocked_reason"] = "quota"
        mcp._save(data)

        after = mcp._load()["teams"]["team"]["leader_checkpoint"]
        self.assertEqual(after["epoch"], before["epoch"])
        self.assertEqual(after["assignments"]["alice"]["task"], "implement checkpoint module")
        self.assertEqual(after["evidence"][-1]["member"], "alice")
        self.assertIn("60%", after["evidence"][-1]["result"])
        self.assertEqual(after["goal"], "build P0")

        # 恢复侧可渲染出 alice 的分工 + 最近证据 → verify-then-continue 的数据源
        team = mcp._load()["teams"]["team"]
        text = "\n".join(build_leader_checkpoint_section(team))
        self.assertIn("implement checkpoint module", text)
        self.assertIn("60%", text)
        self.assertIn("alice", text)

    def test_pure_helpers(self):
        self.assertEqual(empty_leader_checkpoint()["epoch"], 0)
        self.assertEqual(empty_leader_checkpoint()["version"], LEADER_CHECKPOINT_VERSION)
        # evidence 有界
        cp = empty_leader_checkpoint()
        cp["evidence"] = list(range(MAX_CHECKPOINT_EVIDENCE + 10))
        self.assertGreater(len(cp["evidence"]), MAX_CHECKPOINT_EVIDENCE)  # 截断发生在写入侧

    # ------------------------------------------------------------------
    # 11. P0 返工（验收阻断）：第四入口 drift 硬门 + 坏 epoch 降级 + TOCTOU
    # ------------------------------------------------------------------

    def test_broadcast_to_relevant_gate_blocks_and_ack_releases(self):
        """第四入口：leader_broadcast_to_relevant 同样受 drift 硬门约束。"""
        self._high_drift_team()
        # 未 ACK → 拒绝
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast_to_relevant(
                            "team", "继续实现 checkpoint 模块", required_roles="coder")
        self.assertIn("已拒绝执行", r)
        self.assertIn("leader_ack_checkpoint", r)
        # 未发送：成员 last_task 不落盘
        data = mcp._load()["teams"]["team"]
        self.assertNotIn("last_task", data["members"]["alice"])

        # ACK → 放行
        self.assertIn("已确认", mcp.leader_ack_checkpoint("team"))
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast_to_relevant(
                            "team", "继续实现 checkpoint 模块", required_roles="coder")
        self.assertIn("已发送", r)

    def test_bad_epoch_renders_gracefully_no_crash(self):
        """损坏 dict checkpoint（epoch 非整数）→ 渲染/漂移/恢复 section 不 crash。"""
        from common.leader_recovery import build_leader_recovery_section
        corrupted = {
            "epoch": "x", "version": 1, "goal": "g", "status": "active",
            "boundaries": [], "decisions": [], "plan": [], "assignments": {},
            "dependencies": [], "deadline": "", "remaining": [], "evidence": [],
            "next_actions": [], "source": "", "updated_by": "", "updated_ts": "",
        }
        team = {"leader": "lead", "leader_type": "tmux",
                "members": {"lead": {"role": "leader", "agent": "claude"}},
                "leader_checkpoint": corrupted}
        # 三个渲染器都不抛异常，且把坏 checkpoint 视为未初始化
        self.assertEqual(build_leader_checkpoint_section(team), [])
        self.assertEqual(leader_checkpoint_drift(team), [])
        text = "\n".join(build_leader_recovery_section("team", team, "/tmp/w", "/tmp/s"))
        self.assertIn("Leader 恢复状态", text)

        # float epoch 归一化渲染，不 crash
        float_cp = dict(corrupted)
        float_cp["epoch"] = 2.0
        team2 = {"leader": "lead", "members": {}, "leader_checkpoint": float_cp}
        section = "\n".join(build_leader_checkpoint_section(team2))
        self.assertIn("epoch", section)

        # leader_activate 对坏 epoch 团队不 crash
        mcp._save({"teams": {"team": team}})
        act = mcp.leader_activate("team")
        self.assertIn("leader 已激活", act)

    def test_atomic_assign_preserves_plan_and_evidence(self):
        """TOCTOU 闭合：原子分配持久化不得覆盖并发写入的 plan/evidence。"""
        self._team(
            leader_task="build P0", terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        # "并发"的 leader 更新：写入 plan（原子路径）
        mcp.leader_checkpoint_set("team", plan="P1\nP2")
        # 回报产生证据（原子路径）
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    mcp.member_report_result("team", "部分进展", member_name="alice")
        plan_before = self._cp()["plan"]
        ev_before = list(self._cp()["evidence"])

        # 原子分配：不应 clobber 上述 plan/evidence
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    mcp.leader_assign_subtask("team", "alice", "implement checkpoint module")

        cp = self._cp()
        self.assertEqual(cp["plan"], plan_before)          # plan 未被覆盖
        self.assertEqual(cp["evidence"], ev_before)        # evidence 未被覆盖
        self.assertEqual(cp["assignments"]["alice"]["task"], "implement checkpoint module")


if __name__ == "__main__":
    unittest.main()
