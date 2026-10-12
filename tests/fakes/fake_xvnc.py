"""Fake Xvnc: accepts connections on its -rfbport at its -interface address until SIGTERM. With -displayfd it writes
its display number there once it listens, as Xvnc does once it owns the display; when the port is taken it exits 1
without writing, as Xvnc does when another X server holds the display.

SIGTERM is handled from the first statement, so whoever sees the report can stop it at once. The main thread waits in
short sleeps, not in signal.pause(): Python runs a signal handler only in the main thread, and pause() never returns for
a signal that was taken just before it was entered, or by the accept thread. The handler ends the process with
os._exit: raising SystemExit from a handler can land inside library code (Thread.start() waiting on its Event)."""

from __future__ import annotations

import os
import signal
import socket
import sys
import threading
import time

import fakesys


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))  # first: a SIGTERM landing anywhere ends it with exit 0
    fakesys.log_call("Xvnc", argv)
    if fakesys.settings().get("xvnc_fails"):
        print("fake Xvnc: cannot open the display", file=sys.stderr)
        return 1
    port = int(argv[argv.index("-rfbport") + 1])
    host = argv[argv.index("-interface") + 1]
    server = socket.socket()
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        server.bind((host, port))
        server.listen(16)
    except OSError as exc:  # another program holds the port: the display is not ours
        print(f"fake Xvnc: cannot listen on {host}:{port}: {exc}", file=sys.stderr)
        return 1
    if "-displayfd" in argv:  # ready: report the display, as Xvnc does once it owns it
        ready = int(argv[argv.index("-displayfd") + 1])
        os.write(ready, (argv[0].lstrip(":") + "\n").encode())
        os.close(ready)

    def serve() -> None:
        while True:
            conn, _ = server.accept()
            conn.close()

    threading.Thread(target=serve, daemon=True).start()
    while True:
        time.sleep(0.05)  # the main thread comes back to Python, where a pending handler runs, within 50 ms


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
