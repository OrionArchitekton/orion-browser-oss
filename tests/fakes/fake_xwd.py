"""Fake xwd: a capture of the screen, one color unless fake Chrome has drawn on that display (`screen_blank` forces
one color). `xwd_fails` makes it fail as xwd does when it cannot capture; `xwd_not_a_capture` makes it write bytes that
are not a capture at all; `xwd_stall` makes it wait that many seconds first, from the capture after the first
`xwd_stall_after` ones (0: from the first). Its settings are read on every call."""

from __future__ import annotations

import sys
import time

import fakesys


def main(argv: list[str]) -> int:
    fakesys.log_call("xwd", argv)
    settings = fakesys.settings()
    display = argv[argv.index("-display") + 1]
    counter = fakesys.root() / "xwd-captures"
    captures = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(captures))
    if captures > settings.get("xwd_stall_after", 0):
        time.sleep(settings.get("xwd_stall", 0))
    if settings.get("xwd_fails"):
        print(f"xwd:  unable to open display '{display}'", file=sys.stderr)
        return 1
    if settings.get("xwd_not_a_capture"):
        sys.stdout.buffer.write(b"not an xwd capture")
        return 0
    drawn = (fakesys.root() / f"drawn-{display}").exists()
    uniform = settings.get("screen_blank", False) or not drawn
    sys.stdout.buffer.write(fakesys.make_xwd(16, 8, uniform=uniform, colors=256))  # as real xwd: 256 entries
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
