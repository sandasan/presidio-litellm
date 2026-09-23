#!/usr/bin/env python3
"""Генерация config.yaml для LiteLLM-прокси.

Маршрутизация сведена к статичным путям LiteLLM -> OmniRoute -> провайдер:
основной `cloud-sanitized-auto` (комбо `cloud-auto` для агента Hermes — только
модели с tool-calls), `cloud-sanitized-chat` (комбо `cloud-chat` для Open WebUI
— без требований к tool-calls, широкий пул free-моделей) и именованные
`cloud-sanitized-<provider>` (комбо `cloud-<provider>` для фиксированного
провайдера). Исчерпание квот одним пулом не влияет на другой — это
разделение и есть цель двух комбо. Опрос облачных провайдеров и прямой shuffle
в LiteLLM убраны как избыточные — те же провайдеры и ключи уже подключены в
OmniRoute (провижининг `provision_omniroute.sh`), а OmniRoute сам выбирает
модель, агрегирует квоты и фолбэчится между free-тирами.

Анонимизация при этом не страдает: пресidio-гардрейл из секции `guardrails`
(`guardrail: presidio`, `default_on: true`) применяется ко ВСЕМ запросам LiteLLM,
в т.ч. к каждому маршруту к OmniRoute — PII маскируется до выхода из прокси.
Дополнительно включена де-анонимизация ответа (`output_parse_pii` +
`presidio_filter_scope: input`): входящий запрос маскируется токенами
`<PERSON_1>` и т.п., а ответ модели (и аргументы tool-call'ов) восстанавливаются
к оригинальным значениям, чтобы Hermes и пользователь локально видели реальные
данные. В облако при этом уходят только заглушки.

Скрипт использует только стандартную библиотеку, чтобы работать и на хосте,
и внутри контейнера litellm без лишних зависимостей.
"""

import os
from typing import Sequence

MODEL_NAME_AUTO = "cloud-sanitized-auto"
MODEL_NAME_CHAT = "cloud-sanitized-chat"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# Именованные маршруты: auto (все провайдеры) + chat (чат-пул) + по одному на
# провайдера. Список включаемых маршрутов передаётся переменной LITELLM_ROUTES
# (например "auto,chat,mistral,gemini,groq"), которую docker-compose собирает из
# .env-ключей. Контейнер litellm не получает сами ключи провайдеров — только имя
# маршрута; комбо `cloud-<provider>` создаёт provision_omniroute.sh на хосте.
# Пользователь выбирает модель через DEFAULT_MODEL в .env или при запуске
# hermes-chat флагом -m.
def build_routes() -> list[tuple[str, dict]]:
    raw = os.environ.get("LITELLM_ROUTES", "auto").strip()
    routes: list[tuple[str, dict]] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if name == "auto":
            route_name, combo = MODEL_NAME_AUTO, "cloud-auto"
        elif name == "chat":
            route_name, combo = MODEL_NAME_CHAT, "cloud-chat"
        else:
            route_name, combo = f"cloud-sanitized-{name}", f"cloud-{name}"
        routes.append((route_name, omniroute_route(combo)))
    return routes


# --- Утилиты ----------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[generate-config] {msg}", flush=True)


def omniroute_route(combo: str) -> dict:
    """Маршрут LiteLLM -> OmniRoute (комбо `combo`) -> провайдер.

    OmniRoute сам выбирает целевую модель (стратегия auto для комбо): агрегирует
    квоты бесплатных моделей, уходит с исчерпавших лимит на живых и ретраит.
    Комбо создаётся скриптом provision_omniroute.sh при первом запуске стека.
    Запросы приходят сюда уже деидентифицированными через Presidio (гардрейл в
    секции `guardrails` ниже), поэтому PII-защита сохраняется на всём пути,
    а ответ восстановливается к оригиналу через output_parse_pii.

    `model_info.max_input_tokens`: OmniRoute репортит для комбо 32 768 (контекст
    самой маленькой бесплатной модели), но Hermes-агенту нужен контекст >= 64K.
    Реальный минимум ограничивается выбранной моделью, а OmniRoute при запросе
    сам рулит выбором по fit (context-fit среди факторов). Здесь заявляем 128K,
    чтобы Hermes принял модель; большие контексты OmniRoute уведёт на модели
    с достаточным окном (gemini-flash и т.п.).
    """
    return {
        "model": f"openai/{combo}",
        "api_base": "http://omniroute:20128/v1",
        "api_key": "os.environ/OMNIROUTE_API_KEY",
        "max_tokens": 4096,
        "rpm": 30,
        "model_info": {"max_input_tokens": 131072},
    }


# --- Генерация YAML ----------------------------------------------------------


def emit_models(routes: Sequence[tuple[str, dict]]) -> str:
    lines = ["model_list:"]
    for model_name, params in routes:
        lines.append(f"  - model_name: {model_name}")
        model_info = params.pop("model_info", None)
        if model_info:
            lines.append("    model_info:")
            for k, v in model_info.items():
                lines.append(f"      {k}: {v}")
        lines.append("    litellm_params:")
        for k, v in params.items():
            if isinstance(v, int):
                lines.append(f"      {k}: {v}")
            else:
                lines.append(f"      {k}: \"{v}\"")
    return "\n".join(lines)


def build_config(routes: Sequence[tuple[str, dict]]) -> str:
    return "\n".join(
        [
            emit_models(routes),
            "",
            # Маршруты к OmniRoute: полный fallback уже внутри OmniRoute (в т.ч.
            # между провайдерами в комбо), поэтому на стороне LiteLLM оставляем
            # лишь скромный ретрай на 429 (все бесплатные квоты одновременно
            # пусты) — иначе вернём 429 клиенту.
            "router_settings:",
            "  num_retries: 2",
            "  cooldown_time: 60",
            "",
            "litellm_settings:",
            '  callbacks: ["custom_callbacks.proxy_handler_instance", "custom_callbacks.secret_masker_instance"]',
            "",
            # Presidio-гардрейл: маскирование входящего запроса (pre_call) и
            # восстановление оригинальных значений в ответе модели (post_call).
            # presidio_filter_scope: input => маскируем только то, что уходит
            # к провайдеру; ответ обратно не пере-маскируется. default_on: true
            # включает гардрейл для всех запросов без заголовков/префиксов.
            "guardrails:",
            "  - guardrail_name: presidio-anonymizer",
            "    litellm_params:",
            "      guardrail: presidio",
            "      mode: pre_call",
            "      default_on: true",
            "      output_parse_pii: true",
            "      presidio_filter_scope: input",
            "",
        ]
    )


def main() -> None:
    routes = build_routes()

    config_text = build_config(routes)
    with open(OUTPUT, "w") as f:
        f.write(config_text)
    log(f"Записан конфиг {OUTPUT} с {len(routes)} маршрутом:")
    for name, params in routes:
        log(f"  - {name} -> {params['model']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())