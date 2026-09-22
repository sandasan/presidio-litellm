#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GRANT="${HERMES_GRANTS:-$(basename "$PROJECT_DIR")}" 

# Ensure the container is running, then start Hermes in ACP mode for editor integration.
docker compose -f "$PROJECT_DIR/docker-compose.yml" up -d --no-deps hermes-agent >/dev/null 2>&1 || true

exec docker exec -it \
  -e HERMES_GRANTS="$GRANT" \
  -e HERMES_ACCEPT_HOOKS=1 \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes acp --accept-hooks
