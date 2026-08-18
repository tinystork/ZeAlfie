"""Bilingual (EN + FR) internationalisation foundation for the ZeAlfie GUI.

This package is the single coherent translation layer for the product shell.
Widgets never branch on language; they call :func:`translate` (or its alias
:func:`tr`) with a stable dotted key, and this module resolves it against the
current language:

* ``EN`` is the always-complete source/default catalogue;
* ``FR`` is used when the current language is French and the key exists there;
* a missing key falls back to ``EN``, and a key missing from both returns the
  raw key (a programming error, never expected in normal UI).

Dynamic placeholders are formatted with a safe formatter so a missing value
never raises ``KeyError`` and unknown placeholders are preserved verbatim.

.. note::
    The CLI self-update introduced in LOT D is command-line only (there is no
    GUI self-update widget), so its messages remain English and are not part
    of these catalogues.
"""

from __future__ import annotations

import string
from enum import StrEnum

from .catalog import EN, FR


class Language(StrEnum):
    """Supported UI languages."""

    EN = "en"
    FR = "fr"


#: The in-memory current language (process-global, default EN).
_current_language: Language = Language.EN


def get_language() -> Language:
    """Return the current UI language."""
    return _current_language


def set_language(lang: Language) -> None:
    """Set the current UI language (coerces raw strings via ``Language``)."""
    global _current_language
    _current_language = lang if isinstance(lang, Language) else Language(lang)


def reset_language() -> None:
    """Reset to English (used by tests to clear any in-memory override)."""
    global _current_language
    _current_language = Language.EN


class SafeDict(dict):
    """A dict whose ``__missing__`` returns the literal placeholder text."""

    def __missing__(self, key):
        return "{" + str(key) + "}"


def translate(key: str, **kwargs) -> str:
    """Return the localized string for *key*, formatting any ``**kwargs``.

    Resolution order: FR catalogue (only when current language is FR and the
    key exists there) → EN catalogue (always-complete) → the raw key.
    """
    if _current_language is Language.FR and key in FR:
        template = FR[key]
    else:
        template = EN.get(key, key)
    if kwargs:
        return string.Formatter().vformat(template, (), SafeDict(kwargs))
    return template


#: Short alias for :func:`translate`.
tr = translate


# Imported last: these submodules import ``Language`` back from this package,
# so they must be loaded after ``Language`` is defined above.
from .locale import detect_locale  # noqa: E402
from .store import LanguageStore, effective_language  # noqa: E402


__all__ = [
    "Language",
    "get_language",
    "set_language",
    "reset_language",
    "translate",
    "tr",
    "detect_locale",
    "LanguageStore",
    "effective_language",
    "EN",
    "FR",
]
