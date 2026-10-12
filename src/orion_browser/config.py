"""Load and validate the operator's config. State and profiles never live inside a git tree."""

from __future__ import annotations

import hashlib
import ipaddress
import os
import pwd
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from orion_browser.codes import BrowserError, Code

NAME_RE = re.compile(r"[a-z0-9]{1,16}")  # a browser's name becomes a file name and part of a unit name
INSIDE_GIT_TREE = "is inside a git working tree; keep it outside any repository"
TOP_KEYS = frozenset({"chrome", "screen", "lease", "timing", "paths", "view", "browser"})
SEPARATE_FOLDERS = "paths.state and paths.profiles must be separate folders, neither inside the other"


def loopback() -> str:
    """The loopback address every endpoint, VNC port and view uses. ORION_BROWSER_LOOPBACK exists for the tests, which
    use an address of their own so that they never touch a real session's ports; an empty value means the default."""
    host = os.environ.get("ORION_BROWSER_LOOPBACK") or "127.0.0.1"
    try:
        address = ipaddress.IPv4Address(host)  # strict: no leading zeros, ASCII digits only
    except ValueError:
        address = None
    if address is None or not address.is_loopback:
        raise BrowserError(Code.USAGE, f"ORION_BROWSER_LOOPBACK must be a 127.x.y.z address (got {host!r})")
    return str(address)


@dataclass(frozen=True)
class Browser:
    name: str
    screen: int
    restore_session: bool
    profiles_dir: Path
    state_dir: Path

    @property
    def unit(self) -> str:
        """The session's service name: the browser's name and a tag of its lease store (a short hash of the state
        folder itself: the same folder, however spelled or reached through a link, gives the same tag, so a holder
        who reaches its store another way still finds its own session). A second config under the same OS user gets
        services of its own, so it can never stop or share the first's session; a screen, port or profile the two
        share is refused when a session starts, not shared."""
        tag = hashlib.sha256(os.fsencode(os.path.realpath(self.state_dir))).hexdigest()[:8]
        return f"orion-browser-{self.name}-{tag}.service"

    @property
    def profile(self) -> Path:
        return self.profiles_dir / self.name

    @property
    def downloads(self) -> Path:
        return self.state_dir / "downloads" / self.name

    @property
    def port_file(self) -> Path:
        return self.profile / "DevToolsActivePort"

    @property
    def closing_marker(self) -> Path:
        """Written by a stop before it asks Chrome to close, so the session can tell that exit from a crash."""
        return self.state_dir / "sessions" / f"{self.name}.closing"

    @property
    def stop_marker(self) -> Path:
        """Written by the session when a stop signal made it end Chrome: which session, and how the stop ended."""
        return self.state_dir / "sessions" / f"{self.name}.stop"

    @property
    def start_failure_marker(self) -> Path:
        """Written by the session when its own start fails before Chrome starts (its view did not come up): which
        session, and why, for the lease that started it."""
        return self.state_dir / "sessions" / f"{self.name}.start-failed"

    @property
    def display(self) -> str:
        return f":{self.screen}"

    @property
    def vnc_port(self) -> int:
        return 5900 + self.screen

    @property
    def view_port(self) -> int:
        return 6080 + self.screen

    def view_link(self, drive: bool) -> str:
        mode = "0" if drive else "1"
        return f"http://{loopback()}:{self.view_port}/vnc.html?autoconnect=1&resize=scale&view_only={mode}"


@dataclass(frozen=True)
class Config:
    path: Path
    chrome: Path
    width: int
    height: int
    ttl_minutes: int
    max_minutes: int
    stop_grace: float
    stop_timeout: float
    start_limit: float
    state_dir: Path
    profiles_dir: Path
    novnc_dir: Path
    browsers: dict[str, Browser]

    def browser(self, name: str) -> Browser:
        if name not in self.browsers:
            raise BrowserError(Code.USAGE, f"no browser named {name!r} in {self.path}", {"browsers": sorted(self.browsers)})
        return self.browsers[name]


def _usage(message: str) -> BrowserError:
    return BrowserError(Code.USAGE, message)


def _home() -> Path:
    """The operator's home directory: HOME when it is set, otherwise the account's home from the user database. The
    config, state and profiles live under it, the same for every caller: a per-process setting such as XDG_STATE_HOME
    (some launchers, the VS Code snap among them, change it) would give two callers two lease stores, and both could
    drive one browser. Either source must be an absolute path, for the same reason: an empty one (which the standard
    library would read as the root folder) or a relative one would put the lease store under the root folder, or under
    whichever folder the caller happens to be in."""
    raw = os.environ.get("HOME")
    source = "HOME"
    if raw is None:
        try:
            raw = pwd.getpwuid(os.getuid()).pw_dir  # not Path.home(): that turns an empty entry into "/"
        except KeyError:  # no HOME and no entry for this account
            raise _usage("there is no home directory to put the config, state and profiles under; set HOME") from None
        source = "the account's home in the user database"
    home = Path(raw)
    if not home.is_absolute():  # an empty value is Path("."), so it is refused here too
        raise _usage(f"{source} must be an absolute path (found {raw!r}); set HOME to the home directory")
    return home


def config_path() -> Path:
    raw = os.environ.get("ORION_BROWSER_CONFIG")
    if raw:
        return _absolute(raw, "ORION_BROWSER_CONFIG")
    return _home() / ".config" / "orion-browser" / "config.toml"


def default_state() -> Path:
    return _home() / ".local" / "state" / "orion-browser"


def default_profiles() -> Path:
    return _home() / ".local" / "share" / "orion-browser" / "profiles"


def _git_tree_problem(path: Path) -> str | None:
    """Why path may not hold state or profiles, worded to follow the path in an error message; None when it may.
    Adapted from orion-phone: a `gitdir:` file, or a non-empty .git directory, at or above path. A .git directory
    counts with or without a HEAD: a repository that lost its HEAD is damaged, not absent. An empty one is skipped, as
    if absent: git itself would not call it a repository, and other programs' sandboxes mount an empty .git briefly
    (under /tmp, for example). Fails closed: a .git that cannot be inspected (no permission, a symlink loop, an I/O
    error) refuses the path."""
    path = Path(os.path.abspath(path))  # a relative path walks up from the working directory
    for parent in [path, *path.parents]:
        dot = parent / ".git"
        try:
            mode = os.stat(dot).st_mode  # follows a symlinked .git; a loop or a permission error raises below
            if stat.S_ISREG(mode):
                with dot.open("rb") as fh:
                    if fh.read(8) == b"gitdir: ":
                        return INSIDE_GIT_TREE
            elif stat.S_ISDIR(mode):
                with os.scandir(dot) as entries:  # the listing races like the stat: gone means absent, else fail closed
                    if next(entries, None) is not None:  # an empty .git is skipped, as if absent
                        return INSIDE_GIT_TREE
        except FileNotFoundError:
            continue
        except OSError as exc:
            where = f" at {exc.filename}" if exc.filename else ""
            return (
                f"could not be checked for a git working tree ({exc.strerror or exc}{where}); "
                "fix that or keep it outside any repository"
            )
    return None


def _absolute(raw: str, label: str) -> Path:
    if "\0" in raw:
        raise _usage(f"{label} must not contain a NUL character")
    if raw == "~" or raw.startswith("~/"):
        # The home the defaults use. Path.expanduser() reads HOME on its own and turns an empty one into "/", and a
        # remainder that begins with "/" would discard the home when joined, so the slashes are stripped first.
        try:
            path = _home() / raw[1:].lstrip("/")
        except BrowserError as exc:
            raise _usage(f"{label} cannot expand ~ in {raw!r}: {exc.message}") from None
    else:
        try:
            path = Path(raw).expanduser()  # "~user" reads the user database, not HOME
        except RuntimeError:  # ~user names nobody
            raise _usage(f"{label} cannot expand ~ in {raw!r}: no such user") from None
    if not path.is_absolute():
        raise _usage(f"{label} must be an absolute path or start with ~ (got {raw!r})")
    return path


FLAG_BREAK = " --"  # where procs reads the next flag in Chrome's one-string command line (procs._BOUNDARY)


def _refuse_flag_break(path: Path, label: str) -> None:
    """USAGE when the path holds " --". Chrome rewrites its process title into one string, and procs reads its
    --user-data-dir value up to the next " --": a profile path holding that is read cut short, so a Chrome on it would
    go unseen by the collision and gone checks. The state folder holds each browser's download folder."""
    if FLAG_BREAK in str(path):
        raise _usage(
            f"{label} {path} must not contain ' --' (a space and then two hyphens): in Chrome's command line, as the "
            "system shows it, that reads as the start of Chrome's next flag, so a Chrome on that profile could go "
            "unseen; rename the folder"
        )


def _table(value: Any, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _usage(f"{where} must be a table")
    return value


def _only(table: dict[str, Any], allowed: frozenset[str] | set[str], where: str) -> None:
    unknown = sorted(set(table) - set(allowed))
    if unknown:
        raise _usage(f"{where} has unknown keys: {', '.join(unknown)}")


def _text(table: dict[str, Any], key: str, default: str, label: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str) or not value:
        raise _usage(f"{label} must be a non-empty string")
    return value


def _int(table: dict[str, Any], key: str, default: int | None, label: str, low: int, high: int) -> int:
    if key not in table:
        if default is None:
            raise _usage(f"{label} is required")
        return default
    value = table[key]
    if type(value) is not int or not low <= value <= high:  # `type(...) is int` also refuses booleans
        raise _usage(f"{label} must be a whole number from {low} to {high}")
    return value


def _path(table: dict[str, Any], key: str, default: Callable[[], Path], label: str) -> Path:
    """An absolute path from the config, or the default, which is computed only when the key is absent."""
    if key not in table:
        return default()
    return _absolute(_text(table, key, "", label), label)


def _overlap(a: Path, b: Path) -> bool:
    a, b = Path(os.path.normpath(a)), Path(os.path.normpath(b))
    return a == b or a in b.parents or b in a.parents


def _resolved(folder: Path, label: str) -> Path:
    """folder with its links resolved; USAGE when it cannot be (a symlink loop, or a link that cannot be read)."""
    try:
        return folder.resolve()
    except (OSError, RuntimeError) as exc:
        raise _usage(f"{label} {folder} cannot be resolved: {exc}") from None


def refuse_inside_git_tree(folder: Path, label: str) -> None:
    """USAGE when folder (links resolved) is inside a git working tree, or cannot be resolved or checked. The message
    names the folder, and where it leads when a link takes it elsewhere."""
    resolved = _resolved(folder, label)
    problem = _git_tree_problem(resolved)
    if problem:
        leads = f" (a link to {resolved})" if resolved != Path(os.path.abspath(folder)) else ""
        raise _usage(f"{label} {folder}{leads} {problem}")


def load() -> Config:
    # A set HOME must be absolute even when every path below is explicit; an unset one is asked only if it is needed.
    if "HOME" in os.environ:
        _home()
    path = config_path()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise _usage(f"no config at {path}: copy config.example.toml there and edit it") from None
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise _usage(f"cannot read config {path}: {exc}") from None
    cfg = parse(raw, path)
    folders = (("paths.state", cfg.state_dir), ("paths.profiles", cfg.profiles_dir))
    for label, folder in folders:
        refuse_inside_git_tree(folder, label)
    # parse() compared the folders as written; a link can make two folders that look apart one place
    if _overlap(*(_resolved(folder, label) for label, folder in folders)):
        raise _usage(SEPARATE_FOLDERS)
    for b in cfg.browsers.values():  # a profile can be a link: a Chrome on it is started with where it leads
        label = f"browser {b.name}'s profile {b.profile}, a link to"
        resolved = _resolved(b.profile, f"browser {b.name}'s profile")
        _refuse_flag_break(resolved, label if resolved != b.profile else f"browser {b.name}'s profile")
    loopback()  # a bad test override is refused now, not after a session has started
    return cfg


def parse(raw: dict[str, Any], path: Path) -> Config:
    _only(raw, TOP_KEYS, "config")
    chrome = _table(raw.get("chrome", {}), "[chrome]")
    _only(chrome, {"path"}, "[chrome]")
    screen = _table(raw.get("screen", {}), "[screen]")
    _only(screen, {"width", "height"}, "[screen]")
    lease = _table(raw.get("lease", {}), "[lease]")
    _only(lease, {"ttl_minutes", "max_minutes"}, "[lease]")
    timing = _table(raw.get("timing", {}), "[timing]")
    _only(timing, {"stop_grace_seconds", "service_stop_timeout_seconds", "start_limit_seconds"}, "[timing]")
    paths = _table(raw.get("paths", {}), "[paths]")
    _only(paths, {"state", "profiles"}, "[paths]")
    view = _table(raw.get("view", {}), "[view]")
    _only(view, {"novnc_dir"}, "[view]")

    ttl = _int(lease, "ttl_minutes", 30, "lease.ttl_minutes", 1, 10080)
    longest = _int(lease, "max_minutes", 240, "lease.max_minutes", 1, 10080)
    if ttl > longest:
        raise _usage("lease.ttl_minutes must not be longer than lease.max_minutes")
    grace = _int(timing, "stop_grace_seconds", 15, "timing.stop_grace_seconds", 1, 300)
    stop_timeout = _int(timing, "service_stop_timeout_seconds", 30, "timing.service_stop_timeout_seconds", 11, 600)
    if stop_timeout < grace + 10:
        raise _usage(
            "timing.service_stop_timeout_seconds must be at least 10 seconds longer than timing.stop_grace_seconds"
        )
    state = _path(paths, "state", default_state, "paths.state")
    profiles = _path(paths, "profiles", default_profiles, "paths.profiles")
    _refuse_flag_break(state, "paths.state")
    _refuse_flag_break(profiles, "paths.profiles")
    if _overlap(state, profiles):
        raise _usage(SEPARATE_FOLDERS)

    entries = raw.get("browser", [])
    if not isinstance(entries, list) or not entries:
        raise _usage("config needs at least one [[browser]] with a name and a screen")
    browsers: dict[str, Browser] = {}
    screens: set[int] = set()
    for number, entry in enumerate(entries, 1):
        where = f"[[browser]] #{number}"
        entry = _table(entry, where)
        _only(entry, {"name", "screen", "restore_session"}, where)
        name = entry.get("name")
        if not isinstance(name, str) or not NAME_RE.fullmatch(name):
            raise _usage(f"{where}: name must be 1 to 16 lowercase letters or digits")
        if name in browsers:
            raise _usage(f"browser name {name!r} appears twice")
        screen_number = _int(entry, "screen", None, f"browser {name}: screen", 10, 99)
        if screen_number in screens:
            raise _usage(f"browser {name}: screen {screen_number} belongs to another browser")
        restore = entry.get("restore_session", False)
        if type(restore) is not bool:
            raise _usage(f"browser {name}: restore_session must be true or false")
        screens.add(screen_number)
        browsers[name] = Browser(name, screen_number, restore, profiles, state)
    return Config(
        path=path,
        chrome=_absolute(_text(chrome, "path", "/usr/bin/google-chrome", "chrome.path"), "chrome.path"),
        width=_int(screen, "width", 1920, "screen.width", 640, 7680),
        height=_int(screen, "height", 1080, "screen.height", 480, 4320),
        ttl_minutes=ttl,
        max_minutes=longest,
        stop_grace=float(grace),
        stop_timeout=float(stop_timeout),
        start_limit=float(_int(timing, "start_limit_seconds", 30, "timing.start_limit_seconds", 2, 600)),
        state_dir=state,
        profiles_dir=profiles,
        novnc_dir=_absolute(_text(view, "novnc_dir", "/usr/share/novnc", "view.novnc_dir"), "view.novnc_dir"),
        browsers=browsers,
    )
