"""
Multi-Agent MCP — 原子 JSON 写入（零外部依赖）
=============================================

提供 atomic_json_write() 供 common/config、common/data_layer、
mult_agent_mcp、tui/tui_screens 共用。

设计约束:
  - 仅依赖 Python 标准库（json, os, sys, tempfile, pathlib）
  - 不导入任何 common.* 或项目模块（避免循环导入）
  - 使用 tempfile.mkstemp 生成唯一临时文件名（创建即 0600）
  - flush + fsync 后 os.replace，最后 chmod 兜底
  - 所有异常路径均清理尚存临时文件

本模块同时承载**测试隔离守卫**（原在 common/data_layer.py，2026-08-10 下沉）:
atomic_json_write 是全仓所有 JSON 写入的唯一收口 —— 包括绕过 data_layer 的
mult_agent_mcp.py 直调，那正是"直跑 mcp._save 写穿真实 home"的主缺口。
守卫放在这里才能覆盖全部写入方；放在 data_layer 只能覆盖它自己的调用者。
依赖方向：data_layer → atomic_write（单向），故守卫必须在下层，反向 import
会造成循环。data_layer 仍 re-export 这些名字以保持向后兼容。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


# Textual 的 Select 用一个 NoSelection 哨兵单例表示"未选择"（Select.NULL）。
# 它 truthy、非 str、且不可 JSON 序列化，一旦从 TUI 表单漏到这里，json.dump
# 就抛 TypeError；而调用方多是 Textual @work worker，异常没人捕获 → 整个 TUI
# 带 traceback 崩溃。这里按**类名**鸭子判定（不 import textual，保持本模块零
# 外部依赖），把它落成空串 —— 与 TUI 侧 _normalize_select_value 的语义一致。
#
# 只认这一个类名：其他不可序列化对象仍然抛 TypeError，不掩盖真实 bug。
_BLANK_SENTINEL_TYPES = frozenset({"NoSelection"})


def _json_default(o):
    """json.dump 的兜底转换器：仅把 Select 的空选择哨兵转成 ''。"""
    if type(o).__name__ in _BLANK_SENTINEL_TYPES:
        return ""
    raise TypeError(
        f"Object of type {type(o).__name__} is not JSON serializable"
    )


# ---- 测试隔离 fail-fast 防护 -------------------------------------------
# 背景：conftest 只在 pytest 下加载；直接 `python3 tests/test_x.py` 跑时
# conftest 不生效，若测试未设置 MULT_AGENT_MCP_HOME / set_data_file /
# mcp.DATA_FILE 任一隔离，写入会穿透到真实 home（08-09 事件：团队 "t"
# 写穿 + cppipc-dds 整队消失 + agent_users 凭证注册表被清空）。
# 本守卫让"测试进程写真实 home"直接 raise。

def _in_test_process() -> bool:
    """检测是否处于测试进程（pytest 或 unittest 已加载）。

    生产进程（daemon / TUI / 手动脚本）不加载二者，实测 import 链中
    unittest/pytest 均不在 sys.modules，因此守卫对生产零影响。
    """
    return "pytest" in sys.modules or "unittest" in sys.modules


def _real_data_home() -> Path:
    """真实数据根目录（MULT_AGENT_MCP_HOME 未设置时的生产默认）。"""
    return Path(os.path.expanduser("~/.mult_agent_mcp")).resolve()


def assert_write_target_safe(target: str | Path, *, context: str = "写入") -> None:
    """测试进程写入目标指向真实 ~/.mult_agent_mcp → fail-fast。

    隔离修复指引（任选其一）:
      · pytest 运行: export MULT_AGENT_MCP_HOME=$(mktemp -d)（conftest 亦自动隔离）
      · 测试内: data_layer.set_data_file(临时路径)（一条覆盖隔离全仓）
      · 测试内: mcp.DATA_FILE = 临时路径（仅 mcp._load/_save 生效，模块级退回分支）
    """
    if not _in_test_process():
        return
    try:
        resolved = Path(target).resolve()
        real = _real_data_home()
    except OSError:
        return
    if resolved == real or str(resolved).startswith(str(real) + os.sep):
        raise RuntimeError(
            f"❌ 测试进程({context})目标 {resolved} 落在真实数据目录 {real} 下。\n"
            "   隔离修复指引（任选其一）:\n"
            "     · export MULT_AGENT_MCP_HOME=$(mktemp -d) 后跑（pytest 下 conftest 自动隔离）\n"
            "     · data_layer.set_data_file(临时路径)（一条覆盖隔离全仓）\n"
            "     · mcp.DATA_FILE = 临时路径（模块级退回分支，仅 mcp._load/_save 生效）\n"
            "   禁止测试读写真实 ~/.mult_agent_mcp/。"
        )


def atomic_json_write(path: Path, data: dict) -> None:
    """Atomically write JSON data with strict 0600 permissions.

    使用 mkstemp 在同目录创建唯一临时文件（创建即 0600），
    写入 JSON → flush → fsync → os.replace → chmod 0600。

    所有 write/replace/chmod 异常路径均尝试清理临时文件；
    正常路径不残留临时文件（os.replace 是原子重命名）。

    Raises OSError if chmod fails — never silently claims
    security when the filesystem cannot enforce 0600.

    Textual Select 的空选择哨兵（Select.NULL）会被落成 ''（见 _json_default）——
    这是防 TUI 崩溃的最后一道防线，不替代表单层的归一化。

    线程/进程安全: 每个调用获取唯一临时文件名，不会互相覆盖。

    测试隔离: 入口即 assert_write_target_safe —— 本函数是全仓 JSON 写入的
    唯一收口，守卫在此才能覆盖绕过 data_layer 的直调方（如 mcp._save）。
    """
    assert_write_target_safe(path, context="atomic_json_write")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)

    # 同目录唯一临时文件，创建即 0600
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=str(path.parent),
        prefix="." + path.name + ".",
        suffix=".tmp",
    )
    os.chmod(tmp_path, 0o600)

    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=_json_default)
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        # 写入失败 — 清理临时文件后重新抛出
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    try:
        os.replace(tmp_path, path)
    except Exception:
        # replace 失败 — 临时文件可能尚存，清理
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    try:
        os.chmod(path, 0o600)
    except Exception:
        # chmod 失败 — 文件已替换但权限不安全，不得静默
        raise
