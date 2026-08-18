#!/usr/bin/env bash
# Deploy the current main branch to this server.
#
#   cd /opt/cinagi/app && ./scripts/deploy.sh
#
# Safe to run repeatedly. Refuses to run if anything important is missing.
set -euo pipefail

COMPOSE_FILE="docker-compose.prod.yml"
# Where this stack's nginx is reachable from the host. It binds to loopback so
# that the host's own nginx can terminate TLS in front of it.
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8080/healthz}"
cd "$(dirname "$0")/.."

say()  { printf "\n\033[1;36m==> %s\033[0m\n" "$1"; }
warn() { printf "\033[1;33m!!  %s\033[0m\n" "$1"; }
die()  { printf "\033[1;31mxx  %s\033[0m\n" "$1"; exit 1; }

[ -f .env ] || die ".env is missing. Create it on this server (see docs/DEPLOY.md) - it is never in git."
command -v docker >/dev/null || die "docker is not installed."
docker compose version >/dev/null 2>&1 || die "the docker compose plugin is not installed."

if [ -n "$(git status --porcelain)" ]; then
  warn "This server has local changes. They will be kept, but the server should track main exactly."
  git status --short
fi

say "Fetching the latest code"
git fetch --quiet origin main
BEFORE="$(git rev-parse HEAD)"
git merge --ff-only origin/main || die "Cannot fast-forward. Someone has committed on the server. Sort that out first."
AFTER="$(git rev-parse HEAD)"

if [ "$BEFORE" = "$AFTER" ]; then
  say "Already up to date at $(git log --oneline -1)"
else
  say "Updated: $(git log --oneline "$BEFORE".."$AFTER" | wc -l) new commit(s)"
  git log --oneline "$BEFORE".."$AFTER"
fi

say "Building and starting"
docker compose -f "$COMPOSE_FILE" up -d --build

# nginx resolves the app container once at startup; after a rebuild it can be
# pointing at an address that no longer exists. Recreating it is not optional.
say "Recreating nginx so it picks up the new app container"
docker compose -f "$COMPOSE_FILE" up -d --force-recreate nginx

say "Waiting for the app to come up ($HEALTH_URL)"
for attempt in $(seq 1 30); do
  if curl -fsS "$HEALTH_URL" >/dev/null 2>&1; then
    curl -s "$HEALTH_URL"; echo
    break
  fi
  if [ "$attempt" -eq 30 ]; then
    warn "No answer from $HEALTH_URL."
    warn "If nginx is published on a different port, run: HEALTH_URL=http://127.0.0.1:<port>/healthz ./scripts/deploy.sh"
    die "Check: docker compose -f $COMPOSE_FILE logs --tail 100 web"
  fi
  sleep 2
done

MODE="$(grep -E '^OUTBOUND_COMMS_MODE=' .env | cut -d= -f2 || echo unknown)"
say "Deployed $(git log --oneline -1)"
echo "Messaging mode: $MODE"
[ "$MODE" != "live" ] && echo "(Real customers are NOT being messaged while this is not 'live'.)"
