import tempfile
import unittest

from anonymizer_bridge import (
    MappingStore,
    UnresolvedPlaceholderError,
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

    def test_mapping_is_not_shared_between_sessions(self):
        store = self.new_store()
        first_key = self.prepare(store, "session-a")
        placeholder = self.create_person_token(store, first_key)
        second_key = self.prepare(store, "session-b")

        with self.assertRaises(UnresolvedPlaceholderError):
            add_cached_replacements(placeholder, {}, store, second_key)

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