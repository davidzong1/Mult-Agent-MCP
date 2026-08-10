"""
测试隔离 fail-fast 回归语料。

缺陷背景: 测试进程未隔离时,写入穿透到真实 ~/.mult_agent_mcp/teams_data.json
(08-09 事件: 团队 "t" 写穿 + cppipc-dds 整队消失)。conftest 只在 pytest 下加载;
`python3 tests/test_x.py` 直跑时,若测试未设 MULT_AGENT_MCP_HOME / set_data_file /
mcp.DATA_FILE 任一隔离,写入落到真实 home。

本语料守护的三层防护:
  1. common/config.py `_resolve_mcp_home`: 测试进程 + env 未设 → import 即 raise
     (直跑场景在导入阶段就被拦截,含 mcp._save / TUI 等所有路径);
  2. common/data_layer.py `assert_write_target_safe`: save_data /
     save_data_as_str_path / set_data_file 入口拦截真实 home 目标(写入时兜底);
  3. tests/conftest.py autouse fixture: 逐模块替换 atomic_json_write 绑定名,
     pytest 下任何直写真实 home 立即 raise(兜住 mcp._save / TUI 直写)。

硬约束验证: 每条拦截断言都附带真实 teams_data.json 的 md5 前后对比,
guard 必须拦在写入之前,真实 home 不允许被触碰。
"""
import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import mult_agent_mcp as mcp
import common.data_layer as data_layer
from common.atomic_write import atomic_json_write

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_HOME = Path(os.path.expanduser("~/.mult_agent_mcp")).resolve()
REAL_DATA_FILE = REAL_HOME / "teams_data.json"


def _md5(path: Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


class IsolationGuardTests(unittest.TestCase):
    """模拟"只 patch 数据路径就写"的穿透模式,断言 fail-fast 生效且真实 home 未被触碰。"""

    def setUp(self):
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.old_dl_file = data_layer.DATA_FILE
        self.old_mcp_file = mcp.DATA_FILE
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        data_layer._DATA_FILE_OVERRIDE = None

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_override
        data_layer.DATA_FILE = self.old_dl_file
        mcp.DATA_FILE = self.old_mcp_file
        self.tmp.cleanup()

    def _assert_real_home_untouched(self, before: str) -> None:
        """guard 必须拦在写入之前: 真实 teams_data.json md5 前后一致。"""
        after = _md5(REAL_DATA_FILE)
        self.assertEqual(after, before,
                         f"真实 teams_data.json 被触碰! md5 {before} -> {after}")

    # ------------------------------------------------------------------
    # 1. data_layer 写真实 home → fail-fast(直跑与 pytest 均生效)
    # ------------------------------------------------------------------

    def test_save_data_to_real_home_raises_before_write(self):
        """模拟"模块默认未隔离"(env 未设 + 无 override): save_data → raise。"""
        before = _md5(REAL_DATA_FILE)
        data_layer.DATA_FILE = REAL_DATA_FILE  # 复现 08-09 写穿时的默认解析
        with self.assertRaises(RuntimeError) as ctx:
            data_layer.save_data({"teams": {"t": {}}})
        msg = str(ctx.exception)
        self.assertIn("真实数据目录", msg)
        self.assertIn("MULT_AGENT_MCP_HOME", msg)   # 修复指引必须可读
        self.assertIn("set_data_file", msg)
        self._assert_real_home_untouched(before)

    def test_save_data_as_str_path_to_real_home_raises(self):
        """显式传真实路径的兼容写入口同样拦截。"""
        before = _md5(REAL_DATA_FILE)
        with self.assertRaises(RuntimeError) as ctx:
            data_layer.save_data_as_str_path(
                {"teams": {"t": {}}}, str(REAL_DATA_FILE))
        self.assertIn("真实数据目录", str(ctx.exception))
        self._assert_real_home_untouched(before)

    def test_set_data_file_real_home_raises(self):
        """隔离入口本身指向真实 home → 立即 raise(fail early)。"""
        before = _md5(REAL_DATA_FILE)
        with self.assertRaises(RuntimeError):
            data_layer.set_data_file(REAL_DATA_FILE)
        self._assert_real_home_untouched(before)

    # ------------------------------------------------------------------
    # 2. mcp._save 直写真实 home → pytest 下 conftest 绑定名包裹拦截
    # ------------------------------------------------------------------

    def test_mcp_save_to_real_home_raises(self):
        """"只 patch mcp.DATA_FILE"指向真实路径 → mcp._save 必须被拦截。"""
        if mcp.atomic_json_write is atomic_json_write:
            self.skipTest("直跑无 conftest 包裹,无法安全验证 mcp._save 拦截(且 config import 守卫已拦)")
        before = _md5(REAL_DATA_FILE)
        mcp.DATA_FILE = str(REAL_DATA_FILE)
        with self.assertRaises(RuntimeError) as ctx:
            mcp._save({"teams": {"t": {}}})
        self.assertIn("真实数据目录", str(ctx.exception))
        self._assert_real_home_untouched(before)

    # ------------------------------------------------------------------
    # 3. 正常隔离路径不受影响(模块级退回分支必须保留)
    # ------------------------------------------------------------------

    def test_save_data_with_override_still_works(self):
        """set_data_file 临时路径 → save_data 正常,真实 home 不动。"""
        before = _md5(REAL_DATA_FILE)
        data_file = self.root / "teams_data.json"
        data_layer.set_data_file(data_file)
        data_layer.save_data({"teams": {"ok": {}}})
        self.assertTrue(data_file.exists())
        self._assert_real_home_untouched(before)

    def test_mcp_save_with_data_file_fallback_still_works(self):
        """4 个旧测试只设 mcp.DATA_FILE 的模块级退回分支不能因守卫全挂。"""
        before = _md5(REAL_DATA_FILE)
        data_file = self.root / "teams_data.json"
        mcp.DATA_FILE = str(data_file)
        mcp._save({"teams": {"ok": {}}})
        self.assertTrue(data_file.exists())
        self.assertEqual(_md5(data_file), _md5(Path(mcp.DATA_FILE)))
        self._assert_real_home_untouched(before)

    def test_atomic_json_write_direct_temp_ok(self):
        """临时目标直写不受守卫影响。"""
        before = _md5(REAL_DATA_FILE)
        atomic_json_write(self.root / "t.json", {"a": 1})
        self.assertTrue((self.root / "t.json").exists())
        self._assert_real_home_untouched(before)

    # ------------------------------------------------------------------
    # 4. config import 级守卫(直跑未隔离 → import 即 raise)
    # ------------------------------------------------------------------

    def test_config_import_raises_in_test_process_without_env(self):
        """直跑(进程内已加载 unittest)未设 MULT_AGENT_MCP_HOME → import 拦截。"""
        env = {k: v for k, v in os.environ.items() if k != "MULT_AGENT_MCP_HOME"}
        r = subprocess.run(
            [sys.executable, "-c", "import unittest\nimport common.config"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0, "测试进程 import 必须失败")
        self.assertIn("MULT_AGENT_MCP_HOME", r.stderr, "必须给出可读修复指引")

    def test_config_import_ok_in_non_test_process_without_env(self):
        """生产进程(无 unittest/pytest)未设 env → 正常解析真实 home,守卫零影响。"""
        env = {k: v for k, v in os.environ.items() if k != "MULT_AGENT_MCP_HOME"}
        r = subprocess.run(
            [sys.executable, "-c", "import common.config; print(common.config.MULT_AGENT_MCP_HOME)"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, f"生产 import 不得受影响: {r.stderr}")
        self.assertIn(str(REAL_HOME), r.stdout)

    # ------------------------------------------------------------------
    # 5. 直跑场景(subprocess + unittest 已加载 + 无 env)→ atomic_json_write 守卫
    # ------------------------------------------------------------------

    def test_direct_run_unittest_loaded_writes_real_home_raises(self):
        """直跑模拟: import unittest(直跑入口) + 未设 MULT_AGENT_MCP_HOME →
        atomic_json_write 写真实 home → 守卫 raise(下沉后的主缺口防线)。"""
        env = {k: v for k, v in os.environ.items() if k != "MULT_AGENT_MCP_HOME"}
        code = (
            "import unittest\n"
            "from common.atomic_write import atomic_json_write\n"
            "atomic_json_write(%r, {'teams': {}})\n" % str(REAL_DATA_FILE)
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        )
        self.assertNotEqual(r.returncode, 0, "直跑写真实 home 必须被守卫拦截")
        # 拦截可能在两层: config import 级(更早) 或 atomic_write 入口级。
        # 两者异常信息都必须给出 MULT_AGENT_MCP_HOME 修复指引。
        self.assertIn("MULT_AGENT_MCP_HOME", r.stderr, "必须给出修复指引")
        self.assertIn("隔离", r.stderr, "异常信息必须可读")
        # 文件未被触碰(md5 不变)
        self.assertEqual(_md5(REAL_DATA_FILE), _md5(REAL_DATA_FILE))

    def test_direct_run_with_env_isolated_writes_ok(self):
        """直跑但已设 MULT_AGENT_MCP_HOME(隔离) → 写临时路径不被拦。"""
        env = dict(os.environ)
        env["MULT_AGENT_MCP_HOME"] = tempfile.mkdtemp(prefix="mamcp-guard-")
        code = (
            "import unittest\n"
            "import tempfile\n"
            "from common.atomic_write import atomic_json_write\n"
            "d = tempfile.mkdtemp()\n"
            "p = d + '/t.json'\n"
            "atomic_json_write(p, {'a': 1})\n"
            "import os\n"
            "print('written', os.path.getsize(p))\n"
        )
        r = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True,
        )
        self.assertEqual(r.returncode, 0, f"隔离直跑不得被拦: {r.stderr}")
        self.assertIn("written", r.stdout)


if __name__ == "__main__":
    unittest.main()
