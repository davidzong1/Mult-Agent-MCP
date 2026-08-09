"""
mult_agent_mcp 数据隔离 —— set_data_file 一处覆盖隔离全仓
================================================================

铁证背景:mult_agent_mcp.py 的 _load/_save 曾直接引用模块全局 DATA_FILE,
不经过 data_layer._DATA_FILE_OVERRIDE。tester 的隔离测试一旦触到任何走
_load/_save 的路径,set_data_file(tmp) 拦不住,真实 ~/.mult_agent_mcp/
teams_data.json 被覆盖成测试数据、真实团队消失。

修复(方案 A 变体):_load/_save 改为经 data_layer.get_data_file() 解析路径。
  - data_layer.set_data_file(tmp) 覆盖优先生效 → 测试写读 tmp,绝不再碰真实文件
  - 未设置覆盖时退回模块级 DATA_FILE(生产环境两者同值,行为不变;
    同时兼容只改 mcp.DATA_FILE 的既有旧测试写法,它们不回退成污染源)

本测试验证(验收标准):
  1. set_data_file(tmp) 之后,mult_agent_mcp 任何触发 _load/_save 的函数
     (直接 _load/_save、leader_list_team 底层、team_create、list_teams)
     读写都发生在 tmp 而非真实路径。
  2. 同一个 tmp 文件同时隔离 tui_screens 与 mult_agent_mcp 两条路径 ——
     "一个 override 隔离全仓"的最终验收。

红线(同 test_data_file_isolation.py):绝不对真实 ~/.mult_agent_mcp/
teams_data.json 做任何读写。只断言"生效路径 == tmp 且 != 真实默认路径"
(字符串比较),不读取、不比对 md5/mtime。

运行:python -m pytest tests/test_mult_agent_mcp_isolation.py -v
"""

import json
import tempfile
import unittest
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer
from tui import tui_screens


class _OverrideIsolationTestCase(unittest.TestCase):
    """只调 data_layer.set_data_file(tmp),**不改** mcp.DATA_FILE。

    这是与旧 test_data_file_isolation.py 的本质区别:旧测试是"双保险"
    (set_data_file + mcp.DATA_FILE 都设);本测试验证单点覆盖即可隔离
    mult_agent_mcp —— 若实现回退到模块 DATA_FILE,会写真实路径,测试即红。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self.old_mcp_data_file = mcp.DATA_FILE
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        # 关键:只设置 data_layer 覆盖,绝不改 mcp.DATA_FILE
        data_layer.set_data_file(self.data_file)

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        mcp.DATA_FILE = self.old_mcp_data_file
        self.tmp.cleanup()

    def assert_override_active(self):
        """断言覆盖生效,且 tmp != 真实默认路径(纯字符串比较,零读写真实路径)。"""
        self.assertEqual(str(data_layer.get_data_file()), str(self.data_file),
                         "生效路径应等于 set_data_file 指定的 tmp")
        self.assertNotEqual(str(self.data_file), self.old_mcp_data_file,
                            "tmp 不得与模块级真实 DATA_FILE 相同")
        self.assertNotEqual(str(self.data_file), str(data_layer.DATA_FILE),
                            "tmp 不得与 data_layer 真实默认路径相同")
        self.assertEqual(mcp.DATA_FILE, self.old_mcp_data_file,
                         "_load/_save 不得修改模块级 DATA_FILE 引用")


class McpSideOverrideTests(_OverrideIsolationTestCase):
    """验收1:set_data_file(tmp) 后,mult_agent_mcp 的 _load/_save 全走 tmp。"""

    def test_mcp_save_writes_tmp_not_real(self):
        """_save 写入 tmp;真实默认路径不得出现该数据。"""
        fixture = {"teams": {"iso_team": {"role": "tester"}}}
        mcp._save(fixture)
        self.assertTrue(self.data_file.exists(), "_save 应写入 set_data_file 的 tmp 路径")
        self.assertEqual(json.loads(self.data_file.read_text()), fixture)
        self.assert_override_active()

    def test_mcp_load_reads_tmp_not_real(self):
        """_load 读 tmp;真实默认路径中的数据不得被读到。"""
        fixture = {"teams": {"iso_team": {"role": "tester"}}}
        self.data_file.write_text(json.dumps(fixture))
        self.assertEqual(mcp._load(), fixture)
        self.assert_override_active()

    def test_mcp_load_missing_tmp_returns_empty(self):
        """tmp 不存在时 _load 返回空 teams(读不到真实文件)。"""
        self.assertEqual(mcp._load(), {"teams": {}})
        self.assert_override_active()

    def test_leader_list_team_reads_through_override(self):
        """leader_list_team 底层走 _load():读 tmp 中的团队,而非真实路径。"""
        self.data_file.write_text(json.dumps({
            "teams": {"iso_team": {"leader": "", "leader_type": "direct", "members": {}}},
        }))
        out = mcp.leader_list_team("iso_team")
        self.assertIn("iso_team", out)
        self.assertNotIn("不存在", out)
        self.assert_override_active()

    def test_team_create_writes_through_override(self):
        """team_create 触发 _load + _save:团队落在 tmp。"""
        out = mcp.team_create("iso_team")
        self.assertIn("✅", out)
        data = json.loads(self.data_file.read_text())
        self.assertIn("iso_team", data["teams"])
        self.assert_override_active()

    def test_list_teams_reads_through_override(self):
        """list_teams 底层走 _load():只看到 tmp 中的团队。"""
        self.data_file.write_text(json.dumps({"teams": {"iso_team": {}}}))
        out = mcp.list_teams()
        self.assertIn("iso_team", out)
        self.assertNotIn("mcp优化", out, "不得读到真实文件中的团队")
        self.assert_override_active()


class OneOverrideIsolatesWholeRepoTests(_OverrideIsolationTestCase):
    """验收2:同一个 tmp 文件同时隔离 tui_screens 与 mult_agent_mcp ——
    一个 set_data_file 覆盖全仓。"""

    def test_same_tmp_file_serves_both_layers(self):
        """tui 与 mcp 读写同一 tmp 文件(整文件原子写,后写覆盖前写):
        两侧互相能读到对方写入的内容。"""
        tui_screens.save_data({"teams": {"tui_team": {"role": "reviewer"}}})
        # tui 写入 → mcp 可读(同一覆盖文件)
        self.assertIn("tui_team", mcp._load()["teams"])

        mcp._save({"teams": {"mcp_team": {"role": "coder"}}})
        # mcp 写入 → tui 可读(同一覆盖文件)
        self.assertIn("mcp_team", tui_screens.load_data()["teams"])

        # 两侧读到同一文件内容(后写覆盖后两边一致)
        self.assertEqual(mcp._load(), tui_screens.load_data())
        self.assert_override_active()

    def test_tui_writes_mcp_reads_same_file(self):
        """tui_screens.save_data 后,mcp._load 立即可见(同一 tmp 文件)。"""
        tui_screens.save_data({"teams": {"shared_team": {"role": "tester"}}})
        self.assertIn("shared_team", mcp._load()["teams"])
        self.assert_override_active()

    def test_mcp_writes_tui_reads_same_file(self):
        """mcp._save 后,tui_screens.load_data 立即可见(同一 tmp 文件)。"""
        mcp._save({"teams": {"shared_team": {"role": "tester"}}})
        self.assertIn("shared_team", tui_screens.load_data()["teams"])
        self.assert_override_active()


class FallbackCompatibilityTests(_OverrideIsolationTestCase):
    """无覆盖时退回模块级 DATA_FILE —— 兼容只改 mcp.DATA_FILE 的旧测试写法。"""

    def test_no_override_falls_back_to_module_data_file(self):
        """清除覆盖后,_load/_save 写模块级 DATA_FILE(旧测试的隔离方式仍有效)。"""
        data_layer._DATA_FILE_OVERRIDE = None
        old = mcp.DATA_FILE
        try:
            tmp2 = self.root / "module_data.json"
            mcp.DATA_FILE = str(tmp2)
            mcp._save({"teams": {"legacy_iso": {}}})
            self.assertTrue(tmp2.exists(), "无覆盖时应写模块级 DATA_FILE")
            self.assertEqual(mcp._load(), {"teams": {"legacy_iso": {}}})
        finally:
            mcp.DATA_FILE = old
            data_layer._DATA_FILE_OVERRIDE = self.old_data_override

    def test_production_default_paths_identical(self):
        """生产环境(无覆盖):模块级 DATA_FILE 与 data_layer.DATA_FILE 同值,
        修复不改变生产行为。"""
        self.assertEqual(
            mcp.DATA_FILE,
            str(data_layer.DATA_FILE),
            "生产默认路径必须一致(common/config.py:62 与 mult_agent_mcp.py:86)",
        )


if __name__ == "__main__":
    unittest.main()
