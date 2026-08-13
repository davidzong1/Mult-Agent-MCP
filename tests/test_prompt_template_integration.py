"""
独立集成测试 —— prompts/*.ts 运行时权威模板源 + 通道路由（R1 A1-G3）
========================================================================

共同基线：docs/system_prompt_injection_audit.md + 共享上下文
``prompts-ts-authority-acceptance-checklist.md``（A1–F3 逐项验收清单）。
本文件是 **tester 独占**集成切片（与 coder 解析器单测 / reviewer 复审互不重叠），
只经 **parser / prompt_registry 公共接口** 验证可观察出口，不锁内部实现细节。

覆盖（对应验收清单 R1 A1-G3）：
  A. 无需 Node（任务硬约束）：
     render/身份文件路径纯 Python，不 spawn node/tsc/ts-node/npx；
     spawn 命令不含 Node 运行时。
  B. system/user 通道不混淆（C1/C2/C3/C4）：
     system 段只经 Claude append 文件 / Codex AGENTS.md（角色中立）；
     send-keys 首启/恢复为 user 角色，用诚实通道名（[成员上下文]/[恢复通知]）
     不伪称 system；@channel 缺失默认 user（fail-safe）；system 函数禁动态字段。
  C. 坏模板/缺文件（A4/B1/B2/B4）：
     缺文件/坏模板 → parser 抛携带文件+原因的清晰错误；
     registry 层安全回退内建非空身份文本（不崩、不静默丢身份）。
  D. TS 修改新会话可见（F1/F2/G2）：
     render_member_identity 输出来自 prompts/members.ts memberSystemPrompt（权威源）；
     mtime 键控缓存：改 TS → 下次渲染立即生效；改 TS → 新会话身份文件反映编辑。
  E. 现有 Claude/Codex identity 断言（E2/A5）：
     claude_identity_file 0600 + 内容绑定身份；leader 身份来自 leader.ts；Codex
     AGENTS.md 角色中立 + 缺 workspace fail-closed。

边界：只写本文件；不编辑 parser/registry/prompts/mult_agent_mcp.py；
不 commit；不触碰 cppipc-dds 等其他团队状态。隔离镜像
test_prompt_identity_system_layer（data_layer.set_data_file + mcp 全局路径重定向）。
"""

import os
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common import prompt_registry as pr
from common import prompt_template as pt

REPO_ROOT = Path(__file__).resolve().parent.parent

# 注入用临时模板（仅含 memberSystemPrompt，channel 默认 user 即可渲染）
_EDITED_MEMBERS_TS = """export interface MemberPromptVars {
  teamName: string; memberName: string; role: string; agent: string;
  mode: string; leader: string; leaderType: string; teamDir: string;
  shareDir: string;
}
export function memberSystemPrompt(vars: MemberPromptVars): string {
  const v = vars;
  return `集成标记 edited-ts team='${v.teamName}' member='${v.memberName}' role='${v.role}' agent='${v.agent}'
共享工作目录: ${v.teamDir}
共享上下文区: ${v.shareDir}`;
}
"""

_BROKEN_MEMBERS_TS = "export const broken = ;;; 这不是合法 TS 模板\n"


class _IsolatedMCP(unittest.TestCase):
    """数据隔离基类（镜像 test_prompt_identity_system_layer）。"""

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
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                        "ORIGINAL_CWD", "INIT_CWD", "PWD",
                        "MULTI_AGENT_MCP_PROMPTS_DIR")
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

    def _save_team(self, *, workspace, context, members, leader="lead"):
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": leader,
                    "leader_type": "tmux",
                    "default_agent": "claude",
                    "members": {
                        leader: {"role": "leader", "agent": "claude"},
                        **members,
                    },
                }
            }
        })

    def _team(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        self._save_team(
            workspace=workspace, context=self.root / "ctx",
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        return workspace

    def _spawn(self, name, agent, workspace, **spawn_kw):
        """mock _tmux 捕获成员 spawn 命令（镜像 test_prompt_identity_system_layer）。"""
        workspace = Path(workspace)
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
                rc = mcp._tmux_spawn_member("mcp_team", name, agent, str(workspace), **spawn_kw)
        spawn_cmds = [c for c in calls if c[0] in {"new-session", "new-window"}]
        return rc, spawn_cmds


# =====================================================================
# A. 无需 Node —— 纯 Python 渲染，不 spawn Node 运行时（任务硬约束）
# =====================================================================
class NoNodeDependencyTests(_IsolatedMCP):
    """render/身份文件路径必须是纯 Python；若解析层引入 Node 子进程立即红。"""

    def test_render_path_invokes_no_subprocess(self):
        self._team()
        calls = []
        orig_popen = subprocess.Popen

        def fake_popen(*args, **kwargs):
            calls.append(args[0] if args else kwargs.get("args"))
            return orig_popen(*args, **kwargs)

        with mock.patch.object(subprocess, "Popen", side_effect=fake_popen):
            pr.render_member_identity("team", "alice")
            pr.claude_identity_file("team", "alice")
            pr.codex_agents_md("team")
        self.assertEqual(
            calls, [],
            "render/身份文件路径不得调用任何子进程（纯 Python 解析，无 Node/tsc）",
        )

    def test_spawn_commands_contain_no_node_runtime(self):
        self._team()
        rc, spawn = self._spawn("alice", "claude", self.root / "ws")
        self.assertEqual(rc[0], 0)
        self.assertTrue(spawn, "未捕获 spawn 命令")
        for cmd in spawn:
            joined = " ".join(cmd).lower()
            for node_token in ("node ", "tsc ", "ts-node", "npx ", " node_modules/"):
                self.assertNotIn(
                    node_token, joined,
                    f"spawn 命令不得调用 Node 运行时/打包工具: {cmd}",
                )


# =====================================================================
# B. system/user 通道不混淆（C1/C2/C3/C4）
# =====================================================================
class ChannelRoutingTests(_IsolatedMCP):
    """真实 system 只经 append/AGENTS.md；send-keys 首启/恢复是 user 角色不伪称 system。"""

    def test_system_section_only_in_claude_append_file(self):
        """Claude 身份（system 段）只经 --append-system-prompt-file；send-keys 注入的
        首启/恢复上下文不得携带真实 system 机制参数，也不得声称 system（C1/C3）。"""
        self._team()
        path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("你的团队成员身份绑定", content)
        self.assertIn("member_name='alice'", content)
        for ctx in (
            mcp._build_member_initial_context("team", "alice"),
            mcp._build_recovery_context("team", "alice"),
        ):
            with self.subTest(first_line=ctx.splitlines()[0][:30]):
                self.assertNotIn("--append-system-prompt-file", ctx)
                low = ctx.lower()
                self.assertNotIn("system prompt", low)
                self.assertNotIn("system-prompt", low)

    def test_initial_context_uses_honest_channel_prefix(self):
        """首启上下文用诚实通道名 [成员上下文]（C3 伪 system 清理后语义），仍绑身份。"""
        self._team()
        init = mcp._build_member_initial_context("team", "alice")
        self.assertTrue(init.lstrip().startswith("[成员上下文]"), init.splitlines()[0])
        self.assertIn("你的团队成员身份绑定", init)
        self.assertIn("member_name='alice'", init)
        self.assertNotIn("[系统]", init)
        self.assertNotIn("[system]", init)

    def test_recovery_context_binds_identity_no_system_claim(self):
        """恢复上下文绑身份（纵深防御），不伪称 system（C3）。"""
        self._team()
        rec = mcp._build_recovery_context("team", "alice")
        self.assertIn("你的团队成员身份绑定", rec)
        self.assertNotIn("--append-system-prompt-file", rec)
        self.assertNotIn("system prompt", rec.lower())

    def test_codex_agents_md_role_neutral(self):
        """Codex AGENTS.md 段角色中立：不写死具体成员/角色（B2 防多角色串线）。"""
        self._team()
        text = pr.codex_agents_md("team")
        self.assertIn("Multi-Agent MCP 团队约束", text)
        self.assertNotIn("member_name=", text)
        self.assertNotIn("'alice'", text)
        self.assertNotIn("role=", text)

    def test_codex_agents_md_fail_closed_without_workspace(self):
        """无显式 workspace_dir → ensure_codex_agents_md fail-closed 零写入（防污染仓库根）。"""
        mcp._save({
            "teams": {"team": {
                "context_dir": str(self.root / "ctx"),
                "leader": "lead", "leader_type": "tmux",
                "members": {"lead": {"role": "leader", "agent": "claude"}},
            }}
        })
        self.assertEqual(pr.ensure_codex_agents_md("team", str(self.root / "ws")), "")

    def test_repo_template_channel_routing(self):
        """parser 公共接口：repo 模板各函数 @channel 路由正确（system 段唯一权威）。"""
        parsed = pt.load_parsed("members")
        self.assertEqual(parsed.functions["memberSystemPrompt"].channel, "system")
        self.assertEqual(parsed.functions["codexAgentsSection"].channel, "system")
        self.assertEqual(parsed.functions["memberInitialContext"].channel, "initial")
        self.assertEqual(parsed.functions["memberRecoveryContext"].channel, "recovery")
        self.assertEqual(parsed.functions["memberTaskPayload"].channel, "task")

    def test_unmarked_channel_defaults_user(self):
        """@channel 缺失默认 user（fail-safe，绝不默认 system，B5）。"""
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "members.ts").write_text(
            'export function memberSystemPrompt(v: any): string { return `无标注函数`; }',
            encoding="utf-8",
        )
        parsed = pt.load_parsed("members", prompts_dir=prompts)
        self.assertEqual(parsed.functions["memberSystemPrompt"].channel, "user")

    def test_system_fn_forbids_dynamic_fields(self):
        """system 通道函数禁动态字段（task/recoverySection/teammates，C4）。"""
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "members.ts").write_text(
            "/** @channel system */\n"
            "export function memberSystemPrompt(v: any): string { return `...${v.task}`; }",
            encoding="utf-8",
        )
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.render_template("members", "memberSystemPrompt", {"task": "x"},
                               prompts_dir=prompts)
        self.assertIn("禁动态字段", str(cm.exception))


# =====================================================================
# C. 坏模板 / 缺文件 —— 清晰错误 或 安全回退（A4/B1/B2/B4）
# =====================================================================
class TemplateFailureTests(_IsolatedMCP):
    """解析失败：parser 抛携带文件+原因的清晰错误；registry 层安全回退非空身份。"""

    def test_missing_file_clear_error(self):
        """缺文件 → PromptTemplateError 指向缺失模板（B1/D3）。"""
        prompts = self.root / "prompts"
        prompts.mkdir()
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.load_parsed("members", prompts_dir=prompts)
        self.assertIn("members.ts", str(cm.exception))

    def test_broken_template_clear_error(self):
        """坏模板（无通道函数）→ PromptTemplateError 带文件+原因（B2，不崩不静默）。"""
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "members.ts").write_text(_BROKEN_MEMBERS_TS, encoding="utf-8")
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.load_parsed("members", prompts_dir=prompts)
        msg = str(cm.exception)
        self.assertIn("members.ts", msg)
        self.assertTrue(any(tok in msg for tok in ("未找到", "function", "解析", "模板")), msg)

    def test_unknown_placeholder_clear_error(self):
        """${v.unknown}（vars 未提供）→ 明确错误（B4）。"""
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.render_body("${v.unknown}", {"known": "x"})
        self.assertIn("未知占位符", str(cm.exception))

    def test_missing_template_registry_safe_fallback(self):
        """缺模板 → render_member_identity 安全回退内建非空身份文本（A4：不静默丢身份）。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        with mock.patch.object(pr, "_prompts_dir", lambda: prompts):
            out = pr.render_member_identity("team", "alice")
        self.assertTrue(out.strip())
        self.assertIn("你的团队成员身份绑定", out)
        self.assertIn("member_name='alice'", out)

    def test_broken_template_registry_safe_fallback(self):
        """坏模板 → render_member_identity 安全回退内建非空身份文本（A4）。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "members.ts").write_text(_BROKEN_MEMBERS_TS, encoding="utf-8")
        with mock.patch.object(pr, "_prompts_dir", lambda: prompts):
            out = pr.render_member_identity("team", "alice")
        self.assertTrue(out.strip())
        self.assertIn("你的团队成员身份绑定", out)


# =====================================================================
# D. TS 修改新会话可见 —— 权威源 + mtime 缓存 + 新会话注入（F1/F2/G2）
# =====================================================================
class TsEditVisibilityTests(_IsolatedMCP):
    """prompts/*.ts 是运行时权威源：改 TS → 下次渲染 / 新会话身份文件反映编辑。"""

    def test_repo_members_ts_is_runtime_source(self):
        """render_member_identity 输出来自 prompts/members.ts memberSystemPrompt
        （权威源接线，非硬编码）：parser 渲染正文是 registry 输出的身份块前缀。"""
        self._team()
        team = mcp._load()["teams"]["team"]
        vars_ = {
            "teamName": "team", "memberName": "alice", "role": "coder",
            "agent": "claude", "mode": "manual", "leader": "lead",
            "leaderType": "tmux", "teamDir": team["workspace_dir"],
            "shareDir": team["context_dir"], "task": "", "recoverySection": "",
        }
        from_template = pt.render_template("members", "memberSystemPrompt", vars_)
        rendered = pr.render_member_identity("team", "alice")
        prefix, sep, _delivery = rendered.rpartition("\n\n[交付格式]")
        identity = prefix if sep else rendered
        self.assertEqual(
            [l.rstrip() for l in identity.strip().splitlines()],
            [l.rstrip() for l in from_template.splitlines()],
            "render_member_identity 身份块必须来自 prompts/members.ts memberSystemPrompt",
        )

    def test_ts_edit_visible_on_next_render(self):
        """mtime 键控缓存（F1/F2）：改 TS → 下一次渲染立即反映编辑（无需重启 server）。"""
        prompts = self.root / "prompts"
        prompts.mkdir()
        ts = prompts / "members.ts"
        ts.write_text(
            "export function memberSystemPrompt(v: any): string { return `MARKER_A '${v.teamName}'`; }",
            encoding="utf-8",
        )
        first = pt.render_template("members", "memberSystemPrompt",
                                   {"teamName": "t1"}, prompts_dir=prompts)
        self.assertIn("MARKER_A", first)
        time.sleep(0.05)  # 确保 mtime_ns 变化，触发缓存失效（F1 读盘）
        ts.write_text(
            "export function memberSystemPrompt(v: any): string { return `MARKER_B '${v.teamName}'`; }",
            encoding="utf-8",
        )
        second = pt.render_template("members", "memberSystemPrompt",
                                    {"teamName": "t1"}, prompts_dir=prompts)
        self.assertIn("MARKER_B", second)
        self.assertNotIn("MARKER_A", second)

    def test_ts_edit_propagates_to_new_session_identity(self):
        """改 TS → 新会话（spawn append 文件）反映编辑（F2/G2）。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        (prompts / "members.ts").write_text(_EDITED_MEMBERS_TS, encoding="utf-8")
        with mock.patch.object(pr, "_prompts_dir", lambda: prompts):
            path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("集成标记 edited-ts", content)
        self.assertIn("member='alice'", content)


# =====================================================================
# E. 现有 Claude/Codex identity 断言保持（E2/A5）
# =====================================================================
class ExistingIdentityAssertions(_IsolatedMCP):
    """既有身份断言在权威源接线后保持成立（E2：文本/行为兼容；A5：0600）。"""

    def test_claude_identity_file_0600_and_content(self):
        """Claude 身份文件 0600 + 内容绑身份（含 role/agent），防共享区可写注入面。"""
        self._team()
        path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("你的团队成员身份绑定", content)
        self.assertIn("member_name='alice'", content)
        self.assertIn("role='coder'", content)
        self.assertIn("agent='claude'", content)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode & 0o077, 0, f"身份文件权限过宽: {oct(mode)}")

    def test_leader_identity_from_leader_ts(self):
        """leader 身份来自 prompts/leader.ts leaderSystemPrompt（@channel system 权威源）。"""
        self._team()
        path = pr.claude_identity_file("team", "lead", leader=True)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的 leader", content)


if __name__ == "__main__":
    unittest.main()
