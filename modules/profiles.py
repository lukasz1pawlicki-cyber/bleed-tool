"""
Bleed Tool — profiles.py
===========================
Loader profili eksportu per maszyna.

Ładuje `profiles/output_profiles.json` z katalogu projektu i scala z domyślnymi
wartościami z `config.PLOTTERS`. Profile z JSON nadpisują klucze z config'u.

Design:
  - config.PLOTTERS pozostaje jako fallback (gdy JSON brak/corrupted)
  - JSON = źródło prawdy dla operatora (można edytować bez ruszania kodu)
  - Tuple w cmyk/mark_size są dekodowane z JSON list → tuple (zachowuje kompatybilność)

Format JSON (uproszczony):
  {
    "profiles": {
      "summa_s3": {
        "label": "...",
        "mark_type": "opos_rectangle",
        "mark_size_mm": [3, 3],
        "cut_layers": {
          "CutContour": {"ocg_name": "CutContour", "cmyk": [1, 0, 1, 0]},
          ...
        },
        ...
      }
    }
  }

Odczyt niewystarczy — wywołaj `apply_profiles_to_config()` aby podmienić PLOTTERS.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


DEFAULT_PROFILES_PATH = Path(__file__).resolve().parent.parent / "profiles" / "output_profiles.json"


def _list_to_tuple_deep(obj):
    """Rekursywnie zamienia listy na tuple w cmyk/mark_size (JSON nie ma tuple)."""
    if isinstance(obj, list):
        return tuple(_list_to_tuple_deep(x) for x in obj)
    if isinstance(obj, dict):
        return {k: _list_to_tuple_deep(v) for k, v in obj.items()}
    return obj


def load_profiles(path: str | Path | None = None) -> dict:
    """Ładuje profile z JSON i konwertuje listy na tuple.

    Args:
        path: ścieżka do pliku JSON (None = domyślna w profiles/output_profiles.json)

    Returns:
        dict {profile_name: profile_dict} — pusty jeśli plik nie istnieje/corrupted.
    """
    if path is None:
        path = DEFAULT_PROFILES_PATH
    path = Path(path)

    if not path.is_file():
        log.warning(f"Profile file not found: {path} — używam wartości z config.PLOTTERS")
        return {}

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        log.error(f"Błąd ładowania profiles {path}: {e} — fallback na config.PLOTTERS")
        return {}

    profiles = data.get("profiles", {})
    if not isinstance(profiles, dict):
        log.error(f"Profile file {path} ma niepoprawną strukturę (brak 'profiles' dict)")
        return {}

    # Wyczyść _comment/_version z profile root
    profiles = {k: v for k, v in profiles.items() if not k.startswith("_")}

    # Konwertuj listy → tuple (cmyk, mark_size)
    result = {name: _list_to_tuple_deep(cfg) for name, cfg in profiles.items()}

    log.info(f"Załadowano {len(result)} profili z {path}: {list(result.keys())}")
    return result


def merge_with_defaults(defaults: dict, overrides: dict) -> dict:
    """Scala słowniki — overrides nadpisują defaults na pierwszym poziomie.

    Dla kluczy typu "cut_layers" robi shallow-merge na drugim poziomie (żeby
    JSON mógł nadpisać tylko 1 warstwę bez zastępowania całego słownika).

    Args:
        defaults: config.PLOTTERS (dict {name: {...}})
        overrides: wynik load_profiles() (dict {name: {...}})

    Returns:
        Nowy dict — nie modyfikuje oryginałów.
    """
    result = {}
    all_names = set(defaults.keys()) | set(overrides.keys())
    for name in all_names:
        base = dict(defaults.get(name, {}))
        over = overrides.get(name, {})

        # cut_layers: scal zagnieżdżony dict
        if "cut_layers" in over and "cut_layers" in base:
            merged_layers = dict(base["cut_layers"])
            for layer_key, layer_cfg in over["cut_layers"].items():
                merged_layers[layer_key] = layer_cfg
            base["cut_layers"] = merged_layers
            over = {k: v for k, v in over.items() if k != "cut_layers"}

        base.update(over)
        result[name] = base
    return result


def apply_profiles_to_config(
    config_module,
    profiles_path: str | Path | None = None,
) -> dict:
    """Ładuje profile z JSON i nadpisuje config_module.PLOTTERS.

    Wywołanie on-import z config.py. Bezpieczne — w przypadku braku JSON
    zostawia PLOTTERS bez zmian.

    Args:
        config_module: moduł `config` (import config; apply_profiles_to_config(config))
        profiles_path: ścieżka do JSON (None = domyślna)

    Returns:
        dict wynikowych profili (merged).
    """
    overrides = load_profiles(profiles_path)
    if not overrides:
        return dict(getattr(config_module, "PLOTTERS", {}))

    defaults = getattr(config_module, "PLOTTERS", {})
    merged = merge_with_defaults(defaults, overrides)

    # Nadpisz PLOTTERS in-place (zachowaj referencję — inne moduły mogły zaimportować)
    config_module.PLOTTERS.clear()
    config_module.PLOTTERS.update(merged)

    return merged
