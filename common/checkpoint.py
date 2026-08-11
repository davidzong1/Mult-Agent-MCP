"""
Multi-Agent MCP — 成员任务 checkpoint 纯数据层 helper（P1 基础原语）
=====================================================================

在用户池配额换号 / 终端崩溃等中断场景下，"不从头重做"的前提是能把成员
任务的结构化进度持久化下来。现有原语只有扁平字符串（last_task /
last_context）与一次性的 markdown 死亡快照（_save_death_context_snapshot），
都没有"已完成步骤 / 当前步骤 / 产物哈希 / 续跑指令 / epoch / writer"这组
可校验、可幂等续跑的结构。

本模块提供成员任务 checkpoint 的**纯数据层**原语，定位与边界：

  - 只负责一个成员的任务 checkpoint 的读写校验；leader 编排 checkpoint、
    团队级单 epoch 合并、CLI --resume 接线、quota 分支改造**都不在本模块**。
  - 不改写 mult_agent_mcp.py / common/leader_recovery.py（避免与 coder
    的 leader 区域冲突）；调用方（未来 mult_agent_mcp）传入自己的数据锁。
  - 数据落盘在 teams_data.json 的 member["task_checkpoint"] 子文档，
    复用现有 atomic_json_write（0600 + os.replace + 测试 fail-fast 守卫），
    不新增文件、不涉及路径校验、随团队数据一并原子更新。

字段模型（与端到端设计 agents:{member:{...}} 对齐的子集）:

    task_id                任务唯一标识（member 维度）
    task                   原始任务全文（last_task 的冗余，resume 不依赖 last_task）
    task_summary           任务简述（可选，prompt 用）
    completed_steps        list[str] 已完成步骤（幂等追加）
    current_step           str 当前正在执行的步骤
    artifacts              {rel_path: md5_hex} 产物哈希快照
    remaining_instruction  str 续跑指令（剩余未完成工作的说明）
    epoch                  int 单调递增（每次 update 在锁内 +1）
    writer                 str 最后写入者（成员名）
    state                  "running" | "done"
    version                结构版本（CHECKPOINT_VERSION）
    created_ts / updated_ts  ISO 时间戳

幂等 verify-then-continue 语义：
    verify_then_continue(cp, expected_epoch, ...) 通过 = 该 checkpoint 仍是
    最新且 state=running，可以安全续跑；任何一次 update 都会让 epoch+1，
    旧 writer 的本地拷贝 verify 会因 epoch 不匹配而失败，从根上防止
    "旧上下文覆盖新进度"（防旧上下文覆盖 P0）。
    record_step_done 重复记录同一 step 是 no-op。

依赖方向：本模块仅依赖 common.data_layer + stdlib，不 import
mult_agent_mcp / leader_recovery，故可被 MCP server 与 TUI 共用而不成环。
"""

from __future__ import annotations

import hashlib
import os
import threading
from datetime import datetime
from typing import Callable, Optional

from common.data_layer import load_data, save_data

CHECKPOINT_VERSION = 1
MEMBER_CHECKPOINT_KEY = "task_checkpoint"
DEFAULT_STATE = "running"
_DONE_STATE = "done"

# 模块级默认锁：未显式传入锁时使用（幂等、可重入）。
_DEFAULT_LOCK = threading.RLock()

# 校验相关的合法取值
_VALID_STATES = {DEFAULT_STATE, _DONE_STATE}


# ============================================================
# 纯函数：结构校验 / epoch / 幂等步骤 / 产物哈希
# ============================================================

def empty_checkpoint(task_id: str, *, task: str = "", writer: str = "") -> dict:
    """构造一个合法的空 checkpoint（epoch=0，尚未持久化）。"""
    return {
        "task_id": task_id,
        "task": task or "",
        "task_summary": "",
        "completed_steps": [],
        "current_step": "",
        "artifacts": {},
        "remaining_instruction": "",
        "epoch": 0,
        "writer": writer,
        "state": DEFAULT_STATE,
        "version": CHECKPOINT_VERSION,
        "created_ts": "",
        "updated_ts": "",
    }


def next_epoch(current: Optional[dict]) -> int:
    """下一个 epoch：无 checkpoint 时从 1 开始，否则 current.epoch + 1。

    epoch 单调递增是"旧 writer 拒绝"的根：新写入必须比已持久化的 epoch 大，
    否则视为过期上下文，不允许覆盖。
    """
    if not isinstance(current, dict):
        return 1
    epoch = current.get("epoch")
    if not isinstance(epoch, int):
        return 1
    return epoch + 1


def validate_checkpoint(cp: Optional[dict]) -> tuple[bool, list[str]]:
    """校验 checkpoint 结构完整性，返回 (ok, errors)。

    任何非法输入返回 (False, [...]);合法返回 (True, [])。
    artifacts 值要求为 32 位小写十六进制 md5 哈希（由 hash_bytes/hash_file 产出）。
    """
    errors: list[str] = []
    if not isinstance(cp, dict):
        return False, ["checkpoint 必须是 dict"]

    task_id = cp.get("task_id")
    if not isinstance(task_id, str) or not task_id.strip():
        errors.append("task_id 必须是非空字符串")

    if "task" in cp and not isinstance(cp.get("task"), str):
        errors.append("task 必须是字符串")
    if "task_summary" in cp and not isinstance(cp.get("task_summary"), str):
        errors.append("task_summary 必须是字符串")

    steps = cp.get("completed_steps")
    if not isinstance(steps, list) or not all(isinstance(s, str) for s in steps):
        errors.append("completed_steps 必须是字符串列表")
    else:
        seen: set[str] = set()
        for s in steps:
            if s in seen:
                errors.append(f"completed_steps 含重复步骤: {s}")
            seen.add(s)

    if "current_step" in cp and not isinstance(cp.get("current_step"), str):
        errors.append("current_step 必须是字符串")
    if "remaining_instruction" in cp and not isinstance(cp.get("remaining_instruction"), str):
        errors.append("remaining_instruction 必须是字符串")

    artifacts = cp.get("artifacts")
    if not isinstance(artifacts, dict):
        errors.append("artifacts 必须是 dict")
    else:
        for path, digest in artifacts.items():
            if not isinstance(path, str) or not path.strip():
                errors.append("artifacts 的路径必须是非空字符串")
            if not isinstance(digest, str) or len(digest) != 32 or not _is_hex(digest):
                errors.append(f"artifacts['{path}'] 的哈希必须是 32 位十六进制 md5")

    epoch = cp.get("epoch")
    if not isinstance(epoch, int) or epoch < 0:
        errors.append("epoch 必须是非负整数")

    writer = cp.get("writer")
    if not isinstance(writer, str):
        errors.append("writer 必须是字符串")

    state = cp.get("state", DEFAULT_STATE)
    if state not in _VALID_STATES:
        errors.append(f"state 必须是 {sorted(_VALID_STATES)} 之一")

    version = cp.get("version")
    if version is not None and version != CHECKPOINT_VERSION:
        errors.append(f"version 必须是 {CHECKPOINT_VERSION}")

    for ts_key in ("created_ts", "updated_ts"):
        if ts_key in cp and not isinstance(cp.get(ts_key), str):
            errors.append(f"{ts_key} 必须是字符串")

    return (len(errors) == 0), errors


def _is_hex(s: str) -> bool:
    try:
        int(s, 16)
        return True
    except (TypeError, ValueError):
        return False


def record_step_done(cp: dict, step: str) -> dict:
    """幂等记录一个已完成步骤，返回新 cp（不可变风格，不改入参）。

    重复记录同一 step 是 no-op —— 这是"已完成不重做"的基础。
    """
    if not isinstance(cp, dict) or not step:
        return cp
    steps = list(cp.get("completed_steps", []))
    if step not in steps:
        steps.append(step)
    new = dict(cp)
    new["completed_steps"] = steps
    return new


def set_current_step(cp: dict, step: str) -> dict:
    """更新当前步骤，返回新 cp。"""
    new = dict(cp)
    new["current_step"] = step or ""
    return new


def add_artifact(cp: dict, rel_path: str, digest: str) -> dict:
    """记录一个产物哈希，返回新 cp。重复写同一路径以最新值为准。"""
    artifacts = dict(cp.get("artifacts", {}))
    artifacts[rel_path] = digest
    new = dict(cp)
    new["artifacts"] = artifacts
    return new


def hash_bytes(data: bytes) -> str:
    """计算字节内容的 md5 十六进制摘要（32 位小写十六进制）。"""
    return hashlib.md5(data).hexdigest()


def hash_file(path) -> str:
    """计算文件内容的 md5（失败抛 OSError；产物缺失应视为需重做，由调用方判定）。"""
    with open(path, "rb") as f:
        return hashlib.md5(f.read()).hexdigest()


def verify_artifacts(cp: dict, workspace_root) -> list[str]:
    """对照 checkpoint 中的产物哈希核对磁盘现状，返回不匹配/缺失清单。

    幂等续跑前的产物校验：若某个已完成步骤的产物哈希与 checkpoint 不一致，
    说明该步骤的产出已漂移/丢失，必须重做，不能当作已完成。
    """
    mismatches: list[str] = []
    artifacts = cp.get("artifacts", {})
    if not isinstance(artifacts, dict):
        return ["artifacts 非法，无法校验"]
    for rel_path, expected in artifacts.items():
        full = os.path.join(workspace_root, rel_path)
        try:
            actual = hash_file(full)
        except (OSError, IOError):
            mismatches.append(f"{rel_path}: 产物缺失")
            continue
        if actual != expected:
            mismatches.append(f"{rel_path}: 哈希不一致(期望 {expected}, 实际 {actual})")
    return mismatches


def verify_then_continue(
    cp: Optional[dict],
    *,
    expected_epoch: Optional[int] = None,
    expected_writer: Optional[str] = None,
    task_id: Optional[str] = None,
) -> tuple[bool, str]:
    """幂等 verify-then-continue：通过才允许从该 checkpoint 续跑。

    判定（全部通过才返回 True）:
      - 结构合法（validate_checkpoint）
      - state == "running"（done 视为任务已收尾，不可续跑）
      - 若给 expected_epoch: cp.epoch == expected_epoch
        （不匹配 = 已被更新的 writer 覆盖，旧上下文不得续跑/覆盖）
      - 若给 expected_writer: cp.writer == expected_writer
      - 若给 task_id: cp.task_id == task_id（防止任务错位续跑）

    Returns:
        (True, "") 或 (False, reason)
    """
    if not isinstance(cp, dict):
        return False, "无 checkpoint，无法续跑"
    ok, errors = validate_checkpoint(cp)
    if not ok:
        return False, "checkpoint 非法: " + "; ".join(errors[:5])
    if cp.get("state") != DEFAULT_STATE:
        return False, f"checkpoint 已处于 {cp.get('state')} 状态，不可续跑"
    if expected_epoch is not None and cp.get("epoch") != expected_epoch:
        return False, (
            f"checkpoint 已过期: 期望 epoch={expected_epoch}, 实际 epoch={cp.get('epoch')}"
            f"(writer={cp.get('writer')})，已被更新上下文覆盖"
        )
    if expected_writer is not None and cp.get("writer") != expected_writer:
        return False, f"checkpoint writer 不匹配: 期望 {expected_writer}, 实际 {cp.get('writer')}"
    if task_id is not None and cp.get("task_id") != task_id:
        return False, f"checkpoint task_id 不匹配: 期望 {task_id}, 实际 {cp.get('task_id')}"
    return True, ""


def checkpoint_to_lines(cp: Optional[dict]) -> list[str]:
    """把 checkpoint 渲染成紧凑文本行，供恢复提示/续跑消息使用。"""
    if not isinstance(cp, dict):
        return []
    lines = [
        f"- 任务: {cp.get('task') or cp.get('task_id') or ''}",
    ]
    completed = cp.get("completed_steps", [])
    current = cp.get("current_step", "")
    if completed:
        lines.append(f"- 已完成步骤({len(completed)}): {', '.join(completed)}")
    if current:
        lines.append(f"- 当前步骤: {current}")
    artifacts = cp.get("artifacts", {})
    if artifacts:
        lines.append(f"- 产物({len(artifacts)}): {', '.join(artifacts.keys())}")
    if cp.get("remaining_instruction"):
        lines.append(f"- 续跑指令: {cp.get('remaining_instruction')}")
    if cp.get("writer"):
        lines.append(f"- 最后写入: {cp.get('writer')} (epoch={cp.get('epoch')})")
    return lines


# ============================================================
# 持久化：锁内原子 读-改-写 / 读校验 / 清除
# ============================================================

def _now_iso() -> str:
    return datetime.now().isoformat()


def load_checkpoint(
    lock: Optional[threading.RLock] = None,
    *,
    team_name: str,
    member_name: str,
) -> tuple[Optional[dict], list[str]]:
    """读成员任务 checkpoint（持锁，校验后返回）。

    Returns:
        (cp, errors): cp 为校验通过的深拷贝或 None;errors 非空说明读取失败/非法。
    """
    lock = lock or _DEFAULT_LOCK
    with lock:
        data = load_data()
        member = data.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name, {})
        cp = member.get(MEMBER_CHECKPOINT_KEY)
        if cp is None:
            return None, []
        ok, errors = validate_checkpoint(cp)
        if not ok:
            return None, errors
        return cp, []


def save_checkpoint(
    lock: Optional[threading.RLock] = None,
    *,
    team_name: str,
    member_name: str,
    cp: dict,
    writer: str = "",
) -> tuple[bool, str]:
    """全量替换成员任务 checkpoint（锁内原子，epoch 自动单调 +1）。

    ⚠️ 全量替换语义：仅当传入的 cp.epoch **严格大于** 已持久化 epoch 时才允许
    覆盖（或磁盘尚无 checkpoint）。因为磁盘 epoch 只由本模块递增，一个基于
    "同一 epoch" 或 "更旧 epoch" 的本地拷贝必然是过期上下文 —— 拒绝覆盖，
    防止旧 writer 抹掉更新进度（防旧上下文覆盖 P0）。

    ⚠️ 续跑/继续推进请用 update_checkpoint（读-改-写 + CAS），不要用
    save_checkpoint 反复全量替换 —— 那会让 epoch 竞态。

    Returns:
        (True, "") 或 (False, reason)
    """
    ok, errors = validate_checkpoint(cp)
    if not ok:
        return False, "checkpoint 非法: " + "; ".join(errors[:5])
    writer = writer or cp.get("writer", "")
    lock = lock or _DEFAULT_LOCK
    with lock:
        data = load_data()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return False, f"团队 '{team_name}' 不存在"
        member = team.get("members", {}).get(member_name)
        if member is None:
            return False, f"成员 '{member_name}' 不存在"

        current = member.get(MEMBER_CHECKPOINT_KEY)
        if current is not None:
            _, cur_errors = validate_checkpoint(current)
            if cur_errors:
                # 既有 checkpoint 非法 → 拒绝在脏数据上继续（宁可失败不静默覆盖）
                return False, "既有 checkpoint 非法: " + "; ".join(cur_errors[:5])
            cur_epoch = current.get("epoch") if isinstance(current, dict) else None
            if cur_epoch is not None and cp.get("epoch", 0) <= cur_epoch:
                return False, (
                    f"全量替换拒绝过期覆盖: 传入 cp.epoch={cp.get('epoch')} "
                    f"<= 磁盘 epoch={cur_epoch}，旧 writer 不得覆盖新进度"
                )

        new_cp = dict(cp)
        new_cp["epoch"] = next_epoch(current)
        new_cp["writer"] = writer
        new_cp["updated_ts"] = _now_iso()
        if current is None:
            new_cp["created_ts"] = _now_iso()

        member[MEMBER_CHECKPOINT_KEY] = new_cp
        save_data(data)
        return True, ""


def update_checkpoint(
    lock: Optional[threading.RLock] = None,
    *,
    team_name: str,
    member_name: str,
    writer: str,
    expected_epoch: Optional[int] = None,
    updater: Callable[[Optional[dict]], Optional[dict]],
) -> tuple[bool, str]:
    """锁内原子 读-改-写 成员任务 checkpoint。

    updater(current) 接收当前已持久化的合法 checkpoint（无则 None），返回新的
    checkpoint 内容（epoch/writer/updated_ts 由本函数自动盖章）。返回 None 表示
    放弃本次更新（不落盘）。重复的 completed_steps 记录幂等。

    expected_epoch（可选，推荐总是传）是乐观并发 CAS 守卫：
      调用方基于某份拷贝(epoch=E)做了修改，提交时若磁盘上当前 epoch 已 != E，
      说明有更新的 writer 先落盘 —— 本函数拒绝覆盖并返回 False，
      防旧上下文覆盖新进度（P0 防旧上下文覆盖）。
      不传则不做 CAS（无条件递增覆盖，仅用于明确独占场景）。

    并发安全:整个 load→validate→CAS→updater→epoch 盖章→save 都在传入的 lock
    内完成,与其他成员/leader 的数据更新互斥。

    Returns:
        (True, "") 或 (False, reason)
    """
    if not writer:
        return False, "writer 不能为空（必须署名，供防旧上下文覆盖判定）"
    lock = lock or _DEFAULT_LOCK
    with lock:
        data = load_data()
        team = data.get("teams", {}).get(team_name)
        if not team:
            return False, f"团队 '{team_name}' 不存在"
        member = team.get("members", {}).get(member_name)
        if member is None:
            return False, f"成员 '{member_name}' 不存在"

        current = member.get(MEMBER_CHECKPOINT_KEY)
        if current is not None:
            _, cur_errors = validate_checkpoint(current)
            if cur_errors:
                # 既有 checkpoint 非法 → 拒绝在脏数据上继续（宁可失败不静默覆盖）
                return False, "既有 checkpoint 非法: " + "; ".join(cur_errors[:5])

        if expected_epoch is not None:
            cur_epoch = current.get("epoch") if isinstance(current, dict) else None
            # 磁盘尚无 checkpoint → 无"更新的版本"可覆盖，首次写入放行。
            # 磁盘已有版本且 epoch 与调用方基于的拷贝不一致 → 被更新过，拒绝覆盖。
            if cur_epoch is not None and cur_epoch != expected_epoch:
                return False, (
                    f"checkpoint 已被更新: 期望基于 epoch={expected_epoch}, "
                    f"磁盘当前 epoch={cur_epoch}，放弃覆盖（防旧上下文覆盖）"
                )

        new_cp = updater(current)
        if new_cp is None:
            return False, "updater 放弃本次更新"
        ok, errors = validate_checkpoint(new_cp)
        if not ok:
            return False, "新 checkpoint 非法: " + "; ".join(errors[:5])

        # epoch 单调：新写入必须比已持久化的大；当前非法时 current 已被拒绝。
        new_epoch = next_epoch(current)
        new_cp = dict(new_cp)
        new_cp["epoch"] = new_epoch
        new_cp["writer"] = writer
        new_cp["updated_ts"] = _now_iso()
        if current is None:
            new_cp["created_ts"] = _now_iso()

        member[MEMBER_CHECKPOINT_KEY] = new_cp
        save_data(data)
        return True, ""


def clear_checkpoint(
    lock: Optional[threading.RLock] = None,
    *,
    team_name: str,
    member_name: str,
) -> bool:
    """清除成员任务 checkpoint（锁内原子，幂等：不存在也返回 True）。"""
    lock = lock or _DEFAULT_LOCK
    with lock:
        data = load_data()
        member = data.get("teams", {}).get(team_name, {}).get("members", {}).get(member_name)
        if member is None:
            return False
        if member.pop(MEMBER_CHECKPOINT_KEY, None) is not None or MEMBER_CHECKPOINT_KEY in member:
            save_data(data)
        return True


# 兼容简名（调用方少打点）
read_member_checkpoint = load_checkpoint
write_member_checkpoint = save_checkpoint
