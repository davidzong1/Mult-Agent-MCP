"""
MCP Server 启动失败修复 — socks:// 代理环境回归测试
=================================================

背景（P0）：
  当环境存在 ALL_PROXY=socks://127.0.0.1:7890/ 时，FastMCP 启动横幅的 PyPI
  版本检查（log_server_banner -> check_for_newer_version -> httpx.get）会被
  httpx 判为 Unknown scheme（httpx 不支持 socks://）并抛 ValueError，导致
  MCP Server 在绑定端口前崩溃退出、起即死。

修复：
  1. mult_agent_mcp.py 模块顶部在导入 fastmcp（其实例化 settings）之前设置
     FASTMCP_CHECK_FOR_UPDATES=off，并新增 _disable_fastmcp_version_check()
     在 main() 中直接对 settings 再做一次关闭（覆盖环境变量被覆盖的场景）。
  2. common/mcp_daemon.py 的 start_mcp_server() 现在只在确认「进程存活且端口
     开始监听（MCP readiness）」后才返回成功；不再仅凭 PID 存活判成功。

覆盖：
  - 模块级导入即关闭版本检查（真实入口语义）
  - _disable_fastmcp_version_check 在 socks 代理环境下生效
  - _wait_for_mcp_ready 的三种结局（就绪/进程退出/超时）
  - start_mcp_server 仅在 readiness 成功后返回成功；未就绪时失败并清理 PID
  - 真实冷启动 E2E：socks 代理环境下 start → 端口监听 → MCP initialize 握手
    响应（非仅 PID）→ status → stop → 再次 start（重启语义）
"""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common.mcp_daemon as mcp_daemon
import mult_agent_mcp

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SOCKS_PROXY = "socks://127.0.0.1:7890/"


def _free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _mcp_initialize(port: int) -> str:
    """发送 MCP initialize JSON-RPC 请求，返回 HTTP 状态行 + 响应体。

    用 http.client（stdlib，直连 socket，不经 httpx/代理），避免 socks:// 代理
    干扰测试客户端本身。
    """
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "startup-check", "version": "1.0"},
        },
    }).encode()
    conn = http.client.HTTPConnection("127.0.0.1", int(port), timeout=15)
    try:
        conn.request("POST", "/mcp", body=body, headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        })
        resp = conn.getresponse()
        data = resp.read().decode(errors="replace")
        return f"HTTP/1.1 {resp.status}\n{data}"
    finally:
        conn.close()


class FastMcpVersionCheckGuardTests(unittest.TestCase):
    """修复 1：关闭 FastMCP 非必要版本检查。"""

    # 本类测试会修改进程级全局状态：环境变量（ALL_PROXY / all_proxy /
    # FASTMCP_CHECK_FOR_UPDATES，经 _disable_fastmcp_version_check()）以及
    # fastmcp.settings.check_for_updates 单例。这些修改会影响同进程内后续
    # 测试，必须先保存、每个测试后恢复，保证无全局环境污染。
    _ENV_KEYS = (
        "ALL_PROXY", "all_proxy",
        "HTTP_PROXY", "http_proxy",
        "HTTPS_PROXY", "https_proxy",
        "NO_PROXY", "no_proxy",
        "FASTMCP_CHECK_FOR_UPDATES",
    )

    def setUp(self):
        self._env_backup = {k: os.environ.get(k) for k in self._ENV_KEYS}
        try:
            import fastmcp
            self._fastmcp_settings_backup = fastmcp.settings.check_for_updates
        except Exception:  # noqa: BLE001
            self._fastmcp_settings_backup = None
        self.addCleanup(self._restore_global_state)

    def _restore_global_state(self):
        for k in self._ENV_KEYS:
            v = self._env_backup.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if self._fastmcp_settings_backup is not None:
            try:
                import fastmcp
                fastmcp.settings.check_for_updates = self._fastmcp_settings_backup
            except Exception:  # noqa: BLE001
                pass

    def test_import_alone_disables_version_check_like_real_entry(self):
        """真实入口：模块顶部在 fastmcp 之前设置 FASTMCP_CHECK_FOR_UPDATES=off。"""
        code = (
            "import os\n"
            f"os.environ['ALL_PROXY'] = {SOCKS_PROXY!r}\n"
            f"os.environ['all_proxy'] = {SOCKS_PROXY!r}\n"
            "import mult_agent_mcp\n"
            "import fastmcp\n"
            "print('check_for_updates=', fastmcp.settings.check_for_updates)\n"
        )
        env = os.environ.copy()
        env.pop("FASTMCP_CHECK_FOR_UPDATES", None)
        result = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT), env=env, timeout=60,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check_for_updates= off", result.stdout)

    def test_disable_version_check_under_socks_proxy(self):
        os.environ["ALL_PROXY"] = SOCKS_PROXY
        os.environ["all_proxy"] = SOCKS_PROXY
        mult_agent_mcp._disable_fastmcp_version_check()
        import fastmcp
        self.assertEqual(fastmcp.settings.check_for_updates, "off")
        self.assertEqual(os.environ.get("FASTMCP_CHECK_FOR_UPDATES"), "off")


class McpDaemonReadinessTests(unittest.TestCase):
    """修复 2：start 仅在端口 readiness 成功后返回成功。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.pid_file = Path(self.tmp.name) / "mcp_server.pid"
        self.log_file = Path(self.tmp.name) / "mcp_server.log"
        self.pid_patch = mock.patch.object(mcp_daemon, "SERVER_PID_FILE", self.pid_file)
        self.log_patch = mock.patch.object(mcp_daemon, "SERVER_LOG_FILE", self.log_file)
        self.pid_patch.start()
        self.log_patch.start()
        self.addCleanup(self.pid_patch.stop)
        self.addCleanup(self.log_patch.stop)

    def test_wait_ready_success_when_alive_and_listening(self):
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
            with mock.patch.object(mcp_daemon, "_port_listening", return_value=True):
                with mock.patch.object(mcp_daemon, "_find_project_mcp_on_port",
                                       return_value=[12345]):
                    ok, reason = mcp_daemon._wait_for_mcp_ready(12345, "8000", timeout=5)
        self.assertTrue(ok)
        self.assertIn("8000", reason)

    def test_wait_ready_rejects_port_owned_by_non_project(self):
        """端口可连接但监听者非本项目 → 视为未就绪，不误报成功。"""
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
            with mock.patch.object(mcp_daemon, "_port_listening", return_value=True):
                with mock.patch.object(mcp_daemon, "_find_project_mcp_on_port",
                                       return_value=[]):
                    with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project",
                                           return_value=True):
                        ok, reason = mcp_daemon._wait_for_mcp_ready(
                            12345, "8000", timeout=5)
        self.assertFalse(ok)
        self.assertIn("非本项目进程占用", reason)

    def test_wait_ready_fails_when_process_dies(self):
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=False):
            ok, reason = mcp_daemon._wait_for_mcp_ready(12345, "8000", timeout=5)
        self.assertFalse(ok)
        self.assertIn("退出", reason)

    def test_wait_ready_times_out_when_port_never_listens(self):
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
            with mock.patch.object(mcp_daemon, "_port_listening", return_value=False):
                ok, reason = mcp_daemon._wait_for_mcp_ready(12345, "8000", timeout=0.2)
        self.assertFalse(ok)
        self.assertIn("超时", reason)

    def test_start_succeeds_only_when_ready(self):
        with mock.patch.object(mcp_daemon, "_find_mcp_processes", return_value=[]):
            with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=False):
                with mock.patch.object(mcp_daemon, "_spawn_mcp", return_value=(12345, None)):
                    with mock.patch.object(mcp_daemon, "_wait_for_mcp_ready",
                                           return_value=(True, "端口 8000 已就绪")):
                        ok, msg = mcp_daemon.start_mcp_server()
        self.assertTrue(ok)
        self.assertIn("已启动", msg)
        self.assertIn("12345", msg)
        self.assertEqual(self.pid_file.read_text(), "12345")

    def test_start_fails_when_not_ready_cleans_pid_and_includes_log(self):
        self.log_file.write_text("line1\nTraceback\nValueError: boom\n")
        with mock.patch.object(mcp_daemon, "_find_mcp_processes", return_value=[]):
            with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=False):
                with mock.patch.object(mcp_daemon, "_spawn_mcp", return_value=(12345, None)):
                    with mock.patch.object(mcp_daemon, "_wait_for_mcp_ready",
                                           return_value=(False, "进程启动后退出")):
                        with mock.patch.object(mcp_daemon, "_terminate_pid") as term:
                            ok, msg = mcp_daemon.start_mcp_server()
        self.assertFalse(ok)
        self.assertIn("进程启动后退出", msg)
        self.assertIn("日志尾部", msg)
        self.assertFalse(self.pid_file.exists(), "未就绪时不应保留 PID 文件")
        term.assert_called_once_with(12345)

    def test_start_not_ready_terminates_still_alive_child(self):
        """未就绪但子进程仍存活时，必须终止/收割，避免孤立后台进程。"""
        self.log_file.write_text("boot\n")
        with mock.patch.object(mcp_daemon, "_find_mcp_processes", return_value=[]):
            with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=False):
                with mock.patch.object(mcp_daemon, "_spawn_mcp", return_value=(12345, None)):
                    with mock.patch.object(mcp_daemon, "_wait_for_mcp_ready",
                                           return_value=(False, "等待端口 8000 就绪超时")):
                        with mock.patch.object(mcp_daemon, "_terminate_pid") as term:
                            ok, msg = mcp_daemon.start_mcp_server()
        self.assertFalse(ok)
        self.assertIn("超时", msg)
        term.assert_called_once_with(12345)
        self.assertFalse(self.pid_file.exists())


class McpDaemonSocksProxyE2ETests(unittest.TestCase):
    """真实冷启动 E2E：socks 代理环境下 start→握手→status→stop→restart。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        self.pid_file = self.home / "mcp_server.pid"
        self.log_file = self.home / "mcp_server.log"
        self.pid_patch = mock.patch.object(mcp_daemon, "SERVER_PID_FILE", self.pid_file)
        self.log_patch = mock.patch.object(mcp_daemon, "SERVER_LOG_FILE", self.log_file)
        self.pid_patch.start()
        self.log_patch.start()
        self.addCleanup(self.pid_patch.stop)
        self.addCleanup(self.log_patch.stop)
        self.port = _free_port()
        self._env_keys = (
            "MULT_AGENT_MCP_HOME", "FASTMCP_PORT",
            "ALL_PROXY", "all_proxy", "HTTPS_PROXY", "https_proxy",
        )
        self._old_env = {k: os.environ.get(k) for k in self._env_keys}
        self.addCleanup(self._restore_env)

    def _restore_env(self):
        for k in self._env_keys:
            v = self._old_env[k]
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _set_socks_env(self):
        os.environ["MULT_AGENT_MCP_HOME"] = str(self.home)
        os.environ["FASTMCP_PORT"] = str(self.port)
        os.environ["ALL_PROXY"] = SOCKS_PROXY
        os.environ["all_proxy"] = SOCKS_PROXY
        os.environ["HTTPS_PROXY"] = "http://127.0.0.1:7890/"
        os.environ["https_proxy"] = "http://127.0.0.1:7890/"

    def test_cold_start_handshake_status_restart_under_socks_proxy(self):
        self._set_socks_env()
        try:
            ok, msg = mcp_daemon.start_mcp_server()
            self.assertTrue(ok, f"start 应成功: {msg}")
            self.assertIn("已启动", msg)

            # 端口必须真正就绪（非仅 PID 存活）
            self.assertTrue(mcp_daemon._port_listening(self.port),
                            "start 成功后端口应处于监听")

            # MCP 握手必须能响应（验收：完成启动握手并响应 MCP 请求）
            resp = _mcp_initialize(self.port)
            self.assertIn("HTTP/1.1 200", resp, resp)
            self.assertIn("result", resp, resp)

            # 状态查询应为运行中
            running, status = mcp_daemon.mcp_server_status()
            self.assertTrue(running, status)
        finally:
            stop_ok, stop_msg = mcp_daemon.stop_mcp_server()
            self.assertTrue(stop_ok, f"stop 应成功: {stop_msg}")
            self.assertFalse(mcp_daemon._port_listening(self.port),
                             "stop 后端口应关闭")

        # 重启语义：再次冷启动必须同样成功
        ok, msg = mcp_daemon.start_mcp_server()
        self.assertTrue(ok, f"重启应成功: {msg}")
        self.assertTrue(mcp_daemon._port_listening(self.port))
        resp = _mcp_initialize(self.port)
        self.assertIn("result", resp, resp)
        mcp_daemon.stop_mcp_server()
        self.assertFalse(mcp_daemon._port_listening(self.port))


class SpawnLogWriteFailureTests(unittest.TestCase):
    """收口修正：日志文件 open/写 header 失败必须返回 (0, 可诊断错误)。

    修复前：_spawn_mcp 里 `log_fp = open(SERVER_LOG_FILE, "a")` 与 header 写入
    在 try 之外，日志目录/文件不可写（PermissionError/FileNotFoundError 等
    OSError 子类）时异常会直接从 start_mcp_server 冒泡导致启动崩溃。
    修复后：_spawn_mcp 一律返回 (0, 带路径与原因的诊断错误)。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_spawn_returns_error_when_log_open_permission_denied(self):
        with mock.patch(
            "builtins.open",
            side_effect=PermissionError("[Errno 13] Permission denied: 'x'"),
        ):
            pid, err = mcp_daemon._spawn_mcp()
        self.assertEqual(pid, 0)
        self.assertIsNotNone(err)
        self.assertIn("无法打开日志文件", err)
        self.assertIn("Permission", err)

    def test_spawn_returns_error_when_log_dir_missing(self):
        log_file = Path(self.tmp.name) / "missing" / "mcp_server.log"
        with mock.patch.object(mcp_daemon, "SERVER_LOG_FILE", log_file):
            pid, err = mcp_daemon._spawn_mcp()
        self.assertEqual(pid, 0)
        self.assertIsNotNone(err)
        self.assertIn("无法打开日志文件", err)
        self.assertIn(str(log_file), err)

    def test_start_returns_diagnosable_failure_not_exception(self):
        """日志不可写时 start_mcp_server 返回失败消息，不冒泡异常。"""
        log_file = Path(self.tmp.name) / "missing" / "mcp_server.log"
        with mock.patch.object(mcp_daemon, "_find_mcp_processes", return_value=[]):
            with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project",
                                   return_value=False):
                with mock.patch.object(mcp_daemon, "SERVER_LOG_FILE", log_file):
                    ok, msg = mcp_daemon.start_mcp_server()
        self.assertFalse(ok)
        self.assertIn("守护进程启动失败", msg)
        self.assertIn("无法打开日志文件", msg)
        self.assertIn(str(log_file), msg)

    def test_spawn_still_succeeds_when_log_writable(self):
        """回归：日志可写时 _spawn_mcp 正常写 header 并返回 pid。"""
        log_file = Path(self.tmp.name) / "mcp_server.log"
        proc = mock.Mock()
        proc.pid = 999
        with mock.patch.object(mcp_daemon, "SERVER_LOG_FILE", log_file):
            with mock.patch("subprocess.Popen", return_value=proc) as popen:
                pid, err = mcp_daemon._spawn_mcp()
        self.assertEqual((pid, err), (999, None))
        popen.assert_called_once()
        self.assertIn("MCP spawned", log_file.read_text())


if __name__ == "__main__":
    unittest.main()
