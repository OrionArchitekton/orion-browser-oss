"""The decider: the only module that starts or stops a session, and the one place leases are granted."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from orion_browser import devtools, health, prefs, procs, services, setup
from orion_browser.audit import Audit
from orion_browser.codes import BrowserError, Code, Result
from orion_browser.config import Browser, Config, loopback, refuse_inside_git_tree
from orion_browser.lease import Lease, LeaseStore, busy, iso, not_held, private_dir

POLL = 0.2
GONE_AFTER_STOP = 10.0  # seconds to show a session gone once the service manager has stopped it
LOCK_WAIT = 600.0  # seconds a lease attempt waits for another start or stop of the browser before it gives up
LOCK_POLL = 0.05  # seconds between looks while a start or stop holds the session lock
PASSED_ENV = ("PATH", "HOME", "LANG", "XDG_RUNTIME_DIR", "ORION_BROWSER_LOOPBACK")  # what a session's service needs


class LeaseKept(BrowserError):
    """A start that failed and cannot show the session it began gone: the holder keeps the lease, as at a release."""


class _Attempt:
    """How far a lease attempt's start got: whether its session's service has been asked to start, from which moment
    the session may run. A start that tries again (Q4) shows the first service gone before the second is asked."""

    def __init__(self) -> None:
        self.service_asked = False


class Pool:
    def __init__(self, cfg: Config, clock: Callable[[], float] = time.time) -> None:
        self.cfg = cfg
        self.clock = clock
        self.leases = LeaseStore(cfg.state_dir, clock)
        self.audit = Audit(cfg.state_dir, clock)

    # ----- status

    def status(self, name: str | None) -> Result:
        """Each browser's row (or the one named), and the Chrome build every browser runs: its version and age, with a
        note when it is past Chrome's outdated-build age (setup.chrome_build)."""
        if name is None:
            rows = [self._row(b) for b in self.cfg.browsers.values()]
            return Result(Code.OK, f"{len(rows)} browsers", {"browsers": rows, "chrome": setup.chrome_build(self.cfg)})
        row = self._row(self.cfg.browser(name))
        code = {"healthy": Code.OK, "down": Code.BROWSER_DOWN}.get(row["state"], Code.UNHEALTHY)
        return Result(code, f"browser {name} is {row['state']}", {**row, "chrome": setup.chrome_build(self.cfg)})

    def _row(self, b: Browser) -> dict[str, Any]:
        """One browser's state, holder and expiry. Never its endpoint: only lease hands one out."""
        lease = self.leases.current(b.name)
        state = services.state(b.unit)
        running = state not in services.GONE  # as is_active: a session that may still run is never called down
        expired = bool(lease and lease.expires_at <= self.clock())
        row: dict[str, Any] = {
            "browser": b.name,
            "holder": lease.holder if lease else None,
            "expires_at": iso(lease.expires_at) if lease else None,
            "expired": expired,
            "running_past_expiry": running and expired,
        }
        if not running:
            row["state"] = "down"
        elif lease and lease.session and not lease.endpoint:
            row["state"] = "starting"
        else:
            proof = health.prove(b, lease.started_at if lease else 0.0, state)
            row["state"] = "healthy" if proof.ok else "unhealthy"
            row["failed"] = proof.failed
            row["notes"] = proof.notes
        return row

    # ----- lease

    def lease(self, name: str, holder: str, minutes: int | None = None) -> Result:
        """Grant, renew or take over the browser. Another holder's unexpired lease is BUSY at once, even while a start
        or stop is in flight (_turn)."""
        b = self.cfg.browser(name)
        ttl = self._ttl(minutes)

        def refuse() -> None:
            held = self.leases.peek(name)
            if held and held.holder != holder:
                raise busy(held)

        with self._turn(name, refuse):
            return self._lease_locked(b, holder, ttl)

    @contextmanager
    def _turn(self, name: str, refuse: Callable[[], None]) -> Iterator[None]:
        """Hold the browser's session lock: one start or stop at a time. A refusal never waits behind a start or stop in
        flight: while the lock is busy, `refuse` reads the lease again before every try and raises as soon as the lease
        on file refuses the caller. A lease attempt or release that is not refused waits for the start or stop, at most
        LOCK_WAIT seconds, then is BUSY naming no holder."""
        deadline = time.monotonic() + LOCK_WAIT
        while True:
            refuse()
            with self.leases.try_session_lock(name) as got:
                if got:
                    yield
                    return
            if time.monotonic() > deadline:
                raise BrowserError(
                    Code.BUSY, f"browser {name} has been starting or stopping for over {LOCK_WAIT:g} s; try again later",
                    {"browser": name, "holder": None, "expires_at": None},
                )
            time.sleep(LOCK_POLL)

    def _ttl(self, minutes: int | None) -> float:
        minutes = self.cfg.ttl_minutes if minutes is None else minutes
        if not 1 <= minutes <= self.cfg.max_minutes:
            raise BrowserError(Code.USAGE, f"--minutes must be from 1 to {self.cfg.max_minutes}")
        return minutes * 60.0

    def _lease_locked(self, b: Browser, holder: str, ttl: float) -> Result:
        on_file = self.leases.current(b.name)  # what acquire finds: every lease writer holds the session lock, as here
        lease, previous = self.leases.acquire(b.name, holder, ttl)  # BUSY when another holder got there first
        if lease.session and self._answering(b, lease):
            self.audit.record(b.name, holder, "extend", lease.session, expires_at=iso(lease.expires_at))
            return self._granted(b, lease, "extended")
        taking = {"previous_holder": previous.holder, "previous_session": previous.session} if previous else {}
        old_gone = began = False  # how far the attempt got: the old session shown gone, then the new start begun
        attempt = _Attempt()  # and then whether the new session's service was asked to start
        try:
            self._refuse_git_trees(b)  # before anything is written in or beside the profile or its downloads
            with self._profile_start(b):  # another config's start of this profile never overlaps this one
                self._stop_old(b, lease, previous)  # records a takeover, or the stop of the holder's own session
                old_gone = True
                self._refuse_collisions(b, lease)
                began = True
                lease = self._start(b, holder, ttl, attempt)
        except BrowserError as exc:
            if isinstance(exc, LeaseKept):  # what the start began may still run: the lease stays and names it
                failed = exc.data["session"]
            else:
                failed = self._settle(b, lease, previous, on_file, old_gone, began)
            self.audit.record(b.name, holder, "start-failed", failed, code=exc.code.name, reason=exc.message, **taking)
            raise
        except BaseException:  # a Ctrl-C, or a bug: settled as a refusal or a failed start at that point is
            if not attempt.service_asked:  # once it was, the session may run: the lease stays and names it (LeaseKept)
                self._settle(b, lease, previous, on_file, old_gone, began)
            raise
        self.audit.record(b.name, holder, "lease", lease.session, expires_at=iso(lease.expires_at))
        return self._granted(b, lease, "started")

    def _settle(
        self, b: Browser, lease: Lease, previous: Lease | None, on_file: Lease | None, old_gone: bool, began: bool,
    ) -> str:
        """Settle the lease after a lease attempt failed (other than in LeaseKept), or was interrupted before its
        session's service was asked to start, and name the session that failed to start: "" when none did. A failed
        attempt never frees a lease whose session may still run, and never takes away a lease it did not make. `lease`
        is the one acquire wrote for the attempt, `on_file` the one it found.

        - A start that began and failed showed what it began gone (one that cannot ends in LeaseKept), and one
          interrupted before its service was asked to start has nothing running: the lease is freed, and the session
          the start recorded on it is the one that failed.
        - Refused before its start began (another start of the profile under way, a start lock that cannot be opened,
          an old session that cannot be shown gone, a collision): the holder's own lease stays as acquire renewed it,
          still naming its session. A lapsed lease being taken over goes back to the previous holder as it was, unless
          that holder's session was shown gone: the takeover is then on record, and done. Only a lease the attempt
          made is freed."""
        own = on_file is not None and on_file.holder == lease.holder
        if began:
            dropped = self.leases.drop(b.name, lease.holder)
            return dropped.session if dropped else ""
        if previous and not old_gone:
            self.leases.give_back(b.name, lease, previous)
        elif not own:
            self.leases.drop(b.name, lease.holder)
        return ""

    @staticmethod
    def _refuse_git_trees(b: Browser) -> None:
        """USAGE when the browser's profile or its download folder, links resolved, is inside a git working tree (or
        cannot be resolved or checked), as setup refuses them. Loading the config checks only the state and profiles
        folders, and either of these can be a link that leads elsewhere; the start lock, the preferences and Chrome
        would write signed-in profile data there."""
        refuse_inside_git_tree(b.profile, f"browser {b.name}'s profile")
        refuse_inside_git_tree(b.downloads, f"browser {b.name}'s download folder")

    @contextmanager
    def _profile_start(self, b: Browser) -> Iterator[None]:
        """Hold the start lock that sits beside the browser's profile, named for the profile itself with links
        resolved. Two configs that reach one profile, through a shared profiles folder or a link, share the lock, so
        their starts of it never overlap: a start that finds it held is refused at once, before it checks, seeds or
        cleans anything in the profile, and so it can never close the other start's Chrome through the shared port
        file.

        Only folders the tool owns are made owner-only: the configured profiles folder, and one the tool makes. A
        profile linked to a folder elsewhere gets its lock there, owner-only, and that folder keeps its mode. A lock
        that cannot be opened or locked (a folder the user cannot write, a filesystem without locks) is USAGE."""
        try:
            private_dir(b.profiles_dir)
            profile = Path(os.path.realpath(b.profile))  # the profile itself, however its folder is reached
            if not profile.parent.is_dir():  # a link to a folder that does not exist yet: the tool makes it
                private_dir(profile.parent)
            fd = os.open(profile.parent / f"{profile.name}.start", os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as exc:  # a folder that cannot be made or narrowed, or a lock that cannot be opened
            raise BrowserError(
                Code.USAGE, f"cannot open the start lock of browser {b.name}'s profile: {exc}", {"browser": b.name},
            ) from None
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise BrowserError(
                    Code.BROWSER_DOWN,
                    f"browser {b.name}'s profile is being started by another session (another config that reaches "
                    "the same profile); try again once it is up",
                    {"browser": b.name},
                ) from None
            except OSError as exc:  # a filesystem that cannot lock: a problem in the environment, as above
                raise BrowserError(
                    Code.USAGE, f"cannot lock the start lock of browser {b.name}'s profile: {exc}", {"browser": b.name},
                ) from None
            yield
        finally:
            os.close(fd)  # closing the descriptor releases the lock

    def _answering(self, b: Browser, lease: Lease) -> bool:
        """The lease's session still runs, its unit exactly active, and answers at the endpoint the lease records. A
        unit in any other state is not resumed: one that is deactivating already has a stop under way, and the
        endpoint it would hand out dies within the stop grace."""
        if not lease.endpoint or services.state(b.unit) != "active":
            return False
        try:
            port_file = devtools.read_port_file(b.port_file)
            if port_file is None or port_file.endpoint != lease.endpoint:
                return False
            return devtools.version(port_file.port).get("webSocketDebuggerUrl") == lease.endpoint
        except devtools.DevToolsError:
            return False

    def _stop_old(self, b: Browser, lease: Lease, previous: Lease | None) -> None:
        """Make way for a new session: stop the one still running (a takeover, or the holder's own session that stopped
        answering) and show it gone. The takeover, or the stop of the holder's own session, is recorded with how the old
        session ended as soon as that stop is done: a refusal after it, or a new start that fails or is killed, cannot
        keep it off the record. BrowserError, with nothing recorded, when the old session cannot be shown gone."""
        running = services.is_active(b.unit)
        stopped = self._stop(b, (previous or lease).session, lease.holder) if running else "none"
        if previous:
            self.audit.record(
                b.name, lease.holder, "takeover", lease.session,
                previous_holder=previous.holder, previous_session=previous.session, stop=stopped,
            )
        elif running:  # the holder's own session, which no longer answered
            self.audit.record(b.name, lease.holder, "stop", lease.session, stop=stopped)

    def _refuse_collisions(self, b: Browser, lease: Lease) -> None:
        """Refuse a profile or a screen that a process the tool did not start has open: an agent's own Chrome on the
        profile, or another config's session or another VNC server on the screen's ports. That process is never
        stopped: it may be someone's work."""
        strays = procs.holding_profile(b.profile)
        if strays:
            self.audit.record(b.name, lease.holder, "collision", lease.session, pids=[p["pid"] for p in strays])
            raise BrowserError(
                Code.BROWSER_DOWN,
                f"browser {b.name}'s profile is open in a process orion-browser did not start ("
                + ", ".join(f"pid {p['pid']}" for p in strays)
                + "); it has to end before a session can start",
                {"processes": strays},
            )
        taken = [port for port in (b.vnc_port, b.view_port) if _answers(loopback(), port)]
        if taken:
            self.audit.record(b.name, lease.holder, "collision", lease.session, ports=taken)
            raise BrowserError(
                Code.BROWSER_DOWN,
                f"browser {b.name}'s screen {b.display} is in use by a program orion-browser did not start (listening on "
                + ", ".join(map(str, taken))
                + "); it has to end before a session can start",
                {"ports": taken},
            )

    def _start(self, b: Browser, holder: str, ttl: float, attempt: _Attempt) -> Lease:
        """Start a session for the holder's lease and prove it healthy. The lease records the session before its service
        starts (the session refuses to run for any other), and the profile's preferences are written while Chrome is not
        running. A start whose Chrome never wrote its port file is tried once more, on a new port (Q4), unless the
        session said why its own start failed before Chrome started (its view did not come up): that is the answer.
        The lease that records the started session runs `ttl` from that moment, so a slow start never hands out a lease
        partly used or already expired.

        A start that ends in an error once its service may run (systemd-run or systemctl gave no answer in time, or the
        unit's state could not be read) stops what it began, as a start that is not proven healthy does. The lease is
        freed only once what it began is shown gone (LeaseKept otherwise): no session is left running that no lease
        owns."""
        for number in (1, 2):
            attempt.service_asked = False  # nothing of this try runs yet (the first try's service was shown gone)
            session, started = uuid.uuid4().hex[:12], self.clock()
            self.leases.set_session(b.name, holder, session, "", 0, started)
            prefs.seed(b)
            for stale in (b.closing_marker, b.stop_marker, b.start_failure_marker, b.port_file):
                try:
                    stale.unlink(missing_ok=True)
                except OSError as exc:  # a folder in its place, a file that cannot be removed: nothing started yet
                    raise BrowserError(
                        Code.USAGE,
                        f"cannot remove the stale {stale} before browser {b.name}'s start: {exc.strerror or exc}",
                        {"browser": b.name, "path": str(stale)},
                    ) from None
            try:
                attempt.service_asked = True  # from here the session may run
                services.start(b.unit, self._session_argv(b), self._service_env(session), self.cfg.stop_timeout)
                port_file, proof = self._await_healthy(b, started)
            except BrowserError as exc:
                self._stop_begun(b, holder, session, exc)
                raise
            if port_file is not None and proof.ok:
                return self.leases.set_session(
                    b.name, holder, session, port_file.endpoint, port_file.port, started, ttl=ttl,
                )
            reported = self._reported_failure(b, session, proof)
            failure = reported or self._start_failure(b, port_file, proof)
            self._stop_begun(b, holder, session, failure)
            if reported or port_file is not None or number == 2:
                raise failure
        raise AssertionError("unreachable: the second attempt returns or raises")

    def _stop_begun(self, b: Browser, holder: str, session: str, failure: BrowserError) -> None:
        """Stop a session this start began, which did not prove healthy or whose start ended in an error, and record how
        the stop ended (none when nothing of it ran): every stop is on record, the one before the second try (Q4)
        included.

        A session that cannot be shown gone may still run, so the lease is not freed: the start ends in LeaseKept,
        BROWSER_DOWN naming why the start failed and why the stop did, with the start's own data (its failed checks)
        and the stop's, and the holder keeps the lease that names the session, as a release that cannot show its
        session gone does. No second try starts beside it."""
        try:
            stopped = self._stop(b, session, holder)
        except BrowserError as exc:
            raise LeaseKept(
                Code.BROWSER_DOWN,
                f"{failure.message}; stopping the session it began failed ({exc.message}), so the lease "
                f"stays yours: release browser {b.name} once that session can be stopped",
                {**failure.data, **exc.data, "browser": b.name, "session": session},
            ) from exc
        self.audit.record(b.name, holder, "stop", session, stop=stopped)

    def _start_failure(self, b: Browser, port_file: devtools.PortFile | None, proof: health.Health) -> BrowserError:
        """UNHEALTHY when the session runs and its Chrome wrote a port file but did not prove healthy. BROWSER_DOWN
        when its service has ended (whether or not its Chrome wrote a port file first) or Chrome never wrote one."""
        code = Code.UNHEALTHY if port_file is not None and proof.checks["service"] else Code.BROWSER_DOWN
        return BrowserError(
            code,
            f"browser {b.name} did not prove healthy within {self.cfg.start_limit:g} s (failed: {', '.join(proof.failed)})",
            {"failed": proof.failed, "notes": proof.notes},
        )

    @staticmethod
    def _reported_failure(b: Browser, session: str, proof: health.Health) -> BrowserError | None:
        """Why this session's own start failed, when the session wrote that down (runner: its view did not come up
        before Chrome was to start): BROWSER_DOWN in its words, with its data and the failed checks. None when it
        wrote nothing for this session."""
        try:
            record = json.loads(b.start_failure_marker.read_text())
        except (OSError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("session") != session:
            return None
        message, data = record.get("message"), record.get("data")
        if not isinstance(message, str) or not isinstance(data, dict):
            return None
        return BrowserError(Code.BROWSER_DOWN, message, {**data, "failed": proof.failed, "notes": proof.notes})

    @staticmethod
    def _session_argv(b: Browser) -> list[str]:
        return [sys.executable, "-P", "-m", "orion_browser", "_session", b.name]

    def _service_env(self, session: str) -> dict[str, str]:
        env = {key: os.environ[key] for key in PASSED_ENV if os.environ.get(key)}
        env.update({"ORION_BROWSER_SESSION": session, "ORION_BROWSER_CONFIG": str(self.cfg.path)})
        return env

    def _await_healthy(self, b: Browser, started: float) -> tuple[devtools.PortFile | None, health.Health]:
        """Wait, up to the start limit, for this session's port file and a passing health proof, and answer with the
        port file that proof judged: the start never takes a second look of its own, which could disagree. No proof
        starts once the limit is reached, and each gets only the time left for its DevTools calls and its capture,
        so a start answers within the limit and a little more, however slow those are.

        The answer is the newest proof, but one the limit cut short (it ended at the limit) may name a check it had no
        time to run: it never replaces a proof that ran to its end on the same port file, only one that saw less."""
        deadline = time.monotonic() + self.cfg.start_limit
        proof, whole = health.Health(), False  # until a proof is made, nothing is proven
        while True:
            state = services.state(b.unit)
            if time.monotonic() >= deadline:
                return proof.port_file, proof
            made = health.prove(b, started, state, deadline)
            if made.ok or state in services.GONE:  # healthy, or the session has ended: nothing more to wait for
                return made.port_file, made
            in_time = time.monotonic() < deadline
            if in_time or not whole or made.port_file != proof.port_file:
                proof, whole = made, in_time
            time.sleep(min(POLL, max(0.0, deadline - time.monotonic())))

    def _granted(self, b: Browser, lease: Lease, how: str) -> Result:
        return Result(Code.OK, f"{how} browser {b.name} for {lease.holder} until {iso(lease.expires_at)}", {
            "browser": b.name,
            "holder": lease.holder,
            "session": lease.session,
            "endpoint": lease.endpoint,
            "port": lease.port,
            "expires_at": iso(lease.expires_at),
            "watch": b.view_link(drive=False),
        })

    # ----- stop

    def _stop(self, b: Browser, session: str, holder: str) -> str:
        """Stop the browser's session and show it gone. Ask Chrome to close through DevTools and wait the grace; then
        stop the service, whose own stop handling closes cleanly when Chrome still answers. Returns clean, signal or kill
        (none when nothing ran). BROWSER_DOWN when the session cannot be shown gone.

        The close goes only to an endpoint a process of the session's own service listens on (_not_own_endpoint). A port
        file can name another: a Chrome the tool did not start that opened the profile after the start's collision
        check writes its own. That Chrome gets no close and no signal: the stop records that it skipped the close and
        goes on to the service manager's stop of the session's own service.

        The closing marker tells the session that the tool is closing it, so Chrome's exit is no crash. A stop that
        fails once the manager's stop was asked (no answer, a state that cannot be read, no gone proof) takes it back:
        the session may run on, and an exit of its own is then a crash again. A look that fails during the grace
        keeps it: Chrome may be taking the DevTools close, and that exit is the tool's."""
        if not services.is_active(b.unit) and not procs.holding_profile(b.profile):
            return "none"
        b.closing_marker.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        b.closing_marker.write_text(session)
        try:
            port_file = devtools.read_port_file(b.port_file)
        except devtools.DevToolsError:
            port_file = None
        if port_file is not None and (why := self._not_own_endpoint(b, port_file.port)):
            self.audit.record(b.name, holder, "close-skipped", session, by="tool", port=port_file.port, reason=why)
            port_file = None
        if port_file is not None:
            devtools.close_browser(port_file.endpoint)
            if self._wait_gone(b, self.cfg.stop_grace):  # a look that fails here keeps the marker (see above)
                return "clean"
        try:
            services.stop(b.unit, self.cfg.stop_timeout + 15)
            method = self._stop_method(b, session)
            if not self._wait_gone(b, GONE_AFTER_STOP):
                raise BrowserError(
                    Code.BROWSER_DOWN,
                    f"browser {b.name}'s session could not be shown gone",
                    {"service_active": services.is_active(b.unit), "processes": procs.holding_profile(b.profile)},
                )
            return method
        except BrowserError:
            b.closing_marker.unlink(missing_ok=True)  # it may run on: an exit of its own is a crash again
            raise

    @staticmethod
    def _not_own_endpoint(b: Browser, port: int) -> str:
        """Why the port is not shown to be the session's own endpoint, or "" when a process of the browser's service
        (its main process or one descended from it) listens on it. Not shown when the main process cannot be read."""
        try:
            main = services.main_pid(b.unit)
        except BrowserError:
            return "the service's main process is unknown"
        return "" if procs.tree_listens(main, loopback(), port) else "no process of the session listens on the port"

    @staticmethod
    def _stop_method(b: Browser, session: str) -> str:
        """How the session's own stop handling ended Chrome, from its stop marker. No marker for this session: the
        service manager killed it."""
        try:
            record = json.loads(b.stop_marker.read_text())
        except (OSError, ValueError):
            return "kill"
        if not isinstance(record, dict) or record.get("session") != session:
            return "kill"
        return str(record.get("stop", "kill"))

    def _wait_gone(self, b: Browser, seconds: float) -> bool:
        """Gone: the service is inactive and no process has the profile open. A silent port is not evidence."""
        deadline = time.monotonic() + seconds
        while True:
            if not services.is_active(b.unit) and not procs.holding_profile(b.profile):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(POLL)

    # ----- release

    def release(self, name: str, holder: str) -> Result:
        """Stop the session and free the caller's own lease, even a lapsed one. A release that is not the caller's is
        NOT_HOLDER at once: it never waits behind a start or stop in flight, also when the lease changes hands while
        the holder's own release waits for one (_turn). The holder's own release waits: a release already under way
        may yet fail and keep the lease."""
        b = self.cfg.browser(name)

        def refuse() -> None:
            current = self.leases.current(name)
            if current is None or current.holder != holder:
                raise not_held(name, holder, current)

        with self._turn(name, refuse):
            with self.leases.releasing(name, holder) as lease:  # the lease is dropped only if the stop succeeds
                if services.is_active(b.unit):
                    stopped = self._stop(b, lease.session, holder)
                elif strays := procs.holding_profile(b.profile):  # one look: the answer names what it found
                    raise BrowserError(
                        Code.BROWSER_DOWN,
                        f"browser {name}'s profile is open in a process orion-browser did not start; "
                        "the lease stays yours until it ends",
                        {"processes": strays},
                    )
                else:
                    stopped = "none"
                self.audit.record(name, holder, "release", lease.session, stop=stopped)
        return self._released(name, stopped)

    @staticmethod
    def _released(name: str, stopped: str) -> Result:
        message = f"released browser {name}"
        if stopped in ("signal", "kill"):
            message += "; Chrome did not close cleanly, so sign-in changes from the last minute or so may not have been saved"
        return Result(Code.OK, message, {"browser": name, "stop": stopped})

    # ----- view

    def view(self, name: str, holder: str | None, drive: bool) -> Result:
        """The watch link for a running session, and the drive link only for the holder of its unexpired lease. A
        refusal names why, as orion-phone's do: the caller's own lease has expired, another holder has it, or nobody
        does."""
        b = self.cfg.browser(name)
        if not services.is_active(b.unit):
            raise BrowserError(Code.BROWSER_DOWN, f"browser {name} has no session; lease it first")
        data = {"browser": name, "watch": b.view_link(drive=False)}
        if drive:
            lease = self.leases.current(name)
            if not holder or lease is None or lease.holder != holder or lease.expires_at <= self.clock():
                raise not_held(name, holder or "(no holder)", lease)
            data["drive"] = b.view_link(drive=True)
        return Result(Code.OK, f"view of browser {name}", data)


def _answers(host: str, port: int) -> bool:
    """Whether something already accepts connections on host:port: a screen or a view another program holds."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except OSError:
        return False
