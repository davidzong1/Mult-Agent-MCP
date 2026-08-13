"""P1 竞态回归测试 —— 成员已调用 member_report_result、回报仍留在成员对话窗或状态已 idle，
但 Codex leader 误判未发送的竞态（monitor stale 快照整份覆写）。

根因：``_scan_member_terminal`` 整份 load→mutate→save，锁在 load/save 内各自持有、
两端之间释放；与 ``member_report_result`` 的 load→mutate→save 交叠时，monitor 末尾
``_save(data)`` 用开头加载的 stale 快照整份覆写并发 report 落盘的字段。

三类失败形态：
  A1/R1  busy 分类    stale 保存回退 last_task_completed → leader_check_member_status 误判"进行中"
  A2/R2  idle 分类    stale 快照未见到亲笔回报 → 重复生成 monitor_inferred_completion 合成回报，
                      亲笔 pending 被清空顶替、results 双份
  A3/R7  approval 分类 同 R1 回退

验收口径（leader 强加）：不得以 idle/terminal classifier 判完成；事实 = 持久化
report_id + pending 记录 + leader_activate 消费(ACK, leader_last_ack.report_ids)。

分组：
  A 组（失败回归，当前代码 monitor 竞态未修复 → 预期 FAIL×3）
  B 组（修复后通过场景：重复 report / leader 休眠死亡复活 / compact+re-entry /
        双成员并发 / 授权卡点）
  C 组（持久化证据：report_id 稳定、pending 去重、ACK report_ids 一致、
        monitor 推断被亲笔回报替换）

运行：python3 tests/test_report_vs_monitor_race_regression.py  或  pytest tests/...
隔离：MULT_AGENT_MCP_HOME 临时目录 + mock tmux IPC，不触碰生产数据。
"""
import contextlib
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import mult_agent_mcp as mcp  # noqa: E402


def _is_isolated_home() -> bool:
    """pytest 直跑时强制隔离：注入临时 MULT_AGENT_MCP_HOME 到子进程路径。"""
    return bool(os.environ.get("MULT_AGENT_MCP_HOME"))


class RaceRegressionBase(unittest.TestCase):
    """隔离装配基类：重定向全部数据/共享路径到临时目录，不改生产数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old = {
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
        for k in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD"):
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self.old.items():
            setattr(mcp, k, v)
        self.tmp.cleanup()

    # ---- 装配 ----
    def _setup_team(self, members=("alice",)):
        ws, ctx = self.root / "workspace", self.root / "context"
        ws.mkdir(); ctx.mkdir()
        member_map = {}
        for name in members:
            member_map[name] = {
                "role": "coder" if name != "lead" else "leader",
                "agent": "codex" if name == "lead" else "claude",
                "last_task": f"{name} 的任务",
                "last_context": f"{name} 的上下文",
                "last_task_completed": False,
                "tmux_window_id": f"@{hash(name) % 100}",
                "tmux_session": "mcp_team",
                "tmux_session_id": "$1",
                "tmux_session_created": "1000",
            }
        team = {
            "workspace_dir": str(ws),
            "context_dir": str(ctx),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            "members": member_map,
        }
        mcp._save({"teams": {"team": team}})
        return ws, ctx

    def _results(self):
        path = self.root / "context" / "results.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text("utf-8").splitlines()]

    def _team(self):
        return mcp._load()["teams"]["team"]

    def _pending(self):
        return self._team().get("leader_pending_reports", [])

    # ---- tmux IPC mock ----
    def _mocks(self, capture_side_effect=None, member_window=True,
               leader_dead=False, leader_idle=False, send_keys=None):
        """标准 tmux IPC mock 集。capture_side_effect 支持在捕获期间注入并发操作。"""
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"))
        if member_window:
            stack.enter_context(mock.patch.object(
                mcp, "_member_window_target",
                side_effect=lambda t, m: m,  # 窗口目标 = 成员名（lead→lead, alice→alice）
            ))
        stack.enter_context(mock.patch.object(
            mcp, "_capture_window",
            side_effect=capture_side_effect if capture_side_effect is not None
            else (lambda *a, **k: (0, "", "")),
        ))
        stack.enter_context(mock.patch.object(mcp, "_leader_window_is_dead", return_value=leader_dead))
        stack.enter_context(mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=leader_idle))
        if send_keys is None:
            stack.enter_context(mock.patch.object(mcp, "_send_keys", return_value=(0, "")))
        else:
            stack.enter_context(mock.patch.object(mcp, "_send_keys", side_effect=send_keys))
        stack.enter_context(mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")))
        return stack


# ======================================================================
# A 组：monitor stale-save 竞态失败回归（当前代码预期 FAIL×3）
# ======================================================================
class MonitorRaceRegression(RaceRegressionBase):
    """A1/A2/A3 —— monitor stale 整份覆写并发 report。

    当前代码（coder S1/S2/S3 修复回报路径后）`_scan_member_terminal` 末尾
    `_save(data)`（:2477）与 idle 分支 `_save`（:2444/:2473）仍是 stale 快照
    覆写 → 本组断言在修复落地前保持失败，作为回归红线。
    """

    def test_a1_busy_stale_save_reverts_completion(self):
        """A1/R1: 回报后 monitor 按 busy 分类用 stale 快照保存 → 完成标记回退。"""
        self._setup_team()

        def racing_capture(session, win, lines):
            mcp.member_report_result("team", "亲笔结果", member_name="alice")
            return (0, "busy running...", "")

        with self._mocks(capture_side_effect=racing_capture):
            mcp._scan_member_terminal("team", "alice")

        fresh = mcp._load()["teams"]["team"]["members"]["alice"]
        # 事实：亲笔回报确已持久化
        self.assertEqual(len(self._results()), 1, "亲笔回报已写入 results.jsonl")
        # 竞态断言：monitor 不得回退回报落盘的完成标记（当前代码回退 → FAIL）
        self.assertTrue(fresh.get("last_task_completed"),
                        "monitor stale save 回退了回报的 last_task_completed")
        status = mcp.leader_check_member_status("team", "alice")
        self.assertIn("✅ 完成", status, "leader_check_member_status 不得误判进行中")

    def test_a2_idle_stale_save_duplicates_synthetic_report(self):
        """A2/R2: 回报后 monitor 按 idle 分类（stale 快照未见亲笔回报）→ 重复合成回报。"""
        self._setup_team()

        def racing_capture(session, win, lines):
            mcp.member_report_result("team", "亲笔结果", member_name="alice")
            return (0, "❯\n⏸ manual mode on", "")

        with self._mocks(capture_side_effect=racing_capture):
            mcp._scan_member_terminal("team", "alice")

        events = [r.get("event") for r in self._pending()]
        print("A2 pending events =", events)
        print("A2 results len =", len(self._results()))
        # 亲笔回报必须保留 1 份（当前被 stale 保存清空 → 0 ≠ 1 → FAIL）
        self.assertEqual(events.count("member_report"), 1, "亲笔回报 pending 1 份")
        # monitor 不得在亲笔回报后重复生成合成回报（当前多 1 份 → FAIL）
        self.assertEqual(events.count(mcp.MONITOR_INFERRED_EVENT), 0,
                        "monitor 不得重复生成合成回报")

    def test_a3_approval_stale_save_reverts_completion(self):
        """A3/R7: 授权卡点期间回报后 monitor 按 approval 分类 stale 保存 → 完成标记回退。"""
        self._setup_team()

        def racing_capture(session, win, lines):
            mcp.member_report_result("team", "授权后结果", member_name="alice")
            return (0, "⛔ approval required", "")

        with self._mocks(capture_side_effect=racing_capture):
            mcp._scan_member_terminal("team", "alice")

        fresh = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(fresh.get("last_task_completed"),
                        "approval stale save 回退了回报的 last_task_completed")


# ======================================================================
# B 组：修复后通过场景（重复 report / leader 休眠死亡复活 / compact /
#       re-entry / 双成员并发 / 授权卡点）
# ======================================================================
class PostFixScenarios(RaceRegressionBase):
    """B1-B6 —— 回报路径原子性 + ACK 闭环的通过回归。"""

    def test_b1_duplicate_report_idempotent(self):
        """B1: 同成员同任务同结果重复回报 → 幂等跳过，compact 仅一次，report_id 稳定。"""
        self._setup_team()
        compact = []

        def fake_send(session, win, text, **kw):
            if "/compact" in text:
                compact.append(1)
            return 0, ""

        with self._mocks(send_keys=fake_send):
            r1 = mcp.member_report_result("team", "同一结果", member_name="alice")
            r2 = mcp.member_report_result("team", "同一结果", member_name="alice")

        pending = self._pending()
        self.assertEqual(len(pending), 1, "重复回报只保留 1 份 pending")
        self.assertEqual(len(compact), 1, "compact 仅发送一次")
        self.assertIn("幂等跳过", r2, "第二次回报提示幂等跳过")
        self.assertIn("已标记为完成", r1)
        # 全层幂等：重复回报连 results.jsonl 审计也不重复写（写日志在锁内、
        # 去重 return 之后，见 _append_report_entry）。
        results = self._results()
        self.assertEqual(len(results), 1, "重复回报 results.jsonl 仍只 1 条")
        # 事实状态：成员完成标记不因重复回报回退
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(member.get("last_task_completed"))

    def test_b2_leader_resting_persist_activate_acks(self):
        """B2: leader 休眠期间成员回报 → pending 持久化 + activate 消费（ACK 证据）。"""
        self._setup_team()
        team = self._team()
        team["leader_state"] = "resting"
        mcp._save({"teams": {"team": team}})

        with self._mocks(capture_side_effect=lambda *a, **k: (0, "❯\n⏸ manual mode on", ""),
                         leader_idle=True):
            r = mcp.member_report_result("team", "休眠期亲笔结果", member_name="alice")
        self.assertIn("已唤醒 leader 并注入本次回报", r)
        pending = self._pending()
        self.assertEqual(len(pending), 1, "回报在 pending（注入不消费）")
        self.assertIn("report_id", pending[0], "pending 条目含持久化 report_id")
        # leader_activate = 最终 ACK：消费并清空，落盘 ACK 证据
        act = mcp.leader_activate("team")
        self.assertIn("休眠期亲笔结果", act)
        self.assertEqual(len(self._pending()), 0, "activate 后 pending 清空")
        ack = self._team().get("leader_last_ack")
        self.assertIsNotNone(ack, "leader_last_ack 持久化 ACK 证据")
        self.assertEqual(ack["count"], 1)
        self.assertEqual(ack["report_ids"], [pending[0]["report_id"]],
                        "ACK report_ids 与 pending report_id 一致")

    def test_b3_leader_dead_report_persists(self):
        """B3: leader 终端死亡/session 断 → 回报仍持久化 pending，activate 可 ACK。"""
        self._setup_team()
        with mock.patch.object(mcp, "_find_any_session", return_value=None), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            mcp.member_report_result("team", "session死时回报", member_name="alice")
        pending = self._pending()
        self.assertEqual(len(pending), 1, "leader 死时回报仍持久化 pending")
        self.assertEqual(len(self._results()), 1)
        self.assertIn("report_id", pending[0])
        act = mcp.leader_activate("team")
        self.assertIn("session死时回报", act)
        self.assertEqual(self._team().get("leader_last_ack", {}).get("count"), 1)

    def test_b4_compact_and_reentry(self):
        """B4: 回报后 compact 一次；re-entry（member_get_my_task）看到任务已完成。"""
        self._setup_team()
        compact = []

        def fake_send(session, win, text, **kw):
            if "/compact" in text:
                compact.append(1)
            return 0, ""

        with self._mocks(send_keys=fake_send):
            mcp.member_report_result("team", "结果", member_name="alice")
            # 重复回报（同任务不同结果也视为同任务再报 → compact 不再发）
            mcp.member_report_result("team", "结果补充", member_name="alice")

        self.assertEqual(len(compact), 1, "/compact 只发一次")
        # 第二次回报是"不同结果"→ 新 pending 条目（同任务不同结果允许累积）
        self.assertEqual(len(self._pending()), 2, "不同结果各留 1 份")
        # re-entry：任务已完成，无需续跑
        got = mcp.member_get_my_task("team", "alice")
        self.assertIn("已完成", got, "re-entry 看到任务已完成")

    def test_b5_two_members_concurrent_reports(self):
        """B5: 两名成员回报 → 各自 pending 均保留，activate ACK 含双方 report_id。"""
        self._setup_team(members=("alice", "bob"))
        compact = []

        def fake_send(session, win, text, **kw):
            if "/compact" in text:
                compact.append(1)
            return 0, ""

        with self._mocks(send_keys=fake_send):
            mcp.member_report_result("team", "alice 结果", member_name="alice")
            mcp.member_report_result("team", "bob 结果", member_name="bob")

        pending = self._pending()
        self.assertEqual(len(pending), 2, "两成员回报各留 1 份")
        members_pending = sorted(r.get("member") for r in pending)
        self.assertEqual(members_pending, ["alice", "bob"])
        self.assertEqual(len(self._results()), 2, "results.jsonl 2 条")
        act = mcp.leader_activate("team")
        self.assertIn("alice 结果", act) and self.assertIn("bob 结果", act)
        ack = self._team().get("leader_last_ack", {})
        self.assertEqual(ack.get("count"), 2)
        self.assertEqual(sorted(ack.get("report_ids", [])),
                         sorted(r.get("report_id") for r in pending),
                         "ACK report_ids == 双方 report_id")

    def test_b5b_concurrent_finalize_no_pending_loss(self):
        """B5b: 并发回报时 _finalize_agent_completion 的 stale _save 不得清掉并发 pending。

        确定性复现（真实竞态，全量套件高争用下已实测丢 1 条 pending）：
        把线程 A 卡在 _finalize_agent_completion 的 load→save 窗口（/compact 发送点），
        期间线程 B 完成 append 落盘，再放行 A → A 的 stale 快照 _save 会整份覆写，
        若未修复则 B 的 pending 条目被清空。当前代码预期 FAIL（竞态 A 的收尾侧变体）。
        """
        self._setup_team(members=("alice", "bob"))
        gate = threading.Event()     # A 已进入 finalize 窗口
        release = threading.Event()  # 放行 A 完成 _save

        def blocking_send(session, win, text, **kw):
            if "/compact" in text and win == "alice":
                gate.set()
                release.wait(timeout=10)
            return 0, ""

        def run_alice():
            with self._mocks(send_keys=blocking_send):
                mcp.member_report_result("team", "alice 并发结果", member_name="alice")

        t = threading.Thread(target=run_alice)
        t.start()
        self.assertTrue(gate.wait(timeout=10), "A 应进入 finalize 窗口")
        # A 卡在 stale 快照期间，B 完成回报 append → pending=[alice, bob] 落盘
        with self._mocks(send_keys=lambda *a, **k: (0, "")):
            mcp.member_report_result("team", "bob 并发结果", member_name="bob")
        self.assertEqual(len(self._pending()), 2, "B 的回报已在 A 保存前 append")
        # 放行 A：其 finalize 用开头加载的 stale 快照 _save → 清掉 B 条目
        release.set()
        t.join(timeout=10)
        pending = self._pending()
        self.assertEqual(len(pending), 2,
                         "A 的 finalize stale _save 不得清掉 B 的 pending 条目")
        self.assertEqual(sorted(r.get("member") for r in pending), ["alice", "bob"])
        self.assertEqual(len(self._results()), 2, "results 2 条")
        act = mcp.leader_activate("team")
        self.assertIn("alice 并发结果", act) and self.assertIn("bob 并发结果", act)
        self.assertEqual(self._team().get("leader_last_ack", {}).get("count"), 2)

    def test_b6_authorization_stall_report(self):
        """B6: 授权卡点期间成员回报 → 完成标记 + pending 记录，不丢、不重复。"""
        self._setup_team()
        # 成员卡在授权（blocked_reason=approval），随后亲笔回报
        team = self._team()
        team["members"]["alice"]["blocked_reason"] = "approval"
        mcp._save({"teams": {"team": team}})
        with self._mocks():
            mcp.member_report_result("team", "授权后结果", member_name="alice")
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(member.get("last_task_completed"), "授权卡点后完成标记保留")
        self.assertEqual(len(self._pending()), 1)
        self.assertEqual(self._pending()[0]["event"], "member_report", "亲笔事件类型")
        self.assertEqual(len(self._results()), 1, "授权卡点不产生双份")


# ======================================================================
# C 组：持久化 report_id / pending 去重 / ACK 证据 / monitor 推断替换
# ======================================================================
class PersistenceEvidence(RaceRegressionBase):
    """C1-C3 —— 验收以持久化 report_id + pending + ACK 为事实。"""

    def test_c1_report_id_stable_and_in_evidence(self):
        """C1: report_id 在重复回报中稳定；pending/results/ACK 三处一致。"""
        self._setup_team()
        with self._mocks():
            mcp.member_report_result("team", "稳定结果", member_name="alice")
            mcp.member_report_result("team", "稳定结果", member_name="alice")  # 幂等跳过

        pending = self._pending()
        self.assertEqual(len(pending), 1)
        rid = pending[0]["report_id"]
        self.assertTrue(rid.startswith("alice:"), f"report_id 前缀绑定成员: {rid}")
        # 全层幂等：重复回报 results.jsonl 也只 1 条，且含与 pending 一致的 report_id。
        results = self._results()
        self.assertEqual(len(results), 1, "重复回报审计日志 1 条")
        self.assertEqual(results[0].get("report_id"), rid,
                         "results 条目含同一 report_id（持久化证据链）")
        # ACK 引用同一 report_id
        mcp.leader_activate("team")
        ack = self._team().get("leader_last_ack", {})
        self.assertEqual(ack["report_ids"], [rid], "ACK 引用同一 report_id")

    def test_c2_pending_dedup_by_report_id(self):
        """C2: append_leader_pending_report 对同 report_id 幂等（去重）。"""
        self._setup_team()
        # 直接构造两笔同 report_id 的 append → 只留 1 条
        entry = {"member": "alice", "event": "member_report", "result": "x",
                 "report_id": "alice:dup123", "timestamp": "2026-08-12T00:00:00"}
        with mcp.TEAM_DATA_LOCK:
            team = mcp._load()["teams"]["team"]
            mcp.append_leader_pending_report(team, dict(entry))
            mcp.append_leader_pending_report(team, dict(entry))
            mcp._save({"teams": {"team": team}})
        self.assertEqual(len(self._pending()), 1, "同 report_id 幂等去重")

    def test_c3_monitor_inferred_replaced_by_authoritative(self):
        """C3(领导回归断言): monitor 自动完成是推断，亲笔回报替换之，不得双报。"""
        self._setup_team()
        # 先模拟 monitor idle 自动完成（无亲笔回报）→ 推断回报入 pending
        with self._mocks(capture_side_effect=lambda *a, **k: (0, "❯\n⏸ manual mode on", "")):
            mcp._scan_member_terminal("team", "alice", mark_idle_done=True)
        before = [r.get("event") for r in self._pending()]
        self.assertIn(mcp.MONITOR_INFERRED_EVENT, before,
                      "monitor 推断事件入 pending（推断事实，非正式回报）")
        # 成员随后亲笔回报 → S2 替换同任务 monitor 推断
        with self._mocks():
            mcp.member_report_result("team", "亲笔权威结果", member_name="alice")
        after = [r.get("event") for r in self._pending()]
        self.assertEqual(after.count("member_report"), 1, "亲笔回报 1 份")
        self.assertEqual(after.count(mcp.MONITOR_INFERRED_EVENT), 0,
                         "monitor 推断被亲笔回报替换，不得残留双报")
        # 亲笔回报权威：结果含真实结论
        self.assertEqual(self._pending()[0].get("result").strip(), "亲笔权威结果")
        # leader ACK 只消费 1 条（亲笔）
        mcp.leader_activate("team")
        self.assertEqual(self._team().get("leader_last_ack", {}).get("count"), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
