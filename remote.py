"""Remote target over SSH (SRS UC-2).

Uses Windows' own OpenSSH client (ssh.exe) with key authentication only
(BatchMode: ssh never asks for a password, FR-20). Nothing about the login
is stored except the host, user, port and key-file *path* the user typed.

connect() copies remote_agent.py + workloads.py + nvgpu.py + pynvml.py into a
fresh temp folder on the machine, starts the agent there and waits for its
hardware report. After that:
  - RemotePoller stands in for telemetry.Poller (latest / sources / info),
  - RemoteProc stands in for the local workload process (stdout / poll / terminate),
so main.py drives a remote test exactly like a local one.
The app pings the agent every second; the agent stops its workload and exits
after 10 s without contact (FR-22) and deletes its folder when it exits (FR-23).
"""
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
AGENT_FILES = ("remote_agent.py", "workloads.py", "nvgpu.py")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
DIR_RE = re.compile(r"^/[\w./-]*/hcv-agent-\w+$")
MIN_PYTHON = (3, 8)
STALE_S = 2.0  # remote telemetry older than this counts as lost

# Run on the remote machine: check python3, unpack the files sent on stdin into a new
# private temp folder (mkdtemp is mode 700), print where it is and the Python version.
_UNPACK = ("command -v python3 >/dev/null 2>&1 || { echo HCV_NO_PYTHON; exit 3; }; "
           "exec python3 -c \"import sys,json,os,tempfile;d=tempfile.mkdtemp(prefix='hcv-agent-');"
           "[open(os.path.join(d,n),'w').write(s) for n,s in json.load(sys.stdin).items()];"
           "print('HCV_DIR',d,'%d.%d'%sys.version_info[:2])\"")


class RemoteError(Exception):
    pass


def ssh_runner(host, user, key, port=22):
    """argv builder for one command on host over SSH (key login only)."""
    base = ["ssh", "-T", "-o", "BatchMode=yes", "-o", "IdentitiesOnly=yes", "-o", "ConnectTimeout=10",
            "-o", "ServerAliveInterval=3", "-o", "ServerAliveCountMax=3",
            "-o", "StrictHostKeyChecking=accept-new", "-p", str(int(port)), "-i", key, f"{user}@{host}"]
    return lambda remote_cmd: base + [remote_cmd]


def _bundle():
    files = {}
    for n in AGENT_FILES:
        with open(os.path.join(HERE, n), encoding="utf-8") as f:
            files[n] = f.read()
    try:  # NVML bindings: one pure-Python file, so the remote machine needs no pip install
        import pynvml
        with open(pynvml.__file__, encoding="utf-8") as f:
            files["pynvml.py"] = f.read()
    except (ImportError, OSError):
        pass
    return json.dumps(files)


def _ssh_error(stderr, host, port, key):
    text = (stderr or "").strip()
    low = text.lower()
    if "permission denied" in low:
        return f"Login failed: {host} didn't accept the key {key}."
    if "could not resolve hostname" in low or "name or service not known" in low:
        return f"Can't find {host} (the name doesn't resolve)."
    if "timed out" in low:
        return f"No answer from {host} on port {port} (connection timed out)."
    if "connection refused" in low:
        return f"{host} refused the connection on port {port} - is the SSH server running?"
    if "host key verification failed" in low or "identification has changed" in low:
        return (f"{host}'s host key is not the one seen before. If the machine was rebuilt, remove its "
                f"line from ~/.ssh/known_hosts; otherwise this may not be your machine.")
    lines = [l for l in text.splitlines() if l.strip()]
    return f"SSH failed: {lines[-1] if lines else 'no error message'}"


class RemoteProc:
    """The workload running on the remote machine, shaped like subprocess.Popen."""
    pid = None

    def __init__(self, target):
        self.target = target
        self.q = queue.Queue()
        self.returncode = None

    @property
    def stdout(self):
        while True:
            line = self.q.get()
            if line is None:
                return
            yield line

    def poll(self):
        return self.returncode

    def _finish(self, code):
        if self.returncode is None:
            self.returncode = code
            self.q.put(None)

    def terminate(self, timeout=5.0):
        """Ask the agent to stop the workload; True if it didn't confirm in time."""
        if self.returncode is not None:
            return False
        self.target.send(cmd="stop")
        end = time.monotonic() + timeout
        while self.returncode is None and time.monotonic() < end and self.target.alive:
            time.sleep(0.05)
        forced = self.returncode is None
        self._finish(-9)
        return forced


class RemotePoller:
    """Stands in for telemetry.Poller: the agent samples, this only hands it on."""

    def __init__(self, target, gpu_index, disk):
        self.target, self.gpu_index, self.disk = target, gpu_index, disk
        self.t_watch = time.monotonic()

    def start(self):
        self.t_watch = time.monotonic()
        self.target.latest = {}
        self.target.send(cmd="watch", gpu=self.gpu_index, disk=self.disk)

    def stop(self):
        self.target.send(cmd="unwatch")

    @property
    def sources(self):
        return self.target.sources

    @property
    def info(self):
        return self.target.info

    @property
    def latest(self):
        s, now = self.target.latest, time.monotonic()
        if s and now - s["t"] <= STALE_S:
            return s
        if not s and now - self.t_watch < 4.0:
            return {}  # first samples still on their way
        return {"t": now}  # silent: every metric unavailable (UC-12 / UC-2 alt c)


class RemoteTarget:
    def __init__(self, host, user, key, port=22, runner=None):
        self.host, self.user, self.key, self.port = host, user, key, port
        self.label = f"{user}@{host}" if user else host
        self.uses_ssh = runner is None
        self.run = runner or ssh_runner(host, user, key, port)
        self.dir = None
        self.agent = None
        self.alive = False
        self.hw = None
        self.latest, self.info, self.sources = {}, {}, {}
        self.proc = None
        self.lost_reason = None
        self._hello = queue.Queue()
        self._wlock = threading.Lock()

    # ---------------------------------------------------------------- connect / close
    def connect(self):
        """Blocking. Returns the remote hardware report; raises RemoteError with a
        message for the user."""
        if self.uses_ssh and not shutil.which("ssh"):
            raise RemoteError("Windows' OpenSSH client (ssh.exe) isn't installed. Add it under "
                              "Settings > System > Optional features > OpenSSH Client.")
        if self.key and not os.path.isfile(self.key):
            raise RemoteError(f"Key file not found: {self.key}")
        try:
            r = subprocess.run(self.run(_UNPACK), input=_bundle(), capture_output=True, text=True,
                               timeout=45, creationflags=NO_WINDOW)
        except subprocess.TimeoutExpired:
            raise RemoteError(f"{self.host} didn't finish the setup within 45 s.")
        except OSError as e:
            raise RemoteError(f"Can't run ssh: {e}")
        if "HCV_NO_PYTHON" in r.stdout:
            raise RemoteError(f"Python 3 isn't installed on {self.host} (needed: python3 {MIN_PYTHON[0]}."
                              f"{MIN_PYTHON[1]} or newer).")
        m = re.search(r"HCV_DIR (\S+) (\d+)\.(\d+)", r.stdout)
        if r.returncode != 0 or not m:
            raise RemoteError(_ssh_error(r.stderr, self.host, self.port, self.key) if r.returncode == 255
                              else f"Setting up the agent failed: {(r.stderr or r.stdout).strip()[-300:]}")
        if not DIR_RE.match(m.group(1)):
            raise RemoteError(f"Unexpected agent folder on {self.host}: {m.group(1)}")
        self.dir = m.group(1)
        if (int(m.group(2)), int(m.group(3))) < MIN_PYTHON:
            self._remove_dir()
            raise RemoteError(f"{self.host} has Python {m.group(2)}.{m.group(3)}; the agent needs "
                              f"{MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer.")
        try:
            self.agent = subprocess.Popen(
                self.run(f"cd '{self.dir}' && exec python3 -u remote_agent.py"),
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                bufsize=1, creationflags=NO_WINDOW)
        except OSError as e:
            self._remove_dir()
            raise RemoteError(f"Can't start the agent: {e}")
        self.alive = True
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.hw = self._hello.get(timeout=25)
        except queue.Empty:
            self.hw = None
        if not self.hw:
            err = self.agent.stderr.read() if self.agent.poll() is not None else ""
            self.close()
            raise RemoteError(f"The agent on {self.host} didn't report its hardware. "
                              f"{err.strip()[-300:]}".strip())
        threading.Thread(target=self._ping, daemon=True).start()
        return self.hw

    def close(self):
        """Stop the agent (it deletes its own folder) and remove the folder again
        in case it couldn't (FR-23). Doesn't wait for the cleanup."""
        was_alive, self.alive = self.alive, False
        if self.proc:
            self.proc._finish(-9)
        if self.agent:
            if was_alive:
                self.send(cmd="quit", force=True)
            try:
                self.agent.stdin.close()
            except OSError:
                pass
            try:
                self.agent.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.agent.kill()
        self._remove_dir(wait=False)

    def _remove_dir(self, wait=True):
        if not self.dir or not DIR_RE.match(self.dir):
            return
        try:
            p = subprocess.Popen(self.run(f"rm -rf -- '{self.dir}'"), stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
            if wait:
                p.wait(timeout=20)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.dir = None

    # ---------------------------------------------------------------- messages
    def send(self, force=False, **msg):
        if not (self.alive or force) or not self.agent:
            return False
        with self._wlock:
            try:
                self.agent.stdin.write(json.dumps(msg) + "\n")
                self.agent.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def _ping(self):
        while self.alive:
            self.send(cmd="ping")
            time.sleep(1.0)

    def _read(self):
        for line in self.agent.stdout:
            try:
                m = json.loads(line)
            except ValueError:
                continue
            if "tel" in m:
                self.latest = dict(m["tel"], t=time.monotonic())
                self.info = m.get("info") or {}
            elif "wl" in m:
                if self.proc:
                    self.proc.q.put(m["wl"])
            elif "ended" in m:
                if self.proc:
                    self.proc._finish(m["ended"])
            elif "hello" in m:
                self._hello.put(m["hello"])
            elif "sources" in m:
                self.sources = m["sources"]
        # the SSH session ended
        if self.alive:
            self.lost_reason = f"Connection to {self.host} lost."
        self.alive = False
        self._hello.put(None)
        if self.proc and self.proc.returncode is None:
            self.proc.q.put(json.dumps({"error": f"connection to {self.host} lost"}))
            self.proc._finish(-1)

    # ---------------------------------------------------------------- used by main.py
    def poller(self, gpu_index, disk):
        return RemotePoller(self, gpu_index, disk)

    def start(self, script, seconds, disk=None, command=None):
        self.proc = RemoteProc(self)
        if not self.send(cmd="start", script=script, seconds=seconds, disk=disk, command=command):
            raise OSError(f"connection to {self.host} lost")
        return self.proc
