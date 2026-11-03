#!/usr/bin/env bash
#
# plan-release.sh - print the version a release of this image would get.
# See usage() or --help for details.

set -euo pipefail

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

readonly SCRIPT_NAME="${0##*/}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
readonly SCRIPT_DIR
readonly VERSIONS_FILE=/usr/local/share/opsbox/versions.txt
readonly TAG_PATTERN='^v[0-9]+\.[0-9]+\.[0-9]+$'
readonly IMAGE_INPUTS=(Dockerfile .dockerignore rootfs)

# Global, not local to main: the EXIT trap that removes it runs after main has
# returned, when a local would be gone (and unbound, which `set -u` turns into
# a failed exit after a successful run).
tmp=""

# ---------------------------------------------------------------------------
# usage and utilities
# ---------------------------------------------------------------------------

# usage - print a usage message and exit.
usage() {
  cat <<EOF
Usage: scripts/plan-release.sh [--notes FILE] CANDIDATE REPOSITORY

Prints the version a release of CANDIDATE would get (vX.Y.Z), or nothing
when no release is due.

CANDIDATE is an image reference, a local tag or REPOSITORY@sha256:....

REPOSITORY is where releases are published, for example ghcr.io/OWNER/opsbox,
and the last release is REPOSITORY:<last tag without its v>.

--notes FILE writes the release notes there when a release is due.

Reads MAJOR_VERSION and the git tags of the repository it is run in, needs
git, docker and python3, and the rules are in scripts/next-version.py.
EOF
}

# die - print an error message to stderr and exit 1.
die() {
  echo "${SCRIPT_NAME}: error: $*" >&2
  exit 1
}

# repo_root - print the absolute path of the git repository root.
repo_root() {
  git rev-parse --show-toplevel
}

# ---------------------------------------------------------------------------
# release inputs
# ---------------------------------------------------------------------------

# read_major <repo> - read and validate MAJOR_VERSION from the repo.
read_major() {
  local repo="$1" value
  value="$(tr -d '[:space:]' < "$repo/MAJOR_VERSION")"
  if ! [[ "$value" =~ ^[1-9][0-9]*$ ]]; then
    die "MAJOR_VERSION must be a whole number of 1 or more, not '${value}'"
  fi
  echo "$value"
}

# last_tag <repo> - print the highest release tag (vX.Y.Z) in the repo.
last_tag() {
  local repo="$1"
  git -C "$repo" tag --list 'v*' | { grep -E "$TAG_PATTERN" || true; } | \
    sort -V | tail -n 1
}

# read_versions <image> <dest> - extract versions.txt from an image.
read_versions() {
  local image="$1" dest="$2"
  if ! docker run --rm --entrypoint cat "$image" "$VERSIONS_FILE" > "$dest"; then
    die "cannot read $VERSIONS_FILE from $image"
  fi
}

# inputs_changed <repo> <tag> - return success when build files changed.
inputs_changed() {
  local repo="$1" tag="$2"
  ! git -C "$repo" diff --quiet "$tag" HEAD -- "${IMAGE_INPUTS[@]}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# main <args> - parse arguments and run next-version.py.
main() {
  local notes="" repo major tag

  # Parse options.
  while [[ $# -gt 0 ]]; do
    case "$1" in
      -h|--help)
        usage
        exit 0
        ;;
      --notes)
        [[ $# -ge 2 ]] || die "--notes needs a file"
        notes="$2"
        shift 2
        ;;
      -*)
        usage >&2
        exit 2
        ;;
      *)
        break
        ;;
    esac
  done

  # Exactly 2 positional arguments required.
  [[ $# -eq 2 ]] || { usage >&2; exit 2; }

  local candidate="$1" repository="$2"

  repo="$(repo_root)"
  major="$(read_major "$repo")"
  tag="$(last_tag "$repo")"
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT

  # Read the candidate's versions.
  read_versions "$candidate" "$tmp/current.txt"

  # Build args for next-version.py.
  local -a args=(--current "$tmp/current.txt" --major "$major")

  if [[ -n "$tag" ]]; then
    read_versions "${repository}:${tag#v}" "$tmp/previous.txt"
    args+=(--previous "$tmp/previous.txt" --last-tag "$tag")
    if inputs_changed "$repo" "$tag"; then
      args+=(--inputs-changed)
    fi
  fi

  if [[ -n "$notes" ]]; then
    args+=(--notes "$notes")
  fi

  python3 "$SCRIPT_DIR/next-version.py" "${args[@]}"
}

main "$@"
