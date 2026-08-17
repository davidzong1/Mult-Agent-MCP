"""direct 撕裂态 leader：经用户确认后转 tmux 并建窗。

背景（用户实测 P0）：按 T 启动终端后"leader 消失了"，只剩一个没有任何 Agent 的
`__base`。现场数据是 ``leader='zwc-boss'`` / ``leader_type='direct'``，而
``zwc-boss`` 在成员表里是一条**真实成员记录**（role='member'，还留着旧 tmux 窗口
元数据）—— 元数据撕裂态，来自历史上某次 ``claim_leader``（"原 tmux leader 终端
存活但非受管 → 降级为普通成员"分支：leader 名字原样保留、leader_type 改写成
direct）。于是 launch_terminals 按 direct 处理：只建裸 `__base` + 成员窗，不建
leader 窗。

修法（用户裁定）：**只对撕裂子集**额外开一条经确认的通道。
  · 默认（不传 promote_direct_leader）：direct 语义一字不变 —— 不建 leader 窗、
    不改 leader_type。task1 已验收的防双 leader / 静默夺权契约完整保留。
  · 传 True（TUI 确认框已征得用户同意）：写 leader_type='tmux' + role='leader'，
    建 leader 窗；转 tmux 后 leader 自动纳入 _scan_leader_terminal 的配额检测
    （direct 在那里是早返回，压根不检测）。
  · 真外部会话 leader（名字不在 members 里）**任何情况下都不提升** —— 否则就是
    静默夺权，会和还活着的外部会话形成双 leader 同时派单。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from common import data_layer
from common.leader_recovery import direct_leader_is_team_member
from tui import tui_screens as ts


class DirectLeaderTornStateTests(unittest.TestCase):
    """1. 撕裂态判定原语。"""

    def test_direct_leader_that_is_a_member_is_torn(self):
        team = {"leader_type": "direct", "leader": "zwc-boss",
                "members": {"zwc-boss": {"role": "member"}, "alice": {}}}
        self.assertTrue(direct_leader_is_team_member(team))

    def test_external_direct_leader_is_not_torn(self):
        """真外部会话 leader：名字不在 members 里 → 永不提升。"""
        team = {"leader_type": "direct", "leader": "外部会话",
                "members": {"alice": {}}}
        self.assertFalse(direct_leader_is_team_member(team))

    def test_tmux_leader_is_not_torn(self):
        team = {"leader_type": "tmux", "leader": "lead",
                "members": {"lead": {"role": "leader"}}}
        self.assertFalse(direct_leader_is_team_member(team))

    def test_empty_type_is_not_torn(self):
        team = {"leader_type": "", "leader": "lead", "members": {"lead": {}}}
        self.assertFalse(direct_leader_is_team_member(team))

    def test_missing_leader_and_empty_team_are_safe(self):
        self.assertFalse(direct_leader_is_team_member({}))
        self.assertFalse(direct_leader_is_team_member(
            {"leader_type": "direct", "leader": "", "members": {"a": {}}}))
        self.assertFalse(direct_leader_is_team_member(
            {"leader_type": "direct", "leader": "x", "members": {}}))


class DirectLeaderPromoteLaunchTests(unittest.TestCase):
    """2. launch_terminals 的提升行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.root / "teams_data.json")
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.ctx = self.root / "ctx"
        self.ctx.mkdir()
        self.calls: list[list[str]] = []
        self.session = f"mcp_team_{uuid.uuid4().hex[:6]}"

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _save_team(self, *, leader="zwc-boss", members=None):
        members = members if members is not None else {
            "zwc-boss": {"role": "member", "agent": "claude"},
            "alice": {"role": "coder", "agent": "claude"},
        }
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": leader,
            "leader_type": "direct",
            "default_agent": "claude",
            "members": members,
        }}})

    def _fake_tmux_run(self, cmd, timeout=10):
        self.calls.append(list(cmd))
        if cmd[0] == "-V":
            return 0, "tmux 3.2", ""
        if cmd[0] == "has-session":
            return 1, "", ""
        if cmd[0] == "list-windows":
            return 0, "$1\t1000\t@1\t__base", ""
        return 0, "", ""

    def _launch(self, **kwargs):
        patches = [
            mock.patch.object(ts, "_tmux_run", side_effect=self._fake_tmux_run),
            mock.patch.object(ts, "_tmux_session", return_value=self.session),
            mock.patch.object(ts, "configure_claude_mcp", return_value=(True, "")),
            mock.patch.object(ts, "configure_codex_mcp", return_value=(True, "")),
            mock.patch.object(ts, "write_claude_permissions", return_value=""),
            mock.patch.object(ts, "claude_agent_user_launch", return_value=("", "")),
            mock.patch.object(ts.classifier_fallback, "claude_terminal_allow_tools",
                              return_value=[]),
            mock.patch.object(ts, "get_agent_user_env_prefix", return_value=[]),
            mock.patch.object(ts, "get_proxy_env_prefix", return_value=[]),
            mock.patch.object(ts, "merge_env_prefixes", return_value=[]),
            mock.patch.object(ts, "resolve_agent_model", return_value=""),
            mock.patch.object(ts, "resolve_member_effort", return_value=""),
            mock.patch.object(ts, "_leader_terminal_restart_blocked", return_value=False),
            mock.patch.object(ts, "_record_leader_reentry", return_value=None),
            mock.patch.object(ts, "_remember_member_window_id", return_value=""),
            mock.patch.object(ts, "_inject_claude_leader_prompt", return_value=(0, "")),
            mock.patch.object(ts.time, "sleep", return_value=None),
        ]
        for p in patches:
            p.start()
        try:
            return ts.launch_terminals("team", **kwargs)
        finally:
            for p in patches:
                p.stop()

    def _has_window(self, window_name):
        for cmd in self.calls:
            if cmd and cmd[0] in ("new-session", "new-window") and "-n" in cmd:
                if cmd[cmd.index("-n") + 1] == window_name:
                    return True
        return False

    def _team(self):
        return data_layer.load_data()["teams"]["team"]

    # -- 默认不提升：task1 契约回归网 ---------------------------------------

    def test_default_keeps_direct_semantics(self):
        """不传参 = 现状：不建 leader 窗、不改 leader_type、不改 role。"""
        self._save_team()
        ok, msg = self._launch()
        self.assertTrue(ok, msg)
        self.assertFalse(self._has_window("zwc-boss"),
                         "默认不得创建 leader 窗（防双 leader / 静默夺权）")
        self.assertEqual(self._team()["leader_type"], "direct")
        self.assertEqual(self._team()["members"]["zwc-boss"]["role"], "member")

    def test_default_launch_still_starts_members(self):
        self._save_team()
        ok, _ = self._launch()
        self.assertTrue(ok)
        self.assertTrue(self._has_window("alice"), "非 leader 成员窗照常启动")

    # -- 确认后提升 ---------------------------------------------------------

    def test_promote_creates_leader_window_and_calibrates_type(self):
        self._save_team()
        ok, msg = self._launch(promote_direct_leader=True)
        self.assertTrue(ok, msg)
        self.assertTrue(self._has_window("zwc-boss"),
                        "确认提升后必须创建 leader 窗（这正是用户看到的'leader 消失'）")
        team = self._team()
        self.assertEqual(team["leader_type"], "tmux",
                         "提升后必须落 tmux，否则 revive/wakeup/配额检测全被门禁拒绝")
        self.assertEqual(team["members"]["zwc-boss"]["role"], "leader",
                         "与详情页按 L 同口径：leader_type 与 role 一起改")

    def test_promote_result_message_states_what_happened(self):
        self._save_team()
        ok, msg = self._launch(promote_direct_leader=True)
        self.assertTrue(ok)
        self.assertIn("direct", msg)
        self.assertIn("tmux", msg)
        self.assertNotIn("仅启动成员", msg, "提升后不应再走 direct 文案")

    def test_promoted_leader_enters_quota_detection_scope(self):
        """配额检测的唯一门槛就是 leader_type=='tmux'（见 _scan_leader_terminal）。"""
        self._save_team()
        self._launch(promote_direct_leader=True)
        import mult_agent_mcp as mcp
        team = self._team()
        self.assertEqual(team.get("leader_type"), "tmux")
        self.assertTrue(mcp._is_leader_member(team, "zwc-boss"))

    # -- 外部会话 leader 绝不提升 --------------------------------------------

    def test_external_direct_leader_never_promoted_even_with_flag(self):
        """leader 不在 members 里 = 真外部会话，传 True 也不得提升。"""
        self._save_team(leader="外部会话",
                        members={"alice": {"role": "coder", "agent": "claude"}})
        ok, msg = self._launch(promote_direct_leader=True)
        self.assertTrue(ok, msg)
        self.assertFalse(self._has_window("外部会话"),
                         "外部会话 leader 建窗 = 静默夺权 + 双 leader")
        self.assertEqual(self._team()["leader_type"], "direct",
                         "外部会话 leader 的 leader_type 不得被改写")


if __name__ == "__main__":
    unittest.main()
