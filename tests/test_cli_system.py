"""CLI tests for ``zealfie system capabilities`` (M1-2G) and the
``zealfie system gpu-install`` fail-closed stub (M1-2I).

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


def test_system_gpu_install_in_parser():
    p = cli.build_parser()
    args = p.parse_args(["system", "gpu-install"])
    assert args.command == "system"
    assert args.system_command == "gpu-install"


def test_system_gpu_install_fail_closed_stub(monkeypatch):
    """`zealfie system gpu-install` is a fail-closed stub: honest
    human-gate message, non-zero exit, and NO service construction (so
    no acquisition, no planning, no runtime mutation)."""
    made: list[bool] = []

    def _spy_make_service():
        made.append(True)
        raise AssertionError("gpu-install must not construct a service")

    monkeypatch.setattr(cli, "_make_service", _spy_make_service)
    code = cli.run(["system", "gpu-install"])
    assert code != 0
    assert made == []


def test_system_gpu_install_message_is_honest(monkeypatch, capsys):
    """The stub's message states the human gate honestly: no artifact
    source configured, explicit authorization required."""
    monkeypatch.setattr(
        cli, "_make_service",
        lambda: (_ for _ in ()).throw(
            AssertionError("gpu-install must not construct a service")
        ),
    )
    code = cli.run(["system", "gpu-install"])
    captured = capsys.readouterr()
    assert code != 0
    assert "no accelerated artifact source" in captured.err
    assert "explicit authorization" in captured.err


def test_system_gpu_install_never_mutates_runtime(monkeypatch, tmp_path):
    """Invoking the stub leaves an existing runtime state file
    byte-identical and creates no files."""
    import hashlib

    monkeypatch.setattr(
        cli, "_make_service",
        lambda: (_ for _ in ()).throw(
            AssertionError("gpu-install must not construct a service")
        ),
    )
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

    after_hash = hashlib.sha256(state_file.read_bytes()).hexdigest()
    after_files = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*")
    )
    assert after_hash == before_hash
    assert after_files == before_files
