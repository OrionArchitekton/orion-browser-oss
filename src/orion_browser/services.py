"""The only module that runs systemd-run and systemctl (the user instance)."""

from __future__ import annotations

import os
import stat
import subprocess

from orion_browser.codes import BrowserError, Code

CALL_TIMEOUT = 30.0
RUNTIME_BASE = "/run/user"  # holds each user's runtime folder, /run/user/<uid>; read when a command runs
# The states `systemctl is-active` reports. Anything else is no state at all: with no user bus it prints nothing.
STATES = frozenset({
    "active", "reloading", "refreshing", "activating", "deactivating", "maintenance", "inactive", "failed",
})
GONE = frozenset({"inactive", "failed"})  # the only states in which no process of the unit runs


def _environment() -> dict[str, str] | None:
    """The environment a systemd command runs in: None, the caller's own, unless XDG_RUNTIME_DIR is unset. `systemctl
    --user` finds the user's manager through it, and a launcher that filters the environment leaves it out (Codex hands
    an MCP server only HOME, LANG, LOGNAME, PATH, SHELL, TERM and USER), so that every call fails with "Failed to
    connect to bus". It then defaults to /run/user/<uid> when that folder exists and is the user's own."""
    if os.environ.get("XDG_RUNTIME_DIR"):
        return None
    folder = os.path.join(RUNTIME_BASE, str(os.getuid()))
    try:
        found = os.stat(folder)
    except OSError:
        return None
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
        return None
    return {**os.environ, "XDG_RUNTIME_DIR": folder}


def _run(argv: list[str], timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run a systemd command, waiting at most `timeout` seconds; CALL_TIMEOUT, read at each call, when none is given."""
    timeout = CALL_TIMEOUT if timeout is None else timeout
    try:
        return subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, env=_environment()
        )
    except FileNotFoundError:
        raise BrowserError(Code.USAGE, f"{argv[0]} is not installed: orion-browser needs systemd's user instance") from None
    except subprocess.TimeoutExpired:
        raise BrowserError(Code.BROWSER_DOWN, f"`{' '.join(argv[:4])}` gave no answer in {timeout:g} s") from None


def manager_state() -> str:
    """What the user instance says about itself: running, degraded, starting, offline, ..."""
    return _run(["systemctl", "--user", "is-system-running"]).stdout.strip() or "unknown"


def state(unit: str) -> str:
    """The unit's state, exactly as the user instance reports it. USAGE when it reports none (with no user bus
    systemctl prints nothing): a state that cannot be read is never taken for a stopped unit.

    The health proof and a holder's resume of its own session (Pool._answering) want this exact state: only "active"
    passes. Pool._row and Pool._await_healthy read it once and use both readings: the exact state for the proof, GONE
    for whether the session still runs (as is_active)."""
    proc = _run(["systemctl", "--user", "is-active", unit])
    reported = proc.stdout.strip()
    if reported not in STATES:
        said = (proc.stderr or proc.stdout).strip()[-200:] or f"exit code {proc.returncode}"
        raise BrowserError(
            Code.USAGE, f"systemd's user instance cannot be reached ({said}); the state of {unit} is unknown"
        )
    return reported


def is_active(unit: str) -> bool:
    """Whether a process of the unit may still run: true in every state but inactive and failed. The conservative
    reading, for each caller that must never take a running session for a stopped one: the gone proof (Pool._stop,
    Pool._wait_gone), and the stop before a start (Pool._stop_old) and at a release (Pool.release). USAGE when the
    state cannot be read, as state()."""
    return state(unit) not in GONE


def main_pid(unit: str) -> int:
    """The pid of the unit's main process (a session's runner), 0 when none runs. USAGE when it cannot be read, as
    state(): with no user bus systemctl prints nothing."""
    proc = _run(["systemctl", "--user", "show", "--property=MainPID", "--value", unit])
    reported = proc.stdout.strip()
    if proc.returncode != 0 or not reported.isdigit():
        said = (proc.stderr or proc.stdout).strip()[-200:] or f"exit code {proc.returncode}"
        raise BrowserError(
            Code.USAGE, f"systemd's user instance cannot be reached ({said}); the main process of {unit} is unknown"
        )
    return int(reported)


def start(unit: str, argv: list[str], env: dict[str, str], stop_timeout: float) -> None:
    """Start argv as a transient user service. KillMode=mixed: a stop signals only the session's own process, which
    closes Chrome through DevTools first; whatever is left gets SIGKILL after the stop timeout."""
    command = [
        "systemd-run", "--user", f"--unit={unit}", "--collect", "--quiet",
        "--property=KillMode=mixed", f"--property=TimeoutStopSec={int(stop_timeout)}", "--property=Restart=no",
        *(f"--setenv={key}={value}" for key, value in sorted(env.items())),
        "--", *argv,
    ]
    proc = _run(command)
    if proc.returncode != 0:
        raise BrowserError(Code.BROWSER_DOWN, f"could not start {unit}: {(proc.stderr or proc.stdout).strip()[-300:]}")


def stop(unit: str, timeout: float) -> None:
    proc = _run(["systemctl", "--user", "stop", unit], timeout)
    if proc.returncode not in (0, 5):  # 5: the unit is not loaded, so there is nothing to stop
        raise BrowserError(Code.BROWSER_DOWN, f"could not stop {unit}: {(proc.stderr or proc.stdout).strip()[-300:]}")
