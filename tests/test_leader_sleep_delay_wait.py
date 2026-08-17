"""leader_sleep 延时等待 + "未送达绝不消费唤醒" 回归（2026-08-16 修复验收）。

两处修复，缺一都会让 leader 睡死：

R1 `_execute_leader_wakeup_action` —— 旧实现无条件先跑 update_wakeup
   （置 active + pop leader_sleep_until + 计数 +1），再 `if not should_inject`。
   于是唤醒那一刻终端不空闲，这次唤醒被**不可逆地消费**：resting 分支不再成立、
   超时分支也不再成立，下一轮 `_evaluate` 直接 `{"action": "none"}`，终端一个字
   没收到却永远等不到第二次。对 Claude leader 同样成立（唤醒时恰在跑工具）。
   现语义：**投递成功才推进状态**；未送达/注入失败只留延迟证据，下轮重试。

R2 `leader_sleep` —— 从"打标记 + 要求 agent 结束回合等注入"改为**工具内延时
   等待**：阻塞到"新回报 / 卡授权 / 全部完成 / 到点"后带摘要返回，agent 同一
   回合继续。注入兜底不拆（仍写 resting + sleep_until）。

确定性：不靠 wall-clock 竞速。
  - 切片路径用团队字段 `leader_sleep_block_seconds=0`（求值一次事件即返回）；
  - 事件路径打桩 `time.sleep`，在第 N 次轮询间隙改数据，模拟"等待期间发生了
    什么"，循环因此完全由调用次数驱动，不受机器快慢影响。
"""
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common.leader_recovery import pending_leader_reports

CODEX_IDLE = "上一轮回复已完成。\n›\n\n  gpt-5.6-sol high · /home/zwc/ws"
CODEX_BUSY = "上文\n■ Working (12s • esc to interrupt)\n›\n  gpt-5.6-sol high · ~/ws"


class _IsolatedTeamCase(unittest.TestCase):
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
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "MULT_AGENT_MCP_CONTEXT_DIR")
        }
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
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _team(self, *, block_seconds=0, leader_agent="codex", leader_type="tmux",
              members=None, **overrides):
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "terminals_active": False,
            "leader": "lead",
            "leader_type": leader_type,
            "leader_state": "active",
            "leader_sleep_block_seconds": block_seconds,
            "leader_wakeup_config": {
                "enabled": True, "idle_threshold": 4, "approval_alert": True,
                "auto_authorize_first": True, "cooldown_cycles": 6,
                "max_wakeups_per_session": 10, "report_wakeup_enabled": True,
            },
            "members": members if members is not None else {
                "lead": {"role": "leader", "agent": leader_agent},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    def _t(self) -> dict:
        return mcp._load()["teams"]["team"]

    def _mutate(self, fn):
        """在轮询间隙修改团队数据（模拟"等待期间发生了什么"）。"""
        def _upd(latest_team):
            fn(latest_team)
            return {"ok": True}

        mcp._update_team_data("team", _upd)

    def _sleep_until_event(self, mutate_at_poll, mutation, *, max_seconds=120,
                           max_polls=8):
        """打桩 time.sleep 驱动轮询：第 N 次间隙执行 mutation。"""
        state = {"n": 0}

        def fake_sleep(_seconds):
            state["n"] += 1
            if state["n"] == mutate_at_poll:
                self._mutate(mutation)
            if state["n"] > max_polls:
                raise AssertionError("轮询未在预期次数内命中事件")

        with mock.patch.object(mcp.time, "sleep", side_effect=fake_sleep):
            return mcp.leader_sleep("team", max_seconds=max_seconds)


# =====================================================================
# R1：未送达绝不消费唤醒
# =====================================================================
class WakeupNotConsumedUntilDeliveredTests(_IsolatedTeamCase):
    def _resting_team(self, **kw):
        past = (datetime.datetime.now() - datetime.timedelta(seconds=5)).isoformat()
        return self._team(leader_state="resting", leader_sleep_until=past, **kw)

    def _cycle(self, capture, send_rc=0, sent=None):
        sent = sent if sent is not None else []
        with mock.patch.object(mcp, "_find_any_session", return_value="sess"), \
             mock.patch.object(mcp, "_member_window_target", side_effect=lambda t, m: m), \
             mock.patch.object(mcp, "_capture_window", return_value=(0, capture, "")), \
             mock.patch.object(
                 mcp, "_send_context_to_member",
                 side_effect=lambda *a, **k: (sent.append(a[2]) or (send_rc, ""
                                              if send_rc == 0 else "boom"))):
            info = mcp._evaluate_leader_wakeup_conditions("team", [])
            return info, mcp._execute_leader_wakeup_action("team", info), sent

    def test_codex_idle_leader_actually_gets_injected(self):
        """终端识别修好后，codex leader 的超时唤醒必须真的注入。"""
        self._resting_team()
        info, result, sent = self._cycle(CODEX_IDLE)
        self.assertEqual(info["action"], "wakeup_timeout")
        self.assertTrue(result["injected"])
        self.assertEqual(len(sent), 1)
        t = self._t()
        self.assertEqual(t["leader_state"], "active")
        self.assertIsNone(t.get("leader_sleep_until"))
        self.assertEqual(t["leader_wakeup_count"], 1)

    def test_busy_leader_defers_without_consuming_wakeup(self):
        """唤醒时刻终端在跑工具：不注入、不置 active、不删 sleep_until、不计数。"""
        self._resting_team()
        info, result, sent = self._cycle(CODEX_BUSY)
        self.assertEqual(info["action"], "wakeup_timeout")
        self.assertFalse(result["injected"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["reason"], "leader-not-idle")
        self.assertEqual(len(sent), 0)
        t = self._t()
        self.assertEqual(t["leader_state"], "resting", "未送达不得置 active")
        self.assertTrue(t.get("leader_sleep_until"), "未送达不得消耗休眠截止时间")
        self.assertEqual(t.get("leader_wakeup_count", 0), 0)
        self.assertEqual(t["leader_wakeup_deferred_reason"], "leader-not-idle")
        self.assertEqual(t["leader_wakeup_deferred_count"], 1)

    def test_deferred_wakeup_is_retried_next_cycle(self):
        """延迟的唤醒必须在终端空下来的下一轮补上（这是"不消费"的意义所在）。"""
        self._resting_team()
        sent = []
        self._cycle(CODEX_BUSY, sent=sent)
        self.assertEqual(len(sent), 0)
        info, result, sent = self._cycle(CODEX_IDLE, sent=sent)
        self.assertEqual(info["action"], "wakeup_timeout")
        self.assertTrue(result["injected"])
        self.assertEqual(len(sent), 1, "第二轮必须补上")
        t = self._t()
        self.assertEqual(t["leader_state"], "active")
        self.assertIsNone(t.get("leader_sleep_until"))

    def test_inject_failure_also_defers(self):
        """注入失败（codex 提交确认 rc!=0）同样不得消费唤醒。"""
        self._resting_team()
        info, result, sent = self._cycle(CODEX_IDLE, send_rc=-1)
        self.assertFalse(result["injected"])
        self.assertTrue(result["deferred"])
        self.assertEqual(result["reason"], "inject-failed")
        t = self._t()
        self.assertEqual(t["leader_state"], "resting")
        self.assertTrue(t.get("leader_sleep_until"))
        self.assertEqual(t.get("leader_wakeup_count", 0), 0)
        self.assertIsNone(t.get("leader_last_wakeup_ts"), "失败不得刷新注入冷却")

    def test_wakeup_limit_still_blocks_non_timeout(self):
        """回归锚点：max_wakeups_per_session 对非 timeout 唤醒仍然生效。"""
        cfg = {
            "enabled": True, "idle_threshold": 4, "approval_alert": True,
            "auto_authorize_first": True, "cooldown_cycles": 6,
            "max_wakeups_per_session": 1, "report_wakeup_enabled": True,
        }
        self._team(leader_state="resting", leader_wakeup_config=cfg,
                   leader_wakeup_count=1,
                   members={"lead": {"role": "leader", "agent": "codex"},
                            "alice": {"role": "coder", "agent": "claude",
                                      "last_task": "X", "last_task_completed": True}})
        info, result, sent = self._cycle(CODEX_IDLE)
        self.assertEqual(info["action"], "wakeup_all_done")
        self.assertEqual(result["action"], "wakeup-limit")
        self.assertEqual(len(sent), 0)


# =====================================================================
# R2：leader_sleep 延时等待
# =====================================================================
class LeaderSleepDelayWaitTests(_IsolatedTeamCase):
    def test_slice_return_keeps_resting_and_asks_to_recall(self):
        """无事件 + 到单次阻塞上限 → 切片返回，状态原样，提示再调一次。"""
        self._team()
        result = mcp.leader_sleep("team", max_seconds=600)
        self.assertIn("已等待", result)
        self.assertIn("再次调用", result)
        self.assertIn("不要结束回合", result)
        t = self._t()
        self.assertEqual(t["leader_state"], "resting")
        self.assertTrue(t.get("leader_sleep_until"))
        self.assertEqual(t["leader_sleep_slices"], 1)

    def test_member_report_during_wait_returns_immediately(self):
        """等待期间成员回报 → 立刻带回报内容返回，并置 active、解除休眠记账。"""
        self._team(block_seconds=240)

        def add_report(team):
            team.setdefault("leader_pending_reports", []).append({
                "timestamp": datetime.datetime.now().isoformat(),
                "member": "alice", "event": "member_report",
                "result": "登录模块已完成，接口全绿",
                "report_id": "rid-1", "delivered": False,
            })

        result = self._sleep_until_event(2, add_report)
        self.assertIn("收到 1 条成员回报", result)
        self.assertIn("登录模块已完成", result)
        self.assertIn("同一回合", result)
        t = self._t()
        self.assertEqual(t["leader_state"], "active")
        self.assertIsNone(t.get("leader_sleep_until"))
        self.assertEqual(t["leader_wakeup_reason"], "report")
        self.assertTrue(pending_leader_reports(t)[0]["delivered"],
                        "已直接返回给 leader 的回报要标 delivered，避免巡检重复注入")

    def test_pre_existing_report_does_not_end_wait_immediately(self):
        """休眠前就躺着的旧回报不算事件，否则 leader 一睡下就被自己叫醒。"""
        self._team(leader_pending_reports=[{
            "timestamp": "2026-08-16T00:00:00", "member": "alice",
            "event": "member_report", "result": "旧回报",
            "report_id": "old-1", "delivered": False,
        }])
        result = mcp.leader_sleep("team", max_seconds=600)
        self.assertIn("已等待", result)
        self.assertEqual(self._t()["leader_state"], "resting")

    def test_member_blocked_on_approval_ends_wait(self):
        self._team(block_seconds=240)

        def block_alice(team):
            team["members"]["alice"]["last_observed_state"] = "approval"
            team["members"]["alice"]["blocked_reason"] = "approval"

        result = self._sleep_until_event(2, block_alice)
        self.assertIn("卡在授权提示", result)
        self.assertIn("alice", result)
        self.assertIn("leader_authorize_member", result)
        t = self._t()
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(t["leader_wakeup_reason"], "approval")

    def test_auto_authorized_member_does_not_end_wait(self):
        """monitor 自动授权成功会把成员改写成 busy 并清 blocked_reason —— 不算事件。"""
        self._team(block_seconds=0)

        def auto_ok(team):
            team["members"]["alice"]["last_observed_state"] = "busy"
            team["members"]["alice"]["blocked_reason"] = None

        self._mutate(auto_ok)
        result = mcp.leader_sleep("team", max_seconds=600)
        self.assertIn("已等待", result)
        self.assertEqual(self._t()["leader_state"], "resting")

    def test_all_tasks_done_ends_wait(self):
        self._team(block_seconds=240)

        def finish(team):
            team["members"]["alice"]["last_task_completed"] = True

        result = self._sleep_until_event(2, finish)
        self.assertIn("均已完成", result)
        t = self._t()
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(t["leader_wakeup_reason"], "all_done")

    def test_no_task_assigned_does_not_fire_all_done(self):
        """"曾有过工作"守卫：没派过活时不得一睡下就被"全部完成"叫醒。"""
        self._team(members={
            "lead": {"role": "leader", "agent": "codex"},
            "alice": {"role": "coder", "agent": "claude"},
        })
        result = mcp.leader_sleep("team", max_seconds=600)
        self.assertIn("已等待", result)
        self.assertNotIn("均已完成", result)
        self.assertEqual(self._t()["leader_state"], "resting")

    def test_deadline_reached_returns_timeout_summary(self):
        """到达 max_seconds → 超时摘要 + 置 active（真正的等待结束）。"""
        self._team()
        past = (datetime.datetime.now() - datetime.timedelta(seconds=1)).isoformat()
        mcp.leader_sleep("team", max_seconds=600)          # 先进入等待态
        result = mcp._leader_sleep_block("team", until_iso=past, max_seconds=600)
        self.assertIn("已等满", result)
        self.assertIn("识别是否存在阻塞", result)
        t = self._t()
        self.assertEqual(t["leader_state"], "active")
        self.assertIsNone(t.get("leader_sleep_until"))
        self.assertEqual(t["leader_wakeup_reason"], "timeout")

    def test_external_wakeup_ends_wait_without_overwriting_reason(self):
        """注入兜底/leader_activate 把 leader 置 active → 等待立即结束，
        且不覆盖唤醒方写下的 wakeup_reason。"""
        self._team(block_seconds=240)

        def external(team):
            team["leader_state"] = "active"
            team["leader_wakeup_reason"] = "approval"

        result = self._sleep_until_event(2, external)
        self.assertIn("已被激活", result)
        self.assertEqual(self._t()["leader_wakeup_reason"], "approval")

    def test_direct_leader_waits_too(self):
        """direct leader 同样由工具完成等待，不再依赖手动 leader_activate。"""
        self._team(leader_type="direct", block_seconds=240)

        def add_report(team):
            team.setdefault("leader_pending_reports", []).append({
                "timestamp": datetime.datetime.now().isoformat(),
                "member": "alice", "event": "member_report", "result": "done",
                "report_id": "rid-d", "delivered": False,
            })

        result = self._sleep_until_event(2, add_report)
        self.assertIn("收到 1 条成员回报", result)
        self.assertIn("无注入终端", result)
        self.assertIn("无需再调 leader_activate", result)
        self.assertEqual(self._t()["leader_state"], "active")

    def test_wait_does_no_terminal_capture(self):
        """等待只读数据层：每秒一次 tmux dump 既贵又会与 monitor 抢 tmux。"""
        self._team(block_seconds=240)

        def finish(team):
            team["members"]["alice"]["last_task_completed"] = True

        with mock.patch.object(mcp, "_capture_window") as cap:
            self._sleep_until_event(2, finish)
        cap.assert_not_called()


if __name__ == "__main__":
    unittest.main()
