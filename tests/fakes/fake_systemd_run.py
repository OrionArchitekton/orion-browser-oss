"""Fake `systemd-run --user`: starts the command as a new process group, with only the --setenv environment (plus the
fakes' own FAKE_ settings, which a real manager would not pass), as the service manager starts a transient unit.

Setting `start_waits_for` to a file name under FAKE_DIR makes it return only once that file exists (or the unit's main
process has ended, or WAIT_LIMIT seconds have passed): a caller that resumes only after the unit's work is under way,
as one can that is descheduled at that moment. A test sets it to a record fake Chrome writes at launch, so anything the
caller does after the start happens after Chrome started.

Setting `systemd_run_hangs` makes it start the unit and then never answer (after start_waits_for, if set), as a call
whose answer is lost: the unit runs, and only the caller's timeout ends the call.

Setting `listen_before_start` to a port makes it start a program of its own listening on that port at the test
loopback (fakesys.listen), and start the unit only once that program listens: another program taking one of the
screen's ports after the lease's look at them, before the session's own program takes it."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import uuid

import fakesys

WAIT_LIMIT = 15.0  # seconds start_waits_for waits at most


def main(argv: list[str]) -> int:
    fakesys.log_call("systemd-run", argv)
    unit, props, env, command = None, {}, {}, []
    for i, arg in enumerate(argv):
        if arg == "--":
            command = argv[i + 1:]
            break
        if arg.startswith("--unit="):
            unit = arg.split("=", 1)[1]
        elif arg.startswith("--property="):
            key, _, value = arg.split("=", 1)[1].partition("=")
            props[key] = value
        elif arg.startswith("--setenv="):
            key, _, value = arg.split("=", 1)[1].partition("=")
            env[key] = value
        elif arg in ("--user", "--collect", "--quiet"):
            continue
        else:
            print(f"fake systemd-run: unknown option {arg}", file=sys.stderr)
            return 1
    if not unit or not command:
        print("fake systemd-run: needs --unit= and a command after --", file=sys.stderr)
        return 1
    if fakesys.settings().get("systemd_run_fails"):
        print("Failed to start transient service unit: fake failure", file=sys.stderr)
        return 1
    if fakesys.settings().get("listen_before_start"):
        fakesys.listen(int(fakesys.settings()["listen_before_start"]))
    passed = {key: value for key, value in os.environ.items() if key.startswith("FAKE_")}
    with fakesys.units() as units:
        old = units.get(unit)
        if old and fakesys.alive(old["pid"]):
            print(f"Failed to start transient service unit: Unit {unit} already exists.", file=sys.stderr)
            return 1
        log = open(fakesys.root() / f"{unit}.log", "ab")
        proc = subprocess.Popen(
            command, env={**passed, **env, "INVOCATION_ID": uuid.uuid4().hex},
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True, cwd="/",
        )
        units[unit] = {"pid": proc.pid, "props": props, "started": time.time()}
    waits_for = fakesys.settings().get("start_waits_for")
    if waits_for:  # outside the units lock: the unit's own calls must not wait behind this one
        deadline = time.monotonic() + WAIT_LIMIT
        while not (fakesys.root() / waits_for).exists() and proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
    if fakesys.settings().get("systemd_run_hangs"):  # outside the units lock, like the wait above
        time.sleep(fakesys.HANG)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
