"""Tests for the M1-4 LOT E bilingual (EN + FR) i18n foundation.

Pure (no Qt): exercises :mod:`zealfie.i18n` translation lookup, locale
detection, persistence, fallback, and safe placeholder formatting.
"""

from __future__ import annotations

import pytest

from zealfie.i18n import (
    EN,
    FR,
    Language,
    LanguageStore,
    detect_locale,
    effective_language,
    get_language,
    reset_language,
    set_language,
    tr,
    translate,
)


@pytest.fixture(autouse=True)
def _reset_language():
    """Ensure every test starts and ends in the default EN language."""
    reset_language()
    yield
    reset_language()


# ---------------------------------------------------------------------------
# 1 & 2 — English / French rendering
# ---------------------------------------------------------------------------


def test_english_rendering():
    set_language(Language.EN)
    assert translate("app.title") == EN["app.title"]
    assert translate("cards.install") == "Install"
    assert get_language() is Language.EN


def test_french_rendering():
    set_language(Language.FR)
    assert translate("app.title") == FR["app.title"]
    assert translate("cards.install") == "Installer"
    assert get_language() is Language.FR


def test_tr_is_alias_for_translate():
    set_language(Language.FR)
    assert tr("gpu.panel_title") == translate("gpu.panel_title")


# ---------------------------------------------------------------------------
# 3 — Persistence + effective_language
# ---------------------------------------------------------------------------


def test_persisted_selection(tmp_path, monkeypatch):
    path = tmp_path / "language.json"
    LanguageStore(path).save(Language.FR)

    fresh = LanguageStore(path)
    assert fresh.load() is Language.FR
    # Persisted preference wins over locale detection.
    monkeypatch.setattr(
        "zealfie.i18n.store.detect_locale", lambda: Language.EN
    )
    assert fresh.effective_language() is Language.FR


def test_effective_language_falls_back_to_locale(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "zealfie.i18n.store.detect_locale", lambda: Language.FR
    )
    assert LanguageStore(tmp_path / "missing.json").effective_language() is Language.FR


def test_load_is_lenient(tmp_path):
    path = tmp_path / "language.json"
    # Corrupt JSON → None
    path.write_text("{ not json", encoding="utf-8")
    assert LanguageStore(path).load() is None
    # Unknown language value → None
    path.write_text('{"language": "de"}', encoding="utf-8")
    assert LanguageStore(path).load() is None
    # Missing file → None
    assert LanguageStore(tmp_path / "nope.json").load() is None


# ---------------------------------------------------------------------------
# 4 — Fallback for a key missing from FR
# ---------------------------------------------------------------------------


def test_missing_fr_key_falls_back_to_en(monkeypatch):
    monkeypatch.delitem(FR, "cards.launch")
    set_language(Language.FR)
    assert translate("cards.launch") == EN["cards.launch"]
    assert translate("cards.launch") == "Launch"


def test_unknown_key_returns_raw_key():
    # A programming error, never expected in normal UI.
    assert translate("no.such.key.anywhere") == "no.such.key.anywhere"


# ---------------------------------------------------------------------------
# 5 — Locale detection never raises
# ---------------------------------------------------------------------------


def test_detect_locale_unknown_fallback_en(monkeypatch):
    import locale as locale_module

    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(
        locale_module, "getdefaultlocale", lambda: ("de_DE", "UTF-8")
    )
    assert detect_locale() is Language.EN


def test_detect_locale_fr(monkeypatch):
    for var in ("LC_ALL", "LC_MESSAGES"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LANG", "fr_FR.UTF-8")
    assert detect_locale() is Language.FR


def test_detect_locale_survives_locale_failure(monkeypatch):
    import locale as locale_module

    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)

    def _boom():
        raise RuntimeError("locale data unavailable")

    monkeypatch.setattr(locale_module, "getdefaultlocale", _boom)
    assert detect_locale() is Language.EN


# ---------------------------------------------------------------------------
# 6 — No raw dotted translation keys leaked into rendered strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("lang", [Language.EN, Language.FR])
def test_no_raw_translation_keys_leaked(lang):
    set_language(lang)
    samples = (
        translate("app.title"),
        translate("app.subtitle"),
        translate("cards.install"),
        translate("cards.launch"),
        translate("status.installing", name="ZeSolver"),
        translate("gpu.panel_title"),
        translate("gpu.configure"),
        translate("state.not_installed"),
    )
    for text in samples:
        assert text, "rendered string must not be empty"
        for prefix in ("app.", "cards.", "status.", "gpu.", "state.", "error."):
            assert prefix not in text, f"raw key leaked into {text!r}"


# ---------------------------------------------------------------------------
# 7 — Dynamic formatting preserves variables (SafeDict)
# ---------------------------------------------------------------------------


def test_dynamic_formatting_preserves_variables(monkeypatch):
    set_language(Language.EN)
    monkeypatch.setitem(EN, "test.two_placeholders", "Hello {a} and {b}")
    # Provided value is substituted; missing placeholder preserved verbatim.
    assert translate("test.two_placeholders", a="1") == "Hello 1 and {b}"

    text = translate("status.installing", name="X")
    assert text.count("X") == 1
    assert text == "Installing X\u2026"

    # No kwargs → template returned untouched (no KeyError).
    assert translate("status.installing") == "Installing {name}\u2026"


def test_effective_language_module_function_is_usable():
    # The module-level convenience function exists and returns a Language.
    result = effective_language()
    assert isinstance(result, Language)
