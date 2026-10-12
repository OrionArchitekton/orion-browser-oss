"""Screen captures: one color or not, anything that is not a capture is refused, and a capture that fails says why."""

from __future__ import annotations

import errno
import os
import signal
import struct
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent / "fakes"))
from fakesys import make_xwd  # noqa: E402

from orion_browser import xwd  # noqa: E402

# The header words the checks read (the header is 25 big-endian 32-bit words, numbered from 0)
HEADER_SIZE, VERSION, PIXMAP_FORMAT, WIDTH, HEIGHT = 0, 1, 2, 4, 5
BITS_PER_PIXEL, BYTES_PER_LINE, NCOLORS = 11, 12, 19
# The color bytes of a 32-bit pixel as make_xwd lays it out. The fourth byte carries no color: no test here depends on
# whether it is compared, so every difference below is made in a color byte.
BLUE, GREEN, RED = range(3)
VALID = make_xwd(8, 4, uniform=True)


def word(data: bytes, index: int) -> int:
    return struct.unpack_from(">I", data, 4 * index)[0]


def with_word(data: bytes, index: int, value: int) -> bytes:
    """`data` with one header word replaced: valid in every way but the one thing a single check looks at."""
    changed = bytearray(data)
    struct.pack_into(">I", changed, 4 * index, value)
    return bytes(changed)


def without_name(data: bytes) -> bytes:
    """The same capture with no window name, so the header is its bare 100 bytes."""
    return with_word(data[:100] + data[word(data, HEADER_SIZE):], HEADER_SIZE, 100)


def pixel_offset(data: bytes, x: int, y: int) -> int:
    """Where pixel (x, y) starts: after the header, the window name and the colormap, then whole rows."""
    start = word(data, HEADER_SIZE) + 12 * word(data, NCOLORS)
    return start + y * word(data, BYTES_PER_LINE) + 4 * x


def flip(data: bytearray, x: int, y: int, color: int = BLUE) -> None:
    """Change one color byte of pixel (x, y), so that exactly one pixel differs from the rest."""
    data[pixel_offset(data, x, y) + color] ^= 0x01


def make_xwd_at_depth(width: int, height: int, bits_per_pixel: int, uniform: bool) -> bytes:
    """A capture like make_xwd's at 8, 16 or 24 bits per pixel, with no colormap and each row padded to a multiple of 4
    bytes as an X server pads rows. (make_xwd stays at 32 bits: the fakes share it.)"""
    pixel = bits_per_pixel // 8
    bytes_per_line = (width * pixel + 3) // 4 * 4
    name = b"fake\0"
    fields = [100 + len(name), 7, 2, bits_per_pixel, width, height, 0, 0, 32, 0, 32, bits_per_pixel, bytes_per_line, 4,
              0, 0, 0, 8, 0, 0, width, height, 0, 0, 0]
    rows = []
    for y in range(height):
        if uniform:
            pixels = bytes((0x11, 0x22, 0x33)[:pixel]) * width
        else:
            pixels = b"".join(bytes((x * 9 + y * 7 + k) % 256 for k in range(pixel)) for x in range(width))
        rows.append(pixels + b"\xaa" * (bytes_per_line - width * pixel))
    return struct.pack(">25I", *fields) + name + b"".join(rows)


def test_uniform_and_not():
    assert xwd.is_uniform(make_xwd(8, 4, uniform=True))
    assert not xwd.is_uniform(make_xwd(8, 4, uniform=False))


def test_one_different_pixel_counts():
    data = bytearray(make_xwd(8, 4, uniform=True))
    flip(data, 7, 3, RED)  # the last pixel, in a color byte (data[-2] when rows have no padding)
    assert not xwd.is_uniform(bytes(data))


@pytest.mark.parametrize("color", [BLUE, GREEN, RED], ids=["blue", "green", "red"])
def test_a_difference_in_any_color_byte_counts(color):
    data = bytearray(make_xwd(8, 4, uniform=True))
    flip(data, 7, 3, color)
    assert not xwd.is_uniform(bytes(data))


@pytest.mark.parametrize("x", [0, 7], ids=["first-pixel", "last-pixel"])
@pytest.mark.parametrize("y", [0, 1, 2, 3], ids=["first-row", "second-row", "third-row", "last-row"])
def test_a_difference_in_any_one_row_counts(x, y):
    data = bytearray(make_xwd(8, 4, uniform=True))
    flip(data, x, y)
    assert not xwd.is_uniform(bytes(data))


def test_a_one_row_capture_is_judged_by_its_own_pixels():
    """With one row there is no other row to differ from: only the pixels within it can."""
    data = bytearray(make_xwd(8, 1, uniform=True))
    assert xwd.is_uniform(bytes(data))
    flip(data, 7, 0, RED)  # the last pixel of the only row
    assert not xwd.is_uniform(bytes(data))


@pytest.mark.parametrize("bits", [8, 16, 24])
def test_a_capture_at_a_smaller_depth_is_read(bits):
    assert xwd.is_uniform(make_xwd_at_depth(5, 3, bits, uniform=True))
    assert not xwd.is_uniform(make_xwd_at_depth(5, 3, bits, uniform=False))


def test_row_padding_is_ignored():
    assert xwd.is_uniform(make_xwd(8, 4, uniform=True, pad=12))


# Real xwd writes a 256-entry colormap between the window name and the pixels, even on a TrueColor screen.
@pytest.mark.parametrize("colors", [1, 256], ids=["one-entry", "256-entries"])
def test_a_colormap_is_skipped_not_read_as_pixels(colors):
    assert xwd.is_uniform(make_xwd(8, 4, uniform=True, colors=colors))
    assert xwd.is_uniform(make_xwd(8, 4, uniform=True, pad=12, colors=colors))


@pytest.mark.parametrize("colors", [1, 256], ids=["one-entry", "256-entries"])
def test_a_colormap_does_not_hide_a_difference(colors):
    assert not xwd.is_uniform(make_xwd(8, 4, uniform=False, colors=colors))
    data = bytearray(make_xwd(8, 4, uniform=True, colors=colors))
    flip(data, 7, 3, RED)
    assert not xwd.is_uniform(bytes(data))


def test_a_capture_with_no_window_name_is_read():
    """A header size of exactly 100 is the bare header: below that is refused, 100 is not."""
    assert xwd.is_uniform(without_name(make_xwd(8, 4, uniform=True)))
    assert not xwd.is_uniform(without_name(make_xwd(8, 4, uniform=False)))


# Each case is valid but for one thing, so each check is tripped on its own, and the message says which one.
REFUSED = [
    pytest.param(b"", "too short", id="empty"),
    pytest.param(VALID[:99], "too short", id="one-byte-less-than-a-header"),
    pytest.param(VALID[:100], "cut short", id="a-header-and-nothing-else"),
    pytest.param(with_word(VALID, VERSION, 6), "not an xwd ZPixmap capture", id="version-6"),
    pytest.param(with_word(VALID, PIXMAP_FORMAT, 1), "not an xwd ZPixmap capture", id="format-XYPixmap"),
    pytest.param(with_word(VALID, HEADER_SIZE, 99), "not an xwd ZPixmap capture", id="header-size-99"),
    pytest.param(with_word(VALID, WIDTH, 0), "unexpected capture layout", id="width-0"),
    pytest.param(with_word(VALID, HEIGHT, 0), "unexpected capture layout", id="height-0"),
    pytest.param(with_word(VALID, BITS_PER_PIXEL, 0), "unexpected capture layout", id="bits-per-pixel-0"),
    pytest.param(with_word(VALID, BITS_PER_PIXEL, 12), "unexpected capture layout", id="bits-per-pixel-12"),
    pytest.param(with_word(VALID, BYTES_PER_LINE, 31), "shorter than its width", id="bytes-per-line-31"),
    pytest.param(VALID[:-1], "cut short", id="one-byte-short"),
    pytest.param(make_xwd(8, 4, True, pad=12)[:-1], "cut short", id="padded-one-byte-short"),
    pytest.param(VALID[:150], "cut short", id="half-the-pixels"),
    pytest.param(with_word(VALID, NCOLORS, 1), "cut short", id="a-colormap-announced-and-not-there"),
]


@pytest.mark.parametrize("data, reason", REFUSED)
def test_what_is_not_a_capture_is_refused(data, reason):
    with pytest.raises(ValueError, match=reason):
        xwd.is_uniform(data)


# A capture that claims 4 GiB of pixels in a row must be refused by the length check, before anything is built to
# compare with. The reader runs in a child held to 1 GiB, so a regression fails at once instead of allocating the 4 GiB.
HOSTILE_CHILD = """
import resource, sys, tracemalloc
resource.setrlimit(resource.RLIMIT_AS, (2**30, 2**30))
from orion_browser import xwd
data = sys.stdin.buffer.read()
tracemalloc.start()
try:
    xwd.is_uniform(data)
    print("no error")
except ValueError as exc:
    print("ValueError:", exc)
print("peak", tracemalloc.get_traced_memory()[1])
"""


def test_a_hostile_size_is_refused_before_anything_large_is_built():
    # 0x3FFFFFFF pixels of 4 bytes fill the claimed bytes per line exactly, so only the length check can refuse it
    hostile = with_word(with_word(VALID, WIDTH, 0x3FFFFFFF), BYTES_PER_LINE, 0xFFFFFFFC)
    source_root = Path(xwd.__file__).resolve().parents[1]  # the folder of the orion_browser package under test
    env = dict(os.environ, PYTHONPATH=os.pathsep.join(filter(None, [str(source_root), os.environ.get("PYTHONPATH")])))
    child = subprocess.run(
        [sys.executable, "-c", HOSTILE_CHILD], input=hostile, capture_output=True, env=env, timeout=60,
    )
    assert child.returncode == 0, child.stderr.decode(errors="replace")[-500:]  # a MemoryError would end here
    outcome, peak = child.stdout.decode().splitlines()
    assert outcome.startswith("ValueError:") and "cut short" in outcome, outcome
    assert int(peak.split()[1]) < 1_000_000  # bytes traced while it ran: nothing large was built


def stub_xwd(monkeypatch, *, returncode=0, stdout=b"", stderr=b"", hang=False, raises=None):
    """Stand in for subprocess.run as xwd.py calls it, and return the (argv, kwargs) of each call it gets.

    Like the real run, it hands back the child's output only if the caller asked for it to be captured, a child that
    hangs ends only if the caller set a timeout, and `raises` is what it raises when the program cannot be started."""
    calls = []

    def run(argv, **kwargs):
        calls.append((list(argv), kwargs))
        if raises is not None:
            raise raises
        if hang:
            assert kwargs.get("timeout"), "a hung xwd would block for ever: no timeout was set"
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
        piped = kwargs.get("capture_output")
        out = stdout if piped or kwargs.get("stdout") == subprocess.PIPE else None
        err = stderr if piped or kwargs.get("stderr") == subprocess.PIPE else None
        return subprocess.CompletedProcess(argv, returncode, out, err)

    monkeypatch.setattr(xwd.subprocess, "run", run)
    return calls


def test_capture_names_a_missing_xwd(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(RuntimeError, match="x11-apps"):
        xwd.capture(":99")


# An xwd that is installed but cannot be started is a failed capture too. The health check handles RuntimeError and
# ValueError, so anything else would end `status` as an internal error instead of UNHEALTHY.
@pytest.mark.parametrize(
    "error",
    [PermissionError(errno.EACCES, "Permission denied"), OSError(errno.ENOEXEC, "Exec format error")],
    ids=["not-executable", "exec-format-error"],
)
def test_capture_that_cannot_start_xwd_names_xwd_and_the_error(monkeypatch, error):
    stub_xwd(monkeypatch, raises=error)
    with pytest.raises(RuntimeError) as caught:
        xwd.capture(":42")
    message = str(caught.value)
    assert "xwd" in message and error.strerror in message
    assert "x11-apps" not in message  # it is installed: "install the package" would send the operator the wrong way


def test_capture_with_an_xwd_that_is_not_executable(monkeypatch, tmp_path):
    """The real thing, no stub: the PATH holds a file named xwd without the execute bit. xwd itself never runs."""
    program = tmp_path / "xwd"
    program.write_text("#!/bin/sh\n")
    program.chmod(0o644)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError) as caught:
        xwd.capture(":42")
    assert "xwd" in str(caught.value) and os.strerror(errno.EACCES) in str(caught.value)


def test_capture_asks_xwd_for_the_root_window_of_that_display(monkeypatch):
    calls = stub_xwd(monkeypatch, stdout=b"x")
    xwd.capture(":42")
    assert [argv for argv, _ in calls] == [["xwd", "-root", "-silent", "-display", ":42"]]


def test_capture_neither_waits_for_ever_nor_reads_the_callers_stdin(monkeypatch):
    calls = stub_xwd(monkeypatch, stdout=b"x")
    xwd.capture(":42")
    [(_, kwargs)] = calls
    # 5 s: a real capture takes milliseconds, and a hung xwd must not stretch the time limit of the start that asked
    assert xwd.CAPTURE_TIMEOUT == 5.0
    assert kwargs["timeout"] == xwd.CAPTURE_TIMEOUT
    assert kwargs["stdin"] is subprocess.DEVNULL


def test_capture_returns_what_xwd_wrote_unchanged(monkeypatch):
    picture = bytes(range(256)) * 3 + b" \r\n\n"  # not text, and it ends in whitespace
    stub_xwd(monkeypatch, stdout=picture, stderr=b"xwd: a warning that is not an error")
    assert xwd.capture(":42") == picture


def test_capture_that_hangs_names_the_display_and_the_limit(monkeypatch):
    calls = stub_xwd(monkeypatch, hang=True)
    with pytest.raises(RuntimeError) as caught:
        xwd.capture(":42")
    message = str(caught.value)
    assert ":42" in message
    assert f"in {calls[0][1]['timeout']:g} s" in message


def test_capture_that_fails_gives_the_end_of_what_xwd_said(monkeypatch):
    said = "A" * 300 + "xwd: unable to open display ':42'"
    stub_xwd(monkeypatch, returncode=1, stderr=said.encode() + b"\n")
    with pytest.raises(RuntimeError, match="could not capture :42") as caught:
        xwd.capture(":42")
    tail = str(caught.value).split("could not capture :42: ", 1)[1]
    assert tail.endswith("xwd: unable to open display ':42'")  # the end of it, with the newline gone
    assert len(tail) <= 200  # and only the end of it


def test_capture_that_fails_with_stderr_that_is_not_text_still_says_why(monkeypatch):
    stub_xwd(monkeypatch, returncode=1, stderr=b"\xff\xfe xwd: no such display")
    with pytest.raises(RuntimeError, match="no such display"):
        xwd.capture(":42")


def test_capture_from_an_xwd_killed_by_a_signal_is_a_failure(monkeypatch):
    stub_xwd(monkeypatch, returncode=-signal.SIGKILL, stdout=b"half a picture")  # a negative code: killed, not exited
    with pytest.raises(RuntimeError, match="could not capture :42"):
        xwd.capture(":42")
