from __future__ import annotations

import json
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

from .lighting import LightingAccessError, LightZone, RgbColor


logger = logging.getLogger(__name__)


def lighting_config_path() -> Path:
    config_home = os.environ.get("XDG_CONFIG_HOME")
    root = Path(config_home) if config_home else Path.home() / ".config"
    if not root.is_absolute():
        raise LightingAccessError("XDG_CONFIG_HOME must be an absolute path")
    return root / "medion-fan-control" / "lighting.json"


def load_lighting_colors(path: Path | None = None) -> dict[LightZone, RgbColor]:
    config_path = path or lighting_config_path()
    try:
        document = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise LightingAccessError(f"cannot load saved lighting colors: {error}") from error
    if not isinstance(document, dict) or not isinstance(document.get("colors"), dict):
        raise LightingAccessError("saved lighting colors have an invalid format")
    try:
        return {
            LightZone[zone_name.upper()]: RgbColor.from_hex(value)
            for zone_name, value in document["colors"].items()
        }
    except (KeyError, ValueError) as error:
        raise LightingAccessError(f"saved lighting colors have an invalid value: {error}") from error


def save_lighting_colors(colors: Mapping[LightZone, RgbColor], path: Path | None = None) -> None:
    config_path = path or lighting_config_path()
    document = {
        "colors": {
            zone.name.lower(): color.hex
            for zone, color in sorted(colors.items(), key=lambda item: item[0].value)
        }
    }
    temporary_path: Path | None = None
    try:
        config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=config_path.parent,
            prefix=".lighting-",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(json.dumps(document, indent=2) + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, config_path)
        temporary_path = None
    except OSError as error:
        raise LightingAccessError(f"cannot save lighting colors: {error}") from error
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as error:
                logger.warning("Cannot remove temporary settings file %s: %s", temporary_path, error)
