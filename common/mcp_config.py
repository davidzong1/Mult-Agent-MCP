"""
Multi-Agent MCP — 共享 MCP 配置模块（Claude + Codex）
=======================================================

供 MCP Server 与 TUI 共用的 Claude Code / Codex CLI MCP 连接配置函数。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from common.config import server_url, PROJECT_DIR
from common.data_layer import load_data
from common.tmux_utils import run_command

MCP_SERVER_NAME = "mult-agent-mcp"
CLAUDE_GLOBAL_CONFIG_PATH = Path.home() / ".claude.json"
CLAUDE_LEADER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__leader_*",
    "mcp__mult_agent_mcp__leader_*",
]
CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS = [
    "mcp__mult-agent-mcp__member_*",
    "mcp__mult_agent_mcp__member_*",
]


# ============================================================
# Claude Code MCP 配置
# ============================================================

def claude_mcp_config() -> dict:
    """Return the expected Claude Code MCP config for this server."""
    return {"mcpServers": {MCP_SERVER_NAME: claude_mcp_server_config()}}


def claude_mcp_server_config() -> dict:
    """Return the expected single-server Claude Code MCP entry."""
    return {
        "type": "http",
        "url": server_url(),
    }


def _validate_claude_server_config(server: object) -> tuple[bool, str]:
    if not isinstance(server, dict):
        return False, "server 配置缺失"
    expected_url = server_url()
    current_type = server.get("type")
    current_url = server.get("url")
    if current_type != "http":
        return False, f"type 不匹配（当前 {current_type!r}，应为 'http'）"
    if current_url != expected_url:
        return False, f"URL 不匹配（当前 {current_url or '空'}，应为 {expected_url}）"
    return True, "ok"


def _claude_project_entry(data: dict, team_dir: str | Path | None) -> dict | None:
    if team_dir is None:
        return None
    projects = data.get("projects")
    if not isinstance(projects, dict):
        return None
    return projects.get(str(Path(team_dir).expanduser().resolve()))


def claude_global_mcp_status(
    config_path: str | Path | None = None,
    team_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """检查全局 ~/.claude.json 是否存在会覆盖项目配置的同名冲突。"""
    path = Path(config_path) if config_path is not None else CLAUDE_GLOBAL_CONFIG_PATH
    if not path.exists():
        return True, "全局 Claude 配置不存在"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"全局 Claude 配置无法解析: {e}"

    servers = data.get("mcpServers")
    found = False
    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        found = True
        ok, message = _validate_claude_server_config(servers.get(MCP_SERVER_NAME))
        if not ok:
            return False, f"全局 Claude MCP 配置冲突: {message}"

    project_entry = _claude_project_entry(data, team_dir)
    project_servers = project_entry.get("mcpServers") if isinstance(project_entry, dict) else None
    if isinstance(project_servers, dict) and MCP_SERVER_NAME in project_servers:
        found = True
        ok, message = _validate_claude_server_config(project_servers.get(MCP_SERVER_NAME))
        if not ok:
            return False, f"项目 Claude MCP 配置冲突: {message}"

    if found:
        return True, "全局 Claude MCP 配置已匹配"
    return True, "未发现全局同名 MCP 配置"


def repair_claude_global_mcp_if_conflicting(
    config_path: str | Path | None = None,
    team_dir: str | Path | None = None,
) -> tuple[bool, str]:
    """修复会覆盖项目 .claude/mcp.json 的全局同名 MCP 配置。"""
    path = Path(config_path) if config_path is not None else CLAUDE_GLOBAL_CONFIG_PATH
    if not path.exists():
        return True, "全局 Claude 配置不存在"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"全局 Claude 配置无法解析: {e}"

    servers = data.get("mcpServers")

    changed = False
    messages: list[str] = []

    if isinstance(servers, dict) and MCP_SERVER_NAME in servers:
        ok, message = _validate_claude_server_config(servers.get(MCP_SERVER_NAME))
        if not ok:
            servers[MCP_SERVER_NAME] = claude_mcp_server_config()
            changed = True
            messages.append(f"全局 Claude MCP 配置: {message}")

    project_entry = _claude_project_entry(data, team_dir)
    project_servers = project_entry.get("mcpServers") if isinstance(project_entry, dict) else None
    if isinstance(project_servers, dict) and MCP_SERVER_NAME in project_servers:
        ok, message = _validate_claude_server_config(project_servers.get(MCP_SERVER_NAME))
        if not ok:
            project_servers[MCP_SERVER_NAME] = claude_mcp_server_config()
            changed = True
            messages.append(f"项目 Claude MCP 配置: {message}")

    if not changed:
        return True, "全局 Claude MCP 配置已匹配"

    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True, "已修复 " + "；".join(messages)

def claude_mcp_json_path(team_dir: str | Path) -> Path:
    """Claude 的 MCP 配置文件路径（.claude/mcp.json）。"""
    claude_dir = Path(team_dir) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir / "mcp.json"


def write_claude_mcp(team_dir: str | Path) -> Path:
    """为 Claude Code 写入 .claude/mcp.json，返回写入路径。"""
    mcp_json = claude_mcp_json_path(team_dir)
    mcp_json.write_text(
        json.dumps(claude_mcp_config(), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    ok, message = repair_claude_global_mcp_if_conflicting(team_dir=team_dir)
    if not ok:
        raise RuntimeError(message)
    return mcp_json


def claude_mcp_status(team_dir: str | Path) -> tuple[bool, str]:
    """检查 Claude Code MCP 配置是否为当前 streamable-http /mcp 配置。"""
    mcp_json = Path(team_dir) / ".claude" / "mcp.json"
    if not mcp_json.exists():
        return False, "未配置"

    try:
        data = json.loads(mcp_json.read_text(encoding="utf-8"))
    except Exception as e:
        return False, f"配置文件无法解析: {e}"

    server = data.get("mcpServers", {}).get(MCP_SERVER_NAME)
    if not isinstance(server, dict):
        if "teamMCP" in data:
            return False, "旧 teamMCP 配置格式，需要迁移到 mcpServers"
        return False, f"缺少 mcpServers.{MCP_SERVER_NAME}"

    ok, message = _validate_claude_server_config(server)
    if not ok:
        return False, message

    ok, message = claude_global_mcp_status(team_dir=team_dir)
    if not ok:
        return False, message

    return True, str(mcp_json)


def claude_mcp_configured(team_dir: str | Path) -> bool:
    """检查指定目录的 Claude Code MCP 是否已正确配置。"""
    ok, _ = claude_mcp_status(team_dir)
    return ok


def claude_settings_json_path(team_dir: str | Path) -> Path:
    """Claude Code 的 settings.json 路径（权限预配置）。"""
    claude_dir = Path(team_dir) / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    return claude_dir / "settings.json"


def write_claude_permissions(
    team_dir: str | Path,
    *,
    dangerously_skip: bool = False,
    allow_patterns: list[str] | None = None,
    additional_dirs: list[str] | None = None,
) -> Path:
    """为团队的 Claude Code 成员预配置权限策略，写入 .claude/settings.json。

    Args:
        team_dir: 团队工作目录路径
        dangerously_skip: 跳过所有权限检查（生产环境中慎用）
        allow_patterns: 额外允许的工具模式列表
        additional_dirs: 额外允许访问的目录列表
    """
    settings_path = claude_settings_json_path(team_dir)
    team_dir_str = str(Path(team_dir).resolve())

    permissions_config: dict = {}

    if dangerously_skip:
        permissions_config["allow-dangerously-skip-permissions"] = True
    else:
        allow: list[str] = list(allow_patterns or [])
        allow.extend([
            f"Edit({team_dir_str}/*)",
            f"Write({team_dir_str}/*)",
            "Bash(git:*)",
            *CLAUDE_MEMBER_MCP_TOOL_ALLOW_PATTERNS,
        ])
        if additional_dirs:
            for d in additional_dirs:
                allow.extend([
                    f"Edit({d}/*)",
                    f"Write({d}/*)",
                ])
        permissions_config["allow"] = allow

    settings = {"permissions": permissions_config}
    settings_path.write_text(
        json.dumps(settings, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return settings_path


def configure_claude_mcp(team_name: str, team_dir: str | Path) -> str:
    """为团队配置 Claude MCP。返回状态描述。"""
    path = write_claude_mcp(team_dir)
    return f"✅ {team_name} → {path}"


def configure_all_claude_mcp() -> list[tuple[str, bool, str]]:
    """为所有团队配置 Claude MCP。返回 [(team_name, ok, msg), ...]。"""
    results = []
    for name, info in load_data().get("teams", {}).items():
        team_dir = info.get("workspace_dir") or str(PROJECT_DIR)
        try:
            path = write_claude_mcp(team_dir)
            results.append((name, True, f"✅ {path}"))
        except Exception as e:
            results.append((name, False, f"❌ {e}"))
    return results


# ============================================================
# Codex CLI MCP 配置
# ============================================================

CODEX_CONFIG_PATH = Path.home() / ".codex" / "config.toml"


def _codex_mcp_configured() -> bool:
    """检查 Codex 全局配置中是否已注册此 MCP server。"""
    if not CODEX_CONFIG_PATH.exists():
        return False
    content = CODEX_CONFIG_PATH.read_text(encoding="utf-8")
    return f"[mcp_servers.{MCP_SERVER_NAME}]" in content


def _codex_mcp_url() -> str:
    """读取 Codex 配置中的 MCP server URL。"""
    if not CODEX_CONFIG_PATH.exists():
        return ""
    lines = CODEX_CONFIG_PATH.read_text(encoding="utf-8").splitlines()
    in_section = False
    for line in lines:
        stripped = line.strip()
        if stripped == f"[mcp_servers.{MCP_SERVER_NAME}]":
            in_section = True
            continue
        if in_section and stripped.startswith("["):
            return ""
        if in_section and stripped.startswith("url"):
            _, _, value = stripped.partition("=")
            return value.strip().strip('"').strip("'")
    return ""


def _write_codex_mcp_config() -> None:
    """直接写入/更新 codex config.toml 中 MCP server 配置。"""
    CODEX_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if CODEX_CONFIG_PATH.exists():
        lines = CODEX_CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)

    header = f"[mcp_servers.{MCP_SERVER_NAME}]"
    url_line = f'url = "{server_url()}"\n'

    result = []
    in_section = False
    replaced = False
    for line in lines:
        stripped = line.strip()
        if stripped == header:
            if not replaced:
                result.extend([f"\n{header}\n", url_line])
                replaced = True
            in_section = True
            continue
        if in_section and stripped.startswith("["):
            in_section = False
            result.append(line)
            continue
        if in_section:
            continue
        result.append(line)

    if not replaced:
        if result and result[-1].strip():
            result.append("\n")
        result.extend([f"{header}\n", url_line])

    CODEX_CONFIG_PATH.write_text("".join(result), encoding="utf-8")


def configure_codex_mcp() -> tuple[bool, str]:
    """注册此 MCP 服务器到 Codex 全局配置。返回 (ok, msg)。"""
    target_url = server_url()

    if _codex_mcp_configured():
        current = _codex_mcp_url()
        if current == target_url:
            return True, "Codex MCP 已注册（无需重复）"
        try:
            _write_codex_mcp_config()
            return True, f"✅ Codex MCP URL 已修正: {current or '空'} → {target_url}"
        except Exception as e:
            return False, f"❌ 修正失败: {e}\n💡 手动: codex mcp remove {MCP_SERVER_NAME} && codex mcp add {MCP_SERVER_NAME} --url {target_url}"

    # 方式 1: codex mcp add CLI
    rc, _, _ = run_command([
        "codex", "mcp", "add", MCP_SERVER_NAME,
        "--url", target_url,
    ], timeout=15)
    if rc == 0:
        return True, "✅ 已通过 CLI 注册"

    # 方式 2: 直接写配置文件
    try:
        _write_codex_mcp_config()
        return True, "✅ 已写入 ~/.codex/config.toml"
    except Exception as e:
        return False, f"❌ 配置失败: {e}\n💡 手动: codex mcp add {MCP_SERVER_NAME} --url {target_url}"


def remove_codex_mcp() -> str:
    """从 Codex 配置中移除此 MCP 服务器。返回状态描述。"""
    if not _codex_mcp_configured():
        return "not_registered"

    # 方式 1: CLI
    rc, _, _ = run_command([
        "codex", "mcp", "remove", MCP_SERVER_NAME,
    ], timeout=10)
    if rc == 0:
        return "✅ Codex MCP 已通过 CLI 移除。"

    # 方式 2: 直接编辑
    if not CODEX_CONFIG_PATH.exists():
        return "not_registered"

    lines = CODEX_CONFIG_PATH.read_text(encoding="utf-8").splitlines(keepends=True)
    in_section = False
    result = []
    for line in lines:
        if line.strip() == f"[mcp_servers.{MCP_SERVER_NAME}]":
            in_section = True
            continue
        if in_section:
            if line.strip().startswith("[") and line.strip() != f"[mcp_servers.{MCP_SERVER_NAME}]":
                in_section = False
                result.append(line)
            continue
        result.append(line)

    CODEX_CONFIG_PATH.write_text("".join(result), encoding="utf-8")
    return "✅ Codex MCP 已从配置中移除。"


def codex_mcp_registered() -> bool:
    """检查 Codex MCP 是否已注册。"""
    return _codex_mcp_configured()


def ensure_agent_mcp(team_dir: str | Path, agent_cmd: str) -> str:
    """根据 agent 类型确保 MCP 配置已就绪。返回配置摘要。"""
    from common.tmux_utils import agent_type

    atype = agent_type(agent_cmd)
    results = []

    if atype == "claude":
        path = write_claude_mcp(team_dir)
        results.append(f"📄 Claude MCP → {path}")
    elif atype == "codex":
        ok, msg = configure_codex_mcp()
        if ok and "无需重复" in msg:
            results.append("📄 Codex MCP → 已注册（全局配置）")
        else:
            results.append(f"📄 Codex MCP → {msg}")
    else:
        # 未知 agent，两种都尝试
        write_claude_mcp(team_dir)
        configure_codex_mcp()
        results.append("📄 已同时尝试 Claude 和 Codex MCP 配置。")

    return "\n".join(results)
