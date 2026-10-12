"""Stand-in for websockify 0.10.0's auth_plugins, as the distribution ships it: the classes an auth plugin builds on,
with the same names, arguments and attributes. websockify answers an AuthenticationError with its code, its message as
the reason, and its headers. ExpectOrigin takes a WebSocket only when its Origin is one of the origins in the source
(split at whitespace); one with no Origin is refused too."""


class BasePlugin:
    def __init__(self, src=None):
        self.source = src

    def authenticate(self, headers, target_host, target_port):
        pass


class AuthenticationError(Exception):
    def __init__(self, log_msg=None, response_code=403, response_headers=None, response_msg=None):
        self.code = response_code
        self.headers = response_headers or {}
        self.msg = response_msg
        super().__init__(f"{self.code} {response_msg if log_msg is None else log_msg}")


class InvalidOriginError(AuthenticationError):
    def __init__(self, expected, actual):
        self.expected_origin = expected
        self.actual_origin = actual
        super().__init__(
            response_msg="Invalid Origin", log_msg=f"Invalid Origin Header: Expected one of {expected}, got '{actual}'",
        )


class ExpectOrigin:
    def __init__(self, src=None):
        self.source = [] if src is None else src.split()

    def authenticate(self, headers, target_host, target_port):
        origin = headers.get("Origin", None)
        if origin is None or origin not in self.source:
            raise InvalidOriginError(expected=self.source, actual=origin)
