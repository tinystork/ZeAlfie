"""Persistent language-preference store (mirrors Selection/Policy store pattern).

Stores the user's UI language choice as a small JSON file in the user config
directory — the same ``zealfie`` config location used by
:class:`~zealfie.products.policy.ProductPolicyStore`, not a new location.

Reads are lenient: a missing, corrupt, or unknown value yields ``None`` (so
first-run locale inference still works).  Writes are atomic (temp sibling +
fsync + ``os.replace``).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from . import Language
from .locale import detect_locale


def default_language_path() -> Path:
    """Platform-appropriate default path for the language preference file.

    * Linux:   ``$XDG_CONFIG_HOME/zealfie/language.json``
      (default ``~/.config/zealfie/language.json``)
    * macOS:   ``~/Library/Application Support/zealfie/language.json``
    * Windows: ``%APPDATA%/zealfie/language.json``
    """
    if sys.platform == "win32":
        base = os.environ.get(
            "APPDATA",
            str(Path.home() / "AppData" / "Roaming"),
        )
        return Path(base) / "zealfie" / "language.json"
    elif sys.platform == "darwin":
        return (
            Path.home()
            / "Library"
            / "Application Support"
            / "zealfie"
            / "language.json"
        )
    else:
        base = os.environ.get(
            "XDG_CONFIG_HOME",
            str(Path.home() / ".config"),
        )
        return Path(base) / "zealfie" / "language.json"


class LanguageStore:
    """Persistent store for the user's UI language preference.

    Thread-unsafe by design — single-owner at the composition root, mirroring
    :class:`~zealfie.products.selection.SelectionStore`.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path: Path = (
            Path(path) if path is not None else default_language_path()
        )

    @property
    def path(self) -> Path:
        """The filesystem path of the persisted language file."""
        return self._path

    def load(self) -> Language | None:
        """Load the persisted preference (``None`` when missing/invalid)."""
        try:
            if not self._path.exists():
                return None
            payload = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return None
            value = payload.get("language")
            if value in ("en", "fr"):
                return Language(value)
            return None
        except Exception:
            return None

    def save(self, lang: Language) -> None:
        """Persist *lang* atomically."""
        payload = {"language": str(Language(lang))}
        _atomic_write_json(payload, self._path)

    def effective_language(self) -> Language:
        """Return the persisted preference if present, else the detected locale."""
        persisted = self.load()
        if persisted is not None:
            return persisted
        return detect_locale()


def effective_language() -> Language:
    """Convenience wrapper: persisted preference, else detected locale."""
    return LanguageStore().effective_language()


def _atomic_write_json(payload: dict, path: Path) -> None:
    """Persist *payload* as JSON atomically (temp sibling + fsync + replace)."""
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent),
        prefix=".language-",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            Path(tmp).unlink(missing_ok=True)
        except OSError:
            pass
        raise
