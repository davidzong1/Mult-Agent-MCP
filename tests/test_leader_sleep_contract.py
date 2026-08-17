"""leader_sleep 契约验收：**延时等待**语义（Codex 可见提示 + 返回契约）。

⚠️ 语义裁定变更（2026-08-16，用户决策，推翻 task2 P0 的旧契约）
------------------------------------------------------------------
旧契约（task2，本文件原内容）：leader_sleep 只打个 resting 标记，**要求 agent
调用后立即结束当前回合**，等系统往 tmux 终端注入唤醒。
实测该设计在 codex 分支上等价于"真休眠不再醒"：所有注入路径都卡在
``_leader_terminal_is_idle`` → ``_classify_leader_terminal_output``，而后者整套
按 Claude TUI 写成，codex 的 ``›`` 输入框 + ``<model> <effort> · <cwd>`` footer
一律判 unknown，注入永远发不出去。

新契约（本文件现内容）：leader_sleep 是**延时等待** —— 这次工具调用本身就是
那段等待，阻塞到"有成员回报 / 有人卡授权 / 全部完成 / 到点"后带摘要返回，
agent 在**同一回合内继续**。注入唤醒保留为兜底（仍写 resting + sleep_until），
但不再是唯一出路。

因此本文件断言的方向整体反转：
  - 禁止再出现"立即结束当前回合"（那正是让 codex 睡死的那句）；
  - 仍然禁止 shell `sleep` / `time.sleep` / 轮询自造延时（这条不变——等待必须
    由工具完成，自造延时不被记账且让终端一直判 busy）；
  - 工具**允许且应当**阻塞（旧文件里"工具自身不阻塞"的用例语义已作废）。

覆盖：
  A. prompt 渲染契约（两通道 + ts 源三重防线）：含"延时等待"/"不要在调用后
     结束回合"/`time.sleep` 禁令；不含"立即结束当前回合"。
  B. leader_sleep 返回契约：切片返回提示再调一次；direct 尾注语义更新为
     "等待已由工具完成，无需 leader_activate"。
  C. 行为保持：resting / sleep_until / wakeup enabled / monitor_enabled / 签名。
"""
import os
import tempfile
import unittest
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer
from common import prompt_registry

# 新契约关键词（与实现措辞锚定，改实现措辞须同步此处）
CONTRACT_KEYS = (
    "延时等待",
    "不要在调用后结束回合",
    "time.sleep",
    "轮询",
)
# 反契约关键词：出现即回归到会让 codex 睡死的旧设计
FORBIDDEN_KEYS = ("立即结束当前回合",)


def _leader_ts_text() -> str:
    """读取 prompts/leader.ts 源文本（相对本文件定位，不依赖渲染）。"""
    ts_path = Path(__file__).resolve().parent.parent / "prompts" / "leader.ts"
    return ts_path.read_text(encoding="utf-8")


class LeaderSleepContractTests(unittest.TestCase):
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

    def _team(self, **overrides):
        workspace = self.root / "workspace"
        context = self.root / "context"
        workspace.mkdir(exist_ok=True)
        context.mkdir(exist_ok=True)
        team = {
            "workspace_dir": str(workspace),
            "context_dir": str(context),
            "terminals_active": False,
            "leader": "lead",
            "leader_type": "tmux",
            "leader_state": "active",
            # 确定性缝：单次阻塞上限置 0 = 求值一次事件后立刻按切片返回，
            # 事件判定路径与生产完全一致，只是不真的阻塞 240s。
            "leader_sleep_block_seconds": 0,
            "members": {
                "lead": {"role": "leader", "agent": "codex"},
            },
        }
        team.update(overrides)
        mcp._save({"teams": {"team": team}})
        return team

    # ------------------------------------------------------------------
    # A. prompt 渲染契约（Codex 可见 initial + Claude system 双通道 + ts 源）
    # ------------------------------------------------------------------

    def test_initial_channel_render_contains_delay_wait_contract(self):
        """@channel initial（Codex CLI prompt 源）渲染结果含新契约、无旧禁令。"""
        self._team()
        text = mcp._leader_system_prompt("team")
        for key in CONTRACT_KEYS:
            self.assertIn(key, text)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, text, "旧契约会让 codex leader 睡死，不得回归")

    def test_system_channel_render_contains_delay_wait_contract(self):
        """@channel system（Claude identity 文件正文）渲染结果含新契约。"""
        self._team()
        text = prompt_registry._render_leader_system("team")
        for key in CONTRACT_KEYS:
            self.assertIn(key, text)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, text)

    def test_leader_ts_source_both_channels_contain_contract(self):
        """兜底 A4 防线：直接读 ts 源，两段模板体均含新契约、无旧禁令。"""
        text = _leader_ts_text()
        self.assertEqual(text.count("延时等待"), text.count("延时等待"))
        for channel in ("leaderSystemPrompt", "leaderInitialContext"):
            self.assertIn(channel, text)
        # 两个通道各一份，关键词至少各出现一次
        for key in CONTRACT_KEYS:
            self.assertGreaterEqual(
                text.count(key), 2, f"{key} 应在 system/initial 两段模板体中各出现"
            )
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, text)

    # ------------------------------------------------------------------
    # B. leader_sleep 返回契约
    # ------------------------------------------------------------------

    def test_leader_sleep_codex_tmux_returns_slice_contract(self):
        """tmux + codex：无事件 → 切片返回，提示再调一次，绝不叫 agent 结束回合。"""
        self._team(leader_type="tmux")
        result = mcp.leader_sleep("team", max_seconds=120)
        self.assertIn("已等待", result)
        self.assertIn("再次调用", result)
        self.assertIn("不要结束回合", result)
        self.assertIn("time.sleep", result, "自造延时禁令必须保留")
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, result)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "resting", "切片期间保持 resting（注入兜底武装）")
        self.assertTrue(t.get("leader_sleep_until"), "切片不得消耗休眠截止时间")
        self.assertTrue(t["leader_wakeup_config"]["enabled"])
        self.assertTrue(t.get("monitor_enabled"))

    def test_leader_sleep_claude_tmux_same_contract(self):
        """tmux + claude：与 codex 同一套返回契约（延时等待与 CLI 无关）。"""
        self._team(leader_type="tmux",
                   members={"lead": {"role": "leader", "agent": "claude"}})
        result = mcp.leader_sleep("team", max_seconds=120)
        self.assertIn("已等待", result)
        self.assertIn("再次调用", result)
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, result)

    def test_leader_sleep_direct_note_updated(self):
        """direct：尾注保留"无注入终端"，但语义更新为"等待已由工具完成"。"""
        self._team(leader_type="direct",
                   members={"lead": {"role": "leader", "agent": "codex"}})
        result = mcp.leader_sleep("team", max_seconds=120)
        self.assertIn("无注入终端", result)
        self.assertIn("无需再调 leader_activate", result,
                      "direct 不再依赖手动 leader_activate 才能知道发生了什么")
        for key in FORBIDDEN_KEYS:
            self.assertNotIn(key, result)
        t = mcp._load()["teams"]["team"]
        self.assertEqual(t["leader_state"], "resting")

    # ------------------------------------------------------------------
    # C. 行为保持回归
    # ------------------------------------------------------------------

    def test_leader_sleep_signature_and_clamp_unchanged(self):
        """工具签名与 clamp 语义不变（换实现不换接口）。"""
        import inspect

        self._team()
        self.assertEqual(
            list(inspect.signature(mcp.leader_sleep).parameters),
            ["team_name", "max_seconds"],
        )
        mcp.leader_sleep("team", max_seconds=99999)
        self.assertEqual(mcp._load()["teams"]["team"]["leader_sleep_max_seconds"], 3600)
        mcp.leader_sleep("team", max_seconds=1)
        self.assertEqual(mcp._load()["teams"]["team"]["leader_sleep_max_seconds"], 10)


if __name__ == "__main__":
    unittest.main()
