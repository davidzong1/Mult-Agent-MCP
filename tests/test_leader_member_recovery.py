"""
新恢复功能测试：覆盖成员/leader 中断、重复激活、任务完成持久化、重启恢复。

补充覆盖中断闭环（leader 终端死亡自动重建）：
  - _maybe_revive_leader / _revive_leader_terminal / _leader_window_is_dead
  - member_report_result 触发 reason="member_report" 复活（幂等、不误重启活跃 leader）

遵循现有 test_mult_agent_mcp.py / test_completion_compact.py 模式:
unittest + mock, 隔离全局状态, 不依赖真实 tmux。
"""

import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class TestLeaderMemberRecovery(unittest.TestCase):
    """恢复功能测试: 中断 / 重复激活 / 完成持久化 / 重启恢复"""

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
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _setup_team(
        self,
        *,
        leader="lead",
        leader_type="tmux",
        leader_task="",
        leader_task_completed=True,
        members=None,
        terminals_active=False,
    ):
        """创建团队: leader(默认 tmux) + 成员, 支持未完成 leader 任务。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": terminals_active,
            "leader": leader,
            "leader_type": leader_type,
            "leader_revival_config": {
                "enabled": True,
                "min_interval_seconds": 0,
                "max_revivals": 5,
            },
            "members": {
                leader: {"role": "leader", "agent": "claude"},
            },
        }
        if leader_task:
            team["leader_last_task"] = leader_task
            team["leader_last_task_completed"] = leader_task_completed
        for name, info in (members or {}).items():
            team["members"][name] = info
        mcp._save({"teams": {"team": team}})
        return workspace, context

    def _read_results(self, context):
        results_file = context / "results.jsonl"
        if not results_file.exists():
            return []
        return [
            json.loads(line)
            for line in results_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    @staticmethod
    def _fake_tmux():
        def fake(cmd, timeout=10):
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""  # 无会话
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\talice", ""
            return 0, "", ""
        return fake

    def _member_info(self, member_name="alice", **overrides):
        info = {
            "role": "coder",
            "agent": "claude",
            "last_task": "",
            "last_context": "",
            "last_task_completed": True,
            **overrides,
        }
        return info

    # ==================================================================
    # A. 成员/leader 中断 (Interruption)
    # ==================================================================

    def test_scan_member_terminal_recovers_dead_member_with_unfinished_task(self):
        workspace, _ = self._setup_team(members={
            "alice": self._member_info(
                last_task="finish login", last_task_completed=False,
            ),
        }, terminals_active=True)

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value=None):
                with mock.patch.object(mcp, "_recover_and_send", return_value=(True, "")) as recover:
                    result = mcp._scan_member_terminal("team", "alice")

        self.assertEqual(result["action"], "recovered")
        recover.assert_called_once_with("team", "alice", "mcp_team")
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_observed_state"], "dead")

    def test_scan_member_terminal_stops_at_recovery_limit(self):
        self._setup_team(members={
            "alice": self._member_info(
                last_task="finish login", last_task_completed=False,
                recovery_count=3,
            ),
        })
        team_data = mcp._load()["teams"]["team"]
        team_data["monitor_max_recoveries"] = 3
        mcp._save({"teams": {"team": team_data}})

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value=None):
                with mock.patch.object(mcp, "_recover_and_send") as recover:
                    result = mcp._scan_member_terminal("team", "alice")

        self.assertEqual(result["action"], "recovery-limit")
        recover.assert_not_called()

    def test_scan_member_terminal_no_recovery_when_no_unfinished_task(self):
        self._setup_team(members={
            "alice": self._member_info(last_task="done", last_task_completed=True),
        })

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value=None):
                with mock.patch.object(mcp, "_recover_and_send") as recover:
                    result = mcp._scan_member_terminal("team", "alice")

        self.assertEqual(result["action"], "window-missing")
        recover.assert_not_called()

    def test_leader_window_is_dead_when_target_missing(self):
        team = {"leader": "lead"}
        with mock.patch.object(mcp, "_member_window_target", return_value=None):
            self.assertTrue(mcp._leader_window_is_dead("team", team, "mcp_team"))

    def test_leader_window_is_dead_when_cli_crashed_to_shell(self):
        team = {"leader": "lead"}
        shell_output = "zwc@host:~/project$"
        with mock.patch.object(mcp, "_member_window_target", return_value="@1"):
            with mock.patch.object(mcp, "_capture_window", return_value=(0, shell_output, "")):
                self.assertTrue(mcp._leader_window_is_dead("team", team, "mcp_team"))

    def test_leader_window_is_dead_false_when_cli_live(self):
        team = {"leader": "lead"}
        idle_output = "✻ Brewed for 5s\n❯\n⏸ manual mode on"
        with mock.patch.object(mcp, "_member_window_target", return_value="@1"):
            with mock.patch.object(mcp, "_capture_window", return_value=(0, idle_output, "")):
                self.assertFalse(mcp._leader_window_is_dead("team", team, "mcp_team"))

    def test_maybe_revive_leader_revives_dead_tmux_leader(self):
        self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_find_any_session", return_value=None):
            with mock.patch.object(mcp, "_ensure_team_session", return_value=("mcp_team", True)):
                with mock.patch.object(mcp, "_member_window_target", return_value=None):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_inject_claude_leader_prompt", return_value=(0, "")):
                            with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        revived, msg = mcp._maybe_revive_leader("team", reason="patrol")

        self.assertTrue(revived)
        self.assertIn("revived", msg)
        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_revival_count"], 1)
        self.assertEqual(team["leader_state"], "active")
        # 恢复事件写入共享上下文区
        self.assertEqual(self._read_results(self.root / "context")[-1]["event"], "leader_revival")

    def test_maybe_revive_leader_noop_when_leader_live(self):
        self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_leader_window_is_dead", return_value=False):
                with mock.patch.object(mcp, "_tmux_spawn_member") as spawn:
                    with mock.patch.object(mcp, "_inject_claude_leader_prompt") as inject:
                        revived, msg = mcp._maybe_revive_leader("team", reason="patrol")

        self.assertFalse(revived)
        spawn.assert_not_called()
        inject.assert_not_called()

    def test_maybe_revive_leader_noop_for_direct_leader(self):
        self._setup_team(
            leader_type="direct",
            leader_task="总任务", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_tmux_spawn_member") as spawn:
            revived, msg = mcp._maybe_revive_leader("team", reason="patrol")

        self.assertFalse(revived)
        spawn.assert_not_called()

    def test_revive_leader_rate_limited(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            terminals_active=True,
        )
        team = mcp._load()["teams"]["team"]
        team["leader_revival_count"] = 5  # == max_revivals
        mcp._save({"teams": {"team": team}})

        with mock.patch.object(mcp, "_find_any_session", return_value=None):
            with mock.patch.object(mcp, "_ensure_team_session", return_value=("mcp_team", True)):
                with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                    revived, msg = mcp._maybe_revive_leader("team", reason="patrol")

        self.assertFalse(revived)
        self.assertIn("rate-limited", msg)

    # ==================================================================
    # B. 重复激活 (Repeated activation)
    # ==================================================================

    def test_repeated_claim_leader_increments_recovery_count(self):
        self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_tmux_session_alive", return_value=True):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=False):
                result1 = mcp.claim_leader("team")
        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_type"], "direct")
        self.assertEqual(team["leader_recovery_count"], 1)
        self.assertIn("Leader 恢复状态", result1)

        # 第二次 claim(已是 direct leader) → 再次 reentry, 计数继续累积
        result2 = mcp.claim_leader("team")
        team = mcp._load()["teams"]["team"]
        self.assertIn("已经是", result2)
        self.assertEqual(team["leader_recovery_count"], 2)

    def test_claim_leader_demotes_live_tmux_leader_to_member(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_tmux_session_alive", return_value=True):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                result = mcp.claim_leader("team")

        team = mcp._load()["teams"]["team"]
        self.assertIn("降级为普通成员", result)
        self.assertEqual(team["leader_type"], "direct")
        self.assertEqual(team["members"]["lead"]["role"], "member")

    def test_member_get_my_task_repeated_resume_increments_count(self):
        self._setup_team(members={
            "alice": self._member_info(
                last_task="实现 auth", last_context="need OAuth",
                last_task_completed=False,
            ),
        })

        mcp.member_get_my_task("team", "alice")
        mcp.member_get_my_task("team", "alice")

        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_resume_count"], 2)
        self.assertIn("last_resume_ts", member)
        self.assertEqual(member["last_observed_state"], "busy")

    def test_member_get_my_task_returns_persisted_task_after_reload(self):
        self._setup_team(members={
            "alice": self._member_info(
                last_task="实现 auth", last_context="need OAuth",
                last_task_completed=False,
            ),
        })

        # 模拟"重启后重新进入"：数据从磁盘重新加载
        data = mcp._load()
        self.assertTrue(data["teams"]["team"]["members"]["alice"]["last_task"])
        msg = mcp.member_get_my_task("team", "alice")

        self.assertIn("未完成任务", msg)
        self.assertIn("实现 auth", msg)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_resume_count"], 1)

    def test_member_get_my_task_after_completion_does_not_resume(self):
        self._setup_team(members={
            "alice": self._member_info(last_task="已完成任务", last_task_completed=True),
        })

        msg = mcp.member_get_my_task("team", "alice")

        self.assertIn("已完成", msg)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertNotIn("last_resume_count", member)
        self.assertNotIn("last_resume_ts", member)

    def test_revive_leader_terminal_skips_injection_when_spawn_reports_already_exists(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", return_value=None):
                with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "window already exists")):
                    with mock.patch.object(mcp, "_inject_claude_leader_prompt") as inject:
                        with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                            with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                revived, msg = mcp._revive_leader_terminal("team", reason="patrol")

        self.assertFalse(revived)
        self.assertIn("skip injection", msg)
        inject.assert_not_called()

    # ==================================================================
    # C. 任务完成持久化 (Task completion persistence)
    # ==================================================================

    def _mock_completion_io(self, *, revive_real=False):
        """统一 mock 完成收尾相关 IO, 避免真实 tmux / 文件副作用。

        revive_real=True 时不 mock _maybe_revive_leader, 由测试自身
        注入底层 tmux mock 让真实复活路径执行。
        """
        patchers = [
            mock.patch.object(mcp, "_write_member_compressed_context", return_value="member_contexts/alice.md"),
            mock.patch.object(
                mcp, "_finalize_agent_completion",
                return_value={
                    "compact_path": "member_contexts/alice.md",
                    "compact_sent": False, "compact_error": "", "truncated": False,
                    "agent_exited": False,
                },
            ),
            mock.patch.object(
                mcp, "_notify_leader_of_report",
                return_value={"injected": False, "leader": "lead", "reason": "no-resting-tmux-leader"},
            ),
        ]
        if not revive_real:
            patchers.append(mock.patch.object(mcp, "_maybe_revive_leader", return_value=(False, "")))
        for p in patchers:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in patchers])

    def test_member_report_result_persists_completion(self):
        context_dir = self.root / "context"
        self._setup_team(members={
            "alice": self._member_info(
                last_task="实现 auth", last_task_completed=False,
            ),
        })
        self._mock_completion_io()

        result = mcp.member_report_result("team", "auth done", member_name="alice")

        self.assertIn("已标记为完成", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(member["last_task_completed"])
        self.assertEqual(member["last_observed_state"], "idle")
        entries = self._read_results(context_dir)
        self.assertEqual(entries[0]["member"], "alice")
        self.assertEqual(entries[0]["result"], "auth done")

    def test_member_report_result_sets_leader_idle_when_all_done(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=True,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
            },
        )
        self._mock_completion_io()

        mcp.member_report_result("team", "auth done", member_name="alice")

        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_work_state"], "idle")

    def test_member_report_result_keeps_leader_active_when_others_pending(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=True,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
                "bob": self._member_info(
                    role="tester", last_task="写测试", last_task_completed=False,
                ),
            },
        )
        self._mock_completion_io()

        mcp.member_report_result("team", "auth done", member_name="alice")

        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_work_state"], "active")
        self.assertFalse(team["members"]["bob"]["last_task_completed"])

    def test_leader_mark_task_complete_persists_standby(self):
        context_dir = self.root / "context"
        self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            members={
                "alice": self._member_info(last_task="done", last_task_completed=True),
            },
        )
        with mock.patch.object(mcp, "_write_leader_compressed_context", return_value="leader/ctx.md"):
            with mock.patch.object(
                mcp, "_finalize_agent_completion",
                return_value={
                    "compact_path": "leader/ctx.md",
                    "compact_sent": False, "compact_error": "", "truncated": False,
                    "agent_exited": False,
                },
            ):
                result = mcp.leader_mark_task_complete("team", summary="all done")

        self.assertIn("已标记完成", result)
        team = mcp._load()["teams"]["team"]
        self.assertTrue(team["leader_last_task_completed"])
        self.assertEqual(team["leader_work_state"], "idle")
        entries = self._read_results(context_dir)
        self.assertEqual(entries[-1]["event"], "leader_task_completed")
        self.assertEqual(entries[-1]["result"], "all done")

    def test_leader_mark_complete_keeps_active_when_member_unfinished(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            members={
                "alice": self._member_info(
                    last_task="还在写", last_task_completed=False,
                ),
            },
        )
        with mock.patch.object(mcp, "_write_leader_compressed_context", return_value="leader/ctx.md"):
            with mock.patch.object(
                mcp, "_finalize_agent_completion",
                return_value={
                    "compact_path": "leader/ctx.md",
                    "compact_sent": False, "compact_error": "", "truncated": False,
                    "agent_exited": False,
                },
            ):
                result = mcp.leader_mark_task_complete("team", summary="leader done")

        self.assertIn("仍检测到未完成成员任务", result)
        team = mcp._load()["teams"]["team"]
        self.assertTrue(team["leader_last_task_completed"])
        self.assertEqual(team["leader_work_state"], "active")

    # ==================================================================
    # D. 重启恢复 (Restart recovery)
    # ==================================================================

    def test_launch_team_terminals_records_reentry_keeps_active(self):
        self._setup_team(
            leader_type="direct",
            leader_task="",  # 无新任务 → reentry
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
            },
            terminals_active=False,
        )

        with mock.patch.object(mcp, "_leader_terminal_restart_blocked", return_value=False):
            with mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux()):
                with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                    with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                        with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                                with mock.patch.object(mcp, "_start_team_monitor"):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        result = mcp.launch_team_terminals("team")

        self.assertIn("终端已启动", result)
        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_recovery_count"], 1)
        self.assertEqual(team["leader_work_state"], "active")

    def test_launch_team_terminals_records_standby_when_all_done(self):
        self._setup_team(
            leader_type="direct",
            leader_task="已完成总任务", leader_task_completed=True,
            members={
                "alice": self._member_info(last_task="done", last_task_completed=True),
            },
            terminals_active=False,
        )

        with mock.patch.object(mcp, "_leader_terminal_restart_blocked", return_value=False):
            with mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux()):
                with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                    with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                        with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                                with mock.patch.object(mcp, "_start_team_monitor"):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        mcp.launch_team_terminals("team")

        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_work_state"], "idle")
        self.assertNotIn("leader_recovery_count", team)

    def test_launch_team_terminals_blocked_when_leader_live_with_unfinished_work(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_member_window_target", return_value="@1"):
            result = mcp.launch_team_terminals("team")

        self.assertIn("禁止重启", result)

    def test_leader_launch_member_terminal_resends_unfinished_task(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_context="need OAuth",
                    last_task_completed=False,
                ),
            },
            terminals_active=True,
        )

        send_calls = []
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                    with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                        with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                            with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                with mock.patch.object(
                                    mcp, "_send_keys",
                                    side_effect=lambda s, w, t, **kw: send_calls.append((s, w, t)) or (0, ""),
                                ):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        result = mcp.leader_launch_member_terminal("team", "alice")

        self.assertIn("已自动重发未完成任务", result)
        # 恢复上下文 + 未完成任务分别注入
        self.assertEqual(len(send_calls), 2)
        self.assertIn("终端恢复通知", send_calls[0][2])
        self.assertIn("实现 auth", send_calls[1][2])

    def test_leader_launch_member_terminal_skips_resend_when_done(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            members={
                "alice": self._member_info(last_task="已完成任务", last_task_completed=True),
            },
            terminals_active=True,
        )

        send_calls = []
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                    with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                        with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                            with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                with mock.patch.object(
                                    mcp, "_send_keys",
                                    side_effect=lambda s, w, t, **kw: send_calls.append((s, w, t)) or (0, ""),
                                ):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        result = mcp.leader_launch_member_terminal("team", "alice")

        self.assertIn("上次任务已完成，不再重发", result)
        self.assertEqual(len(send_calls), 1)  # 仅恢复上下文, 不重发任务

    def test_leader_launch_member_terminal_idempotent_when_live(self):
        self._setup_team(
            leader_task="总任务", leader_task_completed=False,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
            },
            terminals_active=True,
        )

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_state", return_value=("live", "@5")):
                with mock.patch.object(mcp, "_tmux_spawn_member") as spawn:
                    result = mcp.leader_launch_member_terminal("team", "alice")

        self.assertIn("已在运行", result)
        spawn.assert_not_called()

    # ==================================================================
    # E. 中断闭环: member_report 触发 leader 复活 (补充关注点)
    # ==================================================================

    def test_member_report_revives_dead_leader(self):
        context_dir = self.root / "context"
        self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
            },
            terminals_active=True,
        )
        # 不 mock _maybe_revive_leader, 让真实复活路径执行
        self._mock_completion_io(revive_real=True)

        with mock.patch.object(mcp, "_find_any_session", return_value=None):
            with mock.patch.object(mcp, "_ensure_team_session", return_value=("mcp_team", True)):
                with mock.patch.object(mcp, "_member_window_target", return_value=None):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_inject_claude_leader_prompt", return_value=(0, "")):
                            with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        result = mcp.member_report_result("team", "auth done", member_name="alice")

        self.assertIn("已自动恢复", result)
        team = mcp._load()["teams"]["team"]
        self.assertEqual(team["leader_revival_count"], 1)
        self.assertEqual(team["leader_state"], "active")
        events = self._read_results(context_dir)
        self.assertTrue(any(e.get("event") == "leader_revival" for e in events))

    def test_member_report_does_not_revive_live_leader(self):
        self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
            },
            terminals_active=True,
        )
        self._mock_completion_io()

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_leader_window_is_dead", return_value=False):
                with mock.patch.object(mcp, "_tmux_spawn_member") as spawn:
                    result = mcp.member_report_result("team", "auth done", member_name="alice")

        self.assertNotIn("已自动恢复", result)
        spawn.assert_not_called()
        team = mcp._load()["teams"]["team"]
        self.assertNotIn("leader_revival_count", team)

    # ==================================================================
    # 异常安全: leader 唤醒/终端通知失败绝不能使上报失败或回滚
    # 核心语义: 结果写入 results.jsonl + 成员完成状态(last_task_completed)
    # 与 leader 通知/唤醒/恢复是独立提交的 — 旁路失败只降级为提示。
    # 隔离注入点: notify / revive / locked revive / send context / compact。
    # ==================================================================

    def _setup_report_team(self):
        """异常安全测试共用的团队: alice 未完成任务 + tmux leader 未完成任务。"""
        return self._setup_team(
            leader_task="总任务：完成登录", leader_task_completed=False,
            members={
                "alice": self._member_info(
                    last_task="实现 auth", last_task_completed=False,
                ),
            },
            terminals_active=True,
        )

    def _assert_report_persisted(self, context, result_text="auth done"):
        """断言上报已持久化: results.jsonl 有记录 + 成员完成状态已置位。"""
        self.assertTrue((context / "results.jsonl").exists(), "results.jsonl 必须已写入")
        entries = self._read_results(context)
        self.assertTrue(
            any(e.get("result") == result_text for e in entries),
            "上报结果必须出现在 results.jsonl",
        )
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertTrue(member["last_task_completed"], "成员任务必须标记为完成")
        self.assertEqual(member["last_observed_state"], "idle")

    def test_report_notify_raises_still_persists(self):
        """_notify_leader_of_report 抛异常 → 上报不失败，结果与完成状态已持久化"""
        context_dir = self.root / "context"
        self._setup_report_team()

        def boom(*a, **k):
            raise RuntimeError("notify leader failed (mocked)")

        with mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")), \
             mock.patch.object(mcp, "_maybe_revive_leader", return_value=(False, "")), \
             mock.patch.object(mcp, "_notify_leader_of_report", side_effect=boom):
            result = mcp.member_report_result("team", "auth done", member_name="alice")
        self.assertIn("结果已记录", result)
        self.assertIn("记录 leader 回报失败", result)
        self._assert_report_persisted(context_dir)

    def test_report_revive_raises_still_persists(self):
        """_maybe_revive_leader 抛异常 → 上报不失败，结果与完成状态已持久化"""
        context_dir = self.root / "context"
        self._setup_report_team()

        def boom(*a, **k):
            raise RuntimeError("revive leader failed (mocked)")

        with mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")), \
             mock.patch.object(mcp, "_maybe_revive_leader", side_effect=boom):
            result = mcp.member_report_result("team", "auth done", member_name="alice")
        self.assertIn("结果已记录", result)
        self.assertIn("leader 终端恢复失败", result)
        self.assertIn("结果已保存，不影响本次上报", result)
        self._assert_report_persisted(context_dir)

    def test_report_send_context_raises_still_persists(self):
        """leader 唤醒终端注入(_send_context_to_member)抛异常 → 上报不失败且已持久化"""
        context_dir = self.root / "context"
        self._setup_report_team()
        team = mcp._load()["teams"]["team"]
        team["leader_state"] = "resting"
        team["leader_wakeup_config"] = {"enabled": True}
        mcp._save({"teams": {"team": team}})

        def boom(*a, **k):
            raise RuntimeError("send context to member failed (mocked)")

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_leader_window_is_dead", return_value=False), \
             mock.patch.object(mcp, "_leader_terminal_is_idle", return_value=True), \
             mock.patch.object(mcp, "_send_context_to_member", side_effect=boom), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
            result = mcp.member_report_result("team", "auth done", member_name="alice")
        self.assertIn("结果已记录", result)
        self.assertIn("记录 leader 回报失败", result)
        self._assert_report_persisted(context_dir)

    def test_report_inject_compact_raises_still_persists(self):
        """/compact 注入(_inject_compact)抛异常 → 上报不失败，结果与完成状态已持久化"""
        context_dir = self.root / "context"
        self._setup_report_team()

        def boom(*a, **k):
            raise RuntimeError("compact injection failed (mocked)")

        with mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")), \
             mock.patch.object(mcp, "_inject_compact", side_effect=boom):
            result = mcp.member_report_result("team", "auth done", member_name="alice")
        self.assertIn("结果已记录", result)
        self.assertIn("compact injection error", result)
        self._assert_report_persisted(context_dir)

    def test_report_finalize_raises_still_persists(self):
        """_finalize_agent_completion 整体抛异常(外层兜底) → 上报不失败，结果与完成状态已持久化"""
        context_dir = self.root / "context"
        self._setup_report_team()

        def boom(*a, **k):
            raise RuntimeError("finalize failed (mocked)")

        with mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux()), \
             mock.patch.object(mcp, "_send_keys", return_value=(0, "")), \
             mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")), \
             mock.patch.object(mcp, "_finalize_agent_completion", side_effect=boom):
            result = mcp.member_report_result("team", "auth done", member_name="alice")
        self.assertIn("结果已记录", result)
        self.assertIn("finalize failed", result)
        self._assert_report_persisted(context_dir)

    def test_revive_leader_terminal_converts_locked_exception(self):
        """_revive_leader_terminal 把 locked 事务内异常降级为 (False, msg)，绝不抛出"""
        self._setup_report_team()

        def boom(*a, **k):
            raise RuntimeError("revive locked failed (mocked)")

        with mock.patch.object(mcp, "_revive_leader_terminal_locked", side_effect=boom):
            revived, msg = mcp._revive_leader_terminal("team", reason="member_report")
        self.assertFalse(revived)
        self.assertIn("leader revival error", msg)
