"""Shared primitives used by multiple ZeAlfie subsystems."""

from __future__ import annotations

import re as _re

_NORMALISE_RE = _re.compile(r"[-_.]+")


def normalise_distribution_name(name: str) -> str:
    """Normalise a Python distribution name (PyPA spec).

    Lowercase and collapse every run of ``-``, ``_``, ``.`` into a single ``-``.
    """
    return _NORMALISE_RE.sub("-", name.strip()).lower()
