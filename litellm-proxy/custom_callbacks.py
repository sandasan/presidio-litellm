import json
import logging
import os

import httpx
from litellm.integrations.custom_logger import CustomLogger

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192").strip())

logger = logging.getLogger(__name__)
PRESIDIO_ANALYZER_URL = os.getenv(
    "PRESIDIO_ANALYZER_API_BASE", "http://presidio:5001"
) + "/analyze"
PRESIDIO_ANONYMIZER_URL = os.getenv(
    "PRESIDIO_ANONYMIZER_API_BASE", "http://presidio:5001"
) + "/anonymize"
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


class ChatPayloadGuard(CustomLogger):
    """Reject request formats or tool schemas that bypass Presidio masking."""

    async def _anonymize_text(self, client, text):
        if not text:
            return text
        response = await client.post(
            PRESIDIO_ANALYZER_URL,
            json={"text": text, "language": "en"},
        )
        response.raise_for_status()
        results = response.json()
        if not isinstance(results, list):
            raise ValueError("Invalid Presidio analyzer response")
        if not results:
            return text

        response = await client.post(
            PRESIDIO_ANONYMIZER_URL,
            json={"text": text, "analyzer_results": results},
        )
        response.raise_for_status()
        anonymized = response.json()
        if not isinstance(anonymized, dict) or not isinstance(anonymized.get("text"), str):
            raise ValueError("Invalid Presidio anonymizer response")
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
            raise TypeError("Only JSON chat requests are allowed")
        messages = data.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("Only text chat-completions requests are allowed")
        for message in messages:
            if not isinstance(message, dict):
                raise ValueError("Invalid chat message")
            if message.get("role") not in {
                "system", "developer", "user", "assistant", "tool", "function"
            }:
                raise ValueError("Unsupported chat message role")
            content = message.get("content")
            if isinstance(content, list):
                if any(
                    not isinstance(block, dict)
                    or block.get("type") != "text"
                    or not isinstance(block.get("text"), str)
                    for block in content
                ):
                    raise ValueError("Only text chat content is supported")
            elif content is not None and not isinstance(content, str):
                raise ValueError("Only text chat content is supported")
            elif content is None and not message.get("tool_calls"):
                raise ValueError("Chat message has no inspectable text")

            tool_calls = message.get("tool_calls")
            if tool_calls is not None:
                if not isinstance(tool_calls, list):
                    raise ValueError("Invalid tool calls")
                for tool_call in tool_calls:
                    function = tool_call.get("function") if isinstance(tool_call, dict) else None
                    if not isinstance(function, dict):
                        raise ValueError("Invalid tool-call function")
                    arguments = function.get("arguments")
                    if arguments is not None and not isinstance(arguments, str):
                        raise ValueError("Tool-call arguments must be text")

            function_call = message.get("function_call")
            if function_call is not None and (
                not isinstance(function_call, dict)
                or (
                    function_call.get("arguments") is not None
                    and not isinstance(function_call["arguments"], str)
                )
            ):
                raise ValueError("Invalid legacy function call")

        async with httpx.AsyncClient(timeout=10.0) as client:
            for message in messages:
                unchecked_fields = {
                    key: value
                    for key, value in message.items()
                    if key not in {"role", "content"}
                }
                if isinstance(message.get("content"), list):
                    unchecked_fields["content_metadata"] = [
                        {
                            key: value
                            for key, value in block.items()
                            if key not in {"type", "text"}
                        }
                        for block in message["content"]
                    ]
                if await self._contains_pii(client, unchecked_fields):
                    raise ValueError("Presidio detected PII outside message content")

            for field, value in data.items():
                if field == "messages" or field in LITELLM_INTERNAL_REQUEST_FIELDS:
                    continue
                if field == "metadata":
                    data[field] = await self._anonymize_metadata(client, value)
                    value = data[field]
                    if isinstance(value, dict):
                        value = {key: item for key, item in value.items() if key != "pii_tokens"}
                entities = TOOL_SCHEMA_ENTITIES if field == "tools" else None
                if await self._contains_pii(client, value, entities):
                    raise ValueError(f"Presidio detected PII in request field {field}")
        return data


class MaxTokensClamp(CustomLogger):
    """Ограничивает исходящий max_tokens, чтобы запросы агента подходили
    под лимиты бесплатных моделей (Groq, Gemini и т.д.)."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict):
            return data
        if isinstance(data.get("max_tokens"), int) and data["max_tokens"] > MAX_OUTPUT_TOKENS:
            data["max_tokens"] = MAX_OUTPUT_TOKENS
        return data


class LiteralSecretMasker(CustomLogger):
    """Маскирует точные значения секретов (доменные/бизнес-литералы), которые
    Presidio не распознаёт как PII-паттерн.

    Словарь значений загружается из JSON-файла (по умолчанию
    /app/litellm-proxy/secrets_map.json, формат {"<PLACEHOLDER>": "<value>"}).
    Перед отправкой к провайдеру каждое вхождение значения заменяется на
    placeholder (<PLACEHOLDER>), после ответа — восстанавливается обратно,
    чтобы Hermes и пользователь локально видели оригинальные данные, а наружу
    уходили только заглушки. В отличие от Presidio здесь не нужен паттерн:
    достаточно, что точный литерал попал в словарь — это детерминированно.

    Меры предосторожности: подставляйте только уникальные высокоэнтропийные
    значения (имена сервисов, логины, кодовые слова, хвосты ключей).
    Короткие/частые строки будут маскироваться повсюду и ломать качество.

    Если файл-словарь отсутствует, маскер молча выключен (не мешает стеку).
    """

    def __init__(self, file_path: str | None = None):
        super().__init__()
        file_path = file_path or os.getenv(
            "SECRETS_MAP_FILE", os.environ.get("PYTHONPATH", "") + "/secrets_map.json"
        )
        if not file_path:
            file_path = "/app/litellm-proxy/secrets_map.json"
        self.file_path = file_path
        self.enabled = False
        self._mask = []  # список (value, placeholder), отсортирован по длине value (дл.→кор.)
        self._restore = {}  # placeholder -> value
        self._load()

    def _load(self):
        path = self.file_path
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            logger.warning("Secrets map %s not found — literal masking disabled", path)
            return
        except Exception as e:  # noqa: BLE001
            logger.warning("Secrets map %s unreadable (%s) — literal masking disabled", path, e)
            return
        if not isinstance(raw, dict) or not raw:
            logger.warning("Secrets map %s is empty — literal masking disabled", path)
            return
        items = []
        for placeholder, value in raw.items():
            if not isinstance(value, str) or not value:
                continue
            ph = str(placeholder).strip()
            if not ph.startswith("<"):
                ph = f"<{ph}>"
            items.append((value, ph))
            self._restore[ph] = value
        if not items:
            logger.warning("Secrets map %s has no usable values — literal masking disabled", path)
            return
        self._mask = sorted(items, key=lambda kv: -len(kv[0]))
        self.enabled = True
        logger.warning("LiteralSecretMasker enabled with %d literals (%s)", len(items), path)

    # --- helpers -----------------------------------------------------------

    def _mask_text(self, text: str) -> str:
        for value, placeholder in self._mask:
            if value in text:
                text = text.replace(value, placeholder)
        return text

    def _restore_text(self, text: str) -> str:
        for placeholder, value in self._restore.items():
            if placeholder in text:
                text = text.replace(placeholder, value)
        return text

    def _walk(self, obj):
        """Рекурсивно маскирует/восстанавливает строки в словаре/списке."""
        if isinstance(obj, str):
            return self._mask_text(obj)
        if isinstance(obj, list):
            return [self._walk(x) for x in obj]
        if isinstance(obj, dict):
            return {k: self._walk(v) for k, v in obj.items()}
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
        data = self._walk(data)
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