"""Profile preferences a pool browser needs, written before Chrome starts (Chrome must not be running)."""

from __future__ import annotations

import json
import os
from typing import Any

from orion_browser.codes import BrowserError, Code
from orion_browser.config import Browser
from orion_browser.lease import private_dir


def seed(b: Browser) -> None:
    """Downloads go to the browser's own directory with no prompt; no password-saving prompts; the last exit is on
    record as clean, so Chrome shows an agent no "restore pages" bubble; and Chrome restores the last session, session
    cookies included (open question Q2), only while the browser's config says so. Anything that keeps the preferences
    from being written is USAGE naming the path, so the start is refused and its lease freed."""
    path = b.profile / "Default" / "Preferences"
    for folder in (b.profile, b.profile / "Default", b.downloads):
        try:
            private_dir(folder)  # each level it makes is owner-only, and so is the folder itself, as setup makes them
        except OSError as exc:
            raise BrowserError(Code.USAGE, f"cannot make the owner-only folder {folder}: {exc}") from None
    try:
        prefs = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError, RecursionError) as exc:  # RecursionError: nested deeper than the reader goes
        raise BrowserError(Code.USAGE, f"cannot read Chrome's preferences {path}: {exc}") from None
    if not isinstance(prefs, dict):
        raise BrowserError(Code.USAGE, f"Chrome's preferences {path} are not a JSON object")
    _section(prefs, "download", path).update(
        {"default_directory": str(b.downloads), "prompt_for_download": False}
    )
    _section(prefs, "profile", path).update(
        {"exit_type": "Normal", "exited_cleanly": True, "password_manager_enabled": False}
    )
    prefs["credentials_enable_service"] = False
    if b.restore_session:
        _section(prefs, "session", path)["restore_on_startup"] = 1  # 1: continue where you left off
    elif "session" in prefs and _section(prefs, "session", path).get("restore_on_startup") == 1:
        # Turned off again: the restore value goes, so Chrome opens its own default, the New Tab page. Any other
        # start-up choice (a set of pages, say) restores nothing and is the operator's, so it stays.
        del prefs["session"]["restore_on_startup"]
    tmp = path.with_name("Preferences.orion-browser.tmp")
    try:
        with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
            json.dump(prefs, fh)
            fh.flush()
            os.fsync(fh.fileno())  # on disk before the rename: a crash never leaves an empty Preferences behind
        os.replace(tmp, path)
    except OSError as exc:
        raise BrowserError(Code.USAGE, f"cannot write Chrome's preferences {path}: {exc}") from None


def _section(prefs: dict[str, Any], key: str, path: object) -> dict[str, Any]:
    section = prefs.setdefault(key, {})
    if not isinstance(section, dict):
        raise BrowserError(Code.USAGE, f"Chrome's preferences {path} hold a {key!r} that is not an object")
    return section
