"""
Multi-Agent MCP — Claude Code 权限分类器暂时不可用的 fallback（检测无条件 / allow 严格模式限定）
===============================================================================================

背景
----
Claude Code **原生 ``auto`` 权限模式**用「权限分类器」判定工具安全性（plan /
acceptEdits / manual 走审批流，**不调用分类器**，2026-08-11 真实 headless probe
实证：acceptEdits 对 workspace 内 Write 自动放行、workspace 外 Write 走审批
prompt；auto 对 workspace 外 Write 调分类器）。当分类器暂时不可用（provider
抖动 / 瞬时 API 错误，分类器模型默认 = 主模型）时，需判定的工具被**硬阻断**
（fail-closed），终端报：

    "<model> is temporarily unavailable, so auto mode cannot determine the
     safety of X"

（真实复现：``deepseek/deepseek-v4-flash[1m] is temporarily unavailable, so
auto mode cannot determine the safety of Write right now``，Write 被拒。）

终端不是停在 approval prompt（监控的 approval 检测不会触发），而是把 deny 作为
tool result 返回、模型继续（可只读 / 稍后重试）。若监控把该终端误判为 idle，
``mark_idle_done`` / ``enter_resting`` 会把未完成任务误标完成 →
**丢失 checkpoint/session 上下文**（2026-08-10 全员锁死事故的残留层）。

本模块提供两层 fallback，且**检测与 allow 解耦**：

  1. 预授权（settings 层，**严格模式限定**）：只对**映射到 Claude 原生 ``plan``**
     的模式追加**精选安全** allow 列表，使常规 Bash / workspace 内 Edit 不再查询
     分类器 → 不硬阻断。危险命令（rm/sudo/curl/全量 ``Bash(*)`` / 全量
     ``Edit(**)`` 越界）绝不放行。成员 auto → 原生 acceptEdits（不调用分类器，
     见上）→ 非目标，settings 一字不变。**不扩到 auto/acceptEdits/manual/default**。
  2. 检测 + 审计 + 恢复（监控层，**无条件**）：``detect_classifier_unavailable``
     识别签名，classify 层据此把停滞终端判为 ``classifier_unavailable``（绝不
     idle → 绝不 mark_idle_done）；进出 / 恢复各写一条审计事件。恢复是观察式
     （签名从捕获窗口消失即恢复）。

边界
----
  - **检测无条件**：签名是原生 auto 分类器专用、**自证**的消息；出现即代表终端
    处于 auto 分类器故障，与"假设的原生模式"无关（成员/leader mode 映射可能
    失真——如 auto→acceptEdits 掩盖了实际的原生 auto）。因此
    ``classifier_detection_applies`` **恒 True**，出现签名一律判
    ``classifier_unavailable``（绝不 idle → 绝不 mark_idle_done → 不丢上下文）。
  - **allow 仍严格限定**映射到原生 ``plan`` 的模式（``is_classifier_limited_mode``
    门控，测试证明不外溢）；acceptEdits / default / manual 的 settings 与行为
    一字不变。
  - 不使用 ``--dangerously-skip-permissions``，不批量放行。
  - 不重启 / 不 compact / 不 wipe session：检测只改状态 + 审计。
  - 保留原生 auto fail-closed（Write 硬拒是 Claude 设计，不绕过）；运营降级：
    只读等待 / 分类器内置重试（默认 4 次）/ 显式切 acceptEdits。
  - Codex 不涉及（权限分类器是 Claude Code 概念）。
"""

from __future__ import annotations

import datetime
import json
import os
import re
import tempfile
from pathlib import Path

from common.atomic_write import assert_write_target_safe

# ---------------------------------------------------------------------------
# 模式门（**allow 层**）：只认 **Claude 原生 plan**（实证：成员 auto → 原生
# acceptEdits 不调用分类器；auto 是原生分类器模式但 allow 不扩到它——acceptEdits
# 不调用分类器，扩 allow 无效且破坏零外溢）。
# 检测层无条件，见 ``classifier_detection_applies``。
# ---------------------------------------------------------------------------

#: 追加精选安全 fallback allow 的 Claude 原生权限模式（严格限定：仅 plan）
CLASSIFIER_LIMITED_MODES: frozenset[str] = frozenset({"plan"})

#: 非目标模式显式列证（供测试断言 allow 不外溢）
NON_TARGET_MODES: frozenset[str] = frozenset(
    {"default", "acceptEdits", "accept_edits", "manual", "auto", ""}
)


def claude_native_permission_mode(member_mode: str) -> str:
    """成员模式 → Claude Code 原生 ``--permission-mode`` 值（与
    ``mult_agent_mcp._claude_agent_args`` 的映射一致）：
      auto / accept_edits / never  → acceptEdits
      plan / planning / readonly   → plan
      其余（manual / default / ""）→ default
    """
    m = (member_mode or "manual").strip().lower().replace("-", "_")
    if m in {"auto", "accept", "accept_edits", "never"}:
        return "acceptEdits"
    if m in {"plan", "planning", "readonly", "read_only"}:
        return "plan"
    return "default"


def is_classifier_limited_mode(native_mode: str) -> bool:
    """模式门（**allow 层**）：仅 Claude 原生 ``plan`` 追加精选安全 fallback allow。

    入参是 **Claude 原生** 权限模式（acceptEdits / plan / default / auto）。
    实证（v2.1.227）：成员 auto → 原生 acceptEdits **不调用分类器** → 非目标；
    原生 auto 是分类器模式，但 allow 刻意不扩到它（auto/acceptEdits 非本代码库
    启动产物，扩 allow 无效且破坏零外溢）→ 同样非目标。故 gate 只认 native "plan"。
    注意：**检测层不经过此门**（无条件，见 ``classifier_detection_applies``）。
    """
    n = (native_mode or "").strip().lower().replace("-", "_")
    return n in CLASSIFIER_LIMITED_MODES


def team_classifier_effective_mode(members: dict) -> str:
    """共享 ``settings.json`` 的团队 union 有效模式（G2 修复核心）。

    共享 settings 文件被工作目录下**所有** Claude 进程加载，只能承载一个模式；
    若按 leader 或按当前 spawn 成员的 mode 写，混合团队（leader auto + member
    plan / leader plan + member auto）会随 spawn 顺序 last-writer-wins 翻转或按
    leader 串权（G2 实证）。本函数计算团队 union：任一 claude 成员映射到原生
    ``plan`` → 返回 ``"plan"``（settings 追加精选安全 fallback，plan 成员 settings
    层被覆盖）；否则返回 ``""``（base only，零外溢）。

    codex 成员忽略（权限分类器是 Claude Code 概念，不参与 settings 判定）。

    注意：本函数只决定**共享 settings 层**的 fallback；每 Agent 精确模式仍由
    per-terminal ``--allowedTools`` argv（``claude_terminal_allow_tools``）承载，
    settings 是受信时的纵深防御层（未受信工作区 settings 被整份忽略，argv 不受
    信任门控）。两者语义分离，不互相替代。
    """
    for info in (members or {}).values():
        if not isinstance(info, dict):
            continue
        agent = (info.get("agent") or "").lower()
        if "claude" not in agent:
            continue
        member_mode = info.get("work_mode") or info.get("mode") or ""
        if is_classifier_limited_mode(claude_native_permission_mode(member_mode)):
            return "plan"
    return ""


def classifier_detection_applies(native_mode: str) -> bool:
    """签名检测是否对该（原生）模式生效。**无条件 True（2026-08-11 语义修正）**。

    分类器 unavailable 签名是 Claude Code **原生 auto 权限模式**专用、**自证**的
    消息（acceptEdits / plan / manual 走审批流，实证不产生该签名；真实 headless
    probe 已在 auto 模式复现 ``"<main-model> is temporarily unavailable, so auto
    mode cannot determine the safety of Write"``）。签名出现即代表终端处于 auto
    分类器故障，与其"假设的原生模式"无关——成员/leader 的 mode 映射可能失真
    （如 auto→acceptEdits 掩盖实际的原生 auto），用映射结果去门控检测会把 auto
    leader 出现签名时误判 idle → enter_resting / mark_idle_done → 丢上下文（P0）。

    因此本函数恒 True，调用点 ``classifier_detection_applies(native_mode) and
    detect_classifier_unavailable(text)`` 退化为纯签名检测。``native_mode`` 参数
    保留仅为兼容既有调用点；allow 层的模式门是独立的 ``is_classifier_limited_mode``。
    """
    return True


# ---------------------------------------------------------------------------
# 签名检测：`<model> is temporarily unavailable, so auto mode cannot
# determine the safety of X`（model 名可变、模式词 auto/plan 可变、时态可变）
# ---------------------------------------------------------------------------

# 稳定核心 = "<model> is temporarily unavailable" + "cannot/could not/unable to
# ... determine ... the safety"。要求**前置 model 名 token**（自证上下文，F3：
# 排除"引用故障描述/文档片段"误判——那些文本没有紧邻的 model 名），同一行内允许
# 60/40 字符的松散间隔（容忍 ", so auto mode "、" and plan mode " 等措辞变化）。
# 换行即断开（错误通常单行渲染）。
# F3 二次护栏：匹配不得被引号包裹（广播/任务/回报里引用错误短语会被引号包住；
# 真实终端错误块是工具结果、不带引号）→ 由 _match_in_quotes 排除。
_CLASSIFIER_UNAVAILABLE_RE = re.compile(
    r"(?P<model>[A-Za-z0-9][A-Za-z0-9_./:@\[\]+\-]{0,63})\s+is\s+temporarily\s+unavailable\b"
    r"[^\n]{0,60}"
    r"\b(?:cannot|can't|could\s*not|couldn't|is\s+unable\s+to|was\s+unable\s+to)\b"
    r"[^\n]{0,40}\bdetermine\b[^\n]{0,40}\bsafety\b",
    re.IGNORECASE,
)

# 引用包裹判断：签名两侧任一侧出现引号/反引号/中文引号即视为"引用块"，不误判
# 为真实终端错误。真实错误块是工具 result 文本，不带任何引号包裹。
_QUOTE_CHARS = ('"', "'", "`", "“", "”", "「", "『")


def _match_in_quotes(line: str, start: int, end: int) -> bool:
    """签名是否被引号/反引号包裹（同行的引用块）。"""
    left = line[max(0, start - 1):start]
    right = line[end:end + 1]
    if left in _QUOTE_CHARS or right in _QUOTE_CHARS:
        return True
    # 行首到签名起点之间出现未闭合引号 → 引用块（如 `[广播] 收到："...`）
    before = line[:start]
    for q in ('"', "'", "`", "“", "「"):
        if before.count(q) % 2 == 1:
            return True
    return False


def detect_classifier_unavailable(output: str) -> bool:
    """识别分类器暂时不可用签名（对 model 名 / 模式词 / 时态容错）。

    F3（2026-08-12）：加**自证上下文护栏**——
      1. 要求前置 model 名 token（`<model> is temporarily unavailable`）：引用故障
         描述/文档片段（如广播、任务、回报里转述该报错文本）通常没有紧邻的 model
         名，命中率大降；
      2. 排除被引号/反引号包裹的引用块（真实终端错误是工具 result，不带引号）。

    返回 True 仅当捕获文本含**真实**分类器不可用签名。监控层据此把停滞终端判为
    ``classifier_unavailable``（绝不 idle → 绝不 mark_idle_done），并触发审计
    entered / recovered 事件。签名从捕获窗口消失即恢复（观察式）。
    """
    if not output:
        return False
    for m in _CLASSIFIER_UNAVAILABLE_RE.finditer(output):
        line = output[m.start():m.end()]
        if _match_in_quotes(output, m.start(), m.end()):
            continue
        return True
    return False


# ---------------------------------------------------------------------------
# 精选安全 allow 列表（仅映射到原生 plan 的模式生效；绝不含破坏性 / 网络 / 全量放行）
# ---------------------------------------------------------------------------

#: 只读检查 + 仓库内测试的精选 Bash 模式。刻意排除 rm/sudo/curl/wget/chmod/
#: mv/cp/pip/npm/make/find -delete 等破坏性或可执行任意脚本的命令 ——
#: "危险命令不能因 fallback 无条件放行"。
CLAUDE_FALLBACK_BASH_PATTERNS: tuple[str, ...] = (
    "Bash(git:*)",
    "Bash(pwd:*)",
    "Bash(ls:*)",
    "Bash(cat:*)",
    "Bash(echo:*)",
    "Bash(wc:*)",
    "Bash(head:*)",
    "Bash(tail:*)",
    "Bash(grep:*)",
    "Bash(which:*)",
    "Bash(whoami:*)",
    "Bash(date:*)",
    "Bash(python3 -m pytest:*)",
    "Bash(python3 -m unittest:*)",
    "Bash(python3 -m compileall:*)",
)


def classifier_fallback_allow_patterns(team_dir: str, mode: str) -> list[str]:
    """返回仅对**映射到原生 plan** 的模式追加的精选安全 allow 列表。

    ``mode`` 可为成员模式或其 plan 别名（plan / planning / readonly /
    read_only —— 均映射原生 plan）或任意其他模式；内部先经
    ``claude_native_permission_mode`` 转成 Claude 原生模式，仅当原生 == plan
    时追加；其余（auto / accept_edits / acceptEdits / default / manual / ""）
    → ``[]``（调用方不追加，settings 与既有完全一致，fallback 不外溢）。

    ⚠️ 实证边界：成员 ``auto`` 映射到原生 ``acceptEdits``，而原生 acceptEdits
    **不调用分类器** → 成员 auto 不是 fallback 目标，绝不注入窄规则。

    目标模式 → ``Edit(<team_dir>/*)``（Claude Code 的 Edit 规则同时门控
    Write/Edit 对文件的修改；**不加** ``Write(path)`` 规则——v2.1.210+ 下
    Write(path) 被接受但永不生效还会打启动告警）+ 精选安全 Bash 模式。
    绝不含 ``Bash(*)`` / ``Edit(**)`` 越界放行。
    """
    native = claude_native_permission_mode(mode)
    if not is_classifier_limited_mode(native):
        return []
    return [
        f"Edit({team_dir}/*)",
        *CLAUDE_FALLBACK_BASH_PATTERNS,
    ]


def claude_terminal_allow_tools(
    member_mode: str,
    team_dir: str,
    base_patterns: list[str],
) -> list[str]:
    """模式限定的 ``--allowedTools`` 组合（映射原生 plan 追加 fallback，其他原样）。

    ``--allowedTools`` 是**每终端** CLI 放行（区别于团队共享 settings.json），
    是模式限定 fallback 最精确的载体。入参是**成员模式**；仅当经
    ``claude_native_permission_mode`` 映射为原生 ``plan``（成员 plan / planning /
    readonly）时，在 base（成员/leader 各自 MCP 前缀 + 安全 Bash 窄规则）之上
    追加精选安全窄规则；成员 auto（→ 原生 acceptEdits，实证不调用分类器）、
    manual / default / 空 → 仅 base + scoped Edit，零外溢。

    **F1（2026-08-12）**：scoped ``Edit(<team_dir>/*)`` **无条件携带** —— workspace
    内 Edit/Write 显式放行（G1 真机实证 Edit(path) 规则覆盖 Write 新建文件），且
    argv 层不受 workspace trust 门控，是未受信场景的功能通道；裸 ``Bash``/``Edit``
    已在基座移除（裸 Bash=Bash(*) 无条件放行全部 shell 含 workspace 外写，与任务
    "不得无条件放开危险 Bash/workspace 外写"边界冲突）。

    追加的窄规则（``Bash(git:*)`` / ``Bash(ls:*)`` / ...）命中即绕过分类器
    → 分类器暂时不可用时这些安全操作不硬阻断；危险命令刻意不在集内 → 仍走
    分类器，outage 下保持阻断（"危险命令不能因 fallback 无条件放行"）。
    """
    base = [f"Edit({team_dir}/*)", *base_patterns]
    extra = classifier_fallback_allow_patterns(str(team_dir), member_mode)
    if not extra:
        return base
    merged = list(base)
    for p in extra:
        if p not in merged:
            merged.append(p)
    return merged


# ---------------------------------------------------------------------------
# 审计：进入 / 恢复各写一条 JSONL 事件到团队共享上下文区（可审计、可恢复）
# ---------------------------------------------------------------------------

#: 审计文件名（共享上下文区下）
CLASSIFIER_FALLBACK_AUDIT_FILE = "classifier_fallback_audit.jsonl"

#: 进程内互斥锁：监控线程可能并发写审计文件，append 需要串行化
_AUDIT_LOCK = __import__("threading").Lock()


def classifier_fallback_audit_path(share_dir: str) -> str:
    """审计文件路径（共享上下文区内）。"""
    return os.path.join(share_dir, CLASSIFIER_FALLBACK_AUDIT_FILE)


def record_classifier_fallback_event(
    share_dir: str,
    *,
    team_name: str,
    scope: str,
    member: str,
    mode: str,
    state: str,
    note: str = "",
) -> str:
    """追加一条分类器 fallback 审计事件（原子 append，0600，进程内互斥）。

    ``state`` ∈ {"entered", "recovered"}。返回写入的审计文件路径。
    写失败**绝不抛出**（审计是 best-effort，不能因审计失败拖垮监控主循环）——
    但必须尝试过原子写（mkstemp + fsync + os.replace + chmod 0600）。
    """
    assert state in {"entered", "recovered"}, f"非法审计状态: {state!r}"
    entry = {
        "ts": datetime.datetime.now().isoformat(),
        "team": team_name,
        "scope": scope,          # "leader" | "member"
        "member": member,
        "mode": mode,
        "state": state,
        "event": f"classifier_fallback_{state}",
        "note": note,
    }
    path = Path(classifier_fallback_audit_path(share_dir))
    line = json.dumps(entry, ensure_ascii=False) + "\n"
    try:
        assert_write_target_safe(path, context="classifier_fallback audit")
        path.parent.mkdir(parents=True, exist_ok=True)
        with _AUDIT_LOCK:
            try:
                existing = path.read_text(encoding="utf-8")
            except FileNotFoundError:
                existing = ""
            # mkstemp 同目录唯一临时文件（创建即 0600）→ 写 → fsync → replace → chmod
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(path.parent), prefix="." + path.name + ".", suffix=".tmp"
            )
            os.chmod(tmp_path, 0o600)
            try:
                with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                    f.write(existing)
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, path)
                os.chmod(path, 0o600)
            except Exception:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
    except Exception:
        return ""
    return str(path)
