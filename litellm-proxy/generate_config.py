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
MODEL_NAME_OPENCODE = "opencode-free-anonymized"
OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.yaml")

# Именованные маршруты: auto (все провайдеры) + chat (чат-пул) + по одному на
# провайдера. Список включаемых маршрутов передаётся переменной LITELLM_ROUTES
# (например "auto,chat,mistral,gemini,groq"), которую docker-compose собирает из
# .env-ключей. Контейнер litellm не получает сами ключи провайдеров — только имя
# маршрута; комбо `cloud-<provider>` создаёт provision_omniroute.sh на хосте.
# Пользователь выбирает модель через DEFAULT_MODEL в .env или при запуске
# hermes-chat флагом -m.
# def build_routes() -> list[tuple[str, dict]]:
#     raw = os.environ.get("LITELLM_ROUTES", "auto").strip()
#     routes: list[tuple[str, dict]] = []
#     for item in raw.split(","):
#         name = item.strip()
#         if not name:
#             continue
#         if name == "auto":
#             route_name, combo = MODEL_NAME_AUTO, "cloud-auto"
#         elif name == "chat":
#             route_name, combo = MODEL_NAME_CHAT, "cloud-chat"
#         elif name == "opencode":
#             # НОВЫЙ МАРШРУТ: Напрямую на бесплатный эндпоинт OpenCode
#             route_name = MODEL_NAME_OPENCODE
#             routes.append((route_name, opencode_free_route()))
#         else:
#             route_name, combo = f"cloud-sanitized-{name}", f"cloud-{name}"
#         routes.append((route_name, omniroute_route(combo)))
#     return routes

def build_routes() -> list[tuple[str, dict]]:
    raw = os.environ.get("LITELLM_ROUTES", "auto,opencode").strip()
    routes: list[tuple[str, dict]] = []
    for item in raw.split(","):
        name = item.strip()
        if not name:
            continue
        if name == "auto":
            routes.append((MODEL_NAME_AUTO, omniroute_route("cloud-auto")))
        elif name == "chat":
            routes.append((MODEL_NAME_CHAT, omniroute_route("cloud-chat")))
        elif name == "opencode":
            # ХАРДКОДИМ ТОЧНЫЕ ИМЕНА, КОТОРЫЕ ТРЕБУЕТ ПЛАГИН PHPSTORM
            # Это заставит LiteLLM распознать их и пропустить валидацию 400
            routes.append(("big-pickle", opencode_free_upstream_route("big-pickle")))
            routes.append(("gpt-5.4-nano", opencode_free_upstream_route("gpt-5.4-nano")))
            routes.append((MODEL_NAME_OPENCODE, opencode_free_upstream_route("big-pickle")))
        else:
            routes.append((f"cloud-sanitized-{name}", omniroute_route(f"cloud-{name}")))
    return routes


# --- Утилиты ----------------------------------------------------------------

def opencode_free_upstream_route(target_model_name: str) -> dict:
    """Формирует легитимный OpenAI-маршрут для LiteLLM.

    Передает forward_client_headers, чтобы сохранить сессию,
    и жестко привязывает имя модели для апстрима.
    """
    return {
        "model": f"openai/{target_model_name}", # Показываем LiteLLM, что это валидный OpenAI эндпоинт
        "api_base": "https://104.21.32",
        "api_key": "not-needed-for-free-tier",
        "forward_client_headers": True,
        "max_tokens": 4096,
        "model_info": {"max_input_tokens": 131072},
        "guardrails": ["presidio-anonymizer"]
    }

# def opencode_free_route() -> dict:
    """Прямой маршрут LiteLLM -> OpenCode Free Endpoints с сохранением сессии.

    forward_client_headers: true — критически важен. Он заставляет LiteLLM
    пересылать x-opencode-* токены авторизации, которые генерирует плагин PhpStorm.
    """
#     return {
#         "model": "openai/custom",
#         "api_base": "https://opencode.ai",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True, # LiteLLM запишет это как forward_client_headers: true
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#     }

# def opencode_free_route() -> dict:
#     return {
#         "model": "openai/gpt-4", # Помогаем внутреннему парсеру LiteLLM, чтобы он включил текстовый гардрейл
#         "api_base": "https://opencode.ai",
#         "api_key": "not-needed",
#         "forward_client_headers": True, # LiteLLM запишет это как forward_client_headers: true
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#     }

# def opencode_free_route() -> dict:
#     """Прямой маршрут LiteLLM -> OpenCode Free с жестким вызовом Presidio."""
#     return {
#         "model": "openai/custom",
#         "api_base": "https://opencode.ai",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True,
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#         # ЖЕСТКАЯ ПРИВЯЗКА ГАРДРЕЙЛА К ЭТОЙ МОДЕЛИ:
#         "guardrails": ["presidio-anonymizer"]
#     }

# def opencode_free_route() -> dict:
#     """Маршрут, мимикрирующий под стандартный OpenAI для принудительного вызова Presidio."""
#     return {
#         "model": "openai/gpt-4o",  # МЕНЯЕМ НА СТАНДАРТНУЮ МОДЕЛЬ OPENAI
#         "api_base": "https://opencode.ai",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True,
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#         "guardrails": ["presidio-anonymizer"]
#     }

# def opencode_free_route() -> dict:
#     """Маршрут с полной мимикрией под OpenAI.
#
#     Заставляет LiteLLM активировать весь пайплайн (guardrails + callbacks)
#     для OpenAI моделей, но физически шлет данные на OpenCode.
#     """
#     return {
#         # Говорим LiteLLM, что апстрим — это стандартный OpenAI gpt-4o
#         "model": "openai/gpt-4o",
#         # Переопределяем адрес назначения на эндпоинт OpenCode
#         "api_base": "https://opencode.ai",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True,
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#     }

# def opencode_free_route() -> dict:
#     return {
#         "model": "openai/gpt-4o",
#         # Реальный IP серверов OpenCode и базовый путь бесплатного API
#         "api_base": "https://172.19.0.1",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True,
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#         "guardrails": ["presidio-anonymizer"]
#     }

# def opencode_free_route() -> dict:
#     """Маршрут с жесткой подменой модели для апстрима OpenCode."""
#     return {
#         "model": "openai/big-pickle",  # Указываем базовую модель, которую просит плагин
#         "api_base": "https://104.21.32",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True,
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#         "guardrails": ["presidio-anonymizer"]
#     }

# def opencode_free_route() -> dict:
#     return {
#         "model": "openai/custom",
#         "api_base": "https://104.21.32",
#         "api_key": "not-needed-for-free-tier",
#         "forward_client_headers": True,
#         # Заставляем LiteLLM принудительно подменить имя модели в уходящем JSON:
#         "custom_llm_provider": "openai",
#         "litellm_settings": {
#             "force_model": "big-pickle"
#         },
#         "max_tokens": 4096,
#         "model_info": {"max_input_tokens": 131072},
#         "guardrails": ["presidio-anonymizer"]
#     }

def opencode_free_route(target_model_name: str) -> dict:
    return {
        "model": f"custom_proxy/{target_model_name}", # Указываем тип custom_proxy
        "api_base": "https://104.21.32",
        "api_key": "not-needed-for-free-tier",
        "forward_client_headers": True,
        "max_tokens": 4096,
        "model_info": {"max_input_tokens": 131072},
    }


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


"""def build_config(routes: Sequence[tuple[str, dict]]) -> str:
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
    )"""

"""def build_config(routes: Sequence[tuple[str, dict]]) -> str:
    return "\n".join(
        [
            emit_models(routes),
            "",
            "router_settings:",
            "  num_retries: 2",
            "  cooldown_time: 60",
            "",
            "litellm_settings:",
            # Добавляем "presidio" прямо в список глобальных callbacks — это самый стабильный
            # способ в LiteLLM активировать плагин без конфликтов с парсером YAML.
            '  callbacks: ["custom_callbacks.proxy_handler_instance", "custom_callbacks.secret_masker_instance", "presidio"]',
            "",
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
    )"""

# def build_config(routes: Sequence[tuple[str, dict]]) -> str:
    # return "\n".join(
        # [
            # emit_models(routes),
            # "",
            # "router_settings:",
            # "  num_retries: 2",
            # "  cooldown_time: 60",
            # "",
            # "litellm_settings:",
            # '  callbacks: ["custom_callbacks.proxy_handler_instance", "custom_callbacks.secret_masker_instance", "presidio"]',
            # "",
            # "guardrails:",
            # "  - guardrail_name: presidio-anonymizer",
            # "    litellm_params:",
            # "      guardrail: presidio",
            # "      mode: pre_call",
            # "      default_on: true",
            # "      output_parse_pii: true",
            # "      presidio_filter_scope: input",
            # "      presidio_language: \"en\""  # ФИКСИРУЕМ ЯЗЫК ДЛЯ АНАЛИЗАТОРА ТУТ
            # "",
        # ]
    # )

def build_config(routes: Sequence[tuple[str, dict]]) -> str:
    return "\n".join(
        [
            emit_models(routes),
            "",
            "router_settings:",
            "  num_retries: 2",
            "  cooldown_time: 60",
            "",
            "litellm_settings:",
            '  callbacks: ["custom_callbacks.proxy_handler_instance", "custom_callbacks.secret_masker_instance", "presidio"]',
            # КЛЮЧЕВЫЕ НАСТРОЙКИ ДЛЯ ПРЯМОГО ПРОБРОСА:
            "  allow_unsupported_deployments: true", # Разрешаем модели, которых нет в статичном списке
            "  fall_back_to_passthrough_filter_path: true", # Пропускаем неизвестные URL-пути вроде /responses дальше
            "",
            "guardrails:",
            "  - guardrail_name: presidio-anonymizer",
            "    litellm_params:",
            "      guardrail: presidio",
            "      mode: pre_call",
            "      default_on: true",
            "      output_parse_pii: true",
            "      presidio_filter_scope: input",
            "      presidio_language: \"en\"",
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
