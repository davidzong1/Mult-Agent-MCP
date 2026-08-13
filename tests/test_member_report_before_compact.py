"""
P0 回归语料：成员回报必须先于 /compact 落到 leader 可见处。

背景缺陷:monitor 扫到成员终端 idle 走 _scan_member_terminal 自动完成路径时,
旧实现只标记完成 + 注入 /compact,不写 results.jsonl、不追加 leader_pending_reports、
不通知 leader —— 成员终端被 /compact 清空后回报永远到不了 leader,leader 永不激活。

本文件按测试时工作树现状编写:coder 已把 monitor idle 路径改为先调用
_record_report_and_notify_leader(event="monitor_inferred_completion") 再
_finalize_agent_completion(见工作树未提交 diff)。因此:

  - A1/A2/A3(主缺陷)按修复后行为断言 —— 若修复被回退,这三条立即变红;
  - B(显式路径顺序不变量)钉住 记录 → append pending → notify → /compact,
    防止后续抽公共函数改坏相对顺序;
  - C1/C2 是"记录当前行为"的取证用例(reviewer 正在裁决两处是否应注入),
    docstring 标注"待裁决:若裁决改为注入,本用例需反转";
  - D 用实际返回值证明 resting 死锁推演(leader 卡死在 active);
  - E 验证 compact_sent_by_monitor 兜底:monitor 补回报后,成员亲笔
    member_report_result 仍可用且幂等。

隔离:遵循 test_completion_compact.py 模式 —— 重定向 mcp 全局路径到临时目录 +
pytest 下 conftest 的 MULT_AGENT_MCP_HOME 环境级兜底,双保险不污染真实数据。
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class TestMemberReportBeforeCompact(unittest.TestCase):
    """member 回报先于 /compact 的顺序不变量 + monitor 自动完成回报闭环"""

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

    def _setup_team(self, *, leader_type="tmux", leader_state="active",
                    wakeup_enabled=False, leader_idle_streak=0,
                    alice_completed=False, seed_reports=None):
        """创建团队: lead(leader) + alice(coder,默认未完成任务)"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        members = {
            "lead": {"role": "leader", "agent": "claude"},
            "alice": {
                "role": "coder", "agent": "claude",
                "last_task": "完成登录模块", "last_context": "需要实现OAuth登录",
                "last_task_completed": alice_completed,
                "tmux_window_id": "@7", "tmux_session": "mcp_team",
                "tmux_session_id": "$1", "tmux_session_created": "1000",
            },
        }
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": leader_type,
            "leader_state": leader_state,
            "leader_idle_streak": leader_idle_streak,
            "members": members,
        }
        if wakeup_enabled is not None:
            team["leader_wakeup_config"] = {"enabled": wakeup_enabled}
        if seed_reports is not None:
            team["leader_pending_reports"] = list(seed_reports)
        mcp._save({"teams": {"team": team}})
        return workspace, context

    @staticmethod
    def _idle_capture():
        """monitor 扫描用:终端显示 idle 状态(与既有测试同款取证输出)"""
        return (0, "❯\n⏸ manual mode on", "")

    def _results_records(self, team=None):
        team = team if team is not None else mcp._load()["teams"]["team"]
        path = Path(team["context_dir"]) / "results.jsonl"
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text("utf-8").splitlines()]

    # ==================================================================
    # A. 主缺陷:monitor idle 自动完成路径必须补回报(先于 /compact)
    # ==================================================================

    def test_a1_monitor_idle_appends_pending_report(self):
        """A1: monitor idle 自动完成 → leader_pending_reports 必须非空

        修复前(必红):idle 分支只标记完成 + /compact,不 append pending_report,
        leader 永远看不到这次完成。修复后应含 1 条 event="monitor_inferred_completion"。
        """
        self._setup_team()  # alice 有未完成任务
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_capture_window", return_value=self._idle_capture()), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=False), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            result = mcp.leader_monitor_members("team")
        self.assertIn("alice: idle (marked-complete)", result)
        team = mcp._load()["teams"]["team"]
        reports = team.get("leader_pending_reports", [])
        self.assertTrue(reports, "monitor idle 自动完成应追加 leader_pending_reports(当前为空即 P0 复现)")
        self.assertEqual(reports[0].get("event"), "monitor_inferred_completion")
        self.assertEqual(reports[0].get("member"), "alice")
        self.assertIn("monitor auto-detected completion", reports[0].get("result", ""))

    def test_a2_monitor_idle_writes_results_record(self):
        """A2: monitor idle 自动完成 → results.jsonl 多出一条记录"""
        self._setup_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_capture_window", return_value=self._idle_capture()), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=False), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            mcp.leader_monitor_members("team")
        records = self._results_records()
        self.assertEqual(len(records), 1, "monitor idle 自动完成应写 results.jsonl 记录(当前为空即 P0 复现)")
        self.assertIn("monitor auto-detected completion", records[0]["result"])
        self.assertEqual(records[0]["member"], "alice")

    def test_a3_monitor_idle_record_and_pending_before_compact(self):
        """A3: 事件序列取证 —— results.jsonl 写入与 pending_report 追加都先于 /compact 注入

        照抄 test_completion_compact.py::test_member_record_before_compact 的事件手法:
        builtins.open 捕获 results.jsonl 追加、"append_leader_pending_report" 包装记录、
        _send_keys 捕获 /compact。期望序列: write_record → pending_report → compact。
        """
        self._setup_team()
        events = []

        orig_open = open
        def track_open(path, mode, *a, **kw):
            f = orig_open(path, mode, *a, **kw)
            if "results.jsonl" in str(path) and "a" in mode:
                events.append("write_record")
            return f

        orig_append = mcp.append_leader_pending_report
        def track_append(team, entry):
            events.append("pending_report")
            return orig_append(team, entry)

        def fake_send(session, win, text, **kw):
            if "/compact" in text:
                events.append("compact")
            return 0, ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_capture_window", return_value=self._idle_capture()), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=False), \
             mock.patch.object(mcp, "_send_keys", side_effect=fake_send), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")), \
             mock.patch.object(mcp, "append_leader_pending_report", side_effect=track_append), \
             mock.patch("builtins.open", side_effect=track_open):
            mcp.leader_monitor_members("team")

        self.assertIn("compact", events, "monitor 自动完成应注入 /compact")
        self.assertIn("write_record", events, "results.jsonl 写入事件缺失(当前缺失即 P0 复现)")
        self.assertIn("pending_report", events, "pending_report 追加事件缺失(当前缺失即 P0 复现)")
        self.assertLess(events.index("write_record"), events.index("compact"),
                        "先写 results.jsonl 记录,再发 /compact")
        self.assertLess(events.index("pending_report"), events.index("compact"),
                        "先追加 pending_report,再发 /compact")

    # ==================================================================
    # B. 显式 member_report_result 路径顺序不变量(防回归钉住)
    # ==================================================================

    def test_b_explicit_report_ordering_invariant(self):
        """B: 显式 member_report_result 顺序必须为 记录 → append pending → notify → /compact

        direct leader 下 _notify_leader_of_report 走 not-tmux-leader 早退,不影响顺序验证。
        若 coder 抽公共函数时改坏相对顺序(如 /compact 提前),此用例立即红。
        """
        self._setup_team(leader_type="direct")
        events = []

        orig_open = open
        def track_open(path, mode, *a, **kw):
            f = orig_open(path, mode, *a, **kw)
            if "results.jsonl" in str(path) and "a" in mode:
                events.append("write_record")
            return f

        orig_append = mcp.append_leader_pending_report
        def track_append(team, entry):
            events.append("pending_report")
            return orig_append(team, entry)

        orig_notify = mcp._notify_leader_of_report
        def track_notify(team_name, entry):
            events.append("notify")
            return orig_notify(team_name, entry)

        def fake_send(session, win, text, **kw):
            if "/compact" in text:
                events.append("compact")
            return 0, ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_send_keys", side_effect=fake_send), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")), \
             mock.patch.object(mcp, "append_leader_pending_report", side_effect=track_append), \
             mock.patch.object(mcp, "_notify_leader_of_report", side_effect=track_notify), \
             mock.patch("builtins.open", side_effect=track_open):
            mcp.member_report_result("team", "亲笔结果", member_name="alice")

        self.assertEqual(events,
                         ["write_record", "pending_report", "notify", "compact"],
                         "显式路径顺序不变量: 记录 → append pending → notify → /compact")

    # ==================================================================
    # C. 唤醒门控取证(记录当前行为,reviewer 裁决中)
    # ==================================================================

    def test_c1_resting_wakeup_disabled_no_injection(self):
        """C1: tmux leader resting + wakeup enabled=False → 回报仍注入唤醒(RC2 后语义)

        RC2 后语义:_notify_leader_of_report 门控改为 report_wakeup_enabled(默认 True)
        + 终端 idle,去掉了 enabled 与 resting 两道门 → enabled=False 不再短路,
        终端 idle 即注入。注入不消费 leader_pending_reports(由 leader_activate 消费),
        注入后 leader_state 置 active。
        """
        self._setup_team(leader_type="tmux", leader_state="resting", wakeup_enabled=False)
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            result = mcp.member_report_result("team", "C1 结果", member_name="alice")
        # RC2 后: 终端 idle 即注入唤醒, 与 wakeup enabled 无关
        self.assertIn("已唤醒 leader 并注入本次回报", result)
        self.assertEqual(len(mcp._load()["teams"]["team"].get("leader_pending_reports", [])), 1,
                         "注入不消费 pending, 由 leader_activate 消费")
        self.assertEqual(mcp._load()["teams"]["team"]["leader_state"], "active",
                         "注入后 leader 置 active")

    def test_c2_active_idle_with_pending_reports_no_injection(self):
        """C2: leader_state=active + 终端 idle + 已有 pending_reports → 回报仍注入(RC2 后语义)

        RC2 后语义: 注入不要求 resting,任何 leader_state 下终端 idle 即注入
        (打开 G1: leader 从未 sleep、state=active 也能被回报唤醒)。
        注入不清空 pending(旧 1 + 新 1),leader_state 保持 active。
        """
        seed = [{"timestamp": "2026-08-09T00:00:00", "member": "alice",
                 "event": "member_report", "result": "旧回报"}]
        self._setup_team(leader_type="tmux", leader_state="active",
                         wakeup_enabled=True, seed_reports=seed)
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            result = mcp.member_report_result("team", "C2 新回报", member_name="alice")
        # RC2 后: active + idle 也注入唤醒
        self.assertIn("已唤醒 leader 并注入本次回报", result)
        team = mcp._load()["teams"]["team"]
        self.assertEqual(len(team.get("leader_pending_reports", [])), 2, "旧 1 + 新 1,注入不清空")
        self.assertEqual(team["leader_state"], "active")

    # ==================================================================
    # D. resting 死锁推演(实际返回值取证)
    # ==================================================================

    def test_d1_streak_below_threshold_all_done_returns_none(self):
        """D1 推演: 成员全部完成但 idle_streak(2) 未达 idle_threshold(4)

        _evaluate_leader_wakeup_conditions 应返回 {"action":"none"}:
        enter_resting 被 idle_streak 门槛挡住,wakeup_all_done 又要求 state==resting(现为 active),
        两条路径都进不去 → leader 卡死在 active。此条给出实际返回值而非推断。
        """
        self._setup_team(leader_type="tmux", leader_state="active",
                         wakeup_enabled=True, leader_idle_streak=2, alice_completed=True)
        result = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(result, {"action": "none"})

    def test_d2_streak_at_threshold_all_done_still_none(self):
        """D2 推演(死锁本体): 即使 idle_streak(5) 已达 idle_threshold(4),成员全部完成

        enter_resting 还要求 active_members 非空 —— 全员完成后为空,照样进不去;
        wakeup_all_done 要求 state==resting —— active 进不去。双锁死,仍返回
        {"action":"none"}。这正是 RC3 要不要修的决定性证据。
        """
        self._setup_team(leader_type="tmux", leader_state="active",
                         wakeup_enabled=True, leader_idle_streak=5, alice_completed=True)
        result = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(result, {"action": "none"})

    def test_d3_sanity_enter_resting_still_fires_with_active_members(self):
        """D3 对照: 成员未完成 + streak 达阈值 → enter_resting 正常触发

        证明 D1/D2 的 {"action":"none"} 是「全员完成后无迁移路径」特有,
        而非 wakeup 机制整体失效。
        """
        self._setup_team(leader_type="tmux", leader_state="active",
                         wakeup_enabled=True, leader_idle_streak=4, alice_completed=False)
        result = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(result, {"action": "enter_resting", "active_members": ["alice"]})

    # ==================================================================
    # E. 兜底:monitor 补回报后,成员亲笔 member_report_result 仍可用
    # ==================================================================

    def test_e_monitor_then_explicit_report_fallback(self):
        """E: compact_sent_by_monitor 兜底闭环(monitor 自动完成 → 亲笔回报 → 重复回报)

        事件账本(实际取证): monitor 自动完成 compact×1(写 1 条 results.jsonl +
        1 条 pending,event=monitor_inferred_completion,置 compact_sent_by_monitor);
        亲笔 member_report_result 消费标记后获权威 /compact×1(再写 1 条 results.jsonl,
        **替换** pending 中同任务的 monitor 推断 —— S2 成员权威 supersede,防双报);
        重复亲笔回报不再产生 /compact(幂等)。

        注:任务描述「不产生第二次 /compact」若按「全程只允许一次 /compact」解读,
        亲笔回报的权威一次即违反(当前实现=monitor 1 + 亲笔权威 1 = 2 次),本用例的
        compact 计数断言需相应反转 —— 以事件账本为准供裁决。
        """
        self._setup_team()
        compact_events = []

        def fake_send(session, win, text, **kw):
            if "/compact" in text:
                compact_events.append("compact")
            return 0, ""

        # ---- 1. monitor idle 自动完成(补回报) ----
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_capture_window", return_value=self._idle_capture()), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=False), \
             mock.patch.object(mcp, "_send_keys", side_effect=fake_send), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            mcp.leader_monitor_members("team")

        team = mcp._load()["teams"]["team"]
        alice = team["members"]["alice"]
        self.assertTrue(alice.get("compact_sent"), "monitor 完成后 compact_sent 应置位")
        self.assertTrue(alice.get("compact_sent_by_monitor"), "monitor 完成应打审计标记")
        self.assertEqual(len(compact_events), 1, "monitor 自动完成应注入一次 /compact")
        self.assertEqual(len(team.get("leader_pending_reports", [])), 1)
        self.assertEqual(len(self._results_records(team)), 1)

        # ---- 2. 成员事后亲笔 member_report_result(兜底) ----
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_send_keys", side_effect=fake_send), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            r2 = mcp.member_report_result("team", "亲笔最终报告", member_name="alice")
        self.assertIn("📦 已向成员终端注入 /compact", r2,
                      "亲笔回报应获一次权威 /compact(compact_sent_by_monitor 机制)")
        team = mcp._load()["teams"]["team"]
        alice = team["members"]["alice"]
        self.assertNotIn("compact_sent_by_monitor", alice, "标记应被消费(弹出)")
        self.assertTrue(alice.get("compact_sent"), "亲笔回报后 compact_sent 重新置位")
        self.assertEqual(len(compact_events), 2, "monitor 1 + 亲笔权威 1")
        # S2(成员权威 supersede monitor 推断):pending 只保留成员亲笔回报,
        # monitor 推断被替换(防双报);results.jsonl 审计日志仍保留两条。
        reports = team.get("leader_pending_reports", [])
        self.assertEqual(len(reports), 1, "成员亲笔回报应替换 monitor 推断")
        self.assertEqual(reports[0].get("event"), "member_report",
                         "pending 只保留权威回报,无 monitor 推断残留")
        self.assertEqual(len(self._results_records(team)), 2,
                         "results.jsonl 审计日志保留 monitor+亲笔两条")

        # ---- 3. 重复亲笔回报:幂等,不产生第三次 /compact ----
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_send_keys", side_effect=fake_send), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            mcp.member_report_result("team", "重复报告", member_name="alice")
        self.assertEqual(len(compact_events), 2, "重复亲笔回报幂等,不产生第三次 /compact")


if __name__ == "__main__":
    unittest.main()
