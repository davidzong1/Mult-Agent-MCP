"""tester-claude 独立 live-like 回归（task1 / task2 实际改动后）。

独立验收原则：不采信实现者自测结论。本文件用**独立建模**的假 codex 终端
（区别于 coder 的 _FakeCodexTerminal，实现与断言均独立），驱动真实
_send_keys → _send_context_to_member → _notify_leader_of_report /
_retry_deferred_report_injection / _record_report_and_notify_leader /
leader_activate 全链路，验证任务声明中的关键场景：

task1（codex leader 注入竞态）：
  L1 首次 Enter 被吞 → 检测残留 → 仅补一次 Enter → 消息进入上下文、输入框清空
  L2 已成功提交时不补 Enter（幂等，防双提交/占位误提交）
  L3 确认失败（仍残留）→ 不谎报 injected → pending report 可由 leader_activate 收到
  L4 deferred retry 补投 + 吞窗 → 证据式补 Enter 提交
  L5 重复回报幂等：冷却内第二份不双发，留在 pending
  L6 Claude 路径不回归：claude leader 走盲补 Enter，codex leader 走证据式确认
task2（分类器签名检测无条件 / allow 仍 plan-only）：
  L7 签名在各 native_mode 均判 classifier_unavailable，绝不 idle（member+leader）
  L8 allow 严格 plan-only：auto/acceptEdits/manual/default/"" 零注入；危险命令/
     全量放行绝不在集内；manual/default 行为无变化
  L9 双启动参数入口一致：mcp._claude_agent_args vs tmux_utils.claude_agent_args

隔离：仅写 MULT_AGENT_MCP_HOME 临时目录（conftest 兜底）+ 本类模块级覆盖，
不触碰真实 ~/.mult_agent_mcp/ 与当前 mcp优化 团队。
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp
from common import classifier_fallback as cf


# ---------------------------------------------------------------------------
# 独立建模的假 codex 终端：输入框 + 已提交对话历史 + Enter 吞窗窗口
# ---------------------------------------------------------------------------
class _CodexBox:
    """codex CLI 输入框模型：文本落入输入框，Enter 提交；吞窗窗口吞掉前 N 个
    Enter（文本残留输入框），吞窗后可恢复。capture 输出与真实 codex 渲染一致
    （最后一行是输入框提示符 `›`，带编号的授权选项行被排除）。"""

    PLACEHOLDER = "Implement {feature}"

    def __init__(self, swallow_enters=0, swallow_all=False):
        self.box = self.PLACEHOLDER
        self.history = []          # 已提交进对话的消息
        self.enter_count = 0
        self.paste = ""
        self.swallow_enters = swallow_enters  # 前 N 个 Enter 被吞（输入循环未就绪）
        self.swallow_all = swallow_all        # 永久吞（输入循环卡死）

    def type_text(self, text):
        if text:
            self.box = text

    def press_enter(self):
        self.enter_count += 1
        if self.swallow_all or self.enter_count <= self.swallow_enters:
            return  # 被吞：文本仍残留输入框
        if self.box and self.box != self.PLACEHOLDER:
            self.history.append(self.box)
        self.box = self.PLACEHOLDER

    def capture(self):
        lines = list(self.history) if self.history else ["some prior output"]
        lines.append(f"› {self.box}")
        lines.append("")
        lines.append("  gpt-5.6-sol high · ~/ws")
        return "\n".join(lines)


class _FakeTmux:
    """路由 _tmux / _tmux_with_input 到 _CodexBox。逐命令建模真实行为：
    send-keys（-l 单行 / Enter）、capture-pane、load-buffer/paste-buffer/delete-buffer。"""

    def __init__(self, box: _CodexBox):
        self.box = box
        self.enters = 0
        self.sent = []

    def run(self, cmd, input_text=None, timeout=10):
        name = cmd[0]
        if name == "send-keys":
            args = cmd[3:] if len(cmd) > 1 and cmd[1] == "-t" else cmd[2:]
            if "-l" in args:
                t = args[args.index("-l") + 1]
                self.box.type_text(t)
                self.sent.append(("send", t))
            elif "Enter" in args:
                self.box.press_enter()
                self.enters += 1
            return (0, "", "")
        if name == "capture-pane":
            return (0, self.box.capture(), "")
        if name == "load-buffer":
            self.box.paste = input_text or ""
            return (0, "", "")
        if name == "paste-buffer":
            self.box.type_text(self.box.paste)
            return (0, "", "")
        if name == "delete-buffer":
            return (0, "", "")
        return (0, "", "")


class Task1Task2LiveRegressionTests(unittest.TestCase):
    """独立 live-like 回归：假终端 + 真实注入链路。"""

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

    # ------------------------------------------------------------------ helpers

    def _team(self, leader_agent="codex", **overrides):
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
                "lead": {"role": "leader", "agent": leader_agent,
                         "tmux_window_id": "@1", "tmux_session": "team_sess"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    def _report_entry(self, i=1):
        return {
            "timestamp": f"2026-08-11T10:0{i}:00",
            "member": "alice",
            "event": "member_report",
            "result": f"任务完成，交付物 {i}",
            "artifact_path": "",
        }

    def _tmux_mocks(self, box):
        fake = _FakeTmux(box)
        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=False),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_tmux",
                              side_effect=lambda cmd, timeout=10: fake.run(cmd, timeout=timeout)),
            mock.patch.object(mcp, "_tmux_with_input",
                              side_effect=lambda cmd, input_text="", timeout=10:
                                  fake.run(cmd, input_text=input_text, timeout=timeout)),
        ]
        return mocks, fake

    # ------------------------------------------------------------------ L1-L2

    def test_l1_swallowed_enter_residue_detected_single_resubmit(self):
        """L1: 首次 Enter 被吞 → 检测残留 → 仅补一次 Enter → 消息进上下文、输入框清空。"""
        self._team()
        box = _CodexBox(swallow_enters=1)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry(1))
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"L1 应注入成功: {result}")
        self.assertEqual(box.box, box.PLACEHOLDER, "L1 输入框应回到占位（无残留）")
        # 补 Enter 总数：_send_keys 首次 Enter(被吞) + 证据式补 1 次 = 2
        self.assertEqual(box.enter_count, 2, "L1 应恰好补一次 Enter")
        self.assertTrue(
            any("Leader activation" in h for h in box.history),
            "L1 消息应已提交进对话上下文",
        )

    def test_l2_committed_without_swallow_no_extra_enter(self):
        """L2: 首次 Enter 正常提交 → 证据检查无残留 → 不多发 Enter（防双提交/误提交占位）。"""
        self._team()
        box = _CodexBox(swallow_enters=0)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry(1))
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"L2 应注入成功: {result}")
        self.assertEqual(box.enter_count, 1, "L2 正常路径不得多发 Enter")
        self.assertTrue(any("Leader activation" in h for h in box.history), "L2 应已提交")

    # ------------------------------------------------------------------ L3

    def test_l3_confirm_failure_keeps_pending_leader_activate_retrieves(self):
        """L3: 确认失败（输入循环卡死）→ 不谎报 injected → 回报留在 pending，
        leader_activate 可收到并消费。走真实 _record_report_and_notify_leader 链路。"""
        self._team()
        box = _CodexBox(swallow_all=True)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            _rf, _entry, _werr, notice = mcp._record_report_and_notify_leader(
                "team", "alice", "完成登录模块"
            )
        finally:
            for m in reversed(mocks):
                m.stop()
        # 注入失败：notify 返回 injected=False → notice 提示记入待处理列表
        self.assertIn("leader 待处理列表", notice, f"L3 notice 应提示待处理: {notice!r}")
        # 消息仍残留在输入框（模型侧证据）
        self.assertTrue(box.box.startswith("[system] Leader activation"), "L3 应仍残留")
        # pending 回报仍在，且可由 leader_activate 收取
        data = mcp._load()
        team = data["teams"]["team"]
        from common.leader_recovery import pending_leader_reports
        pending = pending_leader_reports(team)
        self.assertEqual(len(pending), 1, "L3 确认失败回报应留在 pending")
        act = mcp.leader_activate("team")
        self.assertIn("已确认", act, f"L3 leader_activate 应收到回报: {act}")
        data = mcp._load()
        self.assertEqual(data["teams"]["team"].get("leader_pending_reports") or [], [],
                         "L3 leader_activate 应消费清空 pending")

    # ------------------------------------------------------------------ L4

    def test_l4_deferred_retry_reinjects_with_confirm(self):
        """L4: 冷却过期后 _retry_deferred_report_injection 补投 + 吞窗 → 证据式补 Enter。"""
        self._team(
            leader_last_wakeup_ts=(datetime.now() - timedelta(seconds=61)).isoformat(),
            leader_pending_reports=[self._report_entry(1)],
        )
        box = _CodexBox(swallow_enters=1)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            result = mcp._retry_deferred_report_injection("team")
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"L4 补投应成功: {result}")
        self.assertEqual(box.box, box.PLACEHOLDER, "L4 输入框应无残留")
        self.assertEqual(box.enter_count, 2, "L4 应补一次 Enter")
        self.assertTrue(
            any("member reports are waiting" in h for h in box.history),
            "L4 汇总消息应已提交",
        )

    # ------------------------------------------------------------------ L5

    def test_l5_duplicate_report_cooldown_no_double_inject(self):
        """L5: 重复回报幂等 —— 首份注入成功后冷却期内第二份不双发、留在 pending。"""
        self._team()
        box = _CodexBox(swallow_enters=0)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            first = mcp._notify_leader_of_report("team", self._report_entry(1))
            second = mcp._notify_leader_of_report("team", self._report_entry(2))
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(first["injected"], f"L5 首份应注入: {first}")
        self.assertFalse(second["injected"], f"L5 冷却内第二份不得注入: {second}")
        self.assertEqual(second.get("reason"), "report-cooldown")
        # 首份已提交进对话；第二份未打扰终端 → 只提交了一份
        self.assertEqual(box.enter_count, 1, "L5 冷却期不得双发注入")
        self.assertEqual(len([h for h in box.history if "Leader activation" in h]), 1,
                         "L5 对话中应只有一份激活消息")

    # ------------------------------------------------------------------ L6

    def test_l6_claude_leader_still_uses_blind_confirm(self):
        """L6: Claude 路径不回归 —— claude tmux leader 走盲补 Enter，codex leader 走证据式确认，
        由真实 _target_is_*_tmux_leader 在 _notify_leader_of_report 处接线。"""
        # Claude leader
        self._team(leader_agent="claude")
        box = _CodexBox(swallow_enters=1)  # claude 盲补 Enter，即使吞窗也提交
        fake = _FakeTmux(box)
        # 盲补 Enter 的 mock 须建模真实行为（按一次 Enter），否则消息不会提交
        blind = mock.Mock(side_effect=lambda s, w, **kw: box.press_enter() or (0, ""))
        codex_conf = mock.Mock(side_effect=AssertionError("codex leader 不应走证据式确认"))
        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=False),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_tmux",
                              side_effect=lambda cmd, timeout=10: fake.run(cmd, timeout=timeout)),
            mock.patch.object(mcp, "_tmux_with_input",
                              side_effect=lambda cmd, input_text="", timeout=10:
                                  fake.run(cmd, input_text=input_text, timeout=timeout)),
            mock.patch.object(mcp, "_confirm_prompt_submission", blind),
            mock.patch.object(mcp, "_confirm_codex_leader_submission", codex_conf),
        ]
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry(1))
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"L6 claude 应注入: {result}")
        blind.assert_called_once()
        codex_conf.assert_not_called()
        # 消息进入上下文（盲补 Enter 提交）
        self.assertTrue(any("Leader activation" in h for h in box.history), "L6 claude 应提交")

    def test_l6b_codex_leader_never_blind_confirm(self):
        """L6b: codex leader 绝不走 claude 盲补 Enter（防误提交占位/双提交）。"""
        self._team(leader_agent="codex")
        box = _CodexBox(swallow_enters=0)
        fake = _FakeTmux(box)
        blind = mock.Mock(side_effect=AssertionError("codex leader 不应走 claude 盲补 Enter"))
        codex_conf = mock.Mock(side_effect=lambda s, w, m, **kw: (0, ""))
        mocks = [
            mock.patch.object(mcp, "_find_any_session", return_value="team_sess"),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=False),
            mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True),
            mock.patch.object(mcp, "_member_window_target", return_value="@1"),
            mock.patch.object(mcp, "_tmux",
                              side_effect=lambda cmd, timeout=10: fake.run(cmd, timeout=timeout)),
            mock.patch.object(mcp, "_tmux_with_input",
                              side_effect=lambda cmd, input_text="", timeout=10:
                                  fake.run(cmd, input_text=input_text, timeout=timeout)),
            mock.patch.object(mcp, "_confirm_prompt_submission", blind),
            mock.patch.object(mcp, "_confirm_codex_leader_submission", codex_conf),
        ]
        for m in mocks:
            m.start()
        try:
            result = mcp._notify_leader_of_report("team", self._report_entry(1))
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"L6b codex 应注入: {result}")
        codex_conf.assert_called_once()
        blind.assert_not_called()

    # ------------------------------------------------------------------ P1 probe: wakeup-action 注入失败不得写冷却 ts

    def test_w1_wakeup_action_failure_does_not_write_cooldown_ts(self):
        """P1 probe（reviewer 发现，当前代码应 RED）: _execute_leader_wakeup_action
        注入失败不得写 leader_last_wakeup_ts —— 否则失败被 60s 冷却掩盖，
        下一次成员回报被 cooldown 挡住（失败与成功一样触发冷却）。"""
        self._team(leader_agent="codex", leader_wakeup_config={"enabled": True})
        box = _CodexBox(swallow_all=True)  # 输入循环卡死 → 证据式确认失败
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            result = mcp._execute_leader_wakeup_action("team", {"action": "wakeup_all_done"})
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertEqual(result["action"], "wakeup_all_done")
        self.assertFalse(result["injected"], f"w1 注入应失败: {result}")
        self.assertIn("error", result, "w1 应给出未注入原因")
        data = mcp._load()["teams"]["team"]
        self.assertNotIn(
            "leader_last_wakeup_ts", data,
            "w1 注入失败不得写冷却时间戳（失败不可被冷却掩盖）")

    def test_w2_wakeup_action_failure_next_report_not_blocked(self):
        """P1 probe（当前代码应 RED）: wakeup-action 注入失败后，下一次成员回报
        不得被 60s 冷却挡住——冷却只应在**成功**注入后触发。"""
        self._team(leader_agent="codex", leader_wakeup_config={"enabled": True})
        box = _CodexBox(swallow_all=True)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            wake = mcp._execute_leader_wakeup_action("team", {"action": "wakeup_all_done"})
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertFalse(wake["injected"], f"w2 wakeup 注入应失败: {wake}")
        # 失败后立即有新回报 → 不应被 cooldown 挡
        box2 = _CodexBox(swallow_enters=0)
        mocks2, fake2 = self._tmux_mocks(box2)
        for m in mocks2:
            m.start()
        try:
            rep = mcp._notify_leader_of_report("team", self._report_entry(1))
        finally:
            for m in reversed(mocks2):
                m.stop()
        self.assertTrue(
            rep["injected"],
            f"w2 失败后的新回报应能注入（不得被冷却挡）: {rep}")

    def test_w3_wakeup_action_success_writes_cooldown_ts(self):
        """wakeup-action 成功注入 → 写冷却时间戳（成功后才触发冷却，正常生效）。"""
        self._team(leader_agent="codex", leader_wakeup_config={"enabled": True})
        box = _CodexBox(swallow_enters=0)
        mocks, fake = self._tmux_mocks(box)
        for m in mocks:
            m.start()
        try:
            result = mcp._execute_leader_wakeup_action("team", {"action": "wakeup_all_done"})
        finally:
            for m in reversed(mocks):
                m.stop()
        self.assertTrue(result["injected"], f"w3 应注入成功: {result}")
        data = mcp._load()["teams"]["team"]
        self.assertIn("leader_last_wakeup_ts", data, "w3 成功后应写冷却时间戳")

    # ------------------------------------------------------------------ task2: L7

    SIG_AUTO = (
        "deepseek/deepseek-v4-flash[1m] is temporarily unavailable, so auto mode "
        "cannot determine the safety of Write right now."
    )

    def test_l7_signature_classifier_unavailable_all_modes_never_idle(self):
        """L7: 签名在各 native_mode 均判 classifier_unavailable（绝不 idle）——
        member + leader 双侧，覆盖 acceptEdits/default/manual/auto/plan/""。"""
        for native in ("acceptEdits", "default", "manual", "auto", "plan", ""):
            with self.subTest(native=native, side="member"):
                state = mcp._classify_terminal_output(self.SIG_AUTO, native_mode=native)
                self.assertEqual(state, "classifier_unavailable",
                                 f"member native={native!r}")
            with self.subTest(native=native, side="leader"):
                state = mcp._classify_leader_terminal_output(self.SIG_AUTO, native_mode=native)
                self.assertEqual(state, "classifier_unavailable",
                                 f"leader native={native!r}")
            # 无关文本在任意模式下绝不误判 classifier_unavailable（误判对称性）
            with self.subTest(native=native, side="noise"):
                self.assertNotEqual(
                    mcp._classify_terminal_output("❯\n⏸ manual mode on",
                                                  native_mode=native),
                    "classifier_unavailable", f"noise native={native!r}")

    # ------------------------------------------------------------------ task2: L8

    def test_l8_allow_strict_plan_only_no_dangerous(self):
        """L8: allow 严格 plan-only —— auto/acceptEdits/manual/default/"" 零注入；
        plan 注入精选安全；危险/全量放行绝不在集内。"""
        for mode in ("auto", "acceptEdits", "accept_edits", "manual", "default", ""):
            with self.subTest(mode=mode):
                self.assertEqual(cf.classifier_fallback_allow_patterns("/ws", mode), [],
                                 f"mode={mode!r} 不得注入 fallback allow")
                self.assertEqual(
                    cf.claude_terminal_allow_tools(mode, "/ws", ["Bash"]), ["Bash"],
                    f"mode={mode!r} 不得外溢到 allowedTools")
        # plan 注入精选安全
        pats = cf.classifier_fallback_allow_patterns("/ws", "plan")
        self.assertIn("Edit(/ws/*)", pats)
        self.assertIn("Bash(git:*)", pats)
        self.assertIn("Bash(python3 -m pytest:*)", pats)
        # 危险 / 全量放行绝不在集内（任何模式经 claude_terminal_allow_tools 后）
        for mode in ("plan", "auto", "manual", "default"):
            joined = ",".join(cf.claude_terminal_allow_tools(mode, "/ws", []))
            for unsafe in ("Bash(*)", "Edit(*)", "Write(*)", "Bash(sudo",
                           "Bash(rm ", "Bash(curl", "Bash(wget"):
                self.assertNotIn(unsafe, joined, f"mode={mode!r} 越界放行 {unsafe!r}")
        # manual/default 行为与 baseline 一字不差（无 fallback 注入）
        self.assertEqual(
            cf.claude_terminal_allow_tools("manual", "/ws", ["Bash", "Edit"]),
            ["Bash", "Edit"])
        self.assertEqual(
            cf.claude_terminal_allow_tools("default", "/ws", ["Bash", "Edit"]),
            ["Bash", "Edit"])

    # ------------------------------------------------------------------ task2: L9

    def test_l9_dual_arg_entry_consistency(self):
        """L9: 双启动参数入口一致 —— mcp._claude_agent_args vs tmux_utils.claude_agent_args
        对 permission-mode 映射 + fallback allow 接线逐字一致。"""
        from common import tmux_utils as tu
        base = ["mcp__mult-agent-mcp__leader_*", "Bash", "Edit"]
        for mode in ("auto", "plan", "accept_edits", "manual", ""):
            with self.subTest(mode=mode):
                mcp_args = mcp._claude_agent_args(
                    "claude", mode,
                    allowed_tools=cf.claude_terminal_allow_tools(mode, "/ws", base))
                tu_args = tu.claude_agent_args(
                    "claude", mode,
                    allowed_tools=cf.claude_terminal_allow_tools(mode, "/ws", base))
                self.assertEqual(mcp_args, tu_args, f"双入口不一致 mode={mode!r}")
        # plan 追加 fallback、auto 不追加，且危险命令/全量放行绝不在 --allowedTools
        for mode, expect in (("plan", True), ("auto", False), ("manual", False)):
            args = mcp._claude_agent_args(
                "claude", mode,
                allowed_tools=cf.claude_terminal_allow_tools(mode, "/ws", base))
            joined = ",".join(args)
            self.assertEqual("Bash(pwd:*)" in joined, expect, f"mode={mode!r}")
            for unsafe in ("Bash(*)", "Edit(*)", "Write(*)"):
                self.assertNotIn(unsafe, joined, f"mode={mode!r} 越界")


if __name__ == "__main__":
    unittest.main()
