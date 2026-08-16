"""Tests for the M1-2I accelerated deployment engine primitives.

Covers:

* :func:`extend_runtime_lock_with_acceleration` — purity, verbatim base
  entries (same ``LockedDependency`` objects, same insertion order),
  unchanged ``primary_names``, ``required_by`` / ``extras`` edges,
  deterministic append order, and every rejection case;
* ``dataclasses.replace`` rebinding of the deployment plan;
* :class:`AcceleratedSlotMetadata` validation and the slot-keyed
  :class:`AcceleratedSlotMetadataStore` (round-trip, lenient reads,
  atomic-write hygiene);
* the default accelerated gate against a REAL candidate venv built by
  ``SharedRuntime`` (stdlib-only probes, honest failures).

The default-gate tests are ``zealfie_slow`` (real venv + pip installs);
everything else is FAST.
"""

from __future__ import annotations

import json
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
from zealfie.acceleration.deployment import (
    AcceleratedSlotMetadata,
    AcceleratedSlotMetadataStore,
    AcquiredAcceleratedVariant,
    default_accelerated_gate,
    extend_runtime_lock_with_acceleration,
)
from zealfie.building import build_wheel
from zealfie.dependencies.models import LockedDependency, RuntimeLock
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime import (
    DeploymentPlan,
    DesiredComponent,
    DesiredRuntimeState,
    RuntimeLayout,
    RuntimeState,
    SharedRuntime,
)
from zealfie.runtime.probe import probe_runtime_distribution

# ---------------------------------------------------------------------------
# Synthetic plan helpers
# ---------------------------------------------------------------------------


def _hardware() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="compatible",
        products_concerned=("prod-a", "prod-b"),
    )


def _plan(
    *deps: tuple[str, str | None, tuple[str, ...], tuple[str, ...]],
) -> AcceleratedDeploymentPlan:
    """Build a PLAN_READY plan from
    ``(distribution, specifier, extras, declaring_products)`` tuples."""
    entries = tuple(
        PlannedAcceleratedDependency(
            distribution=distribution,
            specifier=specifier,
            extras=extras,
            declaring_products=products,
            variant=AcceleratedVariant(
                distribution=distribution,
                version="1.0.0",
                backend="NVIDIA_CUDA",
            ),
            variant_status=VariantStatus.SELECTED,
        )
        for distribution, specifier, extras, products in deps
    )
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(),
        backend="NVIDIA_CUDA",
        products_concerned=("prod-a", "prod-b"),
        keep_products=(),
        added_requirements=entries,
        source_runtime_state="READY",
        source_active_slot_id="rt-a",
        source_previous_slot_id=None,
        target_runtime="new shared runtime slot with accelerated NVIDIA_CUDA closure",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )


def _acquired(
    distribution: str,
    version: str,
    wheel_path: Path,
) -> AcquiredAcceleratedVariant:
    import hashlib

    sha = hashlib.sha256()
    with open(wheel_path, "rb") as fh:
        while chunk := fh.read(65536):
            sha.update(chunk)
    return AcquiredAcceleratedVariant(
        distribution=distribution,
        version=version,
        wheel_path=wheel_path,
        size=wheel_path.stat().st_size,
        sha256=sha.hexdigest(),
    )


def _tiny_wheel(tmp_path: Path, name: str, version: str = "1.0.0") -> Path:
    """A real-enough wheel file for ``AcquiredAcceleratedVariant``
    (zip archive; never pip-installed in the fast tests)."""
    import zipfile

    safe_name = name.replace("-", "_").replace(".", "_")
    wheel_path = tmp_path / f"{safe_name}-{version}-py3-none-any.whl"
    dist_info = f"{safe_name}-{version}.dist-info"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n",
        )
    return wheel_path


def _base_lock() -> RuntimeLock:
    witness = LockedDependency(
        name="zealfie-witness",
        version="0.0.1",
        wheel_path=Path("/fake/witness.whl"),
        size=1,
        sha256="a" * 64,
        extras=frozenset({"gui"}),
        required_by=frozenset(),
    )
    py_lib = LockedDependency(
        name="py-lib",
        version="2.0.0",
        wheel_path=Path("/fake/py-lib.whl"),
        size=2,
        sha256="b" * 64,
        extras=frozenset(),
        required_by=frozenset({"zealfie-witness"}),
    )
    return RuntimeLock(
        locked={
            witness.name: witness,
            py_lib.name: py_lib,
        },
        primary_names=frozenset({"zealfie-witness"}),
    )


# =============================================================================
# extend_runtime_lock_with_acceleration — invariants
# =============================================================================


def test_extension_preserves_base_entries_verbatim(tmp_path: Path) -> None:
    base_lock = _base_lock()
    base_order = list(base_lock.locked.keys())
    base_objs = {name: base_lock.locked[name] for name in base_order}

    plan = _plan(
        ("fake-b", ">=1.0", ("gpu",), ("prod-a", "prod-b")),
        ("fake-a", None, (), ("prod-a",)),
    )
    wheel_b = _tiny_wheel(tmp_path, "fake-b")
    wheel_a = _tiny_wheel(tmp_path, "fake-a")
    acquired = (
        _acquired("fake-b", "1.0.0", wheel_b),
        _acquired("fake-a", "1.0.0", wheel_a),
    )
    declaring = {"prod-a": "zealfie-witness", "prod-b": "py-lib"}

    extended = extend_runtime_lock_with_acceleration(
        base_lock, plan, acquired, declaring
    )

    # Base entries verbatim: same objects, same insertion order.
    assert extended is not base_lock
    ext_keys = list(extended.locked.keys())
    assert ext_keys[: len(base_order)] == base_order
    for name, obj in base_objs.items():
        assert extended.locked[name] is obj

    # primary_names unchanged (same frozenset object).
    assert extended.primary_names == base_lock.primary_names
    assert extended.primary_names is base_lock.primary_names

    # Accelerated entries appended in sorted-distribution order.
    assert ext_keys[len(base_order):] == ["fake-a", "fake-b"]

    # Accelerated entries carry variant facts + planned edges.
    fake_b = extended.locked["fake-b"]
    assert fake_b.name == "fake-b"
    assert fake_b.version == "1.0.0"
    assert fake_b.wheel_path == wheel_b
    assert fake_b.size == wheel_b.stat().st_size
    assert fake_b.sha256 == acquired[0].sha256
    assert fake_b.extras == frozenset({"gpu"})
    assert fake_b.required_by == frozenset({"zealfie-witness", "py-lib"})

    fake_a = extended.locked["fake-a"]
    assert fake_a.extras == frozenset()
    assert fake_a.required_by == frozenset({"zealfie-witness"})

    # Accelerated entries are never primaries.
    assert extended.dependency_names == frozenset(
        {"py-lib", "fake-a", "fake-b"}
    )


def test_extension_is_pure_no_input_mutation(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", None, (), ("prod-a",)))
    wheel_a = _tiny_wheel(tmp_path, "fake-a")
    acquired = (_acquired("fake-a", "1.0.0", wheel_a),)
    declaring = {"prod-a": "zealfie-witness"}

    locked_before = dict(base_lock.locked)
    plan_entries_before = plan.added_requirements

    extend_runtime_lock_with_acceleration(
        base_lock, plan, acquired, declaring
    )

    assert base_lock.locked == locked_before
    assert list(base_lock.locked.keys()) == list(locked_before.keys())
    assert plan.added_requirements is plan_entries_before
    assert acquired[0].distribution == "fake-a"


def test_extension_rejects_acquired_not_in_plan(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", None, (), ("prod-a",)))
    wheel = _tiny_wheel(tmp_path, "fake-a")
    wheel_other = _tiny_wheel(tmp_path, "fake-b")
    acquired = (
        _acquired("fake-a", "1.0.0", wheel),
        _acquired("fake-b", "1.0.0", wheel_other),
    )
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="not in the accelerated plan"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, acquired, declaring
        )


def test_extension_rejects_missing_acquired_variant(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(
        ("fake-a", None, (), ("prod-a",)),
        ("fake-b", None, (), ("prod-a",)),
    )
    wheel = _tiny_wheel(tmp_path, "fake-a")
    acquired = (_acquired("fake-a", "1.0.0", wheel),)
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="without acquired variant") as exc:
        extend_runtime_lock_with_acceleration(
            base_lock, plan, acquired, declaring
        )
    assert "fake-b" in str(exc.value)


def test_extension_rejects_version_outside_specifier(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", "==2.0.0", (), ("prod-a",)))
    wheel = _tiny_wheel(tmp_path, "fake-a", version="1.0.0")
    acquired = (_acquired("fake-a", "1.0.0", wheel),)
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="does not satisfy declared specifier"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, acquired, declaring
        )


def test_extension_accepts_prerelease_inside_specifier(tmp_path: Path) -> None:
    """Prereleases are allowed when the merged specifier admits them
    (mirrors the planner's ``prereleases=True`` contract)."""
    base_lock = _base_lock()
    plan = _plan(("fake-a", ">=1.0rc1", (), ("prod-a",)))
    wheel = _tiny_wheel(tmp_path, "fake-a", version="1.0.0rc1")
    acquired = (_acquired("fake-a", "1.0.0rc1", wheel),)
    declaring = {"prod-a": "zealfie-witness"}

    extended = extend_runtime_lock_with_acceleration(
        base_lock, plan, acquired, declaring
    )
    assert extended.locked["fake-a"].version == "1.0.0rc1"


def test_extension_rejects_duplicate_acquired(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", None, (), ("prod-a",)))
    wheel = _tiny_wheel(tmp_path, "fake-a")
    variant = _acquired("fake-a", "1.0.0", wheel)
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="duplicate acquired distribution"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, (variant, variant), declaring
        )


def test_extension_rejects_unknown_declaring_product(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", None, (), ("prod-unknown",)))
    wheel = _tiny_wheel(tmp_path, "fake-a")
    acquired = (_acquired("fake-a", "1.0.0", wheel),)
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="no declaring distribution"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, acquired, declaring
        )


def test_extension_rejects_empty_declaring_distribution(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", None, (), ("prod-a",)))
    wheel = _tiny_wheel(tmp_path, "fake-a")
    acquired = (_acquired("fake-a", "1.0.0", wheel),)
    declaring = {"prod-a": "   "}

    with pytest.raises(ValueError, match="must be a non-empty string"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, acquired, declaring
        )


def test_extension_rejects_base_lock_collision(tmp_path: Path) -> None:
    """An accelerated distribution already in the base lock would break
    the verbatim-base-entry invariant — rejected fail-closed."""
    base_lock = _base_lock()
    plan = _plan(("py-lib", None, (), ("prod-a",)))
    wheel = _tiny_wheel(tmp_path, "py-lib")
    acquired = (_acquired("py-lib", "1.0.0", wheel),)
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="already present in the base"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, acquired, declaring
        )


def test_extension_rejects_wrong_acquired_type(tmp_path: Path) -> None:
    base_lock = _base_lock()
    plan = _plan(("fake-a", None, (), ("prod-a",)))
    declaring = {"prod-a": "zealfie-witness"}

    with pytest.raises(ValueError, match="AcquiredAcceleratedVariant"):
        extend_runtime_lock_with_acceleration(
            base_lock, plan, ("not-a-variant",), declaring
        )


# =============================================================================
# DeploymentPlan rebinding via dataclasses.replace
# =============================================================================


def test_deployment_plan_rebind_via_replace(tmp_path: Path) -> None:
    """The RESOLVE step rebinds the deployment plan without touching the
    original (M0-8B input object stays untouched)."""
    lock_a = _base_lock()
    artifact = VerifiedArtifact(
        component_id="zewitness",
        version="0.0.1",
        path=Path("/fake/witness.whl"),
        size=1,
        sha256="a" * 64,
        distribution_name="zealfie-witness",
        wheel_version="0.0.1",
    )
    plan = DeploymentPlan(
        desired_state=DesiredRuntimeState(
            components=(
                DesiredComponent(
                    component_id="zewitness",
                    version="0.0.1",
                    artifact=artifact,
                ),
            )
        ),
        runtime_state=RuntimeState.READY,
        steps=(),
        source_active_slot_id="rt-a",
        dependency_lock=lock_a,
    )

    import dataclasses

    rebound = dataclasses.replace(plan, dependency_lock=lock_a)

    assert rebound is not plan
    assert rebound.dependency_lock is lock_a
    assert rebound.source_active_slot_id == plan.source_active_slot_id
    assert rebound.desired_state is plan.desired_state
    assert plan.dependency_lock is lock_a


# =============================================================================
# AcceleratedSlotMetadata validation
# =============================================================================


def test_slot_metadata_validation_and_sorting() -> None:
    metadata = AcceleratedSlotMetadata(
        backend="NVIDIA_CUDA",
        variants=(("fake-b", "1.0.0", "c" * 64), ("fake-a", "1.0.0", "a" * 64)),
    )
    assert metadata.backend == "NVIDIA_CUDA"
    assert metadata.variants == (
        ("fake-a", "1.0.0", "a" * 64),
        ("fake-b", "1.0.0", "c" * 64),
    )


def test_slot_metadata_rejects_empty_backend() -> None:
    with pytest.raises(ValueError, match="backend must be a non-empty string"):
        AcceleratedSlotMetadata(backend="", variants=(("fake-a", "1.0.0", "a" * 64),))


def test_slot_metadata_rejects_empty_variants() -> None:
    with pytest.raises(ValueError, match="variants must not be empty"):
        AcceleratedSlotMetadata(backend="NVIDIA_CUDA", variants=())


def test_slot_metadata_rejects_malformed_variant() -> None:
    with pytest.raises(ValueError, match="triples of non-empty strings"):
        AcceleratedSlotMetadata(
            backend="NVIDIA_CUDA", variants=(("fake-a", "1.0.0"),)
        )


# =============================================================================
# AcceleratedSlotMetadataStore
# =============================================================================


def _store(tmp_path: Path) -> AcceleratedSlotMetadataStore:
    return AcceleratedSlotMetadataStore(RuntimeLayout(root=tmp_path / "rt"))


def test_metadata_store_round_trip(tmp_path: Path) -> None:
    store = _store(tmp_path)
    meta_a = AcceleratedSlotMetadata(
        backend="NVIDIA_CUDA",
        variants=(("fake-a", "1.0.0", "a" * 64),),
    )
    meta_b = AcceleratedSlotMetadata(
        backend="NVIDIA_CUDA",
        variants=(("fake-b", "2.0.0", "b" * 64),),
    )

    store.record("rt-aaaa", meta_a)
    store.record("rt-bbbb", meta_b)

    assert store.load_slot("rt-aaaa") == meta_a
    assert store.load_slot("rt-bbbb") == meta_b
    assert store.load_slot("rt-cccc") is None

    # Replace on rewrite.
    meta_a2 = AcceleratedSlotMetadata(
        backend="NVIDIA_CUDA",
        variants=(("fake-a", "1.0.1", "c" * 64),),
    )
    store.record("rt-aaaa", meta_a2)
    assert store.load_slot("rt-aaaa") == meta_a2
    assert store.load_slot("rt-bbbb") == meta_b


def test_metadata_store_lenient_reads(tmp_path: Path) -> None:
    store = _store(tmp_path)

    # Missing file.
    assert store.load_slot("rt-aaaa") is None

    # Corrupt JSON.
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("{ not json", encoding="utf-8")
    assert store.load_slot("rt-aaaa") is None

    # Unknown schema version.
    store.path.write_text(
        json.dumps({"schema_version": 99, "slots": {}}), encoding="utf-8"
    )
    assert store.load_slot("rt-aaaa") is None

    # Non-dict root.
    store.path.write_text(json.dumps(["not", "a", "dict"]), encoding="utf-8")
    assert store.load_slot("rt-aaaa") is None

    # Malformed slot entry -> unknown for that slot, others survive.
    good = {
        "schema_version": 1,
        "slots": {
            "rt-good": {
                "backend": "NVIDIA_CUDA",
                "variants": [["fake-a", "1.0.0", "a" * 64]],
            },
            "rt-bad": {"backend": 42},
        },
    }
    store.path.write_text(json.dumps(good), encoding="utf-8")
    assert store.load_slot("rt-good") is not None
    assert store.load_slot("rt-bad") is None
    assert store.load_slot("rt-missing") is None


def test_metadata_store_atomic_write_hygiene(tmp_path: Path) -> None:
    """Writes are atomic: no temp files left behind, valid content on
    disk, parent directory created."""
    store = _store(tmp_path)
    meta = AcceleratedSlotMetadata(
        backend="NVIDIA_CUDA",
        variants=(("fake-a", "1.0.0", "a" * 64),),
    )
    store.record("rt-aaaa", meta)

    assert store.path.is_file()
    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["slots"]["rt-aaaa"]["backend"] == "NVIDIA_CUDA"

    leftovers = list(store.path.parent.glob(".accelerated-metadata-*"))
    assert leftovers == []


def test_metadata_store_validates_slot_ids(tmp_path: Path) -> None:
    store = _store(tmp_path)
    meta = AcceleratedSlotMetadata(
        backend="NVIDIA_CUDA",
        variants=(("fake-a", "1.0.0", "a" * 64),),
    )
    with pytest.raises(ValueError):
        store.record("../escape", meta)


def test_metadata_store_rejects_wrong_type(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with pytest.raises(ValueError, match="AcceleratedSlotMetadata"):
        store.record("rt-aaaa", object())


# =============================================================================
# Default accelerated gate — real candidate venv (zealfie_slow)
# =============================================================================


@pytest.fixture(scope="session")
def fake_accel_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build fake-accel 1.0.0 once per session."""
    d = Path(__file__).resolve().parent / "fixtures" / "fake_accel"
    t = tmp_path_factory.mktemp("shared-fake-accel")
    return build_wheel(d, output_dir=t)


def _slot_python(slot_dir: Path) -> Path:
    import sys

    if sys.platform == "win32":
        return slot_dir / "Scripts" / "python.exe"
    return slot_dir / "bin" / "python"


@pytest.mark.zealfie_slow
def test_default_gate_passes_when_installed_at_planned_version(
    tmp_path: Path, fake_accel_wheel: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The plan backend is NVIDIA_CUDA, whose registered compute probe
    # imports cupy — absent from this synthetic candidate.  The compute
    # probe behaviour is covered by dedicated tests
    # (tests/test_acceleration_backend_probe.py and the Phase F
    # transaction tests); here we keep this slow test focused on the
    # distribution/version gate.
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active = rt.status().active_slot_id
    assert active is not None

    install = rt.install_local_wheel(fake_accel_wheel, slot_id=active)
    assert install.outcome.value in ("INSTALLED", "ALREADY_INSTALLED")

    plan = _plan(("fake-accel", "==1.0.0", (), ("prod-a",)))
    gate = default_accelerated_gate()
    python = str(_slot_python(layout.slot_path(active)))
    assert gate.check(python, plan) is None


@pytest.mark.zealfie_slow
def test_default_gate_fails_on_missing_distribution(
    tmp_path: Path, fake_accel_wheel: Path,
) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active = rt.status().active_slot_id
    assert active is not None

    # fake-accel installed, but the plan also requires fake-accel-b.
    rt.install_local_wheel(fake_accel_wheel, slot_id=active)
    plan = _plan(
        ("fake-accel", "==1.0.0", (), ("prod-a",)),
        ("fake-accel-b", None, (), ("prod-a",)),
    )
    gate = default_accelerated_gate()
    python = str(_slot_python(layout.slot_path(active)))
    error = gate.check(python, plan)
    assert error is not None
    assert "fake-accel-b" in error
    assert "not installed" in error


@pytest.mark.zealfie_slow
def test_default_gate_fails_on_version_mismatch(
    tmp_path: Path, fake_accel_wheel: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Keep this slow test focused on the distribution/version gate: the
    # NVIDIA_CUDA compute probe (cupy) cannot run in this synthetic
    # candidate (covered separately in the Phase F probe tests).
    monkeypatch.setattr(
        "zealfie.acceleration.deployment.get_backend_compute_probe",
        lambda backend: None,
    )
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active = rt.status().active_slot_id
    assert active is not None

    rt.install_local_wheel(fake_accel_wheel, slot_id=active)

    # Installed version is 1.0.0, but the plan selects variant 2.0.0.
    import zealfie.acceleration.planning as planning

    plan = _plan(("fake-accel", "==1.0.0", (), ("prod-a",)))
    variant_2 = AcceleratedVariant(
        distribution="fake-accel", version="2.0.0", backend="NVIDIA_CUDA"
    )
    entry_2 = planning.PlannedAcceleratedDependency(
        distribution="fake-accel",
        specifier="==2.0.0",
        extras=(),
        declaring_products=("prod-a",),
        variant=variant_2,
        variant_status=VariantStatus.SELECTED,
    )
    plan_mismatch = AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(),
        backend="NVIDIA_CUDA",
        products_concerned=("prod-a", "prod-b"),
        keep_products=(),
        added_requirements=(entry_2,),
        source_runtime_state="READY",
        source_active_slot_id="rt-a",
        source_previous_slot_id=None,
        target_runtime="new shared runtime slot with accelerated NVIDIA_CUDA closure",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )

    gate = default_accelerated_gate()
    python = str(_slot_python(layout.slot_path(active)))
    error = gate.check(python, plan_mismatch)
    assert error is not None
    assert "version mismatch" in error
    assert "2.0.0" in error

    # And the matching plan still passes against the same candidate.
    assert gate.check(python, plan) is None


@pytest.mark.zealfie_slow
def test_default_gate_fails_honestly_on_bad_python(tmp_path: Path) -> None:
    plan = _plan(("fake-accel", "==1.0.0", (), ("prod-a",)))
    gate = default_accelerated_gate()
    error = gate.check(str(tmp_path / "no-such-python"), plan)
    assert error is not None
    assert "gate probe failed" in error


@pytest.mark.zealfie_slow
def test_default_gate_probe_is_stdlib_only(
    tmp_path: Path, fake_accel_wheel: Path,
) -> None:
    """The gate probes through the candidate interpreter with a
    stdlib-only script: the observed distribution facts come from
    ``importlib.metadata`` inside the candidate venv."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    active = rt.status().active_slot_id
    assert active is not None
    rt.install_local_wheel(fake_accel_wheel, slot_id=active)

    python = _slot_python(layout.slot_path(active))
    probe = probe_runtime_distribution(str(python), "fake-accel")
    assert probe["installed"] is True
    assert probe["version"] == "1.0.0"
