import io
import json
import sqlite3
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr
from unittest.mock import patch

from anonymizer_bridge import (
    PRESIDIO_ANALYZE_TIMEOUT_SECONDS,
    MappingStore,
    add_cached_replacements,
    anonymize_text,
    anonymize_from_results,
    anonymize_value,
    restore_text,
    restore_nonstream_response,
    restored_chunks,
    guarded_stream_chunks,
    session_key,
    compress_context,
)


class MappingStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database = f"{self.temp_dir.name}/mappings.sqlite3"

    def tearDown(self):
        self.temp_dir.cleanup()

    def new_store(self):
        return MappingStore(self.database)

    def prepare(self, store, session_id, parent_id=None):
        current_key = session_key(session_id)
        parent_key = session_key(parent_id) if parent_id else None
        store.prepare_session(current_key, parent_key)
        return current_key

    def create_person_token(self, store, current_key):
        return anonymize_from_results(
            "Alice",
            [{"start": 0, "end": 5, "entity_type": "PERSON"}],
            {},
            store,
            current_key,
        )

    def test_mapping_survives_store_recreation(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, current_key)

        restarted_store = self.new_store()
        restarted_key = self.prepare(restarted_store, "session-a")
        replacements = {}
        add_cached_replacements(
            placeholder, replacements, restarted_store, restarted_key
        )
        repeated = self.create_person_token(restarted_store, restarted_key)

        self.assertEqual(replacements[placeholder], "Alice")
        self.assertEqual(repeated, placeholder)

    def test_new_placeholders_are_sequential_and_stable(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")

        first = store.get_or_create(current_key, "URL", "https://example.test/a")
        second = store.get_or_create(current_key, "URL", "https://example.test/b")
        repeated = store.get_or_create(current_key, "URL", "https://example.test/a")

        session_namespace = current_key[:12]
        self.assertEqual(first, f"<URL_S{session_namespace}_1>")
        self.assertEqual(second, f"<URL_S{session_namespace}_2>")
        self.assertEqual(repeated, first)

    def test_placeholder_indices_are_unique_across_sessions(self):
        store = self.new_store()
        first_key = self.prepare(store, "session-a")
        second_key = self.prepare(store, "session-b")

        first = store.get_or_create(first_key, "PERSON", "Alice")
        second = store.get_or_create(second_key, "PERSON", "Bob")

        self.assertEqual(first, f"<PERSON_S{first_key[:12]}_1>")
        self.assertEqual(second, f"<PERSON_S{second_key[:12]}_1>")
        self.assertNotEqual(first, second)

    def test_legacy_uuid_placeholders_are_normalized(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        legacy_tokens = {
            "<ORG_ANALYSIS_EMAIL_7806dbcd2fe47a57b7c7d51a1f5f81e7>": (
                "ORG_ANALYSIS_EMAIL",
                "analysis@example.test",
            ),
            "<URL_1_abcdef0123456789abcdef0123456789>": (
                "URL",
                "https://example.test/private",
            ),
        }
        connection = sqlite3.connect(self.database)
        try:
            connection.executemany(
                """INSERT INTO mappings
                   (session_id, placeholder, entity_type, original_value, created_at)
                   VALUES (?, ?, ?, ?, 1)""",
                [
                    (current_key, token, entity_type, original_value)
                    for token, (entity_type, original_value) in legacy_tokens.items()
                ],
            )
            connection.commit()
        finally:
            connection.close()

        replacements = {}
        normalized = add_cached_replacements(
            " ".join(legacy_tokens), replacements, store, current_key
        )

        self.assertEqual(
            normalized,
            f"<ORG_ANALYSIS_EMAIL_S{current_key[:12]}_1> <URL_S{current_key[:12]}_2>",
        )
        self.assertEqual(
            replacements[f"<ORG_ANALYSIS_EMAIL_S{current_key[:12]}_1>"],
            "analysis@example.test",
        )
        self.assertEqual(
            replacements[f"<URL_S{current_key[:12]}_2>"],
            "https://example.test/private",
        )

    def test_unmapped_legacy_uuid_is_normalized_without_leaking_it(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        legacy = "<PERSON_abcdef0123456789abcdef0123456789>"

        replacements = {}
        normalized = add_cached_replacements(legacy, replacements, store, current_key)
        repeated = add_cached_replacements(legacy, {}, store, current_key)

        self.assertEqual(normalized, "[unresolved anonymization placeholder]")
        self.assertEqual(repeated, normalized)
        self.assertEqual(replacements, {})
        self.assertIsNone(store.resolve(current_key, "<PERSON_1>"))

    def test_unknown_model_placeholder_becomes_safe_marker(self):
        for response_text in (
            '{"path":"<US_DRIVER_LICENSE_136>.php"}',
            '{"path":"[unresolved anonymization placeholder].php"}',
        ):
            with self.subTest(response_text=response_text):
                self.assertEqual(
                    restore_text(response_text, {}),
                    '{"path":"[unresolved anonymization placeholder].php"}',
                )

    def test_unknown_placeholder_split_across_response_chunks_is_marked(self):
        response = io.BytesIO(
            ("x" * 4090 + "<US_DRIVER_LICENSE_136>.php").encode("utf-8")
        )

        self.assertIn(
            "[unresolved anonymization placeholder].php",
            "".join(restored_chunks(response, {})),
        )

    def test_unknown_streaming_tool_call_is_withheld_and_stream_completes(self):
        frames = [
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"write_file","arguments":"{\\"path\\":\\"<US_DRIVER_LICENSE_136>.php\\"}"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            "data: [DONE]\n\n",
        ]

        output = "".join(guarded_stream_chunks(io.BytesIO("".join(frames).encode()), {}))

        self.assertIn("Tool call withheld", output)
        self.assertIn('"finish_reason":"stop"', output)
        self.assertIn("data: [DONE]", output)
        self.assertNotIn("write_file", output)
        self.assertNotIn("US_DRIVER_LICENSE_136", output)

    def test_unknown_tool_call_retries_and_continues_with_valid_call(self):
        bad_frames = [
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"write_file","arguments":"{\\"path\\":\\"<US_DRIVER_LICENSE_136>.php\\"}"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        repaired_frames = [
            'data: {"id":"cmpl-y","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"list_dir","arguments":"{\\"path\\":\\"/workspace\\"}"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"cmpl-y","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        retry_requests = []

        def retry_upstream(request_body):
            retry_requests.append(request_body)
            return io.BytesIO("".join(repaired_frames).encode())

        output = "".join(
            guarded_stream_chunks(
                io.BytesIO("".join(bad_frames).encode()),
                {},
                request_body={"messages": [{"role": "user", "content": "finish task"}]},
                retry_upstream=retry_upstream,
                max_retries=2,
            )
        )

        self.assertEqual(len(retry_requests), 1)
        self.assertIn("withheld your previous tool call", retry_requests[0]["messages"][-1]["content"])
        self.assertIn("list_dir", output)
        self.assertIn("data: [DONE]", output)
        self.assertNotIn("US_DRIVER_LICENSE_136", output)

    def test_unknown_tool_call_stops_after_retry_limit(self):
        bad_frames = [
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"write_file","arguments":"{\\"path\\":\\"<US_DRIVER_LICENSE_136>.php\\"}"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            "data: [DONE]\n\n",
        ]
        retry_count = 0

        def retry_upstream(request_body):
            nonlocal retry_count
            retry_count += 1
            return io.BytesIO("".join(bad_frames).encode())

        output = "".join(
            guarded_stream_chunks(
                io.BytesIO("".join(bad_frames).encode()),
                {},
                request_body={"messages": [{"role": "user", "content": "finish task"}]},
                retry_upstream=retry_upstream,
                max_retries=1,
            )
        )

        self.assertEqual(retry_count, 1)
        self.assertIn("after automatic retries", output)
        self.assertIn('"finish_reason":"stop"', output)
        self.assertIn("data: [DONE]", output)
        self.assertNotIn("US_DRIVER_LICENSE_136", output)

    def test_known_streaming_tool_call_is_restored_and_preserved(self):
        frames = [
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":{"name":"write_file","arguments":"{\\"path\\":\\"<PERSON_1>.php\\"}"}}]},"finish_reason":null}]}\n\n',
            'data: {"id":"cmpl-x","model":"m","choices":[{"index":0,"delta":{},"finish_reason":"tool_calls"}]}\n\n',
            "data: [DONE]\n\n",
        ]

        output = "".join(
            guarded_stream_chunks(
                io.BytesIO("".join(frames).encode()),
                {"<PERSON_1>": "Alice"},
            )
        )

        self.assertIn("write_file", output)
        self.assertIn("Alice.php", output)
        self.assertNotIn("<PERSON_1>", output)

    def test_unknown_nonstreaming_tool_call_is_withheld(self):
        body = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "write_file",
                                    "arguments": '{"path":"<US_DRIVER_LICENSE_136>.php"}',
                                }
                            }
                        ]
                    },
                }
            ]
        }

        restored = json.loads(
            restore_nonstream_response(json.dumps(body).encode(), {})
        )
        choice = restored["choices"][0]

        self.assertEqual(choice["finish_reason"], "stop")
        self.assertNotIn("tool_calls", choice["message"])
        self.assertIn("Tool call withheld", choice["message"]["content"])

    def test_analyzer_timeout_is_configured_and_logs_no_input_text(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        private_text = "private text must not appear in timeout logs"
        log = io.StringIO()

        with patch(
            "anonymizer_bridge.urllib.request.urlopen",
            side_effect=urllib.error.URLError(TimeoutError("timed out")),
        ) as open_url:
            with redirect_stderr(log), self.assertRaises(urllib.error.URLError):
                anonymize_text(private_text, {}, store, current_key)

        self.assertEqual(
            open_url.call_args.kwargs["timeout"], PRESIDIO_ANALYZE_TIMEOUT_SECONDS
        )
        self.assertIn("text_chars=", log.getvalue())
        self.assertNotIn(private_text, log.getvalue())

    def test_message_texts_are_analyzed_in_one_batch(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        body = {
            "messages": [
                {"role": "user", "content": "Alice sent the first note."},
                {"role": "assistant", "content": "Alice sent another note."},
            ]
        }

        def analyze_request(request, timeout):
            request_body = json.loads(request.data.decode("utf-8"))
            text = request_body["text"]
            results = []
            search_from = 0
            while True:
                start = text.find("Alice", search_from)
                if start < 0:
                    break
                results.append(
                    {"start": start, "end": start + 5, "entity_type": "PERSON"}
                )
                search_from = start + 5
            return io.BytesIO(json.dumps(results).encode("utf-8"))

        with patch(
            "anonymizer_bridge.urllib.request.urlopen",
            side_effect=analyze_request,
        ) as open_url:
            anonymized = anonymize_value(body, {}, store, current_key)

        self.assertEqual(open_url.call_count, 1)
        self.assertEqual(
            [message["content"] for message in anonymized["messages"]],
            [
                f"<PERSON_S{current_key[:12]}_1> sent the first note.",
                f"<PERSON_S{current_key[:12]}_1> sent another note.",
            ],
        )

    def test_mapping_does_not_expire_when_ttl_is_disabled(self):
        store = MappingStore(self.database, ttl_seconds=0)
        current_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, current_key)

        connection = sqlite3.connect(self.database)
        try:
            connection.execute(
                "UPDATE sessions SET updated_at = 0 WHERE session_id = ?",
                (current_key,),
            )
            connection.commit()
        finally:
            connection.close()

        self.prepare(store, "another-session")
        self.assertEqual(store.resolve(current_key, placeholder), "Alice")

    def test_mapping_is_not_shared_between_sessions(self):
        store = self.new_store()
        first_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, first_key)
        second_key = self.prepare(store, "session-b")

        replacements = {}
        add_cached_replacements(placeholder, replacements, store, second_key)
        self.assertEqual(replacements, {})

    def test_unknown_legacy_placeholder_is_preserved_without_mapping(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        replacements = {}

        add_cached_replacements("<PERSON_1>", replacements, store, current_key)

        self.assertEqual(replacements, {})

    def test_forked_session_inherits_parent_mappings(self):
        store = self.new_store()
        parent_key = self.prepare(store, "parent")
        placeholder = self.create_person_token(store, parent_key)
        child_key = self.prepare(store, "child", "parent")

        replacements = {}
        add_cached_replacements(placeholder, replacements, store, child_key)

        restarted_store = self.new_store()
        restarted_child_key = self.prepare(restarted_store, "child")
        self.assertEqual(
            restarted_store.resolve(restarted_child_key, placeholder), "Alice"
        )
        self.assertEqual(replacements[placeholder], "Alice")


    def test_response_restores_placeholder_from_session_store(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, current_key)

        replacements = store.list_mappings(current_key)
        restored = restore_text(
            f"see {placeholder} in Model_Booking_{placeholder}p:227",
            replacements,
        )

        self.assertEqual(restored, "see Alice in Model_Booking_Alicep:227")
        self.assertNotIn(placeholder, restored)

    def test_unicode_escaped_placeholder_is_restored(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, current_key)
        escaped = placeholder.replace("<", "\\u003c").replace(">", "\\u003e")
        replacements = store.list_mappings(current_key)

        self.assertEqual(restore_text(escaped, replacements), "Alice")
        self.assertIn(
            "Alice.php",
            "".join(
                restored_chunks(
                    io.BytesIO(f'{{"path":"{escaped}.php"}}'.encode()),
                    replacements,
                )
            ),
        )

    def test_html_escaped_placeholder_is_restored(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, current_key)
        html_escaped = placeholder.replace("<", "&lt;").replace(">", "&gt;")
        replacements = store.list_mappings(current_key)

        self.assertEqual(restore_text(html_escaped, replacements), "Alice")

    def test_context_compression_when_threshold_reached(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        replacements = {}

        # Create a large message list (70 messages, threshold is 67)
        messages = []
        for i in range(70):
            messages.append({"role": "user", "content": f"Message {i}"})

        compressed, was_compressed = compress_context(
            messages, replacements, store, current_key
        )

        self.assertTrue(was_compressed)
        self.assertLess(len(compressed), len(messages))
        # Should have summary + last 30 messages
        self.assertEqual(len(compressed), 31)
        self.assertIn("CONTEXT SUMMARY", compressed[0]["content"])
        self.assertIn("Message 69", compressed[-1]["content"])

    def test_context_not_compressed_below_threshold(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        replacements = {}

        # Create a small message list (50 messages, threshold is 67)
        messages = []
        for i in range(50):
            messages.append({"role": "user", "content": f"Message {i}"})

        compressed, was_compressed = compress_context(
            messages, replacements, store, current_key
        )

        self.assertFalse(was_compressed)
        self.assertEqual(len(compressed), len(messages))


if __name__ == "__main__":
    unittest.main()