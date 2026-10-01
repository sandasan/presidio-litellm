#!/usr/bin/env bash
# Идемпотентная настройка (провижининг) OmniRoute:
#   1) логин в management API (пароль из OMNIROUTE_INITIAL_PASSWORD в .env,
#      задаётся сервису omniroute как INITIAL_PASSWORD при первом старте);
#   2) подключение провайдеров openrouter/gemini/groq/mistral/cerebras из .env-ключей
#      (если коннекшн ещё не создан);
#   3) создание провайдерных комбо для ручного выбора маршрута.
#      Пулы cloud-auto и cloud-chat создаёт refresh_omniroute_combo.py
#      только из моделей, прошедших health-probe.
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

echo "✅ Провайдеры OmniRoute настроены; cloud-auto/cloud-chat будут собраны после health-probe."