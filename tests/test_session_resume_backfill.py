"""
P4b 接线：_codex_session_backfill 首启真实 session 回填的原子持久化（mock 隔离）。

在临时 teams_data + mock tmux 隔离下，验证 codex 首启真实 session 回填的接线语义
（全程不触真实凭证 / ~/.codex / CLI）：

  1. 成员回填：marker 记录后 spawn 前刷新，discover 唯一真实 session → 原子写
     member["session_id"]（真实 uuid，非自造）+ session_backfill.resolved=True。
  2. managed leader 同步：leader 为 tmux 时回填同时原子写
     leader_checkpoint.session_id（"先 checkpoint 再 resume"）。
  3. direct leader checkpoint-only：direct leader 不写 session_id / 不写 checkpoint
     session_id（无管理终端可回填），保持 checkpoint 续跑边界。
  4. 已回填不重复：member 已有 session_id → 跳过扫描（零写入）。
  5. resolve 不可定位不写：discover 找到真实 id 但 resolve_codex_session 无法定位
     → 不落盘（belt-and-suspenders 禁回填不可解析 id）。
  6. 时间窗过期停止：时间窗已过仍未 discover → 移除 marker，monitor 停止逐 tick 扫描。
  7. 回填后恢复精确 resume：真实 session 回填落盘后 _session_resume_plan 返回
     {"kind": "resume", argv: codex resume <真实uuid>}（codex -C dir resume <id>）。

隔离：temp teams_data（data_layer.set_data_file）+ mock _tmux / _member_window_state /
_ensure_codex_mcp；CODEX_HOME 注入临时目录，真实 rollout 布局由 helper 写入；
claude 转录根 patch 到临时 home，绝不触真实 ~/.claude / ~/.codex。
"""

import json
import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common import session_resume

UUID_RE = r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"


def _write_codex_rollout(codex_home: Path, *, sid: str, cwd: str,
                         mtime: float, rid: str = "") -> Path:
    """真实布局 rollout：sessions/<y>/<m>/<d>/rollout-<ts>-<uuid>.jsonl。"""
    d = codex_home / "sessions" / "2026" / "08" / "10"
    d.mkdir(parents=True, exist_ok=True)
    rid = rid or sid
    r = d / f"rollout-2026-08-10T00-00-00-{rid}.jsonl"
    payload = {"session_id": sid, "id": rid, "cwd": cwd, "thread_source": "terminal"}
    r.write_text('{"type":"session_meta","payload":%s}\n' % json.dumps(payload),
                 encoding="utf-8")
    os.utime(r, (mtime, mtime))
    return r


class _IsolatedBackfillTestCase(unittest.TestCase):
    """temp teams_data 隔离 + CODEX_HOME/tmux mock 惯例。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "workspace"
        self.workspace.mkdir(exist_ok=True)
        self.codex_home = self.root / "codex_home"
        self.claude_home = self.root / "claude_home"
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
        self.old_codex_env = os.environ.get("CODEX_HOME")
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
        os.environ.pop(session_resume.RESUME_FLAG_ENV, None)  # 默认关闭
        os.environ["CODEX_HOME"] = str(self.codex_home)
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
                         ("CODEX_HOME", self.old_codex_env)):
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

    def _codex_member(self, name="bob", extra=None):
        m = {"role": "coder", "agent": "codex"}
        if extra:
            m.update(extra)
        return m

    def _set_resume(self, on=True):
        if on:
            os.environ[session_resume.RESUME_FLAG_ENV] = "1"
        else:
            os.environ.pop(session_resume.RESUME_FLAG_ENV, None)

    def _member(self, name):
        return mcp._load()["teams"]["team"]["members"][name]

    def _record_marker(self, team_name, member_name, spawn_ts):
        mcp._record_session_backfill_marker(team_name, member_name, spawn_ts=spawn_ts)

    def _run_backfill(self, team_name, member_name, window=300.0):
        mcp._codex_session_backfill(team_name, member_name, window_seconds=window)

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
# 1.5 spawn 全流程：首启标记 / 恢复 resume / 恢复上下文
# ============================================================

class TestBackfillSpawnFlow(_IsolatedBackfillTestCase):
    """经 _tmux_spawn_member 的端到端：首启记录标记，恢复 spawn 精确 resume。"""

    def test_first_spawn_records_marker_no_self_uuid(self):
        """codex 首启通过 spawn 流程记录标记，**不自造 uuid** 当真实 id。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(self.workspace))
        self.assertEqual(rc, 0, err)
        bob = self._member("bob")
        self.assertNotIn("session_id", bob)                  # 无自造 uuid
        bf = bob.get("session_backfill") or {}
        self.assertIn("spawn_ts", bf)                        # 记录 spawn 时间
        self.assertEqual(bf.get("cwd"), str(self.workspace))  # 记录工作目录
        self.assertEqual(bf.get("codex_home"), str(self.codex_home))  # 私有 CODEX_HOME

    def test_recovery_spawn_resumes_backfilled_real_id(self):
        """回填后再次 spawn（恢复）→ 精确 codex resume <真实id>，禁 --last。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        calls, fake_tmux = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(self.workspace))
        self.assertEqual(rc, 0, err)
        bf = self._member("bob")["session_backfill"]
        real = str(uuid.uuid4())
        _write_codex_rollout(self.codex_home, sid=real, cwd=str(self.workspace),
                             mtime=bf["spawn_ts"] + 1)
        # 第二次 spawn（恢复场景）：spawn 前刷新回填 → resume 真实 id
        calls2, fake_tmux2 = self._spawn_capture()
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux2):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(self.workspace))
        self.assertEqual(rc, 0, err)
        spawn = self._find_spawn(calls2)
        self.assertIsNotNone(spawn)
        self.assertIn("resume", spawn)
        self.assertIn(real, spawn)
        self.assertNotIn("--last", spawn)

    def test_recovery_context_omits_empty_codex_sid(self):
        """codex 未回填前恢复消息不渲染空 session_id；回填后渲染真实 id。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        ctx = mcp._build_recovery_context("team", "bob")
        self.assertNotIn("CLI 会话 session_id:", ctx)
        data = mcp._load()
        data["teams"]["team"]["members"]["bob"]["session_id"] = "somesid"
        mcp._save(data)
        ctx2 = mcp._build_recovery_context("team", "bob")
        self.assertIn("CLI 会话 session_id: somesid", ctx2)


# ============================================================
# 1. 成员回填：唯一真实 session 原子写 member.session_id
# ============================================================

class TestMemberBackfill(_IsolatedBackfillTestCase):
    def test_unique_real_session_backfilled_atomically(self):
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        spawn_ts = time.time()
        self._record_marker("team", "bob", spawn_ts)
        real = str(uuid.uuid4())
        _write_codex_rollout(self.codex_home, sid=real, cwd=str(self.workspace),
                             mtime=spawn_ts)
        self._run_backfill("team", "bob")
        m = self._member("bob")
        self.assertEqual(m["session_id"], real)          # 真实 uuid，非自造
        self.assertRegex(real, UUID_RE)
        self.assertTrue(m["session_backfill"]["resolved"])
        self.assertEqual(m["session_backfill"]["cwd"], str(self.workspace))

    def test_managed_codex_does_not_self_fabricate_uuid(self):
        """回填前 codex member 无 session_id；_member_session_id(for_agent=codex) 返回空。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        self.assertNotIn("session_id", self._member("bob"))
        # codex 首启不自造 uuid：真实 id 只能来自 discover
        sid = mcp._member_session_id("team", "bob", str(self.workspace), for_agent="codex")
        self.assertEqual(sid, "")
        self.assertNotIn("session_id", self._member("bob"))

    def test_flag_off_codex_still_generates_uuid_p4_behavior(self):
        """默认关闭：codex _member_session_id 保持 P4 行为（生成 uuid），零变化。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(False)
        sid = mcp._member_session_id("team", "bob", str(self.workspace), for_agent="codex")
        self.assertRegex(sid, UUID_RE)
        self.assertEqual(self._member("bob").get("session_id"), sid)

    def test_marker_recorded_only_for_codex_and_enabled(self):
        self._team(members={"bob": self._codex_member(),
                            "alice": {"role": "coder", "agent": "claude"}})
        self._set_resume(True)
        self._record_marker("team", "bob", time.time())
        self._record_marker("team", "alice", time.time())
        self.assertIn("session_backfill", self._member("bob"))
        self.assertNotIn("session_backfill", self._member("alice"))

    def test_marker_not_overwritten_after_backfill(self):
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        spawn_ts = time.time()
        self._record_marker("team", "bob", spawn_ts)
        real = str(uuid.uuid4())
        _write_codex_rollout(self.codex_home, sid=real, cwd=str(self.workspace),
                             mtime=spawn_ts)
        self._run_backfill("team", "bob")
        # 再次记录（后续恢复 spawn）→ 已回填不覆盖，时间窗仍指向首次会话
        mcp._record_session_backfill_marker("team", "bob", spawn_ts=time.time() + 10)
        m = self._member("bob")
        self.assertEqual(m["session_id"], real)
        self.assertEqual(m["session_backfill"]["spawn_ts"], spawn_ts)


# ============================================================
# 2. managed leader 同步写 leader_checkpoint.session_id
# ============================================================

class TestManagedLeaderBackfill(_IsolatedBackfillTestCase):
    def test_managed_leader_writes_checkpoint_session_id(self):
        self._team(members={}, leader="lead", leader_type="tmux")
        # leader 也是 codex agent（managed tmux leader）
        data = mcp._load()
        data["teams"]["team"]["members"]["lead"]["agent"] = "codex"
        mcp._save(data)
        self._set_resume(True)
        spawn_ts = time.time()
        self._record_marker("team", "lead", spawn_ts)
        real = str(uuid.uuid4())
        _write_codex_rollout(self.codex_home, sid=real, cwd=str(self.workspace),
                             mtime=spawn_ts)
        self._run_backfill("team", "lead")
        m = self._member("lead")
        self.assertEqual(m["session_id"], real)
        cp = mcp._load()["teams"]["team"].get("leader_checkpoint") or {}
        self.assertEqual(cp.get("session_id"), real)   # 先 checkpoint 再 resume


# ============================================================
# 3. direct leader checkpoint-only
# ============================================================

class TestDirectLeaderCheckpointOnly(_IsolatedBackfillTestCase):
    def test_direct_leader_never_backfills(self):
        self._team(members={}, leader="lead", leader_type="direct")
        data = mcp._load()
        data["teams"]["team"]["members"]["lead"]["agent"] = "codex"
        mcp._save(data)
        self._set_resume(True)
        spawn_ts = time.time()
        self._record_marker("team", "lead", spawn_ts)
        # marker 也不应记录（direct leader 无管理终端）
        self.assertNotIn("session_backfill", self._member("lead"))
        # 即使手动存在 rollout，backfill 也不回填
        real = str(uuid.uuid4())
        _write_codex_rollout(self.codex_home, sid=real, cwd=str(self.workspace),
                             mtime=spawn_ts)
        self._run_backfill("team", "lead")
        m = self._member("lead")
        self.assertNotIn("session_id", m)
        cp = mcp._load()["teams"]["team"].get("leader_checkpoint") or {}
        self.assertNotIn("session_id", cp)

    def test_direct_leader_marker_skipped(self):
        """direct leader 不记录 marker：_record_session_backfill_marker 直接 return。"""
        self._team(members={}, leader="lead", leader_type="direct")
        data = mcp._load()
        data["teams"]["team"]["members"]["lead"]["agent"] = "codex"
        mcp._save(data)
        self._set_resume(True)
        self._record_marker("team", "lead", time.time())
        self.assertNotIn("session_backfill", self._member("lead"))


# ============================================================
# 4. 已回填不重复 / resolve 不可定位不写 / 时间窗过期停止
# ============================================================

class TestBackfillGuards(_IsolatedBackfillTestCase):
    def test_already_backfilled_skips_scan(self):
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        sid = str(uuid.uuid4())
        data = mcp._load()
        data["teams"]["team"]["members"]["bob"]["session_id"] = sid
        mcp._save(data)
        spawn_ts = time.time()
        # 即使时间窗内有另一个 rollout，已回填真实 id → 跳过（零写入）
        _write_codex_rollout(self.codex_home, sid=str(uuid.uuid4()),
                             cwd=str(self.workspace), mtime=spawn_ts)
        self._run_backfill("team", "bob")
        self.assertEqual(self._member("bob")["session_id"], sid)

    def test_unresolvable_id_not_persisted(self):
        """discover 找到 id 但 resolve 无法定位（belt-and-suspenders）→ 不落盘。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        spawn_ts = time.time()
        self._record_marker("team", "bob", spawn_ts)
        # discover 读 session_meta payload 得到 sid；resolve 需扫描 rollout 文件名 uuid
        # 构造：payload.session_id 与文件名 uuid 不同 → discover ok 但 resolve 找不到精确
        # 匹配 → 不写（禁回填不可解析 id）。
        meta_sid = str(uuid.uuid4())
        file_sid = str(uuid.uuid4())
        d = self.codex_home / "sessions" / "2026" / "08" / "10"
        d.mkdir(parents=True, exist_ok=True)
        r = d / f"rollout-2026-08-10T00-00-00-{file_sid}.jsonl"
        r.write_text('{"type":"session_meta","payload":%s}\n' % json.dumps({
            "session_id": meta_sid, "id": file_sid, "cwd": str(self.workspace),
        }), encoding="utf-8")
        os.utime(r, (spawn_ts, spawn_ts))
        self._run_backfill("team", "bob")
        m = self._member("bob")
        self.assertNotIn("session_id", m)
        self.assertNotIn("resolved", m.get("session_backfill", {}))

    def test_time_window_expiry_stops_scan(self):
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        # 标记很早之前（时间窗已过），无匹配 rollout
        old_spawn = time.time() - 10_000.0
        self._record_marker("team", "bob", old_spawn)
        self.assertIn("session_backfill", self._member("bob"))
        self._run_backfill("team", "bob")
        # discover 无匹配且已超窗 → marker 被移除，monitor 停止逐 tick 扫描
        m = self._member("bob")
        self.assertNotIn("session_backfill", m)
        self.assertNotIn("session_id", m)

    def test_backfill_disabled_flag_noop(self):
        """feature flag 关闭 → backfill 不扫描、零写入（P4b 默认零变化）。"""
        self._team(members={"bob": self._codex_member()})
        self._set_resume(False)
        spawn_ts = time.time()
        self._record_marker("team", "bob", spawn_ts)
        _write_codex_rollout(self.codex_home, sid=str(uuid.uuid4()),
                             cwd=str(self.workspace), mtime=spawn_ts)
        self._run_backfill("team", "bob")
        m = self._member("bob")
        self.assertNotIn("session_id", m)
        self.assertNotIn("session_backfill", m)   # 关闭时 marker 也不记录


# ============================================================
# 5. 回填后恢复精确 resume（codex -C dir resume <真实uuid>）
# ============================================================

class TestBackfillResume(_IsolatedBackfillTestCase):
    def test_resume_plan_uses_real_session_after_backfill(self):
        self._team(members={"bob": self._codex_member()})
        self._set_resume(True)
        spawn_ts = time.time()
        self._record_marker("team", "bob", spawn_ts)
        real = str(uuid.uuid4())
        _write_codex_rollout(self.codex_home, sid=real, cwd=str(self.workspace),
                             mtime=spawn_ts)
        self._run_backfill("team", "bob")
        self.assertEqual(self._member("bob")["session_id"], real)
        plan = mcp._session_resume_plan("team", "bob", "codex", str(self.workspace))
        self.assertIsNotNone(plan)
        self.assertEqual(plan["kind"], "resume")
        self.assertEqual(plan["session_id"], real)
        self.assertEqual(plan["argv"], ["resume", real])  # codex -C dir resume <id>


if __name__ == "__main__":
    unittest.main()
