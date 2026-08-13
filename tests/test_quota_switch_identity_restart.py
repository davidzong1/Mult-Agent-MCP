"""
额度切换重启·身份重新注入聚焦回归（coder-claude 新增独立文件，不与 tester 冲突）
================================================================================

共同目标「额度不足切换用户池账号的完整流程」在「quota 换号重启」链路上的可观察
出口回归（覆盖共同验收 1-5）：

  1) 新进程启动参数重新加载正确身份：Claude quota 换号重建 spawn 必带
     ``--append-system-prompt-file``（身份进 system 层，抗 compact/resume）；
  2) member/role/agent/team/leader 关系不串号：leader quota 换号渲染 leader 身份段，
     普通成员渲染成员身份段；身份/角色从持久层每次 spawn fresh 重读；
  3) 临时身份文件生命周期：spawn 命令引用的文件在启动前已生成（0600）、每次换号
     mkstemp 唯一新路径（不引用已清理/旧文件）；
  4) Codex AGENTS.md 安全落点：显式 workspace_dir 写入、fail-closed 不污染项目根
     （既有规则不动，本文件验重启路径不回归）；
  5) P2 跨凭证换号不得原生 resume 旧账号会话：generation_migrate 路径已由 tester
     ``test_session_resume_wiring`` 覆盖；本文件补 **kill/recreate 默认路径**
     ``_recover_and_send(reason="quota_switch")`` —— 修复后即使 session_resume 开启、
     旧账号转录存在，也强制 checkpoint-only（无 ``--resume``/``--session-id``）；
     crash 恢复（reason="crash"）仍保留 resume（回归护栏）。

边界：不触真实付费账户/不消耗额度；隔离数据（data_layer.set_data_file）+ 终端 mock
（_tmux 捕获 spawn 命令）；不碰真实 ~/.claude 与仓库根。
"""

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer, session_resume


class _IsolatedQuotaSwitch(unittest.TestCase):
    """镜像 tests/test_session_resume_wiring 的隔离 setUp + spawn 捕获。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.workspace = self.root / "ws"
        self.workspace.mkdir()
        self.claude_home = self.root / "claude_home"
        self.codex_home = self.root / "codex_home"
        self.old_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "TEAM_WORKSPACES_DIR": mcp.TEAM_WORKSPACES_DIR,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
            "SHARE_WORKSPACE_DIR": mcp.SHARE_WORKSPACE_DIR,
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
        }
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE", "ORIGINAL_CWD", "INIT_CWD", "PWD")
        }
        self.old_resume_env = os.environ.get(session_resume.RESUME_FLAG_ENV)
        self.old_codex_home_env = os.environ.get("CODEX_HOME")
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        for key in self.old_env:
            os.environ.pop(key, None)
        os.environ.pop(session_resume.RESUME_FLAG_ENV, None)
        os.environ["CODEX_HOME"] = str(self.codex_home)
        # claude 转录根指向临时 claude_home，绝不触真实 ~/.claude
        self._claude_home_patcher = mock.patch.object(
            mcp, "_member_claude_config_home", return_value=str(self.claude_home))
        self._claude_home_patcher.start()
        self.addCleanup(self._claude_home_patcher.stop)

    def tearDown(self):
        self._claude_home_patcher.stop()
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        for key, old in ((session_resume.RESUME_FLAG_ENV, self.old_resume_env),
                         ("CODEX_HOME", self.old_codex_home_env)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _save(self, data: dict) -> None:
        mcp._save(data)

    def _team(self, members=None, *, leader="lead", leader_type="tmux"):
        context = self.root / "context"
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(self.workspace),
            "context_dir": str(context),
            "terminals_active": False,
            "leader": leader,
            "leader_type": leader_type,
            "members": {leader: {"role": "leader", "agent": "claude"}},
        }
        for name, info in (members or {}).items():
            team["members"][name] = info
        self._save({"teams": {"team": team}})
        return mcp._load()["teams"]["team"]

    def _member(self, name="alice"):
        return mcp._load()["teams"]["team"]["members"][name]

    def _set_resume(self, on=True):
        if on:
            os.environ[session_resume.RESUME_FLAG_ENV] = "1"
        else:
            os.environ.pop(session_resume.RESUME_FLAG_ENV, None)

    def _set_session_and_transcript(self, name="alice"):
        """持久化成员 session_id + 写旧账号转录（resume 本应成功的前提）。"""
        sid = session_resume.new_session_id()
        data = mcp._load()
        data["teams"]["team"]["members"][name]["session_id"] = sid
        mcp._save(data)
        encoded = session_resume.encode_project_dir(str(self.workspace))
        proj = self.claude_home / "projects" / encoded
        proj.mkdir(parents=True, exist_ok=True)
        (proj / f"{sid}.jsonl").write_text("x\n", encoding="utf-8")
        return sid

    def _set_agent_user(self, name, new_user):
        data = mcp._load()
        data["teams"]["team"]["members"][name]["agent_user"] = new_user
        mcp._save(data)

    def _spawn_capture(self):
        calls = []

        def fake_tmux(cmd):
            calls.append(cmd)
            return (0, "", "")

        return calls, fake_tmux

    @staticmethod
    def _find_spawn(calls):
        for c in calls:
            if c and c[0] in ("new-window", "new-session"):
                return c
        return None

    def _quota_switch_recover(self, name, calls, fake_tmux, *, previous="acct-a"):
        """经 _recover_and_send 走 quota_switch kill/recreate 默认路径（mock 终端）。"""
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_write_claude_mcp", return_value=""):
                    with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                        with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                                return mcp._recover_and_send(
                                    "team", name, "mcp_team",
                                    reason="quota_switch", previous_agent_user=previous,
                                )

    def _crash_recover(self, name, calls, fake_tmux):
        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            with mock.patch.object(mcp, "_member_window_state", return_value=("absent", "")):
                with mock.patch.object(mcp, "_write_claude_mcp", return_value=""):
                    with mock.patch.object(mcp, "_ensure_codex_mcp", return_value=""):
                        with mock.patch.object(mcp, "_write_claude_permissions", return_value=""):
                            with mock.patch.object(mcp, "_send_keys", return_value=(0, "")):
                                return mcp._recover_and_send("team", name, "mcp_team")

    @staticmethod
    def _append_file(spawn) -> Path:
        idx = spawn.index("--append-system-prompt-file")
        return Path(spawn[idx + 1])


# =====================================================================
# A. 核心修复：quota 换号 kill/recreate 默认路径 —— 身份重注入 + 禁止原生 resume
# =====================================================================
class TestQuotaSwitchKillRecreateIdentity(_IsolatedQuotaSwitch):

    def _alice(self, **extra):
        m = {"role": "coder", "agent": "claude"}
        m.update(extra)
        return m

    def test_quota_switch_reinjects_identity_and_suppresses_resume(self):
        """验收 1+5：quota 换号重建 spawn 带新身份文件（system 层），且即使
        session_resume 开启、旧账号转录存在也绝不 --resume（只 checkpoint）。"""
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        self._set_session_and_transcript("alice")     # 旧账号转录存在（诱饵）
        self._set_agent_user("alice", "acct-b")
        calls, fake_tmux = self._spawn_capture()
        ok, msg = self._quota_switch_recover("alice", calls, fake_tmux)
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn, "未捕获换号重建 spawn")
        # 1) 身份重新注入 system 层
        self.assertIn("--append-system-prompt-file", spawn,
                      "quota 换号重建必须重新注入身份（append flag）")
        ipath = self._append_file(spawn)
        self.assertTrue(ipath.exists(), f"身份文件应在启动前生成: {ipath}")
        content = ipath.read_text(encoding="utf-8")
        for anchor in ("team", "alice", "coder", "claude"):
            self.assertIn(anchor, content, f"换号后身份文件缺锚点 {anchor!r}")
        # 5) 跨凭证不原生 resume
        self.assertNotIn("--resume", spawn, "跨凭证换号不得 --resume 旧账号会话")
        self.assertNotIn("--session-id", spawn, "跨凭证换号只 checkpoint，不绑旧会话")

    def test_crash_recovery_keeps_resume(self):
        """回归护栏：修复只作用于 quota_switch；crash 恢复仍保留 resume 恢复对话上下文。"""
        self._team(members={"alice": self._alice()})
        self._set_resume(True)
        sid = self._set_session_and_transcript("alice")
        calls, fake_tmux = self._spawn_capture()
        ok, msg = self._crash_recover("alice", calls, fake_tmux)
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertIn("--append-system-prompt-file", spawn, "crash 恢复也须带身份")
        self.assertIn("--resume", spawn, "crash 恢复应原生 resume（非换号）")
        self.assertIn(sid, spawn)
        self.assertTrue(self._append_file(spawn).exists())

    def test_identity_file_unique_per_switch_and_generated_before_launch(self):
        """验收 3：身份文件每次换号 mkstemp 唯一新路径（不引用旧/已清理文件），
        且 spawn 命令引用时文件已生成（0600 私有）。"""
        self._team(members={"alice": self._alice()})
        self._set_agent_user("alice", "acct-b")
        # 第一次换号
        calls1, fake_tmux1 = self._spawn_capture()
        self.assertTrue(self._quota_switch_recover("alice", calls1, fake_tmux1)[0])
        spawn1 = self._find_spawn(calls1)
        p1 = self._append_file(spawn1)
        mode = stat.S_IMODE(os.stat(p1).st_mode)
        self.assertEqual(mode & 0o077, 0, f"身份文件权限过宽: {oct(mode)}")
        # 第二次换号 → 必须生成不同新文件
        self._set_agent_user("alice", "acct-c")
        calls2, fake_tmux2 = self._spawn_capture()
        self.assertTrue(self._quota_switch_recover("alice", calls2, fake_tmux2)[0])
        spawn2 = self._find_spawn(calls2)
        p2 = self._append_file(spawn2)
        self.assertNotEqual(p1, p2, "每次换号必须新身份文件（不得复用旧路径）")
        self.assertTrue(p1.exists() and p2.exists(), "启动前生成 + 运行期不清理")

    def test_identity_reloads_persisted_role(self):
        """验收 2：身份/角色数据每次 spawn 从持久层 fresh 重读（不缓存旧记录）。"""
        self._team(members={"alice": self._alice(role="coder")})
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["role"] = "tester"
        mcp._save(data)                                  # 持久层已改成 tester
        self._set_agent_user("alice", "acct-b")
        calls, fake_tmux = self._spawn_capture()
        self.assertTrue(self._quota_switch_recover("alice", calls, fake_tmux)[0])
        spawn = self._find_spawn(calls)
        content = self._append_file(spawn).read_text(encoding="utf-8")
        self.assertIn("role='tester'", content, "换号重建必须重读持久层最新角色")


# =====================================================================
# B. leader quota 换号：身份渲染 leader 段，不串成员段；同样禁止 resume
# =====================================================================
class TestLeaderQuotaSwitchIdentity(_IsolatedQuotaSwitch):

    def test_leader_quota_switch_renders_leader_identity_no_resume(self):
        """验收 2+5：leader 走 _recover_and_send(quota_switch) 同规则 —— 渲染 leader
        身份段（不串成成员段）+ 禁止原生 resume。"""
        self._team(members={})                            # leader=lead, role=leader
        self._set_resume(True)
        self._set_session_and_transcript("lead")
        self._set_agent_user("lead", "acct-b")
        calls, fake_tmux = self._spawn_capture()
        ok, msg = self._quota_switch_recover("lead", calls, fake_tmux)
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        self.assertIn("--append-system-prompt-file", spawn)
        self.assertNotIn("--resume", spawn, "leader 跨凭证换号也不得 resume")
        content = self._append_file(spawn).read_text(encoding="utf-8")
        self.assertIn("的 leader", content, "leader 换号身份文件应渲染 leader 段")
        self.assertIn("role='leader'", content)
        self.assertNotIn("你不是 leader", content, "leader 身份段不得混入成员身份段的否定措辞")
        self.assertNotIn("你的团队成员身份绑定", content, "leader 身份段不得混入成员身份绑定段")


# =====================================================================
# C. Codex quota 换号：AGENTS.md 安全落点 + 不 resume（验收 4）
# =====================================================================
class TestCodexQuotaSwitchSafeLanding(_IsolatedQuotaSwitch):

    def _bob(self):
        return {"role": "coder", "agent": "codex"}

    def test_codex_quota_switch_writes_agents_md_in_safe_cwd_no_resume(self):
        self._team(members={"bob": self._bob()})
        self._set_resume(True)
        self._set_agent_user("bob", "acct-b")
        calls, fake_tmux = self._spawn_capture()
        ok, msg = self._quota_switch_recover("bob", calls, fake_tmux)
        self.assertTrue(ok, msg)
        spawn = self._find_spawn(calls)
        self.assertIsNotNone(spawn)
        # 换号重建仍在安全 cwd（workspace = 显式 workspace_dir）写入角色中立 AGENTS.md
        agents_md = self.workspace / "AGENTS.md"
        self.assertTrue(agents_md.exists(), "Codex 换号重建应写 AGENTS.md（安全 cwd）")
        content = agents_md.read_text(encoding="utf-8")
        self.assertIn("Multi-Agent", content)
        self.assertNotIn("member_name='bob'", content, "AGENTS.md 必须角色中立")
        # 跨凭证不原生 resume
        self.assertNotIn("resume", spawn)
        self.assertNotIn("--session-id", spawn)


if __name__ == "__main__":
    unittest.main()
