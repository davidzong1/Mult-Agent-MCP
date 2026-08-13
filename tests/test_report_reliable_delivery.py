"""
P1 成员回报可靠交付 / leader 可见性 — 竞态复现与修复回归（coder-claude）
========================================================================

背景缺陷（竞态）: 成员已调用 member_report_result、回报已持久化，但 Codex
leader 仍可能"误判未发送"。根因分四块（本文件逐块复现 + 修复后回归）：

  A. **原子性缺口**：member_report_result 先把 `last_task_completed=True /
     last_observed_state="idle"` 落盘（_save），之后才 append results.jsonl +
     leader_pending_reports。两阶段写非原子 → 存在"已完成但无回报"持久窗口：
     回报持久化失败（或进程中断）时成员仍被标记完成，leader 只见 done 不见
     回报 → 误判未发送。
  B. **可见性缺口**：leader_check_member_status 只暴露 last_task_completed /
     last_observed_state，**不暴露"是否已收到回报"** → Codex leader 便宜的
     数据层轮询无法区分"已完成且已回报"与"已完成但未回报"，只能依赖终端残留
     或额外调用，是"UI/终端残留影响事实状态"的通道。
  C. **幂等缺口**：重复 member_report_result 无条件重复 append pending → 同一
     回报被重复投递/重复提醒。
  D. **消费可见性缺口**：leader_monitor_members 汇总不提示待处理回报数 → Codex
     leader 巡检后不知道要 leader_activate 消费 → 漏消费。

修复不变量（本文件钉住）:
  1. 回报事实来源 = 持久化队列/结果（results.jsonl + leader_pending_reports），
     而非成员对话窗/终端残留；
  2. 写入（pending append）与待处理标记（last_task_completed / last_report_*）
     原子 —— 同一 _update_team_data 锁内完成；回报持久化失败绝不标记完成；
  3. 唤醒失败可恢复（冷却/死 leader 的回报留在 pending，activate/巡检兜底补投）；
  4. leader activate/monitor 不漏消费；ACK（leader_activate）幂等且不误清
     并发新到的未消费报告（TOCTOU 由 _update_team_data 锁内读+清保证）；
  5. 重复回报幂等去重（同成员+同任务+同结果只 append 一次）。

隔离：临时目录重定向 mcp 全局路径 + data_layer.set_data_file；_find_any_session
mock 为 None（不触真实 tmux）；terminals_active=False（_maybe_revive_leader
短路）。绝不触碰真实 ~/.claude / 真实 teams_data.json。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer


class _IsolatedReportRace(unittest.TestCase):
    """隔离团队数据 + tmux 全 mock（镜像 test_completion_compact / 旧隔离模式）。"""

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
        }
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD",
                "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR",
            )
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

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
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for name, val in self.old_globals.items():
            setattr(mcp, name, val)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _setup_team(self, *, terminals_active=False, alice_task=True,
                    alice_completed=False, seed_reports=None,
                    leader_type="tmux"):
        """team: lead(leader) + alice(coder)。terminals_active 默认 False 使
        _maybe_revive_leader 短路；monitor 测试单独传 True。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        alice = {"role": "coder", "agent": "claude"}
        if alice_task:
            alice["last_task"] = "完成登录模块"
            alice["last_context"] = "需要实现OAuth登录"
            alice["last_task_completed"] = alice_completed
            alice["last_observed_state"] = "working" if not alice_completed else "idle"
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": terminals_active,
            "leader": "lead",
            "leader_type": leader_type,
            "leader_state": "active",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": alice,
            },
        }
        if seed_reports is not None:
            team["leader_pending_reports"] = list(seed_reports)
        mcp._save({"teams": {"team": team}})
        return workspace, context

    def _alice(self):
        return mcp._load()["teams"]["team"]["members"]["alice"]

    def _pending(self):
        return mcp._load()["teams"]["team"].get("leader_pending_reports") or []

    def _report(self, result="完成登录模块", **kw):
        """无终端环境调用 member_report_result（session=None → 不死活/不发compact）。"""
        with mock.patch.object(mcp, "_find_any_session", return_value=None):
            return mcp.member_report_result("team", result, member_name="alice", **kw)

    def _results_text(self, context):
        path = context / "results.jsonl"
        return path.read_text(encoding="utf-8") if path.exists() else ""


# =====================================================================
# A. 原子性：回报持久化失败时，成员绝不能被标记完成
#    （旧实现 SAVE#1 先标记完成再写回报 → 存在"done 但无回报"持久窗口）
# =====================================================================
class AtomicityTests(_IsolatedReportRace):

    def _update_first_call_raises(self):
        """mock _update_team_data：第一次调用（pending append）抛异常，后续正常。
        member_report_result 内 _record_report_and_notify_leader 的 append 是首个
        _update_team_data 调用，其后 _finalize_member_state 的调用需放行。"""
        real = mcp._update_team_data
        state = {"n": 0}

        def flaky(*a, **kw):
            state["n"] += 1
            if state["n"] == 1:
                raise RuntimeError("pending append failed")
            return real(*a, **kw)

        return mock.patch.object(mcp, "_update_team_data", side_effect=flaky)

    def test_report_failure_does_not_mark_member_done(self):
        """复现：pending append 抛异常时，成员不得被标成 completed/idle。
        修复前：SAVE#1 已把 last_task_completed=True / idle 落盘 → 误判根因。"""
        self._setup_team(alice_task=True, alice_completed=False)
        with self._update_first_call_raises():
            self._report()
        member = self._alice()
        self.assertFalse(member.get("last_task_completed"),
                         "回报持久化失败仍标记完成 → 'done 但无回报'竞态")
        self.assertNotEqual(member.get("last_observed_state"), "idle",
                            "回报失败仍置 idle → leader 误判已完成")

    def test_report_failure_leaves_no_half_state(self):
        """失败闭门：pending append 失败时，results.jsonl 与 pending 均无该回报，
        成员保持"进行中"（可重试）——绝不出现"已完成但无报告"的半状态。"""
        workspace, context = self._setup_team(alice_task=True)
        with self._update_first_call_raises():
            self._report()
        self.assertNotIn("完成登录模块", self._results_text(context),
                         "回报未持久化时 results.jsonl 不应出现误导性记录")
        self.assertEqual(self._pending(), [], "回报未持久化时 pending 应为空")
        self.assertFalse(self._alice().get("last_task_completed"),
                         "回报未持久化不得标记完成")


# =====================================================================
# B. 可见性：leader_check_member_status 必须暴露"已回报"信号
# =====================================================================
class VisibilityTests(_IsolatedReportRace):

    def test_check_status_surfaces_report_receipt(self):
        """复现：回报成功后，leader_check_member_status 应能直接从数据层看到
        '已回报'（含时间/摘要），而不是依赖成员对话窗/终端残留。"""
        self._setup_team(alice_task=True)
        self._report()
        out = mcp.leader_check_member_status("team", "alice")
        self.assertIn("已回报", out, "数据层轮询须暴露回报接收事实")
        self.assertIn("完成登录模块", out)

    def test_check_status_shows_not_reported_before_report(self):
        """守卫：未回报前应明确提示未收到回报（而非静默），leader 不会误判已发送。"""
        self._setup_team(alice_task=True, alice_completed=False)
        out = mcp.leader_check_member_status("team", "alice")
        self.assertIn("未收到回报", out)

    def test_check_status_no_negative_when_done_and_reported(self):
        """修复后正常路径：已完成且已回报 → 显示已回报，不带 '未收到回报' 误导。"""
        self._setup_team(alice_task=True)
        self._report()
        out = mcp.leader_check_member_status("team", "alice")
        self.assertIn("已回报", out)
        self.assertNotIn("未收到回报", out)


# =====================================================================
# C. 幂等：同成员+同任务+同结果 重复回报只 append 一次
# =====================================================================
class IdempotencyTests(_IsolatedReportRace):

    def test_duplicate_report_appends_once(self):
        """复现：两次相同回报 → pending 仅 1 条（修复前重复 append → leader 重复提醒）。"""
        self._setup_team(alice_task=True)
        self._report()
        self._report()
        pending = self._pending()
        same = [p for p in pending if p.get("member") == "alice"]
        self.assertEqual(len(same), 1, f"重复回报应幂等，实际 {len(same)} 条")

    def test_distinct_reports_both_recorded(self):
        """守卫：不同任务/不同内容的回报不得被幂等误吞。"""
        self._setup_team(alice_task=True)
        self._report("第一期完成")
        self._report("第二期完成（增量）")
        pending = self._pending()
        same = [p for p in pending if p.get("member") == "alice"]
        self.assertEqual(len(same), 2, "不同内容的回报都应记录")


# =====================================================================
# D. 消费可见性：leader_monitor_members 需提示待处理回报 → leader 知道 activate
# =====================================================================
class MonitorConsumptionTests(_IsolatedReportRace):

    def _monitor(self):
        def fake_tmux(cmd, timeout=10):
            return (0, "", "")
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value="@7"):
                with mock.patch.object(mcp, "_capture_window",
                                       return_value=(0, "❯\n⏸ manual mode on", "")):
                    with mock.patch.object(mcp, "_codex_session_backfill", return_value=None):
                        with mock.patch.object(mcp, "_maybe_revive_leader", return_value=(False, "")):
                            with mock.patch.object(mcp, "_reclaim_member_draining_windows", return_value=0):
                                with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                                    return mcp.leader_monitor_members("team")

    def test_monitor_surfaces_pending_count_for_activate(self):
        """复现：存在待处理回报时，leader_monitor_members 汇总应提示待处理条数，
        Codex leader 巡检后知道该调用 leader_activate 消费（不漏消费）。"""
        self._setup_team(alice_task=True, alice_completed=True,
                         seed_reports=[{"member": "alice", "event": "member_report",
                                        "result": "完成登录模块",
                                        "timestamp": "2026-08-11T10:00:00"}],
                         terminals_active=True)
        out = self._monitor()
        self.assertIn("待处理", out, "monitor 汇总须提示待处理回报，否则 leader 漏消费")
        self.assertIn("leader_activate", out)


# =====================================================================
# E. 持久化 / ACK / leader 离线重启（守卫，修复不得回退）
# =====================================================================
class PersistenceAckTests(_IsolatedReportRace):

    def test_report_persists_to_results_and_pending(self):
        """回报落盘双源：results.jsonl + leader_pending_reports（leader 离线也能收到）。"""
        _, context = self._setup_team(alice_task=True)
        self._report()
        self.assertIn("完成登录模块", self._results_text(context))
        self.assertEqual(len(self._pending()), 1)

    def test_activate_acks_and_does_not_clear_concurrent(self):
        """leader_activate 原子消费：drain 后 pending 清空（ACK 幂等，二次调用为空）；"""
        self._setup_team(alice_task=True, alice_completed=True,
                         seed_reports=[{"member": "alice", "event": "member_report",
                                        "result": "完成登录模块",
                                        "timestamp": "2026-08-11T10:00:00"}])
        out1 = mcp.leader_activate("team")
        self.assertIn("完成登录模块", out1)
        self.assertEqual(self._pending(), [], "activate 应 ACK 清空")
        out2 = mcp.leader_activate("team")
        self.assertIn("没有待处理的成员回报", out2)

    def test_report_survives_leader_offline_and_restart(self):
        """leader 离线/休眠/重启场景：回报先进 pending，leader 重新进入后 activate 可见；
        results.jsonl 是独立事实源，activate 只清 pending 不清日志。"""
        _, context = self._setup_team(alice_task=True)
        self._report()                      # session=None → leader 离线，只入 pending
        self.assertEqual(len(self._pending()), 1)
        # 模拟 leader 重启：同一数据文件，leader_activate 消费
        out = mcp.leader_activate("team")
        self.assertIn("完成登录模块", out)
        self.assertIn("完成登录模块", self._results_text(context),  # 日志保留
                      "activate 不应删除 results.jsonl（事实源）")

    def test_report_entry_carries_persistent_report_id(self):
        """S2/S4：结果日志与 pending 均带持久化 report_id（同 id），交付/ACK 引用它；
        验收以此 id 为证据，不依赖 idle/terminal classifier 判完成。"""
        _, context = self._setup_team(alice_task=True)
        self._report()
        results = [json.loads(l) for l in self._results_text(context).splitlines() if l.strip()]
        last = results[-1]
        rid = last.get("report_id")
        self.assertTrue(rid and "alice" in rid, f"results.jsonl 应带 report_id: {last}")
        pending = self._pending()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].get("report_id"), rid,
                         "pending 与 results.jsonl 的 report_id 应一致")
        self.assertIs(pending[0].get("delivered"), False,
                      "未投递时 delivered 默认 False（S3）")
        self.assertEqual(self._alice().get("last_report_id"), rid,
                         "成员记录应记 last_report_id（证据链）")

    def test_activate_records_ack_evidence_with_report_ids(self):
        """S3/S4：leader_activate 消费即 ACK，落盘 leader_last_ack（含 report_id 清单），
        提供持久化 ACK 证据——验收不再以成员对话窗/idle 判确认。"""
        self._setup_team(alice_task=True)
        self._report()
        rid = self._pending()[0].get("report_id")
        mcp.leader_activate("team")
        team = mcp._load()["teams"]["team"]
        ack = team.get("leader_last_ack") or {}
        self.assertEqual(ack.get("count"), 1, "ACK 应记录消费条数")
        self.assertIn(rid, ack.get("report_ids") or [], "ACK 证据应含 report_id")
        self.assertTrue(ack.get("ts"), "ACK 应带时间戳")


if __name__ == "__main__":
    unittest.main()
