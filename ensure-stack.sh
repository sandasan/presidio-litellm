#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"
LOCK_FILE="/tmp/presidio-litellm-stack.lock"

# Serialize simultaneous extension startups so they cannot race on compose.
exec 9>"$LOCK_FILE"
flock 9

mapfile -t SERVICES < <(docker compose -f "$COMPOSE_FILE" config --services)
STACK_RUNNING=1
UNHEALTHY_SERVICES=()
for service in "${SERVICES[@]}"; do
    container="$(docker compose -f "$COMPOSE_FILE" ps -q "$service")"
    state=""
    if [[ -n "$container" ]]; then
        state="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)"
    fi
    if [[ "$state" != "running" ]]; then
        STACK_RUNNING=0
        break
    fi
    health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || true)"
    if [[ "$health" == "unhealthy" ]]; then
        STACK_RUNNING=0
        UNHEALTHY_SERVICES+=("$service")
    elif [[ "$health" == "starting" ]]; then
        STACK_RUNNING=0
    fi
done

if [[ "$STACK_RUNNING" == "1" ]]; then
    echo "Hermes stack is already running; no start command needed." >&2
    exit 0
fi

echo "Hermes stack is incomplete or unhealthy; reconciling services." >&2
for service in "${UNHEALTHY_SERVICES[@]}"; do
    echo "Restarting unhealthy service: $service" >&2
    docker compose -f "$COMPOSE_FILE" up -d --no-deps --force-recreate "$service"
done
docker compose -f "$COMPOSE_FILE" up -d --wait --wait-timeout 300
