"""
成员任务 checkpoint 纯数据层 helper（common.checkpoint）测试。

覆盖:
  - 纯函数: empty_checkpoint / validate_checkpoint / record_step_done(幂等) /
    next_epoch / hash_bytes / hash_file / verify_artifacts / verify_then_continue
  - 持久化: save / update / load / clear 的原子写、epoch 单调、读取校验、
    幂等 verify-then-continue、旧 writer 拒绝(防旧上下文覆盖)

隔离: 测试均在 data_layer.set_data_file(tmp) 临时数据文件上执行，
不触碰真实 ~/.mult_agent_mcp/（conftest 环境级兜底 + atomic_json_write 守卫）。
"""

import hashlib
import tempfile
from pathlib import Path

import pytest

from common import checkpoint as ckpt
from common import data_layer

# ---- 测试环境隔离: 单条覆盖隔离全仓（data_layer / tui / mcp 共用同一数据文件） ----
# 每个测试用独立的 tmp_path，天然互不污染，无需在 teardown 复位。
@pytest.fixture(autouse=True)
def _iso_data_file(tmp_path: Path):
    data_file = tmp_path / "teams_data.json"
    data_layer.set_data_file(data_file)
    yield data_file


def _save_team(members: dict | None = None, team_name: str = "team") -> None:
    data = data_layer.load_data()
    team = data.setdefault("teams", {}).setdefault(team_name, {})
    team.setdefault("members", {}).update(members or {})
    data_layer.save_data(data)


def _member(extra: dict | None = None) -> dict:
    m = {
        "role": "coder",
        "agent": "claude",
        "last_task": "实现登录接口",
        "last_task_completed": False,
        "last_context": "t1 上下文",
    }
    if extra:
        m.update(extra)
    return m


def _load_member(team_name: str = "team", member_name: str = "alice") -> dict:
    return data_layer.load_data()["teams"][team_name]["members"][member_name]


def _load_cp(team_name: str = "team", member_name: str = "alice") -> dict | None:
    return _load_member(team_name, member_name).get(ckpt.MEMBER_CHECKPOINT_KEY)


# ============================================================
# 纯函数: 结构 / epoch / 幂等 / 哈希
# ============================================================

class TestValidateCheckpoint:
    def test_empty_checkpoint_is_valid(self):
        ok, errors = ckpt.validate_checkpoint(ckpt.empty_checkpoint("t1"))
        assert ok, errors

    def test_missing_task_id_invalid(self):
        cp = ckpt.empty_checkpoint("t1")
        del cp["task_id"]
        ok, errors = ckpt.validate_checkpoint(cp)
        assert not ok
        assert any("task_id" in e for e in errors)

    def test_non_int_epoch_invalid(self):
        cp = ckpt.empty_checkpoint("t1")
        cp["epoch"] = "1"
        ok, errors = ckpt.validate_checkpoint(cp)
        assert not ok
        assert any("epoch" in e for e in errors)

    def test_duplicate_steps_invalid(self):
        cp = ckpt.empty_checkpoint("t1")
        cp["completed_steps"] = ["a", "a"]
        ok, errors = ckpt.validate_checkpoint(cp)
        assert not ok
        assert any("重复" in e for e in errors)

    def test_bad_artifact_hash_invalid(self):
        cp = ckpt.empty_checkpoint("t1")
        cp["artifacts"] = {"a.txt": "not-a-hash"}
        ok, errors = ckpt.validate_checkpoint(cp)
        assert not ok
        assert any("哈希" in e for e in errors)

    def test_good_artifact_hash_valid(self):
        cp = ckpt.empty_checkpoint("t1")
        cp["artifacts"] = {"a.txt": ckpt.hash_bytes(b"hello")}
        ok, errors = ckpt.validate_checkpoint(cp)
        assert ok, errors

    def test_bad_state_invalid(self):
        cp = ckpt.empty_checkpoint("t1")
        cp["state"] = "running_now"
        ok, errors = ckpt.validate_checkpoint(cp)
        assert not ok
        assert any("state" in e for e in errors)

    def test_none_invalid(self):
        ok, errors = ckpt.validate_checkpoint(None)
        assert not ok

    def test_wrong_version_invalid(self):
        cp = ckpt.empty_checkpoint("t1")
        cp["version"] = 999
        ok, errors = ckpt.validate_checkpoint(cp)
        assert not ok
        assert any("version" in e for e in errors)


class TestRecordStepIdempotent:
    def test_record_then_dedup(self):
        cp = ckpt.empty_checkpoint("t1")
        cp = ckpt.record_step_done(cp, "step1")
        cp = ckpt.record_step_done(cp, "step1")
        assert cp["completed_steps"] == ["step1"]

    def test_does_not_mutate_input(self):
        cp = ckpt.empty_checkpoint("t1")
        original = ckpt.empty_checkpoint("t1")
        ckpt.record_step_done(cp, "step1")
        assert cp == original


class TestEpoch:
    def test_next_epoch_from_none_is_1(self):
        assert ckpt.next_epoch(None) == 1

    def test_next_epoch_increments(self):
        assert ckpt.next_epoch({"epoch": 3}) == 4

    def test_next_epoch_bad_epoch_resets_to_1(self):
        assert ckpt.next_epoch({"epoch": "x"}) == 1


class TestHashing:
    def test_hash_bytes_is_md5_hex(self):
        assert ckpt.hash_bytes(b"hello") == hashlib.md5(b"hello").hexdigest()
        assert len(ckpt.hash_bytes(b"x")) == 32

    def test_hash_file_matches_hash_bytes(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"data")
        assert ckpt.hash_file(f) == ckpt.hash_bytes(b"data")

    def test_verify_artifacts_missing(self, tmp_path: Path):
        cp = ckpt.empty_checkpoint("t1")
        cp["artifacts"] = {"gone.txt": ckpt.hash_bytes(b"x")}
        mismatches = ckpt.verify_artifacts(cp, tmp_path)
        assert len(mismatches) == 1
        assert "缺失" in mismatches[0]

    def test_verify_artifacts_mismatch(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"v1")
        cp = ckpt.empty_checkpoint("t1")
        cp["artifacts"] = {"a.txt": ckpt.hash_bytes(b"v2")}
        mismatches = ckpt.verify_artifacts(cp, tmp_path)
        assert len(mismatches) == 1
        assert "不一致" in mismatches[0]

    def test_verify_artifacts_clean(self, tmp_path: Path):
        f = tmp_path / "a.txt"
        f.write_bytes(b"v1")
        cp = ckpt.empty_checkpoint("t1")
        cp["artifacts"] = {"a.txt": ckpt.hash_bytes(b"v1")}
        assert ckpt.verify_artifacts(cp, tmp_path) == []


class TestVerifyThenContinue:
    def _running_cp(self, epoch: int = 1, writer: str = "alice") -> dict:
        cp = ckpt.empty_checkpoint("t1", writer=writer)
        cp["epoch"] = epoch
        cp["completed_steps"] = ["design"]
        cp["current_step"] = "code"
        cp["remaining_instruction"] = "继续编码"
        return cp

    def test_pass_when_current(self):
        ok, reason = ckpt.verify_then_continue(self._running_cp(), expected_epoch=1, expected_writer="alice")
        assert ok, reason

    def test_fail_on_stale_epoch(self):
        ok, reason = ckpt.verify_then_continue(self._running_cp(epoch=1), expected_epoch=2)
        assert not ok
        assert "过期" in reason

    def test_fail_on_wrong_writer(self):
        ok, reason = ckpt.verify_then_continue(self._running_cp(writer="alice"), expected_writer="bob")
        assert not ok
        assert "writer" in reason

    def test_fail_on_done_state(self):
        cp = self._running_cp()
        cp["state"] = "done"
        ok, reason = ckpt.verify_then_continue(cp, expected_epoch=1)
        assert not ok
        assert "done" in reason

    def test_fail_on_task_id_mismatch(self):
        ok, reason = ckpt.verify_then_continue(self._running_cp(), task_id="other")
        assert not ok
        assert "task_id" in reason

    def test_fail_on_none(self):
        ok, reason = ckpt.verify_then_continue(None)
        assert not ok


# ============================================================
# 持久化: 原子写 / 读取校验 / 幂等续跑 / 旧 writer 拒绝
# ============================================================

class TestPersistence:
    def _init_team_with_member(self):
        _save_team({"alice": _member()})

    def test_save_then_load_roundtrip(self):
        self._init_team_with_member()
        cp = ckpt.empty_checkpoint("t1", task="实现登录", writer="alice")
        cp = ckpt.record_step_done(cp, "design")
        cp["current_step"] = "code"
        ok, err = ckpt.save_checkpoint(team_name="team", member_name="alice", cp=cp, writer="alice")
        assert ok, err

        loaded, errors = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert errors == []
        assert loaded is not None
        assert loaded["epoch"] == 1  # 首次写入自动 epoch=1
        assert loaded["writer"] == "alice"
        assert loaded["completed_steps"] == ["design"]
        assert loaded["task"] == "实现登录"

    def test_update_auto_stamps_epoch_and_timestamps(self):
        self._init_team_with_member()
        ok, _ = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _cur: ckpt.record_step_done(ckpt.empty_checkpoint("t1"), "design"),
        )
        assert ok
        ok, _ = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _cur: ckpt.record_step_done(_cur, "code"),
        )
        assert ok

        loaded, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert loaded["epoch"] == 2
        assert loaded["completed_steps"] == ["design", "code"]
        assert loaded["created_ts"] and loaded["updated_ts"]

    def test_update_epoch_monotonic_persisted(self):
        self._init_team_with_member()
        for i in range(3):
            ckpt.update_checkpoint(
                team_name="team", member_name="alice", writer="alice",
                updater=lambda _cur: ckpt.record_step_done(_cur or ckpt.empty_checkpoint("t1"), f"s{i}"),
            )
        loaded, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert loaded["epoch"] == 3
        assert loaded["completed_steps"] == ["s0", "s1", "s2"]

    def test_load_none_when_no_checkpoint(self):
        self._init_team_with_member()
        loaded, errors = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert loaded is None
        assert errors == []

    def test_missing_team_fails(self):
        ok, err = ckpt.save_checkpoint(team_name="nope", member_name="alice", cp=ckpt.empty_checkpoint("t1"), writer="alice")
        assert not ok
        assert "不存在" in err

    def test_updater_abort_writes_nothing(self):
        self._init_team_with_member()
        ok, err = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice", updater=lambda _cur: None,
        )
        assert not ok
        assert "放弃" in err
        assert _load_cp() is None

    def test_clear_is_idempotent(self):
        self._init_team_with_member()
        ckpt.update_checkpoint(team_name="team", member_name="alice", writer="alice",
                               updater=lambda _c: ckpt.empty_checkpoint("t1"))
        assert _load_cp() is not None
        assert ckpt.clear_checkpoint(team_name="team", member_name="alice") is True
        assert _load_cp() is None
        assert ckpt.clear_checkpoint(team_name="team", member_name="alice") is True

    def test_clear_missing_member_returns_false(self):
        _save_team({})
        assert ckpt.clear_checkpoint(team_name="team", member_name="ghost") is False


class TestVerifyContinueEndToEnd:
    """端到端: 写→读→verify→续跑→旧 writer 被拒。"""

    def _init(self):
        _save_team({"alice": _member()})

    def test_fresh_writer_continues_old_writer_rejected(self):
        self._init()
        # alice 写第一版（epoch=1）
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.record_step_done(ckpt.empty_checkpoint("t1"), "design"),
        )
        loaded, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        # alice 按手上的 epoch=1 拷贝 verify，通过即可续跑
        ok, reason = ckpt.verify_then_continue(loaded, expected_epoch=1, expected_writer="alice")
        assert ok, reason

        # 并发换号场景: bob 接管同一任务（基于 epoch=1 的 CAS 写入）→ epoch=2
        ok, _ = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="bob", expected_epoch=1,
            updater=lambda _c: ckpt.record_step_done(_c or ckpt.empty_checkpoint("t1"), "code"),
        )
        assert ok

        # alice 的旧拷贝（epoch=1）尝试继续提交 → CAS 拒绝（磁盘已是 epoch=2），
        # 旧上下文不得覆盖新进度
        ok, reason = ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice", expected_epoch=1,
            updater=lambda _c: ckpt.record_step_done(_c, "test"),
        )
        assert not ok
        assert "已被更新" in reason

        # 磁盘上仍是 bob 的最新版
        fresh, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert fresh["epoch"] == 2
        assert fresh["writer"] == "bob"
        assert "test" not in fresh["completed_steps"]

        # bob 自己的最新拷贝重新 verify → 通过
        ok, reason = ckpt.verify_then_continue(fresh, expected_epoch=2, expected_writer="bob")
        assert ok, reason

    def test_verify_then_continue_records_idempotent(self):
        self._init()
        ckpt.update_checkpoint(
            team_name="team", member_name="alice", writer="alice",
            updater=lambda _c: ckpt.empty_checkpoint("t1"),
        )
        loaded, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        ok, reason = ckpt.verify_then_continue(loaded, expected_epoch=loaded["epoch"])
        assert ok, reason
        # 续跑时幂等追加已完成步骤（重复项被合并）
        def updater(cur):
            cp = ckpt.record_step_done(cur, "compile")
            return ckpt.record_step_done(cp, "compile")
        ok, _ = ckpt.update_checkpoint(team_name="team", member_name="alice", writer="alice", updater=updater)
        assert ok
        fresh, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert fresh["completed_steps"].count("compile") == 1

    def test_artifact_hash_then_verify_clean_and_drift(self, tmp_path: Path):
        self._init()
        f = tmp_path / "out.py"
        f.write_bytes(b"v1")
        cp = ckpt.empty_checkpoint("t1")
        cp["artifacts"] = {"out.py": ckpt.hash_file(f)}
        ckpt.update_checkpoint(team_name="team", member_name="alice", writer="alice", updater=lambda _c: cp)

        loaded, _ = ckpt.load_checkpoint(team_name="team", member_name="alice")
        assert ckpt.verify_artifacts(loaded, tmp_path) == []  # 未漂移

        f.write_bytes(b"v2")  # 产物被改写 → 漂移
        mismatches = ckpt.verify_artifacts(loaded, tmp_path)
        assert len(mismatches) == 1
        assert "不一致" in mismatches[0]

    def test_checkpoint_to_lines(self):
        cp = ckpt.empty_checkpoint("t1", task="实现登录", writer="alice")
        cp = ckpt.record_step_done(cp, "design")
        cp["current_step"] = "code"
        lines = ckpt.checkpoint_to_lines(cp)
        joined = "\n".join(lines)
        assert "实现登录" in joined
        assert "design" in joined
        assert "code" in joined
