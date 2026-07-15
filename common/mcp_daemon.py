"""
Multi-Agent MCP — MCP Server 守护进程管理
=========================================

供 TUI 使用的 MCP Server 生命周期管理函数。
通过 PID 文件追踪进程，支持 start/stop/restart/status 操作。
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from common.config import PROJECT_DIR, SERVER_PID_FILE, SERVER_LOG_FILE

SERVER_SCRIPT = PROJECT_DIR / "mult_agent_mcp.py"


# ---- PID 文件管理 ----

def _read_pidfile() -> int | None:
    """读取 PID 文件，返回 PID 或 None"""
    if not SERVER_PID_FILE.exists():
        return None
    try:
        pid = int(SERVER_PID_FILE.read_text().strip())
    except (ValueError, OSError):
        SERVER_PID_FILE.unlink(missing_ok=True)
        return None
    return pid


def _pid_alive(pid: int) -> bool:
    """用 kill(pid, 0) 检测进程是否存活"""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


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
    env.setdefault("FASTMCP_PORT", "8000")

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
    """通过 PID 文件获取守护进程 PID，用 kill(0) 验证存活。"""
    pid = _read_pidfile()
    if pid is not None:
        if _pid_alive(pid):
            return [pid]
        else:
            SERVER_PID_FILE.unlink(missing_ok=True)
    return []


# ---- 公共 API ----

def mcp_server_status() -> tuple[bool, str]:
    """返回 (running, status_text)。"""
    pids = _find_mcp_processes()
    if pids:
        port = os.environ.get("FASTMCP_PORT", "8000")
        return True, f"🟢 运行中 (PID: {', '.join(map(str, pids))}, 端口: {port})"
    return False, "⚫ 未启动"


def start_mcp_server() -> tuple[bool, str]:
    """启动 MCP Server 为守护进程，PID 写入文件。返回 (ok, msg)。"""
    pid = _read_pidfile()
    if pid is not None and _pid_alive(pid):
        port = os.environ.get("FASTMCP_PORT", "8000")
        return True, f"已在运行 (PID: {pid}, 端口: {port})"

    if pid is not None:
        SERVER_PID_FILE.unlink(missing_ok=True)

    new_pid, err = _spawn_mcp()
    if err is not None:
        return False, f"❌ 守护进程启动失败: {err}"

    SERVER_PID_FILE.write_text(str(new_pid))

    import time
    for delay in (0.5, 1.0):
        time.sleep(delay)
        if _pid_alive(new_pid):
            return True, f"✅ 守护进程已启动 (PID: {new_pid})"

    # 进程已死，尝试读取日志定位原因
    tail = ""
    if SERVER_LOG_FILE.exists():
        try:
            lines = SERVER_LOG_FILE.read_text().splitlines()
            tail = "\n".join(lines[-5:])
        except Exception:
            pass
    SERVER_PID_FILE.unlink(missing_ok=True)
    return False, f"❌ 进程启动后退出 (PID: {new_pid})\n日志尾部:\n{tail}"


def stop_mcp_server() -> tuple[bool, str]:
    """通过 PID 文件找到守护进程并 kill。返回 (ok, msg)。"""
    pid = _read_pidfile()
    if pid is None:
        return True, "MCP Server 未在运行"

    if not _pid_alive(pid):
        SERVER_PID_FILE.unlink(missing_ok=True)
        return True, "MCP Server 进程已不存在（PID 文件已清理）"

    import time
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        SERVER_PID_FILE.unlink(missing_ok=True)
        return True, "进程已不存在"

    for _ in range(30):
        time.sleep(0.1)
        if not _pid_alive(pid):
            SERVER_PID_FILE.unlink(missing_ok=True)
            return True, f"✅ 守护进程已停止 (PID: {pid})"

    try:
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.1)
    except OSError:
        pass

    SERVER_PID_FILE.unlink(missing_ok=True)
    if not _pid_alive(pid):
        return True, f"✅ 守护进程已强制停止 (PID: {pid})"
    return False, f"❌ 无法停止进程 (PID: {pid})"


def restart_mcp_server() -> tuple[bool, str]:
    """重启 MCP Server。返回 (ok, msg)。"""
    stop_mcp_server()
    import time
    time.sleep(0.5)
    return start_mcp_server()
