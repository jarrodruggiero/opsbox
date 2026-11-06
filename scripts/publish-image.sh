#!/usr/bin/env bash
#
# publish-image.sh - tag a release's per-architecture images as one image.
# See usage() or --help for details.

set -euo pipefail

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

readonly SCRIPT_NAME="${0##*/}"
readonly VERSION_PATTERN='^v([0-9]+)\.([0-9]+)\.([0-9]+)$'

# ---------------------------------------------------------------------------
# usage and utilities
# ---------------------------------------------------------------------------

# usage - print how to invoke this script.
usage() {
  cat <<EOF
Usage: scripts/${SCRIPT_NAME} REPOSITORY VERSION SOURCE...

Joins the SOURCE images (one per architecture, as REPOSITORY@sha256:...)
into one image and tags it X.Y.Z, X.Y, X and latest for VERSION (vX.Y.Z).

A published version never changes. When REPOSITORY:X.Y.Z already exists, this
fails and tags nothing. scripts/plan-release.sh has already moved past any
version with an image, so reaching one here means something is wrong, and
tagging it would replace what people pinned to X.Y.Z.

Needs docker with buildx.
EOF
}

# die <message> - print an error message and exit.
die() {
  echo "${SCRIPT_NAME}: error: $*" >&2
  exit 1
}

# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------

# is_published <ref> - return success when the reference exists. Any failure
# but "not found" about this very image is fatal: a missing credential helper
# also says "not found", and reading a failure as "not published yet" could
# overwrite a release.
is_published() {
  local ref="$1" output
  if output="$(docker buildx imagetools inspect "$ref" 2>&1)"; then
    return 0
  elif [[ "$output" == *"$ref: not found"* ]]; then
    return 1
  fi
  die "cannot inspect $ref: $output"
}

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

# main <args> - check the version is free, then tag the joined image.
main() {
  if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
    usage
    exit 0
  fi
  (( $# >= 3 )) || { usage >&2; exit 2; }

  local repository="$1" version="$2"
  shift 2
  [[ "$version" =~ $VERSION_PATTERN ]] || die "not a vX.Y.Z version: $version"
  local major="${BASH_REMATCH[1]}" minor="${BASH_REMATCH[2]}" patch="${BASH_REMATCH[3]}"
  local exact="${repository}:${major}.${minor}.${patch}"

  if is_published "$exact"; then
    die "$exact is already published, and a published version never changes."
  fi

  docker buildx imagetools create \
    --tag "$exact" \
    --tag "${repository}:${major}.${minor}" \
    --tag "${repository}:${major}" \
    --tag "${repository}:latest" \
    "$@"
}

main "$@"
