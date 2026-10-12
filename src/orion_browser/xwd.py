"""Screen captures. `xwd -root` reads the whole screen, Chrome's own tab strip and toolbar included, so a blank page
still shows content, while a screen with nothing drawn on it (Xvnc runs with a black root window) is one color."""

from __future__ import annotations

import struct
import subprocess

HEADER = struct.Struct(">25I")  # XWDFileHeader: 25 big-endian 32-bit fields
CAPTURE_TIMEOUT = 5.0  # seconds: a capture takes milliseconds; a hung xwd must not stretch its caller's limit


def is_uniform(data: bytes) -> bool:
    """True when every pixel of an xwd ZPixmap capture is the same. ValueError for anything that is not one."""
    if len(data) < HEADER.size:
        raise ValueError("capture too short")
    fields = HEADER.unpack_from(data, 0)
    header_size, version, pixmap_format = fields[0], fields[1], fields[2]
    width, height = fields[4], fields[5]
    bits_per_pixel, bytes_per_line, ncolors = fields[11], fields[12], fields[19]
    if version != 7 or pixmap_format != 2 or header_size < HEADER.size:
        raise ValueError(f"not an xwd ZPixmap capture (version {version}, format {pixmap_format})")
    if width == 0 or height == 0 or bits_per_pixel == 0 or bits_per_pixel % 8:
        raise ValueError(f"unexpected capture layout: {width}x{height} at {bits_per_pixel} bits per pixel")
    pixel = bits_per_pixel // 8
    if bytes_per_line < width * pixel:
        raise ValueError("capture rows are shorter than its width")
    start = header_size + 12 * ncolors  # the colormap: 12 bytes per entry
    if len(data) < start + bytes_per_line * height:
        raise ValueError("capture is cut short")
    row = data[start:start + pixel] * width
    for y in range(height):
        offset = start + y * bytes_per_line
        if data[offset:offset + width * pixel] != row:
            return False
    return True


def capture(display: str, *, timeout: float | None = None) -> bytes:
    """The whole screen of `display`, as xwd writes it, within `timeout` seconds (CAPTURE_TIMEOUT when not given).
    RuntimeError when xwd is missing, cannot be run, or cannot capture it."""
    limit = CAPTURE_TIMEOUT if timeout is None else timeout
    try:
        proc = subprocess.run(
            ["xwd", "-root", "-silent", "-display", display],
            capture_output=True, timeout=limit, stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        raise RuntimeError("xwd is not installed (package x11-apps)") from None
    except OSError as exc:  # installed but cannot be started: not executable, not a program, out of descriptors
        raise RuntimeError(f"xwd could not be run: {exc}") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"xwd gave no capture of {display} in {limit:g} s") from None
    if proc.returncode != 0:
        raise RuntimeError(f"xwd could not capture {display}: {proc.stderr.decode(errors='replace').strip()[-200:]}")
    return proc.stdout
