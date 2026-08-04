"""
原子 JSON 写入 (atomic_json_write) 文件权限与清理测试。
=====================================================

验证：
  1. 首次创建：文件 mode 为 0600
  2. 覆盖已有 0664 文件：最终 mode 为 0600
  3. 数据完整性：写入后读回一致
  4. tmp 临时文件不残留
  5. 预置宽权限 .tmp 文件：mkstemp 唯一名不被复用
  6. 异常清理：json.dump / os.replace 失败后 tmp 清理
  7. 并发写入：mkstemp 各自唯一 tmp → 无冲突、有效 JSON
  8. common.config 迁移：首次 + changed 二次均为 0600
  9. fresh MULT_AGENT_MCP_HOME 无 contexts 目录 → import 不崩

覆盖生产路径：
  - common.atomic_write.atomic_json_write（核心）
  - common.data_layer.save_data
  - mult_agent_mcp._save
  - tui.tui_screens.save_data
  - common.config._migrate_old_data

全部使用临时目录，不读写真实 home。
"""

import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
from common.atomic_write import atomic_json_write

import mult_agent_mcp as mcp
from tui.tui_screens import save_data as tui_save_data


def _mode(path: str | Path) -> int:
    """返回文件 permission bits（低 9 位）。"""
    return stat.S_IMODE(os.stat(str(path)).st_mode)


def _set_file_mode(path: str | Path, mode: int) -> None:
    """设置文件 permission bits。"""
    os.chmod(str(path), mode)


def _tmp_files_in(dir_path: str | Path) -> list[str]:
    """返回目录中所有 .tmp 文件。"""
    return sorted(
        str(p) for p in Path(dir_path).iterdir()
        if p.suffix == ".tmp" or p.name.endswith(".tmp")
    )


# ============================================================
# atomic_json_write — 核心写入函数
# ============================================================

class AtomicJsonWriteTests(unittest.TestCase):
    """直接测试 atomic_json_write 的 mode/完整性/清理行为。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_create_has_mode_0600(self):
        atomic_json_write(self.target, {"key": "value"})
        self.assertTrue(self.target.exists())
        self.assertEqual(_mode(self.target), 0o600)

    def test_overwrite_0664_becomes_0600(self):
        self.target.write_text('{"old": true}', encoding="utf-8")
        _set_file_mode(self.target, 0o664)
        self.assertEqual(_mode(self.target), 0o664, "setup: 应成功创建 0664 文件")

        atomic_json_write(self.target, {"new": "data"})
        self.assertEqual(_mode(self.target), 0o600)

    def test_overwrite_0644_becomes_0600(self):
        self.target.write_text('{"old": true}', encoding="utf-8")
        _set_file_mode(self.target, 0o644)
        atomic_json_write(self.target, {"new": "data"})
        self.assertEqual(_mode(self.target), 0o600)

    def test_data_complete_after_write(self):
        data = {
            "teams": {
                "test": {
                    "members": {"alice": {"role": "coder"}},
                    "agent_users": {
                        "p1": {
                            "agent_type": "claude",
                            "anthropic_api_key": "sk-ant-fake123",
                        }
                    },
                }
            }
        }
        atomic_json_write(self.target, data)
        with open(self.target, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded, data)

    def test_no_tmp_files_left_after_success(self):
        atomic_json_write(self.target, {"a": 1})
        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [],
                         f"不应残留 tmp 文件，实际: {tmp_files}")

    def test_no_tmp_files_after_overwrite(self):
        self.target.write_text('{"old": 1}', encoding="utf-8")
        atomic_json_write(self.target, {"b": 2})
        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [],
                         f"覆盖后不应残留 tmp 文件，实际: {tmp_files}")

    def test_nested_subdirectory_creates_parents(self):
        nested = self.root / "sub" / "deep" / "data.json"
        atomic_json_write(nested, {"deep": True})
        self.assertTrue(nested.exists())
        self.assertEqual(_mode(nested), 0o600)

    def test_empty_dict_is_written_correctly(self):
        atomic_json_write(self.target, {})
        self.assertTrue(self.target.exists())
        with open(self.target, "r", encoding="utf-8") as f:
            self.assertEqual(json.load(f), {})


# ============================================================
# common.data_layer.save_data — 生产路径 1
# ============================================================

class DataLayerSaveDataTests(unittest.TestCase):
    """通过 common.data_layer.save_data 写入，验证 mode 0600。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.data_file = str(self.root / "teams_data.json")
        data_layer.set_data_file(self.data_file)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        self.tmp.cleanup()

    def test_first_save_has_mode_0600(self):
        data_layer.save_data({"teams": {"t1": {"members": {}}}})
        self.assertEqual(_mode(self.data_file), 0o600)

    def test_overwrite_0664_becomes_0600(self):
        Path(self.data_file).write_text('{"old": 1}', encoding="utf-8")
        _set_file_mode(self.data_file, 0o664)
        data_layer.save_data({"teams": {"t2": {}}})
        self.assertEqual(_mode(self.data_file), 0o600)

    def test_data_roundtrip(self):
        data = {"teams": {"t1": {"members": {"a": {"role": "coder"}}}}}
        data_layer.save_data(data)
        loaded = data_layer.load_data()
        self.assertEqual(loaded, data)

    def test_no_tmp_files_left(self):
        data_layer.save_data({"teams": {}})
        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [])


# ============================================================
# mult_agent_mcp._save — 生产路径 2
# ============================================================

class McpSaveTests(unittest.TestCase):
    """通过 mult_agent_mcp._save 写入，验证 mode 0600。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.project = self.root / "project"
        self.project.mkdir()

        self.old_globals = {
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        mcp_data_dir = self.project / ".mult_agent_mcp"
        mcp_data_dir.mkdir(parents=True)
        mcp.DATA_FILE = str(mcp_data_dir / "teams_data.json")
        mcp.TEAM_WORKSPACES_DIR = str(self.project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(self.project / ".mult_agent_mcp" / "contexts")
        # RLock：_save/_load/_update_team_data 存在锁内重入；tearDown 会恢复原锁
        mcp.TEAM_DATA_LOCK = threading.RLock()
        data_layer.set_data_file(mcp.DATA_FILE)

        self.data_file = mcp.DATA_FILE

    def tearDown(self):
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def test_first_save_has_mode_0600(self):
        mcp._save({"teams": {"t1": {"members": {}}}})
        self.assertEqual(_mode(self.data_file), 0o600)

    def test_overwrite_0664_becomes_0600(self):
        Path(self.data_file).write_text('{"old": 1}', encoding="utf-8")
        _set_file_mode(self.data_file, 0o664)
        mcp._save({"teams": {"t2": {}}})
        self.assertEqual(_mode(self.data_file), 0o600)

    def test_data_roundtrip(self):
        data = {"teams": {"t1": {"members": {"a": {"role": "coder"}}}}}
        mcp._save(data)
        loaded = mcp._load()
        self.assertEqual(loaded, data)

    def test_no_tmp_files_left(self):
        mcp._save({"teams": {}})
        tmp_files = _tmp_files_in(Path(self.data_file).parent)
        self.assertEqual(tmp_files, [])


# ============================================================
# tui.tui_screens.save_data — 生产路径 3
# ============================================================

class TuiSaveDataTests(unittest.TestCase):
    """通过 tui.tui_screens.save_data 写入，验证 mode 0600。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "teams_data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_first_save_has_mode_0600(self):
        tui_save_data({"teams": {"t1": {"members": {}}}}, path=self.target)
        self.assertEqual(_mode(self.target), 0o600)

    def test_overwrite_0664_becomes_0600(self):
        self.target.write_text('{"old": 1}', encoding="utf-8")
        _set_file_mode(self.target, 0o664)
        tui_save_data({"teams": {"t2": {}}}, path=self.target)
        self.assertEqual(_mode(self.target), 0o600)

    def test_data_roundtrip(self):
        data = {"teams": {"t1": {"members": {"a": {"role": "coder"}}}}}
        tui_save_data(data, path=self.target)
        with open(self.target, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertEqual(loaded, data)

    def test_no_tmp_files_left(self):
        tui_save_data({"teams": {}}, path=self.target)
        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [])


# ============================================================
# 集成：三条路径都对已存在 0664 文件生效
# ============================================================

class CrossPathModeConsistencyTests(unittest.TestCase):
    """三条生产路径覆盖同一 0664 文件 → 均为 0600。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.dl_file = str(self.root / "dl_teams_data.json")
        data_layer.set_data_file(self.dl_file)

        self.old_globals = {
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        mcp_project = self.root / "mcp_project"
        mcp_project.mkdir()
        mcp_data_dir = mcp_project / ".mult_agent_mcp"
        mcp_data_dir.mkdir(parents=True)
        mcp.DATA_FILE = str(mcp_data_dir / "teams_data.json")
        mcp.TEAM_WORKSPACES_DIR = str(mcp_project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(mcp_project / ".mult_agent_mcp" / "contexts")
        if not hasattr(mcp, "TEAM_DATA_LOCK") or mcp.TEAM_DATA_LOCK is None:
            mcp.TEAM_DATA_LOCK = threading.RLock()

        self.tui_file = self.root / "tui_teams_data.json"

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        for k, v in self.old_globals.items():
            setattr(mcp, k, v)
        self.tmp.cleanup()

    def test_all_three_paths_yield_0600(self):
        payload = {"teams": {"t": {"members": {}}}}

        Path(self.dl_file).write_text("{}", encoding="utf-8")
        _set_file_mode(self.dl_file, 0o664)
        data_layer.save_data(payload)
        self.assertEqual(_mode(self.dl_file), 0o600,
                         "data_layer.save_data 应产生 0600")

        Path(mcp.DATA_FILE).write_text("{}", encoding="utf-8")
        _set_file_mode(mcp.DATA_FILE, 0o664)
        mcp._save(payload)
        self.assertEqual(_mode(mcp.DATA_FILE), 0o600,
                         "mcp._save 应产生 0600")

        self.tui_file.write_text("{}", encoding="utf-8")
        _set_file_mode(self.tui_file, 0o664)
        tui_save_data(payload, path=self.tui_file)
        self.assertEqual(_mode(self.tui_file), 0o600,
                         "tui save_data 应产生 0600")


# ============================================================
# 预置宽权限 .tmp → mkstemp 唯一名不被复用
# ============================================================

class PreExistingTmpTests(unittest.TestCase):
    """预置固定名 .tmp 文件 — mkstemp 用唯一名，不冲突。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_preexisting_tmp_content_not_in_final(self):
        tmp_path = self.root / "data.json.tmp"
        JUNK = "THIS_IS_JUNK_CONTENT_SHOULD_NOT_PERSIST"
        tmp_path.write_text(JUNK, encoding="utf-8")
        _set_file_mode(tmp_path, 0o666)

        atomic_json_write(self.target, {"real": "data"})

        with open(self.target, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertNotIn("JUNK", content)
        loaded = json.loads(content)
        self.assertEqual(loaded, {"real": "data"})

    def test_preexisting_tmp_wide_mode_final_is_0600(self):
        tmp_path = self.root / "data.json.tmp"
        tmp_path.write_text("junk", encoding="utf-8")
        _set_file_mode(tmp_path, 0o666)

        atomic_json_write(self.target, {"key": "value"})
        self.assertEqual(_mode(self.target), 0o600)

    def test_preexisting_tmp_not_touched_by_mkstemp(self):
        """mkstemp 生成唯一文件名，预置固定名 .tmp 不被覆盖/移走。"""
        foreign_tmp = self.root / "data.json.tmp"
        foreign_tmp.write_text("junk", encoding="utf-8")
        _set_file_mode(foreign_tmp, 0o666)

        atomic_json_write(self.target, {"x": 1})
        self.assertTrue(self.target.exists())
        self.assertEqual(_mode(self.target), 0o600)
        self.assertTrue(foreign_tmp.exists(),
                        "mkstemp 唯一名不与预置固定路径冲突")

    def test_preexisting_tmp_before_save_data_path(self):
        """data_layer.save_data 路径，预置固定名 .tmp 同样不被触碰。"""
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(str(self.target))
        try:
            foreign_tmp = self.root / "data.json.tmp"
            foreign_tmp.write_text("STALE", encoding="utf-8")
            _set_file_mode(foreign_tmp, 0o666)

            data_layer.save_data({"teams": {"t1": {}}})
            self.assertEqual(_mode(self.target), 0o600)
            self.assertTrue(foreign_tmp.exists(),
                            "mkstemp 唯一名，预置固定路径应留存")
        finally:
            data_layer._DATA_FILE_OVERRIDE = self.old_override


# ============================================================
# 异常清理：json.dump / os.replace 失败后的 tmp 清理
# ============================================================

class ExceptionCleanupTests(unittest.TestCase):
    """模拟异常后验证 mkstemp 临时文件清理。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_dump_exception_cleans_tmp(self):
        """json.dump 抛异常 → mkstemp 临时文件被 unlink。"""
        def failing_dump(obj, fp, **kwargs):
            fp.write('{"')
            fp.flush()
            raise RuntimeError("simulated json.dump failure")

        with mock.patch("common.atomic_write.json.dump", side_effect=failing_dump):
            with self.assertRaises(RuntimeError):
                atomic_json_write(self.target, {"key": "value"})

        # 不应残留任何 writer 创建的 .tmp 文件
        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [],
                         f"json.dump 异常后应清理 tmp，实际: {tmp_files}")
        self.assertFalse(self.target.exists(),
                         "json.dump 异常后 target 不应被创建")

    def test_os_replace_exception_cleans_tmp(self):
        """os.replace 抛异常 → mkstemp 临时文件被清理。"""
        with mock.patch("common.atomic_write.os.replace",
                        side_effect=OSError("simulated replace failure")):
            with self.assertRaises(OSError):
                atomic_json_write(self.target, {"key": "value"})

        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [],
                         f"os.replace 异常后应清理 tmp，实际: {tmp_files}")
        self.assertFalse(self.target.exists(),
                         "os.replace 异常后 target 不应存在")


# ============================================================
# 并发 atomic_json_write → mkstemp 各自唯一 tmp 无冲突
# ============================================================

class ConcurrentAtomicWriteTests(unittest.TestCase):
    """多线程并发 atomic_json_write — mkstemp 各自独立 tmp 文件。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.target = self.root / "data.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _run_concurrent_writes(self, data_a: dict, data_b: dict):
        """Helper: fire two concurrent atomic_json_write."""
        errors = []
        barrier = threading.Barrier(2)

        def writer(data):
            try:
                barrier.wait()
                atomic_json_write(self.target, data)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=writer, args=(data_a,))
        t2 = threading.Thread(target=writer, args=(data_b,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        return errors

    def test_concurrent_writes_no_exceptions(self):
        """mkstemp 各自唯一 tmp → 无 FileNotFoundError 冲突。"""
        errors = self._run_concurrent_writes({"a": 1}, {"b": 2})
        self.assertEqual(errors, [],
                         f"mkstemp 唯一名不应有冲突，实际: {errors}")

    def test_concurrent_writes_valid_json(self):
        """并发写入后 target 包含有效 JSON（last-writer-wins）。"""
        self._run_concurrent_writes(
            {"writer": "alpha", "id": 1},
            {"writer": "beta", "id": 2},
        )
        self.assertTrue(self.target.exists())
        with open(self.target, "r", encoding="utf-8") as f:
            content = f.read()
        loaded = json.loads(content)
        self.assertIn(loaded.get("writer"), ("alpha", "beta"))
        self.assertIn(loaded.get("id"), (1, 2))

    def test_concurrent_writes_no_tmp_leftover(self):
        """并发写入后不残留 mkstemp tmp 文件。"""
        self._run_concurrent_writes({"x": 0}, {"y": 1})
        tmp_files = _tmp_files_in(self.root)
        self.assertEqual(tmp_files, [],
                         f"并发后不应残留 tmp，实际: {tmp_files}")

    def test_concurrent_writes_final_mode_0600(self):
        self._run_concurrent_writes({"a": 1}, {"b": 2})
        self.assertEqual(_mode(self.target), 0o600)

    def test_concurrent_writes_with_preexisting_0664_target(self):
        self.target.write_text('{"old": true}', encoding="utf-8")
        _set_file_mode(self.target, 0o664)
        self._run_concurrent_writes({"new_a": 1}, {"new_b": 2})
        self.assertEqual(_mode(self.target), 0o600)

    def test_many_concurrent_writes(self):
        """4 线程并发 → 无异常、有效 JSON、0o600。"""
        errors = []
        barrier = threading.Barrier(4)

        def writer(data):
            try:
                barrier.wait()
                atomic_json_write(self.target, data)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=writer, args=({"n": i},))
            for i in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(errors, [],
                         f"4 线程 mkstemp 不应有冲突，实际: {errors}")
        self.assertTrue(self.target.exists())
        with open(self.target, "r", encoding="utf-8") as f:
            loaded = json.load(f)
        self.assertIn("n", loaded)
        self.assertEqual(_mode(self.target), 0o600)


# ============================================================
# common.config 迁移含 fake key → 最终 mode 0600
# ============================================================

class ConfigMigrationModeTests(unittest.TestCase):
    """common.config._migrate_old_data — 首次 + changed 二次迁移均 0600。"""

    def setUp(self):
        import common.config as config

        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

        self.orig_project_dir = config.PROJECT_DIR
        self.orig_data_file = config.DATA_FILE
        self.orig_contexts_dir = config.CONTEXTS_DIR
        self.orig_mcp_home = config.MULT_AGENT_MCP_HOME

        new_home = self.root / "home" / ".mult_agent_mcp"
        config.MULT_AGENT_MCP_HOME = new_home
        config.DATA_FILE = new_home / "teams_data.json"
        config.CONTEXTS_DIR = new_home / "contexts"

        new_project = self.root / "project"
        new_project.mkdir(parents=True)
        config.PROJECT_DIR = new_project

        self.legacy = config.PROJECT_DIR / "teams_data.json"
        self.config = config

    def tearDown(self):
        self.config.PROJECT_DIR = self.orig_project_dir
        self.config.DATA_FILE = self.orig_data_file
        self.config.CONTEXTS_DIR = self.orig_contexts_dir
        self.config.MULT_AGENT_MCP_HOME = self.orig_mcp_home
        self.tmp.cleanup()

    def test_first_migration_with_fake_key_has_mode_0600(self):
        """首次迁移（DATA_FILE 不存在）→ atomic_json_write → 0o600。"""
        FAKE_KEY = "sk-ant-fake999secret-migration"
        legacy_data = {
            "teams": {
                "test_team": {
                    "members": {"alice": {"role": "coder", "agent": "claude"}},
                    "agent_users": {
                        "p1": {
                            "agent_type": "claude",
                            "anthropic_api_key": FAKE_KEY,
                            "anthropic_base_url": "https://api.example.com",
                            "anthropic_model": "sonnet",
                        }
                    },
                }
            }
        }
        self.legacy.write_text(
            json.dumps(legacy_data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        _set_file_mode(self.legacy, 0o664)

        target = self.config.DATA_FILE
        if target.exists():
            target.unlink()

        with mock.patch.object(self.config, "OLD_SHARE_CONTEXT_DIR",
                               self.root / "no_such_share"):
            self.config._migrate_old_data()

        self.assertTrue(target.exists(),
                        f"迁移应创建 DATA_FILE: {target}")
        self.assertEqual(_mode(target), 0o600,
                         f"首次迁移后 mode 应为 0600，实际 {oct(_mode(target))}")

        # 数据完整性：fake key 保留
        with open(target, "r", encoding="utf-8") as f:
            migrated = json.load(f)
        p1 = migrated["teams"]["test_team"]["agent_users"]["p1"]
        self.assertEqual(p1["anthropic_api_key"], FAKE_KEY)

    def test_changed_second_migration_has_mode_0600(self):
        """二次迁移触发 changed=True → atomic_json_write → 0o600。"""
        FAKE_KEY = "sk-ant-double-run-key"
        legacy_data = {
            "teams": {
                "t": {
                    "members": {"a": {"role": "coder"}},
                    "agent_users": {"p1": {"agent_type": "claude",
                                           "anthropic_api_key": FAKE_KEY}},
                }
            }
        }
        self.legacy.write_text(json.dumps(legacy_data, indent=2),
                               encoding="utf-8")
        _set_file_mode(self.legacy, 0o664)

        target = self.config.DATA_FILE
        if target.exists():
            target.unlink()

        # 第一次迁移（DATA_FILE 不存在 → atomic_json_write）
        with mock.patch.object(self.config, "OLD_SHARE_CONTEXT_DIR",
                               self.root / "no_such_share"):
            self.config._migrate_old_data()
        self.assertEqual(_mode(target), 0o600,
                         f"首次迁移 mode 应为 0600，实际 {oct(_mode(target))}")

        # 在旧数据中新增一个成员以触发 changed=True
        new_legacy = json.loads(self.legacy.read_text(encoding="utf-8"))
        new_legacy["teams"]["t"]["members"]["b"] = {"role": "tester"}
        self.legacy.write_text(json.dumps(new_legacy, indent=2),
                               encoding="utf-8")

        # 第二次迁移（DATA_FILE 已存在 → changed=True → atomic_json_write）
        with mock.patch.object(self.config, "OLD_SHARE_CONTEXT_DIR",
                               self.root / "no_such_share"):
            self.config._migrate_old_data()
        self.assertEqual(_mode(target), 0o600,
                         f"changed 二次迁移 mode 应为 0600，实际 {oct(_mode(target))}")

        # 数据完整性
        with open(target, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertIn("b", data["teams"]["t"]["members"],
                      "新增成员 b 应被合并")
        self.assertEqual(
            data["teams"]["t"]["agent_users"]["p1"]["anthropic_api_key"],
            FAKE_KEY,
        )

    def test_migration_no_legacy_data_returns_false(self):
        """PROJECT_DIR/teams_data.json 不存在 → 返回 False，不创建 DATA_FILE。"""
        # 不创建 legacy 文件
        target = self.config.DATA_FILE
        if target.exists():
            target.unlink()

        # 确保 _migrate_old_data 不会因为有 CONTEXTS_DIR.iterdir() 而崩
        with mock.patch.object(self.config, "OLD_SHARE_CONTEXT_DIR",
                               self.root / "no_such_share"):
            result = self.config._migrate_old_data()

        self.assertFalse(result, "无旧数据应返回 False")


# ============================================================
# fresh MULT_AGENT_MCP_HOME：CONTEXTS_DIR 不存在 → import 不崩
# ============================================================

class FreshHomeImportTests(unittest.TestCase):
    """fresh MULT_AGENT_MCP_HOME（无 contexts 目录）→ import 不崩。

    common.config 模块加载时调用 _migrate_old_data()，其中
    any(CONTEXTS_DIR.iterdir()) 在 CONTEXTS_DIR 不存在时不应
    抛 FileNotFoundError（已由 mkdir 补丁修复）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_migrate_old_data_without_contexts_dir_does_not_raise(self):
        """CONTEXTS_DIR 不存在时 _migrate_old_data 不抛异常。"""
        import common.config as config

        new_home = self.root / "fresh_home" / ".mult_agent_mcp"
        # 仅创建 MULT_AGENT_MCP_HOME（目录级），但不创建 contexts 子目录
        new_home.mkdir(parents=True)

        old_home = config.MULT_AGENT_MCP_HOME
        old_data_file = config.DATA_FILE
        old_contexts = config.CONTEXTS_DIR
        old_project = config.PROJECT_DIR

        try:
            config.MULT_AGENT_MCP_HOME = new_home
            config.DATA_FILE = new_home / "teams_data.json"
            config.CONTEXTS_DIR = new_home / "contexts"
            # 确保 CONTEXTS_DIR 不存在（模拟 fresh install）
            if config.CONTEXTS_DIR.exists():
                import shutil
                shutil.rmtree(str(config.CONTEXTS_DIR))

            new_project = self.root / "empty_project"
            new_project.mkdir(parents=True)
            config.PROJECT_DIR = new_project

            # 无旧 teams_data.json — 走 old_data.exists() → False 分支
            with mock.patch.object(config, "OLD_SHARE_CONTEXT_DIR",
                                   self.root / "no_such_share"):
                result = config._migrate_old_data()

            self.assertFalse(result,
                             "无旧数据应返回 False，且不崩")
        finally:
            config.MULT_AGENT_MCP_HOME = old_home
            config.DATA_FILE = old_data_file
            config.CONTEXTS_DIR = old_contexts
            config.PROJECT_DIR = old_project

    def test_migrate_old_data_with_contexts_dir_does_not_raise(self):
        """CONTEXTS_DIR 存在时 _migrate_old_data 正常工作（回归）。"""
        import common.config as config

        new_home = self.root / "with_ctx" / ".mult_agent_mcp"
        new_home.mkdir(parents=True)
        ctx_dir = new_home / "contexts"
        ctx_dir.mkdir(parents=True)

        old_home = config.MULT_AGENT_MCP_HOME
        old_data_file = config.DATA_FILE
        old_contexts = config.CONTEXTS_DIR
        old_project = config.PROJECT_DIR

        try:
            config.MULT_AGENT_MCP_HOME = new_home
            config.DATA_FILE = new_home / "teams_data.json"
            config.CONTEXTS_DIR = ctx_dir

            new_project = self.root / "empty_project2"
            new_project.mkdir(parents=True)
            config.PROJECT_DIR = new_project

            with mock.patch.object(config, "OLD_SHARE_CONTEXT_DIR",
                                   self.root / "no_such_share"):
                result = config._migrate_old_data()

            self.assertFalse(result)
        finally:
            config.MULT_AGENT_MCP_HOME = old_home
            config.DATA_FILE = old_data_file
            config.CONTEXTS_DIR = old_contexts
            config.PROJECT_DIR = old_project


if __name__ == "__main__":
    unittest.main()
