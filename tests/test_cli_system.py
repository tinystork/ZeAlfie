"""CLI tests for ``zealfie system capabilities`` (M1-2G) and the
real ``zealfie system gpu-install`` handler (ZA-M1-2J Phase D).

Diagnostic and install commands; the fake service is injected via
``_make_service`` so no real host probing, acquisition, or runtime
mutation occurs.
"""

from __future__ import annotations

from io import StringIO

import pytest

from types import SimpleNamespace

import zealfie.cli as cli
from zealfie.acceleration import (
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentPlan,
    AcceleratedDeploymentResult,
    AcceleratedPlanStatus,
    HardwareCompatibility,
    HardwareCompatibilityStatus,
)
from zealfie.host.models import (
    CapabilityStatus,
    GpuKind,
    HostCapabilities,
    RecommendationStatus,
)


def _caps(**kwargs) -> HostCapabilities:
    defaults = dict(
        os_name="Linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=None,
        platform_reason=None,
        gpus=(),
        partial=False,
    )
    defaults.update(kwargs)
    return HostCapabilities(**defaults)


class _FakeSystemService:
    """Fake ZeAlfieService exposing only the host capability API."""

    def __init__(self, caps, rec) -> None:
        self._caps = caps
        self._rec = rec
        self.collect_calls = 0
        self.collected_caps = None
        self.recommend_calls = 0
        self.recommended_caps = None

    def collect_host_capabilities(self):
        self.collect_calls += 1
        self.collected_caps = self._caps
        return self._caps

    def get_acceleration_recommendation(self, capabilities=None):
        self.recommend_calls += 1
        self.recommended_caps = capabilities
        return self._rec


def _recommendation(status, reason="test reason", backend="NVIDIA_CUDA"):
    from zealfie.host.models import AccelerationRecommendation, HostReasonCode

    return AccelerationRecommendation(
        status=status,
        backend=backend,
        reason_code=HostReasonCode.ACCELERATION_NOT_APPLICABLE,
        reason=reason,
    )


def test_system_capabilities_cpu_only(monkeypatch):
    service = _FakeSystemService(
        _caps(),
        _recommendation(RecommendationStatus.NOT_APPLICABLE, "no accelerator"),
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "capabilities"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "Host capabilities:" in output
    assert "OS: Linux" in output
    assert "CPU architecture: x86_64" in output
    assert "GPUs: 0" in output
    assert "Acceleration recommendation: NOT_APPLICABLE" in output
    assert "Reason: no accelerator" in output


def test_system_capabilities_with_gpu(monkeypatch):
    from zealfie.host.models import GpuInfo

    gpu = GpuInfo(
        vendor="NVIDIA",
        model="RTX 4090",
        kind=GpuKind.DISCRETE,
        hardware_present=True,
        driver_status=CapabilityStatus.AVAILABLE,
        driver_version="560.35.03",
        driver_reason_code=None,
        driver_reason=None,
        nvidia_smi_available=True,
        cuda_driver_present=True,
    )
    service = _FakeSystemService(
        _caps(gpus=(gpu,)),
        _recommendation(RecommendationStatus.OFFER_SETUP, "offer"),
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "capabilities"], stdout=stdout)
    assert code == 0
    output = stdout.getvalue()
    assert "GPUs: 1" in output
    assert "NVIDIA RTX 4090 (discrete, driver 560.35.03)" in output
    assert "Acceleration recommendation: OFFER_SETUP" in output


def test_system_capabilities_recommendation_from_single_observation(monkeypatch):
    """CLI collects once and derives the recommendation from that same
    observation — it must not trigger a second collection."""
    caps = _caps()
    service = _FakeSystemService(
        caps,
        _recommendation(RecommendationStatus.NOT_APPLICABLE, "no accelerator"),
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "capabilities"], stdout=stdout)
    assert code == 0
    assert service.collect_calls == 1
    assert service.recommend_calls == 1
    assert service.recommended_caps is service.collected_caps
    assert service.recommended_caps is caps


def test_system_capabilities_in_parser():
    p = cli.build_parser()
    args = p.parse_args(["system", "capabilities"])
    assert args.command == "system"
    assert args.system_command == "capabilities"


def test_system_command_never_mutates(monkeypatch, tmp_path):
    """Running the command only reads from the service; no files created."""
    service = _FakeSystemService(
        _caps(),
        _recommendation(RecommendationStatus.NOT_APPLICABLE, "no accelerator"),
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    before = sorted(p.name for p in tmp_path.iterdir())
    stdout = StringIO()
    code = cli.run(["system", "capabilities"], stdout=stdout)
    assert code == 0
    assert service.collect_calls == 1
    after = sorted(p.name for p in tmp_path.iterdir())
    assert before == after


def test_system_gpu_install_in_parser():
    p = cli.build_parser()
    args = p.parse_args(["system", "gpu-install"])
    assert args.command == "system"
    assert args.system_command == "gpu-install"


def _blocked_plan() -> AcceleratedDeploymentPlan:
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.BLOCKED,
        hardware=HardwareCompatibility(
            status=HardwareCompatibilityStatus.BLOCKED,
            reason_code="ACCELERATION_BLOCKED",
            reason="nvidia driver too old",
            products_concerned=("zebench",),
        ),
        backend=None,
        products_concerned=("zebench",),
        keep_products=(),
        added_requirements=(),
        source_runtime_state="READY",
        source_active_slot_id=None,
        source_previous_slot_id=None,
        target_runtime="no new runtime required",
        blocked=True,
        blocked_reason="nvidia driver too old",
        closure_impact=(),
    )


def _ready_plan() -> AcceleratedDeploymentPlan:
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=HardwareCompatibility(
            status=HardwareCompatibilityStatus.SUPPORTED,
            reason_code="COMPATIBLE",
            reason="compatible",
            products_concerned=("zebench",),
        ),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
        keep_products=(),
        added_requirements=(),
        source_runtime_state="READY",
        source_active_slot_id="rt-old",
        source_previous_slot_id=None,
        target_runtime="new shared runtime slot with accelerated "
        "NVIDIA_CUDA closure",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )


class _FakeGpuInstallService:
    """Fake service for the real gpu-install handler."""

    def __init__(self, plan=None, plan_raises=None, result=None,
                 install_raises=None) -> None:
        self._plan = plan
        self._plan_raises = plan_raises
        self._result = result
        self._install_raises = install_raises
        self.plan_calls = 0
        self.install_kwargs: list[dict] = []

    def build_accelerated_deployment_plan(self, **kwargs):
        self.plan_calls += 1
        if self._plan_raises is not None:
            raise self._plan_raises
        return self._plan

    def install_accelerated_runtime(self, **kwargs):
        self.install_kwargs.append(kwargs)
        if self._install_raises is not None:
            raise self._install_raises
        return self._result


def _success_result() -> AcceleratedDeploymentResult:
    return AcceleratedDeploymentResult(
        success=True,
        cancelled=False,
        phase=AcceleratedDeploymentPhase.COMPLETED,
        active_slot_id="rt-new",
        previous_slot_id="rt-old",
    )


def test_system_gpu_install_not_plan_ready_honest_nonzero(monkeypatch, capsys):
    """A non-PLAN_READY plan is reported honestly with a non-zero exit
    and the installer is never called."""
    service = _FakeGpuInstallService(plan=_blocked_plan())
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    code = cli.run(["system", "gpu-install"])
    assert code != 0
    assert service.plan_calls == 1
    assert service.install_kwargs == []
    err = capsys.readouterr().err
    assert "not available" in err
    assert "BLOCKED" in err
    assert "nvidia driver too old" in err


def test_system_gpu_install_plan_ready_delegates_with_service_defaults(
    monkeypatch,
):
    """A PLAN_READY plan is delegated to install_accelerated_runtime
    WITHOUT an explicit acquirer (the service manifest default is
    used), with progress on stdout and an honest final result."""
    service = _FakeGpuInstallService(plan=_ready_plan(), result=_success_result())

    def _fake_install(**kwargs):
        progress = kwargs.get("progress_callback")
        if progress is not None:
            progress(SimpleNamespace(
                percent=45, message="Planning accelerated runtime"
            ))
        service.install_kwargs.append(kwargs)
        return _success_result()

    service.install_accelerated_runtime = _fake_install
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-install"], stdout=stdout)
    assert code == 0
    assert service.plan_calls == 1
    assert len(service.install_kwargs) == 1
    kwargs = service.install_kwargs[0]
    assert kwargs.get("plan") is service._plan
    assert "acquirer" not in kwargs
    output = stdout.getvalue()
    assert "[45%] Planning accelerated runtime" in output
    assert "Accelerated deployment result:" in output
    assert "Success: yes" in output
    assert "Active slot: rt-new" in output


def test_system_gpu_install_plan_ready_transmits_fetcher_and_work_root(
    monkeypatch,
):
    """ZA-M1-2J.1 wiring: the CLI handler reuses the existing install-deps
    factories and hands the REAL archive fetcher plus the platform work
    root to ``install_accelerated_runtime``.

    Regression guard for the first real gpu-install failure: on d29e758
    the handler delegated WITHOUT a fetcher, so the real service failed
    at [0%] with "no artifact fetcher configured".  On that commit this
    test fails (``kwargs.get("fetcher")`` would be None)."""
    from pathlib import Path

    from zealfie.sources.github import GitHubArchiveFetcher

    service = _FakeGpuInstallService(plan=_ready_plan(), result=_success_result())
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-install"], stdout=stdout)
    assert code == 0
    assert service.plan_calls == 1
    assert len(service.install_kwargs) == 1
    kwargs = service.install_kwargs[0]
    assert kwargs.get("plan") is service._plan
    assert kwargs.get("progress_callback") is not None
    # The production transports are transmitted (never None, never
    # rebuilt from a second source inside the engine).
    assert isinstance(kwargs.get("fetcher"), GitHubArchiveFetcher)
    assert isinstance(kwargs.get("work_root"), Path)
    assert kwargs.get("work_root") is not None
    output = stdout.getvalue()
    assert "Success: yes" in output


def test_system_gpu_install_failed_result_honest_nonzero(monkeypatch):
    """A failed deployment result is reported honestly with a non-zero
    exit (success is never fabricated)."""
    failure = AcceleratedDeploymentResult(
        success=False,
        cancelled=False,
        phase=AcceleratedDeploymentPhase.ACQUIRE,
        reason="accelerated artifact acquisition failed: synthetic",
    )
    service = _FakeGpuInstallService(plan=_ready_plan(), result=failure)
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    stdout = StringIO()
    code = cli.run(["system", "gpu-install"], stdout=stdout)
    assert code != 0
    output = stdout.getvalue()
    assert "Success: no" in output
    assert "accelerated artifact acquisition failed: synthetic" in output


def test_system_gpu_install_build_error_nonzero(monkeypatch, capsys):
    """A plan-builder exception surfaces honestly with a non-zero exit."""
    service = _FakeGpuInstallService(plan_raises=RuntimeError("probe exploded"))
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    code = cli.run(["system", "gpu-install"])
    assert code != 0
    assert service.install_kwargs == []
    assert "gpu install failed" in capsys.readouterr().err


def test_system_gpu_install_never_mutates_runtime(monkeypatch, tmp_path):
    """A refused (BLOCKED) gpu-install leaves an existing runtime state
    file byte-identical and creates no files."""
    import hashlib

    service = _FakeGpuInstallService(plan=_blocked_plan())
    monkeypatch.setattr(cli, "_make_service", lambda: service)
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    state_file = state_dir / "active.json"
    payload = '{"active_slot_id": "rt-slot-before"}\n'
    state_file.write_text(payload)
    before_hash = hashlib.sha256(state_file.read_bytes()).hexdigest()
    before_files = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
    )

    code = cli.run(["system", "gpu-install"])
    assert code != 0
    assert service.install_kwargs == []

    after_hash = hashlib.sha256(state_file.read_bytes()).hexdigest()
    after_files = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
    )
    assert after_hash == before_hash
    assert after_files == before_files
