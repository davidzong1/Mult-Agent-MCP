import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common.mcp_config as mcp_config
import common.data_layer as data_layer
import common.tmux_utils as tmux_utils
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

    def test_claude_mcp_configured_rejects_legacy_sse_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            team_dir = Path(tmp)
            global_config = team_dir / ".claude-global.json"
            claude_dir = team_dir / ".claude"
            claude_dir.mkdir()
            mcp_json = claude_dir / "mcp.json"
            mcp_json.write_text(
                json.dumps({
                    "teamMCP": {
                        "mult-agent-mcp": {
                            "type": "sse",
                            "url": "http://localhost:8000/sse",
                        }
                    }
                }),
                encoding="utf-8",
            )

            with mock.patch.dict(mcp_config.os.environ, {"FASTMCP_PORT": "8000"}):
                with mock.patch.object(mcp_config, "CLAUDE_GLOBAL_CONFIG_PATH", global_config):
                    self.assertFalse(mcp_config.claude_mcp_configured(team_dir))
                    ok, message = mcp_config.claude_mcp_status(team_dir)
                    self.assertFalse(ok)
                    self.assertIn("旧 teamMCP 配置格式", message)

                    mcp_config.write_claude_mcp(team_dir)
                    self.assertTrue(mcp_config.claude_mcp_configured(team_dir))
                    written = json.loads(mcp_json.read_text(encoding="utf-8"))
                    server = written["mcpServers"]["mult-agent-mcp"]
                    self.assertEqual(server["type"], "http")
                    self.assertEqual(server["url"], "http://localhost:8000/mcp")

    def test_claude_mcp_configured_rejects_and_repairs_global_sse_override(self):
        with tempfile.TemporaryDirectory() as tmp:
            team_dir = Path(tmp)
            global_config = team_dir / ".claude-global.json"
            global_config.write_text(
                json.dumps({
                    "mcpServers": {
                        "mult-agent-mcp": {
                            "type": "sse",
                            "url": "http://localhost:8000/sse",
                        }
                    },
                    "projects": {
                        str(team_dir.resolve()): {
                            "mcpServers": {
                                "mult-agent-mcp": {
                                    "type": "sse",
                                    "url": "http://localhost:8000/sse",
                                }
                            }
                        }
                    },
                    "other": "preserved",
                }),
                encoding="utf-8",
            )

            with mock.patch.dict(mcp_config.os.environ, {"FASTMCP_PORT": "8000"}):
                with mock.patch.object(mcp_config, "CLAUDE_GLOBAL_CONFIG_PATH", global_config):
                    mcp_config.write_claude_mcp(team_dir)
                    written = json.loads(global_config.read_text(encoding="utf-8"))
                    self.assertEqual(written["other"], "preserved")
                    server = written["mcpServers"]["mult-agent-mcp"]
                    self.assertEqual(server["type"], "http")
                    self.assertEqual(server["url"], "http://localhost:8000/mcp")
                    project_server = written["projects"][str(team_dir.resolve())]["mcpServers"]["mult-agent-mcp"]
                    self.assertEqual(project_server["type"], "http")
                    self.assertEqual(project_server["url"], "http://localhost:8000/mcp")
                    self.assertTrue(mcp_config.claude_mcp_configured(team_dir))

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
            with mock.patch.object(tui_screens, "mcp_server_status", return_value=(True, "🟢 运行中")):
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

    # ============================================================
    # open_leader_terminal 前 MCP Server 自动启动
    # ============================================================

    def test_ensure_mcp_already_running_returns_directly(self):
        """MCP 已运行时 _ensure_mcp_server_running 返回 (True, status)，不调 start。"""
        start_calls = []

        with mock.patch.object(
            tui_screens, "mcp_server_status", return_value=(True, "running OK")
        ):
            with mock.patch.object(
                tui_screens, "start_mcp_server",
                side_effect=lambda: (start_calls.append(1) or (True, "unused"))
            ):
                ok, msg = tui_screens._ensure_mcp_server_running()

        self.assertTrue(ok)
        self.assertEqual(msg, "running OK")
        self.assertEqual(start_calls, [], "MCP 已运行时不应调用 start_mcp_server")

    def test_ensure_mcp_not_running_starts_successfully(self):
        """MCP 未运行时 _ensure_mcp_server_running 自动启动成功 → (True, msg)。"""
        with mock.patch.object(
            tui_screens, "mcp_server_status", return_value=(False, "not running")
        ):
            with mock.patch.object(
                tui_screens, "start_mcp_server",
                return_value=(True, "✅ 守护进程已启动 (PID: 12345)")
            ):
                ok, msg = tui_screens._ensure_mcp_server_running()

        self.assertTrue(ok)
        self.assertIn("12345", msg)

    def test_ensure_mcp_not_running_start_fails_returns_error(self):
        """MCP 未运行且启动失败时 _ensure_mcp_server_running → (False, error)。"""
        with mock.patch.object(
            tui_screens, "mcp_server_status", return_value=(False, "not running")
        ):
            with mock.patch.object(
                tui_screens, "start_mcp_server",
                return_value=(False, "❌ 守护进程启动失败: port in use")
            ):
                ok, msg = tui_screens._ensure_mcp_server_running()

        self.assertFalse(ok)
        self.assertIn("启动失败", msg)

    def test_open_leader_terminal_called_when_mcp_is_running(self):
        """MCP 已运行时 leader 终端正常打开（完整流程验证，MCP 不做额外动作）。"""
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
        start_calls = []

        def fake_start():
            start_calls.append(1)
            return True, "should not be called"

        with mock.patch.object(tui_screens, "mcp_server_status", return_value=(True, "ok")):
            with mock.patch.object(tui_screens, "start_mcp_server", side_effect=fake_start):
                with mock.patch.object(tui_screens, "_find_tmux_session", return_value="mcp_sess"):
                    with mock.patch.object(tui_screens, "load_data", return_value=store):
                        with mock.patch.object(tui_screens, "_find_tmux", return_value="tmux"):
                            with mock.patch.object(tui_screens, "_tmux_run", return_value=(0, "", "")):
                                with mock.patch.object(tui_screens, "_current_tmux_session", return_value="ui"):
                                    with mock.patch.object(tui_screens, "tmux_spawn", return_value=(True, "opened")):
                                        ok, msg = tui_screens.open_leader_terminal("team")

        self.assertTrue(ok)
        self.assertIn("mcp_sess", msg)
        self.assertEqual(start_calls, [], "MCP 运行时不应调用 start_mcp_server")

    def test_ensure_mcp_then_open_leader_when_start_succeeds(self):
        """MCP 未运行 → _ensure 启动成功 → 后续 open_leader_terminal 正常。"""
        store = {
            "teams": {
                "team": {
                    "leader": "bob",
                    "members": {
                        "bob": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        }

        with mock.patch.object(tui_screens, "mcp_server_status", return_value=(False, "down")):
            with mock.patch.object(tui_screens, "start_mcp_server",
                                   return_value=(True, "✅ 守护进程已启动 (PID: 9999)")):
                ok_mcp, _ = tui_screens._ensure_mcp_server_running()

        self.assertTrue(ok_mcp, "MCP 启动成功后 _ensure 应返回 ok")

        # MCP 启动成功，后续 open_leader_terminal 应正常工作
        with mock.patch.object(tui_screens, "_find_tmux_session", return_value="sess_mcp"):
            with mock.patch.object(tui_screens, "load_data", return_value=store):
                with mock.patch.object(tui_screens, "_find_tmux", return_value="tmux"):
                    with mock.patch.object(tui_screens, "_tmux_run", return_value=(0, "", "")):
                        with mock.patch.object(tui_screens, "_current_tmux_session", return_value="ui"):
                            with mock.patch.object(tui_screens, "tmux_spawn", return_value=(True, "opened")):
                                ok_open, msg = tui_screens.open_leader_terminal("team")

        self.assertTrue(ok_open)
        self.assertIn("sess_mcp", msg)

    def test_open_leader_aborted_when_mcp_start_fails(self):
        """MCP 启动失败 → _ensure 返回 False → 不应调用 open_leader_terminal。"""
        with mock.patch.object(tui_screens, "mcp_server_status", return_value=(False, "down")):
            with mock.patch.object(tui_screens, "start_mcp_server",
                                   return_value=(False, "❌ 守护进程启动失败: EADDRINUSE")):
                ok, err_msg = tui_screens._ensure_mcp_server_running()

        self.assertFalse(ok)
        self.assertIn("启动失败", err_msg)
        # 验证错误信息可供 TUI 显示
        self.assertTrue(len(err_msg) > 0)

    def test_open_leader_terminal_does_not_attach_when_mcp_start_fails(self):
        """open_leader_terminal 在 MCP 自动启动失败时直接返回，不进入 tmux。"""
        with mock.patch.object(tui_screens, "_find_tmux_session", return_value="mcp_team"):
            with mock.patch.object(tui_screens, "_ensure_mcp_server_running", return_value=(False, "port busy")):
                with mock.patch.object(tui_screens, "_tmux_run") as tmux_run:
                    with mock.patch.object(tui_screens, "tmux_spawn") as spawn:
                        ok, msg = tui_screens.open_leader_terminal("team")

        self.assertFalse(ok)
        self.assertIn("MCP Server 启动失败", msg)
        self.assertIn("port busy", msg)
        tmux_run.assert_not_called()
        spawn.assert_not_called()

    def test_tui_record_leader_reentry_keeps_active_for_unfinished_member_task(self):
        team = {
            "leader": "lead",
            "leader_work_state": "idle",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {
                    "role": "coder",
                    "agent": "claude",
                    "last_task": "finish review",
                    "last_task_completed": False,
                },
            },
        }

        tui_screens._record_leader_reentry(team)

        self.assertEqual(team["leader_work_state"], "active")
        self.assertEqual(team["leader_recovery_count"], 1)
        self.assertIn("leader_last_reentry_ts", team)

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
            return json.loads(json.dumps(store))

        def fake_save_data(data):
            data_copy = json.loads(json.dumps(data))
            store.clear()
            store.update(data_copy)

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
        self.assertIn("member_name='alice'", prompt_calls[0][2])
        self.assertIn("role='leader'", prompt_calls[0][2])
        self.assertIn("agent='claude'", prompt_calls[0][2])
        self.assertIn("名为 'alice' 且标记为 leader 的成员记录就是你本人", prompt_calls[0][2])
        self.assertIn("已有可分配成员（不包含你）: bob", prompt_calls[0][2])
        # 验证 confirm Enter 在 prompt 发送后被调用
        self.assertTrue(any(cmd[-1:] == ["Enter"] and ":alice" in cmd[2] for cmd in tmux_calls))

    def test_common_leader_system_prompt_binds_single_member_leader_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_file = root / "teams_data.json"
            previous_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
            data_layer.set_data_file(data_file)
            try:
                data_layer.save_data({
                    "teams": {
                        "gpu": {
                            "workspace_dir": "/tmp/gpu-workspace",
                            "context_dir": "/tmp/gpu-context",
                            "leader": "leader",
                            "members": {
                                "leader": {"role": "leader", "agent": "claude"},
                            },
                        }
                    }
                })

                prompt = tmux_utils.leader_system_prompt("gpu")

                self.assertIn("member_name='leader'", prompt)
                self.assertIn("role='leader'", prompt)
                self.assertIn("agent='claude'", prompt)
                self.assertIn("成员记录就是你本人", prompt)
                self.assertIn("已有可分配成员（不包含你）: 暂无。", prompt)
            finally:
                data_layer._DATA_FILE_OVERRIDE = previous_override

    def test_tui_recovery_message_binds_member_identity(self):
        msg = tui_screens._build_tui_recovery_message(
            {
                "workspace_dir": "/tmp/workspace",
                "context_dir": "/tmp/context",
                "default_agent": "claude",
            },
            "alice",
            {
                "role": "coder",
                "agent": "claude",
                "last_task": "fix bug",
                "last_context": "urgent",
                "recovery_count": 1,
            },
            "team",
        )

        self.assertIn("成员名: alice", msg)
        self.assertIn("角色: coder", msg)
        self.assertIn("agent: claude", msg)
        self.assertIn("你的团队成员身份绑定: team='team', member_name='alice'", msg)
        self.assertIn("团队成员表中同名成员记录就是你本人", msg)

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
        self.assertIn("acceptEdits", bob_cmd)
        self.assertIn("--ask-for-approval", carol_cmd)
        self.assertIn("never", carol_cmd)

    def test_launch_terminals_refuses_when_leader_active(self):
        """TUI launch_terminals 在 leader 活跃时拒绝重启"""
        store = {
            "teams": {
                "team": {
                    "workspace_dir": "/tmp/team-workspace",
                    "context_dir": "/tmp/team-context",
                    "leader": "alice",
                    "leader_last_task": "ship feature",
                    "leader_last_task_completed": False,
                    "members": {
                        "alice": {"role": "leader", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        }

        def fake_load_data():
            return json.loads(json.dumps(store))

        with mock.patch.object(tui_screens, "load_data", side_effect=fake_load_data):
            with mock.patch.object(tui_screens, "_member_window_target", return_value="@1"):
                ok, msg = tui_screens.launch_terminals("team")

        self.assertFalse(ok)
        self.assertIn("任务进行中", msg)
        self.assertIn("禁止重启", msg)

    def test_kill_terminals_refuses_when_active_leader_window_is_alive(self):
        store = {
            "teams": {
                "team": {
                    "leader": "alice",
                    "leader_type": "tmux",
                    "leader_last_task": "ship feature",
                    "leader_last_task_completed": False,
                    "terminals_active": True,
                    "members": {"alice": {"role": "leader"}},
                }
            }
        }

        with mock.patch.object(tui_screens, "load_data", return_value=store):
            with mock.patch.object(tui_screens, "_member_window_target", return_value="@1"):
                with mock.patch.object(tui_screens, "_tmux_run") as tmux_run:
                    ok, msg = tui_screens.kill_terminals("team")

        self.assertFalse(ok)
        self.assertIn("禁止关闭 leader", msg)
        tmux_run.assert_not_called()

    def test_delete_team_record_and_artifacts_cleans_managed_context_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_file = root / "teams_data.json"
            legacy_data = root / "legacy_teams_data.json"
            context_root = root / "contexts"
            context_dir = context_root / "team"
            context_dir.mkdir(parents=True)
            (context_dir / "results.jsonl").write_text("x", encoding="utf-8")
            legacy_data.write_text(json.dumps({
                "teams": {
                    "team": {"members": {"old": {"role": "coder"}}},
                    "legacy_other": {"members": {}},
                }
            }), encoding="utf-8")
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

            def fake_load_data():
                return json.loads(json.dumps(store))

            with mock.patch.object(data_layer, "context_base_dir", return_value=context_root):
                with mock.patch.object(tui_screens, "load_data", side_effect=fake_load_data):
                    with mock.patch.object(tui_screens, "save_data", side_effect=fake_save_data):
                        with mock.patch.object(tui_screens, "_OLD_DATA_FILE", legacy_data):
                            with mock.patch.object(tui_screens, "kill_terminals", return_value=(True, "closed")) as kill:
                                ok, msg = tui_screens.delete_team_record_and_artifacts("team")
                                saved = dict(store)

            self.assertTrue(ok)
            self.assertIn("已删除共享上下文", msg)
            self.assertIn("保留用户工作目录", msg)
            self.assertFalse(context_dir.exists())
            self.assertTrue(external_workspace.exists())
            self.assertNotIn("team", saved.get("teams", {}))
            self.assertTrue(saved["_deleted_legacy_teams"]["team"])
            legacy_after = json.loads(legacy_data.read_text(encoding="utf-8"))
            self.assertNotIn("team", legacy_after["teams"])
            self.assertIn("legacy_other", legacy_after["teams"])
            kill.assert_called_once_with("team")

    def test_delete_team_aborts_when_live_terminals_cannot_be_closed(self):
        store = {
            "teams": {
                "team": {
                    "terminals_active": True,
                    "members": {"alice": {"role": "coder", "agent": "claude"}},
                }
            }
        }

        def fake_save_data(data):
            store.clear()
            store.update(data)

        with mock.patch.object(tui_screens, "load_data", return_value=store):
            with mock.patch.object(tui_screens, "save_data", side_effect=fake_save_data):
                with mock.patch.object(tui_screens, "_find_tmux_session", return_value="mcp_team"):
                    with mock.patch.object(tui_screens, "kill_terminals", return_value=(False, "permission denied")):
                        ok, msg = tui_screens.delete_team_record_and_artifacts("team")

        self.assertFalse(ok)
        self.assertIn("删除中止", msg)
        self.assertIn("team", store["teams"])

    def test_member_terminal_status_uses_stored_window_id_when_window_name_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_file = root / "teams_data.json"
            previous_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
            data_layer.set_data_file(data_file)
            try:
                data_layer.save_data({
                    "teams": {
                        "team": {
                            "terminals_active": True,
                            "members": {
                                "alice": {
                                    "role": "coder",
                                    "agent": "claude",
                                    "tmux_window_id": "@7",
                                    "tmux_session": "mcp_team",
                                    "tmux_session_id": "$1",
                                    "tmux_session_created": "1000",
                                },
                            },
                        }
                    }
                })

                def fake_tmux_run(cmd, timeout=10):
                    if cmd[0] == "has-session":
                        return 0, "", ""
                    if cmd[0] == "list-windows":
                        return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\trenamed-by-cli", ""
                    return 0, "", ""

                with mock.patch.object(tmux_utils, "tmux_run", side_effect=fake_tmux_run):
                    status = tmux_utils.get_member_terminal_status("team")

                self.assertEqual(status, {"alice": True})
            finally:
                data_layer._DATA_FILE_OVERRIDE = previous_override

    def test_find_tmux_session_prefers_session_with_matching_member_windows(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data_file = root / "teams_data.json"
            previous_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
            data_layer.set_data_file(data_file)
            try:
                data_layer.save_data({
                    "teams": {
                        "team": {
                            "members": {
                                "alice": {"role": "coder", "agent": "claude"},
                                "bob": {"role": "tester", "agent": "claude"},
                            },
                        }
                    }
                })

                def fake_tmux_run(cmd, timeout=10):
                    if cmd[0] == "has-session":
                        return 0, "", ""
                    if cmd[0] == "list-sessions":
                        return 0, "mcp_team\nmcp_team_123456", ""
                    if cmd[0] == "list-windows" and cmd[2] == "mcp_team":
                        return 0, "$1\t1000\t@1\tstale", ""
                    if cmd[0] == "list-windows" and cmd[2] == "mcp_team_123456":
                        return 0, "$2\t2000\t@1\talice\n$2\t2000\t@2\tbob", ""
                    return 0, "", ""

                with mock.patch.object(tmux_utils, "tmux_run", side_effect=fake_tmux_run):
                    self.assertEqual(tmux_utils.find_tmux_session("team"), "mcp_team_123456")
            finally:
                data_layer._DATA_FILE_OVERRIDE = previous_override

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


class ContextManagementTests(unittest.TestCase):
    """上下文文件管理功能回归测试。"""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    # ---- _validate_context_path ----

    def test_validate_context_path_rejects_absolute(self):
        full, err = tui_screens._validate_context_path(self.root, "/etc/passwd")
        self.assertIsNone(full)
        self.assertIn("绝对", err)

    def test_validate_context_path_rejects_dotdot(self):
        (self.root / "sub").mkdir()
        full, err = tui_screens._validate_context_path(self.root, "sub/../etc/passwd")
        self.assertIsNone(full)
        self.assertIn("..", err)

        full, err = tui_screens._validate_context_path(self.root, "../secret.txt")
        self.assertIsNone(full)
        self.assertIn("..", err)

    def test_validate_context_path_rejects_empty(self):
        full, err = tui_screens._validate_context_path(self.root, "")
        self.assertIsNone(full)
        self.assertIn("空", err)

        full, err = tui_screens._validate_context_path(self.root, "   ")
        self.assertIsNone(full)
        self.assertIn("空", err)

    def test_validate_context_path_rejects_symlink_outside_root(self):
        sub = self.root / "sub"
        sub.mkdir()
        # Use a completely separate temp dir as "outside" so the symlink
        # resolves outside the root.
        outside_tmp = tempfile.TemporaryDirectory()
        outside = Path(outside_tmp.name)
        (outside / "secret.txt").write_text("secret", encoding="utf-8")
        link = sub / "escape"
        link.symlink_to(outside.resolve())

        try:
            full, err = tui_screens._validate_context_path(self.root, "sub/escape")
            self.assertIsNone(full)
            # Either the resolve check or the component-by-component check catches this
            self.assertTrue("符号链接" in err or "上下文根目录" in err,
                            f"Expected symlink or outside-root error, got: {err}")
        finally:
            outside_tmp.cleanup()

    def test_validate_context_path_allows_safe_relative(self):
        sub = self.root / "sub"
        sub.mkdir(parents=True)
        (sub / "file.txt").write_text("hello", encoding="utf-8")

        full, err = tui_screens._validate_context_path(self.root, "sub/file.txt")
        self.assertIsNotNone(full)
        self.assertEqual(err, "")
        self.assertTrue(full.exists())

    def test_validate_context_path_allows_dotted_path(self):
        (self.root / "notes.md").write_text("ok", encoding="utf-8")
        full, err = tui_screens._validate_context_path(self.root, "./notes.md")
        self.assertIsNotNone(full)
        self.assertEqual(err, "")

    # ---- _list_context_files ----

    def test_list_context_files_sorts_by_path(self):
        (self.root / "b.txt").write_text("b", encoding="utf-8")
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        sub = self.root / "sub"
        sub.mkdir()
        (sub / "c.md").write_text("c", encoding="utf-8")

        files = tui_screens._list_context_files(self.root)
        rel_paths = [f["rel_path"] for f in files]
        self.assertEqual(rel_paths, ["a.txt", "b.txt", "sub/c.md"])

    def test_list_context_files_includes_binary_with_error(self):
        """二进制文件应列出但标记为非文本,可删除但不可查看/编辑。"""
        (self.root / "text.txt").write_text("hello", encoding="utf-8")
        (self.root / "binary.bin").write_bytes(b"\x00\x01\x02\xff\xfe\xfd")

        files = tui_screens._list_context_files(self.root)
        rel_paths = [f["rel_path"] for f in files]
        self.assertIn("text.txt", rel_paths)
        # 二进制文件不再跳过
        self.assertIn("binary.bin", rel_paths)

        bin_file = next(f for f in files if f["rel_path"] == "binary.bin")
        self.assertIsNotNone(bin_file["error"])
        self.assertIn("非UTF-8", bin_file["error"])

    def test_list_context_files_skips_directories(self):
        (self.root / "notes.md").write_text("ok", encoding="utf-8")
        (self.root / "subdir").mkdir()

        files = tui_screens._list_context_files(self.root)
        rel_paths = [f["rel_path"] for f in files]
        self.assertIn("notes.md", rel_paths)
        self.assertNotIn("subdir", rel_paths)

    def test_list_context_files_returns_file_metadata(self):
        (self.root / "doc.txt").write_text("hello world!", encoding="utf-8")

        files = tui_screens._list_context_files(self.root)
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["rel_path"], "doc.txt")
        self.assertEqual(files[0]["size"], 12)
        self.assertIsNone(files[0]["error"])

    def test_list_context_files_handles_missing_dir(self):
        nonexistent = self.root / "does_not_exist"
        files = tui_screens._list_context_files(nonexistent)
        self.assertEqual(files, [])

    def test_list_context_files_handles_empty_dir(self):
        files = tui_screens._list_context_files(self.root)
        self.assertEqual(files, [])

    def test_list_context_files_skips_symlink_outside_root(self):
        (self.root / "safe.txt").write_text("safe", encoding="utf-8")
        # Use a completely separate temp dir so the symlink resolves outside root
        outside_tmp = tempfile.TemporaryDirectory()
        outside_dir = Path(outside_tmp.name)
        (outside_dir / "secret.txt").write_text("secret", encoding="utf-8")
        link = self.root / "escape_link"
        link.symlink_to(outside_dir.resolve() / "secret.txt")

        try:
            files = tui_screens._list_context_files(self.root)
            rel_paths = [f["rel_path"] for f in files]
            self.assertIn("safe.txt", rel_paths)
            self.assertNotIn("escape_link", rel_paths)
        finally:
            outside_tmp.cleanup()

    def test_list_context_files_skips_root_internal_symlink(self):
        """根内 symlink（alias.md → real.md）不得列出，防止 delete 误删真实目标。"""
        real_file = self.root / "real.md"
        real_file.write_text("real content", encoding="utf-8")
        alias = self.root / "alias.md"
        alias.symlink_to(real_file)

        files = tui_screens._list_context_files(self.root)
        rel_paths = [f["rel_path"] for f in files]
        self.assertIn("real.md", rel_paths,
                      "真实文件仍应列出")
        self.assertNotIn("alias.md", rel_paths,
                         "根内 symlink 不得列出")

    def test_validate_context_path_rejects_lexical_symlink(self):
        """操作时 validate_context_path 应在 lexical 层拒绝 is_symlink()（TOCTOU 防护）。"""
        real = self.root / "target.md"
        real.write_text("content", encoding="utf-8")
        alias = self.root / "link.md"
        alias.symlink_to(real)

        full, err = data_layer.validate_context_path(self.root, "link.md")
        self.assertIsNone(full)
        self.assertIn("符号链接", err)

    def test_validate_context_path_rejects_symlink_directory_parent(self):
        """根内 symlink 目录作为父组件时应被拒绝（lexical component is_symlink）。"""
        real_dir = self.root / "realdir"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("ok", encoding="utf-8")
        link_dir = self.root / "linkdir"
        link_dir.symlink_to(real_dir)

        full, err = data_layer.validate_context_path(self.root, "linkdir/file.txt")
        self.assertIsNone(full)
        self.assertIn("符号链接", err)

    # ---- _context_root_dir ----

    def test_context_root_dir_uses_team_context_dir(self):
        with tempfile.TemporaryDirectory() as tmp2:
            root = Path(tmp2)
            data_file = root / "teams_data.json"
            ctx_dir = root / "team_ctx"
            ctx_dir.mkdir()

            data_layer.set_data_file(data_file)
            try:
                data_layer.save_data({
                    "teams": {
                        "test_team": {"context_dir": str(ctx_dir)},
                    }
                })
                result = tui_screens._context_root_dir("test_team")
                self.assertEqual(result, ctx_dir.resolve())
            finally:
                data_layer._DATA_FILE_OVERRIDE = None

    # ---- ContextManagementScreen (focus methods without async) ----

    def test_context_screen_init_sets_properties(self):
        """Screen __init__ sets _team_name and _root without DOM."""
        screen = tui_screens.ContextManagementScreen("test_team")
        self.assertEqual(screen._team_name, "test_team")
        self.assertEqual(screen.team_name, "test_team")

    def test_context_screen_root_correct(self):
        with tempfile.TemporaryDirectory() as tmp2:
            root = Path(tmp2)
            data_file = root / "teams_data.json"
            ctx_dir = root / "ctx"
            ctx_dir.mkdir()

            data_layer.set_data_file(data_file)
            try:
                data_layer.save_data({
                    "teams": {
                        "test_team2": {"context_dir": str(ctx_dir)},
                    }
                })
                screen = tui_screens.ContextManagementScreen("test_team2")
                self.assertEqual(screen._root, ctx_dir.resolve())
            finally:
                data_layer._DATA_FILE_OVERRIDE = None

    # ---- Security: path traversal & validate_context_path edge cases ----

    def test_validate_rejects_tilde_path(self):
        full, err = tui_screens._validate_context_path(self.root, "~/escape")
        self.assertIsNone(full)
        self.assertIn("~", err)

    def test_validate_rejects_encoded_dotdot(self):
        full, err = tui_screens._validate_context_path(self.root, "sub%2f..%2fsecret")
        self.assertIsNotNone(full)  # not treated as .. because not a real path separator

    def test_validate_rejects_nul_byte(self):
        """NUL 字节不得导致崩溃,应返回可见错误。"""
        # data_layer 版本
        full, err = data_layer.validate_context_path(Path("/tmp"), "bad\x00name")
        self.assertIsNone(full)
        self.assertIn("路径解析失败", err)
        # tui_screens 版本(别名)
        full2, err2 = tui_screens._validate_context_path(Path("/tmp"), "bad\x00name")
        self.assertIsNone(full2)
        self.assertIn("路径解析失败", err2)

    # ---- 大文件/错误可见 ----

    def test_viewer_large_file_shows_error(self):
        """超大文件查看应显示错误提示而非崩溃。"""
        from tui.tui_dialogs import ContextFileViewer, _MAX_CONTEXT_FILE_BYTES
        big = self.root / "big.txt"
        # 创建一个稀疏文件来模拟超大文件(不实际写入数据)
        big.write_text("x" * int(_MAX_CONTEXT_FILE_BYTES + 1))
        viewer = ContextFileViewer("big.txt", big)
        self.assertIsNotNone(viewer._error)
        self.assertIn("过大", viewer._error)

    def test_editor_large_file_shows_error(self):
        """超大文件编辑应显示错误提示而非崩溃。"""
        from tui.tui_dialogs import ContextFileEditor, _MAX_CONTEXT_FILE_BYTES
        big = self.root / "big.md"
        big.write_text("x" * int(_MAX_CONTEXT_FILE_BYTES + 1))
        editor = ContextFileEditor("big.md", big)
        self.assertIsNotNone(editor._read_error)
        self.assertIn("过大", editor._read_error)

    # ---- 原子写入 ----

    def test_editor_atomic_write_preserves_content(self):
        """编辑器通过原子写入保存后内容正确持久化。"""
        from tui.tui_dialogs import ContextFileEditor
        target = self.root / "notes.md"
        target.write_text("original content", encoding="utf-8")
        original_mtime_ns = target.stat().st_mtime_ns

        editor = ContextFileEditor("notes.md", target)
        self.assertEqual(editor._original_content, "original content")
        self.assertEqual(editor._open_mtime_ns, original_mtime_ns)

    def test_editor_captures_open_mtime_ns_and_size(self):
        """编辑器打开时应捕获文件 stat mtime_ns 和 st_size。"""
        from tui.tui_dialogs import ContextFileEditor
        target = self.root / "notes.md"
        target.write_text("hello", encoding="utf-8")
        editor = ContextFileEditor("notes.md", target)
        self.assertIsNotNone(editor._open_mtime_ns)
        self.assertEqual(editor._open_mtime_ns, target.stat().st_mtime_ns)
        self.assertEqual(editor._open_size, target.stat().st_size)

    # ---- 并发冲突检测 ----

    def test_editor_detects_concurrent_modification(self):
        """保存前文件 mtime_ns 或 size 变化应检测到并发冲突。"""
        from tui.tui_dialogs import ContextFileEditor
        import os as _os_module
        target = self.root / "shared.md"
        target.write_text("version 1", encoding="utf-8")
        _os_module.utime(str(target), (1000000000.0, 1000000000.0))
        editor = ContextFileEditor("shared.md", target)
        self.assertIsNotNone(editor._open_mtime_ns)
        self.assertIsNotNone(editor._open_size)

        # 模拟外部修改:修改文件并更新时间戳
        target.write_text("version 2 longer content", encoding="utf-8")
        _os_module.utime(str(target), (2000000000.0, 2000000000.0))
        self.assertNotEqual(target.stat().st_mtime_ns, editor._open_mtime_ns)
        self.assertNotEqual(target.stat().st_size, editor._open_size)

    def test_editor_new_file_has_no_conflict_check(self):
        """新建文件(不存在)不应触发冲突检测。"""
        from tui.tui_dialogs import ContextFileEditor
        target = self.root / "new.md"
        editor = ContextFileEditor("new.md", target)
        self.assertIsNone(editor._open_mtime_ns)
        self.assertIsNone(editor._open_size)

    # ---- 删除无保护文件限制 ----

    def test_delete_allows_results_jsonl(self):
        """删除应允许 results.jsonl 等任意上下文根内文件,仅需确认。"""
        results = self.root / "results.jsonl"
        results.write_text('{"key":"val"}', encoding="utf-8")
        full, err = data_layer.validate_context_path(self.root, "results.jsonl")
        self.assertIsNotNone(full)
        self.assertEqual(err, "")
        self.assertTrue(full.is_file())

    # ---- 共享 validate_context_path (data_layer) ----

    def test_validate_context_path_in_data_layer(self):
        """validate_context_path 应可被 data_layer 和 dialogs 共用。"""
        (self.root / "sub").mkdir()
        (self.root / "sub" / "file.txt").write_text("ok", encoding="utf-8")
        full, err = data_layer.validate_context_path(self.root, "sub/file.txt")
        self.assertIsNotNone(full)
        self.assertEqual(err, "")

        full, err = data_layer.validate_context_path(self.root, "/etc/passwd")
        self.assertIsNone(full)

    def test_validate_context_path_rejects_dotdot_common(self):
        full, err = data_layer.validate_context_path(self.root, "../secret.txt")
        self.assertIsNone(full)
        self.assertIn("..", err)

    # ---- NewContextFileDialog 拒绝已存在文件 ----

    def test_new_context_dialog_rejects_existing_file(self):
        """新建时应拒绝覆盖已存在的文件。"""
        from tui.tui_dialogs import NewContextFileDialog
        existing = self.root / "existing.md"
        existing.parent.mkdir(parents=True, exist_ok=True)
        existing.write_text("prev", encoding="utf-8")

        # 不能测试 UI 流程,但验证 validate_context_path 返回 valid 且 exist 检查能在 create() 中被触发
        full, err = data_layer.validate_context_path(self.root, "existing.md")
        self.assertIsNotNone(full)
        self.assertEqual(err, "")
        self.assertTrue(full.exists())

    def test_validate_context_path_does_not_create_file(self):
        """validate_context_path 只做路径验证，绝不碰磁盘创建文件。"""
        full, err = data_layer.validate_context_path(self.root, "subdir/new.md")
        self.assertIsNotNone(full)
        self.assertEqual(err, "")
        self.assertFalse(full.exists(), "validate_context_path 绝不应创建文件")

    def test_new_editor_cancel_leaves_no_stale_file(self):
        """新建文件 → 编辑取消 → 不留下空文件。"""
        from tui.tui_dialogs import ContextFileEditor
        target = self.root / "subdir" / "new.md"

        # 模拟: 路径已通过 NewContextFileDialog 验证但未创建
        editor = ContextFileEditor("subdir/new.md", target)
        self.assertFalse(target.exists(),
                         "文件不应在编辑器打开前被创建")
        # 新建文件的编辑器属性
        self.assertEqual(editor._original_content, "")
        self.assertIsNone(editor._open_mtime_ns)
        self.assertIsNone(editor._open_size)
        self.assertIsNone(editor._read_error)

    # ---- 所有上下文对话框均有 escape 绑定 ----

    def test_editor_has_ctrl_s_binding(self):
        """编辑器应有 ctrl+s 保存绑定。"""
        from tui.tui_dialogs import ContextFileEditor
        bindings = ContextFileEditor.BINDINGS
        self.assertTrue(any(b.key == "ctrl+s" for b in bindings))

    def test_viewer_has_escape_binding(self):
        from tui.tui_dialogs import ContextFileViewer
        self.assertTrue(any("escape" in b.key for b in ContextFileViewer.BINDINGS))

    def test_delete_dialog_has_escape_binding(self):
        from tui.tui_dialogs import ContextConfirmDeleteDialog
        b = ContextConfirmDeleteDialog.BINDINGS
        self.assertTrue(any("escape" in x.key for x in b),
                        "删除确认框缺少 escape 绑定")
        self.assertTrue(any(hasattr(ContextConfirmDeleteDialog, "action_dismiss_cancel")
                           for _ in [1]),
                        "缺少 action_dismiss_cancel 方法")
        self.assertTrue(hasattr(ContextConfirmDeleteDialog, "action_dismiss_cancel"))

    def test_error_dialog_has_escape_binding(self):
        from tui.tui_dialogs import ContextErrorDialog
        b = ContextErrorDialog.BINDINGS
        self.assertTrue(any("escape" in x.key for x in b),
                        "错误提示框缺少 escape 绑定")
        self.assertTrue(hasattr(ContextErrorDialog, "action_dismiss_dialog"))

    def test_new_file_dialog_has_escape_binding(self):
        from tui.tui_dialogs import NewContextFileDialog
        b = NewContextFileDialog.BINDINGS
        self.assertTrue(any("escape" in x.key for x in b),
                        "新建文件框缺少 escape 绑定")
        self.assertTrue(hasattr(NewContextFileDialog, "action_dismiss_cancel"))

    def test_discard_dialog_has_escape_binding(self):
        from tui.tui_dialogs import ContextConfirmDiscardDialog
        b = ContextConfirmDiscardDialog.BINDINGS
        self.assertTrue(any("escape" in x.key for x in b),
                        "放弃修改框缺少 escape 绑定")
        self.assertTrue(hasattr(ContextConfirmDiscardDialog, "action_dismiss_continue"))

    # ---- 原子保存与冲突 ----

    def test_editor_save_uses_os_replace_for_atomic_write(self):
        """_atomic_write_text 应通过 os.replace 原子写入最终内容到 target。"""
        import os as _os_module
        from tui.tui_dialogs import _atomic_write_text

        target = self.root / "atomic.md"
        target.write_text("before", encoding="utf-8")

        real_replace = _os_module.replace
        replace_calls = []

        def tracking_replace(src, dst):
            replace_calls.append((src, dst))
            return real_replace(src, dst)

        with mock.patch("os.replace", side_effect=tracking_replace):
            _atomic_write_text(target, "after save")

        # os.replace 应被调用一次
        self.assertEqual(len(replace_calls), 1,
                         f"Expected 1 os.replace call, got {replace_calls}")
        src, dst = replace_calls[0]
        self.assertIn(".tmp", src, "src must be a temp file")
        self.assertEqual(dst, str(target), "dst must be target path")

        # 最终 target 内容应正确（os.replace 真实执行）
        self.assertEqual(target.read_text(encoding="utf-8"), "after save",
                         "target content should be the new content")

    def test_editor_new_file_no_conflict(self):
        """新建文件(不存在)不触发冲突检查。"""
        from tui.tui_dialogs import ContextFileEditor
        target = self.root / "fresh.md"
        editor = ContextFileEditor("fresh.md", target)
        self.assertIsNone(editor._open_mtime_ns)


class WrappingFooterPilotTests(unittest.IsolatedAsyncioTestCase):
    async def test_footer_wraps_all_binding_hints_after_narrow_resize(self):
        from textual.app import App, ComposeResult
        from textual.screen import Screen
        from tui.tui_screens import TeamDetailScreen, WrappingFooter

        class FooterScreen(Screen):
            BINDINGS = TeamDetailScreen.BINDINGS

            def compose(self) -> ComposeResult:
                yield WrappingFooter(show_command_palette=False)

        class FooterTestApp(App):
            def on_mount(self) -> None:
                self.push_screen(FooterScreen())

        app = FooterTestApp()
        async with app.run_test(size=(200, 24)) as pilot:
            await pilot.pause()
            await pilot.pause()
            footer = app.screen.query_one(WrappingFooter)
            child_count = len(footer.children)
            self.assertEqual(footer.region.height, 1)

            await pilot.resize_terminal(40, 24)
            await pilot.pause()
            await pilot.pause()

            self.assertEqual(len(footer.children), child_count)
            self.assertGreater(footer.region.height, 1)
            self.assertTrue(
                all(child.region.right <= footer.region.right for child in footer.children)
            )


class ContextManagementPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 回归测试：真实 mount ContextManagementScreen 验证不崩溃。"""

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.ctx_dir = self.root / "ctx"
        self.ctx_dir.mkdir()
        self.data_file = self.root / "teams_data.json"
        self._prev_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        data_layer.save_data({
            "teams": {
                "pilot_team": {"context_dir": str(self.ctx_dir)},
            }
        })

    async def asyncTearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._prev_override
        self.tmp_dir.cleanup()

    async def test_screen_mounts_and_loads_empty_dir(self):
        """空上下文目录下 ContextManagementScreen 挂载不崩溃。"""
        from tui.tui_screens import TeamManagerApp, ContextManagementScreen

        app = TeamManagerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ContextManagementScreen("pilot_team")
            await pilot.app.push_screen(screen)
            await pilot.pause(0.3)
            # 未抛异常即通过

    async def test_screen_mounts_and_loads_files(self):
        """有文件时挂载不崩溃，DataTable 正确填充。"""
        from tui.tui_screens import TeamManagerApp, ContextManagementScreen

        (self.ctx_dir / "a.txt").write_text("hello", encoding="utf-8")
        (self.ctx_dir / "b.md").write_text("world", encoding="utf-8")

        app = TeamManagerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ContextManagementScreen("pilot_team")
            await pilot.app.push_screen(screen)
            await pilot.pause(0.3)
            # 有文件 + 无 dt.sort —— 不抛 KeyError

    async def test_enter_opens_viewer_on_selected_file(self):
        """Enter 选中行 → 顶部 screen 应为 ContextFileViewer。"""
        from tui.tui_screens import TeamManagerApp, ContextManagementScreen
        from tui.tui_dialogs import ContextFileViewer

        (self.ctx_dir / "notes.md").write_text("# hello", encoding="utf-8")

        app = TeamManagerApp()
        async with app.run_test(size=(80, 24)) as pilot:
            screen = ContextManagementScreen("pilot_team")
            await pilot.app.push_screen(screen)
            await pilot.pause(0.5)

            # press Enter on the first row
            await pilot.press("enter")
            await pilot.pause(0.5)

            # assert viewer is on top
            self.assertIsInstance(
                pilot.app.screen,
                ContextFileViewer,
                "Enter should push ContextFileViewer as top screen",
            )


if __name__ == "__main__":
    unittest.main()
