"""DevTools: the only module that talks to Chrome. Plain HTTP for its version and page list, a websocket for the rest."""

from __future__ import annotations

import http.client
import json
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.sync.client import connect

from orion_browser.config import loopback

TIMEOUT = 5.0


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """DevTools never redirects, so a 3xx is an error (an HTTPError, which _get turns into a DevToolsError), not a
    request to go somewhere else."""

    def redirect_request(self, *args, **kwargs):
        return None


def _opener() -> urllib.request.OpenerDirector:
    """Loopback only: never through a proxy (an explicit, empty ProxyHandler: the default one reads the environment
    when it is built), never to another address."""
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)


_HTTP = _opener()


class DevToolsError(Exception):
    """Chrome did not answer, or answered with something that is not DevTools."""


@dataclass(frozen=True)
class PortFile:
    port: int
    path: str
    mtime: float

    @property
    def endpoint(self) -> str:
        return f"ws://{loopback()}:{self.port}{self.path}"


def read_port_file(path: Path) -> PortFile | None:
    """The port file Chrome wrote into its profile, or None when there is none."""
    try:
        mtime = path.stat().st_mtime
        lines = path.read_text(encoding="ascii").splitlines()
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise DevToolsError(f"cannot read {path}: {exc}") from None
    if len(lines) < 2 or not lines[0].isdigit() or len(lines[0]) > 5 or not lines[1].startswith("/devtools/browser/"):
        raise DevToolsError(f"{path} is not a DevTools port file")
    port = int(lines[0])
    if not 0 < port < 65536:
        raise DevToolsError(f"{path} names port {port}")
    return PortFile(port, lines[1], mtime)


def _get(port: int, route: str, timeout: float | None = None) -> Any:
    try:
        url = f"http://{loopback()}:{port}{route}"
        with _HTTP.open(url, timeout=TIMEOUT if timeout is None else timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    # URLError is an OSError; bad JSON or encoding is a ValueError; a peer that does not speak HTTP is an HTTPException
    # (BadStatusLine, IncompleteRead, LineTooLong); JSON nested too deep is a RecursionError
    except (OSError, ValueError, http.client.HTTPException, RecursionError) as exc:
        raise DevToolsError(f"no DevTools answer on port {port}: {exc}") from None


def version(port: int, *, timeout: float | None = None) -> dict[str, Any]:
    answer = _get(port, "/json/version", timeout)
    if not isinstance(answer, dict):
        raise DevToolsError(f"port {port} answered /json/version with something that is not DevTools")
    return answer


def pages(port: int, *, timeout: float | None = None) -> list[dict[str, Any]]:
    answer = _get(port, "/json/list", timeout)
    if not isinstance(answer, list):
        raise DevToolsError(f"port {port} answered /json/list with something that is not DevTools")
    return [t for t in answer if isinstance(t, dict) and t.get("type") == "page" and isinstance(t.get("id"), str)]


def page_endpoint(port: int, page_id: str) -> str:
    return f"ws://{loopback()}:{port}/devtools/page/{page_id}"


def call(endpoint: str, method: str, params: dict[str, Any] | None = None, timeout: float = TIMEOUT) -> dict[str, Any]:
    """Send one DevTools command and return its result. `timeout` is one deadline for the whole call, connecting
    included: messages for anything else, however fast they arrive, do not extend it."""
    deadline = time.monotonic() + timeout
    try:
        with connect(endpoint, open_timeout=timeout, close_timeout=1, max_size=None, proxy=None) as ws:
            ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
            while True:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise TimeoutError(f"no answer within {timeout:g} s")
                message = json.loads(ws.recv(timeout=left))
                if not isinstance(message, dict):
                    raise DevToolsError(f"{method}: answered with something that is not a DevTools message")
                if message.get("id") == 1:
                    if "error" in message:
                        raise DevToolsError(f"{method}: {message['error']}")
                    result = message.get("result", {})
                    if not isinstance(result, dict):
                        raise DevToolsError(f"{method}: the result is not an object")
                    return result
    except (OSError, TimeoutError, WebSocketException, ValueError, RecursionError) as exc:
        raise DevToolsError(f"{method} failed: {exc}") from None


def evaluate(endpoint: str, expression: str, *, timeout: float | None = None) -> Any:
    params = {"expression": expression, "returnByValue": True}
    if timeout is None:
        result = call(endpoint, "Runtime.evaluate", params)
    else:
        result = call(endpoint, "Runtime.evaluate", params, timeout)
    if "exceptionDetails" in result:
        details = result["exceptionDetails"]
        text = details.get("text", "an exception") if isinstance(details, dict) else "an exception"
        raise DevToolsError(f"evaluation failed: {text}")
    value = result.get("result", {})
    if not isinstance(value, dict):
        raise DevToolsError("evaluation answered with something that is not a result object")
    return value.get("value")


def close_browser(endpoint: str) -> None:
    """Ask Chrome to close. It may drop the connection before it answers: either way the caller judges by whether the
    process exits."""
    try:
        call(endpoint, "Browser.close")
    except DevToolsError:
        pass
