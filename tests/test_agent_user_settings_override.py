"""
Agent 用户接管 — 每终端 Claude --settings 覆盖（生产修复）
=========================================================

根因（leader 确认 + 实证）:
  Claude Code 用户级 ~/.claude/settings.json 的 env 块会覆盖普通进程 env，
  且遗留 ANTHROPIC_AUTH_TOKEN 优先于 ANTHROPIC_API_KEY；只有 --model(CLI 参数)
  不受 settings env 覆盖 → 终端里"仅 model 生效，ANTHROPIC_BASE_URL/key 未接管"。

修复:
  build_agent_user_claude_settings 为每个接管 Claude profile 的成员生成
  每终端独立的私有 --settings 文件（env 块设置 profile 的 API_KEY/BASE_URL/MODEL，
  并把 AUTH_TOKEN / ANTHROPIC_DEFAULT_* 等置空），优先级高于 user/project settings。
  未接管（系统默认 / __none__ / takeover 关闭 / 类型不匹配 / 非 claude typed）返回 ""，
  不破坏"使用系统默认"。三处权限生成器删除 Write(path)，只保留 Edit(path)。

敏感值约定: 全部使用 sentinel；真实 key 绝不打印。
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import common.data_layer as data_layer
import common.tmux_utils as ctu
import tui.tui_screens as tui_screens

SENT_KEY = "sk-sentinel-settings-test"
SENT_BASE = "https://profile.example.com"
SENT_MODEL = "sentinel-model-1"


def _typed_claude_data(root: Path, *, takeover=True, default_user="", members=None,
                       base_url=SENT_BASE, api_key=SENT_KEY, model=SENT_MODEL) -> dict:
    members = members if members is not None else {
        "lead": {"role": "leader", "agent": "claude", "agent_user": "p_claude"},
    }
    team = {
        "workspace_dir": str(root / "workspace"),
        "context_dir": str(root / "ctx"),
        "default_agent": "claude",
        "leader": "lead",
        "leader_type": "tmux",
        "monitor_enabled": False,
        "members": members,
    }
    if default_user:
        team["default_agent_user"] = default_user
    return {
        "agent_users": {
            "p_claude": {
                "agent_type": "claude",
                "takeover_enabled": takeover,
                "anthropic_api_key": api_key,
                "anthropic_base_url": base_url,
                "anthropic_model": model,
            },
        },
        "teams": {"team": team},
    }


class SettingsBuilderBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.data_file = self.home / "teams_data.json"
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self._old_home = os.environ.get("MULT_AGENT_MCP_HOME")
        data_layer.set_data_file(self.data_file)
        self.addCleanup(self._restore)

    def _restore(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        if self._old_home is None:
            os.environ.pop("MULT_AGENT_MCP_HOME", None)
        else:
            os.environ["MULT_AGENT_MCP_HOME"] = self._old_home
        self.tmp.cleanup()

    def _save(self, data: dict):
        self.data_file.write_text(json.dumps(data, ensure_ascii=False))


class AgentUserClaudeSettingsBuilderTests(SettingsBuilderBase):
    """build_agent_user_claude_settings 的语义与文件内容。"""

    def test_takeover_builds_settings_file_with_profile_env_and_clears(self):
        """接管 → 生成 --settings 文件：profile 三变量写入，AUTH_TOKEN/DEFAULT_* 置空。"""
        self._save(_typed_claude_data(self.root))
        path = ctu.build_agent_user_claude_settings("team", "lead")
        self.assertTrue(path)
        content = json.loads(Path(path).read_text())
        env = content["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], SENT_KEY)
        self.assertEqual(env["ANTHROPIC_BASE_URL"], SENT_BASE)
        self.assertEqual(env["ANTHROPIC_MODEL"], SENT_MODEL)
        # 遗留用户 token / DEFAULT_* 模型必须清空（否则优先于 profile key/base_url）
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "")
        self.assertEqual(env["ANTHROPIC_DEFAULT_SONNET_MODEL"], "")
        self.assertEqual(env["ANTHROPIC_DEFAULT_OPUS_MODEL"], "")
        self.assertEqual(env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], "")
        self.assertEqual(env["ANTHROPIC_REASONING_MODEL"], "")
        self.assertEqual(env["ANTHROPIC_SMALL_FAST_MODEL"], "")

    def test_settings_file_root_keys_only_claude_env(self):
        """生成文件根键只含 Claude 官方 settings 键 env，不得有自定义根字段。

        Claude Code 对 settings 有 schema 校验；未知根字段（如 _agent_user_key）
        可能使整个文件 invalid/被忽略 → 真实终端仍只有 model 生效。这是 P1 修复的
        关键门禁：根键必须 ⊆ Claude 支持的键，本实现只写 env。
        """
        self._save(_typed_claude_data(self.root))
        path = ctu.build_agent_user_claude_settings("team", "lead")
        content = json.loads(Path(path).read_text())
        self.assertEqual(set(content.keys()), {"env"},
                         f"根键必须只含 env，实际: {sorted(content.keys())}")
        self.assertNotIn("_agent_user_key", content, "自定义根字段不得写入 settings JSON")
        self.assertIsInstance(content["env"], dict)
        self.assertEqual(set(content["env"].keys()), set(ctu._CLAUDE_AGENT_USER_ENV_VARS))

    def test_takeover_settings_env_does_not_touch_openai(self):
        """接管 Claude profile 只处理 ANTHROPIC_*；OPENAI_* 不得出现在 settings 文件中
        （避免污染 Claude 的 Bash/MCP 子进程的 openai 环境）。"""
        self._save(_typed_claude_data(self.root))
        path = ctu.build_agent_user_claude_settings("team", "lead")
        content = json.loads(Path(path).read_text())
        env = content["env"]
        for key in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL", "CODEX_MODEL"):
            self.assertNotIn(key, env, f"claude settings 不应处理 {key}")

    def test_default_fallback_takeover_builds_settings(self):
        """回退 default_agent_user + takeover off → 完整接管，生成 settings。"""
        self._save(_typed_claude_data(
            self.root, takeover=False, default_user="p_claude",
            members={"lead": {"role": "leader", "agent": "claude"}}))
        path = ctu.build_agent_user_claude_settings("team", "lead")
        self.assertTrue(path, "default fallback 应生成 settings 文件")

    def test_none_returns_empty_system_default(self):
        """__none__ → 返回 ""，不破坏'使用系统默认'。"""
        self._save(_typed_claude_data(
            self.root, default_user="p_claude",
            members={"lead": {"role": "leader", "agent": "claude",
                              "agent_user": ctu.AGENT_USER_NONE}}))
        self.assertEqual(ctu.build_agent_user_claude_settings("team", "lead"), "")

    def test_explicit_takeover_off_returns_empty(self):
        """显式选择 takeover_enabled=False → 返回 ""（系统默认）。"""
        self._save(_typed_claude_data(
            self.root, takeover=False,
            members={"lead": {"role": "leader", "agent": "claude",
                              "agent_user": "p_claude"}}))
        self.assertEqual(ctu.build_agent_user_claude_settings("team", "lead"), "")

    def test_system_default_returns_empty(self):
        """成员无 agent_user 且团队无 default_agent_user → 返回 ""（系统默认）。"""
        self._save(_typed_claude_data(
            self.root,
            members={"lead": {"role": "leader", "agent": "claude"}}))
        self.assertEqual(ctu.build_agent_user_claude_settings("team", "lead"), "")

    def test_type_mismatch_returns_empty(self):
        """codex 成员 + claude profile → 类型不匹配 → 返回 ""（系统默认）。"""
        self._save(_typed_claude_data(
            self.root,
            members={"lead": {"role": "leader", "agent": "codex", "agent_user": "p_claude"}}))
        self.assertEqual(ctu.build_agent_user_claude_settings("team", "lead"), "")

    def test_codex_profile_returns_empty(self):
        """非 claude typed profile → 返回 ""。"""
        data = {
            "agent_users": {
                "p_codex": {"agent_type": "codex", "takeover_enabled": True,
                            "openai_api_key": "sk-c", "openai_base_url": "https://o.example.com",
                            "codex_model": "gpt-4o"},
            },
            "teams": {"team": {
                "workspace_dir": str(self.root / "ws"),
                "default_agent": "codex", "leader": "lead", "leader_type": "tmux",
                "members": {"lead": {"role": "leader", "agent": "codex",
                                     "agent_user": "p_codex"}}}},
        }
        self._save(data)
        self.assertEqual(ctu.build_agent_user_claude_settings("team", "lead"), "")

    def test_invalid_values_not_injected(self):
        """非法 URL/key/model（shell 元字符/空格）不得写入 settings —— 复用校验防线。"""
        data = _typed_claude_data(
            self.root,
            base_url="https://bad url with space",
            api_key="sk-bad;rm -rf",
            model="bad model $HOME",
        )
        self._save(data)
        path = ctu.build_agent_user_claude_settings("team", "lead")
        self.assertTrue(path)
        env = json.loads(Path(path).read_text())["env"]
        self.assertEqual(env["ANTHROPIC_API_KEY"], "", "非法 key 不得注入")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], "", "非法 URL 不得注入")
        self.assertEqual(env["ANTHROPIC_MODEL"], "", "非法 model 不得注入")

    def test_settings_dir_0700_file_0600(self):
        """目录权限 0700、文件 0600 —— 敏感凭据不暴露。"""
        self._save(_typed_claude_data(self.root))
        path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode), 0o700)

    def test_cross_team_same_member_no_collision(self):
        """不同团队同名成员 → 不同 settings 文件（含 team 分量，不碰撞）。"""
        data = _typed_claude_data(self.root)
        data["teams"]["teamB"] = dict(data["teams"]["team"])
        self._save(data)
        path_a = ctu.build_agent_user_claude_settings("team", "lead")
        path_b = ctu.build_agent_user_claude_settings("teamB", "lead")
        self.assertNotEqual(path_a, path_b, "跨团队同名成员必须用不同 settings 文件")

    def test_sanitize_collision_avoided(self):
        """消毒碰撞（a/b 与 a_b 消毒后同 base）由哈希后缀避免。"""
        data = _typed_claude_data(self.root)
        data["teams"]["team"]["members"]["a/b"] = {"role": "m", "agent": "claude", "agent_user": "p_claude"}
        data["teams"]["team"]["members"]["a_b"] = {"role": "m", "agent": "claude", "agent_user": "p_claude"}
        self._save(data)
        p1 = ctu.build_agent_user_claude_settings("team", "a/b")
        p2 = ctu.build_agent_user_claude_settings("team", "a_b")
        self.assertNotEqual(p1, p2, "消毒碰撞成员必须用不同 settings 文件")

    def test_sensitive_value_not_in_commandline(self):
        """key/base_url 值只进 settings 文件，绝不进 tmux 命令行。"""
        self._save(_typed_claude_data(self.root))
        settings_path = ctu.build_agent_user_claude_settings("team", "lead")
        args = ctu.claude_agent_args("claude", "auto", model=SENT_MODEL,
                                     settings_path=settings_path)
        joined = " ".join(args)
        self.assertIn("--settings", joined)
        self.assertNotIn(SENT_KEY, joined, "API key 值不得出现在命令行")
        self.assertNotIn(SENT_BASE, joined, "BASE_URL 值不得出现在命令行")


class ClaudeAgentArgsSettingsTests(unittest.TestCase):
    """claude_agent_args 注入 --settings 且不影响既有语义。"""

    def test_settings_flag_injected(self):
        args = ctu.claude_agent_args("claude", "auto", model="m1",
                                     settings_path="/x/private.json")
        self.assertIn("--settings", args)
        self.assertEqual(args[args.index("--settings") + 1], "/x/private.json")
        self.assertIn("--model", args)
        self.assertIn("--permission-mode", args)

    def test_no_settings_flag_when_empty(self):
        args = ctu.claude_agent_args("claude", "auto", model="m1")
        self.assertNotIn("--settings", args)
        self.assertIn("--model", args)


class TuiSpawnSettingsFlagTests(unittest.TestCase):
    """TUI leader/member claude spawn 命令携带 --settings 覆盖（typed 接管）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir(parents=True, exist_ok=True)
        self.data_file = self.home / "teams_data.json"
        self.workspace = str(self.root / "workspace")
        Path(self.workspace).mkdir(parents=True)
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        self.addCleanup(self._restore)
        self._build_team()

    def _restore(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _build_team(self):
        data = {
            "agent_users": {
                "p_claude": {"agent_type": "claude", "takeover_enabled": True,
                             "anthropic_api_key": SENT_KEY,
                             "anthropic_base_url": SENT_BASE,
                             "anthropic_model": SENT_MODEL},
            },
            "teams": {"team": {
                "workspace_dir": self.workspace,
                "context_dir": self.workspace,
                "default_agent": "claude",
                "default_agent_user": "p_claude",
                "leader": "lead",
                "leader_type": "tmux",
                "monitor_enabled": False,
                "members": {
                    "lead": {"role": "leader", "agent": "claude"},
                    "coder_a": {"role": "coder", "agent": "claude"},
                    "none_mem": {"role": "coder", "agent": "claude", "agent_user": ctu.AGENT_USER_NONE},
                },
            }},
        }
        self.data_file.write_text(json.dumps(data, ensure_ascii=False))

    def _run_launch(self):
        tmux_calls = []
        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "-V": return 0, "", ""
            if cmd[0] == "has-session": return 1, "", ""
            return 0, "", ""
        with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux):
            with mock.patch.object(ctu, "load_data", return_value=json.loads(self.data_file.read_text())):
                with mock.patch.object(tui_screens, "load_data", return_value=json.loads(self.data_file.read_text())):
                    with mock.patch.object(tui_screens, "save_data", return_value=None):
                        with mock.patch.object(tui_screens, "_tmux_session", return_value="mcp_team_test"):
                            with mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False):
                                with mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None):
                                    with mock.patch.object(tui_screens, "write_claude_mcp", return_value=""):
                                        with mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")):
                                            with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")):
                                                with mock.patch.object(tui_screens, "write_claude_permissions", return_value=""):
                                                    with mock.patch.object(tui_screens, "_remember_member_window_id", return_value=""):
                                                        with mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
                                                            ok, msg = tui_screens.launch_terminals("team")
        return ok, msg, tmux_calls

    def test_leader_and_member_get_settings_flag(self):
        ok, msg, calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        session_cmd = next(c for c in calls if c[0] == "new-session")
        joined = " ".join(str(x) for x in session_cmd)
        self.assertIn("--settings", joined, "leader(default fallback) 应携带 --settings")
        self.assertNotIn(SENT_KEY, joined, "key 值不得出现在命令行")
        lead_win = [c for c in calls if c[0] == "new-window" and "coder_a" in c]
        self.assertEqual(len(lead_win), 1)
        self.assertIn("--settings", " ".join(str(x) for x in lead_win[0]),
                      "default fallback 成员应携带 --settings")

    def test_none_member_no_settings_flag(self):
        ok, msg, calls = self._run_launch()
        self.assertTrue(ok, f"launch failed: {msg}")
        none_win = [c for c in calls if c[0] == "new-window" and "none_mem" in c]
        self.assertEqual(len(none_win), 1)
        joined = " ".join(str(x) for x in none_win[0])
        self.assertNotIn("--settings", joined, "__none__ 成员不覆盖，走系统默认")
        self.assertNotIn(SENT_KEY, joined)


class PurgeSettingsPrecisionTests(SettingsBuilderBase):
    """purge_agent_user_settings / sweep 清理闭环：精确限定 + 可恢复错误显式上报。

    边界要求（leader 门禁）：
      - 只作用于 .agent_user_settings/，绝不宽泛删除；
      - 只删文件名 profile 分量（含哈希）匹配的文件；不匹配（旧格式/任意命名）宁可保留；
      - team/member 精确限定不得误删跨团队同名成员；
      - 删除失败（可恢复错误）显式上报，不静默吞掉。
    """

    def _profile(self, key="p_claude"):
        data = _typed_claude_data(self.root)
        data["teams"]["team"]["members"]["lead"]["agent_user"] = key
        if key != "p_claude":
            data["agent_users"][key] = {"agent_type": "claude", "takeover_enabled": True,
                                        "anthropic_api_key": f"sk-{key}",
                                        "anthropic_base_url": f"https://{key}.example.com",
                                        "anthropic_model": f"model-{key}"}
        self._save(data)
        return data

    def test_purge_scoped_only_to_agent_user_settings(self):
        """只清理 .agent_user_settings/ 下的文件；数据目录其他文件即使同 profile 也不动。"""
        self._profile()
        settings_path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        self.assertTrue(settings_path.parent.name == ".agent_user_settings")
        decoy = self.home / "decoy_settings.json"  # 数据目录同级诱饵
        decoy.write_text(json.dumps({"env": {"ANTHROPIC_API_KEY": SENT_KEY}}))
        removed, failed = ctu.purge_agent_user_settings("p_claude")
        self.assertFalse(settings_path.exists(), "匹配的 settings 文件应被清理")
        self.assertTrue(decoy.exists(), "非 .agent_user_settings 路径绝不能被删除")
        self.assertEqual(removed, 1)
        self.assertEqual(failed, [])

    def test_purge_only_key_matched_files_no_broad_delete(self):
        """只删文件名 profile 分量匹配的文件；其他 profile 的 settings 文件不碰。"""
        data = self._profile()
        data["teams"]["team"]["members"]["coder_b"] = {"role": "coder", "agent": "claude",
                                                       "agent_user": "p_other"}
        self._save(data)
        path_a = Path(ctu.build_agent_user_claude_settings("team", "lead"))    # profile=p_claude
        path_b = Path(ctu.build_agent_user_claude_settings("team", "coder_b"))  # profile=p_other
        removed, failed = ctu.purge_agent_user_settings("p_claude")
        self.assertEqual(removed, 1)
        self.assertFalse(path_a.exists(), "匹配 profile 的 settings 应被清理")
        self.assertTrue(path_b.exists(), "其他 profile 的 settings 不得被宽泛删除")
        self.assertEqual(failed, [])

    def test_purge_team_narrowing_is_exact_no_cross_team_overmatch(self):
        """team 精确限定：跨团队同名成员（同 profile）只清理本团队文件。"""
        import copy
        data = self._profile()
        data["teams"]["teamB"] = copy.deepcopy(data["teams"]["team"])
        self._save(data)
        path_a = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        path_b = Path(ctu.build_agent_user_claude_settings("teamB", "lead"))
        removed, failed = ctu.purge_agent_user_settings("p_claude", team_name="team")
        self.assertEqual(removed, 1, "只应清理 team 的文件，不误删 teamB")
        self.assertFalse(path_a.exists())
        self.assertTrue(path_b.exists(), "teamB 同名成员文件不得被 team 精确限定误删")
        self.assertEqual(failed, [])

    def test_purge_member_narrowing_is_exact(self):
        """member 精确限定：只清理指定成员的文件，同队其他成员不碰。"""
        data = self._profile()
        data["teams"]["team"]["members"]["coder_b"] = {"role": "coder", "agent": "claude",
                                                       "agent_user": "p_claude"}
        self._save(data)
        path_a = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        path_b = Path(ctu.build_agent_user_claude_settings("team", "coder_b"))
        removed, failed = ctu.purge_agent_user_settings("p_claude", team_name="team",
                                                        member_name="lead")
        self.assertEqual(removed, 1)
        self.assertFalse(path_a.exists())
        self.assertTrue(path_b.exists(), "同队其他成员的 settings 不得被 member 精确限定误删")
        self.assertEqual(failed, [])

    def test_purge_surfaces_unlink_failure(self):
        """删除失败（可恢复错误）显式上报到 failed，不静默吞掉、不误删其他文件。"""
        self._profile()
        path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        real_unlink = Path.unlink

        def fake_unlink(self_obj, *a, **k):
            if str(self_obj) == str(path):
                raise OSError(13, "Permission denied")
            return real_unlink(self_obj, *a, **k)

        with mock.patch.object(Path, "unlink", fake_unlink):
            removed, failed = ctu.purge_agent_user_settings("p_claude")
        self.assertEqual(removed, 0, "删除失败的文件不应计入成功")
        self.assertEqual(failed, [str(path)], "删除失败必须显式上报")
        self.assertTrue(path.exists(), "删除失败的文件必须保留（旧凭据残留可见）")

    def test_purge_skips_corrupt_non_builder_file_without_deleting(self):
        """非 builder 命名的损坏文件（无法用文件名分量确认归属）→ 跳过，不删除、不误删。"""
        self._profile()
        path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        corrupt = path.parent / "team__corrupt__profile.json"  # 非规范 3 分量 + 哈希命名
        corrupt.write_text("not-json{{{")
        removed, failed = ctu.purge_agent_user_settings("p_claude")
        self.assertTrue(corrupt.exists(), "非规范命名的文件应保留而非删除")
        self.assertFalse(path.exists(), "正常匹配文件应被删除")
        self.assertEqual(removed, 1)
        self.assertEqual(failed, [])

    def test_purge_cleans_corrupt_file_with_valid_builder_name(self):
        """文件名分量能确认归属时，即使内容损坏也一并清理（文件名哈希即归属证明）。"""
        self._profile()
        path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        # 用同一规范命名覆盖为损坏内容（仍属 p_claude 的 settings 残留）
        path.write_text("not-json{{{")
        removed, failed = ctu.purge_agent_user_settings("p_claude")
        self.assertEqual(removed, 1, "规范命名 + 损坏内容的残留也应按文件名分量清理")
        self.assertFalse(path.exists())
        self.assertEqual(failed, [])

    def test_delete_sweep_cleans_residue_offline(self):
        """agent_user_delete_sweep 端到端：删除 profile 后旧 settings 残留被清理。"""
        self._profile()
        path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        data = data_layer.load_data()
        teams_aff, members_aff = ctu.agent_user_delete_sweep(data, "p_claude")
        self.assertFalse(path.exists(), "delete sweep 后旧凭据 settings 文件应被清理")
        self.assertEqual(teams_aff, 1)
        self.assertEqual(members_aff, 1)

    def test_rename_sweep_cleans_residue_offline(self):
        """agent_user_rename_sweep 端到端：重命名后旧 key 的 settings 残留被清理。"""
        self._profile()
        path = Path(ctu.build_agent_user_claude_settings("team", "lead"))
        data = data_layer.load_data()
        teams_aff, members_aff = ctu.agent_user_rename_sweep(data, "p_claude", "p_claude_v2")
        self.assertFalse(path.exists(), "rename sweep 后旧 key 的 settings 残留应被清理")
        self.assertEqual(teams_aff, 1)
        self.assertEqual(members_aff, 1)


@unittest.skipUnless(shutil.which("claude"), "需要本机真实 claude 二进制")
class RealClaudeSettingsAcceptanceTests(SettingsBuilderBase):
    """真实 claude 启动探针：生成的 --settings 文件必须被接受（不被 schema/解析拒绝）。

    实证结论（leader + 本机 2.1.221 探针）：claude --settings <file> doctor 对
    env-only 与含自定义根字段（_agent_user_key）的文件均退出成功，且都识别自定义
    ANTHROPIC_BASE_URL → 未知字段风险实测不构成当前阻塞。v4 仍移除该非官方字段，
    避免未来 schema 收紧，让生成文件严格符合契约。

    确定性主门禁是 test_settings_file_root_keys_only_claude_env（根键只含 env）。
    本探针用 doctor 子命令（离线可干净退出，不卡 auth）断言生成文件被接受。
    """

    def test_real_claude_accepts_generated_settings_file(self):
        self._save(_typed_claude_data(self.root))
        path = ctu.build_agent_user_claude_settings("team", "lead")
        self.assertTrue(path)
        # 确定性根键门禁（与单元测试一致，这里内联防止探针单独运行时漏掉）
        content = json.loads(Path(path).read_text())
        self.assertEqual(set(content.keys()), {"env"})

        fake_home = self.root / "fake_home"
        (fake_home / ".claude").mkdir(parents=True, exist_ok=True)
        env = dict(os.environ)
        for k in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
                  "ANTHROPIC_MODEL", "CLAUDE_CODE_ENTRYPOINT"):
            env.pop(k, None)
        env["HOME"] = str(fake_home)
        env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
        env["DISABLE_TELEMETRY"] = "1"
        env["CLAUDE_CODE_DISABLE_ONBOARDING"] = "1"
        env["ANTHROPIC_API_KEY"] = SENT_KEY
        env["ANTHROPIC_BASE_URL"] = "https://probe.invalid"

        err_file = self.root / "claude_stderr.txt"
        try:
            r = subprocess.run(
                ["claude", "--settings", path, "doctor"],
                stdout=subprocess.DEVNULL, stderr=err_file.open("w"),
                env=env, cwd=str(self.root), text=True, timeout=30,
            )
        except subprocess.TimeoutExpired:
            self.fail("真实 claude doctor 30s 超时（settings 文件加载卡住）")
        err = err_file.read_text() if err_file.exists() else ""
        low = err.lower()
        # doctor 对有效 --settings 文件应干净退出（离线时 rc=0，仅报未连 API）
        self.assertEqual(r.returncode, 0,
                         f"真实 claude doctor 非零退出（settings 被拒）: {err[:300]}")
        self.assertNotIn(str(path), err,
                         f"真实 claude 因该 settings 文件报错: {err[:300]}")
        for marker in ("unable to load settings", "failed to parse settings",
                       "invalid settings file", "could not read settings"):
            self.assertNotIn(marker, low, f"真实 claude 拒绝 settings: {marker}")


@unittest.skipUnless(shutil.which("tmux"), "需要真实 tmux 环境")
class RealTmuxSettingsOverrideE2ETests(unittest.TestCase):
    """真实 tmux 终端验收：接管 spawn 命令含 --settings 私有文件，环境不泄旧凭据。

    用隔离 TMUX_TMPDIR + fake claude 包装脚本，不启动真实 API 会话、不打印真实 key。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="settings_e2e_")
        self.home = Path(self.tmp) / "home"; self.home.mkdir()
        self.tmux_tmpdir = Path(self.tmp) / "tmux"; self.tmux_tmpdir.mkdir()
        self.ws = Path(self.tmp) / "workspace"; self.ws.mkdir()
        self.bindir = Path(self.tmp) / "bin"; self.bindir.mkdir()
        # 隔离 HOME，避免污染真实 ~/.claude/settings.json
        self.fake_home = Path(self.tmp) / "fake_home"
        (self.fake_home / ".claude").mkdir(parents=True)
        (self.fake_home / ".claude" / "settings.json").write_text(json.dumps({
            "env": {"ANTHROPIC_BASE_URL": "https://USER-SETTING.example.com",
                    "ANTHROPIC_AUTH_TOKEN": "sk-USER-TOKEN",
                    "ANTHROPIC_MODEL": "user-default-model"}}))
        self._old = {k: os.environ.get(k) for k in
                     ("MULT_AGENT_MCP_HOME", "TMUX_TMPDIR", "HOME", "PATH", "TMUX")}
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        os.environ["MULT_AGENT_MCP_HOME"] = str(self.home)
        os.environ["TMUX_TMPDIR"] = str(self.tmux_tmpdir)
        os.environ["HOME"] = str(self.fake_home)
        os.environ["PATH"] = str(self.bindir) + ":" + os.environ.get("PATH", "")
        os.environ.pop("TMUX", None)
        self._stripped = [k for k in os.environ if k.startswith("ANTHROPIC") or k.startswith("OPENAI")]
        for k in self._stripped:
            os.environ.pop(k)
        data_layer.set_data_file(self.home / "teams_data.json")
        self.data_file = self.home / "teams_data.json"

    def tearDown(self):
        ctu.tmux_run(["kill-session", "-t", "mcp_team"])
        for k, v in self._old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k in self._stripped:
            os.environ.pop(k, None)
        data_layer._DATA_FILE_OVERRIDE = self._old_override

    def _save(self, data):
        self.data_file.write_text(json.dumps(data, ensure_ascii=False))

    def test_spawn_injects_settings_and_no_key_in_cmdline(self):
        data = _typed_claude_data(Path(self.tmp), default_user="p_claude", members={
            "lead": {"role": "leader", "agent": "claude"},
        })
        self._save(data)
        fake = self.bindir / "claude"
        fake.write_text("#!/bin/sh\nenv > /tmp/settings_e2e_env.txt 2>/dev/null\n"
                        "echo \"$@\" > /tmp/settings_e2e_args.txt\nexit 0\n")
        fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
        rc, err, _ = ctu.tmux_spawn_member("mcp_team", "lead", "claude", str(self.ws),
                                           new_session=True)
        self.assertEqual(rc, 0, err)
        import time
        time.sleep(0.8)
        args = Path("/tmp/settings_e2e_args.txt").read_text()
        self.assertIn("--settings", args, "真实 tmux 终端应携带 --settings")
        self.assertNotIn(SENT_KEY, args, "key 值不得出现在真实命令行")
        spath = args.split("--settings")[1].split()[0]
        env = json.loads(Path(spath).read_text())["env"]
        self.assertEqual(env["ANTHROPIC_AUTH_TOKEN"], "", "遗留 AUTH_TOKEN 必须清空")
        self.assertEqual(env["ANTHROPIC_BASE_URL"], SENT_BASE)


if __name__ == "__main__":
    unittest.main()
