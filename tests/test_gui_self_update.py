"""Tests for ZA-M1-4.2 — GUI self-update UX (headless).

Covers the mission's shell-integration matrix:

* startup does not block on the self-update check;
* the check/stage runs off the Qt thread (thread-pool worker, never GUI);
* NOT_SUPPORTED / UP_TO_DATE / FAILED → no proposal (silent, shell alive);
* UPDATE_READY → banner proposal visible with the target version;
* "Later" → banner hidden, pending preserved, no re-proposal this session;
* apply is triggered exactly once; on success/handoff the shell closes once
  (no restart loop); on failure no false success is claimed and the shell
  stays usable;
* EN/FR translation of the new banner strings.

All tests run headless via ``QT_QPA_PLATFORM=offscreen``.  Fakes only — no
real network, no build, no pip, no install mutation.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

try:
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.app import (
    ProductShellState,
    ProductUpdateResult,
    UpdateStatus,
)
from zealfie.i18n import Language, reset_language, set_language, translate
from zealfie.runtime.model import RuntimeState
from zealfie.selfupdate import (
    ApplyStatus,
    GuiSelfUpdateResult,
    GuiSelfUpdateStatus,
    SelfUpdateApplyResult,
)

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class FakeService:
    """Minimal fake service: empty catalog + READY empty shell state."""

    def __init__(self) -> None:
        self.collect_calls = 0

    def list_products(self):
        return ()

    def collect_product_state(self) -> ProductShellState:
        self.collect_calls += 1
        return ProductShellState(
            runtime_state=RuntimeState.READY,
            runtime_root=Path("/fake/runtime"),
            products=(),
        )


@pytest.fixture(scope="session")
def qapp():
    import os

    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wait_for(qapp, predicate, timeout_ms: float = 3000.0) -> bool:
    deadline = time.monotonic() + timeout_ms / 1000.0
    while time.monotonic() < deadline:
        qapp.processEvents()
        if predicate():
            return True
        time.sleep(0.01)
    qapp.processEvents()
    return predicate()


def _ready(version="0.0.7"):
    return GuiSelfUpdateResult(GuiSelfUpdateStatus.UPDATE_READY, version=version)


def _window(
    service,
    *,
    check_fn=None,
    apply_fn=None,
    restart_fn=None,
):
    from zealfie.gui.main_window import ZeAlfieMainWindow

    return ZeAlfieMainWindow(
        service=service,  # type: ignore[arg-type]
        self_update_check_fn=check_fn,
        self_update_apply_fn=apply_fn,
        self_update_restart_fn=restart_fn,
    )


# ---------------------------------------------------------------------------
# 1. Startup non-blocking + off-GUI-thread check
# ---------------------------------------------------------------------------


class TestStartupCheck:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_no_self_update_when_not_wired(self, qapp):
        window = _window(FakeService())
        try:
            assert window._self_update_check_thread is None
            assert window._self_update_banner.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_startup_does_not_block(self, qapp):
        entered = threading.Event()
        release = threading.Event()

        def check_fn():
            entered.set()
            release.wait(timeout=5)
            return GuiSelfUpdateResult(GuiSelfUpdateStatus.UP_TO_DATE)

        window = _window(FakeService(), check_fn=check_fn)
        try:
            # Construction returned while the check is still blocked.
            assert entered.wait(timeout=5)
            assert not release.is_set()
        finally:
            release.set()
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_check_thread_is_daemon(self, qapp):
        """The check/stage thread must be daemon so GUI close never blocks exit."""
        entered = threading.Event()
        release = threading.Event()

        def check_fn():
            entered.set()
            release.wait(timeout=5)
            return GuiSelfUpdateResult(GuiSelfUpdateStatus.UP_TO_DATE)

        window = _window(FakeService(), check_fn=check_fn)
        try:
            assert entered.wait(timeout=5), "check thread must start"
            thread = window._self_update_check_thread
            assert thread is not None
            assert thread.daemon, (
                "self-update check/stage thread must be daemon so a slow "
                "network call never blocks interpreter exit"
            )
        finally:
            release.set()
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_check_runs_off_gui_thread(self, qapp):
        main_thread = threading.current_thread()
        ran_on: list[threading.Thread] = []
        done = threading.Event()

        def check_fn():
            ran_on.append(threading.current_thread())
            done.set()
            return GuiSelfUpdateResult(GuiSelfUpdateStatus.UP_TO_DATE)

        window = _window(FakeService(), check_fn=check_fn)
        try:
            assert done.wait(timeout=5)
            assert ran_on and ran_on[0] is not main_thread, (
                "self-update check must not run on the Qt GUI thread"
            )
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 2. Silent outcomes (no proposal, shell alive)
# ---------------------------------------------------------------------------


class TestSilentOutcomes:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    @pytest.mark.parametrize(
        "status",
        [
            GuiSelfUpdateStatus.NOT_SUPPORTED,
            GuiSelfUpdateStatus.UP_TO_DATE,
            GuiSelfUpdateStatus.FAILED,
        ],
    )
    def test_silent_outcomes_show_no_banner(self, qapp, status):
        result = GuiSelfUpdateResult(status)

        def check_fn():
            return result

        window = _window(FakeService(), check_fn=check_fn)
        try:
            # Give the background check time to run and deliver its (silent)
            # result via the queued bridge connection.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                qapp.processEvents()
                time.sleep(0.01)
            qapp.processEvents()
            assert window._self_update_banner.isHidden()
            assert window._self_update_ready_result is None
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 3. UPDATE_READY → proposal visible
# ---------------------------------------------------------------------------


class TestReadyProposal:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_ready_shows_banner_with_version(self, qapp):
        window = _window(FakeService(), check_fn=lambda: _ready("0.0.7"))
        try:
            ok = _wait_for(
                qapp, lambda: not window._self_update_banner.isHidden()
            )
            assert ok, "banner must become visible when UPDATE_READY"
            assert "0.0.7" in window._self_update_banner.message_text
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_later_hides_and_does_not_repropose(self, qapp):
        window = _window(FakeService(), check_fn=lambda: _ready("0.0.7"))
        try:
            ok = _wait_for(
                qapp, lambda: not window._self_update_banner.isHidden()
            )
            assert ok
            window._self_update_banner._later_button.click()

            assert window._self_update_banner.isHidden()
            assert window._self_update_dismissed is True

            # A later result would not re-propose in the same session.
            window._on_self_update_result(_ready("0.0.7"))
            assert window._self_update_banner.isHidden()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 4. Apply action (once), close on success/handoff, no false success
# ---------------------------------------------------------------------------


class TestApplyAction:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def _ready_window(self, qapp, apply_fn, restart_fn=None):
        window = _window(
            FakeService(), check_fn=lambda: _ready("0.0.7"),
            apply_fn=apply_fn, restart_fn=restart_fn,
        )
        ok = _wait_for(qapp, lambda: not window._self_update_banner.isHidden())
        assert ok
        return window

    def test_apply_triggered_exactly_once(self, qapp):
        apply_calls: list[int] = []

        def apply_fn():
            apply_calls.append(1)
            return SelfUpdateApplyResult(ApplyStatus.FAILED, "noop")

        window = self._ready_window(qapp, apply_fn)
        try:
            # Double-click the accept button; the single-shot guard must hold.
            window._self_update_banner._update_button.click()
            window._self_update_banner._update_button.click()
            _wait_for(qapp, lambda: len(apply_calls) >= 1)
            qapp.processEvents()
            time.sleep(0.05)
            assert apply_calls == [1], "apply must run exactly once"
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_handoff_success_closes_shell_once(self, qapp):
        apply_calls: list[int] = []
        restart_calls: list[int] = []

        def apply_fn():
            apply_calls.append(1)
            return SelfUpdateApplyResult(ApplyStatus.HANDOFF_STARTED, "handoff")

        def restart_fn():
            restart_calls.append(1)

        window = self._ready_window(qapp, apply_fn, restart_fn=restart_fn)
        try:
            window._self_update_banner._update_button.click()
            ok = _wait_for(qapp, lambda: window._self_update_restarting)
            assert ok, "accepted update must trigger a controlled close"
            assert apply_calls == [1]
            assert restart_calls == [1], "restart launched exactly once"
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_apply_failure_no_false_success(self, qapp):
        restart_calls: list[int] = []

        def apply_fn():
            return SelfUpdateApplyResult(ApplyStatus.FAILED, "pip failed")

        def restart_fn():
            restart_calls.append(1)

        window = self._ready_window(qapp, apply_fn, restart_fn=restart_fn)
        try:
            window._self_update_banner._update_button.click()
            ok = _wait_for(qapp, lambda: not window._self_update_applying)
            assert ok
            # No false success: no restart, shell not closed, error shown.
            assert restart_calls == []
            assert window._self_update_restarting is False
            assert not window._self_update_banner.isHidden()
            assert "could not be applied" in window._self_update_banner.message_text
            # The proposal remains actionable (retry possible).
            assert window._self_update_banner._update_button.isEnabled()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_restart_path_quits_apply_thread(self, qapp):
        """On the restart/close path the apply QThread is quit, not left running.

        Regression: without the close-path teardown, the ``thread.quit``
        handoff is a queued GUI connection that can be dropped when the event
        loop exits, leaving a still-running QThread to be destroyed at process
        exit ("QThread: Destroyed while thread is still running").
        """
        apply_calls: list[int] = []
        restart_calls: list[int] = []

        def apply_fn():
            apply_calls.append(1)
            return SelfUpdateApplyResult(ApplyStatus.APPLIED, "applied")

        def restart_fn():
            restart_calls.append(1)

        window = self._ready_window(qapp, apply_fn, restart_fn=restart_fn)
        thread = None
        try:
            window._self_update_banner._update_button.click()
            # The apply thread is created synchronously in
            # _on_self_update_accepted (button click is a direct connection).
            thread = window._self_update_apply_thread
            assert thread is not None

            ok = _wait_for(qapp, lambda: window._self_update_restarting)
            assert ok, "accepted update must trigger the restart/close path"
            assert restart_calls == [1]
            assert apply_calls == [1]

            # The close path must quit the apply thread (bounded wait), so it
            # is no longer running and can never be destroyed while running.
            assert not thread.isRunning()
        finally:
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_apply_is_not_started_during_product_transaction(self, qapp):
        apply_calls: list[int] = []

        def apply_fn():
            apply_calls.append(1)
            return SelfUpdateApplyResult(ApplyStatus.APPLIED, "applied")

        window = self._ready_window(qapp, apply_fn)
        try:
            window._install_active = True
            window._self_update_banner._update_button.click()
            qapp.processEvents()
            time.sleep(0.05)
            assert apply_calls == []
            assert window._self_update_applying is False
        finally:
            window._install_active = False
            window.close()
            window.deleteLater()
            qapp.processEvents()

    def test_apply_is_not_started_during_gpu_install(self, qapp):
        """Self-update apply must not start while a GPU runtime install runs.

        The accelerated-install worker is parented to the acceleration
        panel; a self-update restart/close would destroy it mid-run, so
        ``_on_self_update_accepted`` must refuse (mirror of the product
        transaction guard).
        """
        apply_calls: list[int] = []

        def apply_fn():
            apply_calls.append(1)
            return SelfUpdateApplyResult(ApplyStatus.APPLIED, "applied")

        window = self._ready_window(qapp, apply_fn)
        try:
            panel = window._acceleration_panel
            assert panel is not None
            panel._install_active = True
            window._self_update_banner._update_button.click()
            qapp.processEvents()
            time.sleep(0.05)
            assert apply_calls == []
            assert window._self_update_applying is False
        finally:
            window._acceleration_panel._install_active = False
            window.close()
            window.deleteLater()
            qapp.processEvents()


# ---------------------------------------------------------------------------
# 5. Translation (EN/FR)
# ---------------------------------------------------------------------------


class TestTranslation:
    def test_catalog_has_both_languages(self):
        from zealfie.i18n import EN, FR

        for key in (
            "selfupdate.ready",
            "selfupdate.update_restart",
            "selfupdate.later",
            "selfupdate.applying",
            "selfupdate.apply_failed",
        ):
            assert key in EN and EN[key], f"missing EN key {key!r}"
            assert key in FR and FR[key], f"missing FR key {key!r}"
            assert EN[key] != FR[key], f"untranslated key {key!r}"

    def test_translate_renders_per_language(self):
        try:
            set_language(Language.EN)
            assert "0.0.7" in translate("selfupdate.ready", version="0.0.7")
            assert "Update and restart" in translate("selfupdate.update_restart")
            assert translate("selfupdate.later") == "Later"

            set_language(Language.FR)
            assert "0.0.7" in translate("selfupdate.ready", version="0.0.7")
            assert "est prêt" in translate("selfupdate.ready", version="0.0.7")
            assert translate("selfupdate.update_restart") == "Mettre à jour et redémarrer"
            assert translate("selfupdate.later") == "Plus tard"
        finally:
            reset_language()

    def test_banner_renders_french(self, qapp):
        from zealfie.gui.self_update_banner import SelfUpdateBanner

        try:
            set_language(Language.FR)
            banner = SelfUpdateBanner()
            banner.show_ready("0.0.7")
            assert "est prêt" in banner.message_text
            assert banner._update_button.text() == "Mettre à jour et redémarrer"
            assert banner._later_button.text() == "Plus tard"
            banner.close()
            banner.deleteLater()
            qapp.processEvents()
        finally:
            reset_language()

    def test_banner_retranslates_on_language_change(self, qapp):
        """A runtime language switch must re-render the visible banner text."""
        try:
            set_language(Language.EN)
            window = _window(FakeService(), check_fn=lambda: _ready("0.0.7"))
            try:
                ok = _wait_for(
                    qapp, lambda: not window._self_update_banner.isHidden()
                )
                assert ok, "banner must be visible in ready state"
                assert "ready to be installed" in window._self_update_banner.message_text
                assert (
                    window._self_update_banner._update_button.text()
                    == "Update and restart"
                )
                assert window._self_update_banner._later_button.text() == "Later"

                set_language(Language.FR)
                window._retranslate()

                assert "est prêt" in window._self_update_banner.message_text
                assert (
                    window._self_update_banner._update_button.text()
                    == "Mettre à jour et redémarrer"
                )
                assert window._self_update_banner._later_button.text() == "Plus tard"
            finally:
                window.close()
                window.deleteLater()
                qapp.processEvents()
        finally:
            reset_language()


# ---------------------------------------------------------------------------
# 6. Corrective: normal-close teardown (SHOULD-1) + banner stylesheet (NIT-2)
# ---------------------------------------------------------------------------


class FakeApplyThread:
    """Fake QThread-like object recording quit()/wait() for teardown checks."""

    def __init__(self, running: bool = True) -> None:
        self._running = running
        self.quit_called = False
        self.wait_calls: list[int] = []

    def isRunning(self) -> bool:
        return self._running

    def quit(self) -> None:
        self.quit_called = True
        self._running = False

    def wait(self, timeout_ms: int) -> bool:
        self.wait_calls.append(timeout_ms)
        return True


class TestNormalCloseTeardown:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_normal_close_quits_and_waits_apply_thread(self, qapp):
        """Honest-failure close must quit+wait a still-running apply thread.

        After an apply fails, the error is shown and ``_self_update_applying``
        is cleared, so a later window close takes the *normal* close path (not
        the restart path).  That path must still tear down the apply QThread,
        otherwise the thread is destroyed while still running.
        """
        window = _window(FakeService())
        fake = FakeApplyThread(running=True)
        window._self_update_apply_thread = fake
        assert window._self_update_restarting is False
        assert window._self_update_applying is False
        try:
            window.close()
            qapp.processEvents()
            assert fake.quit_called, (
                "normal close must quit a still-running apply thread"
            )
            assert fake.wait_calls == [5000], (
                "normal close must bounded-wait (5000 ms) the apply thread"
            )
            assert not fake.isRunning()
        finally:
            window.deleteLater()
            qapp.processEvents()

    def test_teardown_is_idempotent_and_none_safe(self, qapp):
        window = _window(FakeService())
        try:
            # No thread wired → no-op, must not raise.
            window._teardown_self_update_apply_thread()

            fake = FakeApplyThread(running=True)
            window._self_update_apply_thread = fake
            window._teardown_self_update_apply_thread()
            assert fake.quit_called and fake.wait_calls == [5000]

            # Second call is a no-op (thread already stopped): idempotent.
            fake.quit_called = False
            window._teardown_self_update_apply_thread()
            assert not fake.quit_called
        finally:
            window.deleteLater()
            qapp.processEvents()


class TestBannerStylesheet:
    @pytest.fixture(autouse=True)
    def _qapp(self, qapp):
        return qapp

    def test_busy_purges_error_stylesheet(self, qapp):
        from zealfie.gui.self_update_banner import SelfUpdateBanner

        banner = SelfUpdateBanner()
        try:
            banner.show_error()
            assert "c0392b" in banner._message_label.styleSheet()

            # Retry after a failure → busy must clear the stale red style.
            banner.set_busy(True)
            assert banner._message_label.styleSheet() == ""
            assert banner._message_label.text() == translate("selfupdate.applying")
        finally:
            banner.close()
            banner.deleteLater()
            qapp.processEvents()

    def test_ready_after_error_clears_style(self, qapp):
        from zealfie.gui.self_update_banner import SelfUpdateBanner

        banner = SelfUpdateBanner()
        try:
            banner.show_error()
            assert "c0392b" in banner._message_label.styleSheet()
            banner.show_ready("0.0.7")
            assert banner._message_label.styleSheet() == ""
        finally:
            banner.close()
            banner.deleteLater()
            qapp.processEvents()
