"""
成员级 effort 覆盖 — 解析/校验/启动映射回归测试
================================================================

覆盖（refactor-claude 持有的核心链路）：
  1. provider 分离的 effort 等级：Claude = low/medium/high/xhigh/max，
     Codex = minimal/low/medium/high/xhigh；Claude 不接受 minimal、
     Codex 不接受 max（避免单一集合混用）。
  2. normalize_effort / effort_levels_for / resolve_member_effort 三态：
     显式级别（覆盖 Agent 用户默认） / "off"（关闭） / 缺失或 "inherit"（继承）。
  3. 空参不炸：resolve_member_effort 参数名 agent_kind 不遮蔽同名函数
     agent_type(agent)（曾导致空参 TypeError）。
  4. 启动参数映射：Claude --effort <level>；Codex -c model_reasoning_effort="<level>"。
  5. leader 启动路径：leader 也是成员，resolve_member_effort(team, leader)
     正确解析并经 claude_agent_args / codex_command 注入。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
"""

import tempfile
import unittest
from pathlib import Path

from common import data_layer
from common.data_layer import save_data
from common.tmux_utils import (
    CLAUDE_EFFORT_LEVELS,
    CODEX_EFFORT_LEVELS,
    effort_levels_for,
    normalize_effort,
    resolve_member_effort,
    claude_agent_args,
    codex_command,
)

_PROFILE = {
    "agent_users": {
        "claude_default": {
            "agent_type": "claude", "effort": "high",
            "anthropic_api_key": "k", "anthropic_model": "m",
        },
        "codex_default": {
            "agent_type": "codex", "effort": "minimal",
            "openai_api_key": "k", "codex_model": "m",
        },
        "legacy": {"takeover_enabled": True, "anthropic_base_url": "https://x"},
    },
    "teams": {
        "t": {
            "default_agent": "claude",
            "default_agent_user": "claude_default",
            "members": {
                "leader": {"agent": "claude", "effort": "xhigh"},
                "explicit": {"agent": "claude", "effort": "max"},
                "off": {"agent": "claude", "effort": "off"},
                "inherit_claude": {"agent": "claude", "agent_user": "claude_default"},
                "inherit_codex": {"agent": "codex", "agent_user": "codex_default"},
                "legacy_member": {"agent": "claude", "agent_user": "legacy"},
                "none_member": {"agent": "claude", "agent_user": "__none__"},
                "empty": {"agent": "claude", "effort": ""},
                "codex_wrong_default": {"agent": "codex", "effort": "max"},
                "codex_effort": {"agent": "codex", "agent_user": "codex_default", "effort": "xhigh"},
            },
        },
    },
}


class _EffortIsolatedData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_PROFILE)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()


class EffortProviderSeparationTests(_EffortIsolatedData):
    """Claude / Codex 等级集分离，避免跨 provider 混用。"""

    def test_claude_levels_exclude_minimal_include_max(self):
        self.assertEqual(CLAUDE_EFFORT_LEVELS, ("low", "medium", "high", "xhigh", "max"))
        self.assertNotIn("minimal", CLAUDE_EFFORT_LEVELS)

    def test_codex_levels_exclude_max_include_minimal(self):
        self.assertEqual(CODEX_EFFORT_LEVELS, ("minimal", "low", "medium", "high", "xhigh"))
        self.assertNotIn("max", CODEX_EFFORT_LEVELS)

    def test_effort_levels_for_by_agent(self):
        self.assertEqual(effort_levels_for("claude"), CLAUDE_EFFORT_LEVELS)
        self.assertEqual(effort_levels_for("codex"), CODEX_EFFORT_LEVELS)
        # 未知 agent 走 Claude 集合
        self.assertEqual(effort_levels_for("custom"), CLAUDE_EFFORT_LEVELS)

    def test_normalize_effort_rejects_cross_provider_levels(self):
        # Claude 不接受 minimal；Codex 不接受 max
        self.assertEqual(normalize_effort("minimal", "claude"), "")
        self.assertEqual(normalize_effort("max", "codex"), "")
        # 各自接受合法等级
        self.assertEqual(normalize_effort("max", "claude"), "max")
        self.assertEqual(normalize_effort("minimal", "codex"), "minimal")
        # 三态关键字不受 provider 限制
        self.assertEqual(normalize_effort("inherit", "codex"), "inherit")
        self.assertEqual(normalize_effort("off", "claude"), "off")
        self.assertEqual(normalize_effort("none", "claude"), "off")
        # 大小写 / 中文别名归一化
        self.assertEqual(normalize_effort("MAX", "claude"), "max")
        self.assertEqual(normalize_effort("极高", "claude"), "xhigh")
        self.assertEqual(normalize_effort("极低", "codex"), "minimal")


class ResolveMemberEffortTests(_EffortIsolatedData):
    """三态解析：显式 / 关闭 / 继承 Agent 用户默认。"""

    def test_empty_args_do_not_raise(self):
        # 参数名 agent_kind 不遮蔽 agent_type 函数（回归：曾空参 TypeError）
        self.assertEqual(resolve_member_effort("t", "empty"), "high")
        self.assertEqual(resolve_member_effort("t", "ghost"), "high")

    def test_explicit_level_overrides_default(self):
        self.assertEqual(resolve_member_effort("t", "explicit"), "max")

    def test_explicit_off_disables_even_with_default(self):
        self.assertEqual(resolve_member_effort("t", "off"), "")

    def test_inherit_from_profile(self):
        self.assertEqual(resolve_member_effort("t", "inherit_claude"), "high")
        self.assertEqual(resolve_member_effort("t", "inherit_codex"), "minimal")

    def test_empty_effort_inherits(self):
        self.assertEqual(resolve_member_effort("t", "empty"), "high")

    def test_none_takeover_skips_default_fallback(self):
        self.assertEqual(resolve_member_effort("t", "none_member"), "")

    def test_legacy_profile_has_no_effort(self):
        self.assertEqual(resolve_member_effort("t", "legacy_member"), "")

    def test_codex_member_with_wrong_default_returns_empty(self):
        # 团队默认 profile 是 claude，codex 成员类型不匹配 → 不继承
        self.assertEqual(resolve_member_effort("t", "codex_wrong_default"), "")

    def test_codex_member_explicit_effort_uses_codex_levels(self):
        self.assertEqual(resolve_member_effort("t", "codex_effort"), "xhigh")

    def test_leader_is_also_a_member(self):
        self.assertEqual(resolve_member_effort("t", "leader"), "xhigh")

    def test_invalid_explicit_level_falls_back_to_inherit(self):
        # codex 成员显式 max 非法 → 归一化为空 → 走继承（但默认 profile 类型不匹配 → 空）
        self.assertEqual(resolve_member_effort("t", "codex_wrong_default"), "")


class LaunchArgMappingTests(_EffortIsolatedData):
    """Claude --effort / Codex -c model_reasoning_effort 注入。"""

    def test_claude_agent_args_effort(self):
        # effort 注入语义不变；身份 append flag 恒在（prompt 迁移 §8，双 builder 同步）
        args = claude_agent_args("claude", "manual", effort="max")
        idx = args.index("--effort")
        self.assertEqual(args[idx:idx + 2], ["--effort", "max"])
        self.assertIn("--append-system-prompt-file", args)
        # off / 非法等级（codex 的 minimal）不注入 --effort
        for e in ("off", "minimal"):
            args = claude_agent_args("claude", "manual", effort=e)
            self.assertNotIn("--effort", args)
            self.assertIn("--append-system-prompt-file", args)

    def test_codex_command_effort(self):
        self.assertEqual(
            codex_command("codex", "/tmp", effort="minimal"),
            ["codex", "-C", "/tmp", "-c", 'model_reasoning_effort="minimal"'],
        )
        # codex 不接受 max
        self.assertEqual(
            codex_command("codex", "/tmp", effort="max"),
            ["codex", "-C", "/tmp"],
        )

    def test_claude_agent_args_effort_with_other_flags(self):
        args = claude_agent_args(
            "claude", "auto", model="m1", settings_path="/s", effort="high")
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")
        self.assertIn("--permission-mode", args)  # 其他参数不受影响

    def test_codex_command_effort_with_model_and_prompt(self):
        cmd = codex_command("codex", "/tmp", prompt="do", member_mode="auto",
                            model="o3", effort="high")
        self.assertIn("-c", cmd)
        self.assertIn('model_reasoning_effort="high"', cmd)
        self.assertIn("--model", cmd)
        self.assertIn("--ask-for-approval", cmd)
        self.assertEqual(cmd[-1], "do")


class LeaderLaunchPathTests(_EffortIsolatedData):
    """leader 启动路径：解析 + 注入端到端。"""

    def test_leader_claude_resolve_and_inject(self):
        effort = resolve_member_effort("t", "leader", "claude")
        self.assertEqual(effort, "xhigh")
        args = claude_agent_args("claude", "manual", effort=effort)
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "xhigh")

    def test_leader_codex_resolve_and_inject(self):
        data = {"agent_users": _PROFILE["agent_users"],
                "teams": {"t2": {"default_agent": "codex",
                                 "members": {"leader": {"agent": "codex", "effort": "high"}}}}}
        save_data(data)
        effort = resolve_member_effort("t2", "leader", "codex")
        self.assertEqual(effort, "high")
        cmd = codex_command("codex", "/tmp", effort=effort)
        self.assertIn('model_reasoning_effort="high"', cmd)

    def test_leader_off_no_effort(self):
        data = {"agent_users": _PROFILE["agent_users"],
                "teams": {"t3": {"default_agent": "claude",
                                 "members": {"leader": {"agent": "claude", "effort": "off"}}}}}
        save_data(data)
        self.assertEqual(resolve_member_effort("t3", "leader"), "")
        args = claude_agent_args("claude", "manual", effort="")
        # off → 不注入 --effort；身份 append flag 恒在（prompt 迁移 §8）
        self.assertNotIn("--effort", args)
        self.assertIn("--append-system-prompt-file", args)


if __name__ == "__main__":
    unittest.main()
