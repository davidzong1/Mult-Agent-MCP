"""
身份 system 层注入 —— prompt 迁移大型任务验收契约
====================================================

依据 docs/prompt_migration_fact_check.md（只读事实基线）的**已确认技术路线**
（leader 已拍板，见任务文档）：

  - **Claude Code**：唯一可靠 system 通道 = ``--append-system-prompt-file <tmp>``
    （/compact 免疫，每次启动含 resume 必带）。身份必须经 CLI 参数进入 system
    层，而不是只作为首条 user 消息（compact 摘要会摘除首条 user 消息 → 身份
    灾难性遗忘的根因）。
  - **Codex**：无任何用户可控 system-prompt 通道；唯一自动装载持久指令文件 =
    AGENTS.md（每次启动含 resume 从磁盘重读，抗 compact/resume）。身份固化
    落点 = 团队工作区 AGENTS.md 角色中立身份段。
  - **成员 prompt 模板**：prompts/members.ts 参照 prompts/leader.ts 格式补齐
    —— 已由 coder 落地，模板结构/字段/格式对齐见
    ``tests/test_member_prompt_template.py``（本文件不重复覆盖模板层）。
  - **恢复/压缩路径**：不得退回仅普通 user prompt —— 恢复上下文仍注入身份绑定；
    Claude resume 仍带 append flag。

断言层级说明（共享工作区会实时出现 coder 实现改动，断言只验**可观察出口**，
不锁内部实现细节）：

  - spawn 层：mock ``_tmux`` 捕获成员启动命令，断言 system 层参数
    （Claude ``--append-system-prompt-file``；Codex 身份文件落盘）—— 这是
    "启动参数中身份处于 system 层"的最终可观察出口；
  - 数据层：append 文件 / AGENTS.md 内容必须来自数据层（team/member/role），
    不是硬编码字符串；
  - 恢复层：恢复上下文 / 首启上下文仍注入身份绑定（回归护栏，当前即绿）。

状态约定：本文件是**验收契约**（TDD red→green，已由 coder 落地转绿）。Claude
append / Codex AGENTS.md 相关测试已由 coder 实现并**转绿**；恢复上下文 / 双
builder 一致 / 既有 flags 保留等测试作为回归护栏恒绿。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer


class _IsolatedMCP(unittest.TestCase):
    """镜像 tests/test_mult_agent_mcp.py 的数据隔离 setUp，供 spawn/_load 测试用。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "SHARE_WORKSPACE_DIR": mcp.SHARE_WORKSPACE_DIR,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD")
        }
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        # 清掉 _default_workspace_dir() 读取的 env（PWD/INIT_CWD 等可能指向真实
        # 用户 cwd），使回退确定性落到隔离 PROJECT_DIR，防测试污染真实仓库。
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for name, val in self.old_globals.items():
            setattr(mcp, name, val)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        # 与 test_mult_agent_mcp.py 同构：直接赋值，避免 set_data_file(None) 触发
        # assert_write_target_safe 守卫（None → Path(None) TypeError）。
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # -- 数据与 spawn 辅助 -------------------------------------------------

    def _save_team(self, workspace, context, *, members, leader="lead", leader_agent="claude"):
        mcp._save({
            "teams": {
                "team": {
                    "workspace_dir": str(workspace),
                    "context_dir": str(context),
                    "leader": leader,
                    "leader_type": "tmux",
                    "default_agent": "claude",
                    "members": {
                        leader: {"role": "leader", "agent": leader_agent},
                        **members,
                    },
                }
            }
        })

    def _spawn(self, name, agent, workspace, session="mcp_team", **spawn_kw):
        """mock _tmux 捕获成员 spawn 命令，返回 (rc, spawn_cmds)。"""
        workspace = Path(workspace)
        calls = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                # 返回不与被 spawn 成员同名的既有窗口，避免 _member_window_state
                # 误判"窗口已存在"（spawn lead 时不得命中 lead 窗口）。
                return 0, "$1\t1000\t@1\t__base", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(
                mcp, "_write_claude_permissions",
                return_value=str(workspace / ".claude" / "settings.json"),
            ):
                rc = mcp._tmux_spawn_member(session, name, agent, str(workspace), **spawn_kw)
        spawn_cmds = [c for c in calls if c[0] in {"new-session", "new-window"}]
        return rc, spawn_cmds


# =====================================================================
# A. Claude Code —— 身份进入 system 层（--append-system-prompt-file）
# =====================================================================
class ClaudeSystemLayerIdentityTests(_IsolatedMCP):
    """Claude Code 成员/leader 启动参数必须携带 --append-system-prompt-file，
    使身份处于 system 层、抗 /compact 与 resume。

    已由 coder 按 docs/prompt_migration_fact_check.md §8 在 _claude_agent_args
    单点接线并转绿；生产调用点 = mult_agent_mcp.py:3374(成员)/4955(leader 首启)
    与 tui/tui_screens.py:601/665/1249（经 tmux_utils 副本）。
    """

    def test_claude_member_spawn_carries_append_system_prompt_file(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "alice": {"role": "coder", "agent": "claude"},
        })
        rc, spawn = self._spawn("alice", "claude", workspace)
        self.assertEqual(rc[0], 0)
        self.assertTrue(spawn, "未捕获成员 spawn 命令")
        self.assertTrue(
            any("--append-system-prompt-file" in cmd for cmd in spawn),
            "Claude 成员启动命令缺少 --append-system-prompt-file "
            f"(身份未进入 system 层): {spawn}",
        )

    def test_claude_append_file_binds_team_and_member_identity(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "alice": {"role": "coder", "agent": "claude"},
        })
        rc, spawn = self._spawn("alice", "claude", workspace)
        self.assertEqual(rc[0], 0)
        for cmd in spawn:
            if "--append-system-prompt-file" in cmd:
                idx = cmd.index("--append-system-prompt-file")
                path = cmd[idx + 1]
                self.assertTrue(
                    Path(path).exists(),
                    f"append 文件不存在: {path}（身份文件应可读，见 R3 临时文件生命周期）",
                )
                content = Path(path).read_text(encoding="utf-8")
                # 身份必须来自数据层（team/member/role/agent），非硬编码
                self.assertIn("team", content, "append 文件缺团队身份")
                self.assertIn("alice", content, "append 文件缺成员身份")
                self.assertIn("coder", content, "append 文件缺角色")
                self.assertIn("claude", content, "append 文件缺 agent 类型")
                return
        self.fail("spawn 命令未携带 --append-system-prompt-file")

    def test_claude_spawn_identity_persists_on_resume_disabled_migration(self):
        """P2 跨凭证换号（resume_disabled=True）也是启动路径，身份不得随 resume 丢。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "alice": {"role": "coder", "agent": "claude"},
        })
        rc, spawn = self._spawn("alice", "claude", workspace, resume_disabled=True)
        self.assertEqual(rc[0], 0)
        self.assertTrue(
            any("--append-system-prompt-file" in cmd for cmd in spawn),
            "resume_disabled 换号启动也须携带 append flag（身份不得随 resume 丢失）",
        )

    def test_claude_leader_launch_builder_carries_append_flag(self):
        """managed leader 首启走 _claude_agent_args（4955 raw spawn），身份须进 system 层。"""
        args = mcp._claude_agent_args("claude", "auto")
        self.assertTrue(
            "--append-system-prompt-file" in args,
            f"leader 启动构造器 _claude_agent_args 未携带 append flag: {args}",
        )

    def test_claude_leader_and_member_identity_not_confused(self):
        """验收「leader/member 身份字段正确且不会混淆」：spawn leader 成员渲染 leader
        身份段，spawn 普通成员渲染成员身份段，两者互不串线。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "alice": {"role": "coder", "agent": "claude"},
        })

        def append_file(spawn_cmds):
            for cmd in spawn_cmds:
                if "--append-system-prompt-file" in cmd:
                    idx = cmd.index("--append-system-prompt-file")
                    return Path(cmd[idx + 1])
            self.fail("spawn 命令未携带 --append-system-prompt-file")

        _, leader_spawn = self._spawn("lead", "claude", workspace)
        _, member_spawn = self._spawn("alice", "claude", workspace)
        leader_content = append_file(leader_spawn).read_text(encoding="utf-8")
        member_content = append_file(member_spawn).read_text(encoding="utf-8")
        # leader 身份段
        self.assertIn("的 leader", leader_content, "leader append 文件应是 leader 身份段")
        self.assertIn("role='leader'", leader_content)
        # 成员身份段 —— 与 leader 不混淆
        self.assertIn("的成员", member_content, "成员 append 文件应是成员身份段")
        self.assertIn("role='coder'", member_content)
        self.assertIn("member_name='alice'", member_content)
        self.assertNotIn("的 leader", member_content, "成员 append 文件不得混入 leader 身份段")


# =====================================================================
# B. Codex —— 身份固化到自动装载持久层（AGENTS.md）
# =====================================================================
class CodexSystemLayerIdentityTests(_IsolatedMCP):
    """Codex 无 system-prompt 通道（fact check §2.2 实机证实），唯一自动装载
    持久指令文件 = AGENTS.md。身份固化落点 = 团队工作区 AGENTS.md 角色中立段。

    已由 coder 落地 codex_agents_md()/ensure_codex_agents_md() 注入并转绿。
    """

    def test_codex_spawn_writes_agents_md_with_team_identity(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "bob": {"role": "coder", "agent": "codex"},
        })
        rc, spawn = self._spawn("bob", "codex", workspace)
        self.assertEqual(rc[0], 0)
        agents_md = workspace / "AGENTS.md"
        self.assertTrue(
            agents_md.exists(),
            "Codex 成员启动后团队工作区应写入 AGENTS.md（唯一自动装载持久指令文件）",
        )
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn("team", content, "AGENTS.md 缺团队身份")
        self.assertIn("Multi-Agent", content, "AGENTS.md 缺团队协作身份")

    def test_codex_agents_md_role_neutral_no_cross_member(self):
        """共享 AGENTS.md 必须角色中立（fact check §6.2 B2：不得具体角色串线）。"""
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "bob": {"role": "coder", "agent": "codex"},
            "carol": {"role": "tester", "agent": "codex"},
        })
        self._spawn("bob", "codex", workspace)
        self._spawn("carol", "codex", workspace)
        agents_md = workspace / "AGENTS.md"
        if not agents_md.exists():
            self.skipTest("AGENTS.md 未写入（coder 未落地），契约待实现")
        content = agents_md.read_text(encoding="utf-8")
        # 具体角色不得写死进共享 AGENTS.md（会串线给其他角色成员）
        for member in ("bob", "carol"):
            self.assertNotIn(
                f"member_name='{member}'",
                content,
                f"共享 AGENTS.md 不得绑定具体成员 {member}（角色串线面）",
            )


# =====================================================================
# B2. P1 安全回归 —— 团队缺 workspace_dir 时绝不在项目/用户仓库根创建/覆盖 AGENTS.md
# =====================================================================
class AgentsMdPollutionGuardTests(_IsolatedMCP):
    """P1 安全验收（reviewer）：`_team_dir()` 缺 workspace_dir 时回退
    `_default_workspace_dir()` → PROJECT_DIR（可能=用户仓库根）。当前
    `ensure_codex_agents_md` 无落点守卫，会把团队 AGENTS.md 直接写进回退目录，
    污染用户正常会话（fact-check §7「污染」风险；实测项目根已出现 AGENTS.md）。

    [红→绿] coder 修订后转绿。断言用**隔离临时目录**模拟项目根，不触碰真实仓库；
    只验可观察落点（文件创建/覆盖），不锁内部实现。
    """

    def _save_codex_team_no_workspace(self):
        """团队**缺 workspace_dir**（只给 context_dir），default_agent=codex。"""
        context = self.root / "ctx"
        context.mkdir()
        mcp._save({"teams": {"team": {
            "context_dir": str(context),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "codex",
            "members": {"lead": {"role": "leader", "agent": "codex"}},
        }}})

    def test_no_agents_md_created_in_fallback_dir_when_workspace_missing(self):
        project_root = Path(mcp.PROJECT_DIR)  # setUp 已隔离到临时 project（模拟用户仓库根）
        self._save_codex_team_no_workspace()
        team_dir = mcp._team_dir("team")  # 生产路径回退 → PROJECT_DIR
        self.assertEqual(team_dir, str(project_root), "回退应落到隔离 project 根")
        rc, spawn = self._spawn("lead", "codex", team_dir)
        self.assertEqual(rc[0], 0)
        self.assertFalse(
            (project_root / "AGENTS.md").exists(),
            "团队缺 workspace_dir 时绝不能在项目/用户仓库根创建 AGENTS.md（P1 污染面）",
        )

    def test_existing_user_agents_md_not_overwritten_when_workspace_missing(self):
        """缺 workspace_dir 时，回退目录中用户自有的 AGENTS.md 不得被追加/覆盖。"""
        project_root = Path(mcp.PROJECT_DIR)
        user_agents = project_root / "AGENTS.md"
        original = "# 用户自己的 AGENTS.md\n这是用户仓库的自有内容，不得被团队身份覆盖。\n"
        user_agents.write_text(original, encoding="utf-8")
        self._save_codex_team_no_workspace()
        team_dir = mcp._team_dir("team")
        rc, spawn = self._spawn("lead", "codex", team_dir)
        self.assertEqual(rc[0], 0)
        self.assertEqual(
            user_agents.read_text(encoding="utf-8"), original,
            "缺 workspace_dir 时不得覆盖/追加用户自有 AGENTS.md",
        )


# =====================================================================
# C. 恢复 / 压缩相关路径 —— 不得退回仅普通 user prompt
# =====================================================================
class RecoveryCompactionIdentityTests(_IsolatedMCP):
    """恢复/压缩路径仍须承载身份：恢复上下文 + 首启上下文注入身份绑定。
    当前即绿（回归护栏）；Claude resume 的 append 保留由 A 组覆盖。

    prompt 模板层（members.ts 字段/格式）已由 tests/test_member_prompt_template.py
    覆盖，本组只验运行时上下文仍承载身份，不重复模板层。
    """

    def test_recovery_context_still_binds_identity(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "alice": {"role": "coder", "agent": "claude",
                      "last_task": "ship recovery", "last_task_completed": False},
        })
        msg = mcp._build_recovery_context("team", "alice")
        self.assertIn("member_name='alice'", msg)
        self.assertIn("role='coder'", msg)
        self.assertIn("agent='claude'", msg)
        self.assertIn("你的团队成员身份绑定", msg)

    def test_member_initial_context_binds_identity(self):
        workspace = self.root / "workspace"
        workspace.mkdir()
        self._save_team(workspace, self.root / "ctx", members={
            "alice": {"role": "coder", "agent": "claude"},
        })
        msg = mcp._build_member_initial_context("team", "alice")
        self.assertIn("member_name='alice'", msg)
        self.assertIn("role='coder'", msg)
        self.assertIn("agent='claude'", msg)
        # 身份绑定用 "你的团队成员身份绑定"，不是只有 [系统]/[system] 前缀
        self.assertIn("你的团队成员身份绑定", msg)


# =====================================================================
# D. 既有启动行为兼容 —— 双 builder 同步 + 既有 flags 不回归
# =====================================================================
class StartupCompatRegressionTests(_IsolatedMCP):
    """迁移不得破坏既有启动行为：双 builder 一致（B2 防回漂）、既有 flags 保留。"""

    def test_dual_builder_still_consistent(self):
        """MCP 版与 tmux_utils 版对相同输入必须仍逐字一致（B2 双源同步护栏）。

        若 coder 在 MCP builder 注入 append 而漏 tmux_utils 副本（TUI 6 spawn 点
        走 594 副本），此测试立即红 → 阻止"单点注入漏 TUI"回归。
        """
        from common import tmux_utils as tu
        for mode in ("auto", "plan", "manual", ""):
            with self.subTest(mode=mode):
                self.assertEqual(
                    mcp._claude_agent_args("claude", mode),
                    tu.claude_agent_args("claude", mode),
                    f"双 builder 漂移 mode={mode!r}（append 注入必须双份同步或收敛单真源）",
                )

    def test_builder_existing_flags_unchanged(self):
        """身份注入不得替换/丢弃既有安全 flags（append 是追加，不是 --system-prompt 替换）。"""
        args = mcp._claude_agent_args("claude", "auto")
        self.assertIn("--permission-mode", args)
        self.assertIn("acceptEdits", args)


if __name__ == "__main__":
    unittest.main()
