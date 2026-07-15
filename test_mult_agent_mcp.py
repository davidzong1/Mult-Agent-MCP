import json
import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp


class MultAgentMcpContextTests(unittest.TestCase):
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
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
        }
        self.old_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux_window_exists": mcp._tmux_window_exists,
            "_tmux": mcp._tmux,
        }
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
        }

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
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

    def test_default_workspace_skips_internal_team_workspace(self):
        real_workspace = self.root / "real_workspace"
        real_workspace.mkdir()
        internal_workspace = Path(mcp.TEAM_WORKSPACES_DIR) / "team"
        internal_workspace.mkdir(parents=True)

        os.environ["PWD"] = str(internal_workspace)
        os.environ["INIT_CWD"] = str(real_workspace)

        self.assertEqual(mcp._default_workspace_dir(), str(real_workspace))

    def test_team_dir_and_context_dir_use_persisted_team_settings(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {},
                }
            }
        })

        self.assertEqual(mcp._team_dir("team"), str(workspace))
        self.assertEqual(mcp._share_dir("team"), str(context))

    def test_launch_team_terminals_injects_prompt_for_claude_leader(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "alice",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "members": {
                        "alice": {"role": "leader", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        tmux_calls = []
        prompt_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text, **kwargs: prompt_calls.append((session, window, text)) or (0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team", task="investigate Claude leader context")

        self.assertIn("终端已启动", result)
        self.assertTrue(any(cmd and cmd[0] == "new-session" for cmd in tmux_calls))
        self.assertEqual(len(prompt_calls), 2)
        self.assertEqual(prompt_calls[0][0], "mcp_team")
        self.assertEqual(prompt_calls[0][1], "bob")
        self.assertIn("member_report_result", prompt_calls[0][2])
        self.assertEqual(prompt_calls[1][0], "mcp_team")
        self.assertEqual(prompt_calls[1][1], "alice")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的 leader", prompt_calls[1][2])
        self.assertIn("investigate Claude leader context", prompt_calls[1][2])

    def test_leader_set_member_mode_maps_claude_and_codex_startup_args(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(self.root / "context"),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "codex"},
                    },
                }
            }
        })

        result = mcp.leader_set_member_mode("team", "*", "auto")
        self.assertIn("alice, bob → auto", result)

        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))
                mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(workspace))

        self.assertIn("--permission-mode", calls[0])
        self.assertIn("auto", calls[0])
        self.assertIn("--ask-for-approval", calls[1])
        self.assertIn("never", calls[1])

    def test_leader_monitor_marks_idle_member_complete(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(self.root / "context"),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "finish task",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_capture_window", return_value=(0, "✻ Brewed for 5s\n❯\n⏸ manual mode on", "")):
                    result = mcp.leader_monitor_members("team")

        self.assertIn("alice: idle (marked-complete)", result)
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        self.assertTrue(member["last_task_completed"])
        self.assertEqual(member["last_observed_state"], "idle")

    def test_leader_monitor_auto_authorizes_auto_member(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(self.root / "context"),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "work_mode": "auto",
                            "auto_authorize": True,
                            "last_task": "run command",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })
        auth_calls = []

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_capture_window", return_value=(0, "This command requires approval\nDo you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again", "")):
                    with mock.patch.object(mcp, "_send_authorization_choice", side_effect=lambda session, member, choice: auth_calls.append((session, member, choice)) or (0, "")):
                        result = mcp.leader_monitor_members("team")

        self.assertIn("auto-authorized:session", result)
        self.assertEqual(auth_calls, [("mcp_team", "alice", "2")])
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_observed_state"], "busy")
        self.assertNotIn("blocked_reason", member)

    def test_member_report_result_writes_compressed_context(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "",
                    "leader_type": "",
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "implement context reporting",
                            "last_context": "shared requirements",
                            "last_task_completed": False,
                        }
                    },
                }
            }
        })

        result = mcp.member_report_result(
            "team",
            "Implemented context reporting and tests.",
            member_name="alice",
            artifact_path="reports/alice.md",
            compressed_context="Changed result reporting to emit compact context files.",
        )

        self.assertIn("压缩上下文", result)
        results_file = context / "results.jsonl"
        entry = json.loads(results_file.read_text(encoding="utf-8").splitlines()[-1])
        context_path = context / entry["compressed_context_path"]
        self.assertTrue(context_path.exists())
        self.assertIn("Changed result reporting", context_path.read_text(encoding="utf-8"))

    def test_file_lock_blocks_other_members_and_can_be_released(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        acquired = mcp.member_acquire_file_lock("team", "alice", "app.py", "edit handler", 300)
        blocked = mcp.member_acquire_file_lock("team", "bob", "app.py", "edit tests", 300)
        released = mcp.member_release_file_lock("team", "alice", "app.py")
        reacquired = mcp.member_acquire_file_lock("team", "bob", "app.py", "edit tests", 300)

        self.assertIn("已获得文件锁", acquired)
        self.assertIn("已被 alice 锁定", blocked)
        self.assertIn("已释放文件锁", released)
        self.assertIn("已获得文件锁", reacquired)

    def test_leader_authorize_member_sends_choice_and_enter(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "direct",
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            return 0, "", ""

        mcp._find_any_session = lambda team: "mcp_team"
        mcp._tmux_window_exists = lambda team, window: True
        mcp._tmux = fake_tmux

        result = mcp.leader_authorize_member("team", "alice", "session")

        self.assertIn("已向成员 'alice' 发送授权选择", result)
        self.assertEqual(
            calls,
            [
                ["send-keys", "-t", "mcp_team:alice", "2", "Enter"],
            ],
        )

    def test_send_authorization_choice_retries_failed_tmux_send(self):
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if len(calls) == 1:
                return 1, "", "pane busy"
            return 0, "", ""

        mcp._tmux = fake_tmux

        with mock.patch.object(mcp.time, "sleep", return_value=None):
            rc, err = mcp._send_authorization_choice("mcp_team", "alice", "2")

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(
            calls,
            [
                ["send-keys", "-t", "mcp_team:alice", "2", "Enter"],
                ["send-keys", "-t", "mcp_team:alice", "2", "Enter"],
            ],
        )

    def test_leader_authorize_member_rejects_invalid_choice(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "terminals_active": True,
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        mcp._find_any_session = lambda team: "mcp_team"
        mcp._tmux_window_exists = lambda team, window: True

        result = mcp.leader_authorize_member("team", "alice", "maybe")

        self.assertIn("无效授权选项", result)

    def test_leader_read_member_terminal_captures_target_window(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "terminals_active": True,
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            return 0, "approval prompt", ""

        mcp._find_any_session = lambda team: "mcp_team"
        mcp._tmux_window_exists = lambda team, window: True
        mcp._tmux = fake_tmux

        result = mcp.leader_read_member_terminal("team", "alice", 120)

        self.assertIn("approval prompt", result)
        self.assertEqual(
            calls,
            [["capture-pane", "-t", "mcp_team:alice", "-p", "-S", "-120"]],
        )

    def test_write_claude_permissions_defaults(self):
        """验证 _write_claude_permissions 默认生成正确的白名单 rules"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {},
                }
            }
        })

        path = mcp._write_claude_permissions("team")
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            settings = json.load(f)

        perms = settings["permissions"]
        self.assertNotIn("allow-dangerously-skip-permissions", perms)
        allow = perms["allow"]
        self.assertTrue(any("Edit(" + str(workspace) in r for r in allow))
        self.assertTrue(any("Write(" + str(workspace) in r for r in allow))
        self.assertTrue(any("Bash(git:*)" in r for r in allow))

    def test_write_claude_permissions_dangerously_skip(self):
        """验证 _write_claude_permissions 危险模式"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {},
                }
            }
        })

        path = mcp._write_claude_permissions("team", dangerously_skip=True)
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            settings = json.load(f)

        self.assertTrue(settings["permissions"]["allow-dangerously-skip-permissions"])

    def test_write_claude_permissions_extra_patterns(self):
        """验证 _write_claude_permissions 合并额外白名单规则"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {},
                }
            }
        })

        path = mcp._write_claude_permissions(
            "team",
            allow_patterns=["Bash(npm:*)", "Read(/data/*)"],
            additional_dirs=["/tmp/my_share"],
        )
        self.assertTrue(os.path.exists(path))
        with open(path) as f:
            settings = json.load(f)

        allow = settings["permissions"]["allow"]
        # 额外 pattern 被保留
        self.assertTrue(any("Bash(npm:*)" in r for r in allow))
        self.assertTrue(any("Read(/data/*)" in r for r in allow))
        # shared dir 自动生成 Edit + Write
        self.assertTrue(any("Edit(/tmp/my_share/*)" in r for r in allow))
        self.assertTrue(any("Write(/tmp/my_share/*)" in r for r in allow))
        # 默认项目目录 rules 仍然存在
        self.assertTrue(any("Edit(" + str(workspace) in r for r in allow))

    def test_leader_configure_member_permissions(self):
        """验证 leader_configure_member_permissions 端到端"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        result = mcp.leader_configure_member_permissions(
            "team",
            allow_patterns="Bash(python:*),Read(/mnt/data/*)",
        )
        self.assertIn("已配置", result)
        settings_path = mcp._claude_settings_json_path("team")
        self.assertTrue(os.path.exists(settings_path))
        with open(settings_path) as f:
            settings = json.load(f)
        allow = settings["permissions"]["allow"]
        self.assertTrue(any("Bash(python:*)" in r for r in allow))
        self.assertTrue(any("Read(/mnt/data/*)" in r for r in allow))

    def test_leader_configure_member_permissions_dangerously(self):
        """验证 leader_configure_member_permissions 危险模式"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {},
                }
            }
        })

        result = mcp.leader_configure_member_permissions("team", dangerously_skip=True)
        self.assertIn("跳过全部权限检查", result)
        settings_path = mcp._claude_settings_json_path("team")
        self.assertTrue(os.path.exists(settings_path))
        with open(settings_path) as f:
            settings = json.load(f)
        self.assertTrue(settings["permissions"]["allow-dangerously-skip-permissions"])

    # ============================================================
    # 恢复上下文测试 (Task 1)
    # ============================================================

    def test_build_recovery_context_contains_key_info(self):
        """验证 _build_recovery_context 包含团队、目录、任务信息"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "fix login bug",
                            "last_context": "urgent fix needed",
                            "last_task_completed": False,
                            "recovery_count": 0,
                        },
                    },
                }
            }
        })

        msg = mcp._build_recovery_context("team", "alice")

        self.assertIn("终端恢复通知", msg)
        self.assertIn("团队: team", msg)
        self.assertIn("角色: coder", msg)
        self.assertIn(str(workspace), msg)
        self.assertIn(str(context), msg)
        self.assertIn("fix login bug", msg)
        self.assertIn("urgent fix needed", msg)
        self.assertIn("member_read_shared", msg)
        self.assertIn("member_report_result", msg)

    def test_build_recovery_context_shows_recovery_count(self):
        """验证 recovery_count 在恢复消息中正确显示"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "bob": {
                            "role": "tester",
                            "agent": "codex",
                            "last_task": "",
                            "last_context": "",
                            "last_task_completed": True,
                            "recovery_count": 2,
                        },
                    },
                }
            }
        })

        msg = mcp._build_recovery_context("team", "bob")

        self.assertIn("第3次恢复", msg)  # recovery_count=2 → 显示"第3次"
        self.assertIn("角色: tester", msg)

    def test_record_recovery_event_writes_to_results_jsonl(self):
        """验证 _record_recovery_event 写入 results.jsonl"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        mcp._record_recovery_event("team", "alice", had_task=True)

        results_file = context / "results.jsonl"
        self.assertTrue(results_file.exists())
        entries = [json.loads(line) for line in results_file.read_text().splitlines() if line.strip()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["member"], "alice")
        self.assertEqual(entries[0]["event"], "terminal_recovery")
        self.assertTrue(entries[0]["had_unfinished_task"])

    def test_save_death_context_snapshot_creates_file(self):
        """验证 _save_death_context_snapshot 在 member_contexts/ 下创建快照"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "implement auth",
                            "last_context": "",
                            "last_task_completed": False,
                            "recovery_count": 0,
                        },
                    },
                }
            }
        })

        rel_path = mcp._save_death_context_snapshot("team", "alice")

        snapshot_file = context / rel_path
        self.assertTrue(snapshot_file.exists())
        content = snapshot_file.read_text()
        self.assertIn("Recovery Snapshot: alice", content)
        self.assertIn("implement auth", content)
        self.assertIn("terminal_died", content)

    def test_recover_and_send_updates_recovery_count(self):
        """验证 _recover_and_send 更新 recovery_count 和 last_recovery_ts"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "write tests",
                            "last_context": "",
                            "last_task_completed": False,
                            "recovery_count": 1,
                        },
                    },
                }
            }
        })

        # Mock tmux 操作以避免实际执行
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            return 0, "", ""

        mcp._find_any_session = lambda team: "mcp_team"
        mcp._tmux_window_exists = lambda team, window: False
        mcp._tmux = fake_tmux

        ok, err = mcp._recover_and_send("team", "alice", "mcp_team")

        # 验证恢复计数更新
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["recovery_count"], 2)
        self.assertIn("last_recovery_ts", member)
        self.assertIn("last_terminal_death_ts", member)

        # 验证 recovery 事件写入 results.jsonl
        results_file = context / "results.jsonl"
        self.assertTrue(results_file.exists())

    def test_recover_and_send_returns_error_for_missing_member(self):
        """验证 _recover_and_send 对不存在的成员返回错误"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "members": {},
                }
            }
        })

        ok, err = mcp._recover_and_send("team", "nonexistent", "mcp_team")
        self.assertFalse(ok)
        self.assertIn("不存在", err)


if __name__ == "__main__":
    unittest.main()
