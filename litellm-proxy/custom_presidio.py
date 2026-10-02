import os
import json
import httpx
from litellm.integrations.custom_logger import CustomLogger

PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_API_BASE", "http://presidio:5001") + "/analyze"
PRESIDIO_ANONYMIZER_URL = os.getenv("PRESIDIO_ANONYMIZER_API_BASE", "http://presidio:5001") + "/anonymize"

class PresidioAnonymizer(CustomLogger):
    async def _anonymize_text(self, client: httpx.AsyncClient, text: str) -> str:
        """Вспомогательная функция для отправки текста в Presidio Analyzer + Anonymizer."""
        if text == "":
            return text
        if not isinstance(text, str):
            raise TypeError("Presidio accepts text strings only")

        res_analyzer = await client.post(
            PRESIDIO_ANALYZER_URL,
            json={"text": text, "language": "en"}
        )
        res_analyzer.raise_for_status()
        analyzer_results = res_analyzer.json()
        if not isinstance(analyzer_results, list):
            raise ValueError("Invalid Presidio analyzer response")
        if not analyzer_results:
            return text

        anon_payload = {"text": text, "analyzer_results": analyzer_results}
        res_anon = await client.post(PRESIDIO_ANONYMIZER_URL, json=anon_payload)
        res_anon.raise_for_status()
        anon_data = res_anon.json()
        if not isinstance(anon_data, dict) or not isinstance(anon_data.get("text"), str):
            raise ValueError("Invalid Presidio anonymizer response")
        return anon_data["text"]

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Хук LiteLLM, перехватывающий контекст перед отправкой внешнему провайдеру."""
        try:
            if not isinstance(data, dict):
                raise TypeError("LiteLLM request must be a JSON object")

            messages = data.get("messages", [])
            if not messages or not isinstance(messages, list) or any(
                not isinstance(message, dict) for message in messages
            ):
                raise ValueError("LiteLLM request must contain valid chat messages")

            async with httpx.AsyncClient(timeout=10.0) as client:
                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    # 1. Очистка стандартного содержимого (system, user, assistant, tool)
                    if "content" in message and isinstance(message["content"], str):
                        message["content"] = await self._anonymize_text(client, message["content"])
                    elif "content" in message and isinstance(message["content"], list):
                        for block in message["content"]:
                            if (
                                not isinstance(block, dict)
                                or block.get("type") != "text"
                                or not isinstance(block.get("text"), str)
                            ):
                                raise ValueError("Unsupported non-text message content")
                            block["text"] = await self._anonymize_text(client, block["text"])
                    elif "content" in message and message["content"] is not None:
                        raise ValueError("Unsupported message content type")

                    # 2. Очистка аргументов вызова функций/инструментов (Tool Calls для Агентов)
                    if "tool_calls" in message and isinstance(message["tool_calls"], list):
                        for tool_call in message["tool_calls"]:
                            if not isinstance(tool_call, dict):
                                continue

                            function_data = tool_call.get("function")
                            if not isinstance(function_data, dict):
                                raise ValueError("Invalid tool-call function")
                            args_str = function_data.get("arguments")

                            if args_str is not None:
                                if not isinstance(args_str, str):
                                    raise ValueError("Tool-call arguments must be text")
                                anonymized_args = await self._anonymize_text(client, args_str)
                                function_data["arguments"] = anonymized_args

        except Exception as e:
            print(f"[Presidio Agent Hook Error] {e}", flush=True)
            raise
        return data

proxy_handler_instance = PresidioAnonymizer()
