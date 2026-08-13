"""RC2 测试:成员回报自动激活静默 leader(_notify_leader_of_report 注入门控)。

规格:rc2-spec.md(改动 1-4)。注入门由
    cfg["enabled"] and leader_state=="resting" and idle
改为
    cfg["report_wakeup_enabled"] and idle
并补 §5-2:注入成功置 active 时同原子 pop("leader_sleep_until")。

覆盖:
  V1 默认配置 + tmux leader + active + idle → 注入成功(G1 场景闭环)
  V2 终端 busy → 不注入(误注入防护)
  V3 report_wakeup_enabled=False(逃生阀) → 不注入
  V4 注入成功路径 leader_sleep_until 被 pop → 下轮 _evaluate 不误报 wakeup_timeout
  V6 旧团队数据(无该键) → 默认 True 生效
  K1 direct leader → 不注入,走 pending_reports(reason=not-tmux-leader)
  K2 leader 终端已死 → 不注入(reason=leader-dead)

隔离模式与 test_leader_sleep.py / test_leader_sleep_gap_probe.py 一致:
unittest+mock 全量覆写模块全局路径,不写真实 ~/.mult_agent_mcp/。
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class LeaderWakeupInjectionTests(unittest.TestCase):
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
            "members": {
                "lead": {"role": "leader", "agent": "claude",
                         "tmux_window_id": "@1", "tmux_session": "team_sess"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    def _report_entry(self):
        return {
            "member": "alice", "result": "完成登录模块", "timestamp": "2026-08-09T12:00:00",
            "compressed_context_path": "", "artifact_path": "",
        }

    def _inject_mocks(self, terminal_idle=True, window_dead=False):
        sent = []

        def fake_send(session, target, message, **kw):
            sent.append(message)
            return 0, ""

        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=window_dead),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=terminal_idle),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_send_context_to_member", side_effect=fake_send),
            mock.patch.object(mcp, "_target_is_claude_tmux_leader", return_value=False),
        ]
        return mocks, sent

    def _run_notify(self, mocks, entry=None):
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", entry or self._report_entry())
        finally:
            for m in reversed(mocks):
                m.stop()
        return result

    # ------------------------------------------------------------------
    # V1: G1 场景闭环 — active + idle → 注入成功
    # ------------------------------------------------------------------

    def test_v1_active_idle_injects(self):
        """默认配置 + tmux leader + active + idle + 有回报 → 注入成功(G1 闭环)"""
        self._team(
            leader_idle_streak=3,
            leader_wakeup_config={"enabled": False, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )  # enabled=False 也应注入 —— 门不再是 enabled
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"V1 应注入,got {result}")
        self.assertEqual(len(sent), 1, "V1 应注入 1 条")
        self.assertIn("Leader activation: a member reported a result.", sent[0])
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(t["leader_idle_streak"], 0)
        self.assertEqual(t["leader_wakeup_reason"], "report")

    # ------------------------------------------------------------------
    # V2: 终端 busy → 不注入
    # ------------------------------------------------------------------

    def test_v2_busy_no_inject(self):
        """leader 终端 busy(mid-tool)+ 任何 leader_state → 不注入"""
        self._team(
            leader_state="resting",
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        mocks, sent = self._inject_mocks(terminal_idle=False)
        result = self._run_notify(mocks)
        self.assertFalse(result["injected"])
        self.assertEqual(result.get("reason"), "leader-live")
        self.assertEqual(sent, [], "V2 不应注入任何消息")

    # ------------------------------------------------------------------
    # V3: 逃生阀 report_wakeup_enabled=False → 不注入
    # ------------------------------------------------------------------

    def test_v3_escape_valve_disables_injection(self):
        """report_wakeup_enabled=False + active + idle → 不注入(逃生阀生效)"""
        self._team(
            leader_state="active",
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10,
                                  "report_wakeup_enabled": False},
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertFalse(result["injected"])
        self.assertEqual(result.get("reason"), "leader-live")
        self.assertEqual(sent, [], "V3 逃生阀关闭,不应注入")

    # ------------------------------------------------------------------
    # V4: §5-2 — 注入成功路径 pop leader_sleep_until,不误报 wakeup_timeout
    # ------------------------------------------------------------------

    def test_v4_inject_pops_sleep_until(self):
        """注入成功路径 leader_sleep_until 被 pop → 下轮 _evaluate 不误报 wakeup_timeout"""
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() + timedelta(seconds=300)).isoformat(),
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"])
        t = mcp._load()["teams"]["team"]
        self.assertNotIn("leader_sleep_until", t, "注入唤醒后必须清掉 sleep_until(§5-2)")
        # 下轮评估:无残留时间戳 → 不误报 wakeup_timeout
        action = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(action["action"], "none")

    # ------------------------------------------------------------------
    # V6: 旧团队数据(无键)→ 默认 True
    # ------------------------------------------------------------------

    def test_v6_legacy_data_defaults_true(self):
        """旧团队数据(无 report_wakeup_enabled 键)+ active + idle → 注入成功(默认 True)"""
        self._team(
            leader_state="active",
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )  # 无 report_wakeup_enabled 键
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"V6 旧数据应默认注入,got {result}")
        self.assertEqual(len(sent), 1)

    def test_v6_legacy_no_config_key_at_all(self):
        """完全没有 leader_wakeup_config 键(最旧数据)→ 默认 True 生效"""
        self._team(leader_state="active")  # 无 leader_wakeup_config
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"V6 无配置键应默认注入,got {result}")

    # ------------------------------------------------------------------
    # K1: direct leader → 不注入,走 pending_reports
    # ------------------------------------------------------------------

    def test_k1_direct_leader_no_inject(self):
        """direct leader → 不注入,reason=not-tmux-leader(分支在 cfg 之前,不动)"""
        self._team(leader_type="direct")
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertFalse(result["injected"])
        self.assertEqual(result.get("reason"), "not-tmux-leader")
        self.assertEqual(sent, [], "K1 不应注入任何消息")

    # ------------------------------------------------------------------
    # K2: leader 终端已死 → 不注入
    # ------------------------------------------------------------------

    def test_k2_dead_window_no_inject(self):
        """leader 终端已死 → 不注入,reason=leader-dead(不抢 revival 闭环)"""
        self._team(
            leader_state="active",
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        mocks, sent = self._inject_mocks(terminal_idle=True, window_dead=True)
        result = self._run_notify(mocks)
        self.assertFalse(result["injected"])
        self.assertEqual(result.get("reason"), "leader-dead")
        self.assertEqual(sent, [], "K2 不应注入任何消息")


    # ------------------------------------------------------------------
    # C1-C5: 回报注入冷却(RC2 收口)
    #   冷却时间戳复用 leader_last_wakeup_ts(:1696 注入成功后写),不新增字段;
    #   被冷却跳过的回报仍在 leader_pending_reports(append 先于 notify)。
    # ------------------------------------------------------------------

    def test_c1_first_report_injects(self):
        """C1: 首次回报(无 leader_last_wakeup_ts)→ 注入成功并记录时间戳"""
        self._team(leader_state="active")
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"C1 首次应注入,got {result}")
        self.assertEqual(len(sent), 1)
        t = mcp._load()["teams"]["team"]
        self.assertIn("leader_last_wakeup_ts", t, "注入成功后应记录冷却时间戳")

    def test_c2_second_report_within_cooldown_skips(self):
        """C2: 紧接第一条注入的第二条回报(冷却期内)→ 不注入,reason=report-cooldown"""
        self._team(leader_state="active")
        mocks, sent = self._inject_mocks(terminal_idle=True)
        for m in mocks:
            m.start()
        try:
            first = mcp._notify_leader_of_report("team", self._report_entry())
            second = mcp._notify_leader_of_report("team", self._report_entry())
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(first["injected"], f"C2 首次应注入,got {first}")
        self.assertFalse(second["injected"], f"C2 冷却期内不应注入,got {second}")
        self.assertEqual(second.get("reason"), "report-cooldown")
        self.assertEqual(len(sent), 1, "C2 冷却期内第二条不应产生注入")

    def test_c3_cooldown_expired_resumes(self):
        """C3: 冷却期外(时间戳调到 61 秒前)→ 恢复注入"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"C3 冷却过后应恢复注入,got {result}")
        self.assertEqual(len(sent), 1)

    def test_c4_skipped_report_still_in_pending(self):
        """C4(底线): 冷却期内被跳过的回报仍出现在 leader_pending_reports"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=datetime.now().isoformat(),  # 冷却期内
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        for m in mocks:
            m.start()
        try:
            # 端到端走 _record_report_and_notify_leader(先 append pending 再 notify)
            _, _, write_error, notice, _mark = mcp._record_report_and_notify_leader(
                "team", "bob", "完成另一个模块", event="member_report"
            )
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertIn("已记入 leader 待处理列表", notice, f"C4 应告知待处理,got {notice!r}")
        self.assertNotIn("已唤醒", notice, "C4 冷却期内不应注入唤醒")
        self.assertEqual(sent, [], "C4 不应有任何终端注入")
        self.assertEqual(write_error, "", f"C4 results.jsonl 不应写失败: {write_error}")
        t = mcp._load()["teams"]["team"]
        results = [r.get("result") for r in t.get("leader_pending_reports", [])]
        self.assertIn("完成另一个模块", results, "被跳过的回报必须进 pending,leader_activate 可见(底线)")

    def test_c5_ts_missing_bad_or_future_allows_inject(self):
        """C5: leader_last_wakeup_ts 缺失/非法/未来(时钟回拨)→ 放行注入,不抛异常"""
        # 缺失
        self._team(leader_state="active")
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"C5 缺失应放行,got {result}")
        # 非法格式
        self._team(leader_state="active", leader_last_wakeup_ts="not-a-timestamp")
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"C5 非法格式应放行,got {result}")
        # 未来时间(时钟回拨 → 负差值)
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=(datetime.now() + timedelta(seconds=300)).isoformat(),
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"C5 未来时间应放行,got {result}")


    # ------------------------------------------------------------------
    # B1-B6: 巡检兜底补投(冷却期滞留回报,冷却过期后由 monitor 巡检补投)
    #   挂载点: _monitor_team_wakeup_once 内 _execute_leader_wakeup_action
    #   之后——轮询路径本轮若已注入,其刚写入的 leader_last_wakeup_ts 会
    #   挡住兜底(共享冷却),一轮内天然不双发。B1 走巡检全入口证明挂载,
    #   B2-B6 直接调兜底 helper(每次调用 = 一轮巡检的兜底步)。
    # ------------------------------------------------------------------

    def _pending_entry(self, i=1):
        return {
            "member": f"member{i}",
            "result": f"完成模块{i}",
            "timestamp": "2026-08-09T12:00:00",
            "artifact_path": "",
        }

    def _run_reinject(self, mocks):
        for m in mocks:
            m.start()
        try:
            return mcp._retry_deferred_report_injection("team")
        finally:
            for m in reversed(mocks):
                m.stop()

    def _run_patrol(self, inject_mocks, patrol_mocks):
        all_mocks = list(inject_mocks) + list(patrol_mocks)
        for m in all_mocks:
            m.start()
        try:
            return mcp._monitor_team_wakeup_once("team")
        finally:
            for m in reversed(all_mocks):
                m.stop()

    def test_b1_patrol_reinjects_pending_after_cooldown(self):
        """B1: tmux + 2 条 pending + 冷却已过 + idle → 巡检补投成功并更新 ts"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
            leader_pending_reports=[self._pending_entry(1), self._pending_entry(2)],
        )
        inject_mocks, sent = self._inject_mocks(terminal_idle=True)
        patrol_mocks = [
            mock.patch.object(mcp, "_scan_leader_terminal", return_value={}),
            mock.patch.object(mcp, "_monitor_team_once", return_value=[]),
            mock.patch.object(mcp, "_evaluate_leader_wakeup_conditions",
                              return_value={"action": "none"}),
            mock.patch.object(mcp, "_execute_leader_wakeup_action",
                              return_value={"action": "none"}),
            mock.patch.object(mcp, "_maybe_revive_leader", return_value=(False, "")),
        ]
        result = self._run_patrol(inject_mocks, patrol_mocks)
        reinj = result["report_reinjection"]
        self.assertTrue(reinj["injected"], f"B1 冷却已过应补投,got {reinj}")
        self.assertEqual(len(sent), 1, "B1 应注入 1 条汇总消息")
        self.assertIn("member reports are waiting", sent[0])
        self.assertIn("完成模块1", sent[0])
        self.assertIn("完成模块2", sent[0])
        t = mcp._load()["teams"]["team"]
        self.assertIn("leader_last_wakeup_ts", t, "B1 补投成功后应更新冷却时间戳")
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(
            len(t["leader_pending_reports"]), 2,
            "B1 补投不消费 pending(RC1 底线:只有 leader_activate 清空)",
        )

    def test_b2_cooldown_active_no_reinject(self):
        """B2: 冷却未过(ts 刚写)→ 巡检不注入,reason=report-cooldown"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=datetime.now().isoformat(),
            leader_pending_reports=[self._pending_entry(1)],
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_reinject(mocks)
        self.assertFalse(result["injected"], f"B2 冷却期内不应补投,got {result}")
        self.assertEqual(result.get("reason"), "report-cooldown")
        self.assertEqual(sent, [], "B2 不应有任何注入")

    def test_b3_terminal_busy_no_reinject(self):
        """B3: 终端 busy → 不注入,reason=leader-live(沿用 idle 判据)"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
            leader_pending_reports=[self._pending_entry(1)],
        )
        mocks, sent = self._inject_mocks(terminal_idle=False)
        result = self._run_reinject(mocks)
        self.assertFalse(result["injected"], f"B3 busy 不应补投,got {result}")
        self.assertEqual(result.get("reason"), "leader-live")
        self.assertEqual(sent, [], "B3 不应有任何注入")

    def test_b4_no_pending_no_reinject(self):
        """B4: pending 空 → 不注入,reason=no-pending(不产生空提醒)"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_reinject(mocks)
        self.assertFalse(result["injected"], f"B4 pending 空不应补投,got {result}")
        self.assertEqual(result.get("reason"), "no-pending")
        self.assertEqual(sent, [], "B4 不应有任何注入")

    def test_b5_consecutive_patrols_no_double_inject(self):
        """B5: 连续两轮巡检 → 第二轮被自身写入的 ts 挡住,不重复注入(自受冷却)"""
        self._team(
            leader_state="active",
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
            leader_pending_reports=[self._pending_entry(1), self._pending_entry(2)],
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        for m in mocks:
            m.start()
        try:
            first = mcp._retry_deferred_report_injection("team")
            second = mcp._retry_deferred_report_injection("team")
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(first["injected"], f"B5 第一轮应补投,got {first}")
        self.assertFalse(second["injected"], f"B5 第二轮应被冷却挡住,got {second}")
        # S3 后语义：第一轮已把报告标 delivered → 第二轮无未投递可注入（no-pending）；
        # 若冷却先挡则为 report-cooldown。两者都是"不重复注入"，禁止双发。
        self.assertIn(second.get("reason"), ("report-cooldown", "no-pending"),
                      f"B5 第二轮不得重复注入, reason={second.get('reason')}")
        self.assertEqual(len(sent), 1, "B5 两轮巡检应只注入 1 条(兜底自受冷却约束)")

    def test_b6_direct_leader_no_reinject(self):
        """B6: direct leader + pending → 此路径不注入(归 nudge 管,避免双重打扰)"""
        self._team(
            leader_type="direct",
            leader_state="active",
            leader_pending_reports=[self._pending_entry(1)],
        )
        mocks, sent = self._inject_mocks(terminal_idle=True)
        result = self._run_reinject(mocks)
        self.assertFalse(result["injected"], f"B6 direct leader 不应补投,got {result}")
        self.assertEqual(result.get("reason"), "not-tmux-leader")
        self.assertEqual(sent, [], "B6 不应有任何注入")


class CodexLeaderSubmissionTests(LeaderWakeupInjectionTests):
    """task1 回归: codex leader 注入的提交确认/清空校验(Enter 被吞 → 输入框残留)。

    既有用例把 _send_context_to_member 整体 mock 掉, 覆盖不到提交原语的竞态;
    本类用 FakeCodexTerminal 模拟 codex 输入框 + "渲染/输入循环窗口吞掉首次 Enter",
    通过打补丁 _tmux/_tmux_with_input 驱动真实 _send_keys → _send_context_to_member
    全链路(修复前可复现残留: 消息停在输入框、output 为空、函数仍报 injected)。

    覆盖:
      D1 直接注入 + 首次 Enter 被吞 → 证据式补 Enter, 消息提交、输入框清空
      D2 直接注入 + 首次 Enter 正常 → 不多发 Enter(幂等, 防双提交/误提交占位)
      D3 deferred retry 补投 + 吞窗 → 补 Enter 提交
      D4 补 Enter 后仍残留(输入循环卡死)→ 不谎报 injected, 回报留在 pending
      D5 _message_residue_in_input_box 单测(回显不计/占位不计/多行/授权选项跳过)
      D6 busy→idle 门控: busy 不注入, 转 idle 后才注入
      D7 member_send_message 发往 codex leader 同样走证据式提交确认
    """

    PLACEHOLDER = "Implement {feature}"

    def setUp(self):
        super().setUp()
        # 用 getattr 让本类在"修复前代码"上也能运行: 修复前无该常量, 但会为它
        # 创建模块属性, 以便 D1 真正以"残留输入框"断言失败(而非 setup 报错)。
        self._old_settle = getattr(mcp, "CODEX_CONFIRM_SETTLE_SECONDS", 0.35)
        mcp.CODEX_CONFIRM_SETTLE_SECONDS = 0  # 测试提速: 跳过渲染沉降

    def tearDown(self):
        mcp.CODEX_CONFIRM_SETTLE_SECONDS = self._old_settle
        super().tearDown()

    # ------------------------------------------------------------------
    # fake codex 终端: 输入框 + Enter 吞窗模型
    # ------------------------------------------------------------------

    class _FakeCodexTerminal:
        PLACEHOLDER = "Implement {feature}"

        def __init__(self, swallow_first_enter=False, swallow_all=False):
            self.box = self.PLACEHOLDER
            self.output = []  # 已提交进对话的消息
            self.enter_count = 0
            self.paste_text = ""
            self.swallow_first_enter = swallow_first_enter
            self.swallow_all = swallow_all

        def type_text(self, text):
            if text:
                self.box = text  # 文本落入输入框(替换占位)

        def press_enter(self):
            self.enter_count += 1
            if self.swallow_all or (
                self.swallow_first_enter and self.enter_count == 1
            ):
                return  # 输入循环未就绪/渲染窗口吞掉 Enter → 文本残留输入框
            if self.box and self.box != self.PLACEHOLDER:
                self.output.append(self.box)
            self.box = self.PLACEHOLDER  # 提交后输入框回到占位

        def capture(self):
            lines = list(self.output) if self.output else ["some prior output"]
            lines += [f"› {self.box}", "", "  gpt-5.6-sol high · ~/mult_agent_mcp"]
            return "\n".join(lines)

    class _FakeTmux:
        def __init__(self, term):
            self.term = term
            self.calls = []

        def run(self, cmd, input_text=None, timeout=10):
            self.calls.append((list(cmd), input_text))
            name = cmd[0]
            if name == "send-keys":
                args = cmd[3:] if len(cmd) > 1 and cmd[1] == "-t" else cmd[2:]
                if "-l" in args:
                    self.term.type_text(args[args.index("-l") + 1])
                elif "Enter" in args:
                    self.term.press_enter()
                return (0, "", "")
            if name == "capture-pane":
                return (0, self.term.capture(), "")
            if name == "load-buffer":
                self.term.paste_text = input_text or ""
                return (0, "", "")
            if name == "paste-buffer":
                self.term.type_text(self.term.paste_text)
                return (0, "", "")
            if name == "delete-buffer":
                return (0, "", "")
            return (0, "", "")

    def _codex_team(self, **overrides):
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
            "members": {
                "lead": {"role": "leader", "agent": "codex",
                         "tmux_window_id": "@1", "tmux_session": "team_sess"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    def _inject_codex_mocks(self, term, terminal_idle=True, window_dead=False):
        fake = self._FakeTmux(term)
        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=window_dead),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=terminal_idle),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_target_is_claude_tmux_leader", return_value=False),
            mock.patch.object(
                mcp, "_tmux",
                side_effect=lambda cmd, timeout=10: fake.run(cmd, timeout=timeout),
            ),
            mock.patch.object(
                mcp, "_tmux_with_input",
                side_effect=lambda cmd, input_text="", timeout=10:
                    fake.run(cmd, input_text=input_text, timeout=timeout),
            ),
        ]
        return mocks, fake

    # ------------------------------------------------------------------
    # D1: 直接注入 + 首次 Enter 被吞 → 证据式补 Enter, 无残留
    # ------------------------------------------------------------------

    def test_d1_codex_swallowed_enter_resubmits(self):
        """D1: codex 输入循环吞掉首次 Enter → 补 Enter, 消息提交、输入框清空"""
        self._codex_team()
        term = self._FakeCodexTerminal(swallow_first_enter=True)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"D1 应注入成功, got {result}")
        self.assertEqual(term.box, self.PLACEHOLDER, "D1 输入框应回到占位(无残留)")
        self.assertTrue(
            any("Leader activation" in o for o in term.output),
            "D1 消息应已提交进对话",
        )
        self.assertGreaterEqual(term.enter_count, 2, "D1 吞窗后应补一次 Enter")
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertIn("leader_last_wakeup_ts", t, "D1 注入成功后应记录冷却时间戳")

    # ------------------------------------------------------------------
    # D2: 首次 Enter 正常 → 不多发 Enter(幂等)
    # ------------------------------------------------------------------

    def test_d2_codex_normal_enter_no_extra(self):
        """D2: 首次 Enter 正常提交 → 证据检查无残留, 不多发 Enter(防双提交/占位)"""
        self._codex_team()
        term = self._FakeCodexTerminal(swallow_first_enter=False)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"D2 应注入成功, got {result}")
        self.assertEqual(term.enter_count, 1, "D2 正常路径不应多发 Enter")
        self.assertEqual(term.box, self.PLACEHOLDER)
        self.assertTrue(any("Leader activation" in o for o in term.output), "D2 消息应已提交")

    # ------------------------------------------------------------------
    # D3: deferred retry 补投 + 吞窗 → 补 Enter
    # ------------------------------------------------------------------

    def test_d3_codex_deferred_retry_swallowed(self):
        """D3: _retry_deferred_report_injection 冷却过期补投 + 吞窗 → 补 Enter 提交"""
        self._codex_team(
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
            leader_pending_reports=[self._pending_entry(1)],
        )
        term = self._FakeCodexTerminal(swallow_first_enter=True)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        result = self._run_reinject(mocks)
        self.assertTrue(result["injected"], f"D3 应补投成功, got {result}")
        self.assertEqual(term.box, self.PLACEHOLDER, "D3 输入框应无残留")
        self.assertTrue(
            any("member reports are waiting" in o for o in term.output),
            "D3 汇总消息应已提交",
        )
        self.assertGreaterEqual(term.enter_count, 2, "D3 吞窗后应补一次 Enter")

    # ------------------------------------------------------------------
    # D4: 补 Enter 后仍残留 → 不谎报 injected
    # ------------------------------------------------------------------

    def test_d4_codex_confirm_fails_reports_uninjected(self):
        """D4: 补 Enter 仍被吞(输入循环卡死)→ 不谎报注入, 回报留在 pending"""
        self._codex_team()
        term = self._FakeCodexTerminal(swallow_all=True)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertFalse(result["injected"], f"D4 无法确认提交应 injected=False, got {result}")
        self.assertIn("error", result, "D4 应给出未注入原因")
        self.assertTrue(
            term.box.startswith("[唤醒通知] Leader activation: a member repor"),
            "D4 消息仍残留在输入框",
        )
        # 冷却不掩盖失败: 注入失败不写 leader_last_wakeup_ts → 后续巡检/新回报可重试
        t = mcp._load()["teams"]["team"]
        self.assertNotIn(
            "leader_last_wakeup_ts", t,
            "D4 注入失败不应写冷却时间戳(否则失败被冷却掩盖, 永不再试)",
        )

    # ------------------------------------------------------------------
    # D5: 残留检测单测
    # ------------------------------------------------------------------

    def test_d5_residue_detection_unit(self):
        """D5: 回显不计残留 / 占位不计 / 多行框检测 / 授权选项跳过"""
        msg = "一个测试消息"
        # 消息仍残留在输入框(长文本换行, 提示符行 + 续行)
        self.assertTrue(
            mcp._message_residue_in_input_box(
                "history\n› 一个测试消息\n续行内容\n\n  gpt-5.6-sol high · ~/x", msg
            )
        )
        # 已提交: 消息回显在输出区(输入框上方), 输入框回到占位 → 无残留
        self.assertFalse(
            mcp._message_residue_in_input_box(
                "history\n一个测试消息\n› Implement {feature}\n\n  gpt-5.6-sol high · ~/x", msg
            )
        )
        # 空/占位输入框 → 无残留
        self.assertFalse(
            mcp._message_residue_in_input_box(
                "history\n› Implement {feature}\n\n  gpt-5.6-sol high · ~/x", msg
            )
        )
        # 授权选项行不是输入框 → 无残留
        self.assertFalse(
            mcp._message_residue_in_input_box(
                "history\n› 1. Yes\n  gpt-5.6-sol high · ~/x", msg
            )
        )
        # 空消息 → 无残留
        self.assertFalse(mcp._message_residue_in_input_box("history\n› anything", ""))

    # ------------------------------------------------------------------
    # D6: busy→idle 门控转换
    # ------------------------------------------------------------------

    def test_d6_codex_busy_then_idle_injects(self):
        """D6: busy 不注入, 转 idle 后才注入(门控转换)"""
        self._codex_team()
        busy_mocks, _ = self._inject_codex_mocks(
            self._FakeCodexTerminal(), terminal_idle=False
        )
        for m in busy_mocks:
            m.start()
        try:
            r1 = mcp._notify_leader_of_report("team", self._report_entry())
        finally:
            for m in reversed(busy_mocks):
                m.stop()
        self.assertFalse(r1["injected"], "D6 busy 不应注入")
        self.assertEqual(r1.get("reason"), "leader-live")
        term = self._FakeCodexTerminal(swallow_first_enter=False)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        result = self._run_notify(mocks)
        self.assertTrue(result["injected"], f"D6 idle 后应注入, got {result}")
        self.assertEqual(term.enter_count, 1, "D6 idle 后正常提交, 不多发 Enter")

    # ------------------------------------------------------------------
    # D7: member_send_message 发往 codex leader 同样证据式提交确认
    # ------------------------------------------------------------------

    def test_d7_member_message_to_codex_leader_confirms(self):
        """D7: member_send_message → codex leader 吞窗 → 补 Enter, 消息提交"""
        self._codex_team()
        term = self._FakeCodexTerminal(swallow_first_enter=True)
        fake = self._FakeTmux(term)
        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_target_is_claude_tmux_leader", return_value=False),
            mock.patch.object(
                mcp, "_tmux",
                side_effect=lambda cmd, timeout=10: fake.run(cmd, timeout=timeout),
            ),
            mock.patch.object(
                mcp, "_tmux_with_input",
                side_effect=lambda cmd, input_text="", timeout=10:
                    fake.run(cmd, input_text=input_text, timeout=timeout),
            ),
        ]
        for m in mocks:
            m.start()
        try:
            out = mcp.member_send_message("team", "leader", "对齐完毕")
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertIn("✅", out, f"D7 应发送成功, got {out!r}")
        self.assertEqual(term.box, self.PLACEHOLDER, "D7 输入框应无残留")
        self.assertTrue(
            any("来自其他成员" in o and "对齐完毕" in o for o in term.output),
            "D7 消息应已提交进对话",
        )
        self.assertGreaterEqual(term.enter_count, 2, "D7 吞窗后应补一次 Enter")

    # ------------------------------------------------------------------
    # D8: wakeup action + codex 确认失败 → 不写冷却 ts（P1 冷却掩盖修复）
    # ------------------------------------------------------------------

    def test_d8_wakeup_action_codex_confirm_fail_keeps_pending(self):
        """D8: _execute_leader_wakeup_action + codex 确认失败 → injected=False 且不写 ts"""
        self._codex_team(
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        term = self._FakeCodexTerminal(swallow_all=True)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        for m in mocks:
            m.start()
        try:
            result = mcp._execute_leader_wakeup_action(
                "team", {"action": "wakeup_all_done"}
            )
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertFalse(result["injected"], f"D8 应 injected=False, got {result}")
        self.assertIn("error", result, "D8 应给出失败原因")
        t = mcp._load()["teams"]["team"]
        self.assertNotIn(
            "leader_last_wakeup_ts", t,
            "D8 注入失败不应写冷却 ts（否则新回报被 cooldown 掩盖，永不再试）",
        )
        # 下一回报不被 cooldown 压制：失败后冷却检查应放行
        self.assertTrue(
            mcp._report_wakeup_cooldown_passed(t),
            "D8 失败后冷却检查应放行后续回报注入",
        )

    # ------------------------------------------------------------------
    # D9: wakeup action + 成功注入 → 成功后才写 ts（节流保留）
    # ------------------------------------------------------------------

    def test_d9_wakeup_action_success_writes_ts(self):
        """D9: _execute_leader_wakeup_action + 成功注入 → 才写 ts（节流不被破坏）"""
        self._codex_team(
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        term = self._FakeCodexTerminal(swallow_first_enter=False)
        mocks, fake = self._inject_codex_mocks(term, terminal_idle=True)
        for m in mocks:
            m.start()
        try:
            result = mcp._execute_leader_wakeup_action(
                "team", {"action": "wakeup_all_done"}
            )
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"D9 应注入成功, got {result}")
        t = mcp._load()["teams"]["team"]
        self.assertIn(
            "leader_last_wakeup_ts", t,
            "D9 注入成功后才写冷却 ts（节流供后续防连击）",
        )
        self.assertFalse(
            mcp._report_wakeup_cooldown_passed(t),
            "D9 刚注入后冷却未过，后续注入应被节流（防连击保留）",
        )


if __name__ == "__main__":
    unittest.main()
