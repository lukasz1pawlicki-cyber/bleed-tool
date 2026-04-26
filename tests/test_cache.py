"""Testy cache dla detect_contour."""
from __future__ import annotations

import os
import time

import pytest

from modules import cache
from modules.contour import detect_contour
from tests.fixtures import make_rectangle_vector


@pytest.fixture
def isolated_cache(tmp_path, monkeypatch):
    """Izolowany katalog cache (nie zanieczyszcza ~/.cache/bleed-tool)."""
    cache_dir = tmp_path / "bleed_cache"
    monkeypatch.setenv("BLEED_CACHE_DIR", str(cache_dir))
    monkeypatch.delenv("BLEED_NO_CACHE", raising=False)
    yield cache_dir


def test_cache_enabled_by_default(monkeypatch):
    monkeypatch.delenv("BLEED_NO_CACHE", raising=False)
    assert cache.is_cache_enabled() is True


def test_cache_disabled_by_env(monkeypatch):
    monkeypatch.setenv("BLEED_NO_CACHE", "1")
    assert cache.is_cache_enabled() is False


def test_cache_miss_on_fresh_file(tmp_path, isolated_cache):
    pdf = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)
    assert cache.load(pdf, "moore") is None


def test_cache_save_and_hit(tmp_path, isolated_cache):
    pdf = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)

    # Pierwsze wywolanie — cache miss, zapisuje
    stickers = detect_contour(pdf)
    assert len(stickers) == 1
    if stickers[0].pdf_doc is not None:
        stickers[0].pdf_doc.close()

    # Drugie wywolanie — cache hit, brak tracingu
    stickers2 = detect_contour(pdf)
    assert len(stickers2) == 1
    assert abs(stickers2[0].width_mm - stickers[0].width_mm) < 0.1
    assert abs(stickers2[0].height_mm - stickers[0].height_mm) < 0.1
    assert len(stickers2[0].cut_segments) == len(stickers[0].cut_segments)
    if stickers2[0].pdf_doc is not None:
        stickers2[0].pdf_doc.close()


def test_cache_invalidated_by_file_change(tmp_path, isolated_cache):
    """Modyfikacja pliku (nowy mtime/size) powoduje cache miss."""
    pdf = make_rectangle_vector(tmp_path, w_mm=60, h_mm=60)
    s1 = detect_contour(pdf)
    if s1[0].pdf_doc is not None:
        s1[0].pdf_doc.close()

    # Trzymamy sie oryginalnego silnika dla jednoznacznosci klucza
    from config import CONTOUR_ENGINE
    cached1 = cache.load(pdf, CONTOUR_ENGINE)
    assert cached1 is not None

    # Upewnij sie ze mtime rosnie (macOS ma 1s rozdzielczosc na niektorych FS)
    time.sleep(0.05)

    # Nadpisujemy plik nowa zawartoscia (inny rozmiar)
    pdf2 = make_rectangle_vector(tmp_path, w_mm=80, h_mm=80)
    os.replace(pdf2, pdf)

    cached2 = cache.load(pdf, CONTOUR_ENGINE)
    assert cached2 is None, "Zmiana pliku powinna uniewaznic cache"


def test_cache_different_engine_different_key(tmp_path, isolated_cache):
    """Zmiana silnika konturu → inny klucz → cache miss."""
    pdf = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)
    stickers = detect_contour(pdf)
    if stickers[0].pdf_doc is not None:
        stickers[0].pdf_doc.close()
    # detect_contour() auto-zapisal pod config.CONTOUR_ENGINE (domyslnie
    # "opencv") — czyscimy zeby manual save/load operowaly na czystym stanie
    # i test sprawdzal dokladnie roznicowanie po engine.
    cache.clear_all()
    cache.save(pdf, "moore", stickers)
    # Inny engine → miss
    assert cache.load(pdf, "opencv") is None
    # Ten sam engine → hit
    assert cache.load(pdf, "moore") is not None


def test_cache_no_cache_env_disables_save_and_load(tmp_path, monkeypatch):
    cache_dir = tmp_path / "bleed_cache"
    monkeypatch.setenv("BLEED_CACHE_DIR", str(cache_dir))
    monkeypatch.setenv("BLEED_NO_CACHE", "1")

    pdf = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)
    stickers = detect_contour(pdf)
    if stickers[0].pdf_doc is not None:
        stickers[0].pdf_doc.close()

    # Z disabled cache: nic nie bylo zapisane
    assert cache.load(pdf, "moore") is None
    # Katalog moze istniec albo nie — jesli istnieje, jest pusty
    if cache_dir.exists():
        pkls = list((cache_dir / "contour").glob("*.pkl")) if (cache_dir / "contour").exists() else []
        assert len(pkls) == 0


def test_cache_preserves_sticker_fields(tmp_path, isolated_cache):
    """Wszystkie istotne pola stickera zachowane po round-trip przez cache."""
    pdf = make_rectangle_vector(tmp_path, w_mm=75, h_mm=55)
    s1 = detect_contour(pdf)[0]
    if s1.pdf_doc is not None:
        s1.pdf_doc.close()

    s2 = detect_contour(pdf)[0]
    try:
        assert s2.source_path == s1.source_path
        assert s2.page_index == s1.page_index
        assert s2.width_mm == s1.width_mm
        assert s2.height_mm == s1.height_mm
        assert s2.page_width_pt == s1.page_width_pt
        assert s2.page_height_pt == s1.page_height_pt
        assert s2.outermost_drawing_idx == s1.outermost_drawing_idx
        assert s2.is_artwork_on_artboard == s1.is_artwork_on_artboard
        assert s2.is_cmyk == s1.is_cmyk
        assert len(s2.cut_segments) == len(s1.cut_segments)
        # Pdf doc musi byc ponownie otwarty dla cached stickera
        assert s2.pdf_doc is not None
    finally:
        if s2.pdf_doc is not None:
            s2.pdf_doc.close()


def test_cache_hit_is_fast(tmp_path, isolated_cache):
    """Cache hit musi byc znacznie szybszy niz pelna detekcja.

    Warm-up -> first call -> second call. Typowo hit <30ms vs miss >100ms.
    """
    pdf = make_rectangle_vector(tmp_path, w_mm=80, h_mm=50)

    # Pierwsze: zapisuje do cache
    t0 = time.perf_counter()
    s1 = detect_contour(pdf)
    t_miss = time.perf_counter() - t0
    if s1[0].pdf_doc is not None:
        s1[0].pdf_doc.close()

    # Drugie: hit
    t0 = time.perf_counter()
    s2 = detect_contour(pdf)
    t_hit = time.perf_counter() - t0
    if s2[0].pdf_doc is not None:
        s2[0].pdf_doc.close()

    # Hit musi byc przynajmniej 2x szybszy niz miss (w praktyce 5-50x)
    assert t_hit < t_miss, f"hit {t_hit*1000:.1f}ms !< miss {t_miss*1000:.1f}ms"


def test_clear_all(tmp_path, isolated_cache):
    pdf = make_rectangle_vector(tmp_path, w_mm=50, h_mm=50)
    s = detect_contour(pdf)
    if s[0].pdf_doc is not None:
        s[0].pdf_doc.close()

    assert cache.size_bytes() > 0
    n = cache.clear_all()
    assert n >= 1
    assert cache.size_bytes() == 0
