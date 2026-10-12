"""devtools: every answer that is not DevTools becomes a DevToolsError, whatever shape it takes, and no call outlasts
its limit. Each test talks to a tiny server on the test loopback; there is no Chrome."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path

import pytest
from conftest import LOOPBACK
from websockets.exceptions import ConnectionClosed
from websockets.sync.server import serve

from orion_browser import devtools

# Nesting far past the recursion limit of any JSON parser, Python 3.14's stack-based limit included (5000 parses there).
TOO_DEEP = 100_000

PORT_LINES = "{port}\n/devtools/browser/abc\n"


@pytest.fixture(autouse=True)
def _test_loopback(monkeypatch):
    monkeypatch.setenv("ORION_BROWSER_LOOPBACK", LOOPBACK)


class Peer:
    """A tiny TCP server on the test loopback. `answer(conn, head)` gets each connection and the head of the HTTP
    request it sent, and writes whatever it likes."""

    def __init__(self, answer):
        self._sock = socket.socket()
        self._sock.bind((LOOPBACK, 0))
        self._sock.listen(8)
        self.port = self._sock.getsockname()[1]
        self.requests: list[bytes] = []
        self._answer = answer
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn):
        with conn:
            try:
                head = b""
                while b"\r\n\r\n" not in head:
                    chunk = conn.recv(4096)
                    if not chunk:
                        return
                    head += chunk
                self.requests.append(head)
                self._answer(conn, head)
            except OSError:
                pass

    def close(self):
        try:
            self._sock.shutdown(socket.SHUT_RDWR)  # wakes the thread blocked in accept()
        except OSError:
            pass
        self._sock.close()


@pytest.fixture
def peer():
    started: list[Peer] = []

    def start(answer) -> Peer:
        started.append(Peer(answer))
        return started[-1]

    yield start
    for server in started:
        server.close()


@pytest.fixture
def cdp():
    """Starts a websocket server on the test loopback running `script(ws)` for each connection; returns its endpoint."""
    servers = []

    def start(script) -> str:
        def run(ws):
            try:
                script(ws)
            except ConnectionClosed:  # the client left, as it does when it gives up
                pass

        server = serve(run, LOOPBACK, 0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return f"ws://{LOOPBACK}:{server.socket.getsockname()[1]}/devtools/browser/test"

    yield start
    for server in servers:
        server.shutdown()


def free_port() -> int:
    """A port on the test loopback that nothing listens on."""
    with socket.socket() as probe:
        probe.bind((LOOPBACK, 0))
        return probe.getsockname()[1]


def http(body: bytes, status: str = "200 OK", length: int | None = None):
    """An answer: an HTTP response carrying this body. `length` can declare more than is sent, to cut the body short."""
    head = (f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body) if length is None else length}\r\nConnection: close\r\n\r\n")
    return lambda conn, request: conn.sendall(head.encode() + body)


def raw(data: bytes):
    """An answer: these bytes, whatever they are, then the connection closes."""
    return lambda conn, request: conn.sendall(data)


def reply(**fields):
    """A websocket script: answers the first message with these fields and its id."""
    def script(ws):
        message = json.loads(ws.recv())
        ws.send(json.dumps({"id": message["id"], **fields}))
    return script


# The HTTP side: version() and pages() ------------------------------------------------------------------------------

NOT_DEVTOOLS = {
    "a banner that is not HTTP": raw(b"RFB 003.008\n"),
    "a body cut short": http(b"abcde", length=100),
    "a header over 64 KiB": raw(b"HTTP/1.1 200 OK\r\nX: " + b"a" * 70000 + b"\r\n\r\n"),
    "a body that is not JSON": http(b"not json"),
    "a body that is not UTF-8": http(b"\xff\xfe"),
    "JSON nested past the recursion limit": http(b"[" * TOO_DEEP + b"]" * TOO_DEEP),
    "a server error": http(b"{}", status="500 Internal Server Error"),
}


@pytest.mark.parametrize("answer", NOT_DEVTOOLS.values(), ids=NOT_DEVTOOLS.keys())
def test_an_http_answer_that_is_not_devtools_is_a_devtools_error(peer, answer):
    server = peer(answer)
    with pytest.raises(devtools.DevToolsError):
        devtools.version(server.port)
    with pytest.raises(devtools.DevToolsError):
        devtools.pages(server.port)


def test_nothing_listening_is_a_devtools_error():
    with pytest.raises(devtools.DevToolsError):
        devtools.version(free_port())


def test_a_proxy_in_the_environment_is_never_used(peer, cdp, monkeypatch):
    """DevTools is on the loopback: a proxy that is set (and is not there) must not stand in the way, on HTTP or on a
    websocket."""
    dead = f"http://{LOOPBACK}:{free_port()}"
    for name in ("http_proxy", "https_proxy", "all_proxy", "ws_proxy", "wss_proxy"):
        monkeypatch.setenv(name, dead)
        monkeypatch.setenv(name.upper(), dead)
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(devtools, "_HTTP", devtools._opener())  # the module's own is built once, when it is imported
    assert devtools.version(peer(http(b'{"Browser": "Chrome/150"}')).port) == {"Browser": "Chrome/150"}
    assert devtools.call(cdp(reply(result={"ok": 1})), "Any.method") == {"ok": 1}


def test_an_answer_that_never_comes_is_a_devtools_error_within_the_limit(peer, monkeypatch):
    monkeypatch.setattr(devtools, "TIMEOUT", 0.3)
    server = peer(lambda conn, request: time.sleep(2))
    began = time.monotonic()
    with pytest.raises(devtools.DevToolsError):
        devtools.version(server.port)
    assert time.monotonic() - began < 1.5


def test_a_redirect_is_not_followed(peer):
    """The opener is for loopback DevTools, which never redirects: a 3xx must not send the request anywhere else."""
    elsewhere = peer(http(b'{"Browser": "Chrome/150"}'))
    server = peer(raw(
        f"HTTP/1.1 302 Found\r\nLocation: http://{LOOPBACK}:{elsewhere.port}/json/version\r\n"
        "Content-Length: 0\r\nConnection: close\r\n\r\n".encode()
    ))
    with pytest.raises(devtools.DevToolsError):
        devtools.version(server.port)
    assert elsewhere.requests == []


def test_version_needs_a_json_object_and_pages_a_json_list(peer):
    assert devtools.version(peer(http(b'{"Browser": "Chrome/150"}')).port) == {"Browser": "Chrome/150"}
    with pytest.raises(devtools.DevToolsError):
        devtools.version(peer(http(b"[1]")).port)
    with pytest.raises(devtools.DevToolsError):
        devtools.pages(peer(http(b"{}")).port)


def test_pages_keeps_only_page_targets_with_an_id(peer):
    listing = [{"type": "page", "id": "A"}, {"type": "iframe", "id": "B"}, 5, {"type": "page", "id": 7},
               {"type": "page"}]
    assert devtools.pages(peer(http(json.dumps(listing).encode())).port) == [{"type": "page", "id": "A"}]


# The websocket side: call(), evaluate(), close_browser() -----------------------------------------------------------

def test_call_returns_the_result_and_skips_events_that_come_first(cdp):
    def script(ws):
        message = json.loads(ws.recv())
        ws.send(json.dumps({"method": "Target.targetCreated", "params": {}}))
        ws.send(json.dumps({"id": message["id"] + 5, "result": {"other": True}}))
        ws.send(json.dumps({"id": message["id"], "result": {"ok": 1}}))

    assert devtools.call(cdp(script), "Any.method") == {"ok": 1}


def test_an_error_reply_is_a_devtools_error_that_names_the_method(cdp):
    endpoint = cdp(reply(error={"code": -32601, "message": "no such method"}))
    with pytest.raises(devtools.DevToolsError, match=r"Any\.method.*no such method"):
        devtools.call(endpoint, "Any.method")


MESSAGES_THAT_ARE_NOT_DEVTOOLS = {
    "a message that is a JSON list": lambda ws: ws.send(json.dumps([1, 2])),
    "a message that is a JSON string": lambda ws: ws.send(json.dumps("hello")),
    "a message that is not JSON": lambda ws: ws.send("not json"),
    "a binary message that is not UTF-8": lambda ws: ws.send(b"\xff\xfe"),
    "a message nested past the recursion limit": lambda ws: ws.send("[" * TOO_DEEP + "]" * TOO_DEEP),
    "a result that is not an object": lambda ws: ws.send(json.dumps({"id": 1, "result": 5})),
    "a connection that closes without an answer": lambda ws: None,
}


@pytest.mark.parametrize("send", MESSAGES_THAT_ARE_NOT_DEVTOOLS.values(), ids=MESSAGES_THAT_ARE_NOT_DEVTOOLS.keys())
def test_a_message_that_is_not_devtools_is_a_devtools_error(cdp, send):
    def script(ws):
        ws.recv()
        send(ws)

    with pytest.raises(devtools.DevToolsError):
        devtools.call(cdp(script), "Any.method", timeout=2)


def test_nothing_listening_for_a_call_is_a_devtools_error():
    with pytest.raises(devtools.DevToolsError):
        devtools.call(f"ws://{LOOPBACK}:{free_port()}/devtools/browser/test", "Any.method")


def test_a_server_that_is_not_a_websocket_server_is_a_devtools_error(peer):
    server = peer(http(b"<html>hello</html>"))
    with pytest.raises(devtools.DevToolsError):
        devtools.call(f"ws://{LOOPBACK}:{server.port}/devtools/browser/test", "Any.method", timeout=2)


def test_no_answer_is_a_devtools_error_at_the_deadline(cdp):
    def script(ws):
        ws.recv()
        ws.recv()  # waits for the client to leave

    began = time.monotonic()
    with pytest.raises(devtools.DevToolsError):
        devtools.call(cdp(script), "Any.method", timeout=0.5)
    assert time.monotonic() - began < 2.5


def test_the_deadline_holds_while_unrelated_messages_keep_arriving(cdp):
    """The timeout is one deadline for the whole call. Events for other things must not extend it, even when they come
    faster than the client's shortest wait."""
    def script(ws):
        ws.recv()
        until = time.monotonic() + 4
        while time.monotonic() < until:
            ws.send(json.dumps({"method": "Page.frameNavigated"}))
            time.sleep(0.001)

    began = time.monotonic()
    with pytest.raises(devtools.DevToolsError):
        devtools.call(cdp(script), "Any.method", timeout=0.5)
    assert time.monotonic() - began < 2.5


def result_of(value):
    return reply(result=value)


def test_evaluate_returns_the_value(cdp):
    assert devtools.evaluate(cdp(result_of({"result": {"type": "string", "value": "hi"}})), "1") == "hi"


def test_evaluate_of_something_with_no_value_returns_none(cdp):
    assert devtools.evaluate(cdp(result_of({"result": {"type": "undefined"}})), "undefined") is None
    assert devtools.evaluate(cdp(result_of({})), "undefined") is None


def test_evaluate_names_what_the_page_threw(cdp):
    with pytest.raises(devtools.DevToolsError, match="Uncaught"):
        devtools.evaluate(cdp(result_of({"exceptionDetails": {"text": "Uncaught"}})), "throw 1")


@pytest.mark.parametrize("value", [
    {"exceptionDetails": "boom"},
    {"exceptionDetails": ["boom"]},
    {"result": 5},
    {"result": ["x"]},
], ids=["exceptionDetails is a string", "exceptionDetails is a list", "result is a number", "result is a list"])
def test_evaluate_with_a_reply_of_the_wrong_shape_is_a_devtools_error(cdp, value):
    with pytest.raises(devtools.DevToolsError):
        devtools.evaluate(cdp(result_of(value)), "1")


def test_close_browser_asks_for_browser_close(cdp):
    methods = []

    def script(ws):
        message = json.loads(ws.recv())
        methods.append(message["method"])
        ws.send(json.dumps({"id": message["id"], "result": {}}))

    devtools.close_browser(cdp(script))
    assert methods == ["Browser.close"]


def test_close_browser_never_raises(cdp, peer):
    """The caller judges by whether the process exits, so nothing may escape: not a refusal, not an error reply."""
    devtools.close_browser(f"ws://{LOOPBACK}:{free_port()}/devtools/browser/test")
    devtools.close_browser(cdp(reply(error={"code": -1, "message": "no"})))
    devtools.close_browser(cdp(lambda ws: ws.send("[1]")))
    devtools.close_browser(f"ws://{LOOPBACK}:{peer(http(b'x')).port}/devtools/browser/test")


# The port file ----------------------------------------------------------------------------------------------------

def test_a_missing_port_file_is_none(tmp_path):
    assert devtools.read_port_file(tmp_path / "DevToolsActivePort") is None


def test_a_port_file_gives_its_port_path_endpoint_and_mtime(tmp_path):
    path = tmp_path / "DevToolsActivePort"
    path.write_text(PORT_LINES.format(port=40123))
    found = devtools.read_port_file(path)
    assert (found.port, found.path) == (40123, "/devtools/browser/abc")
    assert found.endpoint == f"ws://{LOOPBACK}:40123/devtools/browser/abc"
    assert found.mtime == path.stat().st_mtime


@pytest.mark.parametrize("text", [
    "", "\n", "40123", "40123\n", "abc\n/devtools/browser/x\n", "-1\n/devtools/browser/x\n",
    "40123\n/devtools/page/x\n", "0\n/devtools/browser/x\n", "65536\n/devtools/browser/x\n",
    "99999999999\n/devtools/browser/x\n", "9" * 5000 + "\n/devtools/browser/x\n",
], ids=["empty", "blank line", "no path line", "no path", "port is text", "port is negative", "path is a page",
        "port 0", "port 65536", "port with 11 digits", "port with 5000 digits"])
def test_a_port_file_that_is_not_one_is_a_devtools_error(tmp_path, text):
    path = tmp_path / "DevToolsActivePort"
    path.write_text(text)
    with pytest.raises(devtools.DevToolsError):
        devtools.read_port_file(path)


def test_the_highest_port_is_accepted(tmp_path):
    path = tmp_path / "DevToolsActivePort"
    path.write_text(PORT_LINES.format(port=65535))
    assert devtools.read_port_file(path).port == 65535


def test_a_port_file_with_bytes_that_are_not_ascii_is_a_devtools_error(tmp_path):
    path = tmp_path / "DevToolsActivePort"
    path.write_bytes(b"40123\n/devtools/browser/\xff\n")
    with pytest.raises(devtools.DevToolsError):
        devtools.read_port_file(path)


def test_a_port_file_that_is_a_folder_is_a_devtools_error(tmp_path):
    (tmp_path / "DevToolsActivePort").mkdir()
    with pytest.raises(devtools.DevToolsError):
        devtools.read_port_file(Path(tmp_path / "DevToolsActivePort"))
