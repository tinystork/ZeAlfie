"""ZA-ICON-02 — hermetic tests for the Windows AppUserModelID helper.

Covers (no Windows machine required — the Win32 call is injected/mocked):

* (a) the exact stable AppUserModelID ``ZeSoftware.ZeAlfie`` is used and
  is what the injected callable receives;
* (b) on Windows (``sys.platform == "win32"`` via monkeypatch) the Win32
  setter is invoked;
* (c) on non-Windows platforms the public entry is a NO-OP (injected
  callable NOT invoked);
* (d) an ``Exception`` from the injected callable degrades safely
  (never raises, returns, warns);
* (e) a non-success HRESULT (``< 0``) degrades safely (never raises,
  returns, warns);
* (f) a success HRESULT (``0``) is accepted;
* (g) source-order regression on ``app.py``: in ``run_gui`` the
  Windows-identity helper is invoked BEFORE ``QApplication`` is
  constructed, and ``apply_app_icon(app)`` (ZA-ICON-01) still follows
  ``QApplication(...)``.

All tests are fast, Qt-free, and hermetic: the helper internals are
stubbed/monkeypatched and ``run_gui`` ordering is asserted at source
level (no real Qt objects are created).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from zealfie.gui.windows_identity import (
    APP_USER_MODEL_ID,
    _set_app_user_model_id,
    apply_windows_app_identity,
)

APP_ID = "ZeSoftware.ZeAlfie"

#: Path of the GUI composition root whose source order is asserted.
_APP_SOURCE = (
    Path(__file__).resolve().parents[1] / "src" / "zealfie" / "gui" / "app.py"
)


# ===========================================================================
# (a) Exact stable AppUserModelID, forwarded to the injected callable
# ===========================================================================


class TestAppUserModelIDConstant:
    def test_constant_is_exact_stable_identity(self):
        """APP_USER_MODEL_ID is the exact, stable, version-independent id."""
        assert APP_USER_MODEL_ID == APP_ID

    def test_constant_is_what_injected_callable_receives(self):
        """The seam passes APP_USER_MODEL_ID verbatim to the callable."""
        received = []
        _set_app_user_model_id(
            lambda app_id: received.append(app_id) or 0, APP_USER_MODEL_ID
        )
        assert received == [APP_ID]


# ===========================================================================
# (b) Windows: the Win32 setter is invoked
# ===========================================================================


class TestWindowsInvocation:
    def test_win32_platform_invokes_win32_setter(self, monkeypatch):
        """On win32, apply_windows_app_identity calls the Win32 setter."""
        calls = []

        def fake_setter(app_id: str) -> int:
            calls.append(app_id)
            return 0

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(
            "zealfie.gui.windows_identity._win32_set_app_user_model_id",
            fake_setter,
        )
        assert apply_windows_app_identity() is None
        assert calls == [APP_ID]


# ===========================================================================
# (c) Non-Windows: NO-OP, injected callable NOT invoked
# ===========================================================================


class TestNonWindowsNoop:
    def test_non_win32_platform_is_noop(self, monkeypatch):
        """On any non-win32 platform no Win32 call is made."""
        monkeypatch.setattr(sys, "platform", "linux")  # deterministic
        calls = []
        monkeypatch.setattr(
            "zealfie.gui.windows_identity._win32_set_app_user_model_id",
            lambda app_id: calls.append(app_id) or 0,
        )
        assert apply_windows_app_identity() is None
        assert calls == []


# ===========================================================================
# (d)/(e)/(f) Degradation contract of the injectable seam
# ===========================================================================


class TestErrorDegradation:
    def test_callable_exception_degrades_safely(self, caplog):
        """An exception from the callable never escapes; warns instead."""

        def boom(app_id: str) -> int:  # noqa: ARG001
            raise OSError("shell32 unavailable")

        with caplog.at_level(
            logging.WARNING, logger="zealfie.gui.windows_identity"
        ):
            assert _set_app_user_model_id(boom, APP_USER_MODEL_ID) is None
        assert any(
            "SetCurrentProcessExplicitAppUserModelID" in record.message
            for record in caplog.records
        )

    def test_failed_hresult_degrades_safely(self, caplog):
        """A FAILED HRESULT (< 0) never escapes; warns instead."""
        with caplog.at_level(
            logging.WARNING, logger="zealfie.gui.windows_identity"
        ):
            result = _set_app_user_model_id(
                lambda app_id: -2147024891, APP_USER_MODEL_ID  # noqa: ARG005
            )
        assert result is None
        assert any(
            "SetCurrentProcessExplicitAppUserModelID" in record.message
            for record in caplog.records
        )

    def test_success_hresult_is_accepted(self, caplog):
        """S_OK (0) is accepted silently: no raise, no warning."""
        calls = []
        with caplog.at_level(
            logging.WARNING, logger="zealfie.gui.windows_identity"
        ):
            result = _set_app_user_model_id(
                lambda app_id: calls.append(app_id) or 0, APP_USER_MODEL_ID
            )
        assert result is None
        assert calls == [APP_ID]
        assert caplog.records == []


# ===========================================================================
# (g) run_gui composition order (source-level, Qt-free regression)
# ===========================================================================


class TestRunGuiCompositionOrder:
    def test_windows_identity_invoked_before_qapplication(self):
        """run_gui calls the identity helper before QApplication(...)."""
        source = _APP_SOURCE.read_text(encoding="utf-8")
        identity_pos = source.index("apply_windows_app_identity()")
        qapp_pos = source.index("QApplication(sys.argv)")
        assert identity_pos < qapp_pos, (
            "apply_windows_app_identity() must run before QApplication"
        )

    def test_apply_app_icon_still_follows_qapplication(self):
        """ZA-ICON-01 behaviour is unchanged: icon apply follows QApplication."""
        source = _APP_SOURCE.read_text(encoding="utf-8")
        qapp_pos = source.index("QApplication(sys.argv)")
        icon_pos = source.index("apply_app_icon(app)")
        assert qapp_pos < icon_pos, (
            "apply_app_icon(app) must still follow QApplication(...)"
        )


# Guard against accidental test runs on an actual Windows host: the
# source-order tests must never import the composition root.
def test_app_source_file_exists():
    """The composition-root source exists (keeps the ordering honest)."""
    assert _APP_SOURCE.is_file()
