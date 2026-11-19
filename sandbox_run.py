"""Run one piece of agent-written Python under restrictions (SRS FR-9):

    python sandbox_run.py <workdir> <script>

Enforced with a Python audit hook (refused actions raise PermissionError in the
agent's code and print a "BLOCKED:" line on stderr, then the code carries on):
  - no network (any socket.* event)
  - no new processes / shell (subprocess, os.system/exec/spawn, CreateProcess)
  - writes, deletes and renames only inside <workdir>
  - reads only inside <workdir>, the Python install, and system program folders
    (so the agent can import numpy/torch but can't read your documents)

This is a best-effort guard against an AI's mistakes, not a security boundary
against deliberately hostile code (e.g. raw ctypes calls are not blocked,
because torch itself needs ctypes). The caller also applies a time limit and
runs it as the normal, non-admin user.
"""
import os
import sys

BLOCK_PROC = {"subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
              "os.startfile", "_winapi.CreateProcess"}
WRITE_EVENTS = {"os.remove", "os.rename", "os.mkdir", "os.rmdir", "os.chmod", "os.truncate",
                "os.link", "os.symlink", "os.utime", "shutil.rmtree", "shutil.copyfile",
                "shutil.move", "shutil.chown"}
LIST_EVENTS = {"os.listdir", "os.scandir"}
WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC


def _norm(p):
    return os.path.normcase(os.path.realpath(p))


def main():
    workdir = _norm(sys.argv[1])
    script = sys.argv[2]
    sys.dont_write_bytecode = True  # no .pyc writes into site-packages

    read_roots = {workdir, _norm(sys.prefix), _norm(sys.base_prefix), _norm(sys.exec_prefix)}
    for var in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if os.environ.get(var):
            read_roots.add(_norm(os.environ[var]))
    import site
    try:
        read_roots.add(_norm(site.getusersitepackages()))
    except Exception:
        pass
    read_roots = tuple(r.rstrip("\\/") + os.sep for r in read_roots)  # includes the workdir
    work_prefix = workdir.rstrip("\\/") + os.sep
    self_file = _norm(__file__)  # tracebacks read this file to print source lines

    def inside(path, roots):
        n = _norm(path)
        return n == workdir or n == self_file or (n + os.sep).startswith(roots)

    seen = set()
    stderr = sys.__stderr__

    def refuse(what, detail):
        key = (what, detail)
        if key not in seen:  # one line per distinct refusal
            seen.add(key)
            stderr.write(f"BLOCKED: {what}: {detail}\n")
            stderr.flush()
        raise PermissionError(f"sandbox: {what} not allowed ({detail})")

    def paths_of(args):
        out = []
        for a in args:
            if isinstance(a, (str, bytes, os.PathLike)):
                try:
                    out.append(os.fsdecode(a))
                except Exception:
                    pass
        return out

    def hook(event, args):
        if event.startswith("socket.") and event not in ("socket.__new__", "socket.gethostname"):
            refuse("network access", event)
        if event in BLOCK_PROC:
            refuse("starting a program", event)
        if event == "open":
            path, mode, flags = args
            if isinstance(path, int) or path is None:
                return
            write = (isinstance(mode, str) and any(c in mode for c in "wax+")) or \
                    (isinstance(flags, int) and flags & WRITE_FLAGS)
            p = os.fsdecode(path)
            if write and not inside(p, (work_prefix,)):
                refuse("write outside the working folder", p)
            if not write and not inside(p, read_roots):
                refuse("read outside the working folder", p)
        elif event in WRITE_EVENTS:
            for p in paths_of(args)[:2]:
                if not inside(p, (work_prefix,)):
                    refuse("change outside the working folder", p)
        elif event in LIST_EVENTS:
            ps = paths_of(args)
            if ps and not inside(ps[0], read_roots):
                refuse("listing outside the working folder", ps[0])

    os.chdir(workdir)
    with open(script, "r", encoding="utf-8") as f:
        source = f.read()
    code = compile(source, script, "exec")
    sys.path[0] = workdir  # was this folder; agent code must not import the app's own files
    sys.addaudithook(hook)  # installed last so our own setup isn't blocked
    exec(code, {"__name__": "__main__", "__file__": script})


if __name__ == "__main__":
    main()
