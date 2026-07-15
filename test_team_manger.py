import unittest
from unittest import mock

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
        self.assertIn("while tmux has-session -t mcp_team_123456", spawn_calls[0][0])
        self.assertIn("env -u TMUX tmux attach -t mcp_team_123456", spawn_calls[0][0])

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
                            with mock.patch.object(tui_screens, "_send_keys", side_effect=lambda session, window, text, **kwargs: prompt_calls.append((session, window, text)) or (0, "")):
                                with mock.patch("time.sleep", return_value=None):
                                    ok, msg = tui_screens.launch_terminals("team")

        self.assertTrue(ok)
        self.assertIn("终端已启动", msg)
        self.assertTrue(any(cmd and cmd[0] == "new-session" for cmd in tmux_calls))
        self.assertEqual(len(prompt_calls), 1)
        self.assertTrue(prompt_calls[0][0].startswith("mcp_team_"))
        self.assertEqual(prompt_calls[0][1], "alice")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的 leader", prompt_calls[0][2])


if __name__ == "__main__":
    unittest.main()
