#!/usr/bin/env python3
"""Опрос ИИ-провайдеров и генерация config.yaml для LiteLLM-прокси.

При старте контейнера litellm (или вручную) скрипт:
  1. Опрашивает провайдеров, у которых заданы API-ключи: OpenRouter, Gemini,
     Groq и Mistral (opencode/zen не используется — его free-модели доступны
     только изнутри opencode).
  2. Для каждого выбирает доступную БЕСПЛАТНУЮ модель с поддержкой tool-use
     (агент Hermes вызывает инструменты).
  3. Пишет итоговый конфиг в litellm-proxy/config.yaml — тот самый, который
     загружает docker-compose.

Скрипт использует только стандартную библиотеку, чтобы работать и на хосте,
и внутри контейнера litellm без лишних зависимостей.
"""

import json
import os
import urllib.parse
import urllib.request
from typing import Optional, Sequence

MODEL_NAME = "cloud-sanitized"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# --- Предиктивные порядки выбора моделей ------------------------------------

GEMINI_PREFERRED = [
    "gemini-3.6-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash",
    "gemini-3.7-flash",
    "gemini-3-flash-preview",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
]

GROQ_PREFERRED = [
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-120b",
    "openai/gpt-oss-20b",
    "meta-llama/llama-3.3-70b-versatile",
    "groq/compound-mini",
]

MISTRAL_PREFERRED = [
    "mistral-small-latest",
    "mistral-small-2603",
    "ministral-8b-latest",
    "mistral-medium-latest",
    "codestral-latest",
]

OPENROUTER_PREFERRED = [
    "qwen/qwen3.8-27b:free",
    "google/gemma-4-31b-it:free",
    "google/gemma-4-26b-a4b-it:free",
    "z-ai/glm-5.2:free",
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3.5-lightning:free",
    "thinkingmachines/inkling:free",
]

# Слова, которых не должно быть в id выбранной модели (спец-модели)
GEMINI_EXCLUDE = (
    "image", "tts", "search", "grounding", "embedding", "prediction",
    "audio", "video", "vision", "code", "thinking", "live", "draw",
)
GROQ_EXCLUDE = (
    "whisper", "embedding", "orpheus", "prompt-guard", "compound", "safeguard",
)
MISTRAL_EXCLUDE = (
    "embed", "fim", "voice", "audio", "vibe", "vision", "vlm",
)

TIMEOUT = 20  # сек на запрос

# --- Утилиты ----------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[generate-config] {msg}", flush=True)


def http_json(url: str, headers: Optional[dict] = None, data: dict | None = None) -> dict | list:
    body = json.dumps(data).encode() if data is not None else None
    req_headers = {
        "User-Agent": "presidio-litellm/1.0 (config-generator)",
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=body, headers=req_headers)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def pick_preferred(ids: Sequence[str], preferred: Sequence[str]) -> Optional[str]:
    for pref in preferred:
        if pref in ids:
            return pref
    return None


def pick_free(ids: Sequence[str], exclude: Sequence[str]) -> Optional[str]:
    """Первая модель с префиксом :free и без спец-суффиксов."""
    bad = set(exclude)
    for mid in ids:
        if mid.endswith(":free") and not any(part in mid for part in bad):
            return mid
    return None


# --- Опросы провайдеров ------------------------------------------------------


def probe_openrouter() -> Optional[dict]:
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key:
        log("OpenRouter: нет OPENROUTER_API_KEY, пропускаю")
        return None
    try:
        data = http_json(
            "https://openrouter.ai/api/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        models = data.get("data", []) if isinstance(data, dict) else data
        ids = [m.get("id", "") for m in models]

        chosen = pick_preferred(ids, OPENROUTER_PREFERRED)
        if chosen is None:
            chosen = pick_free(ids, ("aqa", "audio", "embed"))
        if chosen is None:
            chosen = next(iter(ids), None)
        if not chosen:
            log("OpenRouter: список моделей пуст")
            return None

        log(f"OpenRouter: выбрана модель {chosen}")
        return {
            "model": f"openrouter/{chosen}",
            "api_key": "os.environ/OPENROUTER_API_KEY",
            "max_tokens": 4096,
            "rpm": 15,
        }
    except Exception as e:
        log(f"OpenRouter: ошибка опроса: {e}")
        return None


def probe_gemini() -> Optional[dict]:
    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        log("Gemini: нет GEMINI_API_KEY, пропускаю")
        return None
    try:
        url = "https://generativelanguage.googleapis.com/v1beta/models?key=" + urllib.parse.quote(key)
        data = http_json(url)
        ids = [m.get("name", "").replace("models/", "", 1) for m in data.get("models", [])]

        chosen = pick_preferred(ids, GEMINI_PREFERRED)
        if chosen is None:
            candidates = [i for i in ids if "flash" in i and not any(x in i for x in GEMINI_EXCLUDE)]
            candidates.sort()
            chosen = next(iter(candidates), None)
        if not chosen:
            log("Gemini: не нашёл flash-моделей")
            return None

        log(f"Gemini: выбрана модель {chosen}")
        return {
            "model": f"gemini/{chosen}",
            "api_key": "os.environ/GEMINI_API_KEY",
            "max_tokens": 4096,
            "rpm": 10,
        }
    except Exception as e:
        log(f"Gemini: ошибка опроса: {e}")
        return None


def probe_groq() -> Optional[dict]:
    key = os.getenv("GROQ_API_KEY", "").strip()
    if not key:
        log("Groq: нет GROQ_API_KEY, пропускаю")
        return None
    try:
        data = http_json(
            "https://api.groq.com/openai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        ids = [m.get("id", "") for m in data.get("data", [])]

        chosen = pick_preferred(ids, GROQ_PREFERRED)
        if chosen is None:
            candidates = [
                i for i in ids
                if not any(x in i.lower() for x in GROQ_EXCLUDE)
                and "whisper" not in i.lower()
            ]
            # Не выбираем эмбеддинги/голос/аудио в качестве chat-модели
            candidates = [i for i in candidates if any(k in i.lower() for k in ("qwen", "llama", "gpt-oss", "grok"))]
            candidates.sort()
            chosen = next(iter(candidates), None)
        if not chosen:
            log("Groq: нет подходящих chat-моделей")
            return None

        log(f"Groq: выбрана модель {chosen}")
        return {
            "model": f"groq/{chosen}",
            "api_key": "os.environ/GROQ_API_KEY",
            "max_tokens": 4096,
            "rpm": 20,
        }
    except Exception as e:
        log(f"Groq: ошибка опроса: {e}")
        return None


def probe_mistral() -> Optional[dict]:
    key = os.getenv("MISTRAL_API_KEY", "").strip()
    if not key:
        log("Mistral: нет MISTRAL_API_KEY, пропускаю")
        return None
    try:
        data = http_json(
            "https://api.mistral.ai/v1/models",
            headers={"Authorization": f"Bearer {key}"},
        )
        ids = [m.get("id", "") for m in data.get("data", [])]

        chosen = pick_preferred(ids, MISTRAL_PREFERRED)
        if chosen is None:
            candidates = [i for i in ids if not any(x in i.lower() for x in MISTRAL_EXCLUDE)]
            candidates.sort()
            chosen = next(iter(candidates), None)
        if not chosen:
            log("Mistral: нет подходящих моделей")
            return None

        log(f"Mistral: выбрана модель {chosen}")
        return {
            "model": f"mistral/{chosen}",
            "api_key": "os.environ/MISTRAL_API_KEY",
            "max_tokens": 4096,
            "rpm": 10,
        }
    except Exception as e:
        log(f"Mistral: ошибка опроса: {e}")
        return None


def probe_zen() -> Optional[dict]:
    """OpenAI-совместимый fallback (opencode zen). ОТКЛЮЧЁН: free-модели zen
    доступны только изнутри opencode и возвращают 403 при вызове извне.
    Оставляем заглушку, чтобы при желании включить платные каналы."""
    return None


# --- Генерация YAML ----------------------------------------------------------


def emit_models(routes: Sequence[dict]) -> str:
    lines = ["model_list:"]
    for params in routes:
        lines.append(f"  - model_name: {MODEL_NAME}")
        lines.append("    litellm_params:")
        for k, v in params.items():
            if isinstance(v, int):
                lines.append(f"      {k}: {v}")
            else:
                lines.append(f"      {k}: \"{v}\"")
    return "\n".join(lines)


def build_config(routes: Sequence[dict]) -> str:
    return "\n".join(
        [
            emit_models(routes),
            "",
            "router_settings:",
            "  routing_strategy: simple-shuffle",
            "  num_retries: 4",
            "  cooldown_time: 60",
            "  retry_policy:",
            "    BadRequestErrorRetries: 1",
            "",
            "litellm_settings:",
            '  callbacks: ["presidio", "custom_callbacks.proxy_handler_instance"]',
            "",
        ]
    )


def main() -> None:
    probes = (probe_openrouter, probe_gemini, probe_groq, probe_mistral, probe_zen)
    routes = []
    for probe in probes:
        route = probe()
        if route:
            routes.append(route)

    if not routes:
        log("НЕ УДАЛОСЬ получить ни одной модели ни от одного провайдера.")
        log("Старый config.yaml сохранён без изменений.")
        return 1

    config_text = build_config(routes)
    with open(OUTPUT, "w") as f:
        f.write(config_text)
    log(f"Записан конфиг {OUTPUT} с {len(routes)} маршрутами:")
    for r in routes:
        log(f"  - {r['model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())