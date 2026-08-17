"""Service-layer tests for M1-2G host acceleration discovery.

Verifies that ZeAlfieService exposes the observation -> interpretation ->
service contract, and that the capability collector and recommender are
injectable.
"""

from __future__ import annotations

from zealfie.app import ZeAlfieService
from zealfie.host import HostProber, recommend
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


def test_service_uses_injected_collector_and_recommender():
    calls = []

    def collector():
        calls.append("collect")
        return _caps()

    def recommender(caps):
        calls.append("recommend")
        return "rec"

    service = ZeAlfieService(
        capability_collector=collector,
        recommender=recommender,
    )
    assert service.collect_host_capabilities() is not None
    assert service.get_acceleration_recommendation() == "rec"
    assert calls == ["collect", "collect", "recommend"]


def test_service_defaults_are_constructible_and_hermetic(monkeypatch):
    """Default wiring uses the real HostProber/recommender lazily.

    Constructing a default service must not run any host command.  We
    monkeypatch at the module boundary so the default collector/recommender
    wiring is exercised without ever touching real hardware/system probes.
    """
    from zealfie.app import service as service_module

    calls = []

    class _FakeProber:
        def collect(self):
            calls.append("collect")
            return _caps()

    def _fake_recommend(caps):
        calls.append("recommend")
        return "rec"

    monkeypatch.setattr(service_module, "HostProber", _FakeProber)
    monkeypatch.setattr(service_module, "recommend", _fake_recommend)

    service = ZeAlfieService()
    # Construction is lazy: no probe ran yet.
    assert calls == []

    caps = service.collect_host_capabilities()
    assert isinstance(caps, HostCapabilities)
    rec = service.get_acceleration_recommendation()
    assert rec == "rec"
    assert calls == ["collect", "collect", "recommend"]


def test_prepare_gpu_setup_intent_is_no_mutation(tmp_path):
    # Inject an OFFER_SETUP-capable collector to exercise the intent path.
    # ZA-M1-3A.2: the recommendation overlay consults the runtime's
    # ACTIVE slot (accelerated-metadata + closure) before reporting
    # ALREADY_READY — an empty hermetic runtime keeps this test
    # deterministic on any machine (including hosts with a real
    # validated accelerated runtime).
    from zealfie.host.models import GpuInfo
    from zealfie.runtime.layout import RuntimeLayout
    from zealfie.runtime.manager import SharedRuntime

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

    def collector():
        return _caps(gpus=(gpu,))

    service = ZeAlfieService(
        capability_collector=collector,
        recommender=recommend,
        runtime=SharedRuntime(RuntimeLayout(root=tmp_path / "rt")),
    )
    intent = service.prepare_gpu_setup_intent()
    assert intent.actionable is True
    assert intent.performed_any_mutation is False
    assert intent.recommendation.status is RecommendationStatus.OFFER_SETUP


def test_prepare_gpu_setup_intent_uses_supplied_recommendation_without_reprobe():
    """A supplied recommendation is used as-is — no second hardware probe."""
    from zealfie.host.models import (
        AccelerationRecommendation,
        GpuInfo,
        HostReasonCode,
    )

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
    recommendation = AccelerationRecommendation(
        status=RecommendationStatus.OFFER_SETUP,
        backend="NVIDIA_CUDA",
        reason_code=HostReasonCode.ACCELERATION_OFFER_SETUP,
        reason="offer",
        gpus=(gpu,),
    )

    collect_calls = 0
    recommend_calls = 0

    def collector():
        nonlocal collect_calls
        collect_calls += 1
        return _caps(gpus=(gpu,))

    def recommender(caps):
        nonlocal recommend_calls
        recommend_calls += 1
        return recommendation

    service = ZeAlfieService(
        capability_collector=collector,
        recommender=recommender,
    )
    intent = service.prepare_gpu_setup_intent(recommendation)

    assert intent.recommendation is recommendation
    assert intent.actionable is True
    assert intent.performed_any_mutation is False
    assert collect_calls == 0
    assert recommend_calls == 0
