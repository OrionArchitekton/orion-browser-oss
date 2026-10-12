"""Hand-run smoke test against real Chrome, Xvnc, websockify, xdotool and xwd. Not collected by pytest.

    python tests/smoke_real_browser.py [--keep]

It writes a config of its own in a temporary directory (state and profiles there) with one browser, `smoke`, on
screen :89. No operator config may use that name, that screen or its ports (VNC 5989, view 6169), so a run never
meets an operator's session, and a run refuses to start while another smoke session (an orion-browser-smoke-*
service in any state but inactive or failed) is loaded. It runs the real CLI, drives the session with agent-browser,
checks the view (its page loads at the top level and without Fetch Metadata, a load into a frame is refused, and it
takes a WebSocket only from its own page, through to the screen), has the smoke browser itself frame the view and open
it at the top level (judged from Chrome's own DevTools record of those loads, and the view's log), checks that no stop
skipped its DevTools close (each close reached the session's own Chrome), and prints one JSON summary, with the config
path and Chrome's version and build age, and an "error" naming what stopped the run when something did (a failed
agent-browser command stops it). Exit 0 only when every check passes. A passing run removes its temporary directory;
with --keep it leaves it, so its config serves the operator's own look in the view; a failed run always leaves it."""

from __future__ import annotations

import argparse
import base64
import ctypes
import itertools
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from websockets.sync.client import connect

from orion_browser import procs, services
from orion_browser.config import parse as parse_config

BROWSER = "smoke"  # no operator browser may be called smoke
SCREEN = 89  # VNC port 5989, view port 6169
UNITS = f"orion-browser-{BROWSER}-*.service"  # a smoke session's service, whichever lease store it belongs to
HOLDER = "smoke-test"
FRAMES = ("iframe", "frame", "embed", "object", "fencedframe")  # Sec-Fetch-Dest of a load into another page: refused
TOP_LEVEL = {"Sec-Fetch-Dest": "document", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Site": "none"}  # a tab of its own
GUARD_REFUSED_FRAME = "refused a load into a frame"  # the view guard's own log line for each load it refuses
BLOCKED_BY_CHROME = "net::ERR_BLOCKED_BY_"  # how Chrome's own block errors begin (local network access checks, ...)
# Put a frame of the view on the page the smoke browser shows. Not part of the page agent-browser opens: a frame that
# Chrome held back (a permission prompt, say) would keep the page loading, and agent-browser's open would time out.
ADD_FRAME = "(() => { const f = document.createElement('iframe'); f.src = %s; " \
    "(document.body || document.documentElement).appendChild(f); return 'framed'; })()"
_HTTP = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_LIBC = ctypes.CDLL(None, use_errno=True)
_SYS_PIDFD_SEND_SIGNAL, _SYS_PIDFD_OPEN = 424, 434  # Linux system call numbers, x86-64 and arm64 alike


def cli(env: dict[str, str], *args: str) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "orion_browser", "--json", *args], capture_output=True, text=True, env=env, timeout=300
    )
    return json.loads(proc.stdout)


def drive(session: str, *args: str) -> str:
    """One agent-browser command against the pool browser. A command that fails stops the run: after a failed connect,
    a later command would run in agent-browser's own fallback browser instead of the pool's (spec section 13)."""
    proc = subprocess.run(["agent-browser", "--session", session, *args], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        said = (proc.stderr or proc.stdout).strip()[-200:]
        raise RuntimeError(f"agent-browser {args[0]} exited {proc.returncode}: {said}")
    return proc.stdout.strip()


def evaluate(session: str, expression: str) -> object:
    """agent-browser prints the value as JSON; JSON.stringify results come back as a JSON string of JSON."""
    value = json.loads(drive(session, "eval", expression))
    return json.loads(value) if isinstance(value, str) and value[:1] in "[{" else value


def http_status(url: str, headers: dict[str, str]) -> int:
    """The status of a GET of url sent with these headers, an error status included."""
    try:
        return _HTTP.open(urllib.request.Request(url, headers=headers), timeout=10).status
    except urllib.error.HTTPError as exc:
        return exc.code


class ChromeNetwork:
    """Chrome's own record of its requests for one page (the view's), read through DevTools on a connection of the
    smoke test's own, beside agent-browser's. Every page gets the Network domain, and so does every frame target under
    a page (a frame of another site runs in a target of its own), so the headers recorded are the ones Chrome sent
    (Network.requestWillBeSentExtraInfo) and the status the one it got. One thread opens the connection (as a context
    manager, closed when the thread ends), reads it, and turns the Network domain on for a new frame target at once.
    Each request is filed under the step current when Chrome sent it."""

    def __init__(self, endpoint: str, url: str) -> None:
        self.url, self.step, self.error = url, "", ""
        self.requests: dict[str, dict] = {}
        self._early: dict[str, dict] = {}  # fields of a request whose requestWillBeSent has not come yet
        self._page_ids: set[int] = set()  # Network.enable commands sent to pages
        self.pages_on = 0  # pages whose Network domain is on
        self._ids = itertools.count(1)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._ws = None
        self._thread = threading.Thread(target=self._run, args=(endpoint,), daemon=True)
        self._thread.start()

    def _send(self, method: str, params: dict | None = None, session: str | None = None) -> int:
        number = next(self._ids)
        message: dict[str, object] = {"id": number, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        self._ws.send(json.dumps(message))
        return number

    def _run(self, endpoint: str) -> None:
        try:
            with connect(endpoint, max_size=None, max_queue=None, proxy=None, open_timeout=10) as ws:
                self._ws = ws
                self._send("Target.setDiscoverTargets", {"discover": True})  # targetCreated for each page, open too
                while not self._stop.is_set():
                    try:
                        message = json.loads(ws.recv(timeout=0.2))
                    except TimeoutError:
                        continue
                    self._handle(message)
        except Exception as exc:  # the connection failed or ended: what was recorded stays
            self.error = f"{type(exc).__name__}: {exc}"

    def _handle(self, message: dict) -> None:
        if "id" in message:
            if message["id"] in self._page_ids and "error" not in message:
                with self._lock:
                    self.pages_on += 1
            return
        method, params = message.get("method", ""), message.get("params", {})
        if method == "Target.targetCreated" and params["targetInfo"]["type"] == "page":
            self._send("Target.attachToTarget", {"targetId": params["targetInfo"]["targetId"], "flatten": True})
        elif method == "Target.attachedToTarget":
            session = params["sessionId"]
            number = self._send("Network.enable", session=session)
            if params["targetInfo"]["type"] == "page":
                self._page_ids.add(number)
            auto = {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True}
            self._send("Target.setAutoAttach", auto, session=session)
        elif method.startswith("Network."):
            self._note(method, params)

    def _note(self, method: str, params: dict) -> None:
        request = params.get("requestId", "")
        with self._lock:
            if method == "Network.requestWillBeSent":
                if params["request"]["url"].split("?")[0] == self.url and request not in self.requests:
                    found = {"type": params.get("type"), "sec_fetch_dest": None, "sec_fetch_site": None,
                             "status": None, "failed": None}
                    self.requests[request] = {"step": self.step, **found, **self._early.pop(request, {})}
                return
            if method == "Network.requestWillBeSentExtraInfo":
                headers = {name.lower(): value for name, value in params.get("headers", {}).items()}
                fields = {key.replace("-", "_"): headers.get(key) for key in ("sec-fetch-dest", "sec-fetch-site")}
            elif method == "Network.responseReceived":
                fields = {"status": params["response"]["status"]}
            elif method == "Network.responseReceivedExtraInfo":
                fields = {"status": params.get("statusCode")}
            elif method == "Network.loadingFailed":
                keys = ("errorText", "blockedReason", "corsErrorStatus", "canceled")
                fields = {"failed": {key: params[key] for key in keys if key in params}}
            else:
                return
            if request in self.requests:
                self.requests[request].update(fields)
            else:
                self._early.setdefault(request, {}).update(fields)

    def ready(self, limit: float = 10.0) -> bool:
        """Whether a page has the Network domain on, within limit seconds (and a moment more for any other page)."""
        deadline = time.monotonic() + limit
        while self.pages_on == 0 and not self.error and time.monotonic() < deadline:
            time.sleep(0.05)
        time.sleep(0.5)
        return self.pages_on > 0

    def begin(self, step: str) -> None:
        with self._lock:
            self.step = step

    def outcome(self, step: str, limit: float = 10.0) -> list[dict]:
        """The requests filed under step, once each has a status or a failure (or after limit seconds), with a moment
        more for the headers Chrome reports beside them."""
        deadline = time.monotonic() + limit
        while time.monotonic() < deadline:
            with self._lock:
                found = [r for r in self.requests.values() if r["step"] == step]
                if found and all(r["status"] is not None or r["failed"] for r in found):
                    break
            time.sleep(0.1)
        time.sleep(0.5)
        with self._lock:
            return [{k: v for k, v in r.items() if k != "step"} for r in self.requests.values() if r["step"] == step]

    def close(self) -> None:
        """Stop reading; the thread's with block closes the connection."""
        self._stop.set()
        self._thread.join(timeout=15)


def framer_page() -> ThreadingHTTPServer:
    """A plain page served on loopback by the smoke test itself, as another local program's page would be, for the
    smoke browser to open and frame the view from. Stop it with shutdown()."""

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
            body = b"<!doctype html><title>orion-browser smoke framer</title><body></body>"
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def skipped_closes(state: Path) -> list[dict]:
    """The smoke browser's close-skipped records: a stop of the session that sent no DevTools close because it could
    not show the port file's endpoint was the session's own. A run of the real stack must have none."""
    return [
        record for path in sorted((state / "audit" / BROWSER).glob("*.jsonl"))
        for record in map(json.loads, path.read_text().splitlines()) if record.get("event") == "close-skipped"
    ]


def guard_log(unit: str) -> list[str]:
    """The view guard's refusals of a load into a frame, as the session's log holds them."""
    out = subprocess.run(
        ["journalctl", "--user", "-u", unit, "-o", "cat", "--no-pager"], capture_output=True, text=True, timeout=30,
    ).stdout
    return [line for line in out.splitlines() if GUARD_REFUSED_FRAME in line]


def guard_log_since(unit: str, before: list[str], limit: float = 5.0) -> list[str]:
    """The refusals logged since `before` was read, waiting up to limit seconds for the log to show one."""
    deadline = time.monotonic() + limit
    while True:
        now = guard_log(unit)
        if len(now) > len(before) or time.monotonic() >= deadline:
            return now[len(before):]
        time.sleep(0.2)


def stopped_by(requests: list[dict]) -> str | None:
    """Which layer kept Chrome's framed load of the view's page from loading, shown for every request Chrome made for
    it: "chrome: <its error>" when Chrome blocked the request itself (a net::ERR_BLOCKED_BY_... error, such as its
    local network access checks), "guard: <status>" when the view answered 4xx. None when that is not shown: a request
    answered 2xx (or anything but 4xx), one still unanswered or failed some other way, or no request recorded at all."""
    if not requests:
        return None
    layers = set()
    for request in requests:
        status, error = request["status"], str((request["failed"] or {}).get("errorText", ""))
        if status is not None:
            if not 400 <= status < 500:
                return None
            layers.add(f"guard: {status}")
        elif error.startswith(BLOCKED_BY_CHROME):
            layers.add(f"chrome: {error}")
        else:
            return None
    return ", ".join(sorted(layers))


def refused_in_a_frame(requests: list[dict], logged: list[str]) -> bool:
    """Chrome's own load of the view's page into a frame was refused by the view: Chrome made the request, got 403 for
    it and never anything else, and the request was one for a frame (Chrome's own Sec-Fetch-Dest: iframe, or the
    guard's log line for this step naming it)."""
    for_a_frame = any(r["sec_fetch_dest"] == "iframe" for r in requests) or any(
        "Sec-Fetch-Dest: iframe" in line for line in logged
    )
    return bool(requests) and all(r["status"] == 403 for r in requests) and for_a_frame


def ws_answer(watch: str, origin: str) -> tuple[int, bytes]:
    """The view's answer to a WebSocket upgrade from `origin`: the HTTP status (101 when it accepts the connection) and,
    once accepted, the payload of the first frame it sends, which is the screen's VNC banner ("RFB 003.008\\n") when
    the view reaches the screen. websockify answers 101 before it connects to the screen, so a 101 alone shows only
    that the connection was accepted."""
    parts = urllib.parse.urlsplit(watch)
    key = base64.b64encode(os.urandom(16)).decode()
    request = (
        f"GET /websockify HTTP/1.1\r\nHost: {parts.netloc}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\nOrigin: {origin}\r\n\r\n"
    )
    with socket.create_connection((parts.hostname, parts.port), timeout=10) as conn:
        conn.sendall(request.encode())
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = conn.recv(4096)
            if not chunk:
                break
            data += chunk
        head, _, rest = data.partition(b"\r\n\r\n")
        status = head.split(b"\r\n", 1)[0].split()
        code = int(status[1]) if len(status) > 1 and status[1].isdigit() else 0
        return code, (_first_frame(conn, rest) if code == 101 else b"")


def _first_frame(conn: socket.socket, data: bytes) -> bytes:
    """The payload of the first frame on the connection when it is a binary frame, unmasked as a server's frames are;
    b"" for anything else (a close frame, which websockify sends when it cannot reach the screen, or the connection
    ending first). `data` is what was read past the handshake."""
    def fill(size: int) -> bool:
        nonlocal data
        while len(data) < size:
            chunk = conn.recv(4096)
            if not chunk:
                return False
            data += chunk
        return True

    if not fill(2) or data[0] & 0x0F != 0x2 or data[1] & 0x80:
        return b""
    length, start = data[1] & 0x7F, 2
    if length > 125:  # 126: a 16-bit length follows; 127: a 64-bit one
        start = 4 if length == 126 else 10
        if not fill(start):
            return b""
        length = int.from_bytes(data[2:start], "big")
    return data[start:start + length] if fill(start + length) else b""


def drop_daemon(session: str) -> None:
    """Stop agent-browser's daemon for a session without `close` (which would act on the browser), in this order: a
    pidfd for the process the pid file names (none: the process is gone, nothing to stop); the check that it is that
    session's daemon (an agent-browser program whose environment names the session, which its command line does not,
    and marks it the daemon); SIGTERM through the pidfd, which reaches only that process even if another has taken its
    pid since the pidfd was opened; the pidfd closed."""
    pid_file = Path(f"/run/user/{os.getuid()}/agent-browser/{session}.pid")
    try:
        pid = int(pid_file.read_text().strip())
        pidfd = _pidfd_open(pid)
    except (OSError, ValueError, OverflowError):  # no pid file, its process is gone, or a pid no pidfd can name
        return
    try:
        program = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")[0]
        environ = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        if (
            os.path.basename(program).startswith(b"agent-browser")
            and f"AGENT_BROWSER_SESSION={session}".encode() in environ
            and b"AGENT_BROWSER_DAEMON=1" in environ
        ):
            _pidfd_send_signal(pidfd, signal.SIGTERM)
    except OSError:  # it ended meanwhile
        pass
    finally:
        os.close(pidfd)


def _pidfd_open(pid: int) -> int:
    """os.pidfd_open, or the system call itself where this Python was built without it (uv's build of 3.11, which the
    venv uses, was). OSError when there is no such process."""
    if hasattr(os, "pidfd_open"):
        return os.pidfd_open(pid)
    fd = _LIBC.syscall(ctypes.c_long(_SYS_PIDFD_OPEN), ctypes.c_long(pid), ctypes.c_long(0))
    if fd < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))
    return fd


def _pidfd_send_signal(fd: int, signum: int) -> None:
    """signal.pidfd_send_signal, or the system call itself: the signal reaches only the process the pidfd was opened
    for. OSError when that process has ended."""
    if hasattr(signal, "pidfd_send_signal"):
        signal.pidfd_send_signal(fd, signum)
        return
    sent = _LIBC.syscall(
        ctypes.c_long(_SYS_PIDFD_SEND_SIGNAL), ctypes.c_long(fd), ctypes.c_long(signum), None, ctypes.c_long(0)
    )
    if sent < 0:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Smoke test against the real browser stack (hand-run).")
    parser.add_argument(
        "--keep", action="store_true", help="leave the temporary folder: its config serves the operator's own look"
    )
    args = parser.parse_args(argv)
    if shutil.which("agent-browser") is None:
        print("agent-browser is needed for the smoke test")
        return 2
    loaded = subprocess.run(
        ["systemctl", "--user", "list-units", "--all", "--plain", "--no-legend", UNITS],
        capture_output=True, text=True, check=False,
    ).stdout.splitlines()
    running = [row.split()[0] for row in loaded if len(row.split()) > 2 and row.split()[2] not in services.GONE]
    if running:  # active, activating, deactivating, reloading, ...: a process of it may still run
        print(f"{running[0]} is not stopped: another smoke run holds screen {SCREEN}; stop a stale one with")
        print(f"  systemctl --user stop '{running[0]}'")
        return 2
    root = Path(tempfile.mkdtemp(prefix="orion-browser-smoke-"))
    config = root / "config.toml"
    config.write_text(
        f'[paths]\nstate = "{root}/state"\nprofiles = "{root}/profiles"\n\n[[browser]]\nname = "{BROWSER}"\nscreen = {SCREEN}\n'
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("ORION_BROWSER_")}
    env["ORION_BROWSER_CONFIG"] = str(config)
    sessions = [f"smoke-a-{os.getpid()}", f"smoke-b-{os.getpid()}"]
    checks: dict[str, object] = {"config": str(config)}
    try:
        setup = cli(env, "setup")
        checks["setup"] = setup["ok"]
        checks["chrome"] = setup["data"].get("chrome")  # its version, its build's age, and a note when out of date
        first = cli(env, "lease", BROWSER, "--holder", HOLDER)
        checks["lease"] = first["ok"]
        if not first["ok"]:
            checks["lease_answer"] = first
            checks["kept"] = True
            return _report(checks, ok=False)
        status = cli(env, "status", BROWSER)["data"]
        checks["healthy"] = status.get("state") == "healthy"
        checks["failed_checks"] = status.get("failed")
        checks["profile_held"] = len(procs.holding_profile(root / "profiles" / BROWSER))  # Chrome rewrites its title
        drive(sessions[0], "connect", first["data"]["endpoint"])
        drive(sessions[0], "open", "https://example.com")
        checks["page"] = evaluate(
            sessions[0],
            "JSON.stringify({headless: navigator.userAgent.includes('Headless'), "
            "focused: document.hasFocus(), visibility: document.visibilityState, webdriver: navigator.webdriver})",
        )
        evaluate(sessions[0], "document.cookie = 'orionsmoke=yes; max-age=86400; path=/'")
        checks["view_page"] = _HTTP.open(first["data"]["watch"], timeout=10).status == 200  # no Fetch Metadata
        checks["view_top_level"] = http_status(first["data"]["watch"], TOP_LEVEL) == 200
        in_a_frame = {**TOP_LEVEL, "Sec-Fetch-Site": "cross-site"}  # as a frame on another site's page loads it
        framed = {dest: http_status(first["data"]["watch"], {**in_a_frame, "Sec-Fetch-Dest": dest}) for dest in FRAMES}
        checks["view_framed_statuses"] = framed
        checks["view_framed_refused"] = all(status == 403 for status in framed.values())
        own = urllib.parse.urlsplit(first["data"]["watch"])
        code, banner = ws_answer(first["data"]["watch"], f"{own.scheme}://{own.netloc}")
        checks["view_banner"] = banner[:32].decode("ascii", "replace").strip()  # the screen's, through the view
        checks["view_own_origin"] = code == 101 and banner.startswith(b"RFB ")
        checks["view_foreign_origin_refused"] = ws_answer(first["data"]["watch"], "http://example.com")[0] != 101
        view_page = f"{own.scheme}://{own.netloc}/vnc.html"  # no autoconnect: loaded, it connects nowhere
        unit = parse_config(tomllib.loads(config.read_text()), config).browser(BROWSER).unit
        network, framer = ChromeNetwork(first["data"]["endpoint"], view_page), framer_page()
        try:
            checks["chrome_network_ready"] = network.ready()
            # From a page not on the local host the frame must never load, whichever layer stops it (Chrome's local
            # network access checks, or the guard). From a page on the local host, which Chrome lets through, the
            # guard must: Chrome sent Sec-Fetch-Dest: iframe and the view answered 403.
            framers = {
                "view_framed_by_chrome": "about:blank",
                "view_framed_by_chrome_from_loopback": f"http://127.0.0.1:{framer.server_address[1]}/",
            }
            for key, page in framers.items():
                drive(sessions[0], "open", page)
                before = guard_log(unit)
                network.begin(key)
                evaluate(sessions[0], ADD_FRAME % json.dumps(view_page))
                requests = network.outcome(key)
                logged = guard_log_since(unit, before)
                checks[key] = {
                    "framer": page, "frame": view_page, "chrome_requests": requests, "guard_log": logged,
                    "stopped_by": stopped_by(requests),
                }
            checks["view_framed_by_chrome_refused"] = checks["view_framed_by_chrome"]["stopped_by"] is not None
            loopback = checks["view_framed_by_chrome_from_loopback"]
            checks["view_framed_by_chrome_from_loopback_refused"] = refused_in_a_frame(
                loopback["chrome_requests"], loopback["guard_log"]
            )
            network.begin("top-level")
            drive(sessions[0], "open", view_page)
            requests = network.outcome("top-level")
            checks["view_top_level_by_chrome"] = {"page": view_page, "chrome_requests": requests}
            checks["view_top_level_by_chrome_loaded"] = bool(requests) and all(
                r["status"] == 200 and r["sec_fetch_dest"] == "document" for r in requests
            )
            if network.error:
                checks["chrome_network_error"] = network.error
        finally:
            network.close()
            framer.shutdown()
        released = cli(env, "release", BROWSER, "--holder", HOLDER)
        checks["release_stop"] = released["data"].get("stop")
        checks["profile_free_after_release"] = procs.holding_profile(root / "profiles" / BROWSER) == []
        drop_daemon(sessions[0])
        second = cli(env, "lease", BROWSER, "--holder", HOLDER)
        checks["new_endpoint"] = second["ok"] and second["data"]["endpoint"] != first["data"]["endpoint"]
        drive(sessions[1], "connect", second["data"]["endpoint"])
        drive(sessions[1], "open", "https://example.com")
        checks["cookie_survived"] = "orionsmoke=yes" in str(evaluate(sessions[1], "document.cookie"))
        try:
            _HTTP.open(f"http://127.0.0.1:{first['data']['port']}/json/version", timeout=3)
            checks["old_endpoint_dead"] = False
        except OSError:
            checks["old_endpoint_dead"] = True
        checks["final_release"] = cli(env, "release", BROWSER, "--holder", HOLDER)["data"].get("stop")
        checks["profile_free_at_end"] = procs.holding_profile(root / "profiles" / BROWSER) == []
        checks["skipped_closes"] = skipped_closes(root / "state")  # each stop's close went to the session's own Chrome
        drop_daemon(sessions[1])
    except Exception as exc:  # whatever stops the run still ends in the summary, failed, with its folder kept
        checks["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:  # best effort when a check stopped the run early; NOT_HOLDER after a completed run is fine
            cli(env, "release", BROWSER, "--holder", HOLDER)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
        for session in sessions:
            drop_daemon(session)
    page = checks.get("page") if isinstance(checks.get("page"), dict) else {}
    ok = (
        "error" not in checks and checks.get("setup") and checks.get("healthy") and checks.get("view_page")
        and page.get("headless") is False and page.get("focused") is True and page.get("visibility") == "visible"
        and page.get("webdriver") is False
        and checks.get("release_stop") == "clean" and checks.get("new_endpoint") and checks.get("cookie_survived")
        and checks.get("old_endpoint_dead") and checks.get("final_release") == "clean"
        and checks.get("profile_held", 0) > 0 and checks.get("profile_free_after_release")
        and checks.get("view_own_origin") and checks.get("view_foreign_origin_refused")
        and checks.get("view_top_level") and checks.get("view_framed_refused")
        and checks.get("view_framed_by_chrome_refused") and checks.get("view_framed_by_chrome_from_loopback_refused")
        and checks.get("view_top_level_by_chrome_loaded")
        and checks.get("profile_free_at_end") and checks.get("skipped_closes") == []
    )
    if ok and not args.keep:  # nothing of the run is left running (checked above): its folder goes too
        shutil.rmtree(root)
    checks["kept"] = root.exists()
    return _report(checks, ok=bool(ok))


def _report(checks: dict[str, object], ok: bool) -> int:
    print(json.dumps({"ok": ok, **checks}, indent=1, default=str))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
