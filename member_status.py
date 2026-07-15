def format_member_activity_status(member_info: dict, terminal_alive: bool) -> tuple[str, str]:
    """Return a stable member status label and bucket from persisted task state."""
    has_task = bool(member_info.get("last_task", ""))
    task_completed = member_info.get("last_task_completed", True)
    recovery_count = member_info.get("recovery_count", 0)
    observed_state = member_info.get("last_observed_state", "")

    if terminal_alive:
        if member_info.get("role") == "leader":
            return "👑 leader", "leader"
        if observed_state == "approval":
            return "⏸ approval", "approval"
        if recovery_count > 0 and has_task and not task_completed:
            return "🔄 recovering", "recovering"
        if has_task and not task_completed:
            return "🟢 working", "working"
        return "💤 idle", "idle"

    if has_task and task_completed:
        return "😴 sleep", "sleep"
    return "⚫ dead", "dead"
