#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRANT="${HERMES_GRANTS:-${1:-$(basename "$PROJECT_DIR")}}"

if ! curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
  echo "Hermes ACP: LiteLLM is not ready; starting the stack..." >&2
  docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d
  for _ in $(seq 1 120); do
    if curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
      break
    fi
    sleep 1
  done
fi

if ! curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
  echo "Hermes ACP: LiteLLM did not become ready." >&2
  docker compose -f "$PROJECT_DIR/docker-compose.yml" logs --tail 50 litellm >&2
  exit 1
fi

# Ensure the agent container is running, then start Hermes in ACP mode.
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d --no-deps hermes-agent >/dev/null 2>&1 || true

exec docker exec -it \
  -e HERMES_GRANTS="$GRANT" \
  -e HERMES_ACCEPT_HOOKS=1 \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes acp --accept-hooks
