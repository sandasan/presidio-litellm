#!/usr/bin/env python3
"""Генерация config.yaml для LiteLLM-прокси.

Маршрутизация сведена к ОДНОМУ статичному пути: LiteLLM -> OmniRoute ->
провайдер. Опрос облачных провайдеров и прямой shuffle в LiteLLM убраны как
избыточные — те же провайдеры и ключи уже подключены в OmniRoute (провижининг
`provision_omniroute.sh`), а OmniRoute сам выбирает модель, агрегирует квоты и
фолбэчится между free-тирами.

Анонимизация при этом не страдает: callback `presidio` из `litellm_settings`
применяется ко ВСЕМ исходящим запросам LiteLLM, в т.ч. к единственному
маршруту к OmniRoute — PII маскируется до выхода из прокси.

Скрипт использует только стандартную библиотеку, чтобы работать и на хосте,
и внутри контейнера litellm без лишних зависимостей.
"""

import os
from typing import Sequence

MODEL_NAME_AUTO = "cloud-sanitized-auto"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# --- Утилиты ----------------------------------------------------------------


def log(msg: str) -> None:
    print(f"[generate-config] {msg}", flush=True)


def omniroute_route() -> dict:
    """Единственный маршрут: LiteLLM -> OmniRoute -> провайдер.

    OmniRoute сам выбирает целевую модель (комбо `cloud-auto`, стратегия auto):
    агрегирует квоты бесплатных моделей openrouter/gemini/groq/mistral, уходит с
    исчерпавших лимит на живых и ретраит. Комбо создаётся скриптом
    provision_omniroute.sh при первом запуске стека.
    Запросы приходят сюда уже деидентифицированными через Presidio (callback в
    litellm_settings ниже), поэтому PII-защита сохраняется на всём пути.

    `model_info.max_input_tokens`: OmniRoute репортит для комбо 32 768 (контекст
    самой маленькой бесплатной модели), но Hermes-агенту нужен контекст >= 64K.
    Реальный минимум ограничивается выбранной моделью, а OmniRoute при запросе
    сам рулит выбором по fit (context-fit среди факторов). Здесь заявляем 128K,
    чтобы Hermes принял модель; большие контексты OmniRoute уведёт на модели
    с достаточным окном (gemini-flash и т.п.).
    """
    return {
        "model": "openai/cloud-auto",
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
            # Один маршрут к OmniRoute: полный fallback уже внутри OmniRoute,
            # поэтому на стороне LiteLLM оставляем лишь скромный ретрай на 429
            # (все бесплатные квоты одновременно пусты) — иначе вернём 429 клиенту.
            "router_settings:",
            "  num_retries: 2",
            "  cooldown_time: 60",
            "",
            "litellm_settings:",
            '  callbacks: ["presidio", "custom_callbacks.proxy_handler_instance"]',
            "",
        ]
    )


def main() -> None:
    routes: list[tuple[str, dict]] = [(MODEL_NAME_AUTO, omniroute_route())]

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