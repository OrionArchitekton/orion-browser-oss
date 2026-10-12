"""Test environment: a temp config and state, the fakes on PATH (and nothing else), and a CLI runner."""

from __future__ import annotations

import fcntl
import json
import os
import random
import signal
import subprocess
import sys
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

TESTS = Path(__file__).resolve().parent
FAKES = TESTS / "fakes"
sys.path.insert(0, str(FAKES))  # the tests use the fakes' shared helpers too

from orion_browser import config, procs  # noqa: E402

_r = random.SystemRandom()
# A loopback address of this test session's own: every fake listens there, never on 127.0.0.1 where real sessions do.
LOOPBACK = f"127.{_r.randint(100, 199)}.{_r.randint(0, 255)}.{_r.randint(1, 254)}"
SCREENS = {"anon": 90, "work": 91}
SWEEP_PASSES = 10  # the teardown sweeps until a pass finds nothing, at most this many times
# Variables a developer's shell may carry that would change what the CLI does.
NOT_INHERITED = (
    "ORION_BROWSER_HOLDER", "ORION_BROWSER_SESSION", "ORION_BROWSER_CONFIG", "ORION_BROWSER_LOOPBACK",
    "INVOCATION_ID", "DISPLAY", "http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
)
SHIMS = {
    "systemd-run": "fake_systemd_run.py", "systemctl": "fake_systemctl.py", "Xvnc": "fake_xvnc.py",
    "websockify": "fake_websockify.py", "xdotool": "fake_xdotool.py", "xwd": "fake_xwd.py",
    "google-chrome": "fake_chrome.py",
}
CONFIG = """\
[chrome]
path = "{root}/bin/google-chrome"

[screen]
width = 800
height = 600

[lease]
ttl_minutes = 30
max_minutes = 240

[timing]
stop_grace_seconds = 1
service_stop_timeout_seconds = 11
start_limit_seconds = 5

[paths]
state = "{root}/state"
profiles = "{root}/profiles"

[view]
novnc_dir = "{root}/novnc"

[[browser]]
name = "anon"
screen = {anon}

[[browser]]
name = "work"
screen = {work}
"""


def _shim(path: Path, script: str) -> None:
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKES / script}" "$@"\n')
    path.chmod(0o755)


def _put_lease(path: Path, data: dict) -> None:
    """Write a lease file in one step and owner-only, as the store does: a CLI that is polling the file never reads it
    empty or half written (and calls it damaged). The temporary name is not the store's own (<name>.tmp)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".test-tmp")
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as fh:
        fh.write(json.dumps(data))
    os.replace(tmp, path)


@dataclass
class Env:
    root: Path
    env: dict[str, str] = field(repr=False)  # a copy of the environment: keep it out of pytest failure output

    def _cli(self, args: tuple[str, ...]) -> list[str]:
        return [sys.executable, "-m", "orion_browser", "--json", *args]

    def run(self, *args: str, timeout: float = 120) -> tuple[int, dict]:
        proc = subprocess.run(self._cli(args), capture_output=True, text=True, env=self.env, timeout=timeout)
        return self._answer(proc.returncode, proc.stdout, proc.stderr)

    def popen(self, *args: str) -> subprocess.Popen:
        return subprocess.Popen(self._cli(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=self.env)

    def result(self, proc: subprocess.Popen, timeout: float = 120) -> tuple[int, dict]:
        stdout, stderr = proc.communicate(timeout=timeout)
        return self._answer(proc.returncode, stdout, stderr)

    @staticmethod
    def _answer(code: int, stdout: str, stderr: str) -> tuple[int, dict]:
        try:
            out = json.loads(stdout)
        except json.JSONDecodeError:
            raise AssertionError(f"no JSON (exit {code}): {stdout!r} {stderr!r}") from None
        assert code == out["exit"], f"exit code {code} != JSON exit {out['exit']}: {out}"
        return code, out

    def set_fake(self, **values: Any) -> None:
        path = self.root / "fake" / "settings.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data.update(values)
        path.write_text(json.dumps(data))

    def set_chrome(self, browser: str, **knobs: Any) -> None:
        path = self.root / "fake" / "settings.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        data.setdefault("chrome", {}).setdefault(browser, {}).update(knobs)
        path.write_text(json.dumps(data))

    def calls(self, tool: str) -> list[list[str]]:
        log = self.root / "fake" / "calls.jsonl"
        if not log.exists():
            return []
        return [entry["argv"] for entry in map(json.loads, log.read_text().splitlines()) if entry["tool"] == tool]

    def audit(self, browser: str) -> list[dict]:
        folder = self.root / "state" / "audit" / browser
        return [json.loads(line) for f in sorted(folder.glob("*.jsonl")) for line in f.read_text().splitlines()]

    def events(self, browser: str) -> list[str]:
        return [record["event"] for record in self.audit(browser)]

    def lease_file(self, browser: str) -> Path:
        return self.root / "state" / "leases" / f"{browser}.json"

    def profile(self, browser: str) -> Path:
        return self.root / "profiles" / browser

    def unit(self, browser: str) -> str:
        """The browser's service name, as the CLI derives it from this environment's config."""
        path = self.root / "config.toml"
        return config.parse(tomllib.loads(path.read_text()), path).browser(browser).unit

    def write_lease(self, browser: str, holder: str, session: str = "", endpoint: str = "", age: float = 0) -> None:
        now = time.time() - age
        _put_lease(self.lease_file(browser), {
            "browser": browser, "holder": holder, "acquired_at": now, "expires_at": now + 1800,
            "session": session, "endpoint": endpoint, "port": 0, "started_at": now,
        })

    def age_lease(self, browser: str, seconds: float) -> None:
        """Move the lease's expiry `seconds` into the past, as if it had lapsed that long ago."""
        data = json.loads(self.lease_file(browser).read_text())
        data["expires_at"] = time.time() - seconds
        _put_lease(self.lease_file(browser), data)

    def git_work_tree(self, kind: str) -> Path:
        """A scratch git work tree under the test's root, marked as git marks one: a non-empty .git folder ("folder"),
        or a .git pointer file ("pointer"), as a linked worktree or a submodule has."""
        repo = self.root / f"repo-{kind}"
        if kind == "folder":
            (repo / ".git").mkdir(parents=True)
            (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        else:
            repo.mkdir(parents=True)
            (repo / ".git").write_text(f"gitdir: {self.root}/elsewhere.git\n")
        return repo

    def rewrite_config(self, old: str, new: str) -> None:
        path = self.root / "config.toml"
        text = path.read_text()
        assert text.count(old) == 1, old
        path.write_text(text.replace(old, new))

    @contextmanager
    def holding_session_lock(self, browser: str) -> Iterator[None]:
        """Hold a browser's start and stop lock from this process, as a start in flight does."""
        path = self.root / "state" / "leases" / f"{browser}.act"
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @staticmethod
    def wait_until(probe: Callable[[], Any], timeout: float = 15) -> Any:
        deadline = time.monotonic() + timeout
        while True:
            value = probe()
            if value:
                return value
            if time.monotonic() > deadline:
                raise AssertionError(f"timed out after {timeout} s waiting for {probe}")
            time.sleep(0.1)

    def _ours(self, pid: int) -> bool:
        """Whether the process has this test's FAKE_DIR in its environment, as every process the harness starts does.
        A zombie has none left to read."""
        marker = f"FAKE_DIR={self.root / 'fake'}".encode()
        try:
            return marker in Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            return False

    def _kill_unit_groups(self) -> None:
        """SIGKILL the process group of each unit the fake service manager recorded whose main process is still ours.
        The file is a record, not proof: it is rewritten in place on every call the fakes make, so it can be caught half
        written (the sweep after this finds the same processes by their FAKE_DIR), and a pid in it may be stale or junk
        (0 would be our own group)."""
        try:
            units = json.loads((self.root / "fake" / "units.json").read_text())
        except (OSError, ValueError):
            return
        for entry in units.values() if isinstance(units, dict) else ():
            pid = entry.get("pid") if isinstance(entry, dict) else None
            if isinstance(pid, int) and self._ours(pid):
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass

    def stop_everything(self) -> None:
        """Kill whatever a test left running, even after a failure mid-test (a red phase): each fake unit's process
        group, every process started with this test's FAKE_DIR in its environment (CLI runs, session runners, fakes),
        and a holder of a test profile that carries it too. Only a process that carries this test's FAKE_DIR is killed
        here, so a bug in how holders are matched can never reach a browser the operator is running; a test that
        starts a holder of its own, without it, reaps that holder itself. The sweep repeats until a pass finds nothing
        (at most SWEEP_PASSES times), because one pass over /proc misses a process that is forked while it runs: a CLI
        in the middle of a start, a runner starting its screen. Without this, a leaked fake screen still listening on
        the shared test address would answer the next test's runner."""
        self._kill_unit_groups()
        for _ in range(SWEEP_PASSES):
            killed = 0
            for entry in os.listdir("/proc"):
                if entry.isdigit() and int(entry) != os.getpid() and self._ours(int(entry)):
                    killed += self._kill(int(entry))
            for browser in SCREENS:
                for process in procs.holding_profile(self.profile(browser)):
                    if self._ours(process["pid"]):
                        killed += self._kill(process["pid"])
            if not killed:
                return
            time.sleep(0.01)

    @staticmethod
    def _kill(pid: int) -> bool:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return False
        return True


def make_env(root: Path) -> Env:
    for sub in ("bin", "fake", "novnc", "home"):
        (root / sub).mkdir(parents=True)
    (root / "novnc" / "vnc.html").write_text("<html>fake noVNC</html>")
    for name, script in SHIMS.items():
        _shim(root / "bin" / name, script)
    (root / "config.toml").write_text(CONFIG.format(root=root, **SCREENS))
    env = {key: value for key, value in os.environ.items() if key not in NOT_INHERITED}
    env.update({
        "PATH": str(root / "bin"),  # the fakes and nothing else: a test that removes a shim really removes the tool
        "HOME": str(root / "home"),
        "ORION_BROWSER_CONFIG": str(root / "config.toml"),
        "ORION_BROWSER_LOOPBACK": LOOPBACK,
        # Every process a test starts must inherit FAKE_DIR, a session's too (the fake systemd-run passes FAKE_* on):
        # the fakes keep their knobs and records under it, and the teardown reaps only processes that carry it.
        "FAKE_DIR": str(root / "fake"),
    })
    return Env(root, env)


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    environment = make_env(tmp_path / "t")
    yield environment
    environment.stop_everything()
