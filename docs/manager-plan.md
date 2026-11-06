# The manager: plan

A single web page served by opsbox that does everything setting up and running
the box needs, so nobody has to `docker exec` or `docker attach`:

- sign in to Claude, and sign out;
- start, stop and restart the Remote Control server, see its output, and answer
  its one-time "trust this directory" question;
- add, replace and remove the box's keys (kubeconfig, talosconfig, GitHub
  token and the rest) and its environment settings.

It replaces two manual steps: `docker exec -it … claude auth login`, and
`docker attach` to answer the trust question.

## How sign-in works

On a machine with no browser, `claude auth login` prints a link and waits
(checked in the image, 2026-11-06):

```
If the browser didn't open, visit: https://claude.com/cai/oauth/authorize?…
Paste code here if prompted >
```

1. The manager runs `claude auth login` in a pseudo-terminal and reads the link.
2. The page shows **Sign in to Claude**, which opens that link in a new tab.
3. You sign in on claude.ai itself, 2FA included. claude.ai then shows a code.
4. You paste the code into the page, and the manager types it into the waiting
   CLI.
5. Once `claude auth status` reports a claude.ai sign-in, the manager starts
   Remote Control.

**Your claude.ai password and 2FA never touch opsbox.** It only relays the link
and the one-time code. The code is useless without the verifier the CLI keeps,
because the sign-in uses PKCE.

**That same sign-in is how a browser gets into the page.** The page has no
accounts or passwords of its own:

- **While the box is signed out,** the first sign-in is the box's own. That
  browser gets a session, and Remote Control starts.
- **Once the box is signed in,** a new browser signs in to Claude the same way,
  but into a throwaway `CLAUDE_CONFIG_DIR`. It gets a session only if that
  account (email and organisation) is the box's. The throwaway sign-in is then
  logged out and deleted. The box's own sign-in, and a running Remote Control
  session, are never touched by a browser signing in.
- **A sign-in belongs to the browser that started it.** Only that browser sees
  the link and can finish it, so nobody else on the LAN can finish your first
  setup with their own account.

## What it manages

**Remote Control.** The manager becomes the container's main process (under
tini) and runs `claude remote-control` as its child, in a pseudo-terminal:

- When the server exits (it does after about 10 minutes offline), the manager
  starts it again, with backoff. The restart policy only matters if the manager
  itself dies.
- Its whole output, the manager's own notes included, streams to a terminal on
  the page (xterm.js). The last 256 KiB is kept for a page that opens later.
- When the trust question appears, the page shows it with a **Trust** button,
  and never answers it on its own.
- Start, Stop and Restart buttons.

**Keys.** A fixed catalogue. Each entry has one place on disk, at mode 0600, and
**is write-only**: the page shows whether a key is set and what it
authenticates as, never its contents.

| Key | Stored as | The page shows |
| --- | --- | --- |
| Kubernetes | `~/.kube/config` | context, server, and user name |
| Talos | `~/.talos/config` | context and endpoints |
| GitHub | given to `gh auth login --with-token` | the account, from `gh auth status` |
| Terraform registry | `~/.terraform.d/credentials.tfrc.json` | the host |
| Local LLM | `~/.config/local-llm/key`, plus `LOCAL_LLM_KEY_FILE` | set or not |
| SSH | **generated on the box** (ed25519) | the public key, with a Copy button, and its fingerprint. The private key never leaves the box |

Each key can be **Replaced** (a paste box that opens in place) or **Removed**
(danger, and the confirm says what survives). A pasted value is checked before it's
saved: a kubeconfig must parse and name a context, for example.

**Environment.** An allowlist, stored in `~/.config/opsbox/env` and applied by
restarting Remote Control:

- `CLAUDE_REMOTE_CONTROL_SESSION_NAME_PREFIX`
- `DOCKER_HOST`
- `TZ`
- `GIT_AUTHOR_NAME` and `GIT_AUTHOR_EMAIL`
- `HTTPS_PROXY` and `NO_PROXY`

Anything else is refused. These are refused with the reason, because each one
turns Remote Control off or replaces the sign-in: `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`,
`DISABLE_GROWTHBOOK`, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` and
`CLAUDE_CODE_OAUTH_TOKEN`.

## Who can use it

This page is the front door to the box's keys, so:

- **A browser needs a session,** and gets one only by signing in to Claude as
  the box's account (above). The session is a random token in an HttpOnly,
  SameSite=Strict cookie; the box stores only its SHA-256, for 30 days. A
  different browser signs in again. The page can sign out this browser, every
  browser, or the box itself.
- **Plain HTTP, on port 8080.** Claude's sign-in works without HTTPS: the link
  opens on claude.ai, and only the one-time code comes back. But the code and
  the session cookie then cross the network unencrypted, which is fine on a
  trusted LAN and the reason to put HTTPS in front (a reverse proxy) anywhere
  else.
- **Requests:** a CSRF token on every POST, including the sign-in forms, which
  use a pre-session cookie. State only changes on POST. A Host header that isn't
  an IP address or a name in `OPSBOX_MANAGER_HOSTS` is refused, which defeats
  DNS rebinding.
- **Without a session the page shows only the sign-in:** not the account, the
  state, the keys or the console.
- **It runs no commands of its own choosing**, has no shell, and never displays
  a stored secret.
- **`remote-control`** as the container's command still starts Remote Control
  without the manager, as before.

## How it's built

- **Python standard library,** plus Debian's `python3-yaml` to read kubeconfigs:
  `http.server`, `pty`, `secrets` and `hmac`. CodeQL reads all of it.
- **The terminal is xterm.js,** vendored at build time with `npm ci
  --ignore-scripts` from a lockfile in `ui/`, which checks every package's hash
  and runs none of their install scripts. Dependabot keeps it current.
- **Location:** `rootfs/usr/local/lib/opsbox/manager/`, started by
  `opsbox-manager`. The entrypoint's default command becomes `manager`.
- **One page, rendered on the server**, built from plain HTML forms that work
  without JavaScript. A little vanilla JavaScript adds the live terminal, Copy
  buttons and a refresh when the state changes. Replace and the confirms are
  `<details>`, so they need no JavaScript and no second route. The CSP allows no
  inline script or style, so both are files. Dark mode follows
  `prefers-color-scheme`.

### UI rules (from Stocktake's `docs/contributing/style.md`)

- **One line at a control.** Nothing at all for what people already understand.
- **Every action is a button.** A link that acts is drawn as one (`a.button`),
  and never as a button inside a link.
- **One primary per form,** and `.secondary` for everything else. `.danger`
  destroys something, and its confirm says what survives.
- **Errors say what to do next.**
- **Status is never carried by colour alone:** each state also has a word and a
  symbol.
- **It works at phone width and on a desktop.** Everything fits at 412 px, with
  nothing scrolling sideways.

## Tests

- **Unit tests,** with a fake `claude` that prints the link and waits for a code,
  prints the trust question, and exits on cue. They cover sign-in, restart with
  backoff, the trust button, and sign-out.
- **Access:**
  - the first sign-in signs the box in and starts Remote Control;
  - a second browser signed in as a different Claude account is refused, and
    its throwaway sign-in is deleted either way;
  - a sign-in can't be finished from another browser;
  - a POST without its CSRF token is refused, the sign-in forms included;
  - a foreign Host header is refused;
  - every route except sign-in needs a session.
- **Write-only keys:** every key is set to a sentinel value, then every route is
  fetched. The test fails if the sentinel appears in any response.
- **Hostile input,** after Stocktake's walker: every field is sent NUL, 5000
  characters, markup and `NaN`. The test fails on a 500, on markup echoed back
  unescaped, and on anything written outside the key's own file.
- **Visual:** headless Chrome at 1400 px and 412 px, sized by an iframe rather
  than the window (headless Chrome clamps windows to 500 px). It covers each
  state: signed out, signing in, running, trust asked, and keys. It fails on
  sideways scrolling, on an element past the edge, and on text outside its box.
- **Every test has a named mutation that breaks it** (the
  `tests-that-can-fail` rule).

## Decisions (Jarrod, 2026-11-06)

1. **Sign-in to the page:** Claude's own sign-in, as above. No accounts,
   passwords or login database of its own.
2. **Transport:** plain HTTP on 8080. HTTPS is a proxy in front, for anyone
   who wants it.
3. **Version:** a patch. By the scheme this is a major (no more `docker
   attach`, a new default command, a new port), but nobody has consumed opsbox
   yet, so it ships as a patch and the PR says why.

## Order (Qwen drafts each, as in the qwen-spec skill)

1. The process core: the pseudo-terminal driver for sign-in and Remote Control,
   with the fake-`claude` tests.
2. Browser sessions and CSRF, with their tests.
3. The key catalogue and the environment allowlist, with the write-only test.
4. The routes, then the page: HTML, CSS and JavaScript.
5. Into the image: the entrypoint, the Dockerfile, the template and compose (port
   8080), and the README.
6. The hostile-input and visual tests, and their CI job.
