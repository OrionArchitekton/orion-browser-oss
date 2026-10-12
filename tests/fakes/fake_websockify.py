"""Fake websockify: `[--auth-plugin CLASS [--auth-source SOURCE]] [--web-auth] --web DIR LISTEN TARGET`; serves a page
at LISTEN until SIGTERM. SIGTERM is handled from the first statement, before the port is open, so whoever can connect
can also stop it; serve_forever waits in timed polls, so the main thread comes back to Python to run the handler. The
handler ends the process with os._exit: raising SystemExit from a handler can land inside library code (Thread.start()
waiting on its Event) and corrupt it, and exiting through interpreter shutdown with request threads still running can
abort it (seen in about 1 start in 1,000).

A WebSocket request (an Upgrade header of websocket) is answered 101 and then held until the client closes it, as a
viewer's connection is; any other request gets the page. The auth plugin is loaded and asked as websockify 0.10.0 does
(websocketproxy.py and websockifyserver.py there): loaded before it listens, a name without a dot being one of
websockify.auth_plugins', called with --auth-source, and a plugin that cannot be loaded ends the start; asked about
every WebSocket request, and, with --web-auth, about every page load too. A refusal (its AuthenticationError) is
answered with the error's code, its message as the reason, a Content-Type and the error's headers, no body, and the
connection ends. Under the fakes, websockify.auth_plugins is the stand-in in this folder. The PYTHONPATH it was given
is written to FAKE_DIR/websockify-pythonpath.

With `websockify_forks` set, it serves connections as real websockify 0.10.0 does: each from a process forked for it,
which keeps the listening socket it inherited for as long as it runs, and a page load's process ends once it is
answered. SIGTERM ends each forked process, and then the main one once they have ended, as real
websockify's does; a forked process ends on SIGTERM too. Killing the main process (a crash) leaves the forked ones
running, the port still held, so a new start cannot take it while a viewer stays attached. With `websockify_hangs` as
well, the main process ignores SIGTERM, as a hung one would, so it never ends the processes it forked; with
`websockify_handlers_hang`, the forked processes ignore SIGTERM too, so only SIGKILL ends them.

With `websockify_exits` it exits 1 at once, before it listens, as websockify does when it cannot start (its --web
folder missing, its auth plugin not importable, its port taken). With `websockify_never_listens` it runs, never
listening, until SIGTERM. With `websockify_listen_after` (seconds) it waits that long before it listens, as a slow
start does; a SIGTERM still ends it at once."""

from __future__ import annotations

import os
import signal
import socketserver
import sys
import time
from http.server import BaseHTTPRequestHandler, HTTPServer, ThreadingHTTPServer

import fakesys


class _Page(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 (the stdlib's name)
        upgrade = self.headers.get("upgrade")
        websocket = bool(upgrade) and upgrade.lower() == "websocket"  # websockify's own test for a WebSocket request
        if (websocket or self.server.web_auth) and not self._allowed():
            return
        if websocket:
            self.wfile.write(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
            self.wfile.flush()
            try:
                while self.connection.recv(4096):  # held until the viewer closes it
                    pass
            except OSError:
                pass
            self.close_connection = True
            return
        body = b"<html>fake noVNC</html>"
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _allowed(self) -> bool:
        """Ask the auth plugin about this request, as websockify's auth_connection does, and answer a refusal as its
        send_auth_error does."""
        plugin = self.server.auth_plugin
        if plugin is None:
            return True
        from websockify.auth_plugins import AuthenticationError

        try:
            target_host, target_port = self.server.target
            plugin.authenticate(headers=self.headers, target_host=target_host, target_port=target_port)
        except AuthenticationError as exc:
            self.send_response(exc.code, exc.msg)
            self.send_header("Content-Type", "text/html")
            for name, value in exc.headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.close_connection = True
            return False
        return True

    def log_message(self, *args: object) -> None:
        pass


class _Threads(ThreadingHTTPServer):
    pass


class _Forks(socketserver.ForkingMixIn, HTTPServer):
    """One process per connection, forked with the listening socket still open in it, as real websockify's are."""

    def finish_request(self, request, client_address) -> None:
        # this runs in the forked process: SIGTERM ends it, unless websockify_handlers_hang has it ignored
        hangs = fakesys.settings().get("websockify_handlers_hang")
        signal.signal(signal.SIGTERM, signal.SIG_IGN if hangs else signal.SIG_DFL)
        super().finish_request(request, client_address)


def _auth_plugin(argv: list[str]) -> object | None:
    """The auth plugin, made as websockify 0.10.0's websockify_init makes it, or None without --auth-plugin. An import
    that fails raises, and the start ends before it listens."""
    if "--auth-plugin" not in argv:
        return None
    name = argv[argv.index("--auth-plugin") + 1]
    if "." not in name:
        name = f"websockify.auth_plugins.{name}"
    module, cls = name.rsplit(".", 1)
    __import__(module)
    source = argv[argv.index("--auth-source") + 1] if "--auth-source" in argv else None
    return getattr(sys.modules[module], cls)(source)


def main(argv: list[str]) -> int:
    signal.signal(signal.SIGTERM, lambda *_: os._exit(0))  # first: a SIGTERM landing anywhere ends it with exit 0
    fakesys.log_call("websockify", argv)
    (fakesys.root() / "websockify-pythonpath").write_text(os.environ.get("PYTHONPATH", ""))
    if fakesys.settings().get("websockify_exits"):
        print("fake websockify: cannot start", file=sys.stderr)
        return 1
    if fakesys.settings().get("websockify_never_listens"):
        while True:
            time.sleep(0.05)  # back in Python within 50 ms, where the SIGTERM handler runs
    listen_at = time.monotonic() + float(fakesys.settings().get("websockify_listen_after") or 0)
    while time.monotonic() < listen_at:
        time.sleep(0.05)  # back in Python within 50 ms, where the SIGTERM handler runs
    plugin = _auth_plugin(argv)
    host, _, port = argv[argv.index("--web") + 2].rpartition(":")
    target_host, _, target_port = argv[argv.index("--web") + 3].rpartition(":")
    forks = bool(fakesys.settings().get("websockify_forks"))
    server = (_Forks if forks else _Threads)((host, int(port)), _Page)
    server.auth_plugin, server.web_auth, server.target = plugin, "--web-auth" in argv, (target_host, int(target_port))
    if forks:
        def end_forked_then_exit(*_: object) -> None:
            for pid in list(server.active_children or ()):
                try:
                    os.kill(pid, signal.SIGTERM)
                    os.waitpid(pid, 0)
                except (ProcessLookupError, ChildProcessError):
                    pass
            os._exit(0)

        hangs = fakesys.settings().get("websockify_hangs")
        signal.signal(signal.SIGTERM, signal.SIG_IGN if hangs else end_forked_then_exit)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
