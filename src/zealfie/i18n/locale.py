"""Locale inference for the ZeAlfie GUI (never raises; EN is the safe default).

Detection sources, in priority order:

* ``LC_ALL``, ``LC_MESSAGES``, ``LANG`` environment variables;
* ``locale.getdefaultlocale()[0]``.

A ``fr*`` language tag maps to :class:`~zealfie.i18n.Language.FR`; everything
else (including ``C``/``POSIX``, unknown tags, and any detection failure)
maps to :class:`~zealfie.i18n.Language.EN`.  There is no hard dependency on
locale data being installed.
"""

from __future__ import annotations

import locale
import os

from . import Language


def _language_tag(value: object) -> str:
    """Normalize a locale value to a lowercase language tag (``""`` if unknown).

    ``"fr_FR.UTF-8"`` → ``"fr"``; ``"C"`` → ``"c"``; ``None`` → ``""``.
    """
    if not value:
        return ""
    text = str(value).strip().lower().replace("-", "_")
    return text.split("_")[0].split(".")[0]


def detect_locale() -> Language:
    """Return the inferred UI language (``fr*`` → FR, otherwise EN)."""
    try:
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            tag = _language_tag(os.environ.get(var))
            if not tag:
                continue
            return Language.FR if tag == "fr" else Language.EN

        try:
            default = locale.getdefaultlocale()
        except Exception:
            default = None

        if isinstance(default, (tuple, list)) and default:
            tag = _language_tag(default[0])
            if tag == "fr":
                return Language.FR
    except Exception:
        pass
    return Language.EN
