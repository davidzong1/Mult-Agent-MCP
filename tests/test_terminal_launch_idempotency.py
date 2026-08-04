"""
成员终端创建幂等性 (member terminal launch idempotency) 测试。
=============================================================

task3 修复点：leader_launch_member_terminal / _tmux_spawn_member 在目标成员窗口
已存在时必须幂等——不得重复创建 tmux new-window，否则同一成员会出现多个窗口，
导致 send_keys 目标歧义、状态跟踪错乱。

本文件按"行为契约"设计，不绑定修复落在哪一层（leader_launch_member_terminal
提前短路 或 _tmux_spawn_member 内部跳过）。契约是：

    **给定一个已存活的目标成员窗口，再次启动该成员终端不得产生新的 new-window。**

该幂等契约已随 task3 修复落地（_member_window_state 三态判定 + TERMINAL_SPAWN_LOCK
互斥 + 入口短路），下方契约用例均应为绿。

同时固化两个当前已正确的行为作为回归护栏：
  - 无现存窗口时正常启动（happy path，必须保持可启动）
  - 成员记录层已幂等（leader_add_member 重复成员名直接报"已存在"）

覆盖生产路径：
  - mult_agent_mcp.leader_launch_member_terminal
  - mult_agent_mcp._tmux_spawn_member
  - mult_agent_mcp.leader_add_member
"""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer


class TerminalLaunchIdempotencyTests(unittest.TestCase):
    """成员终端启动幂等 — 通过临时目录隔离模块全局状态。"""

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
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        self.old_funcs = {
            "_find_any_session": mcp._find_any_session,
            "_tmux": mcp._tmux,
        }
        self.old_data_file_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        data_file = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.DATA_FILE = data_file
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        data_layer.set_data_file(data_file)

        self.workspace = project / "team_ws"
        self.workspace.mkdir(parents=True, exist_ok=True)
        self._seed_team()

    def _seed_team(self) -> None:
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(self.workspace),
                    "context_dir": str(self.root / "context"),
                    "terminals_active": True,
                    "default_agent": "claude",
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_funcs.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_file_override
        self.tmp.cleanup()

    # ============================================================
    # 契约：窗口已存在 → 不得重复创建（预期失败，修复未落地）
    # ============================================================

    def test_leader_launch_member_terminal_no_duplicate_when_window_exists(self):
        """幂等契约：成员窗口已存在时，leader_launch_member_terminal
        不得再次 spawn 新窗口。

        契约断言点：调用方 session 中已存在 alice 窗口（三态判定为 live）时
        _tmux_spawn_member 的调用次数必须为 0。
        """
        spawn_calls = []

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@2\talice", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                with mock.patch.object(mcp, "_tmux_spawn_member",
                                       side_effect=lambda s, n, a, d: spawn_calls.append((s, n, a, d)) or (0, "", "")):
                    with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                        with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                            with mock.patch.object(mcp, "_build_recovery_context", return_value="ctx"):
                                with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                                    with mock.patch("time.sleep"):
                                        result = mcp.leader_launch_member_terminal("team", "alice")

        self.assertEqual(
            spawn_calls, [],
            "幂等契约被破坏：成员窗口已存在时仍调用了 _tmux_spawn_member，"
            "会创建重复窗口。期望 leader_launch_member_terminal 检测到现存窗口后短路。"
            f"实际 spawn_calls={spawn_calls}",
        )

    def test_tmux_spawn_member_no_new_window_when_target_exists(self):
        """幂等契约（spawn 层）：_tmux_spawn_member 发现成员窗口已存在时不得发出
        new-window/new-session。

        契约断言点：调用方 session 中已存在 alice 窗口（三态判定为 live）时，
        记录到的 tmux 命令中不得出现 new-window / new-session。
        """
        tmux_calls: list[list[str]] = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\tlead\n$1\t1000\t@2\talice", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_write_claude_permissions", return_value=None):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))

        new_window_cmds = [
            c for c in tmux_calls
            if c and c[0] in ("new-window", "new-session")
        ]
        self.assertEqual(
            new_window_cmds, [],
            "幂等契约被破坏：_tmux_spawn_member 在窗口已存在时仍发出 new-window。"
            f"实际 tmux 命令={tmux_calls}",
        )

    # ============================================================
    # 回归护栏：无现存窗口 → 正常启动（当前正确，必须保持）
    # ============================================================

    def test_leader_launch_member_terminal_spawns_once_when_no_window(self):
        """无现存窗口 → 恰好 spawn 一次并发送恢复上下文（happy path 护栏）。"""
        spawn_calls = []
        send_calls = []

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                with mock.patch.object(mcp, "_tmux_spawn_member",
                                       side_effect=lambda s, n, a, d: spawn_calls.append((s, n, a, d)) or (0, "", "")):
                    with mock.patch.object(mcp, "_write_claude_mcp", return_value="x"):
                        with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")):
                            with mock.patch.object(mcp, "_build_recovery_context", return_value="ctx"):
                                with mock.patch.object(mcp, "_send_keys",
                                                       side_effect=lambda s, w, t: send_calls.append(w) or (0, "")):
                                    with mock.patch("time.sleep"):
                                        result = mcp.leader_launch_member_terminal("team", "alice")

        self.assertEqual(len(spawn_calls), 1, "无现存窗口时应当正常启动一次")
        self.assertIn("已启动", result)
        self.assertGreaterEqual(len(send_calls), 1, "启动后应发送恢复上下文")

    def test_tmux_spawn_member_creates_window_when_no_target(self):
        """无现存窗口 → _tmux_spawn_member 正常发出 new-window（happy path 护栏）。"""
        tmux_calls: list[list[str]] = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_write_claude_permissions", return_value=None):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))

        self.assertEqual(rc, 0, f"启动应成功: {err}")
        self.assertTrue(
            any(c and c[0] == "new-window" for c in tmux_calls),
            "无现存窗口时应当发出 new-window",
        )

    # ============================================================
    # 回归护栏：成员记录层幂等（当前正确）
    # ============================================================

    def test_leader_add_member_duplicate_name_rejected(self):
        """成员记录幂等：同名成员重复添加必须被拒绝，且不得触发终端 spawn。"""
        spawn_calls = []

        with mock.patch.object(mcp, "_tmux_spawn_member",
                               side_effect=lambda s, n, a, d: spawn_calls.append(n) or (0, "", "")):
            result = mcp.leader_add_member("team", "alice", "coder")

        self.assertIn("已存在", result)
        self.assertEqual(spawn_calls, [], "重复添加不得触发任何终端创建")


if __name__ == "__main__":
    unittest.main()
