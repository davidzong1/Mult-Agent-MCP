"""
prompt_registry —— 身份注入链路的聚焦单测（coder-claude 新增回归覆盖）。

覆盖 fact-check §8 已确认技术路线在本模块的落地出口：
  - render_member_identity：纯文本（不采用 [system] 伪标签），身份来自数据层；
  - claude_identity_file：临时文件真实存在 + 内容含 team/member/role/agent
    （R3 生命周期：mkstemp 私有文件，atexit 清理）；
  - default_claude_identity_path：确定性默认路径（双 builder 相等依赖它）；
  - codex_agents_md / ensure_codex_agents_md：团队中立（不写死 member_name）、
    幂等追加、Codex 唯一自动装载持久指令文件（抗 compact/resume）；
  - 双 builder 同步：mcp._claude_agent_args == tmux_utils.claude_agent_args。

数据隔离：经 common.data_layer.set_data_file() 指向临时文件，不触真实
~/.mult_agent_mcp（镜像 test_prompt_identity_system_layer 的隔离 setUp）。
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer, tmux_utils as tu
from common import prompt_registry as pr


class _IsolatedRegistry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(str(self.data_file))
        self._save({
            "teams": {
                "team": {
                    "workspace_dir": str(self.root / "ws"),
                    "context_dir": str(self.root / "ctx"),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "default_agent": "claude",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                        "bob": {"role": "coder", "agent": "codex"},
                        "carol": {"role": "tester", "agent": "codex"},
                    },
                }
            }
        })

    def tearDown(self):
        if self.old_override is not None:
            data_layer.set_data_file(self.old_override)
        else:
            # 无历史覆盖：回落 conftest 隔离默认（MULTI_AGENT_MCP_HOME 下，
            # set_data_file 不接受 None，须传有效路径）。
            data_layer.set_data_file(data_layer.DATA_FILE)
        self.tmp.cleanup()

    def _save(self, data: dict) -> None:
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f)


class RenderMemberIdentityTests(_IsolatedRegistry):
    def test_member_identity_plain_text_no_system_pseudo_tag(self):
        text = pr.render_member_identity("team", "alice")
        self.assertNotIn("[系统]", text)
        self.assertNotIn("[system]", text)

    def test_member_identity_binds_data_layer_fields(self):
        text = pr.render_member_identity("team", "alice")
        for anchor in ("team", "alice", "coder", "claude", "你的团队成员身份绑定"):
            self.assertIn(anchor, text, f"身份段缺锚点 {anchor!r}")

    def test_member_identity_carries_delivery_contract_and_report_first(self):
        # 措辞必须与生产 _member_delivery_contract / _member_report_first_rule 对齐
        text = pr.render_member_identity("team", "alice")
        self.assertIn("member_report_result", text)
        self.assertIn("先回报", text)


class ClaudeIdentityFileTests(_IsolatedRegistry):
    def test_identity_file_exists_and_0600(self):
        path = pr.claude_identity_file("team", "alice")
        self.assertTrue(Path(path).exists(), f"append 文件不存在: {path}")
        # mkstemp 私有文件：无 group/other 权限（防共享区可写注入面）
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode & 0o077, 0, f"临时身份文件权限过宽: {oct(mode)}")

    def test_identity_file_content_from_data_layer(self):
        path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        for anchor in ("team", "alice", "coder", "claude"):
            self.assertIn(anchor, content)

    def test_leader_identity_file_renders_leader_not_member(self):
        path = pr.claude_identity_file("team", "lead", leader=True)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("leader", content)
        self.assertIn("lead", content)


class DefaultPathTests(_IsolatedRegistry):
    def test_default_path_deterministic_and_exists(self):
        p1 = pr.default_claude_identity_path()
        p2 = pr.default_claude_identity_path()
        self.assertEqual(p1, p2, "默认身份路径必须确定性（双 builder 相等依赖）")
        self.assertTrue(Path(p1).exists(), f"默认身份文件应存在: {p1}")


class CodexAgentsMdTests(_IsolatedRegistry):
    def test_codex_agents_md_team_neutral(self):
        md = pr.codex_agents_md("team")
        self.assertIn("team", md)
        self.assertIn("Multi-Agent", md)
        # 共享 AGENTS.md 不得绑定具体成员/角色（防串线 B2）
        for name in ("alice", "bob", "carol"):
            self.assertNotIn(f"member_name='{name}'", md)

    def test_ensure_codex_agents_md_idempotent(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        path = pr.ensure_codex_agents_md("team", str(workspace))
        self.assertTrue(Path(path).exists())
        first = Path(path).read_text(encoding="utf-8")
        pr.ensure_codex_agents_md("team", str(workspace))  # 二次调用不得重复追加
        second = Path(path).read_text(encoding="utf-8")
        self.assertEqual(first, second, "AGENTS.md 重复追加")


class CodexAgentsMdSafetyTests(_IsolatedRegistry):
    """reviewer P1：AGENTS.md 写入落点安全（fact-check §7 B3，不得污染用户仓库根）。

    安全规则：仅写入团队**显式 workspace_dir** 且非项目根；无 workspace_dir（回落
    仓库根）或 workspace==项目根时 fail-closed 零写入。Codex 从启动 cwd 发现
    AGENTS.md，写入落点必须与 spawn cwd 一致。
    """

    _PROJECT_ROOT = str(Path(pr.__file__).resolve().parent.parent)

    def _save_team_variant(self, workspace_dir=None):
        team = {
            "context_dir": str(self.root / "ctx"),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "codex",
            "members": {"lead": {"role": "leader", "agent": "codex"}},
        }
        if workspace_dir is not None:
            team["workspace_dir"] = workspace_dir
        self._save({"teams": {"team": team}})

    def _repo_root_agents_md_state(self):
        p = Path(self._PROJECT_ROOT) / "AGENTS.md"
        return (p.read_text(encoding="utf-8") if p.exists() else None)

    def test_no_workspace_dir_fails_closed_no_repo_root_write(self):
        self._save_team_variant(workspace_dir=None)
        before = self._repo_root_agents_md_state()
        # 模拟 _tmux_spawn_member 传回的回落 team_dir = 项目根
        result = pr.ensure_codex_agents_md("team", self._PROJECT_ROOT)
        self.assertEqual(result, "", "无 workspace_dir 必须 fail-closed（返回空）")
        after = self._repo_root_agents_md_state()
        self.assertEqual(after, before, "项目根 AGENTS.md 不得被产生/改写")

    def test_workspace_equal_project_root_fails_closed(self):
        self._save_team_variant(workspace_dir=self._PROJECT_ROOT)
        before = self._repo_root_agents_md_state()
        result = pr.ensure_codex_agents_md("team", self._PROJECT_ROOT)
        self.assertEqual(result, "", "workspace==项目根必须 fail-closed")
        after = self._repo_root_agents_md_state()
        self.assertEqual(after, before, "项目根 AGENTS.md 不得被改写")

    def test_explicit_workspace_writes_agents_md(self):
        ws = self.root / "ws_explicit"
        ws.mkdir()
        self._save_team_variant(workspace_dir=str(ws))
        path = pr.ensure_codex_agents_md("team", str(ws))
        self.assertTrue(path and Path(path).exists(), f"应写入 AGENTS.md: {path}")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("team", content)
        self.assertIn("Multi-Agent", content)

    def test_existing_agents_md_user_content_preserved(self):
        ws = self.root / "ws_preserve"
        ws.mkdir()
        self._save_team_variant(workspace_dir=str(ws))
        user_text = "# 我的个人 Codex 规范\n- 不要动我的配置\n"
        (ws / "AGENTS.md").write_text(user_text, encoding="utf-8")
        pr.ensure_codex_agents_md("team", str(ws))
        content = (ws / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn(user_text, content, "用户已有 AGENTS.md 内容必须保留")
        # 二次调用不重复追加
        pr.ensure_codex_agents_md("team", str(ws))
        self.assertEqual(content, (ws / "AGENTS.md").read_text(encoding="utf-8"))


class DualBuilderSyncTests(_IsolatedRegistry):
    def test_both_builders_carry_append_flag_and_are_equal(self):
        for mode in ("auto", "plan", "manual", ""):
            a = mcp._claude_agent_args("claude", mode)
            b = tu.claude_agent_args("claude", mode)
            self.assertEqual(a, b, f"双 builder 漂移 mode={mode!r}")
            self.assertIn("--append-system-prompt-file", a)

    def test_builder_explicit_file_wins(self):
        path = pr.claude_identity_file("team", "alice")
        args = mcp._claude_agent_args("claude", "auto", append_system_prompt_file=path)
        idx = args.index("--append-system-prompt-file")
        self.assertEqual(args[idx + 1], path)


if __name__ == "__main__":
    unittest.main()
