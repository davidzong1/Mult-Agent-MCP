"""`__base` 脚手架窗口用完即撤。

背景（用户实测 P0）：TUI 按 T 启动后 leader 窗口不见了，落到一个没有任何 Agent 的
`__base` 窗口 —— 对着一个 bash 提示符，看起来像"agent 没起来"。

`__base` 本身只是个**空壳脚手架**：tmux 必须先有 session 才能 new-window，所以
两处会先建一个不跑任何 CLI 的 `__base` 窗，再把真实窗口接进来：
  1. ``tui/tui_screens.py::launch_terminals`` 的 direct 分支；
  2. ``mult_agent_mcp.py::_ensure_team_session``（session 意外死亡后的中断重建，
     被成员恢复 ``_scan_member_terminal`` 与 leader 复活 ``_revive_leader_terminal_locked`` 调用）。
两处都**只创建、从不回收**，于是空壳常驻为窗口 0。产地 2 正好压在换号链路上，
对应用户要的"额度不足成员切换用户完成后自动关闭 `__base` 终端"。

本文件覆盖：
  1. 纯函数语义：有兄弟窗才删；**只剩 `__base` 时必须拒删**（tmux 杀掉最后一个
     窗口会连 session 一起带走，回收绝不能反过来干掉刚建好的 session）；幂等；
     按 window_id 定位（不受同名窗口/前缀匹配影响）；tmux 报错不抛异常；
  2. 三处接线各一条：TUI 启动 / `_recover_and_send` / `_revive_leader_terminal_locked`。

数据隔离：data_layer.set_data_file 指向临时文件，绝不触碰真实 teams_data.json。
"""
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common.tmux_utils import BASE_WINDOW_NAME, drop_base_window
from tui import tui_screens as ts


def _records(*names: str) -> str:
    """构造 list-windows 输出：#{session_id}\t#{session_created}\t#{window_id}\t#{name}"""
    return "\n".join(
        f"$1\t1000\t@{i + 1}\t{name}" for i, name in enumerate(names)
    )


class _Runner:
    """记录 argv 的假 tmux runner；list-windows 返回指定窗口集合。"""

    def __init__(self, windows: tuple[str, ...], *, list_rc: int = 0, kill_rc: int = 0):
        self.windows = list(windows)
        self.list_rc = list_rc
        self.kill_rc = kill_rc
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=10):
        self.calls.append(list(cmd))
        if cmd[0] == "list-windows":
            return self.list_rc, _records(*self.windows), ""
        if cmd[0] == "kill-window":
            if self.kill_rc == 0:
                target = cmd[cmd.index("-t") + 1]
                idx = int(target.lstrip("@")) - 1
                if 0 <= idx < len(self.windows):
                    self.windows[idx] = None  # 标记已删，保持索引与 @id 对应
            return self.kill_rc, "", ""
        return 0, "", ""

    def kill_targets(self) -> list[str]:
        return [c[c.index("-t") + 1] for c in self.calls if c[0] == "kill-window"]


class DropBaseWindowSemanticsTests(unittest.TestCase):
    """1. 纯函数语义。"""

    def test_drops_base_when_siblings_exist(self):
        run = _Runner(("__base", "lead", "alice"))
        self.assertTrue(drop_base_window("sess", run))
        self.assertEqual(run.kill_targets(), ["@1"], "应按 window_id 精确删掉 __base")

    def test_refuses_when_base_is_the_only_window(self):
        """只剩 __base 时必须拒删 —— 杀掉最后一个窗口会连 session 一起带走。"""
        run = _Runner(("__base",))
        self.assertFalse(drop_base_window("sess", run))
        self.assertEqual(run.kill_targets(), [], "绝不能删掉 session 的最后一个窗口")

    def test_noop_without_base_window(self):
        run = _Runner(("lead", "alice"))
        self.assertFalse(drop_base_window("sess", run))
        self.assertEqual(run.kill_targets(), [])

    def test_idempotent_second_call_is_noop(self):
        run = _Runner(("__base", "lead"))
        self.assertTrue(drop_base_window("sess", run))
        run.windows = ["lead"]
        self.assertFalse(drop_base_window("sess", run), "重复调用应为 no-op")
        self.assertEqual(len(run.kill_targets()), 1)

    def test_targets_window_id_not_name(self):
        """按 @id 定位：窗口名会撞前缀/重名，window_id 全局唯一。"""
        run = _Runner(("lead", "__base"))
        self.assertTrue(drop_base_window("sess", run))
        self.assertEqual(run.kill_targets(), ["@2"])

    def test_list_windows_failure_is_safe(self):
        run = _Runner(("__base", "lead"), list_rc=1)
        self.assertFalse(drop_base_window("sess", run))
        self.assertEqual(run.kill_targets(), [])

    def test_kill_failure_reported_not_raised(self):
        run = _Runner(("__base", "lead"), kill_rc=1)
        self.assertFalse(drop_base_window("sess", run), "kill 失败应返回 False，不抛异常")

    def test_runner_exception_does_not_propagate(self):
        """best-effort：绝不打断调用方的 spawn / 恢复 / 换号主流程。"""
        def boom(cmd, timeout=10):
            raise OSError("tmux gone")

        self.assertFalse(drop_base_window("sess", boom))

    def test_base_window_name_constant(self):
        self.assertEqual(BASE_WINDOW_NAME, "__base")


class _TeamFixture(unittest.TestCase):
    """共享的隔离数据夹具。

    ``mcp.DATA_FILE`` 是模块级全局，改了必须在 tearDown 还原 —— 不还原会把
    后续所有测试的 MCP 侧数据路径钉死在一个已删除的临时目录上（本文件按字母序
    很靠前，污染面几乎是整个测试会话）。data_layer 的 override 同理。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self._old_mcp_data_file = mcp.DATA_FILE
        data_layer.set_data_file(self.root / "teams_data.json")
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.ctx = self.root / "ctx"
        self.ctx.mkdir()
        self.session = f"mcp_team_{uuid.uuid4().hex[:6]}"

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        mcp.DATA_FILE = self._old_mcp_data_file
        self.tmp.cleanup()


class TuiLaunchDropsBaseTests(_TeamFixture):
    """2a. TUI direct 分支启动后必须撤掉自己建的 __base。"""

    def setUp(self):
        super().setUp()
        self.calls: list[list[str]] = []
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": "外部会话",          # 不在 members 里 = 真 direct，不会被提升
            "leader_type": "direct",
            "default_agent": "claude",
            "members": {"alice": {"role": "coder", "agent": "claude"}},
        }}})

    def _fake_tmux_run(self, cmd, timeout=10):
        self.calls.append(list(cmd))
        if cmd[0] == "-V":
            return 0, "tmux 3.2", ""
        if cmd[0] == "has-session":
            return 1, "", ""
        if cmd[0] == "list-windows":
            return 0, _records("__base", "alice"), ""
        return 0, "", ""

    def test_direct_launch_removes_base_window(self):
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
            mock.patch.object(ts.time, "sleep", return_value=None),
        ]
        for p in patches:
            p.start()
        try:
            ok, msg = ts.launch_terminals("team")
        finally:
            for p in patches:
                p.stop()

        self.assertTrue(ok, msg)
        base_created = any(
            c[0] == "new-session" and "-n" in c and c[c.index("-n") + 1] == "__base"
            for c in self.calls
        )
        self.assertTrue(base_created, "direct 分支应先建 __base 承载成员窗")
        self.assertIn(
            ["kill-window", "-t", "@1"], self.calls,
            "成员窗接进来之后必须撤掉 __base 空壳",
        )


class McpRecoverDropsBaseTests(_TeamFixture):
    """2b. 换号 / 恢复重建后必须撤掉 _ensure_team_session 留下的 __base。"""

    def setUp(self):
        super().setUp()
        mcp.DATA_FILE = str(self.root / "teams_data.json")
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "claude",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "t", "last_task_completed": False},
            },
        }}})
        self.tmux_calls: list[list[str]] = []

    def _fake_tmux(self, cmd, timeout=10):
        self.tmux_calls.append(list(cmd))
        if cmd[0] == "list-windows":
            return 0, _records("__base", "alice"), ""
        return 0, "", ""

    def test_recover_and_send_drops_base(self):
        patches = [
            mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux),
            mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")),
            mock.patch.object(mcp, "_write_claude_mcp", return_value=None),
            mock.patch.object(mcp, "_ensure_codex_mcp", return_value=None),
            mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None),
            mock.patch.object(mcp, "_build_recovery_context", return_value="ctx"),
            mock.patch.object(mcp, "_send_keys", return_value=(0, "")),
            mock.patch.object(mcp, "_record_recovery_event", return_value=None),
            mock.patch.object(mcp, "_member_window_target", return_value="alice"),
            mock.patch.object(mcp.time, "sleep", return_value=None),
        ]
        for p in patches:
            p.start()
        try:
            ok, msg = mcp._recover_and_send("team", "alice", self.session,
                                            reason="quota_switch")
        finally:
            for p in patches:
                p.stop()

        self.assertTrue(ok, msg)
        self.assertIn(
            ["kill-window", "-t", "@1"], self.tmux_calls,
            "换号重建成员窗之后必须撤掉 __base（用户明确要求换号完成即关闭）",
        )


class McpReviveLeaderDropsBaseTests(_TeamFixture):
    """2c. leader 复活重建后必须撤掉 __base。"""

    def setUp(self):
        super().setUp()
        mcp.DATA_FILE = str(self.root / "teams_data.json")
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "claude",
            "members": {"lead": {"role": "leader", "agent": "claude"}},
        }}})
        self.tmux_calls: list[list[str]] = []

    def _fake_tmux(self, cmd, timeout=10):
        self.tmux_calls.append(list(cmd))
        if cmd[0] == "list-windows":
            return 0, _records("__base", "lead"), ""
        return 0, "", ""

    def test_revive_leader_drops_base(self):
        patches = [
            mock.patch.object(mcp, "_tmux", side_effect=self._fake_tmux),
            mock.patch.object(mcp, "_tmux_spawn_member", return_value=(0, "", "")),
            mock.patch.object(mcp, "_write_claude_mcp", return_value=None),
            mock.patch.object(mcp, "_ensure_codex_mcp", return_value=None),
            mock.patch.object(mcp, "_find_any_session", return_value=self.session),
            mock.patch.object(mcp, "_leader_window_is_dead", return_value=True),
            mock.patch.object(mcp, "_leader_revival_allowed", return_value=True),
            mock.patch.object(mcp, "_member_window_target", return_value=None),
            mock.patch.object(mcp, "_leader_system_prompt", return_value="p"),
            mock.patch.object(mcp, "_inject_claude_leader_prompt", return_value=(0, "")),
            mock.patch.object(mcp.time, "sleep", return_value=None),
        ]
        for p in patches:
            p.start()
        try:
            ok, msg = mcp._revive_leader_terminal_locked("team", reason="patrol")
        finally:
            for p in patches:
                p.stop()

        self.assertTrue(ok, msg)
        self.assertIn(
            ["kill-window", "-t", "@1"], self.tmux_calls,
            "leader 复活建窗之后必须撤掉 __base",
        )


if __name__ == "__main__":
    unittest.main()
