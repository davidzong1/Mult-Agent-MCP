"""
代理新特性 (agent proxy) 测试。
=================================

覆盖目标:
  1. 团队默认代理配置影响新启动终端
  2. 成员 proxy_enabled=True/False 覆盖团队默认
  3. 默认创建成员继承团队默认代理策略
  4. TUI 保存团队默认/成员覆盖选项不破坏现有数据
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common.tmux_utils import get_proxy_env_prefix, member_proxy_mode
from tui.tui_screens import apply_proxy_action


class AgentProxyTests(unittest.TestCase):
    """代理特性单元测试 — 通过临时目录隔离模块全局状态。"""

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
        self.old_data_file_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        data_file = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.DATA_FILE = data_file
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        # 对齐 common/data_layer 的 data file，使 get_proxy_env_prefix 读取同一文件
        data_layer.set_data_file(data_file)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_funcs.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_file_override
        self.tmp.cleanup()

    # ============================================================
    # get_proxy_env_prefix — 团队默认代理配置
    # ============================================================

    def test_proxy_env_prefix_when_team_proxy_enabled(self):
        """团队代理启用 → 返回 env 前缀含四个代理环境变量。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "192.168.1.1", "port": 8080},
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("team")

        self.assertEqual(len(prefix), 5)  # ["env", http_proxy=, https_proxy=, HTTP_PROXY=, HTTPS_PROXY=]
        self.assertEqual(prefix[0], "env")
        self.assertIn("http_proxy=http://192.168.1.1:8080", prefix)
        self.assertIn("https_proxy=http://192.168.1.1:8080", prefix)
        self.assertIn("HTTP_PROXY=http://192.168.1.1:8080", prefix)
        self.assertIn("HTTPS_PROXY=http://192.168.1.1:8080", prefix)

    def test_proxy_env_prefix_defaults_when_only_enabled(self):
        """团队代理启用但未指定 host/port → 使用默认值 127.0.0.1:7890。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True},
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("team")

        self.assertIn("http_proxy=http://127.0.0.1:7890", prefix)
        self.assertIn("https_proxy=http://127.0.0.1:7890", prefix)

    def test_proxy_env_prefix_empty_when_disabled(self):
        """团队代理禁用 → 返回空列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": False, "host": "10.0.0.1", "port": 3128},
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("team")

        self.assertEqual(prefix, [])

    def test_proxy_env_prefix_empty_when_no_config(self):
        """团队无代理配置 → 返回空列表。"""
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

        prefix = get_proxy_env_prefix("team")

        self.assertEqual(prefix, [])

    def test_proxy_env_prefix_empty_when_enabled_is_falsy_non_bool(self):
        """proxy.enabled 为 0 / None 等 falsy 值 → 返回空列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": 0},
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("team")

        self.assertEqual(prefix, [])

    # ============================================================
    # get_proxy_env_prefix — 成员级 proxy_enabled 覆盖
    # ============================================================

    def test_member_proxy_disabled_overrides_team_enabled(self):
        """成员 proxy_enabled=False → 即使团队代理启用也返回空列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "proxy_enabled": False},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "alice")

        self.assertEqual(prefix, [])

    def test_member_proxy_enabled_overrides_team_disabled(self):
        """成员 proxy_enabled=True → 即使团队代理禁用也返回 env 前缀。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": False, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "bob": {"role": "coder", "agent": "codex", "proxy_enabled": True},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "bob")

        self.assertEqual(len(prefix), 5)
        self.assertEqual(prefix[0], "env")
        self.assertIn("http_proxy=http://10.0.0.1:3128", prefix)

    def test_member_without_proxy_enabled_inherits_team_enabled(self):
        """成员未设置 proxy_enabled 且团队代理启用 → 返回 env 前缀（继承）。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "proxy.example.com", "port": 9000},
                    "members": {
                        "carol": {"role": "tester", "agent": "claude"},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "carol")

        self.assertIn("http_proxy=http://proxy.example.com:9000", prefix)

    def test_member_without_proxy_enabled_inherits_team_disabled(self):
        """成员未设置 proxy_enabled 且团队代理禁用 → 返回空列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": False},
                    "members": {
                        "dave": {"role": "reviewer", "agent": "claude"},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "dave")

        self.assertEqual(prefix, [])

    def test_member_proxy_enabled_true_without_team_config_uses_defaults(self):
        """成员 proxy_enabled=True 但团队无 proxy 配置 → 使用默认 host/port。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "members": {
                        "eve": {"role": "coder", "agent": "claude", "proxy_enabled": True},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "eve")

        self.assertIn("http_proxy=http://127.0.0.1:7890", prefix)

    def test_member_proxy_mode_disabled_overrides_team_enabled(self):
        """成员 proxy_mode=disabled → 即使团队代理启用也返回空列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "proxy_mode": "disabled"},
                    },
                }
            }
        })

        self.assertEqual(member_proxy_mode({"proxy_mode": "disabled"}), "disabled")
        self.assertEqual(get_proxy_env_prefix("team", "alice"), [])

    def test_member_proxy_mode_enabled_overrides_team_disabled(self):
        """成员 proxy_mode=enabled → 即使团队代理禁用也返回 env 前缀。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": False, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "bob": {"role": "coder", "agent": "codex", "proxy_mode": "enabled"},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "bob")
        self.assertIn("http_proxy=http://10.0.0.1:3128", prefix)

    def test_team_proxy_unchanged_when_member_name_empty(self):
        """member_name 为空字符串时，完全按团队默认（无成员覆盖逻辑触发）。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "gw.local", "port": 1080},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "proxy_enabled": False},
                    },
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "")

        # 空 member_name → 走团队默认（启用），不受 alice 覆盖影响
        self.assertIn("http_proxy=http://gw.local:1080", prefix)

    def test_nonexistent_member_falls_back_to_team_default(self):
        """不存在的成员名 → 忽略成员覆盖，走团队默认。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "internal.proxy", "port": 3128},
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("team", "nonexistent")

        self.assertIn("http_proxy=http://internal.proxy:3128", prefix)

    # ============================================================
    # leader_configure_proxy MCP tool
    # ============================================================

    def test_leader_configure_proxy_enables(self):
        """leader_configure_proxy(enabled=True) → 返回成功消息含 proxy URL。"""
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

        result = mcp.leader_configure_proxy("team", enabled=True, host="10.0.0.1", port=3128)

        self.assertIn("已启用", result)
        self.assertIn("http_proxy=http://10.0.0.1:3128", result)
        self.assertIn("https_proxy=http://10.0.0.1:3128", result)

        data = mcp._load()
        proxy = data["teams"]["team"]["proxy"]
        self.assertTrue(proxy["enabled"])
        self.assertEqual(proxy["host"], "10.0.0.1")
        self.assertEqual(proxy["port"], 3128)

    def test_leader_configure_proxy_disables(self):
        """leader_configure_proxy(enabled=False) → 返回已禁用消息。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128},
                    "members": {},
                }
            }
        })

        result = mcp.leader_configure_proxy("team", enabled=False)

        self.assertIn("已禁用", result)
        data = mcp._load()
        self.assertFalse(data["teams"]["team"]["proxy"]["enabled"])

    def test_leader_configure_proxy_nonexistent_team(self):
        """不存在的团队 → 返回错误。"""
        result = mcp.leader_configure_proxy("ghost", enabled=True)
        self.assertIn("不存在", result)

    def test_leader_configure_proxy_defaults_when_no_host_port(self):
        """不传 host/port → 使用默认 127.0.0.1:7890。"""
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

        result = mcp.leader_configure_proxy("team", enabled=True)

        self.assertIn("127.0.0.1:7890", result)
        proxy = mcp._load()["teams"]["team"]["proxy"]
        self.assertEqual(proxy["host"], "127.0.0.1")
        self.assertEqual(proxy["port"], 7890)

    # ============================================================
    # leader_get_proxy_config MCP tool
    # ============================================================

    def test_leader_get_proxy_config_when_enabled(self):
        """代理启用时 → 显示完整配置信息。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "proxy.company.com", "port": 8080},
                    "members": {},
                }
            }
        })

        result = mcp.leader_get_proxy_config("team")

        self.assertIn("启用", result)
        self.assertIn("http_proxy=http://proxy.company.com:8080", result)
        self.assertIn("https_proxy=http://proxy.company.com:8080", result)

    def test_leader_get_proxy_config_when_disabled(self):
        """代理禁用时 → 显示禁用。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": False},
                    "members": {},
                }
            }
        })

        result = mcp.leader_get_proxy_config("team")

        self.assertIn("禁用", result)

    def test_leader_get_proxy_config_when_no_config(self):
        """无代理配置时 → 显示禁用。"""
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

        result = mcp.leader_get_proxy_config("team")

        self.assertIn("禁用", result)

    def test_leader_get_proxy_config_nonexistent_team(self):
        """不存在的团队 → 返回错误。"""
        result = mcp.leader_get_proxy_config("ghost")
        self.assertIn("不存在", result)

    # ============================================================
    # _tmux_spawn_member — 代理前缀注入终端命令
    # ============================================================

    def test_tmux_spawn_member_includes_proxy_when_enabled(self):
        """团队代理启用 → _tmux_spawn_member 的 tmux 命令含 proxy env 前缀。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "claude",
                    "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))

        # 找到 new-window 命令
        spawn_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-window")
        self.assertIn("new-window", spawn_cmd[0])
        # 验证 env 前缀已注入
        self.assertIn("env", spawn_cmd)
        self.assertIn("http_proxy=http://10.0.0.1:3128", spawn_cmd)
        self.assertIn("https_proxy=http://10.0.0.1:3128", spawn_cmd)

    def test_tmux_spawn_member_excludes_proxy_when_disabled(self):
        """团队代理禁用 → _tmux_spawn_member 不含 proxy env 前缀。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "claude",
                    "proxy": {"enabled": False},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))

        spawn_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-window")
        self.assertNotIn("env", spawn_cmd)
        self.assertNotIn("http_proxy", " ".join(spawn_cmd))

    def test_tmux_spawn_member_honors_member_proxy_disabled_override(self):
        """成员 proxy_enabled=False → 即使团队代理启用，终端命令也不含 proxy。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "claude",
                    "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "proxy_enabled": False},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))

        spawn_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-window")
        self.assertNotIn("env", spawn_cmd)
        self.assertNotIn("http_proxy", " ".join(spawn_cmd))

    def test_tmux_spawn_member_honors_member_proxy_enabled_override(self):
        """成员 proxy_enabled=True → 即使团队代理禁用，终端命令也含 proxy。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "claude",
                    "proxy": {"enabled": False, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "bob": {"role": "coder", "agent": "claude", "proxy_enabled": True},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=str(workspace / ".claude" / "settings.json")):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "bob", "claude", str(workspace))

        spawn_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-window")
        self.assertIn("env", spawn_cmd)
        self.assertIn("http_proxy=http://10.0.0.1:3128", spawn_cmd)

    def test_proxy_is_injected_for_codex_member_spawn(self):
        """Codex agent 成员终端启动同样注入 proxy env 前缀。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "codex",
                    "proxy": {"enabled": True, "host": "gw.local", "port": 1080},
                    "members": {
                        "carol": {"role": "tester", "agent": "codex"},
                    },
                }
            }
        })
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                mcp._tmux_spawn_member("mcp_team", "carol", "codex", str(workspace))

        spawn_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-window")
        self.assertIn("env", spawn_cmd)
        self.assertIn("http_proxy=http://gw.local:1080", spawn_cmd)
        # Codex 特有: -C <team_dir> 参数
        self.assertIn("-C", spawn_cmd)
        self.assertIn(str(workspace), spawn_cmd)

    # ============================================================
    # launch_team_terminals — 完整终端启动流程含代理
    # ============================================================

    def test_launch_team_terminals_includes_proxy_in_leader_session(self):
        """团队代理启用 → leader session 命令含 proxy env 前缀。"""
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
                    "proxy": {"enabled": True, "host": "proxy.internal", "port": 3128},
                    "members": {
                        "alice": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        })
        tmux_calls = []

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
                    with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                        with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team", task="test proxy")

        self.assertIn("终端已启动", result)
        session_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-session")
        self.assertIn("env", session_cmd)
        self.assertIn("http_proxy=http://proxy.internal:3128", session_cmd)
        self.assertIn("https_proxy=http://proxy.internal:3128", session_cmd)

    def test_launch_team_terminals_excludes_proxy_when_disabled(self):
        """团队代理禁用 → leader session 命令不含 proxy。"""
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
                    "proxy": {"enabled": False, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "alice": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        })
        tmux_calls = []

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
                    with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                        with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team")

        self.assertIn("终端已启动", result)
        session_cmd = next(cmd for cmd in tmux_calls if cmd[0] == "new-session")
        self.assertNotIn("env", session_cmd)

    # ============================================================
    # 数据持久化 — 保存/加载不破坏现有数据
    # ============================================================

    def test_proxy_config_persists_across_save_load_cycle(self):
        """代理配置在 save → load 循环后保持不变。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "gw.company.com", "port": 9090},
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        # 模拟保存再加载
        data = mcp._load()
        mcp._save(data)
        data2 = mcp._load()

        proxy = data2["teams"]["team"]["proxy"]
        self.assertTrue(proxy["enabled"])
        self.assertEqual(proxy["host"], "gw.company.com")
        self.assertEqual(proxy["port"], 9090)

    def test_member_proxy_enabled_persists_without_corrupting_other_fields(self):
        """成员 proxy_enabled 与其他字段共存，保存/加载后互不破坏。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        original = {
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "codex",
                    "proxy": {"enabled": True, "host": "10.0.0.1", "port": 3128},
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "proxy_enabled": False,
                            "work_mode": "auto",
                            "last_task": "fix bug",
                        },
                        "bob": {
                            "role": "tester",
                            "agent": "codex",
                            "proxy_enabled": True,
                            "last_task": "run tests",
                        },
                        "carol": {
                            "role": "reviewer",
                            "agent": "claude",
                            "last_task_completed": True,
                        },
                    },
                }
            }
        }
        mcp._save(original)

        data = mcp._load()
        members = data["teams"]["team"]["members"]

        # alice: proxy_enabled=False, 其他字段不受影响
        self.assertEqual(members["alice"]["proxy_enabled"], False)
        self.assertEqual(members["alice"]["role"], "coder")
        self.assertEqual(members["alice"]["work_mode"], "auto")
        self.assertEqual(members["alice"]["last_task"], "fix bug")

        # bob: proxy_enabled=True, 其他字段不受影响
        self.assertEqual(members["bob"]["proxy_enabled"], True)
        self.assertEqual(members["bob"]["role"], "tester")
        self.assertEqual(members["bob"]["last_task"], "run tests")

        # carol: 无 proxy_enabled，其他字段不受影响
        self.assertNotIn("proxy_enabled", members["carol"])
        self.assertEqual(members["carol"]["role"], "reviewer")
        self.assertTrue(members["carol"]["last_task_completed"])

        # team proxy 也不受影响
        self.assertEqual(data["teams"]["team"]["proxy"]["enabled"], True)
        self.assertEqual(data["teams"]["team"]["proxy"]["host"], "10.0.0.1")

    def test_team_config_and_member_override_coexist_in_data(self):
        """团队 proxy + 成员 proxy_enabled 在同一个数据结构中并存。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "gw.shared", "port": 8080},
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude", "proxy_enabled": False},
                        "bob": {"role": "coder", "agent": "codex", "proxy_enabled": True},
                        "carol": {"role": "tester", "agent": "claude"},
                    },
                }
            }
        })

        # 验证各成员代理行为
        lead_prefix = get_proxy_env_prefix("team", "lead")
        alice_prefix = get_proxy_env_prefix("team", "alice")
        bob_prefix = get_proxy_env_prefix("team", "bob")
        carol_prefix = get_proxy_env_prefix("team", "carol")

        # lead: 继承团队默认 → 有代理
        self.assertIn("env", lead_prefix)
        # alice: 强制禁用 → 无代理
        self.assertEqual(alice_prefix, [])
        # bob: 强制启用 → 有代理
        self.assertIn("env", bob_prefix)
        # carol: 继承团队默认 → 有代理
        self.assertIn("env", carol_prefix)

    def test_enable_disable_enable_cycle_preserves_config(self):
        """多次启用/禁用循环后，host/port 需显式传递才能保留。"""
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

        # 启用 → 禁用 → 启用（重新传 host/port 保留原值）
        mcp.leader_configure_proxy("team", enabled=True, host="custom.host", port=1234)
        mcp.leader_configure_proxy("team", enabled=False)
        mcp.leader_configure_proxy("team", enabled=True, host="custom.host", port=1234)

        data = mcp._load()
        proxy = data["teams"]["team"]["proxy"]
        self.assertTrue(proxy["enabled"])
        self.assertEqual(proxy["host"], "custom.host")
        self.assertEqual(proxy["port"], 1234)

    # ============================================================
    # 默认创建成员继承团队代理策略（无 proxy_enabled 覆盖）
    # ============================================================

    def test_add_member_does_not_set_proxy_enabled(self):
        """默认创建成员时，不设置 proxy_enabled → 继承团队默认。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "proxy": {"enabled": True, "host": "gw.local", "port": 3128},
                    "members": {},
                }
            }
        })

        result = mcp.add_member("team", "new_coder", "coder")

        self.assertIn("agent=claude", result)
        member = mcp._load()["teams"]["team"]["members"]["new_coder"]
        self.assertNotIn("proxy_enabled", member)
        self.assertEqual(member["role"], "coder")

    def test_leader_add_member_does_not_set_proxy_enabled(self):
        """leader_add_member 创建成员时不设置 proxy_enabled → 继承团队默认。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "terminals_active": True,
                    "proxy": {"enabled": True, "host": "proxy.internal", "port": 9090},
                    "leader": "lead",
                    "leader_type": "direct",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                    },
                }
            }
        })
        spawn_calls = []

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=lambda session, name, agent, team_dir: spawn_calls.append((session, name, agent, team_dir)) or (0, "", "")):
                        result = mcp.leader_add_member("team", "new_tester", "tester")

        self.assertIn("agent=claude", result)
        member = mcp._load()["teams"]["team"]["members"]["new_tester"]
        self.assertNotIn("proxy_enabled", member)

    def test_leader_configure_member_proxy_writes_proxy_mode_and_legacy_flag(self):
        """MCP 成员代理配置写入 proxy_mode，同时保留 proxy_enabled 兼容旧数据读取。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": False, "host": "proxy.internal", "port": 9090},
                    "leader": "lead",
                    "leader_type": "direct",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        result = mcp.leader_configure_member_proxy("team", "alice", proxy_enabled=True)

        self.assertIn("强制启用", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["proxy_mode"], "enabled")
        self.assertTrue(member["proxy_enabled"])

    def test_clear_member_proxy_override_removes_proxy_mode_and_legacy_flag(self):
        """清除成员覆盖后移除 proxy_mode/proxy_enabled，恢复继承团队默认。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True},
                    "leader": "lead",
                    "leader_type": "direct",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "proxy_mode": "disabled",
                            "proxy_enabled": False,
                        },
                    },
                }
            }
        })

        result = mcp.leader_clear_member_proxy_override("team", "alice")

        self.assertIn("已清除", result)
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertNotIn("proxy_mode", member)
        self.assertNotIn("proxy_enabled", member)

    def test_tui_proxy_action_enables_only_selected_member(self):
        """TUI 代理“启用”只强制启用当前选择成员。"""
        team = {
            "proxy": {"enabled": False, "host": "old.proxy", "port": 1000},
            "members": {
                "alice": {"role": "coder", "agent": "claude"},
                "bob": {"role": "tester", "agent": "claude"},
            },
        }

        msg = apply_proxy_action(team, "enabled", "alice", "new.proxy", 8080)

        self.assertIn("alice", msg)
        self.assertFalse(team["proxy"]["enabled"])
        self.assertEqual(team["proxy"]["host"], "new.proxy")
        self.assertEqual(team["proxy"]["port"], 8080)
        self.assertEqual(team["members"]["alice"]["proxy_mode"], "enabled")
        self.assertTrue(team["members"]["alice"]["proxy_enabled"])
        self.assertNotIn("proxy_mode", team["members"]["bob"])

    def test_tui_proxy_action_disables_only_selected_member(self):
        """TUI 代理“禁用”只强制禁用当前选择成员。"""
        team = {
            "proxy": {"enabled": True, "host": "old.proxy", "port": 1000},
            "members": {
                "alice": {"role": "coder", "agent": "claude"},
                "bob": {"role": "tester", "agent": "claude"},
            },
        }

        msg = apply_proxy_action(team, "disabled", "bob", "new.proxy", 8080)

        self.assertIn("bob", msg)
        self.assertTrue(team["proxy"]["enabled"])
        self.assertEqual(team["members"]["bob"]["proxy_mode"], "disabled")
        self.assertFalse(team["members"]["bob"]["proxy_enabled"])
        self.assertNotIn("proxy_mode", team["members"]["alice"])

    def test_tui_proxy_action_all_enable_uses_team_default(self):
        """TUI 代理“全部启用”沿用原团队默认代理启用语义。"""
        team = {
            "proxy": {"enabled": False, "host": "old.proxy", "port": 1000},
            "members": {
                "alice": {"role": "coder", "agent": "claude", "proxy_mode": "disabled"},
            },
        }

        msg = apply_proxy_action(team, "all_enabled", "alice", "new.proxy", 8080)

        self.assertIn("全部启用", msg)
        self.assertTrue(team["proxy"]["enabled"])
        self.assertEqual(team["proxy"]["host"], "new.proxy")
        self.assertEqual(team["proxy"]["port"], 8080)
        self.assertEqual(team["members"]["alice"]["proxy_mode"], "disabled")

    def test_tui_proxy_action_all_disable_uses_team_default(self):
        """TUI 代理“全部禁用”沿用原团队默认代理禁用语义。"""
        team = {
            "proxy": {"enabled": True, "host": "old.proxy", "port": 1000},
            "members": {
                "alice": {"role": "coder", "agent": "claude", "proxy_mode": "enabled"},
            },
        }

        msg = apply_proxy_action(team, "all_disabled", "alice", "new.proxy", 8080)

        self.assertIn("全部禁用", msg)
        self.assertFalse(team["proxy"]["enabled"])
        self.assertEqual(team["proxy"]["host"], "new.proxy")
        self.assertEqual(team["proxy"]["port"], 8080)
        self.assertEqual(team["members"]["alice"]["proxy_mode"], "enabled")

    def test_new_member_inherits_team_proxy_at_terminal_launch(self):
        """新成员未设置 proxy_enabled → 启动终端时自动使用团队代理。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "claude",
                    "proxy": {"enabled": True, "host": "team.proxy", "port": 8080},
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "new_coder": {"role": "coder", "agent": "claude"},
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
            if cmd[0] == "new-window":
                spawn_calls.append(cmd)
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                        with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team")

        self.assertIn("终端已启动", result)
        # new_coder 的 new-window 命令应含代理
        member_cmd = next((cmd for cmd in spawn_calls if "new_coder" in cmd), None)
        self.assertIsNotNone(member_cmd)
        self.assertIn("env", member_cmd)
        self.assertIn("http_proxy=http://team.proxy:8080", member_cmd)

    # ============================================================
    # 数据完整性 — leader_* 和 member_report_result 不破坏 proxy
    # ============================================================

    def test_leader_assign_subtask_preserves_proxy_config(self):
        """leader_assign_subtask 不触碰 proxy 配置。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "keep.me", "port": 3456},
                    "terminals_active": True,
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_window_exists", return_value=True):
                with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                    with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                            mcp.leader_assign_subtask("team", "alice", "build feature")

        proxy = mcp._load()["teams"]["team"]["proxy"]
        self.assertTrue(proxy["enabled"])
        self.assertEqual(proxy["host"], "keep.me")
        self.assertEqual(proxy["port"], 3456)

    def test_member_report_result_preserves_proxy_config(self):
        """member_report_result 不破坏 proxy 和成员 proxy_enabled。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "proxy": {"enabled": True, "host": "safe.proxy", "port": 8888},
                    "members": {
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "proxy_enabled": False,
                            "last_task": "write tests",
                            "last_task_completed": False,
                        },
                    },
                }
            }
        })

        with mock.patch.object(mcp, "_tmux", return_value=(0, "", "")):
            with mock.patch.object(mcp, "_member_window_target", return_value="alice"):
                mcp.member_report_result("team", "done", member_name="alice")

        data = mcp._load()
        self.assertTrue(data["teams"]["team"]["proxy"]["enabled"])
        self.assertEqual(data["teams"]["team"]["members"]["alice"]["proxy_enabled"], False)

    def test_leader_set_member_mode_preserves_member_proxy_enabled(self):
        """leader_set_member_mode 修改 work_mode 但不破坏 proxy_enabled。"""
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
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "proxy_enabled": False,
                        },
                    },
                }
            }
        })

        mcp.leader_set_member_mode("team", "alice", "auto")

        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["work_mode"], "auto")
        self.assertEqual(member["proxy_enabled"], False)

    def test_leader_redefine_member_preserves_member_proxy_enabled(self):
        """leader_redefine_member 修改 role/agent 但不破坏 proxy_enabled。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "leader": "lead",
                    "leader_type": "direct",
                    "members": {
                        "lead": {"role": "leader", "agent": "codex"},
                        "alice": {
                            "role": "coder",
                            "agent": "claude",
                            "proxy_enabled": True,
                        },
                    },
                }
            }
        })

        mcp.leader_redefine_member("team", "alice", role="tester", agent="codex")

        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["role"], "tester")
        self.assertEqual(member["agent"], "codex")
        self.assertEqual(member["proxy_enabled"], True)

    # ============================================================
    # team_create / team_set_default_agent 与代理独立性
    # ============================================================

    def test_team_create_does_not_set_proxy(self):
        """team_create 不设置 proxy 配置 → get_proxy_env_prefix 返回空。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({"teams": {}})

        # 直接构造一个 team_create 等价的团队
        mcp._save({
            "teams": {
                "new_team": {
                    "workspace_dir": str(workspace),
                    "description": "test team",
                    "default_agent": "claude",
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("new_team")
        self.assertEqual(prefix, [])

    def test_team_set_default_agent_preserves_proxy(self):
        """team_set_default_agent 修改默认 agent 不破坏 proxy。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "proxy": {"enabled": True, "host": "keep.proxy", "port": 5000},
                    "members": {},
                }
            }
        })

        result = mcp.team_set_default_agent("team", "codex")

        self.assertIn("codex", result)
        data = mcp._load()
        self.assertEqual(data["teams"]["team"]["proxy"]["host"], "keep.proxy")
        self.assertTrue(data["teams"]["team"]["proxy"]["enabled"])

    # ============================================================
    # 混合场景 — 多成员不同 proxy_enabled + 团队 proxy
    # ============================================================

    def test_launch_team_terminals_mixed_member_proxy_overrides(self):
        """混合场景：部分成员覆盖 proxy_enabled，其余继承团队默认。"""
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "default_agent": "claude",
                    "proxy": {"enabled": True, "host": "shared.proxy", "port": 3128},
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "has_proxy": {"role": "coder", "agent": "claude"},
                        "no_proxy": {"role": "tester", "agent": "claude", "proxy_enabled": False},
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
            if cmd[0] == "new-window":
                spawn_calls.append(cmd)
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp", return_value=str(context / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                        with mock.patch.object(mcp, "_confirm_prompt_submission", return_value=(0, "")):
                            with mock.patch.object(mcp.time, "sleep", return_value=None):
                                result = mcp.launch_team_terminals("team")

        self.assertIn("终端已启动", result)

        # has_proxy 应继承团队代理 → new-window 含 env
        has_proxy_cmd = next((cmd for cmd in spawn_calls if "has_proxy" in cmd), None)
        self.assertIsNotNone(has_proxy_cmd)
        self.assertIn("env", has_proxy_cmd)
        self.assertIn("http_proxy=http://shared.proxy:3128", has_proxy_cmd)

        # no_proxy 应因成员覆盖而禁用 → new-window 不含 env
        no_proxy_cmd = next((cmd for cmd in spawn_calls if "no_proxy" in cmd), None)
        self.assertIsNotNone(no_proxy_cmd)
        self.assertNotIn("env", no_proxy_cmd)

    # ============================================================
    # 边界条件
    # ============================================================

    def test_proxy_env_prefix_format_is_consistent(self):
        """验证返回格式始终是 [env, k1=v1, k2=v2, k3=v3, k4=v4]。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "proxy": {"enabled": True, "host": "1.2.3.4", "port": 5678},
                    "members": {},
                }
            }
        })

        prefix = get_proxy_env_prefix("team")

        self.assertEqual(prefix[0], "env")
        self.assertTrue(all("=" in var for var in prefix[1:]))
        self.assertEqual(len(prefix), 5)  # [env, http_proxy=, https_proxy=, HTTP_PROXY=, HTTPS_PROXY=]
        # 确保大小写变体都存在
        vars_set = set(prefix[1:])
        self.assertIn("http_proxy=http://1.2.3.4:5678", vars_set)
        self.assertIn("https_proxy=http://1.2.3.4:5678", vars_set)
        self.assertIn("HTTP_PROXY=http://1.2.3.4:5678", vars_set)
        self.assertIn("HTTPS_PROXY=http://1.2.3.4:5678", vars_set)

    def test_nonexistent_team_returns_empty_prefix(self):
        """不存在的团队名 → get_proxy_env_prefix 返回空列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({"teams": {}})

        prefix = get_proxy_env_prefix("nonexistent")

        self.assertEqual(prefix, [])


if __name__ == "__main__":
    unittest.main()
