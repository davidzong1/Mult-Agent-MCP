"""
MCP 守护进程黑盒验收 + P0 启动故障回归测试。
=============================================

背景（P0 启动故障）：
  FastMCP 在启动横幅阶段调用 check_for_newer_version()，经 httpx 访问 PyPI。
  当环境含 socks:// 代理（如 ALL_PROXY=socks://127.0.0.1:7890/）且未安装
  httpx[socks] 时，httpx 构造 Client 会抛 ValueError: Unknown scheme for
  proxy URL，异常未被 FastMCP 捕获 → 服务器在启动握手期崩溃退出、端口从不监听。

生产修复（已入工作树）：
  1) mult_agent_mcp.py main() 在 mcp.run 前 fastmcp.settings.check_for_updates='off'
     可靠禁用非必要版本检查，隔离代理副作用。
  2) common/mcp_daemon.py start_mcp_server 改为“端口/MCP 就绪后才返回成功”：
     - _wait_for_mcp_ready 校验监听 PID 属于本项目（非仅 socket connect），
       防止端口被非本项目进程抢占时误报成功；
     - 就绪超时且子进程仍存活时 _terminate_pid 终止/收割后再清 PID，避免孤立进程；
     - stop_mcp_server 用 _process_stopped（僵尸=已停止）避免 stop 误报失败。

本文件覆盖：
  A. 单元回归：_process_stopped / _terminate_pid / _wait_for_mcp_ready /
     start 失败清理路径。
  B. 真实服务器黑盒验收：cold start、status、重复 start、restart、stop 后 start，
     并验证客户端能完成 MCP initialize + tools/list 握手（“服务器可用”而非仅 PID）。

数据隔离：经 MULT_AGENT_MCP_HOME 指向临时目录 + 空闲端口，绝不触碰真实
teams_data.json / 守护进程 pid / log。
"""

import json
import os
import shutil
import socket
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

import common.mcp_daemon as mcp_daemon

MCP_PROTOCOL_VERSION = "2025-06-18"


# ============================================================
# A. 单元回归：新语义
# ============================================================

class ProcessStoppedUnitTests(unittest.TestCase):
    """_process_stopped：不存在/僵尸 = 已停止，存活可运行 = 未停止。"""

    def test_true_when_not_alive(self):
        with mock.patch.object(mcp_daemon, "_pid_alive", return_value=False):
            self.assertTrue(mcp_daemon._process_stopped(123))

    def test_true_for_zombie(self):
        """僵尸：kill(0) 仍成功但 cmdline 已清空 → 视为已停止（修复 stop 误报）。"""
        with mock.patch.object(mcp_daemon, "_pid_alive", return_value=True):
            with mock.patch.object(mcp_daemon, "_pid_cmdline", return_value=[]):
                self.assertTrue(mcp_daemon._process_stopped(123))

    def test_false_when_running(self):
        with mock.patch.object(mcp_daemon, "_pid_alive", return_value=True):
            with mock.patch.object(
                mcp_daemon, "_pid_cmdline", return_value=["python", "mult_agent_mcp.py"]
            ):
                self.assertFalse(mcp_daemon._process_stopped(123))


class TerminatePidUnitTests(unittest.TestCase):
    """_terminate_pid：SIGTERM → 等待 → SIGKILL，最后收割，不留孤立进程。"""

    def _run(self, pid, stopped_value):
        with mock.patch.object(mcp_daemon.os, "kill") as kill:
            with mock.patch.object(mcp_daemon, "_process_stopped", return_value=stopped_value):
                with mock.patch.object(mcp_daemon, "_reap_child") as reap:
                    with mock.patch("time.sleep"):
                        mcp_daemon._terminate_pid(pid, timeout=0.05)
        return kill, reap

    def test_sigterm_then_sigkill_when_still_alive(self):
        kill, reap = self._run(7777, stopped_value=False)
        calls = [c.args for c in kill.mock_calls]
        self.assertEqual(
            calls,
            [(7777, mcp_daemon.signal.SIGTERM), (7777, mcp_daemon.signal.SIGKILL)],
        )
        reap.assert_called_once_with(7777)

    def test_only_sigterm_when_already_stopped(self):
        kill, reap = self._run(7778, stopped_value=True)
        kill.assert_called_once_with(7778, mcp_daemon.signal.SIGTERM)
        reap.assert_called_once_with(7778)

    def test_reaps_when_kill_raises(self):
        """进程已不存在时 os.kill 抛 OSError → 仅收割，不再等待。"""
        with mock.patch.object(mcp_daemon.os, "kill", side_effect=OSError):
            with mock.patch.object(mcp_daemon, "_reap_child") as reap:
                mcp_daemon._terminate_pid(7779, timeout=0.05)
        reap.assert_called_once_with(7779)


class ReadinessUnitTests(unittest.TestCase):
    """_wait_for_mcp_ready：监听者必须属于本项目，端口被抢占/进程退出/超时均不就绪。"""

    def test_ready_true_when_project_listens(self):
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
            with mock.patch.object(mcp_daemon, "_port_listening", return_value=True):
                with mock.patch.object(mcp_daemon, "_find_project_mcp_on_port", return_value=[9999]):
                    with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=False):
                        ok, msg = mcp_daemon._wait_for_mcp_ready(9999, "8000", timeout=1)
        self.assertTrue(ok)
        self.assertIn("9999", msg)

    def test_ready_false_when_port_taken_by_non_project(self):
        """端口可连接但监听者非本项目 → 不就绪（避免误报成功）。"""
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
            with mock.patch.object(mcp_daemon, "_port_listening", return_value=True):
                with mock.patch.object(mcp_daemon, "_find_project_mcp_on_port", return_value=[]):
                    with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=True):
                        ok, msg = mcp_daemon._wait_for_mcp_ready(9999, "8000", timeout=1)
        self.assertFalse(ok)
        self.assertIn("非本项目进程占用", msg)

    def test_ready_false_when_process_exits(self):
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=False):
            ok, msg = mcp_daemon._wait_for_mcp_ready(9999, "8000", timeout=1)
        self.assertFalse(ok)
        self.assertIn("进程启动后退出", msg)

    def test_ready_false_on_timeout(self):
        with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
            with mock.patch.object(mcp_daemon, "_port_listening", return_value=False):
                with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=False):
                    ok, msg = mcp_daemon._wait_for_mcp_ready(9999, "8000", timeout=0.05)
        self.assertFalse(ok)
        self.assertIn("超时", msg)


class StartFailureCleanupTests(unittest.TestCase):
    """start 只有在就绪后才返回成功；失败时终止/收割子进程并清理 PID。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.pid_file = root / "mcp_server.pid"
        self.log_file = root / "mcp_server.log"
        self.pid_patch = mock.patch.object(mcp_daemon, "SERVER_PID_FILE", self.pid_file)
        self.log_patch = mock.patch.object(mcp_daemon, "SERVER_LOG_FILE", self.log_file)
        self.pid_patch.start()
        self.log_patch.start()
        self.addCleanup(self.pid_patch.stop)
        self.addCleanup(self.log_patch.stop)

    def _start_with_ready(self, ready_result):
        with mock.patch.object(mcp_daemon, "_find_mcp_processes", return_value=[]):
            with mock.patch.object(mcp_daemon, "_port_occupied_by_non_project", return_value=False):
                with mock.patch.object(mcp_daemon, "_spawn_mcp", return_value=(7777, None)):
                    with mock.patch.object(
                        mcp_daemon, "_wait_for_mcp_ready", return_value=ready_result
                    ):
                        with mock.patch.object(mcp_daemon, "_terminate_pid") as term:
                            with mock.patch.object(mcp_daemon, "_safe_unlink_pidfile") as unlink:
                                ok, msg = mcp_daemon.start_mcp_server()
        return ok, msg, term, unlink

    def test_success_only_when_ready(self):
        ok, msg, term, unlink = self._start_with_ready((True, "端口 8000 已就绪"))
        self.assertTrue(ok, msg)
        self.assertIn("7777", msg)
        self.assertEqual(self.pid_file.read_text(), "7777")
        term.assert_not_called()
        unlink.assert_not_called()

    def test_failure_terminates_child_and_clears_pid(self):
        """未就绪：必须终止/收割仍存活的子进程，再清理 PID（避免孤立进程）。"""
        ok, msg, term, unlink = self._start_with_ready((False, "等待端口 8000 就绪超时"))
        self.assertFalse(ok)
        self.assertIn("等待端口 8000 就绪超时", msg)
        term.assert_called_once_with(7777)
        unlink.assert_called_once()


# ============================================================
# B. 真实服务器黑盒验收
# ============================================================

class _BlackBoxBase(unittest.TestCase):
    """隔离环境：临时 home + 空闲端口 + 环境接管；兜底清理，绝不触碰真实数据。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)

        self.pid_patch = mock.patch.object(
            mcp_daemon, "SERVER_PID_FILE", self.home / "mcp_server.pid"
        )
        self.log_patch = mock.patch.object(
            mcp_daemon, "SERVER_LOG_FILE", self.home / "mcp_server.log"
        )
        self.pid_patch.start()
        self.log_patch.start()
        self.addCleanup(self.pid_patch.stop)
        self.addCleanup(self.log_patch.stop)

        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        self.port = sock.getsockname()[1]
        sock.close()

        self._env_backup = os.environ.copy()
        os.environ["MULT_AGENT_MCP_HOME"] = str(self.home)
        os.environ["FASTMCP_PORT"] = str(self.port)
        # 默认移除 socks 代理变量；具体测试按需重新注入
        os.environ.pop("ALL_PROXY", None)
        os.environ.pop("all_proxy", None)
        self.addCleanup(self._restore_env)
        self.addCleanup(self._cleanup_server)

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env_backup)

    def _cleanup_server(self):
        """兜底：无论测试结果如何，终止任何残留的本项目 MCP 进程。"""
        try:
            for pid in mcp_daemon._find_port_pids(str(self.port)):
                if mcp_daemon._pid_is_project_mcp(pid):
                    mcp_daemon._terminate_pid(pid)
            mcp_daemon.stop_mcp_server()
        except Exception:
            pass

    def _port_connects(self) -> bool:
        try:
            with socket.create_connection(("127.0.0.1", self.port), timeout=0.3):
                return True
        except OSError:
            return False

    def _mcp_handshake_ok(self) -> tuple[bool, str]:
        """客户端完成 MCP initialize → notifications/initialized → tools/list。

        返回 (ok, 详情)。用 urllib + ProxyHandler({})，绕开环境代理，只连本地。
        """
        base = f"http://127.0.0.1:{self.port}/mcp"

        def rpc(method, params=None, sid=None, rid=1):
            body = json.dumps(
                {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
            ).encode()
            headers = {
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MCP_PROTOCOL_VERSION,
            }
            if sid:
                headers["Mcp-Session-Id"] = sid
            req = urllib.request.Request(base, data=body, headers=headers, method="POST")
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            resp = opener.open(req, timeout=15)
            return resp.headers, resp.read().decode()

        try:
            headers, content = rpc(
                "initialize",
                {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {},
                 "clientInfo": {"name": "blackbox", "version": "1.0"}},
                rid=1,
            )
            sid = headers.get("Mcp-Session-Id")
            if not sid:
                return False, "initialize 未返回 Mcp-Session-Id"
            rpc("notifications/initialized", sid=sid, rid=2)
            _, tools_resp = rpc("tools/list", sid=sid, rid=3)
            for line in tools_resp.splitlines():
                if line.startswith("data: "):
                    data = json.loads(line[6:])
                    tools = data.get("result", {}).get("tools", [])
                    return len(tools) > 0, f"tools={len(tools)}"
            return False, "tools/list 响应缺少 data 行"
        except Exception as e:  # noqa: BLE001
            return False, f"{type(e).__name__}: {e}"


class McpServerColdStartSocksRegressionTests(_BlackBoxBase):
    """P0 回归：socks:// 代理环境下冷启动必须可用（端口/MCP 就绪才算成功）。"""

    def test_cold_start_under_socks_proxy_env_becomes_usable(self):
        os.environ["ALL_PROXY"] = "socks://127.0.0.1:7890/"
        os.environ["all_proxy"] = "socks://127.0.0.1:7890/"

        ok, msg = mcp_daemon.start_mcp_server()
        self.assertTrue(ok, f"socks 代理下冷启动应成功: {msg}")
        self.assertIn("端口", msg, msg)

        running, status = mcp_daemon.mcp_server_status()
        self.assertTrue(running, status)

        ok2, detail = self._mcp_handshake_ok()
        self.assertTrue(ok2, f"MCP initialize/tools-list 握手应成功: {detail}")

        stop_ok, stop_msg = mcp_daemon.stop_mcp_server()
        self.assertTrue(stop_ok, f"stop 应成功（僵尸判定修复）: {stop_msg}")
        running_after, _ = mcp_daemon.mcp_server_status()
        self.assertFalse(running_after)


class McpServerLifecycleBlackBoxTests(_BlackBoxBase):
    """cold start → status → 重复 start → restart → stop → stop 后 start。"""

    def test_lifecycle_cold_status_dup_restart_stop_start(self):
        # cold start
        ok, msg = mcp_daemon.start_mcp_server()
        self.assertTrue(ok, msg)
        pid1 = int(mcp_daemon.SERVER_PID_FILE.read_text().strip())
        running, _ = mcp_daemon.mcp_server_status()
        self.assertTrue(running)
        ok, detail = self._mcp_handshake_ok()
        self.assertTrue(ok, detail)

        # 重复 start：幂等、同一 PID、不重复拉起
        ok, msg = mcp_daemon.start_mcp_server()
        self.assertTrue(ok, msg)
        self.assertIn("已在运行", msg)
        self.assertEqual(int(mcp_daemon.SERVER_PID_FILE.read_text().strip()), pid1)

        # restart：新 PID，仍可用
        ok, msg = mcp_daemon.restart_mcp_server()
        self.assertTrue(ok, msg)
        pid2 = int(mcp_daemon.SERVER_PID_FILE.read_text().strip())
        self.assertNotEqual(pid2, pid1)
        ok, detail = self._mcp_handshake_ok()
        self.assertTrue(ok, detail)

        # stop：状态未启动、端口关闭、返回成功
        ok, msg = mcp_daemon.stop_mcp_server()
        self.assertTrue(ok, f"stop 应成功: {msg}")
        running, _ = mcp_daemon.mcp_server_status()
        self.assertFalse(running)
        self.assertFalse(self._port_connects())

        # stop 后 start：再次可用
        ok, msg = mcp_daemon.start_mcp_server()
        self.assertTrue(ok, msg)
        ok, detail = self._mcp_handshake_ok()
        self.assertTrue(ok, detail)

        mcp_daemon.stop_mcp_server()


if __name__ == "__main__":
    unittest.main()
