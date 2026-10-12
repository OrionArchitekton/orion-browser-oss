"""Fake Google Chrome: writes the DevTools port file into its profile and answers the DevTools calls orion-browser
makes. A clean close (Browser.close) writes Cookies.fake into the profile; SIGTERM ends it without writing that file, as
real Chrome loses recently set cookies on SIGTERM. At launch it records the profile's Preferences file as it finds it,
so a test can tell whether the preferences were on disk before Chrome started. Knobs per profile: see
fakesys.CHROME_DEFAULTS; the ones that shape an answer are read again for every request, so a test can change them
while the session runs.

An outdated build (the `outdated` knob, on by default: the real Chrome was one at the first smoke test, older than
Chrome's eight-week outdated-build age) opens its "Can't update Chrome" bubble as it starts, unless it was started
with the OutdatedBuildDetector feature turned off (the last --disable-features list names it, as Chrome reads that
switch). The bubble keeps the keyboard focus, so the page reports no focus however the window was given it: real
Chrome opens the bubble a moment after it starts, and nothing in orion-browser closes it.

Started on a data: page (the last page its command line names), the page has the focus once the window has it.
Started on anything else, it keeps the keyboard focus in its address bar, so the page reports no focus: real Chrome
did so on about:blank at the first smoke test (however late the window got the focus, and after a navigation over
DevTools too), and on its New Tab page, which it opens when no page is named. (Real Chrome does focus a page it starts
on from the web; the fake does not tell those apart, since a session never starts on one.)

Its pages report navigator.webdriver as Chrome 155 does: true whenever it runs with a DevTools port, with no client
attached, unless a --disable-blink-features list names AutomationControlled (the `ignores_automation_flag`
knob makes it true regardless, as a later Chrome that no longer honors the flag would). It answers an evaluation with
what the expression asks for: the webdriver value only when the expression reads navigator.webdriver. (The tool passes
one --disable-blink-features list, so how Chrome treats repeated lists does not matter here.)

SIGTERM is handled from before the port file exists, so whoever sees that file can stop it at once. The handler only
appends to a list that the main loop checks: Event.set() takes the Event's lock, and a signal that lands while the main
thread holds it inside wait() would deadlock the thread against itself (1 SIGTERM in 100 to 1,000). The port file is
written whole and renamed into place (real Chrome creates it and then fills it, which a reader can catch half done; no
caller depends on that, and the tests would only flake on it)."""

from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
import uuid
from http import HTTPStatus
from pathlib import Path

from websockets.sync.server import serve

import fakesys

DEEP = 100_000  # deep_state's nesting: far past the recursion limit of any JSON parser


def _record_preferences(profile: Path) -> None:
    """Record the Preferences file Chrome reads at launch, as it found it (null when there was none), in
    preferences-at-launch-<profile> under FAKE_DIR. Written whole and renamed into place: a reader sees no record, or
    the whole one."""
    try:
        found = (profile / "Default" / "Preferences").read_bytes().decode("utf-8", "replace")
    except FileNotFoundError:
        found = None
    record = fakesys.root() / f"preferences-at-launch-{profile.name}"
    temporary = record.with_name(f"{record.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps({"preferences": found}))
    os.replace(temporary, record)


def main(argv: list[str]) -> int:
    if argv == ["--version"]:  # answered as Chrome answers it, and not a launch, so not recorded as one
        print("Google Chrome 150.0.0.0 ")
        return 0
    fakesys.log_call("chrome", argv)
    profile = Path(next(arg.split("=", 1)[1] for arg in argv if arg.startswith("--user-data-dir=")))
    knobs = fakesys.chrome_knobs(profile.name)
    disabled = [arg.split("=", 1)[1].split(",") for arg in argv if arg.startswith("--disable-features=")]
    blink_disabled = [arg.split("=", 1)[1].split(",") for arg in argv if arg.startswith("--disable-blink-features=")]
    debug_port = any(arg.startswith("--remote-debugging-port=") for arg in argv)
    # The tool passes one list, so how Chrome treats repeated lists does not matter here: any list naming it counts.
    unmarked = any("AutomationControlled" in names for names in blink_disabled)
    bubble = knobs["outdated"] and "OutdatedBuildDetector" not in (disabled[-1] if disabled else [])
    start_page = next((arg for arg in reversed(argv) if not arg.startswith("-")), "") or "chrome://newtab/"
    address_bar = not start_page.startswith("data:")  # holds the keyboard focus for good (see the docstring)
    stop = threading.Event()  # set by Browser.close, from a connection thread
    # With close_delay, Browser.close sets when to exit instead (a monotonic time), and the main loop exits then. Not a
    # threading.Timer: this Python times lock waits on the wall clock, so a clock step can fire a Timer early.
    close_by: list[float] = []
    terminated: list[int] = []  # the SIGTERM handler appends here; it must not take a lock (see the docstring)

    def on_sigterm(signum: int, frame: object) -> None:
        if not knobs["ignore_sigterm"]:
            terminated.append(signum)

    signal.signal(signal.SIGTERM, on_sigterm)  # before the port file: a SIGTERM landing anywhere ends it cleanly
    _record_preferences(profile)  # what it finds at launch, before it does anything else in the profile
    display = os.environ.get("DISPLAY", "")
    profile.mkdir(parents=True, exist_ok=True)
    launch = fakesys.count_launch(profile.name)
    browser_id = uuid.uuid4().hex
    other_id = uuid.uuid4().hex  # what wrong_endpoint reports instead: another browser's endpoint
    host = fakesys.loopback()

    def process_request(connection, request):
        if request.path == "/json/version":
            answers = fakesys.chrome_knobs(profile.name)
            stalled_until = time.monotonic() + answers["stall_version"]
            while time.monotonic() < stalled_until and not (stop.is_set() or terminated):
                time.sleep(0.05)
            agent ="HeadlessChrome/150.0.0.0" if answers["headless"] else "Chrome/150.0.0.0"
            user_agent = answers["user_agent"]
            if user_agent is None:
                user_agent = f"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 {agent} Safari/537.36"
            reported = other_id if answers["wrong_endpoint"] else browser_id
            return connection.respond(HTTPStatus.OK, json.dumps({
                "Browser": "Chrome/150.0.0.0",
                "User-Agent": user_agent,
                "webSocketDebuggerUrl": f"ws://{host}:{port}/devtools/browser/{reported}",
            }))
        if request.path == "/json/list":
            page_answers = fakesys.chrome_knobs(profile.name)["page_answers"]
            count = len(page_answers) if page_answers is not None else 1
            return connection.respond(HTTPStatus.OK, json.dumps(
                [{"type": "page", "id": f"PAGE{n + 1}", "url": start_page, "title": start_page}
                 for n in range(count)]
            ))
        return None  # anything else is a websocket handshake

    def handler(ws) -> None:
        path = ws.request.path
        for raw in ws:
            message = json.loads(raw)
            reply = {"id": message["id"], "result": {}}
            method = message.get("method")
            if path == f"/devtools/browser/{browser_id}" and method == "Browser.close":
                ws.send(json.dumps(reply))
                if not knobs["ignore_close"]:
                    (profile / "Cookies.fake").write_text("saved")
                    if knobs["close_delay"]:  # it exits that long after it took the close
                        close_by.append(time.monotonic() + knobs["close_delay"])
                    else:
                        stop.set()
                continue
            if path.startswith("/devtools/page/PAGE") and method == "Runtime.evaluate":
                answers = fakesys.chrome_knobs(profile.name)
                page_index = int(path.rsplit("PAGE", 1)[1]) - 1
                if page_index in answers["page_errors"]:
                    ws.send(json.dumps({"id": message["id"], "error": {"message": "target closed"}}))
                    continue
                focused = (fakesys.root() / f"focus-{display}").exists() and not (bubble or address_bar)
                expression = message.get("params", {}).get("expression", "")
                if answers["stringify_spoof"] and "JSON.stringify" in expression:
                    value = '["visible",true,false]'
                elif answers["page_answers"] is not None:
                    value = answers["page_answers"][page_index]
                elif answers["deep_state"]:  # a page that replaced JSON.stringify can answer anything
                    value = "[" * DEEP + "]" * DEEP
                elif answers["page_answer"] is not None:
                    value = answers["page_answer"]
                else:
                    state = ["visible" if answers["visible"] else "hidden", focused]
                    if "navigator.webdriver" in expression:
                        state.append(debug_port and (answers["ignores_automation_flag"] or not unmarked))
                    value = json.dumps(state) if "JSON.stringify" in expression else state
                kind = "string" if isinstance(value, str) else "object"
                reply["result"] = {"result": {"type": kind, "value": value}}
            ws.send(json.dumps(reply))

    server = serve(handler, host, 0, process_request=process_request)
    port = server.socket.getsockname()[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    if launch > knobs["no_port_file_launches"]:
        temporary = profile / f"DevToolsActivePort.{os.getpid()}.tmp"
        temporary.write_text(f"{port}\n/devtools/browser/{browser_id}\n")
        os.replace(temporary, profile / "DevToolsActivePort")  # atomic: a reader sees no file, or the whole one
    drawn = fakesys.root() / f"drawn-{display}"
    drawn.write_text(str(os.getpid()))
    crash_at = time.monotonic() + knobs["crash_after"] if knobs["crash_after"] is not None else None
    while not (stop.wait(0.05) or terminated):
        if crash_at is not None and time.monotonic() > crash_at:
            drawn.unlink(missing_ok=True)
            os._exit(1)
        if close_by and time.monotonic() >= close_by[0]:
            break
    drawn.unlink(missing_ok=True)
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
