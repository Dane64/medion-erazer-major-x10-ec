import contextlib
import io
import subprocess
import sys
import unittest
from unittest.mock import patch

from medion_fan_control import __version__
from medion_fan_control.cli import main, parse_args


class CliTests(unittest.TestCase):
    def test_demo_is_explicit(self):
        self.assertFalse(parse_args([]).demo)
        self.assertTrue(parse_args(["--demo"]).demo)

    def test_help_and_version_work_without_importing_qt(self):
        for argument, expected in (("--help", "--demo"), ("--version", __version__)):
            with self.subTest(argument=argument):
                result = subprocess.run(
                    [
                        sys.executable, "-c",
                        "import sys; sys.modules['PySide6'] = None; "
                        f"from medion_fan_control.cli import main; main([{argument!r}])",
                    ],
                    check=True, capture_output=True, text=True, timeout=10,
                )
                self.assertIn(expected, result.stdout)

    def test_module_entry_point_reports_version(self):
        result = subprocess.run(
            [sys.executable, "-m", "medion_fan_control", "--version"],
            check=True, capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.stdout, f"medion-fan-control {__version__}\n")

    def test_unknown_options_fail_with_usage(self):
        with contextlib.redirect_stderr(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                parse_args(["--unsupported-hardware"])
        self.assertEqual(error.exception.code, 2)
        self.assertIn("unrecognized arguments", output.getvalue())

    def test_unsupported_platform_fails_before_loading_the_gui(self):
        with patch("medion_fan_control.cli.sys.platform", "win32"), contextlib.redirect_stderr(io.StringIO()) as output:
            self.assertEqual(main([]), 1)
        self.assertIn("Linux only", output.getvalue())


if __name__ == "__main__":
    unittest.main()
