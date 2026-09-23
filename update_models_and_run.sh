#!/usr/bin/env bash
set -euo pipefail

# Корень проекта (директория, где лежит этот скрипт)
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$PROJECT_DIR/docker-compose.yml"

cd "$PROJECT_DIR"

# Читаем .env (ключи, DEFAULT_MODEL и т.п.) — docker compose и так это делает,
# здесь нужны значения для финального запуска hermes-chat.
if [ ! -f "$PROJECT_DIR/.env" ]; then
  echo "❌ .env не найден в $PROJECT_DIR — скопируйте .env.example и впишите ключи." >&2
  exit 1
fi
set -a; . "$PROJECT_DIR/.env"; set +a
DEFAULT_MODEL="${DEFAULT_MODEL:-cloud-sanitized-auto}"

echo "🔄 Образ omniroute (следим за latest)..."
docker compose -f "$COMPOSE_FILE" pull omniroute
echo "🔄 Пересборка presidio..."
docker compose -f "$COMPOSE_FILE" build presidio

# OmniRoute поднимаем отдельно и раньше остальных: до общего up нужно
# провижининг (подключение провайдеров + комбо cloud-auto/cloud-chat и др.),
# и только потом стартует litellm (он depends_on omniroute healthy и берёт
# маршруты cloud-auto / cloud-chat).
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

echo "🔐 Провижининг OmniRoute (провайдеры из .env + комбо cloud-auto/chat/mistral/gemini/groq)..."
"$PROJECT_DIR/provision_omniroute.sh"
echo "🩺 Проверка моделей (агентный пул: SSE + tool-call; чат-пул: SSE + ответ)..."
python3 "$PROJECT_DIR/refresh_omniroute_combo.py" || \
  echo "⚠️ Нет новых здоровых целей; сохраняем последнее рабочее комбо."

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

echo "🤖 Запуск Hermes Agent (модель $DEFAULT_MODEL через LiteLLM + Presidio)..."
# Важно: для реального сценария чата нужен TTY, но без интерактивного выбора
# гранта: передаём его явно, чтобы Hermes сразу стартовал в нужном каталоге.
HERMES_GRANT="${HERMES_GRANTS:-$(basename "$PROJECT_DIR")}"
docker exec -it \
  -e HERMES_GRANTS="$HERMES_GRANT" \
  -e CUSTOM_BASE_URL=http://litellm:4000/v1 \
  -e CUSTOM_API_KEY=sk-dummy \
  hermes-agent hermes-chat chat --provider custom -m "$DEFAULT_MODEL"