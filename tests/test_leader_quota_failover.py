"""
leader 配额识别 + 换号（_scan_leader_terminal quota 分支）
================================================================

本次任务第二步/第三步验收：
- 门控解除：quota_failover.enabled=True 时即使 wakeup 关闭也会扫描 leader
  终端（否则 leader 被 _monitor_team_once 跳过、配额耗尽永远漏检）；
  两者皆关时保持 "disabled" 早返回（零额外 tmux capture，默认行为不变）。
- fake-idle 本体回归：配额态绝不累加 leader_idle_streak、绝不 enter_resting
  —— 用 idle 对照证明机制本身活着（真 idle 仍会休息）。
- 换号：确认后复用 _recover_and_send(reason="quota_switch")，但必须先 kill
  旧 leader 窗口（quota 时 CLI 仍存活，不杀窗 _tmux_spawn_member 会返回
  "window already exists"、恢复文本打进旧进程、账号永不生效）。
- 选号统一走 _select_failover_profile → select_failover_candidate；
  pool-type-mismatch 单独告警 quota-type-mismatch 并保持阻塞。

数据隔离：temp teams_data + tmux mock（同 test_quota_failover 套路），
绝不触碰真实 teams_data.json。
"""

import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer

QUOTA_CAPTURE = (
    "✗ Error: 429 insufficient_quota\n"
    "    You exceeded your current quota, please check your plan and billing details.\n"
    "❯"
)
IDLE_CAPTURE = "⏸ manual mode on · tokens: 45.2k\n❯"


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类 + tmux mock（与 test_quota_failover 一致）。"""

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
        self.old_funcs = {"_find_any_session": mcp._find_any_session}
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD",
                        "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
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
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_funcs.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, pool=None, quota_failover=None, agent_user=None,
                   leader_agent="claude", alice_task=False, wakeup_enabled=False):
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        lead = {"role": "leader", "agent": leader_agent}
        if agent_user is not None:
            lead["agent_user"] = agent_user
        alice = {"role": "coder", "agent": "claude"}
        if alice_task:
            alice.update({"last_task": "build feature X", "last_task_completed": False})
        team = {
            "workspace_dir": str(workspace),
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            "members": {"lead": lead, "alice": alice},
        }
        if wakeup_enabled:
            team["leader_wakeup_config"] = {"enabled": True, "idle_threshold": 1}
        if pool is not None:
            team["agent_user_pool"] = list(pool)
        if quota_failover is not None:
            team["quota_failover"] = dict(quota_failover)
        data = {
            "agent_users": {
                k: {"label": k, "agent_type": "claude"}
                for k in ("acct-a", "acct-b", "acct-c")
            },
            "teams": {"team": team},
        }
        mcp._save(data)
        return workspace

    def _scan_leader(self, capture_output: str, spawn_result=(0, "", "")):
        """mock tmux 依赖后扫描 leader 终端一次，返回 (result, team, tmux_calls)。"""
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", side_effect=lambda t, m: m), \
             mock.patch.object(mcp, "_capture_window", return_value=(0, capture_output, "")), \
             mock.patch.object(mcp, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(mcp, "_write_claude_mcp", return_value=str(self.root / "mcp.json")), \
             mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(mcp, "_tmux_spawn_member", return_value=spawn_result), \
             mock.patch.object(mcp, "_send_keys", side_effect=lambda s, w, text, **kw: (0, "")), \
             mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None), \
             mock.patch.object(mcp, "_build_recovery_context", return_value=""), \
             mock.patch.object(mcp, "_record_recovery_event", return_value=None), \
             mock.patch.object(mcp, "_leader_system_prompt", return_value="[leader prompt]"), \
             mock.patch.object(mcp.time, "sleep", return_value=None):
            result = mcp._scan_leader_terminal("team")
        team = mcp._load()["teams"]["team"]
        return result, team, calls


class LeaderQuotaGateTests(_IsolatedTestCase):
    """第一步：wakeup 门控解除 —— 两者皆关保持 disabled，quota 开即扫描。"""

    def test_both_disabled_returns_disabled_without_capture(self):
        """wakeup off + quota off（默认）：state=disabled，零 tmux capture（零额外开销）。"""
        self._save_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_capture_window") as cap:
            result = mcp._scan_leader_terminal("team")
        self.assertEqual(result["state"], "disabled")
        self.assertEqual(result["action"], "disabled")
        cap.assert_not_called()

    def test_quota_enabled_wakeup_off_scans_and_confirms(self):
        """quota_failover.enabled=True + wakeup off：门控解除，双周期确认。
        无池时确认后保持阻塞（quota-pool-empty），与成员侧语义一致。"""
        self._save_team(quota_failover={"enabled": True})
        r1, t, _ = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r1["state"], "unknown", "首周期未确认，绝不 idle")
        self.assertEqual(r1["action"], "quota-suspect:1/2")
        self.assertEqual(t["leader_quota_hits"], 1)
        self.assertNotIn("blocked_reason", t["members"]["lead"])

        r2, t, _ = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r2["state"], "quota")
        self.assertEqual(r2["action"], "quota-pool-empty", "确认后无池可换 → 保持阻塞")
        self.assertEqual(t["leader_quota_hits"], 2)
        self.assertEqual(t["members"]["lead"]["blocked_reason"], "quota")
        self.assertEqual(t["leader_idle_streak"], 0)

    def test_wakeup_on_quota_off_confirm_only(self):
        """wakeup 驱动扫描 + quota_failover 默认关闭：确认并阻塞，绝不换号
        （enabled=False 的默认行为：只记录不换号，与成员侧一致）。"""
        self._save_team(wakeup_enabled=True)
        r1, _, _ = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r1["action"], "quota-suspect:1/2")
        r2, t, calls = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r2["state"], "quota")
        self.assertEqual(r2["action"], "quota-confirmed", "enabled=False → 只确认不换号")
        lead = t["members"]["lead"]
        self.assertEqual(lead["blocked_reason"], "quota")
        self.assertEqual(lead.get("agent_user", ""), "", "默认配置绝不换号")
        self.assertEqual(lead.get("quota_switch_count", 0), 0)
        self.assertNotIn("agent_user_failover_history", lead)
        kills = [c for c in calls if c and c[0] == "kill-window"]
        self.assertEqual(kills, [], "不换号不杀窗")


class LeaderQuotaFakeIdleTests(_IsolatedTestCase):
    """第二步硬约束：配额绝不 fake-idle → 绝不 enter_resting。"""

    def test_quota_never_accumulates_idle_streak(self):
        """连续 5 个配额周期：idle_streak 恒 0，leader 永不 resting。"""
        self._save_team(quota_failover={"enabled": True})
        for _ in range(5):
            r, t, _ = self._scan_leader(QUOTA_CAPTURE)
            self.assertNotEqual(r["state"], "idle")
            self.assertEqual(t["leader_idle_streak"], 0)
        self.assertEqual(t["leader_state"], "active", "配额态绝不 enter_resting")
        self.assertEqual(t["members"]["lead"]["blocked_reason"], "quota")

    def test_quota_vs_real_idle_contrast(self):
        """对照：真 idle 3 周期仍会 enter_resting（机制活着），quota 不会。"""
        self._save_team(quota_failover={"enabled": True}, alice_task=True, wakeup_enabled=True)
        # quota 命中 3 周期 → wakeup 判定绝不 enter_resting
        for _ in range(3):
            self._scan_leader(QUOTA_CAPTURE)
        action = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(action["action"], "none", "配额确认后绝不 enter_resting")
        self.assertEqual(mcp._load()["teams"]["team"]["leader_state"], "active")
        # 对照：真 idle 3 周期 → idle_streak 达标 → 判定+执行 enter_resting
        for _ in range(3):
            self._scan_leader(IDLE_CAPTURE)
        action = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(action["action"], "enter_resting", "真 idle 仍正常休息")
        executed = mcp._execute_leader_wakeup_action("team", action)
        self.assertEqual(executed["action"], "enter_resting")
        self.assertEqual(mcp._load()["teams"]["team"]["leader_state"], "resting")

    def test_non_quota_state_clears_hits(self):
        """suspect 后转 idle（错误滚出/自愈）→ leader_quota_hits 清零。"""
        self._save_team(quota_failover={"enabled": True})
        self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(mcp._load()["teams"]["team"]["leader_quota_hits"], 1)
        r, t, _ = self._scan_leader(IDLE_CAPTURE)
        self.assertEqual(r["state"], "idle", "真 idle 走正常累加")
        self.assertEqual(t["leader_quota_hits"], 0, "非 quota 状态清零计数")
        self.assertNotIn("blocked_reason", t["members"]["lead"])


class LeaderQuotaSwitchTests(_IsolatedTestCase):
    """第三步：确认后换号 —— kill 旧窗口 + _recover_and_send(quota_switch)。"""

    def test_confirmed_switches_leader_account(self):
        """enabled + confirm_cycles=1：换到池下一个，计数/历史/游标正确。"""
        self._save_team(pool=["acct-a", "acct-b"],
                        quota_failover={"enabled": True, "confirm_cycles": 1},
                        agent_user="acct-a")
        r, t, _ = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-switched:acct-b")
        lead = t["members"]["lead"]
        self.assertEqual(lead["agent_user"], "acct-b", "leader 换到池中下一个")
        self.assertEqual(lead["quota_switch_count"], 1, "quota 换号独立计数")
        self.assertEqual(lead.get("recovery_count", 0), 0, "绝不递增崩溃恢复计数")
        hist = lead["agent_user_failover_history"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["from"], "acct-a")
        self.assertEqual(hist[0]["to"], "acct-b")
        self.assertEqual(hist[0]["reason"], "quota_exhausted")
        self.assertEqual(t["agent_user_pool_cursor"], 1)
        self.assertNotIn("blocked_reason", lead, "换号成功解除阻塞")
        self.assertEqual(t["leader_quota_hits"], 0, "换号后清零计数")
        self.assertEqual(t["leader_idle_streak"], 0, "换号后仍不累加 idle")
        self.assertEqual(t["leader_state"], "active")
        self.assertEqual(t["leader_last_observed_state"], "recovering")

    def test_switch_kills_old_leader_window_first(self):
        """换号前必须 kill 旧 leader 窗口：否则 _tmux_spawn_member 报
        window already exists（rc=0）、恢复文本打进旧进程、新账号永不生效。"""
        self._save_team(pool=["acct-a", "acct-b"],
                        quota_failover={"enabled": True, "confirm_cycles": 1},
                        agent_user="acct-a")
        _, _, calls = self._scan_leader(QUOTA_CAPTURE)
        kills = [c for c in calls if c and c[0] == "kill-window"]
        self.assertEqual(len(kills), 1, "换号前必须 kill 旧 leader 窗口")
        self.assertTrue(
            any("lead" in str(x) for x in kills[0]),
            f"kill 目标应是 leader 窗口, got {kills[0]}",
        )

    def test_type_mismatch_blocks_with_special_reason(self):
        """codex leader 配纯 claude 池：单独告警 quota-type-mismatch，绝不静默降级。"""
        self._save_team(pool=["acct-a", "acct-b"],
                        quota_failover={"enabled": True, "confirm_cycles": 1},
                        agent_user="acct-a", leader_agent="codex")
        r, t, calls = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-pool-type-mismatch")
        self.assertEqual(t["members"]["lead"]["blocked_reason"], "quota-type-mismatch")
        self.assertEqual(t["members"]["lead"]["agent_user"], "acct-a", "不换号")
        self.assertEqual(t["members"]["lead"].get("quota_switch_count", 0), 0)
        self.assertNotIn("agent_user_failover_history", t["members"]["lead"])
        kills = [c for c in calls if c and c[0] == "kill-window"]
        self.assertEqual(kills, [], "类型不匹配不换号、不杀窗")

    def test_switch_failure_keeps_blocked(self):
        """重建失败：保留阻塞，agent_user 已记录新号，仍计一次切换。"""
        self._save_team(pool=["acct-a", "acct-b"],
                        quota_failover={"enabled": True, "confirm_cycles": 1},
                        agent_user="acct-a")
        r, t, _ = self._scan_leader(QUOTA_CAPTURE, spawn_result=(1, "", "boom"))
        self.assertIn("quota-switch-failed", r["action"])
        self.assertIn("boom", r["action"])
        self.assertEqual(t["members"]["lead"]["agent_user"], "acct-b")
        self.assertEqual(t["members"]["lead"]["blocked_reason"], "quota", "失败保持阻塞")
        self.assertEqual(t["members"]["lead"]["quota_switch_count"], 1)
    def test_max_switches_reached_stops_switching(self):
        """达 max_switches 上限：停止换号、保持阻塞，不无限重试。"""
        self._save_team(pool=["acct-a", "acct-b"],
                        quota_failover={"enabled": True, "confirm_cycles": 1,
                                        "max_switches": 2},
                        agent_user="acct-a")
        t = mcp._load()["teams"]["team"]
        t["members"]["lead"]["quota_switch_count"] = 2
        mcp._save({"teams": {"team": t}})
        r, t, calls = self._scan_leader(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-switch-limit")
        self.assertEqual(t["members"]["lead"]["agent_user"], "acct-a", "达上限不得再换")
        self.assertEqual(t["members"]["lead"]["quota_switch_count"], 2)
        self.assertEqual(t["members"]["lead"]["blocked_reason"], "quota")
        kills = [c for c in calls if c and c[0] == "kill-window"]
        self.assertEqual(kills, [])


if __name__ == "__main__":
    unittest.main()
