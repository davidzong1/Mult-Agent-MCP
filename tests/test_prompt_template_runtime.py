"""prompt_template runtime —— parser/registry 独立单测（refactor-claude 独占切片）。

覆盖验收清单（共享上下文 prompts-ts-authority-acceptance-checklist.md）中本切片可单测项：
  G1  契约测试（≥6）：占位替换/转义/未知字段/文件缺失/标记解析/幂等
  A1  单遍插值：${v.field} 只替换一遍，恶意值（含 ${...}/$(cmd)/反引号）不二次展开/执行
  A3  路径安全：模块相对定位 + MULTI_AGENT_MCP_PROMPTS_DIR 逃生阀 + prompts_dir 显式覆盖
  A4  失败回退不静默丢身份：缺文件/坏模板 → 回退非空内建文本，不崩溃、不输出空串
  B1-B6 解析稳健性：定位/提取/转义（\\` \\${ CRLF）/未知占位/system 标记/幂等
  E1  registry API 签名不变（render_member_identity/claude_identity_file/codex_agents_md）
  F1-F3 mtime 键控解析缓存、新会话生效、无跨会话渲染状态
  C1  system 段只进真 system 通道（registry 侧：@channel system 函数承载静态身份）
  C2  Codex AGENTS.md 角色中立（不写死成员/角色）

隔离：parser 用例写临时 prompts/*.ts + env 覆盖；registry 用例镜像
tests/test_prompt_registry.py 的 data_layer.set_data_file 数据隔离。只写本文件，
不碰其他团队状态，不 commit。
"""

import inspect
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common import prompt_registry as pr
from common import prompt_template as pt

# 合法多通道示例（对齐已迁移 members.ts 的 @channel 约定）
VALID_MEMBERS_TS = """export interface MemberPromptVars {
  teamName: string; memberName: string; role: string; agent: string;
  mode: string; leader: string; leaderType: string; teamDir: string; shareDir: string;
}
/**
 * 成员静态身份段
 * @channel system
 */
export function memberSystemPrompt(vars: MemberPromptVars): string {
  const v = vars;
  return `你是 '${v.teamName}' 的成员，member='${v.memberName}'，role='${v.role}'。`;
}
"""

# D 组契约同构的"编辑生效"模板（memberSystemPrompt 含自定义标记，可含动态占位）
EDITED_MEMBERS_TS = """export interface MemberPromptVars {
  teamName: string; memberName: string; role: string; agent: string;
  mode: string; leader: string; leaderType: string; teamDir: string; shareDir: string;
  task: string; recoverySection: string;
}
export function memberSystemPrompt(vars: MemberPromptVars): string {
  const v = vars;
  return `自定义身份标记 team='${v.teamName}' member='${v.memberName}' role='${v.role}' agent='${v.agent}'
mode='${v.mode}' leader='${v.leader}' leaderType='${v.leaderType}'
teamDir='${v.teamDir}' shareDir='${v.shareDir}'
总任务: ${v.task}
${v.recoverySection}`;
}
"""

CODEX_TS = """/**
 * @channel system
 */
export function codexAgentsSection(vars: MemberPromptVars): string {
  return `# 团队约束 自定义标记
你是团队 '${v.teamName}' 的成员。
共享上下文区: ${v.shareDir}`;
}
"""


class _TmpPrompts(unittest.TestCase):
    """临时 prompts 目录 + MULTI_AGENT_MCP_PROMPTS_DIR env 覆盖（B1/A3 hook）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.prompts = self.root / "prompts"
        self.prompts.mkdir()
        self.env_patch = mock.patch.dict(
            os.environ, {"MULTI_AGENT_MCP_PROMPTS_DIR": str(self.prompts)})
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def write_ts(self, name, text):
        (self.prompts / f"{name}.ts").write_text(text, encoding="utf-8")


# =====================================================================
# G1/B —— parser 契约
# =====================================================================
class ParserTests(_TmpPrompts):
    """@channel schema / 提取 / 占位符 / 转义 / 幂等（B1-B6, G1）。"""

    def test_parse_and_render_valid_system_channel(self):
        self.write_ts("members", VALID_MEMBERS_TS)
        parsed = pt.load_parsed("members")
        self.assertIn("memberSystemPrompt", parsed.functions)
        fn = parsed.functions["memberSystemPrompt"]
        self.assertEqual(fn.channel, "system", "@channel system 必须识别")
        out = pt.render_template("members", "memberSystemPrompt",
                                 {"teamName": "team", "memberName": "alice", "role": "coder"})
        self.assertEqual(out, "你是 'team' 的成员，member='alice'，role='coder'。")

    def test_missing_channel_defaults_user_fail_safe(self):
        """缺 @channel 默认 user（fail-safe，绝不默认 system）——B5。"""
        self.write_ts("members", "export function f(vars: any): string { return `hi ${v.x}`; }")
        self.assertEqual(pt.load_parsed("members").functions["f"].channel, "user")

    def test_invalid_channel_raises(self):
        self.write_ts("members", """/**
 * @channel bogus
 */
export function f(vars: any): string { return `hi`; }
""")
        with self.assertRaises(pt.PromptTemplateError):
            pt.load_parsed("members")

    def test_header_prose_channel_not_mistaken(self):
        """回归：文件头注释含散文 @channel（如 '@channel 是...'）不得被误读为通道标注（B5）。"""
        ts = """/**
 * 通道标注（@channel 是 system 判定的唯一权威；缺失默认 user，fail-safe）。
 */
export interface MemberPromptVars { teamName: string; }
/**
 * @channel system
 */
export function memberSystemPrompt(vars: MemberPromptVars): string {
  const v = vars;
  return `你是 '${v.teamName}' 的成员。`;
}
"""
        self.write_ts("members", ts)
        parsed = pt.load_parsed("members")
        self.assertEqual(parsed.functions["memberSystemPrompt"].channel, "system")

    def test_system_channel_forbids_dynamic_fields(self):
        """system 函数禁 task/recoverySection/teammates（C4，防动态冻结进 system 文件）。"""
        self.write_ts("members", """/**
 * @channel system
 */
export function memberSystemPrompt(vars: any): string {
  const v = vars;
  return `你是 '${v.teamName}'。总任务: ${v.task}`;
}
""")
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.render_template("members", "memberSystemPrompt", {"teamName": "T", "task": "x"})
        self.assertIn("task", str(cm.exception))

    def test_unknown_placeholder_raises(self):
        """${v.unknown} 未提供 → 明确错误（B4）。"""
        self.write_ts("members", "export function f(vars: any): string { return `x=${v.nope}`; }")
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.render_template("members", "f", {"teamName": "T"})
        self.assertIn("nope", str(cm.exception))

    def test_illegal_placeholder_expression_raises(self):
        """表达式占位符（非 ${v.field} 简单字段）→ 明确错误（B4）。"""
        self.write_ts("members", "export function f(vars: any): string { return `x=${v.a + 1}`; }")
        with self.assertRaises(pt.PromptTemplateError):
            pt.render_template("members", "f", {"a": "1"})

    def test_illegal_placeholder_nested_raises(self):
        """嵌套占位符（${v.a.b}）→ 明确错误（B4）。"""
        self.write_ts("members", "export function g(vars: any): string { return `x=${v.a.b}`; }")
        with self.assertRaises(pt.PromptTemplateError):
            pt.render_template("members", "g", {"a": "1"})

    def test_unclosed_placeholder_raises(self):
        """未闭合 ${...} → 明确错误（B4）。"""
        self.write_ts("members", "export function f(vars: any): string { return `x=${v.a`; }")
        with self.assertRaises(pt.PromptTemplateError):
            pt.render_template("members", "f", {"a": "1"})

    def test_unclosed_body_raises(self):
        """模板体缺闭合反引号 → 明确错误（B2）。"""
        self.write_ts("members", "export function g(vars: any): string { return `oops; }")
        with self.assertRaises(pt.PromptTemplateError):
            pt.load_parsed("members")

    def test_broken_syntax_raises(self):
        """无 export function 的坏模板 → 明确错误（B2）。"""
        self.write_ts("members", "export const broken = ;;; 这不是合法 TS 模板\n")
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.load_parsed("members")
        self.assertIn("未找到任何", str(cm.exception))

    def test_duplicate_function_raises(self):
        self.write_ts("members",
                      "export function f(vars: any): string { return `a`; }\n"
                      "export function f(vars: any): string { return `b`; }\n")
        with self.assertRaises(pt.PromptTemplateError):
            pt.load_parsed("members")

    def test_missing_file_raises(self):
        """prompts/{name}.ts 缺失 → 明确错误（B1/D3）。"""
        with self.assertRaises(pt.PromptTemplateError) as cm:
            pt.load_parsed("nonexistent")
        self.assertIn("不存在", str(cm.exception))

    def test_escapes_backtick_dollar_backslash(self):
        """\\` → 字面反引号；\\${ → 字面 ${ 不替换；\\\\ → 反斜杠（B3）。"""
        ts = (r"export function f(vars: any): string {"
              r"  const v = vars;"
              r"  return `a=\` b=\${x} c=\\ d=${v.teamName}`;"
              r"}")
        self.write_ts("members", ts)
        out = pt.render_template("members", "f", {"teamName": "T"})
        self.assertEqual(out, "a=` b=${x} c=\\ d=T")

    def test_crlf_normalized(self):
        """CRLF 模板体 → 输出归一化为 \\n（B3）。"""
        body = ("export function f(vars: any): string {\r\n"
                "  return `line1\r\nline2 ${v.x}`;\r\n"
                "}\r\n")
        self.write_ts("members", body)
        out = pt.render_template("members", "f", {"x": "X"})
        self.assertEqual(out, "line1\nline2 X")

    def test_single_pass_no_reexpand(self):
        """A1 单遍插值：值含 ${...}/$(cmd)/反引号/{{}} 原样保留，不二次展开/执行。"""
        self.write_ts("members", "export function f(vars: any): string { return `a=${v.teamName}`; }")
        malicious = "${v.memberName} $(touch /tmp/pwn) `boom` {{x}}"
        out = pt.render_template("members", "f", {"teamName": malicious})
        self.assertEqual(out, "a=" + malicious)

    def test_idempotent_parse(self):
        """B6 幂等：相同输入多次解析输出一致。"""
        self.write_ts("members", VALID_MEMBERS_TS)
        p1 = pt.load_parsed("members")
        p2 = pt.load_parsed("members")
        self.assertEqual(p1.functions["memberSystemPrompt"].body,
                         p2.functions["memberSystemPrompt"].body)

    def test_mtime_cache_reparse_on_change(self):
        """F1/F3：mtime 键控缓存——内容+mtime 变化 → 重新解析；同 mtime 复用。"""
        self.write_ts("members", VALID_MEMBERS_TS)
        p1 = pt.load_parsed("members")
        p2 = pt.load_parsed("members")
        self.assertIs(p1, p2, "同 mtime 应命中缓存")
        # 改内容 + 改 mtime → 重解析
        self.write_ts("members", VALID_MEMBERS_TS.replace("member='${v.memberName}'", "member='X'"))
        os.utime(self.prompts / "members.ts", ns=(1, 1))
        p3 = pt.load_parsed("members")
        self.assertNotEqual(p1.functions["memberSystemPrompt"].body,
                            p3.functions["memberSystemPrompt"].body)

    def test_prompts_dir_param_overrides_env(self):
        """A3：显式 prompts_dir 优先于 env（registry 注入可 patch 目录的通道）。"""
        other = self.root / "other"
        other.mkdir()
        (other / "members.ts").write_text(
            "export function f(vars: any): string { return `from-other`; }", encoding="utf-8")
        out = pt.render_template("members", "f", {}, prompts_dir=str(other))
        self.assertEqual(out, "from-other")
        path = pt.template_path("members", prompts_dir=str(other))
        self.assertTrue(str(path).startswith(str(other)))


# =====================================================================
# A4/E1/C1/C2 —— registry 接线契约
# =====================================================================
class _IsolatedRegistry(_TmpPrompts):
    """registry 用例：数据隔离（data_layer.set_data_file）+ 临时 prompts 目录。"""

    def setUp(self):
        super().setUp()
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
                    },
                }
            }
        })

    def tearDown(self):
        if self.old_override is not None:
            data_layer.set_data_file(self.old_override)
        else:
            data_layer.set_data_file(data_layer.DATA_FILE)
        super().tearDown()

    def _save(self, data):
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f)


class RegistryMemberIdentityTests(_IsolatedRegistry):
    def test_member_identity_renders_from_ts(self):
        """F2/C1：成员静态身份来自 members.ts memberSystemPrompt（@channel system）。"""
        self.write_ts("members", EDITED_MEMBERS_TS)
        text = pr.render_member_identity("team", "alice")
        self.assertIn("自定义身份标记", text)
        self.assertIn("member='alice'", text)
        self.assertIn("role='coder'", text)

    def test_member_identity_fallback_when_missing(self):
        """A4：members.ts 缺失 → 回退非空内建身份，不崩溃、不空串。"""
        text = pr.render_member_identity("team", "alice")
        self.assertTrue(text.strip())
        self.assertIn("你的团队成员身份绑定", text)
        self.assertIn("member_name='alice'", text)

    def test_member_identity_fallback_when_broken(self):
        """A4：坏模板 → 回退非空内建身份。"""
        self.write_ts("members", "export const broken = ;;;\n")
        text = pr.render_member_identity("team", "alice")
        self.assertTrue(text.strip())
        self.assertIn("你的团队成员身份绑定", text)

    def test_member_identity_still_carries_delivery_contract(self):
        """既有措辞锚点不回归（E2）：交付合约/顺序义务仍追加。"""
        self.write_ts("members", EDITED_MEMBERS_TS)
        text = pr.render_member_identity("team", "alice")
        self.assertIn("member_report_result", text)
        self.assertIn("先回报", text)


class RegistryClaudeIdentityTests(_IsolatedRegistry):
    def test_claude_identity_file_from_ts_and_0600(self):
        self.write_ts("members", EDITED_MEMBERS_TS)
        path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("自定义身份标记", content)
        mode = stat.S_IMODE(os.stat(path).st_mode)
        self.assertEqual(mode & 0o077, 0, "临时身份文件权限过宽（A5）")

    def test_claude_identity_file_fallback_path_when_missing(self):
        path = pr.claude_identity_file("team", "alice")
        content = Path(path).read_text(encoding="utf-8")
        self.assertTrue(content.strip())
        self.assertIn("你的团队成员身份绑定", content)

    def test_leader_identity_gated_until_ts_migration(self):
        """leader.ts 未迁移（无 @channel system）→ 不当作 system 渲染，回退既有源。"""
        self.write_ts("leader", "export function leaderSystemPrompt(vars: any): string { return `LEADER-TS-未迁移`; }")
        path = pr.claude_identity_file("team", "lead", leader=True)
        content = Path(path).read_text(encoding="utf-8")
        self.assertNotIn("LEADER-TS-未迁移", content, "未迁移 leader.ts 不得被当作 system 渲染")
        self.assertIn("leader", content)

    def test_leader_identity_uses_ts_when_system_migrated(self):
        """leader.ts leaderSystemPrompt 标注 @channel system 后 → 经 .ts 渲染静态段。"""
        self.write_ts("leader", """/**
 * Leader 系统段
 * @channel system
 */
export function leaderSystemPrompt(vars: any): string {
  const v = vars;
  return `你是团队 '${v.teamName}' 的 leader，member='${v.leaderMemberName}'（LEADER-TS-已迁移）。`;
}
""")
        path = pr.claude_identity_file("team", "lead", leader=True)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("LEADER-TS-已迁移", content)
        self.assertIn("member='lead'", content)


class RegistryCodexAgentsTests(_IsolatedRegistry):
    def test_codex_agents_md_from_ts_role_neutral(self):
        """C2：Codex AGENTS.md 角色中立（不写死成员/角色）——来源 codexAgentsSection。"""
        self.write_ts("members", CODEX_TS)
        md = pr.codex_agents_md("team")
        self.assertIn("自定义标记", md)
        self.assertIn("team", md)
        self.assertNotIn("member_name=", md)

    def test_codex_agents_md_fallback_when_missing(self):
        md = pr.codex_agents_md("team")
        self.assertTrue(md.strip())
        self.assertIn("Multi-Agent", md)
        self.assertNotIn("member_name=", md)


class RegistryApiCompatTests(_IsolatedRegistry):
    def test_e1_signatures_unchanged(self):
        """E1：公开 API 签名不变 → 调用点（mult_agent_mcp/tmux_utils/tui）零改动。"""
        self.assertEqual(list(inspect.signature(pr.render_member_identity).parameters),
                         ["team_name", "member_name"])
        sig = inspect.signature(pr.claude_identity_file)
        self.assertEqual(list(sig.parameters), ["team_name", "member_name", "leader"])
        self.assertIs(sig.parameters["leader"].kind, inspect.Parameter.KEYWORD_ONLY)
        self.assertEqual(list(inspect.signature(pr.codex_agents_md).parameters),
                         ["team_name"])
        self.assertEqual(list(inspect.signature(pr.ensure_codex_agents_md).parameters),
                         ["team_name", "team_dir"])

    def test_delivery_contract_stable_across_fallback_and_ts(self):
        """.ts 渲染与内建回退的交付合约部分一致（E2 防漂移）。"""
        ts_text = pr.render_member_identity("team", "alice")
        self.write_ts("members", "export const broken = ;;;")
        fb_text = pr.render_member_identity("team", "alice")
        # 两种路径都含身份绑定与交付合约
        for text in (ts_text, fb_text):
            self.assertIn("你的团队成员身份绑定", text)
            self.assertIn("先回报", text)


# =====================================================================
# 真实 repo prompts/*.ts 生产路径（members.ts 已迁移稳定）
# =====================================================================
class TestRealTsWiring(unittest.TestCase):
    """不覆盖 prompts 目录 → 验证 registry 实际读取仓库 prompts/members.ts。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(str(self.data_file))
        self.old_env = os.environ.pop("MULTI_AGENT_MCP_PROMPTS_DIR", None)
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump({"teams": {"team": {
                "workspace_dir": str(self.root / "ws"),
                "context_dir": str(self.root / "ctx"),
                "leader": "lead", "leader_type": "tmux", "default_agent": "claude",
                "members": {"lead": {"role": "leader", "agent": "claude"},
                            "alice": {"role": "coder", "agent": "claude"}},
            }}}, f)

    def tearDown(self):
        if self.old_override is not None:
            data_layer.set_data_file(self.old_override)
        else:
            data_layer.set_data_file(data_layer.DATA_FILE)
        if self.old_env is not None:
            os.environ["MULTI_AGENT_MCP_PROMPTS_DIR"] = self.old_env
        self.tmp.cleanup()

    def test_real_members_ts_is_wired_into_render(self):
        text = pr.render_member_identity("team", "alice")
        self.assertIn("你是 Multi-Agent MCP 团队 'team' 的成员。", text)
        self.assertIn("member_name='alice'", text)
        self.assertIn("role='coder'", text)
        self.assertIn("先回报", text)  # 交付合约

    def test_real_codex_section_is_wired_role_neutral(self):
        md = pr.codex_agents_md("team")
        self.assertIn("# Multi-Agent MCP 团队约束", md)
        self.assertIn("团队协作环境", md)
        self.assertNotIn("member_name=", md)


if __name__ == "__main__":
    unittest.main()
