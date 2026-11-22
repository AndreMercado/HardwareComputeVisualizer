"""Unit tests for agent.py (AI Benchmark Mode, SRS UC-7, FR-7..9) and sandbox_run.py (FR-9).

Ollama is replaced by a scripted fake `chat`, so the agent's control loop is tested
without a model: finish, nudges, step limit, blocked paths, tool errors."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from _setup import ROOT

import agent


def tool_call(name, **args):
    return {"function": {"name": name, "arguments": args}}


class SafePathTest(unittest.TestCase):
    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="hcv-agent-"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_paths_inside_the_working_folder_are_allowed(self):
        self.assertEqual(agent.safe_path(self.work, "a/b.txt"), os.path.join(self.work, "a", "b.txt"))
        self.assertEqual(agent.safe_path(self.work, "."), self.work)

    def test_escaping_the_working_folder_is_blocked(self):
        for rel in ("../x.txt", "..", os.path.join("a", "..", "..", "x"), os.path.abspath(os.sep)):
            with self.subTest(rel=rel):
                with self.assertRaises(agent.Blocked):
                    agent.safe_path(self.work, rel)

    def test_a_sibling_folder_with_the_same_prefix_is_blocked(self):
        with self.assertRaises(agent.Blocked):
            agent.safe_path(self.work, "../" + os.path.basename(self.work) + "-evil/x")


class ToolsTest(unittest.TestCase):
    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="hcv-agent-"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def test_write_read_and_list_files(self):
        self.assertEqual(agent.call_tool(self.work, 1, "write_file", {"path": "sub/n.txt", "content": "hi"}, 0),
                         "wrote sub/n.txt")
        self.assertEqual(agent.call_tool(self.work, 1, "read_file", {"path": "sub/n.txt"}, 0), "hi")
        self.assertEqual(agent.call_tool(self.work, 1, "list_files", {}, 0), "sub")

    def test_list_hides_dot_files_and_says_empty(self):
        os.makedirs(os.path.join(self.work, ".tmp"))
        self.assertEqual(agent.call_tool(self.work, 1, "list_files", {}, 0), "(empty)")

    def test_unknown_tool(self):
        self.assertEqual(agent.call_tool(self.work, 1, "rm_rf", {}, 0), "unknown tool: rm_rf")

    def test_write_outside_is_blocked(self):
        with self.assertRaises(agent.Blocked):
            agent.call_tool(self.work, 1, "write_file", {"path": "../evil.txt", "content": "x"}, 0)

    def test_run_python_returns_exit_code_and_output(self):
        import time
        out = agent.run_python(self.work, 1, "print(6 * 7)", time.time() + 60)
        self.assertIn("exit code 0", out)
        self.assertIn("42", out)
        self.assertTrue(os.path.isfile(os.path.join(self.work, "step_1.py")))

    def test_run_python_hints_when_nothing_is_printed(self):
        import time
        self.assertIn("printed nothing", agent.run_python(self.work, 2, "x = 1", time.time() + 60))

    def test_tail_keeps_the_end(self):
        self.assertEqual(agent.tail("abc", 5), "abc")
        self.assertTrue(agent.tail("x" * 100 + "END", 10).endswith("END"))
        self.assertTrue(agent.tail("x" * 100, 10).startswith("...(cut)..."))


class PickModelTest(unittest.TestCase):
    def test_preferred_model_wins(self):
        self.assertEqual(agent.pick_model(["tiny:1b", "qwen2.5:7b", "llama3.2:3b"]), "llama3.2:3b")

    def test_otherwise_the_smallest_model_that_can_call_tools(self):
        caps = {"tiny:1b": {"completion"}, "small:2b": {"completion", "tools"}, "big:9b": {"tools"}}
        with mock.patch.object(agent, "capabilities", side_effect=lambda m: caps[m]):
            self.assertEqual(agent.pick_model(["tiny:1b", "small:2b", "big:9b"]), "small:2b")

    def test_none_when_no_model_can_call_tools(self):
        with mock.patch.object(agent, "capabilities", return_value={"completion"}):
            self.assertIsNone(agent.pick_model(["tiny:1b"]))
        self.assertIsNone(agent.pick_model([]))

    def test_ollama_down_gives_none(self):
        with mock.patch.object(agent.urllib.request, "urlopen", side_effect=OSError("refused")):
            self.assertIsNone(agent.ollama_models())
            self.assertEqual(agent.capabilities("x"), set())


class AgentLoopTest(unittest.TestCase):
    """agent.main() with a scripted model."""

    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="hcv-agent-"))
        self.addCleanup(shutil.rmtree, self.work, True)

    def run_agent(self, replies, seconds=60):
        replies = list(replies)
        sent = []

        def fake_chat(model, messages, timeout, think=None):
            sent.append([dict(m) for m in messages])
            return replies.pop(0) if replies else {"content": "..."}

        out = io.StringIO()
        with mock.patch.object(agent, "chat", fake_chat), \
                mock.patch.object(agent, "capabilities", return_value={"tools"}), \
                mock.patch.object(agent, "unload") as unload, \
                mock.patch.object(sys, "argv", ["agent.py", self.work, "m:1b", "render", str(seconds)]), \
                contextlib.redirect_stdout(out):
            agent.main()
        unload.assert_called_once_with("m:1b")  # the model is always unloaded afterwards
        events = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
        return events, sent

    def test_finish_ends_the_run_as_done(self):
        events, _ = self.run_agent([
            {"content": "writing", "tool_calls": [tool_call("write_file", path="a.txt", content="1")]},
            {"tool_calls": [tool_call("finish", summary="Rendered in 3 s.")]},
        ])
        evs = [e["ev"] for e in events]
        self.assertEqual(evs[0], "say")
        self.assertIn("tool", evs)
        self.assertEqual(events[-1], {"ev": "done", "text": "Rendered in 3 s."})
        self.assertTrue(os.path.isfile(os.path.join(self.work, "a.txt")))

    def test_replies_without_tools_are_nudged_then_stopped(self):
        events, sent = self.run_agent([{"content": "thinking"}] * (agent.MAX_NUDGES + 1))
        self.assertEqual(events[-1]["ev"], "stopped")
        self.assertIn("without finishing", events[-1]["text"])
        self.assertIn("You replied without calling a tool", sent[1][-1]["content"])

    def test_a_tool_call_resets_the_nudge_count(self):
        idle = {"content": "hmm"}
        work = {"tool_calls": [tool_call("list_files")]}
        script = [idle] * agent.MAX_NUDGES + [work] + [idle] * agent.MAX_NUDGES + \
                 [{"tool_calls": [tool_call("finish", summary="ok")]}]
        events, _ = self.run_agent(script)
        self.assertEqual(events[-1]["ev"], "done")

    def test_blocked_path_is_reported_and_the_run_continues(self):
        events, sent = self.run_agent([
            {"tool_calls": [tool_call("read_file", path="../../secret.txt")]},
            {"tool_calls": [tool_call("finish", summary="done")]},
        ])
        self.assertIn("blocked", [e["ev"] for e in events])
        self.assertTrue(sent[1][-1]["content"].startswith("BLOCKED:"))
        self.assertEqual(events[-1]["ev"], "done")

    def test_tool_errors_go_back_to_the_model(self):
        events, sent = self.run_agent([
            {"tool_calls": [tool_call("read_file", path="missing.txt")]},
            {"tool_calls": [tool_call("finish", summary="done")]},
        ])
        self.assertIn("tool error: FileNotFoundError", sent[1][-1]["content"])

    def test_arguments_given_as_json_text_are_parsed(self):
        call = {"function": {"name": "write_file", "arguments": json.dumps({"path": "j.txt", "content": "x"})}}
        self.run_agent([{"tool_calls": [call]}, {"tool_calls": [tool_call("finish", summary="ok")]}])
        self.assertTrue(os.path.isfile(os.path.join(self.work, "j.txt")))

    def test_step_limit(self):
        events, _ = self.run_agent([{"tool_calls": [tool_call("list_files")]}] * (agent.MAX_STEPS + 5))
        self.assertEqual(events[-1], {"ev": "stopped", "text": f"step limit reached ({agent.MAX_STEPS} steps)"})

    def test_time_limit(self):
        events, _ = self.run_agent([], seconds=0)
        self.assertEqual(events[-1]["ev"], "timeout")


class SandboxTest(unittest.TestCase):
    """sandbox_run.py runs agent code with no network, no programs and files only in its folder."""

    def setUp(self):
        self.work = os.path.realpath(tempfile.mkdtemp(prefix="hcv-sbx-"))
        self.outside = os.path.realpath(tempfile.mkdtemp(prefix="hcv-outside-"))
        self.addCleanup(shutil.rmtree, self.work, True)
        self.addCleanup(shutil.rmtree, self.outside, True)

    def run_code(self, code):
        script = os.path.join(self.work, "step.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(code)
        return subprocess.run([sys.executable, os.path.join(ROOT, "sandbox_run.py"), self.work, script],
                              capture_output=True, text=True, timeout=60, cwd=self.work)

    def assert_blocked(self, code, what):
        r = self.run_code("try:\n" + "".join("    " + l + "\n" for l in code.splitlines()) +
                          "    print('NOT BLOCKED')\nexcept PermissionError as e:\n    print('refused', e)\n")
        self.assertIn("refused", r.stdout, r.stdout + r.stderr)
        self.assertNotIn("NOT BLOCKED", r.stdout)
        self.assertIn(f"BLOCKED: {what}", r.stderr)

    def test_files_in_the_working_folder_are_allowed(self):
        r = self.run_code("open('a.txt','w').write('x')\nimport os\nos.mkdir('d')\nprint(open('a.txt').read(), os.listdir('.'))")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("x", r.stdout)

    def test_numpy_style_imports_still_work(self):
        r = self.run_code("import json, math, collections\nprint(math.sqrt(16))")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("4.0", r.stdout)

    def test_network_is_blocked(self):
        self.assert_blocked("import socket\nsocket.create_connection(('127.0.0.1', 9), timeout=1)", "network access")

    def test_starting_programs_is_blocked(self):
        self.assert_blocked("import subprocess\nsubprocess.run(['cmd', '/c', 'echo hi'])", "starting a program")
        self.assert_blocked("import os\nos.system('echo hi')", "starting a program")

    def test_writing_outside_is_blocked(self):
        target = os.path.join(self.outside, "evil.txt").replace("\\", "\\\\")
        self.assert_blocked(f"open('{target}', 'w').write('x')", "write outside the working folder")
        self.assertFalse(os.path.exists(os.path.join(self.outside, "evil.txt")))

    def test_reading_outside_is_blocked(self):
        secret = os.path.join(self.outside, "secret.txt")
        with open(secret, "w") as f:
            f.write("s3cret")
        self.assert_blocked(f"print(open({secret!r}).read())", "read outside the working folder")

    def test_deleting_outside_is_blocked(self):
        victim = os.path.join(self.outside, "keep.txt")
        open(victim, "w").close()
        self.assert_blocked(f"import os\nos.remove({victim!r})", "change outside the working folder")
        self.assertTrue(os.path.exists(victim))

    def test_listing_outside_is_blocked(self):
        self.assert_blocked(f"import os\nos.listdir({self.outside!r})", "listing outside the working folder")


if __name__ == "__main__":
    unittest.main()
