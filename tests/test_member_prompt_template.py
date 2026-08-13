"""
prompts/members.ts 成员 prompt 模板的直接单测（适配通道化改造后结构）。

背景（docs/prompt_template_runtime_design.md + docs/system_prompt_injection_audit.md）：
prompts/*.ts 已从"文档模板"升级为**运行时权威模板源**，每通道一个
``export function ...(vars): string { return `...`; }``，函数上方 JSDoc 的
``@channel`` 标注决定通道（system/initial/recovery/task/wakeup；缺失默认 user）。

本测试对模板做**结构/通道/内容不变量**验证（不执行 TS，纯文本检查，不依赖
Python 解析器实现，避免与 prompt_registry/prompt_template 实现耦合）：

  - 通道标注：system 通道函数存在且**不引用动态字段**（task/recoverySection/teammates，C4）；
  - 身份字段完整：MemberPromptVars 全字段在接口中，且每个字段被至少一个函数体引用；
  - 身份防遗忘语义：成员自身记录=本人；leader 记录是协调者、**不是可分配成员**；
  - 不采用 [system]/[系统] 伪标签（C3）；user 通道前缀用诚实通道名；
  - 交付合约/顺序义务措辞锚点存在于 initial/task 通道（[交付格式]/member_report_result/
    compressed_context <= 200/先回报再继续）；
  - 成员工具引导为 member_*，不出现 leader_* 专属调度工具；
  - leader.ts 同构：leaderSystemPrompt（@channel system，无动态字段）+ leaderInitialContext
    （@channel initial，含 teammates/task/recoverySection 动态段）。

不修改生产代码；只读 prompts/ 模板。
"""

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MEMBERS_TS = REPO_ROOT / "prompts" / "members.ts"
LEADER_TS = REPO_ROOT / "prompts" / "leader.ts"

# 成员身份字段（与 MemberPromptVars 接口对齐）。
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

# system 通道禁用的动态字段（C4：动态段不进 system 文件）。
SYSTEM_DYNAMIC_FIELDS = ("task", "recoverySection", "teammates")


def _read(name: str) -> str:
    path = REPO_ROOT / "prompts" / name
    assert path.exists(), f"prompts/{name} 不存在"
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"prompts/{name} 为空"
    return text


def _function_jsdoc(pre: str) -> str | None:
    """提取紧邻函数前的 JSDoc 注释块（只允许空白间隔；无则 None）。

    用 rfind 定位**最末** ``*/`` 与其 ``/**`` 起点，避免文件头/接口字段注释被
    非贪婪正则误吞（@channel 误读成头注释散文）。
    """
    close = pre.rfind("*/")
    if close == -1:
        return None
    if pre[close + 2:].strip():
        return None  # */ 与函数之间有非空白内容 → 不归属该函数
    open_ = pre.rfind("/**", 0, close)
    if open_ == -1:
        return None
    return pre[open_:close + 2]


def _functions(text: str) -> list[tuple[str, str, str]]:
    """提取每个通道函数: (name, @channel, 模板体)。纯文本，不执行 TS。"""
    funcs = []
    for m in re.finditer(r"export\s+function\s+(\w+)\s*\([^)]*\)\s*:\s*string\s*\{", text):
        name = m.group(1)
        jsdoc = _function_jsdoc(text[: m.start()])
        cm = re.search(r"@channel\s+(\w+)", jsdoc or "")
        channel = cm.group(1) if cm else "user"
        ret = re.search(r"return\s*`", text[m.start():])
        assert ret, f"{name} 缺 return `...` 模板体"
        body_start = m.start() + ret.end()
        i = body_start
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "`":
                body = text[body_start:i]
                break
            i += 1
        else:
            assert False, f"{name} 模板体未闭合"
        funcs.append((name, channel, body))
    return funcs


def _by_channel(text: str, channel: str) -> dict[str, str]:
    return {name: body for name, ch, body in _functions(text) if ch == channel}


def test_members_ts_exists_and_has_expected_functions():
    text = _read("members.ts")
    assert "export interface MemberPromptVars" in text
    names = {name for name, _, _ in _functions(text)}
    for expected in (
        "memberSystemPrompt", "codexAgentsSection",
        "memberInitialContext", "memberRecoveryContext", "memberTaskPayload",
    ):
        assert expected in names, f"members.ts 缺通道函数 {expected}"


def test_leader_ts_format_reference_exists():
    _read("leader.ts")


def test_interface_declares_all_required_identity_fields():
    text = _read("members.ts")
    iface = re.search(r"export interface MemberPromptVars \{(.*?)\n\}", text, re.S)
    assert iface, "找不到 MemberPromptVars 接口"
    body = iface.group(1)
    for field in REQUIRED_MEMBER_FIELDS:
        assert re.search(rf"^\s*{field}\s*:", body, re.M), f"接口缺字段 {field}"


def test_every_field_referenced_by_some_function():
    """每个接口字段被至少一个通道函数引用（防死字段/防漏注入）。"""
    text = _read("members.ts")
    all_bodies = "".join(body for _, _, body in _functions(text))
    for field in REQUIRED_MEMBER_FIELDS:
        assert f"${{v.{field}}}" in all_bodies, f"无任何函数引用 ${{v.{field}}}"


def test_system_channels_forbidden_dynamic_fields():
    """@channel system 函数禁引用动态字段（task/recoverySection/teammates，C4）。"""
    text = _read("members.ts")
    system = _by_channel(text, "system")
    assert "memberSystemPrompt" in system, "缺 Claude 成员 system 通道"
    assert "codexAgentsSection" in system, "缺 Codex 角色中立 system 通道"
    for name, body in system.items():
        for df in SYSTEM_DYNAMIC_FIELDS:
            assert f"${{v.{df}}}" not in body, \
                f"system 通道函数 {name} 不得引用动态字段 ${{v.{df}}}（C4）"


def test_user_channel_prefixes_are_honest():
    """user 通道前缀用诚实通道名，不伪称 system（C3）。"""
    text = _read("members.ts")
    funcs = dict((name, body) for name, _, body in _functions(text))
    assert funcs["memberInitialContext"].startswith("[成员上下文]"), \
        "首启上下文前缀应诚实标注 [成员上下文]"
    assert funcs["memberRecoveryContext"].startswith("[恢复通知]"), \
        "恢复上下文前缀应诚实标注 [恢复通知]"


def test_no_system_pseudo_tag_in_template_bodies():
    """任何通道函数模板体不使用 [system]/[系统] 伪标签（C3）。"""
    for name in ("members.ts", "leader.ts"):
        text = _read(name)
        for fn_name, channel, body in _functions(text):
            assert "[system]" not in body.lower() and "[系统]" not in body, \
                f"{name}:{fn_name} 模板体不应使用 [system]/[系统] 伪标签"


def test_delivery_contract_and_report_first_present():
    """交付合约 + 顺序义务措辞锚点必须存在于模板（initial/task 通道）。"""
    text = _read("members.ts")
    assert "[交付格式]" in text
    assert "member_report_result" in text
    assert "1. 结论" in text and "2. 修改文件" in text
    assert "3. 验证/测试" in text and "4. 风险/阻塞" in text
    assert "compressed_context <= 200" in text
    assert "先回报" in text and "再继续" in text


def test_member_own_record_is_self_not_leader():
    text = _read("members.ts")
    assert "${v.memberName}" in text
    assert "就是你本人" in text
    assert "不要冒用其他成员或 leader 的身份" in text


def test_leader_not_assignable_member():
    text = _read("members.ts")
    assert "不是 leader" in text
    assert "leader" in text and "可分配对象" in text
    assert "${v.leader}" in text and "${v.leaderType}" in text


def test_member_tools_are_member_scoped():
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
    for leader_tool in ["leader_assign_subtask", "leader_broadcast", "leader_start_discussion"]:
        assert leader_tool not in text, f"成员模板不应引导 {leader_tool}"


def test_leader_ts_channel_structure():
    """leader.ts：leaderSystemPrompt（system，无动态字段）+ leaderInitialContext（initial）。"""
    text = _read("leader.ts")
    names = {name: body for name, _, body in _functions(text)}
    assert "leaderSystemPrompt" in names, "缺 leader system 通道"
    assert "leaderInitialContext" in names, "缺 leader initial 通道"
    # system 通道禁动态字段
    for df in SYSTEM_DYNAMIC_FIELDS:
        assert f"${{v.{df}}}" not in names["leaderSystemPrompt"], \
            f"leaderSystemPrompt 不得引用动态字段 ${{v.{df}}}（C4）"
    # initial 通道承载动态段
    for df in ("teammates", "task", "recoverySection"):
        assert f"${{v.{df}}}" in names["leaderInitialContext"], \
            f"leaderInitialContext 应承载动态段 ${{v.{df}}}"
    # leader 职责锚点（脏工作树措辞保留：mult agent mcp 工具 / leader_sleep）
    assert "mult agent mcp" in names["leaderSystemPrompt"]
    assert "leader_sleep" in names["leaderSystemPrompt"]
