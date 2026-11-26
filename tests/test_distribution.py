"""The distribution's name and version agree everywhere a listing reads them: pyproject.toml, the package's own
__version__ (what `orion-browser --version` prints), the MCP Registry's server.json, the Gemini CLI manifest, and the
README's mcp-name line, which is how the registry checks that the PyPI package belongs to the server."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import orion_browser
from orion_browser.mcp_server import CLI_TIMEOUT

ROOT = Path(__file__).resolve().parent.parent
PROJECT = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
SERVER = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
GEMINI = json.loads((ROOT / "gemini-extension.json").read_text(encoding="utf-8"))
PINNED = f"{PROJECT['name']}=={PROJECT['version']}"


def test_package_version_matches_the_distribution():
    assert orion_browser.__version__ == PROJECT["version"]


def test_server_json_names_this_pypi_release_and_launches_the_mcp_command():
    assert SERVER["version"] == PROJECT["version"]
    [package] = SERVER["packages"]
    assert (package["registryType"], package["identifier"], package["version"]) == (
        "pypi", PROJECT["name"], PROJECT["version"])
    assert package["transport"] == {"type": "stdio"}
    assert [arg.get("value") for arg in package["runtimeArguments"]] == [PINNED, "orion-browser-mcp"]
    assert "orion-browser-mcp" in PROJECT["scripts"]


def test_gemini_extension_runs_the_same_pinned_release():
    assert (GEMINI["name"], GEMINI["version"]) == (PROJECT["name"], PROJECT["version"])
    [server] = GEMINI["mcpServers"].values()
    assert (server["command"], server["args"]) == ("uvx", ["--from", PINNED, "orion-browser-mcp"])


def test_gemini_timeout_outlasts_the_servers_own_cli_timeout():
    """A lease at the config's maxima can run about 5,170 s; Gemini must not give up before the server does."""
    [server] = GEMINI["mcpServers"].values()
    assert server["timeout"] >= CLI_TIMEOUT * 1000  # Gemini counts milliseconds


def test_readme_carries_the_registry_ownership_line():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert re.search(r"<!-- mcp-name: (\S+) -->", readme).group(1) == SERVER["name"]
