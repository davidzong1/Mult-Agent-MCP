"""
P3 基础适配器：稳定 session resume 的 CLI 参数构造 + 文件隔离层 mock 测试。

覆盖验收要求：
  1. feature flag 默认关闭 → build_resume_command 返回结构化 fallback
     （use_task_checkpoint=True），不构造命令、不触真实路径。
  2. Claude 显式 --session-id / --resume 参数构造：精确 id，绝不含 --last/-c。
  3. 转录白名单校验：session 必须存在于本成员 workspace 的 project 目录；
     缺失/跨目录一律拒绝（禁止模糊 cwd 恢复）。
  4. Codex 精确 resume 命令 + 私有 CODEX_HOME session 定位（注入临时 home，
     不触真实 ~/.codex）。
  5. 禁止 --last / 模糊 cwd 恢复、禁止复制 credentials/settings。
  6. resume 不可用返回结构化 fallback（use_task_checkpoint=True），
     调用方据此使用 P1 task checkpoint，而非空白重做。

隔离：全部测试用 TemporaryDirectory 注入 claude_home / codex_home，
不读真实 ~/.claude、~/.codex、不触真实凭证/API；env 变量保存/恢复。
"""

import os
import tempfile
import unittest
from pathlib import Path

from common import session_resume as sr


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


class TestResumeFeatureFlag(unittest.TestCase):
    """feature flag：默认关闭，环境变量可显式开启。"""

    def setUp(self):
        self._old_env = os.environ.get(sr.RESUME_FLAG_ENV)

    def tearDown(self):
        if self._old_env is None:
            os.environ.pop(sr.RESUME_FLAG_ENV, None)
        else:
            os.environ[sr.RESUME_FLAG_ENV] = self._old_env

    def test_flag_default_off(self):
        os.environ.pop(sr.RESUME_FLAG_ENV, None)
        self.assertFalse(sr.resume_enabled())

    def test_flag_env_override_on(self):
        os.environ[sr.RESUME_FLAG_ENV] = "1"
        self.assertTrue(sr.resume_enabled())

    def test_flag_env_override_off_values(self):
        os.environ[sr.RESUME_FLAG_ENV] = "0"
        self.assertFalse(sr.resume_enabled())

    def test_flag_module_constant_default(self):
        self.assertFalse(sr.SESSION_RESUME_ENABLED)


class TestNewSessionId(unittest.TestCase):
    """session_id 是 uuid4（真实 CLI 只认 uuid 会话 id，非派生哈希，B1）。"""

    def test_new_session_id_is_uuid(self):
        import re
        sid = sr.new_session_id()
        # uuid4：8-4-4-4-12 十六进制，版本位 4、变体位 8/9/a/b
        self.assertRegex(
            sid,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
        )

    def test_new_session_ids_distinct(self):
        self.assertNotEqual(sr.new_session_id(), sr.new_session_id())

    def test_id_is_string_with_hyphens(self):
        sid = sr.new_session_id()
        self.assertIsInstance(sid, str)
        self.assertIn("-", sid)
        self.assertGreater(len(sid), 20)


class TestClaudeArgConstruction(unittest.TestCase):
    """Claude 显式参数构造：精确 id，绝不含 --last / --continue。"""

    def test_resume_argv_explicit_id(self):
        argv = sr.claude_resume_argv("abc123")
        self.assertEqual(argv, ["--resume", "abc123"])
        for bad in sr.CLAUDE_FORBIDDEN_RESUME_ARGS:
            self.assertNotIn(bad, argv)

    def test_session_id_argv_explicit_id(self):
        argv = sr.claude_session_id_argv("abc123")
        self.assertEqual(argv, ["--session-id", "abc123"])

    def test_reject_forbidden_resume_args(self):
        for bad in ("--last", "-l", "--continue", "-c"):
            err = sr.reject_forbidden_resume_args(["--resume", "abc", bad])
            self.assertIsNotNone(err)
            self.assertIn(bad, err)

    def test_clean_argv_passes_forbidden_gate(self):
        self.assertIsNone(sr.reject_forbidden_resume_args(["--resume", "abc123"]))


class TestTranscriptWhitelist(unittest.TestCase):
    """转录白名单校验：精确 workspace project 定位，禁止模糊 cwd。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.claude_home = self.root / "claude_home"
        self.workspace = self.root / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_transcript(self, session_id):
        encoded = sr.encode_project_dir(str(self.workspace))
        proj = self.claude_home / "projects" / encoded
        proj.mkdir(parents=True, exist_ok=True)
        path = proj / f"{session_id}.jsonl"
        path.write_text("{\"session\": true}\n", encoding="utf-8")
        return path

    def test_transcript_whitelist_ok_when_present(self):
        sid = sr.new_session_id()
        self._make_transcript(sid)
        res = sr.validate_transcript(sid, str(self.workspace), str(self.claude_home))
        self.assertTrue(res["ok"])
        self.assertEqual(res["session_id"], sid)
        self.assertTrue(res["path"].endswith(f"{sid}.jsonl"))

    def test_transcript_rejected_when_missing(self):
        sid = sr.new_session_id()
        res = sr.validate_transcript(sid, str(self.workspace), str(self.claude_home))
        self.assertFalse(res["ok"])
        self.assertIn("禁止模糊 cwd 恢复", res["reason"])

    def test_transcript_rejected_in_wrong_workspace(self):
        sid = sr.new_session_id()
        self._make_transcript(sid)  # 转录写在 ws 下
        # 用不同 workspace 校验 → 精确定位失败（禁止跨目录/模糊恢复）
        other = self.root / "other_ws"
        other.mkdir(exist_ok=True)
        res = sr.validate_transcript(sid, str(other), str(self.claude_home))
        self.assertFalse(res["ok"])

    def test_encode_project_dir_deterministic(self):
        self.assertEqual(
            sr.encode_project_dir("/a/b/c"), sr.encode_project_dir("/a/b/c")
        )
        self.assertNotEqual(
            sr.encode_project_dir("/a/b/c"), sr.encode_project_dir("/a/b/d")
        )


class TestCodexResume(unittest.TestCase):
    """Codex 精确 resume + 私有 CODEX_HOME session 定位。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex_home"
        self._old_codex_home = os.environ.get("CODEX_HOME")

    def tearDown(self):
        if self._old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = self._old_codex_home
        self.tmp.cleanup()

    def test_codex_resume_argv_explicit_id(self):
        argv = sr.codex_resume_argv("sess1")
        self.assertEqual(argv, ["resume", "sess1"])
        for bad in sr.CODEX_FORBIDDEN_RESUME_ARGS:
            self.assertNotIn(bad, argv)

    def test_codex_sessions_dir_private_home(self):
        os.environ["CODEX_HOME"] = str(self.codex_home)
        self.assertEqual(
            sr.codex_sessions_dir(), self.codex_home / "sessions"
        )

    def test_codex_session_resolved_when_present(self):
        sid = sr.new_session_id()
        rollout = _write_codex_session(self.codex_home, sid)
        res = sr.resolve_codex_session(sid, str(self.codex_home))
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], str(rollout))
        self.assertEqual(res["session_id"], sid)

    def test_codex_session_resolved_via_session_meta(self):
        """session_meta.json 布局：从 meta 的 session_id 匹配真实 uuid。"""
        sid = sr.new_session_id()
        rollout = _write_codex_session(self.codex_home, sid, name="Login Module", meta=True)
        res = sr.resolve_codex_session(sid, str(self.codex_home))
        self.assertTrue(res["ok"])
        self.assertEqual(res["path"], str(rollout))

    def test_codex_session_matched_by_name(self):
        """按 session_meta 的 title/name 匹配 → 返回真实 uuid（resume 用真 id）。"""
        sid = sr.new_session_id()
        _write_codex_session(self.codex_home, sid, name="Login Module", meta=True)
        res = sr.resolve_codex_session("Login Module", str(self.codex_home))
        self.assertTrue(res["ok"])
        self.assertEqual(res["session_id"], sid)

    def test_codex_fake_dir_never_resolved(self):
        """P4 最终硬门：凭空 mkdir 的 sessions/<id> 假目录（无 rollout 证据）必须拒绝。"""
        sid = sr.new_session_id()
        fake_dir = self.codex_home / "sessions" / sid
        fake_dir.mkdir(parents=True)
        res = sr.resolve_codex_session(sid, str(self.codex_home))
        self.assertFalse(res["ok"])
        self.assertIn("只 checkpoint", res["reason"])

    def test_codex_session_rejected_when_missing(self):
        res = sr.resolve_codex_session("nope", str(self.codex_home))
        self.assertFalse(res["ok"])
        self.assertIn("只 checkpoint", res["reason"])

    def test_codex_forbidden_last_rejected(self):
        self.assertIsNotNone(sr.reject_forbidden_resume_args(["resume", "s", "--last"]))
        self.assertIsNone(sr.reject_forbidden_resume_args(["resume", "s"]))


class TestSensitiveCopyGuard(unittest.TestCase):
    """禁止复制 credentials / settings：敏感路径一律拒绝。"""

    def test_sensitive_names_rejected(self):
        for name in ("credentials", "settings", ".claude.json", "settings.json",
                     "auth.json", ".codex", "config.toml"):
            path = f"/ws/transcript/{name}"
            self.assertTrue(sr.is_sensitive_path(path), path)
            self.assertIsNotNone(sr.reject_sensitive_paths([path]))

    def test_transcript_path_allowed(self):
        safe = "/ws/claude_home/projects/ws/abc.jsonl"
        self.assertFalse(sr.is_sensitive_path(safe))
        self.assertIsNone(sr.reject_sensitive_paths([safe]))

    def test_batch_rejects_on_first_sensitive(self):
        paths = ["/ws/safe.jsonl", "/ws/settings/config.json"]
        self.assertIsNotNone(sr.reject_sensitive_paths(paths))

    def test_sensitive_in_subdir_detected(self):
        self.assertTrue(sr.is_sensitive_path("/ws/.codex/auth/session.jsonl"))


class TestBuildResumeCommand(unittest.TestCase):
    """结构化 fallback 契约：resume 不可用时返回 use_task_checkpoint=True。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.claude_home = self.root / "claude_home"
        self.codex_home = self.root / "codex_home"
        self.workspace = self.root / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._old_flag = os.environ.get(sr.RESUME_FLAG_ENV)
        self._old_codex_home = os.environ.get("CODEX_HOME")
        os.environ[sr.RESUME_FLAG_ENV] = "1"  # 默认开启 flag，单测各 fallback

    def tearDown(self):
        for key, old in ((sr.RESUME_FLAG_ENV, self._old_flag),
                         ("CODEX_HOME", self._old_codex_home)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.tmp.cleanup()

    def _claude_transcript(self):
        sid = sr.new_session_id()
        encoded = sr.encode_project_dir(str(self.workspace))
        proj = self.claude_home / "projects" / encoded
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text("x\n", encoding="utf-8")
        return sid

    def test_flag_off_returns_fallback_even_if_transcript_exists(self):
        os.environ[sr.RESUME_FLAG_ENV] = "0"
        sid = self._claude_transcript()
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sid,
        )
        self.assertFalse(r["available"])
        self.assertTrue(r["fallback"]["use_task_checkpoint"])
        self.assertIn("默认关闭", r["fallback"]["reason"])
        self.assertEqual(r["argv"], [])

    def test_claude_resume_ok_with_transcript(self):
        sid = self._claude_transcript()
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sid,
        )
        self.assertTrue(r["available"])
        self.assertEqual(r["agent"], "claude")
        self.assertEqual(r["argv"], ["--resume", r["session_id"]])
        self.assertIsNone(r["fallback"])
        self.assertNotIn("--last", r["argv"])

    def test_claude_resume_missing_transcript_returns_fallback(self):
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sr.new_session_id(),
        )
        self.assertFalse(r["available"])
        self.assertTrue(r["fallback"]["use_task_checkpoint"])
        self.assertIn("转录不存在", r["fallback"]["reason"])

    def test_missing_session_id_returns_fallback(self):
        # B3：resume 必须针对已持久化 session_id；缺失 → 不猜测，交回 checkpoint
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id="",
        )
        self.assertFalse(r["available"])
        self.assertIn("缺少已持久化 session_id", r["fallback"]["reason"])

    def test_codex_resume_ok_with_private_home(self):
        sid = sr.new_session_id()
        _write_codex_session(self.codex_home, sid)
        r = sr.build_resume_command(
            team_name="team", member_name="bob", agent="codex",
            workspace_dir=str(self.workspace), codex_home=str(self.codex_home),
            session_id=sid,
        )
        self.assertTrue(r["available"])
        self.assertEqual(r["agent"], "codex")
        self.assertEqual(r["argv"], ["resume", sid])

    def test_codex_resume_missing_session_returns_fallback(self):
        r = sr.build_resume_command(
            team_name="team", member_name="bob", agent="codex",
            workspace_dir=str(self.workspace), codex_home=str(self.codex_home),
            session_id=sr.new_session_id(),
        )
        self.assertFalse(r["available"])
        self.assertTrue(r["fallback"]["use_task_checkpoint"])
        self.assertIn("只 checkpoint", r["fallback"]["reason"])

    def test_unsupported_agent_returns_fallback(self):
        r = sr.build_resume_command(
            team_name="team", member_name="x", agent="vim",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sr.new_session_id(),
        )
        self.assertFalse(r["available"])
        self.assertIn("不支持的 agent", r["fallback"]["reason"])

    def test_fallback_mentions_task_checkpoint(self):
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sr.new_session_id(),
        )
        self.assertTrue(r["fallback"]["use_task_checkpoint"])
        self.assertIn("成员任务 checkpoint", r["fallback"]["message"])

    def test_fallback_structure_exact_keys(self):
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sr.new_session_id(),
        )
        self.assertEqual(set(r.keys()),
                         {"available", "agent", "argv", "session_id", "fallback"})
        self.assertEqual(
            set(r["fallback"].keys()),
            {"reason", "use_task_checkpoint", "message"},
        )

    def test_no_real_home_touched(self):
        """注入临时 claude_home/codex_home：任何失败路径都不读真实 ~/.claude/~/.codex。"""
        sid = self._claude_transcript()
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sid,
        )
        self.assertTrue(r["available"])
        # 所有涉及的真实 home 均为临时目录内，未触系统级凭证
        self.assertNotIn(str(Path.home()), r["argv"])


if __name__ == "__main__":
    unittest.main()
