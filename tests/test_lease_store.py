"""LeaseStore: grants, extensions, takeovers, releases, the session lock, and damaged files."""

from __future__ import annotations

import json
import os
import stat
import threading
import time
from dataclasses import asdict, replace

import pytest

from orion_browser.codes import BrowserError, Code
from orion_browser.lease import LeaseStore

VALID_LEASE = '{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1}'  # a lease file, as text


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def store(tmp_path, clock) -> LeaseStore:
    return LeaseStore(tmp_path, clock)


@pytest.fixture
def no_umask():
    """Umask 0, so the modes the code asks for are the modes on disk: a permissive request cannot hide behind the
    machine's own umask."""
    old = os.umask(0)
    yield
    os.umask(old)


@pytest.fixture
def io_log(monkeypatch):
    """What the process renames, removes and syncs, in order: ("replace", the new path), ("unlink", path) and
    ("fsync", the file or folder synced)."""
    log: list[tuple[str, str]] = []
    real_replace, real_unlink, real_fsync = os.replace, os.unlink, os.fsync

    def spy_replace(src, dst, *args, **kwargs):
        log.append(("replace", os.fspath(dst)))
        return real_replace(src, dst, *args, **kwargs)

    def spy_unlink(path, *args, **kwargs):
        log.append(("unlink", os.fspath(path)))
        return real_unlink(path, *args, **kwargs)

    def spy_fsync(fd):
        log.append(("fsync", os.readlink(f"/proc/self/fd/{fd}")))
        return real_fsync(fd)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(os, "unlink", spy_unlink)
    monkeypatch.setattr(os, "fsync", spy_fsync)
    return log


def modes(*paths) -> list[str]:
    return [oct(stat.S_IMODE(path.stat().st_mode)) for path in paths]


def test_grant_then_busy(store):
    lease, previous = store.acquire("work", "agent-a", 60)
    assert (lease.holder, lease.expires_at, lease.session, previous) == ("agent-a", 1_000_060.0, "", None)
    with pytest.raises(BrowserError) as info:
        store.acquire("work", "agent-b", 60)
    assert info.value.code is Code.BUSY
    assert info.value.data == {"browser": "work", "holder": "agent-a", "expires_at": "1970-01-12T13:47:40+00:00"}


def test_own_lease_extends_and_keeps_its_session_even_after_it_lapsed(store, clock):
    store.acquire("work", "a", 60)
    store.set_session("work", "a", "s1", "ws://127.0.0.1:9/devtools/browser/x", 9, 1_000_000.0)
    clock.now += 3600  # lapsed, and nobody took it over
    lease, previous = store.acquire("work", "a", 60)
    assert (lease.session, lease.port, lease.endpoint, previous) == ("s1", 9, "ws://127.0.0.1:9/devtools/browser/x", None)
    assert lease.expires_at == clock.now + 60


def test_takeover_of_an_expired_lease_returns_the_previous_one(store, clock):
    store.acquire("work", "a", 60)
    store.set_session("work", "a", "s1", "ws://x", 9, 1.0)
    clock.now += 61
    lease, previous = store.acquire("work", "b", 60)
    assert (lease.holder, lease.session) == ("b", "")
    assert (previous.holder, previous.session) == ("a", "s1")


def test_set_session_is_for_the_holder_only(store):
    store.acquire("work", "a", 60)
    with pytest.raises(BrowserError) as info:
        store.set_session("work", "b", "s1", "", 0, 1.0)
    assert info.value.code is Code.NOT_HOLDER


def test_release_rules(store, clock):
    store.acquire("work", "a", 60)
    with pytest.raises(BrowserError) as info:
        store.release("work", "b")
    assert info.value.code is Code.NOT_HOLDER and "held by a" in info.value.message
    clock.now += 3600
    store.release("work", "a")  # a holder may release its own lapsed lease
    assert store.current("work") is None
    with pytest.raises(BrowserError) as info:
        store.release("work", "a")
    assert "no one holds it" in info.value.message


def test_releasing_keeps_the_lease_when_the_stop_fails(store):
    store.acquire("work", "a", 60)
    with pytest.raises(RuntimeError):
        with store.releasing("work", "a"):
            raise RuntimeError("stop failed")
    assert store.current("work").holder == "a"


def test_releasing_rules(store, clock):
    entered = []
    with pytest.raises(BrowserError) as info:
        with store.releasing("work", "a"):
            entered.append("no lease")
    assert info.value.code is Code.NOT_HOLDER and "no one holds it" in info.value.message
    store.acquire("work", "a", 60)
    with pytest.raises(BrowserError) as info:
        with store.releasing("work", "b"):
            entered.append("not the holder")
    assert info.value.code is Code.NOT_HOLDER and "held by a" in info.value.message
    assert not entered  # a stop is never started for someone who does not hold the lease
    clock.now += 3600
    with store.releasing("work", "a") as lease:  # a holder may stop and release its own lapsed lease
        assert lease.holder == "a"
    assert store.current("work") is None


def test_releasing_lets_a_contender_see_the_lease_while_the_stop_runs(store):
    store.acquire("work", "a", 60)
    stopping, stopped = threading.Event(), threading.Event()
    seen: dict[str, object] = {}

    def stop():
        with store.releasing("work", "a"):
            stopping.set()
            stopped.wait(timeout=10)  # the caller's stop, still running

    def contend():
        began = time.monotonic()
        seen["peek"], seen["current"] = store.peek("work"), store.current("work")
        try:
            store.acquire("work", "b", 60)
        except BrowserError as exc:
            seen["refused"] = exc.code
        seen["seconds"] = time.monotonic() - began

    stopper = threading.Thread(target=stop, daemon=True)
    contender = threading.Thread(target=contend, daemon=True)
    stopper.start()
    try:
        assert stopping.wait(timeout=5)
        contender.start()
        contender.join(timeout=3)
        assert not contender.is_alive(), "peek, current and acquire wait for the stop to end"
        assert seen["seconds"] < 1
        assert seen["peek"].holder == seen["current"].holder == "a"
        assert seen["refused"] is Code.BUSY
    finally:
        stopped.set()
        stopper.join(timeout=10)
        contender.join(timeout=10)
    assert not (store.dir / "work.json").exists()  # once the stop is over the lease is gone


@pytest.mark.parametrize(
    "change",
    [{"holder": "b"}, {"session": "s2"}],
    ids=["another holders lease", "another session of the same holder"],
)
def test_releasing_leaves_a_lease_that_changed_during_the_stop(store, change):
    store.acquire("work", "a", 60)
    store.set_session("work", "a", "s1", "ws://x", 9, 1.0)
    with store.releasing("work", "a") as lease:
        # another process puts a different lease on file while the stop runs
        (store.dir / "work.json").write_text(json.dumps(asdict(replace(lease, **change))), encoding="utf-8")
    assert store.current("work") == replace(lease, **change)


def test_releasing_finishes_when_the_lease_is_already_gone(store):
    store.acquire("work", "a", 60)
    with store.releasing("work", "a"):
        (store.dir / "work.json").unlink()  # removed by hand while the stop runs
    assert store.current("work") is None


def test_drop_frees_only_the_callers_lease(store):
    store.acquire("work", "a", 60)
    store.drop("work", "b")
    assert store.current("work").holder == "a"
    store.drop("work", "a")
    assert store.current("work") is None


def test_drop_returns_the_lease_it_freed_with_the_session_a_start_recorded(store, clock):
    """A failed start's record names the session the start recorded on the lease, so drop hands back what it freed."""
    store.acquire("work", "a", 60)
    starting = store.set_session("work", "a", "s1", "", 0, clock.now)
    assert store.drop("work", "b") is None  # not the caller's: nothing freed
    assert store.drop("work", "a") == starting
    assert store.drop("work", "a") is None  # nothing left to free


def test_give_back_puts_the_previous_holders_lease_back_as_it_was(store, clock):
    """A takeover refused before the old session was shown gone gives the lapsed lease back to its holder, unchanged."""
    store.acquire("work", "a", 60)
    held = store.set_session("work", "a", "s1", "ws://endpoint", 9222, clock.now)
    clock.now += 120  # a's lease has lapsed
    taker, previous = store.acquire("work", "b", 60)
    assert previous == held
    assert store.give_back("work", taker, previous) is True
    assert store.current("work") == held


def test_give_back_never_overwrites_a_lease_written_since(store, clock):
    """Only the lease the takeover wrote is replaced: once anything else is on file, giving back changes nothing."""
    store.acquire("work", "a", 60)
    clock.now += 120
    taker, previous = store.acquire("work", "b", 60)
    starting = store.set_session("work", "b", "s2", "", 0, clock.now)  # the takeover's start went on
    assert store.give_back("work", taker, previous) is False
    assert store.current("work") == starting
    store.drop("work", "b")
    assert store.give_back("work", taker, previous) is False and store.current("work") is None


def test_peek_hides_an_expired_lease(store, clock):
    assert store.peek("work") is None
    lease, _ = store.acquire("work", "a", 60)
    assert store.peek("work") == lease
    clock.now = lease.expires_at  # a lease is over at its expiry instant
    assert store.peek("work") is None
    assert store.current("work") == lease  # hidden, not removed


def test_try_session_lock_never_waits(store):
    with store.session_lock("work"):
        with store.try_session_lock("work") as got:
            assert got is False
    with store.try_session_lock("work") as got:
        assert got is True


def test_two_try_session_locks_exclude_each_other(store):
    with store.try_session_lock("work") as outer:
        assert outer is True
        with store.try_session_lock("work") as inner:
            assert inner is False  # the outer one really holds the lock
    with store.try_session_lock("work") as again:
        assert again is True  # and lets go of it when its block ends


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"{",
        b'{"browser": "work"}',
        b'{"browser": "work", "holder": "a", "acquired_at": "x", "expires_at": 1}',
        # a boolean is an int in Python but never a timestamp: one case for each timestamp in the file
        b'{"browser": "work", "holder": "a", "acquired_at": true, "expires_at": 1}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": true}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1, "started_at": true}',
        # numbers no calendar can print. NaN is neither before nor after anything, so an expiry of NaN would read as
        # expired and the browser would be taken over; infinity and 1e18 are past the end of any date
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": NaN}',
        b'{"browser": "work", "holder": "a", "acquired_at": NaN, "expires_at": 1}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1, "started_at": NaN}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": Infinity}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1e18}',
        # fields of the wrong type
        b'{"browser": "work", "holder": 5, "acquired_at": 1, "expires_at": 1}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1, "session": 5}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1, "endpoint": null}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1, "port": true}',
        b'{"browser": "work", "holder": "a", "acquired_at": 1, "expires_at": 1, "port": 70000}',
        # not a lease object at all
        b"[]",
        pytest.param(b"[" * 100_000 + b"]" * 100_000, id="nested too deep to parse"),  # 10_000 parses on Python 3.14
        # valid JSON in another encoding: json.loads would take those bytes as they are, the strict UTF-8 read must not
        pytest.param(VALID_LEASE.encode("utf-16"), id="valid lease in UTF-16"),
        b"\xff",
    ],
)
def test_a_damaged_file_is_usage_never_free_or_busy(store, content):
    store.dir.mkdir(parents=True, exist_ok=True)
    (store.dir / "work.json").write_bytes(content)
    for call in (lambda: store.acquire("work", "a", 60), lambda: store.current("work")):
        with pytest.raises(BrowserError) as info:
            call()
        assert info.value.code is Code.USAGE and "work.json is damaged" in info.value.message


def test_a_lease_path_that_is_a_folder_is_damaged_never_free(store):
    path = store.dir / "work.json"
    path.mkdir(parents=True)  # it cannot be read, and it must not read as "no lease" either
    for call in (lambda: store.current("work"), lambda: store.peek("work"), lambda: store.acquire("work", "a", 60)):
        with pytest.raises(BrowserError) as info:
            call()
        assert info.value.code is Code.USAGE and "work.json is damaged" in info.value.message
    assert path.is_dir()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_an_unreadable_lease_file_is_damaged_and_never_taken_over(store):
    store.acquire("work", "a", 60)  # a live lease that this user is then not allowed to read
    path = store.dir / "work.json"
    body = path.read_bytes()
    path.chmod(0o000)
    try:
        try:
            path.read_bytes()
        except PermissionError:
            pass
        else:
            pytest.skip("this file system lets its owner read a file with no permissions")
        for call in (lambda: store.current("work"), lambda: store.peek("work"), lambda: store.acquire("work", "b", 60)):
            with pytest.raises(BrowserError) as info:
                call()
            assert info.value.code is Code.USAGE and "work.json is damaged" in info.value.message
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == body  # b did not get the browser: the lease a holds is still on file


@pytest.mark.parametrize("named", ["yin", "../../x"])
def test_a_lease_file_that_names_another_browser_is_damaged_and_never_written_back(store, named):
    store.dir.mkdir(parents=True, exist_ok=True)
    path = store.dir / "work.json"
    body = json.dumps({"browser": named, "holder": "a", "acquired_at": 1.0, "expires_at": 4e9}).encode()
    path.write_bytes(body)
    calls = (
        lambda: store.current("work"),
        lambda: store.peek("work"),
        lambda: store.acquire("work", "a", 60),  # the holder the file names: it must not be renewed
        lambda: store.acquire("work", "b", 60),
    )
    for call in calls:
        with pytest.raises(BrowserError) as info:
            call()
        assert info.value.code is Code.USAGE and "work.json is damaged" in info.value.message
    assert path.read_bytes() == body  # neither renewed nor written back


def test_names_never_leave_the_lease_directory(store):
    with pytest.raises(ValueError):
        store.acquire("../x", "a", 60)


def test_the_folders_the_store_makes_are_owner_only(tmp_path, clock, no_umask):
    store = LeaseStore(tmp_path / "home" / "state", clock)  # neither "home" nor "state" exists yet
    store.acquire("work", "a", 60)
    assert modes(tmp_path / "home", tmp_path / "home" / "state", store.dir) == ["0o700"] * 3


def test_a_lease_folder_that_exists_is_narrowed_and_the_state_folder_above_it_is_left_alone(tmp_path, clock):
    state = tmp_path / "state"
    (state / "leases").mkdir(parents=True)
    state.chmod(0o755)  # not made by the store: setup owns its mode
    (state / "leases").chmod(0o777)
    LeaseStore(state, clock).acquire("work", "a", 60)
    assert modes(state / "leases", state) == ["0o700", "0o755"]


def test_writing_a_lease_syncs_the_lease_folder_afterwards(store, io_log):
    store.acquire("work", "a", 60)
    renamed = [i for i, (op, target) in enumerate(io_log) if op == "replace" and target == str(store.dir / "work.json")]
    assert len(renamed) == 1, io_log
    assert ("fsync", str(store.dir.resolve())) in io_log[renamed[0] + 1:], io_log


@pytest.mark.parametrize("how", ["release", "drop", "releasing"])
def test_removing_a_lease_syncs_the_lease_folder_afterwards(store, io_log, how):
    store.acquire("work", "a", 60)
    io_log.clear()
    if how == "release":
        store.release("work", "a")
    elif how == "drop":
        store.drop("work", "a")
    else:
        with store.releasing("work", "a"):
            pass
    unlinks = [i for i, (op, target) in enumerate(io_log) if op == "unlink" and target == str(store.dir / "work.json")]
    assert len(unlinks) == 1, io_log
    assert ("fsync", str(store.dir.resolve())) in io_log[unlinks[0] + 1:], io_log
