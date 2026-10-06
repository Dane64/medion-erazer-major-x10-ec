import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from medion_fan_control.lighting import LightingAccessError, LightZone, RgbColor
from medion_fan_control.settings import lighting_config_path, load_lighting_colors, save_lighting_colors


class LightingSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.directory)
        self.path = self.root / "config" / "lighting.json"
        self.colors = {
            LightZone.KEYBOARD: RgbColor(0x12, 0x34, 0x56),
            LightZone.RIGHT_SIDE: RgbColor(0xAB, 0xCD, 0xEF),
        }

    def test_saves_and_loads_selected_colors_with_private_permissions(self):
        save_lighting_colors(self.colors, self.path)
        self.assertEqual(load_lighting_colors(self.path), self.colors)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_missing_settings_mean_no_automatic_lighting_write(self):
        self.assertEqual(load_lighting_colors(self.path), {})
        self.assertFalse(self.path.parent.exists())

    def test_rejects_corrupt_and_invalid_settings(self):
        self.path.parent.mkdir()
        for document in (
            "{", "null", "[]", "{}", '{"colors": []}',
            '{"colors": {"keyboard": "blue"}}', '{"colors": {"unknown": "#123456"}}',
            '{"colors": {"keyboard": null}}', '{"colors": {"keyboard": 123456}}',
            '{"colors": {"keyboard": []}}',
        ):
            with self.subTest(document=document):
                self.path.write_text(document, encoding="utf-8")
                with self.assertRaises(LightingAccessError):
                    load_lighting_colors(self.path)

    def test_rejects_non_utf8_settings(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"\xff")
        with self.assertRaisesRegex(LightingAccessError, "cannot load"):
            load_lighting_colors(self.path)

    def test_failed_replace_preserves_previous_settings_and_removes_temporary_file(self):
        save_lighting_colors(self.colors, self.path)
        previous = self.path.read_bytes()
        with patch("medion_fan_control.settings.os.replace", side_effect=PermissionError("permission denied")):
            with self.assertRaisesRegex(LightingAccessError, "cannot save"):
                save_lighting_colors({LightZone.LID: RgbColor(0, 0, 0)}, self.path)
        self.assertEqual(self.path.read_bytes(), previous)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_failed_flush_does_not_replace_existing_settings(self):
        save_lighting_colors(self.colors, self.path)
        with patch("medion_fan_control.settings.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(LightingAccessError, "disk full"):
                save_lighting_colors({}, self.path)
        self.assertEqual(load_lighting_colors(self.path), self.colors)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_uses_unique_temporary_files(self):
        self.path.parent.mkdir()
        unrelated = self.path.with_suffix(".tmp")
        unrelated.write_text("another operation", encoding="utf-8")
        save_lighting_colors(self.colors, self.path)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "another operation")

    def test_respects_xdg_configuration_directory(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}):
            self.assertEqual(lighting_config_path(), self.root / "medion-fan-control" / "lighting.json")

    def test_empty_xdg_configuration_uses_effective_users_home(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": ""}), patch("medion_fan_control.settings.Path.home", return_value=self.root):
            self.assertEqual(lighting_config_path(), self.root / ".config" / "medion-fan-control" / "lighting.json")

    def test_relative_xdg_configuration_is_reported_instead_of_writing_to_cwd(self):
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": "relative/path"}):
            with self.assertRaisesRegex(LightingAccessError, "absolute path"):
                lighting_config_path()


if __name__ == "__main__":
    unittest.main()
