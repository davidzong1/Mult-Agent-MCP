"""
Agent 用户池 — 数据层测试（common.tmux_utils）
================================================

覆盖 plan-b §3.2 有序池的四个数据层函数：
  1. get_agent_user_pool      — registry 净化 / AGENT_USER_NONE 丢弃 / 保序去重 / 空池
  2. set_agent_user_pool      — 校验 + 整体写入 / 空列表清池并 pop cursor / 失败不部分写入
  3. next_agent_user_in_pool  — 后继 / wrap True/False / 单元素池 None / current 不在池 → 池首
  4. quota_failover_config    — defaults 合并 / 类型强制 / 上下钳制边界

数据隔离：经 data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
"""

import tempfile
import unittest
from pathlib import Path

from common import data_layer
from common.data_layer import load_data, save_data
from common.tmux_utils import (
    AGENT_USER_NONE,
    get_agent_user_pool,
    next_agent_user_in_pool,
    quota_failover_config,
    set_agent_user_pool,
)

_POOL_DATA = {
    "agent_users": {
        "claude_p": {
            "agent_type": "claude",
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-test",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        },
        "codex_p": {
            "agent_type": "codex",
            "takeover_enabled": False,
            "openai_api_key": "sk-fake",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-4o",
        },
        # 同 provider 第二 key（供"保序去重"用例在新同类校验下使用：
        # 团队池现已拒绝混合 provider，去重验证须用同类型 key）
        "claude_p2": {
            "agent_type": "claude",
            "takeover_enabled": False,
            "anthropic_api_key": "sk-ant-2",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-haiku-4-5",
        },
    },
    "teams": {
        "pool_team": {
            # 池内含：合法 key、重复 key、不在 registry 的 key、AGENT_USER_NONE、
            # 仅在团队旧数据(legacy agent_users)中的 key —— 全部应由净化处理
            "agent_user_pool": [
                "codex_p",
                "claude_p",
                "claude_p",
                "ghost_p",
                AGENT_USER_NONE,
                "legacy_p",
            ],
            "agent_user_pool_cursor": 1,
            "agent_users": {"legacy_p": {"agent_type": "codex", "codex_model": "gpt-4o-mini"}},
        },
        "empty_team": {},
        "none_team": {"agent_user_pool": None},
        "str_team": {"agent_user_pool": "not-a-list"},
        "int_team": {"agent_user_pool": [1, "claude_p"]},
    },
}


class _PoolDataBase(unittest.TestCase):
    """数据隔离基类（临时 teams_data.json，不触碰真实数据）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_POOL_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    def _team(self, name: str = "pool_team") -> dict:
        return load_data()["teams"][name]


class GetAgentUserPoolTests(_PoolDataBase):
    """get_agent_user_pool：registry 净化 / 哨兵 / 去重 / 空池。"""

    def test_sanitize_drop_missing_none_dedup(self):
        """净化后：[codex_p, claude_p, legacy_p] —— ghost_p/NONE/重复被清，保序。"""
        pool = get_agent_user_pool(self._team())
        self.assertEqual(pool, ["codex_p", "claude_p", "legacy_p"])

    def test_legacy_registry_keys_kept(self):
        """仅存在于团队旧数据(legacy agent_users)的 key 保留（合并语义）。"""
        self.assertIn("legacy_p", get_agent_user_pool(self._team()))

    def test_missing_pool_returns_empty(self):
        self.assertEqual(get_agent_user_pool(self._team("empty_team")), [])

    def test_none_pool_returns_empty(self):
        self.assertEqual(get_agent_user_pool(self._team("none_team")), [])

    def test_str_pool_returns_empty(self):
        self.assertEqual(get_agent_user_pool(self._team("str_team")), [])

    def test_non_str_members_dropped(self):
        """int 成员被丢弃，字符串成员保留。"""
        self.assertEqual(get_agent_user_pool(self._team("int_team")), ["claude_p"])

    def test_non_dict_team_returns_empty(self):
        self.assertEqual(get_agent_user_pool(None), [])
        self.assertEqual(get_agent_user_pool("pool_team"), [])


class SetAgentUserPoolTests(_PoolDataBase):
    """set_agent_user_pool：校验 + 整体写入 / 清池 / 失败不部分写入。"""

    def test_write_pool_order_preserved_deduped(self):
        """保序去重：重复 key 只保留首次出现位置；重建池后 cursor 归零。

        2026-08-09 同步修改：团队池已加内部 provider 一致性校验（防呆锁
        数据层兜底），混合 provider（如 codex_p+claude_p）现在被拒 ——
        保序去重验证改用同 provider 的 key（claude_p2/claude_p）。
        """
        ok, msg = set_agent_user_pool("empty_team", ["claude_p2", "claude_p", "claude_p2"])
        self.assertTrue(ok, msg)
        self.assertEqual(
            self._team("empty_team")["agent_user_pool"], ["claude_p2", "claude_p"])
        # 重建池后 cursor 归零
        self.assertEqual(self._team("empty_team")["agent_user_pool_cursor"], 0)

    def test_write_mixed_provider_pool_rejected(self):
        """混合 provider 池被拒且不落盘（防呆锁数据层兜底，MCP 直写绕不过）。"""
        ok, msg = set_agent_user_pool("empty_team", ["codex_p", "claude_p"])
        self.assertFalse(ok, msg)
        self.assertIn("同 provider", msg)
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_write_legacy_key_acceptable(self):
        """legacy registry 中的 key 同样可写入（在带 legacy 数据的团队上）。"""
        ok, msg = set_agent_user_pool("pool_team", ["legacy_p"])
        self.assertTrue(ok, msg)
        self.assertEqual(self._team("pool_team")["agent_user_pool"], ["legacy_p"])

    def test_clear_pool_pops_cursor(self):
        ok, msg = set_agent_user_pool("pool_team", [])
        self.assertTrue(ok, msg)
        team = self._team()
        self.assertNotIn("agent_user_pool", team)
        self.assertNotIn("agent_user_pool_cursor", team)

    def test_clear_already_empty_ok(self):
        ok, msg = set_agent_user_pool("empty_team", [])
        self.assertTrue(ok, msg)
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_unknown_key_rejected_no_write(self):
        ok, msg = set_agent_user_pool("empty_team", ["claude_p", "ghost_p"])
        self.assertFalse(ok)
        self.assertIn("ghost_p", msg)
        # 不部分写入：合法 key 也未被写入
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_agent_user_none_rejected_no_write(self):
        ok, msg = set_agent_user_pool("empty_team", ["claude_p", AGENT_USER_NONE])
        self.assertFalse(ok)
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_empty_string_rejected_no_write(self):
        ok, msg = set_agent_user_pool("empty_team", ["claude_p", ""])
        self.assertFalse(ok)
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_non_str_member_rejected_no_write(self):
        ok, msg = set_agent_user_pool("empty_team", [1, "claude_p"])
        self.assertFalse(ok)
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_non_list_rejected_no_write(self):
        ok, msg = set_agent_user_pool("empty_team", "claude_p")
        self.assertFalse(ok)
        self.assertNotIn("agent_user_pool", self._team("empty_team"))

    def test_team_not_found(self):
        ok, msg = set_agent_user_pool("no_such_team", ["claude_p"])
        self.assertFalse(ok)
        self.assertIn("no_such_team", msg)


class NextAgentUserInPoolTests(_PoolDataBase):
    """next_agent_user_in_pool：后继 / wrap / 单元素 / 空池 / 不在池。"""

    # 净化后池: ["codex_p", "claude_p", "legacy_p"]

    def test_next_in_pool(self):
        team = self._team()
        self.assertEqual(next_agent_user_in_pool(team, "codex_p"), "claude_p")
        self.assertEqual(next_agent_user_in_pool(team, "claude_p"), "legacy_p")

    def test_at_tail_wrap_default_true_returns_first(self):
        self.assertEqual(next_agent_user_in_pool(self._team(), "legacy_p"), "codex_p")

    def test_at_tail_wrap_false_returns_none(self):
        data = load_data()
        data["teams"]["pool_team"]["quota_failover"] = {"wrap": False}
        save_data(data)
        self.assertIsNone(next_agent_user_in_pool(self._team(), "legacy_p"))

    def test_current_not_in_pool_returns_first(self):
        team = self._team()
        self.assertEqual(next_agent_user_in_pool(team, "ghost_p"), "codex_p")
        self.assertEqual(next_agent_user_in_pool(team, None), "codex_p")
        self.assertEqual(next_agent_user_in_pool(team, ""), "codex_p")

    def test_single_element_pool_returns_none_even_wrap(self):
        """池长 1：wrap=True 也返回 None（无处可换，避免原地空转）。"""
        set_agent_user_pool("empty_team", ["claude_p"])
        team = self._team("empty_team")
        self.assertEqual(team["agent_user_pool"], ["claude_p"])  # 默认 wrap=True
        self.assertIsNone(next_agent_user_in_pool(team, "claude_p"))

    def test_single_element_pool_wrap_false_none(self):
        set_agent_user_pool("empty_team", ["claude_p"])
        data = load_data()
        data["teams"]["empty_team"]["quota_failover"] = {"wrap": False}
        save_data(data)
        self.assertIsNone(next_agent_user_in_pool(self._team("empty_team"), "claude_p"))

    def test_empty_pool_returns_none(self):
        self.assertIsNone(next_agent_user_in_pool(self._team("empty_team"), "claude_p"))


class QuotaFailoverConfigTests(_PoolDataBase):
    """quota_failover_config：defaults 合并 / 类型强制 / 钳制边界。"""

    DEFAULTS = {"enabled": False, "confirm_cycles": 2, "wrap": True, "max_switches": 6}

    def test_defaults_when_missing(self):
        self.assertEqual(quota_failover_config(self._team("empty_team")), self.DEFAULTS)

    def test_defaults_when_non_dict_stored(self):
        self.assertEqual(quota_failover_config({"quota_failover": "yes"}), self.DEFAULTS)

    def test_defaults_when_no_team(self):
        self.assertEqual(quota_failover_config({}), self.DEFAULTS)

    def test_merge_stored(self):
        cfg = quota_failover_config(
            {"quota_failover": {"enabled": True, "confirm_cycles": 5, "wrap": False, "max_switches": 12}}
        )
        self.assertEqual(cfg, {"enabled": True, "confirm_cycles": 5, "wrap": False, "max_switches": 12})

    def test_partial_merge_keeps_defaults(self):
        cfg = quota_failover_config({"quota_failover": {"max_switches": 3}})
        self.assertEqual(cfg["max_switches"], 3)
        self.assertEqual(cfg["enabled"], self.DEFAULTS["enabled"])
        self.assertEqual(cfg["confirm_cycles"], self.DEFAULTS["confirm_cycles"])
        self.assertEqual(cfg["wrap"], self.DEFAULTS["wrap"])

    def test_type_coercion(self):
        cfg = quota_failover_config({"quota_failover": {"confirm_cycles": "3", "enabled": 1, "max_switches": "9"}})
        self.assertEqual(cfg["confirm_cycles"], 3)
        self.assertIs(cfg["enabled"], True)
        self.assertEqual(cfg["max_switches"], 9)

    def test_clamp_lower_boundary(self):
        cfg = quota_failover_config({"quota_failover": {"confirm_cycles": 0, "max_switches": 0}})
        self.assertEqual(cfg["confirm_cycles"], 1)
        self.assertEqual(cfg["max_switches"], 1)

    def test_clamp_upper_boundary(self):
        cfg = quota_failover_config({"quota_failover": {"confirm_cycles": 99, "max_switches": 99}})
        self.assertEqual(cfg["confirm_cycles"], 10)
        self.assertEqual(cfg["max_switches"], 50)


if __name__ == "__main__":
    unittest.main()
