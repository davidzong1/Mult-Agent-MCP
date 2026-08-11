"""
Claude Code 权限分类器暂时不可用 —— fallback 门禁测试（模式严格限定）
================================================================

背景：Claude 原生 ``plan`` / ``auto`` 权限模式用「权限分类器」判定工具安全性。
分类器暂时不可用（provider 抖动 / 瞬时 API 错误）时，需要判定的工具
（Bash / Write / Edit）被**硬阻断**并报：

    "<model> is temporarily unavailable, so auto mode cannot determine
     the safety of X"

若监控把停在错误后的终端误判 idle，``mark_idle_done`` 会把未完成任务误标
完成 → 丢失 checkpoint/session 上下文（全员锁死事故残留层）。

coder 已落地 common/classifier_fallback.py 的两层 fallback：
  1. 预授权（settings 层）：仅对**映射到原生 plan 的目标模式**追加**精选安全**
     allow，使常规 Bash / workspace 内 Edit 不再查分类器；危险命令绝不放行。
  2. 检测+审计+恢复（监控层）：detect 签名 → classify 判
     ``classifier_unavailable``（绝不 idle → 绝不 mark_idle_done）；
     进出/恢复写审计；观察式恢复。

模式限定（**修正语义，以 Claude Code v2.1.227 bundle 实证为准**）：
  - 目标 = 仅「映射到原生 plan」的模式：plan / planning / readonly / read_only。
  - **非目标 = 成员 auto（→原生 acceptEdits）、acceptEdits / default / manual /
    空。** 成员 auto 的 CLI ``--permission-mode`` 是 acceptEdits，acceptEdits 下
    Bash/Write/Edit 不依赖分类器判定（编辑自动放行、其余走普通 approval prompt，
    由监控授权），因此 **auto 必须断言不注入 fallback**（settings 与 allowedTools
    均不追加），行为零变化。

本测试为**针对性门禁**，严格按 5 条验收 + 模式限定边界：
  A. classifier 正常 → 保持原安全策略；
  B. unavailable → 显式、可审计、可恢复 fallback；
  C. 危险命令（rm/curl/sudo/chmod/eval…）不能因 fallback 无条件放行；
  D. 失败不丢 checkpoint/session 上下文；
  E. 覆盖 normal/unavailable/recovery × bash/write × managed leader/member
     × 模式矩阵（plan/acceptEdits/default/manual）不外溢。

隔离：纯函数层无副作用；监控层用临时 teams_data + mock tmux / capture /
authorize，绝不触真实凭证、真实 tmux、真实 ~/.codex、真实会话。
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import classifier_fallback as cf
from common import data_layer

# ---------------------------------------------------------------------------
# 签名与反例文本（对应真实 CLI 错误形态）
# ---------------------------------------------------------------------------

SIG_AUTO_BASH = (
    "Anthropic is temporarily unavailable, so auto mode cannot determine "
    "the safety of Bash"
)
SIG_PLAN_EDIT = (
    "claude-3-5-sonnet is temporarily unavailable, so plan mode cannot "
    "determine the safety of Edit"
)
SIG_COULD_NOT = (
    "Anthropic is temporarily unavailable, so auto mode could not "
    "determine the safety of Write"
)
SIG_UNABLE = (
    "Anthropic is temporarily unavailable, so auto mode was unable to "
    "determine the safety of Bash"
)
# 2026-08-11 真实 headless probe 复现（auto 模式、workspace 外 Write）：分类器
# 模型=主模型（probe 为 deepseek-v4-flash[1m]，生产为 claude-opus-5[1m]），消息
# 含 JGd 完整后缀 "right now. Wait briefly..."。
SIG_EXACT_PROBE = (
    "deepseek/deepseek-v4-flash[1m] is temporarily unavailable, so auto mode "
    "cannot determine the safety of Write right now. Wait briefly and then try "
    "this action again. If it keeps failing, continue with other tasks that "
    "don't require this action and come back to it later."
)
# 反例：正常 idle / approval / busy / quota / 无关错误
IDLE_FIXTURE = "✻ Brewed for 5s\n❯\n⏸ manual mode on"
APPROVAL_FIXTURE = "This command requires approval\nDo you want to proceed?\n❯ 1. Yes"
BUSY_FIXTURE = "Running Bash: git fetch\n...\n◼"
QUOTA_FIXTURE = "HTTP 429: rate limited\n❯"
UNRELATED_ERR = "FileNotFoundError: no such file\n❯"
# 危险命令形态（fallback 绝不能放行）
DANGEROUS_SUBSTRINGS = (
    "rm",
    "sudo",
    "curl",
    "wget",
    "chmod",
    "chown",
    "mv",
    "cp",
    "pip",
    "npm",
    "make",
    "find -delete",
    "kill",
    "dd",
    "eval",
    "> /dev/sda",
)
UNSAFE_PATTERNS = ("Bash(*)", "Edit(*)", "Edit(**)")

# ---------------------------------------------------------------------------
# 模式限定 / 签名检测 / 安全 allow（纯函数层）
# ---------------------------------------------------------------------------


class TestClassifierModeGate(unittest.TestCase):
    """验收 E：fallback 严格限定「映射到原生 plan」的模式；auto→acceptEdits 非目标。"""

    def test_only_plan_native_is_limited(self):
        # 修正语义（v2.1.227 实证）：仅映射到原生 plan 的模式是目标。
        # 成员 auto → 原生 acceptEdits（非目标）；原生 auto 本项目不产生（无 CLI
        # --permission-mode auto 路径），因此 gate 只认 native "plan"。
        self.assertTrue(cf.is_classifier_limited_mode("plan"))
        for non_target in ("auto", "acceptEdits", "accept_edits", "default",
                           "manual", ""):
            self.assertFalse(cf.is_classifier_limited_mode(non_target), repr(non_target))

    def test_allow_patterns_only_for_plan_mapped_modes(self):
        # 目标 = 映射到原生 plan 的模式：plan/planning/readonly/read_only 都注入。
        for target in ("plan", "planning", "readonly", "read_only"):
            pats = cf.classifier_fallback_allow_patterns("/tmp/ws", target)
            self.assertTrue(pats, target)
            self.assertIn("Edit(/tmp/ws/*)", pats, target)
            self.assertIn("Bash(git:*)", pats, target)

    def test_allow_patterns_empty_for_non_target_modes(self):
        # 非目标：auto（→acceptEdits）/acceptEdits/default/manual/"" 一律空，
        # 不注入 fallback（acceptEdits 行为零变化）。
        for non_target in ("auto", "acceptEdits", "accept_edits", "default",
                           "manual", ""):
            self.assertEqual(
                cf.classifier_fallback_allow_patterns("/tmp/ws", non_target), [],
                repr(non_target),
            )

    def test_native_mode_mapping_consistent_with_cli(self):
        # 成员 "auto" 仍映射到 acceptEdits（无 CLI auto 路径）；plan/planning/
        # readonly → plan；manual/default/"" → default。映射本身不变，gate 用的是
        # 映射结果（仅 plan 为目标）。
        self.assertEqual(cf.claude_native_permission_mode("auto"), "acceptEdits")
        self.assertEqual(cf.claude_native_permission_mode("accept_edits"), "acceptEdits")
        self.assertEqual(cf.claude_native_permission_mode("plan"), "plan")
        self.assertEqual(cf.claude_native_permission_mode("planning"), "plan")
        self.assertEqual(cf.claude_native_permission_mode("readonly"), "plan")
        self.assertEqual(cf.claude_native_permission_mode("manual"), "default")
        self.assertEqual(cf.claude_native_permission_mode(""), "default")


class TestClassifierFallbackNoUnsafeAllow(unittest.TestCase):
    """验收 C：危险命令不能因 fallback 无条件放行（仅目标模式 plan 有 fallback）。"""

    def test_fallback_never_contains_destructive_bash(self):
        # 目标模式（plan 及同义）的 fallback 白名单绝不包含危险命令。
        for target in ("plan", "planning", "readonly"):
            pats = cf.classifier_fallback_allow_patterns("/tmp/ws", target)
            joined = " ".join(pats).lower()
            for bad in DANGEROUS_SUBSTRINGS:
                self.assertNotIn(bad + ":", joined,
                                 f"{target}: 危险命令 {bad} 出现在 fallback allow")

    def test_fallback_never_contains_wildcard_allow(self):
        for target in ("plan", "planning", "readonly"):
            pats = cf.classifier_fallback_allow_patterns("/tmp/ws", target)
            for unsafe in UNSAFE_PATTERNS:
                self.assertFalse(any(unsafe == p or unsafe in p for p in pats),
                                 f"{target}: 越界放行 {unsafe} -> {pats}")

    def test_fallback_is_selective_whitelist(self):
        # 精选安全 Bash 白名单：只读检查 + 仓库内测试，不含可执行任意脚本。
        whitelist = set(cf.CLAUDE_FALLBACK_BASH_PATTERNS)
        self.assertIn("Bash(git:*)", whitelist)
        self.assertIn("Bash(python3 -m pytest:*)", whitelist)
        self.assertIn("Bash(grep:*)", whitelist)
        # 白名单规模小且全部是受限前缀
        self.assertLessEqual(len(whitelist), 20)
        for p in whitelist:
            self.assertTrue(p.startswith("Bash("), p)


class TestDetectClassifierUnavailable(unittest.TestCase):
    """验收 B：签名检测对 model 名 / 模式词 / 时态容错。"""

    def test_standard_auto_bash_signature(self):
        self.assertTrue(cf.detect_classifier_unavailable(SIG_AUTO_BASH))

    def test_plan_mode_variant(self):
        self.assertTrue(cf.detect_classifier_unavailable(SIG_PLAN_EDIT))

    def test_could_not_and_was_unable_variants(self):
        self.assertTrue(cf.detect_classifier_unavailable(SIG_COULD_NOT))
        self.assertTrue(cf.detect_classifier_unavailable(SIG_UNABLE))

    def test_embedded_in_larger_output(self):
        out = "Previous tool output...\n" + SIG_AUTO_BASH + "\n❯"
        self.assertTrue(cf.detect_classifier_unavailable(out))

    def test_exact_probe_signature_detected(self):
        """真实 headless probe 复现（auto + workspace 外 Write）整句命中：
        分类器模型=主模型、含 JGd 完整后缀 right now/Wait briefly。"""
        self.assertTrue(cf.detect_classifier_unavailable(SIG_EXACT_PROBE))

    def test_normal_terminal_never_detected(self):
        for text in (IDLE_FIXTURE, APPROVAL_FIXTURE, BUSY_FIXTURE,
                     QUOTA_FIXTURE, UNRELATED_ERR, ""):
            self.assertFalse(cf.detect_classifier_unavailable(text), repr(text))

    def test_detection_does_not_misjudge_non_target(self):
        """检测层不误判非目标：缺少任一稳定核心词（temporarily unavailable /
        cannot|unable / determine / safety）都不算签名。"""
        for text in (
            "cannot determine the safety of Bash",           # 无 temporarily unavailable
            "temporarily unavailable, please retry",          # 无 determine/safety
            "temporarily unavailable to determine X",         # 无 the safety
            "provider is down, so auto mode cannot determine safety",  # 无 temporarily unavailable
            "temporarily unavailable\ncannot determine the safety of Edit",  # 换行断开
            "Something else is temporarily unavailable, but we can determine safety",
        ):
            self.assertFalse(cf.detect_classifier_unavailable(text), repr(text))


class TestClassifierUnavailableClassification(unittest.TestCase):
    """验收 B+E：classify 层把**任何模式**下出现的分类器签名判为
    ``classifier_unavailable``（检测与 assumed 原生模式解耦，签名自证；
    allow 层仍 plan-only，见 settings / spawn 矩阵测试）。"""

    def test_member_plan_signature_classified(self):
        self.assertEqual(
            mcp._classify_terminal_output(SIG_PLAN_EDIT, native_mode="plan"),
            "classifier_unavailable")

    def test_member_plan_signature_text_variant(self):
        # 文本中的模式词可变（"auto mode cannot determine" 同样命中稳定核心签名），
        # 门控由 native_mode 参数决定，不由文本措辞决定。
        self.assertEqual(
            mcp._classify_terminal_output(SIG_AUTO_BASH, native_mode="plan"),
            "classifier_unavailable")

    def test_member_plan_signature_beats_idle_words(self):
        # 签名 + 底部 ❯/mode-on 常驻 → 仍 classifier_unavailable（检测在 idle 之前）
        out = SIG_PLAN_EDIT + "\n❯\nplan mode on"
        self.assertEqual(
            mcp._classify_terminal_output(out, native_mode="plan"),
            "classifier_unavailable")

    def test_signature_detected_all_modes(self):
        # 2026-08-11 语义修正：签名是原生 auto 分类器专用、**自证**的消息；检测与
        # assumed 原生模式解耦 → 任何模式出现签名一律 classifier_unavailable
        # （绝不 idle → 绝不 mark_idle_done → 不丢上下文）。allow 层仍 plan-only，
        # 见 TestSettingsModeLimited / spawn 矩阵测试。
        for native in ("acceptEdits", "default", "manual", "auto", "plan", ""):
            self.assertEqual(
                mcp._classify_terminal_output(SIG_AUTO_BASH, native_mode=native),
                "classifier_unavailable", f"member native={native!r}")
            self.assertEqual(
                mcp._classify_leader_terminal_output(SIG_PLAN_EDIT, native_mode=native),
                "classifier_unavailable", f"leader native={native!r}")

    def test_unknown_mode_safe_guard_detects(self):
        # 未知 native_mode（""）→ 安全护栏：检测生效（绝不把分类器停滞终端误标
        # idle → 绝不 mark_idle_done 丢上下文）。
        self.assertEqual(
            mcp._classify_terminal_output(SIG_AUTO_BASH, native_mode=""),
            "classifier_unavailable")

    def test_leader_plan_signature_classified(self):
        self.assertEqual(
            mcp._classify_leader_terminal_output(SIG_PLAN_EDIT, native_mode="plan"),
            "classifier_unavailable")

    def test_normal_classify_unchanged(self):
        self.assertEqual(mcp._classify_terminal_output(IDLE_FIXTURE), "idle")
        self.assertEqual(mcp._classify_terminal_output(APPROVAL_FIXTURE), "approval")
        self.assertEqual(mcp._classify_terminal_output(BUSY_FIXTURE), "busy")
        self.assertEqual(mcp._classify_leader_terminal_output(IDLE_FIXTURE), "idle")
        self.assertEqual(mcp._classify_leader_terminal_output(APPROVAL_FIXTURE), "approval")


# ---------------------------------------------------------------------------
# 隔离基类：临时 teams_data + mock tmux / capture / authorize
# ---------------------------------------------------------------------------


class _IsolatedFallbackTestCase(unittest.TestCase):
    """temp teams_data 隔离 + mock tmux 惯例（与既有分类测试一致）。"""

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
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
        }
        self.old_env = {k: os.environ.get(k) for k in (
            "MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD",
            "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR",
        )}
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, *, leader_agent="claude", member_agent="claude",
                   alice_mode="plan", leader_mode="plan", alice_done=False,
                   with_session_id=True):
        workspace = self.root / "workspace"
        share = self.root / "contexts"
        workspace.mkdir(exist_ok=True)
        share.mkdir(exist_ok=True)
        alice = {
            "role": "coder", "agent": member_agent, "work_mode": alice_mode,
            "last_task": "implement the widget", "last_task_completed": alice_done,
        }
        if with_session_id:
            alice["session_id"] = "11111111-2222-3333-4444-555555555555"
        lead = {"role": "leader", "agent": leader_agent, "work_mode": leader_mode}
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(share),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "leader_wakeup_config": {
                        "enabled": True, "idle_threshold": 2,
                        "approval_alert": True, "auto_authorize_first": True,
                        "cooldown_cycles": 3, "max_wakeups_per_session": 10,
                    },
                    "members": {"lead": lead, "alice": alice},
                }
            }
        })

    # ---- 公共 mock 封装 ----
    def _scan_member(self, capture_out, *, auto_authorize_choice="", fresh=True,
                     alice_mode="plan"):
        """调用 _scan_member_terminal，mock 终端捕获。返回 (result, sent_auth, data)。"""
        sent_auth = []
        if fresh:
            self._save_team(alice_mode=alice_mode)
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window",
                                   side_effect=lambda session, window, lines=120: (0, capture_out, "")):
                with mock.patch.object(mcp, "_member_window_target",
                                       side_effect=lambda team_name, member_name: member_name):
                    with mock.patch.object(mcp, "_send_authorization_choice",
                                           side_effect=lambda *a, **k: sent_auth.append(a) or (0, "")):
                        result = mcp._scan_member_terminal(
                            "team", "alice", auto_authorize_choice=auto_authorize_choice)
        return result, sent_auth, mcp._load()

    def _scan_leader(self, capture_out, *, fresh=True):
        """调用 _scan_leader_terminal，mock 终端捕获。返回 (result, data)。"""
        if fresh:
            self._save_team()
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_capture_window",
                                   side_effect=lambda session, window, lines=120: (0, capture_out, "")):
                with mock.patch.object(mcp, "_member_window_target",
                                       side_effect=lambda team_name, member_name: member_name):
                    result = mcp._scan_leader_terminal("team", lines=120)
        return result, mcp._load()

    def _audit_lines(self):
        audit = Path(mcp._share_dir("team")) / cf.CLASSIFIER_FALLBACK_AUDIT_FILE
        if not audit.is_file():
            return []
        return [json.loads(ln) for ln in audit.read_text(encoding="utf-8").splitlines() if ln.strip()]


# ---------------------------------------------------------------------------
# settings 层：模式限定追加精选 allow（验收 E / B / C）
# ---------------------------------------------------------------------------


class TestSettingsModeLimited(_IsolatedFallbackTestCase):
    """验收 E+B+C：_write_claude_permissions 仅对映射到原生 plan 的模式追加 fallback。"""

    def _settings_allow(self, mode):
        self._save_team()
        path = mcp._write_claude_permissions("team", mode=mode)
        with open(path) as f:
            return json.load(f)["permissions"].get("allow", [])

    def test_plan_mode_appends_fallback(self):
        allow = self._settings_allow("plan")
        self.assertIn("Edit(%s/*)" % mcp._team_dir("team"), allow)
        self.assertIn("Bash(git:*)", allow)
        self.assertIn("Bash(python3 -m pytest:*)", allow)
        # 原策略保留（Edit 裸规则仍在）
        self.assertIn("Edit", allow)

    def test_auto_mode_does_not_inject_fallback(self):
        # 修正语义：成员 auto → 原生 acceptEdits 非目标 → settings 不追加 fallback，
        # acceptEdits 行为零变化。Bash(pwd:*) 是仅 fallback 追加的标记（base 不含）。
        base = self._settings_allow("")  # 非目标基线
        allow = self._settings_allow("auto")
        self.assertEqual(allow, base, "auto 不应注入 fallback（acceptEdits 行为零变化）")
        self.assertNotIn("Bash(pwd:*)", allow)
        self.assertNotIn("Bash(python3 -m pytest:*)", allow)

    def test_auto_accept_edits_default_manual_no_spillover(self):
        # 非目标全组：auto/acceptEdits/default/manual/"" 一律与基线一致，零外溢。
        base = self._settings_allow("")  # mode 缺省 = 不外溢基线
        for mode in ("auto", "acceptEdits", "accept_edits", "default", "manual", ""):
            self.assertEqual(self._settings_allow(mode), base,
                             f"mode={mode!r} 不应改变 settings（fallback 外溢）")

    def test_plan_settings_never_contain_unsafe_allow(self):
        allow = self._settings_allow("plan")
        joined = " ".join(allow).lower()
        for unsafe in UNSAFE_PATTERNS:
            self.assertNotIn(unsafe.lower(), joined, f"越界放行 {unsafe}")
        for bad in DANGEROUS_SUBSTRINGS:
            self.assertNotIn("bash(" + bad + ":", joined, f"危险命令 {bad}")

    # ---- spawn 接线矩阵：plan/acceptEdits/default/manual × managed leader/member ----

    def _spawn_member_capture(self, alice_mode):
        """spawn alice 成员，返回 --allowedTools 值（空串=无该参数）。"""
        self._save_team(alice_mode=alice_mode)
        tmux_cmds = []

        def fake_tmux(cmd, timeout=10):
            tmux_cmds.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\totherwin", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions",
                                   side_effect=lambda team, **kw: str(self.root / "s.json")):
                with mock.patch.object(mcp, "claude_agent_user_launch",
                                       return_value=(["A=1"], str(self.root / "settings.json"))):
                    rc, _, err = mcp._tmux_spawn_member(
                        "team", "alice", "claude", str(self.root / "workspace"))
        self.assertEqual(rc, 0, err)
        for cmd in tmux_cmds:
            if "--allowedTools" in cmd:
                return cmd[cmd.index("--allowedTools") + 1]
        return ""

    def test_spawn_member_plan_gets_fallback_allowed_tools(self):
        tools = self._spawn_member_capture("plan")
        self.assertIn("Bash(pwd:*)", tools)
        self.assertIn("Bash(git:*)", tools)
        self.assertIn("Edit(%s/*)" % (self.root / "workspace"), tools)

    def test_spawn_member_auto_no_fallback_allowed_tools(self):
        tools = self._spawn_member_capture("auto")
        self.assertNotIn("Bash(pwd:*)", tools, "auto 成员不应注入 fallback")
        self.assertNotIn("Bash(python3 -m pytest:*)", tools)
        # base 保留（裸 Bash/Edit + MCP 前缀）
        self.assertIn("Bash", tools.split(","))
        self.assertIn("Edit", tools.split(","))

    def test_spawn_member_default_manual_no_fallback(self):
        for mode in ("default", "manual"):
            tools = self._spawn_member_capture(mode)
            self.assertNotIn("Bash(pwd:*)", tools, f"{mode} 成员不应注入 fallback")
            self.assertNotIn("Bash(git:*)", tools)

    def _launch_leader_capture(self, leader_mode):
        """launch managed leader，返回 leader 的 --allowedTools 值。"""
        self._save_team(leader_mode=leader_mode)
        tmux_cmds = []

        def fake_tmux(cmd, timeout=10):
            tmux_cmds.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp",
                                   return_value=str(self.root / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", side_effect=lambda *a, **k: (0, "")):
                            with mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda *a, **k: (0, "")):
                                with mock.patch.object(mcp, "_write_claude_permissions",
                                                       side_effect=lambda team, **kw: str(self.root / "s.json")):
                                    with mock.patch.object(mcp, "claude_agent_user_launch",
                                                           return_value=(["A=1"], str(self.root / "leader_settings.json"))):
                                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                                            result = mcp.launch_team_terminals("team", task="t")
        self.assertIn("终端已启动", result)
        for cmd in tmux_cmds:
            if "--allowedTools" in cmd:
                return cmd[cmd.index("--allowedTools") + 1]
        return ""

    def test_leader_plan_gets_fallback_allowed_tools(self):
        tools = self._launch_leader_capture("plan")
        self.assertIn("Bash(pwd:*)", tools)
        self.assertIn("Bash(git:*)", tools)
        self.assertIn("mcp__mult-agent-mcp__leader_*", tools)

    def test_leader_auto_no_fallback_allowed_tools(self):
        tools = self._launch_leader_capture("auto")
        self.assertNotIn("Bash(pwd:*)", tools, "auto leader 不应注入 fallback")
        self.assertNotIn("Bash(python3 -m pytest:*)", tools)

    def test_leader_default_manual_no_fallback(self):
        for mode in ("default", "manual"):
            tools = self._launch_leader_capture(mode)
            self.assertNotIn("Bash(pwd:*)", tools, f"{mode} leader 不应注入 fallback")
            self.assertNotIn("Bash(git:*)", tools)

    def test_spawn_member_passes_mode(self):
        """成员 spawn 路径把 work_mode 传到 settings writer（接线证据）。"""
        self._save_team()  # alice work_mode=plan
        seen = {}

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\totherwin", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_permissions",
                                   side_effect=lambda team, **kw: seen.update(kw) or str(self.root / "s.json")):
                with mock.patch.object(mcp, "claude_agent_user_launch",
                                       return_value=(["A=1"], str(self.root / "settings.json"))):
                    rc, _, err = mcp._tmux_spawn_member(
                        "team", "alice", "claude", str(self.root / "workspace"))
        self.assertEqual(rc, 0, err)
        self.assertEqual(seen.get("mode"), "plan",
                         "成员 spawn 必须把 work_mode 传入 _write_claude_permissions")

    def test_leader_spawn_passes_mode(self):
        """managed leader spawn 把 leader_mode 传到 settings writer。"""
        self._save_team(leader_mode="plan")  # leader work_mode=plan
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            if cmd[0] == "new-session":
                return 0, "", ""
            return 0, "", ""

        seen = {}
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_write_claude_mcp",
                                   return_value=str(self.root / "contexts" / ".claude" / "mcp.json")):
                with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                    with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")):
                        with mock.patch.object(mcp, "_send_keys", side_effect=lambda *a, **k: (0, "")):
                            with mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda *a, **k: (0, "")):
                                with mock.patch.object(mcp, "_write_claude_permissions",
                                                       side_effect=lambda team, **kw: seen.update(kw) or str(self.root / "s.json")):
                                    with mock.patch.object(mcp, "claude_agent_user_launch",
                                                           return_value=(["A=1"], str(self.root / "settings.json"))):
                                        with mock.patch.object(mcp.time, "sleep", return_value=None):
                                            result = mcp.launch_team_terminals("team", task="t")
        self.assertIn("终端已启动", result)
        self.assertEqual(seen.get("mode"), "plan",
                         "managed leader spawn 必须把 leader_mode 传入 _write_claude_permissions")


# ---------------------------------------------------------------------------
# 监控层（成员）：unavailable 审计 + 上下文保留 + 危险命令不放行（验收 B/C/D）
# ---------------------------------------------------------------------------


class TestMemberMonitorClassifierFallback(_IsolatedFallbackTestCase):
    """验收 B+C+D：成员 classifier_unavailable 状态机。"""

    def test_member_unavailable_marked_and_audited(self):
        result, _auth, data = self._scan_member(SIG_AUTO_BASH)
        self.assertEqual(result["state"], "classifier_unavailable")
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["blocked_reason"], "classifier_unavailable")
        self.assertTrue(member.get("last_blocked_ts"))
        events = self._audit_lines()
        entered = [e for e in events if e["state"] == "entered"]
        self.assertEqual(len(entered), 1)
        self.assertEqual(entered[0]["scope"], "member")
        self.assertEqual(entered[0]["member"], "alice")
        self.assertEqual(entered[0]["mode"], "plan")

    def test_member_unavailable_keeps_context(self):
        """验收 D：失败不丢 checkpoint/session 上下文。"""
        result, _auth, data = self._scan_member(SIG_AUTO_BASH)
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_task"], "implement the widget")
        self.assertEqual(member["session_id"], "11111111-2222-3333-4444-555555555555")
        self.assertFalse(member["last_task_completed"])
        # 不 mark_idle_done：未完成任务不被误标完成
        self.assertNotEqual(result["state"], "idle")

    def test_member_unavailable_does_not_auto_authorize(self):
        """验收 C：危险命令不放行 —— classifier_unavailable 不触发授权注入。"""
        _result, sent_auth, _data = self._scan_member(SIG_AUTO_BASH)
        self.assertEqual(sent_auth, [], "classifier_unavailable 不应注入任何授权选择")

    def test_auto_member_signature_detected_keeps_context(self):
        """2026-08-11 语义修正：auto 成员（→acceptEdits）出现签名 → 判
        classifier_unavailable + blocked_reason + 审计 entered，绝不 mark_idle_done
        （不丢 checkpoint/session 上下文）——P0 底线。"""
        result, _auth, data = self._scan_member(SIG_AUTO_BASH, alice_mode="auto")
        self.assertEqual(result["state"], "classifier_unavailable")
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member.get("blocked_reason"), "classifier_unavailable")
        self.assertEqual(member.get("last_task_completed", True), False,
                         "绝不 mark_idle_done（保留未完成任务）")
        events = self._audit_lines()
        self.assertEqual([e["state"] for e in events], ["entered"],
                         "auto 成员出现签名应写 entered 审计")

    def test_member_recovery_audits_and_clears(self):
        """验收 B：签名消失（观察式恢复）→ 审计 recovered + 清 blocked_reason。"""
        # 第一轮：进入 unavailable（写入 last_observed_state + 审计 entered）
        result, _auth, _data = self._scan_member(SIG_AUTO_BASH)
        self.assertEqual(result["state"], "classifier_unavailable")
        # 第二轮：签名消失（正常 idle）→ recovered + 清 blocked_reason
        result, _auth, data = self._scan_member(IDLE_FIXTURE, fresh=False)
        self.assertEqual(result["state"], "idle")
        member = data["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["last_observed_state"], "idle")
        self.assertNotIn("blocked_reason", member)
        events = self._audit_lines()
        self.assertEqual([e["state"] for e in events],
                         ["entered", "recovered"],
                         [e["state"] for e in events])


# ---------------------------------------------------------------------------
# 监控层（leader）：unavailable 审计 + 不 enter_resting（验收 B/E）
# ---------------------------------------------------------------------------


class TestLeaderMonitorClassifierFallback(_IsolatedFallbackTestCase):
    """验收 B+E：managed leader 的 classifier_unavailable 状态机。"""

    def test_leader_unavailable_audited_and_not_resting(self):
        result, data = self._scan_leader(SIG_PLAN_EDIT)
        self.assertEqual(result["state"], "classifier_unavailable")
        self.assertNotIn(result["action"], ("idle-streak", "resting", "enter_resting"))
        leader_state = data["teams"]["team"].get("leader_state", "active")
        self.assertNotEqual(leader_state, "resting", "classifier_unavailable 不应让 leader 入睡")
        events = self._audit_lines()
        entered = [e for e in events if e["state"] == "entered" and e["scope"] == "leader"]
        self.assertEqual(len(entered), 1, events)
        self.assertEqual(entered[0]["member"], "lead")

    def test_leader_idle_streak_not_incremented(self):
        # 分类器不可用期间 idle_streak 不累加 → 不会误 enter_resting
        _result, data = self._scan_leader(SIG_PLAN_EDIT)
        self.assertEqual(data["teams"]["team"].get("leader_idle_streak", 0), 0)

    def test_leader_recovery_audits(self):
        # 先进入 unavailable（写入 leader_last_observed_state + 审计 entered）
        result, _data = self._scan_leader(SIG_PLAN_EDIT)
        self.assertEqual(result["state"], "classifier_unavailable")
        # 再正常 idle → recovered（scope=leader）
        result, _data = self._scan_leader(IDLE_FIXTURE, fresh=False)
        self.assertEqual(result["state"], "idle")
        events = self._audit_lines()
        states = [e["state"] for e in events if e["scope"] == "leader"]
        self.assertIn("entered", states)
        self.assertIn("recovered", states)

    def test_auto_leader_signature_classifier_unavailable(self):
        """验收（2026-08-11 语义修正）：auto leader（→acceptEdits）出现分类器签名
        → 判 classifier_unavailable + 审计 entered + 绝不 enter_resting。"""
        self._save_team(leader_mode="auto")
        result, data = self._scan_leader(SIG_AUTO_BASH, fresh=False)
        self.assertEqual(result["state"], "classifier_unavailable")
        self.assertNotEqual(
            data["teams"]["team"].get("leader_state", "active"), "resting",
            "auto leader 出现签名不得入睡（不丢上下文）")
        self.assertEqual(data["teams"]["team"].get("leader_idle_streak", 0), 0)
        events = self._audit_lines()
        entered = [e for e in events if e["state"] == "entered" and e["scope"] == "leader"]
        self.assertEqual(len(entered), 1, events)
        self.assertEqual(entered[0]["mode"], "auto")

    def test_auto_leader_recovery_audits(self):
        """验收：auto leader 签名消失（观察式恢复）→ recovered 审计，恢复后行为不变。"""
        self._save_team(leader_mode="auto")
        result, _d = self._scan_leader(SIG_AUTO_BASH, fresh=False)
        self.assertEqual(result["state"], "classifier_unavailable")
        result, _d = self._scan_leader(IDLE_FIXTURE, fresh=False)
        self.assertEqual(result["state"], "idle")
        states = [e["state"] for e in self._audit_lines() if e["scope"] == "leader"]
        self.assertEqual(states, ["entered", "recovered"], states)


# ---------------------------------------------------------------------------
# 汇总状态 / summary 面：classifier_unavailable 视为活跃非 idle（验收 D）
# ---------------------------------------------------------------------------


class TestMemberClassifierBusyGates(_IsolatedFallbackTestCase):
    """验收 D：classifier_unavailable 成员被当作活跃阻塞，不被拉入空闲活动。"""

    def test_member_is_busy_for_discussion_when_classifier_unavailable(self):
        # 讨论模式跳过 busy 成员：classifier_unavailable 成员不得被当作空闲拉入。
        member = {"last_observed_state": "classifier_unavailable",
                  "last_task": "work", "last_task_completed": False}
        self.assertTrue(mcp._member_is_busy_for_discussion(member))

    def test_classify_signature_is_not_idle(self):
        # 复证：classify 层绝不把签名文本判为 idle（monitor 不会 mark_idle_done）。
        self.assertNotEqual(mcp._classify_terminal_output(SIG_AUTO_BASH), "idle")


if __name__ == "__main__":
    unittest.main()
