"""
quota 换号（阶段3）— 按池顺序切换测试
================================================================

来源:docs/plan-b-hot-restart-resume.md 阶段3 + 任务裁定。
范围:确认 quota 后按团队 agent 用户池顺序真正换号;不做会话 resume(阶段2 未做)。

裁定要点(与讨论产物冲突时以此为准):
- enabled=False(默认)→ 只记录不换号,保持 blocked_reason="quota" 行为(默认不变)
- 池遍历直接复用 common.tmux_utils.next_agent_user_in_pool,不重复实现
- 换号计数用独立的 member["quota_switch_count"],与 recovery_count 分开
  (doc §3.2:混用会让换号能力被 monitor_max_recoveries 误杀)
- 池空/池长1/wrap=False 到尾 → 保持阻塞,不静默降级
- 达到 max_switches → 停止换号并告警,不无限重试
- ⚠️ 阶段3 诚实标注:无 resume,换号后是全新会话重发 last_task 从头做

数据隔离:复用 _IsolatedTestCase 套路(temp teams_data + tmux mock),
绝不触碰真实 teams_data.json。
"""

import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer

# 正例语料(复用阶段1):Error 前缀 + 强词 insufficient_quota → 单帧即 quota
QUOTA_CAPTURE = (
    "✗ Error: 429 insufficient_quota\n"
    "    You exceeded your current quota, please check your plan and billing details.\n"
    "❯"
)
IDLE_CAPTURE = "⏸ manual mode on · tokens: 45.2k\n❯"


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类 + tmux mock 惯例(与 test_quota_classification 一致)。"""

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
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
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
                   quota_switch_count=0, recovery_count=0):
        """构造团队数据;pool 为 agent 用户池(需 registry 里有对应 key)。"""
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "leader": "lead",
            "leader_type": "tmux",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {
                    "role": "coder",
                    "agent": "claude",
                    "last_task": "build feature X",
                    "last_task_completed": False,
                    "quota_switch_count": quota_switch_count,
                    "recovery_count": recovery_count,
                },
            },
        }
        if agent_user is not None:
            team["members"]["alice"]["agent_user"] = agent_user
        if pool is not None:
            team["agent_user_pool"] = list(pool)
        if quota_failover is not None:
            team["quota_failover"] = dict(quota_failover)
        data = {
            # agent_type 必须与成员 CLI 类型(claude)一致：tmux_utils 的
            # _profile_matches_atype provider 防呆会滤掉无 agent_type/base_url
            # 的 legacy profile（换过去三处注入全空、原地空转），旧 fixture
            # 只写 {"label": k} 会被整体滤空 → pool-type-mismatch
            "agent_users": {
                k: {"label": k, "agent_type": "claude"}
                for k in ("acct-a", "acct-b", "acct-c", "acct-d")
            },
            "teams": {"team": team},
        }
        mcp._save(data)
        return workspace

    def _scan_alice(self, capture_output: str, spawn_result=(0, "", "")):
        """mock tmux 与 _recover_and_send 内部依赖后扫描 alice 一次。"""
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", side_effect=lambda t, m: m):
                with mock.patch.object(mcp, "_capture_window", return_value=(0, capture_output, "")):
                    with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(self.root / "mcp.json")):
                        with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                            with mock.patch.object(mcp, "_tmux_spawn_member", return_value=spawn_result):
                                with mock.patch.object(mcp, "_send_keys", side_effect=lambda s, w, text, **kw: (0, "")):
                                    with mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None):
                                        with mock.patch.object(mcp, "_build_recovery_context", return_value=""):
                                            with mock.patch.object(mcp, "_record_recovery_event", return_value=None):
                                                with mock.patch.object(mcp.time, "sleep", return_value=None):
                                                    result = mcp._scan_member_terminal("team", "alice")
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        team = mcp._load()["teams"]["team"]
        return result, member, team


class QuotaFailoverEnabledTests(_IsolatedTestCase):
    """enabled=False(默认)不换号;enabled=True 按池顺序切换。"""

    def test_default_disabled_confirms_but_does_not_switch(self):
        """默认配置(enabled 缺省 False):确认 quota 但 agent_user 不变,只记录阻塞。"""
        self._save_team(pool=["acct-a", "acct-b"], agent_user="acct-a")
        # 默认 confirm_cycles=2:首周期 suspect,第二周期确认
        r, m, t = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["state"], "unknown")
        self.assertEqual(r["action"], "quota-suspect:1/2")
        r, m, t = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["state"], "quota")
        self.assertEqual(r["action"], "quota-confirmed")
        self.assertEqual(m["agent_user"], "acct-a", "默认配置不得换号")
        self.assertEqual(m["blocked_reason"], "quota")
        self.assertNotIn("agent_user_failover_history", m)
        self.assertEqual(m["quota_switch_count"], 0, "未换号不得递增计数")
        self.assertEqual(m["recovery_count"], 0)

    def test_enabled_switches_to_next_pool_order(self):
        """enabled=True:确认后按池顺序换到下一个,agent_user/history/cursor 正确。"""
        self._save_team(
            pool=["acct-a", "acct-b"],
            quota_failover={"enabled": True, "confirm_cycles": 1},
            agent_user="acct-a",
        )
        r, m, t = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["state"], "quota")
        self.assertEqual(r["action"], "quota-switched:acct-b")
        self.assertEqual(m["agent_user"], "acct-b", "应换到池中下一个")
        self.assertEqual(m["quota_switch_count"], 1, "换号走独立计数")
        self.assertEqual(m["recovery_count"], 0, "quota 换号不得递增崩溃恢复计数")
        hist = m["agent_user_failover_history"]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0]["from"], "acct-a")
        self.assertEqual(hist[0]["to"], "acct-b")
        self.assertEqual(hist[0]["reason"], "quota_exhausted")
        self.assertTrue(hist[0]["ts"])
        self.assertEqual(t["agent_user_pool_cursor"], 1, "cursor 应为 next 在池中的下标")
        self.assertNotIn("blocked_reason", m, "换号成功应解除阻塞(成员进入重建)")
        self.assertEqual(m["last_observed_state"], "recovering")
        self.assertEqual(m["quota_hits"], 0, "换号后清零计数")

    def test_current_not_in_pool_switches_to_pool_head(self):
        """member 无 agent_user(current 空/不在池)→ 换到池首。"""
        self._save_team(pool=["acct-a", "acct-b"], quota_failover={"enabled": True, "confirm_cycles": 1})
        r, m, t = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-switched:acct-a")
        self.assertEqual(m["agent_user"], "acct-a")
        self.assertEqual(t["agent_user_pool_cursor"], 0)

    def test_switch_then_recurrence_switches_again(self):
        """换号后新账号仍耗尽:下一轮继续沿池走(acct-a → acct-b → acct-c)。"""
        self._save_team(
            pool=["acct-a", "acct-b", "acct-c"],
            quota_failover={"enabled": True, "confirm_cycles": 1},
            agent_user="acct-a",
        )
        r, m, _ = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-switched:acct-b")
        r, m, _ = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-switched:acct-c")
        self.assertEqual(m["agent_user"], "acct-c")
        self.assertEqual(m["quota_switch_count"], 2)


class QuotaFailoverNoSwitchTests(_IsolatedTestCase):
    """池空/池长1/wrap=False 到尾/达 max_switches → 不换号、保持阻塞。"""

    def test_pool_exhausted_wrap_false_blocks(self):
        """wrap=False 走到池尾:不换号、保持阻塞,action 标清是池耗尽。"""
        self._save_team(
            pool=["acct-a", "acct-b"],
            quota_failover={"enabled": True, "confirm_cycles": 1, "wrap": False},
            agent_user="acct-b",
        )
        r, m, t = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-pool-exhausted")
        self.assertEqual(m["agent_user"], "acct-b", "池尾不得换号")
        self.assertEqual(m["blocked_reason"], "quota", "保持阻塞,不静默降级")
        self.assertEqual(m["quota_switch_count"], 0)
        self.assertNotIn("agent_user_failover_history", m)

    def test_single_profile_pool_never_switches(self):
        """池长 1:无处可换,不换号、保持阻塞(避免原地空转)。"""
        self._save_team(
            pool=["acct-a"],
            quota_failover={"enabled": True, "confirm_cycles": 1},
            agent_user="acct-a",
        )
        r, m, _ = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-pool-empty")
        self.assertEqual(m["agent_user"], "acct-a")
        self.assertEqual(m["blocked_reason"], "quota")

    def test_empty_pool_never_switches(self):
        self._save_team(quota_failover={"enabled": True, "confirm_cycles": 1}, agent_user="acct-a")
        r, m, _ = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-pool-empty")
        self.assertEqual(m["agent_user"], "acct-a")
        self.assertEqual(m["blocked_reason"], "quota")

    def test_max_switches_reached_stops_switching(self):
        """达到 max_switches 上限:停止换号、保持阻塞告警,不无限重试。"""
        self._save_team(
            pool=["acct-a", "acct-b"],
            quota_failover={"enabled": True, "confirm_cycles": 1, "max_switches": 2},
            agent_user="acct-a",
            quota_switch_count=2,
        )
        r, m, _ = self._scan_alice(QUOTA_CAPTURE)
        self.assertEqual(r["action"], "quota-switch-limit")
        self.assertEqual(m["agent_user"], "acct-a", "达上限不得再换")
        self.assertEqual(m["quota_switch_count"], 2, "达上限不得再递增")
        self.assertEqual(m["blocked_reason"], "quota", "保持阻塞告警")
        self.assertNotIn("agent_user_failover_history", m)

    def test_switch_failure_keeps_blocked(self):
        """换号重建失败:保留阻塞,action 标清失败原因。"""
        self._save_team(
            pool=["acct-a", "acct-b"],
            quota_failover={"enabled": True, "confirm_cycles": 1},
            agent_user="acct-a",
        )
        r, m, _ = self._scan_alice(QUOTA_CAPTURE, spawn_result=(1, "", "boom"))
        self.assertIn("quota-switch-failed", r["action"])
        self.assertIn("boom", r["action"])
        self.assertEqual(m["agent_user"], "acct-b", "切换目标已记录")
        self.assertEqual(m["blocked_reason"], "quota", "失败保持阻塞")
        self.assertEqual(m["quota_switch_count"], 1, "重建失败也计一次切换(已选定新号)")


class QuotaFailoverCountIsolationTests(_IsolatedTestCase):
    """quota_switch_count 与 recovery_count 互不影响(§3.2 核心)。"""

    def test_three_switches_do_not_touch_recovery_count(self):
        """连换 3 次(acct-a→b→c→d):quota_switch_count=3 而 recovery_count=0,
        若混用计数,3 次就会撞上 monitor_max_recoveries=3 把换号能力误杀。"""
        self._save_team(
            pool=["acct-a", "acct-b", "acct-c", "acct-d"],
            quota_failover={"enabled": True, "confirm_cycles": 1},
            agent_user="acct-a",
        )
        for expected in ("acct-b", "acct-c", "acct-d"):
            r, m, _ = self._scan_alice(QUOTA_CAPTURE)
            self.assertEqual(r["action"], f"quota-switched:{expected}")
        self.assertEqual(m["quota_switch_count"], 3)
        self.assertEqual(m["recovery_count"], 0, "quota 换号绝不递增 recovery_count")

    def test_crash_recovery_does_not_touch_quota_switch_count(self):
        """崩溃恢复(reason 缺省 crash)只递增 recovery_count,不碰 quota_switch_count。"""
        self._save_team(pool=["acct-a", "acct-b"], agent_user="acct-a")
        # 模拟崩溃后死分支重建(不传 reason → crash)
        r, m, _ = self._scan_alice(IDLE_CAPTURE)  # 先确认 idle 路径不触发换号
        self.assertEqual(m["recovery_count"], 0)
        # 直接走一次真实 crash 恢复路径
        ok, msg = mcp._recover_and_send("team", "alice", "mcp_team")
        self.assertTrue(ok, msg)
        m = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(m["recovery_count"], 1)
        self.assertEqual(m["quota_switch_count"], 0, "crash 恢复不碰 quota 计数")
        self.assertEqual(m["agent_user"], "acct-a", "crash 恢复不换号")


if __name__ == "__main__":
    unittest.main()
