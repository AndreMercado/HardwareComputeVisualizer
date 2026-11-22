"""Builds the submission archive from the committed repository.

    python docs/make_submission.py [output.zip]       # default: ../HCV_Group19_Final.zip

Contents: the whole project tree at HEAD (git archive: source, unit tests, docs, diagrams,
API docs, acceptance screenshots, the presentation), plus a DELIVERABLES.md index at the top.
Files git ignores (runs, caches, the other teams' PDFs, build output) are never included.
"""
import io
import os
import subprocess
import sys
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFIX = "HardwareComputeVisualizer/"


def main():
    out = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "..", "HCV_Group19_Final.zip"))
    dirty = subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=ROOT,
                           capture_output=True, text=True).stdout.strip()
    if dirty:
        print("warning: uncommitted changes are NOT in the archive:\n" + dirty)
    head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    data = subprocess.run(["git", "archive", "--format=zip", f"--prefix={PREFIX}", "HEAD"], cwd=ROOT,
                          capture_output=True, check=True).stdout
    with open(os.path.join(ROOT, "docs", "README.md"), encoding="utf-8") as f:
        index = f.read()
    index = (f"# Hardware Compute Visualizer - CS 4398 Group 19 - final submission (commit {head})\n\n"
             "Everything is inside `HardwareComputeVisualizer/`; paths below are relative to its `docs/` folder.\n\n"
             + index)
    with zipfile.ZipFile(io.BytesIO(data)) as src, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        dst.writestr("DELIVERABLES.md", index)
        for item in src.infolist():
            dst.writestr(item, src.read(item))
        names = src.namelist()
    print(f"wrote {out} ({os.path.getsize(out) / 1e6:.1f} MB, {len(names)} entries, commit {head})")


if __name__ == "__main__":
    main()
