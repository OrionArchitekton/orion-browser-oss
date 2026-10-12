# Public release review: 0.1.0

Reviewed 2026-10-12. The public source export contains code, synthetic tests,
example configuration, documentation, CI workflows, and the MIT license.
It excludes private deployment configuration, browser profiles, cookies,
session records, screenshots, and private repository history.

Validation before release:

- 580 synthetic tests passed through the CLI and MCP boundaries.
- The isolated real-browser smoke test passed on Google Chrome 155.0.8059.39:
  headful, visible, focused; clean release; synthetic cookie retained across a
  restart; old endpoint unreachable; profile free at the end.
- Wheel and source distribution built; a fresh environment installed the wheel,
  ran the CLI version command, and imported the MCP server.
- Release review reproduced and fixed multi-window focus selection, page-controlled JSON serialization,
  old-lease loss during a refused takeover, unreadable background targets, and invalid Chrome program paths. Targeted CLI regressions failed before their fixes
  and passed after them; the final full suite and isolated real-browser smoke then passed.
- A synthetic ignored planning document was excluded from the source archive; public release docs remained included.
- Public-file hygiene and redacted secret scans passed. A full-history scan is
  repeated on the release candidate before publication.
- The guide received an independent accuracy and leakage review. Figures use
  generic identity and holder names.

These checks do not prove every website accepts a sign-in, authorize account
mutations, or turn this trusted-host coordination layer into a security sandbox.
See SECURITY.md and the operations runbook for the actual boundary.

The package remains named orion-browser. The public repository is
orion-browser-oss. Chrome and system dependencies are separately installed and
are not relicensed or bundled by this MIT source release.
