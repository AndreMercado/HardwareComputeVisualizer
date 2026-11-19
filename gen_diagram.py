"""Generate CLASS_DIAGRAM.md (Mermaid classDiagram) from the project's .py files.

Reads source with `ast` only (nothing is imported or run, no dependencies).

    python gen_diagram.py          # regenerate once
    python gen_diagram.py --watch  # regenerate whenever a .py file changes
    python gen_diagram.py --check  # exit 1 if CLASS_DIAGRAM.md is stale (CI / hooks)

Shown: classes (attributes, methods, inheritance, "uses" arrows when one class
instantiates another), plus each module as a <<module>> box holding its
top-level functions, with dashed arrows for imports between project modules.
"""

import ast
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(ROOT, "CLASS_DIAGRAM.md")
SKIP_DIRS = {"__pycache__", "venv", ".venv", "env", ".git", "ai_workspace", "node_modules"}
SKIP_FILES = {os.path.basename(__file__)}


def project_files():
    found = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
        for f in sorted(files):
            if f.endswith(".py") and f not in SKIP_FILES:
                found.append(os.path.join(dirpath, f))
    return found


def vis(name):
    if name.startswith("__") and name.endswith("__"):
        return "+"
    return "-" if name.startswith("_") else "+"


def arg_names(fn):
    a = fn.args
    names = [x.arg for x in a.posonlyargs + a.args]
    if names and names[0] in ("self", "cls"):
        names = names[1:]
    if a.vararg:
        names.append("args")
    names += [x.arg for x in a.kwonlyargs]
    if a.kwarg:
        names.append("kwargs")
    return names


def sig(fn):
    # Mermaid member lines break on some punctuation, so keep names only.
    return "%s%s(%s)" % (vis(fn.name), fn.name, ", ".join(arg_names(fn)))


def called_names(node):
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                yield f.id
            elif isinstance(f, ast.Attribute):
                yield f.attr


def base_name(b):
    if isinstance(b, ast.Name):
        return b.id
    if isinstance(b, ast.Attribute):
        return b.attr
    return None


def parse(path):
    mod = os.path.splitext(os.path.basename(path))[0]
    with open(path, encoding="utf-8") as f:
        tree = ast.parse(f.read(), path)
    info = {"module": mod, "functions": [], "classes": [], "imports": set()}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["functions"].append(sig(node))
        elif isinstance(node, ast.ClassDef):
            info["classes"].append(parse_class(node))
        elif isinstance(node, ast.Import):
            info["imports"].update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            info["imports"].add(node.module.split(".")[0])
    return info


def parse_class(node):
    attrs, methods = [], []
    seen = set()

    def add_attr(name):
        if name not in seen:
            seen.add(name)
            attrs.append("%s%s" % (vis(name), name))

    for item in node.body:
        if isinstance(item, ast.Assign):
            for t in item.targets:
                if isinstance(t, ast.Name):
                    add_attr(t.id)
        elif isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
            add_attr(item.target.id)
        elif isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            methods.append(sig(item))
            for n in ast.walk(item):
                targets = []
                if isinstance(n, ast.Assign):
                    targets = n.targets
                elif isinstance(n, (ast.AnnAssign, ast.AugAssign)):
                    targets = [n.target]
                for t in targets:
                    if (isinstance(t, ast.Attribute) and isinstance(t.value, ast.Name)
                            and t.value.id == "self"):
                        add_attr(t.attr)
    return {
        "name": node.name,
        "bases": [b for b in map(base_name, node.bases) if b],
        "attrs": attrs,
        "methods": methods,
        "calls": set(called_names(node)),
    }


def build():
    infos = [parse(p) for p in project_files()]
    modules = {i["module"] for i in infos}
    class_names = {c["name"] for i in infos for c in i["classes"]}
    lines = ["classDiagram", "    direction LR"]
    rels = []

    for i in infos:
        mod = i["module"]
        if i["functions"]:
            lines.append('    class %s["%s.py"] {' % (mod, mod))
            lines.append("        <<module>>")
            lines += ["        " + f for f in i["functions"]]
            lines.append("    }")
        elif not i["classes"]:
            lines.append('    class %s["%s.py"] {\n        <<module>>\n    }' % (mod, mod))
        for c in i["classes"]:
            lines.append("    class %s {" % c["name"])
            lines += ["        " + a for a in c["attrs"]]
            lines += ["        " + m for m in c["methods"]]
            lines.append("    }")
            for b in c["bases"]:
                if b in class_names:
                    rels.append("    %s <|-- %s" % (b, c["name"]))
            for used in sorted(c["calls"] & class_names - {c["name"]}):
                rels.append("    %s ..> %s : uses" % (c["name"], used))
        for dep in sorted(i["imports"] & modules - {mod}):
            rels.append("    %s ..> %s : imports" % (mod, dep))

    return "\n".join(lines + sorted(set(rels)))


def render():
    return (
        "# Class diagram\n\n"
        "<!-- AUTO-GENERATED by gen_diagram.py - do not edit by hand. -->\n\n"
        "```mermaid\n%s\n```\n" % build()
    )


def write(quiet=False):
    text = render()
    old = None
    if os.path.exists(OUT):
        with open(OUT, encoding="utf-8") as f:
            old = f.read()
    if text == old:
        return False
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    if not quiet:
        print("updated", os.path.relpath(OUT))
    return True


def watch():
    def stamp():
        return {p: os.stat(p).st_mtime for p in project_files()}

    last = None
    print("watching for .py changes (Ctrl+C to stop)")
    while True:
        now = stamp()
        if now != last:
            last = now
            try:
                write()
            except SyntaxError as e:
                print("skipped, syntax error:", e)
        time.sleep(1)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        try:
            watch()
        except KeyboardInterrupt:
            pass
    elif "--check" in sys.argv:
        current = open(OUT, encoding="utf-8").read() if os.path.exists(OUT) else ""
        stale = current != render()
        print("stale" if stale else "up to date")
        sys.exit(1 if stale else 0)
    else:
        if not write():
            print("already up to date")
