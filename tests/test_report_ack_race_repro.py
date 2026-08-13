"""
P1 修复任务颗粒度对齐 —— 根因复现探针（只读分析阶段，不改生产代码）
================================================================

目标：复现"成员已调用 member_report_result、回报仍留在成员对话窗或状态已 idle，
但 Codex leader 误判未发送"的竞态，锁定根因，供实现阶段落地最小可靠性方案。

证据层级（诚实标注，不把 mock 当实机）：
  [L1 真实生产函数] 完整执行 member_report_result / _record_report_and_notify_leader /
      _retry_deferred_report_injection / _notify_leader_of_report 的真实代码路径，
      仅 mock tmux IPC 边界(_tmux/_send_keys/_capture_window) 与无关副作用。
  [L1 真实文件产物] results.jsonl、member_contexts/、teams_data.json 真实落盘断言。
  [L2 未覆盖] 真实 claude/codex CLI 消费；本探针不启动真实 CLI。

探针覆盖：
  P1  完成标记先于报告持久化 → 崩溃窗口 = "完成但无报告"（leader 只能看到终端残留）
  P2  注入即投递但无 ACK → 未 leader_activate 时同批 pending 每 60s 重复注入（重放风暴）
  P3  终端残留/状态 idle 不影响事实状态（现有事实状态可独立于 UI 读出）

隔离方式镜像 tests/test_runtime_identity_probe.py：temp 项目根 + data_layer override，
绝不触碰真实 teams_data.json / 真实 tmux session。
"""
import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer

TEAM = "team"
SESSION = "mcp_team"


class _IsolatedReportProbe(unittest.TestCase):
    """隔离团队数据 + tmux mock，复现报告闭环竞态。"""

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
        self._calls = []
        self._sends = []

    def tearDown(self):
        for name, val in self.old_globals.items():
            setattr(mcp, name, val)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # -- 构造 --------------------------------------------------------------

    def _team(self, *, leader_type="tmux", wakeup_enabled=True, report_wakeup=True):
        ws = self.root / "workspace"
        ws.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(ws),
            "leader": "lead",
            "leader_type": leader_type,
            "default_agent": "claude",
            "terminals_active": True,
            "leader_wakeup_config": {
                "enabled": wakeup_enabled,
                "report_wakeup_enabled": report_wakeup,
                "idle_threshold": 2,
                "cooldown_cycles": 0,
            },
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "coder": {
                    "role": "coder", "agent": "claude",
                    "last_task": "task for coder",
                    "last_task_completed": False,
                },
            },
        }
        mcp._save({"teams": {TEAM: team}})
        return ws

    def _load_team(self):
        return mcp._load()["teams"][TEAM]

    # -- tmux mock：leader 窗口存活且空闲、成员窗口存活 ---------------

    def fake_tmux(self, cmd, timeout=10):
        self._calls.append(list(cmd))
        op = cmd[0]
        if op == "has-session":
            return 0, "", ""
        if op == "list-sessions":
            return 0, f"{SESSION}\n", ""
        if op == "list-windows":
            return 0, "$1\t1000\t@1\tcoder\n$1\t1000\t@2\tlead\n", ""
        if op == "capture-pane":
            return 0, "❯\n", ""  # idle（Claude 就绪提示）
        return 0, "", ""

    def _patch_env(self, **patches):
        import contextlib
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mcp, "_tmux", side_effect=self.fake_tmux))
        stack.enter_context(mock.patch.object(
            mcp, "_send_keys",
            side_effect=lambda s, w, text, **kw: self._sends.append((w, text)) or (0, ""),
        ))
        stack.enter_context(mock.patch.object(mcp, "_write_claude_permissions", return_value=""))
        stack.enter_context(mock.patch.object(mcp, "_write_claude_mcp", return_value=None))
        stack.enter_context(mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")))
        stack.enter_context(mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None))
        stack.enter_context(mock.patch.object(mcp, "_record_recovery_event", return_value=None))
        stack.enter_context(mock.patch.object(mcp.time, "sleep", return_value=None))
        for attr, spec in patches.items():
            stack.enter_context(mock.patch.object(mcp, attr, spec))
        return stack

    # -- 断言辅助 ----------------------------------------------------------

    def _results_jsonl(self):
        return Path(mcp._share_dir(TEAM)) / "results.jsonl"

    def _read_results(self):
        path = self._results_jsonl()
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _pending(self):
        return mcp.pending_leader_reports(self._load_team())

    def _inject_texts(self):
        # C3 后唤醒注入前缀为诚实通道名 [唤醒通知]（不再伪称 [system]）
        return [t for _, t in self._sends if t and ("[唤醒通知]" in t or "回报" in t)]


# =====================================================================
# P1 复现：完成标记先于报告持久化 → 崩溃窗口 = "完成但无报告"
# =====================================================================
class ReportOrderingRaceRepro(_IsolatedReportProbe):
    """P1 根因：member_report_result 先把 last_task_completed=True / idle 落盘，
    稍后才写 results.jsonl / pending。该窗口内进程被杀/服务崩溃 → 成员被标记
    完成但 leader 事实状态无任何报告 → leader 只能从成员对话窗残留（UI）判断。"""

    def test_complete_flag_persisted_before_report_record(self):
        """精确复现：模拟在标记完成与写报告之间进程死亡（报告写入被跳过）。"""
        self._team()
        # --- 复刻 member_report_result 的"标记完成"段（line 8962-8970），然后"崩溃"---
        data = mcp._load()
        m = data["teams"][TEAM]["members"]["coder"]
        m["last_task_completed"] = True
        m["last_observed_state"] = "idle"
        mcp._save(data)
        # 崩溃点：_record_report_and_notify_leader 从未执行 → 无 results.jsonl / pending

        # --- leader 事实状态：完成但无报告 ---
        team = self._load_team()
        self.assertTrue(team["members"]["coder"]["last_task_completed"])   # ✅ 完成
        self.assertEqual(team["members"]["coder"]["last_observed_state"], "idle")
        self.assertFalse(self._results_jsonl().exists(), "results.jsonl 不应有记录")
        self.assertEqual(self._pending(), [], "pending 不应有报告")
        # leader 可见口径：leader_check_member_status 显示"✅ 完成"，但无任何报告内容
        out = mcp.leader_check_member_status(TEAM, "coder")
        self.assertIn("✅ 完成", out)
        self.assertNotIn("结果", out)  # 没有任何报告内容可读

    def test_member_terminal_shows_report_but_fact_has_none(self):
        """UI/终端残留 vs 事实状态：成员窗口显示"✅ 结果已记录"，但事实状态无报告。
        这正是"回报仍留在成员对话窗，leader 却判未发送"的镜像：残留只在 UI，
        leader 的数据层读不到任何报告。"""
        self._team()
        # 成员窗口残留（/compact 未清或失败）：leader_read_member_terminal 可见
        self._sends.append(("@1", "✅ 结果已记录到共享上下文区\n📄 /tmp/fake/results.jsonl"))
        # 事实状态：无报告
        self.assertFalse(self._results_jsonl().exists())
        self.assertEqual(self._pending(), [])
        # leader 若靠读终端判"是否已发送"，残留会误导；数据层说"无"——这就是竞态本尊。
        self.assertEqual(len(self._pending()), 0)


# =====================================================================
# P2 复现：注入即投递但无 ACK → 同批 pending 每 60s 重复注入（重放风暴）
# =====================================================================
class ReportReplayNoAckRepro(_IsolatedReportProbe):
    """P2 根因：_notify_leader_of_report 注入成功后不消费/不标记 pending 报告；
    只要 leader 不调用 leader_activate（drain），_retry_deferred_report_injection
    会把同一批 pending 每 60s 反复注入 leader 终端 → leader 看到"待处理"永远在，
    误以为成员回报仍未发送/未确认。"""

    def test_delivered_report_not_reinjected_until_acked(self):
        """S3 修复后语义：已投递(delivered)未 ACK 的报告不再每 60s 重放——
        只重放未投递；pending 保留直到 leader_activate 消费（ACK 唯一消费点）。

        注：本用例原为 P2 复现（同批每周期重放），随 S3 修复翻转断言——"注入即
        投递、未 ACK 重放"正是竞态 B 根因，已根治为"已投递不再重放、只等 ACK"。"""
        self._team()
        with self._patch_env():
            # 成员真实回报一次：results.jsonl + pending + 注入 wakeup（injected）
            ret = mcp.member_report_result(TEAM, "P2 根因回报 内容", member_name="coder")
        self.assertIn("已记录到共享上下文区", ret)
        self.assertEqual(len(self._pending()), 1, "回报应进 pending")
        first_injections = [t for _, t in self._sends if "回报" in t or "[唤醒通知]" in t]
        self.assertTrue(first_injections, "首次注入应发生")
        self.assertTrue(self._pending()[0].get("delivered"),
                        "注入成功应标 delivered（S3）")

        # leader 一直没 leader_activate → 冷却过期后下一次巡检：已投递不再重放
        self._sends.clear()
        # 冷却过期：把 leader_last_wakeup_ts 拨回 60s 前
        def _expire_cooldown():
            data = mcp._load()
            data["teams"][TEAM]["leader_last_wakeup_ts"] = (
                datetime.datetime.now() - datetime.timedelta(seconds=120)
            ).isoformat()
            mcp._save(data)
        with self._patch_env():
            _expire_cooldown()
            again = mcp._retry_deferred_report_injection(TEAM)
        self.assertFalse(again.get("injected"),
                         "已投递未 ACK 的报告不应被巡检重放（S3 根治竞态 B）")
        self.assertEqual(self._sends, [], "已投递报告不得再次注入 leader 终端")
        # pending 原样保留（delivered 不消费报告）——ACK 只存在于 leader_activate drain
        self.assertEqual(len(self._pending()), 1, "注入后 pending 不减少 → 未 ACK")
        self.assertEqual(self._pending()[0]["result"], "P2 根因回报 内容")
        # 最终 ACK：leader_activate 消费清空（持久化 ACK 证据）
        out = mcp.leader_activate(TEAM)
        self.assertIn("P2 根因回报 内容", out)
        self.assertEqual(self._pending(), [], "activate 消费后 pending 清空（ACK 证据）")

    def test_injected_report_remains_unacked_keeps_leader_busy(self):
        """注入成功也不置 ACK → leader_has_unfinished_work 恒真 → 团队永不进待机。"""
        self._team()
        with self._patch_env():
            mcp.member_report_result(TEAM, "另一条回报", member_name="coder")
        # 注入已发生，但 pending 仍非空 → 未完成工作判断恒 True（即使所有任务完成）
        self.assertTrue(self._pending())
        self.assertTrue(mcp.leader_has_unfinished_work(self._load_team()))
        # 即便把成员任务与总任务都标完成，pending 未 ACK 仍判"未完成"
        data = mcp._load()
        data["teams"][TEAM]["leader_last_task"] = "overall"
        data["teams"][TEAM]["leader_last_task_completed"] = True
        data["teams"][TEAM]["members"]["coder"]["last_task_completed"] = True
        mcp._save(data)
        self.assertTrue(mcp.leader_has_unfinished_work(self._load_team()),
                        "pending 未 ACK → 永不判无未完成工作")


# =====================================================================
# P3 证据：终端残留/状态 idle 独立于事实状态可读（修复方向的可测基线）
# =====================================================================
class FactStateIndependentOfResidue(_IsolatedReportProbe):
    """修复后事实状态应可独立于终端残留/UI 读出。此探针验证事实状态
    （results.jsonl + pending）是唯一权威，与终端捕获解耦。"""

    def test_fact_state_readable_without_any_terminal_capture(self):
        """成员回报后，即使终端捕获被完全禁用，事实状态（results.jsonl + pending）
        仍完整可读 —— 这是修复方向（leader 判定只依赖数据层）的落点基线。"""
        self._team()
        with self._patch_env():
            mcp.member_report_result(TEAM, "事实状态 结论", member_name="coder")
        # 不读任何终端，仅数据层：
        entries = [e for e in self._read_results() if e.get("member") == "coder"]
        self.assertTrue(entries)
        self.assertEqual(entries[-1]["result"], "事实状态 结论")
        self.assertTrue(any(r.get("result") == "事实状态 结论" for r in self._pending()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
