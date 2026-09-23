#!/usr/bin/env bash
# Идемпотентная настройка (провижининг) OmniRoute:
#   1) логин в management API (пароль из OMNIROUTE_INITIAL_PASSWORD в .env,
#      задаётся сервису omniroute как INITIAL_PASSWORD при первом старте);
#   2) подключение провайдеров openrouter/gemini/groq/mistral/cerebras из .env-ключей
#      (если коннекшн ещё не создан);
#   3) создание комбо `cloud-auto` (стратегия auto, модели с пригодным SSE)
#      — его использует маршрут cloud-sanitized-auto в LiteLLM.
# Повторный запуск безопасен: пропускает уже созданное.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

[ -f .env ] || { echo "❌ .env не найден в $PROJECT_DIR" >&2; exit 1; }
set -a; . ./.env; set +a

OMNIROUTE_URL="${OMNIROUTE_URL:-http://127.0.0.1:20128}"
PASSWORD="${OMNIROUTE_INITIAL_PASSWORD:-omniro2026!}"
JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT

# GET <path> с cookie сессии -> ответ
omniroute_get() {
  curl -sS -m 10 -b "$JAR" "$OMNIROUTE_URL$1"
}

echo "🔐 Логин в OmniRoute management API..."
if ! curl -sS -m 10 -c "$JAR" -X POST "$OMNIROUTE_URL/api/auth/login" \
       -H 'Content-Type: application/json' \
       -d "{\"password\":\"$PASSWORD\"}" | grep -q '"success":true'; then
  echo "❌ Логин не удался. Убедитесь, что OMNIROUTE_INITIAL_PASSWORD в .env совпадает" >&2
  echo "   с паролем bootstrap-контейнера (первый старт фиксирует его в server.env)." >&2
  exit 1
fi
echo "✅ Авторизован."

for p in openrouter gemini groq mistral cerebras; do
  key_var="${p^^}_API_KEY"
  key="${!key_var:-}"
  if [ -z "$key" ]; then
    echo "⏭️  ${p}: ключ ${key_var} не задан — пропуск."
    continue
  fi
  if omniroute_get /api/providers | grep -q "\"provider\":\"$p\""; then
    echo "✅ ${p}: уже подключён."
  else
    if curl -sS -m 15 -b "$JAR" -X POST "$OMNIROUTE_URL/api/providers" \
         -H 'Content-Type: application/json' \
         -d "{\"provider\":\"$p\",\"name\":\"$p\",\"apiKey\":\"$key\",\"isActive\":true}" \
         | grep -q '"id"'; then
      echo "✅ ${p}: подключён."
    else
      echo "❌ ${p}: не удалось подключить." >&2
      exit 1
    fi
  fi
done

# upsert_combo <имя> <json> — идемпотентное создание/обновление комбо.
upsert_combo() {
  local name="$1" payload="$2"
  if omniroute_get /api/combos | grep -q "\"name\":\"$name\""; then
    echo "ℹ️  Комбо $name уже существует — применяю актуальный состав..."
    CID="$(omniroute_get /api/combos | python3 -c \
      "import sys,json; print(next((c['id'] for c in json.load(sys.stdin)['combos'] if c['name']=='$name'),''))")"
    if [ -n "$CID" ]; then
      if curl -sS -m 15 -b "$JAR" -X PUT "$OMNIROUTE_URL/api/combos/$CID" \
           -H 'Content-Type: application/json' \
           -d "$payload" | grep -q "\"name\":\"$name\""; then
        echo "✅ Комбо $name обновлено."
      else
        echo "⚠️ Не удалось обновить комбо $name (состав остался прежним)."
      fi
    fi
  else
    if curl -sS -m 15 -b "$JAR" -X POST "$OMNIROUTE_URL/api/combos" \
         -H 'Content-Type: application/json' \
         -d "$payload" | grep -q '"id"'; then
      echo "✅ Комбо $name создано."
    else
      echo "❌ Комбо $name: не удалось создать." >&2
      exit 1
    fi
  fi
}

# cloud-auto: все свободные провайдеры. Приоритет по приватности (см. README,
# «Провайдерская приватность»): вес выше у no-training провайдеров — Mistral
# (политика no-training), затем Gemini и Groq (не тренируются на API-трафике по
# умолчанию). У моделей OpenRouter вес минимальный (1): их апстрим-провайдеры
# могут обучаться на трафике, пока в дашборде OpenRouter не выключен
# «Allow training».
#
# Это АГЕНТНЫЙ пул (`cloud-sanitized-auto`): только модели с tool-calls и
# устойчивым SSE — их требует Hermes. Чат-модели без tool-calls живут в
# отдельном комбо cloud-chat (маршрут cloud-sanitized-chat).
upsert_combo cloud-auto '{
  "name": "cloud-auto",
  "strategy": "auto",
  "models": [
    {"provider":"mistral","model":"mistral-small-latest","weight":5},
    {"provider":"gemini","model":"gemini-flash-latest","weight":4}
  ]
}'

# cloud-chat: ЧАТ-пул для Open WebUI (`cloud-sanitized-chat`). В отличие от
# агентного пула, для чата НЕ обязательны tool-calls — уходят модели без них,
# включая быстрые лайт-модели Gemini и Groq (TPM-лимиты Groq не страшны для
# коротких чат-контекстов). Внимание: оба пула по умолчанию разделены, чтобы
# исчерпание квот агентом не выбивало чат. Финальный состав корректирует
# refresh_omniroute_combo.py (проверка здоровья), здесь — стартовый набор.
upsert_combo cloud-chat '{
  "name": "cloud-chat",
  "strategy": "auto",
  "models": [
    {"provider":"mistral","model":"mistral-small-latest","weight":5},
    {"provider":"gemini","model":"gemini-flash-latest","weight":4},
    {"provider":"gemini","model":"gemini-flash-lite-latest","weight":3},
    {"provider":"groq","model":"openai/gpt-oss-120b","weight":4},
    {"provider":"groq","model":"openai/gpt-oss-20b","weight":3},
    {"provider":"groq","model":"qwen/qwen3.8-27b","weight":2},
    {"provider":"mistral","model":"ministral-8b-latest","weight":3},
    {"provider":"openrouter","model":"qwen/qwen3.8-27b:free","weight":1},
    {"provider":"openrouter","model":"z-ai/glm-5.2:free","weight":1},
    {"provider":"openrouter","model":"nvidia/nemotron-3.5-lightning:free","weight":1}
  ]
}'

# Именованные провайдерные комбо (для маршрутов LiteLLM cloud-sanitized-mistral/
# gemini/groq): пользователь может жёстко выбрать конкретного провайдера вместо
# auto. Создаются только для провайдеров, у которых задан ключ в .env. Анони-
# мизация Presidio применяется ко всем маршрутам одинаково (guardrail в config).
[ -n "${MISTRAL_API_KEY:-}" ] && upsert_combo cloud-mistral '{
  "name": "cloud-mistral",
  "strategy": "auto",
  "models": [
    {"provider":"mistral","model":"mistral-small-latest","weight":5}
  ]
}'
[ -n "${GEMINI_API_KEY:-}" ] && upsert_combo cloud-gemini '{
  "name": "cloud-gemini",
  "strategy": "auto",
  "models": [
    {"provider":"gemini","model":"gemini-flash-latest","weight":5}
  ]
}'
[ -n "${GROQ_API_KEY:-}" ] && upsert_combo cloud-groq '{
  "name": "cloud-groq",
  "strategy": "auto",
  "models": [
    {"provider":"groq","model":"openai/gpt-oss-120b","weight":5},
    {"provider":"groq","model":"openai/gpt-oss-20b","weight":3}
  ]
}'

echo "✅ OmniRoute готов к маршрутизации (провайдеры + комбо cloud-auto/cloud-chat/mistral/gemini/groq)."