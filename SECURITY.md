# Security model

orion-browser coordinates agents; it is not a sandbox. Stated plainly, as in section 8 of the specification.

## Defended

- Collisions between holders (leases), and a stale holder driving a new holder's session: every new session is a new
  Chrome process with a new endpoint, and the old endpoint stops answering.
- Collisions between agents and the operator, by coordination: the tool hands the drive link only to the lease
  holder, and the operator signs in under a lease of their own. Watch-only is a setting in the link, not in the VNC
  server, so anyone on the host can still open an interactive view. The view accepts a connection only from its
  own page, so no other web page can connect to the screen, and it refuses to load inside another page's frame, so
  a page cannot frame it and steer a person's clicks or typing into it. Current Chrome also keeps a page that is not
  on the local host from framing it (its local network access checks: only a secure page the person has allowed to
  reach local devices gets through, and the view then refuses it), and the view's own guard refuses a page on the
  local host.
- The tool handling credentials: it never asks for, stores or types a password or a code, and never exports, copies or
  prints cookies or profile contents.
- Personal data reaching this repository: test data is synthetic, and a hygiene test refuses artifacts, real email
  addresses and long dashes.
- Exposure beyond the host: endpoints, VNC ports and views listen on loopback only.

## Not defended

- A hostile page forging its own page properties. Health checks avoid page-controlled JSON serialization,
  but visibility, focus and automation properties remain diagnostic signals, not security attestation.
- A local process of any OS user: it can connect to any session's endpoint or view without a lease, and drive any
  screen directly through its VNC port or its X server, neither of which checks who connects. One running as the
  operator's OS user can also read the profiles, whose cookies are protected only by Chrome's fixed "basic" key
  (unattended runs cannot unlock a keyring). Under WSL2's mirrored networking, processes on the Windows side share
  that loopback. Leases are coordination, not a security boundary.
- A browser that sends no Fetch Metadata. The view refuses to load in a frame, so a page cannot frame it, but it
  tells a load into a frame by the `Sec-Fetch-Dest` header the browser sends with it, and loads a request without
  one. Current Chrome, Firefox and Safari send it; in a browser that does not, a page can still frame the view and
  steer a person's clicks or typing into it.
- A page that opens the view in a window of its own (top level, which the view loads) and times a person's click
  onto it; open drive links only from the tool's own output.
- Anything an agent does while it holds a signed-in browser: it can post, send or buy as the operator. The tool records
  sessions, not actions. Put a publishing rule in front of agents that hold publishing accounts.
- How sites see the pool browsers: Chrome runs with its automation marker turned off, so pages read
  `navigator.webdriver` as false even while an agent drives a browser through DevTools. This is deliberate:
  since Chrome 155 a DevTools port alone sets it, and Google refuses sign-in in a browser that reports
  automation. A site can still tell automation by other means, and its terms on automated use still apply; what
  agents do there is the operator's to govern (above).
- Backups: a profile holds live sign-in state, so a backup of the profile directory holds it too.
- A work tree configured from elsewhere (`git --work-tree`, as some dotfiles setups use) escapes the check that
  keeps state and profiles out of git trees, which looks for a `.git` pointer file or a non-empty `.git` folder at
  or above each folder. An empty `.git` folder does not count: git itself would not call it a repository. The check
  follows a link at a profile or download folder itself, not links inside them (a profile's `Default` folder linked
  into a work tree, for example), and it is made as each start begins, so a link changed after that is seen only at
  the next start.

## Reporting

Report a vulnerability privately to the repository owner through GitHub's private vulnerability reporting.
