"""
Multi-Agent MCP — Claude Code 权限分类器暂时不可用的 fallback（严格模式限定）
========================================================================

背景
----
Claude Code 原生 ``plan`` / ``auto`` 权限模式用「权限分类器」判定工具安全性。
当分类器暂时不可用（provider 抖动 / 瞬时 API 错误）时，任何需要判定的工具
（Bash / Write / Edit）都被**硬阻断**，终端报：

    "<model> is temporarily unavailable, so auto mode cannot determine the
     safety of X"

终端不是停在 approval prompt（监控的 approval 检测不会触发），而是停在错误后
静置。若监控把它误判为 idle，``mark_idle_done`` 会把未完成任务误标完成 →
**丢失 checkpoint/session 上下文**（2026-08-10 全员锁死事故的残留层；
SMALL_FAST_MODEL 修复只治"配置根因"，瞬时不可用本身仍存在）。

本模块提供两层、且**严格模式限定**的 fallback：

  1. 预授权（settings 层）：只对**映射到 Claude 原生 ``plan``** 的模式追加
     **精选安全** allow 列表，使常规 Bash / workspace 内 Edit 不再查询分类器
     → 不硬阻断。危险命令（rm/sudo/curl/全量 ``Bash(*)`` / 全量 ``Edit(**)``
     越界）绝不放行。
  2. 检测 + 审计 + 恢复（监控层）：``detect_classifier_unavailable`` 识别
     签名，classify 层据此把停滞终端判为 ``classifier_unavailable``（绝不
     idle → 绝不 mark_idle_done）；进出 / 恢复各写一条审计事件。恢复是观察式
     （签名从捕获窗口消失即恢复）。

边界
----
  - 只影响**映射到原生 plan** 的模式（plan / planning / readonly）；
    ``acceptEdits``（成员 auto 的实际映射，实证**不调用分类器**）/ ``default`` /
    manual 的 settings 与行为一字不变（allow-list 严格按
    ``is_classifier_limited_mode`` 门控，测试证明不外溢）。
  - 不使用 ``--dangerously-skip-permissions``，不批量放行。
  - 不重启 / 不 compact / 不 wipe session：检测只改状态 + 审计。
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
# 模式门：只认 **Claude 原生 plan**（实证：成员 auto → 原生 acceptEdits 不调用
# 分类器；原生 auto 本项目不产生，无 CLI --permission-mode auto 路径）
# ---------------------------------------------------------------------------

#: 会触发分类器硬阻断的 Claude 原生权限模式（严格限定：仅 plan）
CLASSIFIER_LIMITED_MODES: frozenset[str] = frozenset({"plan"})

#: 非目标模式显式列证（供测试断言 fallback 不外溢）
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
    """模式门：仅 Claude 原生 ``plan`` 需要分类器 fallback。

    入参是 **Claude 原生** 权限模式（acceptEdits / plan / default / auto）。
    实证（v2.1.227）：成员 auto → 原生 acceptEdits **不调用分类器** → 非目标；
    原生 auto 本项目不产生 → 同样非目标。故 gate 只认 native "plan"。
    """
    n = (native_mode or "").strip().lower().replace("-", "_")
    return n in CLASSIFIER_LIMITED_MODES


def classifier_detection_applies(native_mode: str) -> bool:
    """签名检测是否对该（原生）模式生效。

    - 显式非目标（acceptEdits / default / manual / auto）→ 不检测（零外溢，
      acceptEdits 行为与既有完全一致）；
    - ``native_mode`` 缺省 / 未知（``""``）→ 检测（安全护栏：未知上下文绝不把
      分类器停滞终端误标 idle → 绝不 mark_idle_done 丢上下文）；
    - 目标（``plan``）→ 检测。
    """
    if not native_mode:
        return True
    return is_classifier_limited_mode(native_mode)


# ---------------------------------------------------------------------------
# 签名检测：`<model> is temporarily unavailable, so auto mode cannot
# determine the safety of X`（model 名可变、模式词 auto/plan 可变、时态可变）
# ---------------------------------------------------------------------------

# 稳定核心 = "temporarily unavailable" + "cannot/could not/unable to ... determine
# ... the safety"。同一行内允许 60/40 字符的松散间隔（容忍 ", so auto mode "、
# " and plan mode " 等措辞变化）。换行即断开（错误通常单行渲染）。
_CLASSIFIER_UNAVAILABLE_RE = re.compile(
    r"\btemporarily\s+unavailable\b[^\n]{0,60}"
    r"\b(?:cannot|can't|could\s*not|couldn't|is\s+unable\s+to|was\s+unable\s+to)\b"
    r"[^\n]{0,40}\bdetermine\b[^\n]{0,40}\bsafety\b",
    re.IGNORECASE,
)


def detect_classifier_unavailable(output: str) -> bool:
    """识别分类器暂时不可用签名（对 model 名 / 模式词 / 时态容错）。

    返回 True 仅当捕获文本含稳定核心签名。监控层据此把停滞终端判为
    ``classifier_unavailable``（绝不 idle → 绝不 mark_idle_done），并触发
    审计 entered / recovered 事件。
    """
    if not output:
        return False
    return bool(_CLASSIFIER_UNAVAILABLE_RE.search(output))


# ---------------------------------------------------------------------------
# 精选安全 allow 列表（仅 plan/auto 生效；绝不含破坏性 / 网络 / 全量放行）
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
    readonly）时，在 base（成员/leader 各自 MCP 前缀 + 既有 Bash/Edit 放行）之上
    追加精选安全窄规则；成员 auto（→ 原生 acceptEdits，实证不调用分类器）、
    manual / default / 空 → 返回 base 原样，零外溢。

    追加的窄规则（``Bash(git:*)`` / ``Bash(ls:*)`` / ...）命中即绕过分类器
    → 分类器暂时不可用时这些安全操作不硬阻断；危险命令刻意不在集内 → 仍走
    分类器，outage 下保持阻断（"危险命令不能因 fallback 无条件放行"）。
    """
    extra = classifier_fallback_allow_patterns(str(team_dir), member_mode)
    if not extra:
        return list(base_patterns)
    return [*base_patterns, *extra]


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
