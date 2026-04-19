"""
Bleed Tool — settings.py
=========================
Persistencja ustawień GUI między sesjami.

Plik JSON w:
  macOS/Linux: ~/.config/bleed-tool/gui.json
  Windows:     %APPDATA%/bleed-tool/gui.json
  Override:    $BLEED_CONFIG_DIR

Best-effort: jeśli zapis/odczyt się nie powiedzie — log warning, zwrot defaultu.
Nie blokuje startu programu.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

_SETTINGS_VERSION = 1


def _config_dir() -> Path:
    override = os.environ.get("BLEED_CONFIG_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt":
        appdata = os.environ.get("APPDATA")
        base = Path(appdata) / "bleed-tool" if appdata else Path.home() / ".config" / "bleed-tool"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        base = Path(xdg) / "bleed-tool" if xdg else Path.home() / ".config" / "bleed-tool"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _settings_file() -> Path:
    return _config_dir() / "gui.json"


def load() -> dict[str, Any]:
    """Wczytuje ustawienia. Zwraca {} gdy brak pliku / corrupt."""
    p = _settings_file()
    if not p.exists():
        return {}
    try:
        with p.open(encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {}
        if data.get("version") != _SETTINGS_VERSION:
            return {}
        return data.get("values", {})
    except (json.JSONDecodeError, OSError) as e:
        log.warning(f"[settings] load failed: {e}")
        return {}


def save(values: dict[str, Any]) -> None:
    """Zapisuje ustawienia (best-effort)."""
    p = _settings_file()
    payload = {"version": _SETTINGS_VERSION, "values": values}
    try:
        tmp = p.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError as e:
        log.warning(f"[settings] save failed: {e}")


def get(key: str, default: Any = None) -> Any:
    return load().get(key, default)


def update(values: dict[str, Any]) -> None:
    """Merge + save. Inne klucze nietknięte."""
    current = load()
    current.update(values)
    save(current)
