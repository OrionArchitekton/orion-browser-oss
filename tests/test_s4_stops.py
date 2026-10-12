"""S4: stops are clean and proven; crashes, collisions, a stale port file, and the one retry."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest
from conftest import FAKES, LOOPBACK, NOT_INHERITED, SCREENS

from orion_browser import config, procs, runner, services, session
from orion_browser.audit import Audit
from orion_browser.codes import BrowserError, Code
from orion_browser.lease import Lease


@contextmanager
def holding_profile_start(env, browser: str) -> Iterator[None]:
    """Hold the browser's profile start lock from this process, as another config's start of the same profile does."""
    profile = Path(os.path.realpath(env.profile(browser)))
    profile.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(profile.parent / f"{profile.name}.start", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def stray_chrome(env, browser: str) -> subprocess.Popen:
    """A Chrome on the browser's profile that orion-browser did not start (an agent driver launched it directly)."""
    stray = subprocess.Popen(
        [sys.executable, str(FAKES / "fake_chrome.py"), f"--user-data-dir={env.profile(browser)}"],
        env={**env.env, "DISPLAY": ":99"},
    )
    env.wait_until(lambda: (env.profile(browser) / "DevToolsActivePort").exists())
    return stray


def _run_through(env, driver: str, *args: str) -> tuple[int, dict]:
    """Run the real CLI through a driver (a short program that changes one thing, then runs it), and check its answer
    with the harness's own checks, as env.run does: a driver that fails shows its output and its traceback."""
    proc = subprocess.run(
        [sys.executable, "-P", "-c", driver, "--json", *args], capture_output=True, text=True, env=env.env, timeout=120,
    )
    return env._answer(proc.returncode, proc.stdout, proc.stderr)


# Runs the real CLI with the gone proof's wait after the manager's stop cut to 1 s, for the one command a test expects
# to fail that proof: it then fails in seconds, not in GONE_AFTER_STOP. Pool._stop reads GONE_AFTER_STOP when it runs.
GONE_SOONER_DRIVER = (
    "import sys\n"
    "import orion_browser.session as session\n"
    "session.GONE_AFTER_STOP = 1.0\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def test_a_close_chrome_took_is_clean_even_when_the_sessions_teardown_outlasts_the_grace(env):
    """Chrome takes the release's DevTools close and exits at once, but the session's own teardown runs past the stop
    grace (here its view ignores SIGTERM, and is killed 5 s later), so the tool goes on to the service manager's stop.
    The session wrote down that Chrome ended on the tool's close, so the release still says clean, with no warning
    that sign-ins may not have been saved."""
    env.set_fake(websockify_forks=True, websockify_hangs=True)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    code, out = env.run("release", "work", "--holder", "a")
    assert (env.profile("work") / "Cookies.fake").read_text() == "saved"  # Chrome took the close
    assert code == 0 and out["data"]["stop"] == "clean" and "not have been saved" not in out["message"], out
    assert [r["stop"] for r in env.audit("work") if r["event"] == "release"] == ["clean"]


def test_an_ignored_close_falls_back_to_a_signal_and_says_so(env):
    env.set_chrome("work", ignore_close=True)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "signal" and "may not have been saved" in out["message"]
    assert not (env.profile("work") / "Cookies.fake").exists()
    assert [r["stop"] for r in env.audit("work") if r["event"] == "service-stop"] == ["signal"]


def test_an_ignored_signal_ends_in_a_kill(env):
    env.set_chrome("work", ignore_close=True, ignore_sigterm=True)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "kill"


def test_a_stop_from_outside_the_tool_is_clean(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    subprocess.run(["systemctl", "--user", "stop", env.unit("work")], env=env.env, check=True)
    assert (env.profile("work") / "Cookies.fake").read_text() == "saved"
    assert [r["stop"] for r in env.audit("work") if r["event"] == "service-stop"] == ["clean"]


def _stop_while_starting(env, started: str) -> tuple[float, subprocess.Popen]:
    """Start a lease in the background, and once the fake named by `started` has been called (the session is at that
    step), stop the session's service from outside the tool, as the service manager or the operator would. Returns how
    long that stop took and the lease's process."""
    starting = env.popen("lease", "work", "--holder", "a")
    env.wait_until(lambda: env.calls(started), timeout=30)
    began = time.monotonic()
    subprocess.run(["systemctl", "--user", "stop", env.unit("work")], env=env.env, check=True, timeout=60)
    return time.monotonic() - began, starting


def test_a_stop_while_the_session_waits_for_its_view_ends_it_before_chrome_starts(env):
    """A stop signal that reaches the session while it waits for its view: once the view is up, the session sees the
    stop before it starts Chrome and ends there, with nothing to close, recording a stop that found no Chrome."""
    env.set_fake(websockify_listen_after=2)
    took, starting = _stop_while_starting(env, "websockify")
    stopped_at = time.time()
    calls = [json.loads(line) for line in (env.root / "fake" / "calls.jsonl").read_text().splitlines()]
    assert [c["ts"] for c in calls if c["tool"] == "chrome" and c["ts"] < stopped_at] == []  # none for that session
    assert [r["stop"] for r in env.audit("work") if r["event"] == "service-stop"] == ["none"]
    assert took < 11  # the session ended by itself, before the manager's stop timeout (11 s here) killed it
    env.result(starting)  # finding no port file, the lease makes its one more try (Q4), which may start a session


def test_a_stop_while_the_session_focuses_chrome_closes_it_cleanly(env):
    """A stop signal that reaches the session while it gives Chrome's window the focus (here no window ever turns up,
    so that step would wait its whole 15 s): the step ends at once and the stop closes Chrome through DevTools within
    the grace, well before the manager's stop timeout (11 s here) would kill it."""
    env.set_fake(xdotool_no_window=True)
    took, starting = _stop_while_starting(env, "xdotool")
    assert [r["stop"] for r in env.audit("work") if r["event"] == "service-stop"] == ["clean"]
    assert (env.profile("work") / "Cookies.fake").read_text() == "saved"
    assert took < 11
    env.result(starting)


def test_a_crash_is_recorded_and_the_next_lease_starts_a_new_session(env):
    env.set_chrome("work", crash_after=3.0)
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)
    assert "crashed" in env.events("work")
    env.set_chrome("work", crash_after=None)
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["data"]["endpoint"] != first["data"]["endpoint"]


def test_a_release_that_cannot_show_the_session_gone_keeps_the_lease(env):
    env.set_chrome("work", ignore_close=True)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(stop_does_nothing=True)
    code, out = _run_through(env, GONE_SOONER_DRIVER, "release", "work", "--holder", "a")
    assert code == 6 and "could not be shown gone" in out["message"]
    assert env.run("status", "work")[1]["data"]["holder"] == "a"
    env.set_fake(stop_does_nothing=False)
    assert env.run("release", "work", "--holder", "a")[0] == 0


def test_a_profile_open_in_another_process_is_refused_and_left_alone(env):
    stray = stray_chrome(env, "work")
    try:
        code, out = env.run("lease", "work", "--holder", "a")
        assert code == 6 and f"pid {stray.pid}" in out["message"], out
        assert stray.poll() is None  # the tool never stops it
        assert not env.lease_file("work").exists()
        assert "collision" in env.events("work")
    finally:
        stray.kill()
        stray.wait()


def test_a_port_file_written_by_a_chrome_the_tool_did_not_start_never_gets_that_chrome_closed(env):
    """A Chrome the tool did not start opens the profile once the session runs (after the start's collision check) and
    writes its own port file over the session's. No process of the session's service listens on that endpoint, so no
    stop sends it the DevTools close: the tool's stop and the session's own stop handling each skip the close and record
    that, and the service manager stops the session's service. The other Chrome runs on untouched. It still holds the
    profile, so the session cannot be shown gone and the release keeps the lease, as with any process on the profile."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    stray = stray_chrome(env, "work")
    try:
        env.wait_until(lambda: _port(env, "work") not in (None, first["data"]["port"]))
        stray_port = _port(env, "work")
        code, out = _run_through(env, GONE_SOONER_DRIVER, "release", "work", "--holder", "a")
        assert stray.poll() is None  # never closed, never signalled
        assert code == 6 and "could not be shown gone" in out["message"], out
        assert ["--user", "stop", env.unit("work")] in env.calls("systemctl")
        state = subprocess.run(["systemctl", "--user", "is-active", env.unit("work")], env=env.env, capture_output=True)
        assert state.stdout.decode().strip() == "inactive"  # the session's own service was stopped
        skipped = [(r["by"], r["port"], r["reason"]) for r in env.audit("work") if r["event"] == "close-skipped"]
        why = "no process of the session listens on the port"
        assert skipped == [("tool", stray_port, why), ("service", stray_port, why)], env.audit("work")
        (stopped,) = [r for r in env.audit("work") if r["event"] == "service-stop"]
        assert stopped["stop"] == "signal"  # the session's own Chrome, ended by a signal instead
        assert json.loads(env.lease_file("work").read_text())["holder"] == "a"
    finally:
        stray.kill()
        stray.wait()


def _session_stop_in_process(env, monkeypatch) -> tuple[config.Browser, subprocess.Popen, Lease]:
    """The session's own stop handling, run in-process: the test's environment, the browser, a child standing in for
    the session's Chrome that exits by itself 1 s from now (as a Chrome finishing a close it took), and its lease."""
    for key in NOT_INHERITED:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    b = config.load().browser("work")
    b.profile.mkdir(parents=True, exist_ok=True)
    chrome = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1)"])
    lease = Lease(browser="work", holder="a", session="s1", endpoint="", port=0, acquired_at=0, expires_at=0,
                  started_at=0)
    return b, chrome, lease


def test_the_sessions_stop_waits_the_grace_when_nobody_listens_on_the_port_file(env, monkeypatch):
    """Chrome took the tool's DevTools close and has already shut its DevTools listener (real Chrome does so early in
    its teardown), but is still saving when the stop signal reaches the session. The port file names a port nobody
    listens on, so the session sends no close (and records that), yet it still gives its own Chrome the grace before
    any signal: the stop is clean. (In-process: the session's stop handling, with a child as its Chrome.)"""
    b, chrome, lease = _session_stop_in_process(env, monkeypatch)
    unused = socket.socket()
    unused.bind((LOOPBACK, 0))
    port = unused.getsockname()[1]
    unused.close()  # a port nobody listens on any more
    b.port_file.write_text(f"{port}\n/devtools/browser/gone\n")
    assert runner._end_chrome(b, lease, chrome, 10.0, Audit(b.state_dir)) == "clean"
    assert chrome.returncode == 0  # it exited by itself, never signalled
    skipped = [(r["by"], r["port"], r["reason"]) for r in env.audit("work") if r["event"] == "close-skipped"]
    assert skipped == [("service", port, "no process of the session listens on the port")]


@pytest.mark.parametrize("port_file", [None, "not a port file\n"], ids=["missing", "unreadable"])
def test_the_sessions_stop_waits_the_grace_when_there_is_no_port_file(env, monkeypatch, port_file):
    """Chrome removes its port file as it shuts down after a close it took, so a stop signal that reaches the session
    then finds none (or one it cannot read). There is nothing to close through, which the session records, but its own
    Chrome still gets the grace before any signal: the stop is clean."""
    b, chrome, lease = _session_stop_in_process(env, monkeypatch)
    if port_file is not None:
        b.port_file.write_text(port_file)
    assert runner._end_chrome(b, lease, chrome, 10.0, Audit(b.state_dir)) == "clean"
    assert chrome.returncode == 0  # it exited by itself, never signalled
    skipped = [(r["by"], r.get("port"), r["reason"]) for r in env.audit("work") if r["event"] == "close-skipped"]
    assert skipped == [("service", None, "no readable port file")]


def test_a_stop_that_cannot_read_the_services_main_process_leaves_the_close_to_the_session(env):
    """With the service's main process unknown (systemd answers no show), the tool cannot tell whose endpoint the port
    file names, so it skips its own close (recorded) and stops the service. The session's own stop handling knows its
    Chrome and closes it cleanly through DevTools: the release still ends clean."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(show_no_bus=True)
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "clean", out
    skipped = [(r["by"], r["reason"]) for r in env.audit("work") if r["event"] == "close-skipped"]
    assert skipped == [("tool", "the service's main process is unknown")]


def _port(env, browser: str) -> int | None:
    """The port the browser's port file names now, or None when there is no complete one."""
    try:
        return int((env.profile(browser) / "DevToolsActivePort").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def test_a_screen_another_program_holds_is_refused_and_left_alone(env):
    port = 5900 + SCREENS["work"]
    other = socket.create_server((LOOPBACK, port))  # another config's session on this screen, or any program
    try:
        code, out = env.run("lease", "work", "--holder", "a")
        assert code == 6 and str(port) in out["message"], out
        assert env.calls("Xvnc") == [] and env.calls("chrome") == []  # nothing was started on that screen
        assert not env.lease_file("work").exists()
        assert "collision" in env.events("work")
    finally:
        other.close()
    assert env.run("lease", "work", "--holder", "a")[0] == 0  # once it has gone, the browser starts


def test_two_configs_starting_on_one_screen_at_once_never_share_it(env):
    """A second config (its own state and profiles) names the same screen. Of two leases at the same moment, one gets
    the browser and the other is refused, whichever starts first: a session uses a screen only once it owns it."""
    second = env.root / "second.toml"
    second.write_text(
        (env.root / "config.toml").read_text()
        .replace(f'"{env.root}/state"', f'"{env.root}/state2"')
        .replace(f'"{env.root}/profiles"', f'"{env.root}/profiles2"')
    )
    first = env.popen("lease", "work", "--holder", "a")
    other = subprocess.Popen(
        [sys.executable, "-m", "orion_browser", "--json", "lease", "work", "--holder", "b"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**env.env, "ORION_BROWSER_CONFIG": str(second)},
    )
    assert sorted([env.result(first)[0], env.result(other)[0]]) == [0, 6]
    assert len(env.calls("chrome")) == 1  # the refused start never launched a Chrome


@pytest.mark.parametrize("layout", ["shared folder", "profile link"])
def test_two_configs_reaching_one_profile_never_start_it_at_once(env, layout):
    """A second config (its own state and screen) reaches the same profile, through the same profiles folder or a
    link to the profile, and leases the browser at the same moment: one gets it, the other is refused, no second
    Chrome ever opens the profile, and the one that got it keeps running."""
    text = (env.root / "config.toml").read_text().replace(f'"{env.root}/state"', f'"{env.root}/state2"')
    text = text.replace(f"screen = {SCREENS['work']}", "screen = 92")
    if layout == "profile link":
        env.profile("work").mkdir(parents=True, exist_ok=True)
        (env.root / "profiles2").mkdir()
        (env.root / "profiles2" / "work").symlink_to(env.profile("work"))
        text = text.replace(f'"{env.root}/profiles"', f'"{env.root}/profiles2"')
    second = env.root / "second.toml"
    second.write_text(text)
    first = env.popen("lease", "work", "--holder", "a")
    other = subprocess.Popen(
        [sys.executable, "-m", "orion_browser", "--json", "lease", "work", "--holder", "b"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**env.env, "ORION_BROWSER_CONFIG": str(second)},
    )
    assert sorted([env.result(first)[0], env.result(other)[0]]) == [0, 6]
    assert len(env.calls("chrome")) == 1  # the refused start never opened the shared profile
    time.sleep(1)  # time for a session whose Chrome was closed under it to record that
    for state in ("state", "state2"):
        events = [json.loads(line)["event"] for f in (env.root / state / "audit" / "work").glob("*.jsonl")
                  for line in f.read_text().splitlines()]
        assert "crashed" not in events and "service-stop" not in events, (state, events)


def test_a_release_keeps_the_lease_while_another_process_has_the_profile_open(env):
    env.set_chrome("work", crash_after=3.0)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)
    env.set_chrome("work", crash_after=None)
    stray = stray_chrome(env, "work")
    try:
        code, out = env.run("release", "work", "--holder", "a")
        assert code == 6 and "did not start" in out["message"]
        assert env.run("status", "work")[1]["data"]["holder"] == "a"
    finally:
        stray.kill()
        stray.wait()
    assert env.run("release", "work", "--holder", "a")[0] == 0


def test_a_start_whose_chrome_never_writes_a_port_file_is_tried_once_more(env):
    env.set_chrome("work", no_port_file_launches=1)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 0, out
    assert len(env.calls("systemd-run")) == 2


def test_a_screen_that_never_comes_up_fails_after_one_more_try(env):
    env.set_fake(xvnc_fails=True)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 6 and "service" in out["data"]["failed"]
    assert len(env.calls("systemd-run")) == 2
    assert not env.lease_file("work").exists()


def test_a_stale_port_file_is_never_read(env):
    env.profile("work").mkdir(parents=True)
    (env.profile("work") / "DevToolsActivePort").write_text("1\n/devtools/browser/stale\n")
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 0 and out["data"]["port"] != 1


def test_the_session_entry_point_refuses_a_hand_run(env):
    env.write_lease("work", holder="a", session="s1")
    code, out = env.run("_session", "work")
    assert code == 2 and "starts only through" in out["message"]


def _main_pid(env, browser: str) -> int:
    """The main process of the browser's unit, as the fake service manager records it."""
    return json.loads((env.root / "fake" / "units.json").read_text())[env.unit(browser)]["pid"]


def _takeovers(env, browser: str) -> list[dict]:
    return [record for record in env.audit(browser) if record["event"] == "takeover"]


def test_a_takeover_is_recorded_even_when_the_new_start_fails(env):
    """The takeover is recorded as soon as the old session's stop is done, before the new start: a start that then
    fails still leaves on record whose lease was taken over and how its session ended. The failed start's own record
    names the previous holder and session too, as every failed start of a takeover does."""
    _, first = env.run("lease", "work", "--holder", "a")
    env.age_lease("work", 3600)
    env.set_fake(xvnc_fails=True)  # the new session's screen never comes up
    code, out = env.run("lease", "work", "--holder", "b")
    assert code == 6, out
    assert not env.lease_file("work").exists()  # the takeover is done: the failed start frees the lease
    (takeover,) = _takeovers(env, "work")
    assert (takeover["holder"], takeover["previous_holder"]) == ("b", "a")
    assert takeover["previous_session"] == first["data"]["session"] and takeover["stop"] == "clean"
    assert takeover["session"] == ""  # b has no session yet when its takeover is recorded: it names none of its own
    failed = env.audit("work")[-1]
    assert (failed["event"], failed["holder"]) == ("start-failed", "b")
    assert (failed.get("previous_holder"), failed.get("previous_session")) == ("a", first["data"]["session"])


def test_a_takeover_is_recorded_even_when_the_lease_is_killed_during_the_new_start(env):
    """A lease killed while its new session starts (its process ended by the operator, or by its own caller) never
    records the start, but the takeover before it is on record."""
    _, first = env.run("lease", "work", "--holder", "a")
    env.age_lease("work", 3600)
    env.set_chrome("work", stall_version=60)  # the new session's health proof waits on DevTools, so its start lasts
    taking = env.popen("lease", "work", "--holder", "b")
    env.wait_until(lambda: len(env.calls("systemd-run")) == 2)  # the new session's start is under way
    taking.kill()
    taking.communicate()
    (takeover,) = _takeovers(env, "work")
    assert (takeover["holder"], takeover["previous_holder"]) == ("b", "a")
    assert takeover["previous_session"] == first["data"]["session"] and takeover["stop"] == "clean"


def test_the_stop_of_a_holders_own_session_that_no_longer_answers_is_recorded(env):
    """A holder whose own session no longer answers gets a new one. The old session's stop is recorded with how it
    ended, as every stop is, before the new start."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.set_fake(unit_state="deactivating", unit_state_pid=_main_pid(env, "work"))  # that session only
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["message"].startswith("started"), again
    assert env.events("work") == ["lease", "stop", "lease"]
    stop = env.audit("work")[1]
    assert (stop["holder"], stop["session"], stop["stop"]) == ("a", first["data"]["session"], "clean")


def test_a_failed_start_records_its_stop_and_names_the_session_that_failed(env):
    """A start that is not proven healthy stops what it began. That stop is recorded with how it ended, and the failed
    start's record names the session that failed to start."""
    env.set_chrome("work", headless=True)  # the session runs, and never proves healthy
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 7, out
    stop, failed = env.audit("work")
    assert (stop["event"], failed["event"]) == ("stop", "start-failed")
    assert stop["session"] == failed["session"] != "" and stop["stop"] == "clean" and failed["code"] == "UNHEALTHY"


def test_the_stop_before_the_one_more_try_is_recorded(env):
    """A start whose Chrome wrote no port file is stopped and tried once more (Q4). That stop is recorded too, with the
    session it stopped: every session the tool started is on record."""
    env.set_chrome("work", no_port_file_launches=1)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 0, out
    stops = [record for record in env.audit("work") if record["event"] == "stop"]
    assert len(stops) == 1 and stops[0]["stop"] == "signal"  # with no port file, nothing to close it through
    assert stops[0]["session"] not in ("", out["data"]["session"])
    assert env.events("work")[-1] == "lease"


def test_both_tries_of_a_start_that_never_comes_up_are_recorded(env):
    """Two failed tries: each one's stop is recorded with its own session, and the failed start's record names the
    second, the session that failed last."""
    env.set_fake(xvnc_fails=True)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 6, out
    first, second, failed = env.audit("work")
    assert [record["event"] for record in (first, second, failed)] == ["stop", "stop", "start-failed"]
    assert first["session"] != second["session"] == failed["session"] != ""
    assert first["stop"] == second["stop"] == "none"  # the session had ended by itself: nothing was left to stop


def test_a_start_refused_before_its_service_starts_names_its_session(env):
    """The lease records a start's session before anything else in the start, so even a start refused before its
    service starts (here its preferences cannot be read) is recorded with the session it was given."""
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text("[]")
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 2, out
    (failed,) = env.audit("work")
    assert failed["event"] == "start-failed" and failed["session"] != ""


def test_a_lease_refused_before_any_start_names_no_session(env):
    """A lease refused before a session was started for it (here a collision) names none: no session failed."""
    stray = stray_chrome(env, "work")
    try:
        assert env.run("lease", "work", "--holder", "a")[0] == 6
    finally:
        stray.kill()
        stray.wait()
    assert [(record["event"], record["session"]) for record in env.audit("work")] == [
        ("collision", ""), ("start-failed", ""),
    ]


# Runs the real CLI with a short wait for each call to the service manager, as test_s3_contention's GIVE_UP_DRIVER does
# for LOCK_WAIT, so a call that never answers fails in seconds. services._run reads CALL_TIMEOUT when it runs.
SHORT_CALLS_DRIVER = (
    "import sys\n"
    "import orion_browser.services as services\n"
    "services.CALL_TIMEOUT = 3.0\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def _unit_state(env, browser: str) -> str:
    return subprocess.run(
        ["systemctl", "--user", "is-active", env.unit(browser)], capture_output=True, text=True, env=env.env,
    ).stdout.strip()


@pytest.mark.parametrize(
    "knob, code, said",
    [
        ({"systemd_run_hangs": True}, 6, "gave no answer in 3 s"),
        ({"is_active_hangs": 1}, 6, "gave no answer in 3 s"),
        ({"is_active_no_bus": 1}, 2, "user instance cannot be reached"),
    ],
    ids=["systemd-run-times-out", "systemctl-times-out", "state-unreadable"],
)
def test_a_start_that_ends_in_a_service_manager_failure_stops_what_it_began(env, knob, code, said):
    """A start can end in an error from the service manager once its service runs: systemd-run started the unit and
    never answered, or a look at the unit's state timed out or found no state at all. The start stops what it began
    before the lease is freed, so no session is left running that no lease owns, and that stop is recorded."""
    env.set_fake(start_waits_for="preferences-at-launch-work", **knob)  # the failure comes once Chrome has started
    got, out = _run_through(env, SHORT_CALLS_DRIVER, "lease", "work", "--holder", "a")
    assert got == code and said in out["message"], out
    assert not procs.holding_profile(env.profile("work")) and _unit_state(env, "work") == "inactive"
    assert not env.lease_file("work").exists()
    stop, failed = [record for record in env.audit("work") if record["event"] != "service-stop"]
    assert (stop["event"], failed["event"]) == ("stop", "start-failed")
    assert stop["session"] == failed["session"] != "" and stop["stop"] in ("clean", "signal", "kill")


def test_a_holders_own_lease_refused_before_its_new_start_stays_the_holders(env):
    """A holder's own lease, refused before a new start began (here another config's start of the same profile holds
    the start lock, and the refusal comes at once, without waiting for it), stays the holder's: renewed, as the attempt
    wrote it, and still naming its session, which may still run. So another holder gets BUSY, nothing stops that
    session, and no crash is recorded for it. The failed start's record names no session: the old one did not fail to
    start."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.set_fake(unit_state="deactivating", unit_state_pid=_main_pid(env, "work"))  # it no longer answers
    before = json.loads(env.lease_file("work").read_text())
    with holding_profile_start(env, "work"):  # the other config's start of this profile is under way
        code, out = env.run("lease", "work", "--holder", "a", timeout=20)
    assert code == 6 and "being started by another session" in out["message"], out
    failed = env.audit("work")[-1]
    assert (failed["event"], failed["session"]) == ("start-failed", "")
    kept = json.loads(env.lease_file("work").read_text())
    assert (kept["holder"], kept["session"], kept["endpoint"]) == ("a", first["data"]["session"], before["endpoint"])
    assert kept["expires_at"] > before["expires_at"]  # renewed by the attempt
    code, out = env.run("lease", "work", "--holder", "b")
    assert code == 3 and out["data"]["holder"] == "a", out
    assert procs.holding_profile(env.profile("work"))  # nothing stopped the session
    assert "crashed" not in env.events("work") and "stop" not in env.events("work")


def test_a_takeover_whose_old_session_cannot_be_shown_gone_gives_the_lease_back(env):
    """b's takeover of a's lapsed lease cannot show a's session gone: it fails with BROWSER_DOWN, its record names whose
    lease it was taking, and a's lease goes back as it was. a, whose session still runs and answers and which nobody
    took over, then leases again into that same session."""
    env.set_chrome("work", ignore_close=True)  # a close through DevTools does nothing
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    lapsed = json.loads(env.lease_file("work").read_text())
    env.set_fake(stop_does_nothing=True)  # nor does the service manager's stop
    code, out = _run_through(env, GONE_SOONER_DRIVER, "lease", "work", "--holder", "b")
    assert code == 6 and "could not be shown gone" in out["message"], out
    assert json.loads(env.lease_file("work").read_text()) == lapsed
    assert not _takeovers(env, "work")
    failed = env.audit("work")[-1]
    assert (failed["event"], failed["holder"], failed["session"]) == ("start-failed", "b", "")
    assert (failed["previous_holder"], failed["previous_session"]) == ("a", first["data"]["session"])
    env.set_fake(stop_does_nothing=False)
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["message"].startswith("extended"), again
    assert again["data"]["session"] == first["data"]["session"]
    assert again["data"]["endpoint"] == first["data"]["endpoint"]


def test_a_takeover_refused_before_the_old_session_is_stopped_gives_the_lease_back(env):
    """Another config's start of the same profile holds the start lock: b's takeover of a's lapsed lease is refused at
    once, before it touches a's session, and a's lease goes back as it was."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    lapsed = json.loads(env.lease_file("work").read_text())
    with holding_profile_start(env, "work"):
        code, out = env.run("lease", "work", "--holder", "b", timeout=20)
    assert code == 6 and "being started by another session" in out["message"], out
    assert json.loads(env.lease_file("work").read_text()) == lapsed
    assert procs.holding_profile(env.profile("work")) and not _takeovers(env, "work")  # a's session untouched
    failed = env.audit("work")[-1]
    assert (failed["event"], failed["holder"], failed["session"]) == ("start-failed", "b", "")
    assert (failed["previous_holder"], failed["previous_session"]) == ("a", first["data"]["session"])


def test_a_takeover_refused_after_the_old_session_stopped_frees_the_lease(env):
    """b's takeover stops a's session (a signal is needed: Chrome ignores the close), and then another program takes
    the screen's port before b's start: a collision. a's session was shown gone, so the takeover is on record and stays
    done: the lease b made is freed, and a's is not given back."""
    port = 5900 + SCREENS["work"]
    env.set_chrome("work", ignore_close=True)
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    env.set_fake(stop_then_listen=port)  # once a's session is gone, another program listens on its VNC port
    code, out = env.run("lease", "work", "--holder", "b")
    assert code == 6 and str(port) in out["message"], out
    assert not env.lease_file("work").exists() and not procs.holding_profile(env.profile("work"))
    (takeover,) = _takeovers(env, "work")
    assert (takeover["holder"], takeover["previous_holder"], takeover["stop"]) == ("b", "a", "signal")
    assert takeover["previous_session"] == first["data"]["session"]
    assert [event for event in env.events("work") if event != "service-stop"][-3:] == [
        "takeover", "collision", "start-failed",
    ]


def test_a_failed_start_whose_session_cannot_be_shown_gone_keeps_the_lease(env):
    """A start that fails and cannot show the session it began gone keeps the lease for its holder, as a release
    does: the lease names that session, another holder gets BUSY, and the holder's release stops it once it can. The
    answer says what failed in the start as any failed start does, and why the stop failed, each once."""
    env.set_chrome("work", headless=True, ignore_close=True)  # never healthy, and the DevTools close does nothing
    env.set_fake(stop_does_nothing=True)  # nor does the service manager's stop
    code, out = _run_through(env, GONE_SOONER_DRIVER, "lease", "work", "--holder", "a")
    assert code == 6 and "did not prove healthy" in out["message"] and "lease stays yours" in out["message"], out
    assert out["data"]["failed"] == ["headful"] and out["message"].count("could not be shown gone") == 1, out
    kept = json.loads(env.lease_file("work").read_text())
    (failed,) = env.audit("work")
    assert failed["event"] == "start-failed" and failed["session"] != ""
    assert (kept["holder"], kept["session"], kept["endpoint"]) == ("a", failed["session"], "")
    assert env.run("lease", "work", "--holder", "b")[1]["code"] == "BUSY"
    env.set_fake(stop_does_nothing=False)
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "signal", out
    last = env.audit("work")[-1]
    assert (last["event"], last["session"], last["stop"]) == ("release", failed["session"], "signal")


def test_a_failed_start_that_cannot_read_its_units_state_keeps_the_lease(env):
    """The same when the cleanup's look at the unit finds no user bus: the session may still run, so the lease stays
    (BROWSER_DOWN, which an MCP server gives back when it stops), and no crash is recorded for its later clean stop."""
    env.set_fake(start_waits_for="preferences-at-launch-work", is_active_no_bus=2)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 6 and "lease stays yours" in out["message"], out
    assert json.loads(env.lease_file("work").read_text())["holder"] == "a"
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "clean", out
    assert "crashed" not in env.events("work")


def test_a_start_of_the_profile_under_way_elsewhere_refuses_a_lease_at_once(env):
    """Another config's start of the same profile holds the profile's start lock. A lease meanwhile is refused at once
    with BROWSER_DOWN: it never waits for that start, starts nothing and leaves no lease. Once that start is done, the
    lease goes ahead."""
    with holding_profile_start(env, "work"):  # the other config's start of this profile is under way
        started = time.monotonic()
        code, out = env.run("lease", "work", "--holder", "a", timeout=20)
        took = time.monotonic() - started
    assert code == 6 and "being started by another session" in out["message"], out
    assert took < 5, took  # at once: the refusal takes well under a second here; a wait for the other start shows
    assert env.calls("systemd-run") == [] and not env.lease_file("work").exists()
    assert [(record["event"], record["session"]) for record in env.audit("work")] == [("start-failed", "")]
    assert env.run("lease", "work", "--holder", "a")[0] == 0


def test_the_start_lock_beside_a_profile_reached_through_a_link_leaves_that_folder_as_it_was(env):
    """The start lock sits beside the profile itself. When the profile is a link to a folder elsewhere, the lock is
    made there, owner-only, but that folder is not the tool's, so its mode is left as it was. The configured profiles
    folder, the tool's own, is still made owner-only."""
    elsewhere = env.root / "elsewhere"
    (elsewhere / "work").mkdir(parents=True)
    elsewhere.chmod(0o755)
    env.profile("work").parent.mkdir(parents=True)
    env.profile("work").symlink_to(elsewhere / "work")
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 0, out
    assert stat.S_IMODE(elsewhere.stat().st_mode) == 0o755
    assert stat.S_IMODE((elsewhere / "work.start").stat().st_mode) == 0o600
    assert stat.S_IMODE(env.profile("work").parent.stat().st_mode) == 0o700


def test_dos_a_start_lock_that_cannot_be_made_frees_the_lease_the_attempt_made(env):
    """A start lock that cannot be made (here the profile is a link into a folder the user cannot write) refuses the
    lease with USAGE, a problem in the environment, and the lease the attempt made is freed, as after any refusal
    before the start: it never answers INTERNAL and leaves that lease behind for its whole length."""
    elsewhere = env.root / "elsewhere"
    (elsewhere / "work").mkdir(parents=True)
    env.profile("work").parent.mkdir(parents=True)
    env.profile("work").symlink_to(elsewhere / "work")
    elsewhere.chmod(0o555)  # the profile folder is the operator's to write, the folder holding it is not
    try:
        code, out = env.run("lease", "work", "--holder", "a")
    finally:
        elsewhere.chmod(0o755)
    assert code == 2 and "cannot open the start lock" in out["message"], out
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []
    assert env.events("work") == ["start-failed"]


# Runs the real CLI with flock answering as on a filesystem that has no locks. Only session.py's own fcntl is replaced.
NO_LOCKS_DRIVER = (
    "import errno, sys, types\n"
    "import orion_browser.session as session\n"
    "def flock(fd, operation):\n"
    "    raise OSError(errno.ENOLCK, 'No locks available')\n"
    "session.fcntl = types.SimpleNamespace(flock=flock, LOCK_EX=session.fcntl.LOCK_EX, LOCK_NB=session.fcntl.LOCK_NB)\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def test_a_start_lock_that_cannot_be_locked_frees_the_lease_the_attempt_made(env):
    """On a filesystem that cannot lock the start lock, the lease is refused with USAGE and the lease the attempt made
    is freed: no INTERNAL error, no lease left behind."""
    code, out = _run_through(env, NO_LOCKS_DRIVER, "lease", "work", "--holder", "a")
    assert code == 2 and "cannot lock the start lock" in out["message"], out
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []


def test_d5b_a_crash_after_a_stop_that_could_not_show_the_session_gone_is_recorded(env):
    """A stop that cannot show its session gone takes back its word that the session is closing: if that session runs
    on and its Chrome later dies on its own, the death is recorded as a crash, as any other."""
    env.set_chrome("work", ignore_close=True)
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    env.set_fake(stop_does_nothing=True)
    code, out = _run_through(env, GONE_SOONER_DRIVER, "lease", "work", "--holder", "b")
    assert code == 6 and "could not be shown gone" in out["message"], out
    env.set_fake(stop_does_nothing=False)
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["message"].startswith("extended"), again
    (chrome,) = procs.holding_profile(env.profile("work"))
    os.kill(chrome["pid"], signal.SIGKILL)  # a's Chrome dies on its own, long after the failed stop
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)
    assert "crashed" in env.events("work"), env.events("work")


def test_a_stop_that_fails_partway_also_takes_back_its_word(env):
    """The same when the stop fails partway, before its gone proof can decide: here the unit's state cannot be read
    once the manager's stop was asked. The session runs on, and a death of its own is still recorded as a crash."""
    env.set_chrome("work", ignore_close=True)
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    env.set_fake(stop_does_nothing=True, is_active_no_bus_after_stop=1)
    code, out = env.run("lease", "work", "--holder", "b")
    assert code == 2 and "user instance cannot be reached" in out["message"], out
    env.set_fake(stop_does_nothing=False)
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["message"].startswith("extended"), again
    (chrome,) = procs.holding_profile(env.profile("work"))
    os.kill(chrome["pid"], signal.SIGKILL)
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)
    assert "crashed" in env.events("work"), env.events("work")


def test_a_release_refused_for_a_stray_names_the_processes_its_one_look_found(env, monkeypatch):
    """A release refused because a process the tool did not start has the profile open names what its one look at the
    profile found: never what a second look, which may see something else, finds. (In-process, to make the second look
    differ: the first finds a stray, any later one finds it gone.)"""
    for key in NOT_INHERITED:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    env.write_lease("work", holder="a", session="s1")
    found = [{"pid": 4242, "command": "a Chrome orion-browser did not start"}]
    looks = iter([found])
    monkeypatch.setattr(services, "is_active", lambda unit: False)  # no session of the tool's runs
    monkeypatch.setattr(procs, "holding_profile", lambda profile: next(looks, []))
    with pytest.raises(BrowserError) as refused:
        session.Pool(config.load()).release("work", "a")
    assert refused.value.code is Code.BROWSER_DOWN and refused.value.data["processes"] == found
    assert env.lease_file("work").exists()  # the lease stays the holder's


def test_another_configs_lease_during_a_start_is_refused_by_the_start_lock(env):
    """Another config's lease of the same profile while this start still proves its session healthy is refused by the
    start lock: the lock covers the whole start, the moments before Chrome opens the profile included."""
    env.rewrite_config("start_limit_seconds = 5", "start_limit_seconds = 15")
    text = (env.root / "config.toml").read_text().replace(f'"{env.root}/state"', f'"{env.root}/state2"')
    second = env.root / "second.toml"
    second.write_text(text.replace(f"screen = {SCREENS['work']}", "screen = 92"))
    env.set_chrome("work", stall_version=4)  # the first start's health proof waits on DevTools
    first = env.popen("lease", "work", "--holder", "a")
    env.wait_until(lambda: env.calls("chrome"))  # its Chrome is up; its start goes on
    other = subprocess.run(
        [sys.executable, "-m", "orion_browser", "--json", "lease", "work", "--holder", "b"],
        capture_output=True, text=True, env={**env.env, "ORION_BROWSER_CONFIG": str(second)}, timeout=60,
    )
    out = json.loads(other.stdout)
    assert out["exit"] == 6 and "being started by another session" in out["message"], out
    assert env.result(first)[0] == 0


def test_a_takeover_of_a_session_already_ended_that_meets_a_collision_frees_the_lease_it_made(env):
    """a's lease lapsed with its service already inactive. b's takeover finds the old session not running (recorded),
    then meets a Chrome the tool did not start on the profile: BROWSER_DOWN naming it, and the lease b's attempt made
    is freed (the takeover is done, so nothing goes back to a), never left blocking other holders until it expires.
    The other Chrome is left alone."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    subprocess.run(["systemctl", "--user", "stop", env.unit("work")], env=env.env, check=True)
    env.age_lease("work", 60)
    stray = stray_chrome(env, "work")
    try:
        code, out = env.run("lease", "work", "--holder", "b")
        assert code == 6 and f"pid {stray.pid}" in out["message"], out
        assert not env.lease_file("work").exists()
        assert stray.poll() is None
        events = env.events("work")
        assert events[-3:] == ["takeover", "collision", "start-failed"], events
    finally:
        stray.kill()
        stray.wait()


def test_a_collision_never_takes_away_the_holders_own_lease(env):
    """A's session crashed, and a process the tool did not start has the profile open. A's lease attempt is refused
    as a collision before its start begins, and its own lease stays: renewed, still naming its session, BUSY for
    others."""
    env.set_chrome("work", crash_after=2.0)
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)
    env.set_chrome("work", crash_after=None)
    before = json.loads(env.lease_file("work").read_text())
    stray = stray_chrome(env, "work")
    try:
        code, out = env.run("lease", "work", "--holder", "a")
        assert code == 6 and f"pid {stray.pid}" in out["message"], out
        kept = json.loads(env.lease_file("work").read_text())
        assert (kept["holder"], kept["session"]) == ("a", first["data"]["session"])
        assert kept["expires_at"] > before["expires_at"]
        assert env.run("lease", "work", "--holder", "b")[1]["code"] == "BUSY"
    finally:
        stray.kill()
        stray.wait()
    assert env.audit("work")[-1]["event"] == "start-failed" and env.audit("work")[-1]["session"] == ""


def test_a_takeover_of_a_session_that_already_ended_is_recorded(env):
    """A takeover is recorded once the old session is stopped or found not running: here a's session had crashed,
    so the record says its stop was none."""
    env.set_chrome("work", crash_after=2.0)
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)
    env.set_chrome("work", crash_after=None)
    env.age_lease("work", 3600)
    code, out = env.run("lease", "work", "--holder", "b")
    assert code == 0, out
    (takeover,) = _takeovers(env, "work")
    assert (takeover["previous_holder"], takeover["previous_session"], takeover["stop"]) == (
        "a", first["data"]["session"], "none",
    )


def test_a_holders_failed_new_start_frees_its_own_lease(env):
    """The holder's own session no longer answers, so its lease starts a new one, which fails and is shown gone: the
    start began, so the lease is freed, as after any failed start."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.set_fake(unit_state="deactivating", unit_state_pid=_main_pid(env, "work"))  # it no longer answers
    env.set_chrome("work", headless=True)  # the new session never proves healthy
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 7, out
    assert not env.lease_file("work").exists()


def test_a_view_port_another_program_holds_is_refused(env):
    """The screen's view port counts as its own: a program on it refuses the lease, and nothing is started."""
    port = 6080 + SCREENS["work"]
    other = socket.create_server((LOOPBACK, port))
    try:
        code, out = env.run("lease", "work", "--holder", "a")
        assert code == 6 and str(port) in out["message"], out
        assert env.calls("Xvnc") == []
    finally:
        other.close()


def test_a_link_under_another_browser_name_shares_the_start_lock(env):
    """Another config names its browser differently, and its profile is a link to this one's. The start lock is named
    for the profile the link reaches, not for the browser, so their two leases at the same moment never both start:
    one gets it, the other is refused, and only one Chrome ever opens the profile."""
    env.profile("work").mkdir(parents=True, exist_ok=True)
    (env.root / "profiles2").mkdir()
    (env.root / "profiles2" / "work").symlink_to(env.profile("work"))
    text = (env.root / "config.toml").read_text().replace(f'"{env.root}/state"', f'"{env.root}/state2"')
    text = text.replace(f"screen = {SCREENS['work']}", "screen = 92").replace('name = "work"', 'name = "work"')
    second = env.root / "second.toml"
    second.write_text(text.replace(f'"{env.root}/profiles"', f'"{env.root}/profiles2"'))
    first = env.popen("lease", "work", "--holder", "a")
    other = subprocess.Popen(
        [sys.executable, "-m", "orion_browser", "--json", "lease", "work", "--holder", "b"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**env.env, "ORION_BROWSER_CONFIG": str(second)},
    )
    assert sorted([env.result(first)[0], env.result(other)[0]]) == [0, 6]
    assert len(env.calls("chrome")) == 1


def test_a_state_unreadable_while_chrome_takes_the_close_records_no_crash(env):
    """A release closes Chrome through DevTools and Chrome takes the close, but the first look at the unit's state after
    it finds no user bus: the release fails (USAGE) and keeps the lease. Chrome's exit was the tool's close, so no crash
    is recorded, and the next release finds the session gone. Chrome exits a second after it takes the close, so the
    failed look comes well before that exit even under load: a release that took the closing marker back on that
    failure would have it gone by the time the session sees Chrome exit, and a crash would be recorded."""
    env.set_chrome("work", close_delay=1)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(is_active_no_bus_while_closing=str(env.root / "state" / "sessions" / "work.closing"))
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 2 and "cannot be reached" in out["message"], out
    env.wait_until(lambda: env.run("status", "work")[0] == 6, timeout=20)  # the session ended: Chrome took the close
    assert "crashed" not in env.events("work"), env.events("work")
    assert env.run("release", "work", "--holder", "a")[0] == 0


def test_a_takeovers_failed_start_that_keeps_the_lease_names_the_previous_holder(env):
    """b takes over a's lapsed lease, and b's new start fails and cannot show what it began gone, so b keeps the lease
    (LeaseKept). Its failed start's record still names whose lease it took, as every failed start of a takeover does."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    env.set_chrome("work", headless=True, ignore_close=True)  # b's new session never proves healthy, ignores the close
    env.set_fake(stop_does_nothing=True)  # a's session closes through DevTools; b's cleanup needs the manager's stop
    code, out = _run_through(env, GONE_SOONER_DRIVER, "lease", "work", "--holder", "b")
    assert code == 6 and "lease stays yours" in out["message"], out
    failed = env.audit("work")[-1]
    assert failed["event"] == "start-failed" and failed["session"] != ""
    assert (failed.get("previous_holder"), failed.get("previous_session")) == ("a", first["data"]["session"])


# Runs the real CLI with one step of a lease attempt (a Pool method, named by argv[1]) replaced by a Ctrl-C: the step
# sends SIGINT to its own process, as a terminal does, and the interrupt lands while it waits.
INTERRUPT_DRIVER = (
    "import os, signal, sys, time\n"
    "import orion_browser.session as session\n"
    "def interrupted(*args, **kwargs):\n"
    "    os.kill(os.getpid(), signal.SIGINT)\n"
    "    time.sleep(30)\n"
    "    raise AssertionError('the SIGINT never landed')\n"
    "setattr(session.Pool, sys.argv[1], interrupted)\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[2:]))\n"
)


def _interrupted_at(env, step: str, *args: str) -> None:
    """Run a lease command that a Ctrl-C ends at the given step, and check that the interrupt is what ended it."""
    proc = subprocess.run(
        [sys.executable, "-P", "-c", INTERRUPT_DRIVER, step, "--json", *args],
        capture_output=True, text=True, env=env.env, timeout=60,
    )
    assert proc.returncode == -signal.SIGINT and "KeyboardInterrupt" in proc.stderr, (proc.stdout, proc.stderr)


def test_a_lease_interrupted_before_its_start_frees_the_lease_it_made(env):
    """A Ctrl-C before the start began (here at the collision check) frees the lease the attempt made, as a refusal
    there does: it never leaves a fresh lease until its expiry with nothing started."""
    _interrupted_at(env, "_refuse_collisions", "lease", "work", "--holder", "a")
    assert not env.lease_file("work").exists()
    assert env.calls("systemd-run") == []
    assert env.run("lease", "work", "--holder", "b")[0] == 0  # another holder is not kept waiting for an expiry


def test_a_takeover_interrupted_during_the_old_stop_gives_the_lease_back(env):
    """A Ctrl-C while a takeover stops the old session, before it is shown gone: the previous holder's lease goes back
    as it was, as when that stop fails, and no takeover is on record. The taker's lease never stays behind."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.age_lease("work", 3600)
    lapsed = json.loads(env.lease_file("work").read_text())
    _interrupted_at(env, "_stop", "lease", "work", "--holder", "b")
    assert json.loads(env.lease_file("work").read_text()) == lapsed
    assert not _takeovers(env, "work")


def test_a_holders_own_lease_interrupted_before_its_new_start_stays_the_holders(env):
    """A Ctrl-C while the holder's own session, which no longer answers, is stopped: the lease is the holder's own, so
    it stays, renewed and still naming its session, which may still run."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.set_fake(unit_state="deactivating", unit_state_pid=_main_pid(env, "work"))  # it no longer answers
    _interrupted_at(env, "_stop", "lease", "work", "--holder", "a")
    kept = json.loads(env.lease_file("work").read_text())
    assert (kept["holder"], kept["session"]) == ("a", first["data"]["session"])
    assert kept["endpoint"] == first["data"]["endpoint"]


def test_a_lease_interrupted_once_its_start_began_keeps_the_lease_naming_the_session(env):
    """A Ctrl-C once the start began (here while it waits for the session to prove healthy): the session may run, so
    the lease stays, naming the session the start began, until a release or a takeover shows it gone."""
    _interrupted_at(env, "_await_healthy", "lease", "work", "--holder", "a")
    kept = json.loads(env.lease_file("work").read_text())
    assert kept["holder"] == "a" and kept["session"] != "" and kept["endpoint"] == ""
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0, out


# Runs the real CLI with a Ctrl-C inside the start, before its service is asked to start (argv[1] names where): as the
# profile's preferences are written ("prefs.seed"), or as the stale closing marker is removed ("stale-marker").
INTERRUPT_BEFORE_THE_SERVICE_DRIVER = (
    "import os, signal, sys, time\n"
    "from pathlib import Path\n"
    "import orion_browser.prefs as prefs\n"
    "def interrupted(*args, **kwargs):\n"
    "    os.kill(os.getpid(), signal.SIGINT)\n"
    "    time.sleep(30)\n"
    "    raise AssertionError('the SIGINT never landed')\n"
    "if sys.argv[1] == 'prefs.seed':\n"
    "    prefs.seed = interrupted\n"
    "else:\n"
    "    real_unlink = Path.unlink\n"
    "    def unlink(self, *args, **kwargs):\n"
    "        if self.name.endswith('.closing'):\n"
    "            interrupted()\n"
    "        return real_unlink(self, *args, **kwargs)\n"
    "    Path.unlink = unlink\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[2:]))\n"
)


@pytest.mark.parametrize("where", ["prefs.seed", "stale-marker"])
def test_a_lease_interrupted_in_its_start_before_the_service_frees_the_lease_it_made(env, where):
    """A Ctrl-C once the start began but before its session's service was asked to start (the session recorded on the
    lease, the preferences being written, the stale files being removed): nothing runs, so the lease the attempt made
    is freed, as a failed start there frees it, and never left until its expiry."""
    argv = [sys.executable, "-P", "-c", INTERRUPT_BEFORE_THE_SERVICE_DRIVER, where, "--json", "lease", "work"]
    proc = subprocess.run([*argv, "--holder", "a"], capture_output=True, text=True, env=env.env, timeout=60)
    assert proc.returncode == -signal.SIGINT and "KeyboardInterrupt" in proc.stderr, (proc.stdout, proc.stderr)
    assert env.calls("systemd-run") == []
    assert not env.lease_file("work").exists()
    assert env.run("lease", "work", "--holder", "b")[0] == 0  # another holder is not kept waiting for an expiry


@pytest.mark.parametrize("stale", ["DevToolsActivePort", "work.closing", "work.stop", "work.start-failed"])
def test_a_stale_file_that_cannot_be_removed_refuses_the_start_and_frees_the_lease(env, stale):
    """A start removes the stale port file and markers before its service starts. One that cannot be removed (here a
    folder in its place) refuses the start with USAGE naming the path, a problem in the environment, never INTERNAL:
    nothing was started, so the failed start frees the lease."""
    folder = env.profile("work") if stale == "DevToolsActivePort" else env.root / "state" / "sessions"
    (folder / stale / "inside").mkdir(parents=True)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 2 and str(folder / stale) in out["message"], out
    assert out["data"]["path"] == str(folder / stale)
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []
    (failed,) = env.audit("work")
    assert (failed["event"], failed["code"]) == ("start-failed", "USAGE") and failed["session"] != ""


def test_a_stale_marker_in_a_folder_the_user_cannot_write_refuses_the_start(env):
    """A stale marker that is a file, in a folder the user cannot write, cannot be removed either (EACCES, not a folder
    in its place): the start is refused with USAGE naming it, and the lease is freed."""
    sessions = env.root / "state" / "sessions"
    sessions.mkdir(parents=True)
    (sessions / "work.closing").write_text("old")
    sessions.chmod(0o500)
    try:
        code, out = env.run("lease", "work", "--holder", "a")
    finally:
        sessions.chmod(0o700)
    assert code == 2 and out["data"]["path"] == str(sessions / "work.closing"), out
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []


def _contents(folder: Path) -> list[tuple[str, int]]:
    """Every path under the folder with its mode: a file made there, or a mode changed, shows."""
    return sorted((str(path.relative_to(folder)), path.lstat().st_mode) for path in [folder, *folder.rglob("*")])


@pytest.mark.parametrize("git", ["folder", "pointer"])
@pytest.mark.parametrize("linked", ["profile", "downloads"])
def test_a_profile_or_download_folder_linked_into_a_git_tree_refuses_the_start(env, linked, git):
    """A browser's profile, or its download folder, can be a link that leads out of the folders the config checks. One
    that leads into a git work tree (a non-empty .git folder, or a .git pointer file as a linked worktree has) refuses
    the start with USAGE naming it and where it leads, before anything is written there or in the profile: no start
    lock, no preferences, no mode narrowed. The lease the attempt made is freed."""
    repo = env.git_work_tree(git)
    (repo / "target").mkdir()
    before = _contents(repo)
    link = env.profile("work") if linked == "profile" else env.root / "state" / "downloads" / "work"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(repo / "target")
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 2 and "inside a git working tree" in out["message"], out
    assert str(link) in out["message"] and str(repo / "target") in out["message"], out
    assert _contents(repo) == before  # nothing written in the work tree
    assert not (env.profile("work") / "Default" / "Preferences").exists()
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []
    assert [(record["event"], record["code"], record["session"]) for record in env.audit("work")] == [
        ("start-failed", "USAGE", ""),
    ]
