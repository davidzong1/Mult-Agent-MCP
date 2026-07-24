import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import mult_agent_mcp as m


class TaskSelectionDiscussionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_data_file = m.DATA_FILE
        self.old_share_context = m.SHARE_CONTEXT_DIR
        m.DATA_FILE = str(self.root / "teams_data.json")
        m.SHARE_CONTEXT_DIR = str(self.root / "contexts")
        Path(m.SHARE_CONTEXT_DIR).mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        m.DATA_FILE = self.old_data_file
        m.SHARE_CONTEXT_DIR = self.old_share_context
        self.tmp.cleanup()

    def write_data(self, team):
        with open(m.DATA_FILE, "w", encoding="utf-8") as f:
            json.dump({"teams": {"demo": team}}, f)

    def read_team(self):
        with open(m.DATA_FILE, "r", encoding="utf-8") as f:
            return json.load(f)["teams"]["demo"]

    def test_select_task_members_creates_missing_role_with_default_agent(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "terminals_active": False,
            "members": {
                "ldr": {"role": "leader", "agent": "codex"},
                "coder-1": {"role": "coder", "agent": "claude"},
            },
        })

        selection = m._select_task_members(
            "demo",
            "请实现功能并补充测试",
            create_missing=True,
        )

        self.assertEqual(selection["roles"], ["coder", "tester"])
        self.assertIn("coder-1", selection["selected"])
        self.assertIn("tester-claude", selection["created"])
        team = self.read_team()
        self.assertEqual(team["members"]["tester-claude"]["role"], "tester")
        self.assertEqual(team["members"]["tester-claude"]["agent"], "claude")

    def test_discussion_task_marks_mode_ready(self):
        team = {"members": {}}
        m._record_leader_task_start(team, "请讨论分析这个架构方案")

        self.assertTrue(team["discussion"]["enabled"])
        self.assertTrue(team["discussion"]["forced_by_task"])
        self.assertEqual(team["discussion"]["status"], "ready")

    def test_busy_member_detection_for_discussion(self):
        self.assertTrue(m._member_is_busy_for_discussion({
            "last_task": "coding",
            "last_task_completed": False,
        }))
        self.assertTrue(m._member_is_busy_for_discussion({"last_observed_state": "approval"}))
        self.assertFalse(m._member_is_busy_for_discussion({
            "last_task": "done",
            "last_task_completed": True,
        }))

    def test_member_discussion_conclusion_is_shared(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "context_dir": str(self.root / "contexts" / "demo"),
            "members": {
                "ldr": {"role": "leader"},
                "reviewer": {"role": "reviewer"},
                "tester": {"role": "tester"},
            },
            "discussion": {
                "enabled": True,
                "status": "active",
                "session_id": "s1",
                "topic": "讨论测试方案",
                "round": 1,
                "max_rounds": 3,
                "participants": ["reviewer", "tester"],
                "conclusions": {"1": {}},
            },
        })

        result = m.member_report_discussion_conclusion(
            "demo",
            "reviewer",
            "结论：先保留兼容工具，再新增定向工具。",
        )
        self.assertIn("已记录", result)
        shared = m.member_read_discussion("demo")
        self.assertIn("reviewer", shared)
        self.assertIn("保留兼容工具", shared)

    def test_start_discussion_keeps_auto_created_member(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "terminals_active": True,
            "context_dir": str(self.root / "contexts" / "demo"),
            "workspace_dir": str(self.root),
            "members": {
                "ldr": {"role": "leader", "agent": "codex"},
            },
        })

        with (
            mock.patch.object(m, "_find_any_session", return_value="sess"),
            mock.patch.object(m, "_tmux_spawn_member", return_value=(0, "", "")),
            mock.patch.object(m, "_member_window_target", side_effect=lambda _team, name: name),
            mock.patch.object(m, "_send_keys", return_value=(0, "")),
            mock.patch.object(m, "_write_claude_mcp", return_value=None),
            mock.patch.object(m, "_ensure_codex_mcp", return_value="ok"),
        ):
            result = m.leader_start_discussion("demo", "请讨论分析架构方案")

        self.assertIn("讨论模式已开启", result)
        team = self.read_team()
        self.assertIn("analyst-claude", team["members"])
        self.assertEqual(team["discussion"]["status"], "active")
        self.assertEqual(team["discussion"]["participants"], ["analyst-claude"])

    def test_discussion_ends_on_consensus_reached(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "context_dir": str(self.root / "contexts" / "demo"),
            "members": {
                "ldr": {"role": "leader"},
                "reviewer": {"role": "reviewer"},
            },
            "discussion": {
                "enabled": True,
                "status": "active",
                "session_id": "s1",
                "topic": "consensus test",
                "round": 1,
                "max_rounds": 3,
                "participants": ["reviewer"],
                "conclusions": {"1": {"reviewer": "同意方案A"}},
            },
        })

        result = m.leader_discussion_next_round("demo", consensus_reached=True)
        self.assertIn("讨论模式已结束", result)
        self.assertIn("consensus", result)
        team = self.read_team()
        self.assertEqual(team["discussion"]["status"], "ended")
        self.assertEqual(team["discussion"]["ended_reason"], "consensus")

    def test_discussion_ends_on_max_rounds(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "context_dir": str(self.root / "contexts" / "demo"),
            "members": {
                "ldr": {"role": "leader"},
                "reviewer": {"role": "reviewer"},
            },
            "discussion": {
                "enabled": True,
                "status": "active",
                "session_id": "s1",
                "topic": "rounds test",
                "round": 3,
                "max_rounds": 3,
                "participants": ["reviewer"],
                "conclusions": {"3": {}},
            },
        })

        result = m.leader_discussion_next_round("demo")
        self.assertIn("讨论模式已结束", result)
        self.assertIn("max_rounds", result)

    def test_member_read_discussion_after_ended(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "context_dir": str(self.root / "contexts" / "demo"),
            "members": {
                "ldr": {"role": "leader"},
                "reviewer": {"role": "reviewer"},
            },
            "discussion": {
                "enabled": False,
                "status": "ended",
                "session_id": "s1",
                "topic": "finished topic",
                "round": 2,
                "max_rounds": 3,
                "ended_reason": "consensus",
                "participants": ["reviewer"],
                "conclusions": {"1": {"reviewer": "方案A"}, "2": {"reviewer": "最终方案A"}},
            },
        })

        result = m.member_read_discussion("demo")
        self.assertIn("讨论已结束", result)
        self.assertIn("finished topic", result)
        self.assertIn("最终方案A", result)

    def test_discussion_final_entry_written_on_end(self):
        self.write_data({
            "leader": "ldr",
            "leader_type": "direct",
            "default_agent": "claude",
            "context_dir": str(self.root / "contexts" / "demo"),
            "members": {
                "ldr": {"role": "leader"},
                "reviewer": {"role": "reviewer"},
            },
            "discussion": {
                "enabled": True,
                "status": "active",
                "session_id": "s1",
                "topic": "persist test",
                "round": 1,
                "max_rounds": 3,
                "participants": ["reviewer"],
                "conclusions": {"1": {"reviewer": "结论已定"}},
            },
        })

        result = m.leader_discussion_next_round("demo", consensus_reached=True)
        self.assertIn("讨论模式已结束", result)

        disc_file = m._discussion_file("demo")
        self.assertTrue(Path(disc_file).exists())
        with open(disc_file, "r", encoding="utf-8") as f:
            entries = [json.loads(line) for line in f if line.strip()]
        self.assertTrue(any(e.get("type") == "discussion_ended" for e in entries))
        final = next(e for e in entries if e.get("type") == "discussion_ended")
        self.assertEqual(final["ended_reason"], "consensus")
        self.assertIn("1", final["conclusions"])


if __name__ == "__main__":
    unittest.main()
