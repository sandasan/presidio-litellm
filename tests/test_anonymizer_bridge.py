import sqlite3
import tempfile
import unittest

from anonymizer_bridge import (
    MappingStore,
    add_cached_replacements,
    anonymize_from_results,
    session_key,
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

        self.assertEqual(first, "<URL_1>")
        self.assertEqual(second, "<URL_2>")
        self.assertEqual(repeated, first)

    def test_placeholder_indices_are_unique_across_sessions(self):
        store = self.new_store()
        first_key = self.prepare(store, "session-a")
        second_key = self.prepare(store, "session-b")

        first = store.get_or_create(first_key, "PERSON", "Alice")
        second = store.get_or_create(second_key, "PERSON", "Bob")

        self.assertEqual(first, "<PERSON_1>")
        self.assertEqual(second, "<PERSON_2>")

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
            "<ORG_ANALYSIS_EMAIL_1> <URL_2>",
        )
        self.assertEqual(replacements["<ORG_ANALYSIS_EMAIL_1>"], "analysis@example.test")
        self.assertEqual(replacements["<URL_2>"], "https://example.test/private")

    def test_unmapped_legacy_uuid_is_normalized_without_leaking_it(self):
        store = self.new_store()
        current_key = self.prepare(store, "session-a")
        legacy = "<PERSON_abcdef0123456789abcdef0123456789>"

        replacements = {}
        normalized = add_cached_replacements(legacy, replacements, store, current_key)
        repeated = add_cached_replacements(legacy, {}, store, current_key)

        self.assertEqual(normalized, "<PERSON_1>")
        self.assertEqual(repeated, normalized)
        self.assertEqual(replacements, {})

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


if __name__ == "__main__":
    unittest.main()