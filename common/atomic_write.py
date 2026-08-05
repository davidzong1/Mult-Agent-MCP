"""
Multi-Agent MCP — 原子 JSON 写入（零外部依赖）
=============================================

提供 atomic_json_write() 供 common/config、common/data_layer、
mult_agent_mcp、tui/tui_screens 共用。

设计约束:
  - 仅依赖 Python 标准库（json, os, tempfile, pathlib）
  - 不导入任何 common.* 或项目模块（避免循环导入）
  - 使用 tempfile.mkstemp 生成唯一临时文件名（创建即 0600）
  - flush + fsync 后 os.replace，最后 chmod 兜底
  - 所有异常路径均清理尚存临时文件
"""

from __future__ import annotations

import json
import os
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
    """
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
