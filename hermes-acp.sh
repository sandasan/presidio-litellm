#!/usr/bin/env bash
set -euo pipefail

SCRIPT_PATH="$(readlink -f "${BASH_SOURCE[0]}")"
PROJECT_DIR="$(cd "$(dirname "$SCRIPT_PATH")" && pwd)"
GRANT="${HERMES_GRANTS:-${1:-$(basename "$PROJECT_DIR")}}"
TARGET_DIR="${2:-}"

# VS Code ACP extensions invoke the configured path as `hermes acp`.
# Use their workspace cwd as the directly mounted project.
if [[ "${1:-}" == "acp" ]]; then
  TARGET_DIR="$PWD"
  GRANT="$(basename "$TARGET_DIR")"
fi

if [[ -n "$TARGET_DIR" ]]; then
  TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"
  export PROJECTS_DIR="$TARGET_DIR"
  GRANT="."
  SINGLE_PROJECT=1
else
  SINGLE_PROJECT=0
fi

if [[ "${1:-}" == "version" || "${1:-}" == "--version" ]]; then
  docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d --no-deps hermes-agent </dev/null >/dev/null 2>&1 || true
  AGENT_CONTAINER="$(docker compose -f "$PROJECT_DIR/docker-compose.yml" ps -q hermes-agent)"
  for _ in $(seq 1 30); do
    if [[ -n "$AGENT_CONTAINER" ]] \
      && [[ "$(docker inspect -f '{{.State.Status}}' "$AGENT_CONTAINER" 2>/dev/null || true)" == "running" ]] \
      && docker exec "$AGENT_CONTAINER" /opt/venv/bin/hermes version >/dev/null 2>&1; then
      exec docker exec "$AGENT_CONTAINER" /opt/venv/bin/hermes "$@"
    fi
    AGENT_CONTAINER="$(docker compose -f "$PROJECT_DIR/docker-compose.yml" ps -q hermes-agent)"
    sleep 1
  done
  echo "Hermes ACP: hermes-agent is not ready." >&2
  exit 1
fi

if ! curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
  echo "Hermes ACP: LiteLLM is not ready; starting the stack..." >&2
  docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d </dev/null
  for _ in $(seq 1 120); do
    if curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

if ! curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
  echo "Hermes ACP: LiteLLM did not become ready." >&2
  docker compose -f "$PROJECT_DIR/docker-compose.yml" logs --tail 50 litellm </dev/null >&2
  exit 1
fi

# Ensure the agent container is running, then start Hermes in ACP mode. Avoid
# recreating a live container here: the extension may connect immediately after
# startup, and a forced recreate can drop the first ACP response.
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d --no-deps hermes-agent </dev/null >/dev/null 2>&1 || true
AGENT_CONTAINER="$(docker compose -f "$PROJECT_DIR/docker-compose.yml" ps -q hermes-agent)"

for _ in $(seq 1 30); do
  if [[ -n "$AGENT_CONTAINER" ]] \
    && [[ "$(docker inspect -f '{{.State.Status}}' "$AGENT_CONTAINER" 2>/dev/null || true)" == "running" ]] \
    && docker exec "$AGENT_CONTAINER" /opt/venv/bin/hermes version >/dev/null 2>&1; then
    break
  fi
  AGENT_CONTAINER="$(docker compose -f "$PROJECT_DIR/docker-compose.yml" ps -q hermes-agent)"
  sleep 1
done
if [[ -z "$AGENT_CONTAINER" ]] \
  || [[ "$(docker inspect -f '{{.State.Status}}' "$AGENT_CONTAINER" 2>/dev/null || true)" != "running" ]] \
  || ! docker exec "$AGENT_CONTAINER" /opt/venv/bin/hermes version >/dev/null 2>&1; then
  echo "Hermes ACP: hermes-agent container is not running." >&2
  docker compose -f "$PROJECT_DIR/docker-compose.yml" logs --tail 50 hermes-agent </dev/null >&2
  exit 1
fi

# ACP is a JSON-RPC stdio protocol; never allocate a pseudo-TTY here, even when
# the wrapper is launched from the VS Code task terminal.
exec docker exec -i \
  -e HERMES_GRANTS="$GRANT" \
  -e HERMES_SINGLE_PROJECT="$SINGLE_PROJECT" \
  -e HERMES_ACCEPT_HOOKS=1 \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  "$AGENT_CONTAINER" hermes acp --accept-hooks
