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

    def test_migration_merges_legacy_project_and_home_data(self):
        home_data = Path(mcp.DATA_FILE)
        legacy_data = Path(mcp._OLD_DATA_FILE)
        home_data.parent.mkdir(parents=True, exist_ok=True)
        legacy_data.parent.mkdir(parents=True, exist_ok=True)
        legacy_context = Path(mcp._OLD_SHARE_CONTEXT_DIR) / "legacy_team"

        home_data.write_text(json.dumps({
            "teams": {
                "cpp_ipc_dds": {
                    "description": "new team",
                    "workspace_dir": "/home/zwc/cpp_ipc_dds",
                    "context_dir": str(Path(mcp.SHARE_CONTEXT_DIR) / "cpp_ipc_dds"),
                    "members": {"leader": {"role": "leader", "agent": "codex"}},
                },
                "shared": {
                    "description": "home wins",
                    "workspace_dir": "/home/new",
                    "context_dir": str(Path(mcp.SHARE_CONTEXT_DIR) / "shared"),
                    "members": {"leader": {"role": "leader", "agent": "codex"}},
                },
            },
            "_deleted_legacy_teams": {"deleted_team": True},
        }), encoding="utf-8")
        legacy_data.write_text(json.dumps({
            "teams": {
                "legacy_team": {
                    "description": "old team",
                    "workspace_dir": "/home/old",
                    "context_dir": str(legacy_context),
                    "members": {"worker": {"role": "coder", "agent": "claude"}},
                },
                "shared": {
                    "description": "legacy should not overwrite",
                    "members": {
                        "leader": {"role": "member", "agent": "claude"},
                        "reviewer": {"role": "reviewer", "agent": "claude"},
                    },
                },
                "deleted_team": {
                    "description": "must not resurrect",
                    "members": {"worker": {"role": "coder", "agent": "claude"}},
                },
            }
        }), encoding="utf-8")

        mcp._migrate_if_needed()
        data = mcp._load()

        self.assertIn("cpp_ipc_dds", data["teams"])
        self.assertIn("legacy_team", data["teams"])
        self.assertNotIn("deleted_team", data["teams"])
        self.assertEqual(data["teams"]["shared"]["description"], "home wins")
        self.assertEqual(data["teams"]["shared"]["members"]["leader"]["agent"], "codex")
        self.assertIn("reviewer", data["teams"]["shared"]["members"])
        self.assertEqual(
            data["teams"]["legacy_team"]["context_dir"],
            str(Path(mcp.SHARE_CONTEXT_DIR) / "legacy_team"),
        )

    def test_delete_team_cleans_managed_artifacts_and_keeps_user_workspace(self):
        context = Path(mcp.SHARE_CONTEXT_DIR) / "team"
        context.mkdir(parents=True)
        (context / "results.jsonl").write_text("x", encoding="utf-8")
        internal_workspace = Path(mcp.TEAM_WORKSPACES_DIR) / "team"
        internal_workspace.mkdir(parents=True)
        (internal_workspace / "scratch.txt").write_text("x", encoding="utf-8")
        legacy_data = Path(mcp._OLD_DATA_FILE)
        legacy_data.write_text(json.dumps({
            "teams": {
                "team": {"members": {"old": {"role": "coder"}}},
                "legacy_other": {"members": {}},
            }
        }), encoding="utf-8")
        user_workspace = self.root / "user_workspace"
        user_workspace.mkdir()

        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(internal_workspace),
                    "context_dir": str(context),
                    "members": {},
                },
                "external": {
                    "workspace_dir": str(user_workspace),
                    "context_dir": str(Path(mcp.SHARE_CONTEXT_DIR) / "external"),
                    "members": {},
                },
            }
        })

        with mock.patch.object(mcp, "_kill_session", return_value=None):
            result = mcp.delete_team("team")

        self.assertIn("已删除", result)
        self.assertFalse(context.exists())
        self.assertFalse(internal_workspace.exists())
        self.assertTrue(user_workspace.exists())
        data = mcp._load()
        self.assertNotIn("team", data["teams"])
        self.assertIn("external", data["teams"])
        self.assertTrue(data["_deleted_legacy_teams"]["team"])
        legacy_after = json.loads(legacy_data.read_text(encoding="utf-8"))
        self.assertNotIn("team", legacy_after["teams"])
        self.assertIn("legacy_other", legacy_after["teams"])

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
        inject_calls = []
        member_prompt_calls = []

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
                        with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text, **kwargs: member_prompt_calls.append((session, window, text)) or (0, "")):
                            with mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda session, leader, prompt: inject_calls.append((session, leader, prompt)) or (0, "")):
                                with mock.patch.object(mcp.time, "sleep", return_value=None):
                                    result = mcp.launch_team_terminals("team", task="investigate Claude leader context")

        self.assertIn("终端已启动", result)
        self.assertTrue(any(cmd and cmd[0] == "new-session" for cmd in tmux_calls))
        leader_cmd = next(cmd for cmd in tmux_calls if cmd and cmd[0] == "new-session")
        self.assertIn("--allowedTools", leader_cmd)
        leader_tools = leader_cmd[leader_cmd.index("--allowedTools") + 1]
        self.assertIn("mcp__mult-agent-mcp__leader_*", leader_tools)
        self.assertIn("mcp__mult_agent_mcp__leader_*", leader_tools)
        self.assertNotIn("member_*", leader_tools)

        # member bob 的初始上下文仍通过 _send_keys 发送
        self.assertEqual(len(member_prompt_calls), 1)
        self.assertEqual(member_prompt_calls[0][0], "mcp_team")
        self.assertEqual(member_prompt_calls[0][1], "bob")
        self.assertIn("member_report_result", member_prompt_calls[0][2])
        self.assertIn("你的团队成员身份绑定: team='team', member_name='bob'", member_prompt_calls[0][2])
        self.assertIn("role='coder'", member_prompt_calls[0][2])
        self.assertIn("agent='claude'", member_prompt_calls[0][2])
        self.assertIn("团队成员表中同名成员记录就是你本人", member_prompt_calls[0][2])

        # Claude leader 的 prompt 通过 _inject_claude_leader_prompt 注入
        self.assertEqual(len(inject_calls), 1)
        self.assertEqual(inject_calls[0][0], "mcp_team")
        self.assertEqual(inject_calls[0][1], "alice")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的 leader", inject_calls[0][2])
        self.assertIn("member_name='alice'", inject_calls[0][2])
        self.assertIn("role='leader'", inject_calls[0][2])
        self.assertIn("agent='claude'", inject_calls[0][2])
        self.assertIn("名为 'alice' 且标记为 leader 的成员记录就是你本人", inject_calls[0][2])
        self.assertIn("不要把自己的 leader 成员记录当作可分配对象", inject_calls[0][2])
        self.assertIn("已有可分配成员（不包含你）: bob", inject_calls[0][2])
        self.assertIn("investigate Claude leader context", inject_calls[0][2])

    def test_leader_system_prompt_binds_single_member_leader_identity(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "gpu": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "leader",
                    "leader_type": "tmux",
                    "members": {
                        "leader": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        })

        prompt = mcp._leader_system_prompt("gpu")

        self.assertIn("member_name='leader'", prompt)
        self.assertIn("role='leader'", prompt)
        self.assertIn("agent='claude'", prompt)
        self.assertIn("成员记录就是你本人", prompt)
        self.assertIn("已有可分配成员（不包含你）: 暂无。", prompt)

    def test_leader_system_prompt_includes_unfinished_recovery_state(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_last_task": "ship recovery feature",
                    "leader_last_task_completed": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "implement state model",
                            "last_context": "leader crashed",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })

        prompt = mcp._leader_system_prompt("team")

        self.assertIn("Leader 恢复状态", prompt)
        self.assertIn("检测到未完成团队工作", prompt)
        self.assertIn("未完成总任务: ship recovery feature", prompt)
        self.assertIn("alice(role=coder, agent=claude): implement state model", prompt)
        self.assertIn("leader_get_recovery_context", prompt)
        self.assertIn("leader_mark_task_complete", prompt)

    def test_leader_recovery_context_enters_standby_after_mark_complete(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_last_task": "done work",
                    "leader_last_task_completed": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "done",
                            "last_task_completed": True,
                        },
                    },
                }
            }
        })

        result = mcp.leader_mark_task_complete("team", summary="all done", artifact_path="report.md")
        context_msg = mcp.leader_get_recovery_context("team")
        data = mcp._load()

        self.assertIn("已标记完成", result)
        self.assertTrue(data["teams"]["team"]["leader_last_task_completed"])
        self.assertEqual(data["teams"]["team"]["leader_work_state"], "idle")
        self.assertIn("模式: 待机", context_msg)
        self.assertIn("进入正常待机状态", context_msg)
        self.assertTrue((context / "results.jsonl").exists())

    def test_leader_mark_complete_keeps_active_when_member_task_unfinished(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_last_task": "leader done but member pending",
                    "leader_last_task_completed": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "still working",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })

        result = mcp.leader_mark_task_complete("team", summary="leader portion done")
        team = mcp._load()["teams"]["team"]

        self.assertIn("仍检测到未完成成员任务", result)
        self.assertTrue(team["leader_last_task_completed"])
        self.assertEqual(team["leader_work_state"], "active")

    def test_leader_recovery_prompt_compacts_long_member_task_list(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        members = {"lead": {"role": "leader", "agent": "claude"}}
        long_task = "x" * 800
        for idx in range(10):
            members[f"m{idx}"] = {
                "role": "coder",
                "agent": "claude",
                "last_task": long_task,
                "last_context": "context " + ("y" * 400),
                "last_task_completed": False,
            }
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": members,
                }
            }
        })

        prompt = mcp._leader_system_prompt("team")

        self.assertIn("[truncated]", prompt)
        self.assertIn("另有 2 个未完成成员任务", prompt)
        self.assertIn("leader_list_team、leader_monitor_members 和 member_read_shared", prompt)

    def test_leader_recovery_prompt_compacts_long_leader_task_and_context(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        long_task = "task-" + ("x" * 900)
        long_context = "context-" + ("y" * 900)
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_last_task": long_task,
                    "leader_last_context": long_context,
                    "leader_last_task_completed": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        })

        prompt = mcp._leader_system_prompt("team")

        self.assertIn("- 未完成总任务: task-", prompt)
        self.assertIn("- 总任务上下文: context-", prompt)
        self.assertIn("[truncated]", prompt)
        self.assertNotIn("x" * 700, prompt)
        self.assertNotIn("y" * 700, prompt)

    def test_launch_team_terminals_persists_leader_task_for_recovery(self):
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

        with mock.patch.object(mcp, "_tmux", side_effect=lambda cmd, timeout=10: (1, "", "") if cmd[0] == "has-session" else (0, "", "")):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                            with mock.patch.object(mcp, "_inject_claude_leader_prompt", return_value=(0, "")):
                                with mock.patch.object(mcp.time, "sleep", return_value=None):
                                    result = mcp.launch_team_terminals("team", task="recover interrupted leader")

        data = mcp._load()
        team = data["teams"]["team"]
        self.assertIn("终端已启动", result)
        self.assertEqual(team["leader_last_task"], "recover interrupted leader")
        self.assertFalse(team["leader_last_task_completed"])
        self.assertEqual(team["leader_work_state"], "active")

    def test_leader_list_team_marks_tmux_leader_member_as_self(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "alice",
                    "leader_type": "tmux",
                    "members": {
                        "alice": {"role": "leader", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_tmux_session_alive", return_value=True):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                result = mcp.leader_list_team("team")

        self.assertIn("alice 👑LEADER ← 你自己", result)
        self.assertIn("默认成员 agent: claude", result)
        self.assertIn("bob [coder]", result)

    def test_leader_list_team_marks_direct_leader_member_as_self(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "alice",
                    "leader_type": "direct",
                    "members": {
                        "alice": {"role": "member", "agent": "codex"},
                        "bob": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        result = mcp.leader_list_team("team")

        self.assertIn("alice 👑DIRECT-LEADER ← 你自己", result)
        self.assertIn("bob [coder]", result)

    def test_team_get_default_agent_reports_current_default(self):
        mcp._save({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "members": {},
                }
            }
        })

        result = mcp.team_get_default_agent("team")

        self.assertIn("默认成员 agent: claude [claude]", result)

    def test_claude_mcp_configured_rejects_legacy_sse_config(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {"alice": {"role": "leader", "agent": "claude"}},
                }
            }
        })

        mcp_json = Path(mcp._claude_mcp_json_path("team"))
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

        self.assertFalse(mcp._claude_mcp_configured("team"))
        status = mcp.check_agent_setup("team")
        self.assertIn("旧 teamMCP 配置格式", status)

        mcp._write_claude_mcp("team")
        self.assertTrue(mcp._claude_mcp_configured("team"))
        written = json.loads(mcp_json.read_text(encoding="utf-8"))
        server = written["mcpServers"]["mult-agent-mcp"]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], "http://localhost:8000/mcp")

    def test_claude_mcp_configured_rejects_and_repairs_global_sse_override(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {"alice": {"role": "leader", "agent": "claude"}},
                }
            }
        })

        mcp._write_claude_mcp("team")
        global_config = Path(mcp.CLAUDE_GLOBAL_CONFIG_PATH)
        global_config.write_text(
            json.dumps({
                "mcpServers": {
                    "mult-agent-mcp": {
                        "type": "sse",
                        "url": "http://localhost:8000/sse",
                    }
                },
                "projects": {
                    str(workspace.resolve()): {
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

        self.assertFalse(mcp._claude_mcp_configured("team"))
        status = mcp.check_agent_setup("team")
        self.assertIn("全局 Claude MCP 配置冲突", status)

        mcp._write_claude_mcp("team")
        self.assertTrue(mcp._claude_mcp_configured("team"))
        written = json.loads(global_config.read_text(encoding="utf-8"))
        self.assertEqual(written["other"], "preserved")
        server = written["mcpServers"]["mult-agent-mcp"]
        self.assertEqual(server["type"], "http")
        self.assertEqual(server["url"], "http://localhost:8000/mcp")
        project_server = written["projects"][str(workspace.resolve())]["mcpServers"]["mult-agent-mcp"]
        self.assertEqual(project_server["type"], "http")
        self.assertEqual(project_server["url"], "http://localhost:8000/mcp")

    def test_leader_assign_subtask_rejects_direct_leader_member(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "codex",
                    "leader_type": "direct",
                    "members": {
                        "codex": {"role": "member", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_send_keys") as send_keys:
            result = mcp.leader_assign_subtask("team", "codex", "do not send to myself")

        self.assertIn("是你自己", result)
        send_keys.assert_not_called()
        data = mcp._load()
        self.assertNotIn("last_task", data["teams"]["team"]["members"]["codex"])

    def test_leader_broadcast_skips_direct_leader_member(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "codex",
                    "leader_type": "direct",
                    "members": {
                        "codex": {"role": "member", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        send_calls = []

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_member_window_target", side_effect=lambda team, name: "alice" if name == "alice" else None):
                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text: send_calls.append((session, window, text)) or (0, "")):
                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                            result = mcp.leader_broadcast("team", "hello members")

        self.assertIn("alice", result)
        self.assertNotIn("codex", result)
        self.assertEqual(send_calls, [("mcp_team", "alice", "hello members")])

    def test_add_member_uses_team_default_agent_despite_codex_direct_leader(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "leader": "codex",
                    "leader_type": "direct",
                    "members": {
                        "codex": {"role": "member", "agent": "codex"},
                    },
                }
            }
        })

        result = mcp.add_member("team", "alice", "coder", agent="codex")

        self.assertIn("agent=claude", result)
        self.assertIn("来源=团队默认", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["agent"], "claude")

    def test_add_member_allows_explicit_agent_only_with_override_flag(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "members": {},
                }
            }
        })

        result = mcp.add_member("team", "alice", "coder", agent="codex", use_explicit_agent=True)

        self.assertIn("agent=codex", result)
        self.assertIn("来源=显式指定", result)
        self.assertEqual(mcp._load()["teams"]["team"]["members"]["alice"]["agent"], "codex")

    def test_leader_add_member_uses_team_default_agent_when_leader_is_codex(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "terminals_active": True,
                    "leader": "codex",
                    "leader_type": "direct",
                    "members": {
                        "codex": {"role": "member", "agent": "codex"},
                    },
                }
            }
        })
        spawn_calls = []

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=lambda session, name, agent, team_dir: spawn_calls.append((session, name, agent, team_dir)) or (0, "", "")):
                        result = mcp.leader_add_member("team", "alice", "coder", agent="codex")

        self.assertIn("agent=claude", result)
        self.assertIn("来源=团队默认", result)
        self.assertEqual(spawn_calls, [("mcp_team", "alice", "claude", str(workspace))])
        self.assertEqual(mcp._load()["teams"]["team"]["members"]["alice"]["agent"], "claude")

    def test_leader_add_member_allows_explicit_agent_only_with_override_flag(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "terminals_active": True,
                    "leader": "codex",
                    "leader_type": "direct",
                    "members": {
                        "codex": {"role": "member", "agent": "codex"},
                    },
                }
            }
        })
        spawn_calls = []

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=lambda session, name, agent, team_dir: spawn_calls.append((session, name, agent, team_dir)) or (0, "", "")):
                        result = mcp.leader_add_member(
                            "team",
                            "alice",
                            "coder",
                            agent="codex",
                            use_explicit_agent=True,
                        )

        self.assertIn("agent=codex", result)
        self.assertIn("来源=显式指定", result)
        self.assertEqual(spawn_calls, [("mcp_team", "alice", "codex", str(workspace))])

    def test_launch_team_terminals_uses_team_default_when_member_agent_is_empty(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {"role": "coder", "agent": ""},
                    },
                }
            }
        })
        spawn_calls = []

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=lambda session, name, agent, team_dir: spawn_calls.append((session, name, agent, team_dir)) or (0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team")

        self.assertIn("终端已启动", result)
        self.assertEqual(spawn_calls, [("mcp_team", "alice", "claude", str(workspace))])

    def test_launch_team_terminals_direct_skips_direct_leader_member(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "leader": "codex",
                    "leader_type": "direct",
                    "monitor_enabled": False,
                    "members": {
                        "codex": {"role": "member", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        spawn_calls = []

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value="already_configured"):
                    with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=lambda session, name, agent, team_dir, **kw: spawn_calls.append((session, name, agent, team_dir, kw)) or (0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team")

        self.assertIn("终端已启动", result)
        self.assertEqual([call[1] for call in spawn_calls], ["alice"])
        self.assertNotIn("codex", result)

    def test_inject_claude_leader_prompt_calls_send_keys_then_confirm(self):
        """验证 _inject_claude_leader_prompt 依次调用 _send_keys 和 _confirm_prompt_submission"""
        send_calls = []
        confirm_calls = []

        with mock.patch.object(mcp, "_send_keys", side_effect=lambda s, w, t, **kw: send_calls.append((s, w, t)) or (0, "")):
            with mock.patch.object(mcp, "_confirm_prompt_submission", side_effect=lambda s, w, **kw: confirm_calls.append((s, w)) or (0, "")):
                rc, err = mcp._inject_claude_leader_prompt("mcp_team", "alice", "hello leader")

        self.assertEqual(rc, 0)
        self.assertEqual(err, "")
        self.assertEqual(send_calls, [("mcp_team", "alice", "hello leader")])
        self.assertEqual(confirm_calls, [("mcp_team", "alice")])

    def test_inject_claude_leader_prompt_fails_on_send_keys_error(self):
        """验证 _send_keys 失败时 _inject_claude_leader_prompt 短路返回，不调用 _confirm"""
        confirm_calls = []

        with mock.patch.object(mcp, "_send_keys", return_value=(1, "pane not found")):
            with mock.patch.object(mcp, "_confirm_prompt_submission", side_effect=lambda s, w, **kw: confirm_calls.append((s, w)) or (0, "")):
                rc, err = mcp._inject_claude_leader_prompt("mcp_team", "alice", "prompt")

        self.assertNotEqual(rc, 0)
        self.assertIn("send_keys failed", err)
        self.assertEqual(confirm_calls, [])

    def test_inject_claude_leader_prompt_fails_on_confirm_error(self):
        """验证 _confirm_prompt_submission 失败时 _inject_claude_leader_prompt 返回错误"""
        with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
            with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(1, "no session")):
                rc, err = mcp._inject_claude_leader_prompt("mcp_team", "alice", "prompt")

        self.assertNotEqual(rc, 0)
        self.assertIn("confirm failed", err)

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

        spawn_calls = [cmd for cmd in calls if cmd[0] in {"new-session", "new-window"}]
        self.assertIn("--permission-mode", spawn_calls[0])
        self.assertIn("auto", spawn_calls[0])
        self.assertIn("--ask-for-approval", spawn_calls[1])
        self.assertIn("never", spawn_calls[1])

    def test_leader_grant_member_autonomy_sets_agent_specific_auto_policies(self):
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
                        "alice": {"role": "coder", "agent": "claude"},
                        "bob": {"role": "tester", "agent": "codex"},
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value="already_configured"):
                    with mock.patch.object(mcp, "_start_team_monitor", return_value=None):
                        result = mcp.leader_grant_member_autonomy("team", "*")

        self.assertIn("alice, bob", result)
        self.assertIn("Claude auto: alice", result)
        self.assertIn("Codex full approval: bob", result)

        data = mcp._load()
        lead = data["teams"]["team"]["members"]["lead"]
        alice = data["teams"]["team"]["members"]["alice"]
        bob = data["teams"]["team"]["members"]["bob"]

        self.assertNotIn("work_mode", lead)
        self.assertEqual(alice["work_mode"], "auto")
        self.assertTrue(alice["auto_authorize"])
        self.assertEqual(alice["auto_authorize_choice"], "session")
        self.assertEqual(alice["autonomy_policy"], "claude_permission_mode_auto")
        self.assertEqual(bob["work_mode"], "auto")
        self.assertTrue(bob["auto_authorize"])
        self.assertEqual(bob["autonomy_policy"], "codex_ask_for_approval_never")
        self.assertTrue(data["teams"]["team"]["monitor_enabled"])

    def test_leader_grant_member_autonomy_relaunches_member_terminal(self):
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
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        tmux_calls = []
        spawn_calls = []
        send_calls = []

        with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_start_team_monitor", return_value=None):
                    with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
                        with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                            with mock.patch.object(mcp, "_tmux", side_effect=lambda cmd, timeout=10: tmux_calls.append(cmd) or (0, "", "")):
                                with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=lambda session, name, agent, team_dir: spawn_calls.append((session, name, agent, team_dir)) or (0, "", "")):
                                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text, **kwargs: send_calls.append((session, window, text)) or (0, "")):
                                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                                            result = mcp.leader_grant_member_autonomy("team", "alice", relaunch=True)

        self.assertIn("alice: 已重启并加载 auto 权限", result)
        self.assertIn(["kill-window", "-t", "mcp_team:alice"], tmux_calls)
        self.assertEqual(spawn_calls, [("mcp_team", "alice", "claude", str(workspace))])
        self.assertEqual(send_calls[0][0], "mcp_team")
        self.assertEqual(send_calls[0][1], "alice")
        self.assertIn("终端恢复通知", send_calls[0][2])

    # ============================================================
    # 新增：leader_grant_member_autonomy 边缘路径与辅助函数
    # ============================================================

    def test_leader_grant_member_autonomy_rejects_invalid_targets(self):
        """验证授权无效目标时返回错误：不存在的团队、成员、或 tmux leader"""
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
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        r1 = mcp.leader_grant_member_autonomy("nonexistent", "alice")
        self.assertIn("不存在", r1)

        r2 = mcp.leader_grant_member_autonomy("team", "bob")
        self.assertIn("不存在", r2)

        r3 = mcp.leader_grant_member_autonomy("team", "lead")
        self.assertIn("不应授予 member 自动权限", r3)

    def test_leader_grant_member_autonomy_relaunch_when_terminals_inactive(self):
        """验证 relaunch=True 但 terminals_active=False：只保存策略不重启"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(self.root / "context"),
                    "terminals_active": False,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_start_team_monitor", return_value=None):
                    result = mcp.leader_grant_member_autonomy("team", "alice", relaunch=True)

        self.assertIn("终端未启动，已保存授权；下次启动生效", result)
        data = mcp._load()
        self.assertTrue(data["teams"]["team"]["members"]["alice"]["autonomy_granted"])

    def test_leader_grant_member_autonomy_relaunch_when_session_not_found(self):
        """验证 relaunch=True 但找不到 session：警告 + 保存策略"""
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
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_start_team_monitor", return_value=None):
                    with mock.patch.object(mcp, "_find_any_session", return_value=None):
                        result = mcp.leader_grant_member_autonomy("team", "alice", relaunch=True)

        self.assertIn("未找到运行中的终端 session", result)
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["members"]["alice"]["autonomy_policy"],
                         "claude_permission_mode_auto")

    def test_claude_agent_args_auto_plan_manual_modes(self):
        """验证 _claude_agent_args 对三种模式生成正确的 CLI 参数"""

        # auto
        args = mcp._claude_agent_args("claude", "auto")
        self.assertIn("--permission-mode", args)
        self.assertIn("auto", args)

        # plan
        args = mcp._claude_agent_args("claude", "plan")
        self.assertIn("--permission-mode", args)
        self.assertIn("plan", args)

        # manual → 不加 --permission-mode
        args = mcp._claude_agent_args("claude", "manual")
        self.assertNotIn("--permission-mode", args)

        # dangerously_skip → 不加 --permission-mode（skip 隐含）
        args = mcp._claude_agent_args("claude", "auto", dangerously_skip_permissions=True)
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertNotIn("--permission-mode", args)

        # allowed_tools
        args = mcp._claude_agent_args("claude", "manual",
                                      allowed_tools=["mcp__mult-agent-mcp__leader_*"])
        self.assertIn("--allowedTools", args)
        self.assertIn("mcp__mult-agent-mcp__leader_*", args)

    def test_codex_mode_args_auto_plan_manual_modes(self):
        """验证 _codex_mode_args 对三种模式的 CLI 参数映射"""

        self.assertEqual(mcp._codex_mode_args("auto"), ["--ask-for-approval", "never"])
        self.assertEqual(mcp._codex_mode_args("plan"), ["--ask-for-approval", "on-request"])
        self.assertEqual(mcp._codex_mode_args("manual"), [])
        self.assertEqual(mcp._codex_mode_args(""), [])

    def test_normalize_member_mode_aliases_and_invalid(self):
        """验证 _normalize_member_mode 全量别名映射 + 无效输入"""

        # auto 族
        self.assertEqual(mcp._normalize_member_mode("accept_edits"), "auto")
        self.assertEqual(mcp._normalize_member_mode("accept-edits"), "auto")
        self.assertEqual(mcp._normalize_member_mode("never"), "auto")
        self.assertEqual(mcp._normalize_member_mode("accept"), "auto")

        # plan 族
        self.assertEqual(mcp._normalize_member_mode("read_only"), "plan")
        self.assertEqual(mcp._normalize_member_mode("readonly"), "plan")
        self.assertEqual(mcp._normalize_member_mode("planning"), "plan")

        # manual 族
        self.assertEqual(mcp._normalize_member_mode(""), "manual")
        self.assertEqual(mcp._normalize_member_mode("default"), "manual")
        self.assertEqual(mcp._normalize_member_mode("ask"), "manual")

        # 无效
        self.assertEqual(mcp._normalize_member_mode("bogus"), "")

    def test_mode_task_prefix_for_auto_and_plan(self):
        """验证 _mode_task_prefix 为不同模式和 agent 生成正确前缀"""

        # auto → 前缀含 [成员模式: auto]
        prefix = mcp._mode_task_prefix({"work_mode": "auto", "agent": "claude"})
        self.assertIn("[成员模式: auto]", prefix)
        self.assertTrue(len(prefix) > 0)

        # plan + claude → 含 plan 标签，不涉及 codex 特定执行措辞
        prefix = mcp._mode_task_prefix({"work_mode": "plan", "agent": "claude"})
        self.assertIn("[成员模式: plan]", prefix)
        self.assertIn("不要修改文件", prefix)

        # plan + codex → 含更严格约束
        prefix = mcp._mode_task_prefix({"work_mode": "plan", "agent": "codex"})
        self.assertIn("[成员模式: plan]", prefix)
        self.assertIn("破坏性操作", prefix)

        # manual → 空串
        self.assertEqual(mcp._mode_task_prefix({"work_mode": "manual"}), "")

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
                with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
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
                with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                    with mock.patch.object(mcp, "_capture_window", return_value=(0, "This command requires approval\nDo you want to proceed?\n❯ 1. Yes\n  2. Yes, and don't ask again", "")):
                        with mock.patch.object(mcp, "_send_authorization_choice", side_effect=lambda session, member, choice: auth_calls.append((session, member, choice)) or (0, "")):
                            result = mcp.leader_monitor_members("team")

        self.assertIn("auto-authorized:session", result)
        self.assertEqual(auth_calls, [("mcp_team", "alice", "2")])
        data = mcp._load()
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_observed_state"], "busy")
        self.assertNotIn("blocked_reason", member)

    def test_leader_monitor_uses_stored_window_id_when_name_changes(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "tmux_window_id": "@7",
                            "tmux_window_name": "alice",
                            "tmux_session": "mcp_team",
                            "tmux_session_id": "$1",
                            "tmux_session_created": "1000",
                            "last_task": "keep working",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\trenamed-by-cli", ""
            if cmd[0] == "capture-pane":
                self.assertEqual(cmd[2], "@7")
                return 0, "Thinking...\n◼ still in progress", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            result = mcp.leader_monitor_members("team")

        self.assertIn("alice: busy (observed)", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_observed_state"], "busy")
        self.assertNotIn("recovery_count", member)

    def test_leader_assign_subtask_sends_to_stored_window_id_when_name_changes(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "tmux_window_id": "@7",
                            "tmux_window_name": "alice",
                            "tmux_session": "mcp_team",
                            "tmux_session_id": "$1",
                            "tmux_session_created": "1000",
                        },
                    },
                }
            }
        })
        send_targets = []

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\trenamed-by-cli", ""
            if cmd[0] == "send-keys":
                send_targets.append(cmd[2])
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            result = mcp.leader_assign_subtask("team", "alice", "continue task")

        self.assertIn("已分配给 'alice'", result)
        self.assertEqual(send_targets, ["@7", "@7"])

    def test_member_window_target_ignores_stored_window_id_from_other_session(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "tmux_window_id": "@7",
                            "tmux_session": "old_session",
                            "tmux_session_id": "$1",
                            "tmux_session_created": "1000",
                        },
                    },
                }
            }
        })

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\trenamed-other", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            self.assertIsNone(mcp._member_window_target("team", "alice"))

    def test_member_window_target_ignores_stored_window_id_from_recreated_session(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
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

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t2000\t@1\tlead\n$1\t2000\t@7\trenamed-other", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            self.assertIsNone(mcp._member_window_target("team", "alice"))

    def test_find_any_session_prefers_session_with_matching_member_windows(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                        "bob": {"role": "tester", "agent": "claude"},
                    },
                }
            }
        })

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-sessions":
                return 0, "mcp_team\nmcp_team_123456", ""
            if cmd[0] == "list-windows" and cmd[2] == "mcp_team":
                return 0, "$1\t1000\t@1\tstale", ""
            if cmd[0] == "list-windows" and cmd[2] == "mcp_team_123456":
                return 0, "$2\t2000\t@1\talice\n$2\t2000\t@2\tbob", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            self.assertEqual(mcp._find_any_session("team"), "mcp_team_123456")

    def test_load_save_use_team_data_lock(self):
        events = []

        class RecordingLock:
            def __enter__(self):
                events.append("enter")

            def __exit__(self, exc_type, exc, tb):
                events.append("exit")

        mcp.TEAM_DATA_LOCK = RecordingLock()
        mcp._save({"teams": {"team": {"members": {}}}})
        self.assertEqual(mcp._load()["teams"]["team"]["members"], {})
        self.assertEqual(events, ["enter", "exit", "enter", "exit"])

    def test_leader_configure_wakeup_warns_for_direct_leader(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "direct",
                    "members": {"lead": {"role": "leader", "agent": "codex"}},
                }
            }
        })

        with mock.patch.object(mcp, "_start_team_monitor") as start_monitor:
            result = mcp.leader_configure_wakeup("team", enabled=True)

        self.assertIn("direct/未设置 leader", result)
        self.assertTrue(start_monitor.called)
        data = mcp._load()
        self.assertTrue(data["teams"]["team"]["leader_wakeup_config"]["enabled"])

    def test_leader_configure_wakeup_starts_monitor_for_tmux_leader(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {"lead": {"role": "leader", "agent": "codex"}},
                }
            }
        })

        with mock.patch.object(mcp, "_start_team_monitor") as start_monitor:
            result = mcp.leader_configure_wakeup("team", enabled=True, idle_threshold=1, cooldown_cycles=2)

        self.assertIn("leader wakeup 已启用", result)
        start_monitor.assert_called_once_with("team")
        cfg = mcp._load()["teams"]["team"]["leader_wakeup_config"]
        self.assertEqual(cfg["idle_threshold"], 1)
        self.assertEqual(cfg["cooldown_cycles"], 2)

    def test_wakeup_cycle_enters_resting_when_leader_idle_and_member_busy(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_wakeup_config": {
                        "enabled": True,
                        "idle_threshold": 1,
                        "approval_alert": True,
                        "auto_authorize_first": True,
                        "cooldown_cycles": 6,
                        "max_wakeups_per_session": 10,
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "work",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })

        def fake_capture(session, window, lines=120):
            if window == "lead":
                return 0, "✻ Brewed for 5s\n❯\n⏸ manual mode on", ""
            return 0, "Thinking\n◼ running", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                    result = mcp._monitor_team_wakeup_once("team", mark_idle_done=True)

        self.assertEqual(result["action"]["action"], "enter_resting")
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["leader_state"], "resting")

    def test_wakeup_cycle_wakes_leader_once_when_all_members_done(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_state": "resting",
                    "leader_wakeup_config": {
                        "enabled": True,
                        "idle_threshold": 1,
                        "approval_alert": True,
                        "auto_authorize_first": True,
                        "cooldown_cycles": 3,
                        "max_wakeups_per_session": 10,
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "work",
                            "last_task_completed": True,
                        },
                    },
                }
            }
        })
        sent = []

        def fake_capture(session, window, lines=120):
            return 0, "✻ Brewed for 5s\n❯\n⏸ manual mode on", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text: sent.append((session, window, text)) or (0, "")):
                        result = mcp._monitor_team_wakeup_once("team", mark_idle_done=True)

        self.assertEqual(result["action"]["action"], "wakeup_all_done")
        self.assertEqual(len(sent), 1)
        self.assertIn("all tracked member tasks appear complete", sent[0][2])
        data = mcp._load()
        team = data["teams"]["team"]
        self.assertEqual(team["leader_state"], "active")
        self.assertEqual(team["leader_wakeup_cooldown_remaining"], 3)

    def test_wakeup_cycle_wakes_leader_for_manual_approval(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_state": "resting",
                    "leader_wakeup_config": {
                        "enabled": True,
                        "idle_threshold": 1,
                        "approval_alert": True,
                        "auto_authorize_first": True,
                        "cooldown_cycles": 3,
                        "max_wakeups_per_session": 10,
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "work",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })
        sent = []

        def fake_capture(session, window, lines=120):
            if window == "lead":
                return 0, "✻ Brewed for 5s\n❯\n⏸ manual mode on", ""
            return 0, "This command requires approval\nDo you want to proceed?\n❯ 1. Yes", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_capture_window", side_effect=fake_capture):
                    with mock.patch.object(mcp, "_send_keys", side_effect=lambda session, window, text: sent.append((session, window, text)) or (0, "")):
                        result = mcp._monitor_team_wakeup_once("team", mark_idle_done=False)

        self.assertEqual(result["action"]["action"], "wakeup_approval")
        self.assertEqual(len(sent), 1)
        self.assertIn("waiting for authorization", sent[0][2])

    def test_wakeup_cooldown_prevents_reentering_resting(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_state": "active",
                    "leader_idle_streak": 10,
                    "leader_wakeup_cooldown_remaining": 2,
                    "leader_wakeup_config": {
                        "enabled": True,
                        "idle_threshold": 1,
                        "approval_alert": True,
                        "auto_authorize_first": True,
                        "cooldown_cycles": 3,
                        "max_wakeups_per_session": 10,
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "work",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })

        result = mcp._evaluate_leader_wakeup_conditions(
            "team",
            [{"member": "alice", "state": "busy", "action": "observed"}],
        )

        self.assertEqual(result["action"], "none")
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["leader_wakeup_cooldown_remaining"], 1)

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

    def test_member_report_result_sleeps_renamed_window_by_stored_id(self):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "last_task": "finish and sleep",
                            "last_task_completed": False,
                            "tmux_window_id": "@7",
                            "tmux_session": "mcp_team",
                            "tmux_session_id": "$1",
                            "tmux_session_created": "1000",
                        },
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@7\trenamed-by-cli", ""
            if cmd[0] == "kill-window":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            result = mcp.member_report_result("team", "done", member_name="alice")

        self.assertIn("已进入休眠", result)
        kill_calls = [cmd for cmd in tmux_calls if cmd[0] == "kill-window"]
        self.assertEqual(kill_calls, [["kill-window", "-t", "@7"]])

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
        send_calls = [cmd for cmd in calls if cmd[0] == "send-keys"]
        self.assertEqual(
            send_calls,
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

        with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
            result = mcp.leader_read_member_terminal("team", "alice", 120)

        self.assertIn("approval prompt", result)
        capture_calls = [cmd for cmd in calls if cmd[0] == "capture-pane"]
        self.assertEqual(
            capture_calls,
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
        self.assertIn("mcp__mult-agent-mcp__member_*", allow)
        self.assertIn("mcp__mult_agent_mcp__member_*", allow)
        self.assertNotIn("mcp__mult-agent-mcp__leader_*", allow)
        self.assertNotIn("mcp__mult_agent_mcp__leader_*", allow)

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
        self.assertIn("成员名: alice", msg)
        self.assertIn("角色: coder", msg)
        self.assertIn("agent: claude", msg)
        self.assertIn("你的团队成员身份绑定: team='team', member_name='alice'", msg)
        self.assertIn("团队成员表中同名成员记录就是你本人", msg)
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
