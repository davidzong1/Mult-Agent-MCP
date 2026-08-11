"""
P4 接线：session_resume 接入生产 spawn/恢复链路的 mock 隔离测试。

在 tester 实机审计 B1/B2/B3/W1（真实 CLI 偏差阻断）硬修后，本文件验证生产接线
语义（全 mock，不触真实 CLI / 凭证）：

  1. 默认关闭 → spawn 命令零变化（无 --session-id/--resume）、零数据写入
     （member 无 session_id 字段）——P0-P3 行为一字不变。
  2. Claude：session_id 是 uuid4，初次 spawn 生成并持久化，命令带 --session-id
     <uuid>；再次 spawn / crash 恢复复用同一 uuid；转录存在时 --resume <uuid>。
  3. resume 不可用（无转录）→ 回落 --session-id 绑定 + P1 task checkpoint 续跑
     （恢复上下文含 checkpoint 段 + session_id 提示），不空白重做。
  4. P2 generation 跨凭证迁移：resume_disabled → 即使旧账号转录存在也绝不原生
     resume（只 checkpoint），新窗命令无 --resume/--session-id。
  5. Codex：无已持久化 session → 原样启动（无 argv）；有私有 session 目录 →
     精确 `codex -C dir resume <uuid>`。
  6. leader：任务启动时 checkpoint 记录 session_id；首启（launch_team_terminals
     旁路）绑定同一 uuid；复活（_revive_leader_terminal_locked）复用 --resume。
  7. 禁止 --last/-l/--continue/-c；reject_sensitive_paths 已接入（敏感 home →
     不构造 resume）。

隔离：temp teams_data（data_layer.set_data_file）+ mock _tmux / _member_window_state
/ _ensure_codex_mcp；claude 转录根由 setUp patch _member_claude_config_home 指向
临时 claude_home，绝不触真实 ~/.claude / ~/.codex。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common import session_resume

UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def _write_codex_session(codex_home: Path, sid: str, *, name: str = "", meta: bool = False) -> Path:
    """真实 Codex 布局 fixture（P4 最终硬门：只认 rollout-*.jsonl 证据）。

    - meta=False（默认，本机实测布局）:
        <home>/sessions/<year>/<month>/<day>/rollout-<ts>-<uuid>.jsonl
    - meta=True（部分版本）:
        <home>/sessions/<uuid>/session_meta.json + rollout-*.jsonl
    返回 rollout 路径。
    """
    if meta:
        d = codex_home / "sessions" / sid
        d.mkdir(parents=True, exist_ok=True)
        (d / "session_meta.json").write_text(
            '{"session_id": "%s", "title": "%s"}' % (sid, name or "My Session"),
            encoding="utf-8")
        rollout = d / "rollout-0001.jsonl"
    else:
        d = codex_home / "sessions" / "2026" / "08" / "10"
        d.mkdir(parents=True, exist_ok=True)
        rollout = d / f"rollout-2026-08-10T00-00-00-{sid}.jsonl"
    rollout.write_text('{"ok":true}\n', encoding="utf-8")
    return rollout


class _IsolatedResumeTestCase(unittest.TestCase):
    """temp teams_data 隔离 + tmux/claude_home mock 惯例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(exist_ok=True)
        self.claude_home = self.root / "claude_home"
        self.codex_home = self.root / "codex_home"
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
        self.old_resume_env = os.environ.get(session_resume.RESUME_FLAG_ENV)
        self.old_codex_home_env = os.environ.get("CODEX_HOME")
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
        # 默认关闭；测试内显式 _set_resume(True/False)
        os.environ.pop(session_resume.RESUME_FLAG_ENV, None)
        os.environ["CODEX_HOME"] = str(self.codex_home)
        # 关键：claude 转录根指向临时 claude_home，绝不触真实 ~/.claude
        self._claude_home_patcher = mock.patch.object(
            mcp, "_member_claude_config_home", return_value=str(self.claude_home))
        self._claude_home_patcher.start()
        self.addCleanup(self._claude_home_patcher.stop)

    def tearDown(self):
        self._claude_home_patcher.stop()
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, old in ((session_resume.RESUME_FLAG_ENV, self.old_resume_env),
                         ("CODEX_HOME", self.old_codex_home_env)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _team(self, members=None, *, leader="lead", leader_type="tmux"):
        context = self.root / "context"
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(self.workspace),
            "context_dir": str(context),
            "terminals_active": False,
            "leader": leader,
            "leader_type": leader_type,
            "members": {leader: {"role": "leader", "agent": "claude"}},
        }
        for name, info in (members or {}).items():
            team["members"][name] = info
        mcp._save({"teams": {"team": team}})
        return mcp._load()["teams"]["team"]

    def _alice(self, extra=None):
        m = {"role": "coder", "agent": "claude"}
        if extra:
            m.update(extra)
        return m

    def _member(self, name="alice"):
        return mcp._load()["teams"]["team"]["members"][name]

    def _set_resume(self, on=True):
        if on:
            os.environ[session_resume.RESUME_FLAG_ENV] = "1"
        else:
            os.environ.pop(session_resume.RESUME_FLAG_ENV, None)

    def _write_transcript(self, sid):
        encoded = session_resume.encode_project_dir(str(self.workspace))
        proj = self.claude_home / "projects" / encoded
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text("x\n", encoding="utf-8")

    def _spawn_capture(self):
        calls = []

        def fake_tmux(cmd):
            calls.append(cmd)
            return (0, "", "")

        return calls, fake_tmux

    @staticmethod
    def _find_spawn(calls):
        for c in calls:
            if c and c[0] in ("new-window", "new-session"):
                return c
        return None


# ============================================================
# 1. 默认关闭：行为一字不变（P0-P3 不受影响）
# ============================================================

class TestDefaultOff(_IsolatedResumeTestCase):
    def test_spawn_command_unchanged_no_session_data(self):
        self._team(members={"alice": self._alice()})
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertNotIn("--session-id", spawn)
        self.assertNotIn("--resume", spawn)
        self.assertNotIn("session_id", self._member())

    def test_recovery_off_sends_recovery_context_no_resume(self):
        """关闭时 _recover_and_send 恢复上下文照发（checkpoint 语义），命令零变化。"""
        self._team(members={"alice": self._alice()})
        calls, fake_tmux = self._spawn_capture()
        sent = []
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda s, t, txt: (sent.append((s, t, txt)) or (0, ""))):
                        ok, msg = mcp._recover_and_send("team", "alice", "mcp_team", reason="crash")
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertNotIn("--session-id", spawn)
        self.assertNotIn("--resume", spawn)
        self.assertTrue(any("终端恢复通知" in t for _, _, t in sent))
        self.assertNotIn("session_id", self._member())


# ============================================================
# 2. Claude：uuid4 绑定 / 复用 / 恢复
# ============================================================

class TestClaudeResume(_IsolatedResumeTestCase):
    def test_initial_spawn_binds_uuid(self):
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertIn("--session-id", spawn)
        sid = self._member()["session_id"]
        self.assertRegex(sid, UUID_RE)          # uuid4，非派生哈希（B1）
        self.assertIn(sid, spawn)

    def test_second_spawn_reuses_same_uuid(self):
        self._team(members={"alice": self._alice()})
        self._set_resume(True)

        def spawn_once():
            calls, fake_tmux = self._spawn_capture()
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
            return self._member()["session_id"]

        sid1 = spawn_once()
        sid2 = spawn_once()
        self.assertEqual(sid1, sid2)  # 持久化 uuid，换号/恢复稳定复用

    def test_recovery_resumes_same_uuid(self):
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
            with mock.patch.object(mcp, "_tmux", side_effect=lambda c: (0, "", "")):
                mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        sid = self._member()["session_id"]
        # 模拟 Claude 首启后转录已落盘 → 恢复用 --resume <uuid>
        self._write_transcript(sid)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls)
        self.assertIn("--resume", spawn)
        self.assertIn(sid, spawn)
        self.assertNotIn("--last", spawn)


# ============================================================
# 3. 恢复链：crash 恢复复用同一 id；resume 失败回落 checkpoint
# ============================================================

class TestRecoverAndSend(_IsolatedResumeTestCase):
    def test_crash_recovery_resumes_same_id(self):
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["session_id"] = sid
        mcp._save(data)
        self._write_transcript(sid)
        calls, fake_tmux = self._spawn_capture()
        sent = []
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda s, t, txt: (sent.append((s, t, txt)) or (0, ""))):
                        ok, msg = mcp._recover_and_send("team", "alice", "mcp_team", reason="crash")
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIn("--resume", spawn)
        self.assertIn(sid, spawn)
        self.assertTrue(any("终端恢复通知" in t for _, _, t in sent))

    def test_resume_unavailable_falls_back_checkpoint(self):
        """无转录 → 回落 --session-id 绑定 + checkpoint 续跑（不空白重做）。"""
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        from common import checkpoint as ckpt
        cp = ckpt.empty_checkpoint("t1", task="实现 P4", writer="alice")
        cp = ckpt.record_step_done(cp, "design")
        ok_cp, _ = ckpt.save_checkpoint(team_name="team", member_name="alice", cp=cp, writer="alice")
        self.assertTrue(ok_cp)
        calls, fake_tmux = self._spawn_capture()
        sent = []
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda s, t, txt: (sent.append((s, t, txt)) or (0, ""))):
                        ok, msg = mcp._recover_and_send("team", "alice", "mcp_team", reason="crash")
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIn("--session-id", spawn)     # bind，不 resume
        self.assertNotIn("--resume", spawn)
        ctx = next((t for _, _, t in sent if "终端恢复通知" in t), "")
        self.assertIn("成员任务 Checkpoint", ctx)   # P1 verify-then-continue 续跑依据
        self.assertIn("session_id:", ctx)          # P4 恢复提示

    def test_force_checkpoint_only_ignores_resume(self):
        """P2 generation 跨凭证：即使转录存在也强制不 resume（只 checkpoint）。"""
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["session_id"] = sid
        mcp._save(data)
        self._write_transcript(sid)
        plan = mcp._session_resume_plan("team", "alice", "claude", str(self.workspace),
                                        force_checkpoint_only=True)
        self.assertIsNone(plan)


# ============================================================
# 4. P2 generation 跨凭证迁移：只 checkpoint，不原生 resume
# ============================================================

class TestGenMigrateCheckpointOnly(_IsolatedResumeTestCase):
    def test_gen_migrate_never_resumes_cross_credential(self):
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        data = mcp._load()
        data["teams"]["team"]["quota_failover"] = {"generation_migrate": True}
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "acct-b"
        mcp._save(data)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["session_id"] = sid
        mcp._save(data)
        self._write_transcript(sid)  # 旧账号转录存在也绝不原生 resume
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                    ok_mig, msg = mcp._quota_generation_migrate("team", "alice", "mcp_team", "acct-a")
        self.assertTrue(ok_mig, msg)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertIn("alice__g2", spawn)          # 新窗 generation 名
        self.assertNotIn("--resume", spawn)        # 跨凭证禁止原生 resume
        self.assertNotIn("--session-id", spawn)    # 只 checkpoint


# ============================================================
# 5. Codex：无已持久化 session → 原样；有 session → 精确 resume
# ============================================================

class TestCodexResume(_IsolatedResumeTestCase):
    def test_codex_no_session_no_argv(self):
        self._team(members={"bob": {"role": "coder", "agent": "codex"}})
        self._set_resume(True)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertNotIn("resume", spawn)          # 无私有 session → 原样启动
        self.assertNotIn("--session-id", spawn)
        self.assertNotIn("--last", spawn)

    def test_codex_resume_when_session_exists(self):
        self._team(members={"bob": {"role": "coder", "agent": "codex"}})
        self._set_resume(True)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["bob"]["session_id"] = sid
        mcp._save(data)
        _write_codex_session(self.codex_home, sid)  # 真实布局：rollout-<uuid>.jsonl
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls)
        self.assertIn("resume", spawn)             # codex -C dir resume <uuid>
        self.assertIn(sid, spawn)

    def test_codex_fake_dir_falls_back_checkpoint(self):
        """P4 最终硬门：sessions/<sid> 假目录（无 rollout 证据）不得 resume，
        只 checkpoint fallback。"""
        self._team(members={"bob": {"role": "coder", "agent": "codex"}})
        self._set_resume(True)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["bob"]["session_id"] = sid
        mcp._save(data)
        fake = self.codex_home / "sessions" / sid
        fake.mkdir(parents=True)  # 假目录：无 rollout-*.jsonl 证据
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertNotIn("resume", spawn)          # 无实际 ID → 只 checkpoint
        self.assertNotIn("--last", spawn)


# ============================================================
# 6. leader：checkpoint 记录 / 首启绑定 / 复活复用
# ============================================================

class TestLeaderResume(_IsolatedResumeTestCase):
    def test_leader_task_start_records_session_id_in_checkpoint(self):
        self._team(members={"alice": self._alice()})
        team = mcp._load()["teams"]["team"]
        mcp._record_leader_task_start(team, "实现 P4", team_name="team")
        cp = team.get("leader_checkpoint") or {}
        sid = cp.get("session_id", "")
        self.assertRegex(sid, UUID_RE)
        self.assertEqual(self._member("lead").get("session_id"), sid)

    def test_leader_revival_resumes_same_session_id(self):
        self._team(members={"alice": self._alice()})
        data = mcp._load()
        data["teams"]["team"]["terminals_active"] = True
        data["teams"]["team"]["leader_last_task"] = "总任务"
        data["teams"]["team"]["leader_last_task_completed"] = False
        mcp._save(data)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["lead"]["session_id"] = sid
        mcp._save(data)
        self._write_transcript(sid)
        self._set_resume(True)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                    with mock.patch.object(mcp, "_leader_window_is_dead", return_value=True):
                        with mock.patch.object(mcp, "_leader_revival_allowed", return_value=True):
                            with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
                                with mock.patch.object(mcp, "_inject_claude_leader_prompt", return_value=(0, "")):
                                    ok, msg = mcp._revive_leader_terminal_locked("team", reason="test")
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertIn("--resume", spawn)
        self.assertIn(sid, spawn)

    def test_leader_first_launch_binds_session_id(self):
        self._set_resume(True)
        self._team(members={"alice": self._alice()})
        calls, fake_tmux = self._spawn_capture()
        fake_time = mock.MagicMock()
        fake_time.sleep.return_value = None
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                    with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                        with mock.patch.object(mcp, "_start_team_monitor", return_value=None):
                            with mock.patch.object(mcp, "_leader_terminal_restart_blocked", return_value=False):
                                with mock.patch.object(mcp, "time", fake_time):
                                    result = mcp.launch_team_terminals("team", "总任务")
        self.assertIn("已启动", result)
        # leader 首启 new-session 命令绑定稳定 uuid（4430 旁路已接线，G1）
        leader_spawn = next((c for c in calls if c and c[0] == "new-session"), None)
        self.assertIsNotNone(leader_spawn)
        data = mcp._load()["teams"]["team"]
        leader_sid = data["members"]["lead"].get("session_id", "")
        self.assertRegex(leader_sid, UUID_RE)
        self.assertIn("--session-id", leader_spawn)
        self.assertIn(leader_sid, leader_spawn)
        # leader checkpoint 记录同一 session_id（先 checkpoint 再 resume）
        self.assertEqual(data["leader_checkpoint"].get("session_id"), leader_sid)


# ============================================================
# 7. 安全闸：禁 --last；敏感路径不构造 resume
# ============================================================

class TestResumeGuards(_IsolatedResumeTestCase):
    def test_plan_argv_never_contains_forbidden_resume_flags(self):
        self._team(members={"alice": self._alice(),
                            "bob": {"role": "coder", "agent": "codex"}})
        self._set_resume(True)
        for kind_argv in (session_resume.claude_resume_argv("s"),
                          session_resume.claude_session_id_argv("s"),
                          session_resume.codex_resume_argv("s")):
            self.assertIsNone(session_resume.reject_forbidden_resume_args(kind_argv))
            for bad in ("--last", "-l", "--continue", "-c"):
                self.assertNotIn(bad, kind_argv)
        plan = mcp._session_resume_plan("team", "alice", "claude", str(self.workspace))
        self.assertIsNotNone(plan)
        self.assertIsNone(session_resume.reject_forbidden_resume_args(plan["argv"]))

    def test_sensitive_home_blocks_resume(self):
        """reject_sensitive_paths 接入：claude_home 落在敏感段 → 不构造 resume。"""
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["session_id"] = sid
        mcp._save(data)
        self._write_transcript(sid)
        with mock.patch.object(mcp, "_member_claude_config_home",
                               return_value="/tmp/evil/settings/claude_home"):
            plan = mcp._session_resume_plan("team", "alice", "claude", str(self.workspace))
        self.assertIsNone(plan)

    def test_session_id_and_resume_never_coexist(self):
        """Claude 首启 --session-id 与恢复 --resume 互斥：同一命令不可同时出现
        （除非 --fork-session，本项目不使用，见 P4 实机约束补充）。"""
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        # bind 路径（首启，无转录）
        with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
            with mock.patch.object(mcp, "_tmux", side_effect=lambda c: (0, "", "")):
                mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        sid = self._member()["session_id"]
        # resume 路径（转录已存在）
        self._write_transcript(sid)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertIn("--resume", spawn)
        self.assertNotIn("--session-id", spawn)  # resume 时不得同时带 --session-id


# ============================================================
# 8. 新约束互斥：--session-id 与 --resume 永不共存（无 --fork-session）
# ============================================================

class TestClaudeFlagMutualExclusivity(_IsolatedResumeTestCase):
    """Claude 首启只用 --session-id <uuid>，恢复只用 --resume <uuid>，两者互斥。

    领导实机约束：本项目不使用 --fork-session，任何 spawn 命令都不得同时出现
    两个会话标志。bind 场景（无转录）与 resume 场景（有转录）各验证一次。
    """

    def test_bind_and_resume_spawns_never_both_flags(self):
        self._team(members={"alice": self._alice()})
        self._set_resume(True)

        # bind：无转录 → 只有 --session-id，绝无 --resume
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        self.assertEqual(rc, 0, err)
        bind_spawn = self._find_spawn(calls)
        self.assertIsNotNone(bind_spawn)
        self.assertIn("--session-id", bind_spawn)
        self.assertNotIn("--resume", bind_spawn)

        # resume：转录落盘 → 只有 --resume，绝无 --session-id
        sid = self._member()["session_id"]
        self._write_transcript(sid)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        self.assertEqual(rc, 0, err)
        resume_spawn = self._find_spawn(calls)
        self.assertIsNotNone(resume_spawn)
        self.assertIn("--resume", resume_spawn)
        self.assertNotIn("--session-id", resume_spawn)

        # 结构保证：两个标志绝不在同一命令中并存
        for spawn in (bind_spawn, resume_spawn):
            self.assertFalse("--session-id" in spawn and "--resume" in spawn)


if __name__ == "__main__":
    unittest.main()
