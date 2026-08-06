"""
Agent 用户 / 成员 effort 推理强度 — provider 分离注入与覆盖测试
================================================================

大型任务"新增 Agent 用户默认 effort + 成员级 effort 覆盖"的回归锁定。

关键事实（本机 CLI 已确认）：
  - Claude Code 原生 `--effort <low|medium|high|xhigh|max>`；
  - Codex CLI 无独立 effort flag，经 `-c model_reasoning_effort="<level>"`
    覆盖 config.toml（本机已接受），等级 = minimal/low/medium/high/xhigh。

约束（本文件断言）：
  1. effort 等级按 provider 分离并校验：Codex 不得接受 max、Claude 不得接受
     minimal（normalize_effort / effort_levels_for / claude_agent_args /
     codex_command 双端一致）。
  2. resolve_member_effort 三态：成员显式级别 / "off" 关闭 / 继承 Agent 用户
     默认（profile.effort）；参数名 agent_kind 不遮蔽同名函数 agent_type
     （空参路径不得 TypeError）。
  3. 注入位置：member + leader spawn 均携带 effort（claude --effort、
     codex -c model_reasoning_effort）。
  4. TUI AgentUserEditDialog 初始化/保存按 provider 归一化，跨 provider 残留
     （如 codex 存 max）被清除，Select 构造期必有匹配选项。

数据隔离：经 data_layer.set_data_file 指向临时文件；spawn 测试仿
test_agent_user_integration.py 的 mock _tmux 方式，不启动真实 tmux。
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common.tmux_utils import (
    claude_agent_args,
    codex_command,
    effort_levels_for,
    normalize_effort,
    resolve_member_effort,
    CLAUDE_EFFORT_LEVELS,
    CODEX_EFFORT_LEVELS,
)

DATA = {
    "teams": {
        "t1": {
            "default_agent": "claude",
            "agent_users": {
                "c1": {"agent_type": "claude", "takeover_enabled": True,
                       "anthropic_model": "m", "effort": "high"},
                "x1": {"agent_type": "codex", "takeover_enabled": True,
                       "codex_model": "g", "effort": "xhigh"},
            },
            "default_agent_user": "c1",
            "members": {
                "alice": {"role": "coder", "agent": "claude"},
                "bob": {"role": "coder", "agent": "codex",
                        "agent_user": "x1", "effort": "minimal"},
                "carol": {"role": "coder", "agent": "claude",
                          "agent_user": "c1", "effort": "off"},
                "dave": {"role": "coder", "agent": "claude",
                         "agent_user": "c1", "effort": "max"},
                "erin": {"role": "coder", "agent": "codex",
                         "agent_user": "x1", "effort": "max"},  # 非法：codex 不接受 max
            },
        }
    }
}


class _DataIsolated(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        from common.data_layer import save_data
        save_data(DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()


# ============================================================
# A. provider 分离的等级集合与归一化
# ============================================================

class EffortLevelSetTests(unittest.TestCase):
    def test_level_sets_are_provider_specific(self):
        """Claude=low/medium/high/xhigh/max；Codex=minimal/low/medium/high/xhigh。"""
        self.assertEqual(CLAUDE_EFFORT_LEVELS,
                         ("low", "medium", "high", "xhigh", "max"))
        self.assertEqual(CODEX_EFFORT_LEVELS,
                         ("minimal", "low", "medium", "high", "xhigh"))
        self.assertIn("max", effort_levels_for("claude"))
        self.assertNotIn("max", effort_levels_for("codex"))
        self.assertIn("minimal", effort_levels_for("codex"))
        self.assertNotIn("minimal", effort_levels_for("claude"))

    def test_normalize_effort_rejects_cross_provider_levels(self):
        """Codex 不接受 max；Claude 不接受 minimal。"""
        self.assertEqual(normalize_effort("max", "claude"), "max")
        self.assertEqual(normalize_effort("max", "codex"), "")       # codex 拒绝 max
        self.assertEqual(normalize_effort("minimal", "codex"), "minimal")
        self.assertEqual(normalize_effort("minimal", "claude"), "")  # claude 拒绝 minimal
        # 未指定 provider 默认 Claude 集合
        self.assertEqual(normalize_effort("max"), "max")
        self.assertEqual(normalize_effort("minimal"), "")

    def test_normalize_effort_inherit_off_always_valid(self):
        """inherit / off 是成员级三态，任何 provider 均有效。"""
        self.assertEqual(normalize_effort("inherit", "claude"), "inherit")
        self.assertEqual(normalize_effort("inherit", "codex"), "inherit")
        self.assertEqual(normalize_effort("off", "claude"), "off")
        self.assertEqual(normalize_effort("off", "codex"), "off")


# ============================================================
# B. claude_agent_args / codex_command effort 注入
# ============================================================

class EffortArgInjectionTests(unittest.TestCase):
    def test_claude_agent_args_injects_effort(self):
        """claude 成员 effort=max → --effort max（Claude 原生 flag）。"""
        args = claude_agent_args("claude", "auto", effort="max")
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "max")

    def test_claude_agent_args_rejects_minimal(self):
        """Claude 不接受 minimal：不注入 --effort。"""
        args = claude_agent_args("claude", "auto", effort="minimal")
        self.assertNotIn("--effort", args)

    def test_claude_agent_args_empty_off_no_effort(self):
        """空 / off effort 不注入 --effort。"""
        for e in ("", "off", "inherit", None):
            args = claude_agent_args("claude", "auto", effort=e or "")
            self.assertNotIn("--effort", args)

    def test_codex_command_injects_reasoning_effort(self):
        """codex 成员 effort=minimal → -c model_reasoning_effort="minimal"。"""
        cmd = codex_command("codex", "/tmp", effort="minimal")
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1],
                         'model_reasoning_effort="minimal"')

    def test_codex_command_rejects_max(self):
        """Codex 不接受 max：不注入 -c model_reasoning_effort。"""
        cmd = codex_command("codex", "/tmp", effort="max")
        self.assertNotIn("model_reasoning_effort", cmd)

    def test_codex_command_empty_no_effort(self):
        """空 effort 不注入 -c。"""
        cmd = codex_command("codex", "/tmp", effort="")
        self.assertNotIn("model_reasoning_effort", cmd)


# ============================================================
# C. resolve_member_effort 三态 + 遮蔽修复
# ============================================================

class ResolveMemberEffortTests(_DataIsolated):
    def test_no_agent_kind_does_not_shadow_agent_type_function(self):
        """空参路径（不传 agent_kind）不崩溃：agent_type(agent) 函数被正确调用。

        回归锁定：参数曾名 agent_type，空参时 agent_type(agent) 会调用字符串
        → TypeError。改名 agent_kind 后此路径必须工作。
        """
        # alice 未显式指定 agent_user/effort → 继承团队默认 c1.effort=high
        self.assertEqual(resolve_member_effort("t1", "alice"), "high")

    def test_member_explicit_claude_max(self):
        """成员显式 claude effort=max → max（覆盖 Agent 用户默认）。"""
        self.assertEqual(resolve_member_effort("t1", "dave"), "max")

    def test_member_explicit_codex_minimal(self):
        """成员显式 codex effort=minimal → minimal（Codex 集合成员）。"""
        self.assertEqual(resolve_member_effort("t1", "bob"), "minimal")

    def test_member_explicit_codex_max_falls_back_to_inherit(self):
        """成员显式 codex effort=max（非法）→ 不注入，走继承路径。
        erin 显式 max 对 codex 非法 → normalize 为空 → 继承 profile x1.effort=xhigh。
        """
        self.assertEqual(resolve_member_effort("t1", "erin"), "xhigh")

    def test_member_off_suppresses_profile_default(self):
        """成员 effort=off → 显式关闭，即使 Agent 用户有默认 effort 也不注入。"""
        # carol: agent_user=c1(high) + effort=off → ""
        self.assertEqual(resolve_member_effort("t1", "carol"), "")

    def test_empty_member_name_uses_team_default(self):
        """空 member_name（如 leader 无成员记录）→ 按团队默认解析，不崩溃。"""
        self.assertEqual(resolve_member_effort("t1"), "high")

    def test_type_mismatch_not_inherited(self):
        """成员 agent 与 profile 类型不匹配 → 不继承 effort。"""
        # 构造：claude 成员指向 codex profile x1（x1.effort=xhigh）
        with tempfile.TemporaryDirectory() as d:
            from common.data_layer import save_data
            d2 = Path(d) / "teams_data.json"
            data_layer.set_data_file(d2)
            save_data({"teams": {"t": {
                "default_agent": "claude",
                "agent_users": {"x1": {"agent_type": "codex", "effort": "xhigh"}},
                "members": {"m1": {"agent": "claude", "agent_user": "x1"}},
            }}})
            self.assertEqual(resolve_member_effort("t", "m1"), "")

    def test_legacy_profile_no_type_not_inherited(self):
        """legacy profile（无 agent_type）→ 不继承 effort。"""
        with tempfile.TemporaryDirectory() as d:
            from common.data_layer import save_data
            d2 = Path(d) / "teams_data.json"
            data_layer.set_data_file(d2)
            save_data({"teams": {"t": {
                "default_agent": "claude",
                "agent_users": {"legacy": {"effort": "high"}},  # 无 agent_type
                "members": {"m1": {"agent": "claude", "agent_user": "legacy"}},
            }}})
            self.assertEqual(resolve_member_effort("t", "m1"), "")

    def test_missing_profile_not_inherited(self):
        """profile 不存在 → 不继承 effort。"""
        with tempfile.TemporaryDirectory() as d:
            from common.data_layer import save_data
            d2 = Path(d) / "teams_data.json"
            data_layer.set_data_file(d2)
            save_data({"teams": {"t": {
                "default_agent": "claude",
                "agent_users": {"c1": {"agent_type": "claude", "effort": "high"}},
                "members": {"m1": {"agent": "claude", "agent_user": "ghost"}},
            }}})
            self.assertEqual(resolve_member_effort("t", "m1"), "")


# ============================================================
# D. spawn 端到端：member + leader 启动路径携带 effort
# ============================================================

class EffortSpawnInjectionTests(_DataIsolated):
    """真实 spawn 路径（mock _tmux）：new-window 命令携带 effort 参数。"""

    def setUp(self):
        super().setUp()
        import mult_agent_mcp as mcp
        self.mcp = mcp
        self.old_mcp_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR, "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE, "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
        }
        self.old_mcp_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux_window_exists": mcp._tmux_window_exists,
            "_tmux": mcp._tmux,
        }
        # 关键：mcp._save / _load 与 data_layer 读取共用同一数据文件，
        # 否则 resolve_member_effort（走 data_layer）读不到 mcp._save 写入的数据。
        mcp.DATA_FILE = str(self.data_file)

    def tearDown(self):
        for k, v in self.old_mcp_globals.items():
            setattr(self.mcp, k, v)
        for k, v in self.old_mcp_funcs.items():
            setattr(self.mcp, k, v)
        super().tearDown()

    def _spawn(self, team: dict, member: str, agent: str) -> list[str]:
        """保存团队数据 → spawn 指定成员 → 返回 new-window 命令列表。"""
        mcp = self.mcp
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        mcp._save({"teams": {"team": team}})
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

    def test_member_claude_spawn_injects_effort_flag(self):
        """claude 成员显式 effort=high → new-window 命令含 --effort high。"""
        team = {
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "members": {"alice": {"role": "coder", "agent": "claude",
                                   "effort": "high"}},
        }
        cmd = self._spawn(team, "alice", "claude")
        joined = " ".join(cmd)
        self.assertIn("--effort", joined, f"claude spawn 应注入 --effort: {joined}")
        idx = joined.split(" ").index("--effort")
        self.assertEqual(joined.split(" ")[idx + 1], "high")

    def test_member_codex_spawn_injects_reasoning_effort(self):
        """codex 成员 effort=minimal → new-window 命令含 -c model_reasoning_effort。"""
        team = {
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "codex",
            "members": {"bob": {"role": "coder", "agent": "codex",
                                "effort": "minimal"}},
        }
        cmd = self._spawn(team, "bob", "codex")
        joined = " ".join(cmd)
        self.assertIn("model_reasoning_effort", joined,
                      f"codex spawn 应注入 model_reasoning_effort: {joined}")
        self.assertIn('model_reasoning_effort="minimal"', joined)

    def test_member_inherit_spawn_uses_profile_default(self):
        """成员未设 effort → 继承 Agent 用户默认 high → spawn 注入 --effort high。"""
        team = {
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "agent_users": {"c1": {"agent_type": "claude",
                                   "anthropic_model": "m", "effort": "high"}},
            "default_agent_user": "c1",
            "members": {"carol": {"role": "coder", "agent": "claude"}},
        }
        cmd = self._spawn(team, "carol", "claude")
        self.assertIn("--effort high", " ".join(cmd))

    def test_leader_effort_resolved_and_injected(self):
        """leader 启动路径：leader 成员记录 effort=high → resolve 返回 high，
        且 claude_agent_args 注入 --effort high（MCP leader launch 用同一构造器）。"""
        mcp = self.mcp
        # leader 记录在成员表；直接用 MCP 的 launch 构造组件验证组合。
        team = {
            "workspace_dir": str(self.root / "workspace"),
            "default_agent": "claude",
            "members": {"lead": {"role": "leader", "agent": "claude",
                                 "effort": "high"}},
        }
        self.mcp._save({"teams": {"team": team}})
        effort = resolve_member_effort("team", "lead", "claude")
        self.assertEqual(effort, "high")
        args = mcp._claude_agent_args("claude", "auto", effort=effort)
        self.assertIn("--effort", args)
        self.assertEqual(args[args.index("--effort") + 1], "high")

    def test_mcp_and_tmux_utils_constructors_consistent(self):
        """MCP 侧与 tmux_utils 侧构造器对 effort 注入行为一致（双源同步）。"""
        import mult_agent_mcp as mcp
        self.assertEqual(
            mcp._claude_agent_args("claude", "auto", effort="max"),
            claude_agent_args("claude", "auto", effort="max"))
        self.assertEqual(
            mcp._claude_agent_args("claude", "auto", effort="minimal"),
            claude_agent_args("claude", "auto", effort="minimal"))
        self.assertEqual(
            mcp._codex_command("codex", "/tmp", effort="minimal"),
            codex_command("codex", "/tmp", effort="minimal"))
        self.assertEqual(
            mcp._codex_command("codex", "/tmp", effort="max"),
            codex_command("codex", "/tmp", effort="max"))


# ============================================================
# E. TUI AgentUserEditDialog 初始化 / 保存 / provider 切换
# ============================================================

class AgentUserEditEffortTests(_DataIsolated):
    def _dialog(self, **kw) -> object:
        from tui.tui_dialogs import AgentUserEditDialog
        defaults = dict(user_key="p", agent_type="claude", takeover_enabled=True)
        defaults.update(kw)
        return AgentUserEditDialog(**defaults)

    def test_init_normalizes_cross_provider_effort(self):
        """codex profile 存了 max（非法）→ 初始化为 ""，不崩溃。"""
        dlg = self._dialog(agent_type="codex", effort="max")
        self.assertEqual(dlg._effort, "")

    def test_init_keeps_valid_provider_effort(self):
        """codex profile effort=minimal → 保留；claude effort=max → 保留。"""
        dlg_c = self._dialog(agent_type="codex", effort="minimal")
        self.assertEqual(dlg_c._effort, "minimal")
        dlg_x = self._dialog(agent_type="claude", effort="max")
        self.assertEqual(dlg_x._effort, "max")

    def test_init_legacy_profile_defaults_to_claude_levels(self):
        """legacy profile（agent_type=""）初始按 Claude 集合归一化。"""
        dlg = self._dialog(user_key="legacy", agent_type="", effort="max")
        self.assertEqual(dlg._effort, "max")
        dlg2 = self._dialog(user_key="legacy", agent_type="", effort="minimal")
        self.assertEqual(dlg2._effort, "")  # claude 集合拒绝 minimal

    def test_provider_switch_updates_effort_choices(self):
        """provider 切换时 effort Select 选项更新为对应 provider 集合。"""
        from textual.app import App
        from tui.tui_dialogs import AgentUserEditDialog, _effort_value_for

        app = App()
        dialog = AgentUserEditDialog()  # 新建，provider 未选
        async def run():
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.2)
                sel = dialog.query_one("#effort")
                values = [o[1] for o in sel._options]
                # 初始（未选 provider）→ Claude 集合
                self.assertIn("max", values)
                self.assertNotIn("minimal", values)
                # 切到 codex → 更新为 codex 集合
                dialog.query_one("#agent_type").value = "codex"
                await pilot.pause(0.2)
                values2 = [o[1] for o in dialog.query_one("#effort")._options]
                self.assertIn("minimal", values2)
                self.assertNotIn("max", values2)
        import asyncio
        asyncio.run(run())

    def test_save_normalizes_effort_by_provider(self):
        """保存时按 provider 归一化：非法值（如 codex 的 max）被清除为 ""。"""
        from tui.tui_dialogs import agent_user_effort_value_for
        self.assertEqual(agent_user_effort_value_for("codex", "max"), "")
        self.assertEqual(agent_user_effort_value_for("codex", "minimal"), "minimal")
        self.assertEqual(agent_user_effort_value_for("claude", "minimal"), "")
        self.assertEqual(agent_user_effort_value_for("claude", "max"), "max")


if __name__ == "__main__":
    unittest.main()
