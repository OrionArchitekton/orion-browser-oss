"""S2: the view, the drive link for the lease holder only, and the profile preferences written before a start."""

from __future__ import annotations

import ast
import base64
import json
import os
import re
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from conftest import LOOPBACK, SCREENS
from fakesys import alive

from orion_browser import runner
from orion_browser.runner import VIEW_GUARD_FOLDER, VIEW_RETRY_MAX

_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def watch(browser: str) -> str:
    return f"http://{LOOPBACK}:{6080 + SCREENS[browser]}/vnc.html?autoconnect=1&resize=scale&view_only=1"


def answers(url: str) -> bool:
    """The view page answers; a refused connection (not listening yet, or restarting) is a no, not an error."""
    try:
        return _HTTP.open(url, timeout=5).status == 200
    except OSError:
        return False


def view_pids(env, browser: str) -> list[int]:
    """Running fake websockify processes serving this browser's view."""
    listen = f"{LOOPBACK}:{6080 + SCREENS[browser]}".encode()
    pids = []
    for entry in os.listdir("/proc"):
        if entry.isdigit():
            try:
                argv = open(f"/proc/{entry}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if any(arg.endswith(b"fake_websockify.py") for arg in argv) and listen in argv:
                pids.append(int(entry))
    return pids


def view_mains(env, browser: str) -> list[int]:
    """The view's websockify main processes: not the ones a main process forked to serve a connection."""
    pids = view_pids(env, browser)
    return [pid for pid in pids if _parent(pid) not in pids]


def _parent(pid: int) -> int:
    try:
        return int(Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return 0


def own_origin(browser: str) -> str:
    return f"http://{LOOPBACK}:{6080 + SCREENS[browser]}"


def open_websocket(browser: str, origin: str | None) -> tuple[socket.socket, int]:
    """A request for the view's WebSocket, as a page opens one, from `origin` (no Origin header when None): the open
    connection and the status of the answer (101 when the view takes it)."""
    host, port = LOOPBACK, 6080 + SCREENS[browser]
    conn = socket.create_connection((host, port), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    conn.sendall((
        f"GET /websockify HTTP/1.1\r\nHost: {host}:{port}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
        + (f"Origin: {origin}\r\n" if origin is not None else "") + "\r\n"
    ).encode())
    status = conn.recv(1024).split(b"\r\n", 1)[0].split()
    return conn, int(status[1]) if len(status) > 1 and status[1].isdigit() else 0


def attach_viewer(browser: str) -> socket.socket:
    """A viewer on the view's WebSocket, opened as noVNC's page opens it (from the page's own origin): the 101 answer
    read, and the connection then held open."""
    conn, status = open_websocket(browser, own_origin(browser))
    assert status == 101, status
    return conn


def load_status(browser: str, headers: dict[str, str]) -> int:
    """The status of a load of the view's page sent with these request headers (an error status included)."""
    try:
        return _HTTP.open(urllib.request.Request(watch(browser), headers=headers), timeout=5).status
    except urllib.error.HTTPError as exc:
        return exc.code


def preferences_at_launch(env, browser: str) -> dict | None:
    """The Preferences file fake Chrome found in the browser's profile when it launched; None when there was none."""
    found = json.loads((env.root / "fake" / f"preferences-at-launch-{browser}").read_text())["preferences"]
    return None if found is None else json.loads(found)


def test_view_links_and_the_drive_link_only_for_the_holder(env):
    assert env.run("lease", "work", "--holder", "agent-a")[0] == 0
    code, out = env.run("view", "work")
    assert code == 0 and out["data"] == {"browser": "work", "watch": watch("work")}
    code, out = env.run("view", "work", "--drive", "--holder", "operator")
    assert code == 4
    code, out = env.run("view", "work", "--drive")
    assert code == 2 and "--holder" in out["message"]
    code, out = env.run("view", "work", "--drive", "--holder", "agent-a")
    assert code == 0 and out["data"]["drive"].endswith("/vnc.html?autoconnect=1&resize=scale&view_only=0")


def test_the_drive_link_is_refused_to_the_holder_of_an_expired_lease(env):
    """An expired lease is held by no one, though nobody has taken it over and its session still runs: no drive link."""
    assert env.run("lease", "work", "--holder", "agent-a")[0] == 0
    env.age_lease("work", 60)
    code, out = env.run("view", "work", "--drive", "--holder", "agent-a")
    assert (code, out["code"]) == (4, "NOT_HOLDER")
    assert out["message"] == "agent-a does not hold browser work: the lease expired; lease it first"
    assert out["data"] == {"browser": "work"}
    code, out = env.run("view", "work")  # watching needs no lease
    assert code == 0 and out["data"] == {"browser": "work", "watch": watch("work")}


def test_view_needs_a_session(env):
    code, out = env.run("view", "work")
    assert code == 6 and "lease it first" in out["message"]


@pytest.mark.parametrize(
    "case", ["exits-at-once", "never-listens", "port-taken-after-the-look", "websockify-removed", "not-executable"],
)
def test_a_session_whose_view_does_not_come_up_does_not_start(env, case):
    """Every session starts with its view: Chrome starts only once the view's own websockify accepts connections on the
    view's port. A view that never does fails the start at once (no second try: Chrome never ran), BROWSER_DOWN naming
    the view and its port, and the start cleans up as any failed start does: nothing of it is left running, the lease
    is freed, and the stop and the failed start are on record.

    websockify exits at once when it cannot start (its noVNC folder missing, its port taken), or it may never listen.
    A program that takes the view's port after the lease's look at the ports (a moment before the view starts) answers
    there before websockify, unable to listen, exits: it is not the view, and does not count as one. And websockify may
    not start at all: removed, or no longer executable, after setup."""
    port = 6080 + SCREENS["work"]
    if case == "exits-at-once":
        env.set_fake(websockify_exits=True)
    elif case == "never-listens":
        env.set_fake(websockify_never_listens=True)
        env.rewrite_config("start_limit_seconds = 5", "start_limit_seconds = 30")  # the view's own limit comes first
    elif case == "port-taken-after-the-look":
        env.set_fake(listen_before_start=port)
    elif case == "websockify-removed":
        (env.root / "bin" / "websockify").unlink()
    else:
        (env.root / "bin" / "websockify").chmod(0o644)
    code, out = env.run("lease", "work", "--holder", "a")
    assert (code, out["code"]) == (6, "BROWSER_DOWN"), out
    assert out["message"].startswith(f"browser work's view (websockify on port {port}) did not come up: "), out
    assert out["data"]["view_port"] == port and out["data"]["failed"], out
    assert len(env.calls("systemd-run")) == 1 and env.calls("chrome") == []
    assert not env.lease_file("work").exists()
    stop, failed = env.audit("work")
    assert (stop["event"], stop["stop"]) == ("stop", "none")  # the session had ended by itself
    assert (failed["event"], failed["code"], failed["reason"]) == ("start-failed", "BROWSER_DOWN", out["message"])
    assert failed["session"] == stop["session"] != ""
    assert view_pids(env, "work") == []
    assert env.run("status", "work")[1]["data"]["state"] == "down"


def test_the_view_answers_and_is_started_again_if_it_stops(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: answers(watch("work")))
    first = env.wait_until(lambda: view_pids(env, "work"))
    os.kill(first[0], signal.SIGKILL)
    env.wait_until(lambda: view_pids(env, "work") and view_pids(env, "work") != first)
    env.wait_until(lambda: answers(watch("work")))


def test_a_view_that_dies_again_and_again_is_started_again_each_time(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    ended: list[int] = []
    for _ in range(3):
        running = env.wait_until(lambda: [pid for pid in view_pids(env, "work") if pid not in ended])
        env.wait_until(lambda: answers(watch("work")))
        os.kill(running[0], signal.SIGKILL)
        ended.append(running[0])
    env.wait_until(lambda: [pid for pid in view_pids(env, "work") if pid not in ended])
    env.wait_until(lambda: answers(watch("work")))


def test_a_view_that_cannot_be_started_again_never_ends_the_session(env):
    """websockify goes missing mid-session, so every start of the view fails. The session keeps running and healthy, the
    tries are spaced out (each wait doubles, up to a cap), and the view comes back once websockify is back. A view that
    then stays up starts the waits over, and a release still closes Chrome through DevTools, with its cookies saved."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    first = env.wait_until(lambda: view_pids(env, "work"))
    shim, aside = env.root / "bin" / "websockify", env.root / "bin" / "websockify.gone"
    shim.rename(aside)
    log = env.root / "fake" / f"{env.unit('work')}.log"  # the session's output, as the service manager keeps it

    def tries() -> int:
        return log.read_text().count("could not be started again")

    killed = time.monotonic()
    os.kill(first[0], signal.SIGKILL)
    env.wait_until(lambda: tries() >= 2)  # it keeps trying ...
    time.sleep(max(0.0, killed + 3 - time.monotonic()))
    assert tries() <= 5  # ... after waits of 0.2, 0.4, 0.8, 1.6 s: a try on every 0.2 s turn would make about 15 by now
    code, out = env.run("status", "work")
    assert code == 0 and out["data"]["state"] == "healthy"
    env.wait_until(lambda: tries() >= 5)
    announced = [float(wait) for wait in re.findall(r"trying again in ([0-9.]+) s", log.read_text())]
    assert announced[:5] == pytest.approx([0.4, 0.8, 1.6, 3.2, VIEW_RETRY_MAX])  # each wait doubles, up to the cap
    aside.rename(shim)
    back = env.wait_until(lambda: view_pids(env, "work"))
    env.wait_until(lambda: answers(watch("work")))
    time.sleep(VIEW_RETRY_MAX + 0.5)  # the view stays up as long as the longest wait, so the waits start over
    os.kill(back[0], signal.SIGKILL)
    lost = time.monotonic()
    env.wait_until(lambda: [pid for pid in view_pids(env, "work") if pid != back[0]])
    assert time.monotonic() - lost < 2.5  # promptly again, not after the 5 s wait the failed tries built up
    code, out = env.run("release", "work", "--holder", "a")
    assert code == 0 and out["data"]["stop"] == "clean"
    assert (env.profile("work") / "Cookies.fake").read_text() == "saved"
    assert env.events("work") == ["lease", "release"]  # nothing crashed, and nothing was stopped by a signal


def test_a_view_that_keeps_dying_is_started_again_with_a_backoff(env):
    """A websockify that exits as soon as it starts is started again after waits that double, not on every turn of the
    session loop (that made about 15 starts in 3 s), and the session stays healthy meanwhile."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    first = env.wait_until(lambda: view_pids(env, "work"))
    starts = env.root / "fake" / "dying-view-starts"
    dying = env.root / "bin" / "websockify.dying"
    dying.write_text(f'#!/bin/sh\necho started >> "{starts}"\nexit 1\n')
    dying.chmod(0o755)
    dying.replace(env.root / "bin" / "websockify")

    def started() -> int:
        return len(starts.read_text().splitlines()) if starts.exists() else 0

    killed = time.monotonic()
    os.kill(first[0], signal.SIGKILL)
    env.wait_until(lambda: started() >= 2)  # it keeps starting it ...
    time.sleep(max(0.0, killed + 3 - time.monotonic()))
    assert started() <= 5  # ... after waits of 0.2, 0.4, 0.8, 1.6 s
    code, out = env.run("status", "work")
    assert code == 0 and out["data"]["state"] == "healthy"


def test_a_crashed_view_is_started_again_while_a_viewer_is_still_attached(env):
    """Real websockify serves each connection from a process it forks, which keeps the listening socket (the fake does
    too, with websockify_forks). When its main process dies, an attached viewer's process still holds the view's port:
    no new start could take it, and new viewers and page loads would hang until that viewer left. The view runs in a
    process group of its own, which the session ends whole before it starts the view again: a new connection is
    answered while the old viewer has not let go, and that viewer's connection ends with its group."""
    env.set_fake(websockify_forks=True)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: answers(watch("work")))
    viewer = attach_viewer("work")
    try:
        main = env.wait_until(lambda: view_mains(env, "work"))
        serving = env.wait_until(lambda: [pid for pid in view_pids(env, "work") if pid not in main])
        os.kill(main[0], signal.SIGKILL)  # a crash, with the viewer still attached
        env.wait_until(lambda: answers(watch("work")))  # a new start took the port
        assert not any(alive(pid) for pid in serving)  # the crashed view's process for the viewer ended with it
        viewer.settimeout(5)
        assert viewer.recv(1) == b""  # and so did that viewer's connection: noVNC shows its Connect panel
    finally:
        viewer.close()


@pytest.mark.parametrize(
    "fake, chrome",
    [({}, {}), ({}, {"ignore_close": True}), ({"websockify_hangs": True, "websockify_handlers_hang": True}, {})],
    ids=["chrome-closed", "session-stopped", "view-hung"],
)
def test_a_release_leaves_no_view_process_behind(env, fake, chrome):
    """The view's process group is the session's to end, whole: after a release, whether Chrome closed through DevTools
    or the session was stopped by a signal, neither websockify nor the process it forked for a viewer still attached
    is left. That holds even for a websockify that hangs, its main and forked processes alike, which only SIGKILL
    ends: a SIGKILL to its main process alone would leave the forked one holding the view's port."""
    env.set_fake(websockify_forks=True, **fake)
    env.set_chrome("work", **chrome)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: answers(watch("work")))
    viewer = attach_viewer("work")
    try:
        env.wait_until(lambda: len(view_pids(env, "work")) == 2)  # websockify, and its process for the viewer
        code, out = env.run("release", "work", "--holder", "a")
        assert code == 0, out
        assert view_pids(env, "work") == []
        viewer.settimeout(5)
        assert viewer.recv(1) == b""
    finally:
        viewer.close()


def test_the_view_serves_the_novnc_folder_and_bridges_to_the_screen(env):
    """websockify serves the configured noVNC folder at the view port and bridges to the screen's VNC port, all on
    loopback. Its auth plugin is the view's guard, asked about page loads too (--web-auth), with the view's own page
    as the one origin it takes a WebSocket from; websockify finds the guard through its PYTHONPATH, the guard's folder
    alone."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    calls = env.wait_until(lambda: env.calls("websockify"))
    view, screen = f"{LOOPBACK}:{6080 + SCREENS['work']}", f"{LOOPBACK}:{5900 + SCREENS['work']}"
    assert calls == [[
        "--auth-plugin", "orion_browser_view_guard.ViewGuard", "--auth-source", f"http://{view}", "--web-auth",
        "--web", str(env.root / "novnc"), view, screen,
    ]]
    assert (env.root / "fake" / "websockify-pythonpath").read_text() == str(VIEW_GUARD_FOLDER)


@pytest.mark.parametrize("destination", ["iframe", "frame", "embed", "object", "fencedframe"])
def test_the_view_refuses_to_load_in_a_frame(env, destination):
    """Any page in any browser on the host could otherwise show a view in a frame and steer a person's clicks or typing
    into it: the frame is the view's own page, so its WebSocket passes the origin check. The browser says what a load is
    for in Sec-Fetch-Dest, which no page can set, and the view refuses (403) a load into a frame, an embed, an object or
    a fenced frame. So does a view started again after it stopped."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: answers(watch("work")))
    assert load_status("work", {"Sec-Fetch-Dest": destination}) == 403
    first = env.wait_until(lambda: view_pids(env, "work"))
    os.kill(first[0], signal.SIGKILL)
    env.wait_until(lambda: view_pids(env, "work") and view_pids(env, "work") != first)
    env.wait_until(lambda: answers(watch("work")))
    assert load_status("work", {"Sec-Fetch-Dest": destination}) == 403


@pytest.mark.parametrize("destination", [None, "document", "script", "empty"])
def test_the_view_still_loads_at_the_top_level_and_without_fetch_metadata(env, destination):
    """A load in a tab of its own (document), the page's own scripts and fetches, and a client that sends no Fetch
    Metadata at all (not every client does) load as before."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: answers(watch("work")))
    assert load_status("work", {"Sec-Fetch-Dest": destination} if destination else {}) == 200


def test_the_view_takes_a_websocket_only_from_its_own_page(env):
    """The origin check stays as it was, websockify's ExpectOrigin, which the guard extends: a WebSocket from the view's
    own page is taken, and one from any other page, or with no Origin, is refused (403), so no other web page can
    drive the screen."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    env.wait_until(lambda: answers(watch("work")))
    for origin, status in ((own_origin("work"), 101), ("http://example.com", 403), (None, 403)):
        conn, answered = open_websocket("work", origin)
        conn.close()
        assert answered == status, (origin, answered)


def test_the_view_env_replaces_an_inherited_pythonpath(monkeypatch):
    """websockify's PYTHONPATH is the guard's folder alone, even when the session inherited one of its own: nothing
    else is put in front of the system Python's own modules."""
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")
    assert runner._view_env()["PYTHONPATH"] == str(VIEW_GUARD_FOLDER)


def test_the_view_guard_needs_only_the_standard_library_and_websockify():
    """websockify is the system's, run by the system's Python, which has none of orion-browser's dependencies; it gets
    the guard's folder alone as its PYTHONPATH. That folder holds the guard and nothing else, and the guard imports only
    the standard library and websockify's own auth_plugins, never orion_browser."""
    found = sorted(path.name for path in VIEW_GUARD_FOLDER.iterdir() if path.name != "__pycache__")
    assert found == ["orion_browser_view_guard.py"]
    tree = ast.parse((VIEW_GUARD_FOLDER / "orion_browser_view_guard.py").read_text())
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names}
    imported |= {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert "websockify.auth_plugins" in imported
    others = {name for name in imported if name != "websockify.auth_plugins"}
    assert all(name.split(".")[0] in sys.stdlib_module_names for name in others), others


SYSTEM_PYTHON = "/usr/bin/python3"  # the Python the distribution's websockify runs under


def _system_websockify() -> bool:
    try:
        return subprocess.run(
            [SYSTEM_PYTHON, "-c", "import websockify.auth_plugins"], capture_output=True, timeout=30,
        ).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


GUARD_UNDER_THE_SYSTEMS_PYTHON = textwrap.dedent("""\
    import http.client, io, json, sys
    from websockify import auth_plugins
    import orion_browser_view_guard as guard_module

    guard = guard_module.ViewGuard("http://127.0.0.1:6171")

    def verdict(lines):
        raw = "".join(line + "\\r\\n" for line in lines) + "\\r\\n"
        headers = http.client.parse_headers(io.BytesIO(raw.encode()))
        try:
            guard.authenticate(headers=headers, target_host="127.0.0.1", target_port=5991)
        except auth_plugins.AuthenticationError as exc:
            return exc.code
        return "allowed"

    upgrade = ["Upgrade: websocket", "Connection: Upgrade"]
    mixed_case_upgrade = ["Upgrade: WebSocket", "Connection: Upgrade"]
    loaded = [name for name in sys.modules if name == "orion_browser" or name.startswith("orion_browser.")]
    print(json.dumps({
        "extends_expect_origin": issubclass(guard_module.ViewGuard, auth_plugins.ExpectOrigin),
        "orion_browser_imported": bool(loaded),
        "iframe": verdict(["Sec-Fetch-Dest: iframe"]),
        "iframe_upper": verdict(["sec-fetch-dest: IFRAME"]),
        "object": verdict(["Sec-Fetch-Dest: object"]),
        "fencedframe": verdict(["Sec-Fetch-Dest: fencedframe"]),
        "document": verdict(["Sec-Fetch-Dest: document"]),
        "no_fetch_metadata": verdict([]),
        "own_websocket": verdict(upgrade + ["Origin: http://127.0.0.1:6171"]),
        "foreign_websocket": verdict(upgrade + ["Origin: http://example.com"]),
        "websocket_without_origin": verdict(upgrade),
        "foreign_websocket_mixed_case": verdict(mixed_case_upgrade + ["Origin: http://example.com"]),
    }))
""")


@pytest.mark.skipif(not _system_websockify(), reason="needs the system's Python with the distribution's websockify")
def test_the_view_guard_works_under_the_systems_python_and_websockify(tmp_path):
    """Under the system's Python and its websockify (0.10.0 on the operator's host), given the guard's folder alone as
    PYTHONPATH as a view is: the guard loads without orion_browser, extends websockify's own ExpectOrigin, and answers
    the header objects websockify hands it as the view should."""
    proc = subprocess.run(
        [SYSTEM_PYTHON, "-c", GUARD_UNDER_THE_SYSTEMS_PYTHON], capture_output=True, text=True, timeout=30,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(VIEW_GUARD_FOLDER)}, cwd=tmp_path,
    )
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout) == {
        "extends_expect_origin": True, "orion_browser_imported": False,
        "iframe": 403, "iframe_upper": 403, "object": 403, "fencedframe": 403, "document": "allowed",
        "no_fetch_metadata": "allowed", "own_websocket": "allowed", "foreign_websocket": 403,
        "websocket_without_origin": 403, "foreign_websocket_mixed_case": 403,
    }


def test_preferences_written_before_the_start(env):
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    prefs = json.loads((env.profile("work") / "Default" / "Preferences").read_text())
    assert prefs["download"] == {
        "default_directory": str(env.root / "state" / "downloads" / "work"), "prompt_for_download": False,
    }
    assert prefs["profile"]["exit_type"] == "Normal" and prefs["profile"]["exited_cleanly"] is True
    assert prefs["profile"]["password_manager_enabled"] is False and prefs["credentials_enable_service"] is False
    assert "session" not in prefs  # session restore only when the browser's config asks for it
    assert (env.root / "state" / "downloads" / "work").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("restore", [False, True])
def test_chrome_finds_the_preferences_on_disk_when_it_starts(env, restore):
    """The preferences are written while Chrome is not running: before the session's service starts. Here the start
    returns only once Chrome has launched, so preferences written after the start would come too late."""
    if restore:
        work = f'name = "work"\nscreen = {SCREENS["work"]}'
        env.rewrite_config(work, work + "\nrestore_session = true")
    env.set_fake(start_waits_for="preferences-at-launch-work")
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    found = preferences_at_launch(env, "work")
    assert found is not None, "Chrome started before its preferences were written"
    assert found["download"] == {
        "default_directory": str(env.root / "state" / "downloads" / "work"), "prompt_for_download": False,
    }
    assert found["profile"] == {"exit_type": "Normal", "exited_cleanly": True, "password_manager_enabled": False}
    assert found["credentials_enable_service"] is False
    assert found.get("session") == ({"restore_on_startup": 1} if restore else None)


def test_existing_preferences_are_kept(env):
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text(json.dumps({"browser": {"window": "kept"}, "download": {"directory_upgrade": True}}))
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    prefs = json.loads(prefs_file.read_text())
    assert prefs["browser"] == {"window": "kept"}
    assert prefs["download"]["directory_upgrade"] is True and prefs["download"]["prompt_for_download"] is False


@pytest.mark.parametrize("restore", [False, True])
def test_other_profile_and_session_settings_are_kept(env, restore):
    """A start sets only its own keys: the operator's per-site permissions in the profile section and the other session
    settings stay. With restore off, a start-up choice that restores nothing (4: open a set of pages) stays too."""
    work = f'name = "work"\nscreen = {SCREENS["work"]}'
    env.rewrite_config(work, work + f"\nrestore_session = {str(restore).lower()}")
    sites = {"exceptions": {"geolocation": {"https://example.com:443,*": {"setting": 1}}}}
    pages = ["https://example.com/"]
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text(json.dumps({
        "profile": {"content_settings": sites, "exit_type": "Crashed"},
        "session": {"restore_on_startup": 4, "startup_urls": pages},
    }))
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    prefs = json.loads(prefs_file.read_text())
    assert prefs["profile"]["content_settings"] == sites and prefs["profile"]["exit_type"] == "Normal"
    assert prefs["session"] == {"restore_on_startup": 1 if restore else 4, "startup_urls": pages}


def test_session_restore_is_configuration(env):
    env.rewrite_config(f'name = "work"\nscreen = {SCREENS["work"]}', f'name = "work"\nscreen = {SCREENS["work"]}\nrestore_session = true')
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    prefs = json.loads((env.profile("work") / "Default" / "Preferences").read_text())
    assert prefs["session"]["restore_on_startup"] == 1


def test_session_restore_turned_off_again_is_off_at_the_next_launch(env):
    """restore_session works both ways: once it is false again, Chrome finds no restore setting at its next launch, so
    it opens its own default, the New Tab page."""
    work = f'name = "work"\nscreen = {SCREENS["work"]}'
    env.rewrite_config(work, work + "\nrestore_session = true")
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    assert preferences_at_launch(env, "work")["session"] == {"restore_on_startup": 1}
    assert env.run("release", "work", "--holder", "a")[0] == 0
    env.rewrite_config("restore_session = true", "restore_session = false")
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    assert "restore_on_startup" not in preferences_at_launch(env, "work").get("session", {})


def test_unreadable_preferences_refuse_the_start(env):
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text("{")
    code, out = env.run("lease", "work", "--holder", "a")
    assert code == 2 and str(prefs_file) in out["message"]
    assert not env.lease_file("work").exists()


def test_the_preferences_file_is_owner_only(env):
    """The file is written anew, owner-only, and renamed into place: a world-readable one left by another program is
    replaced, not written into."""
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text("{}")
    prefs_file.chmod(0o644)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    assert prefs_file.stat().st_mode & 0o777 == 0o600


def test_the_folders_a_start_makes_are_owner_only(env):
    """The profile, its Default folder and the downloads folder, with every parent a start has to make for them."""
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    profile, downloads = env.profile("work"), env.root / "state" / "downloads"
    for folder in (env.root / "profiles", profile, profile / "Default", downloads, downloads / "work"):
        assert folder.stat().st_mode & 0o777 == 0o700, folder


def test_existing_profile_and_download_folders_are_narrowed_to_owner_only(env):
    folders = (env.profile("work"), env.profile("work") / "Default", env.root / "state" / "downloads" / "work")
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
        folder.chmod(0o755)
    assert env.run("lease", "work", "--holder", "a")[0] == 0
    for folder in folders:
        assert folder.stat().st_mode & 0o777 == 0o700, folder


@pytest.mark.parametrize(("content", "problem"), [
    ("[]", "are not a JSON object"),
    ('{"download": []}', "hold a 'download' that is not an object"),
])
def test_preferences_that_are_not_an_object_refuse_the_start(env, content, problem):
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text(content)
    code, out = env.run("lease", "work", "--holder", "a")
    assert (code, out["code"]) == (2, "USAGE")
    assert out["message"] == f"Chrome's preferences {prefs_file} {problem}"
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []  # refused before the service starts
    assert prefs_file.read_text() == content  # left as it was


def _default_is_a_file(env) -> Path:
    """A file where the profile's Default folder goes, so the folder cannot be made."""
    default = env.profile("work") / "Default"
    default.parent.mkdir(parents=True)
    default.write_text("not a folder")
    return default


def _the_write_meets_a_folder(env) -> Path:
    """A folder where the preferences are first written, so the write fails."""
    (env.profile("work") / "Default" / "Preferences.orion-browser.tmp").mkdir(parents=True)
    return env.profile("work") / "Default" / "Preferences"


def _nested_too_deep(env) -> Path:
    """Preferences nested deeper than the JSON reader goes."""
    prefs_file = env.profile("work") / "Default" / "Preferences"
    prefs_file.parent.mkdir(parents=True)
    prefs_file.write_text("[" * 100_000 + "]" * 100_000)
    return prefs_file


@pytest.mark.parametrize(
    "break_profile", [_default_is_a_file, _the_write_meets_a_folder, _nested_too_deep],
    ids=["default-is-a-file", "write-meets-a-folder", "nested-too-deep"],
)
def test_preferences_that_cannot_be_written_refuse_the_start_and_free_the_lease(env, break_profile):
    """Whatever keeps the preferences from being written is USAGE naming the path: the lease is freed for the next
    holder, the failed start is recorded, and nothing is started."""
    named = break_profile(env)
    code, out = env.run("lease", "work", "--holder", "a")
    assert (code, out["code"]) == (2, "USAGE") and str(named) in out["message"], out
    assert not env.lease_file("work").exists() and env.calls("systemd-run") == []
    assert env.events("work") == ["start-failed"]
