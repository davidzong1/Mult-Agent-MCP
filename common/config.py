"""
Multi-Agent MCP — 共享配置与路径模块
=====================================

统一的路径常量与环境变量约定，供 MCP Server 与 TUI 共用。

路径约定:
  ~/.mult_agent_mcp/                  ← MULT_AGENT_MCP_HOME（可通过 env 覆盖）
  ├── teams_data.json                 ← 团队数据
  ├── contexts/{team}/                ← 团队共享上下文（原 share_context_space/{team}/）
  │   ├── results.jsonl
  │   ├── member_contexts/
  │   └── patches/
  ├── mcp_server.pid                  ← 守护进程 PID
  └── mcp_server.log                  ← 守护进程日志

向后兼容 / 迁移:
  - 如果 {PROJECT_DIR}/teams_data.json 存在而 ~/.mult_agent_mcp/teams_data.json 不存在，
    自动迁移：复制旧数据并更新各团队的 context_dir（如果指向旧位置）。
  - 环境变量 MULT_AGENT_MCP_HOME 可覆盖 ~/.mult_agent_mcp。
  - 环境变量 MULT_AGENT_MCP_CONTEXT_DIR 可覆盖全局上下文根目录。
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path


# ============================================================
# 项目根目录（脚本所在目录，不可变）
# ============================================================
PROJECT_DIR = Path(__file__).resolve().parent.parent


# ============================================================
# MULT_AGENT_MCP_HOME — 数据持久化根目录
# ============================================================

def _resolve_mcp_home() -> Path:
    """解析 MULT_AGENT_MCP_HOME，默认为 ~/.mult_agent_mcp。"""
    env = os.environ.get("MULT_AGENT_MCP_HOME", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    return Path.home() / ".mult_agent_mcp"


MULT_AGENT_MCP_HOME = _resolve_mcp_home()

# 确保目录存在
MULT_AGENT_MCP_HOME.mkdir(parents=True, exist_ok=True)


# ============================================================
# 派生路径常量
# ============================================================

# 团队数据文件
DATA_FILE = MULT_AGENT_MCP_HOME / "teams_data.json"

# 共享上下文根目录（每个团队的上下文缓存在 {CONTEXTS_DIR}/{team}/）
CONTEXTS_DIR = MULT_AGENT_MCP_HOME / "contexts"

# MCP 守护进程管理文件
SERVER_PID_FILE = MULT_AGENT_MCP_HOME / "mcp_server.pid"
SERVER_LOG_FILE = MULT_AGENT_MCP_HOME / "mcp_server.log"

# ============================================================
# 项目级路径（不迁移，与 Git 仓库绑定）
# ============================================================

TEAM_WORKSPACES_DIR = PROJECT_DIR / ".team_workspaces"
SHARE_WORKSPACE_DIR = PROJECT_DIR / "share_work_space"

# 旧的共享上下文目录（向后兼容：如果旧目录有数据且新目录为空，回退使用）
OLD_SHARE_CONTEXT_DIR = PROJECT_DIR / "share_context_space"


# ============================================================
# 环境变量约定
# ============================================================

def context_base_dir() -> Path:
    """
    返回共享上下文的根目录。
    优先使用 MULT_AGENT_MCP_CONTEXT_DIR 环境变量；
    其次使用 ~/.mult_agent_mcp/contexts/。
    """
    env = os.environ.get("MULT_AGENT_MCP_CONTEXT_DIR", "").strip()
    if env:
        return Path(env).expanduser().resolve()
    CONTEXTS_DIR.mkdir(parents=True, exist_ok=True)
    return CONTEXTS_DIR


def _team_context_dir(team_name: str, team_info: dict | None = None) -> Path:
    """
    解析指定团队的共享上下文目录。
    优先级: team_info['context_dir'] > context_base_dir()/team_name
    """
    if team_info and team_info.get("context_dir"):
        return Path(team_info["context_dir"]).expanduser().resolve()
    d = context_base_dir() / team_name
    d.mkdir(parents=True, exist_ok=True)
    return d


# ============================================================
# 迁移逻辑 — 旧 PROJECT_DIR 数据 → ~/.mult_agent_mcp/
# ============================================================

def _migrate_old_data() -> bool:
    """
    将 PROJECT_DIR/teams_data.json 中的旧数据合并到 ~/.mult_agent_mcp/。

    迁移内容:
      1. DATA_FILE 不存在时复制旧 teams_data.json
      2. DATA_FILE 已存在时只合并缺失团队/成员/字段，不覆盖新位置已有数据
      3. share_context_space/ → contexts/（仅复制，不删除旧数据）

    返回 True 表示执行了迁移。
    """
    old_data = PROJECT_DIR / "teams_data.json"
    if not old_data.exists():
        return False

    import json

    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_FILE.exists():
        shutil.copy2(str(old_data), str(DATA_FILE))

    try:
        with open(old_data, "r", encoding="utf-8") as f:
            legacy_data = json.load(f)
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return True

    changed = False
    for team_name, legacy_team in legacy_data.get("teams", {}).items():
        teams = data.setdefault("teams", {})
        if team_name not in teams:
            teams[team_name] = legacy_team
            changed = True
            continue

        team = teams[team_name]
        for key, value in legacy_team.items():
            if key == "members":
                members = team.setdefault("members", {})
                for member_name, legacy_member in value.items():
                    if member_name not in members:
                        members[member_name] = legacy_member
                        changed = True
                    else:
                        for member_key, member_value in legacy_member.items():
                            if member_key not in members[member_name]:
                                members[member_name][member_key] = member_value
                                changed = True
            elif key not in team:
                team[key] = value
                changed = True

    old_context_base = str(OLD_SHARE_CONTEXT_DIR)
    for team_name, team in data.get("teams", {}).items():
        old_context = team.get("context_dir", "")
        if old_context and old_context.startswith(old_context_base):
            new_context = str(CONTEXTS_DIR / team_name)
            team["context_dir"] = new_context
            changed = True

    if changed:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    old_contexts = PROJECT_DIR / "share_context_space"
    if old_contexts.exists() and not any(CONTEXTS_DIR.iterdir()):
        try:
            for item in old_contexts.iterdir():
                if item.is_dir():
                    shutil.copytree(str(item), str(CONTEXTS_DIR / item.name), dirs_exist_ok=True)
                else:
                    shutil.copy2(str(item), str(CONTEXTS_DIR / item.name))
        except Exception:
            pass  # 非关键

    return True


# 模块加载时自动尝试迁移（幂等操作）
_MIGRATED = _migrate_old_data()


# ============================================================
# 便捷函数（兼容旧代码的字符串路径用法）
# ============================================================

def server_url() -> str:
    """返回 MCP 服务器的 HTTP URL。"""
    port = os.environ.get("FASTMCP_PORT", "8000")
    return f"http://localhost:{port}/mcp"


def default_workspace_dir() -> str:
    """
    返回默认工作目录。
    优先使用环境变量中的真实工作目录，跳过内部 .team_workspaces 路径。
    """
    def _is_internal(path: str) -> bool:
        try:
            root = str(TEAM_WORKSPACES_DIR.resolve())
            candidate = str(Path(path).resolve())
            return candidate == root or candidate.startswith(root + os.sep)
        except OSError:
            return False

    for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD"):
        candidate = os.environ.get(key, "").strip()
        if candidate and os.path.isdir(candidate) and not _is_internal(candidate):
            return str(Path(candidate).resolve())
    return str(PROJECT_DIR.resolve())
