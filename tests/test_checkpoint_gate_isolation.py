"""
成员任务 checkpoint 验收隔离用例（③ tester/reviewer 交付）。

覆盖 leader 验收阻断项 ③ 的三个场景，全部在隔离临时数据文件上执行：
  1. 绕过硬门 —— HIGH 漂移未 ACK 时，leader_broadcast_to_relevant 也必须被
     _checkpoint_gate_block 拒绝（与 leader_broadcast / leader_assign_* 一致）。
     coder 的既有测试覆盖了 assign/broadcast，但**未覆盖 broadcast_to_relevant
     这条旁路** —— 若该入口漏接硬门，leader 可绕过 drift 闸门直接注入指令。
  2. ACK 后放行 —— leader_ack_checkpoint 确认当前 epoch 后，broadcast_to_relevant
     恢复放行；旧 ack 在 epoch 推进后失效需重新确认。
  3. quota 恢复从 checkpoint 续跑 —— 换号只改 agent_user/quota_switch_count，
     成员 task_checkpoint（含 completed_steps/remaining_instruction/epoch/writer）
     完整保留；verify-then-continue 数据契约对 refactor 的 _recover_and_send 生效。

依赖：① coder 的 leader_checkpoint 已落地（leader_checkpoint_high_drift /
_checkpoint_gate_block / leader_ack_checkpoint）；② refactor 的 quota 接线未落地，
本文件的契约测试给出 refactor 必须满足的数据不变量，不假定具体实现。

隔离：复用 test_leader_checkpoint 的 mcp 模块全局临时覆盖模式 + data_layer
set_data_file 兜底（conftest 环境级隔离 + atomic_json_write fail-fast 守卫）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import checkpoint as ckpt
from common import data_layer


class TestCheckpointGateIsolation(unittest.TestCase):
    """③ 隔离用例：绕过硬门 / ACK 后放行 / quota 续跑契约。"""

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
        # common.checkpoint 经 data_layer 读写，须与 mcp.DATA_FILE 对齐同一文件，
        # 否则 ckpt 写 conftest 临时文件、mcp._load 读 project 文件，两侧数据分离。
        data_layer.set_data_file(Path(mcp.DATA_FILE))
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

    def _record_task_start(self, task: str, context: str = ""):
        """按真实调用点：_record_leader_task_start 原地改 team → _save。"""
        team = mcp._load()["teams"]["team"]
        mcp._record_leader_task_start(team, task, context)
        mcp._save({"teams": {"team": team}})

    def _high_drift_team(self, members=None):
        """构造 HIGH 漂移：checkpoint.goal 与 leader_last_task 冲突。"""
        self._team(
            leader_task="build P0",
            leader_type="direct",
            terminals_active=True,
            members=members or {"alice": {"role": "coder", "agent": "claude"}},
        )
        self._record_task_start("build P0")
        data = mcp._load()
        team = data["teams"]["team"]
        team["leader_checkpoint"]["goal"] = "不同方向目标，与 leader_last_task 冲突"
        mcp._save(data)
        return mcp._load()["teams"]["team"]

    def _cp(self) -> dict:
        return mcp._load().get("teams", {}).get("team", {}).get("leader_checkpoint", {})

    # ==================================================================
    # ① 绕过硬门：broadcast_to_relevant 旁路
    # ==================================================================

    def test_broadcast_to_relevant_blocked_on_unacked_high_drift(self):
        """P0 task1 门语义：HIGH 漂移未 ACK 时 leader_broadcast_to_relevant 不再整批拒绝，
        而是入队 member_outbox 并 held(checkpoint_gate)——消息不会被送出（drift 保护保留），
        leader_ack_checkpoint 放行后自动投递（不要求人工逐个发送）。

        ⚠️ 回归要点：这是 coder 既有 gate 测试未覆盖的旁路入口 ——
        leader_assign_task_to_relevant 已接 _checkpoint_gate_block，但
        leader_broadcast_to_relevant 若漏接，leader 可用它绕过 drift 闸门
        向成员注入指令。本条要求该入口同样受 drift 保护（held 而非投递）。
        """
        self._high_drift_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast_to_relevant(
                            "team", "继续实现 checkpoint 模块", required_roles="coder",
                        )
        self.assertIn("入队延后投递", r)
        # drift 保护保留：消息 held，未在漂移未确认时被送达
        data = mcp._load()["teams"]["team"]
        outbox = data.get("member_outbox") or []
        self.assertTrue(outbox, "广播应入队 outbox")
        self.assertTrue(
            all(e.get("held_reason") == "checkpoint_gate" for e in outbox),
            "gate-held 消息必须标 held_reason=checkpoint_gate",
        )
        self.assertTrue(
            all(e.get("state") in ("queued", "sending") for e in outbox),
            "漂移未确认时消息不得被送达(delivered)",
        )

    def test_assign_to_relevant_blocked_on_unacked_high_drift(self):
        """绕过硬门：leader_assign_task_to_relevant 同样被拒绝（已接门，回归确认）。"""
        self._high_drift_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_assign_task_to_relevant("team", "实现 checkpoint 模块")
        self.assertIn("已拒绝执行", r)
        data = mcp._load()["teams"]["team"]
        self.assertEqual(data["leader_checkpoint"]["assignments"], {})

    # ==================================================================
    # ② ACK 后放行：确认后旁路入口恢复；旧 ack 失效需重确认
    # ==================================================================

    def test_ack_then_broadcast_to_relevant_allowed(self):
        """ACK 后放行：确认当前 checkpoint 后 broadcast_to_relevant 恢复。"""
        self._high_drift_team()
        r = mcp.leader_ack_checkpoint("team")
        self.assertIn("已确认", r)

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast_to_relevant(
                            "team", "继续实现 checkpoint 模块", required_roles="coder",
                        )
        self.assertIn("已发送", r)

    def test_stale_ack_blocks_to_relevant_after_epoch_bump(self):
        """旧 ack 失效：epoch 推进后 broadcast_to_relevant 需重新确认。"""
        self._high_drift_team()
        self.assertIn("已确认", mcp.leader_ack_checkpoint("team"))
        # 新写入（成员回报）推进 epoch → ack.epoch 过期
        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value={
                    "compact_path": "ctx.md", "compact_sent": False,
                    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
                }):
                    mcp.member_report_result("team", "progress update", member_name="alice")
        data = mcp._load()["teams"]["team"]
        self.assertGreater(
            data["leader_checkpoint"]["epoch"], data["leader_checkpoint_ack"]["epoch"]
        )

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast_to_relevant(
                            "team", "继续", required_roles="coder",
                        )
        # P0 task1 门语义：stale ack 下不再整批拒绝，而是入队 held（消息不被送达）
        self.assertIn("入队延后投递", r)
        data = mcp._load()["teams"]["team"]
        self.assertTrue(
            all(e.get("held_reason") == "checkpoint_gate" for e in data.get("member_outbox") or []),
            "stale ack 下广播必须 held，不得送达",
        )

        # 重新 ACK 最新 epoch → 放行
        self.assertIn("已确认", mcp.leader_ack_checkpoint("team"))
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                        r = mcp.leader_broadcast_to_relevant(
                            "team", "继续", required_roles="coder",
                        )
        self.assertIn("已发送", r)

    # ==================================================================
    # ③ quota 恢复从 checkpoint 续跑（成员 task_checkpoint 数据契约）
    # ==================================================================

    def test_quota_switch_preserves_member_task_checkpoint(self):
        """quota 换号不改动成员 task_checkpoint（completed_steps/remaining_instruction/
        epoch/writer 完整保留）—— refactor 的 _recover_and_send 续跑数据源。"""
        self._team(
            leader_task="build P0",
            leader_type="direct",
            terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        # 用 common.checkpoint 持久化成员任务进度（P1 原语）
        cp = ckpt.empty_checkpoint("t1", task="实现 checkpoint 模块", writer="alice")
        cp = ckpt.record_step_done(cp, "design")
        cp["current_step"] = "code"
        cp["remaining_instruction"] = "继续编码，完成后回报"
        ok, err = ckpt.save_checkpoint(team_name="team", member_name="alice", cp=cp, writer="alice")
        self.assertTrue(ok, err)

        before, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")

        # 模拟 quota 换号数据效果：只改 agent_user + quota_switch_count
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        member["agent_user"] = "acct-b"
        member["quota_switch_count"] = member.get("quota_switch_count", 0) + 1
        member["blocked_reason"] = "quota"
        mcp._save(data)

        after, errors = ckpt.load_checkpoint(team_name="team", member_name="alice")
        self.assertEqual(errors, [])
        self.assertIsNotNone(after)
        self.assertEqual(after["epoch"], before["epoch"])
        self.assertEqual(after["completed_steps"], ["design"])
        self.assertEqual(after["current_step"], "code")
        self.assertEqual(after["remaining_instruction"], "继续编码，完成后回报")
        self.assertEqual(after["writer"], "alice")

    def test_quota_recovery_verify_then_continue_contract(self):
        """quota 恢复的 verify-then-continue 数据契约：
        换号后新窗口基于保留的 checkpoint 续跑（epoch 匹配 + state=running），
        旧 writer 的过期拷贝被拒 —— 供 refactor 的 _recover_and_send 接线后直接消费。"""
        self._team(
            leader_task="build P0",
            leader_type="direct",
            terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        cp = ckpt.empty_checkpoint("t1", task="实现 checkpoint 模块", writer="alice")
        cp = ckpt.record_step_done(cp, "design")
        cp["remaining_instruction"] = "继续编码"
        ok, err = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: cp,
        )
        self.assertTrue(ok, err)

        current, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        # 新窗口（换号后）按保留的 checkpoint 续跑 → 通过
        ok, reason = ckpt.verify_then_continue(
            current, expected_epoch=current["epoch"], expected_writer="alice"
        )
        self.assertTrue(ok, reason)

        # 旧窗口的过期拷贝（epoch 落后）被拒 → 不覆盖新进度
        stale = dict(current)
        stale["epoch"] = current["epoch"] - 1
        ok, reason = ckpt.verify_then_continue(stale, expected_epoch=current["epoch"])
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_quota_switch_uses_team_data_lock(self):
        """② 契约：checkpoint 写入/读取必须传 TEAM_DATA_LOCK（与 leader 数据路径互斥）。

        验证 common.checkpoint 持久化原语接受 mult_agent_mcp.TEAM_DATA_LOCK
        作为锁传入且正确落盘 —— refactor 在 _recover_and_send 里应传此锁。
        """
        self._team(
            leader_task="build P0",
            leader_type="direct",
            terminals_active=True,
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        cp = ckpt.empty_checkpoint("t1", writer="alice")
        ok, err = ckpt.save_checkpoint(
            mcp.TEAM_DATA_LOCK, team_name="team", member_name="alice", cp=cp, writer="alice",
        )
        self.assertTrue(ok, err)
        loaded, errors = ckpt.load_checkpoint(
            mcp.TEAM_DATA_LOCK, team_name="team", member_name="alice",
        )
        self.assertEqual(errors, [])
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded["writer"], "alice")


if __name__ == "__main__":
    unittest.main()
