"""TUI 成员表：leader 恒定置顶（第 1 行）。

背景（用户实测反馈）：成员因配额耗尽换号后，codex leader 余额充足没有参与
换号，但刷新后它在 TUI 成员队列里排到了**第 5 位** —— 要往下翻才能看到
"谁在指挥"。

根因不是换号动的顺序，而是 ``team["members"]`` 就是普通 dict，展示序 = 成员
**写入先后**：团队常常先拉起若干 claude 成员、之后才补一个 codex leader
（或用 set_leader / claim_leader 把某个既有成员提为 leader），leader 自然排在
后面。刷新只是把这个既成事实重新画了一遍。

修法：只改**展示序**（``ordered_team_members``），底层 dict 顺序一字不动 ——
重排数据会牵动一切依赖插入序的读法（窗口创建顺序、遍历顺序、diff 稳定性），
而问题本身只是"看不见"。

本文件覆盖：
  1. 纯函数语义：leader 置顶、其余保持插入序（稳定）、leader 缺省/悬空时
     原样返回且绝不吞成员；
  2. Textual Pilot 端到端：真实挂载 TeamDetailScreen，断言第 1 行就是 leader、
     👑 落在第 1 行、行 key 仍是成员名（选中/编辑/移除按 key 取值，不按行号）；
  3. 回归安全网：底层 teams_data.json 的成员顺序不得被界面刷新改写。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import IsolatedAsyncioTestCase, mock

from textual.widgets import DataTable

from common import data_layer
from common.leader_recovery import ordered_team_members


def _team(leader="lead", names=("reviewer", "coder", "tester", "refactor", "lead")):
    """构造成员 dict —— 默认把 leader 摆在**最后**（复现用户现场：第 5 位）。"""
    return {
        "leader": leader,
        "leader_type": "tmux",
        "default_agent": "claude",
        "members": {
            n: {"role": "leader" if n == leader else "coder",
                "agent": "codex" if n == leader else "claude"}
            for n in names
        },
    }


class OrderedTeamMembersTests(unittest.TestCase):
    """1. 纯函数语义。"""

    def test_leader_is_first_even_when_added_last(self):
        team = _team()
        names = [n for n, _ in ordered_team_members(team)]
        self.assertEqual(names[0], "lead", "leader 必须置顶")
        self.assertEqual(names, ["lead", "reviewer", "coder", "tester", "refactor"])

    def test_non_leader_relative_order_is_stable(self):
        """其余成员相对次序一字不变 —— 刷新之间不得跳动。"""
        team = _team()
        rest = [n for n, _ in ordered_team_members(team)][1:]
        original_rest = [n for n in team["members"] if n != "lead"]
        self.assertEqual(rest, original_rest)

    def test_leader_already_first_is_noop(self):
        team = _team(names=("lead", "reviewer", "coder"))
        self.assertEqual(
            [n for n, _ in ordered_team_members(team)], ["lead", "reviewer", "coder"]
        )

    def test_missing_or_dangling_leader_returns_all_members(self):
        """leader 缺省 / 指向不存在的成员：原样返回，绝不吞成员。"""
        for leader in ("", "ghost"):
            team = _team(names=("reviewer", "coder"))
            team["leader"] = leader
            names = [n for n, _ in ordered_team_members(team)]
            self.assertEqual(names, ["reviewer", "coder"], f"leader={leader!r}")

    def test_empty_team_is_safe(self):
        self.assertEqual(ordered_team_members({}), [])
        self.assertEqual(ordered_team_members({"leader": "x", "members": {}}), [])

    def test_returns_live_member_dicts(self):
        """返回的是成员对象本身（调用方要读 agent/role/agent_user 等字段）。"""
        team = _team()
        pairs = dict(ordered_team_members(team))
        self.assertIs(pairs["lead"], team["members"]["lead"])


class MemberTableLeaderPinnedTests(IsolatedAsyncioTestCase):
    """2+3. Textual Pilot 端到端 + 底层顺序不被改写。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(str(self.root / "teams_data.json"))
        data_layer.save_data({"teams": {"team": _team()}})

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    async def _mount_detail(self):
        """挂载 TeamDetailScreen，返回 (rows, keys) 快照。

        刻意用普通协程而非 async generator：断言写在 ``async for`` 体内时，
        异常会在生成器未关闭的情况下抛出，Textual 的 app context 退出会再抛
        一个 ``Token was created in a different Context``，把真正的断言失败
        埋在噪音里。快照式返回让失败信息干净可读。
        """
        from tui.tui_screens import TeamManagerApp, TeamDetailScreen

        app = TeamManagerApp()
        # 终端存活探测会真的调 tmux：打桩成"无会话"，界面走纯数据渲染。
        with mock.patch("tui.tui_screens._find_tmux_session", return_value=""), \
             mock.patch("tui.tui_screens._sync_team_terminal_state", return_value=False), \
             mock.patch("tui.tui_screens.get_member_terminal_status", return_value={}):
            async with app.run_test(size=(120, 30)) as pilot:
                await app.push_screen(TeamDetailScreen("team"))
                await pilot.pause()
                dt = app.screen.query_one("#member_table", DataTable)
                rows = [
                    [str(c) for c in dt.get_row_at(i)] for i in range(dt.row_count)
                ]
                keys = [str(k.value) for k in dt.rows]
        return rows, keys

    async def test_leader_renders_on_first_row(self):
        rows, keys = await self._mount_detail()
        self.assertTrue(rows, "成员表不应为空")
        self.assertEqual(rows[0][0], "lead", "第 1 行必须是 leader")
        self.assertEqual(keys, ["lead", "reviewer", "coder", "tester", "refactor"])

    async def test_crown_marker_is_on_first_row_only(self):
        rows, _ = await self._mount_detail()
        crowned = [i for i, r in enumerate(rows) if "👑" in r[3]]
        self.assertEqual(crowned, [0], "👑 只应出现在第 1 行")

    async def test_row_keys_are_member_names_not_indexes(self):
        """选中/编辑/移除都按 row_key 取成员名，重排行序不得改变这一点。"""
        _, keys = await self._mount_detail()
        self.assertEqual(set(keys), {"lead", "reviewer", "coder", "tester", "refactor"})

    async def test_underlying_member_order_untouched(self):
        """安全网：界面刷新绝不能改写 teams_data.json 里的成员插入顺序。"""
        before = list(data_layer.load_data()["teams"]["team"]["members"])
        await self._mount_detail()
        after = list(data_layer.load_data()["teams"]["team"]["members"])
        self.assertEqual(before, after)
        self.assertEqual(after[-1], "lead", "底层顺序保持 leader 在最后")


if __name__ == "__main__":
    unittest.main()
