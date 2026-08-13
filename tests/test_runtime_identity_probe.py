"""
运行时隔离探针 —— 验证注入身份/角色约束在派单-回报-恢复闭环中生效
================================================================

任务（leader 子任务）：设计并执行运行时隔离探针，验证
  a) 成员收到的任务是否遵循注入身份/角色约束（派单路由 + system 层身份一致）
  b) 回报是否进入 leader 闭环（results.jsonl + leader_pending_reports + 唤醒/复活）
  c) 覆盖：至少一个正常分派 + 一个 leader 恢复/重启边界

证据层级（诚实标注，不把 mock 当实机）：
  [L1 真实生产函数] leader_assign_subtask / member_report_result /
      _revive_leader_terminal_locked / _tmux_spawn_member / prompt_registry
      渲染均执行真实代码路径，仅 mock tmux IPC 边界(_tmux/_send_keys/_capture_window)
      与无关副作用（权限/配置写入）。这属于"MCP/文件层证据"，不是真实 CLI 消费。
  [L1 真实文件产物] 身份临时文件、results.jsonl、member_contexts/ 压缩上下文、
      AGENTS.md 均为真实磁盘文件，断言直接读盘。
  [L2 未覆盖] 真实 claude/codex CLI 对 --append-system-prompt-file / AGENTS.md
      的实际装载——本探针不启动真实 CLI（禁真账号/额度），该项标注为残余风险。

隔离方式镜像 tests/test_quota_switch_restart_identity.py / test_prompt_identity_system_layer.py：
temp 项目根 + data_layer override，绝不触碰真实 teams_data.json / 真实 tmux session。
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from common import prompt_registry

TEAM = "team"
SESSION = "mcp_team"


class _IsolatedProbeMCP(unittest.TestCase):
    """隔离团队数据 + tmux mock，供运行时探针使用。"""

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
        # 探针用独立 team 会话名，绝不触碰真实 tmux session
        self._calls = []
        self._sends = []

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

    # -- 数据构造 ----------------------------------------------------------

    def _save_team(self, workspace, *, members, leader_task="", terminals_active=True):
        workspace = Path(workspace)
        workspace.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "leader": "lead",
            "leader_type": "tmux",
            "default_agent": "claude",
            "terminals_active": terminals_active,
            "members": members,
        }
        if leader_task:
            team["leader_last_task"] = leader_task
            team["leader_last_task_completed"] = False
        data = {"teams": {TEAM: team}}
        mcp._save(data)
        return workspace

    def _full_team(self):
        ws = self.root / "workspace"
        return self._save_team(
            ws,
            members={
                "lead": {"role": "leader", "agent": "claude"},
                "reviewer": {"role": "reviewer", "agent": "claude"},
                "coder": {"role": "coder", "agent": "claude"},
                "refactor": {"role": "refactor", "agent": "claude"},
                "tester": {"role": "tester", "agent": "claude"},
            },
        )

    # -- tmux mock --------------------------------------------------------

    def fake_tmux(self, cmd, timeout=10):
        """可观测 tmux IPC mock：记录所有命令，按操作返回确定性输出。"""
        self._calls.append(list(cmd))
        op = cmd[0]
        if op == "has-session":
            return 0, "", ""
        if op == "list-sessions":
            return 0, f"{SESSION}\n", ""
        if op == "list-windows":
            # 默认全员窗口存活（@1..@5 = reviewer/coder/refactor/tester/lead）
            return 0, self._windows_text(), ""
        if op == "capture-pane":
            # 默认返回 Claude 就绪提示 → 分类为 idle（leader 存活且空闲）
            return 0, "❯\n", ""
        return 0, "", ""

    def _windows_text(self, *exclude):
        lines = [
            "$1\t1000\t@1\treviewer",
            "$1\t1000\t@2\tcoder",
            "$1\t1000\t@3\trefactor",
            "$1\t1000\t@4\ttester",
            "$1\t1000\t@5\tlead",
        ]
        return "\n".join(l for l in lines if not any(f"\t{n}" in l for n in exclude)) + "\n"

    def _tmux_fake_without(self, *names):
        """构造 tmux fake：list-windows 不含指定成员窗口（触发 absent → 真正 spawn）。"""
        orig = self.fake_tmux

        def fake(cmd, timeout=10):
            if cmd[0] == "list-windows":
                return 0, self._windows_text(*names), ""
            return orig(cmd, timeout)
        return fake

    def _patch_env(self, tmux_fake=None, **patches):
        """mock 终端执行层 + 无关副作用，返回 ExitStack（作为 context manager）。

        ``tmux_fake``：可传入自定义 tmux 命令 fake（如 leader 窗口缺失场景），
        否则用默认 ``self.fake_tmux``（全员窗口存活）。
        """
        import contextlib
        fake = tmux_fake or self.fake_tmux
        stack = contextlib.ExitStack()
        stack.enter_context(mock.patch.object(mcp, "_tmux", side_effect=fake))
        stack.enter_context(mock.patch.object(
            mcp, "_send_keys",
            side_effect=lambda s, w, text, **kw: self._sends.append((w, text)) or (0, ""),
        ))
        stack.enter_context(mock.patch.object(mcp, "_write_claude_permissions", return_value=""))
        stack.enter_context(mock.patch.object(mcp, "_write_claude_mcp", return_value=None))
        stack.enter_context(mock.patch.object(mcp, "_ensure_codex_mcp", return_value=(True, "ok")))
        stack.enter_context(mock.patch.object(mcp, "_save_death_context_snapshot", return_value=None))
        stack.enter_context(mock.patch.object(mcp, "_record_recovery_event", return_value=None))
        stack.enter_context(mock.patch.object(mcp.time, "sleep", return_value=None))
        for attr, spec in patches.items():
            stack.enter_context(mock.patch.object(mcp, attr, spec))
        return stack

    # -- 辅助断言 ----------------------------------------------------------

    def _results_jsonl(self):
        return Path(self._share_dir()) / "results.jsonl"

    def _share_dir(self):
        return mcp._share_dir(TEAM)

    def _read_results(self):
        path = self._results_jsonl()
        if not path.exists():
            return []
        return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]

    def _team(self):
        return mcp._load()["teams"][TEAM]


# =====================================================================
# 探针 A：正常分派 → 身份/角色约束 → 回报进入 leader 闭环
# =====================================================================
class RuntimeIdentityDispatchProbeTests(_IsolatedProbeMCP):
    """正常分派闭环：任务路由到正确成员窗口、system 层身份绑定本人、
    回报进入 leader 闭环（results.jsonl + pending + 唤醒）。"""

    ROLES = {
        "reviewer": "reviewer",
        "coder": "coder",
        "refactor": "refactor",
        "tester": "tester",
    }
    # tmux 窗口 target 映射（@N = 成员窗口，见 _windows_text）
    TARGETS = {"reviewer": "@1", "coder": "@2", "refactor": "@3", "tester": "@4"}

    def test_dispatch_routes_task_to_correct_member_window_and_persists(self):
        """派单文本必须到达目标成员自己的窗口，且 last_task 持久化到该成员。"""
        self._full_team()
        with self._patch_env() as _ctxs:
            for name, role in self.ROLES.items():
                self._calls.clear()
                self._sends.clear()
                task = f"task for {name}"
                ret = mcp.leader_assign_subtask(TEAM, name, task)
                self.assertIn(f"子任务已分配给 '{name}'", ret, ret)
                # 目标窗口 = 该成员自己的窗口（@N 映射，绝不投递到他人窗口）
                targets = {w for w, _ in self._sends}
                self.assertEqual(targets, {self.TARGETS[name]},
                                 f"{name} 应投递到自己的窗口 {self.TARGETS[name]}")
                # send 内容 = 该成员自己的任务 + 交付契约，绝不含其他成员任务
                joined = "\n".join(t for _, t in self._sends)
                self.assertIn(f"[子任务]\n{task}", joined)
                for other in self.ROLES:
                    if other != name:
                        self.assertNotIn(f"task for {other}", joined, f"{name} 收到他人任务")
                self.assertIn("member_report_result", joined)  # 交付契约
                # 持久化到本人
                member = self._team()["members"][name]
                self.assertEqual(member["last_task"], task)
                self.assertFalse(member["last_task_completed"])

    def test_each_member_identity_file_binds_own_identity_and_not_others(self):
        """reviewer/coder/refactor/tester 四种身份各渲染自己的 system 层身份
        文件：member_name/role/agent 是本人，绝不混入他人或 leader 身份。"""
        self._full_team()
        for name, role in self.ROLES.items():
            text = prompt_registry.render_member_identity(TEAM, name)
            self.assertIn(f"team='{TEAM}'", text)
            self.assertIn(f"member_name='{name}'", text)
            self.assertIn(f"role='{role}'", text)
            self.assertIn("agent='claude'", text)
            self.assertIn("你不是 leader", text)  # 成员身份段不是 leader
            for other in self.ROLES:
                if other != name:
                    self.assertNotIn(f"member_name='{other}'", text, f"{name} 混入 {other} 身份")
            self.assertNotIn("member_name='lead'", text)

    def test_dispatch_identity_file_written_0600_and_binds_member(self):
        """派单走 _tmux_spawn_member（恢复路径）时，真实身份文件落盘 0600
        且绑定本人——把 mock 当实机的边界：文件是真实磁盘产物。"""
        self._full_team()
        # coder 窗口缺失 → 真正走 new-window spawn 分支
        with self._patch_env(tmux_fake=self._tmux_fake_without("coder")):
            ok, _ = mcp._recover_and_send(TEAM, "coder", SESSION, reason="crash")
            self.assertTrue(ok)
            spawns = [c for c in self._calls if c[0] in ("new-session", "new-window")]
            self.assertTrue(spawns)
            spawn = spawns[0]
            idx = spawn.index("--append-system-prompt-file")
            path = Path(spawn[idx + 1])
            self.assertTrue(path.exists(), "身份文件在 spawn 前须落盘")
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
            content = path.read_text(encoding="utf-8")
            self.assertIn("member_name='coder'", content)
            self.assertIn("role='coder'", content)
            self.assertIn("你不是 leader", content)

    def test_leader_identity_file_is_leader_prompt_not_member(self):
        """leader=True 时身份文件是 leader system prompt（不是成员段）。"""
        self._full_team()
        path = prompt_registry.claude_identity_file(TEAM, "lead", leader=True)
        content = Path(path).read_text(encoding="utf-8")
        self.assertIn("是 Multi-Agent MCP 团队 'team' 的 leader", content)
        self.assertIn("member_name='lead'", content)
        self.assertNotIn("你不是 leader", content)  # leader 段不含成员否定句

    def test_report_enters_leader_closed_loop(self):
        """正常回报：results.jsonl + leader_pending_reports + 唤醒注入 + 任务完成。
        leader 存活且空闲 → report_wakeup 注入路径，leader 闭环=唤醒消息入终端。"""
        self._full_team()
        # coder 有进行中任务，回报后应标记完成
        def _give_coder_task():
            data = mcp._load()
            data["teams"][TEAM]["members"]["coder"]["last_task"] = "task for coder"
            mcp._save(data)
        _give_coder_task()
        with self._patch_env():
            ret = mcp.member_report_result(TEAM, "probe result 结论", member_name="coder")
        self.assertIn("已记录到共享上下文区", ret)
        # results.jsonl 真实落盘
        entries = self._read_results()
        self.assertTrue(entries, "results.jsonl 应有回报记录")
        last = entries[-1]
        self.assertEqual(last["member"], "coder")
        self.assertEqual(last["result"], "probe result 结论")
        # leader_pending_reports 已记录（信息不丢）
        reports = mcp.pending_leader_reports(self._team())
        self.assertTrue(any(r.get("member") == "coder" for r in reports))
        # leader 唤醒注入路径：leader_wakeup_reason=report（注入成功则置位）
        leader_wakeup_reason = self._team().get("leader_wakeup_reason")
        self.assertEqual(leader_wakeup_reason, "report")
        # 任务完成闭环
        self.assertTrue(self._team()["members"]["coder"]["last_task_completed"])
        # 压缩上下文真实落盘
        ctx_files = list(Path(self._share_dir(), "member_contexts").glob("*coder*"))
        self.assertTrue(ctx_files, "member_contexts 应有 coder 压缩上下文")

    def test_leader_activate_drains_pending_report(self):
        """回报先持久化到 pending，leader 重新进入用 leader_activate 确认收讫。"""
        self._full_team()
        # 关闭唤醒注入（相当于 leader 离线/冷却），回报只进 pending
        def _patch_no_wakeup():
            data = mcp._load()
            data["teams"][TEAM]["leader_wakeup"] = {"report_wakeup_enabled": False}
            mcp._save(data)
        _patch_no_wakeup()
        with self._patch_env():
            mcp.member_report_result(TEAM, "离线回报 内容", member_name="tester")
        # leader_activate 消费 pending
        out = mcp.leader_activate(TEAM)
        self.assertIn("tester", out)
        self.assertIn("离线回报", out)
        # 消费后 pending 清空
        self.assertEqual(mcp.pending_leader_reports(self._team()), [])


# =====================================================================
# 探针 B：leader 恢复/重启边界 → 身份重注入 + 回报跨重启存活
# =====================================================================
class RuntimeLeaderRecoveryProbeTests(_IsolatedProbeMCP):
    """leader 恢复/重启边界：leader 终端死 → 身份重注入（leader prompt 进
    system 层）；leader 停机期间成员回报进入 pending，重启后 leader_activate
    收讫——闭环跨 leader 重启不丢。"""

    def _leader_dead_team(self, *, with_member_task=True):
        ws = self.root / "workspace"
        members = {
            "lead": {"role": "leader", "agent": "claude"},
            "reviewer": {"role": "reviewer", "agent": "claude"},
            "coder": {"role": "coder", "agent": "claude"},
        }
        if with_member_task:
            members["coder"]["last_task"] = "finish refactor"
            members["coder"]["last_task_completed"] = False
        return self._save_team(ws, members=members, leader_task="overall mission")

    def _no_leader_window_fake(self):
        """构造 tmux fake：不列出 leader 窗口 → leader 判定 dead。"""
        orig = self.fake_tmux

        def fake(cmd, timeout=10):
            if cmd[0] == "list-windows":
                return 0, (
                    "$1\t1000\t@1\treviewer\n"
                    "$1\t1000\t@2\tcoder\n"
                    "$1\t1000\t@3\trefactor\n"
                ) + "\n", ""
            return orig(cmd, timeout)
        return fake

    def test_leader_revival_reinjects_leader_identity(self):
        """leader 终端死 → _revive_leader_terminal_locked 重建窗口，Claude 启动
        argv 携带 --append-system-prompt-file 且身份文件是 leader prompt（不串成员）。"""
        self._leader_dead_team()
        with self._patch_env(tmux_fake=self._no_leader_window_fake()):
            ok, msg = mcp._revive_leader_terminal_locked(TEAM)
        self.assertTrue(ok, msg)
        spawns = [c for c in self._calls if c[0] in ("new-session", "new-window")]
        self.assertTrue(spawns, "leader 复活必须产生 spawn 命令")
        spawn = spawns[0]
        self.assertIn("--append-system-prompt-file", spawn)
        idx = spawn.index("--append-system-prompt-file")
        path = Path(spawn[idx + 1])
        content = path.read_text(encoding="utf-8")
        self.assertIn("是 Multi-Agent MCP 团队 'team' 的 leader", content)
        self.assertIn("member_name='lead'", content)
        self.assertNotIn("你不是 leader", content)
        # results.jsonl 记 leader_revival 事件
        events = [e for e in self._read_results() if e.get("event") == "leader_revival"]
        self.assertTrue(events)
        # 复活计数 + leader_state=active
        team = self._team()
        self.assertGreaterEqual(team.get("leader_revival_count", 0), 1)
        self.assertEqual(team.get("leader_state"), "active")

    def test_report_while_leader_down_survives_restart_into_activate(self):
        """leader 停机期间成员回报：自动复活 leader（member_report 触发）+ 回报
        进 pending；leader 重启后 leader_activate 收讫，闭环跨重启不丢。"""
        self._leader_dead_team()
        with self._patch_env(tmux_fake=self._no_leader_window_fake()):
            ret = mcp.member_report_result(TEAM, "停机期回报 结论", member_name="coder")
        self.assertIn("已记录到共享上下文区", ret)
        # 回报真实落盘 results.jsonl（leader_revival 事件会追加在其后，按 member 过滤）
        coder_entries = [e for e in self._read_results() if e.get("member") == "coder"]
        self.assertTrue(coder_entries, "results.jsonl 应有 coder 回报")
        self.assertEqual(coder_entries[-1]["result"], "停机期回报 结论")
        # member_report 触发 leader 自动复活（身份重注入已由上一探针验证）
        self.assertGreaterEqual(self._team().get("leader_revival_count", 0), 1)
        # 回报进 pending（复活后仍不丢）
        self.assertTrue(any(r.get("member") == "coder" for r in mcp.pending_leader_reports(self._team())))
        # leader 重启后收讫
        out = mcp.leader_activate(TEAM)
        self.assertIn("coder", out)
        self.assertIn("停机期回报", out)
        self.assertEqual(mcp.pending_leader_reports(self._team()), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
