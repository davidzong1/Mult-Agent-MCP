"""
task4 后端 — Agent 用户全局迁移生产实现测试。
=============================================

覆盖 common.tmux_utils 新增/修改的生产函数：
  1) migrate_agent_users_global — 纯结构性迁移（R1-R5 + 同 cfg 变体复用）
  2) migrate_agent_users_global_file — 数据文件级迁移（跨进程 flock / 0600 / 幂等 / fail closed）
  3) agent_user_migration_lock — 跨进程迁移临界区锁
  4) 混合/旧数据安全读取 — _effective_agent_user_registry 驱动的
     get_agent_user_env_prefix / resolve_agent_model / list_agent_users
  5) agent_user_rename_sweep / agent_user_delete_sweep / agent_user_ref_count
     —— 与 TUI tui_dialogs 对应实现交叉验证（供 coder 下沉契约对齐）
  6) mult_agent_mcp._migrate_agent_users_global_on_startup — MCP 启动入口
"""

import copy
import os
import stat
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer, tmux_utils
from common.tmux_utils import (
    AGENT_USER_NONE,
    agent_user_delete_sweep,
    agent_user_migration_lock,
    agent_user_ref_count,
    agent_user_rename_sweep,
    get_agent_user_env_prefix,
    list_agent_users,
    migrate_agent_users_global,
    migrate_agent_users_global_file,
    resolve_agent_model,
)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def _claude_cfg(**over) -> dict:
    cfg = {
        "agent_type": "claude",
        "takeover_enabled": True,
        "anthropic_api_key": "sk-ant-test",
        "anthropic_base_url": "https://api.anthropic.com",
        "anthropic_model": "claude-opus-5",
    }
    cfg.update(over)
    return cfg


class DataFileFixture(unittest.TestCase):
    """通过 data_layer.set_data_file 隔离到临时数据文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / ".mult_agent_mcp" / "teams_data.json"
        self.data_file.parent.mkdir(parents=True, exist_ok=True)
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        self.old_have_fcntl = tmux_utils._HAVE_FCNTL

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        tmux_utils._HAVE_FCNTL = self.old_have_fcntl
        self.tmp.cleanup()


# ============================================================
# 1) migrate_agent_users_global — R1-R5 + 变体复用
# ============================================================

class ProductionMigrationTests(DataFileFixture):
    """生产迁移函数直接断言（R1-R5 契约 + 变体复用）。"""

    def test_does_not_mutate_input(self):
        data = {"teams": {"t": {"agent_users": {"p1": _claude_cfg()},
                                "members": {"a": {"agent_user": "p1"}}}}}
        snapshot = copy.deepcopy(data)
        migrate_agent_users_global(data)
        self.assertEqual(data, snapshot)

    def test_R1_plain_migration(self):
        migrated = migrate_agent_users_global({
            "teams": {
                "teamA": {"agent_users": {"p1": _claude_cfg()},
                          "default_agent_user": "p1",
                          "members": {"alice": {"agent_user": "p1"}}},
            },
        })
        self.assertEqual(sorted(migrated["agent_users"].keys()), ["p1"])
        self.assertEqual(migrated["teams"]["teamA"]["members"]["alice"]["agent_user"], "p1")
        self.assertNotIn("agent_users", migrated["teams"]["teamA"])

    def test_R2_same_key_same_cfg_merges(self):
        cfg = _claude_cfg()
        migrated = migrate_agent_users_global({
            "teams": {
                "teamA": {"agent_users": {"p1": cfg}, "members": {"a": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": cfg}, "members": {"b": {"agent_user": "p1"}}},
            },
        })
        self.assertEqual(list(migrated["agent_users"].keys()), ["p1"])
        self.assertEqual(migrated["teams"]["teamB"]["members"]["b"]["agent_user"], "p1")

    def test_R3_conflict_generates_variant(self):
        cfg_a = _claude_cfg(anthropic_base_url="https://a.com")
        cfg_b = _claude_cfg(anthropic_base_url="https://b.com")
        migrated = migrate_agent_users_global({
            "teams": {
                "teamA": {"agent_users": {"p1": cfg_a}, "members": {"a": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": cfg_b},
                          "default_agent_user": "p1",
                          "members": {"b": {"agent_user": "p1"}}},
            },
        })
        self.assertEqual(sorted(migrated["agent_users"].keys()), ["p1", "p1__2"])
        self.assertEqual(migrated["agent_users"]["p1__2"], cfg_b)
        # 仅冲突团队引用改写
        self.assertEqual(migrated["teams"]["teamA"]["members"]["a"]["agent_user"], "p1")
        self.assertEqual(migrated["teams"]["teamB"]["default_agent_user"], "p1__2")
        self.assertEqual(migrated["teams"]["teamB"]["members"]["b"]["agent_user"], "p1__2")

    def test_R3_reuses_existing_variant_with_same_cfg(self):
        """同 cfg 已存在变体时复用，不再生成新变体（跨团队幂等）。"""
        cfg_a = _claude_cfg(anthropic_base_url="https://a.com")
        cfg_b = _claude_cfg(anthropic_base_url="https://b.com")
        migrated = migrate_agent_users_global({
            "teams": {
                "teamA": {"agent_users": {"p1": cfg_a}, "members": {"a": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": cfg_b}, "members": {"b": {"agent_user": "p1"}}},
                "teamC": {"agent_users": {"p1": cfg_b}, "members": {"c": {"agent_user": "p1"}}},
            },
        })
        # p1(cfg_a)、p1__2(cfg_b)；teamB 与 teamC 共享同一变体
        self.assertEqual(sorted(migrated["agent_users"].keys()), ["p1", "p1__2"])
        self.assertEqual(migrated["teams"]["teamB"]["members"]["b"]["agent_user"], "p1__2")
        self.assertEqual(migrated["teams"]["teamC"]["members"]["c"]["agent_user"], "p1__2")

    def test_R3_variant_collision_skips_existing_different_cfg(self):
        """已有 p1__2 但 cfg 不同 → 继续生成 p1__3，不覆盖。"""
        cfg_a = _claude_cfg(anthropic_base_url="https://a.com")
        cfg_b = _claude_cfg(anthropic_base_url="https://b.com")
        cfg_c = _claude_cfg(anthropic_base_url="https://c.com")
        migrated = migrate_agent_users_global({
            "agent_users": {"p1": cfg_a, "p1__2": cfg_b},
            "teams": {
                "teamZ": {"agent_users": {"p1": cfg_c}, "members": {"z": {"agent_user": "p1"}}},
            },
        })
        self.assertEqual(migrated["agent_users"]["p1__3"], cfg_c)
        self.assertEqual(migrated["agent_users"]["p1__2"], cfg_b)
        self.assertEqual(migrated["teams"]["teamZ"]["members"]["z"]["agent_user"], "p1__3")

    def test_R4_none_never_migrated(self):
        migrated = migrate_agent_users_global({
            "teams": {
                "teamA": {"agent_users": {"p1": _claude_cfg()},
                          "members": {"a": {"agent_user": AGENT_USER_NONE}}},
            },
        })
        self.assertNotIn(AGENT_USER_NONE, migrated["agent_users"])
        self.assertEqual(migrated["teams"]["teamA"]["members"]["a"]["agent_user"], AGENT_USER_NONE)

    def test_R5_idempotent(self):
        cfg_a = _claude_cfg(anthropic_base_url="https://a.com")
        cfg_b = _claude_cfg(anthropic_base_url="https://b.com")
        data = {
            "teams": {
                "teamA": {"agent_users": {"p1": cfg_a}, "members": {"a": {"agent_user": "p1"}}},
                "teamB": {"agent_users": {"p1": cfg_b}, "members": {"b": {"agent_user": "p1"}}},
            },
        }
        once = migrate_agent_users_global(data)
        twice = migrate_agent_users_global(once)
        self.assertEqual(once, twice)

    def test_deterministic_stable_order(self):
        data = {
            "teams": {
                "zebra": {"agent_users": {"p1": _claude_cfg()}},
                "alpha": {"agent_users": {"p1": _claude_cfg(anthropic_base_url="https://z.com")}},
            },
        }
        self.assertEqual(migrate_agent_users_global(data), migrate_agent_users_global(data))


# ============================================================
# 2) migrate_agent_users_global_file — 文件级迁移入口
# ============================================================

class MigrationFileEntryTests(DataFileFixture):
    """migrate_agent_users_global_file：0600 / 幂等 / 变更才写盘。"""

    def _write_raw(self, data: dict) -> None:
        with open(self.data_file, "w", encoding="utf-8") as f:
            import json
            json.dump(data, f)

    def test_migrates_file_with_0600(self):
        self._write_raw({
            "teams": {"t": {"agent_users": {"p1": _claude_cfg()},
                            "members": {"a": {"agent_user": "p1"}}}},
        })
        result = migrate_agent_users_global_file()
        self.assertIn("p1", result["agent_users"])
        self.assertNotIn("agent_users", result["teams"]["t"])
        self.assertEqual(_mode(self.data_file), 0o600)

    def test_second_run_is_noop_no_write(self):
        self._write_raw({
            "teams": {"t": {"agent_users": {"p1": _claude_cfg()}}},
        })
        first = migrate_agent_users_global_file()
        mtime1 = os.stat(self.data_file).st_mtime_ns
        time.sleep(0.01)
        second = migrate_agent_users_global_file()
        mtime2 = os.stat(self.data_file).st_mtime_ns
        self.assertEqual(first, second)
        self.assertEqual(mtime1, mtime2, "二次迁移不应写盘")

    def test_missing_file_returns_empty_teams(self):
        result = migrate_agent_users_global_file()
        self.assertEqual(result, {"teams": {}, "agent_users": {}})
        self.assertEqual(_mode(self.data_file), 0o600)

    def test_explicit_data_file_path(self):
        other = self.root / "other" / "teams_data.json"
        other.parent.mkdir(parents=True, exist_ok=True)
        with open(other, "w", encoding="utf-8") as f:
            import json
            json.dump({"teams": {"t": {"agent_users": {"p1": _claude_cfg()}}}}, f)
        result = migrate_agent_users_global_file(other)
        self.assertIn("p1", result["agent_users"])
        self.assertEqual(_mode(other), 0o600)

    def test_corrupt_data_not_overwritten(self):
        self.data_file.write_text("{ not valid json", encoding="utf-8")
        with self.assertRaises(Exception):
            migrate_agent_users_global_file()
        self.assertEqual(self.data_file.read_text(encoding="utf-8"), "{ not valid json")


# ============================================================
# 3) agent_user_migration_lock — 跨进程锁 / fail closed
# ============================================================

class MigrationLockTests(DataFileFixture):
    """跨进程迁移锁：互斥 / fail closed。"""

    def test_lock_serializes_threads(self):
        path = self.root / "data.json"
        order: list[str] = []
        lock = threading.Lock()

        def worker(name: str):
            with agent_user_migration_lock(path):
                with lock:
                    order.append(f"{name}-in")
                time.sleep(0.05)
                with lock:
                    order.append(f"{name}-out")

        t1 = threading.Thread(target=worker, args=("A",))
        t2 = threading.Thread(target=worker, args=("B",))
        t1.start(); time.sleep(0.01); t2.start()
        t1.join(timeout=5); t2.join(timeout=5)
        self.assertEqual(set(order), {"A-in", "A-out", "B-in", "B-out"})
        a_in, a_out = order.index("A-in"), order.index("A-out")
        b_in, b_out = order.index("B-in"), order.index("B-out")
        self.assertLess(a_in, a_out)
        self.assertLess(b_in, b_out)
        # 临界区不重叠：一段完全先于另一段
        self.assertTrue(a_out < b_in or b_out < a_in,
                        f"临界区重叠: {order}")

    def test_lock_is_blocking_while_held(self):
        path = self.root / "data.json"
        acquired = threading.Event()
        release = threading.Event()

        def holder():
            with agent_user_migration_lock(path):
                acquired.set()
                release.wait(2)

        holder_thread = threading.Thread(target=holder)
        holder_thread.start()
        self.assertTrue(acquired.wait(2), "holder 应获得锁")

        got_lock = threading.Event()

        def contender():
            with agent_user_migration_lock(path):
                got_lock.set()

        c = threading.Thread(target=contender, daemon=True)
        c.start()
        time.sleep(0.1)
        self.assertFalse(got_lock.is_set(), "持锁期间竞争者不应获得锁")
        release.set()
        c.join(timeout=2)
        self.assertTrue(got_lock.is_set(), "释放后竞争者应获得锁")
        holder_thread.join(timeout=2)

    def test_fail_closed_when_no_fcntl(self):
        tmux_utils._HAVE_FCNTL = False
        with self.assertRaises(RuntimeError):
            agent_user_migration_lock(self.root / "data.json").__enter__()

    def test_fail_closed_when_lock_open_fails(self):
        with mock.patch("common.tmux_utils.os.open", side_effect=OSError("denied")):
            with self.assertRaises(RuntimeError):
                agent_user_migration_lock(self.root / "data.json").__enter__()

    def test_migrate_file_fail_closed_when_no_fcntl(self):
        tmux_utils._HAVE_FCNTL = False
        with self.assertRaises(RuntimeError):
            migrate_agent_users_global_file()


# ============================================================
# 4) 混合 / 旧数据安全读取 — 全局 registry + 团队旧数据
# ============================================================

class MixedReadPathTests(DataFileFixture):
    """不能因全局已有一项就忽略未迁移的团队 profiles。"""

    def test_team_legacy_wins_on_key_collision(self):
        cfg_global = _claude_cfg(anthropic_base_url="https://global.com")
        cfg_team = _claude_cfg(anthropic_base_url="https://team.com")
        data_layer.save_data({
            "agent_users": {"p1": cfg_global},
            "teams": {"teamA": {"agent_users": {"p1": cfg_team},
                                "members": {"alice": {"agent": "claude", "agent_user": "p1"}}}},
        })
        prefix = get_agent_user_env_prefix("teamA", "alice", "claude")
        self.assertIn("ANTHROPIC_BASE_URL=https://team.com", prefix)
        self.assertNotIn("ANTHROPIC_BASE_URL=https://global.com", prefix)
        self.assertEqual(resolve_agent_model("teamA", "alice"), cfg_team["anthropic_model"])

    def test_global_only_key_resolves(self):
        data_layer.save_data({
            "agent_users": {"p1": _claude_cfg()},
            "teams": {"teamA": {"members": {"alice": {"agent": "claude", "agent_user": "p1"}}}},
        })
        prefix = get_agent_user_env_prefix("teamA", "alice", "claude")
        self.assertIn("ANTHROPIC_BASE_URL=https://api.anthropic.com", prefix)

    def test_legacy_only_key_not_ignored(self):
        """全局非空但团队未迁移 → 成员引用的团队旧 key 仍解析。"""
        data_layer.save_data({
            "agent_users": {"g1": _claude_cfg(anthropic_base_url="https://global.com")},
            "teams": {"teamA": {"agent_users": {"legacy1": _claude_cfg(anthropic_base_url="https://legacy.com")},
                                "members": {"alice": {"agent": "claude", "agent_user": "legacy1"}}}},
        })
        prefix = get_agent_user_env_prefix("teamA", "alice", "claude")
        self.assertIn("ANTHROPIC_BASE_URL=https://legacy.com", prefix)

    def test_list_agent_users_merges_global_and_legacy(self):
        data_layer.save_data({
            "agent_users": {"g1": _claude_cfg()},
            "teams": {"teamA": {"agent_users": {"legacy1": _claude_cfg(anthropic_base_url="https://l.com")}}},
        })
        listed = list_agent_users("teamA")
        self.assertEqual(sorted(listed.keys()), ["g1", "legacy1"])


# ============================================================
# 5) common sweep — 与 TUI 对应实现交叉验证 + M06 契约
# ============================================================

class CommonSweepTests(DataFileFixture):
    """common 版 sweep 与 tui_dialogs 对应函数结果一致，供 coder 下沉契约。"""

    def _tui_funcs(self):
        from tui.tui_dialogs import (
            _agent_user_delete_sweep,
            _agent_user_ref_count,
            _agent_user_rename_sweep,
        )
        return _agent_user_rename_sweep, _agent_user_delete_sweep, _agent_user_ref_count

    def test_rename_matches_tui(self):
        base = {
            "agent_users": {"p1": _claude_cfg()},
            "teams": {
                "tA": {"default_agent_user": "p1", "members": {"a": {"agent_user": "p1"}}},
                "tB": {"agent_users": {"p1": _claude_cfg()}, "members": {"b": {"agent_user": "p1"}}},
            },
        }
        common_data = copy.deepcopy(base)
        tui_data = copy.deepcopy(base)
        common_data["agent_users"]["p_new"] = common_data["agent_users"].pop("p1")
        tui_data["agent_users"]["p_new"] = tui_data["agent_users"].pop("p1")
        tui_rename, tui_delete, tui_count = self._tui_funcs()
        cr = agent_user_rename_sweep(common_data, "p1", "p_new")
        tr = tui_rename(tui_data, "p1", "p_new")
        self.assertEqual(cr, tr)
        self.assertEqual(common_data, tui_data)

    def test_delete_matches_tui(self):
        base = {
            "agent_users": {"p1": _claude_cfg(), "p2": _claude_cfg()},
            "teams": {
                "tA": {"default_agent_user": "p1",
                       "members": {"a": {"agent_user": "p1"}, "b": {"agent_user": "p2"}}},
                "tB": {"agent_users": {"p1": _claude_cfg()}, "members": {"c": {"agent_user": "p1"}}},
            },
        }
        common_data = copy.deepcopy(base)
        tui_data = copy.deepcopy(base)
        common_data["agent_users"].pop("p1", None)
        tui_data["agent_users"].pop("p1", None)
        tui_rename, tui_delete, tui_count = self._tui_funcs()
        cd = agent_user_delete_sweep(common_data, "p1")
        td = tui_delete(tui_data, "p1")
        self.assertEqual(cd, td)
        self.assertEqual(common_data, tui_data)

    def test_ref_count_matches_tui(self):
        base = {
            "teams": {
                "tA": {"default_agent_user": "p1", "members": {"a": {"agent_user": "p1"}, "b": {"agent_user": "p2"}}},
                "tB": {"members": {"c": {"agent_user": "p1"}}},
            },
        }
        tui_rename, tui_delete, tui_count = self._tui_funcs()
        self.assertEqual(agent_user_ref_count(copy.deepcopy(base), "p1"),
                         tui_count(copy.deepcopy(base), "p1"))

    def test_delete_member_ref_not_written_as_none(self):
        data = {
            "agent_users": {"p1": _claude_cfg()},
            "teams": {"tA": {"default_agent_user": "p1",
                             "members": {"a": {"agent_user": "p1"}}}},
        }
        agent_user_delete_sweep(data, "p1")
        self.assertNotIn("default_agent_user", data["teams"]["tA"])
        self.assertNotIn("agent_user", data["teams"]["tA"]["members"]["a"])
        self.assertNotEqual(data["teams"]["tA"]["members"]["a"].get("agent_user"), AGENT_USER_NONE)


# ============================================================
# 6) MCP 启动迁移入口
# ============================================================

class McpStartupMigrationTests(unittest.TestCase):
    """mult_agent_mcp._migrate_agent_users_global_on_startup。"""

    def setUp(self):
        import mult_agent_mcp as mcp
        self.mcp = mcp
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_file = mcp.DATA_FILE
        mcp.DATA_FILE = str(self.root / "teams_data.json")
        self.old_have_fcntl = tmux_utils._HAVE_FCNTL

    def tearDown(self):
        self.mcp.DATA_FILE = self.old_data_file
        tmux_utils._HAVE_FCNTL = self.old_have_fcntl
        self.tmp.cleanup()

    def test_startup_migrates_mcp_data_file(self):
        data_file = Path(self.mcp.DATA_FILE)
        data_file.parent.mkdir(parents=True, exist_ok=True)
        data_file.write_text('{"teams": {"t": {"agent_users": {"p1": {"agent_type": "claude", '
                             '"takeover_enabled": true, "anthropic_base_url": "https://x.com"}}}}}',
                             encoding="utf-8")
        self.mcp._migrate_agent_users_global_on_startup()
        import json
        data = json.loads(data_file.read_text(encoding="utf-8"))
        self.assertIn("p1", data["agent_users"])
        self.assertNotIn("agent_users", data["teams"]["t"])
        self.assertEqual(stat.S_IMODE(os.stat(str(data_file)).st_mode), 0o600)

    def test_startup_swallows_failure(self):
        with mock.patch("mult_agent_mcp.migrate_agent_users_global_file",
                        side_effect=RuntimeError("no lock")):
            # 不应抛出；迁移失败不阻止 MCP 启动
            self.mcp._migrate_agent_users_global_on_startup()


if __name__ == "__main__":
    unittest.main()
