"""Small, testable helpers for kernel attribute files.

All paths are resolved below a configurable root so tests can build a fake
``/sys`` tree in a temporary directory.
"""
from __future__ import annotations

from pathlib import Path


class SysfsError(RuntimeError):
    pass


class Sysfs:
    def __init__(self, root: Path = Path("/")) -> None:
        self.root = Path(root)

    def path(self, relative: str | Path) -> Path:
        relative = Path(relative)
        return self.root / (relative.relative_to("/") if relative.is_absolute() else relative)

    def exists(self, relative: str | Path) -> bool:
        return self.path(relative).exists()

    def read(self, relative: str | Path) -> str:
        path = self.path(relative)
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError) as error:
            raise SysfsError(f"cannot read {path}: {error}") from error

    def read_optional(self, relative: str | Path) -> str | None:
        try:
            return self.read(relative)
        except SysfsError:
            return None

    def read_int(self, relative: str | Path) -> int:
        text = self.read(relative)
        try:
            return int(text, 0)
        except ValueError as error:
            raise SysfsError(f"{self.path(relative)} does not contain an integer: {text!r}") from error

    def read_int_optional(self, relative: str | Path) -> int | None:
        try:
            return self.read_int(relative)
        except SysfsError:
            return None

    def write(self, relative: str | Path, value: object, *, verify: bool = False) -> None:
        path = self.path(relative)
        text = str(value)
        try:
            with path.open("w", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as error:
            raise SysfsError(f"cannot write {text!r} to {path}: {error.strerror or error}") from error
        if verify:
            actual = self.read(relative)
            if actual != text:
                raise SysfsError(f"{path} reads back {actual!r} after writing {text!r}")

    def glob(self, relative_dir: str | Path, pattern: str) -> list[Path]:
        base = self.path(relative_dir)
        return sorted(base.glob(pattern)) if base.is_dir() else []

    def relative(self, absolute: Path) -> Path:
        return Path("/") / absolute.relative_to(self.root)
