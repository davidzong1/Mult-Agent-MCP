"""
TUI 成员终端 spawn —— system prompt 权威源回归（prompts/*.ts，非默认模板）
==========================================================================

独立链路审计补充（refactor-claude）：任务关注「TUI 启动团队成员终端时，system
prompt 是否仍落到 mult_agent_mcp.py 默认模板、未使用 prompts/ 下 TypeScript 模板」。

审计结论：TUI ``launch_terminals`` 成员创建/恢复均经
``prompt_registry.claude_identity_file(team_name, name)`` 渲染 ``prompts/members.ts``
``memberSystemPrompt``（@channel system），经 ``--append-system-prompt-file`` 进真实
system 层；leader 走 ``prompts/leader.ts``；codex 成员走团队 AGENTS.md 角色中立段。
``default_claude_identity_path()`` 占位默认仅在 builder 未传身份文件或写临时文件异常时
触达——生产 spawn 点（tui_screens:679/1266、mult_agent_mcp:3989、tmux_utils:786）均显式
传身份文件，不触达。

既有测试覆盖 MCP 侧 ``_tmux_spawn_member`` 的身份文件接线，但**未覆盖 TUI 侧
``launch_terminals`` 成员 spawn 的 append 文件内容来自 .ts**。本文件补齐该缺口：
对 ``tui_screens.launch_terminals`` 全链路 mock，捕获成员/leader 窗口 spawn 命令，
断言 ``--append-system-prompt-file`` 指向的内容来自 prompts/*.ts（含身份绑定锚点），
且**不是** ``prompt_registry`` 默认占位文本、不是内建内联回退。

隔离：``data_layer.set_data_file`` 指向临时数据文件（conftest 亦隔离 MCP_HOME），
不触真实 teams_data.json / 仓库根。只写本文件，不编辑生产代码。
"""

import json
import os
import stat
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from common import data_layer
from common import prompt_registry as pr
from tui import tui_screens as ts


class TuiMemberSpawnTsSourceTests(unittest.TestCase):
    """TUI launch_terminals 成员/leader spawn —— append 文件内容来自 prompts/*.ts。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.ctx = self.root / "ctx"
        self.ctx.mkdir()
        self.calls: list[list[str]] = []
        # 隔离修复（tester-claude 2026-08-14）：成员窗口存在性判定 member_window_state
        # 经 common.tmux_utils 的**真实 tmux_run**（非本测试 mock 的 ts._tmux_run），
        # 使用固定会话名 'mcp_team' 会与真实 tmux 会话状态碰撞——一旦真实 mcp_team
        # 会话中已存在同名成员窗口，member_window_state 判定为 live 即跳过创建，
        # 导致断言捕获不到 new-window 命令（全量套件顺序依赖根因）。
        # 改用实例级唯一会话名：真实 has-session 必然 rc!=0 → 判定 absent → 创建，
        # 保留真实 member_window_state 语义且不依赖外部 tmux 状态。
        self.session = f"mcp_team_{uuid.uuid4().hex[:6]}"

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    # ---- helpers ----

    def _save_team(self, members):
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "claude",
            "members": members,
        }}})

    def _fake_tmux_run(self, cmd, timeout=10):
        self.calls.append(cmd)
        if cmd[0] == "-V":
            return 0, "tmux 3.2", ""
        if cmd[0] == "has-session":
            return 1, "", ""
        if cmd[0] == "list-windows":
            return 0, "$1\t1000\t@1\t__base", ""
        return 0, "", ""

    def _patches(self):
        """mock launch_terminals 的全部副作用，仅捕获 tmux 命令。"""
        return [
            mock.patch.object(ts, "_tmux_run", side_effect=self._fake_tmux_run),
            mock.patch.object(ts, "_tmux_session", return_value=self.session),
            mock.patch.object(ts, "configure_claude_mcp", return_value=(True, "")),
            mock.patch.object(ts, "configure_codex_mcp", return_value=(True, "")),
            mock.patch.object(ts, "write_claude_permissions", return_value=""),
            mock.patch.object(
                ts, "claude_agent_user_launch",
                return_value=("", str(self.ws / ".claude" / "settings.json")),
            ),
            mock.patch.object(ts.classifier_fallback, "claude_terminal_allow_tools",
                              return_value=[]),
            mock.patch.object(ts, "get_agent_user_env_prefix", return_value=[]),
            mock.patch.object(ts, "get_proxy_env_prefix", return_value=[]),
            mock.patch.object(ts, "merge_env_prefixes", return_value=[]),
            mock.patch.object(ts, "resolve_agent_model", return_value=""),
            mock.patch.object(ts, "resolve_member_effort", return_value=""),
            mock.patch.object(ts, "_leader_terminal_restart_blocked", return_value=False),
            mock.patch.object(ts, "_record_leader_reentry", return_value=None),
            mock.patch.object(ts, "_remember_member_window_id", return_value=""),
            mock.patch.object(ts, "_inject_claude_leader_prompt", return_value=(0, "")),
            mock.patch.object(ts.time, "sleep", return_value=None),
        ]

    def _launch(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            ok, msg = ts.launch_terminals("team")
        finally:
            for p in patches:
                p.stop()
        self.assertTrue(ok, f"launch_terminals 失败: {msg}")
        return self.calls

    def _window_append_file(self, window_name: str):
        """返回指定窗口 spawn 命令的 --append-system-prompt-file 路径。"""
        for cmd in self.calls:
            if cmd and cmd[0] in ("new-session", "new-window") and "-n" in cmd:
                if cmd[cmd.index("-n") + 1] == window_name:
                    if "--append-system-prompt-file" in cmd:
                        idx = cmd.index("--append-system-prompt-file")
                        return cmd[idx + 1]
                    return None
        self.fail(f"未找到窗口 '{window_name}' 的 spawn 命令: {self.calls}")

    # ---- 用例 ----

    def test_tui_member_spawn_append_file_from_members_ts(self):
        """TUI 成员窗口 append 文件内容来自 prompts/members.ts（非默认占位/内联回退）。"""
        self._save_team({
            "lead": {"role": "leader", "agent": "claude"},
            "alice": {"role": "coder", "agent": "claude"},
        })
        self._launch()
        ap = self._window_append_file("alice")
        self.assertIsNotNone(ap, "TUI 成员 spawn 必须携带 --append-system-prompt-file")
        content = Path(ap).read_text(encoding="utf-8")
        # 权威源锚点（members.ts memberSystemPrompt @channel system）
        self.assertIn("你的团队成员身份绑定", content)
        self.assertIn("member_name='alice'", content)
        self.assertIn("role='coder'", content)
        # 绝非默认占位模板 / 内建内联回退
        self.assertNotIn("身份文件占位默认值", content)
        # 成员身份文件 0600（防共享区可写注入面）
        mode = stat.S_IMODE(os.stat(ap).st_mode)
        self.assertEqual(mode & 0o077, 0, f"临时身份文件权限过宽: {oct(mode)}")

    def test_tui_leader_spawn_append_file_from_leader_ts(self):
        """TUI leader 窗口 append 文件内容来自 prompts/leader.ts leaderSystemPrompt。"""
        self._save_team({
            "lead": {"role": "leader", "agent": "claude"},
            "alice": {"role": "coder", "agent": "claude"},
        })
        self._launch()
        ap = self._window_append_file("lead")
        self.assertIsNotNone(ap, "TUI leader spawn 必须携带 --append-system-prompt-file")
        content = Path(ap).read_text(encoding="utf-8")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的 leader", content)
        self.assertIn("member_name='lead'", content)
        self.assertNotIn("身份文件占位默认值", content)

    def test_tui_member_spawn_edits_to_members_ts_visible(self):
        """编辑生效：改 members.ts → TUI 新 spawn 的 append 文件反映编辑（F2/G2）。"""
        self._save_team({
            "lead": {"role": "leader", "agent": "claude"},
            "alice": {"role": "coder", "agent": "claude"},
        })
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "members.ts").write_text(
            "/** @channel system */\n"
            "export function memberSystemPrompt(v: any): string {\n"
            "  return `TUI-EDIT-MARKER team='${v.teamName}' member='${v.memberName}'`;\n"
            "}\n",
            encoding="utf-8",
        )
        with mock.patch.object(pr, "_prompts_dir", lambda: prompts):
            self._launch()
            ap = self._window_append_file("alice")
            self.assertIsNotNone(ap)
            content = Path(ap).read_text(encoding="utf-8")
            self.assertIn("TUI-EDIT-MARKER", content, "TUI 新 spawn append 文件应反映 TS 编辑")
            self.assertIn("member='alice'", content)
            self.assertNotIn("身份文件占位默认值", content)

    def test_tui_codex_member_writes_role_neutral_agents_md(self):
        """TUI codex 成员 spawn：身份固化到团队 AGENTS.md 角色中立段（非默认模板）。"""
        self._save_team({
            "lead": {"role": "leader", "agent": "claude"},
            "bob": {"role": "coder", "agent": "codex"},
        })
        self._launch()
        agents_md = self.ws / "AGENTS.md"
        self.assertTrue(agents_md.exists(), "TUI codex 成员 spawn 应写入 AGENTS.md")
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn("Multi-Agent MCP 团队", content)
        # 角色中立（B2）：共享文件不得写死具体成员
        self.assertNotIn("member_name='bob'", content)
        self.assertNotIn("身份文件占位默认值", content)


if __name__ == "__main__":
    unittest.main()
