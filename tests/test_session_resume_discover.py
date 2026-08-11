"""
P4b 纯函数：discover_codex_session 首启真实 session 回填（临时 home 真实布局）。

在临时 CODEX_HOME 构造**真实** Codex 布局：
    <home>/sessions/<year>/<month>/<day>/rollout-<ts>-<uuid>.jsonl
rollout 首部恒为 type=session_meta 事件，payload 带真实 session_id / id / cwd。

覆盖（对应 P4b 验收点）：
  1. 唯一候选回填：时间窗内 + cwd 匹配 + 唯一真实 session_id → ok=True 返回真实 uuid
  2. 多个候选拒绝：时间窗内两个不同真实 session_id → ok=False 歧义（只 checkpoint）
  3. cwd 不匹配拒绝：payload.cwd 与 workspace_dir 不同 → 不算候选（禁扫到别的会话）
  4. 旧时间窗拒绝：rollout mtime 早于 spawn_ts 时钟偏差 → 不算候选
  5. 未来时间窗拒绝：rollout mtime 晚于 spawn_ts + window → 不算候选
  6. 无 session_meta → 无真实 id → 不算候选
  7. 假目录不认：凭空 mkdir 的 sessions/<id> 无 rollout 证据 → 不算候选
  8. subagent 线程去重：同一真实 session_id 多条 rollout（共享父会话）→ 去重后唯一
     → ok=True（subagent 共享 resumable 父会话，不构成歧义）

隔离：全部注入临时 CODEX_HOME，绝不触真实 ~/.codex / 真实凭证 / API。
"""

import os
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from common import session_resume as sr


def _write_rollout(codex_home: Path, *, sid: str, cwd: str,
                   mtime: float, rollout_id: str = "", day: str = "10") -> Path:
    """写一条真实布局 rollout，返回路径。mtime 由调用方控制（时间窗验证）。"""
    d = codex_home / "sessions" / "2026" / "08" / day
    d.mkdir(parents=True, exist_ok=True)
    rid = rollout_id or sid
    r = d / f"rollout-2026-08-10T00-00-00-{rid}.jsonl"
    payload = {
        "session_id": sid,
        "id": rid,
        "cwd": cwd,
        "thread_source": "terminal",
    }
    r.write_text('{"type":"session_meta","payload":%s}\n' % (
        __import__("json").dumps(payload)), encoding="utf-8")
    os.utime(r, (mtime, mtime))
    return r


class TestDiscoverUniqueBackfill(unittest.TestCase):
    """唯一候选回填：时间窗 + cwd 匹配 + 唯一真实 session_id → 返回真实 uuid。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex_home"
        self.spawn_ts = time.time()
        self.cwd = str(self.root / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_unique_candidate_backfilled(self):
        sid = str(uuid.uuid4())
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=self.spawn_ts)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["session_id"], sid)   # 真实 uuid，非自造
        self.assertTrue(res["path"].endswith(".jsonl"))
        self.assertEqual(res["cwd"], self.cwd)

    def test_clock_skew_negative_allowed(self):
        """rollout mtime 略早于 spawn_ts（时钟偏差）仍视为该次 spawn 新产生。"""
        sid = str(uuid.uuid4())
        mtime = self.spawn_ts - 2.0  # 在 _DISCOVER_CLOCK_SKEW=5 内
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=mtime)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertTrue(res["ok"], res)

    def test_subagent_threads_dedup_to_one(self):
        """同一真实 session_id 多条 rollout（subagent 共享父会话）→ 去重唯一，仍回填。"""
        sid = str(uuid.uuid4())
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=self.spawn_ts,
                       rollout_id=f"{sid}-0001")
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=self.spawn_ts,
                       rollout_id=f"{sid}-0002")
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["session_id"], sid)

    def test_real_id_from_payload_not_filename(self):
        """P4b 关键：可恢复会话 id 取 payload.session_id，不是文件名末段 uuid。
        subagent 线程文件名 uuid 是线程身份；payload.session_id 才是可恢复父会话。"""
        thread_id = str(uuid.uuid4())
        parent_sid = str(uuid.uuid4())
        _write_rollout(self.codex_home, sid=parent_sid, cwd=self.cwd,
                       mtime=self.spawn_ts, rollout_id=thread_id)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["session_id"], parent_sid)
        self.assertNotEqual(res["session_id"], thread_id)


class TestDiscoverAmbiguityRejected(unittest.TestCase):
    """多个候选拒绝：两个不同真实 session_id → 歧义，只 checkpoint。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex_home"
        self.spawn_ts = time.time()
        self.cwd = str(self.root / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_two_distinct_sessions_rejected(self):
        _write_rollout(self.codex_home, sid=str(uuid.uuid4()), cwd=self.cwd, mtime=self.spawn_ts)
        _write_rollout(self.codex_home, sid=str(uuid.uuid4()), cwd=self.cwd, mtime=self.spawn_ts)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])
        self.assertIn("歧义", res["reason"])
        self.assertEqual(len(res["candidates"]), 2)


class TestDiscoverCwdMismatch(unittest.TestCase):
    """cwd 不匹配拒绝：payload.cwd 与 workspace_dir 不同 → 不算候选。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex_home"
        self.spawn_ts = time.time()
        self.cwd = str(self.root / "ws")
        self.other = str(self.root / "other")

    def tearDown(self):
        self.tmp.cleanup()

    def test_cwd_mismatch_rejected(self):
        sid = str(uuid.uuid4())
        _write_rollout(self.codex_home, sid=sid, cwd=self.other, mtime=self.spawn_ts)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])           # cwd 不匹配 → 无候选
        self.assertEqual(res["candidates"], [])

    def test_empty_cwd_rejected(self):
        sid = str(uuid.uuid4())
        _write_rollout(self.codex_home, sid=sid, cwd="", mtime=self.spawn_ts)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])           # 无法确认 cwd → 不算候选


class TestDiscoverTimeWindow(unittest.TestCase):
    """旧/未来时间窗拒绝：rollout mtime 不在 [spawn_ts-偏差, spawn_ts+window] → 不算候选。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex_home"
        self.spawn_ts = time.time()
        self.cwd = str(self.root / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_old_window_rejected(self):
        """早于 spawn_ts - 时钟偏差 → 旧时间窗拒绝。"""
        sid = str(uuid.uuid4())
        mtime = self.spawn_ts - 100.0
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=mtime)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])

    def test_future_window_rejected(self):
        """晚于 spawn_ts + window → 超窗拒绝（不是本次 spawn 产生）。"""
        sid = str(uuid.uuid4())
        mtime = self.spawn_ts + 1000.0
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=mtime)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])

    def test_window_edge_accepted(self):
        """恰好落在上界内 → 仍视为新产生。"""
        sid = str(uuid.uuid4())
        mtime = self.spawn_ts + 200.0  # < 默认 300 上界
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=mtime)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertTrue(res["ok"], res)

    def test_custom_window_short(self):
        """window_seconds 可注入：窗口窄于候选 mtime 偏移 → 排除；放宽后通过。"""
        sid = str(uuid.uuid4())
        _write_rollout(self.codex_home, sid=sid, cwd=self.cwd, mtime=self.spawn_ts + 10)
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home),
                                        window_seconds=2)
        self.assertFalse(res["ok"])
        res2 = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home),
                                         window_seconds=20)
        self.assertTrue(res2["ok"], res2)


class TestDiscoverMissingEvidence(unittest.TestCase):
    """无 session_meta / 假目录不认：没有 rollout 证据一律不算候选。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.codex_home = self.root / "codex_home"
        self.spawn_ts = time.time()
        self.cwd = str(self.root / "ws")

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_session_meta_not_candidate(self):
        """rollout 首行无 session_meta → 无真实 id，不算候选。"""
        sid = str(uuid.uuid4())
        d = self.codex_home / "sessions" / "2026" / "08" / "10"
        d.mkdir(parents=True, exist_ok=True)
        r = d / f"rollout-2026-08-10T00-00-00-{sid}.jsonl"
        r.write_text('{"type":"message","payload":{"role":"user"}}\n', encoding="utf-8")
        os.utime(r, (self.spawn_ts, self.spawn_ts))
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])
        self.assertEqual(res["candidates"], [])

    def test_fake_dir_without_rollout_not_candidate(self):
        """凭空 mkdir 的 sessions/<id> 假目录（无 rollout 文件）一律不认。"""
        sid = str(uuid.uuid4())
        fake = self.codex_home / "sessions" / sid
        fake.mkdir(parents=True)
        (fake / "session_meta.json").write_text(
            '{"session_id": "%s"}' % sid, encoding="utf-8")
        # 无 rollout-*.jsonl → _codex_rollout_paths 扫不到任何证据
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])
        self.assertEqual(res["candidates"], [])

    def test_empty_codex_home(self):
        res = sr.discover_codex_session(self.spawn_ts, self.cwd, str(self.codex_home))
        self.assertFalse(res["ok"])
        self.assertEqual(res["candidates"], [])


if __name__ == "__main__":
    unittest.main()
