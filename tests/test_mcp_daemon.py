import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common.mcp_daemon as mcp_daemon


class McpDaemonLifecycleTests(unittest.TestCase):
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

    def test_legacy_integer_pidfile_is_supported(self):
        self.pid_file.write_text("12345")

        self.assertEqual(mcp_daemon._read_pidfile(), 12345)

    def test_extended_pidfile_with_port_is_supported(self):
        self.pid_file.write_text("12345:8000")

        self.assertEqual(mcp_daemon._read_pidfile(), 12345)

    def test_status_adopts_project_mcp_on_port_when_pidfile_missing(self):
        with mock.patch.object(mcp_daemon, "_find_port_pids", return_value=[2222]):
            with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
                running, status = mcp_daemon.mcp_server_status()

        self.assertTrue(running)
        self.assertIn("2222", status)
        self.assertEqual(self.pid_file.read_text(), "2222")

    def test_stale_pid_cleanup_failure_does_not_crash(self):
        with mock.patch.object(Path, "unlink", side_effect=OSError):
            mcp_daemon._safe_unlink_pidfile()

    def test_start_adopts_existing_project_mcp_without_spawning(self):
        with mock.patch.object(mcp_daemon, "_find_port_pids", return_value=[2222]):
            with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
                with mock.patch.object(mcp_daemon, "_spawn_mcp") as spawn:
                    ok, msg = mcp_daemon.start_mcp_server()

        self.assertTrue(ok)
        self.assertIn("已在运行", msg)
        spawn.assert_not_called()
        self.assertEqual(self.pid_file.read_text(), "2222")

    def test_start_refuses_non_project_port_owner(self):
        with mock.patch.object(mcp_daemon, "_find_port_pids", return_value=[3333]):
            with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=False):
                with mock.patch.object(mcp_daemon, "_spawn_mcp") as spawn:
                    ok, msg = mcp_daemon.start_mcp_server()

        self.assertFalse(ok)
        self.assertIn("非本项目进程占用", msg)
        spawn.assert_not_called()
        self.assertFalse(self.pid_file.exists())

    def test_port_discovery_prefers_procfs_fallback(self):
        with mock.patch.object(mcp_daemon, "_find_port_pids_proc", return_value=[3333]):
            with mock.patch.object(mcp_daemon.subprocess, "run") as run:
                pids = mcp_daemon._find_port_pids("8000")

        self.assertEqual(pids, [3333])
        run.assert_not_called()

    def test_stop_discovers_project_mcp_on_port_when_pidfile_missing(self):
        with mock.patch.object(mcp_daemon, "_find_port_pids", return_value=[4444]):
            with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=True):
                with mock.patch.object(mcp_daemon, "_pid_alive", return_value=False):
                    with mock.patch.object(mcp_daemon.os, "kill") as kill:
                        ok, msg = mcp_daemon.stop_mcp_server()

        self.assertTrue(ok)
        self.assertIn("4444", msg)
        kill.assert_called_once_with(4444, mcp_daemon.signal.SIGTERM)
        self.assertFalse(self.pid_file.exists())

    def test_stop_does_not_kill_non_project_port_owner(self):
        with mock.patch.object(mcp_daemon, "_find_port_pids", return_value=[5555]):
            with mock.patch.object(mcp_daemon, "_pid_is_project_mcp", return_value=False):
                with mock.patch.object(mcp_daemon.os, "kill") as kill:
                    ok, msg = mcp_daemon.stop_mcp_server()

        self.assertTrue(ok)
        self.assertIn("非本项目进程占用", msg)
        kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
