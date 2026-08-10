"""
Multi-Agent MCP — 共享数据层
============================

统一的 teams_data.json 读写接口，供 MCP Server 与 TUI 共用。

提供两种接口:
  - load_data / save_data: 不带锁版本（TUI 使用）
  - get_data / put_data: 带锁版本（MCP server 使用，传入 threading.Lock）

所有 JSON 写入均通过 atomic_json_write() 确保文件权限始终为 0600，
防止 API Key 等敏感数据出现 world/group-readable 窗口。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import threading
from pathlib import Path
from typing import Optional

from common.atomic_write import atomic_json_write
from common.config import DATA_FILE, TEAM_WORKSPACES_DIR, context_base_dir, default_workspace_dir


DELETED_LEGACY_TEAMS_KEY = "_deleted_legacy_teams"


# DATA_FILE 可被测试覆盖（通过修改模块属性）
# 但所有函数内部引用 _resolved_data_file() 以支持动态覆盖
_DATA_FILE_OVERRIDE: Optional[Path] = None


# ---- 测试隔离 fail-fast 防护 -------------------------------------------
# 守卫已下沉至 common/atomic_write.py（2026-08-10）：atomic_json_write 是
# 全仓 JSON 写入的唯一收口，守卫放在那里才能覆盖绕过 data_layer 的直调方
# （如 mult_agent_mcp.py 的 mcp._save）—— 那正是"直跑测试写穿真实 home"
# 的主缺口。依赖方向 data_layer → atomic_write 为单向，反向 import 会循环。
#
# 此处 re-export 保持向后兼容：set_data_file / save_data /
# save_data_as_str_path 仍直接调用，且既有测试可能从 data_layer 导入这些名字。
from common.atomic_write import (  # noqa: F401  (re-export for compatibility)
    assert_write_target_safe,
    _in_test_process,
    _real_data_home,
)


def set_data_file(path: str | Path) -> None:
    """为测试环境设置 DATA_FILE 覆盖。"""
    assert_write_target_safe(path, context="set_data_file")
    global _DATA_FILE_OVERRIDE
    _DATA_FILE_OVERRIDE = Path(path)


def get_data_file() -> Path:
    """返回当前生效的数据文件路径。"""
    if _DATA_FILE_OVERRIDE is not None:
        return _DATA_FILE_OVERRIDE
    return DATA_FILE


# ---- 基础读写 ----


def load_data() -> dict:
    """读取 teams_data.json（不带锁，供 TUI 使用）。"""
    path = get_data_file()
    if not path.exists():
        return {"teams": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data(data: dict) -> None:
    """写入 teams_data.json（不带锁，0600 权限）。"""
    assert_write_target_safe(get_data_file(), context="save_data")
    atomic_json_write(get_data_file(), data)


def mark_legacy_team_deleted(data: dict, team_name: str) -> None:
    """Remember that a team was deleted so legacy project data cannot re-import it."""
    deleted = data.setdefault(DELETED_LEGACY_TEAMS_KEY, {})
    if isinstance(deleted, dict):
        deleted[team_name] = True


# ---- 带锁版本（MCP server 使用）----

def load_data_locked(lock: threading.Lock) -> dict:
    """读取 teams_data.json（持锁）。"""
    with lock:
        return load_data()


def save_data_locked(data: dict, lock: threading.Lock) -> None:
    """写入 teams_data.json（持锁）。"""
    with lock:
        save_data(data)


# ---- 团队/成员信息查询 ----

def team_info(team_name: str) -> dict:
    """获取指定团队的信息 dict。"""
    return load_data().get("teams", {}).get(team_name, {})


def team_context_dir(team_name: str) -> Path:
    """
    解析指定团队的共享上下文目录路径。
    优先级: teams_data.json 中的 context_dir > context_base_dir()/team_name
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    configured = team.get("context_dir")
    if configured:
        return Path(configured).expanduser().resolve()
    return (context_base_dir() / team_name).resolve()


def team_workspace_dir(team_name: str) -> Path:
    """
    解析指定团队的工作目录路径。
    优先级: teams_data.json 中的 workspace_dir > default_workspace_dir()
    """
    data = load_data()
    team = data.get("teams", {}).get(team_name, {})
    configured = team.get("workspace_dir")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(default_workspace_dir()).resolve()


def _is_relative_to(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


def validate_context_path(root: Path, rel_path: str) -> tuple[Path | None, str]:
    """验证上下文文件相对路径的安全性,返回 (resolved_path, error_reason)。

    安全规则:
      - 拒绝空路径
      - 拒绝绝对路径(以 / 开头)
      - 拒绝 ~ 路径
      - 拒绝包含 .. 组件的路径(防止路径穿越)
      - 拒绝解析后不在 root 内的路径
      - 拒绝包含指向 root 外符号链接的路径
    """
    if not rel_path or not rel_path.strip():
        return None, "路径不能为空"
    if rel_path.startswith("/"):
        return None, "不允许绝对路径"
    if rel_path.startswith("~"):
        return None, "不允许 ~ 路径"

    parts = rel_path.replace("\\", "/").split("/")
    if any(p == ".." for p in parts):
        return None, "禁止 .. 路径穿越"

    lexical = root / rel_path
    try:
        if lexical.is_symlink():
            return None, "不支持符号链接"
    except OSError:
        pass  # lexical path may not exist yet (e.g. new file), that's ok

    # 逐段检查中间符号链接(lexical 组件 + is_symlink, 不先 resolve)
    intermediate = root
    for part in parts:
        if not part or part == ".":
            continue
        intermediate = intermediate / part
        try:
            if intermediate.is_symlink():
                return None, f"不支持符号链接 '{part}'"
        except OSError:
            pass  # path doesn't exist yet, skip symlink check

    # 全量 resolve + containment 最终确认
    try:
        full = lexical.resolve()
    except (ValueError, OSError, RuntimeError) as e:
        return None, f"路径解析失败: {e}"

    try:
        full.relative_to(root)
    except ValueError:
        return None, "路径必须在上下文根目录内"

    return full, ""


def cleanup_team_artifacts(team_name: str, team_info: dict) -> list[str]:
    """删除团队托管产物，避免误删用户真实工作目录。

    清理范围:
      - context_dir: 仅当其位于 context_base_dir() 下时删除
      - workspace_dir: 仅当其位于 TEAM_WORKSPACES_DIR 下时删除
      - TEAM_WORKSPACES_DIR/{team_name}: 遗留团队隔离工作区

    返回面向用户的清理结果列表。
    """
    messages: list[str] = []

    context_dir = Path(team_info.get("context_dir") or (context_base_dir() / team_name)).expanduser().resolve()
    context_root = context_base_dir().expanduser().resolve()
    if context_dir.exists():
        if context_dir != context_root and _is_relative_to(context_dir, context_root):
            shutil.rmtree(context_dir)
            messages.append(f"🧹 已删除共享上下文: {context_dir}")
        else:
            messages.append(f"⚠️ 跳过非托管共享上下文: {context_dir}")

    workspace_value = team_info.get("workspace_dir", "")
    if workspace_value:
        workspace_dir = Path(workspace_value).expanduser().resolve()
        workspace_root = TEAM_WORKSPACES_DIR.expanduser().resolve()
        if workspace_dir.exists() and workspace_dir != workspace_root and _is_relative_to(workspace_dir, workspace_root):
            shutil.rmtree(workspace_dir)
            messages.append(f"🧹 已删除团队工作区: {workspace_dir}")
        elif workspace_dir.exists():
            messages.append(f"ℹ️ 保留用户工作目录: {workspace_dir}")

    # 清理遗留 .team_workspaces/{team}/ 隔离工作区
    legacy_tw = (TEAM_WORKSPACES_DIR / team_name).expanduser().resolve()
    workspace_root = TEAM_WORKSPACES_DIR.expanduser().resolve()
    if legacy_tw.exists() and legacy_tw != workspace_root and _is_relative_to(legacy_tw, workspace_root):
        try:
            shutil.rmtree(legacy_tw)
            messages.append(f"🧹 已删除遗留团队工作区: {legacy_tw}")
        except OSError:
            pass

    return messages


# ============================================================
# 向后兼容别名（供 mult_agent_mcp.py 渐进迁移）
# ============================================================

def load_data_as_str_path(data_file: str) -> dict:
    """兼容旧版字符串路径调用。"""
    if not os.path.exists(data_file):
        return {"teams": {}}
    with open(data_file, "r", encoding="utf-8") as f:
        return json.load(f)


def save_data_as_str_path(data: dict, data_file: str) -> None:
    """兼容旧版字符串路径调用（0600 权限）。"""
    assert_write_target_safe(data_file, context="save_data_as_str_path")
    atomic_json_write(Path(data_file), data)
