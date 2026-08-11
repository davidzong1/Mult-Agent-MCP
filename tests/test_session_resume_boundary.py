"""
P3 边界补测：session_resume 适配器的环境变量真值变体、路径编码跨平台、
成功路径契约。所有用例注入临时 home，不触真实凭证/配置。

与 tests/test_session_resume_adapter.py（coder 34 条）互补，只补其未覆盖的
边界面，不重复已有验收点。

⚠️ P2 active/draining generation 缺口：本文件不为其造测试——当前树
（mult_agent_mcp.py / common/tmux_utils.py / common/）无 generation/draining/
窗口生命周期状态机实现，凭空测试会锁死假 API。P2 验收点（新窗 spawn 成功
才提交 active、失败回滚旧窗、draining 不参与监控/配额/回报）待 coder/refactor
交付实际 API 后再补，此处仅以文档形式记录缺口。
"""

import os
import tempfile
import unittest
from pathlib import Path

from common import session_resume as sr


def _write_codex_session(codex_home: Path, sid: str, *, name: str = "", meta: bool = False) -> Path:
    """真实 Codex 布局 fixture（只认 rollout-*.jsonl 证据，P4 最终硬门）。"""
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


class TestResumeFlagTruthiness(unittest.TestCase):
    """resume_enabled 真值完备性：实现接受 "1"/"true"/"True"，其余为关。"""

    def setUp(self):
        self._old = os.environ.get(sr.RESUME_FLAG_ENV)

    def tearDown(self):
        if self._old is None:
            os.environ.pop(sr.RESUME_FLAG_ENV, None)
        else:
            os.environ[sr.RESUME_FLAG_ENV] = self._old

    def test_true_variants_enable(self):
        for v in ("1", "true", "True"):
            os.environ[sr.RESUME_FLAG_ENV] = v
            self.assertTrue(sr.resume_enabled(), v)

    def test_falsy_and_unknown_disable(self):
        for v in ("0", "false", "False", "2", "", "yes"):
            os.environ[sr.RESUME_FLAG_ENV] = v
            self.assertFalse(sr.resume_enabled(), v)

    def test_whitespace_trimmed_or_disabled(self):
        # 实现用 `in ("1","true","True")` 精确匹配，带空格应判关（防误开）
        os.environ[sr.RESUME_FLAG_ENV] = " 1 "
        self.assertFalse(sr.resume_enabled())


class TestProjectDirEncoding(unittest.TestCase):
    """encode_project_dir 跨平台与边界：保留真实目录首尾 '-'（Claude 实际转录目录
    为 -a-b-c 形态；之前 .strip("-") 剥掉前导 '-' 导致 --resume 找不到转录，W1）。"""

    def test_absolute_posix(self):
        # 绝对路径首段是根 → 前导 '-'
        self.assertEqual(sr.encode_project_dir("/a/b/c"), "-a-b-c")

    def test_windows_drive(self):
        self.assertEqual(sr.encode_project_dir("C:\\Users\\x"), "C-Users-x")

    def test_trailing_slash_kept(self):
        self.assertEqual(sr.encode_project_dir("/a/b/"), "-a-b-")

    def test_root_only(self):
        self.assertEqual(sr.encode_project_dir("/"), "-")

    def test_dot_dirs_normalized(self):
        # 不做 realpath，仅按段编码；. 段保留（不跨目录猜测，不解析符号链接）
        self.assertEqual(sr.encode_project_dir("/a/./b"), "-a-.-b")


class TestResumeSuccessContract(unittest.TestCase):
    """成功路径契约：available=True 时 argv/session_id/agent 一致且 fallback=None。

    这是 fallback 的镜像面——P3 验收"resume 可用时返回精确命令"的反向断言，
    确保调用方区分"可用/不可用"不靠字符串嗅探而是结构化字段。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.claude_home = self.root / "claude_home"
        self.codex_home = self.root / "codex_home"
        self.workspace = self.root / "ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._old_flag = os.environ.get(sr.RESUME_FLAG_ENV)
        self._old_codex = os.environ.get("CODEX_HOME")
        os.environ[sr.RESUME_FLAG_ENV] = "1"

    def tearDown(self):
        for key, old in ((sr.RESUME_FLAG_ENV, self._old_flag),
                         ("CODEX_HOME", self._old_codex)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.tmp.cleanup()

    def test_claude_success_contract_consistent(self):
        sid = sr.new_session_id()
        encoded = sr.encode_project_dir(str(self.workspace))
        proj = self.claude_home / "projects" / encoded
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text("x\n", encoding="utf-8")
        r = sr.build_resume_command(
            team_name="team", member_name="alice", agent="claude",
            workspace_dir=str(self.workspace), claude_home=str(self.claude_home),
            session_id=sid,
        )
        self.assertTrue(r["available"])
        self.assertIsNone(r["fallback"])
        self.assertEqual(r["session_id"], sid)
        self.assertEqual(r["argv"], ["--resume", sid])
        self.assertEqual(r["agent"], "claude")

    def test_codex_success_contract_consistent(self):
        sid = sr.new_session_id()
        _write_codex_session(self.codex_home, sid)
        r = sr.build_resume_command(
            team_name="team", member_name="bob", agent="codex",
            workspace_dir=str(self.workspace), codex_home=str(self.codex_home),
            session_id=sid,
        )
        self.assertTrue(r["available"])
        self.assertIsNone(r["fallback"])
        self.assertEqual(r["session_id"], sid)
        self.assertEqual(r["argv"], ["resume", sid])
        self.assertEqual(r["agent"], "codex")

    def test_argv_never_contains_forbidden_resume_flags_on_success(self):
        sid = sr.new_session_id()
        _write_codex_session(self.codex_home, sid)
        r = sr.build_resume_command(
            team_name="team", member_name="carol", agent="codex",
            workspace_dir=str(self.workspace), codex_home=str(self.codex_home),
            session_id=sid,
        )
        self.assertTrue(r["available"])
        self.assertIsNone(sr.reject_forbidden_resume_args(r["argv"]))
        for bad in ("--last", "-l", "--continue", "-c"):
            self.assertNotIn(bad, r["argv"])

    def test_new_session_id_distinct_across_calls(self):
        # session_id 是 uuid4，每次生成不同；"稳定"由持久化保证，不是派生
        self.assertNotEqual(sr.new_session_id(), sr.new_session_id())


if __name__ == "__main__":
    unittest.main()
