"""Shared state and helpers for the fakes and the tests.

Everything lives under $FAKE_DIR: settings.json (knobs a test sets), calls.jsonl (every fake call), units.json (the fake
service manager's units, read and written under units.lock), launches-<profile> (fake Chrome launch counts),
preferences-at-launch-<profile> (the Preferences file fake Chrome found at its last launch), focus-<display> /
drawn-<display> (what fake xdotool focused and where fake Chrome drew), stop-asked (a stop was asked of fake
systemctl) and listening-<port> (a program started by listen() listens: fake systemctl's stop_then_listen, fake
systemd-run's listen_before_start), websockify-pythonpath (the PYTHONPATH fake websockify was given, at its last
start), and a count for each knob that acts only a given number of times (take)."""

from __future__ import annotations

import fcntl
import json
import os
import struct
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

CHROME_DEFAULTS = {
    # Read on every DevTools request, so a test can change them while a session runs:
    "headless": False,  # report a headless user agent
    "visible": True,  # the tab reports visibilityState visible
    "user_agent": None,  # report exactly this user agent ("" for an empty one) instead of a headful or headless one
    "wrong_endpoint": False,  # /json/version names another browser's endpoint than the port file does
    "deep_state": False,  # the page answers the state query with JSON nested too deep to parse (as a page can)
    "page_answer": None,  # the page answers the state query with exactly this value instead of its state
    "page_answers": None,  # ordered answers for several page targets, as with multiple Chrome windows
    "stringify_spoof": False,  # a page replaces JSON.stringify with a fabricated healthy report
    "ignores_automation_flag": False,  # a Chrome that marks itself automated despite --disable-blink-features
    "stall_version": 0,  # seconds /json/version waits before it answers (cut short when the fake stops)
    # Read once, at launch:
    "outdated": True,  # an outdated build, as the real one was at the first smoke test: its update bubble (fake_chrome)
    "ignore_close": False,  # answer Browser.close but keep running
    "close_delay": 0,  # seconds it runs on after it takes Browser.close, before it exits (with ignore_close off)
    "ignore_sigterm": False,  # survive SIGTERM (only SIGKILL ends it)
    "no_port_file_launches": 0,  # this many first launches never write the port file
    "crash_after": None,  # seconds after start to exit with code 1, as a crash
}
HANG = 60.0  # seconds a fake call that hangs waits: longer than any caller's timeout, which ends it first
LISTENER = (  # argv: host, port, a file to create once it listens
    "import socket, sys, time\n"
    "server = socket.create_server((sys.argv[1], int(sys.argv[2])))\n"
    "open(sys.argv[3], 'w').close()\n"
    "while True:\n"
    "    time.sleep(1)\n"
)


def root() -> Path:
    return Path(os.environ["FAKE_DIR"])


def settings() -> dict[str, Any]:
    path = root() / "settings.json"
    return json.loads(path.read_text()) if path.exists() else {}


def chrome_knobs(profile: str) -> dict[str, Any]:
    """The fake Chrome knobs for one profile (named for its browser), over the defaults."""
    return {**CHROME_DEFAULTS, **settings().get("chrome", {}).get(profile, {})}


def log_call(tool: str, argv: list[str]) -> None:
    with open(root() / "calls.jsonl", "a") as fh:
        fh.write(json.dumps({"tool": tool, "argv": argv, "ts": time.time()}) + "\n")


@contextmanager
def units() -> Iterator[dict[str, Any]]:
    """The fake service manager's units, read and written back under a lock."""
    lock = os.open(root() / "units.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root() / "units.json"
        data = json.loads(path.read_text()) if path.exists() else {}
        yield data
        path.write_text(json.dumps(data))
    finally:
        os.close(lock)


def alive(pid: int) -> bool:
    """True while the process exists and is not a zombie. (Bytes: the process name in the file can be any bytes.)"""
    try:
        state = Path(f"/proc/{pid}/stat").read_bytes().rsplit(b")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != b"Z"


def loopback() -> str:
    return os.environ.get("ORION_BROWSER_LOOPBACK", "127.0.0.1")


def listen(port: int) -> None:
    """Start a program of its own (another program, to the tool) listening on `port` at the test loopback, and return
    once it listens, waiting at most 10 s. It runs until the test's teardown ends it."""
    ready = root() / f"listening-{port}"
    subprocess.Popen(
        [sys.executable, "-c", LISTENER, loopback(), str(port), str(ready)],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
    )
    deadline = time.monotonic() + 10
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.02)


def take(counter: str, allowed: int) -> bool:
    """One use of a knob that acts only `allowed` times: True for each of the first `allowed` uses, counted in
    FAKE_DIR/<counter> under a lock of its own, since the fakes run side by side."""
    lock = os.open(root() / f"{counter}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX)
        path = root() / counter
        used = int(path.read_text()) if path.exists() else 0
        if used >= allowed:
            return False
        path.write_text(str(used + 1))
        return True
    finally:
        os.close(lock)


def count_launch(profile: str) -> int:
    """Count a fake Chrome launch for the profile and return the new count (1 for the first launch)."""
    path = root() / f"launches-{profile}"
    count = int(path.read_text()) + 1 if path.exists() else 1
    path.write_text(str(count))
    return count


def make_xwd(width: int, height: int, uniform: bool, pad: int = 0, colors: int = 0) -> bytes:
    """A screen capture as `xwd -root` writes one: a big-endian XWDFileHeader, the window name, `colors` colormap
    entries, then ZPixmap rows at 32 bits per pixel, each followed by `pad` padding bytes.

    Real xwd writes 256 colormap entries even for a TrueColor screen; none is the default. An entry is 12 bytes
    (XWDColor) and looks nothing like a pixel, so a reader that takes the colormap for pixels sees a uniform screen as
    not uniform. Header word 19 (ncolors) is what counts the entries, as the format says; word 18 (colormap_entries)
    stays 0, so a reader that took the count from the wrong word would fail."""
    name = b"fake\0"
    bytes_per_line = width * 4 + pad
    fields = [100 + len(name), 7, 2, 24, width, height, 0, 0, 32, 0, 32, 32, bytes_per_line, 4,
              0xFF0000, 0xFF00, 0xFF, 8, 0, colors, width, height, 0, 0, 0]
    entries = []
    for i in range(colors):
        level = i % 256 * 257  # pixel value i, its color as 16-bit red, green and blue, then all three flags set
        entries.append(struct.pack(">IHHHBB", i, level, level, level, 7, 0))
    rows = []
    for y in range(height):
        if uniform:
            pixels = b"\x10\x20\x30\x00" * width
        else:
            pixels = b"".join(bytes((x * 9 % 256, y * 7 % 256, (x + y) % 256, 0)) for x in range(width))
        rows.append(pixels + b"\xaa" * pad)
    return struct.pack(">25I", *fields) + name + b"".join(entries) + b"".join(rows)
