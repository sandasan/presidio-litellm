#!/usr/bin/env bash
set -euo pipefail

# Корень проекта (директория, где лежит этот скрипт)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"

cd "$PROJECT_DIR"

echo "🔄 Образ omniroute (следим за latest)..."
docker compose -f "$COMPOSE_FILE" pull omniroute
echo "🔄 Пересборка presidio..."
docker compose -f "$COMPOSE_FILE" build presidio

# OmniRoute поднимаем отдельно и раньше остальных: до общего up нужно
# провижининг (подключение провайдеров + комбо cloud-auto), и только потом
# стартует litellm (он depends_on omniroute healthy и берёт маршрут cloud-auto).
echo "🔄 Запуск omniroute (перед провижинингом)..."
docker compose -f "$COMPOSE_FILE" up -d --no-deps omniroute
echo "⏳ Ожидание готовности OmniRoute..."
for i in $(seq 1 90); do
  state="$(docker inspect -f '{{.State.Health.Status}}' omniroute 2>/dev/null || true)"
  [ "$state" = "healthy" ] && { echo "✅ OmniRoute готов на http://127.0.0.1:20128"; break; }
  sleep 1
done
if [ "$(docker inspect -f '{{.State.Health.Status}}' omniroute 2>/dev/null || true)" != "healthy" ]; then
  echo "❌ OmniRoute не поднялся (healthy). Логи:" >&2
  docker compose -f "$COMPOSE_FILE" logs --tail 50 omniroute >&2
  exit 1
fi

echo "🔐 Провижининг OmniRoute (провайдеры из .env + комбо cloud-auto)..."
"$PROJECT_DIR/provision_omniroute.sh"

echo "🔄 Запуск остального стека (presidio + litellm + hermes-agent + open-webui)..."
# up без списка сервисов: создаёт/пересоздаёт все контейнеры, включая hermes-agent
# (работает и с нуля на свежем клоне)
docker compose -f "$COMPOSE_FILE" up -d --force-recreate

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

# litellm стартует только после того, как omniroute станет healthy (depends_on),
# поэтому к этому моменту gateway гарантированно готов
curl -fsS -m 3 -o /dev/null http://127.0.0.1:20128/v1/models 2>/dev/null \
  && echo "✅ OmniRoute готов: дашборд http://127.0.0.1:20128"

curl -fsS -m 3 -o /dev/null http://localhost:3000 2>/dev/null \
  && echo "✅ Open WebUI готов: чат http://localhost:3000" \
  || echo "ℹ️ Open WebUI разогревается: http://localhost:3000"

echo "🤖 Запуск Hermes Agent (модель cloud-sanitized-auto через LiteLLM + Presidio)..."
docker exec -it \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes chat --provider custom -m cloud-sanitized-auto