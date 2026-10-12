"""The view's guard: websockify's auth plugin for every view (`--auth-plugin orion_browser_view_guard.ViewGuard`, with
`--web-auth`, so websockify asks it about each page load as well as each WebSocket).

websockify is the distribution's, run by the system's Python, not by orion-browser's own. So this module imports only
the standard library and websockify's own auth_plugins, never orion_browser, and websockify is given its folder alone
as PYTHONPATH (the folder holds nothing else): nothing of orion-browser's installation reaches websockify's Python.

It refuses, with 403:

- A load into another page's frame: a request whose Sec-Fetch-Dest is iframe, frame, embed, object or fencedframe.
  The browser sets that header (Fetch Metadata) and no page can set it. Otherwise any page in any browser on the host
  could show a view in a frame and steer a person's clicks or typing into it: the frame is the view's own page, so its
  WebSocket passes the origin check below. A request without the header still loads, since not every client sends it.
- A WebSocket from any page but the view's own, exactly as websockify's ExpectOrigin, which this extends (websockify
  takes one auth plugin): its Origin must be one of the --auth-source origins. Page loads carry no Origin, so only a
  WebSocket request is checked, told apart as websockify itself tells it apart (an Upgrade header of websocket)."""

from websockify.auth_plugins import AuthenticationError, ExpectOrigin

FRAMES = frozenset({"iframe", "frame", "embed", "object", "fencedframe"})  # Sec-Fetch-Dest of a load into a page


class ViewGuard(ExpectOrigin):
    def authenticate(self, headers, target_host, target_port):
        destination = str(headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if destination in FRAMES:
            raise AuthenticationError(
                log_msg=f"refused a load into a frame (Sec-Fetch-Dest: {destination})",
                response_msg="Refused in a frame",
            )
        upgrade = headers.get("Upgrade")
        if upgrade and upgrade.lower() == "websocket":  # websockify's own test for a WebSocket request
            super().authenticate(headers, target_host, target_port)
