"""Fake `systemctl --user`: is-active, stop, is-system-running, and show --property=MainPID --value (the unit's main
process while it runs, 0 otherwise).

A stop signals the main process only and SIGKILLs the whole group after TimeoutStopSec, as KillMode=mixed does. A unit
whose main process has exited is collected (and anything left in its group killed), as --collect and the manager's
cleanup would. Setting `stop_does_nothing` makes a stop return without stopping anything. `no_bus` makes every
call fail as systemctl --user does with no user bus: nothing on stdout, a complaint on stderr, exit 1.
`bus_runtime_dir` (a folder) makes every call fail the same way unless the caller's XDG_RUNTIME_DIR names that folder,
where the user's manager is found: the variable unset, as a launcher that filters the environment leaves it (Codex
hands an MCP server only HOME, LANG, LOGNAME, PATH, SHELL, TERM and USER), or set to another folder. `unit_state`
makes is-active report that state (activating, deactivating, ...) for a unit whose main process runs; with
`unit_state_pid` only for the unit whose main process that is (one session), so a session started later reads active.
`show_no_bus` makes only the show calls fail as with no user bus.

Three knobs act a given number of times, on is-active calls that find the unit running, and the calls after them answer
as usual: `is_active_hangs` (a count) makes that many hang until the caller's timeout ends them, and `is_active_no_bus`
(a count) makes that many fail as with no user bus. A call before the unit starts is not counted, so a test can aim
them at the looks a start takes once its service runs. `is_active_no_bus_after_stop` (a count) acts as is_active_no_bus,
but only on calls made once a stop has been asked: the looks of a gone proof after the manager's stop. And
`is_active_no_bus_while_closing` (a file: the browser's closing marker) makes the first is-active call made while that
file exists fail as with no user bus, and no other: a state that cannot be read just after a stop's DevTools close.

Setting `stop_then_listen` to a port makes a stop, once the unit is gone, start a program of its own listening on that
port at the test loopback, and return only once it listens: another program taking a port the stopped session gave
up, before the caller can look. The program runs until the test's teardown ends it."""

from __future__ import annotations

import os
import signal
import sys
import time

import fakesys

def _kill_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def _entry(unit: str) -> dict | None:
    """The unit while its main process runs; a unit whose main process ended is collected here."""
    with fakesys.units() as units:
        entry = units.get(unit)
        if entry and not fakesys.alive(entry["pid"]):
            _kill_group(entry["pid"])
            units.pop(unit)
            return None
        return entry


def main(argv: list[str]) -> int:
    fakesys.log_call("systemctl", argv)
    settings = fakesys.settings()
    if settings.get("no_bus"):
        print("Failed to connect to bus: No medium found", file=sys.stderr)
        return 1
    if settings.get("bus_runtime_dir") and os.environ.get("XDG_RUNTIME_DIR") != settings["bus_runtime_dir"]:
        print("Failed to connect to bus: No medium found", file=sys.stderr)
        return 1
    args = [arg for arg in argv if arg != "--user"]
    if args[:3] == ["show", "--property=MainPID", "--value"] and len(args) == 4:
        if settings.get("show_no_bus"):
            print("Failed to connect to bus: No medium found", file=sys.stderr)
            return 1
        entry = _entry(args[3])
        print(entry["pid"] if entry else 0)
        return 0
    if args == ["is-system-running"]:
        state = settings.get("manager_state", "running")
        print(state)
        return 0 if state == "running" else 1
    if len(args) != 2:
        print(f"fake systemctl: unsupported {argv}", file=sys.stderr)
        return 1
    verb, unit = args
    if verb == "is-active":
        closing = settings.get("is_active_no_bus_while_closing")
        if closing and os.path.exists(closing) and fakesys.take("is-active-no-bus-while-closing", 1):
            print("Failed to connect to bus: No medium found", file=sys.stderr)
            return 1
        entry = _entry(unit)
        if entry is not None and fakesys.take("is-active-hangs", settings.get("is_active_hangs", 0)):
            time.sleep(fakesys.HANG)  # holding no lock: the caller's timeout ends it
        if entry is not None and fakesys.take("is-active-no-bus", settings.get("is_active_no_bus", 0)):
            print("Failed to connect to bus: No medium found", file=sys.stderr)
            return 1
        if entry is not None and (fakesys.root() / "stop-asked").exists() and fakesys.take(
            "is-active-no-bus-after-stop", settings.get("is_active_no_bus_after_stop", 0)
        ):
            print("Failed to connect to bus: No medium found", file=sys.stderr)
            return 1
        if entry is None:
            state = "inactive"
        elif settings.get("unit_state_pid") in (None, entry["pid"]):
            state = settings.get("unit_state", "active")
        else:
            state = "active"
        print(state)
        return 0 if state == "active" else 3
    if verb == "stop":
        (fakesys.root() / "stop-asked").touch()  # for is_active_no_bus_after_stop
        entry = _entry(unit)
        if entry is None:
            print(f"Failed to stop {unit}: Unit {unit} not loaded.", file=sys.stderr)
            return 5
        if settings.get("stop_does_nothing"):
            return 0
        pid = entry["pid"]
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        deadline = time.monotonic() + float(entry["props"].get("TimeoutStopSec", "90"))
        while fakesys.alive(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        _kill_group(pid)
        while fakesys.alive(pid):
            time.sleep(0.05)
        _entry(unit)  # collect it
        if settings.get("stop_then_listen"):
            fakesys.listen(int(settings["stop_then_listen"]))
        return 0
    print(f"fake systemctl: unsupported verb {verb}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
