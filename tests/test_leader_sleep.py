"""leader_sleep 主动休眠 + 超时唤醒闭环测试。

覆盖:
  1. leader_sleep 置 resting + leader_sleep_until + 确保 wakeup enabled
  2. leader_sleep 参数 clamp / 缺 leader / direct leader 提示
  3. _evaluate_leader_wakeup_conditions 超时 → wakeup_timeout；未超时 → none
  4. _execute_leader_wakeup_action wakeup_timeout → active + injected + 清理 until
  5. wakeup_timeout 绕过 max_wakeups_per_session 限额（对照 all_done 受限）
  6. leader_activate 手动唤醒同样清理 leader_sleep_until
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class LeaderSleepTests(unittest.TestCase):
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
        workspace.mkdir()
        context.mkdir()
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            # 延时等待的确定性缝（2026-08-16 语义变更）：leader_sleep 现在是
            # **工具内阻塞等待**，置 0 表示"事件求值一次后立刻按切片返回"，
            # 事件判定路径与生产完全一致，只是不真的阻塞 240s。
            "leader_sleep_block_seconds": 0,
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    # ------------------------------------------------------------------
    # leader_sleep 工具
    # ------------------------------------------------------------------

    def test_leader_sleep_sets_resting_and_until(self):
        self._team()
        result = mcp.leader_sleep("team", max_seconds=120)
        # 2026-08-16：leader_sleep 改为工具内延时等待。无事件发生 + 单次阻塞
        # 上限(本 fixture 置 0)→ 走"切片"返回，状态仍是 resting + sleep_until
        # （注入兜底不拆），提示 agent 再调一次接着等。
        self.assertIn("已等待", result)
        self.assertIn("再次调用", result)
        data = mcp._load()
        t = data["teams"]["team"]
        self.assertEqual(t["leader_state"], "resting")
        self.assertTrue(t.get("leader_sleep_until"))
        self.assertEqual(t["leader_sleep_max_seconds"], 120)
        self.assertTrue(t["leader_wakeup_config"]["enabled"])
        self.assertTrue(t.get("monitor_enabled"))

    def test_leader_sleep_clamps_seconds(self):
        self._team()
        mcp.leader_sleep("team", max_seconds=99999)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_sleep_max_seconds"], 3600)
        mcp.leader_sleep("team", max_seconds=1)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_sleep_max_seconds"], 10)

    def test_leader_sleep_requires_leader(self):
        self._team(leader="")
        result = mcp.leader_sleep("team")
        self.assertIn("未指定 leader", result)

    def test_leader_sleep_unknown_team(self):
        self._team()
        result = mcp.leader_sleep("nope")
        self.assertIn("不存在", result)

    def test_leader_sleep_direct_leader_no_injection_note(self):
        self._team(leader_type="direct")
        result = mcp.leader_sleep("team")
        self.assertIn("已等待", result)
        self.assertIn("无注入终端", result)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "resting")

    # ------------------------------------------------------------------
    # 超时唤醒评估
    # ------------------------------------------------------------------

    def test_evaluate_timeout_when_sleep_expired(self):
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() - timedelta(seconds=1)).isoformat(),
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        action = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(action["action"], "wakeup_timeout")

    def test_evaluate_timeout_not_before_deadline(self):
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() + timedelta(seconds=300)).isoformat(),
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        action = mcp._evaluate_leader_wakeup_conditions("team", [])
        self.assertEqual(action["action"], "none")

    def test_evaluate_member_report_prioritized_over_timeout(self):
        # 成员卡授权应优先于超时：即使 sleep 已过期，有审批成员也要先走 approval
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() - timedelta(seconds=1)).isoformat(),
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": False,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        member_results = [{"member": "alice", "state": "approval", "action": "observed"}]
        action = mcp._evaluate_leader_wakeup_conditions("team", member_results)
        self.assertEqual(action["action"], "wakeup_approval")

    # ------------------------------------------------------------------
    # 超时唤醒执行
    # ------------------------------------------------------------------

    def test_execute_wakeup_timeout_injects_and_activates(self):
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() - timedelta(seconds=1)).isoformat(),
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        sent = []
        with mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True):
            with mock.patch.object(mcp, "_find_any_session", return_value="team_sess"):
                with mock.patch.object(mcp, "_member_window_target", return_value="@1"):
                    with mock.patch.object(
                        mcp, "_send_keys",
                        side_effect=lambda s, w, t: sent.append(t) or (0, ""),
                    ):
                        with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                            result = mcp._execute_leader_wakeup_action("team", {"action": "wakeup_timeout"})

        self.assertEqual(result["action"], "wakeup_timeout")
        self.assertTrue(result["injected"])
        self.assertTrue(sent)
        self.assertIn("sleep timeout reached", sent[0])
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertNotIn("leader_sleep_until", t)
        self.assertEqual(t["leader_wakeup_reason"], "timeout")

    def test_execute_wakeup_timeout_bypasses_limit(self):
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() - timedelta(seconds=1)).isoformat(),
            leader_wakeup_count=10,
            leader_wakeup_config={"enabled": True, "idle_threshold": 4,
                                  "approval_alert": True, "auto_authorize_first": True,
                                  "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        )
        with mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=False):
            with mock.patch.object(mcp, "_find_any_session", return_value="team_sess"):
                with mock.patch.object(mcp, "_member_window_target", return_value="@1"):
                    result = mcp._execute_leader_wakeup_action("team", {"action": "wakeup_timeout"})

        self.assertEqual(result["action"], "wakeup_timeout")
        # 对照：同限额下 wakeup_all_done 应被挡停
        with mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=False):
            with mock.patch.object(mcp, "_find_any_session", return_value="team_sess"):
                with mock.patch.object(mcp, "_member_window_target", return_value="@1"):
                    blocked = mcp._execute_leader_wakeup_action("team", {"action": "wakeup_all_done"})
        self.assertEqual(blocked["action"], "wakeup-limit")

    # ------------------------------------------------------------------
    # leader_activate 手动唤醒
    # ------------------------------------------------------------------

    def test_leader_activate_clears_sleep_until(self):
        self._team(
            leader_state="resting",
            leader_sleep_until=(datetime.now() + timedelta(seconds=300)).isoformat(),
        )
        result = mcp.leader_activate("team")
        self.assertIn("已激活", result)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertNotIn("leader_sleep_until", t)


if __name__ == "__main__":
    unittest.main()
