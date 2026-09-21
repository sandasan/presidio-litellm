import os

from litellm.integrations.custom_logger import CustomLogger

MAX_OUTPUT_TOKENS = int(os.getenv("MAX_OUTPUT_TOKENS", "8192").strip())


class MaxTokensClamp(CustomLogger):
    """Ограничивает исходящий max_tokens, чтобы запросы агента подходили
    под лимиты бесплатных моделей (Groq, Gemini и т.д.)."""

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if not isinstance(data, dict):
            return data
        if isinstance(data.get("max_tokens"), int) and data["max_tokens"] > MAX_OUTPUT_TOKENS:
            data["max_tokens"] = MAX_OUTPUT_TOKENS
        return data


proxy_handler_instance = MaxTokensClamp()