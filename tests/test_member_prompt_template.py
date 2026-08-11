"""
prompts/members.ts 成员 prompt 模板的直接单测。

背景（docs/prompt_migration_fact_check.md）：prompts/*.ts 是**文档模板**，非 Python
运行时载入源（无 TS 加载器/打包）。因此本测试对模板做**结构/内容不变量**验证，而非
运行时 TS 执行：

  - 格式对齐：与 prompts/leader.ts 同构（`export interface XPromptVars` +
    `export function xSystemPrompt(vars)` + `${v.xxx}` 占位渲染）；
  - 身份字段完整：team/member_name/role/agent/mode/leader/leader_type/共享目录/
    上下文/task/recoverySection 全在接口中，且函数体逐一引用（无死字段、无漏占位）；
  - 身份防遗忘语义：成员自身记录=本人；leader 记录是协调者、**不是可分配成员**；
    不采用 `[system]` 伪标签；
  - 与生产源同步：`_member_report_first_rule`（mult_agent_mcp.py:2987）与
    `_member_delivery_contract`（mult_agent_mcp.py:3001）的措辞锚点必须存在于模板。

不修改生产代码；只读 prompts/ 模板。
"""

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMBERS_TS = REPO_ROOT / "prompts" / "members.ts"
LEADER_TS = REPO_ROOT / "prompts" / "leader.ts"

# 成员身份字段（与 _build_member_initial_context / _build_recovery_context 对齐）。
# 命名与 leader.ts 的 LeaderPromptVars 风格一致（camelCase）。
REQUIRED_MEMBER_FIELDS = [
    "teamName",
    "memberName",
    "role",
    "agent",
    "mode",
    "leader",
    "leaderType",
    "teamDir",
    "shareDir",
    "task",
    "recoverySection",
]


def _read(name: str) -> str:
    path = REPO_ROOT / "prompts" / name
    assert path.exists(), f"prompts/{name} 不存在"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"prompts/{name} 为空"
    return text


def test_members_ts_exists_and_nonempty():
    text = _read("members.ts")
    assert "export interface MemberPromptVars" in text
    assert "export function memberSystemPrompt" in text


def test_leader_ts_format_reference_exists():
    """leader.ts 是成员模板的格式参照，必须先存在且保持同构约定。"""
    _read("leader.ts")


def test_interface_declares_all_required_identity_fields():
    text = _read("members.ts")
    iface = re.search(r"export interface MemberPromptVars \{(.*?)\n\}", text, re.S)
    assert iface, "找不到 MemberPromptVars 接口"
    body = iface.group(1)
    for field in REQUIRED_MEMBER_FIELDS:
        # 每个字段有独立 `field: type;` 声明行
        assert re.search(rf"^\s*{field}\s*:", body, re.M), f"接口缺字段 {field}"


def test_function_references_every_interface_field():
    """函数体必须引用全部接口字段——防死字段/防漏注入。"""
    text = _read("members.ts")
    for field in REQUIRED_MEMBER_FIELDS:
        assert f"${{v.{field}}}" in text, f"函数体未引用 ${{v.{field}}}"


def test_member_own_record_is_self_not_leader():
    """成员自身身份绑定：同名记录=本人；不得把 leader 记录当可分配成员。"""
    text = _read("members.ts")
    # 身份绑定锚点
    assert "${v.memberName}" in text
    assert "就是你本人" in text
    # 防串线：不得冒用他人/leader
    assert "不要冒用其他成员或 leader 的身份" in text


def test_leader_not_assignable_member():
    """成员模板必须明确 leader 是协调者而非可分配/可指挥的成员。"""
    text = _read("members.ts")
    assert "不是 leader" in text
    assert "leader" in text and "可分配对象" in text
    assert "${v.leader}" in text and "${v.leaderType}" in text


def test_no_system_pseudo_tag_as_mechanism():
    """fact-check §8 裁决：不采用 `[system]` 伪标签，正文为普通指令文本。"""
    text = _read("members.ts")
    # 模板正文（return 之后的模板字符串）不应依赖 [system]/[系统] 前缀
    body = text.split("return `", 1)[1]
    assert "[system]" not in body.lower(), "模板正文不应使用 [system] 伪标签"


def test_delivery_contract_and_report_first_present():
    """与生产 _member_delivery_contract / _member_report_first_rule 措辞锚点对齐。"""
    text = _read("members.ts")
    assert "[交付格式]" in text
    assert "member_report_result" in text
    assert "1. 结论" in text and "2. 修改文件" in text
    assert "3. 验证/测试" in text and "4. 风险/阻塞" in text
    assert "compressed_context <= 200" in text
    # 顺序义务措辞（_member_report_first_rule 统一措辞）
    assert "先回报" in text and "再继续" in text


def test_member_tools_are_member_scoped():
    """常用工具应为成员工具，不出现 leader 专属调度工具误引导。"""
    text = _read("members.ts")
    member_tools = [
        "member_report_result",
        "member_read_shared",
        "member_send_message",
        "member_acquire_file_lock",
        "member_release_file_lock",
        "member_submit_patch",
    ]
    for tool in member_tools:
        assert tool in text, f"缺成员工具 {tool}"
    # leader 专属工具（分配给成员会误导）：成员模板不应引导使用
    for leader_tool in ["leader_assign_subtask", "leader_broadcast", "leader_start_discussion"]:
        assert leader_tool not in text, f"成员模板不应引导 {leader_tool}"


def test_format_parity_with_leader_ts():
    """与 leader.ts 同构：interface + render fn + 模板字符串 + 动态 recoverySection 占位。"""
    members = _read("members.ts")
    leader = _read("leader.ts")
    # 两者都导出 XxxPromptVars 接口与 xSystemPrompt 渲染函数
    assert re.search(r"export interface \w+PromptVars", members)
    assert re.search(r"export function \w+SystemPrompt", members)
    assert re.search(r"export interface \w+PromptVars", leader)
    assert re.search(r"export function \w+SystemPrompt", leader)
    # 动态恢复段占位一致
    assert "${v.recoverySection}" in members
    assert "${v.recoverySection}" in leader
    # 模板字符串使用 `${v.` 占位风格
    assert "${v." in members
