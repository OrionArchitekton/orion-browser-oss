"""Which processes have a profile open, and whether a process, or a process and its descendants, listen on an
address and port.

Chrome passes the profile as --user-data-dir to every process it starts, then rewrites its process title:
/proc/<pid>/cmdline of a Chrome process is ONE argument holding the whole command line, flags and all, joined by spaces.
Other programs' command lines are NUL-separated, as usual."""

from __future__ import annotations

import os
import re
import socket
import struct
from pathlib import Path
from typing import Iterator


FLAGS = (b"--user-data-dir=", b"-user-data-dir=")  # Chrome reads the flag with two dashes or one
# Where one flag ends and the next begins, on a command line joined by spaces. It bounds a value; trying every cut at a
# space would reach the same answer from the whole rest of the line, with a realpath for each of a long line's flags.
_BOUNDARY = re.compile(rb" (?=--)")
_FLAG_IN_ONE_STRING = re.compile(rb"(?:^| )--?user-data-dir=")
_LISTEN = "0A"  # a socket's state in /proc/net/tcp while it listens (TCP_LISTEN)


def holding_profile(profile: Path) -> list[dict[str, object]]:
    """Live processes whose --user-data-dir is this profile, links resolved on both sides (a relative one is read from
    the process's working folder, as Chrome reads it): pid and command (first 200 characters). It is the same folder,
    never one whose path merely begins with this one's."""
    want = os.path.realpath(profile)
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            argv = Path(f"/proc/{entry}/cmdline").read_bytes().split(b"\0")
            stat = Path(f"/proc/{entry}/stat").read_bytes()  # bytes: the process name in it can be any bytes
        except OSError:  # gone, or not readable
            continue
        if stat.rsplit(b")", 1)[-1].split()[:1] == [b"Z"]:
            continue
        if not any(_names(entry, value, want) for value in _values(argv)):
            continue
        command = b" ".join(arg for arg in argv if arg).decode(errors="replace")[:200]
        found.append({"pid": int(entry), "command": command})
    return sorted(found, key=lambda process: process["pid"])


def _values(argv: list[bytes]) -> Iterator[bytes]:
    """What a command line could give --user-data-dir.

    An ordinary command line is NUL-separated, and is read exactly: an argument that starts with the flag, and its whole
    value, nothing else. So a shell whose script mentions the flag holds nothing, and a process that holds a folder
    whose name merely continues this one's (after a space) does not hold this one.

    A command line that is ONE argument is Chrome's rewritten form, where the boundaries between arguments are lost, so
    only there is it read loosely: the value runs to the next " --" boundary, and every cut of it at a space is tried as
    well as the whole (a positional argument, a URL, after a value joins it, and a folder name can hold a space of its
    own: "My Profiles/work https://example.com"). That is ambiguous, and harmless: a folder named "work extra" reads as
    "work" there, but orion-browser's own profile names (NAME_RE, [a-z0-9]{1,16}) never hold a space, so a sibling
    named with one is not one of ours, and the error is toward refusing a start. A single dash is not a boundary: it
    can be part of a path ("Work - Backup")."""
    args = [arg for arg in argv if arg]
    if len(args) != 1:
        for arg in args:
            for flag in FLAGS:
                if arg.startswith(flag):
                    yield arg[len(flag):]
        return
    line = args[0]
    for match in _FLAG_IN_ONE_STRING.finditer(line):
        boundary = _BOUNDARY.search(line, match.end())
        value = line[match.end():boundary.start() if boundary else len(line)]
        yield value
        for cut, byte in enumerate(value):
            if byte == 0x20 and cut:
                yield value[:cut]


def _names(pid: str, raw: bytes, want: str) -> bool:
    """Whether a process's --user-data-dir value is the folder `want` (already resolved)."""
    path = os.fsdecode(raw)
    try:
        if not os.path.isabs(path):
            path = os.path.join(os.readlink(f"/proc/{pid}/cwd"), path)
        return os.path.realpath(path) == want
    except OSError:  # the process is gone, or its working folder cannot be read
        return False


def listens(pid: int, host: str, port: int) -> bool:
    """Whether the process itself holds a TCP socket listening on host:port (an IPv4 address). An answer on the port
    does not show that: another program can listen there. /proc/net/tcp names each listening socket at that address
    and port by its inode, and the process holds one when one of its descriptors links to it. False when the process is
    gone or its descriptors cannot be read."""
    sockets = _listening(host, port)
    return bool(sockets) and _holds(pid, sockets)


def tree_listens(root: int, host: str, port: int) -> bool:
    """Whether the process `root`, or any process descended from it, holds a TCP socket listening on host:port (as
    listens). A session's service is its main process and what that started: the screen, the view, Chrome and Chrome's
    own processes. False for a root of 0 or less (no process), or when nothing in the tree listens there."""
    sockets = _listening(host, port)
    return root > 0 and bool(sockets) and any(_holds(pid, sockets) for pid in _tree(root))


def _listening(host: str, port: int) -> set[str]:
    """The sockets listening on host:port, as /proc/<pid>/fd links name them (socket:[inode])."""
    # The kernel prints the address as the 32-bit number its four bytes make in this machine's byte order.
    local = "%08X:%04X" % (struct.unpack("=I", socket.inet_aton(host))[0], port)
    try:
        rows = Path("/proc/net/tcp").read_text().splitlines()[1:]
    except OSError:
        return set()
    sockets = set()
    for row in rows:
        fields = row.split()  # sl, local and remote address, state, queues, timer, retransmits, uid, timeout, inode
        if len(fields) > 9 and fields[1] == local and fields[3] == _LISTEN:
            sockets.add(f"socket:[{fields[9]}]")
    return sockets


def _holds(pid: int, sockets: set[str]) -> bool:
    """Whether one of the process's descriptors is one of the sockets. False when its descriptors cannot be read."""
    try:
        descriptors = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for fd in descriptors:
        try:
            if os.readlink(f"/proc/{pid}/fd/{fd}") in sockets:
                return True
        except OSError:  # closed meanwhile
            continue
    return False


def _tree(root: int) -> list[int]:
    """The process and every live process descended from it, from each process's parent in /proc/<pid>/stat."""
    children: dict[int, list[int]] = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            fields = Path(f"/proc/{entry}/stat").read_bytes().rsplit(b")", 1)[1].split()  # state, ppid, ...
        except (OSError, IndexError):  # gone, or not readable
            continue
        if len(fields) > 1 and fields[0] != b"Z":
            children.setdefault(int(fields[1]), []).append(int(entry))
    found, waiting = {root}, [root]
    while waiting:
        for child in children.get(waiting.pop(), ()):
            if child not in found:
                found.add(child)
                waiting.append(child)
    return sorted(found)
