# opsbox

A container image that runs [Claude Code Remote
Control](https://code.claude.com/docs/en/remote-control) with a homelab
operations toolchain. Start a session from the Claude app on your phone, and
Claude works on a box that has your kubeconfig, talosconfig, SSH keys and
repositories, not on your laptop.

Unofficial; not affiliated with Anthropic.

## What's in it

| Tool | Where it comes from | How it stays current |
| --- | --- | --- |
| kubectl, talosctl, Terraform, TFLint, terraform-docs, uv, Node.js and npm, the Docker CLI with buildx and compose, cilium-cli, restic, ShellCheck, mise | Each project's own image, pinned by digest | Dependabot pull requests |
| Claude Code (stable channel), gh | The vendor's signed apt repository | The weekly release |
| Helm 4 | The official tarball from get.helm.sh, checked against its published sha256 | The weekly release |
| git, ssh, curl, jq, make, Python 3 with PyYAML, psql, sqlite3, tmux, dig, ping, nc, rsync and GNU coreutils | Debian 13 (trixie) | The weekly release, which also picks up Debian security fixes |

`docker run --rm ghcr.io/jarrodruggiero/opsbox opsbox-versions` lists the exact
versions in an image. Every release also attaches that list as `versions.txt`.

There is no Docker engine, no sudo and no root at run time. Everything runs as
`opsbox` (uid 1000).

## Running it

[`compose.yaml`](compose.yaml) is a complete example. Two steps are needed
once, after the first start:

1. **Sign in:** `docker exec -it opsbox claude auth login`, and follow the link
   it prints. Remote Control needs a claude.ai sign-in on a Pro, Max, Ultra,
   Team or Enterprise plan. A `claude setup-token` token or an API key can't
   start Remote Control sessions.
2. **Trust the workspace:** `docker attach opsbox`, answer the question about
   trusting `~/workspace`, then detach with Ctrl-P Ctrl-Q.

The box then shows up under Remote Control in the Claude app and at
claude.ai/code, and new sessions start in `~/workspace`.

If the network drops for more than about 10 minutes, `claude remote-control`
exits, and the restart policy brings it back.

### Your keys

Everything personal lives in the volume at `/home/opsbox`, and none of it is
in the image:

- **Claude Code:** `~/.claude`, `~/.claude.json`
- **SSH:** `~/.ssh`
- **kubectl:** `~/.kube/config`
- **talosctl:** `~/.talos/config`
- **gh:** `~/.config/gh`, set up with `gh auth login`
- **Terraform:** `~/.terraform.d/credentials.tfrc.json`
- **Repositories:** clone them into `~/workspace`

The volume must be owned by uid 1000. Back it up, and protect it like the keys
it holds.

Give the box the narrowest credentials that do the job. Anything that can read
your kubeconfig can do what your kubeconfig can do, and Claude can read it.

### Pinned tool versions in a repository

When a repository pins its tools in `.mise.toml` (Terraform, say), run this
once in it:

    mise trust && mise install

From then on, that repository gets its pinned versions, and everywhere else
gets the baked ones. Do this before running Terraform in a repository whose CI
uses an older version than the image. A newer Terraform can write state that
an older one isn't guaranteed to read.

`mise trust` is per repository on purpose: a trusted `.mise.toml` can run
commands, so don't trust one you haven't read.

### Network

Give the box its own address, and allow outbound traffic only where it needs to
go:

- **Claude Code:** the hosts in [its network
  requirements](https://code.claude.com/docs/en/network-config#network-access-requirements).
  At minimum that's `api.anthropic.com`, `claude.ai`, `claude.com` and
  `platform.claude.com` on 443.
- **Your tools:** GitHub (`github.com`, `api.github.com`, `ghcr.io`), any
  registries you pull from, and your own Kubernetes API, Talos API and SSH
  hosts.

Don't set `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC` or `DISABLE_GROWTHBOOK`:
either one turns Remote Control off.

### Docker

The image has the Docker CLI but no engine. To build images from the box, give
it an engine of its own: a VM, or a rootless engine. Never give it the host's
socket, because the Docker socket is root on the machine it belongs to.

## Versions

Releases are automatic. Every merge to `main` that changes the image produces
one, and so does the weekly build when a floating tool or a Debian package has
moved. The version says how much changed:

- **Major:** a big refactor of the image, chosen by raising the number in
  [`MAJOR_VERSION`](MAJOR_VERSION).
- **Minor:** a tool had a major update, or a tool was added or removed. Either
  can break a script that uses it.
- **Patch:** anything else. Minor and patch updates to tools, Debian package
  updates, and changes to the image's own files.

A tool's major version is its first number, except for 0.x versions, where it
is the second, and calendar versions such as mise's `2026.10.25`, where it
never counts. [`scripts/next-version.py`](scripts/next-version.py) applies
these rules, and its tests pin them.

Each release `vX.Y.Z` is published as `X.Y.Z`, `X.Y`, `X` and `latest`. To get
updates without breaking changes, follow `X`.

## Changing it

- **To add a tool with an official image:** give it its own
  `FROM <image>:<tag>@sha256:<digest> AS <name>` stage, copy its binary out, and
  add it to `TOOLS` in
  [`opsbox-versions`](rootfs/usr/local/bin/opsbox-versions). Dependabot only
  reads FROM lines, so a tool pinned anywhere else never gets updated.
- **To test the scripts:** `python3 -m pytest tests`.
- **To test a built image:** `docker build -t opsbox:dev .`, then
  `OPSBOX_IMAGE=opsbox:dev python3 -m pytest tests/test_image.py`.

## Licence

[AGPL-3.0](LICENSE).
