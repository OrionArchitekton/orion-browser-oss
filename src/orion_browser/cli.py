"""orion-browser command line. Exit codes and --json output are the contract agents rely on."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback

from orion_browser import __version__
from orion_browser.codes import BrowserError, Code, Result

HOLDER_RE = re.compile(r"[A-Za-z0-9._:@-]{1,64}")  # the holder lands in lease files and session records
AGENT_COMMANDS = ("status", "lease", "release")
LISTED_COMMANDS = (*AGENT_COMMANDS, "view", "setup")  # what --help names: `_session` is the service's entry point


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # argparse calls this for every usage problem
        raise BrowserError(Code.USAGE, message)


def _holder(parser: argparse.ArgumentParser, note: str = "") -> None:
    parser.add_argument(
        "--holder", default=os.environ.get("ORION_BROWSER_HOLDER"),
        help=f"agent session name (or set ORION_BROWSER_HOLDER){note}",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="orion-browser", description="Signed-in Chrome browsers for AI agents.")
    parser.add_argument("--json", action="store_true", help="print one JSON object")
    parser.add_argument("--version", action="version", version=f"orion-browser {__version__}")
    sub = parser.add_subparsers(
        dest="command", required=True, parser_class=_Parser, metavar="{" + ",".join(LISTED_COMMANDS) + "}"
    )
    status = sub.add_parser("status", help="prove one browser healthy, or list every browser")
    status.add_argument("browser", nargs="?")
    lease = sub.add_parser("lease", help="take the browser for this agent session, or renew your lease")
    lease.add_argument("browser")
    lease.add_argument("--minutes", type=int, help="lease length (default and maximum from config)")
    _holder(lease)
    release = sub.add_parser("release", help="close the browser cleanly and give it back")
    release.add_argument("browser")
    _holder(release)
    view = sub.add_parser("view", help="print the links that show the browser's screen")
    view.add_argument("browser")
    view.add_argument("--drive", action="store_true", help="also print the interactive link (lease holder only)")
    _holder(view, "; needed with --drive")
    sub.add_parser("setup", help="check packages and prepare owner-only directories")
    session = sub.add_parser("_session")  # no help=: it would get a row in --help
    session.add_argument("browser")
    return parser


def _needs_holder(args: argparse.Namespace) -> bool:
    return args.command in ("lease", "release") or (args.command == "view" and args.drive)


def run(argv: list[str]) -> Result:
    args = build_parser().parse_args(argv)
    if _needs_holder(args):
        if not args.holder:
            raise BrowserError(Code.USAGE, "--holder (or ORION_BROWSER_HOLDER) must name the agent session")
        if not HOLDER_RE.fullmatch(args.holder):
            raise BrowserError(Code.USAGE, "holder must be 1 to 64 characters from letters, digits, and . _ : @ -")
    from orion_browser import config

    cfg = config.load()
    if args.command == "setup":
        from orion_browser import setup

        return setup.run(cfg)
    if args.command == "_session":
        from orion_browser.runner import run_session

        return run_session(cfg, args.browser)
    from orion_browser.session import Pool

    pool = Pool(cfg)
    if args.command == "status":
        return pool.status(args.browser)
    if args.command == "lease":
        return pool.lease(args.browser, args.holder, args.minutes)
    if args.command == "view":
        return pool.view(args.browser, args.holder, args.drive)
    return pool.release(args.browser, args.holder)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    as_json = "--json" in argv
    try:
        result = run(argv)
    except BrowserError as exc:
        result = Result(exc.code, exc.message, exc.data)
    except Exception as exc:  # a bug: still answer with one JSON object; the traceback goes to stderr
        traceback.print_exc()
        result = Result(Code.INTERNAL, f"internal error: {type(exc).__name__}: {exc}")
    if as_json:
        print(json.dumps(result.to_json()))
    else:
        _print_text(result)
    return int(result.code)


def _print_text(result: Result) -> None:
    stream = sys.stdout if result.code is Code.OK else sys.stderr
    print(f"{result.code.name}: {result.message}", file=stream)
    for key, value in result.data.items():
        print(f"  {key}: {value}", file=stream)


def entry() -> None:
    raise SystemExit(main())
