"""
数据文件隔离 —— 回归测试（防"测试污染真实 teams_data.json"复发）
================================================================

背景 bug（已实证污染）
----------------------
tui/tui_screens.py 的 load_data/save_data 曾把 DEFAULT_DATA_FILE 作为**默认参数在
导入时求值绑定**：

    def load_data(path: Path = DEFAULT_DATA_FILE) -> dict:   # ← 导入时绑定真实路径
    def save_data(data: dict, path: Path = DEFAULT_DATA_FILE) -> None:

导致 common.data_layer.set_data_file() 对它们无效 —— 测试虽调用 set_data_file(tmp)，
save/load 仍读写真实 ~/.mult_agent_mcp/teams_data.json。

修复（reviewer 完成）：默认参数改为 None，函数体内动态解析 get_data_file()。

双保险隔离（教训）
------------------
data_layer.set_data_file(tmp) 只拦 TUI/common 侧；mult_agent_mcp.py 的
_load/_save 直接引用模块全局 DATA_FILE（不经 _DATA_FILE_OVERRIDE）。因此
setUp 必须同时：
  1. data_layer.set_data_file(tmp)   —— 拦 data_layer / tui_screens(修复版)
  2. mcp.DATA_FILE 指向 tmp          —— 拦 mult_agent_mcp._load/_save
两个都在 tearDown 还原。

红线：本测试**绝不对真实 ~/.mult_agent_mcp/teams_data.json 做任何读写**。
不读取、不比对 md5/mtime —— 只断言"实际生效路径 == tmp 且 != 真实默认路径"
（字符串比较）。
"""

import inspect
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
import mult_agent_mcp as mcp
from tui import tui_screens


class _DataFileIsolationTestCase(unittest.TestCase):
    """temp 数据文件隔离基类（双保险）。

    - data_layer.set_data_file → tmp 文件（拦 data_layer / tui_screens 修复版）
    - mcp.DATA_FILE → 同一 tmp 文件（拦 mult_agent_mcp._load/_save）
    - tearDown 两个都还原；本测试对真实路径零读写。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"

        # 保险一：data_layer 覆盖（TUI / common 侧）
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)

        # 保险二：mcp 模块全局 DATA_FILE 覆盖（MCP server 侧 _load/_save）
        self.old_mcp_data_file = mcp.DATA_FILE
        mcp.DATA_FILE = str(self.data_file)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        mcp.DATA_FILE = self.old_mcp_data_file
        self.tmp.cleanup()

    def assert_isolated_to_tmp(self, what="写入"):
        """断言当前生效路径全部指向 tmp，且 != 真实默认路径（字符串比较，零读写真实路径）。"""
        self.assertEqual(str(data_layer.get_data_file()), str(self.data_file),
                         f"{what}应落在 data_layer 覆盖的 tmp 路径")
        self.assertEqual(mcp.DATA_FILE, str(self.data_file),
                         f"{what}应落在 mcp.DATA_FILE 覆盖的 tmp 路径")
        self.assertNotEqual(str(self.data_file), str(tui_screens.DEFAULT_DATA_FILE),
                            "tmp 路径不得与真实默认路径相同")
        self.assertNotEqual(str(self.data_file), self.old_mcp_data_file,
                            "tmp 路径不得与 mcp 模块原始 DATA_FILE 相同")


class SaveDataIsolationTests(_DataFileIsolationTestCase):
    """A: set_data_file(tmp) + mcp.DATA_FILE=tmp 后 save_data 写 tmp 而非真实路径。"""

    def test_save_data_writes_tmp_not_real(self):
        """保存到 tmp：tmp 文件出现且内容一致，生效路径全指向 tmp。"""
        fixture = {"teams": {"iso_team": {"role": "tester"}}}

        tui_screens.save_data(fixture)

        self.assertTrue(self.data_file.exists(), "tmp 数据文件应被写入")
        self.assertEqual(json.loads(self.data_file.read_text()), fixture)
        self.assert_isolated_to_tmp("save_data 写入")

    def test_save_data_twice_no_real_touch(self):
        """连续两次保存仍只写 tmp（真实文件被覆盖风险最高的路径）。"""
        tui_screens.save_data({"teams": {"a": {}}})
        tui_screens.save_data({"teams": {"a": {}, "b": {}}})

        self.assertEqual(
            json.loads(self.data_file.read_text()),
            {"teams": {"a": {}, "b": {}}},
        )
        self.assert_isolated_to_tmp()

    def test_save_data_explicit_path_still_works(self):
        """显式传 path 的行为不回归：仍写指定文件而非 override。"""
        explicit = self.root / "explicit.json"
        tui_screens.save_data({"teams": {}}, explicit)

        self.assertTrue(explicit.exists())
        self.assertFalse(self.data_file.exists(), "未显式传路径时不应写 tmp")
        self.assert_isolated_to_tmp()


class LoadDataIsolationTests(_DataFileIsolationTestCase):
    """B: set_data_file(tmp) + mcp.DATA_FILE=tmp 后 load_data 读 tmp 而非真实文件。"""

    def test_load_data_reads_tmp_not_real(self):
        """tmp 中存在 fixture 时，load_data() 返回 fixture 内容。"""
        fixture = {"teams": {"iso_team": {"role": "tester"}}}
        self.data_file.write_text(json.dumps(fixture))

        loaded = tui_screens.load_data()

        self.assertEqual(loaded, fixture)
        self.assert_isolated_to_tmp("load_data 读取")

    def test_load_data_missing_tmp_returns_empty(self):
        """tmp 不存在时返回空 teams（读不到真实文件）。"""
        self.assertEqual(tui_screens.load_data(), {"teams": {}})
        self.assert_isolated_to_tmp()


class McpSideIsolationTests(_DataFileIsolationTestCase):
    """C(双保险核心): mult_agent_mcp._load/_save 必须落在 tmp —— 这是 data_layer
    set_data_file 拦不住的路径，必须靠 mcp.DATA_FILE 重定向。"""

    def test_mcp_save_writes_tmp(self):
        """mcp._save 写入 tmp 而非真实路径。"""
        mcp._save({"teams": {"mcp_team": {"role": "leader"}}})

        self.assertTrue(self.data_file.exists(), "mcp._save 应写入 tmp")
        self.assertIn("mcp_team", json.loads(self.data_file.read_text())["teams"])
        self.assert_isolated_to_tmp("mcp._save 写入")

    def test_mcp_load_reads_tmp(self):
        """mcp._load 读 tmp 而非真实路径。"""
        fixture = {"teams": {"mcp_team": {"role": "leader"}}}
        self.data_file.write_text(json.dumps(fixture))

        loaded = mcp._load()

        self.assertIn("mcp_team", loaded.get("teams", {}))
        self.assert_isolated_to_tmp("mcp._load 读取")


class MigrationBranchTests(_DataFileIsolationTestCase):
    """D: tmp 路径不得触发 _migrate_data_to_mcp_home()（迁移只对真实默认路径生效）。"""

    def test_tmp_path_never_triggers_migration(self):
        """即使旧数据文件存在（迁移的另一个触发条件），tmp 路径也不迁移。"""
        old_data = self.root / "legacy_teams_data.json"
        old_data.write_text(json.dumps({"teams": {"legacy": {}}}))
        with mock.patch.object(tui_screens, "_OLD_DATA_FILE", old_data), \
             mock.patch.object(tui_screens, "_migrate_data_to_mcp_home") as mig:
            loaded = tui_screens.load_data()
            mig.assert_not_called()
            self.assertEqual(loaded, {"teams": {}})
        self.assert_isolated_to_tmp()


class SignatureScanTests(_DataFileIsolationTestCase):
    """E: 签名扫描 —— 默认参数不得在导入时绑定 Path 实例（防回归最重要的一条）。

    修复前：def load_data(path: Path = DEFAULT_DATA_FILE)  → default 是 Path 实例 → 红
    修复后：def load_data(path: Path | None = None)          → default 是 None → 绿
    """

    def test_path_defaults_not_bound_path_instance(self):
        for func_name in ("load_data", "save_data"):
            with self.subTest(func=func_name):
                func = getattr(tui_screens, func_name)
                sig = inspect.signature(func)
                self.assertIn(
                    "path", sig.parameters,
                    f"{func_name} 应保留 path 参数（当前签名: {sig}）",
                )
                default = sig.parameters["path"].default
                self.assertNotIsInstance(
                    default, Path,
                    f"{func_name} 的 path 默认值在导入时绑定了具体路径 "
                    f"({default!r})——data_layer.set_data_file() 将失效，"
                    f"测试会污染真实 ~/.mult_agent_mcp/teams_data.json。"
                    f"必须改为 None 并在函数体内 get_data_file() 动态解析",
                )


if __name__ == "__main__":
    unittest.main()
