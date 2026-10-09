"""Atomic, owner-only JSON persistence for the daemon's saved state."""
from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_STATE_PATH = Path("/var/lib/x10ctld/state.json")


class SettingsError(RuntimeError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SettingsError(f"cannot load {path}: {error}") from error
    if not isinstance(document, dict):
        raise SettingsError(f"{path} does not contain a JSON object")
    return document


def save_json_atomic(path: Path, document: dict[str, Any]) -> None:
    temporary_path: Path | None = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    except OSError as error:
        raise SettingsError(f"cannot save {path}: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("Cannot remove temporary settings file %s: %s", temporary_path, error)


class StateStore:
    """Sectioned state document (``lighting``, ``cpu``, ``gpu``, ...)."""

    def __init__(self, path: Path = DEFAULT_STATE_PATH) -> None:
        self.path = path
        try:
            self._document = load_json(path)
            self.load_error: str | None = None
        except SettingsError as error:
            self._document = {}
            self.load_error = str(error)

    def get(self, section: str, default: Any = None) -> Any:
        return self._document.get(section, default)

    def update(self, section: str, value: Any) -> None:
        document = dict(self._document)
        document[section] = value
        save_json_atomic(self.path, document)
        self._document = document

    def merge(self, section: str, value: dict[str, Any]) -> None:
        current = self._document.get(section)
        merged = _deep_merge(current if isinstance(current, dict) else {}, value)
        self.update(section, merged)


def _deep_merge(base: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
