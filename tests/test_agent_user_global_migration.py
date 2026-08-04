"""
task4 — Agent 用户配置全局迁移 + 全局 CRUD sweep 测试。
=========================================================

背景（固定契约）：
  Agent 用户配置从团队级 team["agent_users"] 提升为全局 data["agent_users"]
  registry；CRUD 移到 MainScreen 顶层（跨团队复用）。team.default_agent_user
  与 member.agent_user（含 AGENT_USER_NONE 不接管）语义保留。
  全局 rename/delete 需 sweep 所有团队引用。
  0600 仅作为持久化约束。

迁移规则（R1-R5，拍板契约）：
  R1 普通迁移：  key ∉ 全局 → M[key] = cfg，团队内引用不变
  R2 同名合并：  key ∈ 全局 且 cfg 相同 → 不重复写（去重）
  R3 同名冲突：  key ∈ 全局 且 cfg 不同 → 稳定遍历分配 key__2/key__3…
                 仅同步"发生冲突的团队"的 default/member 引用；其他团队不动；
                 两份配置零丢失
  R4 不接管保护：key == AGENT_USER_NONE 绝不被迁移/合并/重命名
  R5 幂等：      再次执行结果完全一致（迁移后清除团队级存储 → no-op）

全局 delete 语义（M06）：
  删除 M[key]；引用该 key 的 team.default_agent_user 清空；
  引用该 key 的 member.agent_user 清空字段（回退该团队默认），
  不强制写 AGENT_USER_NONE。

本文件分四组：
  1) 已落地的生产函数（真实断言）：
     - tui_dialogs._agent_user_profiles（全局优先 / 旧数据兼容回退）
     - tui_dialogs._agent_user_rename_sweep / _agent_user_delete_sweep / _agent_user_ref_count
     - 全局 registry 持久化 + 0600
  2) 迁移函数契约（R1-R5）：直接对生产 common.tmux_utils.migrate_agent_users_global
     断言（含同名冲突变体复用）。
  3) 读路径全局解析（已翻绿）：common 读路径（get_agent_user_env_prefix /
     resolve_agent_model / get_agent_user_config / list_agent_users）读全局
     data['agent_users']，兼容未迁移团队旧数据。
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path

from common import data_layer
from common.data_layer import load_data, save_data
from common.tmux_utils import (
    AGENT_USER_NONE,
    get_agent_user_env_prefix,
    migrate_agent_users_global,
    resolve_agent_model,
)
from tui.tui_dialogs import (
    _agent_user_profiles,
    _agent_user_ref_count,
    _agent_user_rename_sweep,
    _agent_user_delete_sweep,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def _typed_claude_cfg(**over) -> dict:
    cfg = {
        "agent_type": "claude",
        "takeover_enabled": True,
        "anthropic_api_key": "sk-ant-test",
        "anthropic_base_url": "https://api.anthropic.com",
        "anthropic_model": "claude-opus-5",
    }
    cfg.update(over)
    return cfg


class GlobalRegistryFixture(unittest.TestCase):
    """通过 data_layer.set_data_file 隔离到临时数据文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / ".mult_agent_mcp" / "teams_data.json"
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    def _write(self, data: dict) -> None:
        save_data(data)

    def _load(self) -> dict:
        return load_data()


# ============================================================
# 1) 已落地：_agent_user_profiles — 全局优先 / 旧数据兼容回退
# ============================================================

class AgentUserProfilesReadTests(GlobalRegistryFixture):
    """_agent_user_profiles 的全局优先与旧团队级数据回退（M11 兼容）。"""

    def test_profiles_uses_global_when_present(self):
        """全局 registry 非空 → 直接使用全局，忽略团队级旧数据。"""
        self._write({
            "agent_users": {"g1": _typed_claude_cfg()},
            "teams": {
                "teamA": {"agent_users": {"legacy1": _typed_claude_cfg()}},
            },
        })
        self.assertEqual(list(_agent_user_profiles().keys()), ["g1"])

    def test_profiles_team_fallback_when_no_global(self):
        """无全局 + 指定团队 → 回退该团队旧数据。"""
        self._write({
            "teams": {
                "teamA": {"agent_users": {"p1": _typed_claude_cfg()}},
                "teamB": {"agent_users": {"p2": _typed_claude_cfg()}},
            },
        })
        self.assertEqual(list(_agent_user_profiles("teamA").keys()), ["p1"])
        self.assertEqual(list(_agent_user_profiles("teamB").keys()), ["p2"])

    def test_profiles_merge_legacy_when_no_global(self):
        """无全局 + 未指定团队（全局管理视图）→ 仅返回全局 registry。

        task4 后 _agent_user_profiles 委托 common.list_agent_users：全局管理
        视图（team_name 空）= post-migration 全局 registry，不再合并全部团队
        旧数据；团队级旧数据仅在指定团队读取路径合并（迁移兼容）。
        """
        self._write({
            "teams": {
                "teamA": {"agent_users": {"p1": _typed_claude_cfg()}},
                "teamB": {"agent_users": {"p2": _typed_claude_cfg()}},
            },
        })
        # 全局管理视图：无全局 → 空
        self.assertEqual(_agent_user_profiles(), {})
        # 指定团队读取：合并该团队旧数据（迁移兼容）
        self.assertEqual(sorted(_agent_user_profiles("teamA").keys()), ["p1"])
        self.assertEqual(sorted(_agent_user_profiles("teamB").keys()), ["p2"])

    def test_profiles_empty_when_nothing(self):
        """既无全局也无团队级 → 空 dict，不崩溃。"""
        self._write({"teams": {"teamA": {}}})
        self.assertEqual(_agent_user_profiles(), {})
        self.assertEqual(_agent_user_profiles("teamA"), {})


# ============================================================
# 1) 已落地：全局 rename / delete sweep（M05 / M06）
# ============================================================

class GlobalRenameSweepTests(GlobalRegistryFixture):
    """全局 rename 后 sweep 所有团队的 default/member 引用 + 旧团队级存储。"""

    def test_rename_swaps_global_key_and_sweeps_all_refs(self):
        """完整 rename 流程：M 键更换 + 所有团队 default/member 引用 sweep。"""
        self._write({
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {
                "teamA": {
                    "default_agent_user": "p1",
                    "members": {"alice": {"agent_user": "p1"}},
                },
                "teamB": {
                    "members": {"bob": {"agent_user": "p1"}},
                },
            },
        })
        data = self._load()
        # 对话框内 rename 流程：先在全局 registry 换键，再 sweep 引用
        agent_users = data.setdefault("agent_users", {})
        agent_users["p_new"] = agent_users.pop("p1")
        teams_aff, members_aff = _agent_user_rename_sweep(data, "p1", "p_new")
        save_data(data)

        reloaded = self._load()
        self.assertIn("p_new", reloaded["agent_users"])
        self.assertNotIn("p1", reloaded["agent_users"])
        self.assertEqual(reloaded["teams"]["teamA"]["default_agent_user"], "p_new")
        self.assertEqual(reloaded["teams"]["teamA"]["members"]["alice"]["agent_user"], "p_new")
        self.assertEqual(reloaded["teams"]["teamB"]["members"]["bob"]["agent_user"], "p_new")
        # 无悬空引用
        for team in reloaded["teams"].values():
            self.assertNotEqual(team.get("default_agent_user"), "p1")
            for member in team.get("members", {}).values():
                self.assertNotEqual(member.get("agent_user"), "p1")
        self.assertEqual((teams_aff, members_aff), (2, 2))

    def test_rename_migrates_legacy_team_key(self):
        """旧团队级 agent_users 中的 old_key 一并改名（兼容未迁移数据）。"""
        data = {
            "agent_users": {},
            "teams": {
                "teamA": {
                    "agent_users": {"p1": _typed_claude_cfg()},
                    "members": {"alice": {"agent_user": "p1"}},
                },
            },
        }
        teams_aff, members_aff = _agent_user_rename_sweep(data, "p1", "p_new")
        self.assertIn("p_new", data["teams"]["teamA"]["agent_users"])
        self.assertNotIn("p1", data["teams"]["teamA"]["agent_users"])
        self.assertEqual(data["teams"]["teamA"]["members"]["alice"]["agent_user"], "p_new")
        self.assertEqual((teams_aff, members_aff), (1, 1))


class GlobalDeleteSweepTests(GlobalRegistryFixture):
    """全局 delete 后 sweep：default 清空、member 回退团队默认（M06 契约）。"""

    def test_delete_clears_default_and_member_refs_not_none(self):
        """删除 profile 后：team.default_agent_user 清空；member.agent_user
        清空字段回退团队默认，且不强制写 AGENT_USER_NONE。"""
        data = {
            "agent_users": {"p1": _typed_claude_cfg(), "p2": _typed_claude_cfg()},
            "teams": {
                "teamA": {
                    "default_agent_user": "p1",
                    "members": {
                        "alice": {"agent_user": "p1"},
                        "bob": {"agent_user": "p2"},  # 不受影响
                    },
                },
                "teamB": {
                    "members": {"carol": {"agent_user": "p1"}},
                },
            },
        }
        # 复现 AgentUserManageDialog.delete_user 流程：先从全局 registry 删键，
        # 再 sweep 引用（_agent_user_delete_sweep 只处理引用）
        registry = data.get("agent_users") or {}
        registry.pop("p1", None)
        if not registry:
            data.pop("agent_users", None)
        teams_aff, members_aff = _agent_user_delete_sweep(data, "p1")
        self.assertNotIn("p1", data["agent_users"])
        self.assertIn("p2", data["agent_users"])
        # default 清空
        self.assertNotIn("default_agent_user", data["teams"]["teamA"])
        # member 引用 p1 的清空字段（回退团队默认），不写 __none__
        self.assertNotIn("agent_user", data["teams"]["teamA"]["members"]["alice"])
        self.assertNotIn("agent_user", data["teams"]["teamB"]["members"]["carol"])
        # 无关成员保留
        self.assertEqual(data["teams"]["teamA"]["members"]["bob"]["agent_user"], "p2")
        # 不强制写 AGENT_USER_NONE
        for team in data["teams"].values():
            for member in team.get("members", {}).values():
                self.assertNotEqual(member.get("agent_user"), AGENT_USER_NONE)
        self.assertEqual((teams_aff, members_aff), (2, 2))

    def test_delete_removes_legacy_team_key_and_drops_empty(self):
        """旧团队级 agent_users 中的 key 一并移除；清空后删除该字段。"""
        data = {
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {
                "teamA": {"agent_users": {"p1": _typed_claude_cfg()}},
            },
        }
        _agent_user_delete_sweep(data, "p1")
        self.assertNotIn("agent_users", data["teams"]["teamA"])

    def test_delete_last_profile_drops_global_key(self):
        """删除最后一个 profile → 全局 registry 键被移除（不留空 dict）。"""
        data = {
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {"teamA": {"default_agent_user": "p1"}},
        }
        registry = data.get("agent_users") or {}
        registry.pop("p1", None)
        if not registry:
            data.pop("agent_users", None)
        _agent_user_delete_sweep(data, "p1")
        self.assertNotIn("agent_users", data)

    def test_ref_count_counts_teams_and_members(self):
        """_agent_user_ref_count 正确统计受影响团队/成员数（删除前影响提示）。"""
        data = {
            "teams": {
                "teamA": {"default_agent_user": "p1",
                          "members": {"a": {"agent_user": "p1"}, "b": {"agent_user": "p2"}}},
                "teamB": {"members": {"c": {"agent_user": "p1"}}},
            },
        }
        teams, members = _agent_user_ref_count(data, "p1")
        self.assertEqual((teams, members), (2, 2))


# ============================================================
# 1) 已落地：全局 registry 持久化 + 0600（M10 持久化约束）
# ============================================================

class GlobalRegistryPersistenceTests(GlobalRegistryFixture):
    """全局 registry 经 data_layer.save_data 持久化，数据文件权限 0600。"""

    def test_global_profile_persists_roundtrip_with_0600(self):
        data = {
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {"teamA": {"members": {"alice": {"agent_user": "p1"}}}},
        }
        save_data(data)
        self.assertTrue(self.data_file.exists())
        self.assertEqual(_mode(self.data_file), 0o600, "持久化约束：数据文件必须 0600")
        reloaded = load_data()
        self.assertEqual(reloaded["agent_users"]["p1"]["anthropic_base_url"],
                         "https://api.anthropic.com")


# ============================================================
# 2) 迁移函数契约（R1-R5）— 直接对齐生产 common.tmux_utils.migrate_agent_users_global
# ============================================================

class LegacyMigrationContractTests(GlobalRegistryFixture):
    """R1-R5 迁移契约，直接对生产迁移函数 migrate_agent_users_global 断言。

    生产实现位于 common.tmux_utils（供 MCP 与 TUI 共用）：
      - 稳定遍历（团队按名、profile 按 key）保证幂等/可复现；
      - 迁移后清除各团队级 agent_users → 二次迁移为 no-op（R5 幂等）；
      - 同名冲突复用已有同 cfg 变体，否则稳定分配 key__2/key__3…。
    """

    def test_R1_plain_migration_merges_into_global(self):
        """R1：不同团队的不同 key 全部进入全局 registry，引用不变。"""
        data = {
            "teams": {
                "teamA": {"agent_users": {"p1": _typed_claude_cfg()},
                          "default_agent_user": "p1",
                          "members": {"alice": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p2": _typed_claude_cfg(anthropic_base_url="https://b.com")},
                          "members": {"bob": {"agent_user": "p2"}}},
            },
        }
        migrated = migrate_agent_users_global(data)
        self.assertEqual(sorted(migrated["agent_users"].keys()), ["p1", "p2"])
        self.assertEqual(migrated["teams"]["teamA"]["default_agent_user"], "p1")
        self.assertEqual(migrated["teams"]["teamA"]["members"]["alice"]["agent_user"], "p1")
        self.assertEqual(migrated["teams"]["teamB"]["members"]["bob"]["agent_user"], "p2")
        # 团队级存储已清除
        for team in migrated["teams"].values():
            self.assertNotIn("agent_users", team)

    def test_R2_same_key_same_cfg_merges(self):
        """R2：同名且配置相同 → 全局唯一，两团队引用均指向原 key。"""
        data = {
            "teams": {
                "teamA": {"agent_users": {"p1": _typed_claude_cfg()},
                          "members": {"alice": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": _typed_claude_cfg()},
                          "members": {"bob": {"agent_user": "p1"}}},
            },
        }
        migrated = migrate_agent_users_global(data)
        self.assertEqual(sorted(migrated["agent_users"].keys()), ["p1"])
        self.assertEqual(migrated["teams"]["teamA"]["members"]["alice"]["agent_user"], "p1")
        self.assertEqual(migrated["teams"]["teamB"]["members"]["bob"]["agent_user"], "p1")

    def test_R3_same_key_diff_cfg_renames_and_syncs_own_team(self):
        """R3：同名不同配置 → 稳定遍历分配 key__2；仅冲突团队引用改写，
        另一团队不动；两份配置零丢失。"""
        cfg_a = _typed_claude_cfg(anthropic_base_url="https://a.com")
        cfg_b = _typed_claude_cfg(anthropic_base_url="https://b.com")
        data = {
            "teams": {
                "teamA": {"agent_users": {"p1": cfg_a},
                          "default_agent_user": "p1",
                          "members": {"alice": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": cfg_b},
                          "default_agent_user": "p1",
                          "members": {"bob": {"agent_user": "p1"}}},
            },
        }
        migrated = migrate_agent_users_global(data)
        keys = sorted(migrated["agent_users"].keys())
        # 稳定遍历（teamA 在前）→ teamA 保留 p1，teamB 冲突改名为 p1__2
        self.assertEqual(keys, ["p1", "p1__2"])
        self.assertEqual(migrated["agent_users"]["p1"]["anthropic_base_url"], "https://a.com")
        self.assertEqual(migrated["agent_users"]["p1__2"]["anthropic_base_url"], "https://b.com")
        # 仅 teamB（发生冲突的团队）引用改写
        self.assertEqual(migrated["teams"]["teamA"]["default_agent_user"], "p1")
        self.assertEqual(migrated["teams"]["teamA"]["members"]["alice"]["agent_user"], "p1")
        self.assertEqual(migrated["teams"]["teamB"]["default_agent_user"], "p1__2")
        self.assertEqual(migrated["teams"]["teamB"]["members"]["bob"]["agent_user"], "p1__2")

    def test_R4_none_never_migrated(self):
        """R4：AGENT_USER_NONE 不进入 registry；不接管成员不受影响。"""
        data = {
            "teams": {
                "teamA": {"agent_users": {"p1": _typed_claude_cfg()},
                          "members": {
                              "alice": {"agent_user": "p1"},
                              "bob": {"agent_user": AGENT_USER_NONE},
                          }},
            },
        }
        migrated = migrate_agent_users_global(data)
        self.assertNotIn(AGENT_USER_NONE, migrated["agent_users"])
        self.assertEqual(migrated["teams"]["teamA"]["members"]["bob"]["agent_user"],
                         AGENT_USER_NONE)

    def test_R5_idempotent_second_run_is_noop(self):
        """R5：重复迁移结果完全一致（无重复条目、无重复重命名、引用不再变）。"""
        cfg_a = _typed_claude_cfg(anthropic_base_url="https://a.com")
        cfg_b = _typed_claude_cfg(anthropic_base_url="https://b.com")
        data = {
            "teams": {
                "teamA": {"agent_users": {"p1": cfg_a}, "members": {"a": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": cfg_b}, "members": {"b": {"agent_user": "p1"}}},
            },
        }
        once = migrate_agent_users_global(data)
        twice = migrate_agent_users_global(once)
        self.assertEqual(once, twice)
        self.assertEqual(sorted(twice["agent_users"].keys()), ["p1", "p1__2"])
        self.assertEqual(twice["teams"]["teamB"]["members"]["b"]["agent_user"], "p1__2")

    def test_R3_deterministic_stable_order(self):
        """R3 命名可复现：同一输入多次迁移，冲突 key 分配完全一致。"""
        data = {
            "teams": {
                "zebra": {"agent_users": {"p1": _typed_claude_cfg()},
                          "members": {"z": {"agent_user": "p1"}}},
                "alpha": {"agent_users": {"p1": _typed_claude_cfg(anthropic_base_url="https://z.com")},
                          "members": {"a": {"agent_user": "p1"}}},
            },
        }
        r1 = migrate_agent_users_global(data)
        r2 = migrate_agent_users_global(data)
        self.assertEqual(r1, r2)


# ============================================================
# 3) 读路径全局解析（已落地）
# ============================================================

class GlobalReadPathContractTests(GlobalRegistryFixture):
    """common 读路径应读取全局 registry 解析成员/团队默认 profile。

    已落地：get_agent_user_env_prefix / resolve_agent_model / get_agent_user_config
    / list_agent_users 均读全局 data['agent_users']（兼容未迁移的团队旧数据）。
    以下用例为生产读路径的全局解析契约。
    """

    def test_env_prefix_resolves_global_profile(self):
        """M13：成员引用全局 profile（团队级无配置）→ env 前缀来自全局 registry。"""
        data = {
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {
                "teamA": {"members": {"alice": {"agent": "claude", "agent_user": "p1"}}},
            },
        }
        self._write(data)
        prefix = get_agent_user_env_prefix("teamA", "alice", "claude")
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", prefix)
        self.assertIn("ANTHROPIC_MODEL=claude-opus-5", prefix)

    def test_env_prefix_resolves_global_team_default(self):
        """M07 读路径：成员未指定 → 团队默认指向全局 profile，env 来自全局。"""
        data = {
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {
                "teamA": {"default_agent_user": "p1",
                          "members": {"alice": {"agent": "claude"}}},
            },
        }
        self._write(data)
        prefix = get_agent_user_env_prefix("teamA", "alice", "claude")
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", prefix)

    def test_model_resolves_global_profile(self):
        """全局 typed profile → resolve_agent_model 返回 provider 专属 model。"""
        data = {
            "agent_users": {"p1": _typed_claude_cfg()},
            "teams": {
                "teamA": {"default_agent": "claude",
                          "members": {"alice": {"agent": "claude", "agent_user": "p1"}}},
            },
        }
        self._write(data)
        self.assertEqual(resolve_agent_model("teamA", "alice"), "claude-opus-5")


if __name__ == "__main__":
    unittest.main()
