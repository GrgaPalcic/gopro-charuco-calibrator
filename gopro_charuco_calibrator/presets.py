"""Loadable camera presets.

A preset is a small YAML file describing a known-good, consistent acquisition
configuration (camera mode + GoPro webcam settings + board). Locking these into
a named preset is how operators keep calibration data reproducible across runs
and machines.

Shipped presets live read-only inside the package; user presets are written to
``~/.config/gopro-charuco-calibrator/presets`` and override shipped ones of the
same name.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import AppConfig

SHIPPED_PRESETS_DIR = Path(__file__).resolve().parent / "presets"


def user_presets_dir() -> Path:
    return Path.home() / ".config" / "gopro-charuco-calibrator" / "presets"


def _safe_name(name: str) -> str:
    name = (name or "").strip()
    if not name:
        raise ValueError("Preset name is required.")
    if name != Path(name).name or name in (".", ".."):
        raise ValueError("Preset name must not contain path separators.")
    cleaned = "".join(ch for ch in name if ch.isalnum() or ch in ("_", "-"))
    if not cleaned:
        raise ValueError("Preset name must contain letters, digits, '-' or '_'.")
    return cleaned


def load_preset_file(path: Path) -> tuple[str, AppConfig]:
    """Parse a preset YAML file into ``(title, AppConfig)``.

    Raises ``ValueError`` for malformed files or invalid configs so callers can
    map it to a clear HTTP error.
    """
    try:
        raw = path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)
    except (OSError, yaml.YAMLError) as exc:
        raise ValueError(f"Could not read preset {path.name}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Preset {path.name} must be a YAML mapping.")
    title = str(data.pop("title", path.stem))
    if "config" in data and isinstance(data["config"], dict):
        data = data["config"]
    try:
        config = AppConfig.model_validate(data)
    except ValidationError as exc:
        raise ValueError(f"Invalid preset {path.name}: {exc}") from exc
    return title, config


def _preset_files() -> dict[str, tuple[Path, str]]:
    """Map preset name -> (path, source), with user presets overriding shipped."""
    found: dict[str, tuple[Path, str]] = {}
    for directory, source in ((SHIPPED_PRESETS_DIR, "shipped"), (user_presets_dir(), "user")):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml")):
            found[path.stem] = (path, source)
    return found


def list_presets() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for name, (path, source) in sorted(_preset_files().items()):
        entry: dict[str, Any] = {"name": name, "source": source}
        try:
            title, _ = load_preset_file(path)
            entry["title"] = title
        except ValueError as exc:
            entry["title"] = name
            entry["error"] = str(exc)
        entries.append(entry)
    return entries


def get_preset(name: str) -> tuple[str, AppConfig]:
    files = _preset_files()
    if name not in files:
        raise KeyError(name)
    path, _ = files[name]
    return load_preset_file(path)


def save_preset(name: str, config: AppConfig, title: str | None = None) -> Path:
    safe = _safe_name(name)
    directory = user_presets_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = (directory / f"{safe}.yaml").resolve()
    if directory.resolve() not in path.parents:
        raise ValueError("Refusing to write preset outside the user preset directory.")
    payload: dict[str, Any] = {"title": title or safe}
    payload.update(config.model_dump())
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")
    return path
