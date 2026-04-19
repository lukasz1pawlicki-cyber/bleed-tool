"""
Bleed Tool — cache.py
========================
Disk-based cache dla wynikow detect_contour().

Cel: najdrozszy krok w pipeline (raster trace / vector flattening)
nie powtarza sie gdy operator zmienia tylko bleed_mm lub parametry eksportu.

Klucz cache: sha1(plik_input + mtime + size + config.CONTOUR_ENGINE)
  - zmiana zawartosci pliku -> inny sha1 -> cache miss
  - zmiana silnika konturu -> inny klucz -> cache miss
  - identyczny plik + ten sam engine -> cache hit (typowo <5ms)

Format: pickle — umie natywnie numpy.ndarray (cut_segments).
Lokacja: ~/.cache/bleed-tool/contour/{sha1}.pkl (Linux/macOS)
         %LOCALAPPDATA%/bleed-tool/contour/{sha1}.pkl (Windows, fallback na ~)

Env override:
  BLEED_CACHE_DIR=/custom/path   - katalog cache
  BLEED_NO_CACHE=1               - wylacza cache (zawsze miss)
"""
from __future__ import annotations

import hashlib
import logging
import os
import pickle
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_VERSION = 1  # zmien gdy zmienia sie format Sticker serialization


def _default_cache_dir() -> Path:
    """Zwraca domyslny katalog cache (tworzy jesli nie istnieje)."""
    override = os.environ.get("BLEED_CACHE_DIR")
    if override:
        base = Path(override)
    elif os.name == "nt":
        appdata = os.environ.get("LOCALAPPDATA")
        base = Path(appdata) / "bleed-tool" if appdata else Path.home() / ".cache" / "bleed-tool"
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg) / "bleed-tool" if xdg else Path.home() / ".cache" / "bleed-tool"
    path = base / "contour"
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_cache_enabled() -> bool:
    """Czy cache jest wlaczony (mozna wylaczyc przez env BLEED_NO_CACHE=1)."""
    return os.environ.get("BLEED_NO_CACHE", "").strip() not in ("1", "true", "yes")


def _compute_key(file_path: str, engine: str) -> str:
    """sha1(realpath + mtime_ns + size + engine + cache_version).

    Uzywamy mtime_ns + size (szybkie, nie czytamy zawartosci).
    Dla plikow <1MB mozna by liczyc hash zawartosci, ale na typowych
    naklejkach >5MB stat() jest o 2 rzedy szybszy.
    """
    try:
        st = os.stat(file_path)
    except OSError:
        return ""
    canonical = os.path.realpath(file_path)
    raw = f"{canonical}|{st.st_mtime_ns}|{st.st_size}|{engine}|v{_CACHE_VERSION}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def _cache_file(key: str) -> Path:
    return _default_cache_dir() / f"{key}.pkl"


# ============================================================================
# SERIALIZATION — Sticker -> dict (pickle-safe)
# ============================================================================

def _serialize_sticker(sticker) -> dict:
    """Wyciaga metadane + cut_segments — pomija niepicklowalne (pdf_doc)."""
    return {
        "source_path": sticker.source_path,
        "page_index": sticker.page_index,
        "width_mm": sticker.width_mm,
        "height_mm": sticker.height_mm,
        "cut_segments": sticker.cut_segments,  # tuples z np.ndarray
        "page_width_pt": sticker.page_width_pt,
        "page_height_pt": sticker.page_height_pt,
        "outermost_drawing_idx": sticker.outermost_drawing_idx,
        "raster_path": sticker.raster_path,
        "raster_crop_box": sticker.raster_crop_box,
        "is_bleed_output": sticker.is_bleed_output,
        "cutline_mode": sticker.cutline_mode,
        "is_artwork_on_artboard": sticker.is_artwork_on_artboard,
        "is_cmyk": sticker.is_cmyk,
    }


def _deserialize_sticker(data: dict):
    """Tworzy nowy Sticker z danych cache. pdf_doc zostaje None
    — wywolujacy musi go osobno zaladowac (lazy)."""
    from models import Sticker
    return Sticker(
        source_path=data["source_path"],
        page_index=data["page_index"],
        width_mm=data["width_mm"],
        height_mm=data["height_mm"],
        cut_segments=data["cut_segments"],
        page_width_pt=data["page_width_pt"],
        page_height_pt=data["page_height_pt"],
        outermost_drawing_idx=data["outermost_drawing_idx"],
        raster_path=data.get("raster_path"),
        raster_crop_box=data.get("raster_crop_box"),
        is_bleed_output=data.get("is_bleed_output", False),
        cutline_mode=data.get("cutline_mode", "kiss-cut"),
        is_artwork_on_artboard=data.get("is_artwork_on_artboard", False),
        is_cmyk=data.get("is_cmyk", False),
    )


# ============================================================================
# PUBLIC API
# ============================================================================

def load(file_path: str, engine: str) -> list | None:
    """Probuje zaladowac stickers z cache.

    Zwraca list[Sticker] (bez pdf_doc — trzeba go osobno zaladowac)
    lub None gdy cache miss / disabled / bledny plik cache.
    """
    if not is_cache_enabled():
        return None
    key = _compute_key(file_path, engine)
    if not key:
        return None
    cf = _cache_file(key)
    if not cf.exists():
        return None
    try:
        with cf.open("rb") as f:
            payload = pickle.load(f)
    except (pickle.PickleError, EOFError, OSError) as e:
        log.debug(f"Cache miss (corrupt {cf.name}): {e}")
        return None

    # Sanity: payload musi byc dictem (chroni przed spreparowanymi plikami)
    if not isinstance(payload, dict):
        log.debug(f"Cache miss (not a dict {cf.name})")
        return None

    if payload.get("version") != _CACHE_VERSION:
        log.debug(f"Cache miss (old version {payload.get('version')})")
        return None

    raw_stickers = payload.get("stickers")
    if not isinstance(raw_stickers, list):
        log.debug(f"Cache miss (stickers not a list {cf.name})")
        return None

    try:
        stickers = [_deserialize_sticker(d) for d in raw_stickers]
    except (KeyError, TypeError) as e:
        log.debug(f"Cache miss (bad payload {cf.name}): {e}")
        return None

    log.info(f"[cache] HIT {os.path.basename(file_path)} ({len(stickers)} sticker/ow)")
    return stickers


def save(file_path: str, engine: str, stickers: list) -> None:
    """Zapisuje stickers do cache (best-effort — bledy nie propagują)."""
    if not is_cache_enabled():
        return
    key = _compute_key(file_path, engine)
    if not key:
        return
    cf = _cache_file(key)
    payload = {
        "version": _CACHE_VERSION,
        "engine": engine,
        "created_at": time.time(),
        "stickers": [_serialize_sticker(s) for s in stickers],
    }
    try:
        # Atomic write: tmp + rename (zapobiega polowicznie zapisanym plikom)
        tmp_fd, tmp_path = tempfile.mkstemp(
            suffix=".tmp", prefix=cf.name, dir=str(cf.parent)
        )
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                pickle.dump(payload, f, protocol=pickle.HIGHEST_PROTOCOL)
            os.replace(tmp_path, cf)
        except Exception:
            # Cleanup tmp w razie bledu
            if os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            raise
        log.debug(f"[cache] SAVE {os.path.basename(file_path)}")
    except Exception as e:
        log.debug(f"[cache] SAVE failed for {file_path}: {e}")


def clear_all() -> int:
    """Usuwa wszystkie pliki cache. Zwraca liczbe usunietych."""
    d = _default_cache_dir()
    count = 0
    for p in d.glob("*.pkl"):
        try:
            p.unlink()
            count += 1
        except OSError:
            pass
    return count


def size_bytes() -> int:
    """Sumaryczny rozmiar cache na dysku."""
    d = _default_cache_dir()
    return sum(p.stat().st_size for p in d.glob("*.pkl") if p.is_file())
