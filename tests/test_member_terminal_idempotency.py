"""task3(P1) 回归测试：leader 创建成员时重复创建多个成员终端。

根因：
  - _tmux_spawn_member 无条件执行 new-window，tmux 允许重名窗口，
    同一成员的第二次创建会产生第二个活终端进程（重试 / 并发 / 恢复路径）。
  - leader_add_member 的"检查存在→写入→保存"未在数据锁内原子执行，
    并发创建同名成员会双双通过检查并各自 spawn。

修复：
  - _tmux_spawn_member 增加幂等（_member_window_alive）＋互斥（TERMINAL_SPAWN_LOCK），
    检查与创建在同一锁内原子执行；窗口已存在时复用而非重复创建，
    真实创建失败仍原样返回（不吞错）。
  - add_member / leader_add_member / _select_task_members 的成员写入在 TEAM_DATA_LOCK 内原子化。

本文件仅新增，不影响其他成员的测试文件。
"""

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer


class FakeTmux:
    """有状态的假 tmux：与真实 tmux 一致，允许重名窗口。

    旧实现下连续两次 new-window -n bob 会产生两个 bob 窗口（复现 bug）；
    新实现的幂等 + 互斥应只产生一个。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.sessions = {}  # session -> {window_id: name}
        self.calls = []
        self._seq = 0

    def _next_window_id(self):
        self._seq += 1
        return f"@{self._seq}"

    def __call__(self, cmd, timeout=10):
        c = list(cmd)
        with self._lock:
            self.calls.append(c)
            op = c[0]
            if op == "has-session":
                return (0, "", "") if c[2] in self.sessions else (1, "", "no session")
            if op == "new-session":
                session = c[c.index("-s") + 1]
                name = c[c.index("-n") + 1]
                self.sessions[session] = {self._next_window_id(): name}
                return 0, "", ""
            if op == "new-window":
                session = c[c.index("-t") + 1]
                name = c[c.index("-n") + 1]
                if session not in self.sessions:
                    return 1, "", "no such session"
                # 真实 tmux 允许重名窗口 —— 不检查重复，忠实复现旧 bug
                self.sessions[session][self._next_window_id()] = name
                return 0, "", ""
            if op == "list-windows":
                records = self.sessions.get(c[2], {})
                out = "\n".join(f"$1\t1000\t{wid}\t{name}" for wid, name in records.items())
                return 0, out, ""
            if op == "list-sessions":
                return 0, "\n".join(name for name in self.sessions), ""
            if op == "kill-window":
                session, _, win = c[2].partition(":")
                self.sessions.get(session, {}).pop(win, None)
                return 0, "", ""
            if op == "kill-session":
                self.sessions.pop(c[2], None)
                return 0, "", ""
            return 0, "", ""

    def window_names(self, session):
        with self._lock:
            return list(self.sessions.get(session, {}).values())

    def new_window_calls(self, name):
        out = []
        for c in self.calls:
            if c[0] == "new-window" and c[c.index("-n") + 1] == name:
                out.append(c)
        return out


class MemberTerminalIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir()
        context.mkdir()

        self.old_globals = {
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "SHARE_WORKSPACE_DIR": mcp.SHARE_WORKSPACE_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
        }
        self.old_data_file_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        mcp.MCP_HOME = str(self.root / ".mult_agent_mcp")
        mcp.DATA_FILE = str(self.root / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)  # 让 common.data_layer 与 mcp 读同一份数据
        mcp.TEAM_WORKSPACES_DIR = str(self.root / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(self.root / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(self.root / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(self.root / ".claude.json")
        mcp._OLD_DATA_FILE = str(self.root / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(self.root / "share_context_space")

        self.workspace = workspace
        self.context = context
        self.tmux = FakeTmux()

        # 打桩：spawn 之外的环境副作用
        self.patchers = [
            mock.patch.object(mcp, "_tmux", side_effect=self.tmux),
            mock.patch.object(mcp, "_write_claude_permissions", return_value=""),
            mock.patch.object(mcp, "_write_claude_mcp", return_value=""),
            mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""),
            mock.patch.object(mcp, "_send_keys", return_value=(0, "")),
            mock.patch("time.sleep", return_value=None),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_file_override
        self.tmp.cleanup()

    # ---- helpers ----

    def _save_team(self, members: dict) -> None:
        base = {
            "role": "member",
            "agent": "claude",
            "model": "",
            "last_task": "",
            "last_context": "",
            "last_task_completed": True,
        }
        full = {name: {**base, **(info or {})} for name, info in members.items()}
        mcp._save({
            "teams": {
                "team": {
                    "description": "t",
                    "workspace_dir": str(self.workspace),
                    "context_dir": str(self.context),
                    "default_agent": "claude",
                    "leader_type": "tmux",
                    "terminals_active": True,
                    "members": full,
                }
            }
        })

    def _team_with_leader_and(self, member_name: str = "alice", **member_overrides):
        self._save_team({
            "lead": {"role": "leader"},
            member_name: {"role": "coder", **member_overrides},
        })

    # ---- 幂等：同一成员窗口已存在时不重复创建 ----

    def test_spawn_is_idempotent_when_window_already_alive(self):
        self._team_with_leader_and("alice")
        session = "mcp_team"
        # alice 窗口已存活
        self.tmux.sessions[session] = {"@1": "lead", "@2": "alice"}

        rc1, _, _ = mcp._tmux_spawn_member(session, "alice", "claude", str(self.workspace))
        rc2, _, msg2 = mcp._tmux_spawn_member(session, "alice", "claude", str(self.workspace))

        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        self.assertIn("window already exists", msg2)
        # 两次调用都没有发出 new-window（窗口已存在）
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)
        self.assertEqual(self.tmux.window_names(session).count("alice"), 1)

    def test_spawn_creates_once_then_second_call_reuses(self):
        self._team_with_leader_and("alice")
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead"}  # session 存活，alice 尚未创建

        rc1, _, _ = mcp._tmux_spawn_member(session, "alice", "claude", str(self.workspace))
        rc2, _, _ = mcp._tmux_spawn_member(session, "alice", "claude", str(self.workspace))

        self.assertEqual(rc1, 0)
        self.assertEqual(rc2, 0)
        # 只发出一次 new-window → 只有一个 alice 窗口
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 1)
        self.assertEqual(self.tmux.window_names(session).count("alice"), 1)

    def test_alive_matches_stored_window_id_even_if_renamed(self):
        # 窗口存活但被改名 → 按持久化 tmux_window_id 识别，不创建副本
        self._team_with_leader_and(
            "alice",
            tmux_window_id="@9",
            tmux_window_name="alice",
            tmux_session="mcp_team",
            tmux_session_id="$1",
            tmux_session_created="1000",
        )
        session = "mcp_team"
        self.tmux.sessions[session] = {"@9": "renamed-by-cli"}  # 存活但改名

        rc, _, _ = mcp._tmux_spawn_member(session, "alice", "claude", str(self.workspace))

        self.assertEqual(rc, 0)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)
        self.assertEqual(len(self.tmux.sessions[session]), 1)  # 未产生副本

    # ---- 不吞掉真实创建失败 ----

    def test_spawn_failure_not_swallowed(self):
        self._team_with_leader_and("alice")
        session = "mcp_team"
        # 会话不存在 → new-window 会失败，必须原样返回错误
        self.assertNotIn(session, self.tmux.sessions)

        rc, _, err = mcp._tmux_spawn_member(session, "alice", "claude", str(self.workspace))

        self.assertNotEqual(rc, 0)
        self.assertIn("no such session", err)

    def test_spawn_refuses_when_stored_window_but_query_empty(self):
        """无法确认（成员有持久化窗口记录但 list-windows 为空/失败）→
        不盲目 new-window，返回可见错误，避免瞬时失败时恰好重复创建。"""
        self._team_with_leader_and(
            "alice",
            tmux_window_id="@7",
            tmux_window_name="alice",
            tmux_session="mcp_team",
            tmux_session_id="$1",
            tmux_session_created="1000",
        )
        self.tmux.sessions["mcp_team"] = {}  # session 存活但 list-windows 为空

        rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))

        self.assertNotEqual(rc, 0)
        self.assertIn("无法确认", err)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)

    def test_spawn_refuses_fresh_member_when_query_empty(self):
        """保守规则：活 session 不可能 0 窗口；list-windows 为空即查询失败，
        即使全新成员无窗口记录也一律 unknown，不盲目 new-window。"""
        self._team_with_leader_and("alice")  # 无 tmux_window_id
        self.tmux.sessions["mcp_team"] = {}  # session 存活但 list-windows 为空

        rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))

        self.assertNotEqual(rc, 0)
        self.assertIn("无法确认", err)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)

    def test_spawn_lock_failure_fails_closed(self):
        """跨进程锁不可用（fail closed）→ 不无锁创建，返回可见错误。"""
        self._team_with_leader_and("alice")
        self.tmux.sessions["mcp_team"] = {"@1": "lead"}

        with mock.patch.object(
            mcp, "member_spawn_lock",
            side_effect=RuntimeError("fcntl 不可用"),
        ):
            rc, _, err = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))

        self.assertNotEqual(rc, 0)
        self.assertIn("跨进程成员 spawn 锁", err)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)

    # ---- 锁序回归：TERMINAL_SPAWN_LOCK 与 TEAM_DATA_LOCK 不得反向嵌套 ----

    def test_spawn_not_called_while_data_lock_held(self):
        """leader_add_member 的 spawn 阶段不得持有 TEAM_DATA_LOCK。

        锁序必须是 TERMINAL_SPAWN_LOCK → TEAM_DATA_LOCK；若反向（DATA → TERMINAL）
        与 _tmux_spawn_member 内部的 TERMINAL → DATA 形成环，会产生死锁。
        此用例在 _tmux_spawn_member 被调用时检测当前线程是否仍持有 DATA 锁。
        """
        self._team_with_leader_and("alice")
        self.tmux.sessions["mcp_team"] = {"@1": "lead"}

        held_during_spawn = []
        original_spawn = mcp._tmux_spawn_member

        def spy(*args, **kwargs):
            held_during_spawn.append(mcp.TEAM_DATA_LOCK._is_owned())
            return original_spawn(*args, **kwargs)

        with mock.patch.object(mcp, "_tmux_spawn_member", side_effect=spy):
            mcp.leader_add_member("team", "bob", "coder")

        self.assertTrue(held_during_spawn, "spawn 应至少被调用一次")
        self.assertFalse(
            any(held_during_spawn),
            "spawn 阶段不得持有 TEAM_DATA_LOCK（否则与 TERMINAL_SPAWN_LOCK 可能反向死锁）",
        )

    def test_spawn_terminates_while_acquiring_data_lock(self):
        """_tmux_spawn_member 内部 TERMINAL → DATA 取锁不阻塞（无死锁），
        成功路径与幂等路径均能正常完成。"""
        self._team_with_leader_and("alice")
        self.tmux.sessions["mcp_team"] = {"@1": "lead", "@2": "alice"}

        # 幂等路径：命中 live → _remember_member_window_id → _save(DATA)
        rc, _, msg = mcp._tmux_spawn_member("mcp_team", "alice", "claude", str(self.workspace))
        self.assertEqual(rc, 0)
        self.assertIn("window already exists", msg)

    # ---- new_session 幂等 ----

    def test_new_session_idempotent_when_session_exists(self):
        """new_session=True 且 session 已存在 → 跳过创建（不重复 new-session）。"""
        self._team_with_leader_and("alice")
        self.tmux.sessions["mcp_team"] = {"@1": "lead"}

        rc, _, msg = mcp._tmux_spawn_member("mcp_team", "bob", "claude", str(self.workspace), new_session=True)

        self.assertEqual(rc, 0)
        self.assertIn("session already exists", msg)
        self.assertNotIn("new-session", [c[0] for c in self.tmux.calls])
        self.assertEqual(self.tmux.window_names("mcp_team").count("bob"), 0)

    def test_new_session_creates_when_missing(self):
        """new_session=True 且 session 不存在 → 正常创建。"""
        self._team_with_leader_and("alice")
        self.assertNotIn("mcp_team", self.tmux.sessions)

        rc, _, _ = mcp._tmux_spawn_member("mcp_team", "bob", "claude", str(self.workspace), new_session=True)

        self.assertEqual(rc, 0)
        self.assertIn("mcp_team", self.tmux.sessions)
        self.assertEqual(self.tmux.window_names("mcp_team").count("bob"), 1)

    # ---- 跨进程锁（flock）互斥 ----

    def test_member_spawn_lock_is_exclusive_across_threads(self):
        """common.member_spawn_lock 互斥：持锁期间第二次获取被阻塞，释放后放行。

        flock 基于文件描述符，不同线程/进程各自打开同一路径时彼此互斥，
        这是 MCP / TUI 跨进程防重复 spawn 的核心保证。
        """
        from common.tmux_utils import member_spawn_lock

        lock_acquired = threading.Event()
        release = threading.Event()
        blocked = []

        def holder():
            with member_spawn_lock("team", "bob"):
                lock_acquired.set()
                release.wait(timeout=5)

        def second():
            with member_spawn_lock("team", "bob"):
                blocked.append("second")

        t = threading.Thread(target=holder)
        t.start()
        self.assertTrue(lock_acquired.wait(timeout=5), "第一把锁应能获取")
        t2 = threading.Thread(target=second)
        t2.start()
        t2.join(timeout=0.3)
        self.assertFalse(blocked, "持锁期间第二次获取应被阻塞（跨进程互斥）")
        release.set()
        t.join(timeout=2)
        t2.join(timeout=2)
        self.assertEqual(blocked, ["second"], "释放后第二次获取应放行")

    # ---- 批量路径：自动补角色并发 + 失败可见 ----

    def test_concurrent_select_task_members_single_role_member(self):
        """并发自动补角色 → 同一角色只创建一个成员，且各自 spawn 不产生重复终端。"""
        self._save_team({"lead": {"role": "leader"}})  # 无 coder 成员
        self.tmux.sessions["mcp_team"] = {"@1": "lead"}

        barrier = threading.Barrier(2)
        results = []

        def select():
            barrier.wait()
            results.append(mcp._select_task_members(
                "team", "写一个函数", required_roles="coder", create_missing=True))

        threads = [threading.Thread(target=select) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        members = mcp._load()["teams"]["team"]["members"]
        coders = [n for n, i in members.items() if i.get("role") == "coder"]
        self.assertEqual(len(coders), 1, f"并发自动补角色应只创建一个 coder: {coders}")
        coder = coders[0]
        self.assertEqual(self.tmux.window_names("mcp_team").count(coder), 1)
        for r in results:
            self.assertIn("spawn_failures", r)

    def test_select_task_members_surfaces_spawn_failures(self):
        """批量自动补角色 spawn 失败 → spawn_failures 汇总可见，不静默丢弃。"""
        self._save_team({"lead": {"role": "leader"}})
        self.tmux.sessions["mcp_team"] = {"@1": "lead"}

        with mock.patch.object(mcp, "_tmux_spawn_member", return_value=(1, "", "boom")):
            result = mcp._select_task_members("team", "写个函数", required_roles="coder", create_missing=True)

        self.assertIn("spawn_failures", result)
        self.assertTrue(result["spawn_failures"], "非零 rc 应汇总为可见失败")
        self.assertIn("boom", result["spawn_failures"][0])


    # ---- leader 入口幂等 ----

    def test_leader_add_member_duplicate_returns_exists(self):
        self._team_with_leader_and("alice")
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead", "@2": "alice"}

        result = mcp.leader_add_member("team", "alice", "coder")

        self.assertIn("已存在", result)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)
        self.assertEqual(self.tmux.window_names(session).count("alice"), 1)

    def test_leader_launch_member_terminal_is_idempotent(self):
        self._team_with_leader_and("alice")
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead", "@2": "alice"}

        r1 = mcp.leader_launch_member_terminal("team", "alice")
        r2 = mcp.leader_launch_member_terminal("team", "alice")

        self.assertIn("已在运行", r1)
        self.assertIn("已在运行", r2)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 0)
        self.assertEqual(self.tmux.window_names(session).count("alice"), 1)

    def test_fresh_leader_add_member_spawns_single_window(self):
        # 正常创建流程不回归：新成员恰好产生一个窗口
        self._team_with_leader_and("alice")
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead"}

        result = mcp.leader_add_member("team", "carol", "tester")

        self.assertIn("终端已启动", result)
        self.assertEqual(len(self.tmux.new_window_calls("carol")), 1)
        self.assertEqual(self.tmux.window_names(session).count("carol"), 1)

    # ---- 并发回归 ----

    def test_concurrent_spawn_creates_single_window(self):
        self._team_with_leader_and("bob")
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead"}  # 会话存活，bob 尚未创建

        barrier = threading.Barrier(2)
        results = []

        def spawn():
            barrier.wait()
            results.append(mcp._tmux_spawn_member(session, "bob", "claude", str(self.workspace)))

        threads = [threading.Thread(target=spawn) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 两个调用都成功（幂等），但只产生一个 bob 窗口
        self.assertEqual([r[0] for r in results], [0, 0])
        self.assertEqual(len(self.tmux.new_window_calls("bob")), 1)
        self.assertEqual(self.tmux.window_names(session).count("bob"), 1)

    def test_concurrent_leader_add_member_single_terminal(self):
        self._team_with_leader_and("alice")  # bob 尚不存在
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead"}

        barrier = threading.Barrier(2)
        results = []

        def add():
            barrier.wait()
            results.append(mcp.leader_add_member("team", "bob", "coder"))

        threads = [threading.Thread(target=add) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        members = mcp._load()["teams"]["team"]["members"]
        self.assertIn("bob", members)
        # 恰好一个成功、一个"已存在"
        self.assertEqual(len([r for r in results if "已加入" in r]), 1)
        self.assertEqual(len([r for r in results if "已存在" in r]), 1)
        # 只产生一个 bob 窗口
        self.assertEqual(len(self.tmux.new_window_calls("bob")), 1)
        self.assertEqual(self.tmux.window_names(session).count("bob"), 1)

    # ---- 恢复流程回归 ----

    def test_recover_respawns_when_window_gone_and_no_duplicate_on_repeat(self):
        self._team_with_leader_and("alice")
        session = "mcp_team"
        self.tmux.sessions[session] = {"@1": "lead"}  # alice 窗口已死

        ok1, err1 = mcp._recover_and_send("team", "alice", session)
        self.assertTrue(ok1, err1)
        self.assertEqual(self.tmux.window_names(session).count("alice"), 1)

        # 再次恢复（窗口已存活）→ 不产生副本
        ok2, err2 = mcp._recover_and_send("team", "alice", session)
        self.assertTrue(ok2, err2)
        self.assertEqual(self.tmux.window_names(session).count("alice"), 1)
        self.assertEqual(len(self.tmux.new_window_calls("alice")), 1)


if __name__ == "__main__":
    unittest.main()
