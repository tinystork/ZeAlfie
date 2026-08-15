"""Tests for M1-2I (I3) — non-blocking GUI wiring for the accelerated install.

Covers the pure view reducer (labels, honest percents, done-only-on-
COMPLETED), the QThread worker (progress forwarding, cooperative
cancellation, off-GUI-thread execution), and the panel state machine
(preview -> [Installer] -> progression -> result, with the fail-closed
default: no Installer button unless the previewed plan is PLAN_READY).

Hermetic: fake services only, offscreen Qt, no GPU, no network, no real
install, no real wheels.
"""

from __future__ import annotations

import os
import threading
import time

import pytest

try:
    from PySide6.QtTest import QSignalSpy
    from PySide6.QtWidgets import (
        QApplication,
        QScrollArea,
        QSizePolicy,
        QVBoxLayout,
        QWidget,
    )
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.acceleration import (
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentPlan,
    AcceleratedDeploymentResult,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    CooperativeCancellationError,
    HardwareCompatibility,
    HardwareCompatibilityStatus,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    VariantStatus,
)
from zealfie.app import InstallPhase, InstallProgress, PHASE_PERCENT
from zealfie.gui.presentation import (
    accelerated_install_view,
    accelerated_phase_label,
)
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")

SHA_A = "a" * 40
WHEEL_A = "f" * 64


# ===========================================================================
# Synthetic plans — never real hardware
# ===========================================================================


def _hardware(status: HardwareCompatibilityStatus, reason: str) -> HardwareCompatibility:
    reason_codes = {
        HardwareCompatibilityStatus.SUPPORTED: "COMPATIBLE",
        HardwareCompatibilityStatus.BLOCKED: "ACCELERATION_BLOCKED",
        HardwareCompatibilityStatus.UNKNOWN: "HOST_CAPABILITIES_PARTIAL",
    }
    return HardwareCompatibility(
        status=status,
        reason_code=reason_codes[status],
        reason=reason,
        products_concerned=(),
    )


def _make_plan(status: AcceleratedPlanStatus, **overrides) -> AcceleratedDeploymentPlan:
    defaults: dict = dict(
        status=status,
        hardware=_hardware(
            HardwareCompatibilityStatus.BLOCKED, "nothing to evaluate"
        ),
        backend=None,
        products_concerned=(),
        keep_products=(),
        added_requirements=(),
        source_runtime_state="READY",
        source_active_slot_id=None,
        source_previous_slot_id=None,
        target_runtime="no new runtime required",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )
    defaults.update(overrides)
    return AcceleratedDeploymentPlan(**defaults)


def _ready_plan() -> AcceleratedDeploymentPlan:
    """A real PLAN_READY plan (synthetic variant, never a real framework)."""
    return _make_plan(
        AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(
            HardwareCompatibilityStatus.SUPPORTED, "host acceleration is compatible"
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
        keep_products=(
            PlannedKeepProduct(
                product_id="zebench",
                version="2.0.0",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            ),
        ),
        added_requirements=(
            PlannedAcceleratedDependency(
                distribution="accelerated-lib",
                specifier=">=1.0",
                extras=(),
                declaring_products=("zebench",),
                variant=AcceleratedVariant(
                    distribution="accelerated-lib",
                    version="1.2.0",
                    backend="NVIDIA_CUDA",
                    platform="linux_x86_64",
                ),
                variant_status=VariantStatus.SELECTED,
            ),
        ),
        target_runtime="new shared runtime slot with accelerated NVIDIA_CUDA closure",
        closure_impact=("Add accelerated-lib (>=1.0) [variant 1.2.0]",),
    )


def _blocked_plan() -> AcceleratedDeploymentPlan:
    return _make_plan(
        AcceleratedPlanStatus.BLOCKED,
        blocked=True,
        blocked_reason="no variant catalog entry",
    )


def _rec(status=RecommendationStatus.OFFER_SETUP) -> AccelerationRecommendation:
    gpu = GpuInfo(
        vendor="NVIDIA",
        model="GeForce RTX 4090",
        kind=GpuKind.DISCRETE,
        hardware_present=True,
        driver_status=CapabilityStatus.AVAILABLE,
        driver_version="560.35.03",
        driver_reason_code=None,
        driver_reason=None,
        nvidia_smi_available=True,
        cuda_driver_present=True,
    )
    return AccelerationRecommendation(
        status=status,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="offer",
        gpus=(gpu,),
    )


# ===========================================================================
# 1) Pure reducer — accelerated_phase_label / accelerated_install_view
# ===========================================================================


def test_phase_label_mapping_all_phases():
    expected = {
        AcceleratedDeploymentPhase.PREPARE: "Preparation",
        AcceleratedDeploymentPhase.ACQUIRE: "Download",
        AcceleratedDeploymentPhase.RESOLVE: "Dependency resolution",
        AcceleratedDeploymentPhase.BUILD: "Runtime build",
        AcceleratedDeploymentPhase.VALIDATE: "Validation",
        AcceleratedDeploymentPhase.GATE: "Validation",
        AcceleratedDeploymentPhase.PERSIST: "Activation",
        AcceleratedDeploymentPhase.ACTIVATE: "Activation",
        AcceleratedDeploymentPhase.COMPLETED: "Completed",
    }
    for phase, label in expected.items():
        assert accelerated_phase_label(phase) == label
        # Also accept raw value strings (defensive input).
        assert accelerated_phase_label(phase.value) == label


def test_phase_label_unknown_fallback_never_leaks_raw():
    assert accelerated_phase_label("SOME_FUTURE_PHASE") == "In progress"


def test_view_empty_sequence():
    assert accelerated_install_view(()) == ("Preparation", None, False)


def test_view_progress_sequence_honest_labels_and_percents():
    events = (
        InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
        InstallProgress(InstallPhase.ACQUIRING_DEPENDENCIES, 30, "Acquiring\u2026"),
        InstallProgress(InstallPhase.PLANNING_RUNTIME, 45, "Planning\u2026"),
        InstallProgress(InstallPhase.INSTALLING_RUNTIME, 60, "Installing\u2026"),
        InstallProgress(InstallPhase.VALIDATING, 90, "Validating\u2026"),
        InstallProgress(InstallPhase.ACTIVATING, 95, "Activating\u2026"),
    )
    for i in range(len(events)):
        label, percent, done = accelerated_install_view(events[: i + 1])
        expected = (
            ("Preparation", 0),
            ("Download", 30),
            ("Dependency resolution", 45),
            ("Runtime build", 60),
            ("Validation", 90),
            ("Activation", 95),
        )[i]
        assert (label, percent) == expected
        assert done is False
    final = accelerated_install_view(
        events + (InstallProgress(InstallPhase.COMPLETED, 100, "Complete."),)
    )
    assert final == ("Completed", 100, True)


def test_view_never_100_before_completed():
    view = accelerated_install_view(
        (InstallProgress(InstallPhase.ACTIVATING, 95, "Activating\u2026"),)
    )
    assert view == ("Activation", 95, False)
    assert view[1] != 100


def test_view_failure_result_clears_percent():
    events = (
        InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
        InstallProgress(InstallPhase.ACQUIRING_DEPENDENCIES, 30, "Acquiring\u2026"),
        AcceleratedDeploymentResult(
            success=False,
            cancelled=False,
            phase=AcceleratedDeploymentPhase.ACQUIRE,
            reason="accelerated artifact acquisition failed: no source",
        ),
    )
    assert accelerated_install_view(events) == ("Download", None, False)


def test_view_cancelled_result_clears_percent():
    view = accelerated_install_view(
        (
            InstallProgress(InstallPhase.INSTALLING_RUNTIME, 60, "Installing\u2026"),
            AcceleratedDeploymentResult(
                success=False,
                cancelled=True,
                phase=AcceleratedDeploymentPhase.BUILD,
                reason="accelerated deployment cancelled",
            ),
        )
    )
    assert view == ("Runtime build", None, False)


def test_view_success_result_done_and_100():
    view = accelerated_install_view(
        (
            AcceleratedDeploymentResult(
                success=True,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.COMPLETED,
                active_slot_id="slot-new",
            ),
        )
    )
    assert view == ("Completed", 100, True)


def test_view_raw_phase_completed_done_without_fabricated_percent():
    view = accelerated_install_view(
        (AcceleratedDeploymentPhase.COMPLETED,)
    )
    assert view == ("Completed", None, True)
    assert view[1] != 100  # no fake percent without a progress event


def test_view_ignores_unknown_events():
    view = accelerated_install_view(
        (
            "garbage",
            42,
            None,
            InstallProgress(InstallPhase.VALIDATING, 90, "Validating\u2026"),
            object(),
        )
    )
    assert view == ("Validation", 90, False)


def test_view_uses_canonical_phase_percent_not_event_percent():
    """The reducer trusts the canonical PHASE_PERCENT table, never an
    arbitrary (possibly invented) event percent."""
    rogue = InstallProgress(InstallPhase.VALIDATING, 77, "Validating\u2026")
    label, percent, done = accelerated_install_view((rogue,))
    assert percent == PHASE_PERCENT[InstallPhase.VALIDATING] == 90
    assert label == "Validation"
    assert done is False


def test_view_result_after_completed_progress_stays_done():
    events = (
        InstallProgress(InstallPhase.COMPLETED, 100, "Complete."),
        AcceleratedDeploymentResult(
            success=True,
            cancelled=False,
            phase=AcceleratedDeploymentPhase.COMPLETED,
            active_slot_id="slot-new",
        ),
    )
    assert accelerated_install_view(events) == ("Completed", 100, True)


# ===========================================================================
# Shared QApplication fixture
# ===========================================================================


@pytest.fixture(scope="session")
def qapp():
    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    yield app


def _wait_for(qapp, condition, timeout_ms: int = 5000, interval_ms: int = 20) -> bool:
    from PySide6.QtCore import QThread

    elapsed = 0
    while not condition() and elapsed < timeout_ms:
        qapp.processEvents()
        QThread.msleep(interval_ms)
        elapsed += interval_ms
    return condition()


# ===========================================================================
# 2) Worker — forwarding, cancellation, thread affinity
# ===========================================================================


class _FakeInstallService:
    """Fake ``install_accelerated_runtime`` with controllable behaviour."""

    def __init__(
        self,
        *,
        result=None,
        emissions=(),
        block_event: threading.Event | None = None,
        raise_on_call=None,
        has_method: bool = True,
    ) -> None:
        self._result = result
        self._emissions = emissions
        self._block_event = block_event
        self._raise_on_call = raise_on_call
        self._has_method = has_method
        self.calls: list[dict] = []
        self.called_from_thread_ident: int | None = None

    def install_accelerated_runtime(
        self,
        *,
        plan=None,
        recommendation=None,
        capabilities=None,
        cancel_check=None,
        progress_callback=None,
        **kwargs,
    ):
        self.calls.append(
            {
                "plan": plan,
                "recommendation": recommendation,
                "capabilities": capabilities,
                "cancel_check": cancel_check,
                "progress_callback": progress_callback,
                "fetcher": kwargs.get("fetcher"),
                "work_root": kwargs.get("work_root"),
            }
        )
        self.called_from_thread_ident = threading.get_ident()
        if self._raise_on_call is not None:
            raise self._raise_on_call
        if not self._has_method:
            return
        for progress in self._emissions:
            progress_callback(progress)
        if self._block_event is not None:
            self._block_event.wait(timeout=10)
        if cancel_check is not None:
            # Mirror the real service contract: cooperative cancellation
            # is observed as a result, never leaked as an exception.
            try:
                cancel_check()
            except CooperativeCancellationError as exc:
                return AcceleratedDeploymentResult(
                    success=False,
                    cancelled=True,
                    phase=AcceleratedDeploymentPhase.ACQUIRE,
                    reason=str(exc) or "accelerated deployment cancelled",
                )
        return self._result


def _success_result() -> AcceleratedDeploymentResult:
    return AcceleratedDeploymentResult(
        success=True,
        cancelled=False,
        phase=AcceleratedDeploymentPhase.COMPLETED,
        active_slot_id="slot-new",
        previous_slot_id="slot-old",
    )


class TestAcceleratedInstallWorker:
    def test_worker_forwards_reduced_progress_pairs(self, qapp):
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        service = _FakeInstallService(
            result=_success_result(),
            emissions=(
                InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
                InstallProgress(InstallPhase.ACQUIRING_DEPENDENCIES, 30, "Acquiring\u2026"),
                InstallProgress(InstallPhase.INSTALLING_RUNTIME, 60, "Installing\u2026"),
            ),
        )
        worker = AcceleratedInstallWorker(service)
        got: list[tuple] = []
        finished: list[object] = []
        worker.progress.connect(lambda label, percent: got.append((label, percent)))
        worker.finished.connect(finished.append)

        worker.run()

        # Backend observations became honest reduced pairs (canonical
        # PHASE_PERCENT values, never the raw event payloads).
        assert got == [
            ("Preparation", 0),
            ("Download", 30),
            ("Runtime build", 60),
            ("Completed", 100),  # final view, only after the real result
        ]
        assert len(finished) == 1
        assert finished[0] is service._result
        assert finished[0].success is True

    def test_worker_transmits_fetcher_and_work_root(self, qapp, tmp_path):
        """ZA-M1-2J.1: the worker forwards the composition root's fetcher
        and work root to ``install_accelerated_runtime`` so the KEEP base
        runtime is re-acquired at the exact provenance SHA."""
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        class _FakeFetcher:
            pass

        fetcher = _FakeFetcher()
        work_root = tmp_path / "worker-work"
        service = _FakeInstallService(result=_success_result())
        worker = AcceleratedInstallWorker(
            service,
            plan=_ready_plan(),
            fetcher=fetcher,
            work_root=work_root,
        )
        worker.finished.connect(lambda r: None)
        worker.run()

        call = service.calls[0]
        assert call["fetcher"] is fetcher
        assert call["work_root"] == work_root

    def test_worker_defaults_keep_fail_closed_no_fetcher(self, qapp):
        """Without explicit transports the worker still passes fetcher/
        work_root as None — the service's fail-closed contract decides."""
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        service = _FakeInstallService(result=_success_result())
        worker = AcceleratedInstallWorker(service, plan=_ready_plan())
        worker.finished.connect(lambda r: None)
        worker.run()

        call = service.calls[0]
        assert call["fetcher"] is None
        assert call["work_root"] is None

    def test_worker_passes_plan_and_recommendation_to_service(self, qapp):
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        plan = _ready_plan()
        service = _FakeInstallService(result=_success_result())
        worker = AcceleratedInstallWorker(
            service, plan=plan, recommendation=_rec()
        )
        worker.finished.connect(lambda r: None)
        worker.run()

        call = service.calls[0]
        assert call["plan"] is plan
        assert call["cancel_check"] is not None
        assert call["progress_callback"] is not None

    def test_worker_failure_result_clears_percent(self, qapp):
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        service = _FakeInstallService(
            result=AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.ACQUIRE,
                reason="accelerated artifact acquisition failed: no source",
            ),
            emissions=(
                InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
                InstallProgress(InstallPhase.ACQUIRING_DEPENDENCIES, 30, "Acquiring\u2026"),
            ),
        )
        worker = AcceleratedInstallWorker(service)
        got: list[tuple] = []
        worker.progress.connect(lambda label, percent: got.append((label, percent)))
        worker.run()

        # Last pair reflects the failure: stop-phase label, no fake percent.
        assert got[-1] == ("Download", None)
        assert got[-1][1] is None

    def test_worker_cancelled_result_forwarded(self, qapp):
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        cancelled = AcceleratedDeploymentResult(
            success=False,
            cancelled=True,
            phase=AcceleratedDeploymentPhase.ACQUIRE,
            reason="accelerated deployment cancelled by user",
        )
        service = _FakeInstallService(result=cancelled)
        worker = AcceleratedInstallWorker(service)
        finished: list[object] = []
        worker.finished.connect(finished.append)
        worker.run()
        assert len(finished) == 1
        assert finished[0] is cancelled
        assert finished[0].cancelled is True

    def test_worker_degrades_without_service_method(self, qapp):
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        service = _FakeInstallService()
        # Older service shape: no install method at all (getattr guard).
        service.install_accelerated_runtime = None
        worker = AcceleratedInstallWorker(service)
        finished: list[object] = []
        worker.finished.connect(finished.append)
        worker.run()
        assert len(finished) == 1
        assert finished[0].success is False
        assert "not available" in finished[0].reason

    def test_worker_survives_service_exception(self, qapp):
        from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

        service = _FakeInstallService(raise_on_call=RuntimeError("boom"))
        worker = AcceleratedInstallWorker(service)
        finished: list[object] = []
        worker.finished.connect(finished.append)
        worker.run()
        assert len(finished) == 1
        assert finished[0].success is False
        assert "boom" in finished[0].reason

    # -- Threaded: off-GUI-thread execution + cooperative cancellation ----

    def test_thread_factory_propagates_fetcher_and_work_root(
        self, qapp, tmp_path
    ):
        """create_accelerated_install_thread threads the composition-root
        transports into the worker without starting a real thread."""
        from zealfie.gui.accelerated_install_worker import (
            create_accelerated_install_thread,
        )

        class _FakeFetcher:
            pass

        fetcher = _FakeFetcher()
        work_root = tmp_path / "factory-work"
        service = _FakeInstallService(result=_success_result())
        thread, worker = create_accelerated_install_thread(
            service, fetcher=fetcher, work_root=work_root
        )
        assert worker._fetcher is fetcher
        assert worker._work_root == work_root
        # The thread was never started; release it without terminate().
        thread.deleteLater()
        qapp.processEvents()

    def test_worker_runs_off_gui_thread_and_cancel_is_cooperative(self, qapp):
        """While the fake service blocks inside the worker QThread, the GUI
        thread stays responsive, and cancel() from the GUI thread makes the
        service observe the CooperativeCancellationError path."""
        from zealfie.gui.accelerated_install_worker import (
            create_accelerated_install_thread,
        )

        gui_ident = threading.get_ident()
        block_event = threading.Event()
        service = _FakeInstallService(
            result=_success_result(), block_event=block_event
        )
        thread, worker = create_accelerated_install_thread(service)

        progress_spy = QSignalSpy(worker.progress)
        finished_spy = QSignalSpy(worker.finished)

        thread.start()
        try:
            # Wait for the service call to be inside the worker thread.
            ok = _wait_for(qapp, lambda: service.called_from_thread_ident is not None)
            assert ok, "service call never started"

            # The service ran on the worker thread, NOT the GUI thread.
            assert service.called_from_thread_ident != gui_ident

            # Cancel while the service is blocked — the GUI thread is not.
            worker.cancel()

            # Release the service; its next cancel_check must raise.
            block_event.set()

            ok = _wait_for(qapp, lambda: finished_spy.count() == 1)
            assert ok, "worker never finished"

            result = finished_spy.at(0)[0]
            assert result.cancelled is True, (
                "service must observe the CooperativeCancellationError path"
            )
            assert result.old_runtime_preserved is True

            # The final progress view after cancellation: the label of the
            # phase where the deployment stopped, with no fake percent.
            pairs = [
                (progress_spy.at(i)[0], progress_spy.at(i)[1])
                for i in range(progress_spy.count())
            ]
            assert pairs == [("Download", None)]
        finally:
            worker.cancel()  # ensure the fake never deadlocks
            block_event.set()
            thread.wait(5000)
            thread.deleteLater()
            qapp.processEvents()

    def test_threaded_success_emits_completed_100_after_real_result(self, qapp):
        from zealfie.gui.accelerated_install_worker import (
            create_accelerated_install_thread,
        )

        service = _FakeInstallService(
            result=_success_result(),
            emissions=(
                InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
                InstallProgress(InstallPhase.ACTIVATING, 95, "Activating\u2026"),
            ),
        )
        thread, worker = create_accelerated_install_thread(service)
        progress_spy = QSignalSpy(worker.progress)
        finished_spy = QSignalSpy(worker.finished)
        thread.start()
        try:
            ok = _wait_for(qapp, lambda: finished_spy.count() == 1)
            assert ok, "worker never finished"
            pairs = [
                (progress_spy.at(i)[0], progress_spy.at(i)[1])
                for i in range(progress_spy.count())
            ]
            assert pairs == [
                ("Preparation", 0),
                ("Activation", 95),
                ("Completed", 100),
            ]
            # 100 arrives only in the pair that follows the real result.
            assert pairs[-1][1] == 100
        finally:
            thread.wait(5000)
            thread.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 3) Panel — state machine behind the preview
# ===========================================================================


class _FakePanelService:
    """Panel service: intent + plan preview + controllable install."""

    def __init__(
        self,
        plan=None,
        plan_raises=None,
        *,
        install_result=None,
        emissions=(),
        block_seconds: float = 0.0,
        cancel_poll: bool = False,
        has_install: bool = True,
    ) -> None:
        self._plan = plan
        self._plan_raises = plan_raises
        self._install_result = install_result
        self._emissions = emissions
        self._block_seconds = block_seconds
        self._cancel_poll = cancel_poll
        self._has_install = has_install
        self.plan_calls = 0
        self.plan_kwargs: list[dict] = []
        self.install_calls: list[dict] = []
        self.called_from_thread_ident: int | None = None

    def prepare_gpu_setup_intent(self, recommendation=None):
        from zealfie.host.models import GpuSetupIntent

        return GpuSetupIntent(
            recommendation=recommendation,
            actionable=True,
            message="GPU setup prepared, but no CUDA toolkit was installed.",
        )

    def build_accelerated_deployment_plan(self, **kwargs):
        self.plan_calls += 1
        self.plan_kwargs.append(kwargs)
        if self._plan_raises is not None:
            raise self._plan_raises
        return self._plan

    def install_accelerated_runtime(
        self,
        *,
        plan=None,
        recommendation=None,
        capabilities=None,
        cancel_check=None,
        progress_callback=None,
        fetcher=None,
        work_root=None,
    ):
        self.install_calls.append(
            {
                "plan": plan,
                "recommendation": recommendation,
                "capabilities": capabilities,
                "cancel_check": cancel_check,
                "fetcher": fetcher,
                "work_root": work_root,
            }
        )
        self.called_from_thread_ident = threading.get_ident()
        if not self._has_install:
            raise AssertionError("install method should not be reachable")
        for progress in self._emissions:
            progress_callback(progress)
        if self._cancel_poll:
            # Hermetic cooperative-cancellation stand-in: keep checking the
            # worker's cancel_check until it raises (bounded), like the real
            # service checkpoints; the real service turns the raise into a
            # cancelled result.
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                try:
                    cancel_check()
                except CooperativeCancellationError as exc:
                    return AcceleratedDeploymentResult(
                        success=False,
                        cancelled=True,
                        phase=AcceleratedDeploymentPhase.ACQUIRE,
                        reason=(
                            str(exc) or "accelerated deployment cancelled"
                        ),
                    )
                time.sleep(0.01)
            raise AssertionError("cancel poll never observed a cancel")
        elif self._block_seconds > 0:
            time.sleep(self._block_seconds)
        return self._install_result


class TestAccelerationPanelInstall:
    def test_no_install_button_before_preview(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_ready_plan())
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            # Fail-closed default: no Installer until an explicit preview
            # built a PLAN_READY plan.
            assert panel._install_button.isHidden() is True
            assert panel._cancel_button.isHidden() is True
            assert panel._progress_label.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_install_button_hidden_when_plan_not_ready(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_blocked_plan())
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            # Honest: a BLOCKED plan offers nothing to install.
            assert panel._install_button.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_install_button_visible_when_plan_ready(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_ready_plan())
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            assert panel._install_button.isHidden() is False
            assert "Installer" in panel._install_button.text()
            # Preview still shown in the detail label.
            assert "No changes have been made yet." in panel._detail_label.text()
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_install_click_runs_worker_off_gui_thread_then_ready(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        gui_ident = threading.get_ident()
        service = _FakePanelService(
            plan=_ready_plan(),
            install_result=_success_result(),
            emissions=(
                InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
                InstallProgress(InstallPhase.INSTALLING_RUNTIME, 60, "Installing\u2026"),
            ),
            block_seconds=0.2,
        )
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            panel._install_button.click()

            # Configure + Installer disabled while running.
            assert panel._button.isEnabled() is False
            assert panel._install_button.isHidden() is True
            assert panel._cancel_button.isHidden() is False

            # Real progress: phase label + canonical percent.
            ok = _wait_for(
                qapp,
                lambda: "Runtime build" in panel._progress_label.text(),
            )
            assert ok, f"progress label never updated: {panel._progress_label.text()!r}"
            assert "60%" in panel._progress_label.text()

            ok = _wait_for(
                qapp,
                lambda: "Accelerated runtime ready" in panel._summary_label.text(),
            )
            assert ok, "panel never reached the ready state"

            # The service call ran off the GUI thread and received the
            # previewed plan (identity — no replan, no reprobe).
            assert service.called_from_thread_ident != gui_ident
            assert len(service.install_calls) == 1
            assert service.install_calls[0]["plan"] is service._plan

            # Terminal: cancel hidden, configure re-enabled, no further
            # install offer until re-preview, and the progress shows the
            # real 100 only after the real end.
            assert panel._cancel_button.isHidden() is True
            assert panel._button.isEnabled() is True
            assert panel._install_button.isHidden() is True
            assert "Completed" in panel._progress_label.text()
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_transmits_fetcher_and_work_root_to_worker(self, qapp, tmp_path):
        """ZA-M1-2J.1: AccelerationPanel threads the composition root's
        fetcher/work root through create_accelerated_install_thread into
        the worker, and the real service call receives them."""
        from zealfie.gui.acceleration_panel import AccelerationPanel

        class _FakeFetcher:
            pass

        fetcher = _FakeFetcher()
        work_root = tmp_path / "panel-work"
        service = _FakePanelService(
            plan=_ready_plan(),
            install_result=_success_result(),
            block_seconds=0.05,
        )
        panel = AccelerationPanel(
            service=service, fetcher=fetcher, work_root=work_root
        )
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            panel._install_button.click()

            # The worker received the exact transports from the panel.
            worker = panel._install_worker
            assert worker is not None
            assert worker._fetcher is fetcher
            assert worker._work_root == work_root

            # ... and the service call received them from the worker.
            ok = _wait_for(
                qapp,
                lambda: "Accelerated runtime ready" in panel._summary_label.text(),
            )
            assert ok, "panel never reached the ready state"
            assert len(service.install_calls) == 1
            call = service.install_calls[0]
            assert call["fetcher"] is fetcher
            assert call["work_root"] == work_root
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_without_transports_delegates_with_none(self, qapp):
        """Without transports the panel still builds a worker (None
        fetcher/work root — the fail-closed service contract decides)."""
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(
            plan=_ready_plan(),
            install_result=_success_result(),
            block_seconds=0.05,
        )
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            panel._install_button.click()
            worker = panel._install_worker
            assert worker is not None
            assert worker._fetcher is None
            assert worker._work_root is None
            ok = _wait_for(
                qapp,
                lambda: "Accelerated runtime ready" in panel._summary_label.text(),
            )
            assert ok, "panel never reached the ready state"
            call = service.install_calls[0]
            assert call["fetcher"] is None
            assert call["work_root"] is None
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_install_failure_shows_reason_and_allows_retry(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(
            plan=_ready_plan(),
            install_result=AcceleratedDeploymentResult(
                success=False,
                cancelled=False,
                phase=AcceleratedDeploymentPhase.ACQUIRE,
                reason="accelerated artifact acquisition failed: no source configured",
            ),
            emissions=(
                InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
            ),
            block_seconds=0.1,
        )
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            panel._install_button.click()

            ok = _wait_for(
                qapp,
                lambda: "failed" in panel._summary_label.text().lower(),
            )
            assert ok, "failure summary never shown"

            detail = panel._detail_label.text()
            assert "no source configured" in detail
            assert "Traceback" not in detail

            # No fake 100%: the failure cleared the percent.
            assert "100%" not in panel._progress_label.text()

            # Retry possible: plan still valid, Installer offered again.
            assert panel._button.isEnabled() is True
            assert panel._install_button.isHidden() is False
            assert panel._cancel_button.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_cancel_button_cancels_cooperatively_and_shows_honest_message(
        self, qapp
    ):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(
            plan=_ready_plan(), install_result=None, cancel_poll=True
        )
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            panel._install_button.click()

            # Cancel visible while the worker runs.
            assert panel._cancel_button.isHidden() is False

            # Give the worker a moment to enter the service loop, then
            # cancel through the UI while the call is still in flight.
            time.sleep(0.1)
            qapp.processEvents()
            panel._cancel_button.click()

            ok = _wait_for(
                qapp,
                lambda: "cancelled" in panel._summary_label.text().lower(),
            )
            assert ok, f"cancelled summary never shown: {panel._summary_label.text()!r}"

            detail = panel._detail_label.text()
            assert "cancelled" in detail.lower()
            assert "Traceback" not in detail

            # Terminal state: cancel hidden, buttons usable again.
            assert panel._cancel_button.isHidden() is True
            assert panel._button.isEnabled() is True
            assert panel._install_button.isHidden() is False  # retry
            # No fake progress after cancellation.
            assert "100%" not in panel._progress_label.text()
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_cancel_button_hidden_after_activation(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(
            plan=_ready_plan(),
            install_result=_success_result(),
            emissions=(
                InstallProgress(InstallPhase.PREPARING, 0, "Preparing\u2026"),
                InstallProgress(InstallPhase.ACTIVATING, 95, "Activating\u2026"),
            ),
            block_seconds=0.3,
        )
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            panel._install_button.click()

            # Cancel offered before activation.
            assert panel._cancel_button.isHidden() is False

            # Once the worker reports Activation, cancel disappears —
            # cancelling after the atomic point is a hidden no-op.
            ok = _wait_for(
                qapp,
                lambda: "Activation" in panel._progress_label.text(),
            )
            assert ok, "activation view never shown"
            assert panel._cancel_button.isHidden() is True

            # Still hidden after the real end.
            ok = _wait_for(
                qapp,
                lambda: "Accelerated runtime ready" in panel._summary_label.text(),
            )
            assert ok, "ready summary never shown"
            assert panel._cancel_button.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_panel_degrades_without_install_method(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_ready_plan())
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            assert panel._install_button.isHidden() is False  # plan ready
            # Older service shape: no install method at all (getattr guard).
            service.install_accelerated_runtime = None
            panel._install_button.click()
            text = panel._detail_label.text()
            assert "not available" in text
            assert "Traceback" not in text
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_install_button_requires_fresh_preview_after_recommendation_change(
        self, qapp
    ):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_ready_plan())
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            assert panel._install_button.isHidden() is False
            # A fresh observation invalidates the previewed plan.
            panel.set_recommendation(_rec())
            assert panel._install_button.isHidden() is True
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()


# ===========================================================================
# 4) Panel sizing — the card must grow with its (word-wrapped) content
# ===========================================================================


def _long_preview_plan() -> AcceleratedDeploymentPlan:
    """A PLAN_READY plan whose preview wraps several lines at ~500 px.

    The closure-impact lines are deliberately long so the detail label
    word-wraps inside a representative panel width — the exact condition
    that used to truncate the text (the frame's vertical policy was
    Fixed and its height did not track the wrapped content).
    """
    return _make_plan(
        AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(
            HardwareCompatibilityStatus.SUPPORTED,
            "host acceleration is compatible",
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench", "zefocus"),
        keep_products=(
            PlannedKeepProduct(
                product_id="zebench",
                version="2.0.0",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            ),
            PlannedKeepProduct(
                product_id="zefocus",
                version="1.4.1",
                commit_sha="b" * 40,
                wheel_sha256="c" * 64,
            ),
        ),
        added_requirements=(),
        target_runtime=(
            "new shared runtime slot with accelerated NVIDIA_CUDA closure"
        ),
        closure_impact=(
            "Add accelerated-lib (>=1.0) [variant 1.2.0] — declared by "
            "zebench 2.0.0 to satisfy its accelerated NVIDIA_CUDA closure",
            "Add accelerated-extras (>=0.9) [variant 0.9.3] — declared by "
            "zefocus 1.4.1 for the shared runtime backend",
            "Rebuild the shared runtime slot to host the accelerated "
            "closure for both products",
        ),
    )


def _embed_panel(panel, width: int = 520, height: int = 500) -> QWidget:
    """Embed the panel in a container layout mirroring the main window
    (panel above a widgetResizable scroll area with a bottom stretch)."""
    host = QWidget()
    layout = QVBoxLayout(host)
    layout.setContentsMargins(16, 12, 16, 12)
    layout.setSpacing(10)
    layout.addWidget(panel)
    cards = QWidget()
    cards_layout = QVBoxLayout(cards)
    cards_layout.setContentsMargins(0, 0, 0, 0)
    cards_layout.addStretch()
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setWidget(cards)
    layout.addWidget(scroll)
    host.resize(width, height)
    return host


def _assert_no_overlap_no_clip(panel: AccelerationPanel) -> None:
    """The detail text must fully fit its label and never overlap the
    action buttons; the buttons must be fully inside the panel frame."""
    detail = panel._detail_label
    assert detail.isHidden() is False
    detail_rect = detail.geometry()
    needed = detail.heightForWidth(detail_rect.width())
    # The wrapped text must fit the label (no truncation/clipping).
    assert detail_rect.height() >= needed, (
        f"detail label {detail_rect.height()}px tall but its wrapped "
        f"text needs {needed}px"
    )
    prev_bottom = detail_rect.bottom()
    for button in (
        panel._button,
        panel._install_button,
        panel._cancel_button,
    ):
        if button is None or button.isHidden():
            continue
        rect = button.geometry()
        # Button starts below the previous item (small tolerance for the
        # layout spacing / frame margins).
        assert rect.top() >= prev_bottom - 2, (
            f"{button.objectName()} overlaps the detail text: "
            f"top={rect.top()} previous_bottom={prev_bottom}"
        )
        assert panel.rect().contains(rect), (
            f"{button.objectName()} is clipped by the panel frame"
        )
        prev_bottom = rect.bottom()


class TestAccelerationPanelSizing:
    def test_vertical_policy_is_preferred_not_fixed(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakePanelService(plan=_ready_plan()))
        try:
            policy = panel.sizePolicy()
            assert (
                policy.verticalPolicy() is not QSizePolicy.Policy.Fixed
            ), "the vertical policy must not pin the card height"
            assert policy.verticalPolicy() in (
                QSizePolicy.Policy.Preferred,
                QSizePolicy.Policy.Minimum,
            )
            assert policy.horizontalPolicy() is QSizePolicy.Policy.Expanding
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_no_fixed_or_maximum_heights_anywhere(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakePanelService(plan=_ready_plan()))
        try:
            panel.set_recommendation(_rec())
            panel._button.click()
            widgets = (
                panel,
                panel._summary_label,
                panel._detail_label,
                panel._progress_label,
                panel._button,
                panel._install_button,
                panel._cancel_button,
            )
            for widget in widgets:
                assert widget is not None
                assert widget.maximumHeight() == 16777215, (
                    f"{widget} has a maximum height -> cannot grow"
                )
                assert widget.minimumHeight() == 0, (
                    f"{widget} has a fixed minimum height -> "
                    "layout cannot stay compact"
                )
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_compact_size_hint_stays_small_without_detail(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        panel = AccelerationPanel(service=_FakePanelService(plan=_ready_plan()))
        try:
            panel.set_recommendation(_rec())
            panel.show()
            qapp.processEvents()
            panel.layout().activate()
            compact = panel.sizeHint().height()
            assert compact < 250, f"compact card too tall: {compact}"
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_showing_multi_line_preview_grows_panel_size_hint(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_long_preview_plan())
        panel = AccelerationPanel(service=service)
        try:
            panel.set_recommendation(_rec())
            panel.show()
            qapp.processEvents()
            panel.layout().activate()
            compact = panel.sizeHint().height()

            panel._button.click()  # real preview path (intent + plan lines)
            qapp.processEvents()
            panel.layout().activate()

            detail_lines = panel._detail_label.text().splitlines()
            assert len(detail_lines) >= 8
            grown = panel.sizeHint().height()
            assert grown >= compact + 50, (
                f"size hint did not grow with the detail text: "
                f"compact={compact} grown={grown}"
            )
        finally:
            panel.close()
            panel.deleteLater()
            qapp.processEvents()

    def test_detail_and_buttons_no_overlap_at_representative_width(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        service = _FakePanelService(plan=_long_preview_plan())
        panel = AccelerationPanel(service=service)
        host = _embed_panel(panel, width=520, height=500)
        try:
            panel.set_recommendation(_rec())
            host.show()
            qapp.processEvents()
            panel._button.click()
            qapp.processEvents()
            panel.layout().activate()
            host.layout().activate()
            qapp.processEvents()
            _assert_no_overlap_no_clip(panel)
            # Both the configure and the Installer button are offered
            # (PLAN_READY preview) and must be inside the panel.
            assert panel._install_button.isHidden() is False
            assert panel._button.geometry().top() >= (
                panel._detail_label.geometry().bottom() - 2
            )
        finally:
            host.close()
            host.deleteLater()
            qapp.processEvents()

    def test_font_scaling_grows_panel_and_preserves_no_overlap(self, qapp):
        from zealfie.gui.acceleration_panel import AccelerationPanel

        # Unscaled reference panel.
        ref_panel = AccelerationPanel(
            service=_FakePanelService(plan=_long_preview_plan())
        )
        ref_host = _embed_panel(ref_panel, width=520, height=500)
        # Font-scaled panel (simulated HiDPI scaling).
        scaled_panel = AccelerationPanel(
            service=_FakePanelService(plan=_long_preview_plan())
        )
        font = scaled_panel.font()
        font.setPointSize(font.pointSize() + 3)
        scaled_panel.setFont(font)
        scaled_host = _embed_panel(scaled_panel, width=520, height=500)
        try:
            ref_panel.set_recommendation(_rec())
            ref_host.show()
            scaled_panel.set_recommendation(_rec())
            scaled_host.show()
            qapp.processEvents()

            scaled_compact = scaled_panel.height()

            ref_panel._button.click()
            scaled_panel._button.click()
            qapp.processEvents()
            for p in (ref_panel, scaled_panel):
                p.layout().activate()
            ref_host.layout().activate()
            scaled_host.layout().activate()
            qapp.processEvents()

            # The scaled card grows with its content...
            assert scaled_panel.height() > scaled_compact
            # ... and stays taller than the unscaled one (larger font).
            assert scaled_panel.height() > ref_panel.height()
            # No truncation/overlap under scaling.
            _assert_no_overlap_no_clip(scaled_panel)
            _assert_no_overlap_no_clip(ref_panel)
        finally:
            for host in (ref_host, scaled_host):
                host.close()
                host.deleteLater()
            qapp.processEvents()
