"""S1: one browser, proven healthy: lease, status, release."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from typing import Iterator

import pytest
from conftest import LOOPBACK, SCREENS

from orion_browser import config, devtools, procs, session
from orion_browser.codes import BrowserError, Code

RECORD_KEYS = {
    "ts", "browser", "holder", "session", "event", "expires_at", "stop", "previous_holder", "previous_session",
    "code", "reason", "pids", "ports", "exit_code",
}
EVERY_CHECK = ["service", "endpoint", "visible", "focused", "webdriver", "headful", "screen"]
NO_TAB = ["visible", "focused", "webdriver"]  # the checks judged on the visible tab: a session with none fails all
IN_PROCESS_LIMIT = 60  # seconds an in-process start may take before its test fails (a CLI run fails at 120 s)


def test_lease_status_release_round_trip(env):
    code, out = env.run("lease", "anon", "--holder", "agent-a")
    assert code == 0, out
    data = out["data"]
    assert data["endpoint"].startswith(f"ws://{LOOPBACK}:{data['port']}/devtools/browser/")
    assert data["holder"] == "agent-a" and len(data["session"]) == 12
    assert data["watch"] == f"http://{LOOPBACK}:{6080 + SCREENS['anon']}/vnc.html?autoconnect=1&resize=scale&view_only=1"

    code, status = env.run("status", "anon")
    assert code == 0 and status["data"]["state"] == "healthy" and status["data"]["failed"] == []
    assert data["endpoint"] not in json.dumps(status)  # only lease hands out an endpoint

    code, listing = env.run("status")
    rows = {row["browser"]: row for row in listing["data"]["browsers"]}
    assert (rows["anon"]["state"], rows["anon"]["holder"], rows["work"]["state"]) == ("healthy", "agent-a", "down")
    assert data["endpoint"] not in json.dumps(listing)

    code, out = env.run("release", "anon", "--holder", "agent-a")
    assert code == 0 and out["data"]["stop"] == "clean"
    assert (env.profile("anon") / "Cookies.fake").read_text() == "saved"  # it was closed through DevTools
    code, status = env.run("status", "anon")
    assert code == 6 and status["data"]["state"] == "down"
    assert env.events("anon") == ["lease", "release"]
    for record in env.audit("anon"):
        assert set(record) <= RECORD_KEYS, record


def test_the_session_runs_chrome_on_its_own_screen_and_focuses_it(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    chrome = env.calls("chrome")[-1]
    assert f"--user-data-dir={env.profile('work')}" in chrome and "--remote-debugging-port=0" in chrome
    assert "--window-size=800,600" in chrome
    assert "--ozone-platform=x11" in chrome  # never a Wayland window on the operator's desktop (WSLg)
    screen = env.calls("Xvnc")[-1]
    assert screen[0] == f":{SCREENS['work']}" and "-br" in screen
    assert screen[screen.index("-interface") + 1] == LOOPBACK
    assert any(call[0] == "windowfocus" for call in env.calls("xdotool"))
    started = env.calls("systemd-run")[-1]
    assert f"--unit={env.unit('work')}" in started and "--property=KillMode=mixed" in started


def test_an_outdated_chromes_update_bubble_never_takes_the_pages_focus(env):
    """An outdated Chrome (as the real one was at the first smoke test, and the fake is by default) opens a "Can't
    update Chrome" bubble as it starts, which keeps the keyboard focus from the page, so every start failed its focus
    check. The session starts Chrome with that check turned off: the page has the focus and the lease is healthy."""
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 0, out
    assert "--disable-features=OutdatedBuildDetector" in env.calls("chrome")[-1]
    code, status = env.run("status", "work")
    assert code == 0 and status["data"]["failed"] == [], status


def test_chrome_starts_on_a_page_that_takes_the_focus(env):
    """Chrome started on about:blank, or on no page at all (its New Tab page), keeps the keyboard focus in its address
    bar (as the fake does, after the real one), so its page never has the focus, however its window is given it. The
    session starts Chrome on an empty data: page, which Chrome focuses once the window has the focus."""
    code, out = env.run("lease", "anon", "--holder", "a")
    assert code == 0, out
    assert env.calls("chrome")[-1][-1] != "about:blank"
    code, status = env.run("status", "anon")
    assert code == 0 and status["data"]["failed"] == [], status


MARKED_AUTOMATED = (
    "the page does not report navigator.webdriver false: Chrome marks this browser as automated, and Google refuses "
    "sign-in in it"
)


def test_chrome_runs_with_its_automation_marker_off(env):
    """Since Chrome 155 a DevTools port alone makes every page report navigator.webdriver true, and Google refuses a
    sign-in in such a browser (spec section 13, Q3). Every session turns the marker off, so its page reports false and
    the health proof passes (the fake follows Chrome 155's rule)."""
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 0, out
    argv = env.calls("chrome")[-1]
    # Exactly one list, naming it: how Chrome treats repeated --disable-blink-features lists is not ours to rely on.
    lists = [arg for arg in argv if arg.startswith("--disable-blink-features=")]
    assert lists == ["--disable-blink-features=AutomationControlled"], argv
    code, status = env.run("status", "work")
    assert code == 0 and status["data"]["failed"] == [], status


def test_a_chrome_that_marks_itself_automated_fails_the_start_naming_webdriver(env):
    """A Chrome that no longer honors the flag fails the proof's webdriver check: the start fails as unhealthy, names
    the check with a note in fixed words, frees the lease and stops the session, so no sign-in is tried in it."""
    env.set_chrome("work", ignores_automation_flag=True)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 7 and out["data"]["failed"] == ["webdriver"], out
    assert out["data"]["notes"]["webdriver"] == MARKED_AUTOMATED, out
    assert not env.lease_file("work").exists()
    assert "start-failed" in env.events("work")


def test_a_free_browser_is_down(env):
    code, out = env.run("status", "work")
    assert code == 6 and out["data"]["state"] == "down" and out["data"]["holder"] is None


@pytest.mark.parametrize(
    "fake, chrome, failed",
    [
        ({}, {"headless": True}, ["headful"]),
        ({}, {"visible": False}, ["visible", "focused", "webdriver"]),
        ({"xdotool_no_window": True}, {}, ["focused"]),
        ({"screen_blank": True}, {}, ["screen"]),
    ],
)
def test_a_failed_check_is_named_and_frees_the_lease(env, fake, chrome, failed):
    env.set_fake(**fake)
    env.set_chrome("work", **chrome)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 7 and out["data"]["failed"] == failed, out
    assert not env.lease_file("work").exists()
    assert "start-failed" in env.events("work")
    assert env.run("status", "work")[1]["data"]["state"] == "down"  # everything the start began was stopped


def test_status_reports_a_start_in_progress(env):
    env.write_lease("work", holder="a", session="s1")
    subprocess.run(
        ["systemd-run", "--user", f"--unit={env.unit('work')}", "--", "/bin/sleep", "30"], env=env.env, check=True
    )
    code, out = env.run("status", "work")
    assert code == 7 and out["data"]["state"] == "starting"


def test_status_names_a_failed_check_of_a_running_session(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(screen_blank=True)  # fake xwd reads its settings on every capture
    code, out = env.run("status", "work")
    assert code == 7 and out["data"]["state"] == "unhealthy" and out["data"]["failed"] == ["screen"], out


def _lose_focus(env) -> None:
    (env.root / "fake" / f"focus-:{SCREENS['work']}").unlink()  # fake Chrome reads its focus on every query


def _date_port_file_before_the_start(env) -> None:
    started = json.loads(env.lease_file("work").read_text())["started_at"]
    stale = started - 10  # well past the slack the proof allows for coarse file clocks
    os.utime(env.profile("work") / "DevToolsActivePort", (stale, stale))


# Each case breaks one thing in a session that was proven healthy, and names the checks that must then fail.
ONE_CHECK = {
    "endpoint": (lambda env: env.set_chrome("work", wrong_endpoint=True), ["endpoint"]),
    "no-visible-tab": (lambda env: env.set_chrome("work", visible=False), ["visible", "focused", "webdriver"]),
    "unparsable-page-answer": (lambda env: env.set_chrome("work", deep_state=True), ["visible", "focused", "webdriver"]),
    "not-focused": (_lose_focus, ["focused"]),
    "headless": (lambda env: env.set_chrome("work", headless=True), ["headful"]),
    "empty-user-agent": (lambda env: env.set_chrome("work", user_agent=""), ["headful"]),
    "xwd-fails": (lambda env: env.set_fake(xwd_fails=True), ["screen"]),
    "not-a-capture": (lambda env: env.set_fake(xwd_not_a_capture=True), ["screen"]),
    "stale-port-file": (
        _date_port_file_before_the_start, ["endpoint", "visible", "focused", "webdriver", "headful", "screen"]
    ),
    # Unexpected shapes from the state query show nothing. Focus must be a real true, never merely truthy.
    "focus-not-true": (lambda env: env.set_chrome("work", page_answer=["visible", 1, False]), ["focused"]),
    "answer-not-json": (lambda env: env.set_chrome("work", page_answer="visible, focused"), NO_TAB),
    "answer-one-item": (lambda env: env.set_chrome("work", page_answer=["visible"]), NO_TAB),
    "answer-not-a-list": (
        lambda env: env.set_chrome("work", page_answer={"0": "visible", "1": True, "2": False}), NO_TAB
    ),
    "marked-automated": (lambda env: env.set_chrome("work", ignores_automation_flag=True), ["webdriver"]),
    # webdriver passes only on a real false: null (no such property) or a falsy 0 is not proof.
    "webdriver-null": (
        lambda env: env.set_chrome("work", page_answer=["visible", True, None]), ["webdriver"]
    ),
    "webdriver-falsy-not-false": (
        lambda env: env.set_chrome("work", page_answer=["visible", True, 0]), ["webdriver"]
    ),
    "answer-old-pair": (
        lambda env: env.set_chrome("work", page_answer=["visible", True]), NO_TAB
    ),
    "answer-serialized-text": (lambda env: env.set_chrome("work", page_answer=json.dumps(["visible", True, False])), NO_TAB),
}


@pytest.mark.parametrize("case", ONE_CHECK)
def test_each_check_fails_on_its_own(env, case):
    """Through status, after a healthy lease. Focus and webdriver are judged on the visible tab, so a session with none
    (or whose page answers with something that cannot be parsed) fails all three; a port file from before the session
    leaves nothing to ask, so it fails every check after the service."""
    break_it, failed = ONE_CHECK[case]
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    break_it(env)
    code, out = env.run("status", "work")
    assert code == 7 and out["data"]["state"] == "unhealthy" and out["data"]["failed"] == failed, out


def test_a_session_whose_service_is_not_active_fails_every_check(env):
    """The service check comes first and cannot fail on its own: without an active service there is no session to
    judge, so no other check can pass. Only a start can show it (status calls such a browser down)."""
    env.set_fake(xvnc_fails=True)  # the session's service ends at once: its screen never comes up
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 6 and out["data"]["failed"] == EVERY_CHECK, out
    assert not env.lease_file("work").exists()


def test_status_finds_the_focused_page_after_another_visible_window(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_chrome("work", page_answers=[
        ["visible", False, True],
        ["visible", True, False],
    ])
    code, out = env.run("status", "work")
    assert code == 0 and out["data"]["state"] == "healthy", out
    assert out["data"]["failed"] == []


def test_page_stringify_override_cannot_forge_a_healthy_status(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_chrome("work", stringify_spoof=True, ignores_automation_flag=True)
    code, out = env.run("status", "work")
    assert code == 7 and out["data"]["failed"] == ["webdriver"], out


def test_status_checks_remaining_pages_after_a_target_closes(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_chrome("work", page_answers=[
        ["visible", False, False], ["visible", True, False],
    ], page_errors=[0])
    code, out = env.run("status", "work")
    assert code == 0 and out["data"]["state"] == "healthy", out
    env.set_chrome("work", page_errors=[0, 1])
    code, out = env.run("status", "work")
    assert code == 7 and out["data"]["failed"] == NO_TAB, out


def test_a_page_answer_that_cannot_be_parsed_fails_the_start_cleanly(env):
    """An unusable answer to the state query is a failed check like any other: the start fails as unhealthy, frees the lease and stops the session (S4), and never
    ends in an internal error that leaves the session running."""
    env.set_chrome("work", deep_state=True)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 7 and out["data"]["failed"] == ["visible", "focused", "webdriver"], out
    assert not env.lease_file("work").exists()
    assert "start-failed" in env.events("work")
    assert env.run("status", "work")[1]["data"]["state"] == "down"  # the service was stopped


def test_status_never_shows_the_port_or_the_endpoint(env):
    """status answers anyone, holder or not, and the port alone gives control of the signed-in browser: not even a
    DevTools answer that never comes may bring it into a note."""
    code, granted = env.run("lease", "work", "--holder", "a")
    assert code == 0, granted
    port, endpoint = granted["data"]["port"], granted["data"]["endpoint"]
    env.set_chrome("work", stall_version=60)  # longer than any DevTools timeout
    code, out = env.run("status", "work")
    assert code == 7 and "endpoint" in out["data"]["failed"], out
    text = json.dumps(out)
    assert endpoint not in text and endpoint.rsplit("/", 1)[1] not in text, text
    assert not re.search(rf"(?<!\d){port}(?!\d)", text), text


def test_a_service_manager_that_cannot_be_reached_is_never_taken_for_a_stopped_session(env):
    """With no user bus, systemctl --user reports no state at all. Read as "stopped", a release would skip the stop and
    free a browser whose Chrome still runs. Every command that needs the state refuses instead, and nothing changes."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(no_bus=True)
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 2 and "user instance cannot be reached" in out["message"], out
    assert env.lease_file("work").exists() and procs.holding_profile(env.profile("work"))  # kept, and nothing stopped
    for args in (("status", "work"), ("status",), ("lease", "work", "--holder", "a"), ("lease", "anon", "--holder", "b")):
        code, out = env.run(*args)
        assert code == 2 and "user instance cannot be reached" in out["message"], (args, out)
    assert not env.lease_file("anon").exists()  # a lease that could not start is not kept
    env.set_fake(no_bus=False)
    assert env.run("release", "work", "--holder", "a")[0] == 0


# Runs the real CLI with the folder that holds each user's runtime folder (/run/user) moved to a test folder, as
# test_s3_contention's GIVE_UP_DRIVER moves LOCK_WAIT. services._run reads RUNTIME_BASE when it runs.
RUNTIME_BASE_DRIVER = (
    "import sys\n"
    "import orion_browser.services as services\n"
    "services.RUNTIME_BASE = sys.argv.pop(1)\n"
    "from orion_browser.cli import main\n"
    "raise SystemExit(main(sys.argv[1:]))\n"
)


def status_as_codex_starts_it(env, base) -> tuple[int, dict]:
    """The real CLI's `status`, started as Codex starts an MCP server: XDG_RUNTIME_DIR is not in its environment."""
    env.env.pop("XDG_RUNTIME_DIR", None)
    proc = subprocess.run(
        [sys.executable, "-P", "-c", RUNTIME_BASE_DRIVER, str(base), "--json", "status"],
        capture_output=True, text=True, env=env.env, timeout=120,
    )
    return env._answer(proc.returncode, proc.stdout, proc.stderr)


def test_a_launcher_that_leaves_out_the_runtime_dir_still_reaches_the_users_service_manager(env, tmp_path):
    """Codex starts an MCP server with only HOME, LANG, LOGNAME, PATH, SHELL, TERM and USER. Without XDG_RUNTIME_DIR,
    `systemctl --user` cannot find the user's manager and every call answered USAGE. A call defaults the variable to
    /run/user/<uid> when that folder is the user's own."""
    base = tmp_path / "run" / "user"
    runtime = base / str(os.getuid())
    runtime.mkdir(parents=True)
    env.set_fake(bus_runtime_dir=str(runtime))  # the user's manager is found only through this folder
    code, out = status_as_codex_starts_it(env, base)
    assert code == 0 and [row["browser"] for row in out["data"]["browsers"]] == ["anon", "work"], out


@pytest.mark.parametrize("folder", ["missing", "someone-elses"])
def test_a_runtime_folder_that_is_missing_or_not_the_users_is_never_taken(env, tmp_path, folder):
    """The default is no guess: no folder, or a folder that belongs to someone else, leaves the variable unset, and the
    calls answer USAGE as before."""
    base = tmp_path / "run" / "user"
    runtime = base / str(os.getuid())
    base.mkdir(parents=True)
    if folder == "someone-elses":
        if os.stat("/").st_uid == os.getuid():
            pytest.skip("this user owns /, so no folder it can name is someone else's")
        runtime.symlink_to("/")  # a folder that exists and is not the user's
    env.set_fake(bus_runtime_dir=str(runtime))
    code, out = status_as_codex_starts_it(env, base)
    assert code == 2 and "user instance cannot be reached" in out["message"], out


@pytest.mark.parametrize("state", ["activating", "deactivating", "reloading"])
def test_a_unit_that_is_not_exactly_active_fails_the_service_check(env, state):
    """Only an active unit holds a healthy session. A unit in any other state may still run a process, so status never
    calls it down: it is unhealthy, and with no active service nothing else can be judged."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(unit_state=state)
    code, out = env.run("status", "work")
    assert code == 7 and out["data"]["state"] == "unhealthy" and out["data"]["failed"] == EVERY_CHECK, out


@pytest.mark.parametrize("state", ["deactivating", "maintenance"])
def test_a_release_stops_a_session_in_any_state_but_plainly_gone(env, state):
    """The gone proof's reading: a unit in any state but inactive or failed may still run a process, so a release stops
    the session and shows it gone before it frees the lease. It never takes such a state for a stopped session."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.set_fake(unit_state=state)  # what the unit reports while its main process runs
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "clean", out
    assert not procs.holding_profile(env.profile("work")) and not env.lease_file("work").exists()


LIMIT = 3  # seconds: the start limit the next test sets
MARGIN = 2.5  # seconds a failed start may take past its limit: stopping what it started (1 s stop grace), the CLI


def test_a_start_answers_within_its_limit_when_devtools_and_the_capture_stall(env):
    """No proof starts once the start limit is reached, and each DevTools call and capture gets only the time left, so
    a start whose DevTools answer and screen capture never come still answers within the limit and a little more, and
    fails as any start that is not proven healthy: UNHEALTHY, the lease freed, the session stopped."""
    env.rewrite_config("start_limit_seconds = 5", f"start_limit_seconds = {LIMIT}")
    env.set_chrome("work", stall_version=60)
    env.set_fake(xwd_stall=60)
    began = time.monotonic()
    code, out = env.run("lease", "work", "--holder", "a")
    took = time.monotonic() - began
    failed = ["endpoint", "visible", "focused", "webdriver", "headful", "screen"]
    assert code == 7 and out["data"]["failed"] == failed, out
    assert took < LIMIT + MARGIN, f"the start answered after {took:.1f} s against a {LIMIT} s limit"
    assert not env.lease_file("work").exists()
    assert env.run("status", "work")[1]["data"]["state"] == "down"


def test_the_checks_named_at_the_limit_come_from_a_proof_that_ran_to_its_end(env):
    """A proof the start limit cuts short (here its capture never comes) did not run every check, and would name one as
    failed that passed a moment before. It never replaces a whole proof of the same port file: the answer names only
    what really failed."""
    env.rewrite_config("start_limit_seconds = 5", f"start_limit_seconds = {LIMIT}")
    env.set_chrome("work", headless=True)
    env.set_fake(xwd_stall=60, xwd_stall_after=2)  # two proofs run to their end, then every capture stalls
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 7 and out["data"]["failed"] == ["headful"], out


def _main_pid(env, browser: str) -> int:
    """The main process of the browser's unit, as the fake service manager records it."""
    return json.loads((env.root / "fake" / "units.json").read_text())[env.unit(browser)]["pid"]


def test_a_holder_never_resumes_a_session_whose_unit_is_not_exactly_active(env):
    """A deactivating unit already has a stop under way: resumed, it would hand the holder an endpoint that dies within
    the stop grace. The holder's lease starts a new session instead, with a new endpoint, and that one is healthy."""
    code, first = env.run("lease", "work", "--holder", "a")
    assert code == 0, first
    env.set_fake(unit_state="deactivating", unit_state_pid=_main_pid(env, "work"))  # this session only
    code, again = env.run("lease", "work", "--holder", "a")
    assert code == 0 and again["message"].startswith("started"), again
    assert again["data"]["endpoint"] != first["data"]["endpoint"] and "extend" not in env.events("work")
    assert env.run("status", "work")[1]["data"]["state"] == "healthy"


@contextmanager
def _failing_after(seconds: int) -> Iterator[None]:
    """Fail the test if the block runs longer than `seconds`: a call made in the test's own process has no subprocess
    timeout to stop it. The failure is raised in the block (SIGALRM): pytest's own outcome, which no except clause in
    the code under test catches, as it could a TimeoutError (an OSError)."""

    def out_of_time(signum: int, frame: object) -> None:
        pytest.fail(f"no answer within {seconds} s", pytrace=False)

    previous = signal.signal(signal.SIGALRM, out_of_time)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)


def test_a_start_answers_with_the_port_file_its_proof_judged(env, monkeypatch):
    """The port file is read where it is judged. Forced timing: the first look at the complete port file, once the
    session is ready, finds nothing yet, as if Chrome wrote it a moment later. A look of the start's own taken before
    the proof's must never leave a passing proof without its endpoint: the start would stop a healthy session and fail
    with nothing failed. (In-process, to force the timing: the session itself still runs as its own process.)"""
    for key in [key for key in os.environ if key.startswith("ORION_BROWSER_") or key in ("INVOCATION_ID", "DISPLAY")]:
        monkeypatch.delenv(key)
    for key, value in env.env.items():
        monkeypatch.setenv(key, value)
    fake, display = env.root / "fake", f":{SCREENS['work']}"
    real = devtools.read_port_file
    missed = []

    def first_look_misses(path):
        found = real(path)
        ready = (fake / f"focus-{display}").exists() and (fake / f"drawn-{display}").exists()
        if found is not None and ready and not missed:
            missed.append(path)
            return None
        return found

    monkeypatch.setattr(devtools, "read_port_file", first_look_misses)
    with _failing_after(IN_PROCESS_LIMIT):
        try:
            result = session.Pool(config.load()).lease("work", "a")
        except BrowserError as exc:
            pytest.fail(f"the start failed: {exc.code.name}: {exc.message} {exc.data}")
    assert missed, "the timing was never forced"
    assert result.code is Code.OK
    assert result.data["endpoint"] == real(env.profile("work") / "DevToolsActivePort").endpoint


def test_a_chrome_that_wrote_its_port_file_and_died_ends_the_start_down_after_one_start(env):
    """Chrome wrote its port file, then died before any proof could pass (headless keeps each one failing). The start
    has a port file of its own: it is reported at once, BROWSER_DOWN since no session runs, and never started again.
    Only a Chrome that never wrote a port file is tried once more (Q4, from S4 on)."""
    env.set_chrome("work", headless=True, crash_after=1.5)
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 6 and out["data"]["failed"] == EVERY_CHECK, out
    assert len(env.calls("chrome")) == 1 and env.events("work").count("crashed") <= 1, env.events("work")


def test_a_runtime_dir_the_caller_set_is_never_replaced(env, tmp_path):
    """A launcher that sets XDG_RUNTIME_DIR keeps it, even when /run/user/<uid> exists and is the user's own."""
    base = tmp_path / "run" / "user"
    (base / str(os.getuid())).mkdir(parents=True)  # a default that would be taken if the variable were ignored
    chosen = tmp_path / "chosen"
    chosen.mkdir()
    env.set_fake(bus_runtime_dir=str(chosen))  # the user's manager is found only through the caller's folder
    proc = subprocess.run(
        [sys.executable, "-P", "-c", RUNTIME_BASE_DRIVER, str(base), "--json", "status"],
        capture_output=True, text=True, env={**env.env, "XDG_RUNTIME_DIR": str(chosen)}, timeout=120,
    )
    code, out = env._answer(proc.returncode, proc.stdout, proc.stderr)
    assert code == 0 and [row["browser"] for row in out["data"]["browsers"]] == ["anon", "work"], out
