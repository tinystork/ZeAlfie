"""ZA-M1-2J.2 Phase F — backend compute probe registry and gate behaviour.

FAST, deterministic, GPU-free:

* the generic registry (:mod:`zealfie.acceleration.backend_probe`) —
  lookup semantics, self-containment of the real NVIDIA_CUDA script
  (no zealfie imports, exact OK/FAIL markers);
* the default accelerated gate's compute-probe step — a registered
  probe runs with the candidate interpreter after the
  distribution/version checks pass, and its failure fails the gate
  BEFORE activation; a backend without a probe keeps the
  distribution/version-only behaviour.

The real cupy probe is NEVER executed here (no GPU): the tests inject
synthetic scripts via ``monkeypatch`` into the registry, and the
"candidate venv" is the test interpreter itself (a plan over the
installed ``packaging`` distribution passes the distribution/version
checks hermetically).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    PlannedAcceleratedDependency,
    VariantStatus,
)
from zealfie.acceleration.backend_probe import (
    BACKEND_COMPUTE_PROBES,
    get_backend_compute_probe,
)
from zealfie.acceleration.deployment import (
    _run_backend_compute_probe,
    default_accelerated_gate,
)

OK_SCRIPT = 'print("BACKEND_COMPUTE_PROBE_OK")\n'
FAIL_SCRIPT = (
    'import sys\n'
    'print("BACKEND_COMPUTE_PROBE_FAIL: RuntimeError: synthetic boom")\n'
    "sys.exit(1)\n"
)


def _hardware() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="compatible",
        products_concerned=("zebench",),
    )


def _gate_plan(
    backend: str,
    *,
    distribution: str = "packaging",
    version: str | None = None,
) -> AcceleratedDeploymentPlan:
    """A PLAN_READY plan over a distribution really installed in THIS
    interpreter (``packaging``), so the distribution/version gate steps
    pass hermetically when *version* matches."""
    if version is None:
        import importlib.metadata

        version = importlib.metadata.version(distribution)
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(),
        backend=backend,
        products_concerned=("zebench",),
        keep_products=(),
        added_requirements=(
            PlannedAcceleratedDependency(
                distribution=distribution,
                specifier=f"=={version}",
                extras=(),
                declaring_products=("zebench",),
                variant=AcceleratedVariant(
                    distribution=distribution,
                    version=version,
                    backend="NVIDIA_CUDA",
                ),
                variant_status=VariantStatus.SELECTED,
            ),
        ),
        source_runtime_state="READY",
        source_active_slot_id=None,
        source_previous_slot_id=None,
        target_runtime="new shared runtime slot with accelerated closure",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )


# =============================================================================
# Registry semantics
# =============================================================================


def test_registry_lookup_semantics():
    probe = get_backend_compute_probe("NVIDIA_CUDA")
    assert probe is not None
    assert set(probe) >= {"label", "script"}
    assert "cupy" in probe["script"] and "NVRTC" in probe["script"]

    assert get_backend_compute_probe(" NVIDIA_CUDA ") is not None
    assert get_backend_compute_probe("NO_SUCH_BACKEND") is None
    assert get_backend_compute_probe(None) is None
    assert get_backend_compute_probe("") is None


def test_registry_nvidia_script_is_self_contained():
    """The real probe script never imports ZeAlfie code and carries the
    exact success/failure markers the gate trusts."""
    probe = get_backend_compute_probe("NVIDIA_CUDA")
    assert probe is not None
    script = probe["script"]
    assert "zealfie" not in script
    assert "BACKEND_COMPUTE_PROBE_OK" in script
    assert "BACKEND_COMPUTE_PROBE_FAIL" in script
    # The script must be syntactically valid Python.
    compile(script, "<nvidia-probe>", "exec")


def test_registry_has_exactly_one_backend():
    assert set(BACKEND_COMPUTE_PROBES) == {"NVIDIA_CUDA"}


# =============================================================================
# Gate integration — probe runs only after distribution/version checks
# =============================================================================


def test_gate_runs_probe_when_backend_registered_ok(monkeypatch):
    monkeypatch.setitem(
        BACKEND_COMPUTE_PROBES,
        "FAKE_PROBE_BACKEND",
        {"label": "synthetic probe", "script": OK_SCRIPT},
    )
    gate = default_accelerated_gate()
    error = gate.check(sys.executable, _gate_plan("FAKE_PROBE_BACKEND"))
    assert error is None


def test_gate_fails_when_compute_probe_fails(monkeypatch):
    monkeypatch.setitem(
        BACKEND_COMPUTE_PROBES,
        "FAKE_PROBE_BACKEND",
        {"label": "synthetic probe", "script": FAIL_SCRIPT},
    )
    gate = default_accelerated_gate()
    error = gate.check(sys.executable, _gate_plan("FAKE_PROBE_BACKEND"))
    assert error is not None
    assert "backend compute probe failed for FAKE_PROBE_BACKEND" in error
    assert "synthetic boom" in error


def test_gate_fails_on_nonzero_without_marker(monkeypatch):
    monkeypatch.setitem(
        BACKEND_COMPUTE_PROBES,
        "FAKE_PROBE_BACKEND",
        {"label": "synthetic probe", "script": "import sys\nsys.exit(3)\n"},
    )
    gate = default_accelerated_gate()
    error = gate.check(sys.executable, _gate_plan("FAKE_PROBE_BACKEND"))
    assert error is not None
    assert "backend compute probe failed for FAKE_PROBE_BACKEND" in error
    assert "(exit 3)" in error


def test_gate_fails_on_missing_probe_script(monkeypatch):
    monkeypatch.setitem(
        BACKEND_COMPUTE_PROBES,
        "FAKE_PROBE_BACKEND",
        {"label": "scriptless probe", "script": "   "},
    )
    gate = default_accelerated_gate()
    error = gate.check(sys.executable, _gate_plan("FAKE_PROBE_BACKEND"))
    assert error is not None
    assert "has no script" in error


def test_gate_backend_without_probe_keeps_distribution_version_only():
    """Genericity preserved: a backend with no registered probe keeps the
    previous distribution/version-only behaviour."""
    gate = default_accelerated_gate()
    error = gate.check(sys.executable, _gate_plan("NO_PROBE_BACKEND"))
    assert error is None


def test_gate_probe_skipped_when_version_check_fails(monkeypatch):
    """The probe never runs when the distribution/version checks fail
    first (fail-fast ordering preserved)."""
    monkeypatch.setitem(
        BACKEND_COMPUTE_PROBES,
        "FAKE_PROBE_BACKEND",
        {"label": "synthetic probe", "script": OK_SCRIPT},
    )
    gate = default_accelerated_gate()
    plan = _gate_plan("FAKE_PROBE_BACKEND", version="0.0.0")
    error = gate.check(sys.executable, plan)
    assert error is not None
    assert "version mismatch" in error
    assert "compute probe" not in error


def test_gate_probe_cannot_start_honest_error(monkeypatch):
    monkeypatch.setitem(
        BACKEND_COMPUTE_PROBES,
        "FAKE_PROBE_BACKEND",
        {"label": "synthetic probe", "script": OK_SCRIPT},
    )
    gate = default_accelerated_gate()
    error = gate.check(
        str(Path("/no/such/python-interpreter")),
        _gate_plan("FAKE_PROBE_BACKEND"),
    )
    assert error is not None
    # The distribution probe fails first against a missing interpreter.
    assert "gate probe failed" in error


def test_compute_probe_helper_timeout():
    error = _run_backend_compute_probe(
        sys.executable,
        "FAKE_PROBE_BACKEND",
        {"label": "slow probe", "script": "import time\ntime.sleep(30)\n"},
        timeout=0.5,
    )
    assert error is not None
    assert "timed out" in error
    assert "FAKE_PROBE_BACKEND" in error


def test_compute_probe_helper_ok_marker_required():
    """Exit 0 without the OK marker is still a failure (a silent success
    is never trusted)."""
    error = _run_backend_compute_probe(
        sys.executable,
        "FAKE_PROBE_BACKEND",
        {"label": "silent probe", "script": "pass\n"},
    )
    assert error is not None
    assert "failed" in error
