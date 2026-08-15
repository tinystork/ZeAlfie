"""Tests for M1-2E LOT E.1 — installed-product source provenance.

Covers both the pure provenance store (read/write/unknown/corrupt) and the
service-level persistence ordering through ``install_prepared_product_deployment``
with fake apply / fake runtime (no real venv, no real GitHub).

FAST: no ``zealfie_slow`` marker — no real wheel building, venv, or pip.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from zealfie.app import (
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    ProductProvenance,
    ProductProvenanceStore,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.components.model import EntryPointContract
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, ResolvedSource


# ---------------------------------------------------------------------------
# Constants / helpers
# ---------------------------------------------------------------------------

VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
OTHER_SHA = "e5b1f2a3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9"  # 40 hex
WHEEL_SHA = "a" * 64
OTHER_WHEEL_SHA = "b" * 64

_EP = (EntryPointContract("console_scripts", "zewitness"),)


def _make_ppa(
    product_id: str,
    *,
    version: str = "0.0.1",
    dist_name: str | None = None,
    commit_sha: str = VALID_SHA,
    owner: str = "tinystork",
    repo: str = "ZeWitness",
    ref: str = "main",
    wheel_sha256: str = WHEEL_SHA,
) -> PreparedProductArtifact:
    """Build a PreparedProductArtifact with deterministic provenance."""
    if dist_name is None:
        dist_name = "zealfie-witness"
    remote = RemoteSource(owner=owner, repo=repo, ref=ref)
    resolved = ResolvedSource(source=remote, commit_sha=commit_sha)
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
            sha256=wheel_sha256,
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


def _make_provenance(
    product_id: str = "zewitness",
    *,
    version: str = "0.0.1",
    commit_sha: str = VALID_SHA,
    wheel_sha256: str = WHEEL_SHA,
    **kwargs,
) -> ProductProvenance:
    defaults = dict(
        product_id=product_id,
        version=version,
        source_owner="tinystork",
        source_repo="ZeWitness",
        requested_ref="main",
        commit_sha=commit_sha,
        wheel_sha256=wheel_sha256,
    )
    defaults.update(kwargs)
    return ProductProvenance(**defaults)


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


def _fake_store(tmp_path: Path) -> ProductProvenanceStore:
    return ProductProvenanceStore(_fake_layout(tmp_path))


# ---------------------------------------------------------------------------
# Pure unit tests — ProductProvenance validation
# ---------------------------------------------------------------------------


def test_product_provenance_valid_roundtrip() -> None:
    p = _make_provenance()
    assert p.product_id == "zewitness"
    assert p.version == "0.0.1"
    assert p.commit_sha == VALID_SHA
    assert p.wheel_sha256 == WHEEL_SHA


def test_product_provenance_normalizes_sha_case() -> None:
    p = _make_provenance(commit_sha=VALID_SHA.upper())
    assert p.commit_sha == VALID_SHA  # lowercased


def test_product_provenance_rejects_bad_commit_sha() -> None:
    with pytest.raises(ValueError):
        _make_provenance(commit_sha="short")


def test_product_provenance_rejects_bad_wheel_sha() -> None:
    with pytest.raises(ValueError):
        _make_provenance(wheel_sha256="not-hex")


def test_product_provenance_rejects_empty_field() -> None:
    with pytest.raises(ValueError):
        _make_provenance(source_owner="")


# ---------------------------------------------------------------------------
# Pure unit tests — store read/write
# ---------------------------------------------------------------------------


def test_store_record_and_load_slot(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-abc123", [_make_provenance()])
    loaded = store.load_slot("rt-abc123")
    assert "zewitness" in loaded
    assert loaded["zewitness"].commit_sha == VALID_SHA


def test_store_missing_file_returns_empty(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    assert store.load_slot("rt-anything") == {}
    assert store.load_active() == {}


def test_store_corrupt_file_returns_empty(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("this is not json {{{", encoding="utf-8")
    assert store.load_slot("rt-anything") == {}
    assert store.load_active() == {}


def test_store_schema_version_mismatch_returns_empty(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({"schema_version": 999, "slots": {}}), encoding="utf-8"
    )
    assert store.load_slot("rt-anything") == {}


def test_store_skips_malformed_entry(tmp_path: Path) -> None:
    """A structurally invalid entry is skipped, never raised, never invented."""
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-bad": {
                    "zewitness": {"commit_sha": "not-a-sha"},
                },
            },
        }),
        encoding="utf-8",
    )
    assert store.load_slot("rt-bad") == {}
    assert store.product_provenance("zewitness") is None


def test_store_unknown_slot_and_product(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-abc123", [_make_provenance()])
    assert store.load_slot("rt-other") == {}
    assert store.load_slot("rt-abc123").get("nope") is None


def test_store_record_duplicate_product_raises(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    with pytest.raises(ValueError, match="duplicate product_id"):
        store.record("rt-abc123", [_make_provenance(), _make_provenance()])


def test_store_record_replaces_slot(tmp_path: Path) -> None:
    """Re-recording a slot replaces its product map (immutable full-state slots)."""
    store = _fake_store(tmp_path)
    store.record("rt-abc123", [_make_provenance(version="0.0.1")])
    store.record("rt-abc123", [_make_provenance(version="0.0.2")])
    loaded = store.load_slot("rt-abc123")
    assert set(loaded) == {"zewitness"}
    assert loaded["zewitness"].version == "0.0.2"


def test_store_load_active_reads_pointer(tmp_path: Path) -> None:
    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    store.record("rt-abc123", [_make_provenance()])

    # No pointer yet → empty.
    assert store.load_active() == {}

    save_active_state(layout.active_pointer, "rt-abc123", None)
    active = store.load_active()
    assert active["zewitness"].commit_sha == VALID_SHA
    assert store.product_provenance("zewitness") is not None


# ---------------------------------------------------------------------------
# Service tests — persistence ordering through install_prepared_product_deployment
# ---------------------------------------------------------------------------


def test_provenance_persisted_after_activation_success(
    tmp_path: Path, monkeypatch,
) -> None:
    """After a successful apply, provenance is persisted keyed by the new
    active slot id, with all required fields from the prepared artifact."""
    import zealfie.app.service as svc_mod

    ppa = _make_ppa("zewitness")
    store = _fake_store(tmp_path)
    sel_path = tmp_path / "desired-products.toml"

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test1111",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=sel_path),
        provenance_store=store,
    )

    result = service.install_prepared_product_deployment([ppa])

    assert result.success is True
    assert store.path.is_file(), "provenance file must be written after success"

    loaded = store.load_slot("rt-test1111")
    assert "zewitness" in loaded

    prov = loaded["zewitness"]
    assert prov.product_id == "zewitness"
    assert prov.version == "0.0.1"
    assert prov.source_owner == "tinystork"
    assert prov.source_repo == "ZeWitness"
    assert prov.requested_ref == "main"
    assert prov.commit_sha == VALID_SHA
    assert prov.wheel_sha256 == WHEEL_SHA


def test_provenance_fields_sha_version_source_correct(
    tmp_path: Path, monkeypatch,
) -> None:
    """version/SHA/source fields are taken from prepared artifacts, not
    from user desire or invented values."""
    import zealfie.app.service as svc_mod

    ppa = _make_ppa(
        "zewitness",
        version="2.3.4",
        commit_sha=OTHER_SHA,
        owner="someorg",
        repo="SomeRepo",
        ref="release-2.x",
        wheel_sha256=OTHER_WHEEL_SHA,
    )
    store = _fake_store(tmp_path)

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test2222",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    service.install_prepared_product_deployment([ppa])

    prov = store.load_slot("rt-test2222")["zewitness"]
    assert prov.version == "2.3.4"
    assert prov.source_owner == "someorg"
    assert prov.source_repo == "SomeRepo"
    assert prov.requested_ref == "release-2.x"
    assert prov.commit_sha == OTHER_SHA
    assert prov.wheel_sha256 == OTHER_WHEEL_SHA


def test_apply_failure_old_provenance_unchanged(
    tmp_path: Path, monkeypatch,
) -> None:
    """A failed apply must not write any provenance and must leave old
    active provenance authoritative."""
    import zealfie.app.service as svc_mod

    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    store.record("rt-old", [_make_provenance(version="0.0.1")])
    save_active_state(layout.active_pointer, "rt-old", None)

    ppa_v2 = _make_ppa("zewitness", version="0.0.2", commit_sha=OTHER_SHA)

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
        provenance_store=store,
    )

    result = service.install_prepared_product_deployment([ppa_v2])

    assert result.success is False
    # Old provenance still authoritative.
    assert store.load_slot("rt-old")["zewitness"].version == "0.0.1"
    # No provenance was written for the (failed) candidate slot.
    assert store.load_slot("rt-test9999") == {}
    # No new slot appeared anywhere in the store.
    all_slots = store._load_all()
    assert set(all_slots) == {"rt-old"}


def test_runtime_without_provenance_reads_none(
    tmp_path: Path, monkeypatch,
) -> None:
    """A runtime with no provenance returns safe unknown/None — never an
    invented SHA."""
    import zealfie.app.service as svc_mod

    store = _fake_store(tmp_path)  # empty, no pointer

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-test3333",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    # Before any install: no provenance.
    assert service.product_provenance("zewitness") is None
    assert service.active_provenance() == {}

    # Even without an active pointer, readback stays safe.
    assert service.product_provenance("nope") is None


def test_provenance_corresponds_to_active_runtime_not_failed_candidate(
    tmp_path: Path, monkeypatch,
) -> None:
    """After a failed candidate activation, readback still describes the
    currently active runtime (old slot), never the failed candidate."""
    import zealfie.app.service as svc_mod

    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)

    # Old active runtime with v1 provenance.
    store.record("rt-old", [_make_provenance(version="0.0.1", commit_sha=VALID_SHA)])
    save_active_state(layout.active_pointer, "rt-old", None)

    # Candidate is v2 with a different SHA.
    ppa_v2 = _make_ppa("zewitness", version="0.0.2", commit_sha=OTHER_SHA)

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=False, reason="candidate activation failed",
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    result = service.install_prepared_product_deployment([ppa_v2])
    assert result.success is False

    # Readback follows the active pointer → old slot, not the candidate.
    active = service.active_provenance()
    assert "zewitness" in active
    assert active["zewitness"].version == "0.0.1"
    assert active["zewitness"].commit_sha == VALID_SHA
    assert service.product_provenance("zewitness").commit_sha == VALID_SHA


def test_provenance_corresponds_to_new_active_runtime_after_success(
    tmp_path: Path, monkeypatch,
) -> None:
    """After a successful activation that also updates the active pointer,
    readback returns the NEW slot's provenance."""
    import zealfie.app.service as svc_mod

    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)

    store.record("rt-old", [_make_provenance(version="0.0.1", commit_sha=VALID_SHA)])
    save_active_state(layout.active_pointer, "rt-old", None)

    ppa_v2 = _make_ppa("zewitness", version="0.0.2", commit_sha=OTHER_SHA)

    # Fake apply simulates the real engine: it also switches the pointer.
    def _fake_apply(plan, *, registry, runtime):
        save_active_state(layout.active_pointer, "rt-new", "rt-old")
        return DeploymentResult(success=True, active_slot_id="rt-new")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _fake_apply)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    result = service.install_prepared_product_deployment([ppa_v2])
    assert result.success is True

    active = service.active_provenance()
    assert active["zewitness"].version == "0.0.2"
    assert active["zewitness"].commit_sha == OTHER_SHA


def test_selection_persist_failure_does_not_write_provenance(
    tmp_path: Path, monkeypatch,
) -> None:
    """If selection persistence fails after a successful apply, provenance
    is NOT written — old active provenance remains authoritative."""
    import zealfie.app.service as svc_mod

    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    store.record("rt-old", [_make_provenance(version="0.0.1")])
    save_active_state(layout.active_pointer, "rt-old", None)

    ppa_v2 = _make_ppa("zewitness", version="0.0.2", commit_sha=OTHER_SHA)

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
        provenance_store=store,
    )

    with pytest.raises(RuntimeError, match="selection persist failure"):
        service.install_prepared_product_deployment([ppa_v2])

    # Provenance untouched: only the old slot exists.
    assert set(store._load_all()) == {"rt-old"}
    assert store.load_slot("rt-old")["zewitness"].version == "0.0.1"


def test_provenance_store_disabled_for_fake_runtime_without_layout(
    tmp_path: Path, monkeypatch,
) -> None:
    """A service built on a fake runtime without a layout has provenance
    disabled (store is None) and never writes to the real user runtime."""
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

    assert service.provenance_store is None

    result = service.install_prepared_product_deployment([ppa])
    assert result.success is True
    assert service.active_provenance() == {}
    assert service.product_provenance("zewitness") is None


def test_provenance_write_failure_does_not_rollback_or_raise(
    tmp_path: Path, monkeypatch,
) -> None:
    """A provenance write failure after a successful activation is logged
    and swallowed: the install still reports success and the runtime stays
    active; readback returns safe unknown (never invented)."""
    import zealfie.app.service as svc_mod

    layout = _fake_layout(tmp_path)
    store = ProductProvenanceStore(layout)
    ppa = _make_ppa("zewitness")

    monkeypatch.setattr(
        svc_mod, "apply_deployment_plan",
        lambda plan, *, registry, runtime: DeploymentResult(
            success=True, active_slot_id="rt-new",
        ),
    )

    def _failing_record(slot_id, entries):
        raise OSError("simulated disk failure")

    monkeypatch.setattr(store, "record", _failing_record)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "sel.toml"),
        provenance_store=store,
    )

    # Does not raise and does not report failure despite the write error.
    result = service.install_prepared_product_deployment([ppa])
    assert result.success is True

    # Readback stays safe (unknown), never invented.
    assert service.product_provenance("zewitness") is None
    assert service.active_provenance() == {}


# ---------------------------------------------------------------------------
# M1-2F Phase 4 — discovery-policy provenance metadata (backward-compatible)
# ---------------------------------------------------------------------------


def test_old_v1_provenance_loads_policy_unknown(tmp_path: Path) -> None:
    """A pre-Phase-4 v1 entry (no channel/policy/pin_sha) loads with None
    policy metadata and remains fully usable for KEEP exact SHA."""
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-old": {
                    "zewitness": {
                        "version": "0.0.1",
                        "source_owner": "tinystork",
                        "source_repo": "ZeWitness",
                        "requested_ref": "main",
                        "commit_sha": VALID_SHA,
                        "wheel_sha256": WHEEL_SHA,
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    loaded = store.load_slot("rt-old")
    prov = loaded["zewitness"]
    assert prov.commit_sha == VALID_SHA  # exact SHA preserved for KEEP
    assert prov.channel is None
    assert prov.policy is None
    assert prov.pin_sha is None


def test_follow_provenance_roundtrip_with_channel(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-1", [
        _make_provenance(channel="beta", policy="follow"),
    ])
    loaded = store.load_slot("rt-1")["zewitness"]
    assert loaded.channel == "beta"
    assert loaded.policy == "follow"
    assert loaded.pin_sha is None
    assert loaded.requested_ref == "main"
    assert loaded.commit_sha == VALID_SHA

    # Serialized form omits pin_sha and records channel/policy distinctly.
    raw = json.loads(store.path.read_text(encoding="utf-8"))
    entry = raw["slots"]["rt-1"]["zewitness"]
    assert entry["channel"] == "beta"
    assert entry["policy"] == "follow"
    assert "pin_sha" not in entry


def test_pin_provenance_roundtrip_with_pin_sha(tmp_path: Path) -> None:
    store = _fake_store(tmp_path)
    store.record("rt-1", [
        _make_provenance(policy="pin", pin_sha=VALID_SHA),
    ])
    loaded = store.load_slot("rt-1")["zewitness"]
    assert loaded.policy == "pin"
    assert loaded.pin_sha == VALID_SHA
    assert loaded.channel is None

    raw = json.loads(store.path.read_text(encoding="utf-8"))
    entry = raw["slots"]["rt-1"]["zewitness"]
    assert entry["policy"] == "pin"
    assert entry["pin_sha"] == VALID_SHA
    assert "channel" not in entry


def test_pin_provenance_requires_pin_sha() -> None:
    with pytest.raises(ValueError, match="pin_sha"):
        _make_provenance(policy="pin")  # missing pin_sha
    with pytest.raises(ValueError, match="pin_sha"):
        _make_provenance(policy="pin", pin_sha="not-hex")


def test_follow_provenance_requires_channel() -> None:
    with pytest.raises(ValueError, match="channel"):
        _make_provenance(policy="follow")  # missing channel


def test_unknown_policy_value_is_rejected() -> None:
    with pytest.raises(ValueError, match="policy"):
        _make_provenance(policy="floating")


def test_corrupt_policy_entry_reads_unknown_safe(tmp_path: Path) -> None:
    """A provenance entry with an inconsistent policy (pin without a valid
    pin_sha) is skipped entirely — readback is UNKNOWN, never a fabricated
    SHA or policy."""
    store = _fake_store(tmp_path)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text(
        json.dumps({
            "schema_version": 1,
            "slots": {
                "rt-bad": {
                    "zewitness": {
                        "version": "0.0.1",
                        "source_owner": "tinystork",
                        "source_repo": "ZeWitness",
                        "requested_ref": "main",
                        "commit_sha": VALID_SHA,
                        "wheel_sha256": WHEEL_SHA,
                        "policy": "pin",
                        "pin_sha": "not-hex",
                    },
                },
            },
        }),
        encoding="utf-8",
    )
    assert store.load_slot("rt-bad") == {}
    assert store.product_provenance("zewitness") is None
