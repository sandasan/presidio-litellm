import json
import logging
import os

from litellm.integrations.custom_logger import CustomLogger

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192").strip())

logger = logging.getLogger(__name__)


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
                if hasattr(chunk, "message") and isinstance(getattr(chunk, "message", None).content, str):
                    chunk.message.content = self._restore_text(chunk.message.content)
            except Exception:  # noqa: BLE001 — никогда не ломаем стрим из-за маскера
                pass
            yield chunk


proxy_handler_instance = MaxTokensClamp()
secret_masker_instance = LiteralSecretMasker()