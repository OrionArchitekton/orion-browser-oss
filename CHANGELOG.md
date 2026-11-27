# Changelog

## Unreleased

Release automation only; no change to the package's code.

- Two-step PyPI release through Trusted Publishing, the same as orion-phone. `prepare-release`
  (manual, or a `v*` tag push) runs the checks and the secret scan on the selected source, builds with
  hash-pinned tools and a recorded build epoch, normalizes both archives, and uploads them with their
  SHA256SUMS as the `reviewed-dist` artifact; it has no publishing credential. `release` (manual, from
  main only) verifies that exact artifact against the reviewed manifest digest and version, then, after
  approval of the `pypi` environment, publishes those bytes without rebuilding.
- CI checks and the gitleaks scan can be called by release preparation, CI builds with the release's
  pinned tools, and the source distribution now carries the full test harness and the release scripts.
- docs/runbooks/release-artifacts.md and specs/release-approval-spec.md describe the process.

## 0.1.1

Packaging and listings only; no change to how orion-browser behaves.

- The distribution is now named orion-browser-oss, for PyPI, where orion-browser and orion-browser-mcp
  belong to other projects. The orion-browser and orion-browser-mcp commands and the orion_browser
  import package keep their names. An existing `uv tool` install named orion-browser is uninstalled once
  before upgrading (see the operations runbook).
- PyPI metadata: author, keywords, classifiers, and Homepage, Issues, Changelog and Security links.
- server.json for the official MCP Registry as io.github.OrionArchitekton/orion-browser-oss, run from
  the PyPI package with `uvx --from orion-browser-oss==0.1.1 orion-browser-mcp`, and the README's
  mcp-name line that proves the package belongs to it.
- gemini-extension.json, so Gemini CLI can install the MCP server as an extension.
- README install steps for PyPI, `uvx`, Gemini CLI and the MCP Registry.
- Dependency floors raised since 0.1.0: mcp 2.3.0 and websockets 17.2.

## 0.1.0

Initial public MIT release of orion-browser.

- Persistent Chrome profiles per configured identity and independent virtual screens.
- Exclusive holder leases, renewal, expiry-based takeover, and verified session shutdown.
- CLI setup, status, lease, release, and operator view commands.
- MCP status, lease, and release tools using the same CLI boundary.
- Synthetic integration tests, an optional real-browser smoke check, and an operations runbook.
- Release review fixes: scan visible windows for the focused page, avoid page-controlled JSON
  serialization, and retain the old lease when a takeover cannot prove the profile free.
- Skip unreadable page targets while checking the remaining pages, and reject a directory configured
  as the Chrome executable during setup.

The public source repository is orion-browser-oss; installed commands remain
orion-browser and orion-browser-mcp. Chrome and system dependencies are not bundled.
The release is for cooperative agents on a trusted Linux host. See SECURITY.md.
