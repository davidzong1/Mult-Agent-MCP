"""
Provider 防呆漏洞回归语料 —— 先红后绿（仅改 tests/，生产代码由 coder/refactor 修）
==================================================================================

背景：用户报防呆漏洞。coder-claude 改 common/tmux_utils.py 数据层（agent_type
"other" 语义、legacy profile 判定、团队池同类校验），refactor-claude 改 tui/
（团队池初始锁按 team.default_agent）。本文件是**语料**：把已核实的缺陷现象
固化成测试，复现现状 + 锁定修复后的最终语义。

第一步实测结论（2026-08-09，已对照生产代码逐条核实）：
  - coder 已落地（组 1/组 2/组 3）：
      · atype="other" → _profile_matches_atype 不过滤（return True），自动
        换号安全阀在 select_failover_candidate 的 "pool-other-agent" 分支
        拒绝 —— 自定义 agent 注入侧对 "other" 一律返回空，机器换号必空转；
      · legacy 改为三组字段（base_url / api_key / model）单边推断：两边都
        像 / 都不像 → 无法确定 "" → 两类 CLI 都不过（_profile_resolved_atype）；
      · 团队池 set_agent_user_pool 补内部 provider 一致性校验：池内混号 →
        拒绝；无法确定 provider 的 profile → 拒绝补 agent_type；与
        team.default_agent 不一致**不强制拒绝**（成员可各自覆盖 agent），
        但返回消息带提示（"默认 agent 的成员将无法使用此池"）。
  - refactor 已落地（组 4）：
      · AgentUserPoolDialog 初始锁 = agent_type(team.default_agent or
        "claude")（"default" 模式，打开即收紧，消灭"第一下点错 provider"
        窗口）；全选取消不释放锁；按 t 切 "all" 才回落动态锁。
  → 以下断言全部按**最终语义**锁定（防回归，红 = 回归）。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json；
TUI 用例复用 tests/test_agent_user_pool_tui.py 的既有夹具风格。
"""

import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest import IsolatedAsyncioTestCase

from textual.app import App
from textual.widgets import Label
from textual.widgets._selection_list import SelectionList

from common import data_layer
from common.data_layer import load_data, save_data
from common.tmux_utils import (
    _profile_matches_atype,
    agent_type,
    get_agent_user_pool,
    resolve_pool_atype,
    select_failover_candidate,
    set_agent_user_pool,
    set_member_agent_user_pool,
)
from tui.tui_dialogs import AgentUserPoolDialog
from tui.tui_screens import TeamManagerApp

# ---------------------------------------------------------------
# 组 1/2/3 数据层 fixture
# ---------------------------------------------------------------

# typed ×2；dual_legacy = 双组字段（两边都像 → 无法确定）；api_only_legacy =
# 双 api_key（也两边都像 → 无法确定）；claude_only_legacy / codex_only_legacy =
# 单边 legacy（新语义下应精确匹配对应 provider）。
_PROVIDER_DATA = {
    "agent_users": {
        "claude_p": {
            "agent_type": "claude", "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-test",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        },
        "codex_p": {
            "agent_type": "codex", "takeover_enabled": True,
            "openai_api_key": "sk-fake",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-4o",
        },
        "dual_legacy": {  # legacy + 双组字段 → 两边都像 → 无法确定
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-legacy",
            "anthropic_base_url": "https://api.anthropic.com",
            "openai_api_key": "sk-openai-legacy",
            "openai_base_url": "https://api.openai.com",
        },
        "api_only_legacy": {  # legacy + 双 api_key（无 base_url/model）→ 也两边都像
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-key-only",
            "openai_api_key": "sk-openai-key-only",
        },
        "claude_only_legacy": {  # 单边：只有 claude 组字段
            "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-single",
        },
        "codex_only_legacy": {  # 单边：只有 codex 组字段
            "takeover_enabled": True,
            "openai_api_key": "sk-openai-single",
        },
    },
    "teams": {
        "guard_team": {"members": {}},
    },
}


class _DataLayerBase(unittest.TestCase):
    """数据隔离基类（临时 teams_data.json，不触碰真实数据）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_PROVIDER_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    def _team(self) -> dict:
        return load_data()["teams"]["guard_team"]


# ---------------------------------------------------------------
# 组 1 — agent_type() 返回 "other" 的连锁后果（最终语义锁定）
# ---------------------------------------------------------------

class OtherAgentTypeChainTests(_DataLayerBase):
    """agent_type("my-wrapper.sh") == "other"。

    最终语义：_profile_matches_atype 对 "other" 不过滤（池可见可选），
    自动换号安全阀在 select_failover_candidate 的 "pool-other-agent" 分支
    拒绝 —— 自定义 agent 注入侧对 "other" 一律返回空，机器换号必空转。
    """

    def test_agent_type_returns_other_for_wrapper(self):
        """基础事实：不含 claude/codex 子串的命令 → "other"。"""
        self.assertEqual(agent_type("my-wrapper.sh"), "other")

    def test_other_atype_does_not_filter_typed_or_legacy(self):
        """atype="other" → 不过滤：typed 与 legacy profile 都可见可选。

        修复前（漏洞）：走到末尾 return False → 所有 profile 全不匹配 →
        自定义 agent 成员池一个都不能选。修复后改为不过滤，由
        select_failover_candidate 的 "other" 分支统一把关。
        """
        typed = _PROVIDER_DATA["agent_users"]["claude_p"]
        legacy = _PROVIDER_DATA["agent_users"]["dual_legacy"]
        for name, profile in (("typed", typed), ("legacy", legacy)):
            actual = _profile_matches_atype(profile, "other")
            self.assertTrue(
                actual,
                f"{name} profile 对 atype='other' 应不过滤（实际值: {actual}）")

    def test_other_atype_member_pool_kept(self):
        """连锁修复：agent="my-wrapper.sh" 的成员池在 "other" 下完整保留。

        修复前（漏洞）：池被清空 → 该成员一个号都选不到。
        """
        member = {"agent": "my-wrapper.sh", "agent_user_pool": ["claude_p", "codex_p"]}
        pool = get_agent_user_pool(self._team(), member, atype="other")
        self.assertEqual(
            pool, ["claude_p", "codex_p"],
            f"atype='other' 下成员池应完整保留（实际值: {pool}）")

    def test_other_agent_failover_rejected_by_safety_valve(self):
        """安全阀：atype="other" + 非空池 → 拒绝自动换号（pool-other-agent）。

        不过滤 ≠ 能换号：注入侧对 "other" 全返回空，机器换号必静默空转，
        必须由人确认。这是修复的关键配套，删掉即回归漏洞。
        """
        member = {"agent": "my-wrapper.sh", "agent_user_pool": ["claude_p"]}
        key, reason = select_failover_candidate(self._team(), member)
        self.assertIsNone(key, "自定义 agent 不应被自动换号（实际 key: {key!r}）")
        self.assertEqual(reason, "pool-other-agent")

    def test_empty_agent_fallback_claude_hides_real_type(self):
        """agent="" 且 team.default_agent="" → 兜底 "claude" 掩盖真实 CLI 类型。

        证据：resolve_pool_atype 返回 "claude"，codex profile 因此被判不匹配
        —— 一个实际是 codex 的成员（agent 字段为空）在 claude 兜底下永远
        选不到 codex 号。已知现状（三级链约定），不是本轮修复对象，固化为
        行为基线。
        """
        member = {"agent": ""}
        team = {"default_agent": ""}
        resolved = resolve_pool_atype(team, member)
        self.assertEqual(resolved, "claude", "空 agent + 空默认应兜底 claude")
        matched = _profile_matches_atype(
            _PROVIDER_DATA["agent_users"]["codex_p"], resolved)
        self.assertFalse(
            matched,
            f"codex profile 在兜底 atype='{resolved}' 下应不匹配（实际值: {matched}）")


# ---------------------------------------------------------------
# 组 2 — legacy profile 判定（最终语义锁定）
# ---------------------------------------------------------------

class LegacyProfileMatchTests(_DataLayerBase):
    """无 agent_type 的 legacy profile 判定（三组字段单边推断）。"""

    def test_dual_field_legacy_matches_neither(self):
        """双组字段 legacy → 无法确定 → claude/codex 都不过。

        修复前（漏洞）：只看 base_url → 对 claude 和 codex 都返回 True →
        codex 成员能选到"看起来也像 claude"的号。修复后：两边都像 → 拒绝
        进池，宁可让人补 agent_type 也不放可能空转的号。
        """
        dual = _PROVIDER_DATA["agent_users"]["dual_legacy"]
        for atype in ("claude", "codex"):
            actual = _profile_matches_atype(dual, atype)
            self.assertFalse(
                actual,
                f"dual 字段 legacy 对 atype='{atype}' 应不匹配（实际值: {actual}）")

    def test_dual_field_legacy_not_selectable_in_codex_pool(self):
        """用户现象修复：dual legacy 放进团队池后，codex 池过滤不再选中它。

        池里只剩 dual_legacy（无法确定）与 claude_p（非 codex）→ codex
        过滤后为空 —— 一个号都不会漏给异类成员。
        """
        data = load_data()
        data["teams"]["guard_team"]["agent_user_pool"] = ["dual_legacy", "claude_p"]
        save_data(data)
        pool = get_agent_user_pool(self._team(), atype="codex")
        self.assertNotIn(
            "dual_legacy", pool,
            f"dual legacy 不应被 codex 池过滤选中（实际池: {pool}）")
        self.assertEqual(pool, [], "codex 过滤后不应残留任何号（实际池: {pool}）")

    def test_api_key_only_legacy_matches_neither(self):
        """双 api_key 无 base_url 的 legacy → 两边都像 → 无法确定 → 都不过。

        修复前（只看 base_url → 两边都不像 → 都不过）结果相同、原因不同：
        新语义下它归入"混填两组字段"的不确定类，同样拒绝进池 —— 配了双 key
        应补 agent_type。
        """
        key_only = _PROVIDER_DATA["agent_users"]["api_only_legacy"]
        for atype in ("claude", "codex"):
            actual = _profile_matches_atype(key_only, atype)
            self.assertFalse(
                actual,
                f"api-only legacy 对 atype='{atype}' 应不匹配（实际值: {actual}）")

    def test_single_side_legacy_matches_only_its_provider(self):
        """单边 legacy 精确匹配对应 provider，且不匹配另一侧（修复核心）。

        修复正向证据：只填 claude 组字段 → claude True / codex False；
        只填 codex 组字段 → codex True / claude False。
        """
        claude_side = _PROVIDER_DATA["agent_users"]["claude_only_legacy"]
        codex_side = _PROVIDER_DATA["agent_users"]["codex_only_legacy"]
        self.assertTrue(_profile_matches_atype(claude_side, "claude"),
                        "claude-only legacy 应匹配 claude")
        self.assertFalse(_profile_matches_atype(claude_side, "codex"),
                         "claude-only legacy 不应匹配 codex")
        self.assertTrue(_profile_matches_atype(codex_side, "codex"),
                        "codex-only legacy 应匹配 codex")
        self.assertFalse(_profile_matches_atype(codex_side, "claude"),
                         "codex-only legacy 不应匹配 claude")


# ---------------------------------------------------------------
# 组 3 — 团队池数据层同类校验（最终语义锁定）
# ---------------------------------------------------------------

class TeamPoolDataLayerGuardTests(_DataLayerBase):
    """set_agent_user_pool：内部 provider 一致性 + 无法确定拒绝 + 默认冲突提示。

    最终语义（数据层兜底，MCP 工具直接写也绕不过）：
      - 池内混号 → 拒绝（不部分写入）；
      - 无法确定 provider 的 profile（无 agent_type 且 legacy 字段不唯一/
        缺失）→ 拒绝并提示补 agent_type；
      - 与 team.default_agent 不一致 → **不拒绝**（成员可各自覆盖 agent，
        异类成员由 select_failover_candidate 的 atype 过滤挡掉），但返回
        消息带提示。
    """

    def test_mixed_pool_rejected(self):
        """混合 provider 池 → 拒绝且不落盘（防呆锁数据层兜底）。

        修复前（漏洞）：混合池静默写入成功，TUI 防呆锁被 MCP 直写绕过。
        """
        ok, msg = set_agent_user_pool("guard_team", ["claude_p", "codex_p"])
        self.assertFalse(ok, f"混合池应被拒（返回: {ok}, {msg}）")
        self.assertIn("同 provider", msg)
        self.assertIsNone(
            self._team().get("agent_user_pool"),
            "拒绝后不应落盘（绝无部分写入）")

    def test_undeterminable_profile_rejected(self):
        """无法确定 provider 的 profile → 拒绝并提示补 agent_type。"""
        ok, msg = set_agent_user_pool("guard_team", ["dual_legacy"])
        self.assertFalse(ok, f"无法确定 provider 的 profile 应被拒（返回: {ok}, {msg}）")
        self.assertIn("agent_type", msg)

    def test_wrong_default_pool_writes_with_warning(self):
        """team.default_agent="codex" 时写纯 claude 池 → 允许写入但带提示。

        最终语义：不强制匹配默认 agent（成员可各自覆盖），但必须让操作者
        知情 —— 消息含"默认 agent 的成员将无法使用此池"提示。
        """
        data = load_data()
        data["teams"]["guard_team"]["default_agent"] = "codex"
        save_data(data)
        ok, msg = set_agent_user_pool("guard_team", ["claude_p"])
        self.assertTrue(ok, f"纯 claude 池应可写入（返回: {ok}, {msg}）")
        self.assertIn("无法使用此池", msg, f"应带默认 agent 冲突提示（实际: {msg!r}）")
        self.assertEqual(self._team().get("agent_user_pool"), ["claude_p"])

    def test_pure_pool_matching_default_has_no_warning(self):
        """default_agent 与池类型一致 → 正常写入，无冲突提示（对照）。"""
        ok, msg = set_agent_user_pool("guard_team", ["claude_p"])
        self.assertTrue(ok, f"纯 claude 池应写入成功（返回: {ok}, {msg}）")
        self.assertNotIn("无法使用此池", msg, f"类型一致不应带提示（实际: {msg!r}）")

    def test_member_pool_same_mix_is_rejected(self):
        """成员池同混合 key 也被拒 —— 团队池与成员池防呆锁一致。

        对照证据：成员池先有强校验，团队池本轮补齐同一道锁，两处语义相同。
        """
        data = load_data()
        data["teams"]["guard_team"]["members"] = {"m1": {"role": "coder"}}
        save_data(data)
        ok, msg = set_member_agent_user_pool("guard_team", "m1", ["claude_p", "codex_p"])
        self.assertFalse(ok, f"成员池混合 key 应被拒（返回: {ok}, {msg}）")
        self.assertIn("不匹配", msg)


# ---------------------------------------------------------------
# 组 4 — 团队池 TUI 初始锁（最终语义锁定）
# ---------------------------------------------------------------

_TUI_TEAM = "guard_team"

_TUI_PROFILE_DATA = {
    "agent_users": {
        "A": {  # claude
            "agent_type": "claude", "takeover_enabled": True,
            "anthropic_api_key": "sk-ant-test",
            "anthropic_base_url": "https://api.anthropic.com",
            "anthropic_model": "claude-opus-5",
        },
        "D": {  # codex
            "agent_type": "codex", "takeover_enabled": True,
            "openai_api_key": "sk-fake",
            "openai_base_url": "https://api.openai.com",
            "codex_model": "gpt-4o",
        },
        "E": {},  # legacy：无 agent_type，独立类别
    },
    "teams": {_TUI_TEAM: {"members": {}}},
}


def _make_test_app() -> App[None]:
    """最小 App：只复用生产真实 CSS，不启动 TeamManagerApp 主流程/定时器。"""

    class _TestApp(App[None]):
        CSS = TeamManagerApp.CSS

    return _TestApp()


@asynccontextmanager
async def _pool_pilot(width: int = 100, height: int = 34):
    """挂载 AgentUserPoolDialog，等待布局稳定（照既有夹具）。"""
    app = _make_test_app()
    dialog = AgentUserPoolDialog(_TUI_TEAM)
    async with app.run_test(size=(width, height)) as pilot:
        await pilot.app.push_screen(dialog)
        await pilot.pause()
        await pilot.pause(0.2)
        yield pilot, dialog, pilot.app.screen


async def _click_row(pilot, row: int) -> None:
    """点击 SelectionList 第 row 行（勾选/取消勾选切换，照既有夹具）。"""
    await pilot.click("#agent_user_pool_list", offset=(5, row + 1))
    await pilot.pause()
    await pilot.pause(0.1)


class TeamPoolDialogInitialLockTests(IsolatedAsyncioTestCase):
    """AgentUserPoolDialog 初始锁 = agent_type(team.default_agent or "claude")。

    最终语义（refactor 落地）：打开即收紧（"default" 模式），消灭"第一下
    点错 provider"的窗口；全选取消**不释放锁**；按 t 切 "all" 才回落动态锁。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_TUI_PROFILE_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    async def test_initial_lock_follows_team_default_claude(self):
        """default_agent 未设（兜底 claude）→ 挂载即锁 claude，异 type 行置灰。

        修复前（漏洞）：挂载后 _lock_type is None、所有行 enabled ——
        "打开第一下就能点错 provider"。
        """
        async with _pool_pilot() as (_pilot, dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            self.assertEqual(
                dialog._lock_type, "claude",
                "default_agent 未设应兜底锁 claude（实际锁: "
                f"{dialog._lock_type!r}）")
            self.assertTrue(sel.get_option_at_index(1).disabled, "codex 行应置灰")
            self.assertTrue(sel.get_option_at_index(2).disabled, "legacy 行应置灰")
            self.assertFalse(sel.get_option_at_index(0).disabled, "claude 行应可选")
            hint = screen.query_one("#agent_user_pool_lock", Label)
            self.assertTrue(hint.display, "初始锁提示应可见")
            self.assertIn("claude", hint.render().plain)

    async def test_initial_lock_follows_codex_default(self):
        """default_agent="codex" → 挂载即锁 codex，claude 行置灰（按默认而非硬编码）。"""
        data = load_data()
        data["teams"][_TUI_TEAM]["default_agent"] = "codex"
        save_data(data)
        async with _pool_pilot() as (_pilot, dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            self.assertEqual(dialog._lock_type, "codex")
            self.assertTrue(sel.get_option_at_index(0).disabled, "claude 行应置灰")
            self.assertFalse(sel.get_option_at_index(1).disabled, "codex 行应可选")

    async def test_deselect_all_keeps_initial_lock(self):
        """全选取消不释放初始锁（"default" 模式锁恒在）。

        这是新语义与旧行为（全部取消 → 锁释放）的差异点，删掉即回归
        "第一下点错 provider"窗口。
        """
        async with _pool_pilot() as (pilot, dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            await _click_row(pilot, 0)  # A (claude) → 勾选
            await _click_row(pilot, 0)  # 取消 A → 全空
            self.assertEqual(list(sel.selected), [], "取消后应为空选择")
            self.assertEqual(
                dialog._lock_type, "claude",
                "default 模式下全空也不释放初始锁（实际锁: "
                f"{dialog._lock_type!r}）")
            self.assertTrue(sel.get_option_at_index(1).disabled,
                            "全空后 codex 行仍应置灰")


# ---------------------------------------------------------------
# 组 5 — default_agent 为自定义命令 → 不预锁（leader 落地的三处改动）
# ---------------------------------------------------------------

class TeamPoolDialogOtherDefaultTests(IsolatedAsyncioTestCase):
    """default_agent 为自定义命令（agent_type → "other"）→ 团队池不预锁。

    2026-08-09 新增覆盖（leader 在 tui/tui_dialogs.py 落地三处改动）：
      - _effective_lock：default 模式下仅当 _default_lock_type != "other"
        才返回初始锁 —— 自定义命令团队不预锁，回落动态锁（未勾选 → None）。
        理由：_profile_resolved_atype 永不返回 "other"，锁成 "other" 会让
        每一行都判异类置灰，团队池一个都选不了；
      - 锁 None → 所有行 enabled（"一个都选不了"的直接回归防线）；
      - _update_provider_lock 提示语如实说明"无法自动校验 provider"，
        不再说"已按…锁定"（那与实际不预锁的行为矛盾）。
    与数据层同源：_profile_matches_atype 对 "other" 也不过滤。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        save_data(_TUI_PROFILE_DATA)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    async def test_other_default_has_no_initial_lock(self):
        """default_agent="my-wrapper.sh" → 挂载后 _effective_lock() 是 None，不是 "other"。"""
        data = load_data()
        data["teams"][_TUI_TEAM]["default_agent"] = "my-wrapper.sh"
        save_data(data)
        async with _pool_pilot() as (_pilot, dialog, _screen):
            self.assertEqual(dialog._default_lock_type, "other")
            self.assertIsNone(
                dialog._effective_lock(),
                "自定义命令默认不应预锁成 'other'（实际: "
                f"{dialog._effective_lock()!r}）")
            self.assertIsNone(dialog._lock_type, "挂载后锁状态应同步为 None")

    async def test_other_default_all_rows_enabled(self):
        """同上场景 → 所有行 enabled，无一行 disabled（"一个都选不了"回归防线）。

        修复前（漏洞）：锁成 "other" 后每行都与 resolved type 判异类 →
        全部置灰 → 自定义 agent 团队池一个都选不了。
        """
        data = load_data()
        data["teams"][_TUI_TEAM]["default_agent"] = "my-wrapper.sh"
        save_data(data)
        async with _pool_pilot() as (_pilot, _dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            for i in range(sel.option_count):
                self.assertFalse(
                    sel.get_option_at_index(i).disabled, f"行 {i} 不应置灰")
            self.assertIsNone(_dialog._lock_type)

    async def test_other_default_hint_is_honest(self):
        """提示语含"无法自动校验"，不含"已按…锁定"（文案与行为一致）。"""
        data = load_data()
        data["teams"][_TUI_TEAM]["default_agent"] = "my-wrapper.sh"
        save_data(data)
        async with _pool_pilot() as (_pilot, _dialog, screen):
            hint = screen.query_one("#agent_user_pool_lock", Label)
            self.assertTrue(hint.display, "自定义命令说明提示应可见")
            text = hint.render().plain
            self.assertIn("无法自动校验", text, f"应如实说明无法校验（实际: {text!r}）")
            self.assertIn("自动换号会被拒绝", text, f"应提示自动换号后果（实际: {text!r}）")
            self.assertNotIn("已按", text, f"不应谎称已锁定（实际: {text!r}）")
            self.assertNotIn("锁定", text, f"不应谎称已锁定（实际: {text!r}）")

    async def test_codex_default_still_prelocks(self):
        """default_agent="codex" → 仍预锁 codex、claude 行置灰（other 特判不回归）。"""
        data = load_data()
        data["teams"][_TUI_TEAM]["default_agent"] = "codex"
        save_data(data)
        async with _pool_pilot() as (_pilot, dialog, screen):
            sel = screen.query_one("#agent_user_pool_list", SelectionList)
            self.assertEqual(
                dialog._effective_lock(), "codex",
                "codex 默认仍应预锁（实际: " f"{dialog._effective_lock()!r}）")
            self.assertTrue(sel.get_option_at_index(0).disabled, "claude 行应置灰")
            self.assertFalse(sel.get_option_at_index(1).disabled, "codex 行应可选")


if __name__ == "__main__":
    unittest.main()
