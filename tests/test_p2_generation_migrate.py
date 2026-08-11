"""
P2 隔离测试：事务式 quota 换号 + terminal generation（active/draining）
=======================================================================

覆盖验收点（P2 测试主责，隔离 home + mock tmux，零触生产）：
  1. feature flag 默认关闭 → _quota_generation_migrate_enabled False，
     quota 分支保持既有 kill/recreate 行为不变。
  2. 新窗 spawn 成功才提交 active：_quota_generation_migrate 成功 → 原子
     提升新窗 ACTIVE、旧窗 DRAINING+TTL、terminal_generation 递增。
  3. 失败回滚旧窗：spawn 失败 / 恢复上下文发送失败 / commit 失败 → kill
     新窗 + agent_user 回滚 previous，旧 ACTIVE 保留，不产生半迁移状态。
  4. draining 不参与监控/配额/回报：_member_window_target 只路由 ACTIVE
     窗口（{member}__g{N}），DRAINING 旧窗不被路由；ACTIVE 缺失视为 dead。
  5. draining 回收：_reclaim_member_draining_windows 仅 kill 超 TTL 的
     DRAINING 窗口，ACTIVE 永不回收；terminal_windows 有界。

隔离：临时 teams_data + mock tmux，复用 _IsolatedTestCase 模式。
"""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp


class _IsolatedTestCase(unittest.TestCase):
    """临时 teams_data + mock tmux 隔离基类（同 test_leader_checkpoint 模式）。"""

    def setUp(self):
        from common import data_layer

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
        from common import data_layer
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = None
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _team(self, *, generation_migrate=False, draining_ttl=None, windows=None):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "direct",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude", "agent_user": "acct-a"},
            },
        }
        if generation_migrate:
            team["quota_failover"] = {"generation_migrate": True}
        if draining_ttl is not None:
            team.setdefault("quota_failover", {})["draining_ttl_seconds"] = draining_ttl
        if windows:
            team["members"]["alice"]["terminal_windows"] = windows
        mcp._save({"teams": {"team": team}})
        return team

    def _member(self) -> dict:
        return mcp._load()["teams"]["team"]["members"]["alice"]

    def _mock_spawn_ok(self, side_effect=None):
        """mock _tmux_spawn_member 成功 (0,"","")，可注入 side_effect。"""
        return mock.patch.object(
            mcp, "_tmux_spawn_member",
            side_effect=side_effect or (lambda *a, **k: (0, "", "")),
        )

    def _mock_send_ok(self, side_effect=None):
        return mock.patch.object(
            mcp, "_send_keys",
            side_effect=side_effect or (lambda *a, **k: (0, "")),
        )

    def _mock_find_session(self):
        return mock.patch.object(mcp, "_find_any_session", return_value="mcp_team")

    def _mock_window_records(self, records):
        return mock.patch.object(mcp, "_tmux_window_records", return_value=records)


# ==================================================================
# 1. feature flag 默认关闭
# ==================================================================

class GenerationFeatureFlagTests(_IsolatedTestCase):
    """generation_migrate feature flag 默认关闭；显式配置开启。"""

    def test_flag_default_off(self):
        self._team()  # 无 quota_failover 配置
        team = mcp._load()["teams"]["team"]
        self.assertFalse(mcp._quota_generation_migrate_enabled(team))

    def test_flag_off_when_quota_failover_missing(self):
        self._team(generation_migrate=False)
        team = mcp._load()["teams"]["team"]
        self.assertFalse(mcp._quota_generation_migrate_enabled(team))

    def test_flag_on_when_explicit_true(self):
        self._team(generation_migrate=True)
        team = mcp._load()["teams"]["team"]
        self.assertTrue(mcp._quota_generation_migrate_enabled(team))

    def test_flag_off_when_explicit_false(self):
        self._team(generation_migrate=False)
        team = mcp._load()["teams"]["team"]
        team["quota_failover"] = {"generation_migrate": False}
        mcp._save({"teams": {"team": team}})
        self.assertFalse(mcp._quota_generation_migrate_enabled(team))

    def test_draining_ttl_bounds(self):
        self._team(draining_ttl=15)  # 低于下限 30
        team = mcp._load()["teams"]["team"]
        self.assertEqual(mcp._draining_ttl_seconds(team), 30)
        self._team(draining_ttl=999999)  # 高于上限 86400
        team = mcp._load()["teams"]["team"]
        self.assertEqual(mcp._draining_ttl_seconds(team), 86400)
        self._team(draining_ttl=120)
        team = mcp._load()["teams"]["team"]
        self.assertEqual(mcp._draining_ttl_seconds(team), 120)


# ==================================================================
# 2. 新窗 spawn 成功才提交 active
# ==================================================================

class GenerationCommitTests(_IsolatedTestCase):
    """成功路径：spawn 新窗 → 发送恢复上下文 → COMMIT 原子提升 ACTIVE。"""

    def test_migrate_success_promotes_active_and_drains_old(self):
        self._team(generation_migrate=True)
        # 旧 ACTIVE 窗口记录（generation=1）
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "ACTIVE", "agent_user": "acct-a"},
        ]
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 1
        mcp._save(data)

        with self._mock_find_session(), self._mock_spawn_ok(), self._mock_send_ok():
            ok, msg = mcp._quota_generation_migrate(
                "team", "alice", "mcp_team", previous_agent_user="acct-a",
            )
        self.assertTrue(ok, msg)

        member = self._member()
        self.assertEqual(member["terminal_generation"], 2)
        windows = member["terminal_windows"]
        self.assertEqual(len(windows), 2)
        # 旧窗 DRAINING+TTL
        old = next(w for w in windows if w["generation"] == 1)
        self.assertEqual(old["status"], "DRAINING")
        self.assertTrue(old.get("drained_ts"))
        self.assertTrue(old.get("ttl_until"))
        # 新窗 ACTIVE
        new = next(w for w in windows if w["generation"] == 2)
        self.assertEqual(new["status"], "ACTIVE")
        self.assertEqual(new["name"], "alice__g2")
        self.assertEqual(new["agent_user"], "acct-a")
        # agent_user 保持（调用方已设为 nxt）
        self.assertEqual(member["agent_user"], "acct-a")

    def test_migrate_commits_with_new_agent_user(self):
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"  # 已切新号
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "ACTIVE", "agent_user": "acct-a"},
        ]
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 1
        mcp._save(data)

        with self._mock_find_session(), self._mock_spawn_ok(), self._mock_send_ok():
            ok, msg = mcp._quota_generation_migrate(
                "team", "alice", "mcp_team", previous_agent_user="acct-a",
            )
        self.assertTrue(ok, msg)
        member = self._member()
        self.assertEqual(member["agent_user"], "acct-b")
        new = next(w for w in member["terminal_windows"] if w["generation"] == 2)
        self.assertEqual(new["agent_user"], "acct-b")

    def test_migrate_clears_quota_block(self):
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "ACTIVE"},
        ]
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 1
        data["teams"]["team"]["members"]["alice"]["blocked_reason"] = "quota"
        data["teams"]["team"]["members"]["alice"]["quota_hits"] = 5
        mcp._save(data)

        with self._mock_find_session(), self._mock_spawn_ok(), self._mock_send_ok():
            ok, msg = mcp._quota_generation_migrate(
                "team", "alice", "mcp_team", previous_agent_user="acct-a",
            )
        self.assertTrue(ok, msg)
        member = self._member()
        self.assertNotIn("blocked_reason", member)
        self.assertEqual(member["quota_hits"], 0)


# ==================================================================
# 3. 失败回滚旧窗
# ==================================================================

class GenerationRollbackTests(_IsolatedTestCase):
    """失败路径：spawn/发送/commit 任一失败 → kill 新窗 + agent_user 回滚，
    旧 ACTIVE 保留，不产生半迁移状态。"""

    def _setup_legacy(self, prev_agent_user="acct-a"):
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"  # 已切 nxt
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "ACTIVE", "agent_user": prev_agent_user},
        ]
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 1
        mcp._save(data)

    def test_spawn_failure_rolls_back_agent_user(self):
        self._setup_legacy()
        with self._mock_find_session():
            with mock.patch.object(
                mcp, "_tmux_spawn_member", return_value=(1, "", "boom")
            ) as spawn:
                ok, msg = mcp._quota_generation_migrate(
                    "team", "alice", "mcp_team", previous_agent_user="acct-a",
                )
        self.assertFalse(ok)
        self.assertIn("失败", msg)
        spawn.assert_called_once()
        member = self._member()
        # agent_user 回滚 previous，旧窗仍 ACTIVE，无新窗
        self.assertEqual(member["agent_user"], "acct-a")
        self.assertEqual(member["terminal_generation"], 1)
        self.assertEqual(member["terminal_windows"][0]["status"], "ACTIVE")

    def test_send_failure_rolls_back_and_kills_new_window(self):
        self._setup_legacy()
        killed = []
        with self._mock_find_session(), self._mock_spawn_ok():
            with mock.patch.object(mcp, "_send_keys", return_value=(1, "send fail")):
                with mock.patch.object(mcp, "_tmux") as tmux:
                    tmux.side_effect = lambda *a, **k: killed.append(a) or (0, "", "")
                    ok, msg = mcp._quota_generation_migrate(
                        "team", "alice", "mcp_team", previous_agent_user="acct-a",
                    )
        self.assertFalse(ok)
        self.assertIn("发送失败", msg)
        member = self._member()
        self.assertEqual(member["agent_user"], "acct-a")
        self.assertEqual(member["terminal_generation"], 1)
        # 新窗被 kill（kill-window alice__g2）
        self.assertTrue(any("alice__g2" in str(a) for a in killed))

    def test_commit_failure_rolls_back(self):
        self._setup_legacy()
        killed = []
        with self._mock_find_session(), self._mock_spawn_ok(), self._mock_send_ok():
            with mock.patch.object(mcp, "_promote_generation", return_value=(False, "commit fail")):
                with mock.patch.object(mcp, "_tmux") as tmux:
                    tmux.side_effect = lambda *a, **k: killed.append(a) or (0, "", "")
                    ok, msg = mcp._quota_generation_migrate(
                        "team", "alice", "mcp_team", previous_agent_user="acct-a",
                    )
        self.assertFalse(ok)
        self.assertIn("提升失败", msg)
        member = self._member()
        self.assertEqual(member["agent_user"], "acct-a")
        self.assertEqual(member["terminal_generation"], 1)
        self.assertTrue(any("alice__g2" in str(a) for a in killed))

    def test_spawn_failure_legacy_agent_user_untouched(self):
        """previous 为空时不回滚 agent_user（宁可不回滚也不写空号）。"""
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "ACTIVE", "agent_user": ""},
        ]
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 1
        mcp._save(data)
        with self._mock_find_session():
            with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(1, "", "boom")):
                ok, msg = mcp._quota_generation_migrate(
                    "team", "alice", "mcp_team", previous_agent_user="",
                )
        self.assertFalse(ok)
        # previous 空 → 不回滚，保持新账号（宁可不回滚也不写空号）
        self.assertEqual(self._member()["agent_user"], "acct-b")


# ==================================================================
# 4. draining 不参与监控/配额/回报（窗口路由只认 ACTIVE）
# ==================================================================

class DrainingRoutingTests(_IsolatedTestCase):
    """_member_window_target 迁移后只路由 ACTIVE 窗口，DRAINING 旧窗不被路由。"""

    def test_route_to_active_generation_window(self):
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 2
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "DRAINING"},
            {"name": "alice__g2", "generation": 2, "status": "ACTIVE"},
        ]
        mcp._save(data)
        records = [
            {"id": "w1", "name": "alice", "session_id": "s", "session_created": "1"},
            {"id": "w2", "name": "alice__g2", "session_id": "s", "session_created": "1"},
        ]
        with self._mock_find_session(), self._mock_window_records(records):
            with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                target = mcp._member_window_target("team", "alice")
        # 只路由 ACTIVE 新窗，绝不回退 DRAINING 旧窗
        self.assertEqual(target, "w2")

    def test_active_missing_returns_none_dead(self):
        """ACTIVE 窗口缺失（已迁移但 ACTIVE 不在 records）→ None（视为 dead，
        由恢复重建 ACTIVE，而不是把指令打进 DRAINING 旧窗）。"""
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 2
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "DRAINING"},
            {"name": "alice__g2", "generation": 2, "status": "ACTIVE"},
        ]
        mcp._save(data)
        records = [{"id": "w1", "name": "alice", "session_id": "s", "session_created": "1"}]
        with self._mock_find_session(), self._mock_window_records(records):
            target = mcp._member_window_target("team", "alice")
        self.assertIsNone(target)

    def test_legacy_member_uses_stored_bare_name(self):
        """未迁移成员（generation=1）走既有 stored_id/裸名解析，不受影响。"""
        self._team()
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["tmux_window_id"] = "w9"
        data["teams"]["team"]["members"]["alice"]["tmux_session"] = "mcp_team"
        data["teams"]["team"]["members"]["alice"]["tmux_session_id"] = "s"
        data["teams"]["team"]["members"]["alice"]["tmux_session_created"] = "1"
        mcp._save(data)
        records = [{"id": "w9", "name": "alice", "session_id": "s", "session_created": "1"}]
        with self._mock_find_session(), self._mock_window_records(records):
            target = mcp._member_window_target("team", "alice")
        self.assertEqual(target, "w9")

    def test_generation_normalization_bad_values(self):
        """_member_generation 对损坏/浮点值安全归一化（同 checkpoint_epoch）。

        契约：非 int/空 → 1；数值透传（负数不崩溃，_active_generation_window_name
        的 gen>=2 判定自然把负值当 legacy，不产生 __g 窗口名）。
        """
        self._team()
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        for bad in ("x", None, 2.0, "3"):
            member["terminal_generation"] = bad
            gen = mcp._member_generation(member)
            self.assertIsInstance(gen, int, repr(bad))
            self.assertGreaterEqual(gen, 1, repr(bad))
        # 数值透传不崩溃（-1 仍为 int，路由侧 gen>=2 判定安全）
        member["terminal_generation"] = -1
        self.assertIsInstance(mcp._member_generation(member), int)


# ==================================================================
# 4b. _recover_and_send quota_switch 端到端：flag 开启走迁移，关闭走 kill/recreate
# ==================================================================

class RecoverAndSendQuotaIntegrationTests(_IsolatedTestCase):
    """_recover_and_send(..., reason='quota_switch') 在 generation_migrate 开启时
    走 _quota_generation_migrate（不先 kill），flag 关闭时保持旧 kill/recreate。
    验证"新窗 spawn 成功才提交 active"的端到端接线。"""

    def _setup_migrated_team(self):
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "ACTIVE", "agent_user": "acct-a"},
        ]
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 1
        mcp._save(data)

    def test_flag_on_routes_to_migration_no_initial_kill(self):
        """flag 开启：_recover_and_send quota 分支调用 _quota_generation_migrate，
        且迁移成功前不 kill 旧窗（旧 ACTIVE 保留到 COMMIT 才 DRAINING）。"""
        self._setup_migrated_team()
        killed = []
        with self._mock_find_session(), self._mock_spawn_ok(), self._mock_send_ok():
            with mock.patch.object(mcp, "_tmux") as tmux:
                tmux.side_effect = lambda *a, **k: killed.append(a) or (0, "", "")
                with mock.patch.object(mcp.time, "sleep", return_value=None):
                    ok, msg = mcp._recover_and_send(
                        "team", "alice", "mcp_team", reason="quota_switch",
                        previous_agent_user="acct-a",
                    )
        self.assertTrue(ok, msg)
        # 迁移成功路径：COMMIT 后旧窗 DRAINING，terminal_generation=2
        member = self._member()
        self.assertEqual(member["terminal_generation"], 2)
        old = next(w for w in member["terminal_windows"] if w["generation"] == 1)
        self.assertEqual(old["status"], "DRAINING")
        new = next(w for w in member["terminal_windows"] if w["generation"] == 2)
        self.assertEqual(new["status"], "ACTIVE")

    def test_flag_off_keeps_legacy_kill_recreate(self):
        """flag 关闭：quota 分支先 kill 旧窗再重建（旧行为不变，无 __g 窗口）。"""
        self._team(generation_migrate=False)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        mcp._save(data)
        killed = []
        with self._mock_find_session():
            with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    with mock.patch.object(mcp, "_tmux") as tmux:
                        tmux.side_effect = lambda *a, **k: killed.append(a) or (0, "", "")
                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                            ok, msg = mcp._recover_and_send(
                                "team", "alice", "mcp_team", reason="quota_switch",
                                previous_agent_user="acct-a",
                            )
        self.assertTrue(ok, msg)
        # 旧行为先 kill（kill-window）
        self.assertTrue(any("kill-window" in str(a) for a in killed))
        member = self._member()
        self.assertNotIn("terminal_generation", member)  # 无迁移字段
        self.assertEqual(member["agent_user"], "acct-b")


# ==================================================================
# 5. draining 回收
# ==================================================================

class DrainingReclaimTests(_IsolatedTestCase):
    """_reclaim_member_draining_windows 仅回收超 TTL 的 DRAINING，ACTIVE 永不回收。"""

    def _make_team(self, windows):
        self._team(generation_migrate=True)
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_windows"] = windows
        mcp._save(data)

    def test_reclaims_expired_draining_only(self):
        now = datetime.datetime.now()
        expired = (now - datetime.timedelta(minutes=10)).isoformat()
        future = (now + datetime.timedelta(minutes=10)).isoformat()
        self._make_team([
            {"name": "alice", "generation": 1, "status": "DRAINING", "ttl_until": expired},
            {"name": "alice__g2", "generation": 2, "status": "ACTIVE", "ttl_until": future},
            {"name": "alice__g3", "generation": 3, "status": "DRAINING", "ttl_until": future},
        ])
        killed = []
        with self._mock_find_session():
            with mock.patch.object(mcp, "_tmux") as tmux:
                tmux.side_effect = lambda *a, **k: killed.append(a) or (0, "", "")
                n = mcp._reclaim_member_draining_windows("team", "alice")
        self.assertEqual(n, 1)  # 仅过期 DRAINING alice
        self.assertTrue(any("alice" in str(a) for a in killed))
        self.assertFalse(any("alice__g2" in str(a) for a in killed))
        member = self._member()
        names = [w["name"] for w in member["terminal_windows"]]
        self.assertIn("alice__g2", names)   # ACTIVE 保留
        self.assertIn("alice__g3", names)   # 未过期 DRAINING 保留
        self.assertNotIn("alice", names)    # 过期 DRAINING 已回收

    def test_active_never_reclaimed(self):
        now = datetime.datetime.now()
        past = (now - datetime.timedelta(hours=1)).isoformat()
        self._make_team([
            {"name": "alice__g5", "generation": 5, "status": "ACTIVE", "ttl_until": past},
        ])
        with self._mock_find_session():
            with mock.patch.object(mcp, "_tmux") as tmux:
                tmux.side_effect = lambda *a, **k: (0, "", "")
                n = mcp._reclaim_member_draining_windows("team", "alice")
        self.assertEqual(n, 0)  # ACTIVE 永不回收，即使 ttl 已过
        member = self._member()
        self.assertEqual(len(member["terminal_windows"]), 1)
        self.assertEqual(member["terminal_windows"][0]["status"], "ACTIVE")

    def test_no_windows_noop(self):
        self._make_team([])
        with self._mock_find_session():
            n = mcp._reclaim_member_draining_windows("team", "alice")
        self.assertEqual(n, 0)

    def test_missing_ttl_treated_expired(self):
        self._make_team([
            {"name": "alice", "generation": 1, "status": "DRAINING"},  # 无 ttl_until
        ])
        killed = []
        with self._mock_find_session():
            with mock.patch.object(mcp, "_tmux") as tmux:
                tmux.side_effect = lambda *a, **k: killed.append(a) or (0, "", "")
                n = mcp._reclaim_member_draining_windows("team", "alice")
        self.assertEqual(n, 1)
        self.assertEqual(self._member()["terminal_windows"], [])


if __name__ == "__main__":
    unittest.main()
