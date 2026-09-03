"""ZA-ICON-02 — Windows taskbar/grouping identity (AppUserModelID).

On Windows the taskbar groups and labels a process by its
``AppUserModelID``.  A plain Python/Qt process has none, so Windows
shows a generic launcher icon and a generic group entry.  This module
is the tiny, best-effort shell-identity helper: it asks the Windows
shell to pin the current process to the stable ZeAlfie identity
``ZeSoftware.ZeAlfie`` by calling
``SetCurrentProcessExplicitAppUserModelID``.

Scope and relationship to ZA-ICON-01
------------------------------------
* ZA-ICON-01 (``zealfie.gui.icon``) is the *Qt-level* identity: the
  title-bar/window/dialog icon.  It is unchanged by this module.
* ZA-ICON-02 is the *shell-level* process identity (taskbar/alt-tab
  grouping, notifications, …).  The call MUST happen before
  ``QApplication`` exists — Windows reads the AppUserModelID when the
  process/shell surfaces are first registered, so the composition root
  invokes :func:`apply_windows_app_identity` first (see ``app.py``).

Graceful degradation
--------------------
Everything here is best-effort and NEVER raises:

* on non-Windows platforms :func:`apply_windows_app_identity` is a
  NO-OP (the identity is a Windows-only shell concept);
* on Windows, both a failure of the underlying call and a FAILED
  HRESULT (``< 0``) degrade to a ``logger.warning`` and a plain
  return — a missing identity must never block GUI startup.

The AppUserModelID is a single named constant
:data:`APP_USER_MODEL_ID`; it is STABLE and version-independent (no
version component) so Windows keeps grouping ZeAlfie releases
together across updates.
"""

from __future__ import annotations

import ctypes
import logging
import sys

logger = logging.getLogger(__name__)

#: Stable, version-independent Windows AppUserModelID for ZeAlfie.
#: Deliberately version-independent so Windows groups every ZeAlfie
#: release (and its updates) under one taskbar/alt-tab identity.
APP_USER_MODEL_ID = "ZeSoftware.ZeAlfie"


def apply_windows_app_identity() -> None:
    """Apply the ZeAlfie AppUserModelID to the current Windows process.

    Best-effort shell identity (ZA-ICON-02): on non-Windows platforms
    this is a NO-OP and returns immediately; on Windows it delegates to
    the injectable :func:`_set_app_user_model_id` seam with the real
    Win32 setter.  Never raises and never crashes the GUI: any failure
    degrades to a warning.
    """
    if sys.platform != "win32":
        return
    _set_app_user_model_id(_win32_set_app_user_model_id, APP_USER_MODEL_ID)


def _set_app_user_model_id(callable, app_id: str) -> None:
    """Set the AppUserModelID through an injectable ``callable(app_id)``.

    The injected callable performs the actual Win32 invocation and
    returns its HRESULT.  Degradation contract (never raises):

    * an ``Exception`` escaping the invocation -> ``logger.warning``,
      return ``None``;
    * a non-success HRESULT (``< 0``, i.e. FAILED) -> ``logger.warning``,
      return ``None``;
    * a success HRESULT (``0``, S_OK) -> accepted, return ``None``.

    The seam keeps the failure paths hermetic-testable without a
    Windows machine (the injected callable is mocked/stubbed).
    """
    try:
        hresult = callable(app_id)
    except Exception:
        logger.warning(
            "SetCurrentProcessExplicitAppUserModelID(%r) raised (ignored)",
            app_id,
            exc_info=True,
        )
        return
    if hresult < 0:
        logger.warning(
            "SetCurrentProcessExplicitAppUserModelID(%r) failed "
            "(HRESULT 0x%08X, ignored)",
            app_id,
            hresult & 0xFFFFFFFF,
        )
        return


def _win32_set_app_user_model_id(app_id: str) -> int:
    """Invoke ``shell32.SetCurrentProcessExplicitAppUserModelID``.

    Windows-only: reads ``ctypes.windll`` lazily (the attribute only
    exists on Windows) inside the call, never at import time.  Returns
    the HRESULT as a signed ``int`` (``ctypes.c_long`` restype keeps the
    FAILED/negative sign intact).
    """
    setter = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    setter.restype = ctypes.c_long
    setter.argtypes = [ctypes.c_wchar_p]
    return int(setter(app_id))
