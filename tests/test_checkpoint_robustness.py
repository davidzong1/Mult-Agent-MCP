"""
P0/P1 checkpoint 健壮性测试：损坏恢复 / 原子性 / 旧数据兼容 / 旧写拒绝
====================================================================

补充 test_checkpoint_primitive.py（成员纯函数/持久化正常路径）与
test_leader_checkpoint.py（leader 正常路径）未覆盖的健壮性面：

  1. 损坏恢复：leader_checkpoint / 成员 task_checkpoint 在磁盘上被写坏时，
     恢复渲染与续跑判定必须优雅降级，绝不 crash（leader 重启场景最致命）。
  2. 原子性：写入故障注入后磁盘必须保持上一份完整状态（torn 不可见）。
  3. 旧数据兼容：无 task_checkpoint 的 legacy 成员 / 无 leader_checkpoint 的
     旧团队行为不变。
  4. 旧写拒绝（防旧上下文覆盖 P0）：stale cp 不应覆盖新进度。

隔离：全部测试使用临时 teams_data（data_layer.set_data_file(tmp)），
不触碰真实 ~/.mult_agent_mcp/（conftest 环境级兜底 + atomic_json_write 守卫）。

⚠️ 已知缺陷（测试会失败，作为给 coder 的回归记录）：
  - test_leader_cp_bad_epoch_render_degrades_gracefully：损坏的 dict cp 若
    epoch 非整数，build_leader_checkpoint_section / drift / recovery_section
    抛 ValueError（int('x')）。损坏恢复 P0 要求不 crash，此为真实缺陷。
  - test_save_checkpoint_rejects_stale_epoch：save_checkpoint 的 docstring
    承诺"拒绝过期覆盖"，但实现未比较传入 cp.epoch 与持久化 epoch，直接以
    updater 透传 + 强制新 epoch 覆盖，导致旧内容覆盖新进度。
"""

import json
import tempfile
from pathlib import Path

import pytest

from common import checkpoint as ckpt
from common import data_layer
from common.leader_recovery import (
    build_leader_checkpoint_drift_section,
    build_leader_checkpoint_section,
    build_leader_recovery_section,
    leader_checkpoint,
    leader_checkpoint_drift,
)


@pytest.fixture(autouse=True)
def _iso_data_file(tmp_path: Path):
    data_file = tmp_path / "teams_data.json"
    data_layer.set_data_file(data_file)
    yield data_file


def _save_team(members: dict | None = None, team_name: str = "team",
               leader_cp: object = None) -> None:
    data = data_layer.load_data()
    team = data.setdefault("teams", {}).setdefault(team_name, {})
    team.setdefault("members", {}).update(members or {})
    if leader_cp is not None:
        team["leader_checkpoint"] = leader_cp
    data_layer.save_data(data)


def _member(extra: dict | None = None) -> dict:
    m = {"role": "coder", "agent": "claude", "last_task": "t", "last_task_completed": False}
    if extra:
        m.update(extra)
    return m


# ============================================================
# 1. leader_checkpoint 损坏恢复
# ============================================================

class TestLeaderCheckpointCorruption:
    def test_non_dict_cp_reader_returns_empty(self):
        """磁盘上 leader_checkpoint 被写坏成非 dict → reader 返回 {}，不 crash。"""
        team = {"leader_checkpoint": "garbage"}
        assert leader_checkpoint(team) == {}
        assert leader_checkpoint_drift(team) == []
        assert build_leader_checkpoint_section(team) == []
        assert build_leader_checkpoint_drift_section(team) == []

    def test_none_cp_reader_returns_empty(self):
        team = {"leader_checkpoint": None}
        assert leader_checkpoint(team) == {}

    def test_missing_cp_key_reader_returns_empty(self):
        assert leader_checkpoint({"members": {}}) == {}

    def test_dict_cp_missing_epoch_renders_empty_section(self):
        """dict cp 但缺 epoch（视为未初始化）→ section 为空，不 crash。"""
        cp = {"goal": "g", "boundaries": []}
        team = {"leader_checkpoint": cp}
        assert leader_checkpoint(team) == cp
        assert build_leader_checkpoint_section(team) == []

    def test_bad_epoch_render_degrades_gracefully(self):
        """⚠️ 缺陷回归：损坏 dict cp 的 epoch 非整数时，恢复渲染必须不 crash。

        现状：int('x') 抛 ValueError，会击穿 leader_activate /
        leader_get_recovery_context / build_leader_recovery_section ——
        恰是 leader 重启场景最致命的路径。损坏恢复 P0 要求优雅降级。
        """
        cp = {
            "epoch": "x", "version": 1, "goal": "g", "status": "active",
            "boundaries": [], "decisions": [], "plan": [], "assignments": {},
            "dependencies": [], "deadline": "", "remaining": [], "evidence": [],
            "next_actions": [], "source": "", "updated_by": "", "updated_ts": "",
        }
        team = {"leader": "lead", "members": {"lead": {"role": "leader", "agent": "claude"}},
                "leader_checkpoint": cp}
        # 这三个渲染器都不应抛异常
        assert build_leader_checkpoint_section(team) == []
        assert leader_checkpoint_drift(team) == []
        assert build_leader_checkpoint_drift_section(team) == []
        text = "\n".join(build_leader_recovery_section("team", team, "/tmp/w", "/tmp/s"))
        assert text  # 至少不 crash

    def test_epoch_float_renders(self):
        """epoch 浮点（JSON 里可能出现）→ 归一化为 int 渲染，不 crash。"""
        cp = {"epoch": 2.0, "version": 1, "goal": "g", "status": "active",
              "boundaries": [], "decisions": [], "plan": [], "assignments": {},
              "dependencies": [], "deadline": "", "remaining": [], "evidence": [],
              "next_actions": []}
        team = {"leader_checkpoint": cp}
        assert leader_checkpoint(team) == cp
        assert "epoch" in "\n".join(build_leader_checkpoint_section(team))


# ============================================================
# 2. 成员 checkpoint 损坏恢复与脏数据拒绝
# ============================================================

class TestMemberCheckpointCorruption:
    def test_load_corrupted_returns_errors_not_crash(self):
        _save_team({"alice": _member({"task_checkpoint": "garbage"})})
        cp, errors = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert cp is None
        assert errors and "dict" in " ".join(errors)

    def test_update_over_corrupted_refuses_keeps_disk(self):
        """既有 checkpoint 非法 → 拒绝覆盖，磁盘保持脏数据原样（不静默清掉）。"""
        _save_team({"alice": _member({"task_checkpoint": {"epoch": "x"}})})
        ok, err = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="bob",
            updater=lambda _c: ckpt.empty_checkpoint("t1"),
        )
        assert not ok
        assert "非法" in err
        disk = data_layer.load_data()["teams"]["team"]["members"]["alice"]["task_checkpoint"]
        assert disk == {"epoch": "x"}

    def test_load_none_when_no_checkpoint(self):
        """legacy 成员无 task_checkpoint → load 返回 (None, [])，不 crash。"""
        _save_team({"alice": _member()})
        cp, errors = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert cp is None
        assert errors == []

    def test_update_on_legacy_member_creates_fresh(self):
        """legacy 成员无 cp → update 直接创建（epoch=1，created_ts 盖章）。"""
        _save_team({"alice": _member()})
        ok, err = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.record_step_done(ckpt.empty_checkpoint("t1"), "s1"),
        )
        assert ok, err
        cp, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert cp["epoch"] == 1
        assert cp["created_ts"] and cp["updated_ts"]


# ============================================================
# 3. 旧写拒绝（防旧上下文覆盖 P0）
# ============================================================

class TestStaleWriteRejection:
    def test_verify_then_continue_rejects_stale_epoch(self):
        """续跑判定：旧 writer 的本地拷贝（epoch 已落后于磁盘）被拒，不得续跑。

        verify_then_continue 用调用方传入的 expected_epoch（应取当前磁盘 epoch）
        对照本地拷贝的 epoch：本地拷贝 epoch 落后 → 拒绝，防止旧上下文接管。
        """
        _save_team({"alice": _member()})
        # alice 写第一版（epoch=1），持有本地拷贝
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.empty_checkpoint("t1"),
        )
        stale_copy, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert stale_copy["epoch"] == 1

        # 另一个 writer 把磁盘推进到 epoch=2（新进度）
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="bob",
            updater=lambda _c: ckpt.record_step_done(_c, "newer"),
        )
        fresh, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert fresh["epoch"] == 2

        # 旧 writer 按最新磁盘 epoch 校验自己的本地拷贝 → 拷贝已过期，拒绝续跑
        ok, reason = ckpt.verify_then_continue(stale_copy, expected_epoch=fresh["epoch"])
        assert not ok
        assert "过期" in reason

    def test_save_checkpoint_rejects_stale_epoch(self):
        """⚠️ 缺陷回归：save_checkpoint 的 docstring 承诺拒绝过期覆盖，但实现未校验。

        现状：磁盘已有更新的进度（epoch=1, steps=['newer']）时，用旧 cp
        （epoch=1, task='stale'）save_checkpoint 成功并覆盖——newer 步骤丢失。
        防旧上下文覆盖 P0 要求此处拒绝。
        """
        _save_team({"alice": _member()})
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.record_step_done(_c or ckpt.empty_checkpoint("t1"), "newer"),
        )
        current, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")

        stale = ckpt.empty_checkpoint("t1", task="stale content", writer="old")
        stale["epoch"] = current["epoch"]  # 旧 writer 持有同一 epoch 的本地拷贝
        ok, err = ckpt.save_checkpoint(team_name="team", member_name="alice", cp=stale, writer="old")
        # 期望：拒绝（旧 writer 不应覆盖新进度）
        assert not ok, f"旧 writer 不应被接受: {err}"

        fresh, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert "newer" in fresh["completed_steps"], "新进度不应被旧 writer 覆盖丢失"


# ============================================================
# 4. 原子性：故障注入后磁盘保持完整
# ============================================================

class TestAtomicity:
    def test_save_failure_keeps_previous_checkpoint(self, monkeypatch):
        """写失败（save_data 抛异常）→ 磁盘仍是上一份完整 JSON，torn 不可见。

        checkpoint 模块以绑定名 `from common.data_layer import save_data` 导入，
        必须 patch common.checkpoint.save_data（monkeypatch 模块属性拦不住）。
        """
        _save_team({"alice": _member()})
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.record_step_done(ckpt.empty_checkpoint("t1"), "s1"),
        )
        before, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")

        def boom(data):
            raise OSError("disk full")

        monkeypatch.setattr(ckpt, "save_data", boom)
        with pytest.raises(OSError):
            ckpt.update_checkpoint(
                team_name="team", member_name="alice", writer="alice",
                updater=lambda _c: ckpt.record_step_done(_c, "s2"),
            )
        monkeypatch.undo()

        # 磁盘仍是 epoch=1 且仅 s1 —— 没有半写状态
        after, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert after == before
        assert after["completed_steps"] == ["s1"]

    def test_file_always_valid_json_after_writes(self):
        """多次写入后数据文件始终是可解析的完整 JSON（原子替换无残留）。"""
        _save_team({"alice": _member()})
        for i in range(5):
            ckpt.update_checkpoint(
                team_name="team", member_name="alice", writer="alice",
                updater=lambda _c: ckpt.record_step_done(_c or ckpt.empty_checkpoint("t1"), f"s{i}"),
            )
        path = data_layer.get_data_file()
        raw = path.read_text(encoding="utf-8")
        json.loads(raw)  # 必须可解析
        assert f"s4" in raw
        # 无残留临时文件
        assert not list(path.parent.glob(f".{path.name}.*.tmp"))


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
