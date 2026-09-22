#!/usr/bin/env bash
# Идемпотентная настройка (провижининг) OmniRoute:
#   1) логин в management API (пароль из OMNIROUTE_INITIAL_PASSWORD в .env,
#      задаётся сервису omniroute как INITIAL_PASSWORD при первом старте);
#   2) подключение провайдеров openrouter/gemini/groq/mistral из .env-ключей
#      (если коннекшн ещё не создан);
#   3) создание комбо `cloud-auto` (стратегия auto, только бесплатные модели)
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

for p in openrouter gemini groq mistral; do
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

COMBO_PAYLOAD='{
  "name": "cloud-auto",
  "strategy": "auto",
  "models": [
    {"provider":"mistral","model":"mistral-small-latest","weight":5},
    {"provider":"gemini","model":"gemini-flash-latest","weight":4},
    {"provider":"groq","model":"qwen/qwen3.8-27b","weight":3},
    {"provider":"groq","model":"openai/gpt-oss-120b","weight":3},
    {"provider":"openrouter","model":"qwen/qwen3.8-27b:free","weight":1},
    {"provider":"openrouter","model":"nvidia/nemotron-3.5-lightning:free","weight":1},
    {"provider":"openrouter","model":"thinkingmachines/inkling:free","weight":1},
    {"provider":"openrouter","model":"z-ai/glm-5.2:free","weight":1}
  ]
}'

# Приоритет по приватности (см. README, «Провайдерская приватность»): вес выше
# у no-training провайдеров — Mistral (политика no-training), затем Gemini и
# Groq (не тренируются на API-трафике по умолчанию). У моделей OpenRouter вес
# минимальный (1): их апстрим-провайдеры могут обучаться на трафике, пока в
# дашборде OpenRouter не выключен «Allow training».
if omniroute_get /api/combos | grep -q '"name":"cloud-auto"'; then
  echo "ℹ️  Комбо cloud-auto уже существует — применяю веса приоритета..."
  CID="$(omniroute_get /api/combos | python3 -c "import sys,json; print(next((c['id'] for c in json.load(sys.stdin)['combos'] if c['name']=='cloud-auto'),''))")"
  if [ -n "$CID" ]; then
    curl -sS -m 15 -b "$JAR" -X PUT "$OMNIROUTE_URL/api/combos/$CID" \
      -H 'Content-Type: application/json' \
      -d "$COMBO_PAYLOAD" | grep -q '"name":"cloud-auto"' \
      && echo "✅ Веса комбо cloud-auto обновлены." \
      || echo "⚠️ Не удалось обновить комбо cloud-auto (веса остались прежними)."
  fi
else
  if curl -sS -m 15 -b "$JAR" -X POST "$OMNIROUTE_URL/api/combos" \
       -H 'Content-Type: application/json' \
       -d "$COMBO_PAYLOAD" | grep -q '"id"'; then
    echo "✅ Комбо cloud-auto создано."
  else
    echo "❌ Комбо cloud-auto: не удалось создать." >&2
    exit 1
  fi
fi

echo "✅ OmniRoute готов к маршрутизации (провайдеры + комбо cloud-auto)."