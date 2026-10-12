"""The fakes behave like the real tools in the ways orion-browser relies on."""

from __future__ import annotations

import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

import conftest
import pytest
from conftest import FAKES, LOOPBACK
import fakesys  # noqa: E402  (conftest has put the fakes' folder on sys.path)

from orion_browser import devtools, procs


def _systemctl(env, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], env=env.env, capture_output=True, text=True)


def _port_file(path: Path):
    """The port file, or None until there is a complete one. A DevToolsError counts as "not yet", as it does for every
    production caller: Chrome creates the file before it writes it."""
    try:
        return devtools.read_port_file(path)
    except devtools.DevToolsError:
        return None


def test_fake_service_manager_starts_and_stops_a_unit(env):
    started = subprocess.run(
        ["systemd-run", "--user", "--unit=t1.service", "--collect", "--property=TimeoutStopSec=5", "--", "/bin/sleep", "30"],
        env=env.env,
    )
    assert started.returncode == 0
    assert _systemctl(env, "is-active", "t1.service").stdout.strip() == "active"
    assert _systemctl(env, "stop", "t1.service").returncode == 0
    assert _systemctl(env, "is-active", "t1.service").stdout.strip() == "inactive"


# A unit that models Chrome under a session runner. Its main process ignores SIGTERM, so only the kill after the stop
# timeout can end it; a member of its group records any SIGTERM it is sent.
UNIT_MAIN = """\
import signal, subprocess, sys
signal.signal(signal.SIGTERM, signal.SIG_IGN)
subprocess.run([sys.executable, "-c", sys.argv[1]])
"""
GROUP_MEMBER = """\
import os, signal, time
folder = os.environ["FAKE_DIR"]
def note(*_):
    open(folder + "/member-term", "w").write("TERM")
signal.signal(signal.SIGTERM, note)
open(folder + "/member-ready", "w").write("up")
time.sleep(30)
"""


def _group_members(group: int) -> list[int]:
    """The live (not zombie) processes of a process group."""
    members = []
    for entry in os.listdir("/proc"):
        try:
            fields = Path(f"/proc/{entry}/stat").read_bytes().rsplit(b")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if entry.isdigit() and fields[0] != b"Z" and int(fields[2]) == group:
            members.append(int(entry))
    return members


def test_a_stop_signals_the_main_process_then_kills_the_group(env):
    """KillMode=mixed, both halves. The stop sends SIGTERM to the unit's main process alone, so that a runner can close
    Chrome through DevTools before anything else ends it. Once TimeoutStopSec is over it SIGKILLs what is left of the
    whole group."""
    subprocess.run(
        ["systemd-run", "--user", "--unit=t2.service", "--property=TimeoutStopSec=1", "--",
         sys.executable, "-c", UNIT_MAIN, GROUP_MEMBER],
        env=env.env, check=True,
    )
    fake = env.root / "fake"
    env.wait_until((fake / "member-ready").exists, timeout=5)
    group = json.loads((fake / "units.json").read_text())["t2.service"]["pid"]  # the main process leads its group
    assert len(_group_members(group)) == 2  # it and the member
    began = time.monotonic()
    assert _systemctl(env, "stop", "t2.service").returncode == 0
    assert 0.9 < time.monotonic() - began < 5  # the main process ignored SIGTERM: only the kill ended it
    assert not (fake / "member-term").exists()  # SIGTERM went to the main process alone
    assert env.wait_until(lambda: not _group_members(group), timeout=3)  # and the kill took the member with it
    assert _systemctl(env, "is-active", "t2.service").stdout.strip() == "inactive"


def test_fake_chrome_answers_devtools_and_saves_cookies_only_on_close(env, monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", LOOPBACK)
    for ending, cookies in (("close", True), ("sigterm", False)):
        profile = env.root / f"profile-{ending}"
        chrome = subprocess.Popen(
            [sys.executable, str(FAKES / "fake_chrome.py"), f"--user-data-dir={profile}"],
            env={**env.env, "DISPLAY": ":97"},
        )
        port_file = env.wait_until(lambda: _port_file(profile / "DevToolsActivePort"))
        assert devtools.version(port_file.port)["webSocketDebuggerUrl"] == port_file.endpoint
        if ending == "close":
            devtools.close_browser(port_file.endpoint)
        else:
            os.kill(chrome.pid, signal.SIGTERM)
        assert chrome.wait(timeout=10) == 0
        assert (profile / "Cookies.fake").exists() is cookies


def test_fake_chrome_with_a_close_delay_exits_that_long_after_it_takes_the_close(env, monkeypatch):
    """close_delay: Chrome takes Browser.close (and saves its cookies) at once, then runs on that many seconds before it
    exits. The R1 pin in test_s4_stops relies on that gap; without it the pin catches its bug only when the CLI wins a
    race."""
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", LOOPBACK)
    profile = env.root / "profile-delay"
    env.set_chrome(profile.name, close_delay=2)
    chrome = subprocess.Popen(
        [sys.executable, str(FAKES / "fake_chrome.py"), f"--user-data-dir={profile}"],
        env={**env.env, "DISPLAY": ":97"},
    )
    port_file = env.wait_until(lambda: _port_file(profile / "DevToolsActivePort"))
    asked = time.monotonic()
    devtools.close_browser(port_file.endpoint)
    assert (profile / "Cookies.fake").exists()
    assert chrome.wait(timeout=10) == 0
    assert time.monotonic() - asked >= 2


def test_a_lease_file_a_test_writes_is_owner_only_as_the_stores_are(env):
    """write_lease and age_lease write the lease file as the store does: whole, renamed into place, and owner-only."""
    env.write_lease("work", holder="a")
    assert stat.S_IMODE(env.lease_file("work").stat().st_mode) == 0o600
    env.age_lease("work", 60)
    assert stat.S_IMODE(env.lease_file("work").stat().st_mode) == 0o600


def test_fake_chrome_writes_its_port_file_by_replacing_it(env):
    """A reader polling the profile must never see the port file created but empty or half written, so the fake writes a
    temporary file and renames it over the port file: a stale file is replaced by a new one, never rewritten."""
    profile = env.root / "profile-atomic"
    profile.mkdir()
    port_file = profile / "DevToolsActivePort"
    port_file.write_text("stale\n")
    stale = port_file.stat().st_ino
    chrome = subprocess.Popen(
        [sys.executable, str(FAKES / "fake_chrome.py"), f"--user-data-dir={profile}"],
        env={**env.env, "DISPLAY": ":97"},
    )
    try:
        env.wait_until(lambda: _port_file(port_file))
        assert port_file.stat().st_ino != stale
        assert [path.name for path in profile.glob("DevToolsActivePort*")] == ["DevToolsActivePort"]  # no temporary
    finally:
        chrome.kill()
        chrome.wait()


# Processes that hold a profile. A test starts its own, with no FAKE_DIR in their environment (so the teardown never
# touches them: it must not reach a browser the operator is running), and reaps them itself in a finally.

def _holders(folder: Path) -> list[int]:
    return [process["pid"] for process in procs.holding_profile(folder)]


def _seen(env, folder: Path) -> list[int]:
    """The pids holding the folder, once there are any: /proc/<pid>/cmdline can read empty for an instant after exec."""
    return env.wait_until(lambda: _holders(folder), timeout=5)


def _hold(profile: Path, one_string: bool = False, tail: str = "", flag: str = "--user-data-dir") -> subprocess.Popen:
    """A process that holds the profile. By default its command line is NUL-separated, as an ordinary program's is. With
    one_string it is ONE argument, as real Chrome leaves it once it sets its process title (Python then waits on its
    stdin); `tail` follows the profile on that command line. Chrome reads the flag with one dash or two."""
    if one_string:
        return subprocess.Popen(
            [f"/opt/google/chrome/chrome --type=renderer {flag}={profile}{tail}"],
            executable=sys.executable, stdin=subprocess.PIPE,
        )
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"{flag}={profile}"])


def _reap(*holders: subprocess.Popen) -> None:
    for holder in holders:
        holder.kill()
        holder.wait()
        if holder.stdin:
            holder.stdin.close()


def _environ(pid: int) -> list[bytes]:
    try:
        return Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
    except OSError:
        return []


def _cmdline(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return b""


def test_holding_profile_resolves_links_and_relative_paths(env):
    """Chrome may be handed its profile through a link or a relative path; it still holds the same folder."""
    linked, relative = env.root / "held" / "linked", env.root / "held" / "relative"
    for folder in (linked, relative):
        folder.mkdir(parents=True)
    (env.root / "link").symlink_to(linked)
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    holders = {
        linked: subprocess.Popen([*sleeper, f"--user-data-dir={env.root / 'link'}"]),
        relative: subprocess.Popen([*sleeper, "--user-data-dir=held/../held/relative"], cwd=env.root),
    }
    try:
        for folder, holder in holders.items():
            assert _seen(env, folder) == [holder.pid]
        assert _holders(env.root / "held" / "other") == []
    finally:
        _reap(*holders.values())


def test_holding_profile_names_the_profile_it_is_asked_about_through_a_link(env):
    real = env.root / "held" / "real"
    real.mkdir(parents=True)
    (env.root / "alias").symlink_to(real)
    holder = _hold(real)
    try:
        assert _seen(env, env.root / "alias") == [holder.pid]
    finally:
        _reap(holder)


@pytest.mark.parametrize(("name", "tail"), [
    pytest.param("work", " --lang=en", id="another flag follows"),
    pytest.param("work", " https://example.com", id="a URL follows"),
    pytest.param("work", " https://example.com --lang=en", id="a URL and a flag follow"),
    pytest.param("my profile", " --lang=en", id="the folder name has a space"),
    pytest.param("My Profiles/work", " https://example.com", id="a folder above has a space, a URL follows"),
    pytest.param("My Profiles/work", " https://example.com https://example.org", id="a folder with a space, two URLs"),
    pytest.param("Work - Backup/work", " --lang=en", id="the path has a space, a dash and a space"),
])
def test_holding_profile_sees_chromes_one_string_command_line(env, name, tail):
    """Real Chrome rewrites its process title, so /proc shows its whole command line as ONE argument. It holds the
    profile all the same."""
    profile = env.root / "profiles" / name
    profile.mkdir(parents=True)
    holder = _hold(profile, one_string=True, tail=tail)
    try:
        assert _seen(env, profile) == [holder.pid]
        assert _holders(env.root / "profiles" / "other") == []
    finally:
        _reap(holder)


@pytest.mark.parametrize("one_string", [False, True], ids=["separate arguments", "one string"])
def test_a_holder_of_dan2_does_not_hold_dan(env, one_string):
    """Holding is the same folder, never a folder whose name merely begins with this one's."""
    work, dan2 = env.root / "profiles" / "work", env.root / "profiles" / "dan2"
    for folder in (work, dan2):
        folder.mkdir(parents=True)
    holder = _hold(dan2, one_string)
    try:
        assert _seen(env, dan2) == [holder.pid]  # seen first, so that the next line cannot pass by not seeing it
        assert _holders(work) == []
    finally:
        _reap(holder)


def test_a_command_line_that_only_names_the_profile_in_another_flag_does_not_hold_it(env):
    profile, other = env.root / "profiles" / "work", env.root / "profiles" / "other"
    for folder in (profile, other):
        folder.mkdir(parents=True)
    holder = _hold(other, one_string=True, tail=f" --crash-dumps-dir={profile}/crashes --lang=en")
    try:
        assert _seen(env, other) == [holder.pid]
        assert _holders(profile) == []
    finally:
        _reap(holder)


def test_a_holder_of_a_folder_named_dan_extra_does_not_hold_dan(env):
    """An ordinary command line is read exactly: the whole value of the flag. A folder whose name goes on after a space
    is another folder."""
    work, sibling = env.root / "profiles" / "work", env.root / "profiles" / "work extra"
    for folder in (work, sibling):
        folder.mkdir(parents=True)
    holder = _hold(sibling)
    try:
        assert _seen(env, sibling) == [holder.pid]
        assert _holders(work) == []
    finally:
        _reap(holder)


@pytest.mark.parametrize("tail", [" --lang=en", ""], ids=["a flag follows", "the script ends there"])
def test_a_shell_whose_script_names_the_profile_does_not_hold_it(env, tail):
    """Holding is having the folder open as a process's own --user-data-dir, not running a script that says so."""
    profile = env.root / "profiles" / "work"
    profile.mkdir(parents=True)
    script = f"read line; : chrome --user-data-dir={profile}{tail}"
    shell = subprocess.Popen(["/bin/sh", "-c", script], stdin=subprocess.PIPE)
    try:
        env.wait_until(lambda: script.encode() in _cmdline(shell.pid), timeout=5)  # started: not vacuous below
        assert _holders(profile) == []
    finally:
        _reap(shell)


def test_a_command_line_of_two_arguments_is_not_read_as_one_string(env):
    """Only a command line that is ONE argument, as Chrome leaves its own, is cut apart at its flags."""
    profile = env.root / "profiles" / "work"
    profile.mkdir(parents=True)
    holder = subprocess.Popen(
        [f"/opt/google/chrome/chrome --type=renderer --user-data-dir={profile} --lang=en", "-"],
        executable=sys.executable, stdin=subprocess.PIPE,
    )
    try:
        env.wait_until(lambda: b"--lang=en\0-\0" in _cmdline(holder.pid), timeout=5)  # started, as above
        assert _holders(profile) == []
    finally:
        _reap(holder)


@pytest.mark.parametrize(("one_string", "tail"), [
    pytest.param(False, "", id="separate arguments"),
    pytest.param(True, " --lang=en", id="one string, a flag follows"),
    pytest.param(True, " https://example.com", id="one string, a URL follows"),
])
def test_holding_profile_reads_the_flag_with_one_dash_too(env, one_string, tail):
    """Chrome accepts -user-data-dir= as well as --user-data-dir=."""
    profile = env.root / "profiles" / "work"
    profile.mkdir(parents=True)
    holder = _hold(profile, one_string, tail, flag="-user-data-dir")
    try:
        assert _seen(env, profile) == [holder.pid]
    finally:
        _reap(holder)


def _comm(pid: int) -> bytes:
    try:
        return Path(f"/proc/{pid}/comm").read_bytes()
    except OSError:
        return b""


def test_a_process_name_that_is_not_utf8_breaks_neither_holding_profile_nor_the_liveness_check(env):
    """prctl(PR_SET_NAME) can give a process any bytes as its name, and /proc/<pid>/stat holds them raw. Reading that
    file as text would raise for every caller while such a process exists."""
    profile = env.root / "profiles" / "work"
    profile.mkdir(parents=True)
    code = "import ctypes, time; ctypes.CDLL(None).prctl(15, bytes([255, 254, 253]) + b'odd', 0, 0, 0); time.sleep(60)"
    holder = subprocess.Popen([sys.executable, "-c", code, f"--user-data-dir={profile}"])
    try:
        env.wait_until(lambda: _comm(holder.pid).startswith(b"\xff\xfe\xfd"), timeout=5)  # named: not vacuous below
        assert _seen(env, profile) == [holder.pid]
        assert _holders(env.root / "profiles" / "other") == []
        assert fakesys.alive(holder.pid) is True
    finally:
        _reap(holder)
    assert fakesys.alive(holder.pid) is False


IDLE = [sys.executable, "-c", "import time; time.sleep(60)"]
# Forks 150 sleeping processes and exits: their pids are lower than any process started after it.
BALLAST = "import os, time\nfor _ in range(150):\n    if os.fork() == 0:\n        time.sleep(60)\n        os._exit(0)\n"
# Forks a sleeping child every millisecond, for at most half a second.
SPAWNER = ("import os, time\nfor _ in range(500):\n    if os.fork() == 0:\n        time.sleep(60)\n"
           "        os._exit(0)\n    time.sleep(0.001)\n")


def _marker(env) -> bytes:
    return f"FAKE_DIR={env.root / 'fake'}".encode()


def _carriers(marker: bytes) -> list[int]:
    """The live processes (a zombie has no environment left to read) that carry the marker."""
    return [int(entry) for entry in os.listdir("/proc")
            if entry.isdigit() and int(entry) != os.getpid() and marker in _environ(int(entry))]


@pytest.mark.parametrize("text", ["", '{"t.service": {"pid": 12', "[]", '{"t.service": 5}'],
                         ids=["empty", "cut off mid-write", "not an object", "an entry that is not an object"])
def test_teardown_goes_on_when_the_units_file_cannot_be_read(env, text):
    """The fake service manager rewrites units.json in place on every call, so a teardown can catch it half written.
    That must not stop the sweep, which finds the same processes by their FAKE_DIR."""
    (env.root / "fake" / "units.json").write_text(text)
    sleeper = subprocess.Popen(IDLE, env=env.env)
    try:
        env.wait_until(lambda: _marker(env) in _environ(sleeper.pid), timeout=5)
        env.stop_everything()
        assert sleeper.wait(timeout=5) == -signal.SIGKILL
    finally:
        _reap(sleeper)


def test_teardown_signals_a_recorded_group_only_when_its_leader_carries_this_tests_fake_dir(env, monkeypatch):
    """units.json is a record, not proof: a pid in it may be stale, or junk. Only a leader that is ours is signalled."""
    unit = subprocess.Popen(IDLE, env=env.env, start_new_session=True)  # a unit's main process: its own group
    operators = subprocess.Popen(IDLE, start_new_session=True)  # not ours, as the operator's browser would not be
    recorded = {"unit": unit.pid, "operators": operators.pid, "zero": 0, "init": 1, "negative": -5, "text": "7",
                "flag": True}
    (env.root / "fake" / "units.json").write_text(json.dumps({name: {"pid": pid} for name, pid in recorded.items()}))
    signalled = []
    try:
        env.wait_until(lambda: _marker(env) in _environ(unit.pid), timeout=5)
        with monkeypatch.context() as patched:
            patched.setattr(os, "killpg", lambda pid, sig: signalled.append(pid))
            env.stop_everything()
        assert signalled == [unit.pid]
        assert operators.poll() is None
    finally:
        (env.root / "fake" / "units.json").write_text("{}")  # the junk must not reach the fixture's own teardown
        _reap(unit, operators)


def test_teardown_sweeps_again_for_processes_forked_while_it_runs(env):
    """One pass over /proc misses a process that is forked while it runs: a CLI in the middle of a start, a runner
    starting its screen. Here a spawner keeps forking behind 150 processes that a single pass kills first."""
    marker = _marker(env)
    subprocess.run([sys.executable, "-c", BALLAST], env=env.env, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)
    spawner = subprocess.Popen([sys.executable, "-c", SPAWNER], env=env.env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        env.wait_until(lambda: len(_carriers(marker)) > 160, timeout=5)  # ballast, spawner, and its first children
        env.stop_everything()
        assert env.wait_until(lambda: not _carriers(marker), timeout=5)
    finally:
        _reap(spawner)


def test_teardown_gives_up_after_a_few_passes_when_a_process_cannot_be_killed(env, monkeypatch):
    assert 3 <= conftest.SWEEP_PASSES <= 20
    sleeper = subprocess.Popen(IDLE, env=env.env)
    kills = []

    def cannot_kill(pid, sig):
        kills.append(pid)
        if len(kills) > 100:
            raise RuntimeError("the sweep never stops")

    try:
        env.wait_until(lambda: _marker(env) in _environ(sleeper.pid), timeout=5)
        began = time.monotonic()
        with monkeypatch.context() as patched:
            patched.setattr(os, "kill", cannot_kill)
            env.stop_everything()
        assert kills == [sleeper.pid] * conftest.SWEEP_PASSES
        assert time.monotonic() - began < 3
    finally:
        _reap(sleeper)


def test_teardown_kills_only_processes_that_carry_this_tests_fake_dir(env):
    """A matching bug must never reach a browser the operator is running: a holder of a test profile is killed at
    teardown only when it also carries this test's FAKE_DIR."""
    profile = env.profile("anon")
    profile.mkdir(parents=True)
    marker = f"FAKE_DIR={env.root / 'fake'}".encode()
    another_test = {**env.env, "FAKE_DIR": f"{env.root / 'fake'}2"}  # begins like this test's, is not the same
    idle = [sys.executable, "-c", "import time; time.sleep(60)"]
    holding = [*idle, f"--user-data-dir={profile}"]
    ours_idle = subprocess.Popen(idle, env=env.env)  # carries FAKE_DIR, holds nothing: killed
    ours_holder = subprocess.Popen(holding, env=env.env)  # carries FAKE_DIR, holds the profile: killed
    operators = subprocess.Popen(holding)  # no FAKE_DIR, holds the profile, as the operator's own browser would: spared
    others = subprocess.Popen(holding, env=another_test)  # another test's FAKE_DIR, holds the profile: spared
    try:
        # Each must be readable in /proc before the teardown runs, or sparing it would prove nothing.
        holders = sorted([ours_holder.pid, operators.pid, others.pid])
        env.wait_until(lambda: sorted(_holders(profile)) == holders, timeout=5)
        env.wait_until(lambda: all(marker in _environ(p.pid) for p in (ours_idle, ours_holder)), timeout=5)
        env.stop_everything()
        assert [ours_idle.wait(timeout=5), ours_holder.wait(timeout=5)] == [-signal.SIGKILL] * 2
        assert operators.poll() is None and others.poll() is None
    finally:
        _reap(ours_idle, ours_holder, operators, others)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def test_listens_names_only_the_process_holding_the_listening_socket(env):
    """A socket listening on the address and port, held by this process: another process does not hold it, another
    address or port is not it, and a socket at that address and port that is not listening (one only bound there, or
    a connection accepted there once the listener has closed) does not count."""
    port, bound_port = _free_port(), _free_port()
    server = socket.create_server((LOOPBACK, port))
    bound = socket.socket()
    bound.bind((LOOPBACK, bound_port))
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env.env)
    try:
        assert procs.listens(os.getpid(), LOOPBACK, port)
        assert not procs.listens(other.pid, LOOPBACK, port)
        assert not procs.listens(os.getpid(), "127.0.0.1", port)
        assert not procs.listens(os.getpid(), LOOPBACK, bound_port)
        server.close()
        assert not procs.listens(os.getpid(), LOOPBACK, port)
    finally:
        server.close()
        bound.close()
        other.kill()
        other.wait()
    assert not procs.listens(other.pid, LOOPBACK, port)  # a process that is gone holds nothing
    listener = socket.create_server((LOOPBACK, 0))
    accepted_port = listener.getsockname()[1]
    client = socket.create_connection((LOOPBACK, accepted_port))
    accepted, _ = listener.accept()
    listener.close()
    try:
        assert not procs.listens(os.getpid(), LOOPBACK, accepted_port)  # only an established socket there now
    finally:
        accepted.close()
        client.close()


# A listener (argv: port, address) that prints "ready" once it listens, and a parent that only starts one (argv: port,
# address, the listener's program), so the listener is the parent's child and the test's grandchild.
LISTENER = (
    "import socket, sys, time\n"
    "s = socket.create_server((sys.argv[2], int(sys.argv[1])))\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)
STARTS_A_LISTENER = (
    "import subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', *sys.argv[3:], *sys.argv[1:3]])\n"
    "time.sleep(60)\n"
)


def test_tree_listens_finds_a_listener_among_a_processs_descendants_only(env):
    """tree_listens looks at the process and every process descended from it: a grandchild's listening socket counts
    for its grandparent, a sibling tree's does not, and a root of 0 (no main process) holds nothing."""
    port = _free_port()
    parent = subprocess.Popen(
        [sys.executable, "-c", STARTS_A_LISTENER, str(port), LOOPBACK, LISTENER],
        env=env.env, stdout=subprocess.PIPE, text=True,
    )
    other = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env.env)
    try:
        assert parent.stdout.readline().strip() == "ready"
        assert procs.tree_listens(parent.pid, LOOPBACK, port)
        assert not procs.listens(parent.pid, LOOPBACK, port)  # the parent itself holds no such socket
        assert procs.tree_listens(os.getpid(), LOOPBACK, port)  # the test is an ancestor too
        assert not procs.tree_listens(other.pid, LOOPBACK, port)
        assert not procs.tree_listens(parent.pid, LOOPBACK, _free_port())
        assert not procs.tree_listens(0, LOOPBACK, port)
    finally:
        for process in (other, parent):  # the listener carries this test's FAKE_DIR: the teardown ends it
            process.kill()
            process.wait()


def _xvnc(env, port: int) -> tuple[subprocess.Popen, int]:
    """A fake Xvnc for display :90 on `port`, with a -displayfd pipe: the process and the read end of the pipe."""
    read_end, write_end = os.pipe()
    proc = subprocess.Popen(
        [sys.executable, str(FAKES / "fake_xvnc.py"), ":90", "-rfbport", str(port), "-interface", LOOPBACK,
         "-displayfd", str(write_end)],
        env=env.env, pass_fds=(write_end,), stderr=subprocess.DEVNULL,
    )
    os.close(write_end)
    return proc, read_end


def _everything_written(read_end: int) -> bytes:
    """All that was written to the pipe, read until it closed (it closes when its writer closes it or exits)."""
    try:
        data = b""
        while True:
            ready, _, _ = select.select([read_end], [], [], 10)
            assert ready, "the pipe neither carried data nor closed"
            chunk = os.read(read_end, 100)
            if not chunk:
                return data
            data += chunk
    finally:
        os.close(read_end)


def test_fake_xvnc_reports_its_display_number_once_it_listens(env):
    port = _free_port()
    xvnc, read_end = _xvnc(env, port)
    try:
        assert _everything_written(read_end) == b"90\n"  # the number and a newline, then the pipe is closed
        socket.create_connection((LOOPBACK, port), timeout=5).close()  # it was listening when it reported
    finally:
        xvnc.kill()
        xvnc.wait()


def test_fake_xvnc_stops_on_a_sigterm_sent_the_moment_it_reports(env):
    """It handles SIGTERM from before it reports ready, and waits in a way that sees a signal landing anywhere: exit 0
    within 3 s, every time. A light check (a lost signal was about 1 start in 400), not a proof."""
    for _ in range(20):
        xvnc, read_end = _xvnc(env, _free_port())
        try:
            assert _everything_written(read_end) == b"90\n"
            xvnc.terminate()
            assert xvnc.wait(timeout=3) == 0
        finally:
            xvnc.kill()
            xvnc.wait()


def test_fake_xvnc_exits_1_without_reporting_when_its_port_is_taken(env):
    port = _free_port()
    first, first_end = _xvnc(env, port)
    try:
        assert _everything_written(first_end) == b"90\n"
        second, second_end = _xvnc(env, port)  # a second screen on the same port: the display is not its own
        assert second.wait(timeout=10) == 1
        assert _everything_written(second_end) == b""
        assert first.poll() is None
    finally:
        first.kill()
        first.wait()
