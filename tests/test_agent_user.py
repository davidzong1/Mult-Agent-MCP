"""
Agent 用户管理 (agent_user) 测试。
=================================

覆盖目标:
  1. validate_agent_user_url — URL 安全校验（shell 注入防护）
  2. _agent_user_env_prefix_for_team — agent-type-aware 环境变量注入（旧模型）
  3. _api_key_display — API Key 显示掩码（已配置/未配置）
  4. _validate_key_or_model — API Key / Model 安全校验
  5. _resolve_profile_agent_type / _agent_type_badge — profile 类型解析
  6. _build_agent_user_options — 选择列表构建
  7. Legacy profile 向后兼容
  8. 向后兼容 — 无 agent_users 字段的旧数据
"""

import contextlib
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common.tmux_utils import (
    validate_agent_user_url,
    validate_agent_user_env_value,
    _validate_url_safe,
    _validate_env_value,
    _agent_user_env_prefix_for_team,
)
from tui.tui_dialogs import (
    _api_key_display,
    _resolve_profile_agent_type,
    _agent_type_badge,
    _build_agent_user_options,
    _get_profile_agent_type,
)


@contextlib.contextmanager
def _temp_data_override(data: dict | None = None):
    """把 data_layer 覆盖指向临时数据文件，绝不触碰真实 teams_data.json。

    所有经 data_layer / common.tmux_utils / 迁移落盘的读写都落在临时文件；
    data 非空时先写入（作为真实读路径的基线）。finally 恢复原 override 并清理。
    """
    tmp = tempfile.TemporaryDirectory()
    try:
        prev_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_file = Path(tmp.name) / "teams_data.json"
        data_layer.set_data_file(data_file)
        if data is not None:
            from common.atomic_write import atomic_json_write
            atomic_json_write(data_file, data)
        yield
    finally:
        data_layer._DATA_FILE_OVERRIDE = prev_override
        tmp.cleanup()


@contextlib.contextmanager
def _mock_agent_user_data(data: dict):
    """mock tui_dialogs 与 common 的 load_data 指向同一 data，并把落盘路径隔离到临时文件。

    task4 后 _agent_user_profiles 委托 common.tmux_utils.list_agent_users
    （读路径在 common），而对话框 handler 仍用 tui.tui_dialogs.load_data。
    测试需同时覆盖两个读点，保证全局 registry 与保存路径一致；
    配合 _temp_data_override，未显式 patch 的 save_data/get_data_file/迁移
    也不会触碰真实 teams_data.json。
    """
    with _temp_data_override(data), \
         mock.patch("tui.tui_dialogs.load_data", return_value=data), \
         mock.patch("common.tmux_utils.load_data", return_value=data):
        yield


def _team(agent_users: dict, members: dict | None = None) -> dict:
    return {"agent_users": agent_users, "members": members or {}}


# ============================================================
# validate_agent_user_url — URL 安全校验
# ============================================================

class ValidateAgentUserUrlTests(unittest.TestCase):

    # ---- 合法 URL ----
    def test_accepts_https_with_hostname(self):
        self.assertEqual(validate_agent_user_url("https://api.anthropic.com"), "")

    def test_accepts_http_with_port(self):
        self.assertEqual(validate_agent_user_url("http://localhost:8080/v1"), "")

    def test_accepts_https_with_path(self):
        self.assertEqual(validate_agent_user_url("https://api.openai.com/v1/chat/completions"), "")

    def test_accepts_https_with_query(self):
        self.assertEqual(validate_agent_user_url("https://api.example.com/v1?model=gpt4"), "")

    # ---- 非法 scheme ----
    def test_rejects_ftp_scheme(self):
        err = validate_agent_user_url("ftp://example.com")
        self.assertIn("协议", err)

    def test_rejects_empty_scheme(self):
        err = validate_agent_user_url("://example.com")
        self.assertIn("协议", err)

    # ---- 空/无效 hostname ----
    def test_rejects_empty_url(self):
        err = validate_agent_user_url("")
        self.assertIn("不能为空", err)

    def test_rejects_missing_hostname(self):
        err = validate_agent_user_url("https:///path")
        self.assertIn("主机名", err)

    # ---- Shell 注入攻击 ----
    def test_rejects_dollar_paren_subshell(self):
        err = validate_agent_user_url("https://api.example.com/$(whoami)")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_backtick_injection(self):
        err = validate_agent_user_url("https://api.example.com/`id`")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_semicolon_injection(self):
        err = validate_agent_user_url("https://api.example.com; rm -rf /")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_ampersand_injection(self):
        err = validate_agent_user_url("https://api.example.com & echo hacked")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_pipe_injection(self):
        err = validate_agent_user_url("https://api.example.com | cat /etc/passwd")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_newline_in_url(self):
        """\n 被 _SHELL_DANGEROUS_RE 捕获（0x0a 在 \x00-\x1f）。"""
        err = validate_agent_user_url("https://api.example.com\nrm -rf /")
        self.assertTrue(err, "应拒绝含换行符的 URL")
        self.assertIn("禁止", err)

    def test_rejects_carriage_return(self):
        """\r 被 _SHELL_DANGEROUS_RE 捕获（0x0d 在 \x00-\x1f）。"""
        err = validate_agent_user_url("https://api.example.com\rcurl evil.com")
        self.assertTrue(err, "应拒绝含回车符的 URL")
        self.assertIn("禁止", err)

    def test_rejects_double_quote_injection(self):
        err = validate_agent_user_url('https://api.example.com/"oops"')
        self.assertIn("shell 特殊字符", err)

    def test_rejects_single_quote_injection(self):
        err = validate_agent_user_url("https://api.example.com/'oops'")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_less_than(self):
        err = validate_agent_user_url("https://api.example.com<evil")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_greater_than(self):
        err = validate_agent_user_url("https://api.example.com>output")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_parentheses(self):
        err = validate_agent_user_url("https://api.example.com/(cmd)")
        self.assertIn("shell 特殊字符", err)

    def test_rejects_backslash(self):
        err = validate_agent_user_url("https://api.example.com\\evil")
        self.assertIn("shell 特殊字符", err)

    # ---- userinfo @ 注入 ----
    def test_rejects_userinfo_at(self):
        err = validate_agent_user_url("https://evil@api.example.com/")
        self.assertIn("userinfo", err)

    # ---- URL 标准化绕过 ----
    def test_accepts_trailing_dot_segment(self):
        """urlsplit 不标准化 '..' 路径，geturl() 恒等于输入，此 URL 通过校验。"""
        err = validate_agent_user_url("https://api.example.com/..")
        self.assertEqual(err, "", f"urlsplit 不标准化 .. 路径: {err}")

    def test_accepts_double_scheme_in_path(self):
        """urlsplit 将 'https://evil.com/https://real.com' 解析为 netloc=evil.com，
        path='/https://real.com'；geturl() 重建与原字符串一致，校验通过。"""
        err = validate_agent_user_url("https://evil.com/https://real.com")
        self.assertEqual(err, "")

    # ---- 边界值 — 空格 / 制表符 / 编码换行 / 非法端口 ----
    def test_rejects_plain_space(self):
        """普通空格 ' ' (0x20) 已在 _SHELL_DANGEROUS_RE 中显式列出。"""
        err = validate_agent_user_url("https://evil .com")
        self.assertTrue(err, "应拒绝含空格的 URL")
        self.assertIn("禁止", err)

    def test_rejects_tab_character(self):
        """制表符 \\t (0x09) 在 \\x00-\\x1f + 显式 \\t，双重覆盖。"""
        err = validate_agent_user_url("https://evil.com\tinjected")
        self.assertTrue(err, "应拒绝含制表符的 URL")
        self.assertIn("禁止", err)

    def test_rejects_percent_encoded_lf(self):
        """%0a 等同于 \\n，_PCT_ENCODED_CRLF_RE 应拒绝。"""
        err = validate_agent_user_url("https://evil.com%0aheader")
        self.assertTrue(err, "应拒绝 %0a 编码换行")
        self.assertIn("编码", err)

    def test_rejects_percent_encoded_cr(self):
        """%0d 等同于 \\r。"""
        err = validate_agent_user_url("https://evil.com%0dheader")
        self.assertTrue(err, "应拒绝 %0d 编码回车")
        self.assertIn("编码", err)

    def test_rejects_percent_encoded_crlf_uppercase(self):
        """%0D / %0A 大小写不敏感。"""
        err = validate_agent_user_url("https://evil.com%0Aheader")
        self.assertTrue(err, "应拒绝大写 %0A")
        err2 = validate_agent_user_url("https://evil.com%0Dheader")
        self.assertTrue(err2, "应拒绝大写 %0D")

    def test_rejects_port_out_of_range_value_error(self):
        """端口 99999 导致 urlsplit.port 抛 ValueError。"""
        err = validate_agent_user_url("https://api.example.com:99999/v1")
        self.assertTrue(err, "应拒绝超出范围的端口")
        self.assertIn("端口", err)

    def test_rejects_port_zero(self):
        err = validate_agent_user_url("https://api.example.com:0/v1")
        self.assertTrue(err, "应拒绝端口 0")
        self.assertIn("端口", err)

    # ---- _validate_url_safe 兼容 ----
    def test_validate_url_safe_accepts_valid(self):
        self.assertTrue(_validate_url_safe("https://api.anthropic.com"))

    def test_validate_url_safe_rejects_injection(self):
        self.assertFalse(_validate_url_safe("https://evil.com;id"))

    def test_validate_url_safe_empty_is_not_safe(self):
        """空 URL 现在被 validate_agent_user_url 拒绝（"不能为空"）。"""
        self.assertFalse(_validate_url_safe(""))


# ============================================================
# _agent_user_env_prefix_for_team — agent-type-aware 注入
# 支持 typed profile (3 变量) + legacy fallback
# ============================================================

class AgentUserEnvPrefixTests(unittest.TestCase):

    # ---- typed Claude profile: Bearer 凭据 + BASE_URL/MODEL 注入 ----

    def test_typed_claude_injects_all_three_vars(self):
        team = _team(
            {"p1": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": "sk-ant-test",
                "anthropic_base_url": "https://api.anthropic.com",
                "anthropic_model": "claude-opus-5",
            }},
            {"alice": {"agent_user": "p1"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result[0], "env")
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-ant-test", result)
        self.assertNotIn("ANTHROPIC_API_KEY=", result)
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", result)
        self.assertIn("ANTHROPIC_MODEL=claude-opus-5", result)

    def test_typed_codex_injects_all_three_vars(self):
        team = _team(
            {"p1": {
                "agent_type": "codex", "takeover_enabled": True,
                "openai_api_key": "sk-test",
                "openai_base_url": "https://api.openai.com",
                "codex_model": "gpt-4o",
            }},
            {"bob": {"agent_user": "p1"}},
        )
        result = _agent_user_env_prefix_for_team(team, "bob", "codex")
        self.assertEqual(result[0], "env")
        self.assertIn("OPENAI_API_KEY=sk-test", result)
        self.assertIn("OPENAI_BASE_URL=https://api.openai.com", result)
        self.assertIn("CODEX_MODEL=gpt-4o", result)

    # ---- typed profile 类型不匹配 ----

    def test_typed_claude_profile_refuses_codex_agent(self):
        """Claude typed profile + codex agent → 类型不匹配，空。"""
        team = _team(
            {"p1": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": "sk-a", "anthropic_base_url": "https://a.com",
                "anthropic_model": "sonnet",
            }},
            {"alice": {"agent_user": "p1"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "codex")
        self.assertEqual(result, [])

    def test_typed_codex_profile_refuses_claude_agent(self):
        team = _team(
            {"p1": {
                "agent_type": "codex", "takeover_enabled": True,
                "openai_api_key": "sk-b", "openai_base_url": "https://b.com",
                "codex_model": "gpt-4o",
            }},
            {"bob": {"agent_user": "p1"}},
        )
        result = _agent_user_env_prefix_for_team(team, "bob", "claude")
        self.assertEqual(result, [])

    # ---- 部分字段为空 → 仅注入非空字段 ----

    def test_typed_claude_skips_empty_fields(self):
        team = _team(
            {"p1": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": "sk-ant-test",
                "anthropic_base_url": "",
                "anthropic_model": "",
            }},
            {"alice": {"agent_user": "p1"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-ant-test", result)
        self.assertNotIn("ANTHROPIC_BASE_URL", " ".join(result))
        self.assertNotIn("ANTHROPIC_MODEL", " ".join(result))

    # ---- 危险值不注入 ----

    def test_dangerous_api_key_not_injected(self):
        team = _team(
            {"p1": {
                "agent_type": "claude", "takeover_enabled": True,
                "anthropic_api_key": "sk-ant;id",
                "anthropic_base_url": "https://api.anthropic.com",
                "anthropic_model": "claude-opus-5",
            }},
            {"alice": {"agent_user": "p1"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertNotIn("ANTHROPIC_API_KEY", " ".join(result))
        # 合法字段仍注入
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", result)
        self.assertIn("ANTHROPIC_MODEL=claude-opus-5", result)

    # ---- 保留旧模型测试 ----

    def test_returns_empty_when_no_agent_users(self):
        team = {"members": {"alice": {"agent_user": "p1"}}}
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_returns_empty_when_member_no_agent_user(self):
        team = _team({"p1": {"anthropic_base_url": "https://api.example.com", "openai_base_url": "", "takeover_enabled": True}}, {"alice": {}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_returns_empty_when_takeover_disabled(self):
        team = _team({"p1": {"agent_type": "claude", "anthropic_api_key": "sk-a", "anthropic_base_url": "https://a.com", "anthropic_model": "x", "takeover_enabled": False}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_anthropic_url_for_claude_legacy(self):
        """legacy profile: Claude 只注入 ANTHROPIC_BASE_URL。"""
        team = _team({"p1": {"anthropic_base_url": "https://api.anthropic.com", "openai_base_url": "", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, ["env", "ANTHROPIC_BASE_URL=https://api.anthropic.com"])

    def test_openai_url_for_codex_legacy(self):
        team = _team({"p1": {"anthropic_base_url": "", "openai_base_url": "https://api.openai.com", "takeover_enabled": True}}, {"bob": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "bob", "codex")
        self.assertEqual(result, ["env", "OPENAI_BASE_URL=https://api.openai.com"])

    def test_no_anthropic_url_for_codex_legacy(self):
        team = _team({"p1": {"anthropic_base_url": "https://api.anthropic.com", "openai_base_url": "https://api.openai.com", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "codex")
        self.assertEqual(result, ["env", "OPENAI_BASE_URL=https://api.openai.com"])

    def test_no_openai_url_for_claude_legacy(self):
        team = _team({"p1": {"anthropic_base_url": "https://api.anthropic.com", "openai_base_url": "https://api.openai.com", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, ["env", "ANTHROPIC_BASE_URL=https://api.anthropic.com"])

    def test_unknown_agent_type_returns_empty(self):
        team = _team({"p1": {"agent_type": "claude", "anthropic_api_key": "sk-a", "anthropic_base_url": "https://a.com", "anthropic_model": "x", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "custom")
        self.assertEqual(result, [])

    def test_empty_agent_type_returns_empty(self):
        team = _team({"p1": {"agent_type": "claude", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "")
        self.assertEqual(result, [])

    def test_empty_url_not_injected(self):
        team = _team({"p1": {"agent_type": "claude", "anthropic_api_key": "", "anthropic_base_url": "", "anthropic_model": "", "openai_api_key": "", "openai_base_url": "", "codex_model": "", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_nonexistent_profile_returns_empty(self):
        team = _team({"p1": {"agent_type": "claude", "takeover_enabled": True}}, {"alice": {"agent_user": "nonexistent"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_injection_url_blocked_when_dangerous(self):
        team = _team({"p1": {"anthropic_base_url": "https://evil.com;id", "openai_base_url": "", "takeover_enabled": True}}, {"alice": {"agent_user": "p1"}})
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    # ---- 团队系统默认 profile 回退 ----

    def test_fallback_to_team_default_when_member_has_no_agent_user(self):
        """成员无 agent_user 时回退到 team.default_agent_user。"""
        team = _team(
            {"p1": {"agent_type": "claude", "anthropic_api_key": "sk-test",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-sonnet-5", "takeover_enabled": True,
                    "openai_api_key": "", "openai_base_url": "", "codex_model": ""}},
            {"alice": {}},  # 无 agent_user
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-test", result)
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", result)

    def test_fallback_type_mismatch_returns_empty(self):
        """team 默认 profile 是 claude，但 agent_type=codex 时不注入非匹配 provider 字段。"""
        team = _team(
            {"p1": {"agent_type": "claude", "anthropic_api_key": "sk-test",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-sonnet-5", "takeover_enabled": True,
                    "openai_api_key": "", "openai_base_url": "", "codex_model": ""}},
            {"alice": {}},
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "alice", "codex")
        self.assertEqual(result, [])

    def test_explicit_agent_user_overrides_default(self):
        """成员显式设置了 agent_user 时，不使用团队默认。"""
        team = _team(
            {"p_default": {"agent_type": "claude", "anthropic_api_key": "sk-default",
                          "anthropic_base_url": "https://default.example.com",
                          "anthropic_model": "claude-opus-5", "takeover_enabled": True,
                          "openai_api_key": "", "openai_base_url": "", "codex_model": ""},
             "p_explicit": {"agent_type": "claude", "anthropic_api_key": "sk-explicit",
                           "anthropic_base_url": "https://explicit.example.com",
                           "anthropic_model": "claude-sonnet-5", "takeover_enabled": True,
                           "openai_api_key": "", "openai_base_url": "", "codex_model": ""}},
            {"alice": {"agent_user": "p_explicit"}},
        )
        team["default_agent_user"] = "p_default"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-explicit", result)

    def test_no_default_and_no_member_agent_user_returns_empty(self):
        """既无团队默认也无成员 agent_user → 空列表。"""
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": True}},
            {"alice": {}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    # ---- 默认回退 + takeover_enabled=False: 完整接管（与 MODEL 一致） ----

    def test_default_fallback_full_takeover_injected_claude(self):
        """回退到 default_agent_user 且 takeover_enabled=False → 完整接管：
        MODEL + API_KEY/BASE_URL 全部注入（与 resolve_agent_model 的 MODEL 语义一致）。

        P0 回归：TUI 创建 leader 终端（leader 无显式 agent_user → 回退团队默认）
        时，模型已生效但 Anthropic API key/base URL 未注入。修复后默认 profile
        的完整 Claude 接管与模型保持一致。
        """
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": False,
                    "anthropic_api_key": "sk-secret",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-opus-5"}},
            {"alice": {}},  # 无显式 agent_user
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        # MODEL 注入（default fallback 不受 takeover_enabled 约束）
        self.assertIn("ANTHROPIC_MODEL=claude-opus-5", result)
        # AUTH_TOKEN/BASE_URL 同样注入（与 MODEL 保持一致）
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-secret", result)
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", result)

    def test_default_fallback_full_takeover_injected_codex(self):
        """回退到 default_agent_user + takeover_enabled=False → CODEX_MODEL +
        OPENAI_API_KEY/BASE_URL 全部注入。"""
        team = _team(
            {"p1": {"agent_type": "codex", "takeover_enabled": False,
                    "openai_api_key": "sk-secret",
                    "openai_base_url": "https://api.openai.com",
                    "codex_model": "gpt-4o"}},
            {"bob": {}},
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "bob", "codex")
        self.assertIn("CODEX_MODEL=gpt-4o", result)
        self.assertIn("OPENAI_API_KEY=sk-secret", result)
        self.assertIn("OPENAI_BASE_URL=https://api.openai.com", result)

    def test_legacy_default_fallback_takeover_off_claude_returns_empty(self):
        """Legacy profile (无 agent_type) 无 MODEL 可注入，
        BASE_URL 安全敏感 → default fallback + takeover_enabled=False → 返回 []."""
        team = _team(
            {"legacy_p": {"anthropic_base_url": "https://api.anthropic.com",
                          "openai_base_url": "",
                          "takeover_enabled": False}},
            {"alice": {}},  # 无显式 agent_user
        )
        team["default_agent_user"] = "legacy_p"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [],
                         "legacy + default fallback + takeover off → 应返回 []")

    def test_legacy_default_fallback_takeover_off_codex_returns_empty(self):
        """Legacy profile (无 agent_type) + Codex + takeover_enabled=False → 返回 []."""
        team = _team(
            {"legacy_p": {"anthropic_base_url": "",
                          "openai_base_url": "https://api.openai.com",
                          "takeover_enabled": False}},
            {"bob": {}},
        )
        team["default_agent_user"] = "legacy_p"
        result = _agent_user_env_prefix_for_team(team, "bob", "codex")
        self.assertEqual(result, [],
                         "legacy + default fallback + takeover off → 应返回 []")

    def test_explicit_selection_takeover_off_all_blocked(self):
        """成员显式选择 takeover_enabled=False 的 profile → 全部字段均不注入。
        这是已有契约 test_returns_empty_when_takeover_disabled 的补充验证。"""
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": False,
                    "anthropic_api_key": "sk-secret",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-opus-5"}},
            {"alice": {"agent_user": "p1"}},  # 显式选择
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [],
                         "显式选择 takeover_enabled=False → 全部不注入")

    def test_default_fallback_takeover_on_all_injected(self):
        """回退到 default_agent_user 且 takeover_enabled=True → 全部注入。"""
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": True,
                    "anthropic_api_key": "sk-test",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-sonnet-5"}},
            {"alice": {}},
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertIn("ANTHROPIC_AUTH_TOKEN=sk-test", result)
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", result)
        self.assertIn("ANTHROPIC_MODEL=claude-sonnet-5", result)

    def test_flash_pro_regression_exact_model(self):
        """回归验收：回退 default_agent_user + takeover_enabled=False
        → MODEL=deepseek/deepseek-v4-flash[1m] 正确注入。"""
        team = _team(
            {"deepseek_v4_flash": {
                "agent_type": "claude", "takeover_enabled": False,
                "anthropic_api_key": "",
                "anthropic_base_url": "",
                "anthropic_model": "deepseek/deepseek-v4-flash[1m]",
            }},
            {"coder-claude": {}},  # 无显式 agent_user
        )
        team["default_agent_user"] = "deepseek_v4_flash"
        result = _agent_user_env_prefix_for_team(team, "coder-claude", "claude")
        self.assertIn("ANTHROPIC_MODEL=deepseek/deepseek-v4-flash[1m]", result,
                      "default fallback 必须注入 flash 模型")
        self.assertNotIn("deepseek-v4-pro", " ".join(result),
                         "不应包含默认 pro 模型")
        # profile 未配置 API key → 无 key 注入（空字段跳过，不泄露凭据）
        self.assertNotIn("ANTHROPIC_API_KEY", " ".join(result))


# ============================================================
# _api_key_display — API Key 显示掩码
# ============================================================

class ApiKeyDisplayTests(unittest.TestCase):
    """_api_key_display(s): 已配置/未配置，不泄漏明文。"""

    def test_nonempty_returns_configured(self):
        self.assertEqual(_api_key_display("sk-ant-api03-xxxx"), "已配置")

    def test_empty_returns_not_configured(self):
        self.assertEqual(_api_key_display(""), "未配置")

    def test_whitespace_only_returns_not_configured(self):
        self.assertEqual(_api_key_display("   "), "未配置")

    def test_any_nonempty_returns_configured(self):
        """仅判断是否为空，不区分格式。"""
        self.assertEqual(_api_key_display("x"), "已配置")

    def test_does_not_leak_prefix(self):
        """确认"已配置"不包含输入值的任何部分。"""
        result = _api_key_display("sk-secret-key-12345")
        self.assertEqual(result, "已配置")
        self.assertNotIn("sk", result)
        self.assertNotIn("secret", result)
        self.assertNotIn("12345", result)

    def test_does_not_leak_suffix(self):
        result = _api_key_display("my-key-abc")
        self.assertNotIn("abc", result)


# ============================================================
# validate_agent_user_env_value — API Key / Model 安全校验（public）
# ============================================================

class ApiKeySafetyTests(unittest.TestCase):
    """validate_agent_user_env_value(value, field_name='值') → str (空串=合法)。"""

    # ---- 合法值 ----
    def test_accepts_typical_anthropic_key(self):
        self.assertEqual(validate_agent_user_env_value("sk-ant-api03-abc123xyz", "API_KEY"), "")

    def test_accepts_typical_openai_key(self):
        self.assertEqual(validate_agent_user_env_value("sk-proj-abcdefghijklmnop", "API_KEY"), "")

    def test_accepts_model_name(self):
        self.assertEqual(validate_agent_user_env_value("claude-sonnet-5-20251001", "MODEL"), "")

    def test_accepts_codex_model(self):
        self.assertEqual(validate_agent_user_env_value("gpt-4o", "MODEL"), "")

    def test_accepts_empty_value(self):
        self.assertEqual(validate_agent_user_env_value(""), "")

    def test_accepts_at_sign(self):
        self.assertEqual(validate_agent_user_env_value("key@example"), "")

    # ---- Shell 注入拒绝 ----
    def test_rejects_dollar_paren(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-$(whoami)", "KEY"))

    def test_rejects_backtick(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-`id`", "KEY"))

    def test_rejects_semicolon(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant;rm -rf /", "KEY"))

    def test_rejects_ampersand(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant&&evil", "KEY"))

    def test_rejects_pipe(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant|cat /etc/passwd", "KEY"))

    def test_rejects_double_quote(self):
        self.assertIn("shell", validate_agent_user_env_value('sk-ant"quoted', "KEY"))

    def test_rejects_single_quote(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant'oops'", "KEY"))

    def test_rejects_less_than(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant<evil", "KEY"))

    def test_rejects_greater_than(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant>output", "KEY"))

    def test_rejects_parentheses(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant(cmd)", "KEY"))

    def test_rejects_backslash(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant\\evil", "KEY"))

    def test_rejects_newline(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant\nrm -rf /", "KEY"))

    def test_rejects_carriage_return(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant\rx", "KEY"))

    def test_rejects_tab(self):
        self.assertIn("shell", validate_agent_user_env_value("sk-ant\tkey", "KEY"))

    def test_rejects_percent_encoded_crlf(self):
        self.assertIn("编码", validate_agent_user_env_value("sk-ant%0aheader", "KEY"))

    # ---- 长度限制 ----
    def test_accepts_512_chars(self):
        self.assertEqual(validate_agent_user_env_value("a" * 512, "KEY"), "")

    def test_rejects_513_chars(self):
        self.assertIn("512", validate_agent_user_env_value("a" * 513, "KEY"))

    def test_rejects_space_via_shell_re(self):
        """空格在 _SHELL_DANGEROUS_RE 中显式列出，应被拒绝。"""
        self.assertIn("shell", validate_agent_user_env_value("sk ant key", "KEY"))

    # ---- _validate_env_value bool alias ----
    def test_env_value_true_for_valid(self):
        self.assertTrue(_validate_env_value("sk-ant-valid"))

    def test_env_value_false_for_injection(self):
        self.assertFalse(_validate_env_value("sk-ant;id"))


# ============================================================
# Profile 类型解析 helpers
# ============================================================

class ProfileHelperTests(unittest.TestCase):
    """_resolve_profile_agent_type, _agent_type_badge, _get_profile_agent_type。"""

    # ---- _resolve_profile_agent_type ----
    def test_resolve_claude(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": "claude"}), "claude")

    def test_resolve_codex(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": "codex"}), "codex")

    def test_resolve_uppercase_normalized(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": "CLAUDE"}), "claude")

    def test_resolve_unknown_returns_empty(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": "unknown"}), "")

    def test_resolve_empty_agent_type_returns_empty(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": ""}), "")

    def test_resolve_legacy_with_url_returns_claude(self):
        """legacy 无 agent_type 但带单边 claude 字段 → 数据层推断 claude。

        2026-08-09 同步修改：UI 薄委托数据层 _profile_resolved_atype，
        legacy 按 base_url/api_key/model 三组字段单边推断（旧断言
        "无 agent_type 一律空串"是空类型语义）。
        """
        self.assertEqual(_resolve_profile_agent_type({"anthropic_base_url": "https://a.com"}), "claude")

    def test_resolve_whitespace_agent_type_returns_empty(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": "  "}), "")

    def test_resolve_none_value_returns_empty(self):
        self.assertEqual(_resolve_profile_agent_type({"agent_type": None}), "")

    # ---- _agent_type_badge ----
    def test_badge_claude(self):
        self.assertIn("Claude", _agent_type_badge("claude"))

    def test_badge_codex(self):
        self.assertIn("Codex", _agent_type_badge("codex"))

    def test_badge_legacy(self):
        self.assertIn("旧版", _agent_type_badge(""))
        self.assertIn("旧版", _agent_type_badge("unknown"))

    # ---- _get_profile_agent_type ----
    def test_get_profile_agent_type_claude(self):
        with mock.patch(
            "tui.tui_dialogs._agent_user_profiles",
            return_value={"p1": {"agent_type": "claude"}},
        ):
            self.assertEqual(_get_profile_agent_type("team", "p1"), "claude")

    def test_get_profile_agent_type_missing_key(self):
        with mock.patch(
            "tui.tui_dialogs._agent_user_profiles",
            return_value={},
        ):
            self.assertEqual(_get_profile_agent_type("team", "nonexistent"), "")

    def test_get_profile_agent_type_legacy_with_url_resolves_claude(self):
        """legacy+url → _get_profile_agent_type 推断 claude（2026-08-09 同步：委托数据层）。"""
        with mock.patch(
            "tui.tui_dialogs._agent_user_profiles",
            return_value={"p1": {"anthropic_base_url": "https://a.com"}},
        ):
            self.assertEqual(_get_profile_agent_type("team", "p1"), "claude")


# ============================================================
# _build_agent_user_options — 选项列表构建
# ============================================================

class BuildAgentUserOptionsTests(unittest.TestCase):
    """_build_agent_user_options(team_name, for_agent_type)。"""

    _BASIC_PROFILES = {
        "p1": {"agent_type": "claude", "takeover_enabled": True,
               "anthropic_api_key": "sk-a", "anthropic_base_url": "https://a.com",
               "anthropic_model": "sonnet"},
        "p2": {"agent_type": "codex", "takeover_enabled": False,
               "openai_api_key": "sk-b", "openai_base_url": "https://b.com",
               "codex_model": "gpt-4o"},
        "p3": {"anthropic_base_url": "https://old.com", "takeover_enabled": True},
    }

    def test_always_includes_system_default(self):
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value={}):
            # 隔离 load_data：不依赖真实/残留的 default_agent_user，
            # 确保"系统默认"选项始终存在且无后缀。
            with _mock_agent_user_data({"teams": {"team": {}}}):
                opts = _build_agent_user_options("team")
        self.assertEqual(opts[0], ("系统默认", ""))
        # 即使没有 profile，也有"系统默认"+"不接管"两个选项
        self.assertGreaterEqual(len(opts), 1)

    def test_includes_all_profiles_when_no_filter(self):
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            opts = _build_agent_user_options("team")
        values = [v for _, v in opts]
        self.assertIn("p1", values)
        self.assertIn("p2", values)
        self.assertIn("p3", values)

    def test_filter_claude_only(self):
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            opts = _build_agent_user_options("team", for_agent_type="claude")
        values = [v for _, v in opts]
        self.assertIn("p1", values)   # Claude typed
        self.assertNotIn("p2", values)  # Codex typed → excluded
        self.assertIn("p3", values)   # legacy (no agent_type) → included

    def test_filter_codex_only(self):
        """codex 过滤下 legacy+url 推断为 claude → 被排除（与数据层同源）。

        2026-08-09 同步修改：legacy profile 判定已薄委托数据层
        _profile_resolved_atype —— 带 anthropic_base_url 的 p3 推断为
        claude，codex 过滤不再包含它（旧断言"legacy 一律不过滤"是
        "UI 比数据层更严/更松"漂移前的空类型语义）。
        """
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            opts = _build_agent_user_options("team", for_agent_type="codex")
        values = [v for _, v in opts]
        self.assertNotIn("p1", values)  # Claude typed → excluded
        self.assertIn("p2", values)   # Codex typed
        self.assertNotIn("p3", values)  # legacy+url → claude → codex 过滤排除

    def test_takeover_label_absent(self):
        """简化后 profile label 不显示接管标记，仅显示 Provider 和 key。"""
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            opts = _build_agent_user_options("team")
        p1_option = next((label for label, val in opts if val == "p1"), "")
        self.assertNotIn("🔀", p1_option)
        self.assertIn("Claude", p1_option)
        self.assertIn("p1", p1_option)
        p2_option = next((label for label, val in opts if val == "p2"), "")
        self.assertNotIn("🔀", p2_option)

    def test_badge_in_label(self):
        """Label 应包含 provider 标记。"""
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            opts = _build_agent_user_options("team")
        p1_label = next((label for label, val in opts if val == "p1"), "")
        self.assertIn("Claude", p1_label)
        p2_label = next((label for label, val in opts if val == "p2"), "")
        self.assertIn("Codex", p2_label)
        p3_label = next((label for label, val in opts if val == "p3"), "")
        self.assertIn("Claude", p3_label)  # legacy+url → 数据层推断 claude（不再一律旧版）

    def test_default_profile_marked_with_star(self):
        """系统默认 profile 应带 ⭐ 前缀。"""
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {"default_agent_user": "p1"}}
            }):
                opts = _build_agent_user_options("team")
        p1_label = next((label for label, val in opts if val == "p1"), "")
        self.assertIn("⭐", p1_label)
        # 系统默认 option 应反映默认 key
        default_label = opts[0][0]
        self.assertIn("p1", default_label)
        self.assertIn("系统默认", default_label)

    def test_no_default_profile_shows_plain_system_default(self):
        """无默认 profile 时显示 '系统默认'。"""
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team")
        self.assertEqual(opts[0][0], "系统默认")

    def test_for_agent_type_claude_includes_no_takeover(self):
        """for_agent_type='claude' 过滤时 '不接管' 选项仍存在。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team", for_agent_type="claude")
        values = [v for _, v in opts]
        self.assertIn(AGENT_USER_NONE, values,
                      "for_agent_type='claude' 时 '不接管' 应仍存在")
        labels = [label for label, _ in opts]
        self.assertIn("不接管", labels)

    def test_for_agent_type_codex_includes_no_takeover(self):
        """for_agent_type='codex' 过滤时 '不接管' 选项仍存在。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team", for_agent_type="codex")
        values = [v for _, v in opts]
        self.assertIn(AGENT_USER_NONE, values,
                      "for_agent_type='codex' 时 '不接管' 应仍存在")
        labels = [label for label, _ in opts]
        self.assertIn("不接管", labels)

    def test_for_agent_type_filter_excludes_wrong_provider_but_keeps_no_takeover(self):
        """for_agent_type 正确过滤 profile 但保留 '不接管' 和 '系统默认'。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team", for_agent_type="claude")
        values = [v for _, v in opts]
        self.assertEqual(values[0], "")                      # 系统默认
        self.assertEqual(values[1], AGENT_USER_NONE)          # 不接管
        self.assertIn("p1", values)                           # Claude typed
        self.assertNotIn("p2", values)                        # Codex typed → excluded
        self.assertIn("p3", values)                           # legacy → included

    def test_include_no_takeover_false_excludes_sentinel(self):
        """include_no_takeover=False → 不包含 '不接管' 哨兵（用于 manage dialog）。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team", include_no_takeover=False)
        values = [v for _, v in opts]
        self.assertNotIn(AGENT_USER_NONE, values,
                         "include_no_takeover=False 时不应包含 sentinel")
        labels = [label for label, _ in opts]
        self.assertNotIn("不接管", labels,
                         "include_no_takeover=False 时不应包含 '不接管' label")
        self.assertEqual(values[0], "")  # 系统默认仍存在

    def test_include_no_takeover_false_with_for_agent_type(self):
        """include_no_takeover=False + for_agent_type → 无 sentinel，过滤正确。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team", for_agent_type="claude",
                                                  include_no_takeover=False)
        values = [v for _, v in opts]
        self.assertNotIn(AGENT_USER_NONE, values)
        self.assertIn("p1", values)
        self.assertNotIn("p2", values)


# ============================================================
# 旧 Profile 向后兼容
# ============================================================

class LegacyProfileCompatibilityTests(unittest.TestCase):
    """旧 profile（无 agent_type）行为验证。"""

    def test_legacy_profile_with_claude_url_resolves_claude(self):
        """单边 claude 字段的 legacy → 数据层推断 claude。

        2026-08-09 同步修改：legacy 判定已委托数据层，带 anthropic_base_url
        的旧 profile 推断为 claude（旧断言"返回空串"是空类型语义）。
        """
        old_cfg = {"anthropic_base_url": "https://api.example.com",
                   "openai_base_url": "", "takeover_enabled": True}
        self.assertEqual(_resolve_profile_agent_type(old_cfg), "claude")

    def test_legacy_profile_keeps_old_urls(self):
        """旧 profile 注入仍按旧 helper 行为（按 agent_type 读对应 URL），
        不受 agent_type 字段缺失影响。"""
        team = _team(
            {"legacy_p": {"anthropic_base_url": "https://api.anthropic.com",
                          "openai_base_url": "", "takeover_enabled": True}},
            {"alice": {"agent_user": "legacy_p"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, ["env", "ANTHROPIC_BASE_URL=https://api.anthropic.com"])

    def test_legacy_profile_codex_reads_old_openai_url(self):
        team = _team(
            {"legacy_p": {"anthropic_base_url": "",
                          "openai_base_url": "https://api.openai.com",
                          "takeover_enabled": True}},
            {"bob": {"agent_user": "legacy_p"}},
        )
        result = _agent_user_env_prefix_for_team(team, "bob", "codex")
        self.assertEqual(result, ["env", "OPENAI_BASE_URL=https://api.openai.com"])

    def test_legacy_profile_with_takeover_off_returns_empty(self):
        team = _team(
            {"legacy_p": {"anthropic_base_url": "https://api.example.com",
                          "openai_base_url": "", "takeover_enabled": False}},
            {"alice": {"agent_user": "legacy_p"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_new_profile_with_agent_type_does_not_confuse_helper(self):
        """有 agent_type 的新 profile 不影响旧 helper 行为。"""
        team = _team(
            {"new_p": {"agent_type": "claude",
                       "anthropic_base_url": "https://api.anthropic.com",
                       "openai_base_url": "https://api.openai.com",
                       "takeover_enabled": True}},
            {"alice": {"agent_user": "new_p"}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, ["env", "ANTHROPIC_BASE_URL=https://api.anthropic.com"])


# ============================================================
# AGENT_USER_NONE 哨兵 — 显式不接管
# ============================================================

class AgentUserNoneSentinelEnvTests(unittest.TestCase):
    """AGENT_USER_NONE 哨兵在 env 解析中的行为。"""

    def setUp(self):
        # 本类含内联 set_data_file 用例：捕获进入时的 override，供 tearDown 恢复
        self._prev_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

    def tearDown(self):
        # 恢复 override（含失败时），避免指向已删除临时文件泄漏到后续测试/文件
        data_layer._DATA_FILE_OVERRIDE = self._prev_override

    def test_sentinel_skips_default_fallback(self):
        """成员 agent_user='__none__' → 不注入 env，即使 default_agent_user 存在。"""
        from common.tmux_utils import AGENT_USER_NONE
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": True,
                    "anthropic_api_key": "sk-test",
                    "anthropic_base_url": "https://api.anthropic.com",
                    "anthropic_model": "claude-sonnet-5"}},
            {"alice": {"agent_user": AGENT_USER_NONE}},
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_sentinel_with_no_default_returns_empty(self):
        """成员 agent_user='__none__', 无 default_agent_user → 空列表。"""
        from common.tmux_utils import AGENT_USER_NONE
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": True}},
            {"alice": {"agent_user": AGENT_USER_NONE}},
        )
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_sentinel_never_leaks_into_env(self):
        """'{__none__}' 绝不出现在 env 输出中。"""
        from common.tmux_utils import AGENT_USER_NONE
        team = _team(
            {"p1": {"agent_type": "claude", "takeover_enabled": True,
                    "anthropic_base_url": "https://example.com",
                    "anthropic_model": "sonnet"}},
            {"alice": {"agent_user": AGENT_USER_NONE}},
        )
        team["default_agent_user"] = "p1"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        joined = " ".join(result)
        self.assertNotIn(AGENT_USER_NONE, joined)

    def test_sentinel_not_treated_as_profile_key(self):
        """'{__none__}' 不在 agent_users 中时不应被当作 profile key 查询。"""
        from common.tmux_utils import AGENT_USER_NONE
        team = _team(
            {"real_p": {"agent_type": "claude", "takeover_enabled": True,
                        "anthropic_api_key": "sk-real"}},
            {"alice": {"agent_user": AGENT_USER_NONE}},
        )
        # 即使 default_agent_user 指向真实 profile，sentinel 也不应走 fallback
        team["default_agent_user"] = "real_p"
        result = _agent_user_env_prefix_for_team(team, "alice", "claude")
        self.assertEqual(result, [])

    def test_public_get_agent_user_env_prefix_returns_empty_for_sentinel(self):
        """公共包装层 get_agent_user_env_prefix 对 AGENT_USER_NONE 返回空列表。"""
        from common.tmux_utils import get_agent_user_env_prefix, AGENT_USER_NONE
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent_user": "p1",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_api_key": "sk-test",
                                   "anthropic_base_url": "https://api.anthropic.com",
                                   "anthropic_model": "claude-sonnet-5"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude",
                                       "agent_user": AGENT_USER_NONE},
                        },
                    }
                }
            })
            result = get_agent_user_env_prefix("team", "alice", "claude")
            self.assertEqual(result, [],
                             "公共包装层对 sentinel 应返回空列表")
        finally:
            tmpdir.cleanup()


class AgentUserNoneSentinelOptionsTests(unittest.TestCase):
    """AGENT_USER_NONE 哨兵在 Select 选项中的行为。"""

    _BASIC_PROFILES = {
        "p1": {"agent_type": "claude", "takeover_enabled": True},
    }

    def test_includes_no_takeover_option(self):
        """_build_agent_user_options 始终包含 '不接管' 选项。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {}}
            }):
                opts = _build_agent_user_options("team")
        values = [v for _, v in opts]
        self.assertEqual(values[0], "")               # 系统默认
        self.assertEqual(values[1], AGENT_USER_NONE)   # 不接管
        labels = [label for label, _ in opts]
        self.assertIn("不接管", labels)

    def test_no_takeover_present_when_default_exists(self):
        """团队有 default_agent_user 时，'不接管' 仍存在。"""
        from common.tmux_utils import AGENT_USER_NONE
        with mock.patch("tui.tui_dialogs._agent_user_profiles", return_value=self._BASIC_PROFILES):
            with _mock_agent_user_data({
                "teams": {"team": {"default_agent_user": "p1"}}
            }):
                opts = _build_agent_user_options("team")
        values = [v for _, v in opts]
        self.assertIn(AGENT_USER_NONE, values)
        labels = [label for label, _ in opts]
        self.assertIn("不接管", labels)


# ============================================================
# resolve_agent_model — model string resolution for --model CLI flag
# ============================================================

class ResolveAgentModelTests(unittest.TestCase):
    """resolve_agent_model 函数：从 agent_user profile 解析 model 字符串。"""

    def setUp(self):
        # 本类含内联 set_data_file 用例：捕获进入时的 override，供 tearDown 恢复
        self._prev_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

    def tearDown(self):
        # 恢复 override（含失败时），避免指向已删除临时文件泄漏到后续测试/文件
        data_layer._DATA_FILE_OVERRIDE = self._prev_override

    def test_typed_claude_returns_model(self):
        """Typed Claude profile + takeover enabled → 返回 anthropic_model。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_model": "claude-opus-5",
                                   "anthropic_api_key": "sk-test",
                                   "anthropic_base_url": "https://api.anthropic.com"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude",
                                       "agent_user": "p1"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "claude-opus-5")
        finally:
            tmpdir.cleanup()

    def test_typed_codex_returns_model(self):
        """Typed Codex profile + takeover enabled → 返回 codex_model。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "codex",
                        "agent_users": {
                            "p1": {"agent_type": "codex", "takeover_enabled": True,
                                   "codex_model": "gpt-4o",
                                   "openai_api_key": "sk-test",
                                   "openai_base_url": "https://api.openai.com"},
                        },
                        "members": {
                            "bob": {"role": "tester", "agent": "codex",
                                     "agent_user": "p1"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "bob")
            self.assertEqual(result, "gpt-4o")
        finally:
            tmpdir.cleanup()

    def test_takeover_disabled_returns_empty(self):
        """takeover_enabled=False → 返回空字符串。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": False,
                                   "anthropic_model": "claude-opus-5"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude",
                                       "agent_user": "p1"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "")
        finally:
            tmpdir.cleanup()

    def test_no_agent_user_returns_empty(self):
        """成员无 agent_user 且无 default_agent_user → 返回空。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_model": "claude-opus-5"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "")
        finally:
            tmpdir.cleanup()

    def test_fallback_to_default_agent_user(self):
        """成员无 agent_user 但 team.default_agent_user 存在 → 返回 model。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "default_agent_user": "p1",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_model": "claude-sonnet-5"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "claude-sonnet-5")
        finally:
            tmpdir.cleanup()

    def test_default_fallback_takeover_off_returns_model(self):
        """回退到 default_agent_user + takeover_enabled=False → 仍返回 model。
        与显式选择 takeover_enabled=False 不同（显式选择应阻塞全部字段）。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "default_agent_user": "p1",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": False,
                                   "anthropic_model": "claude-opus-5"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "claude-opus-5",
                             "default fallback 不受 takeover_enabled 约束")
        finally:
            tmpdir.cleanup()

    def test_default_fallback_flash_model_regression(self):
        """回归验收：default_agent_user + takeover_enabled=False
        → 返回 deepseek/deepseek-v4-flash[1m]。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "default_agent_user": "deepseek_v4_flash",
                        "agent_users": {
                            "deepseek_v4_flash": {
                                "agent_type": "claude",
                                "takeover_enabled": False,
                                "anthropic_model": "deepseek/deepseek-v4-flash[1m]",
                            },
                        },
                        "members": {
                            "coder-claude": {"role": "coder", "agent": "claude"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "coder-claude")
            self.assertEqual(result, "deepseek/deepseek-v4-flash[1m]",
                             "default fallback 必须返回 flash 模型")
        finally:
            tmpdir.cleanup()

    def test_type_mismatch_returns_empty(self):
        """Claude profile + codex member → 类型不匹配 → 返回空。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "codex",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_model": "claude-opus-5"},
                        },
                        "members": {
                            "bob": {"role": "tester", "agent": "codex",
                                     "agent_user": "p1"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "bob")
            self.assertEqual(result, "")
        finally:
            tmpdir.cleanup()

    def test_legacy_profile_returns_empty(self):
        """Legacy profile（无 agent_type）→ 不返回 model。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "agent_users": {
                            "p1": {"anthropic_base_url": "https://api.anthropic.com",
                                   "takeover_enabled": True},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude",
                                       "agent_user": "p1"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "")
        finally:
            tmpdir.cleanup()

    def test_agent_user_none_returns_empty(self):
        """AGENT_USER_NONE 哨兵 → 返回空。"""
        from common.tmux_utils import resolve_agent_model, AGENT_USER_NONE
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "default_agent_user": "p1",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_model": "claude-sonnet-5"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude",
                                       "agent_user": AGENT_USER_NONE},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "")
        finally:
            tmpdir.cleanup()

    def test_model_with_brackets_preserved(self):
        """Model 含特殊字符如 [1m] 应原样保留（不进行 shell 转义）。"""
        from common.tmux_utils import resolve_agent_model
        from common.data_layer import save_data, set_data_file
        import tempfile as _tmp
        tmpdir = _tmp.TemporaryDirectory()
        try:
            root = Path(tmpdir.name)
            data_file = str(root / "data.json")
            set_data_file(data_file)
            save_data({
                "teams": {
                    "team": {
                        "default_agent": "claude",
                        "agent_users": {
                            "p1": {"agent_type": "claude", "takeover_enabled": True,
                                   "anthropic_model": "deepseek/deepseek-v4-flash[1m]"},
                        },
                        "members": {
                            "alice": {"role": "coder", "agent": "claude",
                                       "agent_user": "p1"},
                        },
                    }
                }
            })
            result = resolve_agent_model("team", "alice")
            self.assertEqual(result, "deepseek/deepseek-v4-flash[1m]")
        finally:
            tmpdir.cleanup()


# ============================================================
# claude_agent_args / codex_command — model CLI flag
# ============================================================

class ModelCliFlagTests(unittest.TestCase):
    """验证 model 参数被注入到 CLI --model flag 中。"""

    def test_claude_agent_args_with_model(self):
        """model 非空时 claude_agent_args 添加 --model flag。"""
        from common.tmux_utils import claude_agent_args
        args = claude_agent_args("claude", "manual", model="claude-opus-5")
        self.assertIn("--model", args)
        idx = args.index("--model")
        self.assertEqual(args[idx + 1], "claude-opus-5")

    def test_claude_agent_args_without_model(self):
        """model 为空时不添加 --model flag。"""
        from common.tmux_utils import claude_agent_args
        args = claude_agent_args("claude", "manual")
        self.assertNotIn("--model", args)
        args2 = claude_agent_args("claude", "auto", model="")
        self.assertNotIn("--model", args2)

    def test_codex_command_with_model(self):
        """model 非空时 codex_command 添加 --model flag。"""
        from common.tmux_utils import codex_command
        args = codex_command("codex", "/tmp", model="gpt-4o")
        self.assertIn("--model", args)
        idx = args.index("--model")
        self.assertEqual(args[idx + 1], "gpt-4o")

    def test_codex_command_without_model(self):
        """model 为空时不添加 --model flag。"""
        from common.tmux_utils import codex_command
        args = codex_command("codex", "/tmp")
        self.assertNotIn("--model", args)

    def test_model_with_brackets_in_cli_flag(self):
        """含 [1m] 的 model 名通过 --model flag 传递，不触发 shell glob。"""
        from common.tmux_utils import claude_agent_args
        model = "deepseek/deepseek-v4-flash[1m]"
        args = claude_agent_args("claude", "manual", model=model)
        idx = args.index("--model")
        self.assertEqual(args[idx + 1], model)


# ============================================================
# 回归：AgentUserManageDialog 三态（无选择 / 普通用户 / 不接管）
# ------------------------------------------------------------
# task1(P0): 刷新后 Select 处于 Select.NULL（NoSelection）时点击编辑/设默认
#            把 NoSelection 传入 Rich Text → AttributeError。修复为归一化空串。
# task2(P1): 管理界面可明确选择"不接管"，语义与成员编辑一致；且不把哨兵
#            当作可管理 profile（编辑/删除明确拒绝，不崩溃）；设为默认则
#            清除团队默认 = "团队默认不接管"（幂等），不写入 __none__。
# ============================================================

from common.tmux_utils import AGENT_USER_NONE
from textual.widgets import Select
from tui.tui_dialogs import (
    AgentUserManageDialog,
    TeamDefaultAgentUserDialog,
    _selected_profile_key,
    _agent_user_profiles,
    _agent_user_rename_sweep,
    _agent_user_delete_sweep,
    _agent_user_ref_count,
    _global_profile_options,
)

_MANAGE_3STATE_PROFILES = {
    "claude_p": {
        "agent_type": "claude",
        "takeover_enabled": True,
        "anthropic_api_key": "sk-ant-fake123",
        "anthropic_base_url": "https://api.anthropic.com",
        "anthropic_model": "claude-sonnet-5",
    },
}


class SelectedProfileKeyTests(unittest.TestCase):
    """_selected_profile_key — 把 Textual NoSelection 哨兵归一化为空串。"""

    class _FakeSelect:
        def __init__(self, value: object) -> None:
            self.value = value

    def test_null_selection_normalized_to_empty(self) -> None:
        self.assertEqual(_selected_profile_key(self._FakeSelect(Select.NULL)), "")

    def test_system_default_blank_normalized_to_empty(self) -> None:
        self.assertEqual(_selected_profile_key(self._FakeSelect("")), "")

    def test_normal_profile_key_passthrough(self) -> None:
        self.assertEqual(_selected_profile_key(self._FakeSelect("alice")), "alice")

    def test_no_takeover_sentinel_passthrough(self) -> None:
        self.assertEqual(
            _selected_profile_key(self._FakeSelect(AGENT_USER_NONE)),
            AGENT_USER_NONE,
        )


class AgentUserManageGlobalTests(unittest.IsolatedAsyncioTestCase):
    """全局 manage dialog：无选择 / 普通 profile 下 编辑/删除 均不崩溃且行为正确。

    task4：manage dialog 为纯全局 profile 列表（无系统默认/不接管，无设默认），
    三态中的「设为默认」语义移到 TeamDefaultAgentUserDialog。
    """

    def _data(self, **extra) -> dict:
        """全局 registry + 可选的 teams 数据（符合 task4 契约）。"""
        data = {"agent_users": dict(_MANAGE_3STATE_PROFILES), "teams": {}}
        data.update(extra)
        return data

    async def test_no_selection_edit_delete_prompt_not_crash(self) -> None:
        """无行（空列表）→ 编辑/删除提示且不崩溃（无行防护）。"""
        from textual.app import App
        from textual.widgets import Label

        with _mock_agent_user_data({"agent_users": {}, "teams": {}}):
            with mock.patch("common.data_layer.save_data") as mock_save:
                app = App()
                dialog = AgentUserManageDialog()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    # 空列表 → OptionList 稳定挂载（id 不变），无行可高亮
                    result = dialog.query_one("#agent_user_result", Label)
                    await pilot.click("#btn_edit")
                    await pilot.pause(0.3)
                    self.assertIn("请先选择或新建", str(result.render()))
                    self.assertIs(pilot.app.screen, dialog)
                    mock_save.assert_not_called()

                    await pilot.click("#btn_delete")
                    await pilot.pause(0.3)
                    self.assertIn("请先选择或新建", str(result.render()))
                    self.assertIs(pilot.app.screen, dialog)
                    mock_save.assert_not_called()

    async def test_normal_profile_edit_opens_editor_and_preserves_selection(self) -> None:
        """普通 profile → 编辑弹编辑框；返回后高亮行仍有效。"""
        from textual.app import App
        from textual.widgets import OptionList

        with _mock_agent_user_data(self._data()):
            with mock.patch("common.data_layer.save_data"):
                app = App()
                dialog = AgentUserManageDialog()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    option_list = dialog.query_one("#agent_user_list", OptionList)
                    self.assertEqual(option_list.highlighted_option.id, "claude_p",
                                     "初始应高亮第一行")

                    from tui.tui_dialogs import AgentUserEditDialog
                    await pilot.click("#btn_edit")
                    await pilot.pause(0.3)
                    self.assertIsInstance(pilot.app.screen, AgentUserEditDialog)
                    await pilot.press("escape")
                    await pilot.pause(0.3)
                    self.assertIs(pilot.app.screen, dialog)
                    self.assertEqual(option_list.highlighted_option.id, "claude_p",
                                     "取消编辑返回后应保留高亮")

    async def test_manage_dialog_is_pure_profile_list(self) -> None:
        """task4: 全局 manage 用列表展示 profiles（无系统默认 / 无 '不接管' / 无设默认）。"""
        from textual.app import App
        from textual.widgets import OptionList

        with _mock_agent_user_data(self._data()):
            app = App()
            dialog = AgentUserManageDialog()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)

                option_list = dialog.query_one("#agent_user_list", OptionList)
                self.assertIsInstance(
                    option_list, OptionList,
                    "顶层管理应用列表展示，而非单个下拉 Select")
                values = [opt.id for opt in option_list.options]
                self.assertIn("claude_p", values, "应包含全局 profile")
                self.assertNotIn(AGENT_USER_NONE, values,
                                 "全局 manage 不应包含 '不接管'")
                self.assertNotIn("", values, "全局 manage 不应包含 '系统默认'")
                # 每行至少展示 key + provider + 接管状态
                row = next(opt for opt in option_list.options
                           if opt.id == "claude_p")
                row_text = str(row.prompt)
                self.assertIn("Claude", row_text)
                self.assertIn("claude_p", row_text)
                self.assertIn("接管", row_text)
                # 无设默认按钮（团队默认移到 TeamDetailScreen）
                self.assertEqual(len(dialog.query("#btn_set_default")), 0,
                                 "全局 manage 不应有设默认按钮")

    async def test_new_profile_duplicate_key_rejected(self) -> None:
        """新建已存在的全局 profile → 提示已存在，不覆盖保存。"""
        from textual.app import App
        from textual.widgets import Label

        with _mock_agent_user_data(self._data()):
            with mock.patch("common.data_layer.save_data") as mock_save:
                app = App()
                dialog = AgentUserManageDialog()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    # 模拟编辑弹窗返回一个已存在的 key（push_screen_wait 在 app 上）
                    with mock.patch.object(
                        app, "push_screen_wait",
                        return_value={"key": "claude_p",
                                      "agent_type": "claude",
                                      "takeover_enabled": True},
                    ):
                        await pilot.click("#btn_new")
                        await pilot.pause(0.4)
                        result = dialog.query_one("#agent_user_result", Label)
                        self.assertIn("已存在", str(result.render()))
                        mock_save.assert_not_called()

    async def test_create_first_profile_shows_immediately_then_manage(self) -> None:
        """空 registry → 新建第一项 → 列表立即显示并可 edit/rename/delete。

        回归 task4 顶层列表空态 bug：OptionList 必须始终挂载，
        新建首个 profile 后 _refresh_dialog 能直接 query 到并立即刷新。
        """
        from textual.app import App
        from textual.widgets import Label, OptionList

        data = {"agent_users": {}, "teams": {}}
        profile = {
            "key": "alice",
            "agent_type": "claude",
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-fake",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-sonnet-5",
            "openai_api_key": "",
            "openai_base_url": "",
            "codex_model": "",
        }
        with _mock_agent_user_data(data):
            with mock.patch("common.data_layer.save_data"):
                app = App()
                dialog = AgentUserManageDialog()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    option_list = dialog.query_one("#agent_user_list", OptionList)
                    hint = dialog.query_one("#agent_user_empty", Label)
                    self.assertEqual(list(option_list.options), [])
                    self.assertTrue(hint.display, "空 registry 应显示空态提示")

                    # 新建第一个 profile → 列表立即显示，无需重开对话框
                    with mock.patch.object(app, "push_screen_wait",
                                           return_value=dict(profile)):
                        await pilot.click("#btn_new")
                        await pilot.pause(0.4)
                    self.assertEqual([o.id for o in option_list.options], ["alice"])
                    self.assertEqual(option_list.highlighted_option.id, "alice",
                                     "新建后应自动高亮新行")
                    self.assertFalse(hint.display, "有行后空态提示应隐藏")

                    # 编辑可用（编辑确认写回同 key，列表不变、不崩溃）
                    with mock.patch.object(app, "push_screen_wait",
                                           return_value=dict(profile)):
                        await pilot.click("#btn_edit")
                        await pilot.pause(0.4)
                    self.assertEqual([o.id for o in option_list.options], ["alice"])
                    self.assertIs(pilot.app.screen, dialog)

                    # 重命名 → 列表刷新为新 key
                    with mock.patch.object(app, "push_screen_wait",
                                           return_value="bob"):
                        await pilot.click("#btn_rename")
                        await pilot.pause(0.4)
                    self.assertEqual([o.id for o in option_list.options], ["bob"])

                    # 删除最后一个 profile → 空态稳定，按钮不崩
                    with mock.patch.object(app, "push_screen_wait",
                                           return_value=True):
                        await pilot.click("#btn_delete")
                        await pilot.pause(0.4)
                    self.assertEqual(list(option_list.options), [])
                    self.assertTrue(hint.display, "删除最后一项后应回到空态提示")
                    # OptionList 仍稳定挂载（id 一致，未被替换成 Label）
                    self.assertIsInstance(
                        dialog.query_one("#agent_user_list"), OptionList)

                    result = dialog.query_one("#agent_user_result", Label)
                    await pilot.click("#btn_edit")
                    await pilot.pause(0.3)
                    self.assertIn("请先选择或新建", str(result.render()))
                    await pilot.click("#btn_rename")
                    await pilot.pause(0.3)
                    self.assertIn("请先选择或新建", str(result.render()))
                    await pilot.click("#btn_delete")
                    await pilot.pause(0.3)
                    self.assertIn("请先选择或新建", str(result.render()))
                    self.assertIs(pilot.app.screen, dialog)

    async def test_delete_last_profile_keeps_stable_empty_state(self) -> None:
        """删除最后一项后空态稳定：OptionList 常驻、按钮不崩溃。"""
        from textual.app import App
        from textual.widgets import Label, OptionList

        with _mock_agent_user_data(self._data()):
            with mock.patch("common.data_layer.save_data"):
                app = App()
                dialog = AgentUserManageDialog()
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    option_list = dialog.query_one("#agent_user_list", OptionList)
                    self.assertEqual([o.id for o in option_list.options],
                                     ["claude_p"])
                    hint = dialog.query_one("#agent_user_empty", Label)
                    self.assertFalse(hint.display)

                    # 删除唯一 profile
                    with mock.patch.object(app, "push_screen_wait",
                                           return_value=True):
                        await pilot.click("#btn_delete")
                        await pilot.pause(0.4)

                    self.assertEqual(list(option_list.options), [])
                    self.assertTrue(hint.display)
                    self.assertIsInstance(
                        dialog.query_one("#agent_user_list"), OptionList,
                        "空态下 #agent_user_list 仍是 OptionList，不是 Label")

                    result = dialog.query_one("#agent_user_result", Label)
                    for btn in ("#btn_edit", "#btn_rename", "#btn_delete"):
                        await pilot.click(btn)
                        await pilot.pause(0.3)
                        self.assertIn("请先选择或新建", str(result.render()))
                        self.assertIs(pilot.app.screen, dialog)


class TeamDefaultAgentUserThreeStateTests(unittest.IsolatedAsyncioTestCase):
    """TeamDefaultAgentUserDialog（TeamDetailScreen u 入口）三态：
    无选择 / 普通 profile / 不接管 下设为默认均不崩溃且行为正确。"""

    def _data(self, *, default: str = "", teams: dict | None = None) -> dict:
        team = {"agent_users": dict(_MANAGE_3STATE_PROFILES)}
        if default:
            team["default_agent_user"] = default
        data = {"agent_users": dict(_MANAGE_3STATE_PROFILES),
                "teams": {"team": team if teams is None else teams}}
        return data

    async def test_no_selection_prompts_not_crash(self) -> None:
        """无选择（Select.NULL）→ 提示，不保存、不崩溃。"""
        from textual.app import App
        from textual.widgets import Label

        with _mock_agent_user_data(self._data()):
            with mock.patch("common.data_layer.save_data") as mock_save:
                app = App()
                dialog = TeamDefaultAgentUserDialog(team_name="team")
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    select = dialog.query_one("#team_default_select", Select)
                    select.clear()
                    await pilot.pause(0.2)
                    self.assertIs(select.value, Select.NULL)

                    await pilot.click("#btn_set_default")
                    await pilot.pause(0.3)
                    result = dialog.query_one("#team_default_result", Label)
                    self.assertIn("请先选择一个 profile", str(result.render()))
                    self.assertIs(pilot.app.screen, dialog)
                    mock_save.assert_not_called()

    async def test_normal_profile_set_default_persists(self) -> None:
        """选择普通 profile + 设为默认 → 持久化 default_agent_user，刷新显示。"""
        from textual.app import App
        from textual.widgets import Label

        with _mock_agent_user_data(self._data()):
            with mock.patch("common.data_layer.save_data") as mock_save:
                app = App()
                dialog = TeamDefaultAgentUserDialog(team_name="team")
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    select = dialog.query_one("#team_default_select", Select)
                    select.value = "claude_p"
                    await pilot.pause(0.2)

                    await pilot.click("#btn_set_default")
                    await pilot.pause(0.3)
                    result = dialog.query_one("#team_default_result", Label)
                    self.assertIn("已设为团队默认", str(result.render()))
                    mock_save.assert_called_once()
                    saved = mock_save.call_args[0][0]
                    self.assertEqual(
                        saved["teams"]["team"]["default_agent_user"], "claude_p")
                    self.assertEqual(select.value, "claude_p",
                                     "刷新后应保留选择")

    async def test_no_takeover_clears_team_default(self) -> None:
        """选择 '不接管' + 设为默认 → 清除 default_agent_user（幂等）。"""
        from textual.app import App
        from textual.widgets import Label

        with _mock_agent_user_data(self._data(default="claude_p")):
            with mock.patch("common.data_layer.save_data") as mock_save:
                app = App()
                dialog = TeamDefaultAgentUserDialog(team_name="team")
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.app.push_screen(dialog)
                    await pilot.pause(0.3)

                    select = dialog.query_one("#team_default_select", Select)
                    select.value = AGENT_USER_NONE
                    await pilot.pause(0.2)

                    await pilot.click("#btn_set_default")
                    await pilot.pause(0.3)
                    result = dialog.query_one("#team_default_result", Label)
                    self.assertIn("团队默认不接管", str(result.render()))
                    mock_save.assert_called_once()
                    saved = mock_save.call_args[0][0]
                    team = saved["teams"]["team"]
                    self.assertNotIn("default_agent_user", team,
                                     "不接管设默认应清除 default_agent_user")
                    self.assertNotIn(AGENT_USER_NONE, str(saved),
                                     "不应把 __none__ 写入任何持久化字段")

                    # 幂等：再次点击同样清除，不报错
                    await pilot.click("#btn_set_default")
                    await pilot.pause(0.3)
                    self.assertIn("团队默认不接管", str(result.render()))
                    self.assertNotIn(
                        "default_agent_user",
                        mock_save.call_args[0][0]["teams"]["team"])

    async def test_options_include_no_takeover_and_profiles(self) -> None:
        """选项包含 '不接管' + 全局 profile；标签非原始哨兵串。"""
        from textual.app import App

        with _mock_agent_user_data(self._data()):
            app = App()
            dialog = TeamDefaultAgentUserDialog(team_name="team")
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.app.push_screen(dialog)
                await pilot.pause(0.3)
                select = dialog.query_one("#team_default_select", Select)
                values = [v for _, v in select._options]
                self.assertIn(AGENT_USER_NONE, values, "应包含 '不接管'")
                self.assertIn("claude_p", values, "应包含全局 profile")
                label = next(
                    (label for label, v in select._options
                     if v == AGENT_USER_NONE), "")
                self.assertNotEqual(label, AGENT_USER_NONE,
                                    "标签不能用原始哨兵串 '__none__'")
                self.assertIn("不接管", label)


# ============================================================
# task4 — 全局 registry 读路径 + rename/delete sweep 纯 helper
# ------------------------------------------------------------
# 契约：全局 profiles 在 data['agent_users']；team.default_agent_user 引用
# 全局 key；member.agent_user = '' 回退 / '__none__' 不接管 / 全局 key。
# ============================================================

_GLOBAL_PROFILE_A = {"agent_type": "claude", "takeover_enabled": True,
                     "anthropic_api_key": "sk-a", "anthropic_base_url": "https://a",
                     "anthropic_model": "sonnet"}
_GLOBAL_PROFILE_B = {"agent_type": "codex", "takeover_enabled": False,
                     "openai_api_key": "sk-b", "openai_base_url": "https://b",
                     "codex_model": "gpt-4o"}


class AgentUserProfilesReadTests(unittest.TestCase):
    """_agent_user_profiles 委托 common list_agent_users：全局 + 团队旧数据合并。

    合并契约：全局 data['agent_users'] 与团队 team['agent_users'] 合并展示，
    键冲突时团队旧数据优先（迁移 R3 语义）；team_name 为空时仅返回全局视图。
    """

    def test_team_legacy_wins_on_key_conflict(self):
        """全局与团队旧数据键冲突 → 团队旧数据优先（迁移前后读一致）。"""
        data = {
            "agent_users": {"p1": dict(_GLOBAL_PROFILE_A)},
            "teams": {"teamA": {"agent_users": {"p1": dict(_GLOBAL_PROFILE_B)}}},
        }
        with _mock_agent_user_data(data):
            profiles = _agent_user_profiles("teamA")
        self.assertEqual(profiles["p1"], _GLOBAL_PROFILE_B,
                         "键冲突时团队旧数据优先")

    def test_legacy_team_merged_when_global_empty(self):
        """全局为空时，团队旧数据并入（不丢失未迁移 profiles）。"""
        data = {
            "teams": {"teamA": {"agent_users": {"p1": dict(_GLOBAL_PROFILE_A)}}},
        }
        with _mock_agent_user_data(data):
            profiles = _agent_user_profiles("teamA")
        self.assertEqual(profiles["p1"], _GLOBAL_PROFILE_A,
                         "全局为空时并入团队级旧数据")

    def test_global_and_team_merged_non_conflict(self):
        """全局 + 团队各有一条不同 key → 合并展示两者。"""
        data = {
            "agent_users": {"p1": dict(_GLOBAL_PROFILE_A)},
            "teams": {"teamA": {"agent_users": {"p2": dict(_GLOBAL_PROFILE_B)}}},
        }
        with _mock_agent_user_data(data):
            profiles = _agent_user_profiles("teamA")
        self.assertEqual(set(profiles), {"p1", "p2"})

    def test_empty_team_name_returns_global_view(self):
        """无 team_name（全局管理视图）→ 仅返回全局 registry，不合并团队旧数据。"""
        data = {
            "agent_users": {"p1": dict(_GLOBAL_PROFILE_A)},
            "teams": {"teamA": {"agent_users": {"p2": dict(_GLOBAL_PROFILE_B)}}},
        }
        with _mock_agent_user_data(data):
            profiles = _agent_user_profiles()  # 全局管理视图
        self.assertEqual(set(profiles), {"p1"}, "全局视图只含全局 registry")

    def test_empty_returns_empty(self):
        with _mock_agent_user_data({"teams": {}}):
            self.assertEqual(_agent_user_profiles(), {})


class AgentUserGlobalOptionsTests(unittest.TestCase):
    """_global_profile_options：纯 profile 列表，无系统默认/不接管。"""

    def test_only_profiles_with_badge(self):
        data = {
            "agent_users": {
                "p1": dict(_GLOBAL_PROFILE_A),
                "p2": dict(_GLOBAL_PROFILE_B),
                "legacy_p": {"anthropic_base_url": "https://old"},
            },
        }
        with _mock_agent_user_data(data):
            opts = _global_profile_options()
        values = [v for _, v in opts]
        self.assertEqual(set(values), {"p1", "p2", "legacy_p"})
        self.assertNotIn("", values, "不应包含系统默认")
        self.assertNotIn(AGENT_USER_NONE, values, "不应包含不接管")
        p1_label = next((label for label, v in opts if v == "p1"), "")
        self.assertIn("Claude", p1_label)
        legacy_label = next((label for label, v in opts if v == "legacy_p"), "")
        self.assertIn("Claude", legacy_label)  # legacy+url → 数据层推断 claude（不再一律旧版）


class AgentUserRenameSweepTests(unittest.TestCase):
    """_agent_user_rename_sweep：跨团队同步 default/member 引用与旧团队级存储。"""

    def _data(self):
        return {
            "agent_users": {"old": dict(_GLOBAL_PROFILE_A)},
            "teams": {
                "teamA": {
                    "default_agent_user": "old",
                    "members": {
                        "alice": {"role": "coder", "agent_user": "old"},
                        "bob": {"role": "tester", "agent_user": AGENT_USER_NONE},
                    },
                },
                "teamB": {
                    "members": {
                        "carol": {"role": "coder", "agent_user": "old"},
                    },
                },
                "teamC": {
                    "agent_users": {"old": dict(_GLOBAL_PROFILE_A)},
                },
            },
        }

    def test_sweeps_all_teams_default_and_member_refs(self):
        data = self._data()
        teams, members = _agent_user_rename_sweep(data, "old", "new")
        self.assertEqual((teams, members), (3, 2))
        self.assertEqual(data["teams"]["teamA"]["default_agent_user"], "new")
        self.assertEqual(data["teams"]["teamA"]["members"]["alice"]["agent_user"], "new")
        # 不接管哨兵不受 rename 影响
        self.assertEqual(
            data["teams"]["teamA"]["members"]["bob"]["agent_user"], AGENT_USER_NONE)
        self.assertEqual(data["teams"]["teamB"]["members"]["carol"]["agent_user"], "new")
        # 旧团队级存储一并迁移
        self.assertEqual(data["teams"]["teamC"]["agent_users"], {"new": _GLOBAL_PROFILE_A})

    def test_no_refs_is_noop(self):
        data = {"agent_users": {}, "teams": {"teamA": {"members": {}}}}
        teams, members = _agent_user_rename_sweep(data, "old", "new")
        self.assertEqual((teams, members), (0, 0))
        self.assertNotIn("default_agent_user", data["teams"]["teamA"])


class AgentUserDeleteSweepTests(unittest.TestCase):
    """_agent_user_delete_sweep：跨团队清除引用，成员回退团队默认。"""

    def _data(self):
        return {
            "agent_users": {"p1": dict(_GLOBAL_PROFILE_A)},
            "teams": {
                "teamA": {
                    "default_agent_user": "p1",
                    "members": {
                        "alice": {"role": "coder", "agent_user": "p1"},
                        "bob": {"role": "tester"},
                    },
                },
                "teamB": {
                    "members": {
                        "carol": {"role": "coder", "agent_user": "p1"},
                    },
                },
                "teamC": {
                    "agent_users": {"p1": dict(_GLOBAL_PROFILE_A)},
                },
            },
        }

    def test_sweeps_all_teams_and_removes_legacy(self):
        data = self._data()
        teams, members = _agent_user_delete_sweep(data, "p1")
        self.assertEqual((teams, members), (3, 2))
        self.assertNotIn("default_agent_user", data["teams"]["teamA"])
        self.assertNotIn("agent_user", data["teams"]["teamA"]["members"]["alice"])
        self.assertNotIn("agent_user", data["teams"]["teamB"]["members"]["carol"])
        self.assertNotIn("agent_users", data["teams"]["teamC"],
                         "旧团队级存储中的 p1 应移除")

    def test_ref_count(self):
        data = self._data()
        teams, members = _agent_user_ref_count(data, "p1")
        # teamA（default + alice）、teamB（carol）被计入；teamC 仅旧存储无引用不计
        self.assertEqual((teams, members), (2, 2))


if __name__ == "__main__":
    unittest.main()
