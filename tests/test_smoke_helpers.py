"""The hand-run smoke test's own helpers, checked here without a real browser (the smoke test itself is never
collected)."""

from __future__ import annotations

import ast
import os

import smoke_real_browser as smoke

from orion_browser.runner import VIEW_GUARD_FOLDER


def test_drop_daemon_takes_a_pid_too_big_for_a_native_pidfd_as_no_daemon(tmp_path, monkeypatch):
    """A pid file holding a number past what pidfd_open takes: a Python with a native os.pidfd_open (the system's 3.12
    has one; the venv's 3.11 does not) raises OverflowError for any pid beyond a C int. drop_daemon takes it as no
    daemon to stop, as it does a pid file it cannot read."""
    pid_file = tmp_path / "smoke.pid"
    pid_file.write_text(str(2**63))
    real_path = smoke.Path
    monkeypatch.setattr(smoke, "Path", lambda path: pid_file if str(path).endswith("/smoke.pid") else real_path(path))

    def native_pidfd_open(pid: int, flags: int = 0) -> int:
        if pid > 2**31 - 1:
            raise OverflowError("signed integer is greater than maximum")
        raise AssertionError(f"drop_daemon opened pid {pid}, not the one in its pid file")

    monkeypatch.setattr(os, "pidfd_open", native_pidfd_open, raising=False)
    smoke.drop_daemon("smoke")  # returns: nothing to stop


def test_the_smoke_run_asks_for_the_view_as_every_frame_the_guard_refuses():
    """The smoke run asks the real view for its page as each kind of load into a frame, so it checks every
    Sec-Fetch-Dest the view guard refuses against the real websockify. The guard's list is read from its source: the
    guard imports websockify, which the test's own Python may not have."""
    tree = ast.parse((VIEW_GUARD_FOLDER / "orion_browser_view_guard.py").read_text())
    (refused,) = [
        ast.literal_eval(node.value.args[0]) for node in tree.body
        if isinstance(node, ast.Assign) and [getattr(target, "id", None) for target in node.targets] == ["FRAMES"]
    ]
    assert sorted(smoke.FRAMES) == sorted(refused)


def test_skipped_closes_reads_every_close_skipped_record_of_the_smoke_browser(tmp_path):
    """The smoke run checks that no stop of its own real session skipped the DevTools close (a stop does when it cannot
    show the endpoint is the session's): every close-skipped record in the smoke browser's records, from every file."""
    audit = tmp_path / "state" / "audit" / smoke.BROWSER
    audit.mkdir(parents=True)
    (audit / "2026-10-10.jsonl").write_text('{"event": "lease"}\n{"event": "close-skipped", "by": "tool"}\n')
    (audit / "2026-10-11.jsonl").write_text('{"event": "release"}\n{"event": "close-skipped", "by": "service"}\n')
    assert [r["by"] for r in smoke.skipped_closes(tmp_path / "state")] == ["tool", "service"]
    assert smoke.skipped_closes(tmp_path / "elsewhere") == []
