"""Unit tests for workloads.py: the 2D frame, the JSON output contract, and YOUR CODE
(run_usercode) - folder, Python choice, exit code, time limit, and a regression test for
the Windows launch bug fixed in 348ec68 (no folder given -> started with `sh -c`)."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from _setup import ROOT

import workloads


def run_workload(*args, env=None, timeout=60):
    """workloads.py as the app runs it; returns the parsed JSON lines."""
    r = subprocess.run([sys.executable, os.path.join(ROOT, "workloads.py"), *args], capture_output=True,
                       text=True, encoding="utf-8", errors="replace", timeout=timeout,
                       env=dict(os.environ, **(env or {})))
    return r.returncode, [json.loads(l) for l in r.stdout.splitlines() if l.strip()]


class FrameTest(unittest.TestCase):
    def test_mandelbrot_frame_is_deterministic(self):
        a = workloads._render_frame()
        self.assertEqual(a, workloads._render_frame())
        # every pixel does at least one iteration and at most MAXIT
        self.assertGreaterEqual(a, workloads.W * workloads.H)
        self.assertLessEqual(a, workloads.W * workloads.H * workloads.MAXIT)


class ContractTest(unittest.TestCase):
    def test_every_ui_script_has_a_runner(self):
        import main
        scripts = {s for t in main.TESTS.values() for s in t["targets"].values()} - {"agent"}
        self.assertLessEqual(scripts, set(workloads.RUNNERS))

    def test_ram_test_reports_bandwidth_lines(self):
        # 3 s: touching the 2 GB of copy buffers takes about the first second
        code, lines = run_workload("ram", "3")
        self.assertEqual(code, 0)
        rates = [l["gbps"] for l in lines if "gbps" in l]
        self.assertTrue(rates)
        self.assertTrue(all(r > 0 for r in rates))

    def test_failure_is_one_error_line_and_exit_2(self):
        code, lines = run_workload("disk", "1", os.path.join(tempfile.gettempdir(), "no", "such", "folder"))
        self.assertEqual(code, 2)
        self.assertIn("error", lines[-1])


class UserCodeTest(unittest.TestCase):
    def setUp(self):
        self.folder = os.path.realpath(tempfile.mkdtemp(prefix="hcv-usercode-"))
        self.addCleanup(shutil.rmtree, self.folder, True)

    def script(self, body, name="prog.py"):
        path = os.path.join(self.folder, name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def exit_text(self, lines):
        return [l["text"] for l in lines if l.get("ev") == "exit"][-1]

    def out(self, lines):
        return [l["text"] for l in lines if l.get("ev") == "out"]

    def test_plain_command_without_a_folder_starts(self):
        # regression (348ec68): on Windows this was started with `sh -c` and failed
        cmd = f'"{sys.executable}" -c "print(123)"'
        code, lines = run_workload("usercode", "30", cmd, env={"HCV_CWD": "", "HCV_PYTHON": ""})
        self.assertEqual(code, 0, lines)
        self.assertIn("123", self.out(lines))
        self.assertEqual(self.exit_text(lines), "program exited with code 0")

    def test_folder_from_the_app_is_the_working_directory(self):
        prog = self.script("import os\nprint(os.getcwd())\n")
        other = os.path.realpath(tempfile.mkdtemp(prefix="hcv-cwd-"))
        self.addCleanup(shutil.rmtree, other, True)
        _, lines = run_workload("usercode", "30", f'"{prog}"', env={"HCV_CWD": other})
        self.assertEqual(os.path.normcase(self.out(lines)[0]), os.path.normcase(other))

    def test_without_a_folder_a_script_runs_in_its_own_folder(self):
        prog = self.script("import os\nprint(os.getcwd())\n")
        _, lines = run_workload("usercode", "30", f'"{prog}"', env={"HCV_CWD": ""})
        self.assertEqual(os.path.normcase(self.out(lines)[0]), os.path.normcase(self.folder))

    def test_py_file_runs_with_the_python_the_app_chose(self):
        prog = self.script("import sys\nprint(sys.executable)\n")
        _, lines = run_workload("usercode", "30", f'"{prog}"', env={"HCV_PYTHON": sys.executable})
        self.assertEqual(os.path.normcase(self.out(lines)[0]), os.path.normcase(sys.executable))

    def test_non_ascii_output_arrives_intact(self):
        # regression: found in acceptance test AT-9 - "·" showed as "Â·" in the log
        prog = self.script("print('360000 orders \\u00b7 1.9 s \\u00e9')\n")
        _, lines = run_workload("usercode", "30", f'"{prog}"')
        self.assertIn("360000 orders \u00b7 1.9 s \u00e9", self.out(lines))

    def test_exit_code_is_reported(self):
        prog = self.script("import sys\nsys.exit(3)\n")
        _, lines = run_workload("usercode", "30", f'"{prog}"')
        self.assertEqual(self.exit_text(lines), "program exited with code 3")

    def test_heartbeat_lines_while_running(self):
        prog = self.script("import time\ntime.sleep(1)\n")
        _, lines = run_workload("usercode", "30", f'"{prog}"')
        self.assertGreaterEqual(len([l for l in lines if "run_s" in l]), 3)

    def test_time_limit_stops_the_program_and_its_children(self):
        child = self.script("import time\ntime.sleep(60)\n", "child.py")
        prog = self.script(f"import subprocess, sys, time\nsubprocess.Popen([sys.executable, {child!r}])\n"
                           "print('started', flush=True)\ntime.sleep(60)\n")
        code, lines = run_workload("usercode", "2", f'"{prog}"', timeout=30)
        self.assertEqual(code, 0)
        self.assertEqual(self.exit_text(lines), "stopped at the 2 s time limit")
        import psutil
        left = [p for p in psutil.process_iter(["cmdline"]) if child in " ".join(p.info["cmdline"] or [])]
        self.assertEqual(left, [], "the program's child process was left running")

    def test_program_that_cannot_start_is_an_error(self):
        code, lines = run_workload("usercode", "5", os.path.join(self.folder, "missing.exe"))
        self.assertEqual(code, 2)
        self.assertIn("can't start", lines[-1]["error"])
        # regression (acceptance test AT-7): the path showed with doubled backslashes (repr)
        self.assertIn(os.path.join(self.folder, "missing.exe"), lines[-1]["error"])


if __name__ == "__main__":
    unittest.main()
