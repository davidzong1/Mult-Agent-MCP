"""
prompt 注入有效性·角色维度最小诊断（coder-claude 验收自证）
================================================================

验收任务：验证上一闭环的 system prompt 注入有效性，范围覆盖
reviewer/coder/refactor/tester 四类成员 + leader 身份。

既有套件（test_prompt_identity_system_layer / test_quota_switch_*）已覆盖
"leader vs 成员不串号"与 coder 角色锚点，但**未逐角色断言**四类成员。本文件
补最小诊断（只读验证，不改生产），断言可观察出口：

  A. 四角色 append 身份文件逐角色正确渲染，且互不串线（member_x 不得含
     member_y 的 role）；
  B. 首启上下文 / 恢复上下文逐角色正确（system 层外的 user 层双保险）；
  C. leader 身份段与成员身份段边界：leader 不得混入成员否定措辞；
  D. Codex AGENTS.md 角色中立（共享文件不绑定任何具体角色）。

断言只验可观察出口，不锁内部实现（与既有验收契约风格一致）。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer, prompt_registry

ROLES = ["reviewer", "coder", "refactor", "tester"]


class _IsolatedRoles(unittest.TestCase):
    """隔离团队数据 + tmux mock（镜像 test_prompt_identity_system_layer）。"""

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
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD")
        }
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for name, val in self.old_globals.items():
            setattr(mcp, name, val)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, *, members, codex_member=False):
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        context = self.root / "ctx"
        context.mkdir(exist_ok=True)
        default_agent = "codex" if codex_member else "claude"
        lead_agent = "codex" if codex_member else "claude"
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "default_agent": default_agent,
                    "members": {
                        "lead": {"role": "leader", "agent": lead_agent},
                        **members,
                    },
                }
            }
        })
        return workspace, context

    def _spawn(self, name, agent, workspace):
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\t__base", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(
                mcp, "_write_claude_permissions",
                return_value=str(workspace / ".claude" / "settings.json"),
            ):
                mcp._tmux_spawn_member("mcp_team", name, agent, str(workspace))
        return [c for c in calls if c[0] in {"new-session", "new-window"}]

    @staticmethod
    def _append_path(spawn_cmds):
        for cmd in spawn_cmds:
            if "--append-system-prompt-file" in cmd:
                idx = cmd.index("--append-system-prompt-file")
                return Path(cmd[idx + 1])
        return None


class FourRoleIdentityRenderingTests(_IsolatedRoles):
    """A/C/D：四角色 append 身份文件逐角色正确、互不串线；leader 边界。"""

    def test_four_roles_render_own_identity_no_cross_leak(self):
        members = {
            f"m{i}": {"role": role, "agent": "claude"}
            for i, role in enumerate(ROLES)
        }
        ws, _ = self._save_team(members=members)
        contents = {}
        for name, info in members.items():
            spawn = self._spawn(name, info["agent"], ws)
            path = self._append_path(spawn)
            self.assertIsNotNone(path, f"{name} spawn 未携带 append flag")
            self.assertTrue(path.exists(), f"{name} append 文件应已生成")
            contents[name] = path.read_text(encoding="utf-8")
        # 逐角色：自己的 role 命中，其他角色不出现（防跨角色串线）
        for i, name in enumerate(contents):
            role = ROLES[i]
            self.assertIn(f"role='{role}'", contents[name],
                          f"{name} 身份文件缺自身 role='{role}'")
            self.assertIn(f"member_name='{name}'", contents[name])
            for other_role in ROLES:
                if other_role == role:
                    continue
                self.assertNotIn(f"role='{other_role}'", contents[name],
                                 f"{name} 身份文件混入其他角色 role='{other_role}'")
            # 成员段：含"你不是 leader"；不含 leader 段标记
            self.assertIn("你不是 leader", contents[name])
            self.assertNotIn("的 leader。", contents[name])

    def test_leader_identity_boundary_no_member_phrase(self):
        ws, _ = self._save_team(members={"alice": {"role": "coder", "agent": "claude"}})
        lead_spawn = self._spawn("lead", "claude", ws)
        path = self._append_path(lead_spawn)
        self.assertIsNotNone(path)
        content = path.read_text(encoding="utf-8")
        self.assertIn("的 leader", content)
        self.assertIn("role='leader'", content)
        self.assertIn("member_name='lead'", content)
        # 边界：leader 段不得混入成员段专有否定措辞 / 成员绑定段
        self.assertNotIn("你不是 leader", content)
        self.assertNotIn("你的团队成员身份绑定", content)

    def test_codex_agents_md_role_neutral_across_four_roles(self):
        members = {
            f"c{i}": {"role": role, "agent": "codex"}
            for i, role in enumerate(ROLES)
        }
        ws, _ = self._save_team(members=members, codex_member=True)
        for name, info in members.items():
            self._spawn(name, info["agent"], ws)
        agents_md = ws / "AGENTS.md"
        self.assertTrue(agents_md.exists(), "Codex 成员启动应写 AGENTS.md")
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn("Multi-Agent MCP 团队 'team'", content)
        # 共享文件不得绑定任何具体角色（多角色串线面 B2）
        for role in ROLES:
            self.assertNotIn(f"role='{role}'", content,
                             f"AGENTS.md 不得绑定具体角色 {role}")
        for name in members:
            self.assertNotIn(f"member_name='{name}'", content,
                             f"AGENTS.md 不得绑定具体成员 {name}")


class RuntimeContextsRoleTests(_IsolatedRoles):
    """B：首启上下文 / 恢复上下文逐角色正确（user 层双保险）。"""

    def test_initial_and_recovery_context_per_role(self):
        members = {
            f"m{i}": {"role": role, "agent": "claude",
                      "last_task": f"task for {role}", "last_task_completed": False}
            for i, role in enumerate(ROLES)
        }
        self._save_team(members=members)
        for i, name in enumerate(members):
            role = ROLES[i]
            init = mcp._build_member_initial_context("team", name)
            self.assertIn(f"member_name='{name}'", init)
            self.assertIn(f"role='{role}'", init)
            self.assertIn("你的团队成员身份绑定", init)
            # 首启上下文也含交付合约 + 顺序义务（闭环回报义务注入面）
            self.assertIn("member_report_result", init)
            recovery = mcp._build_recovery_context("team", name)
            self.assertIn(f"member_name='{name}'", recovery)
            self.assertIn(f"role='{role}'", recovery)
            for other_role in ROLES:
                if other_role != role:
                    self.assertNotIn(f"role='{other_role}'", recovery)

    def test_prompt_registry_single_source_per_role(self):
        members = {f"m{i}": {"role": role, "agent": "claude"}
                   for i, role in enumerate(ROLES)}
        self._save_team(members=members)
        for i, name in enumerate(members):
            text = prompt_registry.render_member_identity("team", name)
            self.assertIn(f"role='{ROLES[i]}'", text)
            self.assertIn(f"member_name='{name}'", text)
            self.assertIn("你不是 leader", text)


if __name__ == "__main__":
    unittest.main()
