"""
C3 集成测试：普通 tmux send-keys 注入文本不伪称 system。

依据 docs/system_prompt_injection_audit.md §4/§7-7 与验收清单 C3：恢复/唤醒/派单
全部经 tmux send-keys → **user 消息角色**；其中 `[系统]`/`[system]` 前缀是内容层
格式模仿，CLI 不解析为真实 system 消息。C3 要求这些前缀改为诚实通道名（或移除）。

覆盖清理点（本轮 coder-claude 落地）：
  - _build_member_initial_context      → 前缀 [成员上下文]（原 [系统]）
  - _build_recovery_context            → 前缀 [恢复通知]（原 [系统]）
  - _build_recovery_message_tui        → 前缀 [恢复通知]（原 [系统]）
  - _build_leader_wakeup_message       → headline 前缀 [唤醒通知]（原 [system]）

同时验证 user 通道文本不含真实 system 伪标（[系统]/[system]），且身份绑定锚点仍在
（C3 只改标签，不动内容）。

数据隔离：经 common.data_layer.set_data_file() 指向临时文件，不触真实 ~/.mult_agent_mcp。
"""

import json
import tempfile
import unittest
from pathlib import Path

import mult_agent_mcp as mcp
from common import data_layer


class _IsolatedData(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.data_file = self.root / "teams.json"
        self.old_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)
        data_layer.set_data_file(str(self.data_file))
        self._save({
            "teams": {
                "team": {
                    "workspace_dir": str(self.root / "ws"),
                    "context_dir": str(self.root / "ctx"),
                    "leader": "lead",
                    "leader_type": "tmux",
                    "default_agent": "claude",
                    "members": {
                        "lead": {"role": "leader", "agent": "claude"},
                        "alice": {"role": "coder", "agent": "claude"},
                    },
                }
            }
        })

    def tearDown(self):
        if self.old_override is not None:
            data_layer.set_data_file(self.old_override)
        else:
            data_layer.set_data_file(data_layer.DATA_FILE)
        self.tmp.cleanup()

    def _save(self, data: dict) -> None:
        with open(self.data_file, "w", encoding="utf-8") as f:
            json.dump(data, f)


class SendKeysNoPseudoSystemTests(_IsolatedData):
    def test_initial_context_honest_prefix_and_identity(self):
        msg = mcp._build_member_initial_context("team", "alice")
        self.assertNotIn("[系统]", msg)
        self.assertNotIn("[system]", msg)
        self.assertTrue(msg.startswith("[成员上下文]"), "首启上下文应诚实标注 [成员上下文]")
        # C3 只改标签不动内容：身份绑定与交付合约仍在
        self.assertIn("member_name='alice'", msg)
        self.assertIn("你的团队成员身份绑定", msg)
        self.assertIn("member_report_result", msg)

    def test_recovery_context_honest_prefix(self):
        msg = mcp._build_recovery_context("team", "alice")
        self.assertNotIn("[系统]", msg)
        self.assertNotIn("[system]", msg)
        self.assertIn("[恢复通知] 终端恢复通知", msg)
        self.assertIn("member_name='alice'", msg)

    def test_tui_recovery_message_honest_prefix(self):
        team = mcp._load()["teams"]["team"]
        info = team["members"]["alice"]
        msg = mcp._build_recovery_message_tui(team, "alice", info, "team")
        self.assertNotIn("[系统]", msg)
        self.assertNotIn("[system]", msg)
        self.assertIn("[恢复通知] 终端恢复通知", msg)
        self.assertIn("member_name='alice'", msg)

    def test_leader_wakeup_headline_honest_prefix_all_reasons(self):
        cases = [
            ("approval", {"approval_members": ["alice"]}),
            ("report", {"report": {"member": "alice", "result": "done", "artifact_path": ""}}),
            ("pending_reports", {
                "pending_reports": [
                    {"member": "alice", "result": "done", "timestamp": "2026-08-12T00:00:00"},
                ],
            }),
            ("timeout", {}),
            ("other", {}),
        ]
        for reason, details in cases:
            with self.subTest(reason=reason):
                msg = mcp._build_leader_wakeup_message("team", reason, details)
                self.assertNotIn("[system]", msg)
                self.assertNotIn("[系统]", msg)
                self.assertTrue(
                    msg.startswith("[唤醒通知]"),
                    f"{reason} headline 应诚实标注 [唤醒通知]，实际: {msg.splitlines()[0]!r}",
                )


if __name__ == "__main__":
    unittest.main()
