"""Testy modules/profiles.py — loader profili eksportu per maszyna."""
import json
import pytest

from modules.profiles import (
    load_profiles,
    merge_with_defaults,
    apply_profiles_to_config,
    _list_to_tuple_deep,
)


# ============================================================================
# _list_to_tuple_deep — konwersja JSON list → tuple
# ============================================================================

def test_list_to_tuple_simple():
    assert _list_to_tuple_deep([1, 2, 3]) == (1, 2, 3)


def test_list_to_tuple_nested():
    data = {"cmyk": [1, 0, 1, 0], "nested": {"size": [3, 3]}}
    result = _list_to_tuple_deep(data)
    assert result == {"cmyk": (1, 0, 1, 0), "nested": {"size": (3, 3)}}


def test_list_to_tuple_leaves_scalars():
    assert _list_to_tuple_deep(42) == 42
    assert _list_to_tuple_deep("hello") == "hello"
    assert _list_to_tuple_deep(None) is None


# ============================================================================
# load_profiles — odczyt JSON
# ============================================================================

def test_load_profiles_missing_file_returns_empty(tmp_path):
    """Brak pliku → empty dict + warning (nie wyjątek)."""
    nonexistent = tmp_path / "no_such_file.json"
    result = load_profiles(nonexistent)
    assert result == {}


def test_load_profiles_malformed_json_returns_empty(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text("{not valid json}")
    result = load_profiles(bad)
    assert result == {}


def test_load_profiles_empty_profiles_key(tmp_path):
    f = tmp_path / "empty.json"
    f.write_text(json.dumps({"profiles": {}}))
    result = load_profiles(f)
    assert result == {}


def test_load_profiles_valid(tmp_path):
    f = tmp_path / "profiles.json"
    data = {
        "_comment": "test",
        "_version": 1,
        "profiles": {
            "test_plotter": {
                "label": "Test",
                "mark_size_mm": [3, 3],
                "cut_layers": {
                    "CutContour": {"ocg_name": "CC", "cmyk": [1, 0, 1, 0]},
                },
            }
        }
    }
    f.write_text(json.dumps(data))
    result = load_profiles(f)

    assert "test_plotter" in result
    assert result["test_plotter"]["label"] == "Test"
    # Listy zamienione na tuple
    assert result["test_plotter"]["mark_size_mm"] == (3, 3)
    assert result["test_plotter"]["cut_layers"]["CutContour"]["cmyk"] == (1, 0, 1, 0)


def test_load_profiles_strips_underscore_keys(tmp_path):
    """Klucze zaczynające się od _ (np. _comment) są ignorowane na poziomie profile."""
    f = tmp_path / "profiles.json"
    data = {
        "profiles": {
            "_meta": {"ignored": True},
            "real_plotter": {"label": "Real"},
        }
    }
    f.write_text(json.dumps(data))
    result = load_profiles(f)
    assert "_meta" not in result
    assert "real_plotter" in result


def test_load_profiles_malformed_profiles_key(tmp_path):
    """profiles jako string zamiast dict → empty dict."""
    f = tmp_path / "bad.json"
    f.write_text(json.dumps({"profiles": "not a dict"}))
    result = load_profiles(f)
    assert result == {}


# ============================================================================
# merge_with_defaults — scalanie config + JSON
# ============================================================================

def test_merge_adds_new_plotter():
    defaults = {"summa": {"mark_size": (3, 3)}}
    overrides = {"new_plotter": {"mark_size": (5, 5)}}
    result = merge_with_defaults(defaults, overrides)
    assert "summa" in result
    assert "new_plotter" in result


def test_merge_overrides_top_level_keys():
    defaults = {"summa": {"mark_size": (3, 3), "offset": 10}}
    overrides = {"summa": {"offset": 15}}
    result = merge_with_defaults(defaults, overrides)
    assert result["summa"]["mark_size"] == (3, 3)  # zachowany
    assert result["summa"]["offset"] == 15  # nadpisany


def test_merge_cut_layers_shallow_merge():
    """cut_layers: nadpisanie jednej warstwy nie kasuje pozostałych."""
    defaults = {
        "summa": {
            "cut_layers": {
                "CutContour": {"ocg_name": "CutContour", "cmyk": (1, 0, 1, 0)},
                "FlexCut": {"ocg_name": "FlexCut", "cmyk": (0, 1, 1, 0)},
                "Regmark": {"ocg_name": "Regmark", "cmyk": (0, 0, 0, 1)},
            }
        }
    }
    overrides = {
        "summa": {
            "cut_layers": {
                "CutContour": {"ocg_name": "NewName", "cmyk": (0.5, 0, 0.5, 0)},
            }
        }
    }
    result = merge_with_defaults(defaults, overrides)
    layers = result["summa"]["cut_layers"]
    assert layers["CutContour"]["ocg_name"] == "NewName"  # nadpisane
    assert "FlexCut" in layers  # zachowane
    assert "Regmark" in layers  # zachowane


def test_merge_empty_overrides_preserves_defaults():
    defaults = {"summa": {"x": 1}}
    result = merge_with_defaults(defaults, {})
    assert result == defaults


def test_merge_does_not_mutate_inputs():
    defaults = {"a": {"x": 1}}
    overrides = {"a": {"y": 2}}
    _ = merge_with_defaults(defaults, overrides)
    assert defaults == {"a": {"x": 1}}
    assert overrides == {"a": {"y": 2}}


# ============================================================================
# apply_profiles_to_config — integration z config.PLOTTERS
# ============================================================================

def test_apply_profiles_uses_defaults_when_json_missing(tmp_path):
    """Brak JSON → PLOTTERS nie zmienione."""
    class FakeConfig:
        PLOTTERS = {"summa": {"x": 1}}
    nonexistent = tmp_path / "nope.json"
    result = apply_profiles_to_config(FakeConfig, profiles_path=nonexistent)
    assert result == {"summa": {"x": 1}}
    assert FakeConfig.PLOTTERS == {"summa": {"x": 1}}


def test_apply_profiles_merges_and_mutates_in_place(tmp_path):
    class FakeConfig:
        PLOTTERS = {"summa": {"existing_key": "base", "mark_size": (3, 3)}}

    f = tmp_path / "profiles.json"
    f.write_text(json.dumps({
        "profiles": {
            "summa": {"new_key": "from_json"},
            "brand_new": {"label": "X"},
        }
    }))
    result = apply_profiles_to_config(FakeConfig, profiles_path=f)
    assert "new_key" in FakeConfig.PLOTTERS["summa"]
    assert FakeConfig.PLOTTERS["summa"]["existing_key"] == "base"  # zachowany
    assert "brand_new" in FakeConfig.PLOTTERS
    # In-place: same dict reference
    assert id(result) != id(FakeConfig.PLOTTERS)  # result jest nowy
    assert FakeConfig.PLOTTERS["summa"]["new_key"] == "from_json"


# ============================================================================
# Integracja z rzeczywistym config.py
# ============================================================================

def test_config_has_expected_plotters():
    """config.PLOTTERS ma co najmniej summa_s3 i jwei po załadowaniu."""
    import config
    assert "summa_s3" in config.PLOTTERS
    assert "jwei" in config.PLOTTERS


def test_config_summa_profile_has_required_keys():
    import config
    summa = config.PLOTTERS["summa_s3"]
    # Krytyczne klucze dla marks.py i export.py
    assert "mark_size_mm" in summa
    assert "mark_offset_mm" in summa
    assert "cut_layers" in summa
    assert "CutContour" in summa["cut_layers"]
    assert "cmyk" in summa["cut_layers"]["CutContour"]


def test_config_jwei_profile_has_jwei_specific_keys():
    import config
    jwei = config.PLOTTERS["jwei"]
    assert "mark_offset_x_mm" in jwei
    assert "mark_offset_y_mm" in jwei
    # JWEI używa SP3/SP2 zamiast CutContour/FlexCut
    assert jwei["cut_layers"]["CutContour"]["ocg_name"] == "SP3"


def test_config_cmyk_values_are_tuples_not_lists():
    """config.py wewnętrznie używa tuple — JSON ładuje listy, loader konwertuje."""
    import config
    cmyk = config.PLOTTERS["summa_s3"]["cut_layers"]["CutContour"]["cmyk"]
    assert isinstance(cmyk, tuple)
