import json
import logging
import os
import hashlib
import re
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
# Ключ: sha256(text)[:16], значение: (анонимизированный текст, пары плейсхолдер→оригинал)
# Размер кэша: 1024 записи (память ~ несколько MB)
_ANONYMIZE_CACHE_SIZE = 1024
_anonymize_cache: dict[str, tuple[str, tuple[tuple[str, str], ...]]] = {}
_anonymize_cache_order: list[str] = []


def _cache_get(key: str) -> Optional[tuple[str, tuple[tuple[str, str], ...]]]:
    """LRU get: перемещает ключ в конец (most recently used)."""
    if key in _anonymize_cache:
        _anonymize_cache_order.remove(key)
        _anonymize_cache_order.append(key)
        return _anonymize_cache[key]
    return None


def _cache_put(key: str, value: tuple[str, tuple[tuple[str, str], ...]]) -> None:
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


PII_PLACEHOLDER_PATTERN = re.compile(r"<[A-Z][A-Z0-9_]*_[0-9a-f]{10}>")
OPENCODE_PLACEHOLDER_PATTERN = re.compile(
    r"<[A-Z][A-Z0-9_]*_(?:S[0-9a-f]{12}_\d+|\d+(?:_[0-9a-f]{32})?|[0-9a-f]{32})>"
)
_MAX_PLACEHOLDER_LEN = 96


def _detected_language(text: str) -> str:
    if re.search(r"[ҐґЄєІіЇї]", text):
        return "uk"
    if re.search(r"[а-яА-ЯёЁ]", text):
        return "ru"
    return "en"


def _placeholder_for(entity_type: str, original: str) -> str:
    digest = hashlib.sha256(f"{entity_type}:{original}".encode("utf-8")).hexdigest()[:10]
    return f"<{entity_type}_{digest}>"


def _merge_analyzer_spans(text: str, analyzer_results: list) -> list[dict]:
    spans = []
    for result in analyzer_results:
        if not isinstance(result, dict):
            continue
        start, end = result.get("start"), result.get("end")
        entity_type = result.get("entity_type")
        if (
            not isinstance(start, int)
            or isinstance(start, bool)
            or not isinstance(end, int)
            or isinstance(end, bool)
            or not isinstance(entity_type, str)
            or start < 0
            or start >= end
            or end > len(text)
        ):
            continue
        spans.append({"start": start, "end": end, "entity_type": entity_type})
    spans.sort(key=lambda item: (item["start"], item["end"]))
    merged = []
    for span in spans:
        if merged and span["start"] < merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], span["end"])
        else:
            merged.append(span)
    return merged


def _anonymize_from_spans(text: str, spans: list[dict], restore_map: dict[str, str]) -> str:
    parts = []
    cursor = 0
    for span in spans:
        original = text[span["start"]:span["end"]]
        placeholder = _placeholder_for(span["entity_type"], original)
        restore_map[placeholder] = original
        parts.extend((text[cursor:span["start"]], placeholder))
        cursor = span["end"]
    parts.append(text[cursor:])
    return "".join(parts)


def _restore_text(text: str, restore_map: dict[str, str]) -> str:
    if not text or not restore_map:
        return text

    def replace_token(match):
        return restore_map.get(match.group(0), match.group(0))

    text = PII_PLACEHOLDER_PATTERN.sub(replace_token, text)
    text = OPENCODE_PLACEHOLDER_PATTERN.sub(replace_token, text)
    return text


def _restore_stream_piece(pending: str, piece: str, restore_map: dict[str, str]) -> tuple[str, str]:
    combined = pending + piece
    split_at = max(0, len(combined) - _MAX_PLACEHOLDER_LEN)
    for match in PII_PLACEHOLDER_PATTERN.finditer(combined):
        if match.start() < split_at < match.end():
            split_at = match.start()
    for match in OPENCODE_PLACEHOLDER_PATTERN.finditer(combined):
        if match.start() < split_at < match.end():
            split_at = match.start()
    return _restore_text(combined[:split_at], restore_map), combined[split_at:]


def _call_id_from(data) -> str:
    if isinstance(data, dict):
        for key in ("litellm_call_id", "litellm_trace_id"):
            value = data.get(key)
            if isinstance(value, str) and value:
                return value
    return str(id(data))


class LiteralSecretMasker(CustomLogger):
    """
    LiteLLM callback: маскирует PII в запросе (pre_call),
    восстанавливает в ответе (post_call + streaming).
    Плейсхолдеры вставляем сами по спанам analyzer — они совпадают с map.
    """

    def __init__(self):
        super().__init__()
        self.enabled = True
        self._maps_by_call: dict[str, dict[str, str]] = {}

    async def _mask_text(self, text: str, restore_map: dict[str, str]) -> str:
        """Анализирует текст и заменяет спаны стабильными плейсхолдерами."""
        if not text:
            return text

        cache_key = _make_cache_key(text)
        cached = _cache_get(cache_key)
        if cached is not None:
            anonymized, pairs = cached
            restore_map.update(pairs)
            return anonymized

        client = get_presidio_client()
        analyze_resp = await client.post(
            PRESIDIO_ANALYZER_URL,
            json={"text": text, "language": _detected_language(text)},
        )
        analyze_resp.raise_for_status()
        analyzer_results = analyze_resp.json()
        if not isinstance(analyzer_results, list):
            raise ValueError("Invalid Presidio analyzer response")
        if not analyzer_results:
            _cache_put(cache_key, (text, ()))
            return text

        spans = _merge_analyzer_spans(text, analyzer_results)
        local_map: dict[str, str] = {}
        anonymized = _anonymize_from_spans(text, spans, local_map)
        restore_map.update(local_map)
        _cache_put(cache_key, (anonymized, tuple(local_map.items())))
        return anonymized

    async def _walk(self, obj, restore_map: dict[str, str]):
        """Рекурсивно маскирует строки в словаре/списке."""
        if isinstance(obj, str):
            return await self._mask_text(obj, restore_map)
        if isinstance(obj, list):
            return [await self._walk(x, restore_map) for x in obj]
        if isinstance(obj, dict):
            return {k: await self._walk(v, restore_map) for k, v in obj.items()}
        return obj

    def _restore_message_fields(self, msg, restore_map: dict[str, str]) -> None:
        if isinstance(getattr(msg, "content", None), str):
            msg.content = _restore_text(msg.content, restore_map)
        if getattr(msg, "tool_calls", None):
            for tc in msg.tool_calls:
                fn = getattr(tc, "function", None)
                if fn is not None and isinstance(getattr(fn, "arguments", None), str):
                    fn.arguments = _restore_text(fn.arguments, restore_map)

    # --- LiteLLM hooks ----------------------------------------------------

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not self.enabled or not isinstance(data, dict):
            return data
        restore_map = {}
        self._maps_by_call[_call_id_from(data)] = restore_map
        return await self._walk(data, restore_map)

    async def async_post_call_success_hook(self, data, user_api_key_dict, response):
        if not self.enabled:
            return response
        restore_map = self._maps_by_call.get(_call_id_from(data), {})
        if hasattr(response, "choices"):
            for choice in response.choices:
                msg = getattr(choice, "message", None)
                if msg is None:
                    continue
                self._restore_message_fields(msg, restore_map)
        self._maps_by_call.pop(_call_id_from(data), None)
        return response

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        if not self.enabled:
            async for chunk in response:
                yield chunk
            return
        restore_map = self._maps_by_call.get(_call_id_from(request_data), {})
        pending_content = ""
        pending_args: dict[int, str] = {}
        last_chunk = None
        try:
            async for chunk in response:
                if last_chunk is not None:
                    yield last_chunk
                try:
                    if chunk.choices:
                        for choice in chunk.choices:
                            delta = getattr(choice, "delta", None)
                            if delta is not None:
                                if isinstance(getattr(delta, "content", None), str):
                                    restored, pending_content = _restore_stream_piece(
                                        pending_content, delta.content, restore_map
                                    )
                                    delta.content = restored
                                if getattr(delta, "tool_calls", None):
                                    for tc in delta.tool_calls:
                                        fn = getattr(tc, "function", None)
                                        if fn is None or not isinstance(
                                            getattr(fn, "arguments", None), str
                                        ):
                                            continue
                                        index = getattr(tc, "index", 0)
                                        restored, pending_args[index] = _restore_stream_piece(
                                            pending_args.get(index, ""),
                                            fn.arguments,
                                            restore_map,
                                        )
                                        fn.arguments = restored
                    message = getattr(chunk, "message", None)
                    if message is not None:
                        self._restore_message_fields(message, restore_map)
                except Exception:  # noqa: BLE001 — никогда не ломаем стрим из-за маскера
                    pass
                last_chunk = chunk
            if last_chunk is not None:
                try:
                    if pending_content and last_chunk.choices:
                        delta = getattr(last_chunk.choices[0], "delta", None)
                        if delta is not None and isinstance(getattr(delta, "content", None), str):
                            delta.content += _restore_text(pending_content, restore_map)
                            pending_content = ""
                        elif delta is not None:
                            delta.content = _restore_text(pending_content, restore_map)
                            pending_content = ""
                    if pending_args and last_chunk.choices:
                        delta = getattr(last_chunk.choices[0], "delta", None)
                        tool_calls = getattr(delta, "tool_calls", None) if delta else None
                        if tool_calls:
                            for tc in tool_calls:
                                fn = getattr(tc, "function", None)
                                index = getattr(tc, "index", 0)
                                leftover = pending_args.pop(index, "")
                                if leftover and fn is not None and isinstance(
                                    getattr(fn, "arguments", None), str
                                ):
                                    fn.arguments += _restore_text(leftover, restore_map)
                except Exception:  # noqa: BLE001
                    pass
                yield last_chunk
        finally:
            self._maps_by_call.pop(_call_id_from(request_data), None)


proxy_handler_instance = MaxTokensClamp()
secret_masker_instance = LiteralSecretMasker()
chat_payload_guard_instance = ChatPayloadGuard()
