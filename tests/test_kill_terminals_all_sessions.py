"""按 K 一次关干净：团队名下**全部** tmux session 都要被杀。

背景（用户实测 P1）：TUI 按 K 关闭所有终端后，成员终端会自动回来一次，要按两次
才能真正关掉。用户贴的日志：

    [exited]
    [tmux_spawn] 已从团队终端脱离或 attach 返回(0)，2 秒后重新进入。按 Ctrl+C 停止自动重连。
    [exited]
    [tmux_spawn] 命令已结束，退出码: 0。按 Ctrl+D 关闭此窗格

两条独立成因（缺一不可，都在本文件与 test_tmux_exact_session_target.py 里锁住）：

  1. **一个团队会同时拥有多个 session**：MCP server 建 ``mcp_{team}``
     （``_ensure_team_session`` 在 session 意外死亡时还会重建），TUI 建
     ``mcp_{team}_{HHMMSS}``。旧 ``kill_terminals`` 只杀 ``find_tmux_session()``
     挑中的那**一个**，另一个还活着 → 重连循环把用户送回去 → 得按第二次。
  2. **杀在前、落盘在后**：旧实现先 kill-session 再写 terminals_active=False，
     中间窗口里 MCP 巡检看到 terminals_active 仍为 True 而 session 不见了，
     会走 ``_ensure_team_session`` 把 session（连同空壳 __base）重建出来，
     成员跟着复活。必须**先**落盘关掉复活窗口再动手。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
"""
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common import tmux_utils
from tui import tui_screens as ts


class KillTerminalsAllSessionsTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.root / "teams_data.json")
        data_layer.save_data({"teams": {"team": {
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "claude",
            "terminals_active": True,
            "members": {"lead": {"role": "leader"}, "alice": {"role": "coder"}},
        }}})
        # 现场同款：MCP 侧短名 + TUI 侧带时间戳名并存
        self.sessions = ["mcp_team", "mcp_team_215956"]
        self.events: list[str] = []
        self.calls: list[list[str]] = []
        self.kill_rc = 0

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    def _fake_tmux_run(self, cmd, timeout=10):
        self.calls.append(list(cmd))
        if cmd[0] == "list-sessions":
            return 0, "\n".join(self.sessions), ""
        if cmd[0] == "kill-session":
            self.events.append("kill")
            if self.kill_rc == 0:
                target = cmd[cmd.index("-t") + 1].lstrip("=")
                if target in self.sessions:
                    self.sessions.remove(target)
            return self.kill_rc, "", ""
        if cmd[0] == "list-windows":
            return 1, "", ""
        return 0, "", ""

    def _kill(self):
        original_save = data_layer.save_data

        def tracking_save(data):
            team = data.get("teams", {}).get("team", {})
            if team.get("terminals_active") is False:
                self.events.append("save-inactive")
            return original_save(data)

        # tmux_utils.tmux_run 也要打桩：find_all_tmux_sessions 住在 common 里，
        # 只打 ts._tmux_run（同一函数的别名）拦不住它，会打到真实 tmux。
        with mock.patch.object(ts, "_tmux_run", side_effect=self._fake_tmux_run), \
             mock.patch.object(tmux_utils, "tmux_run", side_effect=self._fake_tmux_run), \
             mock.patch.object(ts, "save_data", side_effect=tracking_save), \
             mock.patch.object(ts, "_leader_terminal_restart_blocked", return_value=False):
            return ts.kill_terminals("team")

    def _kill_targets(self):
        return [c[c.index("-t") + 1] for c in self.calls if c[0] == "kill-session"]

    # ------------------------------------------------------------------

    def test_kills_every_team_session_in_one_call(self):
        ok, msg = self._kill()
        self.assertTrue(ok, msg)
        self.assertEqual(
            sorted(t.lstrip("=") for t in self._kill_targets()),
            ["mcp_team", "mcp_team_215956"],
            "团队名下全部 session 都要在一次调用里杀掉，不能留兄弟给第二次 K",
        )
        self.assertEqual(self.sessions, [], "杀完不应有 session 残留")

    def test_kill_targets_are_exact(self):
        """裸名 kill-session 会前缀误伤同前缀的邻居 session。"""
        self._kill()
        for target in self._kill_targets():
            self.assertTrue(target.startswith("="),
                            f"kill-session 必须用精确目标，实际: {target}")

    def test_terminals_active_written_false_before_any_kill(self):
        """顺序硬约束：先关掉巡检的复活窗口，再动手杀。"""
        self._kill()
        self.assertIn("save-inactive", self.events, "必须写 terminals_active=False")
        self.assertIn("kill", self.events)
        self.assertLess(
            self.events.index("save-inactive"), self.events.index("kill"),
            "落盘必须在 kill 之前，否则 MCP 巡检会在中间窗口重建 session",
        )
        self.assertIs(
            data_layer.load_data()["teams"]["team"]["terminals_active"], False
        )

    def test_single_session_message_unchanged(self):
        self.sessions = ["mcp_team"]
        ok, msg = self._kill()
        self.assertTrue(ok)
        self.assertEqual(msg, "终端已关闭")

    def test_multi_session_message_names_them(self):
        ok, msg = self._kill()
        self.assertTrue(ok)
        self.assertIn("mcp_team_215956", msg, "杀了几个要如实说明")

    def test_no_session_reports_not_running(self):
        self.sessions = []
        ok, msg = self._kill()
        self.assertFalse(ok)
        self.assertIn("未找到运行中的终端", msg)

    def test_failed_kill_is_reported_not_swallowed(self):
        """没关干净就如实报告残留，不谎报成功。"""
        self.kill_rc = 1
        ok, msg = self._kill()
        self.assertFalse(ok, "kill 失败不得返回成功")
        self.assertIn("仍有 2 个 session 存活", msg)
        self.assertIn("mcp_team_215956", msg)

    def test_task_in_progress_still_blocks(self):
        with mock.patch.object(ts, "_tmux_run", side_effect=self._fake_tmux_run), \
             mock.patch.object(tmux_utils, "tmux_run", side_effect=self._fake_tmux_run), \
             mock.patch.object(ts, "_leader_terminal_restart_blocked", return_value=True):
            ok, msg = ts.kill_terminals("team")
        self.assertFalse(ok)
        self.assertIn("任务进行中", msg)
        self.assertEqual(self._kill_targets(), [], "被拦时不得杀任何 session")


if __name__ == "__main__":
    unittest.main()
