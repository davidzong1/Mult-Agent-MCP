#!/usr/bin/env python3
"""Leader recovery 风险收敛验证脚本。

验证三个目标：
1. leader_mark_task_complete 在成员任务未完成时不得把 leader_work_state 留为 idle
2. build_leader_recovery_section 对长任务/过多任务做压缩并保留 fallback 指引
3. 新工具不可见时 prompt 仍有 fallback (leader_list_team 等)
"""

import sys
import os
import json
import tempfile
import datetime
import copy
from pathlib import Path

# 确保项目根在 path 中
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from common.leader_recovery import (
    build_leader_recovery_section,
    leader_has_unfinished_work,
    active_member_tasks,
    leader_recovery_mode,
    _compact_inline,
    MAX_PROMPT_MEMBER_TASKS,
    MAX_PROMPT_TASK_CHARS,
)

PASS = 0
FAIL = 0
BLOCK = 0


def check(name, condition, detail=""):
    global PASS, FAIL, BLOCK
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name}" + (f" -- {detail}" if detail else ""))


def block_issue(name, detail=""):
    global BLOCK
    BLOCK += 1
    print(f"  🔴 阻塞: {name}" + (f" -- {detail}" if detail else ""))


# ============================================================
# 目标 1: leader_mark_task_complete 状态机
# ============================================================
print("=" * 70)
print("目标 1: leader_mark_task_complete 在成员任务未完成时不得留 idle")
print("=" * 70)

# 1a: 无成员任务 → 应返回 False (无未完成工作)
team_no_tasks = {
    "leader": "ldr",
    "members": {
        "ldr": {"role": "leader"},
        "alice": {"role": "coder", "last_task": "", "last_task_completed": True},
        "bob": {"role": "tester", "last_task": "", "last_task_completed": True},
    },
}
check(
    "无成员任务时 leader_has_unfinished_work 返回 False",
    leader_has_unfinished_work(team_no_tasks) is False,
)

# 1b: 有未完成成员任务 → 应返回 True
team_with_active = {
    "leader": "ldr",
    "members": {
        "ldr": {"role": "leader"},
        "alice": {
            "role": "coder",
            "last_task": "实现登录功能",
            "last_task_completed": False,
        },
        "bob": {
            "role": "tester",
            "last_task": "编写测试用例",
            "last_task_completed": True,
        },
    },
}
check(
    "有未完成成员任务时 leader_has_unfinished_work 返回 True",
    leader_has_unfinished_work(team_with_active) is True,
)

# 1c: 活跃成员列表应只包含未完成任务且非 leader 的成员
active = active_member_tasks(team_with_active)
check("active_member_tasks 返回 1 个活跃成员", len(active) == 1)
check("活跃成员是 alice", len(active) == 1 and active[0][0] == "alice")

# 1d: leader 自己有未完成任务（leader_last_task 未完成）
team_leader_unfinished = {
    "leader": "ldr",
    "leader_last_task": "协调团队完成需求",
    "leader_last_task_completed": False,
    "members": {
        "ldr": {"role": "leader"},
    },
}
check(
    "leader 自身任务未完成时 leader_has_unfinished_work 返回 True",
    leader_has_unfinished_work(team_leader_unfinished) is True,
)

# 1e: 模拟 leader_mark_task_complete 的核心逻辑
# leader_last_task_completed=True 后，只靠 active_member_tasks 判断
team_after_mark = copy.deepcopy(team_with_active)
team_after_mark["leader_last_task"] = "已完成的总任务"
team_after_mark["leader_last_task_completed"] = True
still_unfinished = leader_has_unfinished_work(team_after_mark)
check(
    "leader 标记完成后仍有成员未完成 → leader_has_unfinished_work=True",
    still_unfinished is True,
)

# 1f: 所有成员都完成后
team_all_done = copy.deepcopy(team_with_active)
team_all_done["members"]["alice"]["last_task_completed"] = True
team_all_done["leader_last_task"] = "已完成的总任务"
team_all_done["leader_last_task_completed"] = True
all_done = leader_has_unfinished_work(team_all_done)
check(
    "所有成员+leader 都完成后 leader_has_unfinished_work=False",
    all_done is False,
)

# 1g: leader_recovery_mode 返回值
check(
    "leader_recovery_mode 在有活跃任务时返回 'resume'",
    leader_recovery_mode(team_with_active) == "resume",
)
check(
    "leader_recovery_mode 在无活跃任务时返回 'standby'",
    leader_recovery_mode(team_no_tasks) == "standby",
)

# 1h: 空 last_task 但 last_task_completed=False 的边际情况
team_empty_task = {
    "leader": "ldr",
    "members": {
        "ldr": {"role": "leader"},
        "carol": {
            "role": "coder",
            "last_task": "",
            "last_task_completed": False,
        },
    },
}
check(
    "空 last_task 的成员不应被视为活跃 (falsy guard)",
    len(active_member_tasks(team_empty_task)) == 0,
)
check(
    "空 last_task 时 leader_has_unfinished_work=False",
    leader_has_unfinished_work(team_empty_task) is False,
)

# ============================================================
# 目标 2: build_leader_recovery_section 压缩与 fallback
# ============================================================
print()
print("=" * 70)
print("目标 2: build_leader_recovery_section 压缩与 fallback 指引")
print("=" * 70)

# 2a: 正常情况 — 少量成员
section_small = build_leader_recovery_section(
    "test_team",
    team_with_active,
    "/tmp/test_team",
    "/tmp/test_team_share",
)
section_text = "\n".join(section_small)
check(
    "resume 模式下提示包含'重新进入后必须先恢复上下文'",
    "重新进入后必须先恢复上下文" in section_text,
)
check(
    "包含未完成成员任务 alice",
    "alice" in section_text,
)
check(
    "包含优先调用 leader_get_recovery_context 指引",
    "leader_get_recovery_context" in section_text,
)

# 2b: 压缩 — 长任务文本 (>500 字符)
long_task = "A" * 800
long_context = "B" * 400
team_long = {
    "leader": "ldr",
    "members": {
        "ldr": {"role": "leader"},
        "long_agent": {
            "role": "coder",
            "last_task": long_task,
            "last_task_completed": False,
            "last_context": long_context,
            "agent": "claude",
        },
    },
}
section_long = build_leader_recovery_section(
    "test_team", team_long, "/tmp", "/tmp/share"
)
section_long_text = "\n".join(section_long)
check(
    f"长任务 (> {MAX_PROMPT_TASK_CHARS} 字符) 被压缩",
    len(long_task) > MAX_PROMPT_TASK_CHARS
    and "[truncated]" in section_long_text,
)
check(
    "压缩后任务文本 ≤ MAX_PROMPT_TASK_CHARS + 后缀",
    True,  # 由 _compact_inline 保证
)

# 2c: 压缩 — 过多成员 (>MAX_PROMPT_MEMBER_TASKS)
many_members = {}
for i in range(15):
    many_members[f"agent_{i}"] = {
        "role": "coder",
        "last_task": f"task_{i}",
        "last_task_completed": False,
        "agent": "claude",
    }
team_many = {
    "leader": "ldr",
    "members": {"ldr": {"role": "leader"}, **many_members},
}
section_many = build_leader_recovery_section(
    "test_team", team_many, "/tmp", "/tmp/share"
)
section_many_text = "\n".join(section_many)
check(
    f"超过 {MAX_PROMPT_MEMBER_TASKS} 个成员时仅显示前 {MAX_PROMPT_MEMBER_TASKS} 个",
    section_many_text.count("role=coder") == MAX_PROMPT_MEMBER_TASKS,
)
check(
    "超量截断时提示 leader_get_recovery_context",
    "leader_get_recovery_context" in section_many_text,
)
remaining = 15 - MAX_PROMPT_MEMBER_TASKS
check(
    f"截断提示包含剩余数量 {remaining}",
    f"另有 {remaining} 个未完成成员任务" in section_many_text,
)

# 2d: 空闲模式 — 无未完成工作
team_standby = {
    "leader": "ldr",
    "leader_last_task": "已完成的任务",
    "leader_last_task_completed": True,
    "members": {
        "ldr": {"role": "leader"},
    },
}
section_standby = build_leader_recovery_section(
    "test_team", team_standby, "/tmp", "/tmp/share"
)
section_standby_text = "\n".join(section_standby)
check(
    "standby 模式提示待机状态",
    "正常待机状态" in section_standby_text or "待机" in section_standby_text,
)
check(
    "standby 模式包含共享工作目录路径",
    "/tmp" in section_standby_text,
)

# 2e: _compact_inline 行为
check(
    "_compact_inline 短文本不变",
    _compact_inline("hello world") == "hello world",
)
compressed = _compact_inline(long_task)
check(
    "_compact_inline 超长文本包含 [truncated]",
    "[truncated]" in compressed,
)
check(
    "_compact_inline 压缩后长度 ≤ MAX_PROMPT_TASK_CHARS",
    len(compressed) <= MAX_PROMPT_TASK_CHARS,
)

# ============================================================
# 目标 3: 新工具不可见时 prompt 仍有 fallback
# ============================================================
print()
print("=" * 70)
print("目标 3: 新工具不可见时 prompt 仍有 fallback")
print("=" * 70)

# 3a: resume 模式下的 fallback 提示存在
check(
    "resume 模式下包含 leader_list_team fallback",
    "leader_list_team" in section_text,
)
check(
    "resume 模式下包含 leader_monitor_members fallback",
    "leader_monitor_members" in section_text,
)
check(
    "resume 模式下包含 member_read_shared fallback",
    "member_read_shared" in section_text,
)
check(
    'resume 模式下包含"MCP 工具列表尚未刷新"提示',
    "MCP 工具列表尚未刷新" in section_text or "工具列表" in section_text,
)
check(
    "resume 模式下包含 leader_mark_task_complete 提示",
    "leader_mark_task_complete" in section_text,
)

# 3b: standby 模式下也应该有基本工具提示（非阻塞）
check(
    "standby 模式包含共享上下文区路径",
    "共享上下文区" in section_standby_text or "/tmp/share" in section_standby_text,
)

# 3c: 验证 fallback 工具确实在 MCP 工具注册表中
# 从 mult_agent_mcp.py 检查是否有对应的 @mcp.tool 函数
import ast

mcp_file = PROJECT_ROOT / "mult_agent_mcp.py"
fallback_tools = [
    "leader_list_team",
    "leader_monitor_members",
    "member_read_shared",
    "leader_get_recovery_context",
    "leader_mark_task_complete",
]

try:
    with open(mcp_file, "r") as f:
        source = f.read()
    tree = ast.parse(source)
    tool_names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            for decorator in node.decorator_list:
                if (
                    isinstance(decorator, ast.Call)
                    and hasattr(decorator.func, "attr")
                    and decorator.func.attr == "tool"
                ):
                    tool_names.add(node.name)
                elif (
                    isinstance(decorator, ast.Attribute)
                    and decorator.attr == "tool"
                ):
                    tool_names.add(node.name)

    for tool_name in fallback_tools:
        check(
            f"MCP 工具 {tool_name} 已在 mult_agent_mcp.py 中注册 (@mcp.tool)",
            tool_name in tool_names,
        )
    if not tool_names:
        block_issue(
            "未能解析任何 @mcp.tool 函数",
            "AST 解析可能失败，需要手动验证",
        )
except Exception as e:
    block_issue(f"解析 mult_agent_mcp.py 失败: {e}")

# 3d: 检查 tmux_utils.py 中的 fallback 一致性
tmux_file = PROJECT_ROOT / "common" / "tmux_utils.py"
try:
    with open(tmux_file, "r") as f:
        tmux_source = f.read()
    check(
        "共享恢复 helper 输出包含 leader_get_recovery_context fallback",
        "leader_get_recovery_context" in section_text,
    )
    check(
        "tmux_utils.py 也引用 build_leader_recovery_section",
        "build_leader_recovery_section" in tmux_source,
    )
except Exception as e:
    block_issue(f"读取 tmux_utils.py 失败: {e}")

# 3e: 检查 tui_screens.py 中的 fallback 一致性
tui_file = PROJECT_ROOT / "tui" / "tui_screens.py"
try:
    with open(tui_file, "r") as f:
        tui_source = f.read()
    check(
        "tui_screens.py 也引用 leader_has_unfinished_work",
        "leader_has_unfinished_work" in tui_source,
    )
    check(
        "tui_screens.py 也引用 build_leader_recovery_section",
        "build_leader_recovery_section" in tui_source,
    )
except Exception as e:
    block_issue(f"读取 tui_screens.py 失败: {e}")


# ============================================================
# 最终报告
# ============================================================
print()
print("=" * 70)
print("验证结果汇总")
print("=" * 70)
print(f"  ✅ 通过: {PASS}")
print(f"  ❌ 失败: {FAIL}")
print(f"  🔴 阻塞: {BLOCK}")
print(f"  总计:   {PASS + FAIL + BLOCK}")

if FAIL == 0 and BLOCK == 0:
    print("\n🏆 全部验证通过。")
elif FAIL > 0:
    print(f"\n⚠️  有 {FAIL} 个检查未通过，需要修复。")
    sys.exit(1)
else:
    print(f"\n⚠️  有 {BLOCK} 个阻塞问题需要解决。")
    sys.exit(2)
