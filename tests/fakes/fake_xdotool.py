"""Fake xdotool: `search` finds one window (none when `xdotool_no_window` is set); `windowfocus` marks the display's
focus, which fake Chrome reads as document.hasFocus()."""

from __future__ import annotations

import os
import sys

import fakesys


def main(argv: list[str]) -> int:
    fakesys.log_call("xdotool", argv)
    display = os.environ.get("DISPLAY", "")
    if argv[:1] == ["search"]:
        if fakesys.settings().get("xdotool_no_window"):
            return 1
        print("4242")
        return 0
    if argv[:1] == ["windowfocus"]:
        (fakesys.root() / f"focus-{display}").write_text(argv[-1])
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
