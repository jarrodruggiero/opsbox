# opsbox: Docker image for running Claude Code Remote Control on a small
# always-on machine. Keys (kubeconfig, talosconfig, SSH keys, gh and Claude
# sign-ins) live ONLY in the volume mounted at /home/opsbox, never in the
# image. The image is public: no hostnames, IPs or usernames other than
# opsbox.
#
# Tools come from three sources: (1) each tool's own official image, copied
# by a multi-stage build so Dependabot can bump it; (2) signed vendor apt
# repos for gh and Claude Code; (3) the Helm official tarball.
#
# A tool's image comes from its own project's registry where it publishes
# one (ghcr.io, quay.io, registry.k8s.io). The rest are Docker Hub's, named
# as docker.io/... so that Dependabot reads Docker Hub's full list of tags:
# mirror.gcr.io only lists the tags someone has already pulled through it.
# CI and releases still pull them through mirror.gcr.io, as a BuildKit
# mirror, because Docker Hub rate-limits GitHub's shared runners.

# Each tool gets its own FROM stage so Dependabot, which reads FROM lines
# only, can bump it. An image named directly in COPY --from=<image> would
# never be updated.
FROM registry.k8s.io/kubectl:v1.37.2@sha256:fe049faa65ae483ac200009a949a68fa4c9985daea12c9cb1195c9c7965301fc AS kubectl
FROM ghcr.io/siderolabs/talosctl:v1.14.5@sha256:0b852e35c1f513ee5eacc22183d6193486012420569fde39fcd5260c51938443 AS talosctl
FROM docker.io/hashicorp/terraform:1.17.1@sha256:a5d5a5a892c98f60fcbfa81eac2a40fc19e7b3fa7719bccae10ff6111d22524a AS terraform
FROM ghcr.io/terraform-linters/tflint:v0.65.0@sha256:2788f4d4fc663c0e953b011b01bf443fe5bfce7587ee916e588e6057b85d6359 AS tflint
FROM quay.io/terraform-docs/terraform-docs:0.20.0@sha256:37329e2dc2518e7f719a986a3954b10771c3fe000f50f83fd4d98d489df2eae2 AS terraform-docs
FROM ghcr.io/astral-sh/uv:0.13.7@sha256:96f40d3904aad8d0bd010539001d36c8e2188b981e1334471a90c4a1d93bb490 AS uv
FROM docker.io/library/node:26.13.0-trixie-slim@sha256:e4db4c914bf8ad241bd24804fc25d272d89bad97e68cdc8a1414862790a02c39 AS node
FROM docker.io/library/docker:29.9.2-cli@sha256:5e6baf67325a79f64ed3bed6863954d5e05d0f3d4e57cbdb14809346814bbfdb AS docker
FROM quay.io/cilium/cilium-cli:v0.20.2@sha256:40597ffa6477095c547d24b617d33b5a5e33395e1872c26d9ce8d09feef67d64 AS cilium
FROM ghcr.io/restic/restic:0.19.1@sha256:2f0373803493361f9304a57150d464677f69a9dad487afec202105aafb2592f2 AS restic
FROM docker.io/koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d AS shellcheck
FROM ghcr.io/jdx/mise:2026.10.25@sha256:91417f41840316b7c5da6854bef393eccea4546ae70f8baf0803e0e94287b68d AS mise

FROM docker.io/library/debian:trixie-slim@sha256:f6b08b09a05725f9431502d3b5714f5c4576e82c1a84fa1debe0c24a7e0c3ec4
ARG TARGETARCH
# CHECKPOINT_DISABLE stops Terraform (and the HashiCorp tools) calling home
# on every run.
ENV LANG=C.UTF-8 CHECKPOINT_DISABLE=1

# Debian packages needed by the tools and the homelab workflow. libatomic1
# is for node, whose own image has it in its base. A rebuild picks up Debian
# security fixes before the base image's digest moves.
RUN set -eu; \
    DEBIAN_FRONTEND=noninteractive apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get -y upgrade; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        bash-completion \
        bind9-dnsutils \
        ca-certificates \
        curl \
        file \
        gawk \
        git \
        iproute2 \
        iputils-ping \
        jq \
        less \
        libatomic1 \
        make \
        netcat-openbsd \
        openssh-client \
        openssl \
        postgresql-client \
        procps \
        python3 \
        python3-yaml \
        rsync \
        sqlite3 \
        tini \
        tmux \
        tzdata \
        unzip \
        xz-utils; \
    rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 1000 --user-group --shell /bin/bash opsbox

# gh and Claude Code have no official image. The keys are pinned by sha256,
# so a changed key fails the build instead of being trusted silently. When a
# vendor rotates a key, check the new fingerprint against its documentation
# and update the hash. The documented fingerprints:
#   gh (github.com/cli/cli, docs/install_linux.md):
#     2C6106201985B60E6C7AC87323F3D4EA75716059
#     7F38BBB59D064DBCB3D84D725612B36462313325
#   Claude Code (code.claude.com/docs/en/setup):
#     31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE Claude Code's `stable` channel
# is about a week behind `latest` and skips releases with major regressions.
# Package-manager installs do not auto-update, which is right for an image:
# a new version arrives with a new image.
RUN set -eu; \
    install -d -m 0755 /etc/apt/keyrings; \
    curl -fsSL -o /etc/apt/keyrings/githubcli-archive-keyring.gpg https://cli.github.com/packages/githubcli-archive-keyring.gpg; \
    curl -fsSL -o /etc/apt/keyrings/claude-code.asc https://downloads.claude.ai/keys/claude-code.asc; \
    printf '6084d5d7bd8e288441e0e94fc6275570895da18e6751f70f057485dc2d1a811b  /etc/apt/keyrings/githubcli-archive-keyring.gpg\nbd70a5e4a268002704024ceba7f8446024114e94f3f0bdd11c23a9e592be81c6  /etc/apt/keyrings/claude-code.asc\n' | sha256sum -c -; \
    printf 'deb [arch=%s signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main\n' "$(dpkg --print-architecture)" > /etc/apt/sources.list.d/github-cli.list; \
    printf 'deb [signed-by=/etc/apt/keyrings/claude-code.asc] https://downloads.claude.ai/claude-code/apt/stable stable main\n' > /etc/apt/sources.list.d/claude-code.list; \
    DEBIAN_FRONTEND=noninteractive apt-get update; \
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends gh claude-code; \
    rm -rf /var/lib/apt/lists/*

# Helm has no official image and its apt repository runs weeks behind, so it
# comes from get.helm.sh. The version check keeps a surprise major version, or
# an error page, out. The checksum get.helm.sh serves beside the tarball would
# prove nothing TLS doesn't: whoever could replace one could replace both. So
# the tarball is checked against its maintainers' signature instead (published
# on GitHub's release page), using Helm's KEYS file pinned by sha256 at the
# v4.3.1 tag's commit. A release signed by a key that file doesn't hold fails
# the build; check the new key with the Helm project, then update the pin.
RUN set -eu; \
    version="$(curl -fsSL https://get.helm.sh/helm4-latest-version)"; \
    printf '%s\n' "$version" | grep -Eqx 'v4\.[0-9]+\.[0-9]+' || { echo "unexpected Helm version: $version" >&2; exit 1; }; \
    tarball="helm-${version}-linux-${TARGETARCH}.tar.gz"; \
    cd /tmp; \
    curl -fsSL -o helm-KEYS https://raw.githubusercontent.com/helm/helm/c0d08d0d6d9424d4a14febb5e8a3e51b0dd00dff/KEYS; \
    printf '5de12e8dca75f8a72d1ad4e9a05a6f9de0356ee520bf0bd703bd2d37a536e85b  helm-KEYS\n' | sha256sum -c -; \
    curl -fsSLO "https://get.helm.sh/${tarball}"; \
    curl -fsSL -o "${tarball}.asc" "https://github.com/helm/helm/releases/download/${version}/${tarball}.asc"; \
    sqv --keyring helm-KEYS "${tarball}.asc" "$tarball"; \
    tar -xzf "$tarball" "linux-${TARGETARCH}/helm"; \
    install -m 0755 "linux-${TARGETARCH}/helm" /usr/local/bin/helm; \
    rm -rf helm-KEYS "$tarball" "${tarball}.asc" "linux-${TARGETARCH}"

# The binaries from the tool stages, owned by root. COPY --from keeps the
# source image's owner, and restic's is uid 1000: opsbox itself, which could
# then replace it with one that records RESTIC_PASSWORD.
COPY --chown=0:0 --from=kubectl /bin/kubectl /usr/local/bin/kubectl
COPY --chown=0:0 --from=talosctl /talosctl /usr/local/bin/talosctl
COPY --chown=0:0 --from=terraform /bin/terraform /usr/local/bin/terraform
COPY --chown=0:0 --from=tflint /usr/local/bin/tflint /usr/local/bin/tflint
COPY --chown=0:0 --from=terraform-docs /usr/local/bin/terraform-docs /usr/local/bin/terraform-docs
COPY --chown=0:0 --from=uv /uv /uvx /usr/local/bin/
# node's npm needs its whole module directory
COPY --chown=0:0 --from=node /usr/local/bin/node /usr/local/bin/node
COPY --chown=0:0 --from=node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
# docker stage gives the CLI plus its buildx and compose plugins; the
# engine is never in this image.
COPY --chown=0:0 --from=docker /usr/local/bin/docker /usr/local/bin/docker
COPY --chown=0:0 --from=docker /usr/local/libexec/docker/cli-plugins /usr/local/libexec/docker/cli-plugins
COPY --chown=0:0 --from=cilium /usr/local/bin/cilium /usr/local/bin/cilium
COPY --chown=0:0 --from=restic /usr/bin/restic /usr/local/bin/restic
COPY --chown=0:0 --from=shellcheck /bin/shellcheck /usr/local/bin/shellcheck
COPY --chown=0:0 --from=mise /usr/local/bin/mise /usr/local/bin/mise

# Same links the node image makes.
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --chown=0:0 --chmod=0755 rootfs/usr/local/bin/ /usr/local/bin/

# Smoke test: every tool must run, on every architecture. It runs as nobody,
# so a tool's first run can't change anything outside the temporary
# directories, and those are emptied below. Its output is also the record the
# release pipeline compares.
USER 65534:65534
RUN set -eu; \
    scratch="$(mktemp -d)"; \
    HOME="$scratch" TMPDIR="$scratch" opsbox-versions > /tmp/versions.txt; \
    rm -rf "$scratch"; \
    cat /tmp/versions.txt
USER root

# Nothing here needs setuid or setgid: the container runs as one user, with
# no sudo, and compose sets no-new-privileges. Without the bits, su, passwd,
# mount and the rest can't raise anyone's privileges in a plain `docker run`
# either.
#
# Interactive shells (`docker exec -it opsbox bash`) start with the same
# umask the entrypoint sets, so what they write is private too.
RUN set -eu; \
    install -D -m 0644 /tmp/versions.txt /usr/local/share/opsbox/versions.txt; \
    find /tmp /var/tmp -mindepth 1 -delete; \
    find / -xdev -perm /6000 -type f -exec chmod a-s {} +; \
    echo 'umask 077' >> /etc/bash.bashrc

# mise's shims come before the baked tools, so a repository that pins a
# version in .mise.toml gets that version once someone has run `mise trust`
# and `mise install` in it. Anywhere else, the shim falls through to the
# baked binary. A newer Terraform than a repository's CI uses can write state
# the older one is not guaranteed to read.
#
# MISE_PARANOID makes trust cover one config file's contents, not a whole
# directory: without it, a trusted repository's .mise.toml could change on
# the next pull and run whatever its env templates say, and an untrusted
# .tool-versions would be installed with no trust at all.
ENV MISE_PARANOID=1
ENV PATH=/home/opsbox/.local/bin:/home/opsbox/.local/share/mise/shims:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ENV SHELL=/bin/bash

USER opsbox
WORKDIR /home/opsbox

# tini reaps the sessions' child processes and forwards signals. --spawn
# same-dir answers the question claude remote-control would otherwise ask
# on its first start, which nobody may be attached to answer.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/opsbox-entrypoint"]
CMD ["remote-control", "--spawn", "same-dir"]
