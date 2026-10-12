"""The session service: its screen and Chrome, kept until Chrome exits or a stop arrives.

Runs only inside the transient service a lease starts (`orion-browser _session <browser>`), which carries the session id
the lease recorded."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from orion_browser import devtools, procs
from orion_browser.audit import Audit
from orion_browser.codes import BrowserError, Code, Result
from orion_browser.config import Browser, Config, loopback
from orion_browser.lease import Lease, LeaseStore

READY_LIMIT = 15.0  # seconds for the screen, then the view, to accept connections, and for Chrome's window to appear
POLL = 0.2
VIEW_RETRY_FIRST = 0.2  # seconds before the view is started again after it stops, the first time
VIEW_RETRY_MAX = 5.0  # the longest wait between starts of the view; one that stays up this long starts the waits over
# The view's guard, websockify's auth plugin, alone in its folder: websockify's PYTHONPATH (orion_browser_view_guard).
VIEW_GUARD_FOLDER = Path(__file__).resolve().parent / "websockify_path"


class _StopRequest:
    def __init__(self) -> None:
        self.requested = False

    def __call__(self, signum: int, frame: object) -> None:
        self.requested = True


def run_session(cfg: Config, name: str) -> Result:
    b = cfg.browser(name)
    lease = _own_lease(cfg, b)
    stop = _StopRequest()
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    audit = Audit(cfg.state_dir)
    host = loopback()
    children: list[subprocess.Popen] = []
    try:
        # A stop signal that arrives while the session starts is acted on between its steps: before Chrome starts, the
        # session ends with nothing to close; once Chrome runs, the focus step ends early and the loop below closes it.
        _start_screen(children, cfg, b, host)
        if stop.requested:
            return _stopped_while_starting(b, lease, audit)
        view = _start_view(children, cfg, b, host, lease.session)
        if stop.requested:
            return _stopped_while_starting(b, lease, audit)
        chrome = _spawn(children, _chrome_argv(cfg, b, host), env={**os.environ, "DISPLAY": b.display})
        _focus(b, chrome.pid, stop)
        while True:
            if stop.requested:
                return _stop_for_signal(cfg, b, lease, chrome, audit)
            code = chrome.poll()
            if code is not None:
                return _chrome_ended(b, lease, code, audit)
            view.tend()  # the view runs for the whole session: started again whenever it stops
            time.sleep(POLL)
    finally:
        for child in reversed(children):
            _end(child)


def _own_lease(cfg: Config, b: Browser) -> Lease:
    """The lease this service was started for. A hand run has no service id or no matching session: refused."""
    session = os.environ.get("ORION_BROWSER_SESSION", "")
    lease = LeaseStore(cfg.state_dir).current(b.name)
    if not os.environ.get("INVOCATION_ID") or not session or lease is None or lease.session != session or lease.endpoint:
        raise BrowserError(Code.USAGE, f"a session of browser {b.name} starts only through `orion-browser lease {b.name}`")
    return lease


def _screen_argv(cfg: Config, b: Browser, host: str, ready_fd: int) -> list[str]:
    return [
        "Xvnc", b.display, "-displayfd", str(ready_fd), "-geometry", f"{cfg.width}x{cfg.height}", "-depth", "24",
        "-rfbport", str(b.vnc_port),
        "-interface", host, "-SecurityTypes", "None", "-AlwaysShared", "-br", "-nolisten", "tcp",
    ]


def _chrome_argv(cfg: Config, b: Browser, host: str) -> list[str]:
    return [
        str(cfg.chrome), f"--user-data-dir={b.profile}", f"--remote-debugging-address={host}",
        "--remote-debugging-port=0", "--window-position=0,0", f"--window-size={cfg.width},{cfg.height}",
        "--no-first-run", "--no-default-browser-check", "--password-store=basic",
        "--ozone-platform=x11",  # X11 even when the environment names a Wayland display (WSLg): its own screen only
        # A build older than Chrome's outdated-build age (eight weeks) opens a "Can't update Chrome" bubble a moment
        # after it starts, which takes the keyboard focus from the page (seen at the first smoke test). Only that check
        # is turned off: updates installed on disk are still announced, and keeping Chrome current is the operator's.
        "--disable-features=OutdatedBuildDetector",
        # Since Chrome 155 a DevTools port alone makes every page report navigator.webdriver true, and Google refuses a
        # sign-in in a browser that does (spec section 13, Q3). Pool browsers present as an ordinary Chrome (S1); the
        # health proof's webdriver check fails a Chrome that no longer honors this flag.
        "--disable-blink-features=AutomationControlled",
        # An empty page, focused once the window has the focus. Started on about:blank, Chrome keeps the keyboard focus
        # in its address bar, and the page never has it, however late the window is given the focus (seen at the
        # first smoke test, and in a namespaced run of the real Chrome, Xvnc and xdotool).
        "data:text/html,",
    ]


def _view_argv(cfg: Config, b: Browser, host: str) -> list[str]:
    """websockify serves the noVNC page and bridges it to the screen's VNC port, both on loopback, with the view's guard
    (websockify_path/orion_browser_view_guard.py) as its auth plugin, asked about page loads too (--web-auth). The
    guard takes a WebSocket only from the page's own origin: the screen has no VNC password, and a browser lets any web
    page open a WebSocket to any address, so otherwise any page in any browser on the host (a pool browser an agent
    pointed at an untrusted site included) could drive the screen. And it refuses a load into another page's frame,
    whose WebSocket would be the view's own page's."""
    return [
        "websockify", "--auth-plugin", "orion_browser_view_guard.ViewGuard", "--auth-source",
        f"http://{host}:{b.view_port}", "--web-auth",
        "--web", str(cfg.novnc_dir), f"{host}:{b.view_port}", f"{host}:{b.vnc_port}",
    ]


def _view_env() -> dict[str, str]:
    """websockify's environment: the session's, with the guard's folder alone as PYTHONPATH (any PYTHONPATH the session
    inherited is not passed on). websockify runs under the system's Python, so nothing else of orion-browser's
    installation (its dependencies, built for its own Python) is put in front of that Python's own modules."""
    return {**os.environ, "PYTHONPATH": str(VIEW_GUARD_FOLDER)}


def _start_view(children: list[subprocess.Popen], cfg: Config, b: Browser, host: str, session: str) -> _View:
    """Start the view and wait until it accepts connections on its port, before Chrome starts: every session starts
    with its view. Only a socket the view's own websockify listens on counts. Another program can take the port after
    the lease's look at the ports, a moment earlier; websockify, unable to listen there, then exits, but that program
    answers on the port before it does.

    A view that does not come up within READY_LIMIT seconds, or cannot be started at all (websockify removed or not
    executable since setup), fails the start: BROWSER_DOWN naming the view and its port, written down for the lease
    that started the session (its start-failure marker), which answers with it."""
    try:
        view = _View(children, _view_argv(cfg, b, host), _view_env(), b.name)
    except (BrowserError, OSError) as exc:
        detail = exc.message if isinstance(exc, BrowserError) else (exc.strerror or str(exc))
        raise _view_failed(b, session, f"it could not be started ({detail})") from None
    if _wait_port(host, b.view_port, READY_LIMIT, view.process, own=True):
        return view
    if _running(view.process):
        raise _view_failed(b, session, f"it did not listen there in {READY_LIMIT:g} s")
    raise _view_failed(b, session, "it ended before it listened there")


def _view_failed(b: Browser, session: str, why: str) -> BrowserError:
    """The failure of a view that did not come up, written down for the lease that started the session."""
    failure = BrowserError(
        Code.BROWSER_DOWN,
        f"browser {b.name}'s view (websockify on port {b.view_port}) did not come up: {why}; "
        f"its output is in `journalctl --user -u {b.unit}`",
        {"view_port": b.view_port},
    )
    record = {"session": session, "message": failure.message, "data": failure.data}
    try:
        b.start_failure_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        b.start_failure_marker.write_text(json.dumps(record))
    except OSError:  # the lease then answers as for any start that did not come up
        pass
    return failure


class _View:
    """The session's view, kept running for the whole session. When it stops, or a start of it fails (websockify gone
    at that moment, a fork that fails), it is started again after a wait that doubles each time, from VIEW_RETRY_FIRST
    up to VIEW_RETRY_MAX; a view that stays up VIEW_RETRY_MAX seconds starts the waits over. A view that cannot be
    started never ends the session: Chrome keeps running, and a stop still closes it through DevTools. Only the first
    start, before Chrome starts, fails the session.

    websockify serves each connection from a process it forks, which keeps the listening socket. So each view runs in a
    process group of its own, and a view that stopped is ended with its whole group before the next start: otherwise a
    viewer still attached when websockify's main process died would keep the view's port, no start could take it, and
    new viewers and page loads would hang until that viewer left. The restart ends that viewer's connection."""

    def __init__(self, children: list[subprocess.Popen], argv: list[str], env: dict[str, str], name: str) -> None:
        self.children, self.argv, self.env, self.name = children, argv, env, name
        self.process: subprocess.Popen | None = _spawn(children, argv, env=env, own_group=True)
        self.started = time.monotonic()
        self.wait = VIEW_RETRY_FIRST
        self.next_try = 0.0

    def tend(self) -> None:
        """One turn of the session loop: note a view that stopped (and end what is left of its group), and start one
        when its wait is over."""
        now = time.monotonic()
        if self.process is not None:
            if now - self.started >= VIEW_RETRY_MAX:
                self.wait = VIEW_RETRY_FIRST  # it stayed up: a later stop is met promptly again
            if _running(self.process):
                return
            self.children.remove(self.process)
            _end(self.process)  # the processes it forked, which would keep the port, go with it
            self.process = None
            self._later(now)
        if now < self.next_try:
            return
        try:
            self.process = _spawn(self.children, self.argv, env=self.env, own_group=True)
        except (BrowserError, OSError) as exc:
            wait = self._later(now)
            why = exc.message if isinstance(exc, BrowserError) else str(exc)
            print(
                f"orion-browser: browser {self.name}'s view could not be started again ({why}); "
                f"trying again in {wait:g} s",
                file=sys.stderr, flush=True,
            )
            return
        self.started = now

    def _later(self, now: float) -> float:
        """Put the next start off by the current wait, and double the wait for the one after. Returns the wait used."""
        wait, self.wait = self.wait, min(self.wait * 2, VIEW_RETRY_MAX)
        self.next_try = now + wait
        return wait


class _GroupLeader(subprocess.Popen):
    """A child started in a process group of its own (the group's id is its pid), ended with that whole group."""


def _spawn(
    children: list[subprocess.Popen], argv: list[str], env: dict[str, str] | None = None,
    pass_fds: tuple[int, ...] = (), own_group: bool = False,
) -> subprocess.Popen:
    kind, group = (_GroupLeader, 0) if own_group else (subprocess.Popen, None)
    try:
        child = kind(argv, stdin=subprocess.DEVNULL, env=env, pass_fds=pass_fds, process_group=group)
    except FileNotFoundError:
        raise BrowserError(Code.USAGE, f"{argv[0]} is not installed; run `orion-browser setup` to see what is missing") from None
    children.append(child)
    return child


def _start_screen(children: list[subprocess.Popen], cfg: Config, b: Browser, host: str) -> None:
    """Start the screen and wait until it is this session's own. Xvnc reports its display on a pipe (-displayfd) only
    once it owns the display and listens, and exits without a word when another X server holds the display. The VNC
    port alone could be answered by another session's Xvnc on the same screen, started a moment earlier."""
    read_end, write_end = os.pipe()
    try:
        screen = _spawn(children, _screen_argv(cfg, b, host, write_end), pass_fds=(write_end,))
    finally:
        os.close(write_end)
    try:
        reported = _read_line(read_end, READY_LIMIT)
    finally:
        os.close(read_end)
    if reported != str(b.screen) or not _wait_port(host, b.vnc_port, READY_LIMIT, screen):
        raise BrowserError(
            Code.BROWSER_DOWN, f"the screen {b.display} did not come up in {READY_LIMIT:g} s, or another X server holds it"
        )


def _read_line(fd: int, limit: float) -> str | None:
    """The first line written to fd within limit seconds; None when the writer closes it first or time runs out."""
    deadline = time.monotonic() + limit
    data = b""
    while b"\n" not in data:
        left = deadline - time.monotonic()
        if left <= 0 or not select.select([fd], [], [], left)[0]:
            return None
        chunk = os.read(fd, 64)
        if not chunk:
            return None
        data += chunk
    return data.split(b"\n", 1)[0].decode(errors="replace").strip()


def _wait_port(host: str, port: int, limit: float, process: subprocess.Popen, own: bool = False) -> bool:
    """Whether host:port accepts connections within limit seconds while the process runs (asked without reaping it, as
    _running does). With `own`, only while the process itself listens there (procs.listens): another program's answer
    on the port is not the process's."""
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not _running(process):
            return False
        if not own or procs.listens(process.pid, host, port):
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except OSError:
                pass
        time.sleep(0.1)
    return False


def _focus(b: Browser, pid: int, stop: _StopRequest) -> None:
    """Give X input focus to Chrome's top-level window. With no window manager on the screen nothing else would, and a
    page without focus can refuse typing (Q1, settled 2026-10-07). A failure leaves the health proof to report it. A
    stop signal ends the wait for the window at once: the stop then closes Chrome."""
    env = {**os.environ, "DISPLAY": b.display}
    deadline = time.monotonic() + READY_LIMIT
    while time.monotonic() < deadline and not stop.requested:
        try:
            found = subprocess.run(
                ["xdotool", "search", "--all", "--onlyvisible", "--pid", str(pid), "--name", "Google Chrome$"],
                capture_output=True, text=True, timeout=5, env=env, stdin=subprocess.DEVNULL,
            )
            windows = found.stdout.split()
            if windows:
                subprocess.run(
                    ["xdotool", "windowfocus", "--sync", windows[0]],
                    capture_output=True, timeout=5, env=env, stdin=subprocess.DEVNULL,
                )
                return
        except (OSError, subprocess.TimeoutExpired):
            return
        time.sleep(POLL)


def _stopped_while_starting(b: Browser, lease: Lease, audit: Audit) -> Result:
    """A stop signal that arrived before Chrome started: nothing of Chrome to close. Written down as a stop that found
    nothing running (none), for the tool's stop to read, and recorded as the session's own stop."""
    b.stop_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    b.stop_marker.write_text(json.dumps({"session": lease.session, "stop": "none"}))
    audit.record(b.name, lease.holder, "service-stop", lease.session, stop="none")
    return Result(Code.OK, f"stopped browser {b.name} before its Chrome started")


def _stop_for_signal(cfg: Config, b: Browser, lease: Lease, chrome: subprocess.Popen, audit: Audit) -> Result:
    method = _end_chrome(b, lease, chrome, cfg.stop_grace, audit)
    b.stop_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    b.stop_marker.write_text(json.dumps({"session": lease.session, "stop": method}))
    audit.record(b.name, lease.holder, "service-stop", lease.session, stop=method)
    return Result(Code.OK, f"stopped browser {b.name} ({method})")


def _chrome_ended(b: Browser, lease: Lease, code: int, audit: Audit) -> Result:
    """Chrome exited. On the tool's DevTools close (its closing marker names this session) that is a clean stop, written
    down as _stop_for_signal writes one: the session's teardown can outlast the tool's grace, and the tool then reads
    how the stop ended from this marker. Otherwise it is a crash."""
    if _closing(b, lease.session):
        b.stop_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        b.stop_marker.write_text(json.dumps({"session": lease.session, "stop": "clean"}))
        return Result(Code.OK, f"browser {b.name} closed")
    audit.record(b.name, lease.holder, "crashed", lease.session, exit_code=code)
    return Result(Code.BROWSER_DOWN, f"Chrome of browser {b.name} exited on its own with code {code}")


def _end_chrome(b: Browser, lease: Lease, chrome: subprocess.Popen, grace: float, audit: Audit) -> str:
    """End Chrome for a stop signal: ask it to close through DevTools (the only stop shown to save recent cookies), then
    signal it, then kill it. The close goes only to an endpoint this service's own processes listen on: a port file
    written by a Chrome the tool did not start (on the same profile) names another, and that Chrome is never closed or
    signalled. Nobody listens there either when this session's Chrome took the tool's close and has already shut its
    DevTools listener while it finishes, and Chrome removes the port file itself as it shuts down. A skipped close is
    recorded with why, and this session's own Chrome always gets the grace to exit before its signal."""
    try:
        port_file = devtools.read_port_file(b.port_file)
    except devtools.DevToolsError:
        port_file = None
    if port_file is None:  # none to close through: never written, or removed by a Chrome finishing a close it took
        audit.record(b.name, lease.holder, "close-skipped", lease.session, by="service", reason="no readable port file")
    elif procs.tree_listens(os.getpid(), loopback(), port_file.port):
        devtools.close_browser(port_file.endpoint)
    else:  # another program's endpoint, or Chrome already shut DevTools while it finishes a close it took
        audit.record(
            b.name, lease.holder, "close-skipped", lease.session, by="service", port=port_file.port,
            reason="no process of the session listens on the port",
        )
    if _exited(chrome, grace):
        return "clean"
    chrome.terminate()
    if _exited(chrome, min(5.0, grace)):
        return "signal"
    chrome.kill()
    chrome.wait()
    return "kill"


def _exited(process: subprocess.Popen, seconds: float) -> bool:
    try:
        process.wait(timeout=seconds)
        return True
    except subprocess.TimeoutExpired:
        return False


def _end(child: subprocess.Popen) -> None:
    if isinstance(child, _GroupLeader):
        _end_group(child)
    elif child.poll() is None:
        child.terminate()
        if not _exited(child, 5):
            child.kill()
            child.wait()


def _end_group(leader: _GroupLeader) -> None:
    """End the leader's whole process group: the leader and every process it forked (websockify's per-connection
    processes, each holding the listening socket). SIGTERM first (websockify then ends what it forked, and those end on
    it too), then, once the leader has ended or after 5 s, SIGKILL for whatever is left. The group is signalled only
    while its leader is unreaped (running, or ended and still a zombie): until then the group's id, the leader's pid,
    cannot be another process's, so no signal reaches anything outside the group. The leader is reaped last."""
    if leader.returncode is None:
        _signal_group(leader.pid, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while _running(leader) and time.monotonic() < deadline:
            time.sleep(0.05)
        _signal_group(leader.pid, signal.SIGKILL)
    leader.wait()


def _signal_group(group: int, signum: int) -> None:
    try:
        os.killpg(group, signum)
    except ProcessLookupError:  # nothing is left in it
        pass


def _running(process: subprocess.Popen) -> bool:
    """Whether the process still runs, asked without reaping it: one that has ended stays a zombie, whose pid (for a
    group leader, the group's id) no other process can be given until _end reaps it."""
    if process.returncode is not None:
        return False
    try:
        return os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None
    except ChildProcessError:  # reaped already: it does not run
        return False


def _closing(b: Browser, session: str) -> bool:
    try:
        return b.closing_marker.read_text() == session
    except OSError:
        return False
