"""Leader recovery helpers shared by MCP server and TUI prompt builders."""

from __future__ import annotations

MAX_PROMPT_MEMBER_TASKS = 8
MAX_PROMPT_TASK_CHARS = 500
MAX_PENDING_REPORTS = 20

# ---- leader_checkpoint 基础 ----
# team 级结构化进度快照：恢复时优先于 last_task 摘要渲染，是 leader 跨重启
# 承接总体方向（目标/边界/决策/分工/依赖/剩余/证据/下一步）的权威来源。
# epoch 单调递增（每次写入 +1），version 为结构 schema 版本。
LEADER_CHECKPOINT_VERSION = 1
LEADER_CHECKPOINT_FIELDS = (
    "goal",
    "boundaries",
    "decisions",
    "plan",
    "assignments",
    "dependencies",
    "deadline",
    "remaining",
    "evidence",
    "next_actions",
)
# 单字段在恢复 prompt 中渲染的最大字符数（防止超长证据刷屏）
MAX_CHECKPOINT_FIELD_CHARS = 400
# checkpoint 中保留的最近证据条数（报告/完成事件追加）
MAX_CHECKPOINT_EVIDENCE = 20

# monitor idle 推断完成时写入的合成回报事件名（成员亲笔回报为 "member_report"）。
MONITOR_INFERRED_EVENT = "monitor_inferred_completion"


def report_origin_prefix(report: dict) -> str:
    """monitor 推断完成的合成回报必须一眼可区分于成员亲笔回报。

    monitor 只看终端 idle 就判定完成，可能误判（成员在思考、等授权、
    或刚崩溃）。渲染不加区分的话，leader 会把机器猜测当成员承诺来读。
    leader_activate 与恢复 prompt 两处渲染共用此函数，避免措辞漂移。
    """
    if (report or {}).get("event") == MONITOR_INFERRED_EVENT:
        return "⚠️[monitor 推断] "
    return ""


def claim_keeps_tmux_leader(
    team: dict,
    *,
    session_alive: bool,
    window_alive: bool,
) -> bool:
    """claim_leader 是否应保持受管 tmux leader 的 tmux 语义（不覆盖为 direct）。

    受管 tmux leader = 成员表中存在 role='leader' 的记录（由 set_leader /
    launch_team_terminals 管理）；存活 = 该 leader 在团队 tmux session 中有
    真实窗口。两者同时成立时，同名 claim 不得把 leader_type 覆盖为 direct——
    否则产生元数据撕裂：leader_type='direct' 但 leader 仍指向一个带活 tmux
    窗口的成员名，使 _is_direct_leader_member() 纯名字匹配误判、leader 授权/
    列表误标该成员为 direct leader。

    真正外部会话接管（旧 leader 终端已关闭 / 非受管）仍走 direct 路径。
    MCP claim_leader 与 TUI action_claim_leader 共用此判定，避免两处语义漂移。
    """
    if not team or team.get("leader_type") != "tmux":
        return False
    leader = team.get("leader", "")
    if not leader or not (session_alive and window_alive):
        return False
    return team.get("members", {}).get(leader, {}).get("role") == "leader"


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
    if pending_leader_reports(team):
        return True
    return bool(active_member_tasks(team))


def leader_recovery_mode(team: dict) -> str:
    """Return resume when work should continue, otherwise standby."""
    return "resume" if leader_has_unfinished_work(team) else "standby"


def member_pending_task(team: dict, member_name: str) -> dict | None:
    """Return the member's persisted unfinished task snapshot, or None.

    Snapshot carries the task/context so a re-entering member can resume
    without depending on the leader's terminal injection. This is the core
    primitive behind the member task resume (成员任务续跑) flow.
    """
    member = team.get("members", {}).get(member_name)
    if not member:
        return None
    if member.get("last_task_completed", True):
        return None
    last_task = (member.get("last_task") or "").strip()
    if not last_task:
        return None
    return {
        "member_name": member_name,
        "team_leader": team.get("leader", ""),
        "role": member.get("role") or "member",
        "agent": _member_agent(team, member),
        "task": last_task,
        "context": (member.get("last_context") or "").strip(),
    }


def pending_leader_reports(team: dict) -> list[dict]:
    """Return member reports made while the leader was away/resting."""
    reports = team.get("leader_pending_reports")
    if not isinstance(reports, list):
        return []
    return [r for r in reports if isinstance(r, dict)]


def append_leader_pending_report(team: dict, entry: dict) -> list[dict]:
    """Append a member report to the leader's pending queue (bounded, idempotent).

    Idempotency (S2): when ``entry`` carries a ``report_id`` and a pending entry
    with the same report_id already exists, the append is skipped — a retried /
    duplicate report never double-delivers. Every appended entry defaults
    ``delivered=False`` (S3): the wakeup path marks an injected report delivered
    without consuming it; ``leader_activate`` drain remains the final ACK.
    """
    reports = pending_leader_reports(team)
    rid = (entry or {}).get("report_id")
    if rid:
        for existing in reports:
            if existing.get("report_id") == rid:
                return reports  # 幂等：同 report_id 已存在则跳过
    new_entry = dict(entry)
    new_entry.setdefault("delivered", False)
    reports.append(new_entry)
    team["leader_pending_reports"] = reports[-MAX_PENDING_REPORTS:]
    return team["leader_pending_reports"]


def undelivered_pending_reports(team: dict) -> list[dict]:
    """Reports not yet injected into the leader's terminal (delivered=False)."""
    return [r for r in pending_leader_reports(team) if not r.get("delivered")]


def mark_pending_reports_delivered(team: dict, report_ids) -> int:
    """Mark pending reports with the given report_ids as delivered (in place).

    S3: 注入成功 ≠ 消费。delivered 标记区分"已投递未确认"（leader 只需
    leader_activate 收讫，不再重放）与"待投递"（系统尚未送达）。调用方须已
    持有 TEAM_DATA_LOCK（或在 _update_team_data 的 updater 内）。
    """
    if not report_ids:
        return 0
    report_ids = set(report_ids)
    reports = pending_leader_reports(team)
    marked = 0
    for r in reports:
        if r.get("report_id") in report_ids and not r.get("delivered"):
            r["delivered"] = True
            marked += 1
    return marked


def build_leader_pending_reports_section(team_name: str, team: dict) -> list[str]:
    """Prompt lines listing reports members made while the leader was away/resting."""
    reports = pending_leader_reports(team)
    if not reports:
        return []
    lines = ["", "成员回报待处理(leader 离开/休息期间的上报):"]
    for i, report in enumerate(reports, 1):
        member = report.get("member") or "unknown"
        result = _compact_inline(report.get("result") or "", MAX_PROMPT_TASK_CHARS)
        ts = (report.get("timestamp") or "")[:19]
        # S4：投递/ACK 状态只读数据层——已投递未确认=leader 只需 activate 收讫，
        # 待投递=系统尚未送达；渲染不依赖成员对话窗/终端残留。
        status = "[已投递未确认]" if report.get("delivered") else "[待投递]"
        line = f"  {i}. [{ts}] {status} {report_origin_prefix(report)}{member}: {result}"
        if report.get("artifact_path"):
            line += f" | artifact: {_compact_inline(report['artifact_path'], 120)}"
        lines.append(line)
    lines.append(
        f"  可用 leader_activate('{team_name}') 查看并确认(会清空待处理回报),"
        "或 leader_get_recovery_context 只读查看。"
    )
    return lines


# ============================================================
# leader_checkpoint：结构化恢复依据
# ============================================================

def empty_leader_checkpoint() -> dict:
    """返回 leader_checkpoint 的空白基线（epoch=0，尚未写入）。"""
    return {
        "version": LEADER_CHECKPOINT_VERSION,
        "epoch": 0,
        "goal": "",
        "boundaries": [],
        "decisions": [],
        "plan": [],
        "assignments": {},
        "dependencies": [],
        "deadline": "",
        "remaining": [],
        "evidence": [],
        "next_actions": [],
        "status": "",  # "" | "active" | "completed"
        "source": "",  # 最近一次写入来源: task_start|assign|report|complete|leader_checkpoint_set
        "updated_by": "",
        "updated_ts": "",
    }


def leader_checkpoint(team: dict) -> dict:
    """读取团队的 leader_checkpoint dict（缺失/损坏时返回空 dict，不抛异常）。"""
    cp = team.get("leader_checkpoint") if isinstance(team, dict) else None
    if not isinstance(cp, dict):
        return {}
    return cp


def checkpoint_epoch(cp: dict) -> int:
    """安全解析 checkpoint epoch：损坏/非整数/缺失返回 0（视为未初始化）。

    磁盘上的 leader_checkpoint 可能被写坏（epoch="x"）或在 JSON 往返后变成
    浮点（epoch=2.0）。恢复渲染 / 漂移判定 / 旧 epoch 校验必须优雅降级，
    绝不 int('x') 抛 ValueError 击穿 leader_activate / leader_get_recovery_context。
    """
    try:
        return int(cp.get("epoch") or 0)
    except (TypeError, ValueError):
        return 0


def leader_checkpoint_drift(team: dict) -> list[str]:
    """检测持久化 checkpoint 与团队实时状态之间的明显漂移。

    保守策略：只标记"清晰、可行动"的矛盾，避免因措辞差异产生误报——
    目标比较允许子串包含（leader 可能重述任务）；分工比较只对双方都非空
    且互不包含的差异报警；完成状态漂移仅在团队已标记完成但 checkpoint
    仍残留 remaining/next_actions 时报警。

    返回人类可读的漂移原因列表；空列表 = 无漂移（或尚无 checkpoint）。
    恢复渲染侧据此禁止自动再分配（见 build_leader_checkpoint_drift_section）。
    """
    cp = leader_checkpoint(team)
    if not cp or checkpoint_epoch(cp) < 1:
        return []
    reasons: list[str] = []

    goal = str(cp.get("goal") or "").strip()
    leader_task = str(team.get("leader_last_task") or "").strip()
    if goal:
        if not leader_task:
            reasons.append("checkpoint.goal 已记录但 leader_last_task 为空，方向记录与任务记录冲突")
        elif leader_task != goal and goal not in leader_task and leader_task not in goal:
            reasons.append("checkpoint.goal 与 leader_last_task 内容不一致")

    assignments = cp.get("assignments")
    if isinstance(assignments, dict):
        members = team.get("members", {})
        for name, asg in assignments.items():
            if not isinstance(asg, dict) or asg.get("status") == "completed":
                continue
            asg_task = str(asg.get("task") or "").strip()
            cur = str((members.get(name) or {}).get("last_task") or "").strip()
            if asg_task and cur and cur != asg_task and asg_task not in cur and cur not in asg_task:
                reasons.append(f"成员 {name}: checkpoint 分工与当前 last_task 不一致")

    done = team.get("leader_last_task_completed", True)
    if done and (cp.get("remaining") or cp.get("next_actions")):
        reasons.append("团队已标记总任务完成，但 checkpoint 仍有剩余工作/下一步动作未清空")

    return reasons


def leader_checkpoint_high_drift(team: dict) -> list[str]:
    """过滤出会令自动再分配不安全的高优先级漂移原因。

    HIGH = 方向冲突（checkpoint.goal 与 leader_last_task 不一致/缺失）与分工矛盾
    （checkpoint.assignments 与成员 last_task 不一致）——这些会令"重新分配/重发任务"
    盲目执行；done-but-remaining 残留为 LOW（仅需收尾清理，不影响分配安全）。
    无 checkpoint 或无 HIGH 漂移时返回空列表。
    """
    return [
        r for r in leader_checkpoint_drift(team)
        if "goal" in r or "分工" in r or "leader_last_task 为空" in r
    ]


def build_leader_checkpoint_section(team: dict) -> list[str]:
    """渲染结构化 leader_checkpoint（恢复时优先显示）。无 checkpoint 时返回空。"""
    cp = leader_checkpoint(team)
    if not cp or checkpoint_epoch(cp) < 1:
        return []
    lines = [
        "",
        "📌 Leader Checkpoint（结构化恢复依据，优先于 last_task 摘要）:",
        f"  - epoch: {checkpoint_epoch(cp)} | version: {cp.get('version')} | "
        f"更新: {str(cp.get('updated_ts') or '')[:19]} | source: {cp.get('source') or 'unknown'}",
    ]

    def _list_field(key: str, label: str) -> None:
        val = cp.get(key)
        items: list[str] = []
        if isinstance(val, list):
            items = [str(x) for x in val if str(x).strip()]
        elif isinstance(val, str) and val.strip():
            items = [val.strip()]
        if items:
            joined = "；".join(items)
            if len(joined) > MAX_CHECKPOINT_FIELD_CHARS:
                joined = joined[: MAX_CHECKPOINT_FIELD_CHARS - 3] + "..."
            lines.append(f"  - {label}: {joined}")

    goal = str(cp.get("goal") or "").strip()
    if goal:
        lines.append(f"  - 目标: {_compact_inline(goal, MAX_CHECKPOINT_FIELD_CHARS)}")
    deadline = str(cp.get("deadline") or "").strip()
    if deadline:
        lines.append(f"  - 截止: {_compact_inline(deadline, 200)}")
    _list_field("boundaries", "边界")
    _list_field("decisions", "已决策")
    _list_field("plan", "计划")
    _list_field("dependencies", "依赖")
    _list_field("remaining", "剩余工作")
    _list_field("next_actions", "下一步")

    assignments = cp.get("assignments")
    if isinstance(assignments, dict) and assignments:
        lines.append("  - 成员分工:")
        for name, asg in list(assignments.items())[:MAX_PROMPT_MEMBER_TASKS]:
            if not isinstance(asg, dict):
                continue
            status = asg.get("status") or "assigned"
            task = _compact_inline(str(asg.get("task") or ""), 200)
            lines.append(f"      * {name} [{status}]: {task or '(未记录)'}")

    evidence = cp.get("evidence")
    if isinstance(evidence, list) and evidence:
        lines.append("  - 最近证据:")
        for ev in evidence[-3:]:
            if not isinstance(ev, dict):
                continue
            member = ev.get("member") or "unknown"
            ts = str(ev.get("timestamp") or "")[:19]
            result = _compact_inline(str(ev.get("result") or ""), 160)
            lines.append(f"      * [{ts}] {member}: {result or '(empty)'}")
    return lines


def build_leader_checkpoint_drift_section(team: dict) -> list[str]:
    """渲染漂移警告：检测到明显漂移时禁止自动再分配，需人工确认。"""
    drift = leader_checkpoint_drift(team)
    if not drift:
        return []
    lines = [
        "",
        "⛔ leader_checkpoint 漂移警告（禁止自动再分配）:",
    ]
    for reason in drift:
        lines.append(f"  - {reason}")
    lines.append(
        "  恢复后必须先向用户确认当前方向（用 leader_checkpoint_set 校正 checkpoint），"
        "再决定是否继续分配；人工确认前不得自动重派任务。"
    )
    return lines


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
    # 漂移检测优先：明显漂移时只渲染 checkpoint + 警告，不给出"继续推进/自动重派"
    # 的默认指引——恢复必须先经人工确认方向。
    drift = leader_checkpoint_drift(team)
    lines = [
        "",
        "Leader 恢复状态:",
    ]
    lines.extend(build_leader_checkpoint_section(team))
    if drift:
        lines.extend(build_leader_checkpoint_drift_section(team))
        lines.extend(build_leader_pending_reports_section(team_name, team))
        lines.extend([
            f"- 共享工作目录: {team_dir}",
            f"- 共享上下文区: {share_dir}",
            f"- 完整恢复摘要: leader_get_recovery_context('{team_name}')",
        ])
        return lines

    leader_task = (team.get("leader_last_task") or "").strip()
    leader_context = (team.get("leader_last_context") or "").strip()
    leader_done = team.get("leader_last_task_completed", True)
    active_tasks = active_member_tasks(team)
    mode = leader_recovery_mode(team)

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

    lines.extend(build_leader_pending_reports_section(team_name, team))
    lines.extend([
        f"- 共享工作目录: {team_dir}",
        f"- 共享上下文区: {share_dir}",
    ])
    return lines
