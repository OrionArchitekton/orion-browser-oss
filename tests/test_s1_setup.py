"""S1: setup checks the host and prepares owner-only directories; holder names are checked."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time

import pytest

UPDATE = "sudo apt install --only-upgrade google-chrome-stable"
PRIVILEGED = re.compile(r"\bsudo\b|\bapt\b")


def text_run(env, *args: str) -> subprocess.CompletedProcess:
    """The CLI as the operator runs it, in text: its exit code, stdout and stderr."""
    return subprocess.run(
        [sys.executable, "-m", "orion_browser", *args], capture_output=True, text=True, env=env.env, timeout=120
    )


def age_chrome(env, days: float, program: str = "google-chrome") -> None:
    """Date a program file under the test's bin folder as a build `days` days old."""
    when = time.time() - days * 86400
    os.utime(env.root / "bin" / program, (when, when))


def test_setup_and_status_report_chromes_version_and_the_age_of_its_build(env):
    """A build made today: its version and age are reported, it is not outdated, and nothing is said about updating."""
    fresh = {"version": "150.0.0.0", "age_days": 0, "outdated": False}
    code, out = env.run("setup")
    assert code == 0 and out["data"]["chrome"] == fresh, out
    assert UPDATE not in text_run(env, "setup").stderr
    for args in (("status",), ("status", "work")):
        out = env.run(*args)[1]
        assert out["data"]["chrome"] == fresh, out


def test_an_outdated_build_is_flagged_by_status_without_the_command_which_only_setup_prints(env):
    """Past eight weeks, status (which agents read, over MCP too) flags the build as outdated with a note for the
    operator, and never carries the command that updates Chrome: updating is a package change under every live
    session, the operator's alone. setup, the operator's command, prints it. Nothing refuses a lease over it."""
    age_chrome(env, 60)
    for args in (("status",), ("status", "work")):
        out = env.run(*args)[1]
        assert not PRIVILEGED.search(json.dumps(out)), out
        chrome = out["data"]["chrome"]
        assert (chrome["version"], chrome["age_days"], chrome["outdated"]) == ("150.0.0.0", 60, True), out
        assert "operator" in chrome["note"], out
    done = text_run(env, "setup")
    assert done.returncode == 0 and UPDATE in done.stderr, done  # a warning line, not a refusal
    assert env.run("lease", "work", "--holder", "a")[0] == 0


@pytest.mark.parametrize("days, outdated", [(55, False), (57, True)])
def test_a_build_is_outdated_only_past_eight_weeks(env, days, outdated):
    """Chrome counts itself out of date past eight weeks, and so does status: no sooner, no later."""
    age_chrome(env, days)
    chrome = env.run("status", "work")[1]["data"]["chrome"]
    assert (chrome["age_days"], chrome["outdated"], "note" in chrome) == (days, outdated, outdated), chrome


def test_the_build_age_is_the_program_files_not_a_links(env):
    """The [chrome] path is usually a link (/usr/bin/google-chrome leads to Google's wrapper through two more): the age
    is that of the program file it leads to, which the package dates to the build, never the link's own."""
    shim = env.root / "bin" / "google-chrome"
    shim.rename(shim.with_name("google-chrome-150"))
    age_chrome(env, 60, "google-chrome-150")
    shim.symlink_to("google-chrome-150")  # a link made just now
    chrome = env.run("status", "work")[1]["data"]["chrome"]
    assert (chrome["version"], chrome["age_days"], chrome["outdated"]) == ("150.0.0.0", 60, True), chrome


def test_status_without_chrome_tells_nothing_of_it_and_keeps_its_own_exit_code(env):
    """With Chrome's program gone, status cannot tell its version, age or currency: each is null, with no note, and
    status answers as it always does (a free browser is BROWSER_DOWN, the list is OK), never INTERNAL."""
    (env.root / "bin" / "google-chrome").unlink()
    unknown = {"version": None, "age_days": None, "outdated": None}
    code, out = env.run("status", "work")
    assert (code, out["data"]["chrome"]) == (6, unknown), out
    code, out = env.run("status")
    assert (code, out["data"]["chrome"]) == (0, unknown), out


@pytest.mark.parametrize("says", ["", "Google Chrome 150.0.0.0"], ids=["silent", "prints-a-version"])
def test_a_chrome_whose_version_run_fails_has_no_version_but_its_age(env, says):
    """A program whose --version run exits 1 gives no version (what a failed run prints is not trusted, even a version
    number), while the age of its build is still reported."""
    broken = env.root / "bin" / "broken-chrome"
    broken.write_text("\n".join(["#!/bin/sh", *([f"echo '{says}'"] if says else []), "exit 1"]) + "\n")
    broken.chmod(0o755)
    age_chrome(env, 60, "broken-chrome")
    env.rewrite_config(f'path = "{env.root}/bin/google-chrome"', f'path = "{broken}"')
    code, out = env.run("status", "work")
    chrome = out["data"]["chrome"]
    assert (code, chrome["version"], chrome["age_days"], chrome["outdated"]) == (6, None, 60, True), out


def test_setup_makes_owner_only_directories(env):
    code, out = env.run("setup")
    assert code == 0, out
    for folder in ("state", "profiles", "profiles/anon", "profiles/work",
                   "state/downloads", "state/downloads/anon", "state/downloads/work"):
        assert (env.root / folder).stat().st_mode & 0o777 == 0o700, folder


def test_setup_names_each_missing_tool_and_the_install_command(env):
    (env.root / "bin" / "Xvnc").unlink()
    (env.root / "bin" / "xwd").unlink()
    (env.root / "novnc" / "vnc.html").unlink()
    code, out = env.run("setup")
    assert code == 2
    assert "Xvnc (package tigervnc-standalone-server)" in out["message"]
    assert "xwd (package x11-apps)" in out["message"]
    assert out["data"]["install"] == "sudo apt install novnc tigervnc-standalone-server x11-apps"
    assert not (env.root / "state").exists()  # nothing is prepared while something is missing


def test_setup_names_a_missing_chrome(env):
    (env.root / "bin" / "google-chrome").unlink()
    code, out = env.run("setup")
    assert code == 2 and "Google Chrome stable" in out["message"]


def test_setup_accepts_a_degraded_manager_and_refuses_a_stopped_one(env):
    env.set_fake(manager_state="degraded")
    assert env.run("setup")[0] == 0
    env.set_fake(manager_state="offline")
    code, out = env.run("setup")
    assert code == 2 and "systemd's user instance" in out["message"]


def test_state_inside_a_git_tree_is_refused(env):
    repo = env.root / "repo"
    (repo / ".git").mkdir(parents=True)
    (repo / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    env.rewrite_config(f'state = "{env.root}/state"', f'state = "{repo}/state"')
    code, out = env.run("setup")
    assert code == 2 and "inside a git working tree" in out["message"]


def test_a_download_folder_inside_a_nested_repository_is_refused(env):
    nested = env.root / "state" / "downloads" / "work"
    (nested / ".git").mkdir(parents=True)
    (nested / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    code, out = env.run("setup")
    assert code == 2 and "inside a git working tree" in out["message"] and "downloads/work" in out["message"]


def test_holder_rules(env):
    code, out = env.run("lease", "work")
    assert code == 2 and "--holder" in out["message"]
    code, out = env.run("lease", "work", "--holder", "has space")
    assert code == 2 and "1 to 64 characters" in out["message"]


def test_a_holder_is_at_most_64_characters(env):
    code, out = env.run("lease", "work", "--holder", "a" * 65)
    assert code == 2 and "1 to 64 characters" in out["message"]
    code, out = env.run("lease", "work", "--holder", "a" * 64)
    assert code == 0 and out["data"]["holder"] == "a" * 64, out
    assert env.run("release", "work", "--holder", "a" * 64)[0] == 0


def test_setup_names_every_missing_tool_at_once(env):
    for tool in ("systemctl", "systemd-run", "xdotool"):
        (env.root / "bin" / tool).unlink()
    code, out = env.run("setup")
    assert code == 2, out
    for named in ("systemctl (package systemd)", "systemd-run (package systemd)", "xdotool (package xdotool)"):
        assert named in out["message"], out
    assert out["data"]["install"] == "sudo apt install systemd xdotool"
    assert not (env.root / "state").exists()


@pytest.mark.parametrize("git", ["folder", "pointer"])
@pytest.mark.parametrize("linked", ["profile", "downloads"])
def test_setup_refuses_a_profile_or_download_folder_linked_into_a_git_tree(env, linked, git):
    """A browser's profile or download folder that is a link into a git work tree (a .git folder, or a .git pointer
    file) is refused by setup too, naming it, before setup makes or narrows any folder."""
    repo = env.git_work_tree(git)
    (repo / "target").mkdir()
    mode = (repo / "target").stat().st_mode
    link = env.profile("work") if linked == "profile" else env.root / "state" / "downloads" / "work"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(repo / "target")
    code, out = env.run("setup")
    assert code == 2 and str(link) in out["message"] and "inside a git working tree" in out["message"], out
    assert (repo / "target").stat().st_mode == mode
