# Changelog

## 0.1.0

Initial public MIT release of orion-browser.

- Persistent Chrome profiles per configured identity and independent virtual screens.
- Exclusive holder leases, renewal, expiry-based takeover, and verified session shutdown.
- CLI setup, status, lease, release, and operator view commands.
- MCP status, lease, and release tools using the same CLI boundary.
- Synthetic integration tests, an optional real-browser smoke check, and an operations runbook.
- Release review fixes: scan visible windows for the focused page, avoid page-controlled JSON
  serialization, and retain the old lease when a takeover cannot prove the profile free.

The public source repository is orion-browser-oss; installed commands remain
orion-browser and orion-browser-mcp. Chrome and system dependencies are not bundled.
The release is for cooperative agents on a trusted Linux host. See SECURITY.md.
