import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common.data_layer as data_layer
import member_status as team_manger
import tui.tui_screens as tui_screens


class TeamManagerStatusTests(unittest.TestCase):
    def test_member_activity_status_for_active_task(self):
        label, bucket = team_manger.format_member_activity_status(
            {"role": "coder", "last_task": "fix bug", "last_task_completed": False},
            True,
        )

        self.assertEqual(label, "🟢 working")
        self.assertEqual(bucket, "working")

    def test_member_activity_status_for_sleeping_completed_task(self):
        label, bucket = team_manger.format_member_activity_status(
            {"last_task": "done", "last_task_completed": True},
            False,
        )

        self.assertEqual(label, "😴 sleep")
        self.assertEqual(bucket, "sleep")

    def test_member_activity_status_for_idle_alive_member(self):
        label, bucket = team_manger.format_member_activity_status({}, True)

        self.assertEqual(label, "💤 idle")
        self.assertEqual(bucket, "idle")

    def test_member_activity_status_for_alive_leader(self):
        label, bucket = team_manger.format_member_activity_status(
            {"role": "leader"},
            True,
        )

        self.assertEqual(label, "👑 leader")
        self.assertEqual(bucket, "leader")

    def test_member_activity_status_for_approval_prompt(self):
        label, bucket = team_manger.format_member_activity_status(
            {"last_observed_state": "approval", "last_task": "fix", "last_task_completed": False},
            True,
        )

        self.assertEqual(label, "⏸ approval")
        self.assertEqual(bucket, "approval")

    def test_member_activity_status_for_dead_member(self):
        label, bucket = team_manger.format_member_activity_status({}, False)

        self.assertEqual(label, "⚫ dead")
        self.assertEqual(bucket, "dead")

    def test_member_activity_status_for_recovering_member(self):
        label, bucket = team_manger.format_member_activity_status(
            {"last_task": "fix bug", "last_task_completed": False, "recovery_count": 1},
            True,
        )

        self.assertEqual(label, "🔄 recovering")
        self.assertEqual(bucket, "recovering")

    def test_member_activity_status_working_over_recovering(self):
        """recovery_count=0 且有未完成任务 → working，不是 recovering"""
        label, bucket = team_manger.format_member_activity_status(
            {"last_task": "fix bug", "last_task_completed": False, "recovery_count": 0},
            True,
        )

        self.assertEqual(label, "🟢 working")
        self.assertEqual(bucket, "working")

    def test_tmux_spawn_keeps_pane_open_after_command_exits(self):
        calls = []

        def fake_tmux_run(cmd):
            calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(tui_screens, "_current_tmux_session", return_value="ui"):
            with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux_run):
                ok, _ = tui_screens.tmux_spawn("echo hi")

        self.assertTrue(ok)
        self.assertEqual(calls[0][0:2], ["split-window", "-h"])
        self.assertIn("echo hi", calls[0][2])
        self.assertIn("exec ${SHELL:-/bin/sh}", calls[0][2])

    def test_open_leader_terminal_uses_reattaching_attach_in_tmux(self):
        store = {
            "teams": {
                "team": {
                    "leader": "alice",
                    "members": {
                        "alice": {"role": "leader", "agent": "codex"},
                    },
                }
            }
        }
        spawn_calls = []
        tmux_calls = []

        def fake_tmux_run(cmd, timeout=10):
            tmux_calls.append(cmd)
            return 0, "", ""

        def fake_tmux_spawn(command, title=""):
            spawn_calls.append((command, title))
            return True, "opened"

        with mock.patch.object(tui_screens, "_find_tmux_session", return_value="mcp_team_123456"):
            with mock.patch.object(tui_screens, "_find_tmux", return_value="tmux"):
                with mock.patch.object(tui_screens, "load_data", return_value=store):
                    with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux_run):
                        with mock.patch.object(tui_screens, "_current_tmux_session", return_value="ui"):
                            with mock.patch.object(tui_screens, "tmux_spawn", side_effect=fake_tmux_spawn):
                                ok, msg = tui_screens.open_leader_terminal("team")

        self.assertTrue(ok)
        self.assertIn("已进入 mcp_team_123456", msg)
        self.assertEqual(tmux_calls[0], ["select-window", "-t", "mcp_team_123456:alice"])
        self.assertEqual(spawn_calls[0][1], "team:leader")
        self.assertIn("trap 'exit 0' INT TERM", spawn_calls[0][0])
        self.assertIn("while tmux has-session -t mcp_team_123456", spawn_calls[0][0])
        self.assertIn("env -u TMUX tmux attach -t mcp_team_123456", spawn_calls[0][0])
        self.assertIn("sleep 2", spawn_calls[0][0])
        self.assertNotIn('exit "$status"', spawn_calls[0][0])

    def test_launch_terminals_injects_prompt_for_claude_leader(self):
        store = {
            "teams": {
                "team": {
                    "workspace_dir": "/tmp/team-workspace",
                    "context_dir": "/tmp/team-context",
                    "leader": "alice",
                    "members": {
                        "alice": {"role": "leader", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        }
        tmux_calls = []
        prompt_calls = []
        sleep_calls = []

        def fake_load_data():
            return store

        def fake_save_data(data):
            store.clear()
            store.update(data)

        def fake_tmux_run(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(tui_screens, "load_data", side_effect=fake_load_data):
            with mock.patch.object(tui_screens, "save_data", side_effect=fake_save_data):
                with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "ok")):
                    with mock.patch.object(tui_screens, "shutil") as fake_shutil:
                        fake_shutil.which.side_effect = lambda name: name
                        with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux_run):
                            with mock.patch.object(tui_screens, "write_claude_permissions", return_value="/tmp/team-workspace/.claude/settings.json"):
                                with mock.patch.object(tui_screens, "_send_keys", side_effect=lambda session, window, text, **kwargs: prompt_calls.append((session, window, text)) or (0, "")):
                                    with mock.patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                                        ok, msg = tui_screens.launch_terminals("team")

        self.assertTrue(ok)
        self.assertIn("终端已启动", msg)
        self.assertTrue(any(cmd and cmd[0] == "new-session" for cmd in tmux_calls))
        leader_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-session")
        self.assertIn("--allowedTools", leader_cmd)
        leader_tools = leader_cmd[leader_cmd.index("--allowedTools") + 1]
        self.assertIn("mcp__mult-agent-mcp__leader_*", leader_tools)
        self.assertIn("mcp__mult_agent_mcp__leader_*", leader_tools)
        self.assertNotIn("member_*", leader_tools)
        # 验证 leader 处于 manual 模式时不传递 --permission-mode
        self.assertNotIn("--permission-mode", leader_cmd)
        # 验证 2s 初始化等待发生在 prompt 注入之前
        self.assertIn(2.0, sleep_calls, f"Expected 2s wait before prompt, sleep calls: {sleep_calls}")
        self.assertEqual(len(prompt_calls), 1)
        self.assertTrue(prompt_calls[0][0].startswith("mcp_team_"))
        self.assertEqual(prompt_calls[0][1], "alice")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的 leader", prompt_calls[0][2])
        # 验证 confirm Enter 在 prompt 发送后被调用
        self.assertTrue(any(cmd[-1:] == ["Enter"] and ":alice" in cmd[2] for cmd in tmux_calls))

    def test_inject_claude_leader_prompt_waits_before_sending(self):
        """验证 _inject_claude_leader_prompt 在发送 prompt 前等待 2s 初始化。"""
        sleep_calls = []
        send_calls = []
        confirm_calls = []

        def fake_send_keys(session, window, text, **kwargs):
            send_calls.append((session, window, text))
            return 0, ""

        def fake_confirm(session, window, delay=0.35):
            confirm_calls.append((session, window, delay))
            return 0, ""

        with mock.patch.object(tui_screens, "_send_keys", side_effect=fake_send_keys):
            with mock.patch.object(tui_screens, "_confirm_prompt_submission", side_effect=fake_confirm):
                with mock.patch.object(tui_screens, "_leader_system_prompt", return_value="<prompt>"):
                    with mock.patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
                        rc, err = tui_screens._inject_claude_leader_prompt("sess", "alice", "team")

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        # 序列验证: sleep(2.0) → send_keys → confirm
        self.assertEqual(sleep_calls, [2.0])
        self.assertEqual(len(send_calls), 1)
        self.assertEqual(send_calls[0][:2], ("sess", "alice"))
        self.assertIn("<prompt>", send_calls[0][2])
        self.assertEqual(len(confirm_calls), 1)
        self.assertEqual(confirm_calls[0][:2], ("sess", "alice"))

    def test_launch_terminals_applies_member_auto_mode_args(self):
        store = {
            "teams": {
                "team": {
                    "workspace_dir": "/tmp/team-workspace",
                    "context_dir": "/tmp/team-context",
                    "leader": "lead",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "bob": {"role": "coder", "agent": "claude", "work_mode": "auto"},
                        "carol": {"role": "tester", "agent": "codex", "work_mode": "auto"},
                    },
                }
            }
        }
        tmux_calls = []

        def fake_load_data():
            return store

        def fake_save_data(data):
            store.clear()
            store.update(data)

        def fake_tmux_run(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(tui_screens, "load_data", side_effect=fake_load_data):
            with mock.patch.object(tui_screens, "save_data", side_effect=fake_save_data):
                with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "ok")):
                    with mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "ok")):
                        with mock.patch.object(tui_screens, "write_claude_permissions", return_value="/tmp/team-workspace/.claude/settings.json"):
                            with mock.patch.object(tui_screens, "shutil") as fake_shutil:
                                fake_shutil.which.side_effect = lambda name: name
                                with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux_run):
                                    with mock.patch("time.sleep", return_value=None):
                                        ok, msg = tui_screens.launch_terminals("team")

        self.assertTrue(ok)
        self.assertIn("终端已启动", msg)
        bob_cmd = next(cmd for cmd in tmux_calls if cmd[:5] == ["new-window", "-t", mock.ANY, "-n", "bob"])
        carol_cmd = next(cmd for cmd in tmux_calls if cmd[:5] == ["new-window", "-t", mock.ANY, "-n", "carol"])
        self.assertIn("--permission-mode", bob_cmd)
        self.assertIn("auto", bob_cmd)
        self.assertIn("--ask-for-approval", carol_cmd)
        self.assertIn("never", carol_cmd)

    def test_delete_team_record_and_artifacts_cleans_managed_context_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_file = root / "teams_data.json"
            context_root = root / "contexts"
            context_dir = context_root / "team"
            context_dir.mkdir(parents=True)
            (context_dir / "results.jsonl").write_text("x", encoding="utf-8")
            external_workspace = root / "real_project"
            external_workspace.mkdir()

            store = {
                "teams": {
                    "team": {
                        "context_dir": str(context_dir),
                        "workspace_dir": str(external_workspace),
                        "terminals_active": True,
                        "members": {},
                    }
                }
            }

            def fake_save_data(data):
                store.clear()
                store.update(data)

            with mock.patch.object(data_layer, "context_base_dir", return_value=context_root):
                with mock.patch.object(tui_screens, "load_data", return_value=store):
                    with mock.patch.object(tui_screens, "save_data", side_effect=fake_save_data):
                        with mock.patch.object(tui_screens, "kill_terminals", return_value=(True, "closed")) as kill:
                            ok, msg = tui_screens.delete_team_record_and_artifacts("team")
                            saved = dict(store)

            self.assertTrue(ok)
            self.assertIn("已删除共享上下文", msg)
            self.assertIn("保留用户工作目录", msg)
            self.assertFalse(context_dir.exists())
            self.assertTrue(external_workspace.exists())
            self.assertNotIn("team", saved.get("teams", {}))
            kill.assert_called_once_with("team")

    def test_data_layer_team_context_dir_uses_persisted_context_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_file = root / "teams_data.json"
            configured_context = root / "custom-context"
            data_layer.set_data_file(data_file)
            try:
                data_layer.save_data({
                    "teams": {
                        "cpp_ipc_dds": {
                            "context_dir": str(configured_context),
                        }
                    }
                })

                self.assertEqual(data_layer.team_context_dir("cpp_ipc_dds"), configured_context.resolve())
            finally:
                data_layer._DATA_FILE_OVERRIDE = None


if __name__ == "__main__":
    unittest.main()
