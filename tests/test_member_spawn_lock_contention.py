"""
成员终端 spawn 跨进程锁（member_spawn_lock）争用测试。
=========================================================

task3 修复点：MCP / TUI / monitor 多进程并发为同一成员创建终端时，必须经
fcntl.flock 跨进程互斥，保证"检查窗口存在 + 创建窗口(new-window/new-session)"
临界区只被一个进程进入 —— **只允许一次 check+spawn**，避免重复创建窗口。

覆盖：
  1. 锁原语跨 fd 互斥：同进程两个线程各持独立 fd 争用同一 team/member 锁，
     临界区必须串行（无丢更新）。
  2. 锁原语跨进程互斥：两个独立子进程争用同一把锁，check→spawn 只发生一次。
  3. MCP 路径：_tmux_spawn_member 的 check+spawn 必须整体包裹在
     member_spawn_lock 临界区内（真实断言，当前为绿）。
  4. new_session 分支可见：session 已存在 → 复用不创建；缺失 → 正常创建。
  5. 自动补角色失败可见：_select_task_members 自动创建缺失角色成员时，
     其终端 spawn 失败必须进入 spawn_failures（不静默吞掉）。
  6. TUI 路径：tui_screens.launch_terminals 经 _member_spawn_lock 包裹
     check+spawn（与 MCP 同一把跨进程共享锁），并发创建同一成员窗口时
     只允许一次 spawn。

所有锁文件都落在临时目录（团队 context_dir 指向 tmp），不污染真实 home。
"""

import contextlib
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common.tmux_utils import member_spawn_lock

import mult_agent_mcp as mcp
import tui.tui_screens as tui_screens

REPO_ROOT = Path(__file__).resolve().parent.parent


class _LockFixture(unittest.TestCase):
    """让 member_spawn_lock 的锁文件解析到临时目录（context_dir → tmp）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.lock_dir = self.root / "contexts" / "team"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        data_layer.save_data({
            "teams": {
                "team": {
                    "context_dir": str(self.lock_dir),
                    "workspace_dir": str(self.root / "ws"),
                    "members": {},
                },
            },
        })

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()


# ============================================================
# 1) 锁原语跨 fd 互斥（同进程两个线程，各自独立 fd）
# ============================================================

class CrossFdLockMutualExclusionTests(_LockFixture):
    """member_spawn_lock 每次进入都会 open 一个新的 fd；两个线程即两个不同 fd。
    若 flock 未互斥，"读计数→sleep→写计数" 会丢更新。"""

    def test_two_fds_serialize_critical_section(self):
        counter = self.root / "counter.txt"
        counter.write_text("0")
        results: list[int] = []

        def worker() -> None:
            # 每次进入锁都打开独立 fd
            with member_spawn_lock("team", "alice"):
                n = int(counter.read_text())
                time.sleep(0.2)  # 放大竞态窗口：若未互斥，两线程都会读到 0
                counter.write_text(str(n + 1))
                results.append(n)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(counter.read_text(), "2",
                         "两把锁（不同 fd）未互斥 → 临界区并行 → 丢更新")
        self.assertEqual(sorted(results), [0, 1])

    def test_lock_file_mode_is_0600(self):
        """锁文件创建为 0600（凭证/敏感信息同源目录约束）。"""
        with member_spawn_lock("team", "alice"):
            pass
        lock_file = self.root / "contexts" / "team" / ".member_spawn_locks" / "alice.lock"
        self.assertTrue(lock_file.exists())
        mode = lock_file.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


# ============================================================
# 2) 锁原语跨进程互斥（两个独立子进程，只允许一次 check+spawn）
# ============================================================

_CROSS_PROC_WORKER = r"""
import sys
import time
from pathlib import Path

sys.path.insert(0, sys.argv[1])
from common import data_layer
from common.tmux_utils import member_spawn_lock

data_file, lock_dir, marker_path = sys.argv[2], sys.argv[3], sys.argv[4]
data_layer.set_data_file(data_file)
# 数据文件已由测试进程写入（含 context_dir → lock_dir），这里只读不写，
# 避免并发 save_data 的原子写冲突检测触发。
marker = Path(marker_path)

with member_spawn_lock("team", "alice"):
    if marker.exists():
        print("REUSED")   # check：窗口已存在 → 复用
    else:
        marker.write_text("spawned")  # spawn：创建窗口（模拟）
        time.sleep(0.3)   # 持有锁，让另一进程阻塞在 flock 上
        print("SPAWNED")
"""


class CrossProcessLockContentionTests(_LockFixture):
    """两个独立进程争用同一 team/member 锁：check+spawn 只允许一次。"""

    def test_two_processes_only_one_check_and_spawn(self):
        marker = self.root / "marker.txt"
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", _CROSS_PROC_WORKER,
                 str(REPO_ROOT), str(self.data_file), str(self.lock_dir), str(marker)],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            )
            for _ in range(2)
        ]
        outs = []
        for p in procs:
            out, err = p.communicate(timeout=60)
            self.assertEqual(p.returncode, 0, f"worker 失败: {err}")
            outs.append(out)

        outcomes = " ".join(outs).split()
        self.assertEqual(outcomes.count("SPAWNED"), 1,
                         f"应恰好一次 spawn（窗口创建），实际 {outcomes}")
        self.assertEqual(outcomes.count("REUSED"), 1,
                         f"应恰好一次复用（窗口已存在），实际 {outcomes}")


# ============================================================
# 3) MCP 路径：check+spawn 整体包裹在共享锁内（真实断言）
# ============================================================

class _McpFixture(unittest.TestCase):
    """隔离 mult_agent_mcp 模块全局状态 + 团队 context_dir 指向临时目录。"""

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
                    "context_dir": str(self.root / "ctx" / "team"),
                    "terminals_active": True,
                    "default_agent": "claude",
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                    },
                },
            },
        })

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()


class McpSpawnSharedLockTests(_McpFixture):
    """MCP _tmux_spawn_member 的 check+spawn 必须在共享锁临界区内原子执行。"""

    def test_check_and_spawn_run_inside_lock_critical_section(self):
        """事件序：lock+ → 状态检查 → spawn → lock-；锁参数为 (team, member)。"""
        events: list = []
        real_lock = mcp.member_spawn_lock

        @contextlib.contextmanager
        def recorded_lock(team, member):
            events.append(("lock+", team, member))
            try:
                with real_lock(team, member):
                    yield
            finally:
                events.append(("lock-", team, member))

        with mock.patch.object(mcp, "_member_window_state",
                               side_effect=lambda *a, **k: events.append("state") or ("absent", "")):
            with mock.patch.object(mcp, "_tmux",
                                   side_effect=lambda *a, **k: events.append("spawn") or (0, "", "")):
                with mock.patch.object(mcp, "_write_claude_permissions", return_value=None):
                    with mock.patch.object(mcp, "member_spawn_lock", new=recorded_lock):
                        rc, _, err = mcp._tmux_spawn_member(
                            "mcp_team", "alice", "claude", str(self.workspace))

        self.assertEqual(rc, 0, f"spawn 应成功: {err}")
        self.assertEqual(events[0], ("lock+", "team", "alice"),
                         "临界区必须以锁获取开始，锁参数 (team, alice)")
        self.assertEqual(events[-1], ("lock-", "team", "alice"),
                         "临界区必须以锁释放结束")
        self.assertLess(events.index(("lock+", "team", "alice")), events.index("state"))
        self.assertLess(events.index("state"), events.index("spawn"))
        self.assertLess(events.index("spawn"), events.index(("lock-", "team", "alice")))

    def test_new_session_reuses_when_session_exists(self):
        """new_session 分支：session 已存在（live）→ 复用，不发 new-session。"""
        tmux_calls: list[list[str]] = []

        def fake_tmux(cmd, *a, **k):
            tmux_calls.append(list(cmd))
            return 0, "", ""

        with mock.patch.object(mcp, "_member_window_state", return_value=("live", "mcp_team")):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                with mock.patch.object(mcp, "_write_claude_permissions", return_value=None):
                    rc, _, err = mcp._tmux_spawn_member(
                        "mcp_team", "alice", "claude", str(self.workspace), new_session=True)

        self.assertEqual(rc, 0)
        self.assertEqual(err, "session already exists")
        self.assertFalse(
            any(c and c[0] in ("new-session", "new-window") for c in tmux_calls),
            f"session 已存在时不应发 new-session/new-window: {tmux_calls}",
        )

    def test_new_session_spawns_when_absent(self):
        """new_session 分支：session 缺失 → 正常发 new-session。"""
        tmux_calls: list[list[str]] = []

        def fake_tmux(cmd, *a, **k):
            tmux_calls.append(list(cmd))
            return 0, "", ""

        with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
            with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
                with mock.patch.object(mcp, "_write_claude_permissions", return_value=None):
                    rc, _, err = mcp._tmux_spawn_member(
                        "mcp_team", "alice", "claude", str(self.workspace), new_session=True)

        self.assertEqual(rc, 0, f"spawn 应成功: {err}")
        self.assertTrue(
            any(c and c[0] == "new-session" for c in tmux_calls),
            f"session 缺失时应发 new-session: {tmux_calls}",
        )

    def test_auto_role_spawn_failure_is_visible(self):
        """自动补角色：leader 按角色自动创建缺失成员，其终端 spawn 失败时
        必须进入 spawn_failures（错误可见，不静默）。"""
        data = mcp._load()
        data["teams"]["team"]["terminals_active"] = True
        mcp._save(data)

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"):
            with mock.patch.object(mcp, "_tmux_spawn_member",
                                   return_value=(-1, "", "boom: spawn failed")):
                with mock.patch.object(mcp, "_write_claude_mcp", return_value=""):
                    with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "")):
                        with mock.patch("time.sleep"):
                            result = mcp._select_task_members(
                                "team", "实现一个功能", required_roles="coder")

        self.assertTrue(result.get("created"),
                        "缺失 coder 角色成员应被自动创建")
        self.assertTrue(result.get("spawn_failures"),
                        f"自动补角色成员 spawn 失败必须可见: {result}")
        self.assertIn("boom", result["spawn_failures"][0])


# ============================================================
# 6) TUI 路径：launch_terminals 应使用共享锁（当前未落地 → 预期失败）
# ============================================================

class TuiLaunchSharedLockTests(unittest.TestCase):
    """TUI launch_terminals 必须与 MCP 使用同一把跨进程共享锁。

    已落地：tui_screens.launch_terminals 在创建成员窗口前经
    _member_spawn_lock(team, member) 包裹 check+spawn 临界区，与 MCP
    _tmux_spawn_member 使用同一把 flock 锁 → 跨进程并发只允许一次 spawn。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = str(self.root / "workspace")
        Path(self.workspace).mkdir(parents=True)
        self.team_data = {
            "teams": {
                "team": {
                    "workspace_dir": self.workspace,
                    "context_dir": self.workspace,
                    "default_agent": "claude",
                    "leader": "lead",
                    "leader_type": "tmux",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "coder_a": {"role": "coder", "agent": "claude"},
                    },
                },
            },
        }

    def tearDown(self):
        self.tmp.cleanup()

    def _run_launch(self):
        tmux_calls = []

        def fake_tmux(cmd, timeout=10):
            tmux_calls.append(list(cmd))
            if cmd[0] == "-V":
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(tui_screens, "_tmux_run", side_effect=fake_tmux):
            with mock.patch.object(tui_screens, "load_data", return_value=self.team_data):
                with mock.patch.object(tui_screens, "save_data", return_value=None):
                    with mock.patch.object(tui_screens, "_tmux_session", return_value="mcp_team_test"):
                        with mock.patch.object(tui_screens, "_leader_terminal_restart_blocked", return_value=False):
                            with mock.patch.object(tui_screens, "_record_leader_reentry", return_value=None):
                                with mock.patch.object(tui_screens, "write_claude_mcp", return_value=""):
                                    with mock.patch.object(tui_screens, "configure_codex_mcp", return_value=(True, "")):
                                        with mock.patch.object(tui_screens, "configure_claude_mcp", return_value=(True, "")):
                                            with mock.patch.object(tui_screens, "write_claude_permissions", return_value=""):
                                                with mock.patch.object(tui_screens, "_remember_member_window_id", return_value=""):
                                                    with mock.patch.object(tui_screens, "_inject_claude_leader_prompt", return_value=(0, "")):
                                                        ok, msg = tui_screens.launch_terminals("team")
        return ok, msg, tmux_calls

    def test_launch_terminals_acquires_shared_lock(self):
        """TUI launch_terminals 必须获取跨进程共享锁（与 MCP 同一把）。

        patch 目标是 tui.tui_screens._member_spawn_lock（TUI 导入的共享锁
        别名），而非 common.tmux_utils.member_spawn_lock —— 证明 TUI 侧
        实际调用了共享锁，而不是被静态导入但未使用。
        """
        lock_calls: list[tuple[str, str]] = []
        real_lock = tui_screens._member_spawn_lock

        @contextlib.contextmanager
        def recorded_lock(team, member):
            lock_calls.append((team, member))
            with real_lock(team, member):
                yield

        with mock.patch.object(tui_screens, "_member_spawn_lock", new=recorded_lock):
            ok, msg, _ = self._run_launch()

        self.assertTrue(ok, f"launch 应成功: {msg}")
        self.assertTrue(
            lock_calls,
            "TUI launch_terminals 未调用跨进程共享锁 _member_spawn_lock；"
            "与 MCP 并发创建同一成员终端时存在重复创建窗口风险。",
        )
        # launch_terminals 对每个成员都应在创建窗口前获取锁
        members = [m for m in self.team_data["teams"]["team"]["members"] if m != "lead"]
        for name in members:
            self.assertIn(("team", name), lock_calls,
                          f"成员 {name} 创建窗口前应获取 (team, {name}) 共享锁")


if __name__ == "__main__":
    unittest.main()
