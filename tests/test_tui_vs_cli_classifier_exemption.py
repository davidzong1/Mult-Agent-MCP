"""
TUI 启动 vs CLI 启动：分类器豁免 / 注入参数差异的确定性回归测试
================================================================

背景（tester 独立根因分析，2026-08-12）
----------------------------------------
用户复现：从 TUI 团队成员管理界面（TeamDetailScreen → action_launch_terminals）
启动 tmux 中的 Claude leader/普通成员，auto/plan 模式下"分类器请求仍被拦截"。
共同边界要求：对比 TUI 与 CLI 启动链路，覆盖 leader/非 leader、auto/plan/manual。

真机 headless probe 决定性证据（真实 claude 2.1.228，本文件附录一）：
  - 成员模式 auto → --permission-mode acceptEdits；plan → plan；manual → 无(默认)。
    **三种映射均不调用分类器**（acceptEdits 下未放行工具走审批 prompt、plan 下
    Write 被只读禁止、Read 等只读工具默认放行）；**仅原生 --permission-mode auto
    调用分类器**（workspace 外 Write 被自动判定放行）。因此本代码库从不用原生
    auto → 成员 auto/plan 理论上不触发"temporarily unavailable"签名。

本文件固化 TUI vs CLI 的三处确定性差异（refactor-claude 共享分析交叉验证 + 本
tester 独立复验），供修复方（coder）对齐与回归：

  D1 【settings 层】CLI `_write_claude_permissions(mode=plan)` 在
     `<ws>/.claude/settings.json` 追加 classifier fallback 窄规则；TUI
     `common.mcp_config.write_claude_permissions(team_workspace)` 原签名无 mode，
     绝不追加 → plan 的 settings 层缺 fallback，且**覆写** MCP 已写内容。
     **【已修复 2026-08-12 coder】**：writer 增加 mode 参数并接线 fallback，
     TUI launch_terminals 传 leader 模式 → TUI plan settings 现含 fallback，
     不再覆写 MCP 已写 fallback（下文两处"缺陷行为断言"已翻转为修复后语义）。
  D2 【argv 层】两链路经 `_claude_agent_args`/`claude_agent_args`（tmux_utils 副本
     逐字一致）构造 --permission-mode / --allowedTools / --append-system-prompt-file，
     在 auto/plan/manual 下逐字一致 —— argv 层 fallback 无差异。
  D3 【monitor/检测链】`_start_team_monitor` 仅 MCP 侧调用（mult_agent_mcp.py
     :5009/:5129/:6514/:6702/:6756/:6826/:6922）；tui/*.py 零调用。TUI 启动的
     团队若后续无 MCP 工具触发，classifier_unavailable 检测/审计半环与 auto
     授权不运行 —— 分类器拦截（或审批卡住）时终端可能被误判 idle。
     **【已修复 2026-08-12 coder】**：MCP server main() 启动
     `_ensure_team_monitors_loop` 周期 sweep（15s，mult_agent_mcp.py:9997），为
     `terminals_active` 团队幂等启动 monitor —— 仅经 TUI 启动的团队也获得
     检测/审计/wakeup 半环（下文 test_sweep_starts_monitor_for_tui_active_team）。

隔离：temp teams_data + mock tmux（仿 test_classifier_fallback_mode_scoped /
test_leader_classifier_claude_tools.py），绝不触碰真实 ~/.mult_agent_mcp / 真实
tmux / 真实凭证。
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
from common import mcp_config as cfg

MEMBER_BASE = [
    "mcp__mult-agent-mcp__member_*",
    "mcp__mult_agent_mcp__member_*",
    "Bash",
    "Edit",
]
LEADER_BASE = [
    "mcp__mult-agent-mcp__leader_*",
    "mcp__mult_agent_mcp__leader_*",
    "Bash",
    "Edit",
]


class _Isolated(unittest.TestCase):
    """temp teams_data + data_layer 隔离（镜像既有 classifier 测试套件惯例）。"""

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

    def _workspace(self):
        ws = self.root / "workspace"
        ws.mkdir(exist_ok=True)
        return ws

    def _save_team(self, ws, leader_mode="plan", alice_mode="plan", terminals_active=True):
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(ws),
                    "context_dir": str(self.root / "contexts"),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "monitor_enabled": False,
                    "terminals_active": terminals_active,
                    "members": {
                        "lead": {"role": "leader", "agent": "claude", "work_mode": leader_mode},
                        "alice": {"role": "coder", "agent": "claude", "work_mode": alice_mode},
                    },
                }
            }
        })

    def _settings_allow(self, ws):
        p = ws / ".claude" / "settings.json"
        if not p.exists():
            return None
        return json.loads(p.read_text(encoding="utf-8"))["permissions"]["allow"]

    # ---------------- CLI 启动链路：捕获 tmux 命令 + settings.json ----------------

    def _cli_launch(self, leader_mode="plan", alice_mode="plan"):
        ws = self._workspace()
        self._save_team(ws, leader_mode, alice_mode)
        cmds = []

        def fake_tmux(cmd, timeout=10):
            cmds.append(list(cmd))
            if cmd[0] == "list-windows":
                # 非空但无 alice 窗口 → alice 判定 absent → 触发 spawn（而非 unknown fail-closed）
                return 0, "$1\t1000\t@1\totherwin", ""
            if cmd[0] in ("-V", "new-session", "new-window", "has-session"):
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(mcp, "_write_claude_mcp", return_value=str(self.root / "mcp.json")), \
             mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(mcp, "claude_agent_user_launch", return_value=(["A=1"], str(self.root / "s.json"))), \
             mock.patch.object(mcp, "_send_keys", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(mcp.time, "sleep", return_value=None):
            mcp.launch_team_terminals("team", task="t")
        return cmds, self._settings_allow(ws)

    # ---------------- TUI 启动链路 ----------------

    def _tui_launch(self, leader_mode="plan", alice_mode="plan"):
        import tui.tui_screens as tui
        ws = self._workspace()
        store = {
            "teams": {"team": {
                "workspace_dir": str(ws),
                "context_dir": str(self.root / "contexts"),
                "leader": "lead",
                "leader_type": "tmux",
                "members": {
                    "lead": {"role": "leader", "agent": "claude", "work_mode": leader_mode},
                    "alice": {"role": "coder", "agent": "claude", "work_mode": alice_mode},
                },
            }}
        }
        cmds = []

        def fake_load():
            return copy.deepcopy(store)

        def fake_save(d):
            store.clear()
            store.update(copy.deepcopy(d))

        def fake_tmux_run(cmd, timeout=10):
            cmds.append(list(cmd))
            if cmd[0] in ("-V", "new-session", "new-window", "list-windows", "has-session"):
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(tui, "load_data", side_effect=fake_load), \
             mock.patch.object(tui, "save_data", side_effect=fake_save), \
             mock.patch.object(tui, "configure_claude_mcp", return_value=(True, "ok")), \
             mock.patch.object(tui, "configure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(tui, "claude_agent_user_launch", return_value=(["A=1"], str(self.root / "s.json"))), \
             mock.patch.object(tui, "_member_window_state", return_value=("missing", "")), \
             mock.patch.object(tui, "_member_spawn_lock"), \
             mock.patch.object(tui, "_send_keys", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(tui, "_inject_claude_leader_prompt", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(tui, "_remember_member_window_id", return_value=None), \
             mock.patch.object(tui, "_leader_terminal_restart_blocked", return_value=False), \
             mock.patch.object(tui, "shutil") as fake_shutil, \
             mock.patch.object(tui, "_tmux_run", side_effect=fake_tmux_run):
            fake_shutil.which.side_effect = lambda name: name
            ok, msg = tui.launch_terminals("team")
        self.assertTrue(ok, msg)
        return cmds, self._settings_allow(ws)

    @staticmethod
    def _allowed_tools(cmd):
        if "--allowedTools" not in cmd:
            return ""
        return cmd[cmd.index("--allowedTools") + 1]


# ---------------------------------------------------------------------------
# D1 settings 层：CLI 有 plan fallback，TUI 缺且覆写
# ---------------------------------------------------------------------------

class TestSettingsLayerParity(_Isolated):

    def test_cli_plan_settings_includes_fallback(self):
        _cmds, allow = self._cli_launch("plan", "plan")
        self.assertIn("Bash(pwd:*)", allow, "CLI plan settings 应含 fallback")
        self.assertIn("Bash(git:*)", allow)

    def test_tui_plan_settings_includes_fallback(self):
        """【修复后语义】TUI plan settings 现含 fallback（coder 2026-08-12 修复：
        common.mcp_config.write_claude_permissions 增加 mode 参数，TUI launch_terminals
        传 leader 模式，与 CLI 同口径）。修复前 TUI 缺 fallback（签名无 mode）。"""
        _cmds, allow = self._tui_launch("plan", "plan")
        self.assertIn("Bash(pwd:*)", allow,
                      "TUI plan settings 应含 fallback（与 CLI 同口径）")
        self.assertIn("Bash(git:*)", allow)

    def test_cli_auto_and_manual_settings_no_fallback(self):
        # F1 后：安全 Bash 是基座（所有模式共享），auto/manual settings 与 plan 一致
        # （含安全 Bash），不外溢裸工具/危险命令。
        for mode in ("auto", "manual"):
            _cmds, allow = self._cli_launch(mode, mode)
            self.assertIn("Bash(pwd:*)", allow, f"CLI {mode} settings 含安全基座")
            self.assertNotIn("Bash", allow, f"CLI {mode} settings 不得含裸 Bash")
            self.assertNotIn("Edit", allow, f"CLI {mode} settings 不得含裸 Edit")

    def test_tui_auto_and_manual_settings_no_fallback(self):
        """F1 后：TUI auto/manual leader settings 与 plan 一致（安全 Bash 基座共享），
        不外溢裸工具/危险命令。"""
        for mode in ("auto", "manual"):
            _cmds, allow = self._tui_launch(mode, mode)
            self.assertIn("Bash(pwd:*)", allow, f"TUI {mode} settings 含安全基座")
            self.assertIn("Bash(git:*)", allow, f"TUI {mode} settings 含 git 安全基座")
            self.assertNotIn("Bash", allow, f"TUI {mode} settings 不得含裸 Bash")
            self.assertNotIn("Edit", allow, f"TUI {mode} settings 不得含裸 Edit")

    def test_tui_overwrites_mcp_settings_fallback(self):
        """【修复后语义】TUI 覆写（带团队 union mode=plan）不再抹掉 MCP fallback；
        F1 后安全 Bash 是基座，覆写后仍含。"""
        ws = self._workspace()
        self._save_team(ws, "plan", "plan")
        # 1) MCP 写（含安全基座）
        mcp._write_claude_permissions("team", mode="plan")
        self.assertIn("Bash(pwd:*)", self._settings_allow(ws))
        # 2) TUI 再写（现传团队 union mode=plan）→ 安全基座保留
        cfg.write_claude_permissions(str(ws), mode="plan")
        allow = self._settings_allow(ws)
        self.assertIn("Bash(pwd:*)", allow, "TUI 覆写后安全基座应保留（修复后语义）")
        self.assertNotIn("Bash", allow, "F1 后不得含裸 Bash")

    def test_tui_overwrites_mcp_settings_with_auto_leader_no_fallback(self):
        """F1 后：TUI 覆写（leader auto）→ 与 plan 一致（安全 Bash 基座共享），
        union 语义下不外溢额外 plan fallback。"""
        ws = self._workspace()
        self._save_team(ws, "plan", "plan")
        mcp._write_claude_permissions("team", mode="plan")
        self.assertIn("Bash(pwd:*)", self._settings_allow(ws))
        cfg.write_claude_permissions(str(ws), mode=mcp._member_mode({"work_mode": "auto"}))
        allow = self._settings_allow(ws)
        self.assertIn("Bash(pwd:*)", allow, "F1 后安全 Bash 基座共享（leader auto 也含）")
        self.assertNotIn("Bash", allow, "F1 后不得含裸 Bash")
        self.assertNotIn("Edit", allow, "F1 后不得含裸 Edit")


# ---------------------------------------------------------------------------
# D2 argv 层：两链路 --allowedTools / --permission-mode 逐字一致
# ---------------------------------------------------------------------------

class TestArgvLayerParity(_Isolated):

    def test_allowedtools_parity_auto_plan_manual(self):
        for mode in ("auto", "plan", "manual"):
            cli_cmds, _a = self._cli_launch(mode, mode)
            tui_cmds, _b = self._tui_launch(mode, mode)
            cli_tools = sorted(self._allowed_tools(c) for c in cli_cmds if "--allowedTools" in c)
            tui_tools = sorted(self._allowed_tools(c) for c in tui_cmds if "--allowedTools" in c)
            self.assertEqual(cli_tools, tui_tools, f"mode={mode} argv --allowedTools 应一致")
            # F1 后：安全 Bash 是基座（所有模式含 pwd/git），plan 额外追加 wc/head 等
            # （两链路一致）；裸 Bash/Edit 绝无。
            joined = ",".join(cli_tools)
            self.assertIn("Bash(pwd:*)", joined, f"mode={mode} 安全基座含 pwd")
            self.assertNotIn("Bash,", joined + ",", f"mode={mode} 不得含裸 Bash")
            self.assertNotIn("Edit", joined.split(","), f"mode={mode} 不得含裸 Edit")

    def test_append_system_prompt_parity(self):
        """两链路 claude spawn 均携带 --append-system-prompt-file（身份进 system 层）。"""
        cli_cmds, _ = self._cli_launch("auto", "auto")
        tui_cmds, _ = self._tui_launch("auto", "auto")
        for label, cmds in (("CLI", cli_cmds), ("TUI", tui_cmds)):
            spawns = [c for c in cmds if c and c[0] in ("new-session", "new-window")]
            self.assertTrue(spawns, f"{label} 应有 spawn 命令")
            for sp in spawns:
                self.assertIn("--append-system-prompt-file", sp, f"{label} spawn 缺身份 flag")

    def test_mode_never_native_auto(self):
        """成员 auto/plan/manual/空 → --permission-mode 永不为原生 auto（分类器模式）。

        真机 probe 实证：仅原生 auto 调用分类器；acceptEdits/plan/manual 不调用。
        因此只要映射不为 auto，成员 auto/plan 不会产生"temporarily unavailable"
        分类器流量。
        """
        for mode in ("auto", "plan", "manual", "", "planning", "readonly"):
            args = mcp._claude_agent_args("claude", mode)
            if "--permission-mode" in args:
                pm = args[args.index("--permission-mode") + 1]
                self.assertNotEqual(pm, "auto", f"mode={mode!r} 映射为原生 auto（不应发生）")
                if mode in ("auto", "accept", "accept_edits", "never"):
                    self.assertEqual(pm, "acceptEdits", f"mode={mode!r}")
                elif mode in ("plan", "planning", "readonly", "read_only"):
                    self.assertEqual(pm, "plan", f"mode={mode!r}")
            else:
                # manual / default / "" → 无 --permission-mode（用户默认 default）
                self.assertIn(mode, ("manual", "", "default"), f"mode={mode!r} 应有 permission-mode")


# ---------------------------------------------------------------------------
# D3 monitor/检测链：CLI 启动 monitor，TUI 不启动
# ---------------------------------------------------------------------------

class TestMonitorParity(_Isolated):

    def test_cli_launch_starts_monitor(self):
        ws = self._workspace()
        self._save_team(ws, "plan", "plan")
        calls = []

        def fake_tmux(cmd, timeout=10):
            if cmd[0] in ("-V", "new-session", "new-window", "list-windows", "has-session"):
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(mcp, "_write_claude_mcp", return_value=str(self.root / "mcp.json")), \
             mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(mcp, "claude_agent_user_launch", return_value=(["A=1"], str(self.root / "s.json"))), \
             mock.patch.object(mcp, "_send_keys", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(mcp, "_inject_claude_leader_prompt", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(mcp.time, "sleep", return_value=None), \
             mock.patch.object(mcp, "_start_team_monitor", side_effect=lambda tn: calls.append(tn)) as sm:
            mcp.launch_team_terminals("team", task="t")
        self.assertEqual(calls, ["team"], "CLI launch 应启动 monitor")
        sm.assert_called()

    def test_tui_launch_does_not_start_monitor(self):
        """TUI launch_terminals 不直接调用 _start_team_monitor（事实）。

        【D3 修复后】检测链兜底已由 MCP 侧 _ensure_team_monitors_once 周期 sweep
        补上：TUI 启动团队（terminals_active=True）会被 sweep 启动 monitor，
        classifier_unavailable 检测/审计半环与 auto 授权不再缺口。
        """
        import tui.tui_screens as tui
        ws = self._workspace()
        store = {
            "teams": {"team": {
                "workspace_dir": str(ws),
                "context_dir": str(self.root / "contexts"),
                "leader": "lead",
                "leader_type": "tmux",
                "members": {
                    "lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
                    "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"},
                },
            }}
        }

        def fake_load():
            return copy.deepcopy(store)

        def fake_save(d):
            store.clear()
            store.update(copy.deepcopy(d))

        def fake_tmux_run(cmd, timeout=10):
            if cmd[0] in ("-V", "new-session", "new-window", "list-windows", "has-session"):
                return 0, "", ""
            return 0, "", ""

        with mock.patch.object(tui, "load_data", side_effect=fake_load), \
             mock.patch.object(tui, "save_data", side_effect=fake_save), \
             mock.patch.object(tui, "configure_claude_mcp", return_value=(True, "ok")), \
             mock.patch.object(tui, "configure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(tui, "claude_agent_user_launch", return_value=(["A=1"], str(self.root / "s.json"))), \
             mock.patch.object(tui, "_member_window_state", return_value=("missing", "")), \
             mock.patch.object(tui, "_member_spawn_lock"), \
             mock.patch.object(tui, "_send_keys", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(tui, "_inject_claude_leader_prompt", side_effect=lambda *a, **k: (0, "")), \
             mock.patch.object(tui, "_remember_member_window_id", return_value=None), \
             mock.patch.object(tui, "_leader_terminal_restart_blocked", return_value=False), \
             mock.patch.object(tui, "shutil") as fake_shutil, \
             mock.patch.object(tui, "_tmux_run", side_effect=fake_tmux_run):
            fake_shutil.which.side_effect = lambda name: name
            ok, msg = tui.launch_terminals("team")
        self.assertTrue(ok, msg)
        # TUI 模块不存在 _start_team_monitor 绑定（全仓仅 mult_agent_mcp 启动 monitor）
        self.assertFalse(hasattr(tui, "_start_team_monitor"),
                         "TUI 启动路径不应直接启动 team monitor")

    def test_sweep_starts_monitor_for_tui_active_team(self):
        """【D3 修复后】_ensure_team_monitors_once 为 terminals_active 团队启动
        monitor —— TUI 启动的团队由 MCP 侧周期 sweep 兜底（检测链不再缺口）。"""
        ws = self._workspace()
        self._save_team(ws, "plan", "plan")
        started = []

        def fake_start(tn):
            started.append(tn)

        with mock.patch.object(mcp, "_start_team_monitor", side_effect=fake_start):
            n = mcp._ensure_team_monitors_once()
        self.assertEqual(n, 1, "sweep 应为 terminals_active 团队启动 monitor")
        self.assertEqual(started, ["team"])

    def test_sweep_skips_inactive_team(self):
        """非活跃团队（terminals_active=False）不被 sweep 启动 monitor。"""
        ws = self._workspace()
        data = {
            "teams": {"team": {
                "workspace_dir": str(ws),
                "context_dir": str(self.root / "contexts"),
                "leader": "lead",
                "leader_type": "tmux",
                "terminals_active": False,
                "members": {
                    "lead": {"role": "leader", "agent": "claude", "work_mode": "plan"},
                },
            }}
        }
        mcp._save(data)
        started = []
        with mock.patch.object(mcp, "_start_team_monitor", side_effect=lambda tn: started.append(tn)):
            n = mcp._ensure_team_monitors_once()
        self.assertEqual(n, 0, "非活跃团队不应启动 monitor")
        self.assertEqual(started, [])


# ---------------------------------------------------------------------------
# 附录一：真机 headless probe 文档证据（2026-08-12，真实 claude 2.1.228）
# ---------------------------------------------------------------------------
# 1) --permission-mode acceptEdits + 未放行 workspace 外 Write → 走审批 prompt
#    （headless 无批准即拒），stderr 无 classifier/permission debug 行。
# 2) --permission-mode plan + 同操作 → plan 只读禁止，无分类器流量。
# 3) --permission-mode auto + 同操作 → 自动判定放行（分类器可用），RESULT:done。
# 4) --permission-mode acceptEdits + 未放行 Read（只读工具）→ 默认放行，无分类器。
# 5) settings.json 写 Edit(/tmp/*) 或 argv --allowedTools 写 Edit(/tmp/*) 均不
#    豁免 workspace 外 Write 的审批（headless 审批不可交互）——settings/argv 的
#    allow 规则对 workspace 内 Edit/Bash 生效，对越界 Write 不豁免。
# 6) settings 层 vs argv 层豁免权重：两者都是 Claude Code 权限系统的 allow 规则、
#    合并生效，**对成员 auto/plan 无区分意义** —— acceptEdits/plan 不调分类器，
#    fallback 是否存在都不影响分类器豁免（不调分类器即无需豁免）；仅原生 auto
#    依赖 allow 豁免分类器（auto + allow(Bash,Edit) 时 workspace 外 Write 被自动
#    判定放行）。D1 修复保证 TUI settings 层与 CLI 一致（plan 含 fallback），
#    消除"settings 层缺规则"这一可复现差异；argv 层两链路本就一致（D2）。
# 结论：成员映射（acceptEdits/plan/default）不调用分类器；原生 auto 才调用。
# 本代码库从不传 --permission-mode auto（test_mode_never_native_auto），因此
# "temporarily unavailable" 分类器签名只在用户侧显式启用原生 auto 或 Claude
# Code 新版本行为变化时出现。TUI 启动链路经 D1（settings fallback 对齐）+
# D3（monitor sweep 兜底）修复后，与 CLI 启动在分类器豁免/检测链上不再有缺口。

if __name__ == "__main__":
    unittest.main()
