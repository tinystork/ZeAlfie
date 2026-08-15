"""Tests for M1-2F Phase 4 corrective — installed-runtime lock read model.

Covers the pure installed-lock store (reduced-lock roundtrip, deterministic
sorting, no transient wheel-path/size/sha256 serialization, UNKNOWN readback,
active-pointer/rollback behaviour) and the service-level persistence ordering
through ``install_prepared_product_deployment`` with fake apply / fake
runtime (no real venv, no real GitHub).

FAST: no ``zealfie_slow`` marker — no real wheel building, venv, or pip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zealfie.app import (
    InstalledDependency,
    InstalledLockStore,
    InstalledRuntimeLock,
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.compatibility import (
    CompatibilityFinding,
    CompatibilityReport,
    CompatibilityVerdict,
)
from zealfie.components.model import EntryPointContract
from zealfie.dependencies.models import LockedDependency, RuntimeLock
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.installed_lock import installed_lock_from_runtime_lock
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, ResolvedSource


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
WHEEL_SHA = "a" * 64
DEP_SHA = "b" * 64

_EP = (EntryPointContract("console_scripts", "zewitness"),)


def _make_ppa(
    product_id: str,
    *,
    version: str = "0.0.1",
    dist_name: str | None = None,
) -> PreparedProductArtifact:
    """Build a PreparedProductArtifact with deterministic provenance."""
    if dist_name is None:
        dist_name = "zealfie-witness"
    remote = RemoteSource(owner="tinystork", repo="ZeWitness", ref="main")
    resolved = ResolvedSource(source=remote, commit_sha=VALID_SHA)
    wheel_path = Path(f"/fake/{product_id}-{version}.whl")
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version=version,
            path=wheel_path,
            size=100,
            sha256=WHEEL_SHA,
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


def _catalog() -> ProductCatalog:
    return ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=_EP,
            remote_source=RemoteSource(owner="tinystork", repo="ZeWitness", ref="main"),
        ),
    ))


class _FakeAbsentRt:
    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


def _fake_layout(tmp_path: Path) -> RuntimeLayout:
    return RuntimeLayout(root=tmp_path / "rt")


def _fake_store(tmp_path: Path) -> InstalledLockStore:
    return InstalledLockStore(_fake_layout(tmp_path))


def _make_runtime_lock() -> RuntimeLock:
    """A planning-time RuntimeLock with transient wheel inputs."""
    return RuntimeLock(
        locked={
            "zealfie-witness": LockedDependency(
                name="zealfie-witness",
                version="0.0.1",
                wheel_path=Path("/transient/zealfie-witness-0.0.1.whl"),
                size=12345,
                sha256=WHEEL_SHA,
                extras=frozenset({"gui"}),
                required_by=frozenset(),
            ),
            "requests": LockedDependency(
                name="requests",
                version="2.31.0",
                wheel_path=Path("/transient/requests-2.31.0.whl"),
                size=999,
                sha256=DEP_SHA,
                extras=frozenset(),
                required_by=frozenset({"zealfie-witness"}),
            ),
        },
        primary_names=frozenset({"zealfie-witness"}),
    )


def _make_installed_lock(*, version: str = "0.0.1") -> InstalledRuntimeLock:
    return InstalledRuntimeLock(
        primary_names=frozenset({"zealfie-witness"}),
        dependencies={
            "zealfie-witness": InstalledDependency(
                name="zealfie-witness",
                version=version,
                extras=("gui",),
                required_by=(),
                primary=True,
            ),
            "requests": InstalledDependency(
                name="requests",
                version="2.31.0",
                extras=(),
                required_by=("zealfie-witness",),
                primary=False,
            ),
        },
    )


# ---------------------------------------------------------------------------
# Pure unit tests — InstalledDependency validation / normalisation
# ---------------------------------------------------------------------------


def test_installed_dependency_sorts_extras_and_required_by() -> None:
    dep = InstalledDependency(
        name="requests",
        version="2.31.0",
        extras=("z", "a", "z"),
        required_by=("zealfie-witness", "alpha"),
    )
    assert dep.extras == ("a", "z")  # sorted + de-duplicated
    assert dep.required_by == ("alpha", "zealfie-witness")  # sorted


def test_installed_dependency_rejects_empty_name_or_version() -> None:
    with pytest.raises(ValueError):
        InstalledDependency(name="", version="1.0")
    with pytest.raises(ValueError):
        InstalledDependency(name="requests", version="  ")


# ---------------------------------------------------------------------------
# Pure unit tests — reduced lock derivation
# ---------------------------------------------------------------------------


def test_installed_lock_from_runtime_lock_drops_transient_fields() -> None:
    lock = installed_lock_from_runtime_lock(_make_runtime_lock())

    assert lock.primary_names == frozenset({"zealfie-witness"})
    assert set(lock.dependencies) == {"zealfie-witness", "requests"}

    primary = lock.dependencies["zealfie-witness"]
    assert primary.name == "zealfie-witness"
    assert primary.version == "0.0.1"
    assert primary.extras == ("gui",)
    assert primary.required_by == ()
    assert primary.primary is True

    dep = lock.dependencies["requests"]
    assert dep.version == "2.31.0"
    assert dep.primary is False
    assert dep.required_by == ("zealfie-witness",)

    # The reduced model has no wheel_path / size / sha256 attributes at all.
    assert not hasattr(primary, "wheel_path")
    assert not hasattr(primary, "size")
    assert not hasattr(primary, "sha256")


def test_installed_lock_from_runtime_lock_none_is_known_empty() -> None:
    lock = installed_lock_from_runtime_lock(None)
    assert lock.primary_names == frozenset()
    assert lock.dependencies == {}


# ---------------------------------------------------------------------------
# Pure unit tests — store read/write
# ---------------------------------------------------------------------------


def test_store_record_and_load_slot_roundtrip(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-abc123", _make_installed_lock())
    loaded = store.load_slot("rt-abc123")
    assert loaded is not None
    assert loaded.primary_names == frozenset({"zealfie-witness"})
    assert set(loaded.dependencies) == {"zealfie-witness", "requests"}
    assert loaded["requests"].version == "2.31.0"
    assert loaded["zealfie-witness"].primary is True


def test_store_serializes_sorted_and_no_transient_fields(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-abc123", _make_installed_lock())

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    slot = raw["slots"]["rt-abc123"]

    # primary_names sorted and deterministic.
    assert slot["primary_names"] == ["zealfie-witness"]

    entry = slot["dependencies"]["zealfie-witness"]
    assert entry["extras"] == ["gui"]  # sorted list
    # No transient install-input artifacts anywhere.
    assert "wheel_path" not in entry
    assert "size" not in entry
    assert "sha256" not in entry
    assert "path" not in entry


def test_store_missing_file_returns_unknown(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    assert store.load_slot("rt-anything") is None
    assert store.load_active() is None


def test_store_corrupt_file_returns_unknown(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("this is not json {{{", encoding="utf-8")
    assert store.load_slot("rt-anything") is None
    assert store.load_active() is None


def test_store_schema_version_mismatch_returns_unknown(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"schema_version": 999, "slots": {}}), encoding="utf-8"
    )
    assert store.load_slot("rt-anything") is None


def test_store_unknown_slot_returns_unknown(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-abc123", _make_installed_lock())
    assert store.load_slot("rt-other") is None


def test_store_record_empty_lock_is_known_empty_not_unknown(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-empty", InstalledRuntimeLock())
    loaded = store.load_slot("rt-empty")
    assert loaded is not None  # known-empty, not UNKNOWN
    assert loaded.primary_names == frozenset()
    assert loaded.dependencies == {}


def test_store_skips_malformed_entry(tmp_path: Path) -> None:
    """A structurally invalid dependency entry is skipped, never fabricated."""
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-bad": {
                    "primary_names": ["zealfie-witness"],
                    "dependencies": {
                        "good": {
                            "name": "good",
                            "version": "1.0",
                            "extras": [],
                            "required_by": [],
                            "primary": True,
                        },
                        "bad": {"name": "bad"},  # missing version
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    loaded = store.load_slot("rt-bad")
    assert loaded is not None
    assert set(loaded.dependencies) == {"good"}
    assert loaded["good"].version == "1.0"


# ---------------------------------------------------------------------------
# Pure unit tests — active pointer / rollback behaviour
# ---------------------------------------------------------------------------


def test_load_active_follows_pointer_and_rollback(tmp_path: Path) -> None:
    layout = _fake_layout(tmp_path)
    store = InstalledLockStore(layout)

    store.record("rt-old", _make_installed_lock(version="0.0.1"))
    store.record("rt-new", _make_installed_lock(version="0.0.2"))

    # No pointer yet → UNKNOWN.
    assert store.load_active() is None

    save_active_state(layout.active_pointer, "rt-new", "rt-old")
    active = store.load_active()
    assert active is not None
    assert active["zealfie-witness"].version == "0.0.2"

    # Rollback: pointer switches back to the previous slot.
    save_active_state(layout.active_pointer, "rt-old", None)
    active = store.load_active()
    assert active is not None
    assert active["zealfie-witness"].version == "0.0.1"


# ---------------------------------------------------------------------------
# Service tests — persistence ordering through install_prepared_product_deployment
# ---------------------------------------------------------------------------


def test_install_writes_installed_lock_after_success(tmp_path: Path, monkeypatch) -> None:
    """After a successful apply, the reduced installed lock is written for the
    new active slot, with no transient wheel path/size/sha256 fields."""
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")
    lock_store = _fake_store(tmp_path)
    lock = _make_runtime_lock()

    monkeypatch.setattr(
        svc_mod, "resolve_runtime_dependencies",
        lambda primary_wheels, *, wheelhouse: lock,
    )
    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-lock1111",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        installed_lock_store=lock_store,
    )

    result = service.install_prepared_product_deployment(
        [ppa], dependency_wheelhouse=tmp_path,
    )

    assert result.success is True
    assert lock_store.path.is_file(), "installed-lock file must be written after success"

    loaded = lock_store.load_slot("rt-lock1111")
    assert loaded is not None
    assert loaded.primary_names == frozenset({"zealfie-witness"})
    assert set(loaded.dependencies) == {"zealfie-witness", "requests"}
    assert loaded["requests"].required_by == ("zealfie-witness",)
    assert loaded["zealfie-witness"].primary is True
    assert loaded["requests"].primary is False

    raw = json.loads(lock_store.path.read_text(encoding="utf-8"))
    entry = raw["slots"]["rt-lock1111"]["dependencies"]["zealfie-witness"]
    assert "wheel_path" not in entry
    assert "size" not in entry
    assert "sha256" not in entry


def test_install_without_dependency_lock_records_known_empty(
    tmp_path: Path, monkeypatch,
) -> None:
    """No dependency wheelhouse → no resolved closure → a known-empty lock is
    recorded for the slot (not UNKNOWN)."""
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")
    lock_store = _fake_store(tmp_path)

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-nolock",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        installed_lock_store=lock_store,
    )

    result = service.install_prepared_product_deployment([ppa])
    assert result.success is True

    loaded = lock_store.load_slot("rt-nolock")
    assert loaded is not None  # known-empty, not UNKNOWN
    assert loaded.primary_names == frozenset()
    assert loaded.dependencies == {}


def test_apply_failure_does_not_write_lock(tmp_path: Path, monkeypatch) -> None:
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")
    lock_store = _fake_store(tmp_path)

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=False, reason="simulated apply failure",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        installed_lock_store=lock_store,
    )

    result = service.install_prepared_product_deployment([ppa])
    assert result.success is False
    assert not lock_store.path.is_file()


def test_compatibility_block_does_not_write_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    from zealfie.app import ProductCompatibilityBlockedError

    ppa = _make_ppa("zewitness")
    lock_store = _fake_store(tmp_path)

    blocked = CompatibilityReport(
        verdict=CompatibilityVerdict.INCOMPATIBLE,
        findings=(
            CompatibilityFinding(
                verdict=CompatibilityVerdict.INCOMPATIBLE,
                code="API_VERSION_MISMATCH",
                blocking=True,
            ),
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        installed_lock_store=lock_store,
    )
    monkeypatch.setattr(service, "evaluate_prepared_compatibility", lambda pa: blocked)

    with pytest.raises(ProductCompatibilityBlockedError):
        service.install_prepared_product_deployment([ppa])

    assert not lock_store.path.is_file()


def test_selection_failure_does_not_write_lock(
    tmp_path: Path, monkeypatch,
) -> None:
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")
    lock_store = _fake_store(tmp_path)

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-new",
        ),
    )

    sel_store = SelectionStore(path=tmp_path / "sel.toml")

    def _failing_select(product_id, *, catalog):
        raise RuntimeError("simulated selection persist failure")

    monkeypatch.setattr(sel_store, "select", _failing_select)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=sel_store,
        installed_lock_store=lock_store,
    )

    with pytest.raises(RuntimeError, match="selection persist failure"):
        service.install_prepared_product_deployment([ppa])

    assert not lock_store.path.is_file()


def test_installed_lock_store_disabled_for_fake_runtime_without_layout(
    tmp_path: Path, monkeypatch,
) -> None:
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test4444",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),  # no .layout attribute
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
    )

    assert service.installed_lock_store is None
    assert service.active_installed_lock() is None

    result = service.install_prepared_product_deployment([ppa])
    assert result.success is True
    assert service.active_installed_lock() is None


def test_installed_lock_write_failure_does_not_rollback_or_raise(
    tmp_path: Path, monkeypatch,
) -> None:
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")
    lock_store = _fake_store(tmp_path)

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-new",
        ),
    )

    def _failing_record(slot_id, lock):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(lock_store, "record", _failing_record)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        installed_lock_store=lock_store,
    )

    # Does not raise and does not report failure despite the write error.
    result = service.install_prepared_product_deployment([ppa])
    assert result.success is True
    # Readback stays safe (unknown), never invented.
    assert service.active_installed_lock() is None


def test_active_installed_lock_reads_active_slot_after_success(
    tmp_path: Path, monkeypatch,
) -> None:
    """The service readback follows the active pointer (mirrors provenance)."""
    import zealfie.app.service as svc_mod

    layout = _fake_layout(tmp_path)
    lock_store = InstalledLockStore(layout)
    ppa = _make_ppa("zewitness")

    def _fake_apply(plan, *, registry, runtime):
        save_active_state(layout.active_pointer, "rt-new", None)
        return DeploymentResult(success=True, active_slot_id="rt-new")

    monkeypatch.setattr(
        svc_mod, "resolve_runtime_dependencies",
        lambda primary_wheels, *, wheelhouse: _make_runtime_lock(),
    )
    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _fake_apply)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        installed_lock_store=lock_store,
    )

    result = service.install_prepared_product_deployment(
        [ppa], dependency_wheelhouse=tmp_path,
    )
    assert result.success is True

    active = service.active_installed_lock()
    assert active is not None
    assert active["zealfie-witness"].version == "0.0.1"
