#!/usr/bin/env python3
"""任务B: 数据恢复。普通 python3 进程(不 import unittest/pytest,不触发守卫)。"""
import hashlib, json, os, sys, tempfile, time
from pathlib import Path

HOME = Path("/home/zwc/.mult_agent_mcp")
LIVE = HOME / "teams_data.json"
BAK = HOME / "teams_data.pre-pool-refactor.json"


def md5(p):
    return hashlib.md5(p.read_bytes()).hexdigest()


def snapshot(p):
    st = p.stat()
    return (st.st_mtime_ns, st.st_size, md5(p))


def atomic_write(p, data):
    p = Path(p)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".teams_data.tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def merge_data(live, bak):
    teams = dict(live.get("teams", {}))
    restored = []
    if "cppipc-dds" not in teams:
        teams["cppipc-dds"] = bak["teams"]["cppipc-dds"]
        restored.append("teams/cppipc-dds")
    if "t" in teams:
        del teams["t"]
    out = dict(live)
    out["teams"] = teams
    if "agent_users" not in live:
        out["agent_users"] = bak["agent_users"]
        restored.append("agent_users")
    dl = out.get("_deleted_legacy_teams")
    if isinstance(dl, dict) and "cppipc-dds" in dl:
        del dl["cppipc-dds"]
        restored.append("_deleted_legacy_teams/cppipc-dds")
    elif isinstance(dl, list) and "cppipc-dds" in dl:
        dl.remove("cppipc-dds")
        restored.append("_deleted_legacy_teams(list)/cppipc-dds")
    return out, restored


def main():
    if not LIVE.exists() or not BAK.exists():
        print("FATAL: live or backup missing")
        return 1
    s0 = snapshot(LIVE)
    print("live before:", "mtime_ns=%d" % s0[0], "size=%d" % s0[1], "md5=%s" % s0[2])
    for attempt in range(1, 4):
        live = json.loads(LIVE.read_text(encoding="utf-8"))
        bak = json.loads(BAK.read_text(encoding="utf-8"))
        if attempt == 1:
            ts = time.strftime("%Y%m%d_%H%M%S")
            bakpath = HOME / ("teams_data.pre-restore-%s.json" % ts)
            atomic_write(bakpath, live)
            os.chmod(bakpath, 0o600)
            print("PERSISTENT BACKUP: %s md5=%s size=%d mode=%o" % (
                bakpath.name, md5(bakpath), bakpath.stat().st_size,
                bakpath.stat().st_mode & 0o777))
        new, restored = merge_data(live, bak)
        s1 = snapshot(LIVE)
        if s1 != s0:
            print("attempt %d: CAS mismatch -> retry" % attempt)
            s0 = s1
            time.sleep(1)
            continue
        atomic_write(LIVE, new)
        print("attempt %d: written, restored=%s" % (attempt, restored))
        print("live after: size=%d mode=%o md5=%s" % (
            LIVE.stat().st_size, LIVE.stat().st_mode & 0o777, md5(LIVE)))
        return 0
    print("FATAL: CAS mismatch after 3 attempts, 放弃(未做半写)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
