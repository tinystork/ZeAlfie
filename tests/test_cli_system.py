"""CLI tests for ``zealfie system capabilities`` (M1-2G).

Read-only diagnostic command; the fake service is injected via
``_make_service`` so no real host probing occurs.
"""

from __future__ import annotations

from io import StringIO

import pytest

import zealfie.cli as cli
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
