"""Draws the repository's branch / merge history (like GitHub's Network page) from git itself.

    python docs/presentation/git_graph.py      # writes git_graph.mmd; render with docs/render_diagrams.py

Walks main's first-parent line; each merge commit "Merge branch 'X'" becomes branch X with its own
commits, merged back into main.
"""
import os
import re
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))


def git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True, check=True).stdout


def main():
    lines = git("rev-list", "--first-parent", "--reverse", "--parents", "main").split("\n")
    out = ["%%{init: {'gitGraph': {'showCommitLabel': true, 'rotateCommitLabel': false}} }%%", "gitGraph"]
    for row in filter(None, lines):
        parts = row.split()
        c, parents = parts[0], parts[1:]
        if len(parents) < 2:
            out.append(f'  commit id: "{c[:7]}"')
            continue
        branch = re.search(r"Merge branch '([^']+)'", git("log", "-1", "--format=%s", c))
        name = branch.group(1) if branch else "feature"
        side = git("rev-list", "--reverse", f"{parents[0]}..{parents[1]}").split()
        out.append(f"  branch {name}")
        out.append(f"  checkout {name}")
        for sc in side:
            out.append(f'  commit id: "{sc[:7]}"')
        out.append("  checkout main")
        out.append(f'  merge {name} id: "{c[:7]}"')
    path = os.path.join(HERE, "git_graph.mmd")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(out) + "\n")
    print(f"wrote {os.path.relpath(path, ROOT)} ({len(out)} lines)")


if __name__ == "__main__":
    main()
