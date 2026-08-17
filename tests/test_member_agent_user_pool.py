"""
成员级 Agent 用户池 + provider 校验 —— 数据层测试（common.tmux_utils）
====================================================================

本轮新增的数据层函数与字段配套测试，覆盖五块：
  1. resolve_pool_atype        — 三级链 member.agent → team.default_agent → "claude"
  2. _profile_matches_atype    — provider 判定（typed 类型相等 / legacy 按 base_url /
                                 atype 空不过滤）
  3. member_pool_is_activated  — 原始 agent_user_pool 为非空 list 即激活（非净化结果）
  4. select_failover_candidate — 成员池优先、类型过滤，reason ∈
                                 pool-empty / pool-type-mismatch / pool-exhausted
  5. set_member_agent_user_pool — 写成员池，内建三道校验（registry / 哨兵 / provider），
                                 失败绝不部分写入

核心场景（裁定映射）：
  - 跨 provider 换号防呆：codex 成员写 claude key 必须被数据层拒绝并给出可读原因
    （含 "不匹配"/"静默空转"），只靠 TUI 置灰挡不住 MCP 工具直接写；
  - 成员池激活后团队池完全不参与：即使成员池被净化清空、团队池还有货，也不回落
    （用户裁定：哪怕成员池配额全部用完也不切回 team 池）。

数据隔离（双保险）：data_layer.set_data_file(tmp) 拦 data_layer 侧读写 +
tmux_utils.DATA_FILE 指向同一 tmp（拦 tmux_utils 侧可能出现的模块级引用）。
对真实 ~/.mult_agent_mcp/teams_data.json 零读写。
"""

import tempfile
import unittest
from pathlib import Path

from common import data_layer
from common import tmux_utils
from common.data_layer import load_data, save_data
from common.tmux_utils import (
    AGENT_USER_NONE,
    _profile_matches_atype,
    member_pool_is_activated,
    resolve_pool_atype,
    select_failover_candidate,
    set_member_agent_user_pool,
)

# =====================================================================
# 语料：全局 registry（typed claude / typed codex / legacy 两种）
# =====================================================================

_CLAUDE_A = {
    "agent_type": "claude",
    "takeover_enabled": True,
    "anthropic_api_key": "sk-ant-a",
    "anthropic_base_url": "https://api.anthropic.com",
    "anthropic_model": "claude-opus-5",
}
_CLAUDE_B = {
    "agent_type": "claude",
    "takeover_enabled": True,
    "anthropic_api_key": "sk-ant-b",
    "anthropic_base_url": "https://api.anthropic.com",
    "anthropic_model": "claude-haiku-4-5",
}
_CODEX_A = {
    "agent_type": "codex",
    "takeover_enabled": False,
    "openai_api_key": "sk-fake-a",
    "openai_base_url": "https://api.openai.com",
    "codex_model": "gpt-4o",
}
_CODEX_B = {
    "agent_type": "codex",
    "takeover_enabled": False,
    "openai_api_key": "sk-fake-b",
    "openai_base_url": "https://api.openai.com",
    "codex_model": "gpt-4o-mini",
}
# legacy profile（无 agent_type 字段）：兜底语义按携带的 base_url 字段判定
_LEGACY_CLAUDE = {"takeover_enabled": True, "anthropic_base_url": "https://api.anthropic.com"}
_LEGACY_CODEX = {"takeover_enabled": True, "openai_base_url": "https://api.openai.com"}
_LEGACY_BARE = {"takeover_enabled": True}  # 无任何 base_url

_REGISTRY = {
    "claude_a": _CLAUDE_A,
    "claude_b": _CLAUDE_B,
    "codex_a": _CODEX_A,
    "codex_b": _CODEX_B,
    "legacy_claude": _LEGACY_CLAUDE,
    "legacy_codex": _LEGACY_CODEX,
    "legacy_bare": _LEGACY_BARE,
}


def _make_member(agent: str, **extra) -> dict:
    member = {"role": "coder"}
    if agent:
        member["agent"] = agent
    member.update(extra)
    return member


def _make_team(default_agent: str = "claude", wrap: bool = True,
               team_pool=None, members=None) -> dict:
    team = {"default_agent": default_agent, "quota_failover": {"wrap": wrap}}
    if team_pool is not None:
        team["agent_user_pool"] = list(team_pool)
    team["members"] = members or {}
    return team


# =====================================================================
# 一、resolve_pool_atype —— 三级链（纯函数，无数据文件依赖）
# =====================================================================

class ResolvePoolAtypeTests(unittest.TestCase):
    """池过滤应使用的 provider 类型：member.agent → team.default_agent → "claude"。"""

    def test_member_agent_wins_over_team_default(self):
        """member.agent 有值 → 用它（跑哪个 CLI 由 member.agent 决定，agent_user 不改变 CLI）。"""
        team = _make_team(default_agent="codex")
        member = _make_member("claude")
        self.assertEqual(resolve_pool_atype(team, member), "claude")

    def test_empty_member_agent_falls_back_to_team_default(self):
        """member.agent 空/缺失 → 团队默认。"""
        team = _make_team(default_agent="codex")
        self.assertEqual(resolve_pool_atype(team, _make_member("")), "codex")
        self.assertEqual(resolve_pool_atype(team, {}), "codex")

    def test_both_empty_falls_back_to_claude(self):
        """member.agent 与 team.default_agent 都空 → "claude" 兜底。"""
        team = _make_team(default_agent="")
        self.assertEqual(resolve_pool_atype(team, _make_member("")), "claude")
        self.assertEqual(resolve_pool_atype({}, {}), "claude")

    def test_codex_member_resolves_codex(self):
        self.assertEqual(resolve_pool_atype(_make_team(), _make_member("codex")), "codex")

    def test_case_insensitive_and_embedded_agent(self):
        """agent_type 大小写不敏感，且从完整命令串中识别关键字。"""
        self.assertEqual(resolve_pool_atype(_make_team(), _make_member("Codex")), "codex")
        self.assertEqual(resolve_pool_atype(_make_team(), _make_member("my-claude-wrapper")), "claude")

    def test_both_keywords_codex_wins(self):
        """命令串同时含 codex 与 claude 时按 agent_type 顺序 codex 优先。"""
        self.assertEqual(resolve_pool_atype(_make_team(), _make_member("claude-codex")), "codex")

    def test_non_dict_args_fallback_claude(self):
        """非 dict 参数（防呆）→ "claude" 兜底，不抛错。"""
        self.assertEqual(resolve_pool_atype(None, None), "claude")
        self.assertEqual(resolve_pool_atype("team", "member"), "claude")


# =====================================================================
# 二、_profile_matches_atype —— provider 判定（纯函数）
# =====================================================================

class ProfileMatchesAtypeTests(unittest.TestCase):
    """profile 能否真正为 atype 类型的 CLI 注入凭证（跨 provider 换号防呆）。"""

    def test_claude_profile_matches_claude(self):
        self.assertTrue(_profile_matches_atype(_CLAUDE_A, "claude"))

    def test_codex_profile_matches_codex(self):
        self.assertTrue(_profile_matches_atype(_CODEX_A, "codex"))

    def test_claude_profile_does_not_match_codex(self):
        """核心场景：claude profile 对 codex CLI 是 False —— 换过去注入为空、立刻再撞配额。"""
        self.assertFalse(_profile_matches_atype(_CLAUDE_A, "codex"))

    def test_codex_profile_does_not_match_claude(self):
        self.assertFalse(_profile_matches_atype(_CODEX_A, "claude"))

    def test_legacy_profile_matches_by_base_url(self):
        """legacy（无 agent_type）按 base_url 字段兜底：与注入回退分支一致。"""
        self.assertTrue(_profile_matches_atype(_LEGACY_CLAUDE, "claude"))
        self.assertTrue(_profile_matches_atype(_LEGACY_CODEX, "codex"))
        self.assertFalse(_profile_matches_atype(_LEGACY_CLAUDE, "codex"))
        self.assertFalse(_profile_matches_atype(_LEGACY_CODEX, "claude"))

    def test_legacy_profile_without_base_url_never_matches(self):
        """legacy 无任何 base_url → 两类 CLI 都 False（注入必为空）。"""
        self.assertFalse(_profile_matches_atype(_LEGACY_BARE, "claude"))
        self.assertFalse(_profile_matches_atype(_LEGACY_BARE, "codex"))

    def test_empty_atype_does_not_filter(self):
        """atype 为空（无法确定 CLI 类型）→ 不过滤，保持既有行为。"""
        self.assertTrue(_profile_matches_atype(_CLAUDE_A, ""))
        self.assertTrue(_profile_matches_atype(_LEGACY_BARE, ""))
        self.assertTrue(_profile_matches_atype(None, ""))

    def test_non_dict_profile_rejected(self):
        self.assertFalse(_profile_matches_atype(None, "claude"))
        self.assertFalse(_profile_matches_atype("claude_a", "claude"))

    def test_other_atype_does_not_filter_pool(self):
        """atype 为 other（自定义 agent 命令）→ 不过滤，池可见可选。

        旧语义是"一律不匹配"，实测即缺陷 A：agent_type() 对不含 claude/codex
        关键字的命令返回 "other"，此前走 return False → 自定义 agent 成员的池
        **一个都选不了**（静默全 False，无任何提示）。

        修正后分层：UI/池层不过滤（人能看见能选），安全阀下沉到自动换号 ——
        select_failover_candidate 对 "other" 返回 pool-other-agent 拒绝机器换号，
        因为注入侧（_agent_user_env_prefix_for_team）对 "other" 一律返回空，
        机器自动换号必然静默空转，必须由人确认。
        """
        self.assertTrue(_profile_matches_atype(_CLAUDE_A, "other"))
        self.assertTrue(_profile_matches_atype(_LEGACY_CLAUDE, "other"))


# =====================================================================
# 三、member_pool_is_activated —— 原始字段判定（纯函数）
# =====================================================================

class MemberPoolIsActivatedTests(unittest.TestCase):
    """激活判定 = 原始 agent_user_pool 为非空 list（非净化结果）。"""

    def test_missing_field_not_activated(self):
        self.assertFalse(member_pool_is_activated({}))
        self.assertFalse(member_pool_is_activated(_make_member("claude")))

    def test_empty_list_not_activated(self):
        self.assertFalse(member_pool_is_activated(_make_member("claude", agent_user_pool=[])))

    def test_non_empty_list_activated(self):
        self.assertTrue(member_pool_is_activated(
            _make_member("claude", agent_user_pool=["claude_a", "claude_b"])
        ))

    def test_none_or_non_list_not_activated(self):
        self.assertFalse(member_pool_is_activated(_make_member("claude", agent_user_pool=None)))
        self.assertFalse(member_pool_is_activated(_make_member("claude", agent_user_pool="claude_a")))
        self.assertFalse(member_pool_is_activated(None))


# =====================================================================
# 数据隔离基类（双保险）—— 本测试对真实路径零读写
# =====================================================================

class _MemberPoolIsolatedTestCase(unittest.TestCase):
    """双保险隔离基类：

    - data_layer.set_data_file(tmp)   —— 拦 data_layer 侧全部读写
    - tmux_utils.DATA_FILE = tmp      —— 拦 tmux_utils 侧模块级引用
    tearDown 双还原（tmux_utils.DATA_FILE 原本不存在则删除）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"

        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)

        self.old_tu_data_file = getattr(tmux_utils, "DATA_FILE", None)
        tmux_utils.DATA_FILE = str(self.data_file)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        if self.old_tu_data_file is None:
            if hasattr(tmux_utils, "DATA_FILE"):
                del tmux_utils.DATA_FILE
        else:
            tmux_utils.DATA_FILE = self.old_tu_data_file
        self.tmp.cleanup()

    def _save(self, team_name: str, team: dict) -> None:
        """把 registry + 单个团队写入 tmp 数据文件。"""
        save_data({
            "agent_users": {k: dict(v) for k, v in _REGISTRY.items()},
            "teams": {team_name: team},
        })

    def _team(self, team_name: str = "pool_team") -> dict:
        return load_data()["teams"][team_name]

    def _member(self, name: str, team_name: str = "pool_team") -> dict:
        return load_data()["teams"][team_name]["members"][name]

    def assert_isolated(self):
        """断言生效路径全指向 tmp 且 != 真实默认路径。"""
        self.assertEqual(str(data_layer.get_data_file()), str(self.data_file))
        self.assertEqual(tmux_utils.DATA_FILE, str(self.data_file))
        self.assertNotEqual(str(self.data_file),
                            str(Path.home() / ".mult_agent_mcp" / "teams_data.json"))


# =====================================================================
# 四、select_failover_candidate —— 成员池优先 + 类型过滤（最关键的 15 个）
# =====================================================================

class SelectFailoverCandidateTeamPoolTests(_MemberPoolIsolatedTestCase):
    """团队池路径（成员池未激活），reason 判定与起点语义。"""

    def test_team_pool_returns_next_same_type(self):
        """团队池激活 + 池内有同类候选 → 返回下一个。"""
        self._save("pool_team", _make_team(
            team_pool=["claude_a", "claude_b"],
            members={"m": _make_member("claude", agent_user="claude_a")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_b", ""))

    def test_team_pool_empty_returns_pool_empty(self):
        """团队池缺失 → (None, "pool-empty")。"""
        self._save("pool_team", _make_team(members={"m": _make_member("claude")}))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-empty")

    def test_team_pool_all_wrong_type_returns_type_mismatch(self):
        """团队池非空但全是异类（claude 成员 + 纯 codex 池）→ pool-type-mismatch，
        绝不静默降级：换过去三处注入全为空，等于原地空转。"""
        self._save("pool_team", _make_team(
            team_pool=["codex_a", "codex_b"],
            members={"m": _make_member("claude")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-type-mismatch")

    def test_team_pool_mixed_types_filters_to_own_type(self):
        """团队池混合类型 → 类型过滤后按自身类型顺序切换，跳过异类。"""
        self._save("pool_team", _make_team(
            team_pool=["claude_a", "codex_a", "claude_b"],
            members={"m": _make_member("claude", agent_user="claude_a")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_b", ""))

    def test_team_pool_tail_wrap_false_returns_exhausted(self):
        """wrap=False 已到池尾 → (None, "pool-exhausted")。"""
        self._save("pool_team", _make_team(
            wrap=False,
            team_pool=["claude_a", "claude_b"],
            members={"m": _make_member("claude", agent_user="claude_b")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-exhausted")

    def test_team_pool_single_element_returns_pool_single(self):
        """池长 1：无处可换 → pool-single（即使 wrap=True 也不原地空转）。

        新语义：与 pool-empty 分开成因 —— "池空"要去查是不是漏配了池，
        "池里只有一个号"是配了但不够用，处置是补第二个号。
        """
        self._save("pool_team", _make_team(
            team_pool=["claude_a"],
            members={"m": _make_member("claude", agent_user="claude_a")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-single")

    def test_team_pool_current_not_in_pool_returns_head(self):
        """current 不在池中 → 返回池首。"""
        self._save("pool_team", _make_team(
            team_pool=["claude_a", "claude_b"],
            members={"m": _make_member("claude", agent_user="ghost_p")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_a", ""))

    def test_team_pool_current_empty_returns_head(self):
        """current 为空（无 agent_user 字段）→ 返回池首。"""
        self._save("pool_team", _make_team(
            team_pool=["claude_a", "claude_b"],
            members={"m": _make_member("claude")},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_a", ""))


class SelectFailoverCandidateMemberPoolTests(_MemberPoolIsolatedTestCase):
    """成员池路径（激活后团队池完全不参与）。"""

    def test_member_pool_returns_next_same_type(self):
        """成员池激活 + 池内有同类候选 → 返回下一个。"""
        self._save("pool_team", _make_team(
            team_pool=["codex_a"],  # 团队池即使有货也不参与
            members={"m": _make_member("claude",
                                       agent_user="claude_a",
                                       agent_user_pool=["claude_a", "claude_b"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_b", ""))

    def test_member_pool_exhausted_wrap_false(self):
        """成员池 wrap=False 到尾 → (None, "pool-exhausted")。"""
        self._save("pool_team", _make_team(
            wrap=False,
            team_pool=["claude_a"],
            members={"m": _make_member("claude",
                                       agent_user="claude_b",
                                       agent_user_pool=["claude_a", "claude_b"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-exhausted")

    def test_member_pool_all_wrong_type_returns_type_mismatch(self):
        """成员池非空但全是异类（claude 成员 + 池里只有 codex）→ pool-type-mismatch。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user_pool=["codex_a", "codex_b"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-type-mismatch")

    def test_member_pool_all_stale_keys_returns_pool_empty(self):
        """成员池 key 全不在 registry（被净化清空）→ pool-empty（池本身为空）。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user_pool=["ghost_a", "ghost_b"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-empty")

    def test_member_pool_exhausted_never_falls_back_to_team_pool(self):
        """裁定核心：成员池被净化清空 + 团队池还有货 → 仍 pool-empty，绝不回落团队池
        （"我只配了这几个号"的预期失效是最大问题，排障也无法判断实际走了哪个池）。"""
        self._save("pool_team", _make_team(
            team_pool=["claude_a", "claude_b"],  # 团队池有可用候选
            members={"m": _make_member("claude",
                                       agent_user_pool=["ghost_a"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-empty")

    def test_member_pool_single_element_returns_pool_single(self):
        """成员池长 1：无处可换 → pool-single（成因与 pool-empty 分开，见团队池同名用例）。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user="claude_a",
                                       agent_user_pool=["claude_a"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertIsNone(key)
        self.assertEqual(reason, "pool-single")

    def test_member_pool_current_not_in_pool_returns_head(self):
        """成员池 current 不在池中 → 池首。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user="ghost_p",
                                       agent_user_pool=["claude_a", "claude_b"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_a", ""))

    def test_member_pool_current_empty_returns_head(self):
        """成员池 current 为空 → 池首。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user_pool=["claude_a", "claude_b"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_a", ""))

    def test_member_pool_mixed_types_skips_foreign_type(self):
        """成员池混合类型 → 过滤后按自身类型顺序切换（claude_a → claude_b，跳过 codex_a）。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user="claude_a",
                                       agent_user_pool=["claude_a", "claude_b", "codex_a"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("claude_b", ""))

    def test_legacy_profile_matches_own_type_in_pool(self):
        """legacy profile（无 agent_type，带 base_url）按类型参与成员池切换。"""
        self._save("pool_team", _make_team(
            members={"m": _make_member("claude",
                                       agent_user="claude_a",
                                       agent_user_pool=["claude_a", "legacy_claude"])},
        ))
        key, reason = select_failover_candidate(self._team(), self._member("m"))
        self.assertEqual((key, reason), ("legacy_claude", ""))


# =====================================================================
# 五、set_member_agent_user_pool —— 三道校验 + 原子性
# =====================================================================

class SetMemberAgentUserPoolTests(_MemberPoolIsolatedTestCase):
    """写成员池：registry / 哨兵 / provider 三道校验，失败绝不部分写入。"""

    def _save_with(self, member_name="m", member=None, team_name="pool_team"):
        self._save(team_name, _make_team(members={member_name: member or _make_member("claude")}))
        return member_name

    # ---- 成功路径 ----

    def test_write_success_order_preserved_deduped(self):
        """写入成功 → member["agent_user_pool"] 存在且保序去重，cursor 归零，落盘。"""
        m = self._save_with(member=_make_member("claude"))
        ok, msg = set_member_agent_user_pool("pool_team", m, ["claude_b", "claude_a", "claude_b"])
        self.assertTrue(ok, msg)
        member = self._member(m)
        self.assertEqual(member["agent_user_pool"], ["claude_b", "claude_a"])
        self.assertEqual(member["agent_user_pool_cursor"], 0)
        self.assert_isolated()

    def test_write_legacy_profile_matching_type_ok(self):
        """legacy profile（带对应 base_url）与成员类型匹配 → 可写入。"""
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, ["legacy_claude"])
        self.assertTrue(ok, msg)
        self.assertEqual(self._member(m)["agent_user_pool"], ["legacy_claude"])

    def test_clear_pool_pops_field_and_cursor(self):
        """空列表 → 取消激活：agent_user_pool / cursor 都被 pop，返回"已取消"。"""
        m = self._save_with(member=_make_member("claude", agent_user_pool=["claude_a"]))
        ok, msg = set_member_agent_user_pool("pool_team", m, [])
        self.assertTrue(ok, msg)
        self.assertIn("已取消", msg)
        member = self._member(m)
        self.assertNotIn("agent_user_pool", member)
        self.assertNotIn("agent_user_pool_cursor", member)
        self.assert_isolated()

    # ---- 校验拒绝路径（三道防线） ----

    def test_key_not_in_registry_rejected_no_write(self):
        """key 不在 registry → 拒绝，消息含 key 名，合法 key 也不写入（原子性）。"""
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, ["claude_a", "ghost_p"])
        self.assertFalse(ok)
        self.assertIn("ghost_p", msg)
        self.assertNotIn("agent_user_pool", self._member(m))

    def test_agent_user_none_sentinel_rejected(self):
        """AGENT_USER_NONE 哨兵 → 拒绝，绝不进池。"""
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, [AGENT_USER_NONE])
        self.assertFalse(ok)
        self.assertIn("哨兵", msg)
        self.assertNotIn("agent_user_pool", self._member(m))

    def test_provider_mismatch_codex_member_claude_key_rejected(self):
        """★ 数据层强校验核心：codex 成员写 claude key → 拒绝，原因精确含
        "类型"/"不匹配"/"静默空转"（可读原因给运维，不是裸 False）。"""
        m = self._save_with(member=_make_member("codex"))
        ok, msg = set_member_agent_user_pool("pool_team", m, ["claude_a"])
        self.assertFalse(ok)
        self.assertIn("类型", msg)
        self.assertIn("不匹配", msg)
        self.assertIn("静默空转", msg)
        self.assertNotIn("agent_user_pool", self._member(m))

    def test_provider_mismatch_claude_member_codex_key_rejected(self):
        """反向同样拒绝：claude 成员写 codex key。"""
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, ["codex_a"])
        self.assertFalse(ok)
        self.assertIn("不匹配", msg)
        self.assertIn("静默空转", msg)
        self.assertNotIn("agent_user_pool", self._member(m))

    def test_partial_write_failure_rolls_back_entirely(self):
        """部分写入失败 → 全量不写（原子性）：首个合法 key 也不能残留。"""
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, ["claude_a", "codex_a"])
        self.assertFalse(ok)
        self.assertIn("不匹配", msg)
        self.assertNotIn("agent_user_pool", self._member(m))

    def test_failure_keeps_existing_pool_untouched(self):
        """已有池时写入失败 → 原池原样保留（不被覆盖、不部分更新）。"""
        m = self._save_with(member=_make_member("claude", agent_user_pool=["claude_a"]))
        ok, msg = set_member_agent_user_pool("pool_team", m, ["claude_b", "codex_a"])
        self.assertFalse(ok)
        member = self._member(m)
        self.assertEqual(member["agent_user_pool"], ["claude_a"], "失败不得覆盖既有成员池")

    # ---- 参数/存在性防呆 ----

    def test_member_not_found(self):
        self._save_with()  # 先建团队，确保走的是成员校验分支而非"团队不存在"
        ok, msg = set_member_agent_user_pool("pool_team", "nobody", ["claude_a"])
        self.assertFalse(ok)
        self.assertIn("成员不存在", msg)

    def test_team_not_found(self):
        ok, msg = set_member_agent_user_pool("no_such_team", "m", ["claude_a"])
        self.assertFalse(ok)
        self.assertIn("团队不存在", msg)

    def test_non_list_keys_rejected(self):
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, "claude_a")
        self.assertFalse(ok)
        self.assertIn("必须是列表", msg)

    def test_empty_string_key_rejected(self):
        m = self._save_with()
        ok, msg = set_member_agent_user_pool("pool_team", m, ["claude_a", ""])
        self.assertFalse(ok)
        self.assertIn("非空字符串", msg)
        self.assertNotIn("agent_user_pool", self._member(m))


if __name__ == "__main__":
    unittest.main()
