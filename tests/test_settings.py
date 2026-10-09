import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medion_fan_control.settings import SettingsError, StateStore, load_json, save_json_atomic


class StateStoreTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.path = self.root / "state" / "state.json"

    def test_round_trip_with_private_permissions(self):
        save_json_atomic(self.path, {"a": 1})
        self.assertEqual(load_json(self.path), {"a": 1})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_file_is_empty_and_corrupt_file_is_reported(self):
        self.assertEqual(load_json(self.path), {})
        self.path.parent.mkdir()
        for text in ("{", "[]", "\xff"):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="latin-1")
                with self.assertRaises(SettingsError):
                    load_json(self.path)

    def test_store_keeps_load_error_and_starts_empty(self):
        self.path.parent.mkdir()
        self.path.write_text("{", encoding="utf-8")
        store = StateStore(self.path)
        self.assertIsNotNone(store.load_error)
        self.assertIsNone(store.get("cpu"))

    def test_merge_is_deep_and_persistent(self):
        store = StateStore(self.path)
        store.merge("cpu", {"rapl": {"pl1": {"watts": 40}}, "no_turbo": False})
        store.merge("cpu", {"rapl": {"pl2": {"watts": 90}}})
        reloaded = StateStore(self.path)
        self.assertEqual(reloaded.get("cpu"), {"no_turbo": False, "rapl": {"pl1": {"watts": 40}, "pl2": {"watts": 90}}})

    def test_failed_replace_preserves_previous_document(self):
        store = StateStore(self.path)
        store.update("lighting", {"x": 1})
        with patch("medion_fan_control.settings.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(SettingsError):
                store.update("lighting", {"x": 2})
        self.assertEqual(StateStore(self.path).get("lighting"), {"x": 1})
        self.assertEqual(store.get("lighting"), {"x": 1})
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])


if __name__ == "__main__":
    unittest.main()
