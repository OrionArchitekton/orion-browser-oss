"""S3: two holders, one browser: BUSY at once, renewals, takeovers, damaged files."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest
from conftest import LOOPBACK

from orion_browser.lease import iso

# Runs the real CLI with a short wait limit, so the give-up needs no 600 s wait and no hook in the product code.
# Pool.lease reads LOCK_WAIT when it runs, so this reaches it.
GIVE_UP_DRIVER = (
    "import sys\n"
    "import orion_browser.session as session\n"
    "session.LOCK_WAIT = 0.5\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def test_busy_at_once_naming_the_holder(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    began = time.monotonic()
    code, out = env.run("lease", "work", "--holder", "b")
    assert code == 3 and out["data"]["holder"] == "a" and time.monotonic() - began < 5


def test_refusals_never_wait_behind_a_start_or_stop_in_flight(env):
    with env.holding_session_lock("work"):
        began = time.monotonic()
        assert env.run("release", "work", "--holder", "b", timeout=20)[0] == 4  # no lease on file: refused at once
        env.write_lease("work", holder="a")
        assert env.run("lease", "work", "--holder", "b", timeout=20)[0] == 3
        assert env.run("release", "work", "--holder", "b", timeout=20)[0] == 4
        assert time.monotonic() - began < 10


def test_a_holder_arriving_during_a_start_gets_busy_once_the_lease_is_on_file(env):
    with env.holding_session_lock("work"):
        waiting = env.popen("lease", "work", "--holder", "b")
        env.wait_until(env.lease_file("work").with_suffix(".lock").exists)  # b has begun its first look for a lease
        time.sleep(0.3)  # and, finding none, waits for the start in flight
        assert waiting.poll() is None
        env.write_lease("work", holder="a")  # the start in flight writes its lease
        code, out = env.result(waiting, timeout=10)
        assert code == 3 and out["data"]["holder"] == "a"


def test_a_start_or_stop_that_never_ends_is_given_up_on(env):
    with env.holding_session_lock("work"):  # no lease on file: the lease is not refused, it waits
        proc = subprocess.run(
            [sys.executable, "-P", "-c", GIVE_UP_DRIVER, "--json", "lease", "work", "--holder", "b"],
            capture_output=True, text=True, env=env.env, timeout=10,
        )
    assert proc.stdout, proc.stderr  # a driver that fails shows its traceback
    out = json.loads(proc.stdout)
    assert proc.returncode == out["exit"] == 3
    assert out["data"] == {"browser": "work", "holder": None, "expires_at": None}  # every BUSY carries the same keys
    assert "starting or stopping for over 0.5 s" in out["message"]
    assert not env.lease_file("work").exists() and env.events("work") == []


def test_a_release_whose_lease_changes_hands_while_it_waits_is_refused_at_once(env):
    """The holder's own release waits for a start or stop in flight, but the lease is looked at again while it waits:
    once another holder's lease is on file (here the start in flight is b's takeover), the release is NOT_HOLDER at
    once, without waiting for that start to finish."""
    env.write_lease("work", holder="a")
    with env.holding_session_lock("work"):
        waiting = env.popen("release", "work", "--holder", "a")
        env.wait_until(env.lease_file("work").with_suffix(".lock").exists)  # its first look at the lease
        time.sleep(0.3)  # the lease was a's own: it waits for the start or stop in flight
        assert waiting.poll() is None
        env.write_lease("work", holder="b")  # the start in flight writes its lease
        changed = time.monotonic()
        code, out = env.result(waiting, timeout=10)  # answered while the session lock is still held
        assert time.monotonic() - changed < 5
    assert code == 4 and out["message"] == "a does not hold browser work: it is held by b; lease it first", out
    assert env.events("work") == []


def test_a_release_waiting_for_a_start_or_stop_that_never_ends_is_given_up_on(env):
    """The holder's own release waits for a start or stop in flight at most LOCK_WAIT, as a lease does, then is BUSY
    naming no holder, and the lease stays the holder's."""
    env.write_lease("work", holder="a")
    with env.holding_session_lock("work"):
        proc = subprocess.run(
            [sys.executable, "-P", "-c", GIVE_UP_DRIVER, "--json", "release", "work", "--holder", "a"],
            capture_output=True, text=True, env=env.env, timeout=10,
        )
    assert proc.stdout, proc.stderr
    out = json.loads(proc.stdout)
    assert proc.returncode == out["exit"] == 3
    assert out["data"] == {"browser": "work", "holder": None, "expires_at": None}
    assert "starting or stopping for over 0.5 s" in out["message"]
    assert json.loads(env.lease_file("work").read_text())["holder"] == "a" and env.events("work") == []


def _cpu_seconds(pid: int) -> float:
    """User plus system CPU time of a process, from /proc/<pid>/stat (fields 14 and 15)."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def test_a_lease_waiting_for_a_start_sleeps_between_looks(env):
    with env.holding_session_lock("work"):
        waiting = env.popen("lease", "work", "--holder", "b")
        env.wait_until(env.lease_file("work").with_suffix(".lock").exists)
        before = _cpu_seconds(waiting.pid)
        time.sleep(1)
        assert _cpu_seconds(waiting.pid) - before < 0.1  # it looks every 50 ms; it never spins
        env.write_lease("work", holder="a")
        assert env.result(waiting, timeout=10)[0] == 3


def test_two_holders_at_once_exactly_one_wins(env):
    first, second = env.popen("lease", "work", "--holder", "a"), env.popen("lease", "work", "--holder", "b")
    assert sorted((env.result(first)[0], env.result(second)[0])) == [0, 3]


def test_two_browsers_start_in_parallel(env):
    first, second = env.popen("lease", "anon", "--holder", "x"), env.popen("lease", "work", "--holder", "y")
    assert (env.result(first)[0], env.result(second)[0]) == (0, 0)


def test_a_holders_second_lease_waits_for_its_first_start(env):
    first, second = env.popen("lease", "work", "--holder", "a"), env.popen("lease", "work", "--holder", "a")
    (code1, out1), (code2, out2) = env.result(first), env.result(second)
    assert (code1, code2) == (0, 0), (out1, out2)
    assert sorted(out["message"].split()[0] for out in (out1, out2)) == ["extended", "started"]
    assert out1["data"]["session"] == out2["data"]["session"]
    assert env.events("work") == ["lease", "extend"]
    assert len(env.calls("systemd-run")) == 1


def test_a_release_during_the_holders_start_waits_for_it(env):
    env.set_chrome("work", stall_version=2)  # the start's health proof waits 2 s on DevTools
    starting = env.popen("lease", "work", "--holder", "a")
    env.wait_until(lambda: env.calls("systemd-run"))  # the start is under way
    code, out = env.run("release", "work", "--holder", "a")
    assert env.result(starting)[0] == 0
    assert code == 0, out
    assert env.events("work") == ["lease", "release"]


def test_renewal_keeps_the_session_and_endpoint(env):
    _, first = env.run("lease", "work", "--holder", "a")
    code, again = env.run("lease", "work", "--holder", "a", "--minutes", "120")
    assert code == 0 and again["message"].startswith("extended")
    assert again["data"]["endpoint"] == first["data"]["endpoint"]
    assert again["data"]["expires_at"] > first["data"]["expires_at"]
    assert env.events("work") == ["lease", "extend"]


def test_a_new_session_gets_the_whole_lease_however_long_its_start_took(env):
    """A lease that starts a session runs its length from the moment the start succeeds, not from the lease attempt:
    a slow start (here the health proof waits 4 s on DevTools) never hands the holder a lease partly used, or, with
    --minutes 1 and a slow takeover, one already expired that a contender waiting for the start would take at once."""
    env.rewrite_config("start_limit_seconds = 5", "start_limit_seconds = 15")
    env.set_chrome("work", stall_version=4)
    code, out = env.run("lease", "work", "--holder", "a", "--minutes", "1")
    returned = time.time()
    assert code == 0, out
    lease = json.loads(env.lease_file("work").read_text())
    assert 58 < lease["expires_at"] - returned <= 60, lease["expires_at"] - returned
    assert out["data"]["expires_at"] == iso(lease["expires_at"])  # the answer names the expiry on file


@pytest.mark.parametrize("minutes", ["0", "241"])
def test_minutes_outside_the_limits(env, minutes):
    code, out = env.run("lease", "work", "--holder", "a", "--minutes", minutes)
    assert code == 2 and "--minutes must be from 1 to 240" in out["message"]


def test_an_own_lapsed_lease_resumes_the_same_session(env):
    _, first = env.run("lease", "work", "--holder", "a")
    env.age_lease("work", 120)
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["data"]["endpoint"] == first["data"]["endpoint"]


def test_expiry_stops_nothing_by_itself(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.age_lease("work", 3600)
    code, out = env.run("status", "work")
    assert code == 0 and out["data"]["expired"] is True and out["data"]["running_past_expiry"] is True


def test_a_takeover_restarts_the_session_and_the_old_endpoint_stops_answering(env):
    _, first = env.run("lease", "work", "--holder", "a")
    env.age_lease("work", 3600)
    code, second = env.run("lease", "work", "--holder", "b")
    assert code == 0 and second["data"]["endpoint"] != first["data"]["endpoint"]
    with pytest.raises(OSError):
        socket.create_connection((LOOPBACK, first["data"]["port"]), timeout=1).close()
    takeover = next(record for record in env.audit("work") if record["event"] == "takeover")
    assert takeover["previous_holder"] == "a" and takeover["previous_session"] == first["data"]["session"]
    assert takeover["stop"] == "clean"


def test_a_damaged_lease_file_is_usage(env):
    env.lease_file("work").parent.mkdir(parents=True, exist_ok=True)
    env.lease_file("work").write_text("{")
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 2 and "is damaged" in out["message"]
    assert env.run("status", "work")[0] == 2


def test_release_rules(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    code, out = env.run("release", "work", "--holder", "b")
    assert code == 4 and out["message"] == "b does not hold browser work: it is held by a; lease it first"
    env.age_lease("work", 3600)  # a holder may release its own lapsed lease
    assert env.run("release", "work", "--holder", "a")[0] == 0
    assert env.run("release", "work", "--holder", "a")[0] == 4
