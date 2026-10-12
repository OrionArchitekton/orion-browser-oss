"""Session records: one line per lease or session event, in the operator's state directory."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Callable

from orion_browser.config import NAME_RE as _NAME_RE  # one rule for browser names: the name becomes a folder name
from orion_browser.lease import iso, private_dir

RESERVED = frozenset({"ts", "browser", "holder", "session", "event"})  # a record's own fields


class Audit:
    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.state_dir = state_dir
        self.clock = clock

    def _dir(self, *parts: str) -> Path:
        return private_dir(self.state_dir.joinpath(*parts))  # every level it makes is owner-only, and so is the last

    def record(self, browser: str, holder: str, event: str, session: str = "", **details: Any) -> None:
        """Append one record. Callers pass names, ids, codes and how a stop ended: never page content, page addresses,
        typed text or images. A browser name that is not a plain name, or a detail named like one of the record's own
        fields, is a caller bug and raises ValueError."""
        if not _NAME_RE.fullmatch(browser):  # before any disk access: "../x" or "/abs" must not leave the audit folder
            raise ValueError(f"invalid browser name {browser!r}")
        clash = sorted(RESERVED.intersection(details))
        if clash:  # callers pass fixed keys, so a clash is a programming error, not data to fix up
            raise ValueError(f"record details may not replace {', '.join(clash)}")
        now = self.clock()
        entry = {"ts": iso(now), "browser": browser, "holder": holder, "session": session, "event": event, **details}
        path = self._dir("audit", browser) / (time.strftime("%Y-%m-%d", time.gmtime(now)) + ".jsonl")
        line = (json.dumps(entry, sort_keys=True) + "\n").encode()
        with open(os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o600), "ab") as fh:
            size = os.fstat(fh.fileno()).st_size
            if size and os.pread(fh.fileno(), 1, size - 1) != b"\n":  # an earlier append failed half way
                line = b"\n" + line  # its torn half stays on its own line and is never glued to this record
            fh.write(line)
            fh.flush()
            os.fsync(fh.fileno())
