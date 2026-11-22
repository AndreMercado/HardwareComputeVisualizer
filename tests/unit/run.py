"""Runs the unit tests (tests/unit/test_*.py).

    python tests/unit/run.py              # all unit tests
    python tests/unit/run.py --coverage   # also measure statement + branch coverage (pip install coverage)

Exit code 0 = all passed."""
import os
import subprocess
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def main():
    if "--coverage" in sys.argv:
        rc = subprocess.call([sys.executable, "-m", "coverage", "run", "--rcfile", os.path.join(ROOT, ".coveragerc"),
                              os.path.abspath(__file__)], cwd=ROOT)
        subprocess.call([sys.executable, "-m", "coverage", "combine", "-q"], cwd=ROOT)
        subprocess.call([sys.executable, "-m", "coverage", "report"], cwd=ROOT)
        subprocess.call([sys.executable, "-m", "coverage", "html", "-q", "-d", os.path.join("tests", "work", "coverage")],
                        cwd=ROOT)
        print("HTML report: tests/work/coverage/index.html")
        return rc
    sys.path.insert(0, HERE)
    suite = unittest.defaultTestLoader.discover(HERE, pattern="test_*.py", top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
