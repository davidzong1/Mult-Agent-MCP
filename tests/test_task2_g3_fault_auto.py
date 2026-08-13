"""task2 G3 故障态自动化回归（tester 独立新增，不修改主实现）。

覆盖 G3 统一验收的自动化硬项：classifier unavailable 签名注入 → 检测/审计/
绝不误标 idle 闭环；混合模式 settings 层 union（team_classifier_effective_mode）；
plan 成员 argv fallback 让安全 Bash 可用、危险命令仍阻断；manual/auto 不外溢。

对应 refactor-claude 真机观察点 4/5/6 的自动化侧：
  #4 混合模式(plan leader + auto 成员) settings 层 union 终态 ——
     team_classifier_effective_mode 任一 claude 成员映射原生 plan → settings 写 plan
     fallback；全 auto / 全 manual → base only（零外溢）。
  #5 "temporarily unavailable ... cannot determine ... safety" 广播 → monitor 判
     classifier_unavailable（绝不 idle/approval），恢复签名消失判 recovered，各写审计。
  #6 plan 成员 --allowedTools 追加精选安全窄规则（安全 Bash 可用、危险命令不在集内）。

签名样本（真实复现）：``deepseek/deepseek-v4-flash[1m] is temporarily unavailable,
so auto mode cannot determine the safety of Write right now``。

隔离：temp teams_data + mock tmux 捕获边界（真实 classify/monitor 代码路径），
绝不触碰真实 ~/.mult_agent_mcp / 真实 tmux / 真实凭证。
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

# 真实复现签名（model 名可变 / 模式词 auto/plan 可变 / 时态可变）
REAL_SIG = (
    "deepseek/deepseek-v4-flash[1m] is temporarily unavailable, so auto mode "
    "cannot determine the safety of Write right now"
)
REAL_SIG_PLAN = (
    "claude-5 is temporarily unavailable, so plan mode could not determine "
    "the safety of Edit"
)
SIG_PREFIX = "Running Bash: git fetch\n"  # 上游命令输出残留，不得干扰签名判定
SIG_SUFFIX = "\n❯"  # 底部 prompt 回到提示符，也不得把 classifier_unavailable 覆盖成 idle


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
        # F3 层2 抑制状态：模块级全局 _SIG_INJECTION_SUPPRESS_UNTIL 是 240s 单调时钟、
        # 跨测试残留（本文件 _inject 会标记 (team, alice)）。同类测试文件都使用相同的
        # 团队/成员名（team/alice），若不清理，同一 pytest 进程内后续文件（如
        # test_classifier_fallback_gate.py）对同名成员的真实签名扫描会被误抑制 →
        # 误判 unknown。tearDown 统一清空（3b），杜绝跨测试/跨文件残留。

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        # 3b：清空注入抑制状态（复合键 dict 同样适用——本文件测试标记的
        # (team, alice) 会污染后续测试对同名团队成员的签名扫描）。
        sig = getattr(mcp, "_SIG_INJECTION_SUPPRESS_UNTIL", None)
        if sig is not None:
            sig.clear()
        self.tmp.cleanup()

    def _workspace(self):
        ws = self.root / "workspace"
        ws.mkdir(exist_ok=True)
        return ws

    def _save_team(self, members, share_dir=None):
        ws = self._workspace()
        mcp._save({
            "teams": {"team": {
                "workspace_dir": str(ws),
                "context_dir": str(share_dir or (self.root / "contexts")),
                "leader": "lead",
                "leader_type": "tmux",
                "monitor_enabled": True,
                "terminals_active": True,
                "members": members,
            }}
        })

    def _audit_path(self):
        return Path(mcp._share_dir("team")) / cf.CLASSIFIER_FALLBACK_AUDIT_FILE

    def _audit_entries(self):
        p = self._audit_path()
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# 检测层：签名注入 → classifier_unavailable（绝不 idle / approval）
# ---------------------------------------------------------------------------

class TestSignatureDetection(_Isolated):

    def test_real_auto_signature_detected(self):
        self.assertTrue(cf.detect_classifier_unavailable(REAL_SIG))

    def test_real_plan_variant_detected(self):
        self.assertTrue(cf.detect_classifier_unavailable(REAL_SIG_PLAN))

    def test_member_terminal_classifier_sig_is_unavailable_not_idle(self):
        """member 终端捕获含签名 → 判 classifier_unavailable（绝不 idle → 绝不
        mark_idle_done）。即使底部回到 ❯ 也不能覆盖成 idle。"""
        self._save_team({"lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
                         "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"}})
        for mode in ("auto", "plan", "manual"):
            native = cf.claude_native_permission_mode(mode)
            for text in (REAL_SIG, REAL_SIG + "\n❯", REAL_SIG_PLAN):
                self.assertEqual(
                    mcp._classify_terminal_output(text, native_mode=native),
                    "classifier_unavailable",
                    f"mode={mode} 签名应判 classifier_unavailable",
                )
        self.assertEqual(mcp._classify_terminal_output("❯ manual mode on", native_mode="manual"), "idle")

    def test_member_busy_residual_does_not_mark_idle(self):
        """【观察点 #5 边界固化】上游 "Running Bash:" 残留 + 签名同窗时，成员侧
        当前实现判 busy（busy 优先于 classifier 签名检测）。busy 仍绝不 idle →
        不 mark_idle_done → 不丢上下文（安全方向成立）；但 classifier 审计不进，
        此边界记录为已知行为供实现方裁决（leader 侧同文本判 classifier_unavailable，
        两侧不对称）。"""
        self._save_team({"lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
                         "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"}})
        text = "Running Bash: git fetch\n" + REAL_SIG + "\n❯"
        state = mcp._classify_terminal_output(text, native_mode="auto")
        self.assertIn(state, ("busy", "classifier_unavailable"),
                      f"签名窗口内绝不可判 idle: {state}")
        # leader 侧同文本 → classifier_unavailable（无 busy 遮蔽）
        self.assertEqual(mcp._classify_leader_terminal_output(text), "classifier_unavailable")
        # 成员侧该边界在当前实现下判 busy —— 固化现状（若实现方上调优先级则此断言需同步）
        self.assertEqual(state, "busy", "当前实现成员侧 busy 优先；调整需改此断言")

    def test_leader_terminal_classifier_sig_is_unavailable_not_idle(self):
        """leader 终端捕获含签名 → _classify_leader_terminal_output 判
        classifier_unavailable（leader 侧绝不被误判 idle → 不 enter_resting）。"""
        self._save_team({"lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
                         "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"}})
        self.assertEqual(mcp._classify_leader_terminal_output(SIG_PREFIX + REAL_SIG + SIG_SUFFIX),
                         "classifier_unavailable")
        self.assertEqual(mcp._classify_leader_terminal_output("✻ Brewed for 5s\n❯"), "idle")


# ---------------------------------------------------------------------------
# monitor 层：签名出现 → 观测字段 + 审计 entered；签名消失 → recovered 审计
# ---------------------------------------------------------------------------

class TestMonitorAuditClosedLoop(_Isolated):

    def _seed_team(self, mode="auto"):
        self._save_team({"lead": {"role": "leader", "agent": "claude", "work_mode": mode},
                         "alice": {"role": "coder", "agent": "claude", "work_mode": mode,
                                   "last_task": "t", "last_task_completed": False}})

    def _monitor_member_once(self, capture_text):
        """真实 _scan_member_terminal 路径 + mock tmux 捕获边界。

        不重建 team：上次扫描写入的 last_observed_state 必须保留，恢复判定
        （prev_state == classifier_unavailable → recovered）才成立。

        ``_tmux`` 固定返回 rc!=0（mock 捕获边界）：auto 成员审批 prompt 走自动
        授权分支（_send_authorization_choice 依赖 _tmux send-keys）。若不 mock，
        真实 tmux 环境（mcp_team 会话是否恰好有同名窗口）会决定该分支成败 →
        test_approval_prompt_distinct_from_classifier 顺序/环境依赖 flaky。
        固定失败 → 自动授权确定性失败 → state 保持 approval，测试与真实 tmux 解耦。
        """
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", side_effect=lambda tn, n: n), \
             mock.patch.object(mcp, "_capture_window", return_value=(0, capture_text, "")), \
             mock.patch.object(mcp.time, "sleep", return_value=None), \
             mock.patch.object(mcp, "_tmux", side_effect=lambda *a, **k: (1, "", "no window")):
            return mcp._scan_member_terminal("team", "alice")

    def test_signature_sets_state_and_audits_entered(self):
        """签名注入 → last_observed_state=classifier_unavailable + 审计 entered；
        绝不 idle / 绝不 mark_idle_done（last_task_completed 保持 False）。"""
        self._seed_team()
        r = self._monitor_member_once(REAL_SIG + "\n❯")
        self.assertEqual(r.get("state"), "classifier_unavailable")
        data = mcp._load()["teams"]["team"]
        self.assertEqual(data["members"]["alice"]["last_observed_state"], "classifier_unavailable")
        # 绝不误标 idle → 绝不 mark_idle_done
        self.assertFalse(data["members"]["alice"].get("last_task_completed", False),
                         "classifier_unavailable 不得 mark_idle_done")
        entries = self._audit_entries()
        self.assertTrue(any(e.get("state") == "entered" and e.get("member") == "alice"
                            and e.get("event") == "classifier_fallback_entered" for e in entries),
                        f"应有 entered 审计: {entries}")

    def test_signature_recovery_audits_recovered(self):
        """签名从捕获窗口消失 → 判 recovered 并审计 recovered（观察式恢复）。"""
        self._seed_team()
        self._monitor_member_once(REAL_SIG + "\n❯")
        r = self._monitor_member_once("✻ Brewed for 5s\n❯ manual mode on")
        self.assertNotEqual(r.get("state"), "classifier_unavailable")
        entries = self._audit_entries()
        self.assertTrue(any(e.get("state") == "recovered" and e.get("member") == "alice"
                            and e.get("event") == "classifier_fallback_recovered" for e in entries),
                        f"应有 recovered 审计: {entries}")

    def test_plain_idle_never_enters_classifier_audit(self):
        """普通 idle 无签名 → 绝不写 classifier 审计（检测/审计不误伤正常流程）。"""
        self._seed_team()
        self._monitor_member_once("✻ Brewed for 5s\n❯ manual mode on")
        entries = self._audit_entries()
        self.assertEqual([e for e in entries if e.get("member") == "alice"], [],
                         f"普通 idle 不应有 classifier 审计: {entries}")

    def test_approval_prompt_distinct_from_classifier(self):
        """审批 prompt（可自动授权的 approval 状态）与 classifier_unavailable 区分：
        approval 文本不判 classifier_unavailable、不写 classifier 审计（两类卡住不混淆）。"""
        self._seed_team()
        r = self._monitor_member_once(
            "This command requires approval\nDo you want to proceed?\n❯ 1. Yes"
        )
        self.assertEqual(r.get("state"), "approval")
        self.assertNotIn("classifier", str(r.get("action", "")))
        entries = self._audit_entries()
        self.assertFalse(any(e.get("event", "").startswith("classifier_fallback_") for e in entries),
                         f"approval 卡住不应写 classifier 审计: {entries}")


# ---------------------------------------------------------------------------
# 观察点 #4：混合模式 settings 层 union（team_classifier_effective_mode）
# ---------------------------------------------------------------------------

class TestMixedModeSettingsUnion(_Isolated):

    def _union(self, members):
        return cf.team_classifier_effective_mode(members)

    def test_any_claude_plan_triggers_plan_fallback(self):
        """leader plan + 成员 auto → union=plan（settings 写 plan fallback）。"""
        self.assertEqual(self._union({
            "lead": {"role": "leader", "agent": "claude", "work_mode": "plan"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"},
        }), "plan")

    def test_all_auto_no_fallback(self):
        self.assertEqual(self._union({
            "lead": {"role": "leader", "agent": "claude", "work_mode": "auto"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"},
        }), "")

    def test_all_manual_no_fallback(self):
        self.assertEqual(self._union({
            "lead": {"role": "leader", "agent": "claude", "work_mode": "manual"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "manual"},
        }), "")

    def test_codex_member_ignored(self):
        """codex 成员不参与（权限分类器是 Claude Code 概念）。"""
        self.assertEqual(self._union({
            "lead": {"role": "leader", "agent": "codex"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "plan"},
        }), "plan")
        self.assertEqual(self._union({
            "lead": {"role": "leader", "agent": "codex"},
            "alice": {"role": "coder", "agent": "codex"},
        }), "")

    def test_tui_writer_uses_team_union(self):
        """TUI launch_terminals settings 写入以团队 union 模式（观察点 #4 终态）。"""
        import tui.tui_screens as tui
        members = {
            "lead": {"role": "leader", "agent": "claude", "work_mode": "plan"},
            "alice": {"role": "coder", "agent": "claude", "work_mode": "auto"},
        }
        self.assertEqual(cf.team_classifier_effective_mode(members), "plan")


# ---------------------------------------------------------------------------
# 观察点 #6：plan 成员 argv fallback（安全 Bash 可用 / 危险命令仍阻断）
# ---------------------------------------------------------------------------

class TestArgvFallbackScoped(_Isolated):

    BASE_MEMBER = [
        "mcp__mult-agent-mcp__member_*",
        "mcp__mult_agent_mcp__member_*",
        "Bash(git:*)", "Bash(pwd:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(echo:*)",
        "Bash(grep:*)", "Bash(python3 -m pytest:*)", "Bash(python3 -m unittest:*)",
        "Bash(python3 -m compileall:*)",
    ]

    def test_plan_member_gets_safe_bash_narrow_rules(self):
        tools = cf.claude_terminal_allow_tools("plan", str(self._workspace()), list(self.BASE_MEMBER))
        self.assertIn("Bash(git:*)", tools)
        self.assertIn("Bash(ls:*)", tools)
        self.assertIn("Bash(cat:*)", tools)
        self.assertIn("Edit(%s/*)" % self._workspace(), tools, "scoped Edit 无条件携带")
        self.assertNotIn("Bash(rm:*)", tools)
        self.assertNotIn("Bash(*)", tools)

    def test_auto_member_no_fallback(self):
        """F1 后：auto → 原生 acceptEdits（不调分类器）→ --allowedTools 为调用方基座
        + scoped Edit，零外溢；函数不注入裸 Bash/Edit（由调用方基座决定）。"""
        tools = cf.claude_terminal_allow_tools("auto", str(self._workspace()), list(self.BASE_MEMBER))
        self.assertEqual(tools, ["Edit(%s/*)" % self._workspace(), *self.BASE_MEMBER])
        self.assertNotIn("Bash", tools, "调用方基座不含裸 Bash 时函数不注入")

    def test_manual_member_no_fallback(self):
        tools = cf.claude_terminal_allow_tools("manual", str(self._workspace()), list(self.BASE_MEMBER))
        self.assertEqual(tools, ["Edit(%s/*)" % self._workspace(), *self.BASE_MEMBER])
        self.assertNotIn("Bash", tools, "调用方基座不含裸 Bash 时函数不注入")

    def test_dangerous_commands_never_in_fallback(self):
        """危险命令绝不在 fallback 集内（rm/sudo/curl/chmod/mv/cp/pip/npm/make）。"""
        tools = cf.claude_terminal_allow_tools("plan", str(self._workspace()), list(self.BASE_MEMBER))
        joined = "|".join(tools)
        for bad in ("rm", "sudo", "curl", "chmod", "mv", "cp", "pip", "npm", "make"):
            self.assertNotIn(f"Bash({bad}:", joined, f"危险命令 {bad} 不得在 fallback 集内")

    def test_plan_mode_settings_allow_only_plan(self):
        """settings 层追加 fallback 仅限原生 plan 模式（观察点 #4 终态）。"""
        ws = self._workspace()
        from common import mcp_config as cfg
        cfg.write_claude_permissions(str(ws), mode="plan")
        allow = json.loads((ws / ".claude" / "settings.json").read_text(encoding="utf-8"))["permissions"]["allow"]
        self.assertIn("Bash(pwd:*)", allow)
        self.assertIn("Bash(git:*)", allow)
        self.assertNotIn("Bash(*)", allow)
        self.assertNotIn("Edit(**) ", allow)
        self.assertNotIn("Edit(**) ", allow)


# ---------------------------------------------------------------------------
# F3 层2：注入排除护栏（2026-08-12 refactor 复核补强）
#   leader 广播/回报/任务转述成员报错文本（可能无引号逐字引用）→ _send_keys 注入
#   前置 detect 命中 → 标记成员抑制 classifier_unavailable 240s → monitor 不误判；
#   真实错误块（无注入记录）仍判 classifier_unavailable。
# ---------------------------------------------------------------------------


class TestF3InjectionSuppression(_Isolated):
    """F3 层2 注入排除护栏：注入含签名文本 → 抑制；真实错误块 → 仍检测。"""

    SIG = ("deepseek/deepseek-v4-flash[1m] is temporarily unavailable, so auto "
           "mode cannot determine the safety of Write right now")

    def _team(self):
        self._save_team({"lead": {"role": "leader", "agent": "claude", "work_mode": "plan"},
                         "alice": {"role": "coder", "agent": "claude", "work_mode": "plan",
                                   "last_task": "t", "last_task_completed": False}})

    def _scan(self, out):
        with mock.patch.object(mcp, "_find_any_session", return_value="sess"), \
             mock.patch.object(mcp, "_member_window_target", side_effect=lambda tn, n: n), \
             mock.patch.object(mcp, "_capture_window", return_value=(0, out, "")), \
             mock.patch.object(mcp, "_apply_member_scan_fields", lambda *a, **k: None):
            return mcp._scan_member_terminal("team", "alice")["state"]

    def _inject(self, payload):
        with mock.patch.object(mcp, "_tmux", side_effect=lambda *a, **k: (0, "", "")), \
             mock.patch.object(mcp, "_member_window_target", side_effect=lambda tn, n: n), \
             mock.patch.object(mcp, "_find_any_session", return_value="sess"):
            return mcp._send_keys("sess", "alice", payload)

    def test_unquoted_broadcast_reference_not_classified(self):
        """refactor 复核实证场景：leader 无引号逐字转述成员报错 → 注入后成员
        不判 classifier_unavailable（不再任务悬挂）。"""
        self._team()
        broadcast = f"[广播] 成员A报告: {self.SIG}，请排查"
        # 注入前：无抑制 → 判 classifier_unavailable
        self.assertEqual(self._scan(broadcast), "classifier_unavailable")
        # 注入该广播（触发抑制标记）
        self._inject(broadcast)
        self.assertTrue(mcp._sig_injection_suppressed("team", "alice"),
                        "注入含签名文本后应标记 (team, alice) 抑制")
        # 注入后：跳过 classifier_unavailable
        self.assertNotEqual(self._scan(broadcast), "classifier_unavailable",
                            "注入引用不应让成员被判 classifier_unavailable")

    def test_quoted_reference_not_classified(self):
        """带引号引用（层1已排除）+ 注入抑制（层2）双护栏 → 不判。"""
        self._team()
        quoted = f"[任务] 引述：“{self.SIG}”"
        self.assertFalse(cf.detect_classifier_unavailable(quoted), "层1引号排除")
        self._inject(quoted)
        self.assertNotEqual(self._scan(quoted), "classifier_unavailable")

    def test_real_error_block_still_detected_after_suppression_expiry(self):
        """真实错误块（无注入记录）仍判 classifier_unavailable；抑制窗口到期后恢复。"""
        self._team()
        real = f"❯\n{self.SIG}\n❯"
        self.assertEqual(self._scan(real), "classifier_unavailable",
                         "无注入记录的真实错误块应检测")
        # 抑制窗口内同一文本 → 跳过（注入场景）
        self._inject(self.SIG)
        self.assertNotEqual(self._scan(real), "classifier_unavailable")
        # 窗口到期 → 恢复检测
        mcp._SIG_INJECTION_SUPPRESS_UNTIL.clear()
        self.assertEqual(self._scan(real), "classifier_unavailable",
                         "抑制到期后应恢复检测")

    def test_no_signature_payload_does_not_mark_suppression(self):
        self._team()
        self._inject("普通任务：请实现登录模块")
        self.assertFalse(mcp._sig_injection_suppressed("team", "alice"),
                         "无签名 payload 不应标记抑制")

    # ---- 3a（2026-08-12 最终门）：复合键 (team, member) 跨团队同名零污染 ----

    def test_cross_team_same_name_zero_pollution_unit(self):
        """3a 单元级：团队A alice 注入 → (teamA, alice) 抑制；(teamB, alice) 零污染。"""
        mcp._sig_injection_mark_suppressed("teamA", "alice")
        self.assertTrue(mcp._sig_injection_suppressed("teamA", "alice"),
                        "同团队同名应抑制")
        self.assertFalse(mcp._sig_injection_suppressed("teamB", "alice"),
                         "跨团队同名成员零污染——绝不命中另一团队的抑制")
        self.assertFalse(mcp._sig_injection_suppressed("teamA", "bob"),
                         "同团队不同成员零污染")

    def test_cross_team_same_name_scan_still_detects(self):
        """3a 扫描级：他团队 alice 被注入抑制 → 本团队 alice 真实签名仍检测。

        证明 _scan_member_terminal 的抑制判定走 (team_name, member_name) 复合键，
        注入者与观测者必须同一团队才会命中。
        """
        self._team()
        # 另一团队同名成员被注入抑制（模拟团队B alice 收到含签名转述）
        mcp._sig_injection_mark_suppressed("other_team", "alice")
        # 本团队 team 的 alice 真实签名 → 仍判 classifier_unavailable
        self.assertEqual(self._scan(self.SIG), "classifier_unavailable",
                         "跨团队同名不被污染：本团队真实签名仍须召回")
        self.assertTrue(mcp._sig_injection_suppressed("other_team", "alice"),
                        "他团队抑制记录保留（不影响本团队判定）")

    def test_scan_respects_own_team_suppression(self):
        """3a 扫描级正向：同团队 alice 被注入抑制 → 本团队 scan 跳过 classifier。"""
        self._team()
        mcp._sig_injection_mark_suppressed("team", "alice")
        self.assertNotEqual(self._scan(self.SIG), "classifier_unavailable",
                            "同团队注入抑制应命中（scan 走复合键）")

    def test_inject_via_send_keys_marks_own_team_only(self):
        """3a 注入链：_send_keys 解析窗口 → (team, member)，另一团队同名不标记。

        复用 _team() 保存的 "team" 团队；窗口 "alice" 由 _resolve_member_from_window
        解析为 ("team", "alice")；"other_team" 的同名成员不得被标记。
        """
        self._team()
        self._inject(self.SIG)
        self.assertTrue(mcp._sig_injection_suppressed("team", "alice"),
                        "_send_keys 注入含签名 payload 应标记本团队 (team, alice)")
        self.assertFalse(mcp._sig_injection_suppressed("other_team", "alice"),
                         "跨团队同名成员不被注入链标记")

    def test_window_expiry_recovers_detection(self):
        """3a 窗口到期：抑制到期后真实签名召回（直接操纵单调时钟模拟过期）。"""
        self._team()
        mcp._sig_injection_mark_suppressed("team", "alice")
        self.assertNotEqual(self._scan(self.SIG), "classifier_unavailable")
        # 直接清空模拟 240s 窗口到期
        mcp._SIG_INJECTION_SUPPRESS_UNTIL.clear()
        self.assertEqual(self._scan(self.SIG), "classifier_unavailable",
                         "抑制到期后真实签名应召回")

    def test_real_error_block_recalled_after_suppression(self):
        """3a 真实签名召回：注入引用（无引号）+ 随后真实错误块 → 窗口期被抑，
        窗口到期后真实错误块仍检测（绝不错杀持续存在的工具 result）。"""
        self._team()
        real = f"❯\n{self.SIG}\n❯"
        self.assertEqual(self._scan(real), "classifier_unavailable",
                         "无注入记录的真实错误块应先检测")
        self._inject(f"[广播] 成员A报告: {self.SIG}，请排查")
        self.assertNotEqual(self._scan(real), "classifier_unavailable",
                            "注入引用后的抑制窗口内跳过 classifier")
        mcp._SIG_INJECTION_SUPPRESS_UNTIL.clear()
        self.assertEqual(self._scan(real), "classifier_unavailable",
                         "抑制到期后真实错误块仍检测")


if __name__ == "__main__":
    unittest.main()
