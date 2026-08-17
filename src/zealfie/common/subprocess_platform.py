"""Platform-specific subprocess kwargs for ZeAlfie-owned technical helpers.

ZeAlfie launches technical helper subprocesses (host probes, pip, builds,
backend compute probes) while installing and inspecting products.  On real
Windows these helpers briefly flash a foreground console window — a visible
UX defect (W-UX-01).  This module provides the single tiny helper that every
technical call site uses to suppress that window.

Product application launches (:mod:`zealfie.launching.executor`) MUST NOT
use this helper: GUI applications started for the user must open normally.
"""

from __future__ import annotations

import subprocess
import sys


def technical_subprocess_platform_kwargs() -> dict[str, int]:
    """Platform kwargs for ZeAlfie-owned technical helper subprocesses.

    Returns ``{"creationflags": subprocess.CREATE_NO_WINDOW}`` on Windows
    (``sys.platform == "win32"``) so technical helpers (probes, pip, builds)
    never flash a foreground console window; returns ``{}`` on every other
    platform. NEVER use this for product application launches (GUI apps
    started via zealfie.launching.executor) — those must open normally.
    Known limitation: venv creation (stdlib venv/ensurepip) spawns its
    own internal subprocess without creationflags; not addressed here.
    """
    if sys.platform == "win32":
        return {"creationflags": subprocess.CREATE_NO_WINDOW}
    return {}
