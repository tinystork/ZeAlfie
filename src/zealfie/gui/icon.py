"""ZA-ICON-01 — canonical ZeAlfie application-icon resolver.

Single place that turns the packaged ZeAlfie icon assets into a Qt
``QIcon`` for the running application, its main window, and any other
top-level window/dialog (all of which inherit the QApplication default
icon unless they override it).

Resource model
--------------
Icon assets ship inside the ``zealfie.icon`` subpackage (see
``pyproject.toml`` ``[tool.setuptools.package-data]``): the PNG raster
set used at runtime plus ``zealfie.ico`` / ``zealfie.icns`` kept in the
package for future Windows EXE / macOS bundle stages.  Resolution goes
through ``importlib.resources`` (the repository's canonical resource
pattern — see ``zealfie.products.catalog``), never through absolute
paths or ``__file__``-relative hacks, so it works from a source
checkout AND from an installed wheel.

Graceful degradation
--------------------
Every function here is best-effort: when the packaged resource is
missing or unreadable they return ``None`` / no-op and NEVER raise, so
a missing visual identity can never make ZeAlfie unusable.

Future packaging notes (documented only — NOT implemented here)
---------------------------------------------------------------
* Windows taskbar/EXE identity is a packager concern, not a Qt one:
  the future Windows packager must embed ``zealfie.ico`` into the
  produced EXE and call ``SetCurrentProcessExplicitAppUserModelID``
  (AppUserModelID) early in the packaged launcher so Windows groups
  the taskbar/alt-tab entry under the ZeAlfie identity.  The Qt layer
  in this module cannot set the EXE icon or a real AppUserModelID.
* macOS Dock/.app identity is likewise a packager concern: the future
  macOS bundler must place ``zealfie.icns`` at
  ``Contents/Resources/zealfie.icns`` and reference it from
  ``Info.plist`` (``CFBundleIconFile``).  While running from a source
  checkout or an unbundled wheel, the Dock icon is the QApplication /
  window icon supplied by this module (layer A).
"""

from __future__ import annotations

import importlib.resources
import logging

logger = logging.getLogger(__name__)

#: Subpackage that carries the icon assets (regular package, see
#: ``src/zealfie/icon/__init__.py``).  Kept in sync with the package-data
#: globs in ``pyproject.toml``.
ICON_PACKAGE = "zealfie.icon"

#: Raster used for the Qt runtime window/application icon.
ICON_RESOURCE = "zealfie_256.png"


def _icon_resource_file() -> str | None:
    """Return the packaged icon file path, or ``None`` when unavailable.

    Never raises: any resolution failure (package absent, asset missing,
    unreadable) degrades to ``None``.
    """
    try:
        resource = importlib.resources.files(ICON_PACKAGE).joinpath(ICON_RESOURCE)
        if not resource.is_file():
            logger.warning(
                "ZeAlfie icon resource %r is missing from %r (ignored)",
                ICON_RESOURCE,
                ICON_PACKAGE,
            )
            return None
        return str(resource)
    except Exception as exc:
        logger.debug("ZeAlfie icon resource unavailable: %s", exc)
        return None


def load_app_icon() -> QIcon | None:
    """Return the ZeAlfie app icon as a Qt ``QIcon``, or ``None``.

    Best-effort and side-effect free: a missing package, a missing asset,
    an undecodable file, or an absent PySide6 all degrade to ``None``
    (the platform default icon then stays in place).  Never raises.
    """
    try:
        from PySide6.QtGui import QIcon
    except Exception:
        return None
    path = _icon_resource_file()
    if path is None:
        return None
    try:
        icon = QIcon(path)
    except Exception:
        logger.warning("ZeAlfie icon could not be built (ignored)", exc_info=True)
        return None
    if icon.isNull():
        logger.warning("ZeAlfie icon %r is not decodable (ignored)", path)
        return None
    return icon


def apply_app_icon(app) -> None:
    """Set the ZeAlfie icon as the QApplication default window icon.

    Qt propagates this default to every top-level window/dialog that does
    not set its own icon (window decorations, taskbar/alt-tab entries,
    dialogs, …).  Best-effort: silent no-op when the asset is missing.
    Never raises.
    """
    icon = load_app_icon()
    if icon is None:
        return
    try:
        app.setWindowIcon(icon)
    except Exception:
        logger.warning("ZeAlfie app icon could not be applied (ignored)",
                       exc_info=True)


def apply_window_icon(window) -> None:
    """Explicitly set the ZeAlfie icon on a top-level window.

    Belt-and-braces on top of :func:`apply_app_icon`: windows normally
    inherit the QApplication default, but the explicit set keeps the
    identity stable even when a window is created without the standard
    composition root (tests, embedding, tooling).  Best-effort: silent
    no-op when the asset is missing.  Never raises.
    """
    icon = load_app_icon()
    if icon is None:
        return
    try:
        window.setWindowIcon(icon)
    except Exception:
        logger.warning("ZeAlfie window icon could not be applied (ignored)",
                       exc_info=True)
