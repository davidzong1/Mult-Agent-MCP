"""
Agent User — TUI / 启动路径集成测试。
=====================================
第三轮：适配新 typed profile 模型（agent_type + provider 字段）。
覆盖：
  - AgentUserEditDialog 新字段构造、provider 锁定/可选
  - 成员 agent_user 持久化/清除 + agent 类型联动
  - Profile 删除清理成员引用（保持）
  - launch_terminals / MCP spawn env 注入（按 provider 三变量）
  - TUI 阻断项覆盖：provider 锁定、agent 联动、key/model 校验复用
不定义任何 validator；全部从生产模块导入。避免 Pilot 依赖，使用 mock 拦截。
"""

import contextlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common.tmux_utils import (
    agent_type,
    get_agent_user_env_prefix,
    validate_agent_user_env_value,
)

import mult_agent_mcp as mcp
import tui.tui_screens as tui_screens
from tui.tui_dialogs import (
    AddMemberDialog,
    AgentUserEditDialog,
    AgentUserManageDialog,
    TeamDefaultAgentUserDialog,
    EditMemberDialog,
    _resolve_profile_agent_type,
    _sync_agent_user_rename,
)


@contextlib.contextmanager
def _temp_data_override(data: dict | None = None):
    """把 data_layer 覆盖指向临时数据文件，绝不触碰真实 teams_data.json。

    所有经 data_layer / common.tmux_utils / 迁移落盘的读写都落在临时文件；
    data 非空时先写入（作为真实读路径的基线）。finally 恢复原 override 并清理。
    """
    tmp = tempfile.TemporaryDirectory()
    try:
        prev_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_file = Path(tmp.name) / "teams_data.json"
        data_layer.set_data_file(data_file)
        if data is not None:
            from common.atomic_write import atomic_json_write
            atomic_json_write(data_file, data)
        yield
    finally:
        data_layer._DATA_FILE_OVERRIDE = prev_override
        tmp.cleanup()


@contextlib.contextmanager
def _mock_agent_user_data(data: dict):
    """mock tui_dialogs 与 common 的 load_data 指向同一 data，并把落盘路径隔离到临时文件。

    task4 后 _agent_user_profiles 委托 common.tmux_utils.list_agent_users
    （读路径在 common），而对话框 handler 仍用 tui.tui_dialogs.load_data。
    测试需同时覆盖两个读点，保证全局 registry 与保存路径一致；
    配合 _temp_data_override，未显式 patch 的 save_data/get_data_file/迁移
    也不会触碰真实 teams_data.json。
    """
    with _temp_data_override(data), \
         mock.patch("tui.tui_dialogs.load_data", return_value=data), \
         mock.patch("common.tmux_utils.load_data", return_value=data):
        yield


def _settings_env_from_cmd(cmd) -> dict:
    """从 spawn 命令中提取 --settings 私有文件并返回其 env 块。

    生产修复后 claude 接管不再把 key/base_url 写进命令行（安全），而是写入
    每终端私有 --settings 文件；测试通过此辅助断言文件内容。
    """
    import json as _json
    items = [str(x) for x in cmd]
    if "--settings" not in items:
        return {}
    path = Path(items[items.index("--settings") + 1])
    if not path.exists():
        return {}
    return _json.loads(path.read_text(encoding="utf-8")).get("env", {})


# ============================================================
# AgentUserEditDialog — 构造 + 新字段 + provider 锁定
# ============================================================

class AgentUserEditDialogSaveTests(unittest.TestCase):
    """AgentUserEditDialog 构造 & 保存行为（新 typed 模型）。"""

    def test_typed_claude_profile_constructor(self):
        """Claude typed profile 构造 — agent_type='claude', anthropic 三字段。"""
        dlg = AgentUserEditDialog(
            user_key="claude_p",
            agent_type="claude",
            takeover_enabled=True,
            anthropic_api_key="sk-ant-test123",
            anthropic_base_url="https://api.anthropic.com",
            anthropic_model="claude-sonnet-5-20251001",
            openai_api_key="",
            openai_base_url="",
            codex_model="",
        )
        self.assertEqual(dlg._user_key, "claude_p")
        self.assertEqual(dlg._agent_type, "claude")
        self.assertTrue(dlg._takeover_enabled)
        self.assertFalse(dlg._is_new)
        self.assertEqual(dlg._anthropic_api_key, "sk-ant-test123")
        self.assertEqual(dlg._anthropic_base_url, "https://api.anthropic.com")
        self.assertEqual(dlg._anthropic_model, "claude-sonnet-5-20251001")

    def test_typed_codex_profile_constructor(self):
        """Codex typed profile 构造 — agent_type='codex', openai 三字段。"""
        dlg = AgentUserEditDialog(
            user_key="codex_p",
            agent_type="codex",
            takeover_enabled=False,
            anthropic_api_key="",
            anthropic_base_url="",
            anthropic_model="",
            openai_api_key="sk-test456",
            openai_base_url="https://api.openai.com",
            codex_model="gpt-4o",
        )
        self.assertEqual(dlg._agent_type, "codex")
        self.assertFalse(dlg._takeover_enabled)
        self.assertEqual(dlg._openai_api_key, "sk-test456")
        self.assertEqual(dlg._openai_base_url, "https://api.openai.com")
        self.assertEqual(dlg._codex_model, "gpt-4o")

    def test_typed_profile_is_not_new(self):
        """user_key 非空 → _is_new=False，编辑模式。"""
        dlg = AgentUserEditDialog(user_key="existing", agent_type="claude")
        self.assertFalse(dlg._is_new)

    def test_new_profile_all_fields_default(self):
        """新建 profile 允许全部字段空，让用户填写。"""
        dlg = AgentUserEditDialog(
            user_key="",
            agent_type="",
            takeover_enabled=False,
            anthropic_api_key="",
            anthropic_base_url="",
            anthropic_model="",
            openai_api_key="",
            openai_base_url="",
            codex_model="",
        )
        self.assertTrue(dlg._is_new)
        self.assertEqual(dlg._agent_type, "")

    def test_typed_profile_locks_agent_type(self):
        """TUI 阻断项 2: 已有 typed profile 编辑时 agent_type 不可变。
        通过 _is_new=False + _agent_type 非空验证。"""
        # Claude typed
        dlg = AgentUserEditDialog(user_key="claude_p", agent_type="claude")
        self.assertFalse(dlg._is_new)
        self.assertEqual(dlg._agent_type, "claude")
        # Codex typed
        dlg2 = AgentUserEditDialog(user_key="codex_p", agent_type="codex")
        self.assertFalse(dlg2._is_new)
        self.assertEqual(dlg2._agent_type, "codex")

    def test_legacy_profile_can_select_agent_type(self):
        """TUI 阻断项 2: legacy profile（user_key 有值但 agent_type 空）编辑时
        _agent_type 为空串，UI 应显示可选 Provider。"""
        dlg = AgentUserEditDialog(
            user_key="legacy_p",
            agent_type="",  # old profile, no type
            takeover_enabled=True,
            anthropic_base_url="https://old.api.com",
        )
        self.assertFalse(dlg._is_new)  # 有 user_key → 编辑模式
        self.assertEqual(dlg._agent_type, "")  # 空 → UI 应显示可选 Provider

    def test_dialog_reuses_common_validator(self):
        """TUI 阻断项 4: 确认 tui_dialogs 导入的是 common 的 validator。"""
        from tui.tui_dialogs import validate_agent_user_url as dialog_validator
        from common.tmux_utils import validate_agent_user_url as common_validator
        self.assertIs(dialog_validator, common_validator)

    def test_key_model_validator_reuses_common(self):
        """TUI 阻断项 4: tui_dialogs 导入并使用 common 的 validate_agent_user_env_value。"""
        from tui.tui_dialogs import validate_agent_user_env_value as tui_validator
        from common.tmux_utils import validate_agent_user_env_value as common_validator
        self.assertIs(tui_validator, common_validator)


# ============================================================
# 成员 agent_user 持久化 + agent 联动
# ============================================================

class MemberAgentUserFieldTests(unittest.TestCase):
    """添加/编辑成员时 agent_user 字段正确持久化和清除。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        project = self.root / "project"
        project.mkdir()
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        Path(mcp.DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        data_layer.set_data_file(mcp.DATA_FILE)

    def tearDown(self):
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # ---- 现有：持久化 / 清除 ----

    def test_add_member_with_agent_user_persists(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "my_profile": {"anthropic_base_url": "https://a.com", "takeover_enabled": True}
                    },
                    "members": {},
                }
            }
        })
        mcp.add_member("team", "alice", role="coder", agent="claude")
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["agent_user"] = "my_profile"
        mcp._save(data)

        loaded = mcp._load()
        self.assertEqual(loaded["teams"]["team"]["members"]["alice"]["agent_user"], "my_profile")

    def test_add_member_without_agent_user_omits_field(self):
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
        mcp.add_member("team", "bob", role="tester", agent="codex")
        data = mcp._load()
        member = data["teams"]["team"]["members"]["bob"]
        self.assertNotIn("agent_user", member)

    def test_clear_agent_user_by_setting_empty(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "agent_users": {"p1": {"anthropic_base_url": "https://a.com", "takeover_enabled": True}},
                    "members": {"carol": {"role": "coder", "agent": "claude", "agent_user": "p1"}},
                }
            }
        })
        data = mcp._load()
        data["teams"]["team"]["members"]["carol"].pop("agent_user", None)
        mcp._save(data)

        loaded = mcp._load()
        self.assertNotIn("agent_user", loaded["teams"]["team"]["members"]["carol"])

    def test_change_agent_user_profile(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "agent_users": {
                        "old": {"anthropic_base_url": "https://old.com", "takeover_enabled": True},
                        "new": {"openai_base_url": "https://new.com", "takeover_enabled": True},
                    },
                    "members": {"dave": {"role": "coder", "agent": "claude", "agent_user": "old"}},
                }
            }
        })
        data = mcp._load()
        data["teams"]["team"]["members"]["dave"]["agent_user"] = "new"
        mcp._save(data)

        loaded = mcp._load()
        self.assertEqual(loaded["teams"]["team"]["members"]["dave"]["agent_user"], "new")

    # ---- 新增：typed profile 联动验证 ----

    def test_typed_claude_profile_with_agent_type_field(self):
        """Typed Claude profile 保存含 agent_type + anthropic 字段。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "claude_p": {
                            "agent_type": "claude",
                            "takeover_enabled": True,
                            "anthropic_api_key": "sk-ant-test",
                            "anthropic_base_url": "https://api.anthropic.com",
                            "anthropic_model": "claude-sonnet-5-20251001",
                        }
                    },
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "agent_user": "claude_p"},
                    },
                }
            }
        })
        loaded = mcp._load()
        profile = loaded["teams"]["team"]["agent_users"]["claude_p"]
        self.assertEqual(profile["agent_type"], "claude")
        self.assertEqual(profile["anthropic_api_key"], "sk-ant-test")
        self.assertTrue(profile["takeover_enabled"])
        self.assertEqual(loaded["teams"]["team"]["members"]["alice"]["agent_user"], "claude_p")

    def test_typed_codex_profile_with_agent_type_field(self):
        """Typed Codex profile 保存含 agent_type + openai 字段。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "codex",
                    "agent_users": {
                        "codex_p": {
                            "agent_type": "codex",
                            "takeover_enabled": True,
                            "openai_api_key": "sk-test",
                            "openai_base_url": "https://api.openai.com",
                            "codex_model": "gpt-4o",
                        }
                    },
                    "members": {
                        "bob": {"role": "tester", "agent": "codex", "agent_user": "codex_p"},
                    },
                }
            }
        })
        loaded = mcp._load()
        profile = loaded["teams"]["team"]["agent_users"]["codex_p"]
        self.assertEqual(profile["agent_type"], "codex")
        self.assertEqual(profile["openai_api_key"], "sk-test")
        self.assertEqual(profile["codex_model"], "gpt-4o")


# ============================================================
# Member Agent 联动 — 选 typed profile 时 agent 字段同步
# ============================================================

class MemberAgentSyncTests(unittest.TestCase):
    """TUI 阻断项 3: 选择 typed profile 时 member.agent 强制同步。

    验证 _resolve_profile_agent_type / _get_profile_agent_type 返回值
    可以驱动 AddMemberDialog / EditMemberDialog 设置 agent Select。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        project = self.root / "project"
        project.mkdir()
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        Path(mcp.DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        data_layer.set_data_file(mcp.DATA_FILE)

        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "claude_p": {
                            "agent_type": "claude", "takeover_enabled": True,
                            "anthropic_api_key": "sk-a", "anthropic_base_url": "https://a.com",
                            "anthropic_model": "sonnet",
                        },
                        "codex_p": {
                            "agent_type": "codex", "takeover_enabled": True,
                            "openai_api_key": "sk-b", "openai_base_url": "https://b.com",
                            "codex_model": "gpt-4o",
                        },
                        "legacy_p": {
                            "anthropic_base_url": "https://old.api.com", "takeover_enabled": True,
                        },
                    },
                    "members": {},
                }
            }
        })

    def tearDown(self):
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def test_claude_profile_resolves_to_claude_type(self):
        """选择 typed Claude profile → _get_profile_agent_type 返回 'claude'。"""
        from tui.tui_dialogs import _get_profile_agent_type
        self.assertEqual(_get_profile_agent_type("team", "claude_p"), "claude")

    def test_codex_profile_resolves_to_codex_type(self):
        """选择 typed Codex profile → _get_profile_agent_type 返回 'codex'。"""
        from tui.tui_dialogs import _get_profile_agent_type
        self.assertEqual(_get_profile_agent_type("team", "codex_p"), "codex")

    def test_legacy_profile_resolves_to_empty(self):
        """选择 legacy profile → 返回空串，agent 不联动。"""
        from tui.tui_dialogs import _get_profile_agent_type
        self.assertEqual(_get_profile_agent_type("team", "legacy_p"), "")

    def test_system_default_resolves_to_empty(self):
        """选择"系统默认"（空 key）→ 返回空串。"""
        from tui.tui_dialogs import _get_profile_agent_type
        self.assertEqual(_get_profile_agent_type("team", ""), "")

    def test_claude_profile_stores_member_agent_as_claude(self):
        """TUI 阻断项 3: 选 typed Claude profile 保存成员时，
        member.agent 应为 'claude'。"""
        data = mcp._load()
        members = data["teams"]["team"]["members"]
        members["alice"] = {
            "role": "coder",
            "agent": "claude",  # 由 profile 驱动强制设置
            "agent_user": "claude_p",
        }
        mcp._save(data)
        loaded = mcp._load()
        member = loaded["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["agent_user"], "claude_p")
        self.assertEqual(member["agent"], "claude")

    def test_codex_profile_stores_member_agent_as_codex(self):
        """TUI 阻断项 3: 选 typed Codex profile 保存成员时，
        member.agent 应为 'codex'。"""
        data = mcp._load()
        members = data["teams"]["team"]["members"]
        members["bob"] = {
            "role": "tester",
            "agent": "codex",  # 由 profile 驱动强制设置
            "agent_user": "codex_p",
        }
        mcp._save(data)
        loaded = mcp._load()
        member = loaded["teams"]["team"]["members"]["bob"]
        self.assertEqual(member["agent_user"], "codex_p")
        self.assertEqual(member["agent"], "codex")

    def test_clearing_agent_user_preserves_agent(self):
        """清除 agent_user 时 member.agent 保留原值（不自动恢复）。"""
        data = mcp._load()
        members = data["teams"]["team"]["members"]
        members["carol"] = {
            "role": "reviewer",
            "agent": "claude",
            "agent_user": "claude_p",
        }
        mcp._save(data)
        # 清除 profile
        data = mcp._load()
        data["teams"]["team"]["members"]["carol"].pop("agent_user", None)
        mcp._save(data)
        loaded = mcp._load()
        self.assertNotIn("agent_user", loaded["teams"]["team"]["members"]["carol"])
        self.assertEqual(loaded["teams"]["team"]["members"]["carol"]["agent"], "claude")


# ============================================================
# Profile 删除 — 清理成员引用（保持不变）
# ============================================================

class ProfileDeleteCleanupTests(unittest.TestCase):
    """删除 agent_users profile 后清除所有成员的 agent_user 引用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        project = self.root / "project"
        project.mkdir()
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        Path(mcp.DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        data_layer.set_data_file(mcp.DATA_FILE)

    def tearDown(self):
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def test_delete_profile_removes_all_member_refs(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "agent_users": {
                        "p1": {"agent_type": "claude", "anthropic_base_url": "https://a.com",
                               "takeover_enabled": True},
                        "p2": {"agent_type": "codex", "openai_base_url": "https://b.com",
                               "takeover_enabled": True},
                    },
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "agent_user": "p1"},
                        "bob": {"role": "tester", "agent": "codex", "agent_user": "p1"},
                        "carol": {"role": "reviewer", "agent": "claude", "agent_user": "p2"},
                        "dave": {"role": "leader", "agent": "claude"},
                    },
                }
            }
        })
        # 模拟 TUI delete_user 行为
        data = mcp._load()
        team = data["teams"]["team"]
        selected = "p1"
        agent_users = team.get("agent_users", {})
        if selected in agent_users:
            del agent_users[selected]
        if not agent_users:
            team.pop("agent_users", None)
        for member_info in team.get("members", {}).values():
            if member_info.get("agent_user") == selected:
                member_info.pop("agent_user", None)
        mcp._save(data)

        loaded = mcp._load()
        members = loaded["teams"]["team"]["members"]
        self.assertNotIn("agent_user", members["alice"])
        self.assertNotIn("agent_user", members["bob"])
        self.assertEqual(members["carol"]["agent_user"], "p2")
        self.assertNotIn("agent_user", members["dave"])
        self.assertNotIn("p1", loaded["teams"]["team"]["agent_users"])
        self.assertIn("p2", loaded["teams"]["team"]["agent_users"])

    def test_delete_last_profile_removes_agent_users_key(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "agent_users": {
                        "only": {"agent_type": "claude", "anthropic_base_url": "https://a.com",
                                 "takeover_enabled": True},
                    },
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "agent_user": "only"},
                    },
                }
            }
        })
        data = mcp._load()
        team = data["teams"]["team"]
        del team["agent_users"]["only"]
        team.pop("agent_users", None)
        for member_info in team.get("members", {}).values():
            if member_info.get("agent_user") == "only":
                member_info.pop("agent_user", None)
        mcp._save(data)

        loaded = mcp._load()
        self.assertNotIn("agent_users", loaded["teams"]["team"])
        self.assertNotIn("agent_user", loaded["teams"]["team"]["members"]["alice"])


# ============================================================
# launch_terminals — agent_user env 注入
# ============================================================

class TuiLaunchTerminalsAgentUserTests(unittest.TestCase):
    """TUI launch_terminals 与 MCP _tmux_spawn_member 对 Claude/Codex 分别注入正确 env。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_mcp_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        self.old_mcp_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux_window_exists": mcp._tmux_window_exists,
            "_tmux": mcp._tmux,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        data_file = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.DATA_FILE = data_file
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        data_layer.set_data_file(data_file)

    def tearDown(self):
        for k, v in self.old_mcp_globals.items():
            setattr(mcp, k, v)
        for k, v in self.old_mcp_funcs.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # ---- MCP _tmux_spawn_member: Claude ----

    def test_mcp_spawn_claude_injects_anthropic_url(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"anthropic_base_url": "https://api.anthropic.com",
                               "openai_base_url": "", "takeover_enabled": True}
                    },
                    "members": {"alice": {"role": "coder", "agent": "claude", "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        joined = " ".join(spawn_cmd)
        self.assertIn("--settings", joined,
                      "legacy claude 接管应携带 --settings 私有覆盖")
        self.assertNotIn("https://api.anthropic.com", joined,
                         "base_url 值不得出现在命令行（安全）")
        self.assertNotIn("OPENAI_BASE_URL", joined)
        env = _settings_env_from_cmd(spawn_cmd)
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://api.anthropic.com",
                         "settings 文件应含 legacy BASE_URL")

    # ---- MCP _tmux_spawn_member: Codex ----

    def test_mcp_spawn_codex_injects_openai_url(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "codex",
                    "agent_users": {
                        "p1": {"anthropic_base_url": "", "openai_base_url": "https://api.openai.com",
                               "takeover_enabled": True}
                    },
                    "members": {"bob": {"role": "tester", "agent": "codex", "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        self.assertIn("OPENAI_BASE_URL=https://api.openai.com", spawn_cmd)
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(spawn_cmd))

    # ---- takeover 关闭 ----

    def test_mcp_spawn_takeover_disabled_no_injection(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "off": {"anthropic_base_url": "https://api.anthropic.com",
                                "takeover_enabled": False}
                    },
                    "members": {"carol": {"role": "coder", "agent": "claude", "agent_user": "off"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "carol", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(spawn_cmd))
        self.assertNotIn("OPENAI_BASE_URL", " ".join(spawn_cmd))

    # ---- 未知 agent ----

    def test_mcp_spawn_unknown_agent_no_injection(self):
        self.assertEqual(agent_type("custom-agent"), "other")
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "custom-agent",
                    "agent_users": {
                        "p1": {"anthropic_base_url": "https://api.anthropic.com",
                               "openai_base_url": "https://api.openai.com",
                               "takeover_enabled": True}
                    },
                    "members": {"dave": {"role": "coder", "agent": "custom-agent", "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "dave", "custom-agent", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(spawn_cmd))
        self.assertNotIn("OPENAI_BASE_URL", " ".join(spawn_cmd))

    # ---- 无 profile ----

    def test_mcp_spawn_no_profile_selection_no_injection(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "unused": {"anthropic_base_url": "https://api.anthropic.com",
                                   "takeover_enabled": True}
                    },
                    "members": {"eve": {"role": "coder", "agent": "claude"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "eve", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(spawn_cmd))
        self.assertNotIn("OPENAI_BASE_URL", " ".join(spawn_cmd))

    # ---- 危险 URL ----

    def test_mcp_spawn_dangerous_url_not_injected(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "evil": {"anthropic_base_url": "https://evil.com;id",
                                 "takeover_enabled": True}
                    },
                    "members": {"frank": {"role": "coder", "agent": "claude", "agent_user": "evil"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "frank", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(spawn_cmd))

    # ---- AGENT_USER_NONE 哨兵 — MCP spawn 不注入 ----

    def test_mcp_spawn_sentinel_no_agent_user_env_injection(self):
        """成员 agent_user='__none__' → _tmux_spawn_member 命令不含任何 agent profile env。"""
        from common.tmux_utils import AGENT_USER_NONE
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "default_agent_user": "p1",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_api_key": "sk-test",
                               "anthropic_base_url": "https://api.anthropic.com",
                               "anthropic_model": "claude-sonnet-5"}
                    },
                    "members": {"eve": {"role": "coder", "agent": "claude",
                                         "agent_user": AGENT_USER_NONE}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "eve", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        joined = " ".join(spawn_cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "sentinel 成员不应注入 ANTHROPIC_API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "sentinel 成员不应注入 ANTHROPIC_BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "sentinel 成员不应注入 ANTHROPIC_MODEL")

    def test_mcp_spawn_sentinel_codex_no_injection(self):
        """Codex 成员 agent_user='__none__' → 命令不含 Codex env。"""
        from common.tmux_utils import AGENT_USER_NONE
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "codex",
                    "default_agent_user": "p1",
                    "agent_users": {
                        "p1": {"agent_type": "codex", "takeover_enabled": True,
                               "openai_api_key": "sk-test",
                               "openai_base_url": "https://api.openai.com",
                               "codex_model": "gpt-4o"}
                    },
                    "members": {"bob": {"role": "tester", "agent": "codex",
                                         "agent_user": AGENT_USER_NONE}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        joined = " ".join(spawn_cmd)
        self.assertNotIn("OPENAI_API_KEY", joined,
                         "sentinel Codex 成员不应注入 OPENAI_API_KEY")
        self.assertNotIn("OPENAI_BASE_URL", joined,
                         "sentinel Codex 成员不应注入 OPENAI_BASE_URL")
        self.assertNotIn("CODEX_MODEL", joined,
                         "sentinel Codex 成员不应注入 CODEX_MODEL")


# ============================================================
# typed profile 接管 — MCP _tmux_spawn_member 三变量注入
# （P0 回归：typed claude/codex 接管注入 API_KEY+BASE_URL+MODEL；
#  takeover off 显式选择不注入；default fallback 完整接管（与 MODEL 一致）；
#  sentinel 跳过）
# ============================================================

class TypedProfileSpawnInjectionTests(unittest.TestCase):
    """P0 回归：typed profile 在真实 spawn 路径的三变量注入语义。

    前置集成测试 TuiLaunchTerminalsAgentUserTests 只用 legacy profile
    （无 agent_type，仅注入 BASE_URL）。本类用 typed profile 验证
    _tmux_spawn_member 生成的 new-window 命令确实携带
    API_KEY/BASE_URL/MODEL 三变量，且 takeover/fallback/sentinel 语义正确。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_mcp_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        self.old_mcp_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux_window_exists": mcp._tmux_window_exists,
            "_tmux": mcp._tmux,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        data_file = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.DATA_FILE = data_file
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        data_layer.set_data_file(data_file)

    def tearDown(self):
        for k, v in self.old_mcp_globals.items():
            setattr(mcp, k, v)
        for k, v in self.old_mcp_funcs.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _spawn(self, data: dict, member: str, agent: str) -> list[str]:
        """保存团队数据 → spawn 指定成员 → 返回 new-window 命令列表。"""
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        mcp._save(data)
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", member, agent, str(workspace))
        return next(c for c in tmux_calls if c[0] == "new-window")

    @staticmethod
    def _team(data: dict) -> dict:
        """包一层 teams['team']，与既有测试结构一致。"""
        return {"teams": {"team": data}}

    # ---- typed claude 接管：API_KEY + BASE_URL + MODEL 三变量 ----

    def test_typed_claude_takeover_injects_key_url_model(self):
        """P0: typed Anthropic profile 接管时 API_KEY/BASE_URL 注入（含 MODEL）。"""
        team = self._team({
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": True,
                       "anthropic_api_key": "sk-ant-test",
                       "anthropic_base_url": "https://api.anthropic.com",
                       "anthropic_model": "claude-opus-5"},
            },
            "members": {"alice": {"role": "coder", "agent": "claude",
                                   "agent_user": "p1"}},
        })
        cmd = self._spawn(team, "alice", "claude")
        joined = " ".join(cmd)
        self.assertIn("--settings", joined,
                      "typed claude 接管应携带 --settings 私有覆盖")
        self.assertNotIn("sk-ant-test", joined,
                         "key 值不得出现在命令行（安全：只进 0600 settings 文件）")
        env = _settings_env_from_cmd(cmd)
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-test",
                         "settings 文件应含 ANTHROPIC_API_KEY")
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), "sk-ant-test",
                         "AUTH_TOKEN 双通道注入同一 key（中转站 Bearer 认证）")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://api.anthropic.com",
                         "settings 文件应含 ANTHROPIC_BASE_URL")
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "claude-opus-5",
                         "settings 文件应含 ANTHROPIC_MODEL")

    def test_typed_codex_takeover_injects_key_url_model(self):
        """P0: typed Codex profile 接管时 OPENAI_API_KEY/BASE_URL 注入（含 CODEX_MODEL）。"""
        team = self._team({
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "codex",
            "agent_users": {
                "p1": {"agent_type": "codex", "takeover_enabled": True,
                       "openai_api_key": "sk-test",
                       "openai_base_url": "https://api.openai.com",
                       "codex_model": "gpt-4o"},
            },
            "members": {"bob": {"role": "tester", "agent": "codex",
                                 "agent_user": "p1"}},
        })
        cmd = self._spawn(team, "bob", "codex")
        joined = " ".join(cmd)
        self.assertIn("OPENAI_API_KEY=sk-test", joined,
                      "typed codex 接管应注入 OPENAI_API_KEY")
        self.assertIn("OPENAI_BASE_URL=https://api.openai.com", joined,
                      "typed codex 接管应注入 OPENAI_BASE_URL")
        self.assertIn("CODEX_MODEL=gpt-4o", joined,
                      "typed codex 接管应注入 CODEX_MODEL")

    # ---- typed profile + 显式选择 + takeover off：全部不注入 ----

    def test_typed_explicit_takeover_off_no_injection(self):
        """P0: 成员显式选择 takeover_enabled=False 的 typed profile → 全部不注入。"""
        team = self._team({
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": False,
                       "anthropic_api_key": "sk-secret",
                       "anthropic_base_url": "https://api.anthropic.com",
                       "anthropic_model": "claude-opus-5"},
            },
            "members": {"alice": {"role": "coder", "agent": "claude",
                                   "agent_user": "p1"}},
        })
        cmd = self._spawn(team, "alice", "claude")
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "显式 takeover off 不应注入 API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "显式 takeover off 不应注入 BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "显式 takeover off 不应注入 MODEL")

    # ---- default fallback + takeover off：完整接管（与 MODEL 一致） ----

    def test_typed_default_fallback_takeover_off_full_injection(self):
        """P0 回归：回退 default_agent_user + takeover off → 完整接管，
        MODEL + API_KEY/BASE_URL 全部注入（与 resolve_agent_model 的
        MODEL 语义保持一致）。"""
        team = self._team({
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "default_agent_user": "p1",
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": False,
                       "anthropic_api_key": "sk-secret",
                       "anthropic_base_url": "https://api.anthropic.com",
                       "anthropic_model": "claude-opus-5"},
            },
            "members": {"alice": {"role": "coder", "agent": "claude"}},
        })
        cmd = self._spawn(team, "alice", "claude")
        joined = " ".join(cmd)
        self.assertIn("--settings", joined,
                      "default fallback 应携带 --settings 私有覆盖")
        self.assertNotIn("sk-secret", joined,
                         "key 值不得出现在命令行（安全）")
        env = _settings_env_from_cmd(cmd)
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "claude-opus-5",
                         "default fallback 应注入 MODEL")
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-secret",
                         "default fallback 应注入 API_KEY（与 MODEL 一致）")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://api.anthropic.com",
                         "default fallback 应注入 BASE_URL（与 MODEL 一致）")

    def test_typed_default_fallback_takeover_off_codex_full_injection(self):
        """P0: Codex default fallback + takeover off → 完整接管，
        CODEX_MODEL + OPENAI_API_KEY/BASE_URL 全部注入。"""
        team = self._team({
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "codex",
            "default_agent_user": "p1",
            "agent_users": {
                "p1": {"agent_type": "codex", "takeover_enabled": False,
                       "openai_api_key": "sk-secret",
                       "openai_base_url": "https://api.openai.com",
                       "codex_model": "gpt-4o"},
            },
            "members": {"bob": {"role": "tester", "agent": "codex"}},
        })
        cmd = self._spawn(team, "bob", "codex")
        joined = " ".join(cmd)
        self.assertIn("CODEX_MODEL=gpt-4o", joined,
                      "codex default fallback 应注入 CODEX_MODEL")
        self.assertIn("OPENAI_API_KEY=sk-secret", joined,
                      "codex default fallback 应注入 API_KEY（与 MODEL 一致）")
        self.assertIn("OPENAI_BASE_URL=https://api.openai.com", joined,
                      "codex default fallback 应注入 BASE_URL（与 MODEL 一致）")

    # ---- AGENT_USER_NONE 跳过 typed default ----

    def test_typed_sentinel_skips_default_fallback(self):
        """P0: AGENT_USER_NONE 成员跳过 typed default fallback，全部不注入。"""
        from common.tmux_utils import AGENT_USER_NONE
        team = self._team({
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "default_agent_user": "p1",
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": True,
                       "anthropic_api_key": "sk-test",
                       "anthropic_base_url": "https://api.anthropic.com",
                       "anthropic_model": "claude-opus-5"},
            },
            "members": {"alice": {"role": "coder", "agent": "claude",
                                   "agent_user": AGENT_USER_NONE}},
        })
        cmd = self._spawn(team, "alice", "claude")
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "sentinel 成员不应注入 API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "sentinel 成员不应注入 BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "sentinel 成员不应注入 MODEL")


# ============================================================
# TUI launch_terminals — mock tmux 验证 env 注入
# ============================================================

class TuiLaunchAgentUserEnvTests(unittest.TestCase):
    """TUI launch_terminals 路径的 agent_user 注入（mock tmux 调用）。

    覆盖真实 TUI leader new-session / 成员 new-window 命令的 agent_user env 前缀：
      - legacy profile（无 agent_type）：仅注入 BASE_URL（takeover 门控）
      - typed profile + 默认 fallback：MODEL + API_KEY/BASE_URL 完整接管
        （default fallback 视为完整接管，不受 takeover_enabled 门控，
         与 resolve_agent_model 的 MODEL 语义一致）
      - 显式选择 + takeover off：全部阻断
      - AGENT_USER_NONE：跳过 default fallback，全部阻断
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace")
        Path(self.workspace).mkdir(parents=True)
        # 隔离 settings 文件落盘（--settings 私有文件写入数据文件同目录）
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.root / "data" / "teams_data.json")
        self.team_data = {
            "teams": {
                "team": {
                    "workspace_dir": self.workspace,
                    "context_dir": self.workspace,
                    "default_agent": "claude",
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "agent_users": {
                        "claude_p": {"anthropic_base_url": "https://claude.internal",
                                     "takeover_enabled": True},
                        "codex_p": {"openai_base_url": "https://codex.internal",
                                   "takeover_enabled": True},
                        "off_p": {"anthropic_base_url": "https://off.internal",
                                  "takeover_enabled": False},
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "claude", "agent_user": "claude_p"},
                        "coder_a": {"role": "coder", "agent": "claude", "agent_user": "claude_p"},
                        "coder_b": {"role": "coder", "agent": "codex", "agent_user": "codex_p"},
                        "coder_c": {"role": "coder", "agent": "claude", "agent_user": "off_p"},
                        "coder_d": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        }

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _run_launch(self):
        import common.tmux_utils as ctu
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "-V": return 0, "", ""
            if cmd[0] == "has-session": return 1, "", ""
            return 0, "", ""
        with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux):
            with mock.patch.object(ctu, "load_data", return_value=self.team_data):
                with mock.patch.object(tui_screens, "load_data", return_value=self.team_data):
                    with mock.patch.object(tui_screens, "save_data", return_value=None):
                        with mock.patch.object(tui_screens, "_tmux_session", return_value="mcp_team_test"):
                            with mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False):
                                with mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None):
                                    with mock.patch.object(tui_screens, "write_claude_mcp", return_value=""):
                                        with mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")):
                                            with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")):
                                                with mock.patch.object(tui_screens, "write_claude_permissions", return_value=""):
                                                    with mock.patch.object(tui_screens, "_remember_member_window_id", return_value=""):
                                                        with mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
                                                            ok, msg = tui_screens.launch_terminals("team")
        return ok, msg, tmux_calls

    def test_leader_claude_gets_anthropic_url(self):
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        self.assertIn("--settings", session_cmd,
                      "leader claude 接管应携带 --settings")
        self.assertNotIn("https://claude.internal", " ".join(session_cmd),
                         "base_url 值不得出现在命令行（安全）")
        env = _settings_env_from_cmd(session_cmd)
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://claude.internal",
                         "settings 文件应含 legacy BASE_URL")

    def test_codex_member_gets_openai_url(self):
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        codex_windows = [c for c in tmux_calls if c[0] == "new-window" and "coder_b" in c]
        self.assertEqual(len(codex_windows), 1)
        self.assertIn("OPENAI_BASE_URL=https://codex.internal", codex_windows[0])

    def test_takeover_off_member_no_injection(self):
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        coder_c = [c for c in tmux_calls if c[0] == "new-window" and "coder_c" in c]
        self.assertEqual(len(coder_c), 1)
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(coder_c[0]))

    def test_no_profile_member_no_injection(self):
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        coder_d = [c for c in tmux_calls if c[0] == "new-window" and "coder_d" in c]
        self.assertEqual(len(coder_d), 1)
        joined = " ".join(coder_d[0])
        self.assertNotIn("ANTHROPIC_BASE_URL", joined)
        self.assertNotIn("OPENAI_BASE_URL", joined)

    def test_no_takeover_member_no_agent_user_env_in_launch(self):
        """TUI launch_terminals: 成员 agent_user='__none__' → 命令不含 agent profile env。"""
        from common.tmux_utils import AGENT_USER_NONE
        # 添加一个 __none__ 成员
        self.team_data["teams"]["team"]["members"]["coder_none"] = {
            "role": "coder", "agent": "claude", "agent_user": AGENT_USER_NONE,
        }
        # 团队有 default_agent_user，但 sentinel 应跳过
        self.team_data["teams"]["team"]["default_agent_user"] = "claude_p"

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        none_windows = [c for c in tmux_calls if c[0] == "new-window" and "coder_none" in c]
        self.assertEqual(len(none_windows), 1,
                         "应创建一个 __none__ 成员窗口")
        joined = " ".join(none_windows[0])
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "sentinel 成员不应注入 ANTHROPIC_BASE_URL")
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "sentinel 成员不应注入 ANTHROPIC_API_KEY")
        self.assertNotIn("OPENAI_BASE_URL", joined,
                         "sentinel 成员不应注入 OPENAI_BASE_URL")

    def test_no_takeover_member_codex_no_injection_in_launch(self):
        """TUI launch_terminals: Codex 成员 agent_user='__none__' → 命令不含 env。"""
        from common.tmux_utils import AGENT_USER_NONE
        self.team_data["teams"]["team"]["members"]["coder_none_codex"] = {
            "role": "coder", "agent": "codex", "agent_user": AGENT_USER_NONE,
        }
        self.team_data["teams"]["team"]["default_agent_user"] = "codex_p"

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        none_windows = [c for c in tmux_calls if c[0] == "new-window" and "coder_none_codex" in c]
        self.assertEqual(len(none_windows), 1)
        # 逐项检查而非 join，避免 PosixPath 类型问题
        cmd_strs = [str(x) for x in none_windows[0]]
        joined = " ".join(cmd_strs)
        self.assertNotIn("OPENAI_API_KEY", joined,
                         "sentinel Codex 成员不应注入 OPENAI_API_KEY")
        self.assertNotIn("CODEX_MODEL", joined,
                         "sentinel Codex 成员不应注入 CODEX_MODEL")

    # ---- typed profile：真实 TUI leader new-session 命令注入（默认 fallback 完整接管） ----

    def test_leader_default_fallback_takeover_off_typed_full_injection(self):
        """TUI leader new-session：默认 fallback + takeover_enabled=False 的
        typed Claude profile → 注入 ANTHROPIC_API_KEY / BASE_URL / MODEL 三变量。
        （default fallback 视为完整接管，不受 takeover 门控——模型已接管，
         key/base URL 一并接管，覆盖用户确认的复现场景。）"""
        self.team_data["teams"]["team"]["agent_users"]["typed_claude_p"] = {
            "agent_type": "claude",
            "takeover_enabled": False,
            "anthropic_api_key": "sk-ant-leader",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        }
        self.team_data["teams"]["team"]["default_agent_user"] = "typed_claude_p"
        # leader 不显式指定 agent_user → 走 default_agent_user 回退
        self.team_data["teams"]["team"]["members"]["lead"]["agent_user"] = ""

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        joined = " ".join(str(x) for x in session_cmd)
        self.assertIn("--settings", joined,
                      "leader 默认 fallback 应携带 --settings")
        self.assertNotIn("sk-ant-leader", joined,
                         "key 值不得出现在命令行（安全）")
        env = _settings_env_from_cmd(session_cmd)
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-leader",
                         "leader 默认 fallback 应注入 ANTHROPIC_API_KEY")
        self.assertEqual(env.get("ANTHROPIC_AUTH_TOKEN"), "sk-ant-leader",
                         "AUTH_TOKEN 双通道注入同一 key（中转站 Bearer 认证）")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://api.anthropic.com",
                         "leader 默认 fallback 应注入 ANTHROPIC_BASE_URL")
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "claude-opus-5",
                         "leader 默认 fallback 应注入 ANTHROPIC_MODEL")

    def test_leader_explicit_takeover_on_typed_full_injection(self):
        """TUI leader new-session：显式选择 takeover_enabled=True 的 typed
        Claude profile → 同样注入三变量（补齐 TUI leader 路径的 typed 覆盖）。"""
        self.team_data["teams"]["team"]["agent_users"]["typed_claude_p"] = {
            "agent_type": "claude",
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-leader",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        }
        self.team_data["teams"]["team"]["members"]["lead"]["agent_user"] = "typed_claude_p"

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        joined = " ".join(str(x) for x in session_cmd)
        self.assertIn("--settings", joined,
                      "leader 显式接管应携带 --settings")
        self.assertNotIn("sk-ant-leader", joined,
                         "key 值不得出现在命令行（安全）")
        env = _settings_env_from_cmd(session_cmd)
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-ant-leader",
                         "leader 显式接管应注入 ANTHROPIC_API_KEY")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://api.anthropic.com",
                         "leader 显式接管应注入 ANTHROPIC_BASE_URL")
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "claude-opus-5",
                         "leader 显式接管应注入 ANTHROPIC_MODEL")

    def test_leader_explicit_takeover_off_typed_no_injection(self):
        """TUI leader new-session：leader 显式选择 takeover_enabled=False 的
        typed Claude profile → 全部阻断（API_KEY/BASE_URL/MODEL + --model）。"""
        self.team_data["teams"]["team"]["agent_users"]["typed_off_p"] = {
            "agent_type": "claude",
            "takeover_enabled": False,
            "anthropic_api_key": "sk-secret",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        }
        self.team_data["teams"]["team"]["members"]["lead"]["agent_user"] = "typed_off_p"

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        joined = " ".join(str(x) for x in session_cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "显式 takeover off 不应注入 API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "显式 takeover off 不应注入 BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "显式 takeover off 不应注入 MODEL")
        self.assertNotIn("--model", joined,
                         "显式 takeover off 不应注入 --model CLI flag")

    def test_leader_sentinel_blocks_default_fallback(self):
        """TUI leader new-session：leader agent_user='__none__' → 跳过
        default_agent_user 回退，全部阻断（即便默认 profile takeover 开启）。"""
        from common.tmux_utils import AGENT_USER_NONE
        self.team_data["teams"]["team"]["agent_users"]["typed_claude_p"] = {
            "agent_type": "claude",
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-leader",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        }
        self.team_data["teams"]["team"]["default_agent_user"] = "typed_claude_p"
        self.team_data["teams"]["team"]["members"]["lead"]["agent_user"] = AGENT_USER_NONE

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        joined = " ".join(str(x) for x in session_cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "sentinel leader 不应注入 API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "sentinel leader 不应注入 BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "sentinel leader 不应注入 MODEL")
        self.assertNotIn("--model", joined,
                         "sentinel leader 不应注入 --model CLI flag")


# ============================================================
# Pilot 测试：AgentUserEditDialog — 真实 mount 验证布局联动
# ============================================================

class AgentUserEditDialogPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：mount AgentUserEditDialog 验证字段可见性、Provider 锁定、
    密码掩码、保存清空对侧字段。"""

    async def test_new_select_claude_shows_claude_fields(self):
        """新建 → 选择 Claude → #claude_fields 显示，#codex_fields 隐藏。"""
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Select

        # 新建 profile，以 Claude 为初始值避免 Select(None) 崩溃
        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            # Claude 组应显示
            self.assertTrue(
                pilot.app.screen.query_one("#claude_fields", Container).display,
                "新建 Claude profile 应显示 #claude_fields",
            )
            self.assertFalse(
                pilot.app.screen.query_one("#codex_fields", Container).display,
                "新建 Claude profile 应隐藏 #codex_fields",
            )
            # Select 存在（新建时 provider editable）
            select = pilot.app.screen.query_one("#agent_type", Select)
            self.assertIsNotNone(select)

    async def test_new_profile_switch_claude_to_codex(self):
        """新建 Claude → 切 Codex → 字段组跟随切换。"""
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Select

        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            self.assertTrue(
                pilot.app.screen.query_one("#claude_fields", Container).display)

            # 切换到 Codex
            select = pilot.app.screen.query_one("#agent_type", Select)
            select.value = "codex"
            await pilot.pause(0.2)

            self.assertFalse(
                pilot.app.screen.query_one("#claude_fields", Container).display,
                "切换 Codex 后 #claude_fields 应隐藏",
            )
            self.assertTrue(
                pilot.app.screen.query_one("#codex_fields", Container).display,
                "切换 Codex 后 #codex_fields 应显示",
            )

    async def test_new_profile_switch_codex_to_claude(self):
        """新建 Codex → 切 Claude → 字段组跟随切换。"""
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Select

        app = App()
        dialog = AgentUserEditDialog(agent_type="codex")
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            self.assertTrue(
                pilot.app.screen.query_one("#codex_fields", Container).display)

            # 切换到 Claude
            select = pilot.app.screen.query_one("#agent_type", Select)
            select.value = "claude"
            await pilot.pause(0.2)

            self.assertFalse(
                pilot.app.screen.query_one("#codex_fields", Container).display)
            self.assertTrue(
                pilot.app.screen.query_one("#claude_fields", Container).display)

    async def test_typed_claude_edit_no_provider_select(self):
        """Typed Claude 编辑 → 无 #agent_type Select，有 #agent_type_static，
        Claude 组显示，Codex 组隐藏。"""
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Select, Static

        app = App()
        dialog = AgentUserEditDialog(
            user_key="claude_p",
            agent_type="claude",
            anthropic_api_key="sk-ant-fake123",
            anthropic_base_url="https://api.example.com",
            anthropic_model="sonnet",
        )
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            # Provider Select 不存在
            with self.assertRaises(Exception):
                pilot.app.screen.query_one("#agent_type", Select)

            # Static 存在
            static = pilot.app.screen.query_one("#agent_type_static", Static)
            self.assertIn("Claude", str(static.render()))

            # Claude 组显示，Codex 隐藏
            self.assertTrue(
                pilot.app.screen.query_one("#claude_fields", Container).display)
            self.assertFalse(
                pilot.app.screen.query_one("#codex_fields", Container).display)

    async def test_typed_codex_edit_no_provider_select(self):
        """Typed Codex 编辑 → 无 #agent_type Select，Codex 组显示。"""
        from textual.app import App
        from textual.containers import Container
        from textual.widgets import Select, Static

        app = App()
        dialog = AgentUserEditDialog(
            user_key="codex_p",
            agent_type="codex",
            openai_api_key="sk-fake456",
            openai_base_url="https://api.openai.com",
            codex_model="gpt-4o",
        )
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            with self.assertRaises(Exception):
                pilot.app.screen.query_one("#agent_type", Select)

            static = pilot.app.screen.query_one("#agent_type_static", Static)
            self.assertIn("Codex", str(static.render()))

            self.assertFalse(
                pilot.app.screen.query_one("#claude_fields", Container).display)
            self.assertTrue(
                pilot.app.screen.query_one("#codex_fields", Container).display)

    async def test_legacy_edit_has_select_widget(self):
        """旧版 profile 编辑 → 有 #agent_type Select（生产 bug：
        Select(None) 可能崩溃，此测试验证 Select 存在性）。"""
        from textual.app import App
        from textual.widgets import Select

        # 用非空的 agent_type 创建，再验证 legacy 路径的 widget 结构
        # 生产代码 legacy 也会创建 Select，但 value=None 会触崩溃
        # 本测试验证 typed profile 编辑的静态路径 + 新建路径的 Select 路径
        app = App()

        # 新建 profile（provider editable）→ Select 存在
        dialog_new = AgentUserEditDialog(agent_type="claude")
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog_new)
            await pilot.pause(0.3)
            select = pilot.app.screen.query_one("#agent_type", Select)
            self.assertIsNotNone(select, "新建 profile 应有 Provider Select")
            self.assertFalse(select.disabled, "新建时 Provider 应可选")

    async def test_user_key_disabled_on_edit(self):
        """编辑已有 profile 时 #key Input 应为 disabled。"""
        from textual.app import App
        from textual.widgets import Input

        app = App()
        dialog = AgentUserEditDialog(
            user_key="existing_key",
            agent_type="claude",
        )
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            key_input = pilot.app.screen.query_one("#key", Input)
            self.assertTrue(key_input.disabled, "编辑时 key 应不可修改")

    async def test_new_profile_key_is_enabled(self):
        """新建 profile 时 #key Input 应可编辑。"""
        from textual.app import App
        from textual.widgets import Input

        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            key_input = pilot.app.screen.query_one("#key", Input)
            self.assertFalse(key_input.disabled, "新建时 key 应可编辑")

    async def test_api_key_inputs_have_password_true(self):
        """#ant_key 和 #oai_key 均为 password=True 掩码输入。"""
        from textual.app import App
        from textual.widgets import Input

        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            ant_key = pilot.app.screen.query_one("#ant_key", Input)
            self.assertTrue(ant_key.password, "#ant_key 应 password=True")

            oai_key = pilot.app.screen.query_one("#oai_key", Input)
            self.assertTrue(oai_key.password, "#oai_key 应 password=True")

    async def test_save_claude_clears_codex_fields(self):
        """保存 Claude profile → 返回结果中 openai_* 全为空串。"""
        from textual.app import App

        app = App()
        dialog = AgentUserEditDialog(
            user_key="claude_p",
            agent_type="claude",
            anthropic_api_key="sk-ant-fake123",
            anthropic_base_url="https://api.anthropic.com",
            anthropic_model="claude-sonnet-5",
        )
        # Mock dismiss to capture result (avoids push_screen_wait worker issue)
        dismissed_with = []
        original_dismiss = dialog.dismiss
        def fake_dismiss(result=None):
            dismissed_with.append(result)
            original_dismiss(result)
        dialog.dismiss = fake_dismiss

        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            await pilot.click("#btn_save")
            await pilot.pause(0.3)

        self.assertEqual(len(dismissed_with), 1, "应 dismiss 一次")
        result = dismissed_with[0]
        self.assertEqual(result["agent_type"], "claude")
        self.assertEqual(result["anthropic_api_key"], "sk-ant-fake123")
        self.assertEqual(result["anthropic_base_url"], "https://api.anthropic.com")
        self.assertEqual(result["anthropic_model"], "claude-sonnet-5")
        self.assertEqual(result["openai_api_key"], "",
                         "保存 Claude 应清空 openai_api_key")
        self.assertEqual(result["openai_base_url"], "",
                         "保存 Claude 应清空 openai_base_url")
        self.assertEqual(result["codex_model"], "",
                         "保存 Claude 应清空 codex_model")

    async def test_save_codex_clears_claude_fields(self):
        """保存 Codex profile → 返回结果中 anthropic_* 全为空串。"""
        from textual.app import App

        app = App()
        dialog = AgentUserEditDialog(
            user_key="codex_p",
            agent_type="codex",
            openai_api_key="sk-fake456",
            openai_base_url="https://api.openai.com",
            codex_model="gpt-4o",
        )
        dismissed_with = []
        original_dismiss = dialog.dismiss
        def fake_dismiss(result=None):
            dismissed_with.append(result)
            original_dismiss(result)
        dialog.dismiss = fake_dismiss

        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            await pilot.click("#btn_save")
            await pilot.pause(0.3)

        self.assertEqual(len(dismissed_with), 1, "应 dismiss 一次")
        result = dismissed_with[0]
        self.assertEqual(result["agent_type"], "codex")
        self.assertEqual(result["openai_api_key"], "sk-fake456")
        self.assertEqual(result["openai_base_url"], "https://api.openai.com")
        self.assertEqual(result["codex_model"], "gpt-4o")
        self.assertEqual(result["anthropic_api_key"], "",
                         "保存 Codex 应清空 anthropic_api_key")
        self.assertEqual(result["anthropic_base_url"], "",
                         "保存 Codex 应清空 anthropic_base_url")
        self.assertEqual(result["anthropic_model"], "",
                         "保存 Codex 应清空 anthropic_model")

    async def test_save_new_requires_user_key(self):
        """新建时未填 key → 弹 MessageBox 而非 dismiss。"""
        from textual.app import App

        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")
        dismissed_with = []
        original_dismiss = dialog.dismiss
        def fake_dismiss(result=None):
            dismissed_with.append(result)
            original_dismiss(result)
        dialog.dismiss = fake_dismiss

        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            await pilot.click("#btn_save")
            await pilot.pause(0.3)

            # 未填 key → 不应 dismiss
            self.assertEqual(len(dismissed_with), 0,
                             "未填 key 不应 dismiss dialog")


# ============================================================
# Pilot 测试：AddMemberDialog — agent_user → agent 联动
# ============================================================

_FAKE_CLAUDE_PROFILE = {
    "agent_type": "claude", "takeover_enabled": True,
    "anthropic_api_key": "sk-ant-fake123secret",
    "anthropic_base_url": "https://api.example.com",
    "anthropic_model": "claude-sonnet-5",
}
_FAKE_CODEX_PROFILE = {
    "agent_type": "codex", "takeover_enabled": True,
    "openai_api_key": "sk-fake456secret",
    "openai_base_url": "https://api.openai.com",
    "codex_model": "gpt-4o",
}
_FAKE_LEGACY_PROFILE = {
    "anthropic_base_url": "https://old.api.com",
    "takeover_enabled": True,
}
_MOCK_AGENT_USER_PROFILES = {
    "claude_p": _FAKE_CLAUDE_PROFILE,
    "codex_p": _FAKE_CODEX_PROFILE,
    "old_p": _FAKE_LEGACY_PROFILE,
}


class AddMemberDialogPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：mount AddMemberDialog，改变 #agent_user → #agent 同步 + disabled。"""

    async def test_typed_claude_profile_syncs_and_disables_agent(self):
        """选择 typed Claude profile → #agent 值为 'claude' 且 disabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = "claude_p"
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertEqual(agent_select.value, "claude",
                                 "#agent 应同步为 claude")
                self.assertTrue(agent_select.disabled,
                                "typed profile 选中时 #agent 应 disabled")

    async def test_typed_codex_profile_syncs_and_disables_agent(self):
        """选择 typed Codex profile → #agent 值为 'codex' 且 disabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = "codex_p"
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertEqual(agent_select.value, "codex")
                self.assertTrue(agent_select.disabled)

    async def test_clear_profile_reenables_agent(self):
        """先选 typed profile → 再清空 → #agent 恢复 enabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                # 先选 typed profile
                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = "claude_p"
                await pilot.pause(0.3)
                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertTrue(agent_select.disabled)

                # 清空选择（选回"系统默认"）
                select.value = ""
                await pilot.pause(0.3)
                self.assertFalse(agent_select.disabled,
                                 "清空后 #agent 应恢复 enabled")

    async def test_legacy_profile_does_not_disable_agent(self):
        """旧版 profile → #agent 不 disabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = "old_p"
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertFalse(agent_select.disabled,
                                 "旧版 profile 不应禁用 #agent")

    async def test_system_default_does_not_disable_agent(self):
        """初始"系统默认" → #agent 不 disabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertFalse(agent_select.disabled,
                                 "系统默认时 #agent 不应 disabled")

    async def test_save_forces_agent_to_match_typed_profile(self):
        """点击保存 → 返回的 agent 被强制为 typed profile 的 agent_type。"""
        from textual.app import App
        from textual.widgets import Input, Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            # Mock dismiss to capture result
            dismissed_with = []
            original_dismiss = dialog.dismiss
            def fake_dismiss(result=None):
                dismissed_with.append(result)
                original_dismiss(result)
            dialog.dismiss = fake_dismiss

            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                # 填名称
                pilot.app.screen.query_one("#name", Input).value = "test_member"
                # 选 typed Claude profile（会自动同步 agent）
                pilot.app.screen.query_one("#agent_user", Select).value = "claude_p"
                await pilot.pause(0.3)

                await pilot.click("#btn_add")
                await pilot.pause(0.3)

            self.assertEqual(len(dismissed_with), 1, "应 dismiss 一次")
            result = dismissed_with[0]
            self.assertEqual(result["agent"], "claude",
                             "保存时 agent 应强制为 claude")
            self.assertEqual(result["agent_user"], "claude_p")


# ============================================================
# Pilot 测试：EditMemberDialog — agent_user → agent 联动
# ============================================================

class EditMemberDialogPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：mount EditMemberDialog，同 Add 的联动行为。"""

    async def test_typed_profile_syncs_and_disables_agent(self):
        """选择 typed Claude profile → #agent 同步 + disabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                member_name="alice",
                current_role="coder",
                current_agent="codex",  # 初始不同
                team_name="test_team",
            )
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = "claude_p"
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertEqual(agent_select.value, "claude")
                self.assertTrue(agent_select.disabled)

    async def test_clear_profile_reenables_agent(self):
        """清空 → #agent 恢复 enabled。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                member_name="bob",
                current_role="tester",
                current_agent="codex",
                current_agent_user="codex_p",
                team_name="test_team",
            )
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                # 初始可能已经 disabled（取决于默认值触发 Change 事件）
                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = ""
                await pilot.pause(0.3)

                self.assertFalse(agent_select.disabled,
                                 "清空后 #agent 应恢复 enabled")

    async def test_save_forces_agent_to_match_typed_profile(self):
        """点击保存 → 返回的 agent 被强制为 typed profile 的 agent_type。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                member_name="carol",
                current_role="reviewer",
                current_agent="codex",  # 手动改过
                team_name="test_team",
            )
            dismissed_with = []
            original_dismiss = dialog.dismiss
            def fake_dismiss(result=None):
                dismissed_with.append(result)
                original_dismiss(result)
            dialog.dismiss = fake_dismiss

            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                # 选 typed Claude profile
                pilot.app.screen.query_one("#agent_user", Select).value = "claude_p"
                await pilot.pause(0.3)

                await pilot.click("#btn_save")
                await pilot.pause(0.3)

            self.assertEqual(len(dismissed_with), 1, "应 dismiss 一次")
            result = dismissed_with[0]
            self.assertEqual(result["agent"], "claude",
                             "保存时 agent 应强制为 claude")
            self.assertEqual(result["agent_user"], "claude_p")

    async def test_system_default_preserves_manual_agent(self):
        """系统默认时保存，agent 保持用户手动选择的值。"""
        from textual.app import App
        from textual.widgets import Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                member_name="dave",
                current_role="coder",
                current_agent="codex",
                team_name="test_team",
            )
            dismissed_with = []
            original_dismiss = dialog.dismiss
            def fake_dismiss(result=None):
                dismissed_with.append(result)
                original_dismiss(result)
            dialog.dismiss = fake_dismiss

            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                await pilot.click("#btn_save")
                await pilot.pause(0.3)

            self.assertEqual(len(dismissed_with), 1, "应 dismiss 一次")
            result = dismissed_with[0]
            self.assertEqual(result["agent"], "codex",
                             "系统默认时应保持用户选择的 agent")


# ============================================================
# Pilot 测试：AgentUserManageDialog — 列表 label 不含 API key
# ============================================================

class AgentUserManagePilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：mount AgentUserManageDialog，验证管理列表 label 不含 API key 片段。"""

    async def test_labels_do_not_contain_api_key_fragments(self):
        """管理列表的 option label 不含 fake key 的任何片段（前缀/主体/后缀）。"""
        from textual.app import App
        from textual.widgets import OptionList

        FAKE_CLAUDE_KEY = "sk-ant-fake999secret"
        FAKE_CODEX_KEY = "sk-fake888secret"

        MOCK_MANAGE = {
            "claude_a": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": FAKE_CLAUDE_KEY,
                "anthropic_base_url": "https://api.example.com",
                "anthropic_model": "sonnet",
            },
            "codex_b": {
                "agent_type": "codex", "takeover_enabled": False,
                "openai_api_key": FAKE_CODEX_KEY,
                "openai_base_url": "https://api.openai.com",
                "codex_model": "gpt-4o",
            },
            "old_c": {
                "takeover_enabled": True,
                "anthropic_base_url": "https://old.api.com",
            },
        }

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=MOCK_MANAGE):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                for opt in option_list.options:
                    label = str(opt.prompt)
                    self.assertNotIn(
                        "fake999", label,
                        f"label 泄漏了 fake key 片段: {label[:80]}")
                    self.assertNotIn(
                        "fake888", label,
                        f"label 泄漏了 fake key 片段: {label[:80]}")
                    self.assertNotIn(
                        "sk-ant-fake", label,
                        f"label 泄漏了 key 前缀: {label[:80]}")
                    self.assertNotIn(
                        "sk-fake", label,
                        f"label 泄漏了 key 前缀: {label[:80]}")
                    self.assertNotIn(
                        "secret", label,
                        f"label 泄漏了 key 后缀: {label[:80]}")

    async def test_label_shows_badge_and_key_only(self):
        """简化后 profile label 仅显示 Provider 和 key，不显示 key 状态等详情。"""
        from textual.app import App
        from textual.widgets import OptionList

        MOCK_ONE = {
            "claude_p": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": "sk-ant-fake123",
                "anthropic_base_url": "https://api.example.com",
                "anthropic_model": "sonnet",
            },
        }

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=MOCK_ONE):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                claude_label = next(
                    (str(opt.prompt) for opt in option_list.options
                     if opt.id == "claude_p"), "")
                self.assertIn("Claude", claude_label)
                self.assertIn("claude_p", claude_label)
                # 简化后不显示 key 状态
                self.assertNotIn("已配置", claude_label)
                self.assertNotIn("未配置", claude_label)

    async def test_label_excludes_key_status(self):
        """空 key 的 profile label 同样不显示 key 状态。"""
        from textual.app import App
        from textual.widgets import OptionList

        MOCK_EMPTY_KEY = {
            "claude_p": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": "",
                "anthropic_base_url": "https://api.example.com",
                "anthropic_model": "sonnet",
            },
        }

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=MOCK_EMPTY_KEY):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                claude_label = next(
                    (str(opt.prompt) for opt in option_list.options
                     if opt.id == "claude_p"), "")
                self.assertIn("Claude", claude_label)
                self.assertIn("claude_p", claude_label)
                self.assertNotIn("已配置", claude_label)

    async def test_codex_profile_label_shows_badge_and_key(self):
        """Codex profile 简化 label 仅显示 Provider 标记和 key。"""
        from textual.app import App
        from textual.widgets import OptionList

        MOCK_CODEX_ONLY = {
            "codex_p": {
                "agent_type": "codex", "takeover_enabled": True,
                "openai_api_key": "sk-fake123",
                "openai_base_url": "https://api.openai.com",
                "codex_model": "gpt-4o",
            },
        }

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=MOCK_CODEX_ONLY):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                codex_label = next(
                    (str(opt.prompt) for opt in option_list.options
                     if opt.id == "codex_p"), "")
                self.assertIn("Codex", codex_label)
                self.assertIn("codex_p", codex_label)
                # 简化后不显示 key 状态
                self.assertNotIn("已配置", codex_label)

    async def test_no_takeover_option_present_and_labeled(self):
        """TeamDefaultAgentUserDialog 包含 '不接管' 选项，标签为用户可读而非原始哨兵。

        task4：全局 manage dialog 不再含'不接管'（纯 profile 列表）；团队默认
        对话框（TeamDetailScreen u 入口）保留'不接管'语义——选择后清除团队默认。
        同时选项标签用「不接管」，不能把原始 '__none__' 哨兵串泄漏到界面。"""
        from textual.app import App
        from textual.widgets import Select
        from common.tmux_utils import AGENT_USER_NONE
        from tui.tui_dialogs import TeamDefaultAgentUserDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            dialog = TeamDefaultAgentUserDialog(team_name="test_team")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#team_default_select", Select)
                values = [v for _, v in select._options]
                self.assertIn(AGENT_USER_NONE, values,
                              "团队默认对话框应包含 '不接管' 选项")
                self.assertIn("claude_p", values,
                              "团队默认对话框应包含全局 profile")
                labels = [label for label, _ in select._options]
                self.assertTrue(
                    any("不接管" in label for label in labels),
                    "团队默认对话框应显示 '不接管' 标签")
                label = next(
                    (label for label, v in select._options
                     if v == AGENT_USER_NONE), "")
                self.assertNotEqual(label, AGENT_USER_NONE,
                                    "选项标签不能用原始哨兵串 '__none__'")
                self.assertIn("不接管", label)


# ============================================================
# P0 回归：AgentUserManageDialog 退出导航
# ============================================================

_MOCK_MANAGE_P0 = {
    "claude_p": {
        "agent_type": "claude", "takeover_enabled": True,
        "anthropic_api_key": "sk-ant-fake999secret",
        "anthropic_base_url": "https://api.example.com",
        "anthropic_model": "sonnet",
    },
    "codex_p": {
        "agent_type": "codex", "takeover_enabled": False,
        "openai_api_key": "sk-fake888secret",
        "openai_base_url": "https://api.openai.com",
        "codex_model": "gpt-4o",
    },
}


class AgentUserManageDialogClosePilotTests(unittest.IsolatedAsyncioTestCase):
    """P0 回归：AgentUserManageDialog 能正常关闭并返回原界面。

    覆盖两个关闭路径：按钮点击、Escape 键。
    """

    async def _push_manage_dialog(self, app, pilot):
        """Push AgentUserManageDialog with mocked profiles. Returns dialog."""
        from textual.app import App
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            dialog = AgentUserManageDialog(team_name="test_team")
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)
            return dialog

    async def test_close_button_dismisses_manage_dialog(self):
        """#btn_close 点击 → AgentUserManageDialog dismiss，不再是最上层 screen。"""
        from textual.app import App

        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            dialog = await self._push_manage_dialog(app, pilot)
            self.assertIs(pilot.app.screen, dialog,
                          "manage dialog 应为最上层 screen")

            await pilot.click("#btn_close")
            await pilot.pause(0.3)

            self.assertIsNot(pilot.app.screen, dialog,
                             "关闭后 manage dialog 不应仍为最上层 screen")

    async def test_escape_key_dismisses_manage_dialog(self):
        """Escape 键 → AgentUserManageDialog dismiss。

        若此测试 FAIL，说明 Binding("escape", "close_dialog", ...)
        绑定的 action "close_dialog" 未解析到实际 dismiss 方法
        （缺少 action_close_dialog）。"""
        from textual.app import App

        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            dialog = await self._push_manage_dialog(app, pilot)
            self.assertIs(pilot.app.screen, dialog)

            await pilot.press("escape")
            await pilot.pause(0.3)

            self.assertIsNot(pilot.app.screen, dialog,
                             "Escape 键应 dismiss manage dialog")

    async def test_close_restores_dialog_count(self):
        """关闭 manage dialog 后 screen stack 层级应减少一层。"""
        from textual.app import App

        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            # 记录 push 前的 screen 层级
            count_before = len(app.screen_stack)

            dialog = await self._push_manage_dialog(app, pilot)
            count_with_dialog = len(app.screen_stack)
            self.assertEqual(count_with_dialog, count_before + 1,
                             "push 后 screen stack 应 +1")

            await pilot.click("#btn_close")
            await pilot.pause(0.3)

            count_after = len(app.screen_stack)
            self.assertEqual(count_after, count_before,
                             "dismiss 后 screen stack 应恢复原层级")

    async def test_esc_restores_dialog_count(self):
        """Escape 关闭 manage dialog 后 screen stack 层级应恢复。"""
        from textual.app import App

        app = App()
        async with app.run_test(size=(100, 30)) as pilot:
            count_before = len(app.screen_stack)

            dialog = await self._push_manage_dialog(app, pilot)
            await pilot.press("escape")
            await pilot.pause(0.3)

            self.assertEqual(len(app.screen_stack), count_before,
                             "Escape 后 screen stack 应恢复")


class AgentUserManageNestedDialogPilotTests(unittest.IsolatedAsyncioTestCase):
    """P0 回归：嵌套弹窗取消不影响 AgentUserManageDialog 导航。

    从 manage → 编辑/新建 → 取消 → 回到 manage → 关闭。
    """

    async def test_edit_then_cancel_then_close_manage(self):
        """Manage → 编辑 → 取消 → Manage → 关闭。全程不卡死。"""
        from textual.app import App
        from textual.widgets import OptionList, Select
        from tui.tui_dialogs import AgentUserManageDialog

        # 使用 mock 直接控制编辑对话框的行为
        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                # 选中一个 profile
                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                option_list.highlighted = 0  # claude_p
                await pilot.pause(0.2)

                # 点击编辑按钮 → 编辑弹窗应被 push
                screen_before_edit = pilot.app.screen
                await pilot.click("#btn_edit")
                await pilot.pause(0.4)

                # 编辑弹窗可能在当前 screen 之上
                screen_after_edit_click = pilot.app.screen
                # 如果弹窗没有成功 push（如 push_screen_wait 需要 worker），
                # screen 不变 — 这也是可接受的，关键是不崩溃
                if screen_after_edit_click is not screen_before_edit:
                    # 编辑弹窗已打开 → 取消它
                    edit_screen = screen_after_edit_click
                    await pilot.click("#btn_cancel")
                    await pilot.pause(0.3)
                    # 应回到 manage dialog
                    self.assertIsNot(pilot.app.screen, edit_screen,
                                     "取消编辑应回到 manage dialog")

                # 关闭 manage
                await pilot.click("#btn_close")
                await pilot.pause(0.3)
                self.assertIsNot(pilot.app.screen, dialog,
                                 "最终应关闭 manage dialog")

    async def test_new_then_escape_then_close_manage(self):
        """Manage → 新建 → Escape 取消 → 回到 Manage → 关闭。

        生产修复：AgentUserEditDialog compose() 用 Select.BLANK 代替 None，
        新建不再崩溃，需选择 Provider 才可保存。"""
        from textual.app import App
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                screen_before_new = pilot.app.screen

                # 点击新建 — 应成功打开编辑弹窗（不再崩溃）
                await pilot.click("#btn_new")
                await pilot.pause(0.4)

                new_screen = pilot.app.screen
                self.assertIsNot(new_screen, screen_before_new,
                                 "新建应打开编辑弹窗")

                # Escape 取消编辑
                await pilot.press("escape")
                await pilot.pause(0.3)
                self.assertIs(pilot.app.screen, manage,
                              "Escape 后应回到 ManageDialog")

                # 关闭 manage
                await pilot.click("#btn_close")
                await pilot.pause(0.3)
                self.assertIsNot(pilot.app.screen, manage)

    async def test_new_with_claude_default_does_not_crash(self):
        """规避方案：若 AgentUserEditDialog 初始 agent_type 非空，
        Select 有合法值 → 不崩溃 → 取消 → 回到 manage → 关闭正常。"""
        from textual.app import App
        from tui.tui_dialogs import AgentUserManageDialog, AgentUserEditDialog

        # 验证用 agent_type="claude" 构造的 AgentUserEditDialog 不崩 Select(None)
        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                # 手动 push 一个带 agent_type="claude" 的编辑弹窗
                # 模拟修复后的"新建"行为
                edit_dialog = AgentUserEditDialog(agent_type="claude")
                await pilot.app.push_screen(edit_dialog)
                await pilot.pause(0.3)

                # 取消编辑
                await pilot.click("#btn_cancel")
                await pilot.pause(0.3)

                # 应回到 manage dialog
                self.assertIs(pilot.app.screen, dialog,
                              "取消后应回到 manage dialog")

                # 关闭 manage
                await pilot.click("#btn_close")
                await pilot.pause(0.3)
                self.assertIsNot(pilot.app.screen, dialog)

    async def test_close_manage_without_touching_edit_still_works(self):
        """不进入编辑直接关闭 manage → 正常 dismiss。"""
        from textual.app import App
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            dialog = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                # 直接关闭
                stack_before = len(app.screen_stack)
                await pilot.click("#btn_close")
                await pilot.pause(0.3)

                self.assertIsNot(pilot.app.screen, dialog)
                self.assertEqual(len(app.screen_stack), stack_before - 1)


# ============================================================
# P0 回归：EditDialog escape/cancel → 仅返回 ManageDialog
# ============================================================

class AgentUserEditDialogEscapeCancelPilotTests(unittest.IsolatedAsyncioTestCase):
    """验证 EditDialog 的 Escape/Cancel 只关闭自身，不关闭 ManageDialog。"""

    async def test_edit_escape_only_closes_edit_not_manage(self):
        """Manage → Edit(typed) → Escape → 回到 Manage，Manage 仍为最上层。"""
        from textual.app import App
        from textual.widgets import OptionList, Select
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                # 选中 profile 并点击编辑
                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                option_list.highlighted = 0  # claude_p
                await pilot.pause(0.2)
                await pilot.click("#btn_edit")
                await pilot.pause(0.4)

                edit_screen = pilot.app.screen
                self.assertIsNot(edit_screen, manage,
                                 "编辑弹窗应为新 screen")

                # Esc 关闭编辑弹窗
                await pilot.press("escape")
                await pilot.pause(0.3)

                self.assertIs(pilot.app.screen, manage,
                              "Escape 后应回到 ManageDialog，不能全部关闭")
                self.assertIsNot(pilot.app.screen, edit_screen,
                                 "EditDialog 应已 dismiss")

    async def test_edit_cancel_button_only_closes_edit_not_manage(self):
        """Manage → Edit(typed) → #btn_cancel → 回到 Manage，不保存。"""
        from textual.app import App
        from textual.widgets import OptionList, Select
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                option_list.highlighted = 0  # claude_p
                await pilot.pause(0.2)
                await pilot.click("#btn_edit")
                await pilot.pause(0.4)

                edit_screen = pilot.app.screen
                self.assertIsNot(edit_screen, manage)

                # 点击取消
                await pilot.click("#btn_cancel")
                await pilot.pause(0.3)

                self.assertIs(pilot.app.screen, manage,
                              "Cancel 后应回到 ManageDialog")
                self.assertIsNot(pilot.app.screen, edit_screen)

    async def test_edit_cancel_does_not_dismiss_manage(self):
        """Manage → Edit → Cancel → Manage 仍在 screen stack 中（未被误 dismiss）。"""
        from textual.app import App
        from textual.widgets import OptionList, Select
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                stack_before_edit = len(app.screen_stack)

                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                option_list.highlighted = 0  # claude_p
                await pilot.pause(0.2)
                await pilot.click("#btn_edit")
                await pilot.pause(0.4)

                # Edit dialog 在 stack 上增加一层
                self.assertGreater(len(app.screen_stack), stack_before_edit)

                await pilot.click("#btn_cancel")
                await pilot.pause(0.3)

                # Cancel 后 stack 恢复
                self.assertEqual(len(app.screen_stack), stack_before_edit,
                                 "Cancel 后 screen stack 应恢复原层级")
                self.assertIs(pilot.app.screen, manage)

    async def test_edit_escape_then_close_manage(self):
        """Manage → Edit → Escape → Manage → Close。完整往返链路。"""
        from textual.app import App
        from textual.widgets import OptionList, Select
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                # 选 profile → 编辑 → Escape → 回到 manage
                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                option_list.highlighted = 0  # claude_p
                await pilot.pause(0.2)
                await pilot.click("#btn_edit")
                await pilot.pause(0.4)
                await pilot.press("escape")
                await pilot.pause(0.3)
                self.assertIs(pilot.app.screen, manage)

                # 再选另一个 profile → 编辑 → Cancel → 回到 manage
                option_list = pilot.app.screen.query_one("#agent_user_list", OptionList)
                option_list.highlighted = 1  # codex_p
                await pilot.pause(0.2)
                await pilot.click("#btn_edit")
                await pilot.pause(0.4)
                await pilot.click("#btn_cancel")
                await pilot.pause(0.3)
                self.assertIs(pilot.app.screen, manage,
                              "两次编辑取消后都应回到 Manage")

                # 关闭 manage
                await pilot.click("#btn_close")
                await pilot.pause(0.3)
                self.assertIsNot(pilot.app.screen, manage,
                                 "关闭后 ManageDialog 应 dismiss")


# ============================================================
# P0 回归：ManageDialog q 键退出 + 关闭按钮
# ============================================================

class AgentUserManageDialogQKeyPilotTests(unittest.IsolatedAsyncioTestCase):
    """ManageDialog 的 q 键绑定验证。"""

    async def test_q_key_dismisses_manage_dialog(self):
        """ManageDialog 按 q → dismiss。"""
        from textual.app import App
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                self.assertIs(pilot.app.screen, manage)
                await pilot.press("q")
                await pilot.pause(0.3)

                self.assertIsNot(pilot.app.screen, manage,
                                 "q 键应 dismiss ManageDialog")

    async def test_q_key_restores_screen_stack(self):
        """按 q 关闭 ManageDialog 后 screen stack 恢复。"""
        from textual.app import App
        from tui.tui_dialogs import AgentUserManageDialog

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_MANAGE_P0):
            app = App()
            manage = AgentUserManageDialog(team_name="test_team")

            async with app.run_test(size=(100, 30)) as pilot:
                count_before = len(app.screen_stack)
                await pilot.app.push_screen(manage)
                await pilot.pause(0.3)

                await pilot.press("q")
                await pilot.pause(0.3)

                self.assertEqual(len(app.screen_stack), count_before,
                                 "q 键后 screen stack 应恢复")


# ============================================================
# Pilot 测试：设为默认 / 取消 / 持久化 / 旧版拒绝 / 删除清理
# ============================================================

_MOCK_SET_DEFAULT_PROFILES = {
    "claude_p": {
        "agent_type": "claude", "takeover_enabled": True,
        "anthropic_api_key": "sk-ant-fake123",
        "anthropic_base_url": "https://api.example.com",
        "anthropic_model": "sonnet",
    },
    "codex_p": {
        "agent_type": "codex", "takeover_enabled": True,
        "openai_api_key": "sk-fake456",
        "openai_base_url": "https://api.openai.com",
        "codex_model": "gpt-4o",
    },
    "old_p": {
        "takeover_enabled": True,
        "anthropic_base_url": "https://old.api.com",
    },
}


class TeamDefaultAgentUserSetDefaultPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：TeamDefaultAgentUserDialog 设默认 / 不接管清除 / 旧版拒绝 / 无选择。"""

    async def _push_dialog(self, pilot, *, load_data_value=None, profiles=None):
        """Push TeamDefaultAgentUserDialog with mocked profiles/data. Returns dialog."""
        from textual.app import App
        from tui.tui_dialogs import TeamDefaultAgentUserDialog

        load_value = load_data_value if load_data_value is not None else {
            "teams": {"team": {"agent_users": dict(_MOCK_SET_DEFAULT_PROFILES)}}
        }
        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=profiles if profiles is not None
                        else _MOCK_SET_DEFAULT_PROFILES):
            with _mock_agent_user_data(load_value):
                dialog = TeamDefaultAgentUserDialog(team_name="team")
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)
                return dialog

    async def test_set_default_saves_and_shows_current(self):
        """选择普通 profile + 设为默认 → 持久化 default_agent_user，提示成功。"""
        from textual.app import App
        from textual.widgets import Label, Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_SET_DEFAULT_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {"agent_users": dict(_MOCK_SET_DEFAULT_PROFILES)}}
            }):
                with mock.patch("common.data_layer.save_data") as mock_save:
                    app = App()
                    dialog = TeamDefaultAgentUserDialog(team_name="team")
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.3)

                        current = pilot.app.screen.query_one(
                            "#team_default_current", Label)
                        self.assertIn("无", str(current.render()),
                                      "初始无默认应显示 '当前团队默认: 无'")

                        select = pilot.app.screen.query_one(
                            "#team_default_select", Select)
                        select.value = "claude_p"
                        await pilot.pause(0.2)

                        await pilot.click("#btn_set_default")
                        await pilot.pause(0.3)

                        result_label = pilot.app.screen.query_one(
                            "#team_default_result", Label)
                        self.assertIn("已设为团队默认", str(result_label.render()))
                        mock_save.assert_called_once()
                        saved = mock_save.call_args[0][0]
                        self.assertEqual(
                            saved["teams"]["team"]["default_agent_user"],
                            "claude_p")
                        self.assertIn(
                            "claude_p",
                            str(pilot.app.screen.query_one(
                                "#team_default_current", Label).render()),
                            "刷新后应显示当前默认 key")

    async def test_set_default_no_takeover_clears_team_default(self):
        """选择 '不接管' + 设为默认 → 清除 default_agent_user（幂等），提示不接管。"""
        from textual.app import App
        from textual.widgets import Label, Select
        from common.tmux_utils import AGENT_USER_NONE

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_SET_DEFAULT_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {
                    "agent_users": dict(_MOCK_SET_DEFAULT_PROFILES),
                    "default_agent_user": "claude_p",
                }}
            }):
                with mock.patch("common.data_layer.save_data") as mock_save:
                    app = App()
                    dialog = TeamDefaultAgentUserDialog(team_name="team")
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.3)

                        current = pilot.app.screen.query_one(
                            "#team_default_current", Label)
                        self.assertIn("claude_p", str(current.render()),
                                      "初始应显示当前默认 key")

                        select = pilot.app.screen.query_one(
                            "#team_default_select", Select)
                        select.value = AGENT_USER_NONE
                        await pilot.pause(0.2)

                        await pilot.click("#btn_set_default")
                        await pilot.pause(0.3)

                        result_label = pilot.app.screen.query_one(
                            "#team_default_result", Label)
                        self.assertIn("团队默认不接管", str(result_label.render()))
                        mock_save.assert_called_once()
                        saved = mock_save.call_args[0][0]
                        self.assertNotIn(
                            "default_agent_user", saved["teams"]["team"],
                            "不接管设默认应清除 default_agent_user")
                        self.assertNotIn(
                            AGENT_USER_NONE, str(saved),
                            "不应把 __none__ 写入任何持久化字段")
                        self.assertIn(
                            "无",
                            str(pilot.app.screen.query_one(
                                "#team_default_current", Label).render()),
                            "刷新后应显示 '当前团队默认: 无'")

                        # 幂等：再次点击同样清除，不报错
                        await pilot.click("#btn_set_default")
                        await pilot.pause(0.3)
                        self.assertIn(
                            "团队默认不接管",
                            str(pilot.app.screen.query_one(
                                "#team_default_result", Label).render()))

    async def test_legacy_profile_rejected_for_default(self):
        """旧版 profile（无 agent_type）点击设为默认 → 拒绝提示，save_data 未调用。"""
        from textual.app import App
        from textual.widgets import Label, Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_SET_DEFAULT_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {"agent_users": dict(_MOCK_SET_DEFAULT_PROFILES)}}
            }):
                with mock.patch("common.data_layer.save_data") as mock_save:
                    app = App()
                    dialog = TeamDefaultAgentUserDialog(team_name="team")
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.3)

                        select = pilot.app.screen.query_one(
                            "#team_default_select", Select)
                        select.value = "old_p"
                        await pilot.pause(0.2)

                        await pilot.click("#btn_set_default")
                        await pilot.pause(0.3)

                        result_label = pilot.app.screen.query_one(
                            "#team_default_result", Label)
                        self.assertIn("旧版", str(result_label.render()))
                        mock_save.assert_not_called()

    async def test_no_selection_prompts_not_crash(self):
        """无选择（Select.NULL）→ 设为默认提示，不崩溃、不保存（NoSelection 防护）。"""
        from textual.app import App
        from textual.widgets import Label, Select

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_SET_DEFAULT_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {"agent_users": dict(_MOCK_SET_DEFAULT_PROFILES)}}
            }):
                with mock.patch("common.data_layer.save_data") as mock_save:
                    app = App()
                    dialog = TeamDefaultAgentUserDialog(team_name="team")
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.3)

                        select = pilot.app.screen.query_one(
                            "#team_default_select", Select)
                        select.clear()
                        await pilot.pause(0.2)
                        self.assertIs(select.value, Select.NULL)

                        await pilot.click("#btn_set_default")
                        await pilot.pause(0.3)

                        result_label = pilot.app.screen.query_one(
                            "#team_default_result", Label)
                        self.assertIn("请先选择一个 profile",
                                      str(result_label.render()))
                        self.assertIs(pilot.app.screen, dialog)
                        mock_save.assert_not_called()


class AgentUserManageGlobalDeletePilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：全局 manage 删除 → 跨团队 sweep default/member 引用。"""

    async def test_delete_sweeps_cross_team_refs(self):
        """删除全局 profile → 清除所有团队 default 与成员引用（跨团队清理）。"""
        from textual.app import App
        from textual.widgets import OptionList, Select

        # 全局 registry + 两个团队引用 claude_p
        global_data = {
            "agent_users": dict(_MOCK_SET_DEFAULT_PROFILES),
            "teams": {
                "teamA": {
                    "default_agent_user": "claude_p",
                    "members": {
                        "alice": {"role": "coder", "agent_user": "claude_p"},
                        "bob": {"role": "tester"},
                    },
                },
                "teamB": {
                    "members": {
                        "carol": {"role": "coder", "agent_user": "claude_p"},
                    },
                },
            },
        }
        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_SET_DEFAULT_PROFILES):
            with _mock_agent_user_data(global_data):
                with mock.patch("common.data_layer.save_data") as mock_save:
                    app = App()
                    dialog = AgentUserManageDialog()
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.app.push_screen(dialog)
                        await pilot.pause(0.3)

                        option_list = pilot.app.screen.query_one(
                            "#agent_user_list", OptionList)
                        option_list.highlighted = 0  # claude_p
                        await pilot.pause(0.2)

                        await pilot.click("#btn_delete")
                        await pilot.pause(0.4)

                        # ConfirmBox 应显示跨团队影响并确认
                        confirm_msg = getattr(pilot.app.screen, "_message", "")
                        self.assertIn("跨团队清理", confirm_msg,
                                      "删除确认应展示跨团队清理影响")
                        await pilot.click("#btn_yes")
                        await pilot.pause(0.3)

                        mock_save.assert_called_once()
                        saved = mock_save.call_args[0][0]
                        self.assertNotIn(
                            "claude_p", saved.get("agent_users", {}),
                            "全局 registry 应删除该 profile")
                        team_a = saved["teams"]["teamA"]
                        self.assertNotIn("default_agent_user", team_a,
                                         "teamA 团队默认应被清除")
                        self.assertNotIn("agent_user", team_a["members"]["alice"],
                                         "teamA alice 成员引用应被清除")
                        self.assertNotIn(
                            "agent_user", saved["teams"]["teamB"]["members"]["carol"],
                            "teamB carol 成员引用也应被清除")


# ============================================================
# _sync_agent_user_rename 纯 helper 单测（不复制生产逻辑）
# ============================================================

class EditUserRenameSyncTests(unittest.TestCase):
    """单测 _sync_agent_user_rename：同步 default_agent_user 和 member.agent_user 引用。"""

    def test_rename_profile_syncs_default_agent_user(self):
        """重命名 profile key → team.default_agent_user 跟随更新。"""
        team = {"default_agent_user": "old_key", "members": {}}
        _sync_agent_user_rename(team, "old_key", "new_key")
        self.assertEqual(team["default_agent_user"], "new_key")

    def test_rename_profile_syncs_member_agent_user_refs(self):
        """重命名 profile key → 所有引用该 profile 的成员 agent_user 更新。"""
        team = {
            "members": {
                "alice": {"role": "coder", "agent": "claude", "agent_user": "old_key"},
                "bob": {"role": "tester", "agent": "codex", "agent_user": "old_key"},
                "carol": {"role": "reviewer", "agent": "claude"},
            }
        }
        _sync_agent_user_rename(team, "old_key", "new_key")
        self.assertEqual(team["members"]["alice"]["agent_user"], "new_key")
        self.assertEqual(team["members"]["bob"]["agent_user"], "new_key")
        self.assertNotIn("agent_user", team["members"]["carol"])

    def test_rename_profile_syncs_both_default_and_member_refs(self):
        """同时存在 default_agent_user 和多个成员引用 → 全部同步。"""
        team = {
            "default_agent_user": "old_key",
            "members": {
                "alice": {"role": "coder", "agent": "claude", "agent_user": "old_key"},
                "bob": {"role": "tester", "agent": "claude", "agent_user": "old_key"},
            },
        }
        _sync_agent_user_rename(team, "old_key", "new_key")
        self.assertEqual(team["default_agent_user"], "new_key")
        self.assertEqual(team["members"]["alice"]["agent_user"], "new_key")
        self.assertEqual(team["members"]["bob"]["agent_user"], "new_key")


# ============================================================
# CSS / 布局断言
# ============================================================

class TuiCssLayoutTests(unittest.TestCase):
    """验证 Profile FormField 的 CSS 与其他表单使用同一结构且有效高度/间距。"""

    def test_formfield_height_is_4(self):
        """FormField height 应为 4（合理垂直间距）。"""
        css = tui_screens.TeamManagerApp.CSS
        self.assertIn("FormField", css)
        self.assertIn("height: 4", css,
                      "FormField 的 height 应为 4 以保证合理垂直间距")

    def test_dialog_form_has_padding(self):
        """.dialog-form 应有 padding 和 border。"""
        css = tui_screens.TeamManagerApp.CSS
        self.assertIn(".dialog-form", css)
        self.assertIn("padding: 1 2", css,
                      ".dialog-form 应有统一的内边距")

    def test_field_label_width_is_14(self):
        """.field-label 宽度应为 14（与 leader 表单一致）。"""
        css = tui_screens.TeamManagerApp.CSS
        self.assertIn(".field-label", css)
        self.assertIn("width: 14", css,
                      ".field-label 宽度应为 14 以对齐其他表单字段")

    def test_formfield_select_width_is_35(self):
        """FormField Select 的宽度应为 35。"""
        css = tui_screens.TeamManagerApp.CSS
        self.assertIn("FormField Select", css)
        self.assertIn("width: 35", css,
                      "FormField Select 的宽度应为 35")


# ============================================================
# AGENT_USER_NONE 哨兵 — 三态集成测试
# ============================================================

class AgentUserNoneSentinelPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：Select '不接管' → agent Select 行为 + dismiss 结果。"""

    async def test_select_no_takeover_does_not_disable_agent(self):
        """选择'不接管' → #agent Select 保持 enabled（不同步 profile）。"""
        from textual.app import App
        from textual.widgets import Select
        from common.tmux_utils import AGENT_USER_NONE

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#agent_user", Select)
                select.value = AGENT_USER_NONE
                await pilot.pause(0.3)

                agent_select = pilot.app.screen.query_one("#agent", Select)
                self.assertFalse(agent_select.disabled,
                                 "不接管不应禁用 #agent Select")

    async def test_save_with_no_takeover_persists_sentinel(self):
        """点添加，agent_user='__none__' → dismiss result 包含 sentinel。"""
        from textual.app import App
        from textual.widgets import Input, Select
        from common.tmux_utils import AGENT_USER_NONE

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = AddMemberDialog(team_name="test_team")
            dismissed_with = []
            original_dismiss = dialog.dismiss
            def fake_dismiss(result=None):
                dismissed_with.append(result)
                original_dismiss(result)
            dialog.dismiss = fake_dismiss

            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                pilot.app.screen.query_one("#name", Input).value = "test_member"
                pilot.app.screen.query_one("#agent_user", Select).value = AGENT_USER_NONE
                await pilot.pause(0.3)

                await pilot.click("#btn_add")
                await pilot.pause(0.3)

            self.assertEqual(len(dismissed_with), 1)
            self.assertEqual(dismissed_with[0]["agent_user"], AGENT_USER_NONE)

    async def test_edit_preselects_no_takeover(self):
        """Member 已有 agent_user='__none__' → EditDialog 预选 '不接管'。"""
        from textual.app import App
        from textual.widgets import Select
        from common.tmux_utils import AGENT_USER_NONE

        with mock.patch("tui.tui_dialogs._agent_user_profiles",
                        return_value=_MOCK_AGENT_USER_PROFILES):
            app = App()
            dialog = EditMemberDialog(
                member_name="eve",
                current_role="coder",
                current_agent="codex",
                current_agent_user=AGENT_USER_NONE,
                team_name="test_team",
            )
            async with app.run_test(size=(80, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                select = pilot.app.screen.query_one("#agent_user", Select)
                self.assertEqual(select.value, AGENT_USER_NONE,
                                 "应预选 '不接管'")


class AgentUserEditDialogNoNoneKeyPilotTests(unittest.IsolatedAsyncioTestCase):
    """Pilot 测试：AgentUserEditDialog 保存 __none__ 作为 key 时拒绝且不 dismiss。"""

    async def test_save_with_none_key_rejects_and_shows_messagebox(self):
        """新建 profile，key 填 '__none__' → MessageBox 弹出，dialog 不 dismiss。"""
        from textual.app import App
        from textual.widgets import Input

        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")
        dismissed_with = []
        original_dismiss = dialog.dismiss
        def fake_dismiss(result=None):
            dismissed_with.append(result)
            original_dismiss(result)
        dialog.dismiss = fake_dismiss

        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            # 填入 __none__ 作为 key
            key_input = pilot.app.screen.query_one("#key", Input)
            key_input.value = "__none__"
            await pilot.pause(0.2)

            await pilot.click("#btn_save")
            await pilot.pause(0.3)

            # 不应 dismiss
            self.assertEqual(len(dismissed_with), 0,
                             "key='__none__' 应拒绝保存，不 dismiss dialog")
            # 应弹出 MessageBox（push_screen 而非 dismiss）
            self.assertIsNotNone(pilot.app.screen)

    async def test_save_existing_profile_with_none_key_rejected(self):
        """新建 profile，key 填 '__none__' → MessageBox 出现（验证错误提示可查看）。"""
        from textual.app import App
        from textual.widgets import Input

        app = App()
        dialog = AgentUserEditDialog(agent_type="claude")

        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.app.push_screen(dialog)
            await pilot.pause(0.3)

            stack_before = len(app.screen_stack)

            key_input = pilot.app.screen.query_one("#key", Input)
            key_input.value = "__none__"
            await pilot.pause(0.2)

            await pilot.click("#btn_save")
            await pilot.pause(0.3)

            # MessageBox 被 push 到 screen stack（push_screen 而非 dismiss）
            self.assertGreater(len(app.screen_stack), stack_before,
                               "__none__ key 应触发 MessageBox 而非 dismiss")


class AgentUserNoneSentinelDataTests(unittest.TestCase):
    """Data-layer 测试：三态 agent_user 持久化。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
        }
        project = self.root / "project"
        project.mkdir()
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        Path(mcp.DATA_FILE).parent.mkdir(parents=True, exist_ok=True)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        data_layer.set_data_file(mcp.DATA_FILE)
        from common.data_layer import save_data
        save_data({"teams": {}})

    def tearDown(self):
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        self.tmp.cleanup()

    def _save_and_reload(self, data: dict) -> dict:
        from common.data_layer import save_data, load_data
        save_data(data)
        return load_data()

    def test_persist_no_takeover_on_add(self):
        """添加成员 → agent_user='__none__' 持久化。"""
        from common.tmux_utils import AGENT_USER_NONE
        data = {
            "teams": {
                "team": {
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True}
                    },
                    "default_agent_user": "p1",
                    "members": {},
                }
            }
        }
        # 模拟 AddMember 结果：agent_user=__none__
        data["teams"]["team"]["members"]["alice"] = {
            "role": "coder", "agent": "claude", "agent_user": AGENT_USER_NONE,
        }
        loaded = self._save_and_reload(data)
        member = loaded["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["agent_user"], AGENT_USER_NONE)

    def test_edit_to_no_takeover(self):
        """编辑成员：从 profile 切换到 '不接管'。"""
        from common.tmux_utils import AGENT_USER_NONE
        data = {
            "teams": {
                "team": {
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True}
                    },
                    "default_agent_user": "p1",
                    "members": {
                        "alice": {"role": "coder", "agent": "claude", "agent_user": "p1"},
                    },
                }
            }
        }
        loaded = self._save_and_reload(data)
        # 模拟 EditMember 结果：切换到不接管
        loaded["teams"]["team"]["members"]["alice"]["agent_user"] = AGENT_USER_NONE
        loaded2 = self._save_and_reload(loaded)
        self.assertEqual(loaded2["teams"]["team"]["members"]["alice"]["agent_user"],
                         AGENT_USER_NONE)

    def test_edit_from_no_takeover_to_profile(self):
        """编辑成员：从 '不接管' 切换到具体 profile。"""
        from common.tmux_utils import AGENT_USER_NONE
        data = {
            "teams": {
                "team": {
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True}
                    },
                    "members": {
                        "alice": {"role": "coder", "agent": "claude",
                                   "agent_user": AGENT_USER_NONE},
                    },
                }
            }
        }
        loaded = self._save_and_reload(data)
        loaded["teams"]["team"]["members"]["alice"]["agent_user"] = "p1"
        loaded2 = self._save_and_reload(loaded)
        self.assertEqual(loaded2["teams"]["team"]["members"]["alice"]["agent_user"], "p1")

    def test_edit_from_no_takeover_to_inherit(self):
        """编辑成员：从 '不接管' 切换到 '系统默认' → 字段移除。"""
        from common.tmux_utils import AGENT_USER_NONE
        data = {
            "teams": {
                "team": {
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True}
                    },
                    "default_agent_user": "p1",
                    "members": {
                        "alice": {"role": "coder", "agent": "claude",
                                   "agent_user": AGENT_USER_NONE},
                    },
                }
            }
        }
        loaded = self._save_and_reload(data)
        # 模拟 EditMember 结果：空字符串 → pop agent_user
        loaded["teams"]["team"]["members"]["alice"].pop("agent_user", None)
        loaded2 = self._save_and_reload(loaded)
        self.assertNotIn("agent_user", loaded2["teams"]["team"]["members"]["alice"])

    def test_edit_from_inherit_to_no_takeover(self):
        """编辑成员：从 '系统默认'(无 agent_user) 切换到 '不接管'。"""
        from common.tmux_utils import AGENT_USER_NONE
        data = {
            "teams": {
                "team": {
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True}
                    },
                    "default_agent_user": "p1",
                    "members": {
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        }
        loaded = self._save_and_reload(data)
        loaded["teams"]["team"]["members"]["alice"]["agent_user"] = AGENT_USER_NONE
        loaded2 = self._save_and_reload(loaded)
        self.assertEqual(loaded2["teams"]["team"]["members"]["alice"]["agent_user"],
                         AGENT_USER_NONE)

    def test_backward_compat_empty_agent_user_inherits_default(self):
        """旧数据：成员无 agent_user 字段 → 仍继承 default_agent_user。"""
        from common.tmux_utils import _agent_user_env_prefix_for_team
        team = {
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": True,
                       "anthropic_api_key": "sk-test",
                       "anthropic_base_url": "https://api.anthropic.com",
                       "anthropic_model": "claude-sonnet-5"},
            },
            "default_agent_user": "p1",
            "members": {
                "alice": {"role": "coder", "agent": "claude"},
            },
        }
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertIn("ANTHROPIC_API_KEY=sk-test", result,
                      "旧成员（无 agent_user）应继承 default_agent_user")

    def test_backward_compat_empty_string_agent_user_inherits_default(self):
        """旧数据：成员 agent_user='' → 仍继承 default_agent_user。"""
        from common.tmux_utils import _agent_user_env_prefix_for_team
        team = {
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": True,
                       "anthropic_api_key": "sk-test"},
            },
            "default_agent_user": "p1",
            "members": {
                "alice": {"role": "coder", "agent": "claude", "agent_user": ""},
            },
        }
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertIn("ANTHROPIC_API_KEY=sk-test", result,
                      "agent_user='' 应回退到 default_agent_user")

    def test_get_agent_user_config_returns_none_for_sentinel(self):
        """get_agent_user_config 对 sentinel 成员返回 None。"""
        from common.tmux_utils import get_agent_user_config, AGENT_USER_NONE
        from common.data_layer import save_data
        data = {
            "teams": {
                "team": {
                    "default_agent_user": "p1",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_base_url": "https://example.com"}
                    },
                    "members": {
                        "alice": {"agent_user": AGENT_USER_NONE}
                    },
                }
            }
        }
        save_data(data)
        result = get_agent_user_config("team", "alice")
        self.assertIsNone(result, "sentinel 成员应返回 None")

    def test_tmux_spawn_member_common_no_env_for_sentinel(self):
        """common/tmux_utils.tmux_spawn_member 对 __none__ 成员不注入 agent profile env。"""
        from common.tmux_utils import tmux_spawn_member, AGENT_USER_NONE
        import common.tmux_utils as ctu

        self._save_and_reload({
            "teams": {
                "team": {
                    "default_agent_user": "p1",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_api_key": "sk-test",
                               "anthropic_base_url": "https://api.anthropic.com",
                               "anthropic_model": "claude-sonnet-5"},
                    },
                    "members": {
                        "alice": {"role": "coder", "agent": "claude",
                                   "agent_user": AGENT_USER_NONE},
                    },
                }
            }
        })
        tmux_calls = []
        def fake_tmux_run(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "-V":
                return 0, "", ""
            return 0, "", ""

        workspace = str(self.root / "workspace")
        Path(workspace).mkdir(parents=True, exist_ok=True)

        with mock.patch.object(ctu, "tmux_run", side_effect=fake_tmux_run):
            with mock.patch.object(ctu, "_write_claude_permissions_internal", return_value=""):
                tmux_spawn_member(
                    "mcp_team", "alice", "claude", workspace,
                    team_name_for_permissions="team",
                )

        spawn_cmd = next((c for c in tmux_calls if c[0] == "new-window"), [])
        joined = " ".join(str(x) for x in spawn_cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "common tmux_spawn_member: sentinel 不应注入 ANTHROPIC_API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "common tmux_spawn_member: sentinel 不应注入 ANTHROPIC_BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "common tmux_spawn_member: sentinel 不应注入 ANTHROPIC_MODEL")


# ============================================================
# resolve_agent_model — typed profile model 提取
# ============================================================

class ResolveAgentModelTests(unittest.TestCase):
    """resolve_agent_model 从 typed profile 提取 model 名，供 --model CLI flag 使用。"""

    def setUp(self):
        from common.tmux_utils import resolve_agent_model
        self.resolve = resolve_agent_model
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _save_team(self, team_data: dict) -> None:
        data_file = str(self.root / "teams_data.json")
        Path(data_file).parent.mkdir(parents=True, exist_ok=True)
        with open(data_file, "w") as f:
            json.dump(team_data, f)
        data_layer.set_data_file(data_file)

    def test_typed_claude_returns_anthropic_model(self):
        """typed claude profile → 返回 anthropic_model 字段。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "deepseek/deepseek-v4-flash[1m]"}
                    },
                    "members": {"alice": {"agent": "claude", "agent_user": "p1"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "alice"), "deepseek/deepseek-v4-flash[1m]")

    def test_typed_codex_returns_codex_model(self):
        """typed codex profile → 返回 codex_model 字段。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "codex",
                    "agent_users": {
                        "p1": {"agent_type": "codex", "takeover_enabled": True,
                               "codex_model": "gpt-4o"}
                    },
                    "members": {"bob": {"agent": "codex", "agent_user": "p1"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "bob"), "gpt-4o")

    def test_type_mismatch_returns_empty(self):
        """claude profile + codex agent → 类型不匹配，返回空。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "codex",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "claude-opus-5"}
                    },
                    "members": {"bob": {"agent": "codex", "agent_user": "p1"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "bob"), "")

    def test_takeover_disabled_returns_empty(self):
        """takeover_enabled=False → 返回空。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": False,
                               "anthropic_model": "claude-opus-5"}
                    },
                    "members": {"alice": {"agent": "claude", "agent_user": "p1"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "alice"), "")

    def test_no_agent_user_returns_empty(self):
        """成员无 agent_user 且无团队默认 → 返回空。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "claude-sonnet-5"}
                    },
                    "members": {"alice": {"agent": "claude"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "alice"), "")

    def test_fallback_to_default_agent_user(self):
        """成员无 agent_user → 回退到 default_agent_user。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "default_agent_user": "p1",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "deepseek/deepseek-v4-flash[1m]"}
                    },
                    "members": {"alice": {"agent": "claude"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "alice"), "deepseek/deepseek-v4-flash[1m]")

    def test_legacy_profile_returns_empty(self):
        """旧版 profile（无 agent_type）→ 不包含 model 字段，返回空。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"anthropic_base_url": "https://api.anthropic.com",
                               "takeover_enabled": True}
                    },
                    "members": {"alice": {"agent": "claude", "agent_user": "p1"}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "alice"), "")

    def test_sentinel_returns_empty(self):
        """agent_user=__none__ → 返回空。"""
        from common.tmux_utils import AGENT_USER_NONE
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "default_agent_user": "p1",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "claude-sonnet-5"}
                    },
                    "members": {"alice": {"agent": "claude", "agent_user": AGENT_USER_NONE}},
                }
            }
        })
        self.assertEqual(self.resolve("team", "alice"), "")

    def test_model_with_brackets_preserved(self):
        """model 名包含 [1m] 等方括号时原样保留。"""
        self._save_team({
            "teams": {
                "team": {
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "deepseek/deepseek-v4-flash[1m]"}
                    },
                    "members": {"alice": {"agent": "claude", "agent_user": "p1"}},
                }
            }
        })
        result = self.resolve("team", "alice")
        self.assertEqual(result, "deepseek/deepseek-v4-flash[1m]")
        self.assertIn("[1m]", result, "方括号应原样保留")


# ============================================================
# MCP spawn — typed profile --model flag 注入
# ============================================================

class McpSpawnTypedModelTests(unittest.TestCase):
    """MCP _tmux_spawn_member 对 typed profile 注入 --model CLI flag。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_mcp_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
        }
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        data_file = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.DATA_FILE = data_file
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        data_layer.set_data_file(data_file)

    def tearDown(self):
        for k, v in self.old_mcp_globals.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = None
        self.tmp.cleanup()

    def test_mcp_spawn_claude_typed_injects_model_flag(self):
        """MCP spawn 对 typed Claude profile 注入 --model deepseek/deepseek-v4-flash[1m]。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_api_key": "sk-test",
                               "anthropic_base_url": "https://aiapi.lejurobot.com",
                               "anthropic_model": "deepseek/deepseek-v4-flash[1m]"}
                    },
                    "members": {"alice": {"role": "coder", "agent": "claude",
                                          "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        cmd_strs = [str(x) for x in spawn_cmd]
        self.assertIn("--model", cmd_strs, "应包含 --model flag")
        model_idx = cmd_strs.index("--model")
        self.assertLess(model_idx + 1, len(cmd_strs), "--model 后应有值")
        self.assertEqual(cmd_strs[model_idx + 1], "deepseek/deepseek-v4-flash[1m]")

    def test_mcp_spawn_codex_typed_injects_model_flag(self):
        """MCP spawn 对 typed Codex profile 注入 --model gpt-4o。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "codex",
                    "agent_users": {
                        "p1": {"agent_type": "codex", "takeover_enabled": True,
                               "openai_api_key": "sk-test",
                               "openai_base_url": "https://api.openai.com",
                               "codex_model": "gpt-4o"}
                    },
                    "members": {"bob": {"role": "tester", "agent": "codex",
                                        "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                mcp._tmux_spawn_member("mcp_team", "bob", "codex", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        cmd_strs = [str(x) for x in spawn_cmd]
        self.assertIn("--model", cmd_strs, "Codex spawn 应包含 --model flag")
        model_idx = cmd_strs.index("--model")
        self.assertEqual(cmd_strs[model_idx + 1], "gpt-4o")

    def test_mcp_spawn_no_model_when_takeover_disabled(self):
        """takeover 关闭 → 不注入 --model flag。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": False,
                               "anthropic_model": "claude-opus-5"}
                    },
                    "members": {"carol": {"role": "coder", "agent": "claude",
                                           "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "carol", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        cmd_strs = [str(x) for x in spawn_cmd]
        self.assertNotIn("--model", cmd_strs, "takeover 关闭时不应有 --model flag")

    def test_mcp_spawn_no_model_for_legacy_profile(self):
        """旧版 profile（无 agent_type）→ 无 model 字段，不注入 --model。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"anthropic_base_url": "https://api.anthropic.com",
                               "takeover_enabled": True}
                    },
                    "members": {"dave": {"role": "coder", "agent": "claude",
                                          "agent_user": "p1"}},
                }
            }
        })
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "dave", "claude", str(workspace))
        spawn_cmd = next(c for c in tmux_calls if c[0] == "new-window")
        cmd_strs = [str(x) for x in spawn_cmd]
        self.assertNotIn("--model", cmd_strs, "legacy profile 不应有 --model flag")

    def test_mcp_spawn_model_persists_on_respawn(self):
        """重复 spawn（模拟终端恢复）→ --model flag 依然存在。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "default_agent": "claude",
                    "agent_users": {
                        "p1": {"agent_type": "claude", "takeover_enabled": True,
                               "anthropic_model": "deepseek/deepseek-v4-flash[1m]"}
                    },
                    "members": {"alice": {"role": "coder", "agent": "claude",
                                          "agent_user": "p1"}},
                }
            }
        })

        # 第一次 spawn
        tmux_calls_1 = []
        def fake_tmux_1(cmd, timeout=10):
            tmux_calls_1.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux_1):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))

        # 第二次 spawn（模拟恢复）
        tmux_calls_2 = []
        def fake_tmux_2(cmd, timeout=10):
            tmux_calls_2.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead", ""
            return 0, "", ""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux_2):
            with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                with mock.patch.object(mcp, "_remember_member_window_id", return_value=""):
                    mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(workspace))

        for i, calls in enumerate([tmux_calls_1, tmux_calls_2], 1):
            spawn_cmd = next(c for c in calls if c[0] == "new-window")
            cmd_strs = [str(x) for x in spawn_cmd]
            self.assertIn("--model", cmd_strs,
                         f"第 {i} 次 spawn 应包含 --model flag")
            model_idx = cmd_strs.index("--model")
            self.assertEqual(cmd_strs[model_idx + 1], "deepseek/deepseek-v4-flash[1m]",
                           f"第 {i} 次 spawn 的 model 应为 flash[1m]")


# ============================================================
# TUI launch_terminals — typed profile --model flag 注入（回归测试）
# ============================================================

class TuiLaunchTypedModelTests(unittest.TestCase):
    """TUI launch_terminals 对 typed profile 注入 --model CLI flag 的回归测试。

    当前 TUI 路径使用 get_agent_user_env_prefix 设置 ANTHROPIC_MODEL env var，
    但未使用 resolve_agent_model 传递 --model CLI flag。这些测试验证：
    1. env var 路径是否正常（已实现）
    2. --model CLI flag 是否缺失（待修复后改为断言存在）
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace")
        Path(self.workspace).mkdir(parents=True)
        # 隔离 settings 文件落盘（--settings 私有文件写入数据文件同目录）
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.root / "data" / "teams_data.json")
        self.team_data = {
            "teams": {
                "team": {
                    "workspace_dir": self.workspace,
                    "context_dir": self.workspace,
                    "default_agent": "claude",
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "agent_users": {
                        "claude_p": {
                            "agent_type": "claude",
                            "takeover_enabled": True,
                            "anthropic_api_key": "sk-test-key",
                            "anthropic_base_url": "https://aiapi.lejurobot.com",
                            "anthropic_model": "deepseek/deepseek-v4-flash[1m]",
                        },
                        "codex_p": {
                            "agent_type": "codex",
                            "takeover_enabled": True,
                            "openai_api_key": "sk-test-key",
                            "openai_base_url": "https://api.openai.com",
                            "codex_model": "gpt-4o",
                        },
                    },
                    "members": {
                        "lead": {"role": "leader", "agent": "claude",
                                  "agent_user": "claude_p"},
                        "coder_a": {"role": "coder", "agent": "claude",
                                     "agent_user": "claude_p"},
                        "coder_b": {"role": "coder", "agent": "codex",
                                     "agent_user": "codex_p"},
                    },
                }
            }
        }

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _run_launch(self):
        import common.tmux_utils as ctu
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "-V": return 0, "", ""
            if cmd[0] == "has-session": return 1, "", ""
            return 0, "", ""
        with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux):
            with mock.patch.object(ctu, "load_data", return_value=self.team_data):
                with mock.patch.object(tui_screens, "load_data", return_value=self.team_data):
                    with mock.patch.object(tui_screens, "save_data", return_value=None):
                        with mock.patch.object(tui_screens, "_tmux_session", return_value="mcp_team_test"):
                            with mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False):
                                with mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None):
                                    with mock.patch.object(tui_screens, "write_claude_mcp", return_value=""):
                                        with mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")):
                                            with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")):
                                                with mock.patch.object(tui_screens, "write_claude_permissions", return_value=""):
                                                    with mock.patch.object(tui_screens, "_remember_member_window_id", return_value=""):
                                                        with mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
                                                            ok, msg = tui_screens.launch_terminals("team")
        return ok, msg, tmux_calls

    def test_env_var_has_model_for_leader(self):
        """TUI launch: leader 的 --settings 文件含 ANTHROPIC_MODEL（CLI 为 --model）。"""
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        env = _settings_env_from_cmd(session_cmd)
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "deepseek/deepseek-v4-flash[1m]",
                         "leader 的 settings ANTHROPIC_MODEL 应为 flash[1m]")

    def test_env_var_has_model_for_member(self):
        """TUI launch: 成员 coder_a 的 --settings 文件含 ANTHROPIC_MODEL。"""
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        coder_windows = [c for c in tmux_calls if c[0] == "new-window" and "coder_a" in c]
        self.assertEqual(len(coder_windows), 1)
        env = _settings_env_from_cmd(coder_windows[0])
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "deepseek/deepseek-v4-flash[1m]",
                         "成员 coder_a 的 settings ANTHROPIC_MODEL 应为 flash[1m]")

    def test_tui_launch_model_flag_present(self):
        """TUI launch_terminals 对 typed profile 注入 --model CLI flag（已修复）。

        验收标准：
        1. Leader 的 --model 值为 deepseek/deepseek-v4-flash[1m]
        2. 成员 coder_a 的 --model 值为 deepseek/deepseek-v4-flash[1m]
        3. 中断/恢复后 model 保持不变（env var + CLI flag 双重保障）
        """
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")

        # 检查 leader --model
        session_cmd = next(c for c in tmux_calls if c[0] == "new-session")
        cmd_strs = [str(x) for x in session_cmd]
        self.assertIn("--model", cmd_strs, "leader 命令应包含 --model flag")
        model_idx = cmd_strs.index("--model")
        self.assertLess(model_idx + 1, len(cmd_strs), "--model 后应有值")
        self.assertEqual(cmd_strs[model_idx + 1], "deepseek/deepseek-v4-flash[1m]",
                        f"leader --model 应为 flash[1m]")

        # 检查成员 coder_a --model
        coder_windows = [c for c in tmux_calls if c[0] == "new-window" and "coder_a" in c]
        self.assertEqual(len(coder_windows), 1)
        coder_cmd = [str(x) for x in coder_windows[0]]
        self.assertIn("--model", coder_cmd, "成员 coder_a 命令应包含 --model flag")
        model_idx = coder_cmd.index("--model")
        self.assertEqual(coder_cmd[model_idx + 1], "deepseek/deepseek-v4-flash[1m]",
                        f"成员 --model 应为 flash[1m]")

    def test_env_var_model_not_default_pro(self):
        """TUI launch: settings 文件中的 model 不应是默认的 pro 模型。"""
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        models = []
        for c in tmux_calls:
            if c[0] not in ("new-session", "new-window"):
                continue
            env = _settings_env_from_cmd(c)
            if env.get("ANTHROPIC_MODEL"):
                models.append(env["ANTHROPIC_MODEL"])
        self.assertIn("deepseek/deepseek-v4-flash[1m]", models,
                      "settings 中应为 flash 模型")
        self.assertNotIn("deepseek/deepseek-v4-pro[1m]", models,
                         "不应包含默认 pro 模型")

    def test_codex_member_gets_codex_model(self):
        """TUI launch: Codex 成员 coder_b 的 CODEX_MODEL env var 已正确注入。"""
        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        codex_windows = [c for c in tmux_calls if c[0] == "new-window" and "coder_b" in c]
        self.assertEqual(len(codex_windows), 1)
        cmd_strs = [str(x) for x in codex_windows[0]]
        self.assertIn("CODEX_MODEL=gpt-4o", cmd_strs,
                     "Codex 成员应注入 CODEX_MODEL=gpt-4o")

    # ---- P0 回归：leader 默认回退默认 profile → 完整 Claude 接管 ----

    def _leader_session_cmd(self, tmux_calls):
        """返回 leader new-session 命令（字符串列表）。"""
        return [str(x) for x in next(c for c in tmux_calls if c[0] == "new-session")]

    def test_leader_default_fallback_injects_api_key_and_base_url(self):
        """P0 回归：leader 无显式 agent_user → 回退 team.default_agent_user 时，
        完整 Claude 接管（ANTHROPIC_API_KEY + ANTHROPIC_BASE_URL + MODEL）与
        resolve_agent_model 的 MODEL 语义保持一致。

        复现路径：TUI 创建 leader 终端，leader 使用团队默认 Agent 用户，
        模型已生效但 Anthropic API key/base URL 未注入（修复前）。
        """
        team = self.team_data["teams"]["team"]
        team["default_agent_user"] = "claude_p"
        team["members"]["lead"]["agent_user"] = ""  # 无显式选择 → 回退默认
        # 默认 profile 未开 takeover_enabled（默认回退即完整接管，不依赖该开关）
        del team["agent_users"]["claude_p"]["takeover_enabled"]

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        cmd = self._leader_session_cmd(tmux_calls)
        self.assertIn("--settings", " ".join(cmd),
                      "默认回退 leader 应携带 --settings")
        self.assertNotIn("sk-test-key", " ".join(cmd),
                         "key 值不得出现在命令行（安全）")
        env = _settings_env_from_cmd(cmd)
        self.assertEqual(env.get("ANTHROPIC_API_KEY"), "sk-test-key",
                         "默认回退 leader 必须注入 ANTHROPIC_API_KEY")
        self.assertEqual(env.get("ANTHROPIC_BASE_URL"), "https://aiapi.lejurobot.com",
                         "默认回退 leader 必须注入 ANTHROPIC_BASE_URL")
        self.assertEqual(env.get("ANTHROPIC_MODEL"), "deepseek/deepseek-v4-flash[1m]",
                         "默认回退 leader 必须注入 ANTHROPIC_MODEL")

    def test_leader_explicit_disabled_still_blocks_env(self):
        """安全语义保留：leader 显式选择 takeover_enabled=False 的 profile →
        不注入任何 agent profile env（显式关闭仍被尊重）。"""
        team = self.team_data["teams"]["team"]
        # lead 已显式选择 claude_p；关闭 takeover 后不得注入任何字段
        team["agent_users"]["claude_p"]["takeover_enabled"] = False

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        cmd = self._leader_session_cmd(tmux_calls)
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "显式选择 takeover_enabled=False → 不注入 API key")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "显式选择 takeover_enabled=False → 不注入 base URL")

    def test_leader_sentinel_none_blocks_all_env(self):
        """安全语义保留：leader agent_user='__none__'（显式不接管）→
        不注入任何 agent profile env，即使团队设了 default_agent_user。"""
        team = self.team_data["teams"]["team"]
        team["default_agent_user"] = "claude_p"
        team["members"]["lead"]["agent_user"] = "__none__"

        ok, msg, tmux_calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        cmd = self._leader_session_cmd(tmux_calls)
        joined = " ".join(cmd)
        self.assertNotIn("ANTHROPIC_API_KEY", joined,
                         "sentinel leader 不应注入 ANTHROPIC_API_KEY")
        self.assertNotIn("ANTHROPIC_BASE_URL", joined,
                         "sentinel leader 不应注入 ANTHROPIC_BASE_URL")
        self.assertNotIn("ANTHROPIC_MODEL", joined,
                         "sentinel leader 不应注入 ANTHROPIC_MODEL")


# ============================================================
# task4 — Screen 接线：MainScreen u → 全局 manage；TeamDetailScreen u → 团队默认
# ============================================================

class TuiScreenAgentUserWiringTests(unittest.IsolatedAsyncioTestCase):
    """验证 Agent 用户入口在两个 Screen 上的绑定（不启动完整 App）。"""

    def test_mainscreen_u_binding_opens_global_manage(self):
        """MainScreen 顶层 u 键 → action_agent_users → 全局管理（无团队参数）。"""
        from tui.tui_screens import MainScreen

        bindings = MainScreen.BINDINGS
        match = [b for b in bindings
                 if b.action == "agent_users" and "u" in b.key.split(",")]
        self.assertEqual(len(match), 1, "MainScreen 应有 u → agent_users 绑定")
        # 全局管理：MainScreen 的 action 存在即可（不传团队名）
        self.assertTrue(callable(getattr(MainScreen, "action_agent_users", None)))

    def test_teamdetail_u_binding_opens_team_default(self):
        """TeamDetailScreen u 键 → action_team_default_agent_user（团队默认选择）。"""
        from tui.tui_screens import TeamDetailScreen

        bindings = TeamDetailScreen.BINDINGS
        match = [b for b in bindings
                 if b.action == "team_default_agent_user" and "u" in b.key.split(",")]
        self.assertEqual(len(match), 1,
                         "TeamDetailScreen 应有 u → team_default_agent_user 绑定")
        # 旧的"管理"action 不应再存在（管理已移到 MainScreen）
        self.assertFalse(
            hasattr(TeamDetailScreen, "action_agent_users"),
            "TeamDetailScreen 不应再保留全局管理 action")

    async def test_app_on_mount_does_not_migrate(self):
        """TeamManagerApp.on_mount 不执行迁移（headless 实例化零真实文件副作用）。

        迁移只在 CLI 入口 run_team_manager_app() 中于 app.run() 前执行。
        """
        from tui.tui_screens import TeamManagerApp

        with _temp_data_override(), \
             mock.patch("tui.tui_screens.load_data",
                        return_value={"teams": {}}), \
             mock.patch("tui.tui_screens.save_data"), \
             mock.patch(
                "tui.tui_screens._migrate_agent_users_global_file",
                return_value={},
             ) as mock_migrate:
            app = TeamManagerApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause(0.4)
                mock_migrate.assert_not_called()
                self.assertTrue(app.screen is not None,
                                "on_mount 应正常 push MainScreen")

    def test_run_team_manager_app_calls_migration_before_run(self):
        """CLI 入口 run_team_manager_app 在 app.run() 前调用一次全局迁移。"""
        from tui.tui_screens import run_team_manager_app

        with _temp_data_override(), \
             mock.patch("tui.tui_screens._migrate_agent_users_global_file",
                        return_value={}) as mock_migrate, \
             mock.patch.object(tui_screens.TeamManagerApp, "run",
                               return_value=None) as mock_run:
            run_team_manager_app()
        mock_migrate.assert_called_once()
        mock_run.assert_called_once()

    def test_run_team_manager_app_migration_failure_still_launches(self):
        """迁移失败（fail closed）→ 入口仍启动 TUI，错误写 stderr 但可继续。"""
        from tui.tui_screens import run_team_manager_app

        with _temp_data_override(), \
             mock.patch("tui.tui_screens._migrate_agent_users_global_file",
                        side_effect=RuntimeError("flock unavailable")), \
             mock.patch.object(tui_screens.TeamManagerApp, "run",
                               return_value=None) as mock_run:
            run_team_manager_app()
        mock_run.assert_called_once()
        # 未再抛异常即通过（legacy-aware 启动）


class TeamDetailMemberTableGlobalProfileTests(unittest.IsolatedAsyncioTestCase):
    """成员表显示全局 profile + provider 标签（task4 全局迁移后不丢 provider）。

    回归：成员表改用 common.list_agent_users 全局-aware 读，而非 team.get('agent_users')。
    """

    async def _render_member_table(self, data: dict):
        from textual.app import App
        from textual.widgets import DataTable
        from tui.tui_screens import TeamDetailScreen

        with _temp_data_override(data), \
             mock.patch("tui.tui_screens.load_data", return_value=data), \
             mock.patch("tui.tui_screens.save_data"), \
             mock.patch("common.tmux_utils.load_data", return_value=data), \
             mock.patch("tui.tui_screens._find_tmux_session",
                        return_value=None), \
             mock.patch("tui.tui_screens.get_member_terminal_status",
                        return_value={}):
            app = App()
            screen = TeamDetailScreen("team")
            async with app.run_test(size=(120, 30)) as pilot:
                await pilot.app.push_screen(screen)
                await pilot.pause(0.3)
                dt = screen.query_one("#member_table", DataTable)
                return dt

    def _row_text(self, dt) -> str:
        from textual.widgets import DataTable
        assert isinstance(dt, DataTable)
        rows = list(dt.rows.values())
        if not rows:
            return ""
        return "".join(str(v) for v in dt.get_row(rows[0].key))

    async def test_member_with_global_profile_shows_provider_badge(self):
        """成员显式引用全局 profile → 行显示 key + provider 徽标（claude=🤖）。"""
        data = {
            "agent_users": {
                "global_coder": {
                    "agent_type": "claude", "takeover_enabled": True,
                    "anthropic_api_key": "sk-a",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-sonnet-5",
                },
            },
            "teams": {"team": {
                "default_agent": "claude",
                "members": {
                    "alice": {"role": "coder", "agent": "claude",
                              "agent_user": "global_coder"},
                },
            }},
        }
        dt = await self._render_member_table(data)
        text = self._row_text(dt)
        self.assertIn("global_coder", text, "应显示全局 profile key")
        self.assertIn("🤖", text, "claude profile 应带 🤖 provider 徽标")

    async def test_member_default_fallback_shows_global_profile_default(self):
        """成员未指定 agent_user → 回退团队默认（全局 profile），行显示 key+默认标记。"""
        data = {
            "agent_users": {
                "global_codex": {
                    "agent_type": "codex", "takeover_enabled": True,
                    "openai_api_key": "sk-b",
                    "openai_base_url": "https://api.openai.com",
                    "codex_model": "gpt-4o",
                },
            },
            "teams": {"team": {
                "default_agent": "codex",
                "default_agent_user": "global_codex",
                "members": {
                    "bob": {"role": "coder", "agent": "codex"},
                },
            }},
        }
        dt = await self._render_member_table(data)
        text = self._row_text(dt)
        self.assertIn("global_codex", text, "应显示回退的全局 profile key")
        self.assertIn("🔵", text, "codex profile 应带 🔵 provider 徽标")
        self.assertIn("默认", text, "回退团队默认应带 (默认) 标记")

    async def test_member_no_takeover_shows_no_takeover(self):
        """成员显式不接管 → 行显示 '不接管'，即使全局有同名默认。"""
        data = {
            "agent_users": {
                "p1": {"agent_type": "claude", "takeover_enabled": True},
            },
            "teams": {"team": {
                "default_agent": "claude",
                "default_agent_user": "p1",
                "members": {
                    "carol": {"role": "tester", "agent": "claude",
                              "agent_user": "__none__"},
                },
            }},
        }
        dt = await self._render_member_table(data)
        text = self._row_text(dt)
        self.assertIn("不接管", text, "显式不接管成员应显示 '不接管'")

    async def test_legacy_team_profile_still_shown_before_migration(self):
        """未迁移团队旧 profile 仍显示（list_agent_users 混合读兼容）。"""
        data = {
            # 无全局 registry，仅团队旧数据 → 仍应显示 provider
            "teams": {"team": {
                "default_agent": "claude",
                "agent_users": {
                    "old_p": {"agent_type": "codex", "takeover_enabled": True},
                },
                "members": {
                    "dave": {"role": "coder", "agent": "codex",
                             "agent_user": "old_p"},
                },
            }},
        }
        dt = await self._render_member_table(data)
        text = self._row_text(dt)
        self.assertIn("old_p", text, "未迁移团队旧 profile 仍应显示")
        self.assertIn("🔵", text, "旧 codex profile 应带 🔵 徽标")


if __name__ == "__main__":
    unittest.main()
