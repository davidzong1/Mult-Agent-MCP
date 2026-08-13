# prompts/*.ts 运行时可编辑权威模板源 —— 架构设计

> 状态（2026-08-12）：**已落地实现**。由成员 refactor-claude（coder）按 leader
> 派单"独立设计模板 schema、通道枚举、文件定位/安装打包、缓存/热更新、解析失败策略、
> Claude/Codex CLI 支持边界"编写；实现由 refactor（`common/prompt_template.py` +
> `common/prompt_registry.py` 接线）+ coder-claude（`prompts/*.ts` 通道化迁移 +
> C3 send-keys 伪 `[系统]`/`[system]` 前缀清理 + 结构/集成测试 + docs 同步）落地。
> 已满足验收清单 A1–G3 主体；stage-2 复审待 reviewer 执行。
> 版本控制：docs/ 被 .gitignore 忽略，本文件不进 Git（与 prompt_migration_fact_check.md 一致）。
>
> 依据事实基线：docs/prompt_migration_fact_check.md（只读核对产物）+ 源码直读
> （行号以 2026-08-12 工作树为准）。

---

## 0. 目标与约束

- **目标**：`prompts/*.ts` 从"文档模板"升级为**运行时权威文本源**；Python `prompt_registry`
  读取/解析/渲染；明确标记为 system 的段落经 Agent **真实** system 通道注入。
- **不变量**：普通 tmux send-keys / argv 位置参数注入一律是 **user 消息**，不得伪称 system；
  `[系统]`/`[system]` 文本前缀必须从这些通道移除（fact-check §1 / §7 已证实 Codex 不解析、
  内容是格式模仿，属提示注入面）。
- **平台边界**：Claude 有真 system 通道（`--append-system-prompt-file`）；**Codex 无任何
  system-prompt 通道**（fact-check §6.1 实机证实），只能退化到 AGENTS.md 自动装载层或
  user 消息——具体成员/角色身份对 Codex 无法真 system 注入，属承认的平台限制。
- **工程约束**：无 Node/TS runtime；解析失败有清晰错误 + 安全回退；业务改 .ts 后**新会话
  生效**；兼容安装路径（repo 就地运行 / install.sh alias / 未来 pip）；保护现有脏工作树，
  不 commit。

---

## 1. 模板 schema（.ts 新约定）

### 1.1 结构

每文件一个业务主题（members/leader），内含**多个通道函数**，每个函数 = 一条注入通道：

```ts
/**
 * 成员 system 通道 —— 经真实 system 层注入（Claude --append-system-prompt-file）。
 * @channel system          # 必选标注：system | initial | recovery | task | wakeup
 * @vars MemberPromptVars   # 字段契约（本函数引用的字段子集）
 */
export interface MemberPromptVars {
  /** 团队名称 */
  teamName: string;
  ...
}

export function memberSystemPrompt(vars: MemberPromptVars): string {
  const v = vars;
  return `你是 Multi-Agent MCP 团队 '${v.teamName}' 的成员。...`;
}
```

- **`@channel` 标注是 system 判定的唯一权威**：仅 `@channel system` 的函数允许进真实 system
  通道；缺失时**默认 user**（fail-safe，绝不默认 system）。
- **占位符子集**：模板正文只允许 `${v.<identifier>}` 简单字段引用；任何其他 `${...}`
  （表达式/嵌套）→ 解析错误。不支持 TS 求值（无 Node）。
- **system 函数禁动态字段**：`task`/`recoverySection`/`teammates` 等动态值只出现在
  initial/recovery/task 通道；system 文件在 spawn 时写死，动态内容进去会冻结过期
  （见 1.2 现有 `_leader_system_prompt` 的缺陷）。

### 1.2 现有 .ts 重构（拆静态/动态，标通道）

| 文件 | 函数（新） | @channel | 内容 | 现状对应 |
|---|---|---|---|---|
| members.ts | `memberSystemPrompt` | system | 静态身份 + 交付合约 + 顺序义务（无 task/recoverySection） | `render_member_identity`（prompt_registry:64） |
| members.ts | `memberInitialContext` | initial | 身份 + delivery + task + recoverySection（动态段占位由 Python 注入） | `_build_member_initial_context`（mult_agent_mcp:3625） |
| members.ts | `memberRecoveryContext` | recovery | 恢复协议静态框架（身份/工具清单/report-first），动态段（checkpoint/session_id/generation）占位 | `_build_recovery_context`（mult_agent_mcp:8747） |
| members.ts | `memberTaskPayload` | task | 派单框架（[子任务]/[必要上下文]/[分配原因]/delivery） | `_build_member_task_payload`（mult_agent_mcp:3613） |
| leader.ts | `leaderSystemPrompt` | system | 身份 + leader 职责（无 teammates/task/recoverySection） | `_leader_system_prompt` 静态部分（mult_agent_mcp:4226） |
| leader.ts | `leaderInitialContext` | initial | 身份 + duty + teammates + task + recoverySection | `_leader_system_prompt(team, task)` 全装配 |
| leader.ts | `leaderRecoveryContext` | recovery | 复活/换号恢复框架 | `_revive_leader_terminal_locked` 注入（:9458） |
| 新 codex 段 | `codexAgentsSection` | system（role-neutral） | 团队中立协作约束 | `codex_agents_md`（prompt_registry:169） |

**关键缺陷修复**：现在 `claude_identity_file(leader=True)` 渲染 `_leader_system_prompt(team_name)`
（task=""）仍会带 `build_leader_recovery_section` 动态段（:4263）——动态内容被冻结进 system 文件。
新 schema 下 system 函数**不引用** recoverySection，动态段全部移入 initial/recovery 通道。

---

## 2. 通道枚举 + 注入机制 + 真 system 判定

| # | 通道 | 注入机制 | 载体 | 真 system? | 现调用点（接线目标） |
|---|---|---|---|---|---|
| C1 | Claude 成员 system | `--append-system-prompt-file` | system | ✅ 真 | `_claude_agent_args:3548`；tmux_utils `claude_agent_args:639`；`claude_identity_file`（prompt_registry:130） |
| C2 | Claude leader system | `--append-system-prompt-file` | system | ✅ 真 | MCP launch:5597 / `_tmux_spawn_member` 复活 / tmux_utils:786-799 |
| C3 | Codex 团队中立 | 团队 workspace `AGENTS.md`（自动装载） | 持久文件 | ⚠️ 非 system role，自动装载层 | `ensure_codex_agents_md`（tmux_utils:754, MCP:5571） |
| C4 | 成员首启上下文 | tmux send-keys | 首条 user | ❌ | `_send_keys(_build_member_initial_context)` :3418/5521/5650 |
| C5 | Codex 首启/任务 | CLI argv 位置参数 | 首条 user | ❌ | `_codex_command`（tmux_utils:658, MCP:4058） |
| C6 | Claude leader 首启 | tmux send-keys ×2（`_inject_claude_leader_prompt`） | 首条 user | ❌ | :5657 / 复活 :9481 |
| C7 | 成员恢复 | tmux send-keys | user | ❌ | `_recover_and_send` :9011 / :7619 / :8118 / `_quota_generation_migrate` :9199 |
| C8 | leader 复活/换号 | tmux send-keys / extra_message | user | ❌ | :9458 / `_recover_and_send` extra_message :2102 |
| C9 | 唤醒/回报/授权注入 | tmux send-keys | user | ❌ | `_send_keys` 短消息（fact-check §1:1755-1794） |
| C10 | 任务派单 | tmux send-keys | user | ❌ | `_send_keys(_build_member_task_payload)` :5984/6062 |
| C11 | TUI 恢复 | tmux send-keys | user | ❌ | `_build_tui_recovery_message`（tui_screens:127） |

> 接线原则：**C1/C2 的唯一真 system 出口**。C3 是 Codex 唯一自动装载持久层，但只能承载
> **角色中立**内容（B2 防串线）。C4–C11 全部 user 消息，从 .ts 渲染但**永不**带 `[系统]` 伪标。

---

## 3. 文件定位与安装打包

- **现状**：无 pyproject/setup（`ls` 无打包文件）；安装 = repo 就地运行（install.sh 只加
  `alias teammcp="python $THISDIR/main.py"`）；`main.py`/`mult_agent_mcp.py`/TUI 从项目根启动。
  cwd 不可依赖（spawn/恢复时 cwd 是 team workspace，非 repo 根）。
- **定位**：新增 `common/prompt_template.py` 用
  `PROJECT_DIR = Path(__file__).resolve().parent.parent`（与 common/config.py:37 / tui_screens.py:169
  同源），`PROJECT_DIR / "prompts" / f"{name}.ts"`。**禁止 cwd 相对路径**。
- **逃生阀**：支持 `MULT_AGENT_MCP_PROMPTS_DIR` 环境变量覆盖（打包后独立布放 / 测试注入坏
  模板时指向 tmp 目录）。
- **打包兼容**：本项目当前不打包；若未来 pip 安装，`.ts` 需进 package data，`PROJECT_DIR`
  仍解析到安装目录内的 prompts/。本设计不引入打包步骤，只保证 `__file__` 相对定位 + env 覆盖。

---

## 4. 缓存 / 热更新

- **读取时机**：每次渲染读 .ts。为降高频 spawn 的 parse 开销，用
  **mtime 键控缓存**：`{ (abs_path, mtime_ns) → ParsedTemplate }`；mtime 变化即重解析
  （71 行级别，重解析代价 < 1ms，可接受）。
- **生效语义**：业务改 .ts → 下一次 spawn/recovery/revive 重新渲染立即生效（**新会话生效**）；
  已在跑的会话不受影响（append 文件 spawn 时已写死，符合预期）。**不做跨会话热替换**
  （已跑会话的 system 内容不可变，这是 CLI 平台事实）。
- **不缓存渲染结果**：team/member/role/recovery 等动态值每次现算；只缓存 parse 结果。

---

## 5. 解析失败策略（清晰错误 + 安全回退）

- `render_channel(channel, team_name, member_name, **vars)` 在 spawn 路径**永不 raise**：
  1. **成功** → 返回渲染文本。
  2. **解析失败**（文件缺失/语法错/占位符越界/缺接口/`${}` 非 `v.field` 形式）→
     - **清晰错误**：写 stderr 一行 + 共享上下文 `results.jsonl`
       `{"event":"prompt_template_parse_error","file","channel","err","ts"}`（供 leader/监控可见）；
     - **安全回退**：**last-good 缓存**（该通道最近一次成功渲染文本）；无 last-good →
       **内建最小身份回退**（现有 `_DEFAULT_IDENTITY_TEXT` 类，prompt_registry:37，保持 spawn
       不阻塞）；system 文件不回退时不可含误导文本，只放最小身份。
  3. **user 通道**（C4-C11）失败时可追加一行 `⚠️ 模板解析失败，使用回退文本`（可观测）；
     **system 通道**（C1/C2/C3）不污染追加——回退最小身份即可。
- 降级链路必须有单测覆盖（注入坏 .ts 验证）。

---

## 6. Claude/Codex CLI 支持边界 + 不能真 system 注入的路径

### 6.1 Claude
- 真 system 只经 `--append-system-prompt-file`：成员（C1）与 leader（C2）。
- resume：`_claude_agent_args:3548` 恒追加 append flag（不受 resume_argv 影响）✅ 无缺口。
- 其余通道（C4/C6/C7/C8/C9/C10/C11）都是 mid-session/事件驱动 → user 消息，平台如此。

### 6.2 Codex —— **不能真 system 注入的路径（必须识别并文档承认）**
1. **具体成员/角色身份无法进 system**：Codex 无 system-prompt 通道；唯一自动装载持久文件
   AGENTS.md 因**共享于多角色**只能承载角色中立内容（B2），成员名/角色/agent 写进去会串线。
   → 成员具体身份只能退化到首条 user 消息（C5）+ 恢复上下文（C7），**不抗 compact/resume
   遗忘**。这是平台硬边界，设计只保证"诚实降级"（不伪称 system）。
2. **resume 丢身份**：`_codex_command:4047-4050` resume 分支只回放 `-C dir + resume_argv`，
   丢弃 prompt。resume 后身份仅靠 `_recover_and_send` 恢复上下文（C7）补注。
3. **AGENTS.md fail-closed**：`ensure_codex_agents_md:200-217` 无显式 workspace_dir 或
   workspace==项目根时不写入 → Codex 成员退回首条消息注入（已有，保持）。

### 6.3 其他不可真 system 路径
- **direct/claim leader**（无终端）：身份靠工具输出，无注入通道（fact-check §4.5）。
- **redefine_member / member_set_agent**：改 role/agent 不重注入身份（fact-check §4.5）——
  即使 .ts 化也仍是"改后不生效"，需 leader 决定是否补重注入（非本设计范围，风险登记）。
- **TUI 成员首启**（tui 648/662）：现根本不注入身份（fact-check §4.4）；Claude 成员经
  tmux_utils append 文件已有身份（C1），但缺 initial 上下文（C4 等价物）——接线时补。

### 6.4 "伪称 system" 修复点（user 通道去 `[系统]` 伪标）
- `_build_member_initial_context:3636` `[系统] Multi-Agent MCP 成员上下文`
- `_build_recovery_context:8769` `[系统] 终端恢复通知`
- `_build_tui_recovery_message:127`（同族标签）
- leader 唤醒/激活注入 `[system]`/`[系统]` 前缀（fact-check §1:1755-1794）
→ 统一改中立标签（如 `[团队通知]`）或去除；仅 `@channel system` 的内容允许语义上视为 system。

---

## 7. 收敛双 builder 与既有文本处置

- **文本单真源** = `prompt_registry`（读 .ts 渲染）。MCP `_claude_agent_args` 与
  tmux_utils `claude_agent_args` 仍各自拼 CLI 参数，但 `append_system_prompt_file` 内容统一
  `claude_identity_file()`（已如此，保持）。
- **leader 三份文本收敛**：MCP `_leader_system_prompt`（:4226）/ tmux_utils `leader_system_prompt`
  （:663）/ TUI `_leader_system_prompt`（tui_screens:271，fact-check §2 已漂移）→ 统一走
  `render_channel("leader_initial"/"leader_system", ...)` 单一入口；`leader_duty_prompt`（:4185）
  已是单一来源，转为 leader.ts `leaderSystemPrompt` 的静态内联段。
- **既有 Python 文本处置**：正常路径全部改走 .ts 渲染；Python 内联文本降级为
  "内建最小身份回退"（第 5 节）+ **契约测试**断言 .ts 渲染覆盖原锚点措辞（
  `[交付格式]`/`member_report_result`/`先回报再继续` 等，对齐 test_member_prompt_template.py），
  避免静默丢语义。不回退到"全量 Python 文本副本"（避免双维护漂移）。

---

## 8. 具体调用点接线清单（实现阶段按此接线）

**渲染入口（新增 common/prompt_template.py）**：`parse_ts(path) -> ParsedTemplate`；
`render_channel(channel, vars) -> str`；`template_dir() -> Path`。

| 接线点 | 现状 | 改为 |
|---|---|---|
| prompt_registry `render_member_identity`（:64） | Python 内联 | `render_channel("member_system", vars)`（读 members.ts） |
| prompt_registry `claude_identity_file` leader 分支（:139） | `_leader_system_prompt(team_name)` | `render_channel("leader_system", vars)`（无 task/recovery） |
| prompt_registry `codex_agents_md`（:169） | Python 内联 | `render_channel("codex_agents_section")` |
| MCP `_build_member_initial_context`（:3625） | Python 内联 | `render_channel("member_initial", vars)` + 去 `[系统]` 伪标 |
| MCP `_build_recovery_context`（:8747） | Python 内联 | `render_channel("member_recovery", vars)`，动态段（checkpoint/session/generation）作占位值传入 + 去 `[系统]` 伪标 |
| MCP `_build_member_task_payload`（:3613） | Python 内联 | `render_channel("member_task", vars)` |
| MCP `_leader_system_prompt`（:4226） | Python 内联 | `render_channel("leader_initial", vars)`（含 task/recovery） |
| tmux_utils `leader_system_prompt`（:663） | Python 内联 | 同 `leader_initial` 渲染入口（收敛） |
| TUI `_leader_system_prompt`（tui_screens:271） | Python 内联 | 同入口收敛 |
| TUI `_build_tui_recovery_message`（:127） | Python 内联 | `render_channel("member_recovery", vars)` 降级版（可选：直接复用 C7） |
| spawn 注入（C1/C2/C5） | append 文件 / argv | 内容源统一 .ts；argv 拼装函数**不动**（append flag/位置参数机制保持） |
| 恢复/唤醒/派单（C7/C8/C9/C10） | `_send_keys` | 文本源统一 .ts 渲染，机制（send-keys）不变 |

---

## 9. 测试与文档计划

**新增单测**
- `tests/test_prompt_template_parser.py`：合法 .ts 解析（channels/字段/占位符）；占位符⊆接口；
  坏语法/未知 `${}`/缺 `@channel` → 结构化错误；缺 `@channel` 默认 user。
- `tests/test_prompt_template_render.py`：渲染后无残留 `${`；system 函数不含 task/recovery/
  teammates 动态字段；动态占位值透传。
- `tests/test_prompt_template_fallback.py`：注入坏 .ts → last-good/最小回退 + error 事件落
  results.jsonl；system 通道不追加误导行。
- `tests/test_prompt_template_callsites.py`：断言 C1-C11 接线点最终落到 `render_channel`
  （spawn argv / send-keys 内容含 .ts 渲染锚点）。

**扩展既有**
- `test_member_prompt_template.py`：增加 `@channel` 校验、system 函数无动态字段断言。
- `test_prompt_identity_system_layer.py`：Claude append 文件内容来自 .ts system 通道且不含
  task；Codex AGENTS.md 内容来自 role-neutral 段；resume 路径仍携带 append flag。

**文档**
- 本文（docs/prompt_template_runtime_design.md）作设计基线。
- 更新 prompts/*.ts 头部注释（标注 @channel 约定与"仅 system 通道进真 system"）；
- prompt_migration_fact_check §2/§8 备注：.ts 已从文档模板升级为运行时源。

---

## 10. 风险与阻塞

- **R1 漂移收敛面大**：leader 三份 + recovery 两份 + TUI 首启缺口——接线顺序建议
  先 parser → 再 members.ts → 再 leader.ts，逐通道切 + 每步跑既有 9 处身份断言
  （fact-check §7）与 test_prompt_identity_system_layer 全绿再动下一块。
- **R2 Codex 平台边界**：具体成员/角色身份对 Codex 无法真 system 注入——属承认的平台限制，
  非本设计可解；需 leader 明确接受"Codex 成员身份仍走首条消息/恢复上下文"。
- **R3 动态段仍在 Python**：checkpoint/session_id/generation/teammates 等动态段保留 Python
  构建、作为占位值注入 .ts 框架——.ts 只承担静态框架，避免模板内求值（无 Node 约束使然）。
- **R4 现有测试锚点**：`.ts` 化后若措辞有微调会触碰跨 3 文件 9 处断言 + 身份层测试——
  需同步，属预期成本。
- **R5 脏工作树**：当前 git status 有大量未提交改动（classifier/tmux/leader_recovery/
  tests 等）。实现阶段**不 commit**；新增文件（prompt_template.py、新测试）与修改文件分开
  管理，避免把他人半成品裹进改动。

---

*（本设计为只读方案，不含代码改动；实现阶段由 leader 拆分派单。）*
