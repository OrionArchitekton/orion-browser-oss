---
verified: 2026-10-11
review_after: 2027-01-11
topics: [orion-browser, browsers, sign-in, operator]
references:
  - specs/orion-browser-spec.md
  - config.example.toml
  - tests/smoke_real_browser.py
---

# orion-browser operations

## Install or upgrade

1. `sudo apt install tigervnc-standalone-server novnc websockify x11-apps xdotool` (once).
2. `uv tool install --force <source checkout of orion-browser>`. MCP servers already running keep their loaded code
   until their agent sessions restart (each CLI call they spawn runs the new code). Browsers running when the tool is
   upgraded keep the Chrome they were started with, and its flags, until they are released: release running browsers
   before upgrading, or release and lease them again after.
3. Config at `~/.config/orion-browser/config.toml` (see `config.example.toml`), then `orion-browser setup`.
4. Check: from the source checkout, run the smoke test with the installed tool's own Python, so it tests what step 2
   installed: `"$(uv tool dir)/orion-browser/bin/python" tests/smoke_real_browser.py` (it needs agent-browser on
   PATH). It prints `"ok": true`. It runs its own browser, `smoke`,
   on screen 89 (no browser in the config may use that name or screen), so it can run while the operator's browsers
   are leased; it refuses to start while an `orion-browser-smoke-*` service is in any state but inactive or failed,
   and a passing run removes its folder unless given `--keep`.
5. Keep Chrome current. Sessions turn off Chrome's own out-of-date bubble (it took the page's focus), and the
   system's automatic updates skip Google's repository by default, so nothing updates Chrome by itself.
   `orion-browser setup` and `orion-browser --json status` report its version and build age; past eight weeks
   status shows `outdated: true` and setup prints the update command. Update while every browser is down (see
   status): `sudo apt install --only-upgrade google-chrome-stable`, then repeat step 4.

## First sign-in for a browser (the operator)

0. Before any browser is signed in: the operator's posting rule covers pool browsers (SECURITY.md, "Not
   defended"), and the view's framing risk is guarded (SECURITY.md).
1. `orion-browser lease <browser> --holder <you> --minutes 60`
2. `orion-browser view <browser> --drive --holder <you>`; open the drive link in a browser tab.
3. Sign in to each of the browser's accounts by hand, on the site's own sign-in page (for Google, open
   accounts.google.com in a tab). Decline Chrome's own sign-in and sync ("Sign in to Chrome", "Turn on sync"):
   sync would copy your synced passwords and history into a profile agents drive. Second-factor codes come from
   you, never from an agent.
4. `orion-browser release <browser> --holder <you>`: the result must say `stop: clean`.
5. Check the sign-ins survived (open question Q2): lease again under another holder name, open its drive view
   (`orion-browser view <browser> --drive --holder <that name>`), open each platform, and note whether it is still
   signed in. Record each platform below. If one signs out, set `restore_session = true` for that browser and check
   again (`chrome://settings/onStartup` in the drive view shows Continue where you left off).
6. Google accounts (open question Q3): record whether Google accepted the sign-in in the drive view.

| Date | Browser | Platform | Signed in after a clean stop and start | Notes |
|---|---|---|---|---|

While `restore_session` is false (the default), each start resets Chrome's "Continue where you left off"
startup setting, even one chosen by hand in the drive view; other startup choices are kept. To keep a browser's
tabs across stops, set `restore_session = true` for it in the config instead.

## Watching

- `orion-browser --json status`: every browser's state, holder and expiry, and Chrome's version and build age
  (`outdated: true` past eight weeks: see Install or upgrade, step 5).
- `orion-browser view <browser>`: the watch-only link.
- Session records: `~/.local/state/orion-browser/audit/<browser>/<date>.jsonl` (the UTC date; under [paths] state
  when it is set): lease and extend (with the expiry),
  takeover and release (with how the old session's stop ended), stop (the tool's stop of a holder's own session
  that no longer answered, or of a session a start began, with how it ended), service-stop (the session's own
  record of a stop signal that reached it, so such a stop shows twice; a kill by the service manager after its stop
  timeout shows once), crashed, collision, start-failed (naming the session that failed to start and, for a
  takeover, the previous holder and session) and close-skipped (a stop that sent no DevTools close; `reason` says
  why: `no process of the session listens on the port` (a Chrome orion-browser did not start wrote the port file, or
  the session's Chrome had already shut DevTools or exited), `no readable port file` (by the session's own stop
  handling only: Chrome never wrote one, or removed it as it shut down after a close it took), or `the service's main
  process is unknown` (by the tool only); `by` says whether the tool or the session's own stop handling skipped it).
- A session's own log: `journalctl --user -u 'orion-browser-<browser>-*'` (a service name ends in a short tag of
  the config's lease store).

## Problems

- `BROWSER_DOWN` naming a pid: a process orion-browser did not start has the profile open (often an agent driver
  launched on the profile directory). Find out whose it is before ending it; the tool never ends it. A release
  refused this way keeps the lease: once that process has ended, release again (its pids are in the result's
  data, under processes).
- `BROWSER_DOWN` naming a port: a program orion-browser did not start holds the browser's screen (another
  config's session on the same screen number, or another VNC server). The tool never ends it; give the browser
  another screen, or end that program.
- `BROWSER_DOWN` saying the view did not come up (it names websockify's port): the session's websockify exited,
  never listened, or could not be started at all (the message then says why: removed or not executable since
  setup; run `orion-browser setup`), so the start failed and nothing of it was left running. Its output, in the
  `journalctl` command the message gives, says why: often the noVNC folder is missing (`[view] novnc_dir`, see
  setup), or another program took the view's port.
- `BROWSER_DOWN` saying the profile is being started by another session: another config that reaches the same
  profile is starting it. Give each config profiles of its own (spec section 9).
- `USAGE` saying the start lock of a browser's profile cannot be opened or locked: the folder that holds the
  profile (for a linked profile, the folder its link reaches) is not writable by the operator, or its filesystem
  has no file locks. Fix the folder, or move the profile; the refused attempt took no other holder's lease.
- A release, or a failed start, that cannot show the session gone keeps its lease (status names the holder; after
  a failed start it shows the browser as starting, and the session records hold a start-failed record saying "the
  lease stays yours"; a start in progress shows starting too, for a few minutes at most, and a lease command
  interrupted (Ctrl-C) once its session's service was asked to start leaves the browser starting until that holder
  releases it, or another holder takes it over once the lease lapses):
  `systemctl --user stop 'orion-browser-<browser>-*'`, then release again (after a failed start: release, or lease
  again to retry). A takeover that cannot show the old session gone gives the lease back to its previous holder:
  stop the service the same way, then lease again.
- `stop: signal` or `stop: kill`: Chrome did not close through DevTools in time; recent sign-ins may need repeating.
  With a close-skipped record by the session (`by: service`, reason `no process of the session listens on the
  port`) beside it and a release that could not show the session gone, a Chrome orion-browser did not start opened
  the profile (see the pid entry above). By the tool alone, with `stop: clean`, systemd did not tell the tool the
  session's main process.
- `UNHEALTHY` or a failed start naming the `webdriver` check alone, with the note "the page does not report
  navigator.webdriver false". The common, harmless case: a session started by an earlier orion-browser (before the
  flag that turns Chrome's automation marker off was added) has no flag, so its first status after the upgrade names
  `webdriver` alone; release it and lease it again. Only a NEW session that fails the check means Chrome no longer
  honors the flag (spec S1), and Google would refuse a sign-in in it. (With `visible` and `focused` failed beside it,
  no tab showed itself: that is a page problem, not this one.) Every new lease then fails the same way until this
  is settled; it is the operator's decision (check the Chrome version with `orion-browser setup`, and the spec's
  section 13).
- A lease held by `mcp-<hex>` after its agent session ended: its MCP server was killed before it could give the
  lease back. Wait for it to expire, or release it with that holder (status shows it):
  `orion-browser release <browser> --holder mcp-<hex>`. If the server stopped while one of its lease calls was still
  running, its give-back waits for that call and then releases (up to about 17 minutes at the default timings,
  longer when it held several browsers); check status again before releasing by hand.

## Rollback

1. Release every browser (`status` lists holders), or stop the services: `systemctl --user stop 'orion-browser-*.service'`.
2. `uv tool uninstall orion-browser` and remove it from each MCP client (`claude mcp remove orion-browser`; the
   [mcp_servers.orion-browser] entry in Codex's config).
3. Profiles stay in `~/.local/share/orion-browser/profiles/`. Deleting one removes that local sign-in state; it does not revoke remote sessions. Deletion is the
   operator's call.

To go back a version instead: the source checkout stays on main. Check out the previous release in a folder of its
own (a git worktree of its tag, for example), install it from there with `uv tool install --force <that checkout>`,
and run Install or upgrade, step 4, from that checkout. `~/.local/state/orion-browser` stays as well.
