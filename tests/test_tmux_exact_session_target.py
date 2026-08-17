"""tmux 精确 session 目标（`=name`）—— 前缀匹配是 P1"关不干净"的第二条成因。

tmux 的 target-session 解析默认做**前缀匹配**。实测（tmux 3.x）：

    $ tmux new-session -d -s probe_x_120000
    $ tmux has-session -t probe_x    ; echo $?   →  rc=0   ← 前缀误命中！
    $ tmux has-session -t "=probe_x" ; echo $?   →  can't find session / rc=1

后果一（重连循环）：``_reattaching_tmux_attach_command`` 用裸名探活，第一次关闭
终端后 ``has-session`` 被兄弟 session 冒充成"还活着" → 循环不 break → 2 秒后
``attach`` 同样前缀命中，把用户送回另一个团队 session。用户看到的就是
"按 K 关掉后自动重开一次，要按两次"。

后果二（session 查找）：``find_tmux_session`` 旧实现用 ``has-session`` 的返回码
判定"精确名存在"，只有 ``mcp_{team}_HHMMSS`` 时会误判，把一个**根本不存在的短名**
当候选返回。MCP 侧同名函数 ``_find_any_session`` 早就改成核对 list-sessions 的
真实输出并写了注释，TUI 这份副本此前漏修 —— 两侧行为分叉。

本文件把这两条都钉死，并锁住 find_all_tmux_sessions 的枚举语义。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common import tmux_utils
from common.tmux_utils import exact_session_target, find_all_tmux_sessions, find_tmux_session
from tui import tui_screens as ts


class ExactSessionTargetTests(unittest.TestCase):

    def test_prefixes_with_equals(self):
        self.assertEqual(exact_session_target("mcp_team"), "=mcp_team")

    def test_reattach_loop_uses_exact_target(self):
        cmd = ts._reattaching_tmux_attach_command("/usr/bin/tmux", "mcp_team")
        self.assertIn("=mcp_team", cmd, "探活/attach 必须用精确目标")
        self.assertNotIn(
            "has-session -t mcp_team ", cmd,
            "裸名探活会被兄弟 session 冒充成活的，循环永远不 break",
        )

    def test_reattach_loop_still_breaks_and_reconnects(self):
        """精确化不得破坏原有循环结构（脱离后自动重进、session 没了就退出）。"""
        cmd = ts._reattaching_tmux_attach_command("/usr/bin/tmux", "mcp_team")
        self.assertIn("has-session", cmd)
        self.assertIn("attach", cmd)
        self.assertIn("break", cmd)
        self.assertIn("sleep 2", cmd)

    def test_session_name_is_shell_quoted(self):
        cmd = ts._reattaching_tmux_attach_command("/usr/bin/tmux", "mcp_te am")
        self.assertIn("'=mcp_te am'", cmd, "含空格的 session 名必须被引起来")


class FindSessionsExactnessTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.root / "teams_data.json")
        data_layer.save_data({"teams": {"team": {"members": {}}}})
        self.sessions: list[str] = []

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _fake_tmux_run(self, cmd, timeout=10):
        if cmd[0] == "list-sessions":
            return (0, "\n".join(self.sessions), "") if self.sessions else (1, "", "")
        if cmd[0] == "has-session":
            # 真实 tmux 的前缀匹配行为（正是本文件要防的坑）
            target = cmd[cmd.index("-t") + 1]
            if target.startswith("="):
                return (0 if target[1:] in self.sessions else 1), "", ""
            return (0 if any(s.startswith(target) for s in self.sessions) else 1), "", ""
        if cmd[0] == "list-windows":
            return 1, "", ""
        return 0, "", ""

    def _patched(self):
        return mock.patch.object(tmux_utils, "tmux_run", side_effect=self._fake_tmux_run)

    def test_find_all_enumerates_exact_and_timestamped(self):
        self.sessions = ["mcp_team", "mcp_team_215956", "mcp_other", "unrelated"]
        with self._patched():
            self.assertEqual(
                find_all_tmux_sessions("team"), ["mcp_team", "mcp_team_215956"]
            )

    def test_find_all_ignores_other_teams_with_shared_prefix(self):
        """`mcp_team2` 不属于团队 `team` —— 前缀枚举必须带下划线分隔。"""
        self.sessions = ["mcp_team2", "mcp_teamwork_101010"]
        with self._patched():
            self.assertEqual(find_all_tmux_sessions("team"), [])

    def test_find_all_empty_when_no_sessions(self):
        with self._patched():
            self.assertEqual(find_all_tmux_sessions("team"), [])

    def test_find_session_never_returns_phantom_short_name(self):
        """只有带时间戳 session 时，不得返回不存在的短名 mcp_team。"""
        self.sessions = ["mcp_team_215956"]
        with self._patched():
            self.assertEqual(find_tmux_session("team"), "mcp_team_215956")

    def test_find_session_returns_exact_when_it_really_exists(self):
        self.sessions = ["mcp_team"]
        with self._patched():
            self.assertEqual(find_tmux_session("team"), "mcp_team")

    def test_find_session_none_when_nothing_matches(self):
        self.sessions = ["mcp_other"]
        with self._patched():
            self.assertIsNone(find_tmux_session("team"))


if __name__ == "__main__":
    unittest.main()
