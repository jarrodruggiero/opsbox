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

## What it manages

**Remote Control.** The manager becomes the container's main process (under
tini) and runs `claude remote-control` as its child, in a pseudo-terminal:

- When the server exits (it does after about 10 minutes offline), the manager
  starts it again, with backoff. The restart policy only matters if the manager
  itself dies.
- It keeps the last 500 lines of output for the page.
- When the trust question appears, the page shows it with a **Trust** button,
  and never answers it on its own.
- Start, Stop and Restart buttons.

**Keys.** A fixed catalogue. Each entry has one place on disk, at mode 0600, and
**is write-only**: the page shows whether a key is set and what it
authenticates as, never its contents.

| Key | Stored as | The page shows |
| --- | --- | --- |
| Kubernetes | `~/.kube/config` | context, server, and user name |
| Talos | `~/.talos/config` | context, endpoints, and role |
| GitHub | given to `gh auth login --with-token` | the account, from `gh auth status` |
| Terraform registry | `~/.terraform.d/credentials.tfrc.json` | the host |
| Local LLM | `~/.config/local-llm/key`, plus `LOCAL_LLM_KEY_FILE` | set or not |
| SSH | **generated on the box** (ed25519) | the public key, with a Copy button, and its fingerprint. The private key never leaves the box |

Each key can be **Replaced** (a dialog with a paste box) or **Removed** (danger,
and the confirm says what survives). A pasted value is checked before it's
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

- **HTTPS only, on port 8443.** The manager makes a self-signed certificate at
  first start, and prints its fingerprint in the container log, so you can
  check it the first time your browser asks.
- **The first visitor can't claim it.** First start prints a one-time setup
  code in the container log, which only someone who can read that log sees.
  The code sets a password, stored as an scrypt hash, and then stops working.
  Resetting means deleting `~/.config/opsbox/manager`, which prints a new code.
- **Sessions:** a cookie that is HttpOnly, Secure and SameSite=Strict, lasting 12
  hours.
- **Requests:** a CSRF token on every POST. Five sign-in attempts a minute.
  State only changes on POST.
- **It runs no commands of its own choosing**, has no shell, and never displays
  a stored secret.
- **The router should still allow 8443 only from your own devices.**
- **`OPSBOX_MANAGER=off`** starts Remote Control without the manager, as today.

## How it's built

- **Python standard library only:** `http.server`, `ssl`, `pty`,
  `hashlib.scrypt`, `secrets` and `hmac`. There's nothing to pin or update,
  and CodeQL reads all of it.
- **Location:** `rootfs/usr/local/lib/opsbox/manager/`, started by
  `opsbox-manager`. The entrypoint's default command becomes `manager`.
- **One page, rendered on the server**, built from plain HTML forms that work
  without JavaScript. A little vanilla JavaScript adds Copy buttons, live
  status polling, and `<dialog>`s for the Replace forms. Each dialog includes
  the same form its standalone route renders, with a `<noscript>` link to it.
  CSS is inline, and dark mode follows `prefers-color-scheme`.

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
  - the setup code works once;
  - a wrong password is refused, and the rate limit holds;
  - a POST without its CSRF token is refused;
  - the cookie carries the right flags;
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

## Decisions needed

1. **Sign-in to the page:** a password set with the one-time code from the log
   (recommended), or passkeys from the start (more work; Stocktake has them).
2. **Certificate:** self-signed and made by the box (recommended, because it
   works for anyone who runs the image), or one from your internal CA.
3. **Version:** by the scheme, new image files are a patch. If this counts as a
   big refactor (no more `docker attach`, a new port), raise `MAJOR_VERSION` to 2.

## Order (Qwen drafts each, as in the qwen-spec skill)

1. The process core: the pseudo-terminal driver for sign-in and Remote Control,
   with the fake-`claude` tests.
2. Access: the setup code, the password, sessions, CSRF and the rate limit, with
   their tests.
3. The key catalogue and the environment allowlist, with the write-only test.
4. The page: HTML, CSS and JavaScript, and the routes.
5. Into the image: the entrypoint, the Dockerfile, the template and compose (port
   8443), and the README.
6. The hostile-input and visual tests, and their CI job.
