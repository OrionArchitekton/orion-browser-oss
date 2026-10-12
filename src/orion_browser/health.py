"""The health proof: positive evidence only. A missing or failed signal never counts as healthy.

status shows a proof's notes to anyone, holder or not, so a note never holds the endpoint or its port: either one
gives control of the signed-in browser. A DevTools error names the port, so the notes are fixed words, never an
error's text."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from orion_browser import devtools, xwd
from orion_browser.config import Browser

CHECKS = ("service", "endpoint", "visible", "focused", "webdriver", "headful", "screen")
PORT_FILE_SLACK = 1.0  # seconds: a port file this much older than the session's start still counts (coarse file clocks)
STATE_EXPRESSION = "[document.visibilityState, document.hasFocus(), navigator.webdriver]"
MARKED_AUTOMATED = (
    "the page does not report navigator.webdriver false: Chrome marks this browser as automated, and Google refuses "
    "sign-in in it"
)
UNREADABLE_PORT_FILE = "the port file cannot be read"
NO_PORT_FILE = "no port file from this session"
NO_ANSWER = "DevTools did not answer, or answered with something that is not DevTools"
OTHER_ADDRESS = "the endpoint answers with another address than its port file"
NO_VISIBLE_TAB = "no tab reports itself visible"
OUT_OF_TIME = "the start limit ran out before this could be checked"


@dataclass
class Health:
    checks: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(CHECKS, False))
    notes: dict[str, str] = field(default_factory=dict)
    port_file: devtools.PortFile | None = None  # the port file of this session the proof judged, if it found one

    @property
    def ok(self) -> bool:
        return all(self.checks.values())

    @property
    def failed(self) -> list[str]:
        return [name for name, passed in self.checks.items() if not passed]


class _OutOfTime(Exception):
    """The deadline passed before a step could start."""


def prove(b: Browser, started_at: float, state: str, deadline: float | None = None) -> Health:
    """Read-only: never navigates, never opens, closes or activates a tab, never types. The screen capture is judged in
    memory and never written to disk.

    `state` is the unit's state from services.state(): the service check passes only when it is exactly "active". With a
    deadline (a time.monotonic() reading), each DevTools call and the capture get no more than the time left, and a step
    with no time left is not started: its checks stay failed.

    This session's port file, when Chrome wrote one, is on the result whatever the checks find, even when the service
    has ended: a start is tried again only when its Chrome never wrote one. A caller never reads its own port file,
    which could disagree with the proof's."""
    health = Health()
    try:
        port_file, unreadable = devtools.read_port_file(b.port_file), False
    except devtools.DevToolsError:
        port_file, unreadable = None, True
    if port_file is not None and port_file.mtime >= started_at - PORT_FILE_SLACK:  # not one an earlier session wrote
        health.port_file = port_file
    health.checks["service"] = state == "active"
    if not health.checks["service"]:
        health.notes["service"] = f"{b.unit} is {state}"
        return health
    if health.port_file is None:
        health.notes["endpoint"] = UNREADABLE_PORT_FILE if unreadable else NO_PORT_FILE
        return health
    try:
        _ask_devtools(health, health.port_file, deadline)
    except devtools.DevToolsError:
        health.notes["endpoint"] = NO_ANSWER
    except _OutOfTime:
        health.notes["endpoint"] = OUT_OF_TIME
    try:
        capture = xwd.capture(b.display, timeout=_time_for(deadline, xwd.CAPTURE_TIMEOUT))
        health.checks["screen"] = not xwd.is_uniform(capture)
        if not health.checks["screen"]:
            health.notes["screen"] = f"the screen {b.display} is one uniform color"
    except (RuntimeError, ValueError) as exc:  # xwd's own words and the display: never the port
        health.notes["screen"] = str(exc)
    except _OutOfTime:
        health.notes["screen"] = OUT_OF_TIME
    return health


def _ask_devtools(health: Health, port_file: devtools.PortFile, deadline: float | None) -> None:
    """The endpoint, headful, visible, focused and webdriver checks, from the browser's DevTools answers."""
    answer = devtools.version(port_file.port, timeout=_time_for(deadline, devtools.TIMEOUT))
    health.checks["endpoint"] = answer.get("webSocketDebuggerUrl") == port_file.endpoint
    if not health.checks["endpoint"]:
        health.notes["endpoint"] = OTHER_ADDRESS
    agent = str(answer.get("User-Agent", ""))
    health.checks["headful"] = bool(agent) and "Headless" not in agent
    visible_state = None
    for page in devtools.pages(port_file.port, timeout=_time_for(deadline, devtools.TIMEOUT)):
        endpoint = devtools.page_endpoint(port_file.port, page["id"])
        try:
            raw = devtools.evaluate(endpoint, STATE_EXPRESSION, timeout=_time_for(deadline, devtools.TIMEOUT))
        except devtools.DevToolsError:
            continue  # a listed page can close or throw; another page may still prove healthy
        state = _page_state(raw)
        if state is not None and state[0] == "visible":
            visible_state = state
            if state[1] is True:  # several windows can each have a visible tab; only one has focus
                break
    if visible_state is None:
        health.notes["visible"] = NO_VISIBLE_TAB
        return
    health.checks["visible"] = True
    health.checks["focused"] = visible_state[1] is True  # a real true: a page can answer anything truthy
    health.checks["webdriver"] = visible_state[2] is False  # judged on that same page, never another window
    if not health.checks["webdriver"]:
        health.notes["webdriver"] = MARKED_AUTOMATED


def _page_state(raw: object) -> tuple[object, object, object] | None:
    """Accept only the three-item array returned by DevTools value serialization. Never invoke a page's
    JSON.stringify. Page-reported properties remain untrusted, so this is a health signal, not attestation."""
    if not isinstance(raw, list) or len(raw) != 3:
        return None
    return raw[0], raw[1], raw[2]


def _time_for(deadline: float | None, default: float) -> float:
    """How long one step may take: its default, or the time left before the deadline when that is less. _OutOfTime
    when none is left: the step is not started."""
    if deadline is None:
        return default
    left = deadline - time.monotonic()
    if left <= 0:
        raise _OutOfTime
    return min(default, left)
