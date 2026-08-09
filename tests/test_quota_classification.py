"""
quota 状态识别 — 语料矩阵 + 双周期确认 + 止血回归测试
================================================================

来源:docs/plan-b-hot-restart-resume.md 阶段1 + 共享上下文区三份讨论产物
(quota-rules-reviewer.md / quota-falsepositive-analyst.md / quota-corpus.md)。
leader 裁定(与讨论产物冲突时以此为准)在本测试的映射:

- 裁定1: quota 关键词是【必要条件】。纯 429 rate limiting(无 quota 词)不算
  quota——限流会自愈,换号只会抖动。→ 语料 P2(429 rate limit)从正例移到反例。
  同样因无关键词被移反例的还有 P1("credit balance is too low" 不在强词表)与
  P8("上游负载已饱和" 无任何 quota 词)。三者的关键词必要条件来自裁定1,
  与语料自身的"Error 前缀+域名"判定依据冲突,以裁定为准。
- 裁定2: 词表剔除裸 "billing"(启动横幅 "API Usage Billing" 必现)与裸 "quota"。
- 裁定3: 白名单排除 disk quota exceeded(EDQUOT)与 http 402 downloading(npm/uvx)。
- 裁定4: 运行中的 CLI 是 alternate screen,capture 只拿到约 35 行视口,
  zone(尾 16 行)限定因此更有效。

判定顺序(要求 A): approval → live-tool(busy) → busy_markers(busy) → quota
→ dead → idle → unknown。suspect 在分类器返回 unknown,绝不 idle(要求 B)。

数据隔离:复用 test_leader_classifier_claude_tools.py 的 _IsolatedTestCase 套路
(temp teams_data + tmux mock),绝不触碰真实 teams_data.json。
"""

import os
import tempfile
import unittest
from unittest import mock
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer

# =====================================================================
# 一、正例语料(quota-corpus.md §一,12 条;按裁定1 迁移后为 9 条)
# =====================================================================

P1_CREDIT_BALANCE = (
    "✗ Error: credit balance is too low to access the Anthropic API\n"
    "  Please go to https://console.anthropic.com/settings/plans to subscribe to a plan or contact us.\n"
    "  request-id: req_01XyZabc123\n"
    "❯"
)
P2_RATE_LIMIT = (
    "✗ API Error: 429 Request was rejected due to rate limiting. Please try again soon.\n"
    "  request-id: req_01WfGha456\n"
    "  error: rate_limit_error, status: 429\n"
    "❯"
)
P3_INSUFFICIENT_QUOTA = (
    "✗ Error: 429 insufficient_quota\n"
    "    You exceeded your current quota, please check your plan and billing details.\n"
    "❯"
)
P4_BILLING_HARD_LIMIT = (
    "✗ Error: 429 Billing hard limit has been reached\n"
    "    You have reached the billing hard limit on your account.\n"
    "❯"
)
P5_ZH_BALANCE = "✗ 余额不足，请充值后重试\n❯"
P6_ZH_QUOTA = "Error: 额度不足\n❯"
P7_ZH_ARREARS = "✗ 欠费，请充值后在继续使用\n❯"
P8_ZH_SATURATED = "Error: 当前分组上游负载已饱和，请稍后再试\n❯"
P9_HTTP_402 = (
    "✗ API Error: Request failed with status code 402\n"
    '  error: {"error":{"code":"402","message":"insufficient balance"}}\n'
    "  status: 402, url: https://api.anthropic.com/v1/messages\n"
    "❯"
)
P10_BARE_JSON = (
    '{"error":{"message":"Insufficient Balance","type":"insufficient_funds",'
    '"param":null,"code":"402"}}\n'
    "❯"
)
P11_LITELLM = (
    "✗ litellm.AuthenticationError: Authentication Error, HTTP Error 402: payment required\n"
    "❯"
)
P12_DOUBLE_FAILURE = P3_INSUFFICIENT_QUOTA + "\n" + P3_INSUFFICIENT_QUOTA

# =====================================================================
# 二、反例语料(quota-corpus.md §二,16 条 + 裁定迁移 3 条 = 19 条)
# =====================================================================

N1_READ_DOC = (
    "QUOTA_MARKERS = (\n"
    '    "insufficient balance", "insufficient_quota", "quota exceeded",\n'
    '    "exceeded your current quota", "billing", "payment required",\n'
    '    "余额不足", "额度不足", "欠费",\n'
    '    "402",                      # 谨慎：需与上下文组合，避免误伤\n'
    ")"
)
N2_EDIT_DIFF = (
    "❯ Edit: mult_agent_mcp.py\n"
    "+ QUOTA_MARKERS = (\n"
    '+     "insufficient balance", "insufficient_quota", "quota exceeded",\n'
    '+     "exceeded your current quota", "billing", "payment required",\n'
    '+     "余额不足", "额度不足", "欠费",\n'
    '+     "402",\n'
    "+ )"
)
N3_PYTEST_PASSED = (
    "tests/test_quota_classification.py::test_quota_exhausted PASSED\n"
    "tests/test_quota_classification.py::test_quota_markers_in_code PASSED\n"
    "============================= 2 passed in 0.42s =============================\n"
    "❯"
)
N4_PYTEST_FAILED = (
    '>       assert classify(text) == "quota"\n'
    "E       AssertionError: assert 'unknown' == 'quota'\n"
    "tests/test_quota_classification.py:31: AssertionError"
)
N5_GREP = (
    'mult_agent_mcp.py:196:     "insufficient balance", "insufficient_quota", "quota exceeded",\n'
    'mult_agent_mcp.py:197:     "exceeded your current quota", "billing", "payment required",\n'
    'mult_agent_mcp.py:198:     "余额不足", "额度不足", "欠费",\n'
    "❯"
)
N6_GIT_DIFF = (
    "diff --git a/mult_agent_mcp.py b/mult_agent_mcp.py\n"
    "@@ -192,7 +192,7 @@ def _classify_terminal_output(output: str) -> str:\n"
    '-    "billing", "payment required",\n'
    '+    "余额不足", "额度不足", "欠费",'
)
N7_GIT_LOG = (
    "commit a1b2c3d4e5f6\n"
    "    feat: handle quota exceeded errors from relays\n"
    "    fix: mark blocked_reason when quota exhausted\n"
    "❯"
)
N8_PROSE = "我现在要实现余额不足的识别逻辑，先看一下现有 _classify_terminal_output 的结构"
N9_TASK_INJECT = "[子任务] 讨论 quota 识别：设计 QUOTA_MARKERS 匹配规则，防止余额不足导致的误判"
N10_BANNER = "deepseek/deepseek-v4-flash[1m] with max effort · API Usage Billing"
N11_IDLE_STATUS = "⏸ manual mode on · tokens: 45.2k\n❯"
N12_CLAUDE_BUSY = "✢ Waddling… (12s · ↓ 1.2k tokens)\n❯ esc to interrupt"
N13_CODEX_BUSY = "◦ Working (5s • esc to interrupt)"
N14_TRACEBACK = (
    "Traceback (most recent call last):\n"
    '  File "tests/test_quota_classification.py", line 41, in test_quota_exhausted\n'
    '    assert classify(text) == "quota"\n'
    '  File "/usr/lib/python3.12/re/__init__.py", line 234, in _compile\n'
    "    raise re.error\n"
    "re.error: nothing to repeat at position 7\n"
    "❯"
)
N15_FENCE = (
    "下面是被分类器应该识别的错误示例：\n"
    "\n"
    "```text\n"
    "✗ Error: 402 Payment Required\n"
    '   {"error":{"code":"402"}}\n'
    "```\n"
)
N16_CAT_ECHO = (
    "cat tests/test_quota_classification.py\n"
    "def test_quota_exhausted():\n"
    '    text = "✗ 余额不足，请充值后重试"\n'
    '    assert classify(text) == "quota"'
)

# =====================================================================
# 三、边界例语料(quota-corpus.md §三,4 条)
# =====================================================================

B1_ERROR_SCROLLED_AWAY = N11_IDLE_STATUS  # 真配额错误已滚出 tail → 接受的残余漏判
B2_RETRY_AFTER_ERROR = (
    "✗ Error: 429 insufficient_quota\n"
    "    You exceeded your current quota, please check your plan and billing details.\n"
    "❯\n"
    "[重试] 我换一种方式再试一次...\n"
    "✢ Waddling… (3s · ↓ 800 tokens)"
)
B3_MIXED_SCREEN = (
    "QUOTA_MARKERS = (\n"
    '    "insufficient balance", "insufficient_quota", "quota exceeded",\n'
    '    "余额不足", "402",\n'
    ")\n"
    "✗ Error: 429 insufficient_quota\n"
    "    You exceeded your current quota, please check your plan and billing details.\n"
    "❯"
)
B4_SAME_TWICE = P3_INSUFFICIENT_QUOTA  # 连续两屏同款错误 → 扫描层双周期确认


def _classify(text: str) -> str:
    return mcp._classify_terminal_output(text)


class QuotaClassifierStateTests(unittest.TestCase):
    """直接单元测试 _classify_terminal_output 的 quota 判定(无 tmux/数据依赖)。

    反例断言两件事:绝不为 "quota"(硬约束),且落点为推演过的确定状态——
    每个反例的确定状态已按实现语义逐条推演,注释标明依据;若后续规则演进
    改变落点,应同步更新断言而非放宽 "not quota"。
    """

    # ---- 正例:必须 quota ----
    def test_p3_insufficient_quota_is_quota(self):
        self.assertEqual(_classify(P3_INSUFFICIENT_QUOTA), "quota")

    def test_p4_billing_hard_limit_with_429_is_quota(self):
        """弱词 billing hard limit + 同行 429 佐证(横幅行无 Error 前缀无状态码,不会误伤)。"""
        self.assertEqual(_classify(P4_BILLING_HARD_LIMIT), "quota")

    def test_p5_zh_balance_is_quota(self):
        self.assertEqual(_classify(P5_ZH_BALANCE), "quota")

    def test_p6_zh_quota_is_quota(self):
        self.assertEqual(_classify(P6_ZH_QUOTA), "quota")

    def test_p7_zh_arrears_is_quota(self):
        self.assertEqual(_classify(P7_ZH_ARREARS), "quota")

    def test_p9_http_402_response_is_quota(self):
        """API Error + 402 + 域名:弱词同行佐证;error JSON 行同时含强词 insufficient balance。"""
        self.assertEqual(_classify(P9_HTTP_402), "quota")

    def test_p10_bare_json_error_body_is_quota(self):
        """裸 JSON 错误体({ 开头含 "error"):强词 insufficient balance + 402。"""
        self.assertEqual(_classify(P10_BARE_JSON), "quota")

    def test_p11_litellm_error_is_quota(self):
        self.assertEqual(_classify(P11_LITELLM), "quota")

    def test_p12_double_failure_is_quota(self):
        """agent 自动重试后二次失败,同屏两段错误:单帧即 quota,双周期交给扫描层。"""
        self.assertEqual(_classify(P12_DOUBLE_FAILURE), "quota")

    # ---- 反例:绝不能 quota ----
    def test_p1_credit_balance_is_not_quota(self):
        """裁定1 迁移:无关键词("credit balance" 不在强词表),即使 Error 前缀+域名。"""
        self.assertEqual(_classify(P1_CREDIT_BALANCE), "idle")

    def test_p2_pure_rate_limit_429_is_not_quota(self):
        """裁定1:纯 429 rate limiting 无 quota 词 → 不算 quota;
        error 行 + 429 仍构成嫌疑 → unknown,绝不 idle(不伪造成功)。"""
        self.assertEqual(_classify(P2_RATE_LIMIT), "unknown")

    def test_p8_upstream_saturated_is_not_quota(self):
        """裁定1 迁移:无任何 quota 词("负载已饱和" 不在词表),落普通 idle。"""
        self.assertEqual(_classify(P8_ZH_SATURATED), "idle")

    def test_n1_read_doc_is_not_quota(self):
        """Read 文档正文:词表字面全命中但非错误形态行 → 仅证据 → unknown。"""
        self.assertEqual(_classify(N1_READ_DOC), "unknown")

    def test_n2_edit_diff_is_not_quota(self):
        """Edit diff 回显:G3 行级否决所有 +/- 行;完成后落 idle。"""
        self.assertEqual(_classify(N2_EDIT_DIFF), "idle")

    def test_n3_pytest_passed_is_not_quota(self):
        """pytest 输出:G7 否决 ::test_ / PASSED / ==== 行,idle 正常。"""
        self.assertEqual(_classify(N3_PYTEST_PASSED), "idle")

    def test_n4_pytest_failed_is_not_quota(self):
        """断言详情行:E/> 前缀 + path:line 全部被 G4/G7 否决。"""
        self.assertEqual(_classify(N4_PYTEST_FAILED), "unknown")

    def test_n5_grep_is_not_quota(self):
        self.assertEqual(_classify(N5_GREP), "idle")

    def test_n6_git_diff_is_not_quota(self):
        """git diff:G3 否决;fixture 无 ❯(语料原文),落 unknown。"""
        self.assertEqual(_classify(N6_GIT_DIFF), "unknown")

    def test_n7_git_log_is_not_quota(self):
        """commit message 含 "quota exceeded":非错误形态 → 仅证据 → unknown。"""
        self.assertEqual(_classify(N7_GIT_LOG), "unknown")

    def test_n8_prose_is_not_quota(self):
        self.assertEqual(_classify(N8_PROSE), "unknown")

    def test_n9_task_inject_is_not_quota(self):
        self.assertEqual(_classify(N9_TASK_INJECT), "unknown")

    def test_n10_banner_is_not_quota(self):
        """启动横幅 "API Usage Billing":弱词仅 billing hard limit/details 组合,裸词不匹配。"""
        self.assertEqual(_classify(N10_BANNER), "unknown")

    def test_n11_idle_status_is_idle(self):
        self.assertEqual(_classify(N11_IDLE_STATUS), "idle")

    def test_n12_claude_busy_is_busy(self):
        """live-tool 优先否决一切(quota 判定在 live-tool 之后)。"""
        self.assertEqual(_classify(N12_CLAUDE_BUSY), "busy")

    def test_n13_codex_busy_is_busy(self):
        self.assertEqual(_classify(N13_CODEX_BUSY), "busy")

    def test_n14_traceback_is_not_quota(self):
        """Traceback:G7 否决;re.error 非错误形态行,无 quota 证据 → idle。
        (语料偏好 unknown,但本阶段未含 error→unknown 兜底,以裁定范围为准)"""
        self.assertEqual(_classify(N14_TRACEBACK), "idle")

    def test_n15_fence_is_not_quota(self):
        """markdown 围栏内的假错误(Error+402 完全符合结构):G6 状态化否决。"""
        self.assertEqual(_classify(N15_FENCE), "unknown")

    def test_n16_cat_echo_is_not_quota(self):
        """cat 回显:字符串字面量含 余额不足 但非错误形态 → 仅证据 → unknown。"""
        self.assertEqual(_classify(N16_CAT_ECHO), "unknown")

    # ---- 边界例 ----
    def test_b1_error_scrolled_away_is_idle(self):
        """真配额错误已滚出 tail:接受的残余漏判(靠 busy 超时兜底,不在本阶段)。"""
        self.assertEqual(_classify(B1_ERROR_SCROLLED_AWAY), "idle")

    def test_b2_retry_after_error_is_busy(self):
        """错误后 agent 自行重试(spinner 活跃):live-tool 否决 quota。"""
        self.assertEqual(_classify(B2_RETRY_AFTER_ERROR), "busy")

    def test_b3_mixed_screen_is_quota(self):
        """同屏文档正文 + 真错误行:上方文档行被否决/仅证据,错误行定案。"""
        self.assertEqual(_classify(B3_MIXED_SCREEN), "quota")

    def test_b4_same_twice_is_quota(self):
        """单屏即 quota;连续两屏的稳定性确认在扫描层测试(双周期)。"""
        self.assertEqual(_classify(B4_SAME_TWICE), "quota")

    # ---- 现存缺陷修复回归(E) ----
    def test_approval_markers_are_zone_limited(self):
        """approval 只认底部活动区:残留授权文案滚动在 zone 外不再是 approval。"""
        self.assertEqual(
            _classify("grep result:\nDo you want to proceed? = yes\n❯"), "idle"
        )

    def test_api_key_prompt_is_approval(self):
        """漏网授权提示 "do you want to use this api key" 并入词表(实测曾判 idle)。"""
        self.assertEqual(
            _classify("Detected a custom API key in your environment\nDo you want to use this API key?\n❯ 2. No (recommended)"),
            "approval",
        )

    def test_real_approval_prompt_is_approval(self):
        self.assertEqual(
            _classify("This command requires approval\nDo you want to proceed?\n❯ 1. Yes"),
            "approval",
        )

    def test_bare_shell_prompt_still_dead(self):
        """dead 判定移到 quota 之后,崩溃到 shell 的识别不受影响。"""
        self.assertEqual(_classify("user@host:~/repo$"), "dead")


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类 + tmux mock 惯例(与 test_leader_classifier_claude_tools 一致)。"""

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
        self.old_funcs = {"_find_any_session": mcp._find_any_session}
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR")
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
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_funcs.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _save_team(self, member_capture=None, confirm_cycles=None):
        workspace = self.root / "workspace"
        workspace.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "leader": "lead",
            "leader_type": "tmux",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {
                    "role": "coder",
                    "agent": "claude",
                    "last_task": "build feature X",
                    "last_task_completed": False,
                },
            },
        }
        if confirm_cycles is not None:
            team["quota_failover"] = {"confirm_cycles": confirm_cycles}
        mcp._save({"teams": {"team": team}})
        return workspace

    def _scan_alice(self, capture_output: str, mark_idle_done=True) -> dict:
        """mock tmux 依赖后扫描 alice 一次,返回 (result, member)。"""
        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_member_window_target", side_effect=lambda t, m: m):
                with mock.patch.object(mcp, "_capture_window", return_value=(0, capture_output, "")):
                    result = mcp._scan_member_terminal(
                        "team", "alice", mark_idle_done=mark_idle_done
                    )
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        return result, member


class QuotaScanConfirmTests(_IsolatedTestCase):
    """_scan_member_terminal 的 quota 分支:双周期确认 + 绝不 mark_idle_done。"""

    def test_quota_needs_two_cycles_to_confirm(self):
        """周期1 稳定命中 → quota-suspect 返回 unknown 并保存计数;周期2 → quota-confirmed。"""
        self._save_team()
        r1, m1 = self._scan_alice(P3_INSUFFICIENT_QUOTA)
        self.assertEqual(r1["state"], "unknown")
        self.assertEqual(r1["action"], "quota-suspect:1/2")
        self.assertEqual(m1["quota_hits"], 1)
        self.assertNotIn("blocked_reason", m1)

        r2, m2 = self._scan_alice(P3_INSUFFICIENT_QUOTA)
        self.assertEqual(r2["state"], "quota")
        self.assertEqual(r2["action"], "quota-confirmed")
        self.assertEqual(m2["quota_hits"], 2)
        self.assertEqual(m2["blocked_reason"], "quota")
        self.assertTrue(m2["last_blocked_ts"])

    def test_quota_confirm_cycles_reads_team_config_and_clamps(self):
        """confirm_cycles=1 首周期即确认;越界值钳制到 1..10。"""
        self._save_team(confirm_cycles=1)
        r, m = self._scan_alice(P3_INSUFFICIENT_QUOTA)
        self.assertEqual(r["state"], "quota")
        self.assertEqual(r["action"], "quota-confirmed")

        self._save_team(confirm_cycles=0)  # 钳制到 1
        r, m = self._scan_alice(P3_INSUFFICIENT_QUOTA)
        self.assertEqual(r["state"], "quota")

        self._save_team(confirm_cycles=99)  # 钳制到 10
        r, m = self._scan_alice(P3_INSUFFICIENT_QUOTA)
        self.assertEqual(r["state"], "unknown")
        self.assertEqual(m["quota_hits"], 1)

    def test_quota_never_marks_task_complete(self):
        """核心止血回归:quota 命中(含确认后)绝不执行 mark_idle_done,
        last_task_completed 必须保持 False,不触发 /compact 收尾。"""
        self._save_team()
        for _ in range(3):  # 连续 3 个周期,超过确认阈值仍不标记完成
            result, member = self._scan_alice(P3_INSUFFICIENT_QUOTA)
            self.assertNotEqual(result["action"], "marked-complete")
            self.assertFalse(member["last_task_completed"])

    def test_quota_hits_cleared_on_other_states(self):
        """周期1 quota-suspect → 周期2 idle(错误滚出/成员自愈)→ 计数清零。"""
        self._save_team()
        self._scan_alice(P3_INSUFFICIENT_QUOTA)
        r, m = self._scan_alice(N11_IDLE_STATUS)
        self.assertEqual(r["state"], "idle")
        self.assertEqual(m["quota_hits"], 0)
        self.assertNotIn("blocked_reason", m)

    def test_quota_hits_cleared_on_busy_retry(self):
        """成员错误后自行重试(spinner)→ busy 清零计数,防抖动换号。"""
        self._save_team()
        self._scan_alice(P3_INSUFFICIENT_QUOTA)
        r, m = self._scan_alice(B2_RETRY_AFTER_ERROR)
        self.assertEqual(r["state"], "busy")
        self.assertEqual(m["quota_hits"], 0)

    def test_quota_confirmed_then_self_healed_clears_blocked(self):
        """确认后再现 idle(换号/自愈后)→ blocked_reason 被清除,成员恢复可分配。"""
        self._save_team(confirm_cycles=1)
        self._scan_alice(P3_INSUFFICIENT_QUOTA)
        r, m = self._scan_alice(N11_IDLE_STATUS)
        self.assertEqual(r["state"], "idle")
        self.assertNotIn("blocked_reason", m)
        self.assertEqual(m["quota_hits"], 0)


if __name__ == "__main__":
    unittest.main()
