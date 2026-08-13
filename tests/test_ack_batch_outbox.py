"""P0 task1 回归：批量 ACK 消息队列（member_outbox）与硬门门语义。

覆盖统一验收：
  1) 批量 ACK 不因单个硬门导致整批失败或要求人工串行 —— broadcast 未 ACK 时
     入队 held、ACK 后自动投递（不送达漂移消息 + 不整批拒绝）；
  2) 失败/重试/顺序语义清楚且无静默丢消息 —— 有界显式 queue-full、重试耗尽
     显式 failed、per-target FIFO、message_id 幂等、sending 崩溃残留可恢复；
  3) 新增回归覆盖多成员、部分失败/阻塞、原单发路径（member_send_message 不变）、
     任务分配硬门仍拒绝（验收#5）。

隔离：临时 teams_data + mock tmux，零触生产（conftest 环境级兜底）。
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp


class _IsolatedTestCase(unittest.TestCase):
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
        from common import data_layer

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
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # ------------------------------------------------------------------ helpers

    def _team(self, members=None, high_drift=False, leader_task=None):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        mbr = {
            "lead": {"role": "leader", "agent": "codex",
                     "tmux_window_id": "@lead", "tmux_session": "sess"},
            "a": {"role": "coder", "agent": "claude",
                  "tmux_window_id": "@a", "tmux_session": "sess",
                  "last_task": "任务A", "last_task_completed": False},
            "b": {"role": "coder", "agent": "claude",
                  "tmux_window_id": "@b", "tmux_session": "sess",
                  "last_task": "任务B", "last_task_completed": False},
            "c": {"role": "coder", "agent": "claude",
                  "tmux_window_id": "@c", "tmux_session": "sess",
                  "last_task": "任务C", "last_task_completed": False},
        }
        if members:
            mbr.update(members)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            "members": mbr,
        }
        if high_drift:
            team["leader_checkpoint"] = {
                "epoch": 1, "version": 1, "goal": "实现登录模块",
                "assignments": {"a": {"task": "任务A", "status": "assigned"}},
            }
            team["leader_last_task"] = leader_task or "完全不同方向"
        mcp._save({"teams": {"team": team}})
        return team

    def _outbox(self):
        return mcp._load().get("teams", {}).get("team", {}).get("member_outbox") or []

    def _tmux_mocks(self, fail_members=()):
        """mock tmux 相关调用；send_keys 对 fail_members 返回失败。"""
        sent = []
        fail = set(fail_members)

        def fake_send_keys(session, window, text, **kw):
            # window 实为 _member_window_target 返回的成员名（见下）
            sent.append((window, text))
            return (1, "boom") if window in fail else (0, "")

        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="sess"),
            mock.patch.object(mcp, "_member_window_target",
                              side_effect=lambda team_name, name: name),
            mock.patch.object(mcp, "_send_keys", side_effect=fake_send_keys),
            mock.patch.object(mcp, "_recover_and_send", return_value=(True, "")),
            mock.patch.object(mcp.time, "sleep", return_value=None),
        ]
        return mocks, sent

    def _enter(self, mocks):
        for m in mocks:
            m.start()
        self._stack = mocks

    def _exit(self):
        for m in reversed(self._stack):
            m.stop()

    # ------------------------------------------------------------------ 批量 ACK

    def test_batch_ack_delivers_to_all_members(self):
        """leader_batch_ack 一次调用 → 全部成员入队 + 自动投递（无需逐个发送）。"""
        self._team()
        mocks, sent = self._tmux_mocks()
        self._enter(mocks)
        try:
            r = mcp.leader_batch_ack("team", "[ACK] 收到你们全部回报")
        finally:
            self._exit()
        self.assertIn("批量 ACK 已入队 3/3 人", r)
        self.assertIn("已送达: a, b, c", r)
        self.assertEqual({w for w, _ in sent}, {"a", "b", "c"})
        self.assertEqual(len(sent), 3)
        for e in self._outbox():
            self.assertEqual(e["state"], "delivered", f"应全部 delivered: {e}")

    def test_batch_ack_subset_only(self):
        """member_names 指定子集 → 只入队/投递指定成员。"""
        self._team()
        mocks, sent = self._tmux_mocks()
        self._enter(mocks)
        try:
            r = mcp.leader_batch_ack("team", "[ACK] 部分", member_names="a,b")
        finally:
            self._exit()
        self.assertIn("2/2 人", r)
        self.assertEqual({w for w, _ in sent}, {"a", "b"})
        self.assertNotIn("c", {w for w, _ in sent})

    def test_batch_ack_ignores_leader_member(self):
        """leader 成员自动排除，不被当成投递目标。"""
        self._team()
        mocks, sent = self._tmux_mocks()
        self._enter(mocks)
        try:
            r = mcp.leader_batch_ack("team", "[ACK]", member_names="lead,a")
        finally:
            self._exit()
        self.assertEqual({w for w, _ in sent}, {"a"})

    # ------------------------------------------------------------------ 部分失败/重试

    def test_batch_ack_partial_failure_others_delivered(self):
        """部分失败不整批失败：a 投递失败 → b/c 仍送达，a 保留在队列重试。"""
        self._team()
        mocks, sent = self._tmux_mocks(fail_members={"a"})
        self._enter(mocks)
        try:
            r = mcp.leader_batch_ack("team", "[ACK] 收到")
        finally:
            self._exit()
        self.assertIn("已送达: b, c", r)
        self.assertIn("将重试", r)  # a 失败但未超限 → retrying
        states = {e["target_member"]: e["state"] for e in self._outbox()}
        self.assertEqual(states["b"], "delivered")
        self.assertEqual(states["c"], "delivered")
        self.assertEqual(states["a"], "queued", "失败未超限应回 queued 等待重试")
        self.assertEqual(self._outbox()[0]["retries"], 1)

    def test_retry_exhausted_explicit_failed_queryable(self):
        """重试耗尽 → 显式 failed + last_error，leader_outbox_status 可查询。"""
        self._team()
        mocks, sent = self._tmux_mocks(fail_members={"a"})
        self._enter(mocks)
        try:
            mcp._enqueue_outbox_messages("team", ["a"], "hello", "ack")
            for _ in range(mcp.OUTBOX_RETRY_MAX):
                r = mcp.leader_flush_outbox("team")
            st = mcp.leader_outbox_status("team")
        finally:
            self._exit()
        states = {e["target_member"]: e["state"] for e in self._outbox()}
        self.assertEqual(states["a"], "failed", "重试耗尽应显式 failed")
        self.assertIn("a", st)
        self.assertIn("failed=1", st)
        self.assertIn("boom", [e.get("last_error") for e in self._outbox() if e["target_member"] == "a"])

    # ------------------------------------------------------------------ 硬门门语义

    def test_broadcast_gate_hold_then_ack_auto_delivers(self):
        """批量 ACK 不因单个硬门整批失败：broadcast 未 ACK 时入队 held（不送达），
        leader_ack_checkpoint 后自动投递（不要求人工逐个发送）。"""
        self._team(high_drift=True)
        mocks, sent = self._tmux_mocks()
        self._enter(mocks)
        try:
            r1 = mcp.leader_broadcast("team", "[ACK] 收到回报")
            self.assertIn("入队延后投递 3/3", r1)
            self.assertEqual(sent, [], "漂移未确认时不得送出")
            held = self._outbox()
            self.assertTrue(held)
            self.assertTrue(all(e["held_reason"] == "checkpoint_gate" for e in held))

            # 注意：ack_checkpoint 内部自动推进 outbox，无需 mock _send_keys 差异
            r2 = mcp.leader_ack_checkpoint("team")
            self.assertIn("已自动投递 outbox", r2)
        finally:
            self._exit()
        self.assertEqual({w for w, _ in sent}, {"a", "b", "c"})
        self.assertTrue(all(e["state"] == "delivered" for e in self._outbox()))

    def test_assign_still_rejects_on_unacked_high_drift(self):
        """任务分配硬门仍拒绝（验收#5）：HIGH 漂移未 ACK 时 leader_assign_subtask 拒绝，
        成员 last_task 不变（分配不落盘）。"""
        self._team(high_drift=True)
        with mock.patch.object(mcp, "_find_any_session", return_value="sess"):
            with mock.patch.object(mcp, "_member_window_target",
                                   side_effect=lambda t, n: n):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    r = mcp.leader_assign_subtask("team", "a", "implement checkpoint")
        self.assertIn("已拒绝执行", r)
        data = mcp._load()["teams"]["team"]
        self.assertEqual(data["members"]["a"]["last_task"], "任务A",
                         "gate 拒绝时分配不得覆盖成员 last_task")

    # ------------------------------------------------------------------ 队列语义

    def test_outbox_bounded_no_silent_drop(self):
        """有界：队列满 → 显式 queue-full 拒绝（无静默丢消息）。"""
        self._team()
        mocks, sent = self._tmux_mocks()
        self._enter(mocks)
        try:
            # 塞满队列
            for i in range(mcp.MEMBER_OUTBOX_MAX):
                mcp._enqueue_outbox_messages(
                    "team", ["a"], f"msg{i}", "ack", held_reason="")
            # 满后再入队 → 显式拒绝
            res = mcp._enqueue_outbox_messages("team", ["b"], "overflow", "ack")
        finally:
            self._exit()
        self.assertIn("b:queue-full", res["rejected"])
        self.assertEqual(len(self._outbox()), mcp.MEMBER_OUTBOX_MAX,
                         "队列不得超过上限")

    def test_message_id_idempotent_no_duplicate(self):
        """message_id 幂等：同 id 重复入队跳过，重试不双发。"""
        self._team()
        mid = {"a": "ack_test_1", "b": "ack_test_2"}
        r1 = mcp._enqueue_outbox_messages(
            "team", ["a", "b"], "hello", "ack", message_ids=mid)
        r2 = mcp._enqueue_outbox_messages(
            "team", ["a", "b"], "hello", "ack", message_ids=mid)
        self.assertEqual(len(r1["enqueued"]), 2)
        self.assertTrue(all("dup" in x for x in r2["rejected"]),
                        f"重复入队应拒绝: {r2['rejected']}")
        self.assertEqual(len(self._outbox()), 2, "同 id 不得产生重复条目")

    def test_per_target_fifo_order(self):
        """per-target FIFO：同一成员多条消息按入队顺序投递，一次推进只送队首。"""
        self._team()
        mocks, sent = self._tmux_mocks()
        self._enter(mocks)
        try:
            mcp._enqueue_outbox_messages("team", ["a"], "first", "ack")
            mcp._enqueue_outbox_messages("team", ["a"], "second", "ack")
            # 第一次推进：per-target FIFO 只送队首 first，second 保持 queued
            r1 = mcp._advance_member_outbox_once("team")
            self.assertEqual(r1["delivered"], ["a"])
            a_msgs = [t for w, t in sent if w == "a"]
            self.assertEqual(a_msgs, ["first"], "同成员一次只能送队首")
            states = [e["state"] for e in self._outbox()]
            self.assertEqual(states, ["delivered", "queued"], "second 应仍 queued")
            # 第二次推进：second 送达，顺序保持 FIFO
            r2 = mcp._advance_member_outbox_once("team")
            self.assertEqual(r2["delivered"], ["a"])
            a_msgs = [t for w, t in sent if w == "a"]
            self.assertEqual(a_msgs, ["first", "second"],
                             "同成员消息必须 FIFO 顺序投递")
        finally:
            self._exit()
        self.assertTrue(all(e["state"] == "delivered" for e in self._outbox()))

    def test_stale_sending_recovered_not_stuck(self):
        """崩溃恢复：sending 状态停留超过阈值 → 重置回 queued 并重试（不永久卡死）。"""
        self._team()
        # 手工塞一条 stuck 在 sending 的旧消息（模拟发送中途崩溃）
        stale_ts = (datetime.now() - timedelta(seconds=mcp.OUTBOX_SENDING_STALE_SECONDS + 10)).isoformat()
        self._team()
        with mock.patch.object(mcp, "_find_any_session", return_value="sess"):
            with mock.patch.object(mcp, "_member_window_target",
                                   side_effect=lambda t, n: n):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    mcp._enqueue_outbox_messages("team", ["a"], "hello", "ack")
                    data = mcp._load()
                    entry = data["teams"]["team"]["member_outbox"][0]
                    entry["state"] = "sending"
                    entry["sending_started_ts"] = stale_ts
                    mcp._save(data)
                    r = mcp._advance_member_outbox_once("team")
        self.assertIn("a", r.get("delivered") or [], f"sending 残留应恢复投递: {r}")
        self.assertEqual(self._outbox()[0]["state"], "delivered")

    # ------------------------------------------------------------------ 原单发路径

    def test_member_send_message_unchanged(self):
        """原单发路径不回归：member_send_message 仍正常工作、不入 outbox。"""
        self._team()
        sent = []

        def fake_send_keys(session, window, text, **kw):
            sent.append((window, text))
            return 0, ""

        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="sess"),
            mock.patch.object(mcp, "_member_window_target",
                              side_effect=lambda team_name, name: name),
            mock.patch.object(mcp, "_send_keys", side_effect=fake_send_keys),
            mock.patch.object(mcp, "_target_is_claude_tmux_leader", return_value=False),
            mock.patch.object(mcp, "_target_is_codex_tmux_leader", return_value=False),
        ]
        for m in mocks:
            m.start()
        try:
            r = mcp.member_send_message("team", "b", "[ACK] 你的回报已收到")
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertIn("消息已发送给", r)
        self.assertEqual([w for w, _ in sent], ["b"])
        self.assertEqual(self._outbox(), [], "单发路径不得写入 outbox")


if __name__ == "__main__":
    unittest.main()
