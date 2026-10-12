# orion-browser specification

Status: accepted 2026-10-07. This is the living validation reference for orion-browser: update it in
the same change whenever behavior changes.

## 1. Purpose

orion-browser gives AI agent harnesses signed-in desktop browsers they do not have to share.

Today every agent that needs a signed-in session drives the operator's own Chrome: one window, one
focused tab, on the operator's desktop. Agents take each other's tabs, a covered window stops page
editors from working, and attaching needs the operator to click Allow and keep the window on screen.

orion-browser runs a pool of Chrome browsers, one per identity, each on its own virtual screen on the
operator's machine, and hands each one to one agent at a time. It is a coordination layer, not a
sandbox: browsers hold real signed-in accounts. Its job is to make sure exactly one holder drives a
browser at a time, that a new holder never shares a browser process with an old one, that sign-in
state survives from one session to the next, and that every session leaves a record.

It is the desktop-browser sibling of orion-phone and follows its conventions: the lease model, the
result codes, the command-line tool as the single authority, MCP parity, and public-ready hygiene.

## 2. Success criteria

Set by the operator 2026-10-06:

- **No collisions.** Agents never take each other's tabs or the operator's, and nothing needs the
  operator's Chrome on screen or an Allow click.
- **Parallel signed-in work.** Agents work signed in to different identities at the same time.
- **Unattended runs.** A long job runs to the end without the operator, unless another holder
  legitimately takes over a lapsed lease.

Not a goal: two holders driving the same identity at the same moment.

## 3. Terms

One canonical term per concept.

| Term | Meaning | Replaces |
|---|---|---|
| browser | One named identity in config (for example `work`, `personal`, `anon`): its profile, its screen number, and, while a session runs, its Chrome process on its own screen. | instance, slot, pool member |
| identity | The accounts signed in to one browser's profile, for example one brand's X, LinkedIn, YouTube and TikTok accounts. | account group, persona |
| profile | A browser's saved Chrome data: cookies, sign-ins, settings. It persists from one session to the next. | user data dir, Chrome profile |
| session | One run of a browser's Chrome process, from the lease that starts it to its stop. Every session has its own id and its own endpoint. | run, instance, boot |
| screen | The virtual display a browser's Chrome draws on: one per browser, never shown on the operator's desktop. | display, X display, virtual monitor |
| endpoint | The DevTools address a session listens on: loopback only, a new random port every session. | CDP URL, debug port |
| port file | The file Chrome writes into its profile to say which port and path its endpoint has. | DevToolsActivePort |
| lease | The exclusive, time-limited right for one holder to drive one browser. | lock, claim, reservation |
| holder | The agent session, or the operator, named on a lease. | owner, user |
| operator | The human who owns the browsers and their accounts. | user, admin |
| view | A link that shows a browser's screen in an ordinary browser tab: watch-only, or interactive (drive) for the lease holder. | VNC, viewer, remote desktop |
| clean stop | A session ended by asking Chrome to close through DevTools: the only stop shown to save recent sign-in state (section 13). | graceful shutdown |
| session record | One audit line per lease or session event. | log entry |

## 4. Scope

In v1:

- A command-line tool, the single authority for leases and for starting and stopping sessions.
- An MCP server exposing the agent commands (status, lease, release) as tools. Every tool call goes
  through the command-line tool, so leases are decided in exactly one place.
- A setup command that checks the host's packages and prepares owner-only directories.
- A view for the operator: watch any running browser, and drive one to sign in.
- Sessions run as user services of the host's service manager, so a session outlives the agent
  command that started it and no agent harness or sandbox owns the browser process.

Not in v1:

- The operator's own Chrome. Agents keep its current rules until its work moves into pool browsers; a
  lease for it may come in v2.
- Two holders in one identity at once.
- Access from other machines (remote hosts or private networks).
- Headless browsers.
- Per-action audit or screen recording.
- Per-profile proxies or network identities.
- Managing extensions.
- Automatic sign-in.
- Any attempt to infer what an agent's action means (for example "this click publishes a post").

## 5. Decisions on record

Made by the operator 2026-10-06 and 2026-10-07. Reopen one only with new evidence.

1. Browsers run on virtual screens on the Linux side of the operator's machine (Linux or WSL2): not
   on the Windows desktop, not on another node.
2. orion-browser is a sibling tool to orion-phone, not an extension of it. It carries its own copy of
   orion-phone's lease core instead of depending on the orion-phone package, so the two tools release
   on their own schedules. A shared package is extracted if a third tool needs leases.
3. One browser per brand: `work`, `personal`, and `anon` (signed in
   to nothing). The list is configuration: the repository ships an example, not this list.
4. The operator's own Windows Chrome is out of v1.
5. The view is a browser tab (noVNC), not a desktop viewer application.
6. Control model A: a lease hands out the session's endpoint and agents drive with their own tools
   (agent-browser, Playwright). Every new session is a new Chrome process on the same saved profile.
   Rejected: routing every action through orion-browser (B), and a gatekeeping proxy in front of
   DevTools (C).

## 6. Scenarios

Each scenario is a vertical slice: it runs end to end through the real command-line boundary and is
demoable on its own. Listed in build order.

### S1. One browser, proven healthy

The operator runs setup once, then leases a browser, checks it, and releases it.

Acceptance:

- `setup` checks every dependency and names each missing one with the package that provides it:
  Google Chrome from Google's own package, the VNC display server, websockify, the noVNC web files,
  the service manager's user instance, xdotool (focus, Q1) and xwd (screen captures). It never installs
  a package and never runs anything as root: it prints the install command for the operator to run.
- `setup` creates the state, profile and download directories owner-only, and refuses with USAGE any of
  them that is inside a git working tree or is not an absolute path (a leading `~` is expanded to the
  home directory; a path holding a NUL character is refused). A browser's profile or download folder can
  be a link that leads elsewhere, so the check follows links, and every start makes it again for both
  before anything is written in or beside either: one inside a git working tree refuses the start with
  USAGE naming it and where it leads, and the lease the attempt made is freed. A state or profiles path,
  or a browser's profile with its links resolved, that holds a space followed by two hyphens (` --`) is
  refused with USAGE whenever the config is loaded: in Chrome's command line as the system shows it,
  that reads as the start of Chrome's next flag, so a Chrome on that profile could go unseen.
- Configuration is checked before anything else runs. Browser names are 1 to 16 lowercase letters and
  digits, and unique. Screen numbers are unique and from 10 to 99, clear of the host's own displays.
  Lease lengths are positive, with the default no longer than the maximum. A bad value is USAGE naming
  the key.
- Every command that names a holder takes it from `--holder` or `ORION_BROWSER_HOLDER`: 1 to 64
  characters of letters, digits, and `. _ : @ -`. Anything else is USAGE.
- `lease <browser>` with no session running starts one, and answers OK only after the health proof
  below passes, with: the browser, the holder, the session id, the endpoint (the full DevTools
  websocket address and its port), the expiry, and the watch-only view link.
- `status <browser>` reports healthy only with positive proof, all of:
  - the session's service is active;
  - the endpoint named in a port file written after this session started answers, and the address it
    reports matches the file;
  - the active tab's page reports itself visible and focused;
  - the same page reports `navigator.webdriver` false: Chrome does not mark the browser as automated
    (Google refuses sign-in in a browser that reports automation, Q3);
  - the browser's user agent is not a headless one;
  - a fresh capture of the whole screen (Chrome's own tab strip and toolbar included, so a blank page
    still shows content) is not a single uniform color.

  A running browser that fails any of these is UNHEALTHY, naming each failed check. A browser with no
  session is BROWSER_DOWN. A missing or failed signal never counts as healthy.
- Every session starts Chrome with its automation marker turned off
  (`--disable-blink-features=AutomationControlled`). Since Chrome 155 a DevTools port alone makes every
  page report `navigator.webdriver` true, with no client attached (section 13). Pool browsers do not
  mark themselves as automated to any site, an agent driving one included, though a site can still
  tell automation by other means; SECURITY.md says so. A Chrome
  that no longer honors the flag fails the health proof's `webdriver` check, so every lease is refused
  until the operator decides, rather than a sign-in failing later.
- Health checks only read. They never navigate, never open, close or activate a tab, and never type,
  so a check cannot move an agent's focus or change its page. A screen capture is judged in memory and
  never written to disk.
- `status` with no browser lists every configured browser with its state (down, starting, healthy,
  unhealthy), holder, expiry, and whether a session is running past an expired lease. `status` never
  prints an endpoint: only `lease` hands one out, and only to the holder.
- `release <browser>` stops the session cleanly (S4) and frees the lease.
- Every lease, extension, takeover, start, stop (with how it ended), crash and failed start writes
  one session record: time, browser, holder, session id, event, and its details. So does a
  collision: a lease refused because a process the tool did not start has the browser's profile open
  or listens on its screen's ports (S4), and so does a stop that skips its DevTools close because it
  cannot show that a process of the session's service listens on the endpoint the port file names, or
  because the session's own stop handling finds no port file to close through (S4). A takeover is
  recorded once the old session is stopped or found not running, before the new session starts, so a
  new start that fails, or a lease command
  ended during it, cannot keep it off the record. Its record names no session of its own, only the
  previous holder, the previous session and how that session's stop ended. Every other stop the tool
  makes, of the holder's own session that no longer answered or of a session a start began (the one
  stopped before the retry of Q4 included), is recorded as a stop of that session with how it ended,
  `none` when nothing of it still ran. In a lease attempt, a stop that cannot show its session gone
  is not recorded as a stop: the failed start's record says so. A failed start's record names the
  session that failed to start, or none when the lease was refused before a session was started for
  it, and, for a takeover, the previous holder and session. A refusal that changes no lease and no
  session (such as BUSY, NOT_HOLDER, USAGE) writes none. A record never holds page content, page
  addresses, typed text, or images.

### S2. Signed in across sessions

The operator signs in to a browser's accounts once; agents then use that browser signed in.

Acceptance:

- Every session starts with its view, keeps it running (starting it again if it ever stops), and stops
  it with the session. Chrome starts only once the view's own websockify accepts connections on the
  view's port (another program's answer there does not count). A view that does not within 15 seconds,
  or whose websockify cannot be started at all (removed, or no longer executable, since setup), fails
  the start without the Q4 retry, BROWSER_DOWN naming the view and its port, and the start stops
  everything it started and frees the lease (S4). That answer needs a start limit longer than the
  screen's start plus those 15 seconds (the default 30 seconds is); under a shorter one the start
  limit ends the health proof first, and the start fails as any start that did not prove healthy
  (S4). A viewer attached when the view is started again is disconnected: noVNC 1.3.0
  then shows its Connect panel rather than connecting again by itself, and the viewer connects from
  there. `view <browser>` prints the watch-only link for a running session (BROWSER_DOWN when the
  browser has no session). A browser's view link is the same in every session (its port follows from
  the screen number), so the operator can bookmark it.
- `view <browser> --drive` prints an interactive link, and is refused with NOT_HOLDER unless the caller
  holds the lease. The operator signs in by leasing the browser under their own holder name, so no
  agent is driving while they type.
- The tool never asks for, receives, stores or types a password or a second-factor code. The operator
  signs in by hand in the drive view.
- After the operator releases the browser (a clean stop), an agent's new session on it is still
  signed in. This is checked for each platform at the operator's first sign-in on each browser, and
  the result is recorded in the operator's runbook (open question Q2).
- The endpoint `lease` returns belongs to the session, and an agent that attaches to it finds a
  headful browser. An agent driver that fails to attach and falls back to a browser of its own is for
  the agent to catch: the documentation tells agents to check the user agent before they act.
- Each browser's downloads land in its own owner-only directory. Files to upload are given as ordinary
  Linux paths.

### S3. Two holders, one browser

Acceptance:

- A lease attempt on a browser whose unexpired lease someone else holds returns BUSY with the holder
  and the expiry at once. It never waits for a start or stop in progress, and it does nothing to the
  browser.
- When two or more lease attempts start at the same instant, exactly one succeeds. (Race test: 8
  concurrent claimers, 200 rounds, zero double grants.)
- The lease is written before its session starts, so a second holder arriving during the start gets
  BUSY instead of starting a session of its own. A start that fails stops what it started, frees the
  lease and returns the failure. When what it started cannot be shown gone (S4), the lease is not
  freed: the start returns BROWSER_DOWN, and the holder keeps the lease, as with a release.
- A holder renews by leasing again: its own unexpired lease is extended, and its session and endpoint
  stay the same. `--minutes` sets the lease length, up to the configured maximum (defaults: 30
  minutes, at most 240). Fewer than 1 or more than the maximum is USAGE. A lease that starts a new
  session runs that length from the moment the start succeeds, however long the start took (the old
  session's stop in a takeover included), so it is never handed out partly used or already expired. A
  renewal that keeps its session, or a lapsed lease that resumes its session, runs it from the moment
  it is granted.
- A holder whose own lease lapsed, with no takeover since and its session still running and answering
  at its endpoint, leases again into the same session and endpoint. (Answering, not the full health
  proof: an agent's page may legitimately lose focus mid-run, and a restart would end its work.)
- Expiry stops nothing by itself: a session whose lease has expired keeps running until its browser is
  taken over or released.
- A lease attempt on a browser whose lease someone else held and let expire takes it over: the old
  session is stopped (S4) and a new one started, the previous endpoint stops answering, and the
  takeover is recorded with the previous holder.
- A takeover whose old session cannot be shown gone, or that is refused before that session is
  stopped or found not running, fails with BROWSER_DOWN, or USAGE for a problem in the environment
  (a unit state that cannot be read, a start lock that cannot be opened or locked), and gives the
  lease back to the previous holder as it was.
- A damaged lease file is reported as USAGE, naming the file and how to clear it. It is never treated
  as free (that could hand a browser to a second holder) or as BUSY (holders would retry forever). A
  lease file that names a browser other than its own is damaged too.
- Starts and stops of one browser run one at a time, under a lock of their own: a refusal (BUSY,
  NOT_HOLDER) never waits behind one. A lease attempt that is not refused (the browser is free, or the
  lease is the caller's own or has expired) waits for a start or stop in progress, at most 600
  seconds; past that it returns BUSY naming no holder, and the caller tries again later. A release by
  the lease's own holder also waits for a start or stop in progress, as long and with the same BUSY
  past that (its lease stays): a release already under way may yet fail and keep the lease. While
  either waits, it reads the lease again on every look, so a lease that changes hands meanwhile is
  refused at once (BUSY for a lease attempt, NOT_HOLDER for a release), without waiting for the start
  or stop to end.

### S4. Stops are clean and proven

Acceptance:

- Every stop first asks the session's own Chrome to close through DevTools (when the port file names
  its endpoint, see below) and waits for it to exit (a grace period, default 15 seconds). Only then is
  the session's service stopped by signal, and the service manager kills whatever is left after its
  stop timeout (default 30 seconds).
- A session's service answers a stop signal the same way: it asks Chrome to close through DevTools
  before anything else, so a stop from outside the tool (a stop by the service manager, as at an
  orderly shutdown) is clean whenever Chrome still answers. A stop signal that reaches the session
  while it is still starting is acted on between its steps (a wait for the screen or the view already
  under way runs to its end first): before Chrome starts, the session ends there with nothing to
  close, and its own stop record says `none`; once Chrome runs, the wait for Chrome's window ends at
  once, and the stop closes Chrome through DevTools as above.
- The close goes only to the session's own Chrome: an endpoint that a process of the session's service
  listens on. A Chrome the tool did not start that opens the profile after the start's collision check
  can write its own port file, and that endpoint gets no close and no signal. The stop records that it
  skipped the close and goes on to the service manager's stop of the session's own service, whose stop
  handling makes the same check, sends no close either, and ends its own Chrome by signal if it has
  not exited within the grace. When the tool cannot read the service's main process, it skips its own
  close the same way, and the session's stop handling still closes its own Chrome through DevTools.
  The session's stop handling also gives its own Chrome the grace when it finds no port file to close
  through (Chrome has not written it yet, or it was removed), and records that skipped close.
- Every stop records how it ended: `clean`, `signal`, or `kill`. A release that needed a signal or a
  kill still succeeds, and its result says that sign-in changes from the last minute or so may not
  have been saved.
- A session counts as gone only on positive evidence: its service is inactive and no process has the
  browser's profile open. An endpoint that does not answer is supporting evidence only, because on
  this host a closed port can either refuse a connection or drop it. A release frees the lease only
  once the session is shown gone; if that cannot be shown, the release fails and the holder keeps the
  lease.
- A Chrome that exits on its own (a crash) leaves the browser BROWSER_DOWN, and the session is
  recorded as crashed. The holder's next lease starts a new session, with a new endpoint.
- A new session starts only after any old port file is removed, and its endpoint is read only from a
  port file written after the session started. An old port file, or a session marker the tool keeps,
  that cannot be removed (a folder in its place, for example) refuses the start with USAGE naming it,
  and the lease is freed: nothing was started.
- A browser whose profile is open in a process the tool did not start (for example an agent driver
  launched straight on the profile directory) is refused with BROWSER_DOWN naming that process. The
  tool never deletes Chrome's own profile lock and never stops that process. Two starts of one
  profile never overlap, even from two configs that share the profiles folder: a start holds a lock
  beside the profile from before it stops an old session until the start ends, however it ends, and
  a start that finds the lock held is refused at once with BROWSER_DOWN. A start lock that cannot be
  opened or locked, a problem in the environment, refuses the start with USAGE.
- A browser whose screen is in use by a program the tool did not start (another config's session on the
  same screen number, another X server on that display, or another program on its ports) is refused
  with BROWSER_DOWN, naming the ports when they already answer as the lease starts. A session uses its
  screen only once its own X server reports that it owns the display, so two sessions starting on one
  screen at the same moment never share it. The tool never stops that program.
- A lease attempt refused before its start begins (a collision, another start of the profile under
  way, a start lock that cannot be opened or locked, or the holder's old session that cannot be
  shown gone) never takes away the holder's own lease: it stays, renewed and still naming its
  session. Only a lease the attempt itself made is freed. A lease command interrupted before its start
  begins (Ctrl-C) settles the lease the same way, and a takeover interrupted before the old session is
  shown gone gives the lease back to the previous holder as it was. Interrupted once its start began
  but before the session's service was asked to start (while the start records the session, writes
  the preferences and removes stale files), nothing runs: the lease is freed, as a failed start there
  frees it. Once the service was asked to start, the session may run: an interrupted lease command
  leaves the lease with its holder, naming that session, until a release, or a takeover once it
  lapses, shows the session gone. A kill cannot be handled at all.
- A start that is not proven healthy within the start limit (default 30 seconds) stops everything it
  started, frees the lease, and returns BROWSER_DOWN or UNHEALTHY with what failed, after the one
  retry of Q4 when Chrome never wrote its port file (none when the session reported that its view did
  not come up, S2). A start that ends in an error from the service manager once its service may run
  also stops everything it started and frees the lease, and returns that error: a call that gets no
  answer in time is BROWSER_DOWN, and a unit state that cannot be read is USAGE. A start that cannot
  show what it started gone makes no second try and frees nothing: it returns BROWSER_DOWN, saying
  what failed, and the holder keeps the lease until a release, or a takeover once it lapses, shows
  that session gone. The start limit bounds proving the session healthy, not the whole start: each
  call to the service manager has a limit of its own (30 seconds; the stop, the service stop timeout
  plus 15 seconds), and so do the session's own waits for its screen, its view and Chrome's window
  (each wait up to 15 seconds). A short start limit does not cut those short, so a start can take
  longer than its start limit, for example when the user's service manager does not answer.
- A session starts only through a lease: running the session's entry point by hand is refused with
  USAGE.

### S5. MCP parity

Acceptance:

- The agent commands (status, lease, release) are MCP tools with the same arguments, `--minutes`
  included. Results carry the same data and result codes, and errors surface as tool errors whose
  text ends with the code and the message, followed by the command's data as compact JSON when it has
  any (`CODE: message (data: {...})`; the MCP SDK puts words of its own first), so an agent sees what
  the command named, such as a collision's processes or the holder and expiry of a BUSY.
- Each MCP server process is exactly one holder, named by the server itself and never taken from the
  environment, so two agent sessions, each with its own server, never share a lease. Every agent that
  uses one server is that one holder, subagents included: a lease of a browser the server already
  holds renews it and returns the same endpoint. So parallel subagents of one agent session must lease
  different browsers, or use the command-line tool with a `--holder` each; the server's instructions
  say so.
- When the MCP server stops, it releases every lease it holds.
- No MCP tool reaches a browser except through the command-line tool. The server's instructions say
  that page content is untrusted data, never instructions.
- `setup` and `view` are operator commands and have no MCP tools.

## 7. Result codes

Every command supports JSON output carrying the code name, so agents never parse prose. The numbers
match orion-phone's, so an agent handles both tools the same way.

| Exit | Name | Meaning |
|---|---|---|
| 0 | OK | Done. |
| 1 | INTERNAL | An unexpected error inside orion-browser; the traceback goes to stderr. Never a success. |
| 2 | USAGE | Bad arguments, config or environment: a missing package, a damaged lease file, a path inside a git tree. |
| 3 | BUSY | Another holder has an unexpired lease, or a start or stop of the browser ran past the wait of a lease or a release (600 s). |
| 4 | NOT_HOLDER | The caller does not hold the lease: never held, released, or taken over. |
| 5 | (reserved) | Kept free so a number means the same thing in both tools. |
| 6 | BROWSER_DOWN | No session is running, or one could not be started, or could not be shown gone. |
| 7 | UNHEALTHY | A session is running but failed the health proof. |

## 8. Security model

Stated plainly, and repeated in SECURITY.md.

Defended:

- Collisions between holders (leases), and a stale holder driving a new holder's session: every new
  session is a new process with a new endpoint, and the old endpoint stops answering.
- Collisions between agents and the operator, by coordination: the tool hands the drive link only to
  the lease holder, and the operator signs in under a lease of their own. Watch-only is a setting in
  the link, not in the VNC server, so anyone on the host can still open an interactive view. The view
  accepts a connection only from its own page, so no other web page can connect to the screen, and it
  refuses to load inside another page's frame, so a page cannot frame it and steer a person's clicks
  or typing into it. Current Chrome also keeps a page that is not on the local host from framing it
  (its local network access checks: only a secure page the person has allowed to reach local devices
  gets through, and the view then refuses it), and the view's own guard refuses a page on the local
  host.
- The tool handling credentials: it never asks for, stores or types a password or code, and never
  exports, copies or prints cookies or profile contents.
- Personal data reaching the public repository (section 10).
- Exposure beyond the host: endpoints, VNC ports and view ports listen on loopback only.

Not defended:

- A local process of any OS user: it can connect to any session's endpoint or view without a lease,
  and drive any screen directly through its VNC port or its X server, neither of which checks who
  connects. One running as the operator's OS user can also read the profiles, whose cookies are
  protected only by Chrome's fixed "basic" key (unattended runs cannot unlock a keyring). Under WSL2's
  mirrored networking, processes on the Windows side share that loopback. Leases are coordination, not
  a security boundary.
- A browser that sends no Fetch Metadata. The view refuses to load in a frame, so a page cannot frame
  it, but it tells a load into a frame by the `Sec-Fetch-Dest` header the browser sends with it, and
  loads a request without one. Current Chrome, Firefox and Safari send it; in a browser that does not,
  a page can still frame the view and steer a person's clicks or typing into it.
- A page that opens the view in a window of its own (top level, which the view loads) and times a
  person's click onto it; open drive links only from the tool's own output.
- Anything an agent does while it holds a signed-in browser: it can post, send or buy as the
  operator. The tool records sessions, not actions.
- How sites see the pool browsers: Chrome runs with its automation marker turned off, so pages read
  `navigator.webdriver` as false even while an agent drives a browser through DevTools. This is
  deliberate (S1, Q3). A site can still tell automation by other means, and its terms on automated use
  still apply; what agents do there is the operator's to govern.
- Backups: a profile holds live sign-in state, so a backup of the profile directory holds it too.
- A work tree configured from elsewhere (`git --work-tree`, as some dotfiles setups use) escapes the
  git-tree check, which looks for a `.git` pointer file or a non-empty `.git` folder at or above each
  folder. An empty `.git` folder does not count: git itself would not call it a repository. The check
  follows a link at a profile or download folder itself, not links inside them (a profile's `Default`
  folder linked into a work tree, for example), and it is made as each start begins, so a link changed
  after that is seen only at the next start.

Operator policy, outside the tool: a pool browser signed in to publishing accounts bypasses the
publish skills' gates (the redactor, the caps, the arm flags) exactly as orion-phone's open phone
does, so the same rule applies. Post or send only when the operator's request for that session
explicitly asks; never from a timer, sweep, loop or nudge; run the lane's publish checks first; and
count the post toward the lane's cap. The operator's posting rule is extended to cover pool browsers
before the first signed-in lease.

## 9. Configuration and state

All outside the repository.

- Config: `~/.config/orion-browser/config.toml`, or wherever `ORION_BROWSER_CONFIG` points. It holds the
  Chrome path, the screen size (default
  1920x1080), the lease lengths, the stop grace, the service stop timeout (at least 10 seconds longer
  than the grace) and the start limit, and the browsers, each with a name, a screen number and, when a
  platform needs it, `restore_session` (Q2). Values outside sane ranges are refused as USAGE: leases up
  to a week, a screen from 640x480 to 7680x4320, a stop grace up to 300 seconds, and the service stop
  timeout and start limit up to 600 seconds. The start limit (`start_limit_seconds`) bounds proving a
  new session healthy; the service manager's calls (30 seconds each; the stop, the service stop
  timeout plus 15 seconds) and the session's own waits for its screen, its view and Chrome's window
  (each wait up to 15 seconds) have limits of their own, which a short start limit does not cut short
  (S4). The repository ships only `config.example.toml`.
- Profiles: `~/.local/share/orion-browser/profiles/<browser>/`, owner-only.
- State: `~/.local/state/orion-browser/`, owner-only: leases, session records, and each browser's
  downloads.
- These defaults come from the home directory alone, never from the XDG variables, which some
  launchers change: every caller must find the same lease store, or two callers could drive one
  browser. The home directory is HOME when it is set (it must then be an absolute
  path, or the config is refused as USAGE), and otherwise the account's home from the system user
  database, which must also be an absolute path; a `~` in a configured path expands to that same
  home. With no home directory and no `[paths]`, the config is refused as USAGE.
- The config's optional `[paths]` table moves state and profiles elsewhere (two separate folders,
  neither inside the other), and its `[view]` table names the noVNC web files (default
  `/usr/share/novnc`); the Chrome path defaults to `/usr/bin/google-chrome`.
- Ports, all on loopback: the endpoint is random per session; VNC is 5900 plus the screen number; the
  view is 6080 plus the screen number.
- Each session runs as a transient user service named for its browser and tagged with its lease store (a
  short hash of the state folder), so two lease stores never share a service (two configs that name one
  state folder, however spelled, share its store and its services). The screen, the view and Chrome all
  run inside it, so the view stops when the session does. A stop signals the session's own process first
  (KillMode=mixed), so it can close Chrome through DevTools before anything else is ended.
- orion-browser expects one lease store per OS user. A second config under the same OS user (a test
  config with its own `[paths]`, for example) has a lease store and services of its own, but its
  browsers still meet the first's on any screen, port or profile they share, so they must use screens
  and profiles the first does not use. If they do meet, a lease that would share a screen or a profile
  with a running session is refused with BROWSER_DOWN, also when both start at the same moment; it never
  stops the running session.

## 10. Repository hygiene (public-ready from the first commit)

- MIT license.
- Test data is synthetic only: fakes of the service manager, Chrome, the VNC display server,
  websockify, xdotool and xwd on PATH, replaying hand-written behavior. No screenshot, screen capture,
  profile, cookie database, session record, HAR file or config from a real browser is ever committed:
  a test accepts only the kinds of file the repository holds (source, tests, Markdown, TOML, workflow
  files and images under `docs/assets/`), and refuses Chrome profile files, SQLite files, symbolic
  links, nested repositories (gitlinks), and any file that is not UTF-8 text.
- `.gitignore` excludes profiles, state, images, config, Chrome profile files, cookie databases and HAR
  files, except documentation images; a test checks that it covers every artifact suffix and profile
  file name the hygiene test refuses.
- CI runs a secret scanner on every push and pull request. Third-party actions are pinned to commit
  SHAs.
- No long dashes (U+2013, U+2014, U+2015) in any file, enforced by a test.
- Documentation images come from the `anon` browser, signed in to nothing.
- Before the repository is made public: a full-history scan for secrets and for the operator's
  personal identifiers returns zero findings, and the review is recorded.

## 11. Test seams

Decision: one primary seam, the command-line boundary (exit code plus JSON), with fakes on PATH for
the service manager, Chrome, the VNC display server, websockify, xdotool and xwd. The fake Chrome writes
a port file and answers the few DevTools calls the tool makes: its version, the page list, an
evaluation, and close. Its pages report `navigator.webdriver` as Chrome 155 does: true whenever it
runs with a DevTools port, unless started with the automation marker turned off. S1 through S5 are
all exercised there, and MCP parity tests call the MCP tools over the same fakes.

One secondary seam: a real-browser smoke test the operator runs locally, not in CI. It writes a config of
its own with one browser, `smoke`, on screen 89. The operator's config must not use that name, that screen
or its ports, so a run cannot stop or take over an operator's session, and a run refuses to start while a
`smoke` session is active. It leases `smoke`, attaches agent-browser, proves the active tab visible,
focused, headful and not marked as automated (`navigator.webdriver` false), sets a long-lived cookie,
releases, leases again, finds the cookie, and confirms the old endpoint no longer answers. It also
checks the view's frame guard and WebSocket origin, and that no stop skipped its DevTools close.

## 12. Constraints and dependencies

- A Linux host with a user instance of systemd. Commands reach it through XDG_RUNTIME_DIR, or through
  /run/user/<uid> when a launcher leaves the variable out and that folder is the user's own. WSL2 on
  The original WSL2 host qualified (checked 2026-10-07: systemd 255, `systemd-run --user` available).
- Google Chrome stable from Google's own package repository (checked: 150.0.7871.46 installed), not
  a snap build of Chromium. `setup` and `status` give Chrome's version, the age of its build and
  whether it is outdated: older than eight weeks, the age at which Chrome counts itself out of date.
  `status` gives an outdated build a note for the operator; only `setup` gives the command that updates
  Chrome, because keeping Chrome current is the operator's and agents never update it. Nothing is
  refused over it.
- From the distribution's own archive (Ubuntu 24.04, versions checked 2026-10-07):
  tigervnc-standalone-server 1.13.1, novnc 1.3.0, websockify 0.10.0, x11-apps 7.7 (xwd, for screen
  captures), and xdotool (already installed, for focus).
- Python 3.11 or later.
- Runtime Python dependencies: `mcp`, the official Model Context Protocol Python SDK, as in
  orion-phone; and `websockets`, because DevTools speaks only over a websocket and the standard
  library has no websocket client. Confirm each package's official source before adding it. No other
  runtime dependency without a recorded reason.

## 13. Evidence from the spikes (2026-10-06 and 07) and the operator's checks (2026-10-11)

Throwaway runs on the development host with scratch profiles, recorded so planning starts from facts:

- Headful Chrome runs on a virtual screen. Its page reports `visibilityState` `visible`,
  `navigator.webdriver` `false`, and a headful user agent (Chrome 150; see the Chrome 155 entries).
- `--remote-debugging-port=0` gives a random port per session, which Chrome writes to the port file.
  After a restart, the old port refused connections.
- Chrome leaves the port file behind when it exits: a start that does not remove it first reads the
  previous session's port.
- Ending Chrome with SIGTERM lost cookies set a moment before: none were on disk afterwards. Ending it
  with the DevTools `Browser.close` command saved both a long-lived and a session-only cookie.
- With no window manager, the page reported `document.hasFocus()` `false` after Chrome's X windows were
  given input focus by class; giving focus to the top-level window, found by its title, made it `true`
  (Q1).
- Without an explicit window size, Chrome filled about 1050x793 of a 1440x900 screen. `--window-size`
  fixed it.
- Chrome 155 (2026-10-11, with the operator): a DevTools port alone makes every page report
  `navigator.webdriver` true. Read with no DevTools client at all (a `data:` page writing the value
  into its window title, on a scratch screen): no port gave false, `--remote-debugging-port=0` gave
  true, and the port with `--disable-blink-features=AutomationControlled` gave false. Google refused a
  sign-in typed in the drive view of a browser reporting true ("This browser or app may not be
  secure", both its web sign-in and Chrome's own sign-in), and accepted the web sign-in in a browser
  with no port and in one with the port and the flag.
- Chrome 155 leaves the port file behind after a close through DevTools too (a real stop of a
  session's service by the service manager, 2026-10-11).
- Chrome 155 moves its browser process into a systemd scope of its own; the processes it starts stay in
  the session's service. The tool finds Chrome by process tree, so its checks hold. When a session's
  runner was killed outright, the service manager stopped the screen and Chrome ended with it within
  2 seconds (an unclean end, as for any kill).
- From the operator's runbook: a failed agent-browser attach can leave later commands running in a
  headless fallback browser of the driver's own.

## 14. Open questions (resolve during planning)

- Q1. Resolved 2026-10-07: X input focus on Chrome's top-level window, found by its title and given
  focus with xdotool, with no window manager (section 13). Confirmed 2026-10-08 by the smoke test,
  after two fixes it called for. Chrome starts on an empty `data:` page: started on `about:blank`, it
  keeps the keyboard focus in its address bar, and the page never has it. And Chrome starts with its
  outdated-build check turned off: a build older than Chrome's eight-week outdated age opens a "Can't
  update Chrome" bubble a moment after it starts, which takes the focus. Keeping Chrome current is the
  operator's: a pool browser no longer shows that bubble, so `setup` and `status` warn instead
  (section 12).
- Q2. Session-only cookies. Does any platform the operator uses (X, LinkedIn, YouTube Studio, TikTok,
  Instagram, Facebook and Meta Business Suite, Stripe, Search Console) sign out across a clean stop
  and start with Chrome's default startup setting? If one does, the fix is to turn on session restore
  for that browser's profile, as configuration. Checked 2026-10-11 on Chrome 155: `restore_session`
  true shows "Continue where you left off" in Chrome's start-up settings, and set back to false shows
  "Open the New Tab page"; Chrome keeps the value the tool writes. Each platform's result is recorded
  in the runbook at the operator's first sign-in.
- Q3. Resolved 2026-10-11: Google refuses a sign-in in a browser that reports `navigator.webdriver`
  true, which since Chrome 155 is any browser with a DevTools port, and accepts one with the automation
  marker turned off (section 13). Every session now starts with it off, and the health proof checks it
  (S1). Sign in on the site's own page, never through Chrome's own sign-in and sync (runbook).
- Q4. Resolved 2026-10-07: a start whose Chrome never writes its port file is stopped and started once
  more, on a new random port (a collision with a listener on the Windows side under mirrored networking
  would look like that); any other failed start, a view that did not come up included (S2), is
  reported at once.
- Q5. Automated part resolved 2026-10-08 by the smoke test: the distribution's noVNC 1.3.0,
  websockify 0.10.0 and TigerVNC 1.13.1 work together. The view's page loads, and the view takes a
  connection from its own page through to the screen and refuses one from another site's page. On
  WSLg, where `/tmp/.X11-unix` is read-only, Xvnc listens on its abstract socket only, and Chrome
  and the tools reach the screen there. Resolved 2026-10-11 by the operator's own look: the drive view,
  opened from Chrome on Windows, fills with the pool browser's window, and clicks and typing reach it.
  The view's framing guard (section 8) stays, by the operator's decision of the same day.
