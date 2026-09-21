import os
import json
import httpx
from litellm.integrations.custom_logger import CustomLogger

PRESIDIO_ANALYZER_URL = os.getenv("PRESIDIO_ANALYZER_API_BASE", "http://presidio:5001") + "/analyze"
PRESIDIO_ANONYMIZER_URL = os.getenv("PRESIDIO_ANONYMIZER_API_BASE", "http://presidio:5001") + "/anonymize"

class PresidioAnonymizer(CustomLogger):
    async def _anonymize_text(self, client: httpx.AsyncClient, text: str) -> str:
        """Вспомогательная функция для отправки текста в Presidio Analyzer + Anonymizer."""
        if not text or not isinstance(text, str):
            return text

        try:
            # 1. Анализ текста на PII
            res_analyzer = await client.post(
                PRESIDIO_ANALYZER_URL,
                json={"text": text, "language": "en"}
            )

            if res_analyzer.status_code == 200:
                analyzer_results = res_analyzer.json()
                if analyzer_results and isinstance(analyzer_results, list):
                    # 2. Анонимизация найденных сущностей
                    anon_payload = {
                        "text": text,
                        "analyzer_results": analyzer_results
                    }
                    res_anon = await client.post(
                        PRESIDIO_ANONYMIZER_URL,
                        json=anon_payload
                    )
                    if res_anon.status_code == 200:
                        anon_data = res_anon.json()
                        if isinstance(anon_data, dict) and "text" in anon_data:
                            return anon_data["text"]
        except Exception as e:
            print(f"[Presidio Anonymization Sub-Error] {e}", flush=True)

        return text

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        """Хук LiteLLM, перехватывающий контекст перед отправкой внешнему провайдеру."""
        try:
            if not isinstance(data, dict):
                return data

            messages = data.get("messages", [])
            if not messages or not isinstance(messages, list):
                return data

            async with httpx.AsyncClient(timeout=10.0) as client:
                for message in messages:
                    if not isinstance(message, dict):
                        continue

                    # 1. Очистка стандартного содержимого (system, user, assistant, tool)
                    if "content" in message and isinstance(message["content"], str):
                        message["content"] = await self._anonymize_text(client, message["content"])
                    elif "content" in message and isinstance(message["content"], list):
                        # Для мультимодальных сообщений или разделенных блоков текста
                        for block in message["content"]:
                            if isinstance(block, dict) and block.get("type") == "text" and "text" in block:
                                block["text"] = await self._anonymize_text(client, block["text"])

                    # 2. Очистка аргументов вызова функций/инструментов (Tool Calls для Агентов)
                    if "tool_calls" in message and isinstance(message["tool_calls"], list):
                        for tool_call in message["tool_calls"]:
                            if not isinstance(tool_call, dict):
                                continue

                            function_data = tool_call.get("function", {})
                            args_str = function_data.get("arguments")

                            if args_str and isinstance(args_str, str):
                                # Пробуем анонимизировать JSON-строку аргументов
                                anonymized_args = await self._anonymize_text(client, args_str)
                                function_data["arguments"] = anonymized_args

        except Exception as e:
            print(f"[Presidio Agent Hook Error] {e}", flush=True)

        return data

proxy_handler_instance = PresidioAnonymizer()
