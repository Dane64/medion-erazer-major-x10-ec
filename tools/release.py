"""Validate release tags and package contents before generating checksums."""

from __future__ import annotations

import argparse
import configparser
import hashlib
import re
import tarfile
import zipfile
from email.parser import BytesParser
from pathlib import Path, PurePosixPath


PROJECT_NAME = "medion-erazer-major-x10-ec"
ARCHIVE_NAME = PROJECT_NAME.replace("-", "_")
PACKAGE_NAME = "medion_fan_control"
REQUIRED_MODULES = {
    f"{PACKAGE_NAME}/{name}.py"
    for name in (
        "__init__", "__main__", "_version", "audio", "cli", "client", "controller", "cpu", "daemon",
        "display", "gpu", "gui", "hardware", "icons", "lighting", "protocol", "settings", "sysfs",
        "telemetry", "theme", "widgets",
    )
}
REQUIRED_ASSETS = {
    f"{PACKAGE_NAME}/assets/{name}"
    for name in ("x10-control.svg", "fonts/Outfit-Variable.ttf", "fonts/OFL.txt")
}
REQUIRED_PACKAGE_FILES = REQUIRED_MODULES | REQUIRED_ASSETS
REQUIRED_SOURCE_FILES = {
    ".gitignore", "README.md", "CHANGELOG.md", "CONTRIBUTING.md", "LICENSE", "pyproject.toml", "uv.lock",
    "docs/building.md", "docs/protocol.md", "docs/user-guide.md", "docs/releases.md", "docs/secure-boot.md",
    "tools/release.py", "kernel/x10_ec.c", "kernel/Makefile", "kernel/dkms.conf", "kernel/LICENSE-GPL-2.0.txt",
    "packaging/systemd/x10ctld.service", "packaging/x10ctld.conf", "packaging/desktop/x10-control.desktop",
    "scripts/install.sh", "scripts/mok-sign.sh", "scripts/uninstall.sh",
    "ext/arc-dgpu-ctl/Makefile", "ext/arc-dgpu-ctl/VERSION", "ext/arc-dgpu-ctl/src/arc-dgpu-ctl",
}
TAG_PATTERN = re.compile(
    r"v(?P<version>(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)"
    r"(?P<prerelease>(?:a|b|rc)(?:0|[1-9]\d*))?)"
)


def parse_tag(tag: str) -> tuple[str, bool]:
    match = TAG_PATTERN.fullmatch(tag)
    if match is None:
        raise ValueError("release tags must be vMAJOR.MINOR.PATCH, optionally followed by aN, bN, or rcN")
    return match["version"], match["prerelease"] is not None


def metadata_version(data: bytes) -> str:
    metadata = BytesParser().parsebytes(data)
    if metadata.get("Name") != PROJECT_NAME:
        raise ValueError("distribution metadata has the wrong project name")
    version = metadata.get("Version")
    if not version:
        raise ValueError("distribution metadata is missing its version")
    return version


def validate_paths(paths: set[str]) -> None:
    for name in paths:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts or "\\" in name:
            raise ValueError(f"unsafe archive path: {name}")
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo", ".log"}:
            raise ValueError(f"generated file in distribution: {name}")


def check_distributions(directory: Path, tag: str | None = None) -> tuple[Path, Path]:
    wheels = sorted(directory.glob("*.whl"))
    sdists = sorted(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError("expected exactly one wheel and one source distribution in the output directory")
    wheel, sdist = wheels[0], sdists[0]

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        paths = set(names)
        validate_paths(paths)
        if len(paths) != len(names):
            raise ValueError("duplicate wheel members")
        if not REQUIRED_PACKAGE_FILES <= paths:
            raise ValueError(f"wheel is missing files: {sorted(REQUIRED_PACKAGE_FILES - paths)}")
        metadata_paths = [name for name in paths if name.endswith(".dist-info/METADATA")]
        if len(metadata_paths) != 1:
            raise ValueError("wheel must contain exactly one METADATA file")
        version = metadata_version(archive.read(metadata_paths[0]))
        dist_info = f"{ARCHIVE_NAME}-{version}.dist-info"
        if metadata_paths[0] != f"{dist_info}/METADATA":
            raise ValueError("wheel metadata directory does not match its version")
        if any(not name.startswith((f"{PACKAGE_NAME}/", f"{dist_info}/")) for name in paths):
            raise ValueError("wheel contains files outside the application and its metadata")
        if f"{dist_info}/licenses/LICENSE" not in paths:
            raise ValueError("wheel is missing the license")
        entry_points_path = f"{dist_info}/entry_points.txt"
        if entry_points_path not in paths:
            raise ValueError("wheel is missing its launcher")
        entry_points = configparser.ConfigParser()
        entry_points.read_string(archive.read(entry_points_path).decode("utf-8"))
        for launcher in ("x10-control", "medion-fan-control"):
            if entry_points.get("gui_scripts", launcher, fallback="") != "medion_fan_control.cli:main":
                raise ValueError("wheel has an incorrect GUI entry point")
        if entry_points.get("console_scripts", "x10ctld", fallback="") != "medion_fan_control.daemon:main":
            raise ValueError("wheel has an incorrect daemon entry point")

    if tag is not None and version != parse_tag(tag)[0]:
        raise ValueError(f"built version {version!r} does not match release tag {tag!r}")
    if wheel.name != f"{ARCHIVE_NAME}-{version}-py3-none-any.whl":
        raise ValueError("unexpected wheel filename")
    if sdist.name != f"{ARCHIVE_NAME}-{version}.tar.gz":
        raise ValueError("source distribution filename does not match the wheel version")

    with tarfile.open(sdist, "r:gz") as archive:
        members = archive.getmembers()
        if any(not member.isfile() and not member.isdir() for member in members):
            raise ValueError("source distribution contains links or special files")
        names = [member.name for member in members]
        validate_paths(set(names))
        if len(set(names)) != len(names):
            raise ValueError("duplicate source distribution members")
        prefix = f"{ARCHIVE_NAME}-{version}/"
        if any(not name.startswith(prefix) and name != prefix.rstrip("/") for name in names):
            raise ValueError("source distribution contains files outside its root")
        files = {member.name.removeprefix(prefix) for member in members if member.isfile()}
        required = REQUIRED_PACKAGE_FILES | REQUIRED_SOURCE_FILES | {"PKG-INFO"}
        if not required <= files:
            raise ValueError(f"source distribution is missing files: {sorted(required - files)}")
        if not any(name.startswith("tests/test_") and name.endswith(".py") for name in files):
            raise ValueError("source distribution is missing its tests")
        allowed_roots = {PACKAGE_NAME, "tests", "docs", "tools", "packaging", "kernel", "ext"}
        unrelated = {
            name for name in files
            if name not in required
            and PurePosixPath(name).parts[0] not in allowed_roots
            and not (PurePosixPath(name).parent == PurePosixPath("scripts") and name.endswith(".sh"))
        }
        unrelated |= {name for name in files if PurePosixPath(name).name == ".git"}
        if unrelated:
            raise ValueError(f"source distribution contains unrelated files: {sorted(unrelated)}")
        metadata_file = archive.extractfile(f"{prefix}PKG-INFO")
        if metadata_file is None:
            raise ValueError("source distribution metadata is not a file")
        with metadata_file:
            if metadata_version(metadata_file.read()) != version:
                raise ValueError("wheel and source distribution versions differ")
    return wheel, sdist


def write_checksums(directory: Path, distributions: tuple[Path, Path]) -> Path:
    lines = []
    for path in sorted(distributions):
        with path.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        lines.append(f"{digest}  {path.name}\n")
    destination = directory / "SHA256SUMS"
    destination.write_text("".join(lines), encoding="ascii")
    return destination


def changelog_section(text: str, version: str) -> str:
    """Body of the `## [version]` section of a Keep a Changelog file, or ''."""
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = re.match(rf"## \[{re.escape(version)}\](?:\s|$)", line) is not None
            continue
        if inside:
            body.append(line)
    return "\n".join(body).strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    tag_parser = commands.add_parser("tag", help="validate a tag and print GitHub Actions outputs")
    tag_parser.add_argument("tag")
    check_parser = commands.add_parser("check", help="validate a wheel and sdist, then write SHA256SUMS")
    check_parser.add_argument("directory", type=Path)
    check_parser.add_argument("--tag", help="require package versions to match this release tag")
    notes_parser = commands.add_parser("notes", help="print the CHANGELOG.md section for a release tag")
    notes_parser.add_argument("tag")
    notes_parser.add_argument("--changelog", type=Path, default=Path("CHANGELOG.md"))
    args = parser.parse_args(argv)
    try:
        if args.command == "tag":
            version, prerelease = parse_tag(args.tag)
            print(f"version={version}\nprerelease={str(prerelease).lower()}")
        elif args.command == "notes":
            notes = changelog_section(args.changelog.read_text(encoding="utf-8"), parse_tag(args.tag)[0])
            if notes:
                print(notes)
        else:
            distributions = check_distributions(args.directory, args.tag)
            write_checksums(args.directory, distributions)
            print("Validated wheel, source distribution, and SHA256SUMS.")
    except (OSError, ValueError, UnicodeError, zipfile.BadZipFile, tarfile.TarError, configparser.Error) as error:
        parser.exit(1, f"Release validation failed: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
