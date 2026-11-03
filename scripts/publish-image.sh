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

A published version never changes. When REPOSITORY:X.Y.Z already exists
with different images, this fails and tags nothing: a release build is never
reproduced byte for byte, so a second run after a failure would otherwise
replace what people pinned to X.Y.Z. When it exists with the same images
(re-running a publish that failed after tagging), the tags are set again.

Needs docker with buildx, and jq.
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

# platform_digests <ref> - print the digests of the images a reference
# holds, one per line, sorted: an index's platform manifests (attestations,
# whose platform is unknown, left out), or a single manifest's own digest.
platform_digests() {
  local ref="$1"
  docker buildx imagetools inspect "$ref" --format '{{json .Manifest}}' \
    | jq -r 'if .manifests then .manifests[] | select(.platform.os != "unknown") | .digest else .digest end' \
    | sort
}

# existing_digests <ref> - like platform_digests, but print nothing when the
# reference doesn't exist. Any other failure is fatal: reading a registry
# error as "not published yet" would let this overwrite a release.
existing_digests() {
  local ref="$1" output
  if output="$(docker buildx imagetools inspect "$ref" --format '{{json .Manifest}}' 2>&1)"; then
    platform_digests "$ref"
  elif [[ "$output" == *"not found"* ]]; then
    return 0
  else
    die "cannot inspect $ref: $output"
  fi
}

# wanted_digests <source>... - print the platform digests of all the sources.
# A pipeline's status is its last command's, and a loop's is its last pass's,
# so each failure is turned into an exit on the spot: otherwise a failed
# lookup of the first source would vanish behind the second's success.
wanted_digests() {
  local source
  for source in "$@"; do
    platform_digests "$source" || die "cannot inspect $source"
  done | sort
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

  local existing wanted
  existing="$(existing_digests "$exact")"
  wanted="$(wanted_digests "$@")"
  if [[ -n "$existing" && "$existing" != "$wanted" ]]; then
    die "$exact is already published with different images, and a published version never changes. If an earlier publish tagged it and then failed, re-run that publish (it has the same images); otherwise finish that release by hand, at the commit it was built from."
  fi

  docker buildx imagetools create \
    --tag "$exact" \
    --tag "${repository}:${major}.${minor}" \
    --tag "${repository}:${major}" \
    --tag "${repository}:latest" \
    "$@"
}

main "$@"
