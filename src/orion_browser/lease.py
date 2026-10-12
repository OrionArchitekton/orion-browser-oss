"""Leases: one holder per browser at a time. flock serializes every read-check-write.

Copied from orion-phone's lease core (MIT, same author) and trimmed for browsers: no boot marks. A lease carries the
session started for it instead, so a holder that leases again can be given the same endpoint."""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from orion_browser.codes import BrowserError, Code
from orion_browser.config import NAME_RE as _NAME_RE  # one rule for browser names: the name becomes a file name


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def private_dir(path: Path) -> Path:
    """The folder `path`, owner-only. Each level that is missing is made on its own with mode 0o700 (mkdir with
    parents=True gives the missing parents the umask default), and `path` itself is narrowed to 0o700 even when it
    existed already. Folders above it that exist are left as they are: their mode is not ours to change."""
    missing = []
    for level in (path, *path.parents):
        if level.is_dir():
            break
        missing.append(level)
    for level in reversed(missing):
        level.mkdir(mode=0o700, exist_ok=True)
    path.chmod(0o700)
    return path


def _sync_dir(directory: Path) -> None:
    """fsync a folder. A rename or an unlink is an entry in the folder, and a power loss can undo it until the folder is
    synced."""
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Lease:
    browser: str
    holder: str
    acquired_at: float
    expires_at: float
    session: str = ""  # the session started for this lease: "" until a start begins
    endpoint: str = ""  # its DevTools address: "" while it is starting
    port: int = 0
    started_at: float = 0.0


def _parse(raw: bytes) -> Lease:
    """A lease from file bytes. Anything but the known fields with sane values raises."""
    lease = Lease(**json.loads(raw.decode("utf-8")))  # strict UTF-8: json.loads(bytes) also accepts UTF-16 and UTF-32
    for text in (lease.browser, lease.holder, lease.session, lease.endpoint):
        if type(text) is not str:
            raise ValueError("browser, holder, session and endpoint must be strings")
    if type(lease.port) is not int or not 0 <= lease.port < 65536:
        raise ValueError("port must be a port number")
    for stamp in (lease.acquired_at, lease.expires_at, lease.started_at):
        if type(stamp) not in (int, float):  # bool is an int subclass but never a timestamp
            raise ValueError("timestamps must be numbers")
        iso(stamp)  # raises for NaN, infinity, and any value the messages could not print
    return lease


class LeaseStore:
    """One JSON file per browser. Refusals are BrowserError: BUSY or NOT_HOLDER for ordinary conflicts, USAGE when a
    lease file is damaged. A browser name that is not a plain name is a caller bug and raises ValueError."""

    def __init__(self, state_dir: Path, clock: Callable[[], float] = time.time) -> None:
        self.dir = state_dir / "leases"
        self.clock = clock

    def _open_lock(self, browser: str, kind: str) -> int:
        if not _NAME_RE.fullmatch(browser):  # before any disk access: "../x" or "/abs" must not leave the lease dir
            raise ValueError(f"invalid browser name {browser!r}")
        private_dir(self.dir)
        return os.open(self.dir / f"{browser}.{kind}", os.O_RDWR | os.O_CREAT, 0o600)

    @contextmanager
    def _locked(self, browser: str, kind: str = "lock") -> Iterator[Path]:
        fd = self._open_lock(browser, kind)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield self.dir / f"{browser}.json"
        finally:
            os.close(fd)  # closing the descriptor releases the flock

    @staticmethod
    def _read(path: Path, browser: str) -> Lease | None:
        """The lease on file, or None. A file that cannot be trusted raises USAGE: it must never read as free (a second
        holder would get the browser) or as busy (holders would retry forever). That includes a folder in its place, a
        file this user cannot read, and a file that names another browser: none of them is renewed or written back."""
        try:
            raw = path.read_bytes()
        except FileNotFoundError:  # the only error that means "no lease"
            return None
        except OSError as exc:  # a folder, a file this user cannot read, an I/O error: something is there, unreadable
            raise damaged(path, browser) from exc
        try:
            lease = _parse(raw)
        except (ValueError, TypeError, RecursionError, OverflowError, OSError) as exc:  # OSError: fromtimestamp
            raise damaged(path, browser) from exc
        if lease.browser != browser:
            raise damaged(path, browser)
        return lease

    @staticmethod
    def _write(path: Path, lease: Lease) -> None:
        tmp = path.with_name(path.name + ".tmp")
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "wb") as fh:
            fh.write(json.dumps(asdict(lease)).encode())
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _sync_dir(path.parent)

    def session_lock(self, browser: str) -> AbstractContextManager[Path]:
        """The browser's start and stop lock: one start or stop at a time. Separate from the lease's own lock, so a
        refusal (BUSY, NOT_HOLDER) never waits behind a start or stop in flight. Lock order: this lock first, the lease
        lock inside it, never the reverse."""
        return self._locked(browser, "act")

    @contextmanager
    def try_session_lock(self, browser: str) -> Iterator[bool]:
        """The session lock if it is free at this moment: True while it is held here, False when a start or stop holds
        it. Never waits."""
        fd = self._open_lock(browser, "act")
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
            except BlockingIOError:
                got = False
            yield got
        finally:
            os.close(fd)

    def current(self, browser: str) -> Lease | None:
        """The lease on file, expired or not."""
        with self._locked(browser) as path:
            return self._read(path, browser)

    def peek(self, browser: str) -> Lease | None:
        """The current unexpired lease, if any."""
        lease = self.current(browser)
        return lease if lease and lease.expires_at > self.clock() else None

    def acquire(self, browser: str, holder: str, ttl: float) -> tuple[Lease, Lease | None]:
        """Grant or extend a lease, and return the other holder's expired lease it took over, if any.

        The holder's own lease, live or lapsed, is extended and keeps its session: nobody took it over, so the session,
        if it still runs, is still the holder's. A free browser, or another holder's expired lease, gets a new lease with
        no session."""
        with self._locked(browser) as path:
            now = self.clock()
            current = self._read(path, browser)
            if current and current.holder != holder and current.expires_at > now:
                raise busy(current)
            if current and current.holder == holder:
                lease, previous = replace(current, expires_at=now + ttl), None
            else:
                lease, previous = Lease(browser, holder, now, now + ttl), current
            self._write(path, lease)
            return lease, previous

    def set_session(
        self, browser: str, holder: str, session: str, endpoint: str, port: int, started_at: float,
        ttl: float | None = None,
    ) -> Lease:
        """Record the session started for the caller's lease (endpoint "" while it is starting). With `ttl`, the lease
        also runs that long from now: a start that succeeds gives its holder the whole lease, however long the start
        took. NOT_HOLDER unless the lease on file is the caller's own."""
        with self._locked(browser) as path:
            current = self._read(path, browser)
            if not current or current.holder != holder:
                raise not_held(browser, holder, current)
            expires_at = current.expires_at if ttl is None else self.clock() + ttl
            lease = replace(
                current, session=session, endpoint=endpoint, port=port, started_at=started_at, expires_at=expires_at,
            )
            self._write(path, lease)
            return lease

    def release(self, browser: str, holder: str) -> Lease:
        """Give back the caller's own lease, even a lapsed one. A lease another holder took over is theirs."""
        with self._locked(browser) as path:
            current = self._read(path, browser)
            if not current or current.holder != holder:
                raise not_held(browser, holder, current)
            path.unlink()
            _sync_dir(path.parent)
            return current

    @contextmanager
    def releasing(self, browser: str, holder: str) -> Iterator[Lease]:
        """Yield the caller's own lease while the caller stops the session, and drop it only if the block finishes
        without an exception. A holder may release its own lapsed lease; one another holder took over is NOT_HOLDER.

        The lease lock is held to check the holder and again to remove the file, never across the block. A stop can take
        tens of seconds, and a contender's peek() or current() would wait that long for the lock instead of seeing the
        lease, and a lease attempt would wait instead of getting BUSY. What keeps the lease from changing hands during
        the block is the session lock, not this one: every caller holds the session lock around this call, so no grant
        or takeover can happen in between. The file is read again before it is removed, and removed only if it is
        still the same lease (same holder and session)."""
        with self._locked(browser) as path:
            current = self._read(path, browser)
            if not current or current.holder != holder:
                raise not_held(browser, holder, current)
        yield current
        with self._locked(browser) as path:
            again = self._read(path, browser)
            if again and (again.holder, again.session) == (current.holder, current.session):
                path.unlink()
                _sync_dir(path.parent)

    def drop(self, browser: str, holder: str) -> Lease | None:
        """Free the caller's lease after a start that failed, and return it as it was on file: with the session the
        start recorded on it, if it got that far. Nothing happens when the lease is not the caller's: None."""
        with self._locked(browser) as path:
            current = self._read(path, browser)
            if current and current.holder == holder:
                path.unlink()
                _sync_dir(path.parent)
                return current
            return None

    def give_back(self, browser: str, taker: Lease, previous: Lease) -> bool:
        """Undo a takeover refused before the previous holder's session was shown gone: put that holder's lease back as
        it was. Only while the file still holds the lease the takeover wrote (`taker`, as acquire returned it), so it
        never replaces a lease written since. True when the lease was given back."""
        with self._locked(browser) as path:
            if self._read(path, browser) != taker:
                return False
            self._write(path, previous)
            return True


def busy(lease: Lease) -> BrowserError:
    return BrowserError(
        Code.BUSY,
        f"browser {lease.browser} is held by {lease.holder} until {iso(lease.expires_at)}",
        {"browser": lease.browser, "holder": lease.holder, "expires_at": iso(lease.expires_at)},
    )


def damaged(path: Path, browser: str) -> BrowserError:
    return BrowserError(
        Code.USAGE,
        f"lease file {path} is damaged; if no agent is using browser {browser}, delete it and retry",
        {"browser": browser, "path": str(path)},
    )


def not_held(browser: str, holder: str, current: Lease | None) -> BrowserError:
    if current is None:
        why = "no one holds it"
    elif current.holder != holder:
        why = f"it is held by {current.holder}"
    else:
        why = "the lease expired"
    return BrowserError(
        Code.NOT_HOLDER, f"{holder} does not hold browser {browser}: {why}; lease it first", {"browser": browser}
    )
