"""Audit: one JSON line per record, in the operator's state directory, owner-only, and never outside it."""

from __future__ import annotations

import json
import os
import re
import stat
import time
from datetime import datetime, timezone

import pytest

from orion_browser.audit import Audit

TORN = b'{"browser": "work", "ev'  # half a record: an append that failed part way


def utc(*when: int) -> float:
    return datetime(*when, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self) -> None:
        self.now = utc(2026, 10, 7, 12, 0, 0)

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def state(tmp_path):
    return tmp_path / "home" / "state"  # neither folder exists yet: the first record makes them


@pytest.fixture
def far_east_zone(monkeypatch):
    """A local time zone 14 hours ahead of UTC (a POSIX rule, so no zone database is needed): a file named for the
    local date instead of the UTC date lands on another day, whatever zone the machine runs in."""
    monkeypatch.setenv("TZ", "XXX-14")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


@pytest.fixture
def no_umask():
    """Umask 0, so the modes the code asks for are the modes on disk: a permissive request cannot hide behind the
    machine's own umask."""
    old = os.umask(0)
    yield
    os.umask(old)


def modes(*paths) -> list[str]:
    return [oct(stat.S_IMODE(path.stat().st_mode)) for path in paths]


def test_each_record_is_one_json_line_with_sorted_keys(state, clock):
    audit = Audit(state, clock)
    audit.record("work", "agent-a", "lease", session="s1", minutes=30)
    clock.now += 5
    audit.record("work", "agent-b", "busy")  # a refusal: no session and no details
    clock.now += 5
    audit.record("work", "agent-a", "stop", session="s1", ended="clean", exit=0)
    data = (state / "audit" / "work" / "2026-10-07.jsonl").read_bytes()
    assert data.endswith(b"\n")
    rows = [json.loads(line) for line in data.splitlines()]
    assert rows == [
        {"ts": "2026-10-07T12:00:00+00:00", "browser": "work", "holder": "agent-a", "session": "s1", "event": "lease",
         "minutes": 30},
        {"ts": "2026-10-07T12:00:05+00:00", "browser": "work", "holder": "agent-b", "session": "", "event": "busy"},
        {"ts": "2026-10-07T12:00:10+00:00", "browser": "work", "holder": "agent-a", "session": "s1", "event": "stop",
         "ended": "clean", "exit": 0},
    ]
    assert all(list(row) == sorted(row) for row in rows)  # json.loads keeps the order of the keys on the line


def test_the_file_is_named_for_the_utc_day_and_kept_per_browser(state, clock, far_east_zone):
    audit = Audit(state, clock)
    clock.now = utc(2026, 10, 7, 23, 59, 59)  # already the 8th in a zone 14 hours ahead
    audit.record("work", "agent-a", "lease")
    clock.now = utc(2026, 10, 8, 0, 0, 1)
    audit.record("work", "agent-a", "stop")
    audit.record("one", "agent-b", "lease")
    found = sorted(path.relative_to(state).as_posix() for path in state.rglob("*.jsonl"))
    assert found == ["audit/one/2026-10-08.jsonl", "audit/work/2026-10-07.jsonl", "audit/work/2026-10-08.jsonl"]


def test_every_folder_the_audit_makes_is_owner_only_and_so_is_the_file(state, clock, no_umask):
    Audit(state, clock).record("work", "agent-a", "lease")
    # "home" above the state folder was missing too, and the audit makes it level by level
    folders = [state.parent, state, state / "audit", state / "audit" / "work"]
    assert modes(*folders) == ["0o700"] * 4
    (day_file,) = (state / "audit" / "work").iterdir()
    assert modes(day_file) == ["0o600"]


def test_an_audit_folder_that_exists_is_narrowed_to_owner_only(state, clock):
    folder = state / "audit" / "work"
    folder.mkdir(parents=True)
    folder.chmod(0o777)
    Audit(state, clock).record("work", "agent-a", "lease")
    assert modes(folder) == ["0o700"]


def test_details_may_not_replace_the_fixed_fields(state, clock):
    audit = Audit(state, clock)
    # browser, holder, session and event are parameters of record(), so Python itself refuses a second value for them;
    # ts is the one a caller can still pass in the details
    with pytest.raises(ValueError, match="record details may not replace ts"):
        audit.record("work", "agent-a", "lease", ts="1999-12-31T23:59:59+00:00")
    assert not state.exists()  # refused before anything was written


@pytest.mark.parametrize("earlier_records", [0, 2])
def test_a_torn_last_line_is_left_as_it_was_and_the_next_record_starts_a_line_of_its_own(state, clock, earlier_records):
    audit = Audit(state, clock)
    day_file = state / "audit" / "work" / "2026-10-07.jsonl"
    day_file.parent.mkdir(parents=True)
    for _ in range(earlier_records):
        audit.record("work", "agent-a", "lease")
    with open(day_file, "ab") as fh:
        fh.write(TORN)
    before = day_file.read_bytes()
    assert before.endswith(TORN)
    audit.record("work", "agent-b", "lease", session="s2")
    after = day_file.read_bytes()
    assert after.startswith(before)  # the torn half is exactly as it was
    tail = after[len(before):]
    assert tail.startswith(b"\n") and tail.endswith(b"\n") and tail.count(b"\n") == 2  # one line, its own
    assert json.loads(tail)["holder"] == "agent-b"


def test_a_name_that_is_not_a_plain_name_is_refused_before_anything_is_written(tmp_path, state, clock):
    audit = Audit(state, clock)
    absolute = str(tmp_path / "abs")  # joined onto a path, an absolute name replaces everything before it
    for name in ("../x", "../../x", absolute, "", "Dan", "work/../x", "work\n", "a" * 17):
        with pytest.raises(ValueError, match=re.escape(f"invalid browser name {name!r}")):
            audit.record(name, "agent-a", "lease")
        assert not list(tmp_path.rglob("*")), f"{name!r} left something on disk"  # the sandbox: state and beside it
