"""
Multi-Agent MCP — MCP Server 守护进程管理
=========================================

供 TUI 使用的 MCP Server 生命周期管理函数。
通过 PID 文件追踪进程，支持 start/stop/restart/status 操作。
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
from pathlib import Path

from common.config import PROJECT_DIR, SERVER_PID_FILE, SERVER_LOG_FILE

SERVER_SCRIPT = PROJECT_DIR / "mult_agent_mcp.py"
DEFAULT_MCP_PORT = "8000"


def _mcp_port() -> str:
    """Return the configured FastMCP port as text."""
    return os.environ.get("FASTMCP_PORT", DEFAULT_MCP_PORT)


def _safe_unlink_pidfile() -> None:
    """Best-effort PID file cleanup."""
    try:
        SERVER_PID_FILE.unlink(missing_ok=True)
    except OSError:
        pass


def _write_pidfile(pid: int) -> None:
    """Best-effort PID file write."""
    try:
        SERVER_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        SERVER_PID_FILE.write_text(str(pid))
    except OSError:
        pass


# ---- PID 文件管理 ----

def _read_pidfile() -> int | None:
    """读取 PID 文件，返回 PID 或 None"""
    if not SERVER_PID_FILE.exists():
        return None
    try:
        text = SERVER_PID_FILE.read_text().strip()
        pid = int(text.split(":", 1)[0].strip())
        if pid <= 0:
            raise ValueError
    except (ValueError, OSError):
        _safe_unlink_pidfile()
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    """用 kill(pid, 0) 检测进程是否存活"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _pid_cmdline(pid: int) -> list[str]:
    """Read a process command line from procfs."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return []
    return [part.decode(errors="replace") for part in raw.split(b"\0") if part]


def _pid_cwd(pid: int) -> Path | None:
    """Read a process working directory from procfs."""
    try:
        return Path(f"/proc/{pid}/cwd").resolve()
    except OSError:
        return None


def _resolve_process_arg(pid: int, arg: str) -> Path | None:
    """Resolve an executable/script argument in a target process context."""
    if not arg or arg.startswith("-"):
        return None
    path = Path(arg)
    if not path.is_absolute():
        cwd = _pid_cwd(pid)
        if cwd is None:
            return None
        path = cwd / path
    try:
        return path.resolve()
    except OSError:
        return None


def _pid_is_project_mcp(pid: int) -> bool:
    """Return True only for this repository's MCP server process."""
    if not _pid_alive(pid):
        return False

    cmdline = _pid_cmdline(pid)
    if not cmdline:
        return False

    try:
        server_script = SERVER_SCRIPT.resolve()
    except OSError:
        server_script = SERVER_SCRIPT

    for arg in cmdline:
        resolved = _resolve_process_arg(pid, arg)
        if resolved == server_script:
            return True

    return False


def _find_port_pids(port: str) -> list[int]:
    """Find listener PIDs for a TCP port using common local tools."""
    proc_pids = _find_port_pids_proc(port)
    if proc_pids:
        return proc_pids

    commands = (
        ["ss", "-ltnp", f"sport = :{port}"],
        ["lsof", "-tiTCP:" + str(port), "-sTCP:LISTEN"],
    )
    pids: set[int] = set()

    for cmd in commands:
        try:
            result = subprocess.run(
                cmd,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=2,
                check=False,
            )
        except (FileNotFoundError, subprocess.SubprocessError, OSError):
            continue

        if cmd[0] == "ss":
            for match in re.finditer(r"pid=(\d+)", result.stdout):
                pids.add(int(match.group(1)))
        else:
            for line in result.stdout.splitlines():
                line = line.strip()
                if line.isdigit():
                    pids.add(int(line))

        if pids:
            break

    return sorted(pids)


def _find_project_mcp_on_port(port: str | None = None) -> list[int]:
    """Return verified project MCP listeners on the configured port."""
    port = port or _mcp_port()
    return [pid for pid in _find_port_pids(port) if _pid_is_project_mcp(pid)]


def _find_port_pids_proc(port: str) -> list[int]:
    """Find listener PIDs for a TCP port through procfs socket inodes."""
    try:
        port_hex = f"{int(port):04X}"
    except ValueError:
        return []

    inodes: set[str] = set()
    for table in (Path("/proc/net/tcp"), Path("/proc/net/tcp6")):
        try:
            lines = table.read_text().splitlines()
        except OSError:
            continue
        for line in lines[1:]:
            fields = line.split()
            if len(fields) < 10:
                continue
            local_address = fields[1]
            state = fields[3]
            inode = fields[9]
            _, _, local_port = local_address.rpartition(":")
            if state == "0A" and local_port.upper() == port_hex and inode != "0":
                inodes.add(inode)

    if not inodes:
        return []

    pids: set[int] = set()
    try:
        proc_dirs = list(Path("/proc").iterdir())
    except OSError:
        return []

    for proc_dir in proc_dirs:
        if not proc_dir.name.isdigit():
            continue
        fd_dir = proc_dir / "fd"
        try:
            fds = list(fd_dir.iterdir())
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(fd)
            except OSError:
                continue
            match = re.fullmatch(r"socket:\[(\d+)\]", target)
            if match and match.group(1) in inodes:
                pids.add(int(proc_dir.name))
                break

    return sorted(pids)


def _port_occupied_by_non_project(port: str | None = None) -> bool:
    """Return True if the configured port is occupied, but not by this MCP."""
    port = port or _mcp_port()
    pids = _find_port_pids(port)
    return bool(pids) and not any(_pid_is_project_mcp(pid) for pid in pids)


# ---- 进程生命周期 ----

def _spawn_mcp() -> tuple[int, str | None]:
    """
    用 subprocess.Popen 启动 MCP 守护进程。
    stdout/stderr 重定向到日志文件用于故障诊断。
    返回 (pid, None) 成功, (0, err_msg) 失败。
    """
    if not SERVER_SCRIPT.exists():
        return 0, f"脚本不存在: {SERVER_SCRIPT}"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("FASTMCP_PORT", DEFAULT_MCP_PORT)

    import datetime
    log_fp = open(SERVER_LOG_FILE, "a")
    log_fp.write(f"\n--- MCP spawned at {datetime.datetime.now()} ---\n")
    log_fp.flush()

    try:
        proc = subprocess.Popen(
            [sys.executable, str(SERVER_SCRIPT)],
            cwd=str(PROJECT_DIR),
            stdin=subprocess.DEVNULL,
            stdout=log_fp,
            stderr=log_fp,
            env=env,
            start_new_session=True,
        )
        log_fp.close()
        return proc.pid, None
    except FileNotFoundError:
        log_fp.close()
        return 0, f"Python解释器不存在: {sys.executable}"
    except PermissionError:
        log_fp.close()
        return 0, f"权限不足: {SERVER_SCRIPT}"
    except Exception as e:
        log_fp.close()
        return 0, f"启动异常: {e}"


def _find_mcp_processes() -> list[int]:
    """Find running project MCP processes via PID file or port adoption."""
    pid = _read_pidfile()
    if pid is not None:
        if _pid_is_project_mcp(pid):
            return [pid]
        _safe_unlink_pidfile()

    pids = _find_project_mcp_on_port()
    if pids:
        _write_pidfile(pids[0])
        return pids
    return []


# ---- 公共 API ----

def mcp_server_status() -> tuple[bool, str]:
    """返回 (running, status_text)。"""
    pids = _find_mcp_processes()
    if pids:
        port = _mcp_port()
        return True, f"🟢 运行中 (PID: {', '.join(map(str, pids))}, 端口: {port})"
    return False, "⚫ 未启动"


def start_mcp_server() -> tuple[bool, str]:
    """启动 MCP Server 为守护进程，PID 写入文件。返回 (ok, msg)。"""
    pids = _find_mcp_processes()
    if pids:
        port = _mcp_port()
        return True, f"已在运行 (PID: {', '.join(map(str, pids))}, 端口: {port})"

    port = _mcp_port()
    if _port_occupied_by_non_project(port):
        return False, f"❌ 端口 {port} 已被非本项目进程占用，未启动 MCP Server"

    new_pid, err = _spawn_mcp()
    if err is not None:
        return False, f"❌ 守护进程启动失败: {err}"

    _write_pidfile(new_pid)

    import time
    for delay in (0.5, 1.0):
        time.sleep(delay)
        if _pid_is_project_mcp(new_pid):
            return True, f"✅ 守护进程已启动 (PID: {new_pid})"

    # 进程已死，尝试读取日志定位原因
    tail = ""
    if SERVER_LOG_FILE.exists():
        try:
            lines = SERVER_LOG_FILE.read_text().splitlines()
            tail = "\n".join(lines[-5:])
        except Exception:
            pass
    _safe_unlink_pidfile()
    return False, f"❌ 进程启动后退出 (PID: {new_pid})\n日志尾部:\n{tail}"


def stop_mcp_server() -> tuple[bool, str]:
    """Find the project MCP server and stop it. 返回 (ok, msg)。"""
    pids = _find_mcp_processes()
    if not pids:
        port = _mcp_port()
        if _port_occupied_by_non_project(port):
            return True, f"端口 {port} 被非本项目进程占用，未执行停止操作"
        return True, "MCP Server 未在运行"

    import time
    stopped: list[int] = []
    failed: list[int] = []

    for pid in pids:
        if not _pid_is_project_mcp(pid):
            failed.append(pid)
            continue

        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            stopped.append(pid)
            continue

        for _ in range(30):
            time.sleep(0.1)
            if not _pid_alive(pid):
                stopped.append(pid)
                break
        else:
            try:
                os.kill(pid, signal.SIGKILL)
                time.sleep(0.1)
            except OSError:
                pass
            if not _pid_alive(pid):
                stopped.append(pid)
            else:
                failed.append(pid)

    _safe_unlink_pidfile()
    if failed:
        return False, f"❌ 无法停止进程 (PID: {', '.join(map(str, failed))})"
    return True, f"✅ 守护进程已停止 (PID: {', '.join(map(str, stopped))})"


def restart_mcp_server() -> tuple[bool, str]:
    """重启 MCP Server。返回 (ok, msg)。"""
    stop_mcp_server()
    import time
    time.sleep(0.5)
    return start_mcp_server()
