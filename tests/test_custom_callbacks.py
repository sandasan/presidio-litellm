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
                    self.callbacks.PRESIDIO_SANITIZER_URL,
                    {"text": private_text, "language": "en"},
                )
            ],
        )


if __name__ == "__main__":
    unittest.main()