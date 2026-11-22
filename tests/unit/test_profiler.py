"""Unit tests for profiler.py helpers: finding a script's project folder, deciding when it must
run as `python -m`, and keeping GPU readings out of programs that never used the GPU."""
import os
import shutil
import tempfile
import unittest

import _setup  # noqa: F401

import profiler


class ProjectTest(unittest.TestCase):
    def setUp(self):
        self.base = os.path.realpath(tempfile.mkdtemp(prefix="hcv-prof-"))
        self.addCleanup(shutil.rmtree, self.base, True)

    def make(self, rel, text=""):
        path = os.path.join(self.base, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return path

    def test_root_is_the_nearest_folder_with_a_marker(self):
        self.make("proj/requirements.txt")
        script = self.make("proj/a/b/run.py")
        self.assertEqual(profiler.project_root(script), os.path.join(self.base, "proj"))

    def test_without_a_marker_the_script_folder(self):
        script = self.make("loose/run.py")
        self.assertEqual(profiler.project_root(script), os.path.join(self.base, "loose"))

    def test_module_name_for_relative_imports(self):
        self.make("proj/pkg/__init__.py")
        self.make("proj/pkg/sub/__init__.py")
        script = self.make("proj/pkg/sub/mod.py", "from . import x\n")
        self.assertEqual(profiler.module_name(script), ("pkg.sub.mod", os.path.join(self.base, "proj")))

    def test_package_main(self):
        self.make("proj/tool/__init__.py")
        script = self.make("proj/tool/__main__.py", "print(1)\n")
        self.assertEqual(profiler.module_name(script)[0], "tool")

    def test_plain_script_in_a_package_runs_as_a_file(self):
        self.make("proj/pkg/__init__.py")
        script = self.make("proj/pkg/plain.py", "import os\n")
        self.assertEqual(profiler.module_name(script), (None, None))

    def test_folder_names_that_are_not_identifiers(self):
        self.make("proj/my-pkg/__init__.py")
        script = self.make("proj/my-pkg/m.py", "from . import x\n")
        self.assertEqual(profiler.module_name(script), (None, None))


class SampleHelpersTest(unittest.TestCase):
    def test_num_excludes_bools(self):
        self.assertTrue(profiler.num(1.5))
        self.assertTrue(profiler.num(0))
        self.assertFalse(profiler.num(True))
        self.assertFalse(profiler.num(None))

    def test_without_gpu_removes_gpu_and_pcie_readings(self):
        rows = [{"t": 0, "cpu_util": 50, "gpu_util": 99, "pcie_rx": 1000, "board_util": 80}]
        self.assertEqual(profiler._without_gpu(rows), [{"t": 0, "cpu_util": 50}])


if __name__ == "__main__":
    unittest.main()
