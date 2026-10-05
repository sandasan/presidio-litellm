import json
import logging
import os
import hashlib
from typing import Optional

import httpx
from litellm.integrations.custom_logger import CustomLogger

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192").strip())

logger = logging.getLogger(__name__)
PRESIDIO_ANALYZER_API_BASE = os.getenv(
    "PRESIDIO_ANALYZER_API_BASE", "http://presidio:5001"
)
PRESIDIO_ANALYZER_URL = PRESIDIO_ANALYZER_API_BASE + "/analyze"
PRESIDIO_ANONYMIZER_URL = PRESIDIO_ANALYZER_API_BASE + "/anonymize"
LITELLM_INTERNAL_REQUEST_FIELDS = {
    "litellm_call_id",
    "litellm_logging_obj",
    "proxy_server_request",
    "secret_fields",
    "standard_logging_object",
}
TOOL_SCHEMA_ENTITIES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_SSN",
    "SECRET_KEY",
    "DB_CONNECTION",
    "INTERNAL_IP",
    "INTERNAL_HOST",
    "INTERNAL_PATH",
]

# ---- HTTP connection pool (singleton) ----
_presidio_client: Optional[httpx.AsyncClient] = None


def get_presidio_client() -> httpx.AsyncClient:
    """Возвращает singleton AsyncClient с пулингом соединений к Presidio."""
    global _presidio_client
    if _presidio_client is None or _presidio_client.is_closed:
        limits = httpx.Limits(max_connections=20, max_keepalive_connections=10)
        timeout = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)
        _presidio_client = httpx.AsyncClient(limits=limits, timeout=timeout)
    return _presidio_client


# ---- LRU кэш для ответов Presidio ----
# Ключ: sha256(text)[:16], значение: анонимизированный текст
# Размер кэша: 1024 записи (память ~ несколько MB)
_ANONYMIZE_CACHE_SIZE = 1024
_anonymize_cache: dict[str, str] = {}
_anonymize_cache_order: list[str] = []


def _cache_get(key: str) -> Optional[str]:
    """LRU get: перемещает ключ в конец (most recently used)."""
    if key in _anonymize_cache:
        _anonymize_cache_order.remove(key)
        _anonymize_cache_order.append(key)
        return _anonymize_cache[key]
    return None


def _cache_put(key: str, value: str) -> None:
    """LRU put: удаляет старые записи при переполнении."""
    if key in _anonymize_cache:
        _anonymize_cache_order.remove(key)
    elif len(_anonymize_cache) >= _ANONYMIZE_CACHE_SIZE:
        oldest = _anonymize_cache_order.pop(0)
        _anonymize_cache.pop(oldest, None)
    _anonymize_cache[key] = value
    _anonymize_cache_order.append(key)


def _make_cache_key(text: str) -> str:
    """Хеш текста для ключа кэша (первые 16 байт sha256)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


class ChatPayloadGuard(CustomLogger):
    """Reject request formats or tool schemas that bypass Presidio masking."""

    async def _anonymize_text(self, client, text):
        if not text:
            return text
        response = await client.post(
            PRESIDIO_ANONYMIZER_URL,
            json={"text": text, "language": "en"},
        )
        response.raise_for_status()
        anonymized = response.json()
        if not isinstance(anonymized, dict) or not isinstance(anonymized.get("text"), str):
            raise ValueError("Invalid Presidio sanitizer response")
        return anonymized["text"]

    async def _anonymize_metadata(self, client, value):
        if isinstance(value, str):
            return await self._anonymize_text(client, value)
        if isinstance(value, list):
            return [await self._anonymize_metadata(client, item) for item in value]
        if isinstance(value, dict):
            sanitized = {}
            for key, item in value.items():
                safe_key = await self._anonymize_text(client, key) if isinstance(key, str) else key
                if key == "pii_tokens":
                    sanitized[safe_key] = item
                else:
                    sanitized[safe_key] = await self._anonymize_metadata(client, item)
            return sanitized
        return value

    async def _contains_pii(self, client, value, entities=None):
        if isinstance(value, str):
            if not value:
                return False
            payload = {"text": value, "language": "en"}
            if entities is not None:
                payload["entities"] = entities
            response = await client.post(
                PRESIDIO_ANALYZER_URL,
                json=payload,
            )
            response.raise_for_status()
            results = response.json()
            if not isinstance(results, list):
                raise ValueError("Invalid Presidio analyzer response")
            return bool(results)
        if isinstance(value, list):
            for item in value:
                if await self._contains_pii(client, item, entities):
                    return True
        elif isinstance(value, dict):
            for key, item in value.items():
                if await self._contains_pii(client, key, entities):
                    return True
                if await self._contains_pii(client, item, entities):
                    return True
        return False

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict):
            return data

        client = get_presidio_client()

        # 1. Защита tool_schemas: если схема содержит PII — маскируем или режект
        if "tools" in data and isinstance(data["tools"], list):
            for tool in data["tools"]:
                if not isinstance(tool, dict):
                    continue
                func = tool.get("function")
                if not isinstance(func, dict):
                    continue
                params = func.get("parameters")
                if isinstance(params, dict) and await self._contains_pii(client, params, TOOL_SCHEMA_ENTITIES):
                    logger.warning("Tool schema contains PII, rejecting request")
                    raise ValueError("Tool schema contains PII; use runtime arguments instead")

        # 2. Проверка метаданных на утечки (кроме pii_tokens)
        for key in LITELLM_INTERNAL_REQUEST_FIELDS:
            data.pop(key, None)

        if "metadata" in data and isinstance(data["metadata"], dict):
            meta = data["metadata"]
            for key, value in list(meta.items()):
                if key == "pii_tokens":
                    continue
                if await self._contains_pii(client, value):
                    logger.warning(f"Metadata field '{key}' contains PII, removing")
                    meta.pop(key, None)

        return data

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        return response


class MaxTokensClamp(CustomLogger):
    """Clamp max_tokens to a safe upper bound."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict):
            return data
        max_tokens = data.get("max_tokens")
        if isinstance(max_tokens, int) and max_tokens > MAX_OUTPUT_TOKENS:
            data["max_tokens"] = MAX_OUTPUT_TOKENS
        return data


class LiteralSecretMasker(CustomLogger):
    """
    LiteLLM callback: маскирует PII в запросе (pre_call),
    восстанавливает в ответе (post_call + streaming).
    Использует singleton HTTP клиент + LRU кэш для ускорения.
    """

    def __init__(self):
        super().__init__()
        self.enabled = True
        self._placeholder_map: dict[str, str] = {}  # placeholder -> original

    async def _mask_text(self, text: str) -> str:
        """Отправляет текст в Presidio, маскирует, сохраняет маппинг."""
        if not text:
            return text

        # --- Проверка кэша ---
        cache_key = _make_cache_key(text)
        cached = _cache_get(cache_key)
        if cached is not None:
            return cached

        client = get_presidio_client()

        # 1. Анализ
        analyze_resp = await client.post(
            PRESIDIO_ANALYZER_URL,
            json={"text": text, "language": "en"},
        )
        analyze_resp.raise_for_status()
        analyzer_results = analyze_resp.json()
        if not isinstance(analyzer_results, list):
            raise ValueError("Invalid Presidio analyzer response")
        if not analyzer_results:
            _cache_put(cache_key, text)
            return text

        # 2. Анонимизация
        anon_resp = await client.post(
            PRESIDIO_ANONYMIZER_URL,
            json={"text": text, "analyzer_results": analyzer_results},
        )
        anon_resp.raise_for_status()
        anon_data = anon_resp.json()
        if not isinstance(anon_data, dict) or not isinstance(anon_data.get("text"), str):
            raise ValueError("Invalid Presidio anonymizer response")
        anonymized = anon_data["text"]

        # 3. Сохраняем маппинг placeholder -> original для де-анонимизации
        for result in analyzer_results:
            entity_type = result["entity_type"]
            start = result["start"]
            end = result["end"]
            original = text[start:end]
            placeholder = f"<{entity_type}_{len(self._placeholder_map) + 1}>"
            self._placeholder_map[placeholder] = original

        _cache_put(cache_key, anonymized)
        return anonymized

    def _restore_text(self, text: str) -> str:
        """Восстанавливает оригинальные значения из маппинга."""
        if not text:
            return text
        for placeholder, original in self._placeholder_map.items():
            text = text.replace(placeholder, original)
        return text

    async def _walk(self, obj):
        """Рекурсивно маскирует строки в словаре/списке."""
        if isinstance(obj, str):
            return await self._mask_text(obj)
        if isinstance(obj, list):
            return [await self._walk(x) for x in obj]
        if isinstance(obj, dict):
            return {k: await self._walk(v) for k, v in obj.items()}
        return obj

    def _restore_walk(self, obj):
        if isinstance(obj, str):
            return self._restore_text(obj)
        if isinstance(obj, list):
            return [self._restore_walk(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._restore_walk(v) for k, v in obj.items()}
        return obj

    # --- LiteLLM hooks ----------------------------------------------------

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not self.enabled or not isinstance(data, dict):
            return data
        self._placeholder_map.clear()
        data = await self._walk(data)
        return data

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        if not self.enabled:
            return response
        if hasattr(response, "choices"):
            for choice in response.choices:
                msg = getattr(choice, "message", None)
                if msg is None:
                    continue
                if isinstance(msg.content, str):
                    msg.content = self._restore_text(msg.content)
                if getattr(msg, "tool_calls", None):
                    for tc in msg.tool_calls:
                        fn = getattr(tc, "function", None)
                        if fn is not None and isinstance(getattr(fn, "arguments", None), str):
                            fn.arguments = self._restore_text(fn.arguments)
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        if not self.enabled:
            async for chunk in response:
                yield chunk
            return
        async for chunk in response:
            try:
                if chunk.choices:
                    for choice in chunk.choices:
                        delta = getattr(choice, "delta", None)
                        if delta is not None:
                            if isinstance(getattr(delta, "content", None), str):
                                delta.content = self._restore_text(delta.content)
                            if getattr(delta, "tool_calls", None):
                                for tc in delta.tool_calls:
                                    fn = getattr(tc, "function", None)
                                    if fn is not None and isinstance(
                                        getattr(fn, "arguments", None), str
                                    ):
                                        fn.arguments = self._restore_text(fn.arguments)
                message = getattr(chunk, "message", None)
                if message is not None:
                    content = getattr(message, "content", None)
                    if isinstance(content, str):
                        message.content = self._restore_text(content)
            except Exception:  # noqa: BLE001 — никогда не ломаем стрим из-за маскера
                pass
            yield chunk


proxy_handler_instance = MaxTokensClamp()
secret_masker_instance = LiteralSecretMasker()
chat_payload_guard_instance = ChatPayloadGuard()
