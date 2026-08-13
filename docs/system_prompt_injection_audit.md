# system_prompt_injection_audit —— 现状审计（2026-08-12）

> **验收基线维护记录（reviewer-claude，2026-08-12）**：leader（codex）已裁决——
> 复用既有真实注入通道（Claude `--append-system-prompt-file` / Codex AGENTS.md），
> **不重写注入器**；后续仅补「prompts/*.ts → prompt_registry 运行时权威源 + system 段落
> 显式标记 + 通道契约 + 缺口测试与文档」。本报告为全员共同基线；
> 逐项验收清单见共享上下文 `prompts-ts-authority-acceptance-checklist.md`（A1–F3）。
> 复审触发点：`common/prompt_registry.py` 出现 .ts 读取/解析代码，或新测试将 .ts 作为
> 运行时输入；落地后按清单阶段2逐项核验（重点：只补权威源、不破坏既有注入器、报告/文档与实现同步）。

> 状态：**只读审计产物**（审计人 coder-claude）。本报告回答任务前置问题：
> 「上一团队是否已实现 system prompt 注入？prompts TS 是否运行时读取？本轮任务
> 缺口在哪？」，供全员以此作为共同基线后再决定是否修改。**本文不包含任何业务代码
> 改动**；全部论断标注 `文件:行号`。
>
> 审计范围：MCP 服务器侧（`mult_agent_mcp.py`）、TUI 侧（`tui/tui_screens.py` +
> `common/tmux_utils.py` 并行副本）、`common/prompt_registry.py`、`prompts/*.ts`、
> 相关测试、`docs/prompt_migration_fact_check.md`。未触碰 cppipc-dds 等其他团队状态。

---

## TL;DR

| 问题 | 结论 |
|---|---|
| 上一团队是否已实现真实 system prompt 注入？ | **是，基座已实现并转绿**。Claude 经 `--append-system-prompt-file`（`prompt_registry.claude_identity_file`），Codex 经团队工作区 AGENTS.md 角色中立段。实现自提交 e2be8b2 起进入 HEAD（`common/prompt_registry.py` 已被 Git 跟踪）。 |
| prompts/*.ts 是否运行时读取？ | **否**。全仓无 TS 加载器/打包；无任何 Python 代码 open `prompts/`。运行时文本仍硬编码在 Python。ts 仅是文档模板（契约 spec）。 |
| 本轮任务（TS 为运行时可编辑权威源 + system 段落显式标记路由）是否满足？ | **未满足**——这正是缺口。但「真实 system 通道注入」基座已就位，本轮只需在其上加「TS 解析 → system 段路由」层，**无需重建通道**。 |

---

## 1. 现有架构与调用链（消息角色四层）

当前所有注入文本落在四个消息角色层，持久性由**注入通道**决定，不由内容标记决定：

| 层 | 机制 | 载体 | 消费方 |
|---|---|---|---|
| **L1 真实 system 层（Claude）** | `--append-system-prompt-file <path>` | CLI 参数 → CLI system prompt | Claude Code |
| **L2 自动装载持久指令文件（Codex）** | 团队工作区 `AGENTS.md` | 磁盘文件，每次启动含 resume 重读 | Codex |
| **L3 首条 user 消息（Claude）** | tmux `send-keys`（`_send_keys` mult_agent_mcp.py:1213） | 首条 user 消息 | Claude / 通用 |
| **L3 首条 user 消息（Codex）** | CLI 位置参数（`_codex_command` mult_agent_mcp.py:4040 尾部 `cmd.append(prompt)`） | 首条 user 消息 | Codex |
| **L4 恢复/唤醒注入** | tmux `send-keys`（`_recover_and_send` L9011；`_send_context_to_member` L2412） | 新 user 消息 | Claude / Codex |

**调用链（成员）**：
```
_tmux_spawn_member (mult_agent_mcp.py:3962-4005)
  ├─ Claude: prompt_registry.claude_identity_file(L3989) → render_member_identity(L64)
  │          → write_identity_file(mkstemp 0600) → _claude_agent_args(append flag L3543-3548)
  └─ Codex:  prompt_registry.ensure_codex_agents_md(L3960) → codex_agents_md(L169) 写 AGENTS.md
随后 _send_keys(_build_member_initial_context) 注入首条 user 消息（L5521/L5650 等）
```

**调用链（leader）**：
```
launch_team_terminals (mult_agent_mcp.py:5558-5617)
  ├─ Claude: _leader_system_prompt(L4226) → claude_identity_file(leader=True, L5597)
  │          → _inject_claude_leader_prompt(L1340) send-keys 首条 user 消息
  └─ Codex:  ensure_codex_agents_md(L5571) + _codex_command(leader_prompt 位置参数, L5579)
```

**并行实现（TUI 侧）**：TUI 走 `common/tmux_utils.py` 副本而非 MCP 版：
- `tmux_utils.claude_agent_args` L633-639（append flag，双 builder 相等断言护栏）
- `tmux_utils.tmux_spawn_member` L783-788（`claude_identity_file`）
- `tui/tui_screens.py`：L611（leader identity）、L679（member identity）、L1266（恢复 identity）、L590/L664/L1250（AGENTS.md）
- `tui/tui_screens.py:_leader_system_prompt` L276（leader 首启提示文本，与 MCP 版同源复制）

---

## 2. prompts/*.ts 是否已运行时读取 —— **否**

证据（grep 全仓 `.py/.js/.json/.ts/.yaml/.sh/Makefile`）：
- 无任何 Python 代码引用 `prompts/` 目录（无 `open("prompts/...")`、无 `Path(...)/prompts/`）。
- 无 TS 加载器、无 `package.json`/`tsconfig`/`node_modules`/`dist` 等打包产物。
- `prompts/leader.ts`（5780B）与 `prompts/members.ts`（HEAD 3610B）当前**仅是文档模板**：
  `export interface XxxPromptVars` + `export function xSystemPrompt(vars)` + `${v.xxx}` 占位渲染，
  为契约 spec（`tests/test_member_prompt_template.py` 只做结构/内容不变量验证，不执行 TS）。

因此：**对 prompts/*.ts 的任何编辑，当前对运行时零效果**。

运行时文本的**权威定义仍硬编码在 Python**（即事实基线的"平行动定义"）：

| 内容 | 权威定义位置 |
|---|---|
| 成员静态身份（Claude append 文件正文） | `prompt_registry.render_member_identity` common/prompt_registry.py:64 |
| leader 系统提示（Claude append 文件正文） | `_leader_system_prompt` mult_agent_mcp.py:4226 + `leader_duty_prompt` L4185 |
| Codex AGENTS.md 角色中立段 | `prompt_registry.codex_agents_md` common/prompt_registry.py:169 |
| 交付合约 / 顺序义务 | `_member_delivery_contract` L3599 / `_member_report_first_rule` L3585 |
| 成员首条 user 消息 | `_build_member_initial_context` L3625 |
| 成员恢复消息 | `_build_recovery_context` L8747 |
| TUI 恢复消息 | `_build_tui_recovery_message` mult_agent_mcp.py:9499（tui_screens 侧对应） |
| TUI leader 提示 | `tui/tui_screens.py:_leader_system_prompt` L276（第三份，见 §7） |

---

## 3. 真实 system 通道矩阵（Claude/Codex × leader/member）

| | **Claude** | **Codex** |
|---|---|---|
| **真实 system 通道** | `--append-system-prompt-file`（/compact 免疫、resume 必带） | 无 system-prompt 通道；唯一自动装载持久指令文件 = 团队工作区 `AGENTS.md` |
| **接线点** | `_claude_agent_args` mult_agent_mcp.py:3543-3548；`tmux_utils.claude_agent_args` L633-639 | `ensure_codex_agents_md` mult_agent_mcp.py:3960（成员）/L5571（leader）；tmux_utils L754 |
| **member 内容源** | `render_member_identity`（成员静态身份 + 交付合约） | `codex_agents_md`（**角色中立**，不写死成员/角色，防共享文件多角色串线 B2） |
| **leader 内容源** | `_leader_system_prompt`（leader=True 分支） | 同一 `codex_agents_md`（AGENTS.md 为团队共享，leader/member 读同一份角色中立段） |
| **渲染函数** | `claude_identity_file` common/prompt_registry.py:130（mkstemp 0600，atexit 清理） | `codex_agents_md` L169 / `ensure_codex_agents_md` L194（幂等追加，保留用户内容） |
| **安全守卫** | 临时文件 0600（test_prompt_registry.py:87-93 断言） | fail-closed：无显式 workspace_dir 或 workspace==项目根时**零写入**（L211-217） |

> 关键语义：**Claude 成员身份是「每个成员一份」**（append 文件每次 spawn 重渲染）；
> **Codex AGENTS.md 是「团队一份、角色中立」**（共享文件，多角色成员读同一份，B2 防串线）。
> 因此「system 段落」对两种 agent 的内容约束不同——本轮若引入 TS 权威源，Codex 段必须
> 保持角色中立，Claude 段可承载成员身份。

---

## 4. 启动 / 恢复 / 唤醒区别

| 场景 | 注入文本 | 注入机制 | 消息角色 | 位置 |
|---|---|---|---|---|
| **Claude 成员/leader 首启** | append 文件（system 层）+ 首条 user 消息 | CLI flag + send-keys | system + user | mult_agent_mcp.py:3989/5597；L5521/L5650 |
| **Codex 成员/leader 首启** | AGENTS.md + CLI 位置参数 | 磁盘重读 + CLI prompt | 持久指令 + user | L3960/5571/5579 |
| **成员崩溃恢复 / 换号** | `_build_recovery_context`（身份+generation+checkpoint+report-first） | `_recover_and_send` send-keys（L9011-9017） | **新 user 消息** | L8747/8894 |
| **leader 复活** | `_leader_system_prompt`（extra_message） | `_recover_and_send` extra_message | 新 user 消息 | L2102；`_revive_leader_terminal_locked` |
| **leader 配额切换** | `_leader_system_prompt` | `_recover_and_send` extra_message | 新 user 消息 | L2102 等 |
| **唤醒（leader）** | `_build_leader_wakeup_message`（headline 含 `[system]` 前缀） | `_send_context_to_member` send-keys | **新 user 消息**（`[system]` 仅是内容层格式模仿，非真实 system） | L2411-2418；headline L2300/2316/2322 |
| **任务派单（成员）** | `_build_member_task_payload` + `_mode_task_prefix` | send-keys | 新 user 消息 | L3613/6051/6061 |

> **重要**：唤醒/恢复/派单全部经 tmux send-keys → **user 消息角色**；其中 `[system]`/
> `[系统]` 前缀是内容层格式模仿，CLI 不解析为真实 system 消息（fact-check §1 L34-37）。
> 当前代码**没有**用普通 send-keys 内容冒充真实 system——真实 system 只经 append flag
> / AGENTS.md 两个通道。

---

## 5. 代码与测试证据

### 5.1 生产代码（全部在 HEAD 或工作树，非计划）
- `common/prompt_registry.py`：渲染 + 注入单一源（254 行，**已被 Git 跟踪**，HEAD 提交 e2be8b2）。
- `mult_agent_mcp.py:_claude_agent_args` L3543-3548：append flag 单点接线。
- `mult_agent_mcp.py:_tmux_spawn_member` L3985-3991：成员 + managed leader 复活统一入口。
- `mult_agent_mcp.py` leader 首启 L5558-5617：raw spawn + append flag + send-keys。
- `common/tmux_utils.py` L633-639 / L783-788：TUI 侧并行接线。
- `tui/tui_screens.py` L611/L679/L1266/L590/L664/L1250：TUI spawn/恢复接线。

### 5.2 测试证据（已绿）
- `tests/test_prompt_registry.py`：身份文件 0600/内容来自数据层、默认路径确定性、codex AGENTS.md 团队中立 + 幂等 + fail-closed、双 builder 相等。
- `tests/test_prompt_identity_system_layer.py`：**大型任务验收契约**——Claude spawn 携带 append flag（A 组）、Codex AGENTS.md 团队中立（B 组）、缺 workspace_dir 不污染仓库根（B2 组）、恢复/首启仍绑身份（C 组）、双 builder 一致（D 组）。
- `tests/test_prompt_injection_roles_verify.py`：四角色（reviewer/coder/refactor/tester）+ leader 逐角色 append 身份不串线。
- `tests/test_runtime_identity_probe.py`：L1 运行时探针（mock tmux IPC 边界，真实文件产物：身份文件/results.jsonl/AGENTS.md）。
- `tests/test_member_prompt_template.py`：members.ts 结构/字段/措辞不变量（防漂移护栏）。

### 5.3 事实基线
- `docs/prompt_migration_fact_check.md`：§1（身份=首条 user，无 system 层）已**过时**（append flag 已落地）；
  §5/§6.2（prompts/*.ts 不可作为运行时载入源）**仍成立**；§8 推荐方向 = 本任务要做的事。
  docs/ 被 `.gitignore:16` 忽略，未纳入 Git 跟踪。

---

## 6. 安装 / 热更新行为

| 通道 | 热更新语义 |
|---|---|
| Claude append 文件 | **每次 spawn 重渲染**（`claude_identity_file` → `render_member_identity`，mkstemp 新文件）。身份/模板文本改动 → **新启动会话立即生效**；已运行终端不生效（CLI 启动时读一次）。 |
| Codex AGENTS.md | **写一次幂等**（`ensure_codex_agents_md` 检查 marker，命中则不追加，L226）。`codex_agents_md` 文本改动 → **已写文件不更新**（需删块/删文件重写才生效）；新团队目录首次 spawn 生效。 |
| prompts/*.ts | **任何改动零运行时效果**（不被读取）。 |
| 安装路径 | prompt_registry 不读 prompts/，无包内路径问题；**若本轮让 Python 读 .ts，需处理打包安装后 prompts/ 可能不在包内**（应支持包内/包外相对路径查找 + 回退内建文本）。 |

---

## 7. 已知缺口

1. **prompts/*.ts 非运行时源**（本任务核心缺口）：运行时文本硬编码在 Python，ts 仅为文档模板；业务改身份文本需改 Python 代码并重启 MCP server。
2. **system 段落无显式标记/路由**：现有注入靠"通道接线点"隐式区分，模板里没有"这一段是 system"的可编辑标记；`[system]` 前缀是内容层模仿，不能作为路由依据。
3. **文本多份平行动定义，漂移风险**：leader 提示有 MCP（L4226）/TUI（tui_screens L276）/tmux_utils（L663）三份 + leader.ts 第四份；成员身份有 `render_member_identity` / `_build_member_initial_context` / members.ts 三份。fact-check §2/§3.4 已证实漂移（如 tmux_utils L663 与 MCP 版工具引导不一致）。
4. **Codex AGENTS.md 热更新不生效**：幂等 marker 使文本改动不传播到已写文件。
5. **TUI 恢复降级**：`_build_tui_recovery_message`（mult_agent_mcp.py:9499）缺 checkpoint/report-first/discussion 工具清单，与 MCP 恢复协议漂移。
6. **身份变更不重注入**：`leader_redefine_member`/`member_set_agent` 改 role/agent 不重渲染身份。
7. **唤醒内容用 `[system]` 前缀**（L2300/2316/2322）：内容层模仿，与真实 system 无关——本轮需保持"普通 send-keys 不伪称 system"的既有正确语义。

---

## 8. 是否满足本轮任务（逐条对照）

本轮任务要求：**prompts/*.ts 成为运行时可编辑的权威模板源，Python prompt_registry 读取/解析，
把明确标记为 system 的段落经 Agent 真实 system-prompt 通道注入；区分 Claude/Codex、leader/member、
初始/恢复/唤醒/任务通道；普通 tmux send-keys 不得伪称 system；兼容安装路径；解析失败有清晰错误或
安全回退；无需 TS runtime/Node；业务修改后新会话生效；补契约/集成测试与文档；保护脏工作树、不 commit。**

| 要求 | 现状 | 是否满足 |
|---|---|---|
| TS 作为运行时可编辑权威源 | 否，ts 未被读取 | ❌ 缺口 |
| prompt_registry 读取/解析 .ts | 否 | ❌ 缺口 |
| system 段落显式标记 + 真实通道注入 | 无显式标记；但**真实通道基座已实现**（append/AGENTS.md） | ⚠️ 通道✅、标记❌ |
| 区分 Claude/Codex/leader/member/初始/恢复/唤醒/任务 | 通道层**已区分**（§3/§4） | ✅（通道层） |
| send-keys 不伪称 system | **已正确**（恢复/唤醒/派单全走 user 角色；真实 system 仅 append/AGENTS.md） | ✅ |
| 安装路径兼容 | 未涉及（.ts 未被读）；若接入需处理 | ⚠️ 待实现时处理 |
| 解析失败清晰错误/安全回退 | 无解析器 | ❌ 缺口 |
| 无 Node/TS runtime | 现状无（ts 未执行）；接入需保证纯 Python 解析 | ⚠️ 待实现时保证 |
| 业务修改新会话生效 | Claude append 已生效；Codex AGENTS.md 不生效；.ts 不生效 | ⚠️ 部分 |
| 补契约/集成测试 + 文档 | 测试已覆盖现有通道（§5.2）；解析层测试待补 | ⚠️ 部分 |
| 保护脏工作树 / 不 commit | 本轮只读，未改动 | ✅ |

**结论**：上一团队已完成「真实 system 通道注入」的基座（Claude append / Codex AGENTS.md /
leader/member 区分 / 恢复·唤醒走 user 通道），且测试转绿。**未做**「prompts/*.ts 作为运行时
权威源 + system 段落显式标记路由」。本轮实现应**复用既有通道，只加解析层**，不重建注入链路。

---

## 附录：工作树现状（保护项）

- `prompts/leader.ts`（+19/-9）与 `prompts/members.ts`（+1：`compressed_context <= 500`→`200`）：
  **已有未提交改动**（leader.ts 措辞「MCP 工具」→「mult agent mcp 工具」、新增「熟悉MCP工具」节、
  休眠工具点名 `leader_sleep`；members.ts 200 字）。本轮若改造 .ts 必须**保留这些改动**，不得覆盖。
- `docs/` 被 gitignore（.gitignore:16），本报告不纳入 Git 跟踪，属工作区交付物。
- 未触碰 cppipc-dds / 其他团队状态；未 commit。
