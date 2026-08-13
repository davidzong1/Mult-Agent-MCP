"""tester-claude 独立新增（task1 颗粒度对齐）——批量 ACK 硬门拦截验收测试。

场景：leader 向全部成员同时发送 ACK（确认/广播）时，当前实现由
``_checkpoint_gate_block`` 在批量入口做"整批一次判定"：单个 HIGH 漂移未确认 →
整批拒绝，leader 只能逐个 ``member_send_message`` 绕门（人工串行）。

【task1 主实现（已落地，多 agent_mcp.py:426-700 member_outbox）】：
  - 通知类 fan-out（leader_batch_ack / leader_broadcast / leader_broadcast_to_relevant）
    使用有界(MEMBER_OUTBOX_MAX=100)、自动推进、可观测 ``member_outbox``；
    gate 挡住时 held_reason="checkpoint_gate" 入队（queued），
    leader_ack_checkpoint 放行后自动投递；跨成员窗口并行、同目标 FIFO；
    state ∈ queued/sending/delivered/failed，含 retries/last_error/message_id。
  - leader_batch_ack 是 ACK/通知语义，不受硬门拦截（免门，入队即推进）。
  - 任务分配类（leader_assign_subtask / leader_assign_task_to_relevant）硬门保持拒绝。

本文件两段设计：
  - 【现状基线】断言当前可复现行为（整批拦截/逐个绕门/无漂移放行）。主实现落地后
    skip（被验收段取代）。
  - 【修复后验收】@skipUnless(_IMPL_READY) 才运行；按落地契约断言：
    leader_batch_ack 同步快路 / 门挡入队非整批拒绝 / ACK 后自动投递 /
    部分失败重试到 failed / 容量幂等顺序 / 任务分配类硬门保留 / 原单发回归。

隔离：与 conftest 环境隔离一致（MULT_AGENT_MCP_HOME + data_layer.set_data_file），
不碰真实 ~/.mult_agent_mcp/。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer

# ---------------------------------------------------------------------------
# task1 主实现落地检测：member_outbox + leader_batch_ack
# ---------------------------------------------------------------------------


def _task1_fix_present() -> bool:
    """返回 True 当 task1 主实现（member_outbox 队列 + leader_batch_ack）已落地。"""
    if hasattr(mcp, "leader_batch_ack"):
        return True
    for marker in ("member_outbox", "_member_outbox", "outbox"):
        if hasattr(mcp, marker):
            return True
    try:
        data = mcp._load() or {}
        for team in (data.get("teams") or {}).values():
            if isinstance(team, dict) and "member_outbox" in team:
                return True
    except Exception:
        pass
    return False


_IMPL_READY = _task1_fix_present()


# ---------------------------------------------------------------------------
# 公共隔离基类
# ---------------------------------------------------------------------------


class _Task1Base(unittest.TestCase):
    THREE = {
        "alice": {"role": "coder", "agent": "claude"},
        "bob": {"role": "reviewer", "agent": "claude"},
        "carol": {"role": "tester", "agent": "claude"},
    }

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.old_globals = {}
        for k in (
            "PROJECT_DIR", "MCP_HOME", "DATA_FILE", "TEAM_WORKSPACES_DIR",
            "SHARE_CONTEXT_DIR", "SHARE_WORKSPACE_DIR", "CLAUDE_GLOBAL_CONFIG_PATH",
            "_OLD_DATA_FILE", "_OLD_SHARE_CONTEXT_DIR",
        ):
            self.old_globals[k] = getattr(mcp, k)
        mcp.PROJECT_DIR = str(self.project)
        mcp.MCP_HOME = str(self.project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(self.project / ".mult_agent_mcp" / "teams_data.json")
        mcp.TEAM_WORKSPACES_DIR = str(self.project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(self.project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(self.project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(self.project / ".claude.json")
        mcp._OLD_DATA_FILE = str(self.project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(self.project / "share_context_space")
        data_layer.set_data_file(Path(mcp.DATA_FILE))
        self.workspace = self.root / "workspace"
        self.context = self.root / "context"
        self.workspace.mkdir(exist_ok=True)
        self.context.mkdir(exist_ok=True)

    def tearDown(self):
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        self.tmp.cleanup()

    # ---- helpers ----
    def _mk_team(self, members=None, leader_task=""):
        team = {
            "workspace_dir": str(self.workspace),
            "context_dir": str(self.context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "members": {"lead": {"role": "leader", "agent": "claude"}},
        }
        for n, i in (members or self.THREE).items():
            team["members"][n] = i
        if leader_task:
            team["leader_last_task"] = leader_task
            team["leader_last_task_completed"] = False
        mcp._save({"teams": {"team": team}})
        return mcp._load()["teams"]["team"]

    def _record_task_start(self, task):
        team = mcp._load()["teams"]["team"]
        mcp._record_leader_task_start(team, task)
        mcp._save({"teams": {"team": team}})

    def _make_high_drift(self):
        """goal 已记录但 leader_last_task 为空 → HIGH 漂移（复刻既有 gate 测试）。"""
        self._record_task_start("build P0")
        data = mcp._load()
        data["teams"]["team"].pop("leader_last_task", None)
        mcp._save(data)
        return mcp._load()["teams"]["team"]

    def _patch_send(
        self, *, targets=None, send_rc=0, send_error="", recover_ok=False, recover_error=""
    ):
        """mock 终端发送相关调用。

        targets 为成员列表时，仅这些成员有窗口；其余触发 _recover_and_send。
        recover_ok/recover_error 控制 _recover_and_send 的返回（部分失败路径）。
        """
        def _target(team_name, name):
            if targets is not None and name not in targets:
                return ""
            return name

        def _recover(team_name, name, session, extra_message=""):
            return recover_ok, recover_error

        return (
            mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"),
            mock.patch.object(mcp, "_member_window_target", side_effect=_target),
            mock.patch.object(mcp, "_send_keys", return_value=(send_rc, send_error)),
            mock.patch.object(mcp, "_recover_and_send", side_effect=_recover),
            mock.patch.object(mcp, "_send_context_to_member", return_value=(send_rc, send_error)),
            mock.patch.object(mcp.time, "sleep", return_value=None),
        )

    def _start(self, mocks_):
        for m in mocks_:
            m.start()
        self.addCleanup(lambda: [m.stop() for m in mocks_])

    def _outbox(self):
        team = mcp._load()["teams"]["team"]
        return team.get("member_outbox", [])


# ===========================================================================
# 一、现状基线（主实现落地前 green；落地后 skip，由验收段取代）
# ===========================================================================


@unittest.skipIf(_IMPL_READY, "task1 主实现已落地：现状基线不再适用")
class TestTask1Baseline(_Task1Base):
    """固化当前可复现行为，作为任务交付事实基线。"""

    def test_baseline_broadcast_whole_batch_blocked_on_single_high_drift(self):
        """现状基线：单个 HIGH 漂移 → leader_broadcast 整批拒绝，3 人全收不到。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_broadcast("team", "全员 ACK")
        self.assertIn("已拒绝执行", r)
        self.assertIn("leader_ack_checkpoint", r)
        self.assertNotIn("alice", r)

    def test_baseline_one_by_one_bypass_via_member_send_message(self):
        """现状基线：同态逐个 member_send_message 可绕门成功 → 当前唯一出路=人工串行。"""
        self._mk_team()
        self._make_high_drift()
        sent = []
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", side_effect=lambda tn, n: n):
                with mock.patch.object(mcp, "_send_context_to_member", return_value=(0, "")):
                    for who in ("alice", "bob", "carol"):
                        r = mcp.member_send_message("team", who, "ACK")
                        self.assertIn("已发送给", r)
                        sent.append(who)
        self.assertEqual(sent, ["alice", "bob", "carol"])

    def test_baseline_no_drift_broadcast_allows_all(self):
        """现状基线：无漂移时批量广播放行（对照组）。"""
        self._mk_team()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_broadcast("team", "全员 ACK")
        self.assertIn("3/3", r)
        for who in ("alice", "bob", "carol"):
            self.assertIn(who, r)


# ===========================================================================
# 二、修复后验收（按落地契约；主实现落地前 skip）
# ===========================================================================


@unittest.skipUnless(_IMPL_READY, "task1 主实现未落地：member_outbox 契约验收暂跳过")
class TestTask1BatchAckAcceptance(_Task1Base):
    """按落地契约验收：有界/自动推进/可观测 member_outbox；
    gate 挡住时 queued(held)、ACK 后自动投递；跨成员并发、同目标 FIFO；
    queued/sending/delivered/failed + retries/last_error/message_id；
    任务分配类硬门保持拒绝。"""

    # ---- leader_batch_ack：免门 + 同步快路 ----

    def test_leader_batch_ack_sync_fastpath_all_delivered(self):
        """leader_batch_ack 入队即推进：全部成员 delivered，一次调用完成整批。"""
        self._mk_team()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_batch_ack("team", "全员 ACK")
        self.assertIn("批量 ACK 已入队", r)
        for who in ("alice", "bob", "carol"):
            self.assertIn(who, r)
        outbox = self._outbox()
        self.assertEqual(len(outbox), 3)
        self.assertTrue(
            all(o.get("state") == "delivered" for o in outbox),
            f"应全部 delivered，实际: {[(o.get('target_member'), o.get('state')) for o in outbox]}",
        )

    def test_leader_batch_ack_immune_to_high_drift(self):
        """leader_batch_ack 是 ACK/通知语义：HIGH 漂移下也立即投递（免门），不整批失败。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_batch_ack("team", "全员 ACK")
        self.assertNotIn("已拒绝执行", r)
        outbox = self._outbox()
        self.assertTrue(
            all(o.get("state") == "delivered" for o in outbox),
            f"batch_ack 免门应全部 delivered: {[(o.get('target_member'), o.get('state')) for o in outbox]}",
        )

    # ---- gate 挡 → 入队（queued/held），非整批拒绝 ----

    def test_gate_block_enqueues_not_whole_batch_reject(self):
        """HIGH 漂移未确认时 leader_broadcast 不再整批拒绝，而是入队并 held。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_broadcast("team", "全员 ACK")
        self.assertNotIn("已拒绝执行", r)
        self.assertIn("入队延后投递", r)
        outbox = self._outbox()
        self.assertEqual(len(outbox), 3)
        for o in outbox:
            self.assertEqual(o.get("state"), "queued")
            self.assertEqual(o.get("held_reason"), "checkpoint_gate")

    def test_gate_block_broadcast_to_relevant_enqueues(self):
        """leader_broadcast_to_relevant 在 gate 挡住时同样入队而非整批拒绝。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=["alice", "bob"]))
        r = mcp.leader_broadcast_to_relevant(
            "team", "继续实现", required_roles="coder,reviewer"
        )
        self.assertNotIn("已拒绝执行", r)
        outbox = self._outbox()
        self.assertEqual(len(outbox), 2)
        for o in outbox:
            self.assertEqual(o.get("state"), "queued")
            self.assertEqual(o.get("held_reason"), "checkpoint_gate")

    # ---- ACK 后自动投递 ----

    def test_ack_advances_queued_to_delivered(self):
        """leader_ack_checkpoint 放行后 member_outbox 自动推进投递，无需人工重发。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=list(self.THREE)))
        r1 = mcp.leader_broadcast("team", "全员 ACK")
        self.assertIn("入队延后投递", r1)
        self.assertEqual(len(self._outbox()), 3)
        ack = mcp.leader_ack_checkpoint("team")
        self.assertIn("已确认", ack)
        outbox = self._outbox()
        self.assertTrue(
            all(o.get("state") == "delivered" for o in outbox),
            f"ACK 后应全部 delivered: {[(o.get('target_member'), o.get('state')) for o in outbox]}",
        )
        self.assertFalse(
            any(o.get("state") in ("queued", "failed") for o in outbox),
            f"不应有滞留 queued/failed: {[(o.get('target_member'), o.get('state')) for o in outbox]}",
        )

    # ---- 部分失败 → retries → failed，且无静默丢消息 ----

    def test_partial_failure_retries_to_failed_no_silent_loss(self):
        """部分成员发送失败（recover 失败）→ retries 累计、最终 failed 带 last_error；
        成功成员保持 delivered，失败不静默吞掉。"""
        self._mk_team()
        self._make_high_drift()
        # carol 窗口缺失且 recover 失败；alice/bob 正常投递
        self._start(
            self._patch_send(
                targets=["alice", "bob"],
                recover_ok=False,
                recover_error="mock-recover-fail",
            )
        )
        r = mcp.leader_broadcast("team", "全员 ACK")
        self.assertIn("入队延后投递", r)
        # ACK 后自动推进一轮
        ack = mcp.leader_ack_checkpoint("team")
        self.assertIn("已确认", ack)
        # 推进多轮到 carol 达到重试上限
        for _ in range(mcp.OUTBOX_RETRY_MAX + 1):
            mcp.leader_flush_outbox("team")
        outbox = {o.get("target_member"): o for o in self._outbox()}
        self.assertEqual(outbox["alice"]["state"], "delivered")
        self.assertEqual(outbox["bob"]["state"], "delivered")
        self.assertEqual(outbox["carol"]["state"], "failed")
        self.assertGreaterEqual(int(outbox["carol"].get("retries") or 0), mcp.OUTBOX_RETRY_MAX)
        self.assertTrue(outbox["carol"].get("last_error"))

    # ---- 有界容量 ----

    def test_outbox_bounded_capacity(self):
        """member_outbox 有界：队列满则显式拒绝（queue-full），绝不静默丢消息。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=list(self.THREE)))
        # 压到小上限，验证满则拒绝
        old_max = mcp.MEMBER_OUTBOX_MAX
        mcp.MEMBER_OUTBOX_MAX = 3
        try:
            r1 = mcp.leader_broadcast("team", "第一批")
            self.assertIn("入队延后投递 3/3", r1)
            r2 = mcp.leader_broadcast("team", "第二批")
            self.assertIn("拒绝", r2)
            self.assertIn("queue-full", r2)
        finally:
            mcp.MEMBER_OUTBOX_MAX = old_max
        self.assertEqual(len(self._outbox()), 3)

    # ---- 幂等 / 同目标 FIFO ----

    def test_message_id_idempotent_no_dup_on_retry(self):
        """同 message_id 重复入队跳过（幂等）；delivered 后重放不双发。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=["alice"]))
        # 直接用入队原语验证幂等：同 message_id 二次入队 → rejected dup
        r1 = mcp._enqueue_outbox_messages(
            "team", ["alice"], "msg", kind="ack", message_ids={"alice": "mid-1"}
        )
        r2 = mcp._enqueue_outbox_messages(
            "team", ["alice"], "msg", kind="ack", message_ids={"alice": "mid-1"}
        )
        self.assertEqual(len(r1.get("enqueued") or []), 1)
        self.assertIn("dup", " ".join(r2.get("rejected") or []))
        self.assertEqual(len(self._outbox()), 1)

    def test_fifo_order_same_target(self):
        """同目标成员消息顺序保持 FIFO（先入先送）。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=["alice"]))
        mcp.leader_broadcast("team", "msg-1")
        mcp.leader_broadcast("team", "msg-2")
        alice_entries = [o for o in self._outbox() if o.get("target_member") == "alice"]
        self.assertEqual(len(alice_entries), 2)
        # ACK 后只推进队首（msg-1），msg-2 仍 queued → 证明 per-target FIFO
        mcp.leader_ack_checkpoint("team")
        alice_entries = [o for o in self._outbox() if o.get("target_member") == "alice"]
        self.assertEqual(alice_entries[0]["state"], "delivered")
        self.assertEqual(alice_entries[0]["payload"], "msg-1")
        self.assertEqual(alice_entries[1]["state"], "queued")
        # 再次 flush → msg-2 送达
        mcp.leader_flush_outbox("team")
        alice_entries = [o for o in self._outbox() if o.get("target_member") == "alice"]
        self.assertEqual(alice_entries[1]["state"], "delivered")

    # ---- 任务分配类硬门保留 / 原单发回归 ----

    def test_assignment_hard_gate_still_rejects(self):
        """任务分配类（leader_assign_subtask）在 HIGH 漂移未确认时保持拒绝、不落盘。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=["alice"]))
        r = mcp.leader_assign_subtask("team", "alice", "implement x")
        self.assertIn("已拒绝执行", r)
        data = mcp._load()["teams"]["team"]
        self.assertNotIn("last_task", data["members"]["alice"])
        self.assertEqual(data["leader_checkpoint"]["assignments"], {})

    def test_assign_to_relevant_hard_gate_still_rejects(self):
        """leader_assign_task_to_relevant 同样保持硬门拒绝（任务分配类不受 fan-out 豁免）。"""
        self._mk_team()
        self._make_high_drift()
        self._start(self._patch_send(targets=["alice"]))
        r = mcp.leader_assign_task_to_relevant("team", "实现 checkpoint 模块")
        self.assertIn("已拒绝执行", r)

    def test_single_assign_path_no_drift_no_regression(self):
        """原单发路径 leader_assign_subtask 无漂移时行为不变（可分配、落盘、checkpoint）。"""
        self._mk_team()
        self._start(self._patch_send(targets=["alice"]))
        r = mcp.leader_assign_subtask("team", "alice", "implement x")
        self.assertIn("子任务已分配", r)
        data = mcp._load()["teams"]["team"]
        self.assertEqual(data["members"]["alice"]["last_task"], "implement x")
        self.assertFalse(data["members"]["alice"]["last_task_completed"])

    def test_broadcast_no_drift_sync_path_unchanged(self):
        """无漂移批量广播仍同步 3/3 全送达（不走队列的快路保持原语义）。"""
        self._mk_team()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_broadcast("team", "全员 ACK")
        self.assertIn("3/3", r)
        self.assertEqual(self._outbox(), [])

    # ---- 不破坏既有团队状态 ----

    def test_batch_ack_does_not_pollute_member_task_state(self):
        """批量 ACK 不应把普通成员误标记为有任务/已完成。"""
        self._mk_team()
        self._start(self._patch_send(targets=list(self.THREE)))
        r = mcp.leader_batch_ack("team", "全员 ACK")
        self.assertNotIn("已拒绝执行", r)
        data = mcp._load()["teams"]["team"]
        for who in ("alice", "bob", "carol"):
            self.assertNotIn("last_task", data["members"][who])
            self.assertNotIn("last_task_completed", data["members"][who])


if __name__ == "__main__":
    unittest.main(verbosity=2)
