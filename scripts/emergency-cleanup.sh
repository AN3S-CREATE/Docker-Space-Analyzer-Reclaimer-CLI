#!/usr/bin/env bash
#
# emergency-cleanup.sh — standalone "free Docker space now" for hosts without
# Python / the docker-disk toolkit installed. Runs the SAFEST high-impact prune
# first and NEVER removes named volumes or in-use objects. Review before running.
#
# Usage:
#   ./emergency-cleanup.sh            # dry-run: show what would be freed
#   ./emergency-cleanup.sh --yes      # actually prune the safe tier
#
# Exit codes: 0 success, 3 docker not available.

set -euo pipefail

DRY_RUN=1
for arg in "$@"; do
  case "$arg" in
    --yes|-y) DRY_RUN=0 ;;
    -h|--help) grep '^#' "$0" | cut -c3-; exit 0 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "ERROR: docker not found on PATH. Start Docker Desktop or install Docker Engine." >&2
  exit 3
fi
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: cannot reach the Docker daemon. Start it (systemctl start docker / Docker Desktop)." >&2
  exit 3
fi

echo "== Current Docker disk usage =="
docker system df
echo

run() {
  # run <description> <command...>
  local desc="$1"; shift
  if [[ "$DRY_RUN" -eq 1 ]]; then
    echo "[dry-run] would: $desc  ->  $*"
  else
    echo "== $desc =="
    "$@" || echo "  (step failed, continuing)"
  fi
}

# Safest, highest-impact first. None of these touch named volumes or running objects.
run "Prune build cache"          docker builder prune -f
run "Remove dangling images"     docker image prune -f
run "Remove stopped containers"  docker container prune -f
run "Remove unused networks"     docker network prune -f

echo
echo "== Docker disk usage after =="
docker system df

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo
  echo "Dry run only. Re-run with --yes to actually reclaim space."
fi

cat <<'EONOTE'

Higher-impact options (review carefully — these can delete data):
  * Unused (but tagged) images:      docker image prune -a
  * ALL unused incl. anon volumes:   docker system prune -a --volumes
  * Named volumes:                   back them up first, then `docker volume rm <name>`

For safe, protect-list-aware, audited cleanup use the full toolkit:
  pipx install docker-disk-toolkit   # or: uv tool install docker-disk-toolkit
  docker-disk cleanup --level 2 --protect 'postgres|nextcloud|truenas'
EONOTE
