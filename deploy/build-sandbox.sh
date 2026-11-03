#!/usr/bin/env bash
# Build (and smoke-test) the sandbox image used by Blast Radius Zero.
#
# Usage:
#   deploy/build-sandbox.sh              # incremental, re-runnable
#   deploy/build-sandbox.sh --no-cache   # clean rebuild
#
# Honours SANDBOX_IMAGE (default: brz-sandbox:1) so the app and the built tag
# cannot drift apart.
set -Eeuo pipefail
IFS=$'\n\t'

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd -- "$here/.." && pwd)"
image="${SANDBOX_IMAGE:-brz-sandbox:1}"
dockerfile="$root/sandbox/Dockerfile"

log()  { printf '\n==> %s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
_brz_err() { printf '\nERROR: %s failed at line %s (exit %s)\n' "${BASH_SOURCE[0]}" "$1" "$2" >&2; }
trap '_brz_err "$LINENO" "$?"' ERR

[[ -f "$dockerfile" ]] || die "Dockerfile not found: $dockerfile"
command -v docker >/dev/null 2>&1 || die "docker CLI not installed"
docker info >/dev/null 2>&1 || die "docker daemon not reachable (is it running? are you in the docker group?)"

build_args=()
case "${1:-}" in
  "")            ;;
  --no-cache)    build_args+=(--no-cache) ;;
  -h|--help)     sed -n '2,12p' "${BASH_SOURCE[0]}"; exit 0 ;;
  *)             die "unknown argument: $1 (expected --no-cache)" ;;
esac

log "building $image from sandbox/Dockerfile"
docker build \
  --pull \
  "${build_args[@]}" \
  --tag "$image" \
  --file "$dockerfile" \
  "$root/sandbox"

log "smoke-testing $image under the same flags the control plane uses"
docker run --rm \
  --network none \
  --read-only \
  --user 65534:65534 \
  --cap-drop ALL \
  --security-opt no-new-privileges:true \
  --tmpfs /tmp:rw,noexec,nosuid,nodev,size=16m \
  "$image" \
  python -c 'import numpy, pandas, matplotlib; print("brz-sandbox ok", numpy.__version__, pandas.__version__, matplotlib.__version__)'

log "done: $image"
