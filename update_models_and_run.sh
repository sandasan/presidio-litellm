#!/usr/bin/env bash
set -euo pipefail

# Корень проекта (директория, где лежит этот скрипт)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"

cd "$PROJECT_DIR"

echo "🔄 Пересборка и запуск стека (presidio + litellm)..."
# presidio: код изменился -> пересборка образа
docker compose -f "$COMPOSE_FILE" build presidio
docker compose -f "$COMPOSE_FILE" up -d --force-recreate presidio litellm

echo "⏳ Ожидание готовности LiteLLM-прокси..."
for i in $(seq 1 120); do
  if curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
    echo "✅ LiteLLM готов на http://localhost:4000"
    break
  fi
  sleep 1
done

if ! curl -fsS -m 3 http://localhost:4000/health/liveliness >/dev/null 2>&1; then
  echo "❌ LiteLLM не поднялся. Логи:" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail 50 litellm >&2
  exit 1
fi

echo "🤖 Запуск Hermes Agent (модель cloud-sanitized через LiteLLM + Presidio)..."
docker exec -it \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes chat --provider custom -m cloud-sanitized