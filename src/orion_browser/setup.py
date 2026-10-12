"""Setup: check the host's packages and prepare owner-only directories. It never installs anything and never runs as
root: it prints the install command for the operator."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from orion_browser import services
from orion_browser.codes import BrowserError, Code, Result
from orion_browser.config import Config, refuse_inside_git_tree
from orion_browser.lease import private_dir

TOOLS = (
    ("Xvnc", "tigervnc-standalone-server"), ("websockify", "websockify"), ("xdotool", "xdotool"), ("xwd", "x11-apps"),
    ("systemctl", "systemd"), ("systemd-run", "systemd"),
)
MANAGER_OK = ("running", "degraded")  # degraded: some unrelated unit failed; the user instance still runs services
OUTDATED_AFTER = 8 * 7 * 86400  # seconds: Chrome's own outdated-build age, past which it counts itself out of date
UPDATE = "sudo apt install --only-upgrade google-chrome-stable"  # the operator's: only setup prints it


def chrome_build(cfg: Config) -> dict[str, object]:
    """The configured Chrome's version, the age of its build in whole days, and whether the build is outdated: older
    than eight weeks, Chrome's own outdated-build age (False under it, None when the age cannot be read). The session
    turns off Chrome's out-of-date bubble (it took the page's focus), so this is where the operator learns that Chrome
    needs updating. The age is the modification time of the program file the [chrome] path leads to (links followed),
    which Google's package sets to the build's time. What cannot be read is None.

    status hands this to agents (and over MCP), so it never holds the command that updates Chrome: an outdated build
    gets a note for the operator, and only setup, the operator's command, prints the command. Updating is a package
    change under every live session, the operator's alone. Each setup and status call runs the program with --version
    (about 20 ms; a hung one is given up on after 10 s); a lease is never refused over what it finds."""
    info: dict[str, object] = {"version": None, "age_days": None, "outdated": None}
    try:
        age = max(0.0, time.time() - os.stat(cfg.chrome).st_mtime)
    except OSError:
        return info
    try:
        said = subprocess.run(
            [str(cfg.chrome), "--version"], capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL,
            check=True,  # only a run that succeeded is read for a version
        ).stdout
    except (OSError, subprocess.SubprocessError):  # cannot be run, hung, or failed
        said = ""
    info["version"] = next((word for word in said.split() if word[:1].isdigit()), None)  # "Google Chrome 150.0..."
    info["age_days"] = int(age // 86400)
    info["outdated"] = age > OUTDATED_AFTER
    if info["outdated"]:
        info["note"] = (
            f"Chrome's build is {info['age_days']} days old, past Chrome's own eight weeks; "
            "the operator should update it (orion-browser setup prints how)"
        )
    return info


def run(cfg: Config) -> Result:
    missing: list[str] = []
    packages: set[str] = set()
    if not os.access(cfg.chrome, os.X_OK):
        missing.append(f"{cfg.chrome} (Google Chrome stable, from Google's own package)")
    for tool, package in TOOLS:
        if shutil.which(tool) is None:
            missing.append(f"{tool} (package {package})")
            packages.add(package)
    if not (cfg.novnc_dir / "vnc.html").is_file():
        missing.append(f"{cfg.novnc_dir}/vnc.html (package novnc)")
        packages.add("novnc")
    if shutil.which("systemctl") is not None:  # without it there is no state to ask for, and it is named above
        state = services.manager_state()
        if state not in MANAGER_OK:
            missing.append(f"systemd's user instance (it reports {state!r})")
    if missing:
        data: dict[str, object] = {"missing": missing}
        if packages:
            data["install"] = "sudo apt install " + " ".join(sorted(packages))
        raise BrowserError(Code.USAGE, "missing: " + "; ".join(missing), data)
    folders = [cfg.state_dir, cfg.profiles_dir]
    for b in cfg.browsers.values():
        folders += [b.profile, b.downloads]
    for folder in folders:  # every one, not only the roots: a repository can sit inside the state folder too
        refuse_inside_git_tree(folder, "directory")
    for folder in folders:
        private_dir(folder)  # safe when a lease makes the same folder at the same moment
    chrome = chrome_build(cfg)
    if chrome["outdated"]:
        print(
            f"orion-browser: warning: Chrome's build is {chrome['age_days']} days old, past Chrome's own eight weeks; "
            f"update it: {UPDATE}",
            file=sys.stderr, flush=True,
        )
    return Result(Code.OK, f"ready: {len(cfg.browsers)} browsers", {
        "browsers": sorted(cfg.browsers), "directories": [str(folder) for folder in folders], "chrome": chrome,
    })
