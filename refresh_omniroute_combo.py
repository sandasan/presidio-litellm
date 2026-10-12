#!/usr/bin/env python3
"""Keep the Hermes cloud-auto combo limited to currently usable tool models.

Two independent pools are probed and refreshed:

- `cloud-auto` (agent route `cloud-sanitized-auto`) — Hermes needs tool calls and
  a large context, so a target is accepted only when a streaming tool-call probe
  returns a real tool call and a terminal SSE event.
- `cloud-chat` (chat route `cloud-sanitized-chat`) — Open WebUI chat does not
  need tool calls, so a target is accepted on a plain streaming completion
  (non-empty content plus [DONE]). This keeps the chat usable even when agent
  quotas for tool-capable models are exhausted.

Candidates are now automatically discovered from OmniRoute API instead of being
hardcoded. The script fetches all available models from connected providers,
filters them by capabilities (tool_calling for agent pool), and probes each one.
Providers without an API key in `.env` are skipped automatically.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.cookiejar import CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPCookieProcessor, Request, build_opener


PROJECT_DIR = Path(__file__).resolve().parent
STATE_FILE = PROJECT_DIR / ".omniroute-health.json"
CATALOG_CACHE_FILE = PROJECT_DIR / ".omniroute-catalog.json"
AGENT_COMBO = "cloud-auto"
CHAT_COMBO = "cloud-chat"
SUCCESS_TTL = 300
FAILURE_COOLDOWN = 900
PROBE_TIMEOUT = 30
CATALOG_CACHE_TTL = 600  # Кэшировать каталог на 10 минут

# Провайдеры, которые нужно пропускать (например, платные или нестабильные)
EXCLUDED_PROVIDERS = {"openrouter-free", "local"}

# Direct провайдеры с бесплатными тирами (приоритет над openrouter)
DIRECT_PROVIDERS = {"mistral", "gemini", "groq", "cerebras"}

# Паттерны для фильтрации платных моделей (исключаем модели с этими признаками)
PAID_MODEL_PATTERNS = [
    "-high", "-medium", "-low", "-xhigh", "-none",  # Effort tiers
    "-pro", "-ultra", "-preview",  # Premium tiers
    "grok", "gpt-5", "gpt-6", "gpt-astra", "gpt-sol", "gpt-terra", "gpt-luna",  # Платные GPT
    "claude-fable", "claude-opus",  # Платные Claude
    "muse-spark",  # Платные Meta
    "mercury", "schematron",  # Платные модели
]

# Ограничение на количество моделей для проверки (чтобы не перегружать)
MAX_MODELS_TO_PROBE = 50


def load_env() -> None:
    env_file = PROJECT_DIR / ".env"
    if not env_file.exists():
        return
    for raw_line in env_file.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))
    if not os.getenv("OMNIROUTE_URL"):
        os.environ["OMNIROUTE_URL"] = "http://127.0.0.1:20128"


def log(message: str) -> None:
    print(f"[omniroute-health] {message}", flush=True)


class OmniRoute:
    def __init__(self) -> None:
        self.base_url = os.getenv("OMNIROUTE_URL", "http://127.0.0.1:20128").rstrip("/")
        self.password = os.getenv("OMNIROUTE_INITIAL_PASSWORD", "omniro2026!")
        self.api_key = os.getenv("OMNIROUTE_API_KEY", "sk-omniroute")
        self.opener = build_opener(HTTPCookieProcessor(CookieJar()))

    def get_connected_providers(self) -> list[str]:
        """Получить список подключённых провайдеров (из .env ключей)."""
        providers = []
        env_providers = ["openrouter", "gemini", "groq", "mistral", "cerebras"]
        for provider in env_providers:
            if os.getenv(f"{provider.upper()}_API_KEY"):
                providers.append(provider)
        return providers

    def fetch_all_models(self) -> list[dict[str, Any]]:
        """Получить все доступные модели из OmniRoute API с кэшированием."""
        now = int(time.time())

        # Проверяем кэш
        if CATALOG_CACHE_FILE.exists():
            try:
                cached = json.loads(CATALOG_CACHE_FILE.read_text())
                cached_time = cached.get("timestamp", 0)
                if now - cached_time < CATALOG_CACHE_TTL:
                    log(f"Using cached model catalog (age: {now - cached_time}s)")
                    return cached.get("models", [])
            except (OSError, json.JSONDecodeError):
                pass

        # Запрашиваем свежий каталог
        request = Request(
            f"{self.base_url}/v1/models?prefix=alias",
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            with self.opener.open(request, timeout=PROBE_TIMEOUT) as response:
                data = json.loads(response.read().decode())
                models = data.get("data", [])

                # Сохраняем в кэш
                cache_data = {"timestamp": now, "models": models}
                CATALOG_CACHE_FILE.write_text(json.dumps(cache_data, indent=2))
                log(f"Fetched {len(models)} models from OmniRoute API (cached)")
                return models
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            log(f"Failed to fetch models: {exc}")
            # Если есть кэш, используем его даже если устарел
            if CATALOG_CACHE_FILE.exists():
                try:
                    cached = json.loads(CATALOG_CACHE_FILE.read_text())
                    log("Using stale cached model catalog as fallback")
                    return cached.get("models", [])
                except (OSError, json.JSONDecodeError):
                    pass
            return []

    def get_model_candidates(
        self, require_tool_calls: bool
    ) -> list[dict[str, Any]]:
        """Автоматически собрать кандидатов из OmniRoute API.

        Фильтрует модели по:
        - Подключённому провайдеру (есть API ключ в .env)
        - Поддержке tool-calls (если требуется для agent пула)
        - Исключённым провайдерам
        - Платным паттернам ( effort tiers, premium prefixes)
        - Для openrouter: только модели с :free суффиксом
        - Для direct провайдеров: все модели (они имеют бесплатные тиры)
        - Ограничивает количество моделей для проверки
        """
        all_models = self.fetch_all_models()
        connected_providers = set(self.get_connected_providers())
        candidates = []

        for model in all_models:
            # Пропускаем комбо и модели без провайдера
            if model.get("owned_by") == "combo":
                continue

            model_id = model.get("id", "")
            if not model_id:
                continue

            # Определяем провайдера из model_id (формат: provider/model или provider/model:variant)
            parts = model_id.split("/")
            if len(parts) < 2:
                continue

            provider = parts[0]
            if provider in EXCLUDED_PROVIDERS:
                continue

            # Проверяем, что провайдер подключён
            if provider not in connected_providers:
                continue

            # Для openrouter: только бесплатные модели (:free суффикс)
            if provider == "openrouter" and ":free" not in model_id:
                continue

            # Фильтруем платные модели по паттернам
            model_lower = model_id.lower()
            if any(pattern in model_lower for pattern in PAID_MODEL_PATTERNS):
                continue

            # Проверяем поддержку tool-calls (для agent пула)
            capabilities = model.get("capabilities", {})
            if require_tool_calls and not capabilities.get("tool_calling", False):
                continue

            # Собираем информацию о модели
            model_name = parts[1] if len(parts) > 1 else model_id
            # Убираем суффиксы вроде :free
            if ":free" in model_name:
                model_name = model_name.split(":free")[0]

            # Веса: выше для direct провайдеров, моделей с большим контекстом и tool-calls
            context_length = model.get("context_length", 0)
            weight = 3  # базовый вес
            if provider in DIRECT_PROVIDERS:
                weight += 3  # приоритет direct провайдеров
            if context_length >= 100000:
                weight += 2
            elif context_length >= 32000:
                weight += 1
            if require_tool_calls:
                weight += 1  # приоритет для моделей с tool-calls

            candidates.append(
                {
                    "provider": provider,
                    "model": model_id,
                    "weight": weight,
                    "context_length": context_length,
                }
            )

        # Сортируем по весу (убывание)
        candidates.sort(key=lambda x: x["weight"], reverse=True)

        # Ограничиваем количество моделей для проверки
        if len(candidates) > MAX_MODELS_TO_PROBE:
            log(f"Limiting candidates from {len(candidates)} to {MAX_MODELS_TO_PROBE}")
            candidates = candidates[:MAX_MODELS_TO_PROBE]

        return candidates

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        body = json.dumps(payload).encode() if payload is not None else None
        headers = {"Content-Type": "application/json"}
        request = Request(f"{self.base_url}{path}", data=body, headers=headers, method=method)
        with self.opener.open(request, timeout=PROBE_TIMEOUT) as response:
            return json.loads(response.read().decode())

    def login(self) -> None:
        result = self.request("POST", "/api/auth/login", {"password": self.password})
        if result.get("success") is not True:
            raise RuntimeError("management API login failed")

    def probe(self, model: str, require_tool_calls: bool) -> tuple[bool, str]:
        if require_tool_calls:
            payload: dict[str, Any] = {
                "model": model,
                "messages": [{"role": "user", "content": "Use the probe tool exactly once."}],
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "probe",
                            "description": "Health probe",
                            "parameters": {"type": "object", "properties": {}},
                        },
                    }
                ],
                "tool_choice": {"type": "function", "function": {"name": "probe"}},
                "stream": True,
            }
        else:
            payload = {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with exactly: pong"}],
                "max_tokens": 40,
                "stream": True,
            }
        request = Request(
            f"{self.base_url}/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        chunks = []
        try:
            with self.opener.open(request, timeout=PROBE_TIMEOUT) as response:
                while True:
                    chunk = response.read(4096)
                    if not chunk:
                        break
                    chunks.append(chunk.decode(errors="replace"))
                    text = "".join(chunks)
                    if '"error"' in text:
                        return False, "error in stream: " + text.strip()[:160]
                    if "data: [DONE]" in text:
                        if require_tool_calls:
                            if '"tool_calls"' in text and '"finish_reason":"tool_calls"' in text:
                                return True, "tool-call stream completed"
                            return False, "stream completed without tool_calls"
                        if '"content"' in text:
                            return True, "chat stream completed"
                        return False, "stream completed without assistant content"
                    if len(text) > 512_000:
                        return False, "probe response exceeded 512 KiB"
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            return False, str(exc)
        text = "".join(chunks)
        if require_tool_calls:
            if '"tool_calls"' in text and '"finish_reason":"tool_calls"' in text:
                return True, "tool-call stream completed without DONE marker"
            return False, "stream ended without [DONE]"
        if '"content"' in text:
            return True, "chat stream completed without DONE marker"
        return False, "stream ended without [DONE] or assistant content"

    def update_combo(self, name: str, models: list[dict[str, Any]]) -> None:
        combos = self.request("GET", "/api/combos").get("combos", [])
        combo = next((item for item in combos if item.get("name") == name), None)
        payload = {"name": name, "strategy": "auto", "models": models}
        if combo:
            self.request("PUT", f"/api/combos/{combo['id']}", payload)
        else:
            self.request("POST", "/api/combos", payload)

    def remove_combo(self, name: str) -> None:
        combos = self.request("GET", "/api/combos").get("combos", [])
        combo = next((item for item in combos if item.get("name") == name), None)
        if combo:
            request = Request(
                f"{self.base_url}/api/combos/{combo['id']}",
                headers={"Content-Type": "application/json"},
                method="DELETE",
            )
            with self.opener.open(request, timeout=PROBE_TIMEOUT) as response:
                response.read()


def load_state() -> dict[str, dict[str, Any]]:
    try:
        value = json.loads(STATE_FILE.read_text())
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict[str, dict[str, Any]]) -> None:
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2) + "\n")
    temporary.replace(STATE_FILE)


def run_pool(route: OmniRoute, state: dict[str, dict[str, Any]], combo: str,
             candidates: list[dict[str, Any]], require_tool_calls: bool) -> int:
    now = int(time.time())
    active = []
    mode = "agent" if require_tool_calls else "chat"
    for candidate in candidates:
        provider = candidate["provider"]
        model_id = candidate["model"]
        # Пропускаем, если нет API ключа (хотя get_model_candidates уже фильтрует)
        if not os.getenv(f"{provider.upper()}_API_KEY"):
            log(f"skip {mode}: {provider} (no API key)")
            continue
        # Используем model_id как ключ в state (он уникален)
        key = f"{mode}/{model_id}"
        previous = state.get(key, {})
        checked = int(previous.get("checked", 0))
        ttl = SUCCESS_TTL if previous.get("ok") else FAILURE_COOLDOWN
        if now - checked < ttl:
            if previous.get("ok"):
                active.append(candidate)
                log(f"cached OK ({mode}): {key}")
            else:
                log(f"cooldown ({mode}): {key} ({previous.get('reason', 'failed')})")
            continue

        ok, reason = route.probe(model_id, require_tool_calls)
        state[key] = {"checked": now, "ok": ok, "reason": reason}
        if ok:
            active.append(candidate)
            log(f"OK ({mode}): {key} ({reason})")
        else:
            log(f"FAIL ({mode}): {key} ({reason})")

    if not active:
        try:
            route.remove_combo(combo)
        except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
            log(f"{combo} could not be removed: {exc}")
            return 1
        log(f"{combo} removed: no healthy models")
        return 1

    try:
        route.update_combo(combo, active)
    except (HTTPError, URLError, OSError, json.JSONDecodeError) as exc:
        log(f"{combo} update failed: {exc}")
        return 1
    log(f"{combo} updated ({mode}): " + ", ".join(item["model"] for item in active))
    return 0


def run_once() -> int:
    load_env()
    route = OmniRoute()
    state = load_state()

    try:
        route.login()
    except (HTTPError, URLError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        log(f"login failed: {exc}")
        return 1

    code = 0

    # Динамически получаем кандидатов для agent пула (требуются tool-calls)
    log("Fetching agent pool candidates (require tool_calls)...")
    agent_candidates = route.get_model_candidates(require_tool_calls=True)
    if agent_candidates:
        log(f"Found {len(agent_candidates)} agent candidates")
        code |= run_pool(route, state, AGENT_COMBO, agent_candidates, True)
    else:
        log("No agent candidates found, skipping agent pool")

    # Динамически получаем кандидаты для chat пула (tool-calls не обязательны)
    log("Fetching chat pool candidates (no tool_calls requirement)...")
    chat_candidates = route.get_model_candidates(require_tool_calls=False)
    if chat_candidates:
        log(f"Found {len(chat_candidates)} chat candidates")
        code |= run_pool(route, state, CHAT_COMBO, chat_candidates, False)
    else:
        log("No chat candidates found, skipping chat pool")

    save_state(state)
    return code


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="repeat health checks")
    parser.add_argument("--interval", type=int, default=300, help="loop interval in seconds")
    args = parser.parse_args()
    if not args.loop:
        return run_once()
    while True:
        run_once()
        time.sleep(max(30, args.interval))


if __name__ == "__main__":
    sys.exit(main())