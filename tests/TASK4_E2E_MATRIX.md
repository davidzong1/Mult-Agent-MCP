# task4 — Agent 用户配置全局迁移 E2E 测试矩阵

> **真实范围**：Agent 用户配置从 `team["agent_users"]` 提升为全局 `data["agent_users"]` registry；
> CRUD 从 TeamDetailScreen 移到 MainScreen 顶层（跨团队复用）。
> `team.default_agent_user`（团队默认）、`member.agent_user`（成员覆盖/不接管 `AGENT_USER_NONE`）语义保留；
> 全局 rename/delete 需 sweep 所有团队引用。
> 0600 仅作为持久化约束（数据文件权限不变式），非本任务重点。
>
> 生成方：tester(task4)。⚠️ 迁移代码尚未落地，矩阵为 **TARGET 行为**；落地点在下方标注。
> 更新于 2026-08-03。

## 0. 术语与读写路径

| 概念 | 现状（迁移前） | 目标（迁移后） |
|---|---|---|
| profile 存储 | `team["agent_users"][key]`（每团队一份，无法跨团队复用） | `data["agent_users"][key]`（全局唯一 registry） |
| 团队默认 | `team["default_agent_user"]`（引用 key） | 不变，仍引用全局 key |
| 成员覆盖 | `member["agent_user"]`（引用 key / `AGENT_USER_NONE`） | 不变，仍引用全局 key |
| 不接管 | `AGENT_USER_NONE` 哨兵 | 保留，绝不被迁移/重命名/合并 |
| CRUD 入口 | TeamDetailScreen `u`(Agent用户) | MainScreen 顶层 teams 管理 + 团队内仍可选引用 |
| 读路径 | `team.get("agent_users", {})` | 应改为读全局 registry，兼容团队级旧数据回退 |
| 持久化 | `atomic_json_write` 0600 | 不变（仅持久化约束） |

## 1. 迁移规则（TARGET 契约）

设 `M` = 全局 registry（`data["agent_users"]`）。对每个团队 `t` 的每个 `(key, cfg) ∈ t["agent_users"]`：

| 规则 | 条件 | 动作 |
|---|---|---|
| **R1 普通迁移** | `key ∉ M` | `M[key] = cfg`；`t` 内引用不变 |
| **R2 同名相同合并** | `key ∈ M` 且 `M[key] == cfg` | 不重复写入（去重），`t` 内引用不变，即"合并" |
| **R3 同名冲突唯一重命名** | `key ∈ M` 且 `M[key] != cfg` | 按**稳定遍历顺序**为 `t` 分配唯一新 key `key__2`、`key__3`…（首个冲突后从 `__2` 递增）；`M[新key]=cfg`；**仅重写 `t` 的引用**（`t["default_agent_user"]` 与 `t` 内各 `member["agent_user"]` 中等于旧 key 的 → 新 key）；其他团队引用不受影响。**零数据丢失**：两份配置均完整保留 |
| **R4 不接管保护** | `key == AGENT_USER_NONE` | 跳过，绝不进入 registry |
| **R5 幂等** | 迁移已执行过 | 再次执行结果不变：无重复条目、无重复重命名、引用不再变 |

迁移完成后清除各 `team["agent_users"]`（或保留空 dict）；二次迁移因无团队级数据而为 no-op，保证幂等。

**全局 CRUD sweep（已拍板契约）**：
- **rename(旧key→新key)**：更新 `M`；sweep 所有团队 `default_agent_user` 与所有成员 `member.agent_user`，把等于旧 key 的引用改为新 key。
- **delete(key)**（2026-08-03 拍板）：
  - 删除 `M[key]`；
  - 引用该 key 的 `team["default_agent_user"]` → **清除**（置空）；
  - 引用该 key 的 `member["agent_user"]` → **清空字段**（删除键，或置空串），使该成员**回退其团队默认**；**不强制写 `AGENT_USER_NONE`**；
  - 迁移时因冲突重命名产生的引用（已指向 `key__2` 等新 key）不受影响。

## 2. E2E 测试矩阵

| ID | 场景 | 前置 | 步骤 | 期望 | 状态 |
|---|---|---|---|---|---|
| M01 | **单团队幂等迁移** | teamA 有 `agent_users={p1:cfg}` | 执行迁移 → 再执行一次 | 第一次 `M[p1]==cfg`、`teamA.default/member.agent_user` 引用不变；第二次结果完全一致（无重复） | 🔲 待落地 |
| M02 | **同名相同合并** | teamA、teamB 均有 `p1` 且内容相同 | 迁移 | `M` 中 `p1` 唯一；两团队引用均指向 `p1`；`M` 无重复 | 🔲 待落地 |
| M03 | **同名冲突唯一重命名+同步本团队引用** | teamA `p1=cfgA`，teamB `p1=cfgB`（内容不同） | 迁移 | 稳定遍历下 `M` 中一个为 `p1`，另一个为 **`p1__2`**；**仅 teamB** 的 `default_agent_user`/`member.agent_user` 中旧 `p1` 引用改为 `p1__2`；teamA 引用不动；**两份配置零丢失** | 🔲 待落地 |
| M04 | **全局 CRUD** | 已迁移 | MainScreen 顶层新建 / 编辑 / 删除 profile | 全局 registry 增改删生效；其他团队立即可引用（跨团队复用） | 🔲 待落地 |
| M05 | **全局 rename sweep 所有引用** | 已迁移，多团队引用 `p1` | 顶层 rename `p1→p_new` | `M` 键更新；**所有团队** `default_agent_user` 与所有 `member.agent_user` 中 `p1` 引用均改为 `p_new` | 🔲 待落地 |
| M06 | **全局 delete sweep 引用** | 已迁移，成员/团队默认引用 `p1` | 顶层 delete `p1` | `M[p1]` 删除；引用 `p1` 的 `team.default_agent_user` **清空**；引用 `p1` 的 `member.agent_user` **清空字段回退该团队默认（不写 `__none__`）**；不再有悬空引用 | 🔲 待落地 |
| M07 | **团队默认保留** | 迁移前 `teamA.default_agent_user=p1` | 迁移 + 读路径 | `teamA` 默认 profile 生效（env 注入 / TUI 显示"默认"） | 🔲 待落地 |
| M08 | **成员覆盖保留** | `member.agent_user=p1` 且团队默认不同 | 迁移 + 读路径 | 成员取 `p1` 而非团队默认 | 🔲 待落地 |
| M09 | **不接管保留** | `member.agent_user=AGENT_USER_NONE` | 迁移 + 读路径 | 不注入任何 env；TUI 显示"不接管"；`AGENT_USER_NONE` 不进 registry | 🔲 待落地 |
| M10 | **0600 持久化约束** | 已迁移 | 迁移前后 `stat -c "%a" DATA_FILE` | 始终 `600`（仅持久化约束，非重点） | ✅ 现有覆盖 |
| M11 | **读路径兼容** | 有团队级旧数据残留（未迁移干净） | 读 `get_agent_user_env_prefix` | 全局优先，团队级回退不崩溃 | 🔲 待落地 |
| M12 | **MainScreen 顶层入口** | TUI | 顶层进入 Agent 用户管理 → CRUD | 全局列表展示 Provider 标记；可引用到任意团队 | 🔲 待落地 |
| M13 | **env 注入回归** | 已迁移 | 启动 claude/codex 成员终端 | `env` 前缀来自全局 registry 的 typed profile | 🔲 待落地（依赖读路径改造） |

## 3. 自动化测试现状与缺口

**现有可复用（迁移前语义）**：
- `tests/test_agent_user.py` — 团队级 `_team(agent_users, members)` 的 env 注入 / options / None 哨兵
- `tests/test_agent_user_integration.py` — 团队级 TUI 对话框、持久化
- `tests/test_file_permissions.py` — 0600 持久化约束（M10）

**迁移后需新增/改造（🔲）**：
- `_team()` 等 fixture 需从团队级 `agent_users` 改为全局 registry + 团队引用
- 迁移函数单元测试：R1–R5（幂等 / 同名合并 / 冲突重命名 / 引用同步）
- 全局 CRUD + sweep：rename/delete 全团队引用扫描（M05/M06）
- 冲突重命名后"仅本团队引用被改，其他团队不动"（M03）——防止误 sweep

## 4. 验收命令

```bash
# 自动化（现有覆盖 + 迁移落地后补充）
python -m pytest \
  tests/test_agent_user.py \
  tests/test_agent_user_integration.py \
  tests/test_file_permissions.py \
  -q

# 迁移后需新增：
#   tests/test_agent_user_global_migration.py  （R1–R5 + sweep）

# 手动 E2E：
#   构造两团队同名冲突数据 → 触发迁移 → 核对 registry 唯一化 + 引用同步
python - <<'PY'
import json, os
p = os.path.expanduser("~/.mult_agent_mcp/teams_data.json")
d = json.load(open(p))
print("global agent_users:", len(d.get("agent_users", {})))
for t, team in d.get("teams", {}).items():
    print(t, "default:", team.get("default_agent_user"), "local:", list((team.get("agent_users") or {}).keys()))
PY
```

## 5. 风险 / 阻塞

- **读路径改造耦合**：M13 依赖 `common/tmux_utils.py` 读路径从团队级改读全局，落地顺序应先改读路径再切 CRUD，避免中间态引用悬空。
- **现有测试 fixture 依赖团队级 `agent_users`**：迁移落地会触及 `test_agent_user*.py` 的 `_team()` helper，需与相关成员协调同步更新，避免共享目录冲突。
- **稳定遍历顺序需定义**：R3 的 `key__2`/`key__3` 依赖团队/成员的稳定遍历顺序，迁移函数内应固定排序（如按团队名、成员名排序）保证幂等与可复现。
- **delete 引用清空语义**：已拍板——`default_agent_user` 清空、`member.agent_user` 清空字段回退团队默认、不写 `__none__`；实现需与读路径的"空引用→团队默认"逻辑对齐。
