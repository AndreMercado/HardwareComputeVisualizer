"""Unit tests for remote.py (UC-2): SSH command line, error messages, the agent's message
stream, the Popen-shaped RemoteProc and the RemotePoller's staleness rule. No network:
the SSH session is replaced by fakes."""
import io
import json
import threading
import time
import unittest
from unittest import mock

import _setup  # noqa: F401

import remote


class FakeAgent:
    """Stands in for the ssh subprocess running remote_agent.py."""

    def __init__(self, lines=()):
        self.stdout = io.StringIO("".join(json.dumps(l) + "\n" for l in lines))
        self.stdin = io.StringIO()

    def sent(self):
        return [json.loads(l) for l in self.stdin.getvalue().splitlines()]


def target_with(lines=()):
    t = remote.RemoteTarget("box", "andre", None, runner=lambda cmd: ["true"])
    t.agent = FakeAgent(lines)
    t.alive = True
    return t


class SshTest(unittest.TestCase):
    def test_key_only_batch_login_with_keepalive(self):
        argv = remote.ssh_runner("10.0.0.5", "me", r"C:\k\id", port=2222)("echo hi")
        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)        # never asks for a password
        self.assertIn("IdentitiesOnly=yes", argv)
        self.assertIn("ServerAliveInterval=3", argv)
        self.assertEqual(argv[argv.index("-p") + 1], "2222")
        self.assertEqual(argv[argv.index("-i") + 1], r"C:\k\id")
        self.assertEqual(argv[-2:], ["me@10.0.0.5", "echo hi"])

    def test_errors_are_explained(self):
        cases = {
            "Permission denied (publickey).": "didn't accept the key",
            "ssh: Could not resolve hostname nope": "doesn't resolve",
            "connect to host x port 22: Connection timed out": "timed out",
            "connect to host x port 22: Connection refused": "is the SSH server running",
            "WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!": "host key",
            "something odd\nlast line": "SSH failed: last line",
            "": "no error message",
        }
        for err, want in cases.items():
            with self.subTest(err=err):
                self.assertIn(want, remote._ssh_error(err, "x", 22, "k"))

    def test_agent_folder_pattern(self):
        self.assertTrue(remote.DIR_RE.match("/tmp/hcv-agent-ab12_x"))
        for bad in ("/tmp/other", "tmp/hcv-agent-x", "/tmp/hcv-agent-x; rm -rf ~", "/tmp/hcv-agent-"):
            self.assertFalse(remote.DIR_RE.match(bad), bad)


class ConnectTest(unittest.TestCase):
    def run_connect(self, stdout, code=0, stderr=""):
        t = remote.RemoteTarget("box", "me", None, runner=lambda cmd: ["fake", cmd])
        done =mock.Mock(stdout=stdout, stderr=stderr, returncode=code)
        with mock.patch.object(remote.subprocess, "run", return_value=done), \
                mock.patch.object(remote.subprocess, "Popen") as popen:
            try:
                t.connect()
            except remote.RemoteError as e:
                return str(e), popen
        return None, popen

    def test_missing_python(self):
        err, popen = self.run_connect("HCV_NO_PYTHON\n", code=3)
        self.assertIn("Python 3 isn't installed", err)
        popen.assert_not_called()

    def test_old_python_is_refused_and_cleaned_up(self):
        err, popen = self.run_connect("HCV_DIR /tmp/hcv-agent-abc 3.6\n")
        self.assertIn("needs 3.8", err)
        self.assertIn("rm -rf", popen.call_args[0][0][-1])  # the uploaded folder is removed

    def test_unexpected_folder_is_refused(self):
        err, _ = self.run_connect("HCV_DIR /home/me/x 3.12\n")
        self.assertIn("Unexpected agent folder", err)

    def test_ssh_failure_is_explained(self):
        err, _ = self.run_connect("", code=255, stderr="Permission denied (publickey).")
        self.assertIn("didn't accept the key", err)

    def test_missing_key_file(self):
        t = remote.RemoteTarget("box", "me", r"C:\no\such\key", runner=lambda cmd: ["fake"])
        with self.assertRaisesRegex(remote.RemoteError, "Key file not found"):
            t.connect()


class MessageStreamTest(unittest.TestCase):
    def test_messages_are_dispatched(self):
        t = target_with([
            {"hello": {"cpu": "EPYC"}},
            {"sources": {"cpu_temp": "k10temp"}},
            {"tel": {"cpu_util": 40.0}, "info": {"pcie_link": "x16"}},
            {"wl": '{"gbps": 12}'},
            {"ended": 0},
        ])
        t.proc = remote.RemoteProc(t)
        t._read()
        self.assertEqual(t._hello.get_nowait(), {"cpu": "EPYC"})
        self.assertEqual(t.sources, {"cpu_temp": "k10temp"})
        self.assertEqual(t.latest["cpu_util"], 40.0)
        self.assertIn("t", t.latest)
        self.assertEqual(t.info, {"pcie_link": "x16"})
        self.assertEqual(list(t.proc.stdout), ['{"gbps": 12}'])
        self.assertEqual(t.proc.poll(), 0)

    def test_lost_connection_fails_the_running_workload(self):
        t = target_with([])  # the ssh session ends at once
        t.proc = remote.RemoteProc(t)
        t._read()
        self.assertFalse(t.alive)
        self.assertEqual(t.lost_reason, "Connection to box lost.")
        self.assertEqual(t.proc.poll(), -1)
        self.assertIn("connection to box lost", json.loads(next(iter(t.proc.stdout)))["error"])

    def test_bad_lines_are_skipped(self):
        t = target_with([])
        t.agent.stdout = io.StringIO('not json\n{"sources": {"a": 1}}\n')
        t._read()
        self.assertEqual(t.sources, {"a": 1})

    def test_start_sends_the_command(self):
        t = target_with()
        proc = t.start("cpu2d", 30, command=None)
        self.assertIsInstance(proc, remote.RemoteProc)
        self.assertEqual(t.agent.sent()[-1], {"cmd": "start", "script": "cpu2d", "seconds": 30, "disk": None,
                                             "command": None})

    def test_start_on_a_dead_link_raises(self):
        t = target_with()
        t.alive = False
        with self.assertRaises(OSError):
            t.start("cpu2d", 30)


class RemoteProcTest(unittest.TestCase):
    def test_terminate_waits_for_the_agent(self):
        t = target_with()
        p = remote.RemoteProc(t)
        threading.Timer(0.1, p._finish, args=(0,)).start()  # the agent confirms the stop
        self.assertFalse(p.terminate(timeout=2))
        self.assertEqual(t.agent.sent()[-1], {"cmd": "stop"})

    def test_terminate_without_confirmation_is_forced(self):
        p = remote.RemoteProc(target_with())
        self.assertTrue(p.terminate(timeout=0.2))
        self.assertEqual(p.poll(), -9)

    def test_finish_is_only_recorded_once(self):
        p = remote.RemoteProc(target_with())
        p._finish(0)
        p._finish(-9)
        self.assertEqual(p.poll(), 0)
        self.assertFalse(p.terminate())  # already ended: nothing to force


class RemotePollerTest(unittest.TestCase):
    def test_fresh_sample_is_passed_on(self):
        t = target_with()
        poller = t.poller(0, None)
        poller.start()
        self.assertEqual(t.agent.sent()[-1], {"cmd": "watch", "gpu": 0, "disk": None})
        t.latest = {"cpu_util": 5.0, "t": time.monotonic()}
        self.assertEqual(poller.latest["cpu_util"], 5.0)

    def test_first_samples_may_take_a_moment(self):
        poller = target_with().poller(None, None)
        poller.start()
        self.assertEqual(poller.latest, {})

    def test_stale_or_silent_means_every_metric_unavailable(self):  # UC-12 / UC-2 alt c
        t = target_with()
        poller = t.poller(None, None)
        poller.start()
        t.latest = {"cpu_util": 5.0, "t": time.monotonic() - remote.STALE_S - 1}
        self.assertEqual(set(poller.latest), {"t"})
        t.latest = {}
        poller.t_watch -= 10
        self.assertEqual(set(poller.latest), {"t"})


if __name__ == "__main__":
    unittest.main()
