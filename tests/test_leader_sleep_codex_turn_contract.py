"""Codex leader 的**回合语义**：延时等待期间不结束回合，返回后同一回合继续。

⚠️ 本文件的前提在 2026-08-16 被用户裁定反转
------------------------------------------------------------------
原内容（task2 P0）验收的是"codex 调用 leader_sleep 后必须**立即结束回合**、
等系统注入唤醒"。实测该设计在 codex 分支上等价于**再也醒不过来**：

  实机取样（真实 codex leader 窗口）:
      › /compact
        [唤醒通知] Leader activation: ...        ← 注入文本卡在输入框
        gpt-5.6-sol high · /tmp/.../workspace    ← codex footer
      _classify_leader_terminal_output(...) == "unknown"   ← 不是 idle

  所有注入路径（超时唤醒 / 回报唤醒 / 授权唤醒 / 巡检兜底补投）都以
  ``_leader_terminal_is_idle`` 为前置门，codex 恒判 unknown → 一条都发不出去。
  于是"结束回合"成了单程票：agent 停了，唤醒永远不到。

现语义：leader_sleep = **工具内延时等待**。工具调用本身就是那段等待，返回值
就是"这段时间发生了什么"，agent 拿到返回值在同一回合继续。因此：

  - 禁止再出现"立即结束当前回合"（提示与返回值都不得有）；
  - "严禁自造同步延时"保留且加强（等待必须走本工具）；
  - codex 与 claude 在本工具上**不再分叉**：延时等待与 CLI 品种无关，
    分叉只会让两条路各自腐化（codex 那条正是这么烂掉的）。

覆盖：
  1. prompts/leader.ts 两个通道的模板体：含延时等待契约，不含旧禁令；
  2. 渲染端到端（codex leader 团队）：_leader_system_prompt 同上；
  3. leader_sleep 返回：codex / claude / direct 三分支的回合语义；
  4. 回归锚点：签名、状态机（resting / sleep_until / wakeup / monitor）不变。
"""
import os
import re
import tempfile
import unittest
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer

REPO_ROOT = Path(__file__).resolve().parent.parent
LEADER_TS = REPO_ROOT / "prompts" / "leader.ts"

# 回合语义关键词
TURN_KEEP_KEYS = ("延时等待", "不要在调用后结束回合")
SYNC_DELAY_BAN_KEYS = ("自己造等待", "time.sleep")
TURN_END_BANNED = "立即结束当前回合"


def _functions(text: str) -> list[tuple[str, str]]:
    """提取 ``export function <name>(...): string { return `...`; }`` 的函数体。

    纯文本逐字符扫描，跳过转义序列（``\\`` + 下一字符），转义反引号不视为闭合；
    与 test_member_prompt_template.py 及运行时解析器语义一致。返回 [(name, body)]。
    """
    out = []
    for m in re.finditer(r"export\s+function\s+(\w+)\s*\([^)]*\)\s*:\s*string\s*\{", text):
        name = m.group(1)
        ret = re.search(r"return\s*`", text[m.start():])
        assert ret, f"{name} 缺 return `...` 模板体"
        i = m.start() + ret.end()
        body_start = i
        while i < len(text):
            if text[i] == "\\":
                i += 2
                continue
            if text[i] == "`":
                out.append((name, text[body_start:i]))
                break
            i += 1
        else:
            assert False, f"{name} 模板体未闭合"
    return out


class LeaderPromptTurnSemanticsTests(unittest.TestCase):
    """prompts/leader.ts 模板级契约（不依赖 MCP 模块，只读模板）。"""

    def test_leader_ts_declares_delay_wait_not_turn_end(self):
        text = LEADER_TS.read_text(encoding="utf-8")
        names = {name: body for name, body in _functions(text)}
        self.assertIn("leaderSystemPrompt", names)
        self.assertIn("leaderInitialContext", names)
        for channel, body in names.items():
            for key in TURN_KEEP_KEYS:
                self.assertIn(key, body, f"{channel} 缺延时等待契约 '{key}'")
            for key in SYNC_DELAY_BAN_KEYS:
                self.assertIn(key, body, f"{channel} 缺自造延时禁令 '{key}'")
            self.assertNotIn(
                TURN_END_BANNED, body,
                f"{channel} 仍带旧的'结束回合'禁令——该设计会让 codex leader 睡死",
            )

    def test_leader_ts_tells_agent_to_recall_on_slice(self):
        """切片提示必须写进模板：不然 agent 收到"已等待 X/600"会不知道要再调。"""
        text = LEADER_TS.read_text(encoding="utf-8")
        for _, body in _functions(text):
            self.assertIn("再调一次", body)


class LeaderSleepTurnSemanticsTests(unittest.TestCase):
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
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        self.old_env = {
            key: os.environ.get(key)
            for key in ("MULT_AGENT_MCP_WORKSPACE", "MULT_AGENT_MCP_CONTEXT_DIR")
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
        mcp.CLAUDE_GLOBAL_CONFIG_PATH = str(project / ".claude.json")
        mcp._OLD_DATA_FILE = str(project / "teams_data.json")
        mcp._OLD_SHARE_CONTEXT_DIR = str(project / "share_context_space")
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
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()

    def _team(self, *, leader_agent="codex", leader_type="tmux", **overrides):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": True,
            "leader": "lead",
            "leader_type": leader_type,
            "leader_state": "active",
            "leader_sleep_block_seconds": 0,   # 确定性缝（见 test_leader_sleep_contract）
            "members": {
                "lead": {"role": "leader", "agent": leader_agent},
                "alice": {"role": "coder", "agent": "claude",
                          "last_task": "登录模块", "last_task_completed": False},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    # ---- codex + tmux：返回不得要求结束回合，且必须给出续等指引 ----

    def test_codex_leader_sleep_keeps_turn_alive(self):
        self._team(leader_agent="codex")
        result = mcp.leader_sleep("team", max_seconds=120)
        self.assertNotIn(TURN_END_BANNED, result)
        self.assertIn("不要结束回合", result)
        self.assertIn("再次调用", result)
        self.assertIn("time.sleep", result, "自造延时禁令保留")
        # 状态语义不变：resting + sleep_until + wakeup enabled + monitor enabled
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "resting")
        self.assertTrue(t.get("leader_sleep_until"))
        self.assertEqual(t["leader_sleep_max_seconds"], 120)
        self.assertTrue(t["leader_wakeup_config"]["enabled"])
        self.assertTrue(t.get("monitor_enabled"))

    # ---- claude + tmux：与 codex 同一套返回（不再按 CLI 分叉） ----

    def test_claude_leader_sleep_same_as_codex(self):
        self._team(leader_agent="claude")
        claude_result = mcp.leader_sleep("team", max_seconds=120)
        self._team(leader_agent="codex")
        codex_result = mcp.leader_sleep("team", max_seconds=120)
        for result in (claude_result, codex_result):
            self.assertIn("已等待", result)
            self.assertIn("再次调用", result)
            self.assertNotIn(TURN_END_BANNED, result)

    # ---- codex + direct：无注入终端尾注保留，但不再要求手动 leader_activate ----

    def test_codex_direct_no_longer_needs_manual_activate(self):
        self._team(leader_agent="codex", leader_type="direct")
        result = mcp.leader_sleep("team", max_seconds=120)
        self.assertIn("已等待", result)
        self.assertIn("无注入终端", result)
        self.assertIn("无需再调 leader_activate", result)
        self.assertNotIn(TURN_END_BANNED, result)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "resting")

    # ---- 端到端：Codex leader 实际可见的 initial prompt 含新契约 ----

    def test_rendered_codex_leader_initial_prompt_has_delay_wait_contract(self):
        self._team(leader_agent="codex")
        text = mcp._leader_system_prompt("team")
        for key in TURN_KEEP_KEYS:
            self.assertIn(key, text)
        self.assertIn("time.sleep", text)
        self.assertNotIn(TURN_END_BANNED, text)

    # ---- 回归锚点：工具签名不变、clamp 不变 ----

    def test_leader_sleep_signature_and_state_anchor(self):
        import inspect

        self._team(leader_agent="codex")
        sig = inspect.signature(mcp.leader_sleep)
        self.assertEqual(list(sig.parameters), ["team_name", "max_seconds"])
        result = mcp.leader_sleep("team", max_seconds=99999)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_sleep_max_seconds"], 3600)
        self.assertIn("已等待", result)


if __name__ == "__main__":
    unittest.main()
