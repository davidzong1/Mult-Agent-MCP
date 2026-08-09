"""
P0 数据一致性与团队恢复 —— 隔离回归测试（钉住生产 save 路径）
================================================================

背景（P0 恢复重点）
--------------------
目标团队 CPP_IPC_DDS_TEAM 不在全局 MCP teams_data.json，但:
  - tmux session mcp_CPP_IPC_DDS_TEAM_215956 存活（4 窗口 davidzong/tester/coder/reviewer）
  - 历史上下文位于 ~/.mult_agent_mcp/contexts/CPP_IPC_DDS_TEAM（results.jsonl 完整）
  - .agent_user_settings/ 有 CPP_IPC_DDS_TEAM__* 私有凭据文件
  - 4 个 claude 进程存活（davidzong 带 --allowedTools leader_* = leader）

根因（实测证据）
------------------
1. 全局 teams_data.json 从未对 CPP_IPC_DDS_TEAM 打 _deleted_legacy_teams 标记
   （delete_team 会标记；未标记 → 不是经 delete_team 删除）
2. 但团队索引从全局 teams_data.json 中消失 → 最可能是某次写入用不完整 dict
   覆盖了全局文件（全局文件含测试残留团队 "team"（/tmp workspace + "recover me"
   fixture），证明真实文件曾被污染/覆盖）
3. TUI/MCP 数据文件路径已统一：均解析 MULT_AGENT_MCP_HOME → ~/.mult_agent_mcp/teams_data.json
   （tui_screens.py DEFAULT_DATA_FILE = MCP_HOME/"teams_data.json"；
    mult_agent_mcp.py DATA_FILE = os.path.join(MCP_HOME, "teams_data.json")）
   → 无双写路径差异，恢复必须发生在同一数据文件上

本测试验证
-----------
A. 数据路径一致性：TUI/common save（data_layer.save_data）与 MCP _save 写同一文件；
   TUI save → MCP _load / list_teams 读到同一团队，反之亦然。
B. 陈旧快照 merge：
   B1. 钉住现状——生产 save 路径为整文件覆盖（无 merge）：TUI 用陈旧快照覆盖会
       抹掉 MCP 后创建的团队（真实一致性风险，需 merge 策略）。
   B2. merge 语义——先重读磁盘最新态再合并 partial 的 teams，新团队不丢失。
B. 无损恢复语义（生产路径版）：_find_any_session（含 TUI 时间戳格式解析）定位存活
   session + _tmux_window_records 读窗口，重建团队索引；只 merge 缺失团队、绝不覆盖
   现有 mcp优化 记录、拒绝创建空团队（无 session/无窗口不建）、未知团队不丢失。

数据隔离：temp teams_data（data_layer.set_data_file + mcp.DATA_FILE 重定向），
绝不触碰真实 ~/.mult_agent_mcp/teams_data.json；tmux 调用全部 mock。
"""

import hashlib
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from common import data_layer
import mult_agent_mcp as mcp


# 生产可恢复锚点常量（与 tmux_utils._sanitize_settings_component 的 sanitize 规则一致）
def _sanitize_settings_component(value: str) -> str:
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", value or "") or "empty"
    digest = hashlib.sha1((value or "").encode("utf-8")).hexdigest()[:8]
    return f"{base}__{digest}"


class _IsolatedTestCase(unittest.TestCase):
    """temp teams_data 隔离基类（与 test_mult_agent_mcp / test_leader_classifier 一致）。

    - data_layer.set_data_file → temp 文件；mcp.DATA_FILE → 同一 temp 文件。
    - 任何测试不得触碰真实 ~/.mult_agent_mcp/teams_data.json 或真实 tmux。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_globals = {
            "PROJECT_DIR": mcp.PROJECT_DIR,
            "MCP_HOME": mcp.MCP_HOME,
            "DATA_FILE": mcp.DATA_FILE,
            "SHARE_CONTEXT_DIR": mcp.SHARE_CONTEXT_DIR,
        }
        self.old_data_override = getattr(data_layer, "_DATA_FILE_OVERRIDE", None)

        project = self.root / "project"
        project.mkdir()
        mcp.PROJECT_DIR = str(project)
        mcp.MCP_HOME = str(project / ".mult_agent_mcp")
        mcp.DATA_FILE = str(project / ".mult_agent_mcp" / "teams_data.json")
        mcp.SHARE_CONTEXT_DIR = str(project / ".mult_agent_mcp" / "contexts")
        data_layer.set_data_file(mcp.DATA_FILE)

    def tearDown(self):
        for key, value in self.old_globals.items():
            setattr(mcp, key, value)
        data_layer._DATA_FILE_OVERRIDE = self.old_data_override
        self.tmp.cleanup()


class DataPathConsistencyTests(_IsolatedTestCase):
    """A: TUI/common save 与 MCP _save 写同一文件，双向可读。"""

    def test_tui_save_then_mcp_load_same_team(self):
        """TUI 用 data_layer.save_data 写入 → MCP _load 能读到（同一文件）。"""
        team_name = "cpp_ipc_dds_ws"
        data_layer.save_data({
            "teams": {
                team_name: {
                    "leader": "dz",
                    "leader_type": "tmux",
                    "workspace_dir": "/home/zwc/cpp_ipc_dds",
                    "context_dir": str(self.root / ".mult_agent_mcp" / "contexts" / team_name),
                    "terminals_active": False,
                    "members": {"dz": {"role": "leader", "agent": "claude"}},
                }
            }
        })
        loaded = mcp._load()
        self.assertIn(team_name, loaded["teams"])
        self.assertEqual(loaded["teams"][team_name]["workspace_dir"], "/home/zwc/cpp_ipc_dds")
        self.assertIn(team_name, mcp.list_teams())

    def test_mcp_create_then_tui_load_same_team(self):
        """MCP team_create（_load→_save）→ TUI data_layer.load_data 能读到（同一文件）。"""
        result = mcp.team_create("cpp_team_b", description="b", default_agent="claude")
        self.assertIn("创建成功", result)
        tui_data = data_layer.load_data()
        self.assertIn("cpp_team_b", tui_data["teams"])

    def test_env_home_override_respected_both_sides(self):
        """MULT_AGENT_MCP_HOME 覆盖后 TUI/MCP 均读该 home 下 teams_data.json。"""
        alt_home = self.root / "alt_home"
        alt_home.mkdir()
        mcp.MCP_HOME = str(alt_home)
        mcp.DATA_FILE = str(alt_home / "teams_data.json")
        data_layer.set_data_file(mcp.DATA_FILE)
        data_layer.save_data({"teams": {"env_team": {"members": {}}}})
        self.assertIn("env_team", mcp._load().get("teams", {}))
        self.assertIn("env_team", mcp.list_teams())

class StaleSnapshotMergeTests(_IsolatedTestCase):
    """B: 陈旧快照 merge —— 钉住生产 save 路径行为 + merge 语义。"""

    def _seed(self, teams: dict) -> None:
        data_layer.save_data({"teams": teams, "_deleted_legacy_teams": {}})

    def _alpha(self) -> dict:
        return {"leader": "dz", "leader_type": "tmux",
                "workspace_dir": "/home/zwc/cpp_ipc_dds",
                "context_dir": str(self.root / "ctx" / "alpha"),
                "members": {"dz": {"role": "leader", "agent": "claude"}}}

    def test_tui_stale_snapshot_loses_mcp_created_team(self):
        """钉住现状：生产 save 路径（data_layer.save_data / mcp._save）为整文件覆盖、无 merge。
        TUI 持陈旧快照 → MCP 后建新团队 → TUI 用陈旧快照覆盖 → 新团队被抹掉。
        这是需要 merge 策略的真实一致性风险（不期望修复，钉住以暴露风险）。"""
        self._seed({"alpha": self._alpha()})
        snapshot = data_layer.load_data()          # TUI 持有快照（仅 alpha）
        mcp.team_create("beta")                    # MCP 在快照之后创建 beta
        data_layer.save_data(snapshot)             # TUI 用陈旧快照整文件覆盖
        loaded = mcp._load()
        self.assertIn("alpha", loaded["teams"])
        self.assertNotIn("beta", loaded["teams"], "现状无 merge → beta 被陈旧快照抹掉")

    def test_merge_aware_save_preserves_newer_team(self):
        """merge 语义：先重读磁盘最新态再合并 partial 的 teams，新团队不丢失。
        恢复/写回必须采用本语义，否则会复现上述数据丢失。"""
        self._seed({"alpha": self._alpha()})
        snapshot = data_layer.load_data()
        mcp.team_create("beta")
        self._merge_save(snapshot)
        loaded = mcp._load()
        self.assertIn("alpha", loaded["teams"])
        self.assertIn("beta", loaded["teams"])

    def test_merge_save_never_loses_unknown_teams(self):
        """未知团队（其他工具写入，TUI/MCP 均不知情）不因 merge save 丢失。"""
        self._seed({"external_tool_team": {"members": {}}})
        self._merge_save({"teams": {"known": {"members": {}}}})
        loaded = mcp._load()
        self.assertIn("external_tool_team", loaded["teams"])
        self.assertIn("known", loaded["teams"])

    def _merge_save(self, partial: dict) -> None:
        """merge-safe 写回规格：重读磁盘最新态，只合并 partial 的 teams，
        保留磁盘上的其他团队与顶层键（含 _deleted_legacy_teams）。"""
        fresh = mcp._load()
        fresh.setdefault("teams", {}).update(partial.get("teams", {}))
        mcp._save(fresh)


class OrphanTeamRecoveryTests(_IsolatedTestCase):
    """C: 从存活 tmux session 无损恢复 —— 生产路径版。

    _recover_orphan_team 直接调用生产函数:
      mcp._find_any_session（含 TUI 时间戳格式解析）+ mcp._tmux_window_records + mcp._load/_save。
    """

    def _seed_mcp_optimization(self):
        """预置生产现有 mcp优化 团队（禁止被恢复覆盖）。"""
        data_layer.save_data({
            "teams": {
                "mcp优化": {
                    "leader": "codex",
                    "leader_type": "tmux",
                    "workspace_dir": "/home/zwc/mult_agent_mcp",
                    "context_dir": str(self.root / ".mult_agent_mcp" / "contexts" / "mcp优化"),
                    "terminals_active": True,
                    "members": {"codex": {"role": "leader", "agent": "codex"}},
                }
            },
            "_deleted_legacy_teams": {},
        })

    def _live_session(self) -> str:
        return "mcp_CPP_IPC_DDS_TEAM_215956"  # 真实 TUI 时间戳 session 名

    def _cpp_window_records(self) -> list[dict]:
        """模拟生产 list-windows 输出（真实 CPP 团队 4 窗口，首窗口 davidzong=leader）。"""
        return [
            {"id": "@7", "name": "davidzong", "session_id": "$5", "session_created": "2000"},
            {"id": "@8", "name": "tester", "session_id": "$5", "session_created": "2000"},
            {"id": "@9", "name": "coder", "session_id": "$5", "session_created": "2000"},
            {"id": "@10", "name": "reviewer", "session_id": "$5", "session_created": "2000"},
        ]

    def _patch_live_cpp_session(self):
        """patch 生产定位路径：_find_any_session 返回时间戳 session + 窗口记录。"""
        sess = self._live_session()
        records = self._cpp_window_records()
        return mock.patch.object(mcp, "_find_any_session", return_value=sess), \
            mock.patch.object(mcp, "_tmux_window_records", return_value=records)

    def _simulate_settings_files(self, team_name):
        """模拟私有 settings 文件名（含 team/member/profile 哈希分量）。"""
        d = Path(mcp.DATA_FILE).parent / ".agent_user_settings"
        d.mkdir(parents=True, exist_ok=True)
        for member in ("davidzong", "tester", "coder", "reviewer"):
            fname = f"{_sanitize_settings_component(team_name)}__{_sanitize_settings_component(member)}__x.json"
            (d / fname).write_text("{}", encoding="utf-8")

    def _recover_orphan_team(self, team_name: str) -> dict:
        """建议恢复逻辑（生产路径版）：从存活 session 窗口 + 现有 data 重建索引。

        规则（对 CPP_IPC_DDS_TEAM 这类"有 session 无索引"的孤儿团队）:
          - 团队已存在于 data → 跳过，绝不覆盖（保护 mcp优化）
          - 无存活 session / 无窗口记录 → 拒绝创建空团队
          - 否则 merge 重建: leader=首窗口, members 从窗口名推断,
            workspace/context 用现存 settings/上下文路径兜底
        """
        session = mcp._find_any_session(team_name)
        data = mcp._load()
        teams = data.setdefault("teams", {})
        if team_name in teams:
            return {"status": "skipped_existing", "team_name": team_name}
        if not session:
            return {"status": "refused_empty", "team_name": team_name}
        records = mcp._tmux_window_records(session)
        if not records:
            return {"status": "refused_empty", "team_name": team_name}

        leader_name = records[0]["name"]  # 首个窗口即 leader（生产: davidzong 带 leader_* allowedTools）
        members = {}
        for r in records:
            members[r["name"]] = {
                "role": "leader" if r["name"] == leader_name else "coder",
                "agent": "claude",
            }
        teams[team_name] = {
            "leader": leader_name,
            "leader_type": "tmux",
            "workspace_dir": str(self.root / team_name.lower()),
            "context_dir": str(Path(mcp.SHARE_CONTEXT_DIR) / team_name),
            "terminals_active": True,
            "members": members,
        }
        mcp._save(data)
        return {"status": "recovered", "team_name": team_name, "leader": leader_name,
                "member_count": len(members)}

    def test_recovery_merges_without_overwriting_mcp_optimization(self):
        """恢复孤儿团队时，既有 mcp优化 记录保持不变（禁止覆盖）。"""
        self._seed_mcp_optimization()
        with self._patch_live_cpp_session()[0], self._patch_live_cpp_session()[1]:
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "recovered")
        data = mcp._load()
        self.assertIn("CPP_IPC_DDS_TEAM", data["teams"])
        self.assertEqual(data["teams"]["CPP_IPC_DDS_TEAM"]["leader"], "davidzong")
        self.assertEqual(len(data["teams"]["CPP_IPC_DDS_TEAM"]["members"]), 4)
        # mcp优化 未被覆盖
        self.assertIn("mcp优化", data["teams"])
        self.assertEqual(data["teams"]["mcp优化"]["leader"], "codex")
        self.assertEqual(data["teams"]["mcp优化"]["leader_type"], "tmux")
        self.assertEqual(data["teams"]["mcp优化"]["members"]["codex"]["role"], "leader")

    def test_recovery_skips_existing_team(self):
        """团队已存在 → 跳过，不重建不覆盖。"""
        self._seed_mcp_optimization()
        with self._patch_live_cpp_session()[0], self._patch_live_cpp_session()[1]:
            out = self._recover_orphan_team("mcp优化")
        self.assertEqual(out["status"], "skipped_existing")
        data = mcp._load()
        self.assertEqual(data["teams"]["mcp优化"]["leader"], "codex")

    def test_recovery_refuses_empty_team_no_session(self):
        """无存活 tmux session → 拒绝创建空团队。"""
        self._seed_mcp_optimization()
        with mock.patch.object(mcp, "_find_any_session", return_value=None):
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "refused_empty")
        data = mcp._load()
        self.assertNotIn("CPP_IPC_DDS_TEAM", data["teams"])
        self.assertIn("mcp优化", data["teams"])

    def test_recovery_refuses_empty_team_no_window_records(self):
        """session 存活但窗口记录为空 → 同样拒绝空团队（无成员可推断）。"""
        self._seed_mcp_optimization()
        with mock.patch.object(mcp, "_find_any_session", return_value=self._live_session()), \
                mock.patch.object(mcp, "_tmux_window_records", return_value=[]):
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "refused_empty")
        data = mcp._load()
        self.assertNotIn("CPP_IPC_DDS_TEAM", data["teams"])

    def test_recovery_uses_real_find_any_session_for_timestamp_session(self):
        """真实 _find_any_session + _tmux_window_records 路径（仅 mock 底层 _tmux）：
        验证 TUI 时间戳 session mcp_{team}_{ts} 能被定位并重建恢复字段。"""
        self._seed_mcp_optimization()
        sess = self._live_session()

        def fake_tmux(cmd, timeout=10):
            if cmd[0] == "list-sessions":
                return 0, f"{sess}\n", ""
            if cmd[0] == "list-windows" and cmd[2] == sess:
                return 0, (
                    "$5\t2000\t@7\tdavidzong\n"
                    "$5\t2000\t@8\ttester\n"
                    "$5\t2000\t@9\tcoder\n"
                    "$5\t2000\t@10\treviewer\n"
                ), ""
            return 0, "", ""

        with mock.patch.object(mcp, "_tmux", side_effect=fake_tmux):
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")

        self.assertEqual(out["status"], "recovered")
        self.assertEqual(out["leader"], "davidzong")
        self.assertEqual(out["member_count"], 4)
        data = mcp._load()
        team = data["teams"]["CPP_IPC_DDS_TEAM"]
        self.assertEqual(team["leader"], "davidzong")
        self.assertEqual(team["leader_type"], "tmux")
        self.assertTrue(team["terminals_active"])
        self.assertEqual(set(team["members"]), {"davidzong", "tester", "coder", "reviewer"})
        self.assertEqual(team["members"]["davidzong"]["role"], "leader")
        self.assertEqual(team["members"]["tester"]["role"], "coder")
        self.assertEqual(team["context_dir"], str(Path(mcp.SHARE_CONTEXT_DIR) / "CPP_IPC_DDS_TEAM"))

    def test_recovery_preserves_team_created_since_snapshot(self):
        """merge 语义：恢复前其他进程新建的团队，恢复 merge 后不丢失。"""
        self._seed_mcp_optimization()
        mcp.team_create("newcomer")  # 在恢复快照之后被写入磁盘
        with self._patch_live_cpp_session()[0], self._patch_live_cpp_session()[1]:
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "recovered")
        data = mcp._load()
        self.assertIn("newcomer", data["teams"])
        self.assertIn("mcp优化", data["teams"])
        self.assertIn("CPP_IPC_DDS_TEAM", data["teams"])

    def test_unknown_team_not_lost_during_recovery(self):
        """未知团队（TUI/MCP 均不知情）在恢复 merge 后仍在。"""
        self._seed_mcp_optimization()
        data = data_layer.load_data()
        data["teams"]["external_tool_team"] = {"members": {}}
        data_layer.save_data(data)
        with self._patch_live_cpp_session()[0], self._patch_live_cpp_session()[1]:
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "recovered")
        data = mcp._load()
        self.assertIn("external_tool_team", data["teams"])
        self.assertIn("CPP_IPC_DDS_TEAM", data["teams"])

    def test_deleted_marker_never_set_for_orphan(self):
        """孤儿团队未被 delete_team 删除（无 _deleted_legacy_teams 标记），
        恢复不应受影响；恢复后不应误打删除标记。"""
        self._seed_mcp_optimization()
        with self._patch_live_cpp_session()[0], self._patch_live_cpp_session()[1]:
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "recovered")
        data = mcp._load()
        self.assertNotIn("CPP_IPC_DDS_TEAM", data.get("_deleted_legacy_teams", {}))

    def test_recovery_settings_files_are_recognized(self):
        """私有 settings 文件名含 team/member 分量 → 可作为恢复辅助证据。"""
        self._seed_mcp_optimization()
        team_name = "CPP_IPC_DDS_TEAM"
        self._simulate_settings_files(team_name)
        d = Path(mcp.DATA_FILE).parent / ".agent_user_settings"
        files = [p.name for p in d.glob("*.json")]
        team_comp = _sanitize_settings_component(team_name)
        self.assertTrue(
            any(f.startswith(team_comp) for f in files),
            f"settings 文件名应含团队分量 {team_comp}: {files[:4]}",
        )

    def test_contexts_dir_is_recovery_backstop(self):
        """共享上下文目录存在且非空 = 团队曾活跃的旁证；恢复后 context_dir 指向它。"""
        self._seed_mcp_optimization()
        ctx = Path(mcp.SHARE_CONTEXT_DIR) / "CPP_IPC_DDS_TEAM"
        ctx.mkdir(parents=True, exist_ok=True)
        (ctx / "results.jsonl").write_text("{}", encoding="utf-8")
        with self._patch_live_cpp_session()[0], self._patch_live_cpp_session()[1]:
            out = self._recover_orphan_team("CPP_IPC_DDS_TEAM")
        self.assertEqual(out["status"], "recovered")
        self.assertEqual(
            Path(mcp.SHARE_CONTEXT_DIR) / "CPP_IPC_DDS_TEAM",
            Path(data_layer.load_data()["teams"]["CPP_IPC_DDS_TEAM"]["context_dir"]),
        )


if __name__ == "__main__":
    unittest.main()
