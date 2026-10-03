"""Environment: boot targets (static dir, shell command), wait-for-ready, seed hooks.

A flow verifies *something* — this module gets that something running and
waits until it answers, so the runner never races a cold boot.
"""
from __future__ import annotations

import os
import shlex
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
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
