"""
quota 换号重启身份重注入 —— 独立端到端回归（验收契约）
================================================================

背景（【额度切换重启风险·颗粒度对齐】任务 + tester 子任务）：
成员（含 leader）因额度不足切换用户池账号的完整流程——
  识别 quota → 选新用户 → 旧终端退出/关窗 → 重启命令 → system prompt 身份重注入。

本文件验证**完整换号链的可观察出口**（argv、身份文件生成顺序/内容、cwd、
AGENTS.md 落点、generation 门控、resume 语义），不锁内部实现细节，只改测试。

路径矩阵（重启身份注入入口，Claude/Codex × member/leader × MCP/后台恢复）：
  A. 非 generation 默认换号（kill/recreate）：_recover_and_send(reason="quota_switch")
     → _tmux_spawn_member → Claude 带 --append-system-prompt-file（数据层渲染身份）
  B. Codex 成员换号：ensure_codex_agents_md → 团队显式 workspace 写 AGENTS.md
     （角色中立段）；缺 workspace_dir → fail-closed 零写入（P1 语义不回归）
  C. leader 换号：与成员同入口，is_leader_spawn=True → 渲染 leader 身份段，不串号
  D. generation_migrate（P2 事务式）：spawn {member}__g{N+1} 新窗（resume_disabled），
     原子提升 ACTIVE、旧窗 DRAINING+TTL；恢复上下文带 generation 标注
  E. resume 语义：quota 换号 = 跨凭证迁移，**不得原生 resume 旧账号会话**
     （coder 修复：resume_disabled=(reason=="quota_switch")，生成与默认路径同规则）；
     crash 恢复仍允许 resume/bind，但两种路径身份都在 system 层（append flag 恒在）
  F. 端到端：_scan_member_terminal quota 分支 → 池选择 → 重启 → argv 可观察

断言层级：只验 argv / 文件落点/内容 / 数据层身份 / generation 状态等可观察出口；
禁止真实账号/额度，全部隔离临时数据 + tmux mock。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer

# 正例语料（复用 test_quota_failover）：Error 前缀 + 强词 insufficient_quota → 单帧即 quota
QUOTA_CAPTURE = (
    "✗ Error: 429 insufficient_quota\n"
    "    You exceeded your current quota, please check your plan and billing details.\n"
    "❯"
)

# 通用 agent 用户 registry（agent_type 必须与成员 CLI 类型一致，防 provider 滤空）
_AGENT_USERS = {
    "acct-a": {"label": "acct-a", "agent_type": "claude"},
    "acct-b": {"label": "acct-b", "agent_type": "claude"},
    "codex-a": {"label": "codex-a", "agent_type": "codex"},
    "codex-b": {"label": "codex-b", "agent_type": "codex"},
}


class _IsolatedRestartMCP(unittest.TestCase):
    """隔离团队数据 + tmux mock，供换号重启链测试用。

    镜像 tests/test_prompt_identity_system_layer.py 的 _IsolatedMCP 隔离：
    temp project 模拟用户仓库根，清空 _default_workspace_dir() 读取的 env，
    数据层 override 指向临时文件，绝不触碰真实 teams_data.json。
    """

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
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # -- 数据构造 -----------------------------------------------------------

    def _save_team(self, workspace, *, members, agent_users=None,
                   quota_failover=None, pool=None, default_agent="claude"):
        workspace = Path(workspace)
        workspace.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": default_agent,
            "members": members,
        }
        if pool is not None:
            team["agent_user_pool"] = list(pool)
        if quota_failover is not None:
            team["quota_failover"] = dict(quota_failover)
        data = {
            "agent_users": agent_users or _AGENT_USERS,
            "teams": {"team": team},
        }
        mcp._save(data)
        return workspace

    # -- 换号重启执行器（走真实 _recover_and_send + _tmux_spawn_member）-----

    def _restart(self, member_name, *, reason="quota_switch", previous_agent_user="",
                 workspace=None, resume_env=False, next_agent_user=None):
        """mock 终端执行层后跑 _recover_and_send，捕获 (ok, msg, all_tmux_calls, sends)。

        sends = _send_keys 收到的 (target, text) 列表，供恢复上下文断言。
        ``next_agent_user``：模拟监控侧先完成的池选择步骤——真实流程里
        _scan_member_terminal 先落盘 member["agent_user"]=nxt（写 history/cursor），
        再调 _recover_and_send 重启；直接调用 _recover_and_send 需先模拟这一步，
        否则新账号 env 不会注入（身份/凭证语义断言才成立）。
        """
        if next_agent_user:
            def updater(team):
                m = team.get("members", {}).get(member_name)
                if isinstance(m, dict):
                    m["agent_user"] = next_agent_user
                return {"ok": True}
            mcp._update_team_data("team", updater)
        if resume_env:
            os.environ["MULT_AGENT_MCP_SESSION_RESUME"] = "1"
        workspace = Path(workspace) if workspace else self.root / "workspace"
        calls = []
        sends = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\t__base", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(mcp, "_send_keys",
                               side_effect=lambda s, w, text, **kw: sends.append((w, text)) or (0, "")), \
             mock.patch.object(mcp, "_write_claude_permissions",
                               return_value=str(workspace / ".claude" / "settings.json")), \
             mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".mcp.json")), \
             mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None), \
             mock.patch.object(mcp, "_record_recovery_event", return_value=None), \
             mock.patch.object(mcp.time, "sleep", return_value=None):
            ok, msg = mcp._recover_and_send(
                "team", member_name, "mcp_team",
                reason=reason, previous_agent_user=previous_agent_user,
            )
        if resume_env:
            os.environ.pop("MULT_AGENT_MCP_SESSION_RESUME", None)
        return ok, msg, calls, sends

    def _spawns(self, calls):
        return [c for c in calls if c[0] in ("new-session", "new-window")]

    def _append_path(self, spawn_cmds):
        for cmd in spawn_cmds:
            if "--append-system-prompt-file" in cmd:
                idx = cmd.index("--append-system-prompt-file")
                return Path(cmd[idx + 1])
        self.fail(f"spawn 命令未携带 --append-system-prompt-file: {spawn_cmds}")


# =====================================================================
# A. Claude 成员 —— 非 generation 默认换号（kill/recreate）重启身份重注入
# =====================================================================
class QuotaSwitchClaudeMemberRestartTests(_IsolatedRestartMCP):
    """Claude 成员额度耗尽 → 选新账号 → kill 旧窗 → 重启：argv 必须携带
    --append-system-prompt-file，且身份文件内容是**原逻辑身份**
    （member_name/role/team/leader 不变），账号凭证变化不影响身份。"""

    def _member_team(self, *, workspace=None, agent_user="acct-a", session_id=""):
        workspace = workspace or self.root / "workspace"
        alice = {
            "role": "coder", "agent": "claude",
            "last_task": "build feature X", "last_task_completed": False,
        }
        if agent_user:
            alice["agent_user"] = agent_user
        if session_id:
            alice["session_id"] = session_id
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "claude"}, "alice": alice},
            quota_failover={"enabled": True, "confirm_cycles": 1},
            pool=["acct-a", "acct-b"],
        )
        return workspace

    def test_quota_switch_restart_spawn_carries_system_identity(self):
        ws = self._member_team()
        ok, msg, calls, _ = self._restart("alice", reason="quota_switch", previous_agent_user="acct-a", workspace=ws)
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        self.assertTrue(spawns, "换号重启必须产生 new-window/new-session 命令")
        self.assertTrue(
            any("--append-system-prompt-file" in c for c in spawns),
            "换号重启的 Claude 启动命令必须携带 --append-system-prompt-file（身份进 system 层）",
        )

    def test_quota_switch_restart_append_file_still_binds_logical_identity(self):
        """账号凭证 acct-a→acct-b 变化，但 member_name/role/agent/team/leader 不变。"""
        ws = self._member_team(agent_user="acct-a")
        ok, msg, calls, _ = self._restart("alice", reason="quota_switch", previous_agent_user="acct-a",
                                          workspace=ws, next_agent_user="acct-b")
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        path = self._append_path(spawns)
        content = path.read_text(encoding="utf-8")
        # 原逻辑身份（数据层渲染，与凭证解耦）
        self.assertIn("team='team'", content)
        self.assertIn("member_name='alice'", content)
        self.assertIn("role='coder'", content)
        self.assertIn("agent='claude'", content)
        self.assertIn("leader 是 'lead'", content)
        # 不是 leader 段（成员身份不串号）
        self.assertIn("你不是 leader", content)
        # 凭证（acct-b）绝不出现在身份文件（凭证不是身份）
        self.assertNotIn("acct-b", content)
        # 换号后数据层 agent_user 已切到池下一个
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["agent_user"], "acct-b", "换号后应切到池中下一个账号")

    def test_restart_identity_file_lifecycle_fresh_private_readable(self):
        """R3 临时 prompt 文件生命周期：每次重启生成**新**文件（mkstemp），
        0600 私有，且在 spawn 命令发出前已落盘可读（旧进程退出→新进程启动间安全）。"""
        ws = self._member_team()
        _, _, calls1, _ = self._restart("alice", reason="crash", workspace=ws)
        path1 = self._append_path(self._spawns(calls1))
        _, _, calls2, _ = self._restart("alice", reason="crash", workspace=ws)
        path2 = self._append_path(self._spawns(calls2))
        # 每次重启都是新文件（不复用旧文件，避免陈旧身份/竞写）
        self.assertNotEqual(path1, path2, "每次重启应生成新的身份文件")
        for p in (path1, path2):
            self.assertTrue(p.exists(), f"身份文件在 spawn 时须已落盘可读: {p}")
            self.assertEqual(os.stat(p).st_mode & 0o777, 0o600, f"身份文件须 0600 私有: {p}")
            self.assertIn("member_name='alice'", p.read_text(encoding="utf-8"))

    def test_quota_switch_restart_recovery_context_binds_identity(self):
        """重启后的恢复上下文（send-keys 注入的 user 层）仍携带身份绑定——
        system 层与 user 层双保险，不退回仅普通 prompt。"""
        ws = self._member_team()
        ok, msg, _, sends = self._restart("alice", reason="quota_switch", previous_agent_user="acct-a", workspace=ws)
        self.assertTrue(ok, msg)
        joined = "\n".join(t for _, t in sends)
        self.assertIn("member_name='alice'", joined)
        self.assertIn("role='coder'", joined)
        self.assertIn("agent='claude'", joined)
        self.assertIn("终端恢复通知", joined)


# =====================================================================
# B. Codex 成员 —— AGENTS.md 重新发现 + P1 fail-closed 不回归
# =====================================================================
class QuotaSwitchCodexMemberRestartTests(_IsolatedRestartMCP):
    """Codex 成员换号重启：身份固化到唯一自动装载持久指令文件 AGENTS.md，
    落**显式 workspace**（Codex 从启动 cwd 发现）；缺 workspace_dir 时
    fail-closed 零写入（P1 污染面不得回归）。"""

    def _codex_member_team(self, *, workspace=None, agent_user="codex-a"):
        workspace = workspace or self.root / "workspace"
        bob = {
            "role": "tester", "agent": "codex",
            "last_task": "run tests", "last_task_completed": False,
        }
        if agent_user:
            bob["agent_user"] = agent_user
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "codex"}, "bob": bob},
            quota_failover={"enabled": True, "confirm_cycles": 1},
            pool=["codex-a", "codex-b"],
        )
        return workspace

    def test_codex_quota_switch_restart_writes_agents_md_in_explicit_workspace(self):
        ws = self._codex_member_team(agent_user="codex-a")
        ok, msg, calls, _ = self._restart("bob", reason="quota_switch", previous_agent_user="codex-a",
                                          workspace=ws, next_agent_user="codex-b")
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        self.assertTrue(spawns)
        # 重启命令从显式 workspace 启动（Codex 发现 AGENTS.md 的 cwd 语义）
        self.assertIn("-C", spawns[0])
        self.assertIn(str(ws), spawns[0])
        agents_md = ws / "AGENTS.md"
        self.assertTrue(agents_md.exists(), "Codex 换号重启后团队 workspace 应有 AGENTS.md")
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn("Multi-Agent MCP 团队 'team'", content)
        # 角色中立：共享文件不得绑定具体成员（多角色串线面 B2）
        self.assertNotIn("member_name='bob'", content)
        self.assertNotIn("role='tester'", content)
        # 换号后凭证切换，身份（team 级中立段）不变
        self.assertEqual(mcp._load()["teams"]["team"]["members"]["bob"]["agent_user"], "codex-b")

    def test_codex_quota_switch_restart_fail_closed_no_workspace(self):
        """团队缺 workspace_dir：换号重启也不得在项目/用户仓库根创建 AGENTS.md。"""
        context = self.root / "ctx"
        context.mkdir()
        mcp._save({
            "agent_users": {"codex-a": {"label": "a", "agent_type": "codex"},
                            "codex-b": {"label": "b", "agent_type": "codex"}},
            "teams": {"team": {
                "context_dir": str(context),
                "leader": "lead", "leader_type": "tmux",
                "default_agent": "codex",
                "quota_failover": {"enabled": True, "confirm_cycles": 1},
                "agent_user_pool": ["codex-a", "codex-b"],
                "members": {
                    "lead": {"role": "leader", "agent": "codex"},
                    "bob": {"role": "tester", "agent": "codex",
                            "last_task": "run tests", "last_task_completed": False,
                            "agent_user": "codex-a"},
                },
            }},
        })
        project_root = Path(mcp.PROJECT_DIR)
        # _team_dir 回退 → PROJECT_DIR（模拟用户仓库根）
        ok, msg, _, _ = self._restart("bob", reason="quota_switch", previous_agent_user="codex-a",
                                      workspace=project_root)
        self.assertTrue(ok, msg)
        self.assertFalse(
            (project_root / "AGENTS.md").exists(),
            "缺 workspace_dir 时换号重启也绝不能在项目/用户仓库根写 AGENTS.md（P1 fail-closed）",
        )


# =====================================================================
# C. leader —— 换号重启身份 = leader 段，不串成员段
# =====================================================================
class QuotaSwitchLeaderRestartTests(_IsolatedRestartMCP):
    """leader 额度耗尽换号：与成员同入口（_recover_and_send），但
    is_leader_spawn=True → append 文件渲染 **leader 身份段**（member_name==leader、
    role='leader'），绝不渲染成普通成员段。"""

    def _leader_team(self, *, workspace=None, agent_user="acct-a"):
        workspace = workspace or self.root / "workspace"
        lead = {
            "role": "leader", "agent": "claude",
            "last_task": "lead the team", "last_task_completed": False,
        }
        if agent_user:
            lead["agent_user"] = agent_user
        self._save_team(
            workspace,
            members={"lead": lead, "alice": {"role": "coder", "agent": "claude"}},
            quota_failover={"enabled": True, "confirm_cycles": 1},
            pool=["acct-a", "acct-b"],
        )
        return workspace

    def test_leader_quota_switch_restart_renders_leader_identity(self):
        ws = self._leader_team(agent_user="acct-a")
        ok, msg, calls, _ = self._restart("lead", reason="quota_switch", previous_agent_user="acct-a",
                                          workspace=ws, next_agent_user="acct-b")
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        path = self._append_path(spawns)
        content = path.read_text(encoding="utf-8")
        # leader 身份段（逻辑身份：member_name/role/team 不变，凭证已切）
        self.assertIn("的 leader。", content)
        self.assertIn("member_name='lead'", content)
        self.assertIn("role='leader'", content)
        self.assertIn("团队 'team' 的 leader", content)
        # 不串成普通成员段（"你不是 leader" 是成员段专有标记）
        self.assertNotIn("你不是 leader", content)
        # 凭证变化不影响 leader 身份
        self.assertNotIn("acct-b", content)
        self.assertEqual(mcp._load()["teams"]["team"]["members"]["lead"]["agent_user"], "acct-b")

    def test_leader_quota_switch_restart_recovery_extra_message_is_leader_prompt(self):
        """leader 换号时 _recover_and_send 以 extra_message 注入 leader 系统提示——
        user 层仍是 leader 身份（与 append 文件同源），不串成员。"""
        ws = self._leader_team(agent_user="acct-a")
        # 直接验证 _leader_system_prompt 渲染（换号后 leader 重启消息的来源）
        prompt = mcp._leader_system_prompt("team", "lead the team")
        self.assertIn("的 leader。", prompt)
        self.assertIn("member_name='lead'", prompt)
        self.assertIn("role='leader'", prompt)
        # 与成员段不混淆：不出现"你不是 leader"成员专有措辞
        self.assertNotIn("你不是 leader", prompt)
        self.assertNotIn("member_name='alice'", prompt)


# =====================================================================
# D. generation_migrate（P2 事务式）—— 新窗 ACTIVE / 旧窗 DRAINING / 身份重注入
# =====================================================================
class QuotaSwitchGenerationMigrateRestartTests(_IsolatedRestartMCP):
    """generation_migrate 开启时：换号 spawn 新窗 {member}__g{N+1}（resume_disabled），
    原子提升 ACTIVE、旧窗 DRAINING；新窗仍带身份 append flag，恢复上下文带
    generation 标注。"""

    def test_generation_migrate_spawns_new_window_with_identity_and_no_resume(self):
        workspace = self.root / "workspace"
        alice = {
            "role": "coder", "agent": "claude",
            "last_task": "build feature X", "last_task_completed": False,
            "agent_user": "acct-a", "session_id": "11111111-1111-1111-1111-111111111111",
        }
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "claude"}, "alice": alice},
            quota_failover={"enabled": True, "confirm_cycles": 1, "generation_migrate": True},
            pool=["acct-a", "acct-b"],
        )
        ok, msg, calls, sends = self._restart("alice", reason="quota_switch",
                                              previous_agent_user="acct-a", workspace=workspace,
                                              resume_env=True, next_agent_user="acct-b")
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        self.assertTrue(any("alice__g2" in str(c) for c in spawns), "应 spawn ACTIVE 新窗 alice__g2")
        # 跨凭证迁移：新窗不得原生 resume 旧账号会话（resume_disabled=True）
        for c in spawns:
            self.assertNotIn("--resume", c, "generation 换号新窗不得 --resume 旧账号会话")
            self.assertNotIn("--session-id", c, "generation 换号新窗不得 bind 旧会话 id")
        # 身份仍进 system 层
        self.assertTrue(any("--append-system-prompt-file" in c for c in spawns))
        # 恢复上下文标注新窗口 generation（回报门控依据）
        joined = "\n".join(t for _, t in sends)
        self.assertIn("generation: g2", joined)
        # 状态机：新窗 ACTIVE、旧窗 DRAINING、generation 递增、凭证切换
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        self.assertEqual(member["terminal_generation"], 2)
        by_name = {w["name"]: w for w in member.get("terminal_windows", [])}
        self.assertEqual(by_name["alice__g2"]["status"], "ACTIVE")
        self.assertEqual(by_name["alice"]["status"], "DRAINING")
        self.assertEqual(member["agent_user"], "acct-b")

    def test_non_generation_kill_recreate_also_forces_checkpoint_only(self):
        """coder 修复：非 generation 默认换号（kill/recreate）同样 resume_disabled
        —— 与 generation 路径同规则（P2 跨凭证迁移不原生 resume 旧账号会话）。"""
        workspace = self.root / "workspace"
        alice = {
            "role": "coder", "agent": "claude",
            "last_task": "build feature X", "last_task_completed": False,
            "agent_user": "acct-a", "session_id": "11111111-1111-1111-1111-111111111111",
        }
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "claude"}, "alice": alice},
            quota_failover={"enabled": True, "confirm_cycles": 1},
            pool=["acct-a", "acct-b"],
        )
        ok, msg, calls, _ = self._restart("alice", reason="quota_switch",
                                          previous_agent_user="acct-a", workspace=workspace,
                                          resume_env=True)
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        for c in spawns:
            self.assertNotIn("--resume", c, "默认换号路径也不得原生 resume 旧账号会话")
            self.assertNotIn("--session-id", c, "默认换号路径也不得 bind 旧会话 id")
        self.assertTrue(any("--append-system-prompt-file" in c for c in spawns),
                        "checkpoint-only 换号仍须身份进 system 层")


# =====================================================================
# E. resume 语义 —— 换号不退回普通 user prompt；crash 恢复仍允许 resume
# =====================================================================
class QuotaSwitchResumeSemanticsTests(_IsolatedRestartMCP):
    """resume/首启均不退回普通 user prompt：无论 quota 换号（强制 checkpoint-only）
    还是 crash 恢复（允许 resume/bind），Claude 启动 argv 都恒带
    --append-system-prompt-file —— 身份在 system 层，抗 /compact 与 resume。"""

    def test_crash_recovery_with_resume_still_carries_system_identity(self):
        workspace = self.root / "workspace"
        alice = {
            "role": "coder", "agent": "claude",
            "last_task": "build feature X", "last_task_completed": False,
            "agent_user": "acct-a", "session_id": "11111111-1111-1111-1111-111111111111",
        }
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "claude"}, "alice": alice},
            pool=["acct-a", "acct-b"],
        )
        # crash 恢复（默认 reason）：resume 未禁用 → 可 bind 会话 id（保留对话上下文）
        ok, msg, calls, _ = self._restart("alice", reason="crash", workspace=workspace, resume_env=True)
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        self.assertTrue(any("--append-system-prompt-file" in c for c in spawns),
                        "crash 恢复（含 resume/bind）也须身份进 system 层")
        # crash 恢复允许会话续接（与换号不同）
        self.assertTrue(
            any("--session-id" in c or "--resume" in c for c in spawns),
            "crash 恢复（resume 未禁用）应允许会话续接（--session-id/--resume）",
        )

    def test_quota_switch_identity_in_system_layer_not_plain_prompt(self):
        """换号重启的最终可观察出口：身份经 --append-system-prompt-file 进入 system 层，
        不是仅靠恢复上下文首条 user 消息（compact 会摘除首条 user 消息 → 身份遗忘根因）。"""
        ws = self._member_team_for_resume()
        ok, msg, calls, sends = self._restart("alice", reason="quota_switch",
                                              previous_agent_user="acct-a", workspace=ws,
                                              resume_env=True)
        self.assertTrue(ok, msg)
        spawns = self._spawns(calls)
        # system 层：append flag 恒在
        self.assertTrue(any("--append-system-prompt-file" in c for c in spawns))
        # 即便 user 层恢复上下文被 compact 摘除，system 层身份仍可恢复
        path = self._append_path(spawns)
        self.assertIn("member_name='alice'", path.read_text(encoding="utf-8"))

    def _member_team_for_resume(self):
        workspace = self.root / "workspace"
        alice = {
            "role": "coder", "agent": "claude",
            "last_task": "build feature X", "last_task_completed": False,
            "agent_user": "acct-a", "session_id": "11111111-1111-1111-1111-111111111111",
        }
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "claude"}, "alice": alice},
            quota_failover={"enabled": True, "confirm_cycles": 1},
            pool=["acct-a", "acct-b"],
        )
        return workspace


# =====================================================================
# F. 端到端 —— _scan_member_terminal quota 分支 → 池选择 → 重启 → argv
# =====================================================================
class QuotaSwitchScanEndToEndTests(_IsolatedRestartMCP):
    """完整换号链端到端：后台监控 _scan_member_terminal 识别 quota →
    池选择新账号 → _recover_and_send 重启 → 观察 spawn argv 与身份文件。
    不 mock _tmux_spawn_member（真实执行），只 mock 终端捕获/发送层。"""

    def _scan_alice_quota(self, *, generation_migrate=False):
        workspace = self.root / "workspace"
        alice = {
            "role": "coder", "agent": "claude",
            "last_task": "build feature X", "last_task_completed": False,
            "agent_user": "acct-a",
        }
        qf = {"enabled": True, "confirm_cycles": 1}
        if generation_migrate:
            qf["generation_migrate"] = True
        self._save_team(
            workspace,
            members={"lead": {"role": "leader", "agent": "claude"}, "alice": alice},
            quota_failover=qf,
            pool=["acct-a", "acct-b"],
        )
        calls = []
        sends = []

        def fake_tmux(cmd, timeout=10):
            calls.append(cmd)
            if cmd[0] == "has-session":
                return 0, "", ""
            if cmd[0] == "list-windows":
                return 0, "$1\t1000\t@1\t__base", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", side_effect=lambda t, m: m), \
             mock.patch.object(mcp, "_capture_window", return_value=(0, QUOTA_CAPTURE, "")), \
             mock.patch.object(mcp, "_tmux", side_effect=fake_tmux), \
             mock.patch.object(mcp, "_send_keys",
                               side_effect=lambda s, w, text, **kw: sends.append((w, text)) or (0, "")), \
             mock.patch.object(mcp, "_write_claude_permissions",
                               return_value=str(workspace / ".claude" / "settings.json")), \
             mock.patch.object(mcp, "_write_claude_mcp", return_value=str(workspace / ".mcp.json")), \
             mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")), \
             mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None), \
             mock.patch.object(mcp, "_record_recovery_event", return_value=None), \
             mock.patch.object(mcp.time, "sleep", return_value=None):
            result = mcp._scan_member_terminal("team", "alice")
        member = mcp._load()["teams"]["team"]["members"]["alice"]
        return result, member, calls, sends

    def test_scan_quota_confirmed_restarts_with_system_identity(self):
        """监控识别 quota → 选新账号 → 重启：换号成功、argv 带身份 append flag、
        身份文件仍是原逻辑身份。"""
        result, member, calls, _ = self._scan_alice_quota()
        self.assertEqual(result["action"], "quota-switched:acct-b")
        self.assertEqual(member["agent_user"], "acct-b")
        spawns = [c for c in calls if c[0] in ("new-session", "new-window")]
        self.assertTrue(spawns, "端到端换号必须产生重启命令")
        self.assertTrue(any("--append-system-prompt-file" in c for c in spawns),
                        "端到端换号重启 argv 必须携带身份 append flag")
        for c in spawns:
            if "--append-system-prompt-file" in c:
                path = Path(c[c.index("--append-system-prompt-file") + 1])
                content = path.read_text(encoding="utf-8")
                self.assertIn("member_name='alice'", content)
                self.assertIn("role='coder'", content)
                self.assertNotIn("acct-b", content, "凭证不得混入身份文件")
                return
        self.fail("未找到身份文件")

    def test_scan_quota_generation_migrate_restarts_active_new_window(self):
        """端到端 + generation_migrate：新窗 ACTIVE、旧窗 DRAINING、凭证切换、身份在。"""
        result, member, calls, _ = self._scan_alice_quota(generation_migrate=True)
        self.assertEqual(result["action"], "quota-switched:acct-b")
        self.assertEqual(member["terminal_generation"], 2)
        by_name = {w["name"]: w for w in member.get("terminal_windows", [])}
        self.assertEqual(by_name["alice__g2"]["status"], "ACTIVE")
        self.assertEqual(by_name["alice"]["status"], "DRAINING")
        spawns = [c for c in calls if c[0] in ("new-session", "new-window")]
        self.assertTrue(any("--append-system-prompt-file" in c for c in spawns),
                        "generation 换号新窗也须身份进 system 层")


if __name__ == "__main__":
    unittest.main()
