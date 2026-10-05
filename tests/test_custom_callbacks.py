import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


class ChatPayloadGuardTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        logger_module = types.ModuleType("litellm.integrations.custom_logger")
        logger_module.CustomLogger = type("CustomLogger", (), {})
        fake_modules = {
            "litellm": types.ModuleType("litellm"),
            "litellm.integrations": types.ModuleType("litellm.integrations"),
            "litellm.integrations.custom_logger": logger_module,
        }
        callback_path = (
            Path(__file__).resolve().parents[1]
            / "litellm-proxy"
            / "custom_callbacks.py"
        )
        spec = importlib.util.spec_from_file_location(
            "custom_callbacks_under_test", callback_path
        )
        cls.callbacks = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, fake_modules):
            spec.loader.exec_module(cls.callbacks)

    async def test_metadata_uses_single_local_sanitizer_request(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return {"text": "mail <EMAIL_ADDRESS_1>"}

        class Client:
            def __init__(self):
                self.calls = []

            async def post(self, url, *, json):
                self.calls.append((url, json))
                return Response()

        client = Client()
        private_text = "mail alice@example.test"
        result = await self.callbacks.ChatPayloadGuard()._anonymize_text(
            client, private_text
        )

        self.assertEqual(result, "mail <EMAIL_ADDRESS_1>")
        self.assertEqual(
            client.calls,
            [
                (
                    self.callbacks.PRESIDIO_ANONYMIZER_URL,
                    {"text": private_text, "language": "en"},
                )
            ],
        )


class LiteralSecretMaskerTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        logger_module = types.ModuleType("litellm.integrations.custom_logger")
        logger_module.CustomLogger = type("CustomLogger", (), {})
        fake_modules = {
            "litellm": types.ModuleType("litellm"),
            "litellm.integrations": types.ModuleType("litellm.integrations"),
            "litellm.integrations.custom_logger": logger_module,
        }
        callback_path = (
            Path(__file__).resolve().parents[1]
            / "litellm-proxy"
            / "custom_callbacks.py"
        )
        spec = importlib.util.spec_from_file_location(
            "custom_callbacks_masker_under_test", callback_path
        )
        cls.callbacks = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, fake_modules):
            spec.loader.exec_module(cls.callbacks)

    def setUp(self):
        self.callbacks._anonymize_cache.clear()
        self.callbacks._anonymize_cache_order.clear()

    async def test_inserted_placeholder_is_restored_in_response(self):
        class Response:
            def raise_for_status(self):
                pass

            def json(self):
                return [
                    {
                        "entity_type": "URL",
                        "start": self.start,
                        "end": self.end,
                        "score": 0.9,
                    }
                ]

        class Client:
            def __init__(self):
                self.calls = []

            async def post(self, url, *, json):
                text = json["text"]
                start = text.find("Invoice.php")
                response = Response()
                response.start = start
                response.end = start + len("Invoice.php")
                self.calls.append(url)
                return response

        client = Client()
        masker = self.callbacks.LiteralSecretMasker()
        data = {
            "litellm_call_id": "call-1",
            "messages": [
                {
                    "role": "user",
                    "content": "application/classes/Invoice.php:670",
                }
            ],
        }
        with patch.object(self.callbacks, "get_presidio_client", return_value=client):
            masked = await masker.async_pre_call_hook(None, None, data, "completion")

        content = masked["messages"][0]["content"]
        self.assertNotIn("Invoice.php", content)
        self.assertIn("<URL_", content)
        self.assertTrue(all(url.endswith("/analyze") for url in client.calls))

        class Choice:
            def __init__(self):
                self.message = types.SimpleNamespace(
                    content=f"Rendered {content} through ORGANIZATION bootstrap",
                    tool_calls=None,
                )

        response = types.SimpleNamespace(choices=[Choice()])
        restored = await masker.async_post_call_success_hook(data, None, response)
        self.assertIn("Invoice.php", restored.choices[0].message.content)
        self.assertNotIn("<URL_", restored.choices[0].message.content)

    async def test_opencode_session_placeholder_is_restored_from_request_map(self):
        masker = self.callbacks.LiteralSecretMasker()
        token = "<URL_Sc5a734906620_77>"
        data = {"litellm_call_id": "call-2"}
        masker._maps_by_call["call-2"] = {token: "Invoice.ph"}
        choice = types.SimpleNamespace(
            message=types.SimpleNamespace(
                content=f"application/classes/{token}p:670",
                tool_calls=None,
            )
        )
        response = types.SimpleNamespace(choices=[choice])
        restored = await masker.async_post_call_success_hook(data, None, response)
        self.assertEqual(
            restored.choices[0].message.content,
            "application/classes/Invoice.php:670",
        )


if __name__ == "__main__":
    unittest.main()