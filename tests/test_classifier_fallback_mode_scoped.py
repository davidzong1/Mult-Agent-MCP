"""
Claude Code 权限分类器暂时不可用 —— 模式限定 fallback 的 --allowedTools 层测试
================================================================================

背景：common/classifier_fallback.py 提供两层 fallback——(1) settings 层（团队共享
.claude/settings.json 追加精选安全 allow，仅 plan/auto）；(2) 检测+审计+恢复（监控
层）。本文件补上第三层：**每终端 --allowedTools** 模式限定（plan/auto 追加精选安全
窄规则，manual/default/其他原样），并验证：

  A. 正常（classifier 可用）→ 窄规则惰性存在（安全命令本就被放行，其余仍走分类器）；
  B. unavailable（classifier 暂时不可用）→ 窄规则命中即绕过分类器 → 安全命令可执行、
     危险命令仍被阻断（不在集内）；
  C. recovery → 同一配置幂等（重入 spawn/构造结果一致，观察式恢复无需改配置）；
  D. 覆盖 bash/write、managed leader/member、TUI 三处 spawn 接线；
  E. 模式限定：plan（及映射原生 plan 的 planning/readonly）有 fallback；
     auto（→原生 acceptEdits）/acceptEdits/manual/default 无 → 不外溢。

隔离：纯函数层无副作用；spawn 层用临时 teams_data + mock tmux，绝不触真实凭证、
真实 tmux、真实 ~/.codex、真实会话。
"""

import copy
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import classifier_fallback as cf
from common import data_layer
from common import tmux_utils as tu

# 与 gate 测试一致的危险命令形态
DANGEROUS_SUBSTRINGS = (
    "rm", "sudo", "curl", "wget", "chmod", "chown", "mv", "cp",
    "pip", "npm", "make", "find -delete", "kill", "dd", "> /dev/sda",
)
UNSAFE_PATTERNS = ("Bash(*)", "Edit(*)", "Edit(**)")

# F1（2026-08-12，leader 批准）后基座 = MCP 前缀 + 精选安全 Bash pattern（与
# classifier_fallback.CLAUDE_FALLBACK_BASH_PATTERNS 一致；无裸 Bash=Bash(*) 泄漏，
# 无裸 Edit——scoped Edit(<ws>/*) 由 claude_terminal_allow_tools 无条件追加，
# G1 实证覆盖 Write 新建）。
MEMBER_BASE = [
    "mcp__mult-agent-mcp__member_*",
    "mcp__mult_agent_mcp__member_*",
    "Bash(git:*)",
    "Bash(pwd:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(echo:*)",
    "Bash(wc:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(grep:*)",
    "Bash(which:*)",
    "Bash(whoami:*)",
    "Bash(date:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(python3 -m unittest:*)",
    "Bash(python3 -m compileall:*)",
]
LEADER_BASE = [
    "mcp__mult-agent-mcp__leader_*",
    "mcp__mult_agent_mcp__leader_*",
    "Bash(git:*)",
    "Bash(pwd:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(echo:*)",
    "Bash(wc:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(grep:*)",
    "Bash(which:*)",
    "Bash(whoami:*)",
    "Bash(date:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(python3 -m unittest:*)",
    "Bash(python3 -m compileall:*)",
]


# ---------------------------------------------------------------------------
# 纯函数层：claude_terminal_allow_tools 模式限定矩阵（验收 A/B/C/E）
# ---------------------------------------------------------------------------


class TestTerminalAllowToolsModeScoped(unittest.TestCase):
    """验收 E：F1 后安全 Bash 是**基座**（所有模式共享），仅 plan 追加 fallback 去重
    后可能与基座相同；裸 Bash/Edit 绝无（裸 Bash=Bash(*) 泄漏）。"""

    def test_auto_mode_does_not_inject_fallback(self):
        # F1 后：auto 成员的安全基座含 scoped Edit(ws/*) + 安全 Bash（基座共享），
        # 不额外注入 plan fallback；裸 Bash/Edit 绝无。
        tools = cf.claude_terminal_allow_tools("auto", "/ws", MEMBER_BASE)
        self.assertIn("Edit(/ws/*)", tools, "F1 后 scoped Edit 在基座")
        self.assertIn("Bash(pwd:*)", tools, "F1 后安全 Bash 在基座")
        self.assertNotIn("Bash", tools, "F1 后不得含裸 Bash")
        self.assertNotIn("Edit", tools, "F1 后不得含裸 Edit")

    def test_plan_mode_appends_fallback(self):
        tools = cf.claude_terminal_allow_tools("plan", "/ws", MEMBER_BASE)
        self.assertIn("Edit(/ws/*)", tools)
        self.assertIn("Bash(ls:*)", tools)
        for b in MEMBER_BASE:
            self.assertIn(b, tools)
        # F1：plan 追加的 fallback 去重后与基座一致（不重复、不裸放行）
        self.assertNotIn("Bash", tools, "F1 后不得含裸 Bash")
        self.assertNotIn("Edit", tools, "F1 后不得含裸 Edit")

    def test_manual_default_empty_unchanged_no_spillover(self):
        # F1 后：manual/default/""/acceptEdits 与 auto 同为安全基座，不外溢、不裸放行。
        base_auto = cf.claude_terminal_allow_tools("auto", "/ws", MEMBER_BASE)
        for mode in ("manual", "default", "", "acceptEdits"):
            got = cf.claude_terminal_allow_tools(mode, "/ws", MEMBER_BASE)
            self.assertEqual(got, base_auto, f"mode={mode!r} fallback 外溢")
            self.assertNotIn("Bash", got, f"mode={mode!r} 不得含裸 Bash")
            self.assertNotIn("Edit", got, f"mode={mode!r} 不得含裸 Edit")

    def test_leader_base_also_mode_scoped(self):
        # plan leader 追加 fallback；auto（→acceptEdits）与 manual 同为安全基座。
        tools = cf.claude_terminal_allow_tools("plan", "/ws", LEADER_BASE)
        self.assertIn("Bash(pwd:*)", tools)
        self.assertIn("mcp__mult-agent-mcp__leader_*", tools)
        self.assertNotIn("member_*", " ".join(tools))
        self.assertIn("Edit(/ws/*)", cf.claude_terminal_allow_tools("auto", "/ws", LEADER_BASE))
        self.assertIn("Edit(/ws/*)", cf.claude_terminal_allow_tools("manual", "/ws", LEADER_BASE))

    def test_never_contains_dangerous_or_wildcard(self):
        for mode in ("plan", "planning", "readonly"):
            tools = cf.claude_terminal_allow_tools(mode, "/ws", MEMBER_BASE)
            joined = " ".join(tools).lower()
            for bad in DANGEROUS_SUBSTRINGS:
                self.assertNotIn("bash(" + bad + ":", joined,
                                 f"{mode}: 危险命令 {bad} 出现在 fallback allow")
            for unsafe in UNSAFE_PATTERNS:
                self.assertNotIn(unsafe.lower(), joined, f"{mode}: 越界放行 {unsafe}")

    def test_recovery_identical_config(self):
        """验收 C：recovery 无需改配置 —— 重复构造结果一致（幂等）。"""
        for mode in ("plan", "auto", "manual"):
            a = cf.claude_terminal_allow_tools(mode, "/ws", MEMBER_BASE)
            b = cf.claude_terminal_allow_tools(mode, "/ws", MEMBER_BASE)
            self.assertEqual(a, b, f"mode={mode}: 重复构造应一致")


class TestSettingsLayerModeScoped(unittest.TestCase):
    """验收 E：settings 层 auto 模式绝不注入 fallback（不因任何 native 转换误判）。"""

    def test_allow_patterns_reject_member_mode_auto(self):
        # 修正语义：auto→acceptEdits 非目标 → 空；plan 同义仍注入（对照）。
        self.assertEqual(cf.classifier_fallback_allow_patterns("/ws", "auto"), [])
        self.assertEqual(cf.classifier_fallback_allow_patterns("/ws", "acceptEdits"), [])
        self.assertIn("Bash(pwd:*)", cf.classifier_fallback_allow_patterns("/ws", "plan"))
        self.assertIn("Bash(pwd:*)", cf.classifier_fallback_allow_patterns("/ws", "readonly"))


# ---------------------------------------------------------------------------
# 隔离基类：临时 teams_data + mock tmux 惯例（与 gate 测试一致）
# ---------------------------------------------------------------------------


class _IsolatedSpawnTestCase(unittest.TestCase):
    """temp teams_data 隔离 + mock tmux 惯例。"""

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

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, *, alice_mode="auto", leader_mode="manual"):
        workspace = self.root / "workspace"
        share = self.root / "contexts"
        workspace.mkdir(exist_ok=True)
        share.mkdir(exist_ok=True)
        alice = {"role": "coder", "agent": "claude", "work_mode": alice_mode,
                 "last_task": "implement the widget", "last_task_completed": False}
        lead = {"role": "leader", "agent": "claude", "work_mode": leader_mode}
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(share),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {"lead": lead, "alice": alice},
                }
            }
        })
        return workspace, share

    @staticmethod
    def _allowed_tools_from_cmds(cmds) -> str:
        """从 mock tmux 命令列表中提取 claude spawn 的 --allowedTools 值。"""
        for cmd in cmds:
            if "--allowedTools" in cmd:
                return cmd[cmd.index("--allowedTools") + 1]
        return ""


# ---------------------------------------------------------------------------
# MCP 成员 spawn：_tmux_spawn_member 的 --allowedTools 模式限定（验收 D/E）
# ---------------------------------------------------------------------------


class TestMemberSpawnWiring(_IsolatedSpawnTestCase):
    """验收 D+E：成员 spawn 的 --allowedTools 按 mode 追加/不追加 fallback。"""

    def _spawn_member(self, mode):
        self._save_team(alice_mode=mode)
        tmux_cmds = []

        def fake_tmux(cmd, timeout=10):
            tmux_cmds.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\totherwin", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "claude_agent_user_launch",
                                   return_value=(["A=1"], str(self.root / "settings.json"))):
                rc, _, err = mcp._tmux_spawn_member(
                    "team", "alice", "claude", str(self.root / "workspace"))
        self.assertEqual(rc, 0, err)
        return tmux_cmds

    def test_member_auto_no_fallback_in_allowed_tools(self):
        # 修正语义 + F1：auto→acceptEdits 非目标；安全 Bash 是基座（auto 也含 scoped
        # Edit + 安全 Bash），但不带额外 plan fallback；裸 Bash/Edit 绝无。
        tools = self._allowed_tools_from_cmds(self._spawn_member("auto"))
        self.assertIn("Bash(pwd:*)", tools, "F1 后安全 Bash 在基座（auto 也含）")
        self.assertIn("Edit(%s/*)" % (self.root / "workspace"), tools, "F1 后 scoped Edit 在基座")
        self.assertNotIn("Bash", tools.split(","), "F1 后不得含裸 Bash")
        self.assertNotIn("Edit", tools.split(","), "F1 后不得含裸 Edit")

    def test_member_plan_gets_fallback(self):
        tools = self._allowed_tools_from_cmds(self._spawn_member("plan"))
        self.assertIn("Bash(pwd:*)", tools)
        self.assertIn("Bash(git:*)", tools)

    def test_member_manual_no_fallback_no_leak(self):
        # F1 后：manual 成员同为安全基座（含 scoped Edit + 安全 Bash），无额外 plan
        # fallback；裸 Bash/Edit 绝无。
        tools = self._allowed_tools_from_cmds(self._spawn_member("manual"))
        self.assertIn("Bash(pwd:*)", tools, "F1 后安全 Bash 在基座（manual 也含）")
        self.assertIn("Edit(%s/*)" % (self.root / "workspace"), tools)
        self.assertNotIn("Bash", tools.split(","), "F1 后不得含裸 Bash")
        self.assertNotIn("Edit", tools.split(","), "F1 后不得含裸 Edit")

    def test_member_auto_never_contains_dangerous(self):
        tools = self._allowed_tools_from_cmds(self._spawn_member("auto"))
        joined = tools.lower()
        for bad in DANGEROUS_SUBSTRINGS:
            self.assertNotIn("bash(" + bad + ":", joined, f"危险命令 {bad}")


# ---------------------------------------------------------------------------
# MCP managed leader spawn：launch_team_terminals 的 leader --allowedTools
# ---------------------------------------------------------------------------


class TestLeaderSpawnWiring(_IsolatedSpawnTestCase):
    """验收 D+E：managed leader --allowedTools 按模式限定（plan 追加 fallback，
    auto/manual 原样）。"""

    def _launch_leader(self, leader_mode):
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
                                with mock.patch.object(mcp, "claude_agent_user_launch",
                                                       return_value=(["A=1"], str(self.root / "leader_settings.json"))):
                                    with mock.patch.object(mcp.time, "sleep", return_value=None):
                                        result = mcp.launch_team_terminals("team", task="t")
        self.assertIn("终端已启动", result)
        return tmux_cmds

    def test_leader_plan_gets_fallback(self):
        tools = self._allowed_tools_from_cmds(self._launch_leader("plan"))
        self.assertIn("Bash(pwd:*)", tools)
        self.assertIn("Bash(git:*)", tools)
        self.assertIn("mcp__mult-agent-mcp__leader_*", tools)

    def test_leader_auto_no_fallback(self):
        # F1 后：auto leader 同享安全基座（含 scoped Edit + 安全 Bash），无额外 plan fallback。
        tools = self._allowed_tools_from_cmds(self._launch_leader("auto"))
        self.assertIn("Bash(pwd:*)", tools, "F1 后安全 Bash 在基座（auto leader 也含）")
        self.assertIn("Bash(git:*)", tools)

    def test_leader_manual_no_fallback(self):
        # F1 后：manual leader 同为安全基座（含安全 Bash）。
        tools = self._allowed_tools_from_cmds(self._launch_leader("manual"))
        self.assertIn("Bash(pwd:*)", tools, "F1 后安全 Bash 在基座（manual leader 也含）")
        self.assertIn("Bash(git:*)", tools)


# ---------------------------------------------------------------------------
# 正常 / unavailable / recovery 语义（验收 A/B/C）
# ---------------------------------------------------------------------------


class TestNormalUnavailableRecoverySemantics(unittest.TestCase):
    """验收 A+B+C：fallback 是静态、惰性、幂等的安全 allow。

    - normal：安全命令本就被 base 放行（含窄规则后依然惰性），非安全命令仍走
      分类器（分类器可用 → 正常判定）；
    - unavailable：窄规则命中即绕过分类器 → 安全命令不硬阻断；未命中命令仍走
      分类器 → outage 下保持阻断（危险命令不在集内）；
    - recovery：同一配置，无需任何变更（观察式恢复）。
    """

    def test_safe_commands_covered_classifier_independent(self):
        """安全命令首词在窄规则前缀集内 → 不查分类器即可执行（unavailable 不硬阻断）。
        F1 后安全 Bash 是**基座**（所有模式共享），plan/auto/manual 均含本文件基座
        中的安全前缀；裸 Bash/Edit 绝无。"""
        for mode in ("plan", "planning", "readonly", "auto", "manual"):
            tools = cf.claude_terminal_allow_tools(mode, "/ws", MEMBER_BASE)
            joined = ",".join(tools)
            for first in ("git", "ls", "cat", "echo", "pwd", "grep",
                          "python3 -m pytest"):
                self.assertIn(f"Bash({first}:*)", joined,
                              f"{mode}: 缺安全前缀 {first}")
            self.assertNotIn("Bash", tools, f"{mode}: F1 后不得含裸 Bash")
            self.assertNotIn("Edit", tools, f"{mode}: F1 后不得含裸 Edit")

    def test_unsafe_commands_never_covered(self):
        for mode in ("plan", "planning", "readonly"):
            tools = cf.claude_terminal_allow_tools(mode, "/ws", MEMBER_BASE)
            joined = ",".join(tools).lower()
            for bad in DANGEROUS_SUBSTRINGS:
                self.assertNotIn("bash(" + bad + ":", joined, f"{mode}: {bad} 被放行")


# ---------------------------------------------------------------------------
# TUI 并行 spawn 路径：tui_screens.launch_terminals 同样按模式限定
# ---------------------------------------------------------------------------


class TestTuiSpawnWiring(_IsolatedSpawnTestCase):
    """验收 D+E：TUI 是独立于 MCP 的第三条 spawn 路径，同样携带模式限定 fallback。"""

    def _tui_spawn(self, leader_mode="plan", alice_mode="auto", bob_mode="manual",
                   settings_seen=None):
        import tui.tui_screens as tui_screens

        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        store = {
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(self.root / "contexts"),
                    "leader": "lead",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude", "work_mode": leader_mode},
                        "alice": {"role": "coder", "agent": "claude", "work_mode": alice_mode},
                        "bob": {"role": "tester", "agent": "claude", "work_mode": bob_mode},
                    },
                }
            }
        }
        tmux_cmds = []

        def fake_load_data():
            # 与真实 load_data 一致：每次返回独立 dict（避免 save_data 的
            # clear+update 把同一对象清空——data 与 store 同引用时 store 变空）
            return copy.deepcopy(store)

        def fake_save_data(data):
            store.clear()
            store.update(copy.deepcopy(data))

        def fake_write_permissions(*args, **kwargs):
            if settings_seen is not None:
                settings_seen.update(kwargs)
            return str(workspace / ".claude" / "settings.json")

        def fake_tmux_run(cmd, timeout=10):
            tmux_cmds.append(cmd)
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(tui_screens, "load_data", side_effect=fake_load_data):
            with mock.patch.object(tui_screens, "save_data", side_effect=fake_save_data):
                with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "ok")):
                    with mock.patch.object(tui_screens, "write_claude_permissions",
                                           side_effect=fake_write_permissions):
                        with mock.patch.object(tui_screens, "shutil") as fake_shutil:
                            fake_shutil.which.side_effect = lambda name: name
                            with mock.patch.object(tui_screens, "claude_agent_user_launch",
                                                   return_value=([], "")):
                                with mock.patch.object(tui_screens, "team_workspace_dir",
                                                       return_value=str(workspace)):
                                    with mock.patch.object(tui_screens, "_remember_member_window_id",
                                                           return_value=None):
                                        with mock.patch.object(tui_screens, "_send_keys",
                                                               side_effect=lambda *a, **k: (0, "")):
                                            with mock.patch.object(tui_screens, "_confirm_prompt_submission",
                                                                   side_effect=lambda *a, **k: (0, "")):
                                                with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux_run):
                                                    with mock.patch("time.sleep", return_value=None):
                                                        ok, msg = tui_screens.launch_terminals("team")
        self.assertTrue(ok, msg)
        tools_by_member = {}
        for cmd in tmux_cmds:
            if "--allowedTools" not in cmd:
                continue
            if "-n" in cmd:
                member = cmd[cmd.index("-n") + 1]
            else:
                member = "session"
            tools_by_member[member] = cmd[cmd.index("--allowedTools") + 1]
        return tools_by_member

    def test_tui_leader_plan_gets_fallback(self):
        tools = self._tui_spawn()
        self.assertIn("Bash(pwd:*)", tools.get("lead", ""))
        self.assertIn("Bash(git:*)", tools.get("lead", ""))

    def test_tui_member_auto_no_fallback(self):
        # F1 后：TUI auto 成员同享安全基座（scoped Edit + 安全 Bash），无裸 Bash/Edit。
        tools = self._tui_spawn()
        alice_tools = tools.get("alice", "")
        self.assertIn("Bash(pwd:*)", alice_tools, "F1 后安全 Bash 在基座（auto 成员也含）")
        self.assertIn("Edit(%s/*)" % (self.root / "workspace"), alice_tools)
        self.assertNotIn("Bash", alice_tools.split(","), "F1 后不得含裸 Bash")
        self.assertNotIn("Edit", alice_tools.split(","), "F1 后不得含裸 Edit")

    def test_tui_member_manual_no_fallback(self):
        # F1 后：manual 成员同为安全基座（含安全 Bash）。
        tools = self._tui_spawn()
        bob_tools = tools.get("bob", "")
        self.assertIn("Bash(pwd:*)", bob_tools, "F1 后安全 Bash 在基座（manual 成员也含）")
        self.assertIn("Bash(git:*)", bob_tools)

    def test_tui_leader_plan_passes_mode_to_settings_writer(self):
        """TUI launch_terminals 把 settings writer 的 mode 传为**团队 union 有效模式**
        （对齐 MCP launch_team_terminals 的 mode=team_classifier_effective_mode）。
        leader=plan → union=plan → settings 层追加分类器 fallback。"""
        seen = {}
        self._tui_spawn(leader_mode="plan", settings_seen=seen)
        self.assertEqual(seen.get("mode"), "plan",
                         "TUI 团队 union mode=plan 必须传入 write_claude_permissions")

    def test_tui_leader_manual_passes_mode_manual(self):
        """manual 团队（无 plan 成员）→ union 有效模式为空（非目标），settings
        不外溢（保留既有语义；G2 后不再传 leader 单一模式，避免按 leader 串权）。"""
        seen = {}
        self._tui_spawn(leader_mode="manual", settings_seen=seen)
        self.assertEqual(seen.get("mode"), "",
                         "全 manual 团队 union 有效模式应为空（非 plan 不外溢）")


class TestDualArgBuilderConsistency(unittest.TestCase):
    """验收：双 arg 构造入口一致（MCP 侧 ``_claude_agent_args`` vs
    TUI/tmux_utils 侧 ``claude_agent_args``）——permission-mode 映射 + classifier
    fallback allow 接线逐字一致。"""

    def test_permission_mode_mapping_identical(self):
        for mode, expect_perm in (("auto", "acceptEdits"), ("plan", "plan"),
                                  ("manual", None), ("", None)):
            mcp_args = mcp._claude_agent_args("claude", mode)
            tu_args = tu.claude_agent_args("claude", mode)
            self.assertEqual(mcp_args, tu_args, f"mode={mode!r}")
            if expect_perm is None:
                self.assertNotIn("--permission-mode", mcp_args, f"mode={mode!r}")
            else:
                self.assertIn("--permission-mode", mcp_args, f"mode={mode!r}")
                self.assertIn(expect_perm, mcp_args, f"mode={mode!r}")

    def test_fallback_allow_consistency_plan_yes_auto_no(self):
        # 两入口经 claude_terminal_allow_tools 后对 plan 追加 fallback、auto 不追加，
        # 且两入口结果逐字一致；危险命令/全量放行绝不在 --allowedTools。
        base = ["mcp__mult-agent-mcp__leader_*", "Bash", "Edit"]
        for mode, expect_fallback in (("plan", True), ("auto", False)):
            mcp_args = mcp._claude_agent_args(
                "claude", mode,
                allowed_tools=cf.claude_terminal_allow_tools(mode, "/ws", base))
            tu_args = tu.claude_agent_args(
                "claude", mode,
                allowed_tools=cf.claude_terminal_allow_tools(mode, "/ws", base))
            self.assertEqual(mcp_args, tu_args, f"mode={mode!r}")
            joined = ",".join(mcp_args)
            has_fallback = "Bash(pwd:*)" in joined
            self.assertEqual(has_fallback, expect_fallback,
                             f"mode={mode!r} fallback 追加与预期不符: {joined}")
            for unsafe in ("Bash(*)", "Edit(*)", "Write(*)"):
                self.assertNotIn(unsafe, joined, f"mode={mode!r} 越界放行")

    def test_settings_allow_consistent_plan_yes_others_no(self):
        # settings 层同口径：plan 追加 fallback 标记，auto/manual/default/"" 不追加。
        for mode, expect in (("plan", True), ("auto", False),
                             ("acceptEdits", False), ("manual", False), ("", False)):
            pats = cf.classifier_fallback_allow_patterns("/ws", mode)
            self.assertEqual(bool(pats), expect, f"mode={mode!r}")
            if expect:
                self.assertIn("Edit(/ws/*)", pats)
                self.assertIn("Bash(git:*)", pats)


# ===========================================================================
# G2 团队 union 有效模式（task2：共享 settings 不按 leader 串权、不随 spawn 顺序翻转）
# ===========================================================================


class TestTeamUnionEffectiveMode(unittest.TestCase):
    """``team_classifier_effective_mode``：共享 settings 的确定性团队 union 模式。

    根因（task2 G2 真机+代码实证）：共享 settings.json 被工作目录下所有 Claude
    进程加载，只能承载一个模式；旧实现按 leader 单一模式写（TUI）或按当前 spawn
    成员 mode 写（MCP _tmux_spawn_member），混合团队会随 spawn 顺序 last-writer-wins
    翻转或按 leader 串权（leader auto + member plan 时 plan 成员 settings 层缺
    fallback）。本 helper 取团队 union：任一 claude 成员映射原生 plan → "plan"，
    否则 ""。确定性、不依赖 spawn 顺序 / leader 身份。"""

    def test_mixed_leader_auto_member_plan_union_plan(self):
        """leader auto + member plan → union=plan（plan 成员 settings 层被覆盖，
        不再因 leader auto 缺 fallback）。"""
        members = {
            "lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "plan"},
        }
        self.assertEqual(cf.team_classifier_effective_mode(members), "plan")

    def test_mixed_leader_plan_member_auto_union_plan(self):
        """反向混合：leader plan + member auto → union=plan（leader 需 fallback）。"""
        members = {
            "lead": {"role": "leader", "agent": "claude", "work_mode": "plan"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"},
        }
        self.assertEqual(cf.team_classifier_effective_mode(members), "plan")

    def test_all_auto_union_empty(self):
        """全 auto 团队 → ""（不外溢，fallback 仅 plan 需要）。"""
        members = {
            "lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"},
        }
        self.assertEqual(cf.team_classifier_effective_mode(members), "")

    def test_all_manual_union_empty(self):
        """全 manual 团队 → ""（无 plan 成员，不外溢）。"""
        members = {
            "lead": {"role": "leader", "agent": "claude", "work_mode": "manual"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "manual"},
        }
        self.assertEqual(cf.team_classifier_effective_mode(members), "")

    def test_codex_only_ignored(self):
        """仅 codex 成员 → ""（权限分类器是 Claude 概念，codex 不参与 settings 判定）。"""
        members = {
            "lead": {"role": "leader", "agent": "codex", "work_mode": "plan"},
        }
        self.assertEqual(cf.team_classifier_effective_mode(members), "")

    def test_planning_readonly_aliases_union_plan(self):
        """plan/planning/readonly 别名都映射原生 plan → union=plan。"""
        for alias in ("plan", "planning", "readonly", "read_only"):
            members = {"alice": {"role": "coder", "agent": "claude", "work_mode": alias}}
            self.assertEqual(cf.team_classifier_effective_mode(members), "plan",
                             f"alias={alias!r} 应映射 plan")

    def test_empty_members_empty(self):
        self.assertEqual(cf.team_classifier_effective_mode({}), "")


class TestTeamUnionSettingsDeterminism(_IsolatedSpawnTestCase):
    """G2：共享 settings 写入确定性——混合团队 union 不随 spawn 顺序翻转
    （TUI 与 MCP 双路径同口径）。"""

    def _write_union(self, leader_mode="auto", alice_mode="plan"):
        self._save_team(leader_mode=leader_mode, alice_mode=alice_mode)
        team = mcp._load()["teams"]["team"]
        return mcp._write_claude_permissions(
            "team", mode=cf.team_classifier_effective_mode(team["members"]))

    def test_mixed_team_settings_contains_plan_fallback(self):
        """leader auto + member plan → 共享 settings 含 plan fallback（成员 plan
        的 settings 层被覆盖；旧实现按 leader 写会缺）。"""
        path = self._write_union(leader_mode="auto", alice_mode="plan")
        allow = json.load(open(path))["permissions"]["allow"]
        self.assertIn("Bash(pwd:*)", allow, "混合团队 union=plan 应含 fallback")

    def test_all_auto_team_settings_no_fallback(self):
        """全 auto 团队 → settings 不外溢（union=""）：安全 Bash 是基座（所有模式共享），
        全 auto 团队 settings 与手动/空团队一致，不因任何成员 plan 而额外追加。"""
        path = self._write_union(leader_mode="auto", alice_mode="auto")
        allow = json.load(open(path))["permissions"]["allow"]
        # F1：安全 Bash 在基座（全 auto 也含）；union 语义 = 全 auto 与全 manual 一致
        self.assertIn("Bash(pwd:*)", allow, "F1 后安全 Bash 在基座（全 auto 也含）")
        manual_path = self._write_union(leader_mode="manual", alice_mode="manual")
        self.assertEqual(allow, json.load(open(manual_path))["permissions"]["allow"],
                         "全 auto 与全 manual 的 settings 一致（union 不外溢）")

    def test_union_deterministic_across_spawn_order(self):
        """同一团队 union 模式重复写 → settings 逐字节一致（不随调用方身份翻转）。"""
        a = self._write_union(leader_mode="auto", alice_mode="plan")
        b = self._write_union(leader_mode="auto", alice_mode="plan")
        self.assertEqual(
            json.load(open(a)), json.load(open(b)),
            "同团队 union 重复写应一致（消除 last-writer-wins 翻转）")


if __name__ == "__main__":
    unittest.main()
