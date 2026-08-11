"""
P1 接线返工：common/checkpoint 接入 _recover_and_send 的 quota_switch/recovery 路径。

覆盖验收要求（③ tester 重点核对）：
  1. 有 checkpoint 时，恢复消息包含 checkpoint 指针与 verify-then-continue，
     已完成步骤明确列出不重做 —— 不再只重发空白 last_task。
  2. spawn 失败保留 checkpoint / 旧状态（不丢失、不篡改、不误发恢复消息）。
  3. 无 checkpoint 诚实回落现状（空白重发 last_task，无 checkpoint 段）。
  4. crash 恢复路径同样受益（恢复消息携带 checkpoint）。
  5. checkpoint 读取必须传 TEAM_DATA_LOCK（与 leader 数据路径互斥）。

实现位置：_build_member_checkpoint_section（持 TEAM_DATA_LOCK 读最新 checkpoint）
接线进 _build_recovery_context（_recover_and_send / 成员重启 / 授权重启共用）。

隔离：复用 test_checkpoint_gate_isolation 的 mcp 模块全局临时覆盖模式 +
data_layer.set_data_file 对齐同一文件（conftest 环境级隔离 + fail-fast 守卫）。
"""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import checkpoint as ckpt
from common import data_layer


class TestQuotaCheckpointWiring(unittest.TestCase):
    """P1 接线隔离用例：quota 换号 / 崩溃恢复从 checkpoint 续跑。"""

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
            "CLAUDE_GLOBAL_CONFIG_PATH": mcp.CLAUDE_GLOBAL_CONFIG_PATH,
            "_OLD_DATA_FILE": mcp._OLD_DATA_FILE,
            "_OLD_SHARE_CONTEXT_DIR": mcp._OLD_SHARE_CONTEXT_DIR,
            "TEAM_DATA_LOCK": mcp.TEAM_DATA_LOCK,
        }
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "MULT_AGENT_MCP_WORKSPACE", "CODEX_WORKSPACE",
                "ORIGINAL_CWD", "INIT_CWD", "PWD", "MULT_AGENT_MCP_CONTEXT_DIR",
            )
        }
        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.TEAM_WORKSPACES_DIR = str(project / ".team_workspaces")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        mcp.SHARE_WORKSPACE_DIR = str(project / "share_work_space")
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
        # common.checkpoint 经 data_layer 读写，须与 mcp.DATA_FILE 对齐同一文件，
        # 否则 ckpt 写 conftest 临时文件、mcp._load 读 project 文件，两侧数据分离。
        data_layer.set_data_file(Path(mcp.DATA_FILE))
        for key in self.old_env:
            os.environ.pop(key, None)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _team(self, *, members=None):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": "direct",
            "leader_last_task": "build P0",
            "leader_last_task_completed": False,
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
            },
        }
        for name, info in (members or {}).items():
            team["members"][name] = info
        mcp._save({"teams": {"team": team}})
        return team

    def _alice(self, *, last_task="", last_context="", completed=True):
        return {
            "role": "coder",
            "agent": "claude",
            "last_task": last_task,
            "last_context": last_context,
            "last_task_completed": completed,
        }

    def _seed_checkpoint(self, *, member="alice", steps=("design", "code"),
                         current="test", remaining="继续编码，完成后回报"):
        cp = ckpt.empty_checkpoint("t1", task="实现 checkpoint 模块", writer=member)
        for s in steps:
            cp = ckpt.record_step_done(cp, s)
        cp["current_step"] = current
        cp["remaining_instruction"] = remaining
        ok, err = ckpt.save_checkpoint(
            team_name="team", member_name=member, cp=cp, writer=member,
        )
        self.assertTrue(ok, err)
        loaded, errors = ckpt.load_checkpoint(team_name="team", member_name=member)
        self.assertEqual(errors, [])
        return loaded

    def _call_recover(self, *, reason="quota_switch", spawn_rc=0, spawn_err=""):
        """调用 _recover_and_send，捕获恢复消息；返回 (ok, msg, sent_texts)。"""
        sent: list[str] = []
        spawn_err = spawn_err or ("spawn error" if spawn_rc else "")

        def _send_keys(*args, **kwargs):
            if len(args) >= 3:
                sent.append(args[2])
            return (0, "")

        with mock.patch.object(mcp, "_find_any_session", return_value="mcp_team"), \
             mock.patch.object(mcp, "_member_window_target", return_value="alice"), \
             mock.patch.object(mcp, "_tmux", return_value=(0, "", "")), \
             mock.patch.object(mcp, "_tmux_spawn_member", return_value=(spawn_rc, "", spawn_err)), \
             mock.patch.object(mcp, "_send_keys", side_effect=_send_keys), \
             mock.patch.object(mcp.time, "sleep", return_value=None), \
             mock.patch.object(mcp, "_write_claude_mcp", return_value=None), \
             mock.patch.object(mcp, "_ensure_codex_mcp", return_value=None), \
             mock.patch.object(mcp, "_save_death_context_snapshot", return_value=""), \
             mock.patch.object(mcp, "_record_recovery_event", return_value=None):
            ok, msg = mcp._recover_and_send("team", "alice", "mcp_team", reason=reason)
        return ok, msg, sent

    def _member(self):
        return mcp._load()["teams"]["team"]["members"]["alice"]

    # ==================================================================
    # ① 有 checkpoint：恢复消息含进度指针 + verify-then-continue，不空白重做
    # ==================================================================

    def test_quota_switch_recovery_includes_checkpoint_not_blank_replay(self):
        """quota 换号恢复消息携带 checkpoint 指针（已完成步骤/当前步骤/续跑指令/
        epoch/writer + verify-then-continue 规则），而不是只重发空白 last_task。"""
        self._team(members={
            "alice": self._alice(last_task="实现 checkpoint 模块", completed=False),
        })
        self._seed_checkpoint()
        before, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")

        ok, msg, sent = self._call_recover(reason="quota_switch")
        self.assertTrue(ok, msg)
        self.assertTrue(sent, "应发送恢复消息")
        recovery = sent[0]

        # checkpoint 指针：任务 / 已完成步骤 / 当前步骤 / 续跑指令 / writer+epoch
        self.assertIn("成员任务 Checkpoint", recovery)
        self.assertIn("实现 checkpoint 模块", recovery)
        self.assertIn("design", recovery)
        self.assertIn("code", recovery)
        self.assertIn("当前步骤: test", recovery)
        self.assertIn("继续编码，完成后回报", recovery)
        self.assertIn("alice", recovery)
        self.assertIn(f"epoch={before['epoch']}", recovery)

        # verify-then-continue 规则：已完成步骤不重做、产物先核对
        self.assertIn("续跑规则(verify-then-continue)", recovery)
        self.assertIn("不重做", recovery)
        self.assertIn("产物哈希", recovery)

        # 有 checkpoint 不得只重发空白 last_task —— 结构化进度已进恢复消息
        self.assertIn("上次未完成任务", recovery)

        # quota 换号：独立计数递增，checkpoint 本身不被改动（epoch 保留）
        after, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        self.assertEqual(after["epoch"], before["epoch"])
        self.assertEqual(after["completed_steps"], ["design", "code"])
        self.assertEqual(self._member()["quota_switch_count"], 1)

    def test_crash_recovery_also_includes_checkpoint(self):
        """crash（非 quota）恢复同样携带 checkpoint —— 所有恢复路径一致续跑。"""
        self._team(members={
            "alice": self._alice(last_task="实现 checkpoint 模块", completed=False),
        })
        self._seed_checkpoint(steps=("design",), current="code")

        ok, msg, sent = self._call_recover(reason="crash")
        self.assertTrue(ok, msg)
        self.assertIn("成员任务 Checkpoint", sent[0])
        self.assertIn("design", sent[0])
        # crash 走 recovery_count，不触碰 quota 独立计数
        member = self._member()
        self.assertEqual(member["recovery_count"], 1)
        self.assertEqual(member.get("quota_switch_count", 0), 0)

    # ==================================================================
    # ② spawn 失败：保留 checkpoint / 旧状态，不误发恢复消息
    # ==================================================================

    def test_quota_spawn_failure_preserves_checkpoint_and_old_state(self):
        """spawn 失败返回可见错误；checkpoint 与旧任务状态完整保留，恢复消息未发出。"""
        self._team(members={
            "alice": self._alice(
                last_task="实现 checkpoint 模块", last_context="ctx", completed=False,
            ),
        })
        self._seed_checkpoint()
        before, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")

        ok, msg, sent = self._call_recover(reason="quota_switch", spawn_rc=1)
        self.assertFalse(ok)
        self.assertIn("终端重建失败", msg)
        self.assertIn("spawn error", msg)
        # 窗口没起来，不应发送任何恢复消息
        self.assertEqual(sent, [])

        # checkpoint 完整保留（epoch/字段不变）
        after, errors = ckpt.load_checkpoint(team_name="team", member_name="alice")
        self.assertEqual(errors, [])
        self.assertEqual(after["epoch"], before["epoch"])
        self.assertEqual(after["completed_steps"], ["design", "code"])
        self.assertEqual(after["current_step"], "test")
        self.assertEqual(after["remaining_instruction"], "继续编码，完成后回报")
        self.assertEqual(after["writer"], "alice")

        # 旧状态保留（last_task / last_context / 完成标记未被清空或篡改）
        member = self._member()
        self.assertEqual(member["last_task"], "实现 checkpoint 模块")
        self.assertEqual(member["last_context"], "ctx")
        self.assertEqual(member["last_task_completed"], False)

    # ==================================================================
    # ③ 无 checkpoint：诚实回落现状（空白重发 last_task，无 checkpoint 段）
    # ==================================================================

    def test_no_checkpoint_graceful_fallback_to_last_task_replay(self):
        """无 checkpoint 时恢复消息不含 checkpoint 段，按现状重发 last_task 从头做。"""
        self._team(members={
            "alice": self._alice(last_task="修复登录 bug", completed=False),
        })

        ok, msg, sent = self._call_recover(reason="quota_switch")
        self.assertTrue(ok, msg)
        recovery = sent[0]

        # 无 checkpoint → 不渲染任何 checkpoint 行（诚实回落）
        self.assertNotIn("成员任务 Checkpoint", recovery)
        self.assertNotIn("续跑规则", recovery)
        self.assertNotIn("不重做", recovery)

        # 仍按既有行为重发 last_task（无 checkpoint 时的现状路径不变）
        self.assertIn("上次未完成任务: 修复登录 bug", recovery)
        self.assertEqual(self._member()["quota_switch_count"], 1)

    def test_corrupt_checkpoint_degrades_with_visible_warning(self):
        """磁盘 checkpoint 非法时给出可见降级提示，不崩、不静默续跑脏数据。"""
        self._team(members={
            "alice": self._alice(last_task="实现 checkpoint 模块", completed=False),
        })
        # 写入非法 checkpoint（epoch 类型损坏）
        data = mcp._load()
        data["teams"]["team"]["members"]["alice"]["task_checkpoint"] = {
            "task_id": "t1", "epoch": "x", "writer": "alice",
        }
        mcp._save(data)

        ok, msg, sent = self._call_recover(reason="quota_switch")
        self.assertTrue(ok, msg)
        self.assertIn("checkpoint 非法", sent[0])
        self.assertIn("回落为空白重发", sent[0])

    # ==================================================================
    # ④ 数据契约：checkpoint 读取传 TEAM_DATA_LOCK（与 leader 数据路径互斥）
    # ==================================================================

    def test_checkpoint_read_passes_team_data_lock(self):
        """_build_member_checkpoint_section 读取 checkpoint 必须传 TEAM_DATA_LOCK。"""
        self._team(members={
            "alice": self._alice(last_task="实现 checkpoint 模块", completed=False),
        })
        self._seed_checkpoint()

        captured: dict = {}
        orig = mcp.checkpoint.load_checkpoint

        def spy(lock=None, **kwargs):
            captured["lock"] = lock
            return orig(lock, **kwargs)

        with mock.patch.object(mcp.checkpoint, "load_checkpoint", side_effect=spy):
            ok, msg, sent = self._call_recover(reason="quota_switch")
        self.assertTrue(ok, msg)
        self.assertIn("成员任务 Checkpoint", sent[0])
        self.assertIs(captured["lock"], mcp.TEAM_DATA_LOCK)


if __name__ == "__main__":
    unittest.main()
