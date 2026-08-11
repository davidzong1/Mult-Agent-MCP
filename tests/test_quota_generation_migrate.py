"""
P2 事务式 quota 换号 + 终端 generation 隔离测试
====================================================

覆盖领导【实施 P2】交付点 + feature flag 语义：

  1. 原子迁移 —— spawn 新窗 {member}__g{N+1} 成功后原子提升 ACTIVE、旧窗记
     DRAINING+TTL、terminal_generation+1、agent_user 保持新账号、checkpoint 不动。
  2. 失败回滚 —— spawn/接续(发恢复上下文)失败：回滚 agent_user=previous，旧窗
     ACTIVE 保持、原 checkpoint 保持、不产生半迁移状态（不提升 generation）。
  3. 路由只走 ACTIVE —— _member_window_target 只解析 ACTIVE 窗口；ACTIVE 缺失
     返回 None(视为 dead)而非回退 DRAINING 旧窗；_scan_member_terminal 只捕获
     ACTIVE，DRAINING 旧窗的 quota 输出不计数。
  4. generation 回报门控 —— 旧 generation 回报被拒（不落 results.jsonl），
     ACTIVE generation 放行。
  5. 失败兜底 —— feature flag 默认关闭走 kill/recreate（旧行为不变）；开启但
     迁移失败兜底 kill/recreate 且 agent_user 保持 nxt。
  6. draining 有界 + TTL 回收 —— 超 TTL 的 DRAINING 旧窗被 kill + 记录剪除；
     ACTIVE 永不回收。
  7. crash 恢复重建 ACTIVE generation 窗口名（{member}__g{N}），不退回裸名。

隔离：mcp 模块全局临时覆盖 + data_layer.set_data_file（conftest 环境级隔离），
复用 test_quota_failover 的 _IsolatedTestCase 惯例，绝不触碰真实 teams_data。
"""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer

MEMBER_DONE = {
    "compact_path": "ctx.md", "compact_sent": False,
    "compact_error": "no tmux", "truncated": False, "agent_exited": False,
}


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类 + tmux mock 惯例（与 test_quota_failover 一致）。"""

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
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                        "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
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

    def _team(self, members=None, *, terminals_active=True, quota_failover=None):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": terminals_active,
            "leader": "lead",
            "leader_type": "direct",
            "members": {"lead": {"role": "leader", "agent": "claude"}},
        }
        for name, info in (members or {}).items():
            team["members"][name] = info
        if quota_failover:
            team["quota_failover"] = quota_failover
        mcp._save({"teams": {"team": team}})
        return mcp._load()["teams"]["team"]

    def _alice(self, extra=None):
        m = {"role": "coder", "agent": "claude"}
        if extra:
            m.update(extra)
        return m

    def _member(self) -> dict:
        return mcp._load()["teams"]["team"]["members"]["alice"]

    def _set_flag(self, on=True):
        data = mcp._load()
        data["teams"]["team"]["quota_failover"] = {"generation_migrate": on}
        mcp._save(data)


# ============================================================
# 1. feature flag 语义
# ============================================================

class TestFeatureFlag(_IsolatedTestCase):
    def test_flag_defaults_off(self):
        """默认关闭：既有 kill/recreate 换号行为一字不变。"""
        team = self._team(members={"alice": self._alice()})
        self.assertFalse(mcp._quota_generation_migrate_enabled(team))

    def test_flag_on_when_set(self):
        self._team(members={"alice": self._alice()})
        self._set_flag(True)
        self.assertTrue(mcp._quota_generation_migrate_enabled(mcp._load()["teams"]["team"]))

    def test_flag_ignores_non_dict(self):
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["quota_failover"] = "garbage"
        mcp._save(data)
        self.assertFalse(mcp._quota_generation_migrate_enabled(mcp._load()["teams"]["team"]))


# ============================================================
# 2. 原子迁移成功
# ============================================================

class TestAtomicMigration(_IsolatedTestCase):
    def test_success_promotes_active_and_drains_old(self):
        """迁移成功：新窗 g2 提升 ACTIVE，旧裸名窗 DRAINING+TTL，generation+1，
        agent_user 保持新账号，checkpoint 不动。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        mcp._save(data)
        # 成员任务 checkpoint（验证换号不触碰进度）
        from common import checkpoint as ckpt
        cp = ckpt.empty_checkpoint("t1", task="实现 P2", writer="alice")
        cp = ckpt.record_step_done(cp, "design")
        ok, err = ckpt.save_checkpoint(team_name="team", member_name="alice", cp=cp, writer="alice")
        self.assertTrue(ok, err)

        spawn_windows = []
        def fake_spawn(session, name, agent, team_dir, *, window_name=None, **kw):
            spawn_windows.append(window_name)
            return (0, "", "")

        with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=fake_spawn):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                ok_mig, msg = mcp._quota_generation_migrate("team", "alice", "mcp_team", "acct-a")

        self.assertTrue(ok_mig, msg)
        self.assertEqual(spawn_windows, ["alice__g2"])  # 新窗 generation 名

        m = self._member()
        self.assertEqual(m["terminal_generation"], 2)
        self.assertEqual(m["agent_user"], "acct-b")       # 保持新账号
        self.assertEqual(m["quota_hits"], 0)
        by_gen = {w["generation"]: w for w in m["terminal_windows"]}
        self.assertEqual(by_gen[2]["status"], "ACTIVE")
        self.assertEqual(by_gen[2]["name"], "alice__g2")
        self.assertEqual(by_gen[1]["status"], "DRAINING")  # legacy 裸名旧窗补记
        self.assertIn("ttl_until", by_gen[1])
        # checkpoint 不动
        cp2, errs = ckpt.load_checkpoint(team_name="team", member_name="alice")
        self.assertEqual(errs, [])
        self.assertEqual(cp2["completed_steps"], ["design"])
        self.assertEqual(cp2["writer"], "alice")

    def test_success_keeps_existing_active_gen_draining(self):
        """连续迁移（g2→g3）：已记录 g2 ACTIVE 被置 DRAINING，g3 提升。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice["agent_user"] = "acct-c"
        alice["terminal_generation"] = 2
        alice["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "DRAINING", "ttl_until": "x"},
            {"name": "alice__g2", "generation": 2, "status": "ACTIVE", "created_ts": "y"},
        ]
        mcp._save(data)

        spawn_windows = []
        def fake_spawn(session, name, agent, team_dir, *, window_name=None, **kw):
            spawn_windows.append(window_name)
            return (0, "", "")

        with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=fake_spawn):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                ok_mig, msg = mcp._quota_generation_migrate("team", "alice", "mcp_team", "acct-b")

        self.assertTrue(ok_mig, msg)
        self.assertEqual(spawn_windows, ["alice__g3"])
        m = self._member()
        self.assertEqual(m["terminal_generation"], 3)
        by_gen = {w["generation"]: w for w in m["terminal_windows"]}
        self.assertEqual(by_gen[2]["status"], "DRAINING")  # 旧 ACTIVE → DRAINING
        self.assertEqual(by_gen[3]["status"], "ACTIVE")
        self.assertEqual(by_gen[3]["name"], "alice__g3")


# ============================================================
# 3. 失败回滚
# ============================================================

class TestMigrationFailure(_IsolatedTestCase):
    def test_spawn_failure_rolls_back_agent_user(self):
        """spawn 失败：回滚 agent_user=previous，不提升 generation、不记录窗口、
        checkpoint 不动 —— 旧窗仍唯一权威。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        mcp._save(data)
        from common import checkpoint as ckpt
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.record_step_done(ckpt.empty_checkpoint("t1"), "design"),
        )

        with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(-1, "", "spawn boom")):
            ok_mig, msg = mcp._quota_generation_migrate("team", "alice", "mcp_team", "acct-a")

        self.assertFalse(ok_mig)
        self.assertIn("创建失败", msg)
        m = self._member()
        self.assertEqual(m["agent_user"], "acct-a")        # 回滚 previous
        self.assertEqual(m.get("terminal_generation", 1), 1)  # 未提升
        self.assertNotIn("terminal_windows", m)            # 未记录任何窗口
        cp, errs = ckpt.load_checkpoint(team_name="team", member_name="alice")
        self.assertEqual(errs, [])
        self.assertEqual(cp["completed_steps"], ["design"])  # checkpoint 不动

    def test_send_failure_rolls_back_and_kills_new_window(self):
        """接续(发恢复上下文)失败：清理新窗 + 回滚 agent_user，旧窗 ACTIVE 保持。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        mcp._save(data)

        kills = []
        def fake_tmux(cmd):
            if cmd and cmd[0] == "kill-window":
                kills.append(cmd[2])
            return (0, "", "")

        with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
            with mock.patch.object(mcp, "_send_keys", return_value=(-1, "send boom")):
                with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                    ok_mig, msg = mcp._quota_generation_migrate("team", "alice", "mcp_team", "acct-a")

        self.assertFalse(ok_mig)
        self.assertIn("发送失败", msg)
        self.assertTrue(any("alice__g2" in k for k in kills), kills)  # 清理新窗
        m = self._member()
        self.assertEqual(m["agent_user"], "acct-a")
        self.assertEqual(m.get("terminal_generation", 1), 1)
        self.assertNotIn("terminal_windows", m)

    def test_commit_failure_rolls_back(self):
        """commit(提升)失败：kill 新窗 + 回滚，旧 ACTIVE 保持。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        mcp._save(data)

        kills = []
        def fake_tmux(cmd):
            if cmd and cmd[0] == "kill-window":
                kills.append(cmd[2])
            return (0, "", "")

        with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                    # 提升失败：成员在 commit 前被移除
                    with mock.patch.object(mcp, "_promote_generation", return_value=(False, "member gone")):
                        ok_mig, msg = mcp._quota_generation_migrate("team", "alice", "mcp_team", "acct-a")

        self.assertFalse(ok_mig)
        self.assertIn("提升失败", msg)
        self.assertTrue(any("alice__g2" in k for k in kills))
        self.assertEqual(self._member()["agent_user"], "acct-a")


# ============================================================
# 4. 路由只走 ACTIVE + 旧窗不参与监控
# ============================================================

class TestActiveRouting(_IsolatedTestCase):
    def _g2_team(self):
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice["terminal_generation"] = 2
        alice["terminal_windows"] = [
            {"name": "alice", "generation": 1, "status": "DRAINING",
             "drained_ts": "x", "ttl_until": "y"},
            {"name": "alice__g2", "generation": 2, "status": "ACTIVE", "created_ts": "z"},
        ]
        mcp._save(data)

    def test_target_routes_to_active_generation(self):
        """_member_window_target 解析 ACTIVE g2，绝不回退到 DRAINING 旧窗/裸名。"""
        self._g2_team()
        records = [
            {"id": "@1", "name": "alice", "session_id": "s1", "session_created": "c1"},
            {"id": "@2", "name": "alice__g2", "session_id": "s1", "session_created": "c1"},
        ]
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_records", return_value=records):
                target = mcp._member_window_target("team", "alice")
        self.assertEqual(target, "@2")

    def test_target_missing_active_returns_none_not_old(self):
        """ACTIVE g2 缺失 → None（视为 dead），不回退 DRAINING 旧窗（旧窗不参与监控）。"""
        self._g2_team()
        records = [
            {"id": "@1", "name": "alice", "session_id": "s1", "session_created": "c1"},
        ]
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_records", return_value=records):
                target = mcp._member_window_target("team", "alice")
        self.assertIsNone(target)

    def test_scan_captures_only_active(self):
        """_scan_member_terminal 只捕获 ACTIVE g2；g1 的 quota 输出不计数/不触达。"""
        self._g2_team()
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice["last_task"] = "t"
        alice["last_task_completed"] = False
        mcp._save(data)
        records = [
            {"id": "@1", "name": "alice", "session_id": "s1", "session_created": "c1"},
            {"id": "@2", "name": "alice__g2", "session_id": "s1", "session_created": "c1"},
        ]
        captured = []

        def fake_capture(session, target, lines):
            captured.append(target)
            return (0, "busy output", "") if target == "@2" else (0, "quota output", "")

        def fake_classify(out, *, native_mode=""):
            return "quota" if "quota" in out else "busy"

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_records", return_value=records):
                with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                    with mock.patch.object(mcp, "_classify_terminal_output", side_effect=fake_classify):
                        with mock.patch.object(mcp, "_tmux", return_value=(0, "", "")):
                            r = mcp._scan_member_terminal("team", "alice")

        self.assertEqual(captured, ["@2"])          # 只捕获 ACTIVE
        self.assertEqual(r["state"], "busy")
        # 旧窗 g1 的 quota 输出绝不累加 quota_hits（旧窗排除 quota 计数）
        self.assertEqual(self._member().get("quota_hits", 0), 0)


# ============================================================
# 5. generation 回报门控
# ============================================================

class TestGenerationReportGate(_IsolatedTestCase):
    def test_stale_generation_report_rejected(self):
        """旧 generation 回报被门控拒绝，且不落 results.jsonl。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 3
        data["teams"]["team"]["members"]["alice"]["last_task"] = "t"
        data["teams"]["team"]["members"]["alice"]["last_task_completed"] = False
        mcp._save(data)

        r = mcp.member_report_result("team", "旧窗口结果", member_name="alice", generation=2)
        self.assertIn("回报门控", r)
        self.assertIn("g3", r)
        results_file = os.path.join(mcp._share_dir("team"), "results.jsonl")
        self.assertFalse(os.path.exists(results_file))  # 门控在任何写入前

    def test_current_generation_report_allowed(self):
        """ACTIVE generation 回报放行并写入。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["terminal_generation"] = 3
        data["teams"]["team"]["members"]["alice"]["last_task"] = "t"
        data["teams"]["team"]["members"]["alice"]["last_task_completed"] = False
        mcp._save(data)

        with mock.patch.object(mcp, "_notify_leader_of_report", return_value={}):
            with mock.patch.object(mcp, "_write_member_compressed_context", return_value="ctx.md"):
                with mock.patch.object(mcp, "_finalize_agent_completion", return_value=dict(MEMBER_DONE)):
                    r = mcp.member_report_result("team", "当前窗口结果", member_name="alice", generation=3)
        self.assertNotIn("回报门控", r)
        results_file = os.path.join(mcp._share_dir("team"), "results.jsonl")
        self.assertTrue(os.path.exists(results_file))
        with open(results_file, encoding="utf-8") as f:
            raw = f.read()
        self.assertIn("generation", raw)  # entry 带 generation
        self.assertIn('"generation": 3', raw)


# ============================================================
# 6. _recover_and_send：flag 关闭旧行为 / flag 开启迁移 / 失败兜底
# ============================================================

class TestRecoverAndSend(_IsolatedTestCase):
    def _base(self, flag=True, gen=1):
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice["agent_user"] = "acct-b"
        alice["last_task"] = "t"
        alice["last_task_completed"] = False
        if gen >= 2:
            alice["terminal_generation"] = gen
            alice["terminal_windows"] = [
                {"name": "alice__g2", "generation": 2, "status": "ACTIVE", "created_ts": "x"},
            ]
        data["teams"]["team"]["quota_failover"] = {"generation_migrate": flag}
        mcp._save(data)

    def _run(self, spawn_side_effect, kills=None, send_rc=(0, "")):
        with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=spawn_side_effect):
            with mock.patch.object(mcp, "_send_keys", return_value=send_rc):
                with mock.patch.object(mcp, "_tmux", side_effect=(kills if kills else (lambda cmd: (0, "", "")))):
                    with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
                        with mock.patch.object(mcp, "_write_claude_mcp", return_value="mcp.json"):
                            with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                with mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None):
                                    with mock.patch.object(mcp, "_build_recovery_context", return_value=""):
                                        with mock.patch.object(mcp, "_record_recovery_event", return_value=None):
                                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                                return mcp._recover_and_send(
                                                    "team", "alice", "mcp_team",
                                                    reason="quota_switch", previous_agent_user="acct-a",
                                                )

    def test_flag_off_keeps_kill_recreate_old_behavior(self):
        """flag 关闭：走 kill/recreate（旧行为），spawn 裸名窗口，agent_user 保持 nxt。"""
        self._base(flag=False)
        kills = []
        spawn_windows = []

        def fake_tmux(cmd):
            if cmd and cmd[0] == "kill-window":
                kills.append(cmd[2])
            return (0, "", "")

        def fake_spawn(session, name, agent, team_dir, *, window_name=None, **kw):
            spawn_windows.append(window_name)
            return (0, "", "")

        ok, msg = self._run(fake_spawn, kills=fake_tmux)
        self.assertTrue(ok, msg)
        self.assertEqual(spawn_windows, [None])           # 裸名（legacy 未迁移）
        self.assertTrue(kills)                             # 杀了旧窗
        self.assertEqual(self._member()["agent_user"], "acct-b")

    def test_flag_on_migration_success_no_kill(self):
        """flag 开启且迁移成功：不 kill 旧窗，spawn g2，原子提升 ACTIVE。"""
        self._base(flag=True)
        kills = []
        spawn_windows = []

        def fake_tmux(cmd):
            if cmd and cmd[0] == "kill-window":
                kills.append(cmd[2])
            return (0, "", "")

        def fake_spawn(session, name, agent, team_dir, *, window_name=None, **kw):
            spawn_windows.append(window_name)
            return (0, "", "")

        ok, msg = self._run(fake_spawn, kills=fake_tmux)
        self.assertTrue(ok, msg)
        self.assertEqual(spawn_windows, ["alice__g2"])    # 新窗 generation 名
        self.assertFalse(kills)                            # 未 kill 旧窗
        m = self._member()
        self.assertEqual(m["terminal_generation"], 2)
        by_gen = {w["generation"]: w for w in m["terminal_windows"]}
        self.assertEqual(by_gen[2]["status"], "ACTIVE")
        self.assertEqual(by_gen[1]["status"], "DRAINING")

    def test_flag_on_migration_failure_falls_back_to_kill_recreate(self):
        """flag 开启但迁移失败（g2 spawn 失败）：兜底 kill/recreate，agent_user 保持 nxt。"""
        self._base(flag=True)
        kills = []
        spawn_windows = []

        def fake_tmux(cmd):
            if cmd and cmd[0] == "kill-window":
                kills.append(cmd[2])
            return (0, "", "")

        def fake_spawn(session, name, agent, team_dir, *, window_name=None, **kw):
            spawn_windows.append(window_name)
            # g2 迁移窗口创建失败 → 触发兜底；兜底（裸名）成功
            if window_name and "__g" in window_name:
                return (-1, "", "mig spawn boom")
            return (0, "", "")

        ok, msg = self._run(fake_spawn, kills=fake_tmux)
        self.assertTrue(ok, msg)
        # 迁移尝试 g2（失败）+ 兜底裸名（成功）
        self.assertEqual(spawn_windows, ["alice__g2", None])
        self.assertTrue(kills)                             # 兜底 kill 了旧窗
        m = self._member()
        self.assertEqual(m["agent_user"], "acct-b")        # 兜底后保持 nxt
        self.assertEqual(m.get("terminal_generation", 1), 1)  # 未提升（迁移失败）


# ============================================================
# 7. draining 有界 + TTL 回收
# ============================================================

class TestDrainingReclaim(_IsolatedTestCase):
    def test_reclaim_kills_expired_draining_only(self):
        """超 TTL 的 DRAINING 被 kill+剪除；未过期 DRAINING 保留；ACTIVE 永不回收。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        now = datetime.datetime.now()
        past = (now - datetime.timedelta(seconds=3600)).isoformat()
        future = (now + datetime.timedelta(seconds=3600)).isoformat()
        alice["terminal_generation"] = 3
        alice["terminal_windows"] = [
            {"name": "alice__g1", "generation": 1, "status": "DRAINING", "ttl_until": past},
            {"name": "alice__g2", "generation": 2, "status": "DRAINING", "ttl_until": future},
            {"name": "alice__g3", "generation": 3, "status": "ACTIVE", "created_ts": future},
        ]
        mcp._save(data)

        kills = []
        def fake_tmux(cmd):
            if cmd and cmd[0] == "kill-window":
                kills.append(cmd[2])
            return (0, "", "")

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                n = mcp._reclaim_member_draining_windows("team", "alice")

        self.assertEqual(n, 1)                            # 只回收 g1
        self.assertTrue(any("alice__g1" in k for k in kills), kills)
        self.assertFalse(any("alice__g3" in k for k in kills))  # ACTIVE 不回收
        names = {w["name"] for w in self._member()["terminal_windows"]}
        self.assertNotIn("alice__g1", names)
        self.assertIn("alice__g2", names)
        self.assertIn("alice__g3", names)

    def test_reclaim_noop_without_windows(self):
        self._team(members={"alice": self._alice()})
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux", return_value=(0, "", "")):
                n = mcp._reclaim_member_draining_windows("team", "alice")
        self.assertEqual(n, 0)
        self.assertNotIn("terminal_windows", self._member())


# ============================================================
# 8. crash 恢复重建 ACTIVE generation 窗口名
# ============================================================

class TestCrashRecoveryGeneration(_IsolatedTestCase):
    def test_crash_recovery_spawns_active_generation_window(self):
        """已迁移成员 crash 恢复：重建 ACTIVE 窗口名 {member}__g{N}，不退回裸名。"""
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice["terminal_generation"] = 2
        alice["terminal_windows"] = [
            {"name": "alice__g2", "generation": 2, "status": "ACTIVE", "created_ts": "x"},
        ]
        alice["last_task"] = "t"
        alice["last_task_completed"] = False
        mcp._save(data)

        spawn_windows = []
        def fake_spawn(session, name, agent, team_dir, *, window_name=None, **kw):
            spawn_windows.append(window_name)
            return (0, "", "")

        with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=fake_spawn):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
                    with mock.patch.object(mcp, "_member_window_target", return_value="alice__g2"):
                        with mock.patch.object(mcp, "_write_claude_mcp", return_value="mcp.json"):
                            with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                with mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None):
                                    with mock.patch.object(mcp, "_record_recovery_event", return_value=None):
                                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                                            ok, msg = mcp._recover_and_send("team", "alice", "mcp_team")
        self.assertTrue(ok, msg)
        self.assertEqual(spawn_windows, ["alice__g2"])


if __name__ == "__main__":
    unittest.main()
