"""GUI entry point for ZeAlfie (M1-2C).

Provides the ``zealfie-gui`` console script entry point.
"""

from __future__ import annotations


def main() -> None:
    """Entry point for ``zealfie-gui``."""
    from .app import run_gui

    run_gui()
