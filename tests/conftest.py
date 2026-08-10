"""
测试环境级隔离 — 任何人跑 pytest 都不可能污染真实 ~/.mult_agent_mcp/。

事故背景:真实 ~/.mult_agent_mcp/teams_data.json 已被测试覆盖两次("team" / "iso_team"
写入真实 home),靠备份恢复。根因是部分读写路径绕过 data_layer._DATA_FILE_OVERRIDE
(见 isolation-audit.md 观察点 1/2)。本文件提供环境级兜底,不依赖任何单条路径是否
正确走 override:

  - pytest 收集 tests/ 时会先导入本文件,早于任何项目模块;
  - 若 MULT_AGENT_MCP_HOME 尚未设置,则指向一个一次性临时目录(进程退出时自动清理);
  - 三处核心模块(tui/tui_screens.py:165 _mcp_home、common/config.py:43
    _resolve_mcp_home、mult_agent_mcp.py:77 _mcp_home)的路径常量均在 import 时解析
    该 env,因此在导入任何项目模块之前设置 env,全部数据路径(teams_data.json /
    contexts/ / mcp_server.pid / mcp_server.log)都会落在临时目录。

优先级:显式传入的 MULT_AGENT_MCP_HOME 永远被尊重,例如
    MULT_AGENT_MCP_HOME=$(mktemp -d) python3 -m pytest tests/ -q
此时 conftest 不覆盖、不清理用户指定的目录。

注意:python3 tests/test_x.py 直跑(unittest.main)不经 pytest,不会加载本文件,
仍须手动带 env。
"""

import atexit
import os
import shutil
import tempfile

import pytest

_ISOLATED = False

if not os.environ.get("MULT_AGENT_MCP_HOME", "").strip():
    _ISOLATED = True
    _ISOLATION_HOME = tempfile.mkdtemp(prefix="mamcp-test-home-")
    os.environ["MULT_AGENT_MCP_HOME"] = _ISOLATION_HOME
    atexit.register(shutil.rmtree, _ISOLATION_HOME, ignore_errors=True)


def pytest_configure(config):
    """会话开始打一行隔离状态,便于回归时确认生效。"""
    home = os.environ.get("MULT_AGENT_MCP_HOME", "")
    if _ISOLATED:
        print(f"[isolation] MULT_AGENT_MCP_HOME={home} (conftest 临时目录,测试结束自动清理)")
    else:
        print(f"[isolation] MULT_AGENT_MCP_HOME={home} (显式设置,尊重不清理)")


@pytest.fixture(autouse=True)
def _guard_real_home_atomic_write(monkeypatch):
    """最后防线：任何 atomic_json_write 目标指向真实 ~/.mult_agent_mcp → fail-fast。

    各模块均以 `from common.atomic_write import atomic_json_write` 绑定名导入
    （mult_agent_mcp / tui.tui_screens / common.config / common.data_layer），
    仅替换模块属性拦不住调用 —— 必须逐点替换绑定名。
    data_layer 自身已带 assert_write_target_safe 守卫，此层兜住其余直写点。
    守卫只拦真实 home 路径，临时目录写入零影响。
    """
    from common import atomic_write
    from common.data_layer import assert_write_target_safe

    original = atomic_write.atomic_json_write

    def guarded(path, data, **kwargs):
        assert_write_target_safe(path, context="atomic_json_write")
        return original(path, data, **kwargs)

    targets = (
        "common.atomic_write",
        "common.config",
        "common.data_layer",
        "mult_agent_mcp",
        "tui.tui_screens",
    )
    for name in targets:
        try:
            mod = __import__(name, fromlist=["*"])
        except Exception:
            continue  # 未导入的模块无需替换
        monkeypatch.setattr(mod, "atomic_json_write", guarded)
