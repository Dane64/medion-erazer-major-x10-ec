import contextlib
import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools.release import (
    ARCHIVE_NAME,
    PROJECT_NAME,
    REQUIRED_MODULES,
    REQUIRED_SOURCE_FILES,
    check_distributions,
    main,
    parse_tag,
    write_checksums,
)


class ReleaseTagTests(unittest.TestCase):
    def test_accepts_canonical_versions_and_marks_prereleases(self):
        for version in ("0.1.0", "1.2.3", "10.20.30", "1.2.3a1", "1.2.3b2", "1.2.3rc1"):
            with self.subTest(version=version):
                parsed, prerelease = parse_tag(f"v{version}")
                self.assertEqual(parsed, version)
                self.assertEqual(prerelease, any(character.isalpha() for character in version))

    def test_rejects_non_release_tags_and_noncanonical_versions(self):
        for tag in (
            "", "1.2.3", "v1", "v1.2", "v01.2.3", "v1.02.3", "v1.2.03", "v1.2.3rc01",
            "v1.2.3-rc.1", "v1.2.3.dev1", "v1.2.3+local", "v1.2.3\n", "v1.2.3;echo bad",
        ):
            with self.subTest(tag=tag), self.assertRaises(ValueError):
                parse_tag(tag)

    def test_tag_command_emits_validated_action_outputs(self):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(["tag", "v1.2.3rc1"]), 0)
        self.assertEqual(output.getvalue(), "version=1.2.3rc1\nprerelease=true\n")


class DistributionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.version = "1.2.3"
        self.dist_info = f"{ARCHIVE_NAME}-{self.version}.dist-info"
        self.wheel = self.root / f"{ARCHIVE_NAME}-{self.version}-py3-none-any.whl"
        self.sdist = self.root / f"{ARCHIVE_NAME}-{self.version}.tar.gz"
        metadata = f"Metadata-Version: 2.4\nName: {PROJECT_NAME}\nVersion: {self.version}\n\n".encode()
        self.wheel_files = dict.fromkeys(REQUIRED_MODULES, b"# package fixture\n")
        self.wheel_files.update({
            f"{self.dist_info}/METADATA": metadata,
            f"{self.dist_info}/licenses/LICENSE": b"license fixture\n",
            f"{self.dist_info}/entry_points.txt": b"[gui_scripts]\nmedion-fan-control = medion_fan_control.cli:main\n",
        })
        self.source_files = dict.fromkeys(REQUIRED_MODULES | REQUIRED_SOURCE_FILES, b"source fixture\n")
        self.source_files.update({"PKG-INFO": metadata, "tests/test_gui.py": b"# test fixture\n"})

    def build_fixtures(self, *, symlink=False):
        with zipfile.ZipFile(self.wheel, "w") as archive:
            for name, data in self.wheel_files.items():
                archive.writestr(name, data)
        with tarfile.open(self.sdist, "w:gz") as archive:
            for name, data in self.source_files.items():
                member = tarfile.TarInfo(f"{ARCHIVE_NAME}-{self.version}/{name}")
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
            if symlink:
                member = tarfile.TarInfo(f"{ARCHIVE_NAME}-{self.version}/link")
                member.type = tarfile.SYMTYPE
                member.linkname = "../outside"
                archive.addfile(member)

    def test_validates_both_distributions_and_generates_exact_checksums(self):
        self.build_fixtures()
        distributions = check_distributions(self.root, "v1.2.3")
        self.assertEqual(distributions, (self.wheel, self.sdist))
        checksum_path = write_checksums(self.root, distributions)
        expected = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(distributions)
        )
        self.assertEqual(checksum_path.read_text(encoding="ascii"), expected)

    def test_requires_matching_release_version(self):
        self.build_fixtures()
        with self.assertRaisesRegex(ValueError, "does not match release tag"):
            check_distributions(self.root, "v1.2.4")

    def test_refuses_missing_or_stale_extra_distributions(self):
        with self.assertRaisesRegex(ValueError, "exactly one"):
            check_distributions(self.root)
        self.build_fixtures()
        (self.root / "stale.whl").write_bytes(b"")
        with self.assertRaisesRegex(ValueError, "exactly one"):
            check_distributions(self.root)

    def test_requires_application_modules_license_and_launcher(self):
        for name in (
            "medion_fan_control/cli.py",
            f"{self.dist_info}/licenses/LICENSE",
            f"{self.dist_info}/entry_points.txt",
        ):
            with self.subTest(name=name):
                data = self.wheel_files.pop(name)
                self.build_fixtures()
                with self.assertRaises(ValueError):
                    check_distributions(self.root)
                self.wheel_files[name] = data

    def test_rejects_wrong_launcher(self):
        self.wheel_files[f"{self.dist_info}/entry_points.txt"] = (
            b"[gui_scripts]\nmedion-fan-control = medion_fan_control.gui:main\n"
        )
        self.build_fixtures()
        with self.assertRaisesRegex(ValueError, "entry point"):
            check_distributions(self.root)

    def test_rejects_wrong_project_and_mismatched_sdist_metadata(self):
        self.wheel_files[f"{self.dist_info}/METADATA"] = b"Name: unrelated\nVersion: 1.2.3\n"
        self.build_fixtures()
        with self.assertRaisesRegex(ValueError, "wrong project"):
            check_distributions(self.root)
        self.wheel_files[f"{self.dist_info}/METADATA"] = self.source_files["PKG-INFO"]
        self.source_files["PKG-INFO"] = f"Name: {PROJECT_NAME}\nVersion: 9.9.9\n".encode()
        self.build_fixtures()
        with self.assertRaisesRegex(ValueError, "versions differ"):
            check_distributions(self.root)

    def test_source_distribution_requires_documentation_and_tests(self):
        for name in ("README.md", "docs/user-guide.md", "tools/release.py", "tests/test_gui.py"):
            with self.subTest(name=name):
                data = self.source_files.pop(name)
                self.build_fixtures()
                with self.assertRaisesRegex(ValueError, "missing"):
                    check_distributions(self.root)
                self.source_files[name] = data

    def test_rejects_intermediate_or_unrelated_files(self):
        for name in ("scripts/probe.c", "artifacts/capture.txt", ".github/workflows/release.yml"):
            with self.subTest(name=name):
                self.source_files[name] = b"unrelated"
                self.build_fixtures()
                with self.assertRaisesRegex(ValueError, "unrelated"):
                    check_distributions(self.root)
                del self.source_files[name]

    def test_rejects_generated_wheel_files_and_unsafe_paths(self):
        for name in ("medion_fan_control/__pycache__/gui.pyc", "../outside", "/absolute", "wrong\\path"):
            with self.subTest(name=name):
                self.wheel_files[name] = b"unwanted"
                self.build_fixtures()
                with self.assertRaises(ValueError):
                    check_distributions(self.root)
                del self.wheel_files[name]

    def test_rejects_source_symlinks_without_extracting_them(self):
        self.build_fixtures(symlink=True)
        with self.assertRaisesRegex(ValueError, "links or special files"):
            check_distributions(self.root)

    def test_failed_command_does_not_emit_success_or_checksums(self):
        with contextlib.redirect_stderr(io.StringIO()) as errors, contextlib.redirect_stdout(io.StringIO()) as output:
            with self.assertRaises(SystemExit) as error:
                main(["check", str(self.root)])
        self.assertEqual(error.exception.code, 1)
        self.assertIn("Release validation failed", errors.getvalue())
        self.assertEqual(output.getvalue(), "")
        self.assertFalse((self.root / "SHA256SUMS").exists())


if __name__ == "__main__":
    unittest.main()
