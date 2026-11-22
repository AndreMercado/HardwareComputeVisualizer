"""Unit tests for control.py - the local JSON API (AI CONTROL and the VS Code editor link).
A real ControlServer runs on a free port; a thread plays the app's UI side."""
import json
import os
import queue
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import _setup  # noqa: F401

import control


class ServerTest(unittest.TestCase):
    def setUp(self):
        self.token_file = os.path.join(tempfile.mkdtemp(prefix="hcv-ctl-"), "token.txt")
        self.server = control.ControlServer(port=0, token_file=self.token_file)
        self.running = False
        self.stop = threading.Event()
        self.ui = threading.Thread(target=self.fake_ui, daemon=True)
        self.ui.start()

    def tearDown(self):
        self.stop.set()
        self.server.close()

    def fake_ui(self):
        """What App._drain_control does: take a request, answer on its reply queue."""
        while not self.stop.is_set():
            try:
                action, args, reply = self.server.requests.get(timeout=0.05)
            except queue.Empty:
                continue
            if action == "state":
                reply.put((200, {"running": self.running}))
            else:
                reply.put((200, {"did": action, "args": args}))

    def call(self, action, body=None, token=None, raw=None):
        url = f"http://127.0.0.1:{self.server.port}/{action}"
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
        req.add_header("X-HCV-Token", self.server.token if token is None else token)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.load(r)
        except urllib.error.HTTPError as e:
            return e.code, json.load(e)

    def test_token_is_written_and_removed(self):
        with open(self.token_file) as f:
            self.assertEqual(f.read(), self.server.token)
        self.server.close()
        self.assertFalse(os.path.exists(self.token_file))
        self.server = control.ControlServer(port=0, token_file=None)  # for tearDown

    def test_wrong_or_missing_token_is_refused(self):
        self.assertEqual(self.call("state", token="nope")[0], 403)
        self.assertEqual(self.call("state", token="")[0], 403)

    def test_unknown_action(self):
        code, body = self.call("format_disk")
        self.assertEqual(code, 404)
        self.assertIn("start", body["actions"])

    def test_body_must_be_a_json_object(self):
        self.assertEqual(self.call("select", raw=b"{nope")[0], 400)
        self.assertEqual(self.call("select", raw=b"[1, 2]")[0], 400)

    def test_requests_reach_the_ui_thread_with_their_arguments(self):
        self.assertEqual(self.call("select", {"part": "cpu"}), (200, {"did": "select", "args": {"part": "cpu"}}))
        self.assertEqual(self.call("state"), (200, {"running": False}))

    def test_wait_returns_when_the_test_ends(self):
        self.running = True
        threading.Timer(0.6, lambda: setattr(self, "running", False)).start()
        code, body = self.call("wait", {"timeout": 10})
        self.assertEqual(code, 200)
        self.assertTrue(body["finished"])

    def test_wait_times_out(self):
        self.running = True
        code, body = self.call("wait", {"timeout": 0.6})
        self.assertFalse(body["finished"])

    def test_listens_on_localhost_only(self):
        self.assertEqual(self.server.httpd.server_address[0], "127.0.0.1")


class EditorLinkTest(unittest.TestCase):
    def test_editor_link_allows_only_its_actions(self):
        server = control.ControlServer(port=0, token_file=None, actions=control.EDITOR_ACTIONS)
        try:
            def call(action):
                req = urllib.request.Request(f"http://127.0.0.1:{server.port}/{action}",
                                             headers={"X-HCV-Token": server.token})
                try:
                    with urllib.request.urlopen(req, timeout=5) as r:
                        return r.status, json.load(r)
                except urllib.error.HTTPError as e:
                    return e.code, json.load(e)
            code, body = call("hello")
            self.assertEqual(code, 200)
            self.assertEqual(body["version"], control.APP_VERSION)
            for forbidden in ("replay", "compare", "ai_task", "runs"):
                self.assertEqual(call(forbidden)[0], 404, forbidden)
        finally:
            server.close()

    def test_editor_actions_are_a_subset(self):
        self.assertLessEqual(control.EDITOR_ACTIONS - {"hello"}, control.ACTIONS)


if __name__ == "__main__":
    unittest.main()
