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
# ui is the page's vendored terminal, so it's part of the image too.
readonly IMAGE_INPUTS=(Dockerfile .dockerignore rootfs ui)

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
Usage: scripts/plan-release.sh [--notes FILE] [--only-if-newest] CANDIDATE REPOSITORY

Prints the version a release of CANDIDATE would get (vX.Y.Z), or nothing
when no release is due. A version that already has an image is skipped: a
publish that failed after tagging leaves one with no git tag, and a published
version never changes.

CANDIDATE is an image reference, a local tag or REPOSITORY@sha256:....

REPOSITORY is where releases are published, for example ghcr.io/OWNER/opsbox,
and the last release is REPOSITORY:<last tag without its v>.

--notes FILE writes the release notes there when a release is due.

--only-if-newest prints nothing unless HEAD is the commit main is at on GitHub.
The next run on main releases this commit too, and GitHub refuses GITHUB_TOKEN
a release at a commit whose workflow files differ from main's.

Reads MAJOR_VERSION and the git tags of the repository it is run in, needs
git, docker with buildx and python3, plus a signed-in gh for --only-if-newest,
and the rules are in scripts/next-version.py.
EOF
}

# die - print an error message to stderr and exit 1.
die() {
  echo "${SCRIPT_NAME}: error: $*" >&2
  exit 1
}

# repo_root - print the absolute path of the git repository it is run in.
repo_root() {
  git rev-parse --show-toplevel 2>/dev/null \
    || die "run this from inside the opsbox repository: it reads MAJOR_VERSION and the tags there"
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

# newest_on_main - print the commit main is at on GitHub right now. It asks the
# API with the job's token because GitHub can demand sign-in for anonymous git
# reads, even of a public repository, and `git ls-remote` would also match a
# branch whose name merely ends in refs/heads/main.
newest_on_main() {
  local sha
  sha="$(gh api 'repos/{owner}/{repo}/git/ref/heads/main' --jq .object.sha)" \
    || die "cannot read main from GitHub"
  [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || die "GitHub gave no commit for main: $sha"
  echo "$sha"
}

# image_exists <ref> - return success when the registry has this image. Only
# "not found" about this very image means it doesn't: a missing credential
# helper also says "not found", and reading any other failure that way could
# hand a published version to a second image.
image_exists() {
  local ref="$1" output
  if output="$(docker buildx imagetools inspect "$ref" 2>&1)"; then
    return 0
  elif [[ "$output" == *"$ref: not found"* ]]; then
    return 1
  fi
  die "cannot inspect $ref: $output"
}

# first_free_version <repository> <version> - print the version, or the first
# one after it with no image. The tags only say what was released, so a publish
# that failed after tagging would otherwise be given the same number by every
# later run. A minor or a major (patch 0) moves to the next minor, because X.Y
# only ever takes patches.
first_free_version() {
  local repository="$1" version="$2" major minor patch
  IFS=. read -r major minor patch <<< "${version#v}"
  while image_exists "${repository}:${major}.${minor}.${patch}"; do
    echo "${SCRIPT_NAME}: v${major}.${minor}.${patch} has an image but no git tag, so it is skipped" >&2
    if (( patch == 0 )); then
      minor=$((minor + 1))
    else
      patch=$((patch + 1))
    fi
  done
  echo "v${major}.${minor}.${patch}"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# main <args> - parse arguments, run next-version.py, then check main and the
# registry.
main() {
  local notes="" only_if_newest=false repo major tag version newest head

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
      --only-if-newest)
        only_if_newest=true
        shift
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
    # Releases only move forward. Without this, re-running an old workflow run
    # (or dispatching one on a branch) would publish older or unmerged code as
    # the next version, and move `latest` onto it.
    git -C "$repo" merge-base --is-ancestor "$tag" HEAD \
      || die "$tag is not an ancestor of HEAD; refusing to release older or unmerged code"
    read_versions "${repository}:${tag#v}" "$tmp/previous.txt"
    args+=(--previous "$tmp/previous.txt" --last-tag "$tag")
    if inputs_changed "$repo" "$tag"; then
      args+=(--inputs-changed)
    fi
  fi

  if [[ -n "$notes" ]]; then
    args+=(--notes "$notes")
  fi

  version="$(python3 "$SCRIPT_DIR/next-version.py" "${args[@]}")"
  [[ -n "$version" ]] || return 0

  # Asked after the slow image reads, so little time passes before the caller
  # tags.
  if [[ "$only_if_newest" == true ]]; then
    newest="$(newest_on_main)"
    head="$(git -C "$repo" rev-parse HEAD)"
    if [[ "$newest" != "$head" ]]; then
      echo "${SCRIPT_NAME}: main has moved on to ${newest:0:7}, so the next run on main releases this too" >&2
      return 0
    fi
  fi

  first_free_version "$repository" "$version"
}

main "$@"
