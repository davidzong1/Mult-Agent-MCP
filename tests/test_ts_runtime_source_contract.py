"""
独立回归契约 —— 新 TS runtime source（共同基线 docs/system_prompt_injection_audit.md）
=====================================================================================

背景（共同基线 docs/system_prompt_injection_audit.md，2026-08-12）：
  审计结论：真实 system 通道基座已落地并转绿——Claude 经 ``--append-system-prompt-file``
  （prompt_registry.claude_identity_file），Codex 经团队工作区 AGENTS.md 角色中立段；
  但 **prompts/*.ts 当前不是运行时源**（无 TS 加载器/打包，运行时文本硬编码在 Python，
  ts 仅是文档模板）。本轮任务 = 复用既有通道，加「TS 解析 → system 段落显式标记路由」层。

本文件是 **tester 独立回归契约**（与 coder 测试文件互不重叠；只验可观察出口，不锁
coder 内部实现细节）。覆盖 leader 子任务要求：

  A. 通道元数据：真实 system 身份只经 append 文件 / AGENTS.md；tmux send-keys 注入的
     首启/恢复上下文是 **user 角色**，不得携带真实 system 机制（append flag）的伪称。
  B. 编辑生效（TS 权威源）：``render_member_identity`` 与 ``prompts/members.ts`` 模板
     静态段**逐字对等**——TS 被改而运行时未跟随即红（当前已绿 = 两者在同步，防漂移护栏）。
  C. 无 Node：render/身份文件路径是纯 Python，不 spawn node/tsc/ts-node；spawn 命令
     不含 Node 运行时。
  D. TDD 契约（依赖契约 hook ``common.prompt_registry._prompts_dir`` 已落地，
     缺 hook 时 skip 且明示）：
     - 编辑生效：修改临时 members.ts → render / 新 spawn append 文件反映编辑；
     - 坏模板：解析失败须有清晰错误或安全回退（不崩溃、不静默产出空文本）；
     - 缺文件：prompts 目录/文件缺失须有清晰错误或安全回退。

D 组依赖契约 hook：``common.prompt_registry`` 已暴露可 patch 的 ``_prompts_dir()``
函数（经 ``prompt_template._prompts_dir`` 解析 + ``MULTI_AGENT_MCP_PROMPTS_DIR``
逃生阀），供测试注入临时模板。缺 hook 时 D 组 skip（消息明示"待 coder 落地"），
不因未实现而红整条套件。

隔离：镜像 tests/test_prompt_registry.py / tests/test_prompt_identity_system_layer.py
（``data_layer.set_data_file`` + mcp 全局路径重定向），不触真实 teams_data.json / 仓库根。
只写本文件，不碰其他团队状态，不 commit。
"""

import json
import os
import re
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common import prompt_registry as pr

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMBERS_TS = REPO_ROOT / "prompts" / "members.ts"
LEADER_TS = REPO_ROOT / "prompts" / "leader.ts"

# 契约 hook 候选名（prompt_registry 暴露任一即可 patch 注入临时模板）
_PROMPTS_DIR_HOOKS = ("_PROMPTS_DIR", "PROMPTS_DIR", "_prompts_dir")


def _ts_function_body(ts_path: Path, fn_name: str) -> str:
    """按函数名提取 TS 文件中 ``export function <fn>... return `...`;`` 的模板正文。

    多函数 TS 设计（memberSystemPrompt/codexAgentsSection/memberInitialContext/...）
    下旧「单 return」假设（split 首个 ``return ``` 到末个 ```;``）失效——必须定位到
    指定函数再取反引号模板体（纯字符串，不执行 TS，不依赖 parser 实现）。
    """
    text = ts_path.read_text(encoding="utf-8")
    pat = re.compile(
        r"export\s+function\s+" + re.escape(fn_name) + r"\s*\([^)]*\)\s*:\s*string\s*\{"
    )
    m = pat.search(text)
    assert m, f"{ts_path} 缺函数 {fn_name}"
    ret = text.find("return `", m.end())
    assert ret != -1, f"{ts_path} 函数 {fn_name} 缺 return ` 模板体"
    start = ret + len("return `")
    i, n = start, len(text)
    while i < n:
        if text[i] == "\\":
            i += 2
            continue
        if text[i] == "`":
            return text[start:i].strip()
        i += 1
    raise AssertionError(f"{ts_path} 函数 {fn_name} 模板体未闭合")


def _substitute(body: str, vars_: dict) -> str:
    """把 ${v.xxx} 占位符替换为具体值。"""
    for key, value in vars_.items():
        body = body.replace("${v.%s}" % key, value)
    return body


def _prompts_override(tmp_dir: Path):
    """重定向 prompt_registry 的 prompts 目录解析（契约 hook）。返回 patch 或 None。

    轮询候选 hook；命中常量则 patch 常量，命中函数则 patch 函数返回固定目录。
    """
    for attr in _PROMPTS_DIR_HOOKS:
        cur = getattr(pr, attr, None)
        if cur is None:
            continue
        if not callable(cur):
            return mock.patch.object(pr, attr, str(tmp_dir))
        return mock.patch.object(pr, attr, lambda: str(tmp_dir))
    return None


class _IsolatedMCP(unittest.TestCase):
    """数据隔离基类（镜像 test_prompt_identity_system_layer 的 _IsolatedMCP）。"""

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
                        "ORIGINAL_CWD", "INIT_CWD", "PWD")
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

    def _spawn(self, name, agent, workspace, session="mcp_team", **spawn_kw):
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
                rc = mcp._tmux_spawn_member(session, name, agent, str(workspace), **spawn_kw)
        spawn_cmds = [c for c in calls if c[0] in {"new-session", "new-window"}]
        return rc, spawn_cmds


# =====================================================================
# A. 通道元数据：真实 system 只经 append/AGENTS.md；send-keys 是 user 角色
# =====================================================================
class ChannelMetadataTests(_IsolatedMCP):
    """审计 §3/§4 通道区分护栏：系统身份在真实通道；send-keys 注入不伪称 system。"""

    def _team(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        self._save_team(
            workspace=workspace, context=self.root / "ctx",
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        return workspace

    def test_claude_system_identity_only_via_append_file(self):
        """真实 system 身份（绑定段落）承载于 append 文件——Claude 唯一 system 通道。"""
        self._team()
        path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("你的团队成员身份绑定", content)
        self.assertIn("member_name='alice'", content)
        self.assertIn("role='coder'", content)
        # 临时文件私有（0600），防共享区可写注入面
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode & 0o077, 0, f"临时身份文件权限过宽: {oct(mode)}")

    def test_codex_system_identity_only_via_agents_md(self):
        """Codex 无 system 通道；身份固化到团队工作区 AGENTS.md（自动装载层）。"""
        workspace = self.root / "ws"
        workspace.mkdir()
        self._save_team(
            workspace=workspace, context=self.root / "ctx",
            members={"bob": {"role": "coder", "agent": "codex"}},
        )
        rc, spawn = self._spawn("bob", "codex", workspace)
        self.assertEqual(rc[0], 0)
        agents_md = workspace / "AGENTS.md"
        self.assertTrue(agents_md.exists(), "Codex 成员启动应写入 AGENTS.md")
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn("Multi-Agent", content)
        self.assertNotIn("member_name='bob'", content, "共享 AGENTS.md 必须角色中立（B2）")

    def test_send_keys_contexts_user_role_no_system_mechanism(self):
        """首启/恢复经 tmux send-keys 注入的是 user 角色消息：不得携带真实 system
        机制（append flag）参数，不得声称自己是 system prompt（audit §4 语义）。"""
        self._team()
        for ctx in (
            mcp._build_member_initial_context("team", "alice"),
            mcp._build_recovery_context("team", "alice"),
        ):
            with self.subTest(ctx_first_line=ctx.splitlines()[0][:30]):
                self.assertNotIn(
                    "--append-system-prompt-file", ctx,
                    "send-keys 注入不得携带真实 system 机制参数（伪称 system 面）",
                )
                low = ctx.lower()
                self.assertNotIn("system prompt", low)
                self.assertNotIn("system-prompt", low)

    def test_send_keys_contexts_still_bind_identity(self):
        """user 角色上下文仍须承载身份绑定（纵深防御；审计 C 组护栏），但不伪称 system。"""
        self._team()
        init = mcp._build_member_initial_context("team", "alice")
        rec = mcp._build_recovery_context("team", "alice")
        self.assertIn("你的团队成员身份绑定", init)
        self.assertIn("member_name='alice'", init)
        self.assertIn("你的团队成员身份绑定", rec)


# =====================================================================
# B. 编辑生效（TS 权威源）—— render 与 memberSystemPrompt 逐字对等
# =====================================================================
class TsAuthoritativeParityTests(_IsolatedMCP):
    """TS 权威源护栏：render_member_identity 输出（身份块，交付合约追加之前）必须与
    prompts/members.ts **memberSystemPrompt**（@channel system）模板逐字对等（替换
    ${v.xxx} 后）。按函数名提取（多函数 TS 设计，旧「单 return」假设已失效）；
    TS 被业务修改而运行时未跟随即红——「TS 修改被 Python 读取 / 编辑生效」独立回归。"""

    def _member_vars(self):
        team = {"teamName": "team", "memberName": "alice", "role": "coder",
                "agent": "claude", "mode": "manual", "leader": "lead",
                "leaderType": "tmux", "teamDir": str(self.root / "ws"),
                "shareDir": str(self.root / "ctx")}
        return team

    def _team(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        self._save_team(
            workspace=workspace, context=self.root / "ctx",
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        return workspace

    def test_render_matches_members_ts_system_fn_verbatim(self):
        self._team()
        body = _ts_function_body(MEMBERS_TS, "memberSystemPrompt")
        expected = _substitute(body, self._member_vars())
        actual = pr.render_member_identity("team", "alice")
        # render_member_identity = memberSystemPrompt 模板正文 + "\n\n" + 交付合约
        #（_append_delivery_contract 惰性复用 mult_agent_mcp 单一措辞源）。比较身份块。
        prefix, sep, _delivery = actual.rpartition("\n\n[交付格式]")
        identity = prefix if sep else actual
        self.assertEqual(
            [l.rstrip() for l in identity.strip().splitlines()],
            [l.rstrip() for l in expected.splitlines()],
            "render_member_identity 与 prompts/members.ts memberSystemPrompt 逐字不一致"
            "（TS 权威源漂移）",
        )

    def test_members_ts_system_fn_placeholders_covered_by_render(self):
        """memberSystemPrompt（@channel system）引用的每个 ${v.xxx} 都在 render 输出中
        被数据层值替换（无漏注入）；system 函数不引用动态字段（C4）。"""
        self._team()
        body = _ts_function_body(MEMBERS_TS, "memberSystemPrompt")
        placeholders = set(re.findall(r"\$\{v\.(\w+)\}", body))
        self.assertTrue(placeholders, "memberSystemPrompt 应含占位符")
        # system 通道函数禁动态字段（task/recoverySection/teammates，C4）
        dynamic = {"task", "recoverySection", "teammates"}
        self.assertTrue(
            placeholders.isdisjoint(dynamic),
            f"system 函数引用动态字段: {placeholders & dynamic}",
        )
        actual = pr.render_member_identity("team", "alice")
        for ph in placeholders:
            self.assertIn(self._member_vars()[ph], actual, f"占位符 {ph} 未注入渲染输出")


# =====================================================================
# C. 无 Node 依赖 —— 纯 Python 渲染，不 spawn node/tsc/ts-node
# =====================================================================
class NoNodeDependencyTests(_IsolatedMCP):
    """render/身份文件路径必须是纯 Python（任务要求"无需 TS runtime/Node"）。
    若 coder 解析层依赖 node 子进程，此组立即红。"""

    def _team(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        self._save_team(
            workspace=workspace, context=self.root / "ctx",
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        return workspace

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
# D. TDD 契约（待 coder 落地 TS 解析器后自动激活；缺 hook 时 skip 明示）
# =====================================================================
# 编辑生效（新会话注入）、坏模板、缺文件 —— 需 prompt_registry 暴露可 patch 的
# prompts 目录解析 hook（见 _prompts_override / _PROMPTS_DIR_HOOKS 契约）。
_EDITED_MEMBERS_TS = """export interface MemberPromptVars {
  teamName: string; memberName: string; role: string; agent: string;
  mode: string; leader: string; leaderType: string; teamDir: string;
  shareDir: string; task: string; recoverySection: string;
}
export function memberSystemPrompt(vars: MemberPromptVars): string {
  const v = vars;
  return `自定义身份标记 team='${v.teamName}' member='${v.memberName}' role='${v.role}' agent='${v.agent}'
mode='${v.mode}' leader='${v.leader}' leaderType='${v.leaderType}'
teamDir='${v.teamDir}' shareDir='${v.shareDir}'
[交付格式]
完成后调用 member_report_result，result 仅包含:
1. 结论 2. 修改文件 3. 验证/测试 4. 风险/阻塞
compressed_context <= 200 字；不要复述过程日志。
⚠️ 顺序义务：任务完成后的第一个动作必须是调用 member_report_result 回报，在此之前不要执行 /compact；若上下文即将耗尽，先回报再继续。
总任务: ${v.task}
${v.recoverySection}`;
}
"""

_BROKEN_MEMBERS_TS = "export const broken = ;;; 这不是合法 TS 模板\n"


class TsRuntimeSourceTddTests(_IsolatedMCP):
    """编辑生效 / 坏模板 / 缺文件 —— 契约验收（独立于 coder 实现，只验可观察出口）。

    通过 _prompts_override 把 prompt_registry 的 prompts 目录解析重定向到临时目录，
    注入受控模板；缺 hook 时 skip（消息指向契约要求），不因未实现而红整条套件。
    """

    def _team(self):
        workspace = self.root / "ws"
        workspace.mkdir()
        self._save_team(
            workspace=workspace, context=self.root / "ctx",
            members={"alice": {"role": "coder", "agent": "claude"}},
        )
        return workspace

    def test_edit_to_ts_propagates_to_render(self):
        """编辑生效：修改临时 members.ts → render 输出反映编辑（TS 权威源核心契约）。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        override = _prompts_override(prompts)
        if override is None:
            self.skipTest(
                "prompt_registry 未暴露可 patch 的 prompts 目录解析 hook "
                "（_PROMPTS_DIR/_prompts_dir，契约见本文件 docstring，待 coder 落地）"
            )
        (prompts / "members.ts").write_text(_EDITED_MEMBERS_TS, encoding="utf-8")
        with override:
            out = pr.render_member_identity("team", "alice")
        self.assertIn("自定义身份标记", out, "TS 编辑必须反映到 render 输出")
        self.assertIn("member='alice'", out)

    def test_edit_to_ts_propagates_to_new_spawn(self):
        """编辑生效 → 新会话注入：改 TS 后新 spawn 的 append 文件反映编辑。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        override = _prompts_override(prompts)
        if override is None:
            self.skipTest(
                "prompt_registry 未暴露可 patch 的 prompts 目录解析 hook "
                "（_PROMPTS_DIR/_prompts_dir，契约见本文件 docstring，待 coder 落地）"
            )
        (prompts / "members.ts").write_text(_EDITED_MEMBERS_TS, encoding="utf-8")
        with override:
            rc, spawn = self._spawn("alice", "claude", self.root / "ws")
        self.assertEqual(rc[0], 0)
        for cmd in spawn:
            if "--append-system-prompt-file" in cmd:
                idx = cmd.index("--append-system-prompt-file")
                content = Path(cmd[idx + 1]).read_text(encoding="utf-8")
                self.assertIn("自定义身份标记", content, "新会话 append 文件应反映 TS 编辑")
                return
        self.fail("spawn 命令未携带 --append-system-prompt-file")

    def test_broken_template_clear_error_or_safe_fallback(self):
        """坏模板：解析失败须有清晰错误（指向模板）或安全回退非空文本，不得崩溃。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        override = _prompts_override(prompts)
        if override is None:
            self.skipTest("prompt_registry 未暴露 prompts 目录解析 hook（待 coder 落地）")
        (prompts / "members.ts").write_text(_BROKEN_MEMBERS_TS, encoding="utf-8")
        with override:
            try:
                out = pr.render_member_identity("team", "alice")
            except Exception as exc:  # 允许：清晰错误
                msg = str(exc)
                self.assertTrue(
                    any(tok in msg for tok in ("members.ts", "prompts", "模板", "parse", "解析")),
                    f"坏模板错误信息应指向模板文件/解析问题: {msg!r}",
                )
            else:  # 或：安全回退非空文本
                self.assertTrue(
                    out.strip(), "坏模板应安全回退到非空文本，不得静默产出空身份"
                )

    def test_missing_prompts_file_clear_error_or_safe_fallback(self):
        """缺文件：prompts/members.ts 缺失须清晰错误（FileNotFound 指向文件）或安全回退。"""
        self._team()
        prompts = self.root / "prompts"
        prompts.mkdir()
        override = _prompts_override(prompts)
        if override is None:
            self.skipTest("prompt_registry 未暴露 prompts 目录解析 hook（待 coder 落地）")
        with override:  # 不创建 members.ts
            try:
                out = pr.render_member_identity("team", "alice")
            except FileNotFoundError as exc:
                self.assertIn("members.ts", str(exc), "缺文件错误应指向缺失模板")
            except Exception as exc:
                msg = str(exc)
                self.assertTrue(
                    any(tok in msg for tok in ("members.ts", "prompts", "模板")),
                    f"缺文件错误应指向模板: {msg!r}",
                )
            else:
                self.assertTrue(
                    out.strip(), "缺模板应安全回退到非空文本，不得静默产出空身份"
                )


if __name__ == "__main__":
    unittest.main()
