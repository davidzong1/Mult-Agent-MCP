"""Leader recovery helpers shared by MCP server and TUI prompt builders."""

from __future__ import annotations

MAX_PROMPT_MEMBER_TASKS = 8
MAX_PROMPT_TASK_CHARS = 500


def _default_agent(team: dict) -> str:
    return (team.get("default_agent") or "claude").strip() or "claude"


def _member_agent(team: dict, member: dict) -> str:
    return (member.get("agent") or _default_agent(team)).strip() or "claude"


def active_member_tasks(team: dict) -> list[tuple[str, dict]]:
    """Return non-leader members with persisted unfinished tasks."""
    leader = team.get("leader", "")
    active = []
    for name, member in team.get("members", {}).items():
        if name == leader:
            continue
        if member.get("last_task") and not member.get("last_task_completed", True):
            active.append((name, member))
    return active


def leader_has_unfinished_work(team: dict) -> bool:
    if team.get("leader_last_task") and not team.get("leader_last_task_completed", True):
        return True
    return bool(active_member_tasks(team))


def leader_recovery_mode(team: dict) -> str:
    """Return resume when work should continue, otherwise standby."""
    return "resume" if leader_has_unfinished_work(team) else "standby"


def _compact_inline(text: str, limit: int = MAX_PROMPT_TASK_CHARS) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    keep = max(1, limit - 15)
    return f"{text[:keep]} ...[truncated]"


def build_leader_recovery_section(
    team_name: str,
    team: dict,
    team_dir: str,
    share_dir: str,
) -> list[str]:
    """Build the leader prompt section that tells a re-entered leader what to do."""
    leader_task = (team.get("leader_last_task") or "").strip()
    leader_context = (team.get("leader_last_context") or "").strip()
    leader_done = team.get("leader_last_task_completed", True)
    active_tasks = active_member_tasks(team)
    mode = leader_recovery_mode(team)

    lines = [
        "",
        "Leader 恢复状态:",
    ]
    if mode == "resume":
        lines.append("检测到未完成团队工作。你重新进入后必须先恢复上下文并继续推进，不要把自己当作新成员。")
        if leader_task and not leader_done:
            lines.append(f"- 未完成总任务: {_compact_inline(leader_task)}")
        if leader_context:
            lines.append(f"- 总任务上下文: {_compact_inline(leader_context, 240)}")
        if active_tasks:
            lines.append("- 未完成成员任务:")
            for name, member in active_tasks[:MAX_PROMPT_MEMBER_TASKS]:
                role = member.get("role") or "member"
                agent = _member_agent(team, member)
                task = _compact_inline(member.get("last_task") or "")
                context = _compact_inline(member.get("last_context") or "", 240)
                item = f"  * {name}(role={role}, agent={agent}): {task}"
                if context:
                    item += f" | context: {context}"
                lines.append(item)
            remaining = len(active_tasks) - MAX_PROMPT_MEMBER_TASKS
            if remaining > 0:
                lines.append(f"  * ... 另有 {remaining} 个未完成成员任务，请调用 leader_get_recovery_context 查看完整状态。")
        lines.extend([
            f"- 优先调用 leader_get_recovery_context('{team_name}') 获取完整恢复摘要和最近共享结果。",
            "- 如果当前 agent 会话的 MCP 工具列表尚未刷新而看不到该工具，先用 leader_list_team、leader_monitor_members 和 member_read_shared 继续恢复。",
            "- 根据成员状态继续协调；只在团队工作确实完成后调用 leader_mark_task_complete。",
        ])
    else:
        reason = "上次总任务已标记完成。" if leader_task and leader_done else "未发现已分配的未完成工作。"
        lines.extend([
            f"{reason}重新进入后进入正常待机状态，等待用户新任务。",
            f"- 如需复核历史结果，可读取共享上下文区: {share_dir}",
            "- 新任务到来后先调用 leader_list_team，再分配或广播。",
        ])

    lines.extend([
        f"- 共享工作目录: {team_dir}",
        f"- 共享上下文区: {share_dir}",
    ])
    return lines
