"""Environment: boot targets (static dir, shell command), wait-for-ready, seed hooks.

A flow verifies *something* — this module gets that something running and
waits until it answers, so the runner never races a cold boot.
"""
from __future__ import annotations

import contextlib
import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from urllib.request import urlopen


def find_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for_port(port: int, timeout_s: float = 60, host: str = "127.0.0.1") -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(1)
            try:
                s.connect((host, port))
                return
            except OSError:
                time.sleep(0.5)
    raise TimeoutError(f"port {port} not open after {timeout_s}s")


def wait_for_http(url: str, timeout_s: float = 60, expect: int = 200) -> None:
    deadline = time.time() + timeout_s
    last: Exception | None = None
    while time.time() < deadline:
        try:
            with urlopen(url, timeout=5) as resp:
                if resp.status == expect:
                    return
                last = RuntimeError(f"HTTP {resp.status}")
        except Exception as e:  # noqa: BLE001 — retry until deadline
            last = e
        time.sleep(0.5)
    raise TimeoutError(f"{url} not ready after {timeout_s}s (last: {last})")


@dataclass
class ProcTarget:
    """A booted target: static server or long-running command."""
    url: str
    proc: subprocess.Popen
    log_path: str | None = None
    _log_file: object = field(default=None, repr=False)

    def stop(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=10)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass
        if self._log_file:
            try:
                self._log_file.close()
            except Exception:
                pass

    def __enter__(self) -> "ProcTarget":
        return self

    def __exit__(self, *_) -> None:
        self.stop()


def _logfile_for(name: str) -> tuple[object, str]:
    path = os.path.join("/tmp", f"qaloop-{name}-{os.getpid()}.log")
    f = open(path, "ab")
    return f, path


def boot_static(directory: str, port: int | None = None) -> ProcTarget:
    """Serve a directory over HTTP. Returns a ProcTarget (use as context manager)."""
    port = port or find_free_port()
    logf, log_path = _logfile_for("static")
    proc = subprocess.Popen(
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=directory, stdout=logf, stderr=subprocess.STDOUT)
    url = f"http://127.0.0.1:{port}/"
    wait_for_http(url, timeout_s=30)
    return ProcTarget(url=url, proc=proc, log_path=log_path, _log_file=logf)


def boot_command(cmd: str, cwd: str | None = None, env: dict | None = None,
                 wait: dict | None = None, timeout_s: float = 90,
                 name: str = "cmd") -> ProcTarget:
    """Run a long-lived command (dev server etc.) and wait until ready.

    wait: {"port": n} | {"http": url} | {"log_contains": str}
    """
    merged = dict(os.environ)
    if env:
        merged.update({k: str(v) for k, v in env.items()})
    logf, log_path = _logfile_for(name)
    proc = subprocess.Popen(shlex.split(cmd), cwd=cwd, env=merged,
                            stdout=logf, stderr=subprocess.STDOUT)
    url = ""
    try:
        if not wait or "port" in wait:
            port = (wait or {}).get("port")
            if port is None:
                raise ValueError("boot_command needs wait.port when no http/log wait given")
            wait_for_port(int(port), timeout_s)
            url = f"http://127.0.0.1:{port}/"
        elif "http" in wait:
            url = wait["http"]
            wait_for_http(url, timeout_s)
        elif "log_contains" in wait:
            needle = wait["log_contains"]
            deadline = time.time() + timeout_s
            while time.time() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f"command exited ({proc.returncode}) before ready")
                with open(log_path, "rb") as f:
                    if needle.encode() in f.read():
                        break
                time.sleep(0.5)
            else:
                raise TimeoutError(f"log never contained {needle!r} after {timeout_s}s")
        else:
            raise ValueError(f"unknown wait spec {wait}")
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
        raise
    return ProcTarget(url=url, proc=proc, log_path=log_path, _log_file=logf)


@contextlib.contextmanager
def flow_services(specs: list[dict], base_dir: str, run_dir: str):
    """Boot a flow's `services:` block, export their URLs, tear down after.

    For each service, exports QALOOP_SERVICE_<NAME>_URL (and _PORT when the
    wait spec names a port) into os.environ so the spec's ${VAR} substitution
    can use them. Service logs are copied into <run_dir>/services/ on teardown.
    Yields {name: ProcTarget}. Stops already-booted services if one fails.
    """
    targets: dict[str, ProcTarget] = {}
    exported: list[str] = []
    log_dir = os.path.join(run_dir, "services")
    try:
        for s in specs:
            name, uname = s["name"], s["name"].upper()
            cwd = s.get("cwd")
            if cwd and not os.path.isabs(cwd):
                cwd = os.path.join(base_dir, cwd)
            wait = dict(s["wait"])
            # Default wait URLs to 127.0.0.1 when only a port is given.
            t = boot_command(s["command"], cwd=cwd, env=s.get("env") or None,
                             wait=wait, timeout_s=s.get("timeout_s", 90),
                             name=f"svc-{name}")
            targets[name] = t
            url = s.get("url") or t.url
            if url and not s.get("url"):
                # t.url may be an http-wait health URL with a path
                # (e.g. http://127.0.0.1:8933/api/greeting); the service's
                # public base is the origin, not the health endpoint.
                parts = urlsplit(url)
                if parts.path not in ("", "/"):
                    url = f"{parts.scheme}://{parts.netloc}/"
            url_key = f"QALOOP_SERVICE_{uname}_URL"
            os.environ[url_key] = url
            exported.append(url_key)
            port = wait.get("port")
            if port:
                port_key = f"QALOOP_SERVICE_{uname}_PORT"
                os.environ[port_key] = str(port)
                exported.append(port_key)
        yield targets
    finally:
        for t in reversed(list(targets.values())):
            t.stop()
        for var in exported:
            os.environ.pop(var, None)
        if targets:
            os.makedirs(log_dir, exist_ok=True)
            for name, t in targets.items():
                if t.log_path and os.path.exists(t.log_path):
                    with open(t.log_path, "rb") as fsrc:
                        with open(os.path.join(log_dir, f"{name}.log"), "wb") as fdst:
                            fdst.write(fsrc.read())
