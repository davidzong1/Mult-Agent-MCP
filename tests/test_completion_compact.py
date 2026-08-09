"""
测试完成收尾机制：member_report_result 与 leader_mark_task_complete 的
压缩上下文生成 + /compact 强制执行。

覆盖:
  A. member 完成 - agent 活着/退出, 压缩上下文 <=2000 token
  B. leader 完成 - agent 活着/退出, 压缩上下文 + /compact
  C. 记录先于 /compact 命令发送
  D. /compact 发送失败 + monitor idle 自动完成入口

遵循现有 test_mult_agent_mcp.py 模式: unittest + mock, 隔离全局状态。
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class TestCompletionCompactMechanism(unittest.TestCase):
    """完成收尾机制测试"""

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
        self.old_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux_window_exists": mcp._tmux_window_exists,
            "_tmux": mcp._tmux,
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
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _setup_team(self, member_name="alice", last_task_completed=False,
                    leader_type="tmux", with_leader_terminal=False):
        """创建团队: lead(tmux leader) + alice(coder member)"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        members = {
            "lead": {"role": "leader", "agent": "codex"},
            member_name: {
                "role": "coder", "agent": "claude",
                "last_task": "完成登录模块", "last_context": "需要实现OAuth登录",
                "last_task_completed": last_task_completed,
                "tmux_window_id": "@7", "tmux_session": "mcp_team",
                "tmux_session_id": "$1", "tmux_session_created": "1000",
            },
        }
        if with_leader_terminal:
            members["lead"]["tmux_window_id"] = "@1"
            members["lead"]["tmux_session"] = "mcp_team"
            members["lead"]["tmux_session_id"] = "$1"
            members["lead"]["tmux_session_created"] = "1000"
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": leader_type,
                    "leader_last_task": "总任务：完成登录系统",
                    "leader_last_task_completed": False,
                    "members": members,
                }
            }
        })
        return workspace, context

    @staticmethod
    def _alive_tmux():
        """返回模拟存活终端的 fake_tmux"""
        def fake(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\talice", ""
            if cmd[0] == "list-sessions":
                return 0, "mcp_team\n", ""
            return 0, "", ""
        return fake

    # ==================================================================
    # member_report_result
    # ==================================================================

    def test_member_compressed_context_size_le_2000_chars(self):
        """压缩上下文文件 ≤2000 字符，长文本被截断"""
        _, ctx = self._setup_team()
        # 注入长任务/上下文到成员数据
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice["last_task"] = ("任务描述" * 80)
        alice["last_context"] = ("上下文背景" * 80)
        mcp._save(data)
        result = mcp.member_report_result("team",
            ("完成了很多功能" * 40), member_name="alice")
        self.assertIn("压缩上下文", result)
        entry = json.loads(
            (ctx / "results.jsonl").read_text("utf-8").splitlines()[-1])
        content = (ctx / entry["compressed_context_path"]).read_text("utf-8")
        self.assertLessEqual(len(content), 2000)
        self.assertIn("...", content)  # 截断标记

    def test_member_compact_sent_when_terminal_alive(self):
        """终端存活 → /compact 已注入，结果消息含 📦 标记"""
        self._setup_team()
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                    result = mcp.member_report_result("team", "done", member_name="alice")
        self.assertIn("📦 已向成员终端注入 /compact", result)

    def test_member_no_compact_when_terminal_dead(self):
        """终端已死 → 仍生成压缩上下文，但不发 /compact"""
        self._setup_team()
        data = mcp._load()
        alice = data["teams"]["team"]["members"]["alice"]
        alice.pop("tmux_window_id", None)
        alice.pop("tmux_session", None)
        mcp._save(data)
        with mock.patch.object(mcp, "_find_any_session", return_value=None):
            result = mcp.member_report_result("team", "done", member_name="alice")
        self.assertIn("压缩上下文", result)
        self.assertNotIn("📦", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(member["last_task_completed"])

    def test_member_record_before_compact(self):
        """压缩上下文写入先于 /compact 注入"""
        self._setup_team()
        events = []
        orig = mcp._write_member_compressed_context
        def track_write(*a, **kw):
            events.append("write"); return orig(*a, **kw)
        def fake_send(session, win, text, **kw):
            if "/compact" in text: events.append("compact"); return 0, ""
        def fake_confirm(session, win, **kw):
            events.append("confirm"); return 0, ""
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_write_member_compressed_context", side_effect=track_write):
                with mock.patch.object(mcp, "_send_keys", side_effect=fake_send):
                    with mock.patch.object(mcp, "_confirm_prompt_submission", side_effect=fake_confirm):
                        mcp.member_report_result("team", "done", member_name="alice")
        wi = events.index("write")
        self.assertLess(wi, events.index("compact"), "先写记录再发 /compact")

    def test_member_compact_failure_still_writes(self):
        """/compact 发送失败 → 压缩上下文仍生成，任务仍标记完成"""
        self._setup_team()
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", return_value=(1, "err")):
                result = mcp.member_report_result("team", "done", member_name="alice")
        self.assertIn("压缩上下文", result)
        self.assertTrue(mcp._load()["teams"]["team"]["members"]["alice"]["last_task_completed"])

    def test_member_explicit_compressed_context(self):
        """成员提供 compressed_context → 文件包含用户提供的值"""
        self._setup_team()
        mcp.member_report_result("team", "长结果" * 50, member_name="alice",
                                 compressed_context="自定义摘要-OAuth完成")
        ctx = Path(mcp._load()["teams"]["team"]["context_dir"])
        entry = json.loads((ctx / "results.jsonl").read_text("utf-8").splitlines()[-1])
        content = (ctx / entry["compressed_context_path"]).read_text("utf-8")
        self.assertIn("自定义摘要-OAuth完成", content)

    # ==================================================================
    # leader_mark_task_complete
    # ==================================================================

    def test_leader_compressed_context_and_compact(self):
        """leader 完成 → 生成压缩上下文 + 注入 /compact"""
        _, ctx = self._setup_team(last_task_completed=True, with_leader_terminal=True)
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                    result = mcp.leader_mark_task_complete("team", summary="全部完成")
        self.assertIn("已标记完成", result)
        self.assertIn("📦 已向 leader 终端注入 /compact", result)
        self.assertIn("压缩上下文", result)
        # 验证 leader 压缩上下文文件存在
        mctx = ctx / "member_contexts"
        files = list(mctx.glob("*_leader.md"))
        self.assertTrue(len(files) > 0, "leader 压缩上下文文件应存在")

    def test_leader_no_compact_when_terminal_dead(self):
        """leader 终端已死 → 仍生成压缩上下文，但不发 /compact"""
        self._setup_team(last_task_completed=True)  # no leader terminal
        with mock.patch.object(mcp, "_tmux", return_value=(1, "", "no session")):
            result = mcp.leader_mark_task_complete("team", summary="done")
        self.assertIn("已标记完成", result)
        self.assertNotIn("📦", result)

    def test_leader_record_before_compact(self):
        """leader 压缩上下文先于 /compact"""
        self._setup_team(last_task_completed=True, with_leader_terminal=True)
        events = []
        def fake_send(session, win, text, **kw):
            if "/compact" in text: events.append("compact"); return 0, ""
        def fake_confirm(session, win, **kw):
            events.append("confirm"); return 0, ""
        # track results.jsonl write → write_record
        orig_open = open
        def track_open(path, mode, *a, **kw):
            f = orig_open(path, mode, *a, **kw)
            if "results.jsonl" in str(path) and "a" in mode: events.append("write_record")
            return f
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", side_effect=fake_send):
                with mock.patch.object(mcp, "_confirm_prompt_submission", side_effect=fake_confirm):
                    with mock.patch("builtins.open", side_effect=track_open):
                        mcp.leader_mark_task_complete("team", summary="done")
        wi = events.index("write_record")
        self.assertLess(wi, events.index("compact"), "先写记录再发 /compact")

    def test_leader_compact_failure_still_completes(self):
        """/compact 失败 → 仍标记完成，记录写入"""
        self._setup_team(last_task_completed=True, with_leader_terminal=True)
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", return_value=(1, "err")):
                result = mcp.leader_mark_task_complete("team", summary="done")
        self.assertIn("已标记完成", result)
        self.assertIn("注入失败", result)
        data = mcp._load()
        self.assertTrue(data["teams"]["team"]["leader_last_task_completed"])

    def test_leader_active_when_member_unfinished(self):
        """成员未完成时 leader 标记完成 → 仍 compact，但提示仍有未完成任务"""
        self._setup_team(last_task_completed=False, with_leader_terminal=True)
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                    result = mcp.leader_mark_task_complete("team", summary="leader done")
        self.assertIn("仍检测到未完成成员任务", result)
        self.assertIn("📦 已向 leader 终端注入 /compact", result)

    # ==================================================================
    # monitor idle 自动完成入口
    # ==================================================================

    def test_monitor_idle_triggers_compact(self):
        """leader_monitor_members 发现空闲成员 → 标记完成 → 不应遗漏 compact"""
        self._setup_team()
        # 模拟 monitor 发现成员 idle（cli 已返回 prompt）
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["last_task"] = "done task"
        data["teams"]["team"]["members"]["alice"]["last_task_completed"] = False
        mcp._save(data)

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                    with mock.patch.object(mcp, "_capture_window",
                                           return_value=(0, "❯\n⏸ manual mode on", "")):
                        result = mcp.leader_monitor_members("team")

        self.assertIn("alice: idle (marked-complete)", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(member["last_task_completed"])
        # 验证: monitor 标记完成后，后续 member_report_result 也应触发 compact
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                    r2 = mcp.member_report_result("team", "final report", member_name="alice")
        self.assertIn("📦 已向成员终端注入 /compact", r2)

    # ==================================================================
    # 边缘情况
    # ==================================================================

    def test_duplicate_completion_each_sends_compact(self):
        """重复 member_report_result 幂等：压缩上下文每次写入，/compact 仅首次发送"""
        self._setup_team()
        compact_count = [0]
        def fake_send(session, win, text, **kw):
            if "/compact" in text: compact_count[0] += 1; return 0, ""
        with mock.patch.object(mcp, "_tmux", side_effect=self._alive_tmux()):
            with mock.patch.object(mcp, "_send_keys", side_effect=fake_send):
                with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                    mcp.member_report_result("team", "first", member_name="alice")
                    mcp.member_report_result("team", "second", member_name="alice")
        self.assertEqual(compact_count[0], 1, "/compact 仅发送一次（compact_sent 幂等）")
        ctx = Path(mcp._load()["teams"]["team"]["context_dir"])
        entries = [json.loads(l) for l in (ctx / "results.jsonl").read_text("utf-8").splitlines()]
        self.assertGreaterEqual(len(entries), 2, "results.jsonl 每次调用都追加记录")

    def test_no_member_name_still_writes(self):
        """不提供 member_name → 仍生成压缩上下文，不标记完成"""
        self._setup_team()
        result = mcp.member_report_result("team", "anonymous")
        self.assertIn("压缩上下文", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertFalse(member.get("last_task_completed", False))

    def test_artifact_path_in_context(self):
        """artifact_path 出现在压缩上下文文件中"""
        self._setup_team()
        mcp.member_report_result("team", "done", member_name="alice",
                                 artifact_path="reports/final.md")
        ctx = Path(mcp._load()["teams"]["team"]["context_dir"])
        entry = json.loads((ctx / "results.jsonl").read_text("utf-8").splitlines()[-1])
        content = (ctx / entry["compressed_context_path"]).read_text("utf-8")
        self.assertIn("reports/final.md", content)

    def test_nonexistent_team(self):
        """不存在的团队 → 错误消息，不崩溃"""
        self.assertIn("不存在", mcp.member_report_result("ghost", "x", member_name="a"))
        self.assertIn("不存在", mcp.leader_mark_task_complete("ghost", summary="x"))


if __name__ == "__main__":
    unittest.main()
