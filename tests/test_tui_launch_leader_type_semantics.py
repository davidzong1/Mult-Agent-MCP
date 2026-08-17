"""
task1 三态验收 + 失败复现：TUI ``launch_terminals`` 对 ``leader_type`` 的语义
============================================================================

四方讨论已一致的语义（2026-08-14，实现仍冻结，仅测试文件）：

    (tmux, L)    → 正常启动 leader 窗 + 成员窗；leader_type 保持 tmux
    ("", L)      → leader 窗 spawn **成功后**原子写 leader_type=tmux；失败不得提前写
    ("", "")     → 拒绝：提示先指定 Leader（不隐式造 leader）
    (direct, L)  → 只启动**非 leader** 成员窗；不创建 leader 窗；不改 leader_type

本文件只新增/断言测试，**不修改** tui_screens.py / mult_agent_mcp.py / tmux_utils.py。
工作区既有 task2 改动与未跟踪测试（test_leader_sleep_contract.py、
test_leader_sleep_codex_turn_contract.py、test_tui_member_spawn_ts_source.py、
test_tui_system_prompt_source.py）一律保留、不覆盖。

【失败证据】当前代码预期以下用例**失败**（证明缺陷，供 coder 实现后转绿）：
  - ``test_direct_launch_must_not_spawn_leader_window``
    —— 现在 launch_terminals 无 leader_type 门禁，direct 也会创建 leader 窗（双 leader）。
  - ``test_empty_type_success_launch_calibrates_tmux``
    —— 现在空值型启动后 leader_type 仍为 ""，revive/wakeup/注入全被门禁拒绝。

其余用例锁定已正确的行为与原子性不变量（当前即绿，防回归）。

mock 模式复用 test_tui_member_spawn_ts_source.py：data_layer 隔离 + 捕获
``ts._tmux_run`` 命令 + 实例级唯一会话名（避免真实 tmux 状态碰撞）。
"""

import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

import mult_agent_mcp as mcp
from common import data_layer
from tui import tui_screens as ts


class TuiLaunchLeaderTypeSemanticsTests(unittest.TestCase):
    """TUI launch_terminals 对 leader_type 三态（tmux / '' / direct）的验收与失败复现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams_data.json"
        self._old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(self.data_file)
        self.ws = self.root / "ws"
        self.ws.mkdir()
        self.ctx = self.root / "ctx"
        self.ctx.mkdir()
        self.calls: list[list[str]] = []
        # 实例级唯一会话名：真实 has-session 必然 rc!=0 → 判定窗口 absent → 创建，
        # 保留真实 member_window_state 语义且不依赖外部 tmux 状态。
        self.session = f"mcp_team_{uuid.uuid4().hex[:6]}"
        self._fail_leader_session = False

    def tearDown(self):
        data_layer._DATA_FILE_OVERRIDE = self._old_override
        self.tmp.cleanup()

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _save_team(self, leader_type, *, leader="lead", members=None):
        members = members or {
            "lead": {"role": "leader", "agent": "claude"},
            "alice": {"role": "coder", "agent": "claude"},
        }
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": leader,
            "leader_type": leader_type,
            "default_agent": "claude",
            # 延时等待的确定性缝（2026-08-16）：leader_sleep 现在是工具内阻塞
            # 等待，置 0 = 求值一次事件后立刻按切片返回，不真的阻塞 240s。
            "leader_sleep_block_seconds": 0,
            "members": members,
        }}})

    def _fake_tmux_run(self, cmd, timeout=10):
        self.calls.append(cmd)
        if cmd[0] == "-V":
            return 0, "tmux 3.2", ""
        if cmd[0] == "has-session":
            return 1, "", ""
        if cmd[0] == "list-windows":
            return 0, "$1\t1000\t@1\t__base", ""
        if cmd[0] == "new-session":
            # 支持 leader 窗 spawn 失败注入
            return (1, "", "leader spawn failed") if self._fail_leader_session else (0, "", "")
        return 0, "", ""

    def _patches(self):
        return [
            mock.patch.object(ts, "_tmux_run", side_effect=self._fake_tmux_run),
            mock.patch.object(ts, "_tmux_session", return_value=self.session),
            mock.patch.object(ts, "configure_claude_mcp", return_value=(True, "")),
            mock.patch.object(ts, "configure_codex_mcp", return_value=(True, "")),
            mock.patch.object(ts, "write_claude_permissions", return_value=""),
            mock.patch.object(
                ts, "claude_agent_user_launch",
                return_value=("", str(self.ws / ".claude" / "settings.json")),
            ),
            mock.patch.object(ts.classifier_fallback, "claude_terminal_allow_tools",
                              return_value=[]),
            mock.patch.object(ts, "get_agent_user_env_prefix", return_value=[]),
            mock.patch.object(ts, "get_proxy_env_prefix", return_value=[]),
            mock.patch.object(ts, "merge_env_prefixes", return_value=[]),
            mock.patch.object(ts, "resolve_agent_model", return_value=""),
            mock.patch.object(ts, "resolve_member_effort", return_value=""),
            mock.patch.object(ts, "_leader_terminal_restart_blocked", return_value=False),
            mock.patch.object(ts, "_record_leader_reentry", return_value=None),
            mock.patch.object(ts, "_remember_member_window_id", return_value=""),
            mock.patch.object(ts, "_inject_claude_leader_prompt", return_value=(0, "")),
            mock.patch.object(ts.time, "sleep", return_value=None),
        ]

    def _launch(self):
        patches = self._patches()
        for p in patches:
            p.start()
        try:
            ok, msg = ts.launch_terminals("team")
        finally:
            for p in patches:
                p.stop()
        return ok, msg

    def _has_window(self, window_name: str):
        """在捕获的 tmux 命令中查找是否存在窗口名为 window_name 的 spawn。"""
        for cmd in self.calls:
            if not cmd:
                continue
            if cmd[0] in ("new-session", "new-window") and "-n" in cmd:
                if cmd[cmd.index("-n") + 1] == window_name:
                    return True
        return False

    def _leader_type(self):
        return data_layer.load_data().get("teams", {}).get("team", {}).get("leader_type", "")

    # ------------------------------------------------------------------
    # 失败证据：当前代码应失败的用例（证明缺陷）
    # ------------------------------------------------------------------

    def test_direct_launch_must_not_spawn_leader_window(self):
        """direct：不得创建 leader 窗、不改 leader_type（当前代码会建 leader 窗 → FAIL）。"""
        self._save_team("direct")
        ok, msg = self._launch()
        self.assertTrue(ok, f"direct 团队启动应成功（仅成员）: {msg}")
        # 关键不变量：不创建 leader 窗
        self.assertFalse(
            self._has_window("lead"),
            "direct leader_type 不得创建 leader 窗（避免双 leader / 静默夺权）",
        )
        self.assertEqual(self._leader_type(), "direct", "direct 启动不得改动 leader_type")

    def test_empty_type_success_launch_calibrates_tmux(self):
        """空值型 + leader 已设：leader 窗 spawn 成功后原子落 tmux（当前代码不写 → FAIL）。"""
        self._save_team("")
        ok, msg = self._launch()
        self.assertTrue(ok, f"空值型启动应成功: {msg}")
        self.assertTrue(
            self._has_window("lead"),
            "空值型启动应创建 leader 窗（TUI 明确启动 leader 的预期）",
        )
        self.assertEqual(
            self._leader_type(), "tmux",
            "空值型启动成功后必须原子校准 leader_type=tmux（否则 revive/wakeup 被门禁拒绝）",
        )

    # ------------------------------------------------------------------
    # 原子性不变量（当前即绿，锁定行为）
    # ------------------------------------------------------------------

    def test_empty_type_spawn_failure_keeps_type_empty(self):
        """空值型 + leader 窗 spawn 失败：不得提前写 tmux，返回失败。"""
        self._save_team("")
        self._fail_leader_session = True
        ok, msg = self._launch()
        self.assertFalse(ok, "leader 窗 spawn 失败应返回失败")
        self.assertIn("创建 leader 终端失败", msg)
        self.assertEqual(self._leader_type(), "", "spawn 失败不得提前写 leader_type=tmux")

    def test_empty_leader_rejected_without_window(self):
        """空值型 + 空 leader：拒绝启动 leader，不隐式造 leader。"""
        self._save_team("", leader="")
        ok, msg = self._launch()
        self.assertFalse(ok, "无 leader 应拒绝启动")
        self.assertTrue("Leader" in msg or "leader" in msg, f"应提示先指定 Leader: {msg}")
        self.assertFalse(self._has_window("lead"))

    # ------------------------------------------------------------------
    # 正常路径：tmux 启动 leader + 成员（锁定）
    # ------------------------------------------------------------------

    def test_tmux_type_launch_spawns_leader_and_members(self):
        """tmux：启动 leader 窗 + 成员窗，leader_type 保持 tmux。"""
        self._save_team("tmux")
        ok, msg = self._launch()
        self.assertTrue(ok, f"tmux 启动失败: {msg}")
        self.assertTrue(self._has_window("lead"), "tmux 应创建 leader 窗")
        self.assertTrue(self._has_window("alice"), "tmux 应创建成员窗")
        self.assertEqual(self._leader_type(), "tmux", "tmux 启动后 leader_type 不变")
        # 时间戳会话：启动使用的 session 名是 mcp_{team}_{HHMMSS} 形态
        self.assertTrue(self.session.startswith("mcp_team_"), f"session 名应为时间戳形态: {self.session}")

    # ------------------------------------------------------------------
    # 时间戳 session/窗口解析：不误命中旧 session（锁定）
    # ------------------------------------------------------------------

    def test_find_any_session_prefers_timestamped_with_member_windows(self):
        """_find_any_session：旧裸会话 mcp_team 有 0 匹配时，选中带成员窗的时间戳会话。"""
        data_layer.save_data({"teams": {"team": {
            "workspace_dir": str(self.ws),
            "context_dir": str(self.ctx),
            "leader": "lead",
            "leader_type": "tmux",
            "members": {
                "lead": {"role": "leader", "agent": "claude"},
                "alice": {"role": "coder", "agent": "claude"},
            },
        }}})

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "list-sessions":
                return 0, "mcp_team\nmcp_team_123456", ""
            if cmd[0] == "list-windows":
                # 时间戳会话含 lead/alice 窗口；裸会话无窗口（旧/空）
                if "mcp_team_123456" in cmd:
                    return 0, "s1\t100\t@1\tlead\ns1\t100\t@2\talice", ""
                return 0, "", ""
            if cmd[0] == "has-session":
                return 1, "", ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            got = mcp._find_any_session("team")
        self.assertEqual(got, "mcp_team_123456",
                         "应选中带成员窗的时间戳会话，而非 0 匹配的旧裸会话")

    # ------------------------------------------------------------------
    # tmux 语义可观察性（需求 5）：TUI 启动后的元数据对 MCP 表现为 tmux leader
    # ------------------------------------------------------------------

    def test_tui_launched_tmux_leader_sleep_auto_injection_branch(self):
        """TUI 启动(tmux) 后的 leader 元数据：leader_sleep 走自动注入分支（非 direct）。"""
        self._save_team("tmux")
        ok, msg = self._launch()
        self.assertTrue(ok, msg)
        # 消除 monitor 线程干扰（tmux 语义观察与 terminals_active 无关）
        data = data_layer.load_data()
        data["teams"]["team"]["terminals_active"] = False
        data_layer.save_data(data)
        result = mcp.leader_sleep("team", max_seconds=120)
        # 2026-08-16 语义变更：leader_sleep 改为工具内延时等待，返回的是"这段
        # 等待发生了什么"，不再是"已进入休眠 + 自动注入说明"。tmux 与 direct 的
        # 区别落在 direct 专属的"无注入终端"尾注上（tmux 分支必须没有它）。
        self.assertIn("已等待", result)
        self.assertNotIn("无注入终端", result, "tmux 语义不得落为 direct 提示")

    def test_tui_launched_tmux_leader_member_report_wakeup(self):
        """TUI 启动(tmux) 的 leader：成员回报 → 唤醒评估命中 wakeup_approval。"""
        data = data_layer.load_data().get("teams", {}).get("team", {})
        # 复用 _save_team 构造的 tmux 团队 + 回报输入
        self._save_team("tmux")
        from datetime import datetime, timedelta
        team = data_layer.load_data()["teams"]["team"]
        team.update({
            "leader_state": "resting",
            "leader_sleep_until": (datetime.now() - timedelta(seconds=1)).isoformat(),
            "leader_wakeup_config": {"enabled": True, "idle_threshold": 4,
                                     "approval_alert": True, "auto_authorize_first": False,
                                     "cooldown_cycles": 6, "max_wakeups_per_session": 10},
        })
        data_layer.save_data({"teams": {"team": team}})
        member_results = [{"member": "alice", "state": "approval", "action": "observed"}]
        action = mcp._evaluate_leader_wakeup_conditions("team", member_results)
        self.assertEqual(action["action"], "wakeup_approval",
                         "tmux leader 的成员回报应触发唤醒评估（非 direct 无注入）")

    # ------------------------------------------------------------------
    # 需求 6：direct 既有 leader_activate 语义不回归
    # ------------------------------------------------------------------

    def test_direct_leader_activate_no_regression(self):
        """direct：leader_sleep 提示无注入终端 + leader_activate 手动唤醒可清睡眠态。"""
        self._save_team("direct")
        from datetime import datetime, timedelta
        team = data_layer.load_data()["teams"]["team"]
        team["leader_state"] = "resting"
        team["leader_sleep_until"] = (datetime.now() + timedelta(seconds=300)).isoformat()
        data_layer.save_data({"teams": {"team": team}})

        sleep_result = mcp.leader_sleep("team", max_seconds=120)
        self.assertIn("无注入终端", sleep_result, "direct leader 保持手动激活语义")
        self.assertIn("leader_activate", sleep_result)

        activate_result = mcp.leader_activate("team")
        self.assertIn("已激活", activate_result)
        t = data_layer.load_data()["teams"]["team"]
        self.assertEqual(t["leader_state"], "active")
        self.assertNotIn("leader_sleep_until", t, "leader_activate 应清理睡眠态")


if __name__ == "__main__":
    unittest.main()
