"""Catalogue-evolution boundary test for an existing ZeAlfie installation.

ZA-ZC-ADMISSION: admitting ZeCalibrator as a 5th managed product must be a
purely additive catalogue change for an existing installation.  This test
builds a *pre-ZeCalibrator* user state on disk using the REAL stores, then
loads it with the REAL current catalogue and asserts the evolution is
non-destructive:

* the catalog now knows 5 products (ZeCalibrator present);
* the legacy 4-product selection is byte-identical (no re-write/reorder/drop);
* every legacy policy (channel / pin_sha / policy) is preserved exactly and
  no policy was added for ZeCalibrator;
* active provenance is unchanged (same slot, same per-product version /
  commit_sha / wheel_sha256);
* ZeCalibrator reports as KNOWN but NOT INSTALLED and UNMANAGED (reason
  NOT_INSTALLED), while the legacy products' observations are unchanged;
* NO automatic installation happened (no new provenance entry, no selection
  rewrite, no candidate slot, no policy rewrite);
* the loaded selection still validates against the (now 5-product) catalog.

This is a real state-loading/evolution-boundary test, not a constant
assertion: it exercises SelectionStore, ProductPolicyStore,
ProductProvenanceStore, InstalledLockStore, save_active_state, and a real
ZeAlfieService against a persisted legacy on-disk state.  The only injected
doubles are the runtime status (a READY status pointing at the prepared
slot) and the distribution probe (so no real subprocess is spawned).
"""

from __future__ import annotations

from pathlib import Path

from zealfie.app import (
    InstalledDependency,
    InstalledLockStore,
    InstalledRuntimeLock,
    ManagedStatus,
    ProductPolicy,
    ProductPolicyStore,
    ProductProvenance,
    ProductProvenanceStore,
    ProductStateReasonCode,
    SelectionStore,
    ZeAlfieService,
    validate_selection_against_catalog,
)
from zealfie.products.catalog import default_catalog
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import RuntimeReasonCode, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state


LEGACY_IDS = ("zesolver", "zemosaic", "zeseestarstacker", "zeanalyser")

VALID_SHA = "d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"  # 40 hex
WHEEL_SHA = "e" * 64
PIN_SHA = "a" * 40
SLOT_ID = "rt-legacy0001"


class _ReadyRuntime:
    """Minimal runtime double returning a fixed READY status."""

    def __init__(self, status: RuntimeStatus) -> None:
        self._status = status

    def status(self) -> RuntimeStatus:
        return self._status


def _not_installed_probe(runtime_python: str, distribution_name: str) -> dict:
    """Probe double: nothing is installed in the runtime slot."""
    return {
        "python_version": "3.13.5",
        "installed": False,
        "version": None,
        "entry_points": [],
    }


def _provenance(product_id: str, version: str) -> ProductProvenance:
    return ProductProvenance(
        product_id=product_id,
        version=version,
        source_owner="tinystork",
        source_repo=product_id.title(),
        requested_ref="main",
        commit_sha=VALID_SHA,
        wheel_sha256=WHEEL_SHA,
        channel="stable",
        policy="follow",
    )


def test_admitting_zecalibrator_preserves_existing_install(tmp_path: Path) -> None:
    """A pre-ZeCalibrator on-disk state survives the 5-product catalogue
    unchanged, with no automatic installation of the new product."""

    # ------------------------------------------------------------------
    # 1. Persist a legacy selection (the 4 legacy ids) via SelectionStore.
    # ------------------------------------------------------------------
    selection_store = SelectionStore(path=tmp_path / "desired-products.toml")
    catalog = default_catalog()
    for pid in LEGACY_IDS:
        selection_store.select(pid, catalog=catalog)
    selection_bytes_before = selection_store.path.read_bytes()

    # ------------------------------------------------------------------
    # 2. Persist a non-default product policy (zeseestarstacker→beta, and
    #    zesolver→pin with a 40-hex pin_sha) via ProductPolicyStore.
    # ------------------------------------------------------------------
    policy_store = ProductPolicyStore(path=tmp_path / "product-policy.toml")
    policy_store.set_policy(
        ProductPolicy(
            product_id="zeseestarstacker", channel="beta", policy="follow"
        )
    )
    policy_store.set_policy(
        ProductPolicy(product_id="zesolver", policy="pin", pin_sha=PIN_SHA)
    )
    policy_bytes_before = policy_store.path.read_bytes()

    # ------------------------------------------------------------------
    # 3. Persist a READY runtime slot with provenance for 2 products and
    #    an installed lock, all via the REAL stores.
    # ------------------------------------------------------------------
    layout = RuntimeLayout(root=tmp_path / "runtime")
    slot_path = layout.slot_path(SLOT_ID)
    (slot_path / "bin").mkdir(parents=True)
    (slot_path / "bin" / "python").write_text("#!/bin/sh\n")  # never executed

    save_active_state(layout.active_pointer, SLOT_ID, previous_slot_id=None)

    provenance_store = ProductProvenanceStore(layout)
    provenance_store.record(
        SLOT_ID,
        [
            _provenance("zesolver", "1.0.0"),
            _provenance("zemosaic", "2.0.0"),
        ],
    )
    provenance_bytes_before = provenance_store.path.read_bytes()

    installed_lock_store = InstalledLockStore(layout)
    installed_lock_store.record(
        SLOT_ID,
        InstalledRuntimeLock(
            primary_names=frozenset({"ZeSolver", "ZeMosaic"}),
            dependencies={
                "zesolver": InstalledDependency(
                    name="ZeSolver", version="1.0.0", primary=True
                ),
                "zemosaic": InstalledDependency(
                    name="ZeMosaic", version="2.0.0", primary=True
                ),
            },
        ),
    )
    lock_bytes_before = installed_lock_store.path.read_bytes()

    # ------------------------------------------------------------------
    # 4. Instantiate a real ZeAlfieService against that persisted state.
    # ------------------------------------------------------------------
    status = RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout.root,
        active_slot_id=SLOT_ID,
        active_path=slot_path,
        python_executable=slot_path / "bin" / "python",
        python_version="3.13.5",
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )
    service = ZeAlfieService(
        catalog=catalog,
        runtime=_ReadyRuntime(status),
        selection_store=selection_store,
        policy_store=policy_store,
        provenance_store=provenance_store,
        installed_lock_store=installed_lock_store,
    )

    # ------------------------------------------------------------------
    # 5. Assert every evolution invariant.
    # ------------------------------------------------------------------
    # -- Catalog has 5 products and ZeCalibrator is present.
    assert len(service.catalog) == 5
    assert "zecalibrator" in service.catalog
    assert service.catalog.get("zecalibrator").display_name == "ZeCalibrator"

    # -- Legacy selection is byte-identical (no re-write, reorder, drop).
    assert selection_store.path.read_bytes() == selection_bytes_before
    assert selection_store.selected_product_ids == tuple(sorted(LEGACY_IDS))

    # -- Every legacy policy is preserved exactly; no policy added for
    #    ZeCalibrator (it falls back to the stable/follow default).
    zsss = service.product_policy("zeseestarstacker")
    assert zsss.channel == "beta"
    assert zsss.policy == "follow"
    zesolver = service.product_policy("zesolver")
    assert zesolver.policy == "pin"
    assert zesolver.pin_sha == PIN_SHA
    zc = service.product_policy("zecalibrator")
    assert zc.channel == "stable"
    assert zc.policy == "follow"
    assert policy_store.path.read_bytes() == policy_bytes_before

    # -- Active provenance is unchanged (same slot, same per-product values).
    active_prov = service.active_provenance()
    assert set(active_prov) == {"zesolver", "zemosaic"}
    assert active_prov["zesolver"].version == "1.0.0"
    assert active_prov["zesolver"].commit_sha == VALID_SHA
    assert active_prov["zesolver"].wheel_sha256 == WHEEL_SHA
    assert active_prov["zemosaic"].version == "2.0.0"
    assert active_prov["zemosaic"].commit_sha == VALID_SHA
    assert active_prov["zemosaic"].wheel_sha256 == WHEEL_SHA
    assert provenance_store.path.read_bytes() == provenance_bytes_before
    assert installed_lock_store.path.read_bytes() == lock_bytes_before
    assert service.active_installed_lock() is not None
    assert service.active_installed_lock().primary_names == frozenset(
        {"ZeSolver", "ZeMosaic"}
    )

    # -- Product state: ZeCalibrator KNOWN / NOT INSTALLED / UNMANAGED;
    #    the legacy products' observations are unchanged (MANAGED, not
    #    installed in the probed slot).
    shell = service.collect_product_state(probe_fn=_not_installed_probe)
    by_id = {p.product_id: p for p in shell.products}
    assert len(shell.products) == 5

    zc_state = by_id["zecalibrator"]
    assert zc_state.known is True
    assert zc_state.installed is False
    assert zc_state.launchable is False
    assert zc_state.managed == ManagedStatus.UNMANAGED
    assert zc_state.reason_code == ProductStateReasonCode.NOT_INSTALLED

    for pid in LEGACY_IDS:
        legacy = by_id[pid]
        assert legacy.known is True
        assert legacy.installed is False
        assert legacy.managed == ManagedStatus.MANAGED
        assert legacy.reason_code == ProductStateReasonCode.NOT_INSTALLED

    # -- No automatic installation happened: selection/provenance/policy
    #    bytes unchanged and no candidate slot was created.
    assert selection_store.path.read_bytes() == selection_bytes_before
    assert policy_store.path.read_bytes() == policy_bytes_before
    assert provenance_store.path.read_bytes() == provenance_bytes_before
    assert installed_lock_store.path.read_bytes() == lock_bytes_before
    slot_entries = sorted(
        p.name for p in layout.slots.iterdir() if p.is_dir()
    )
    assert slot_entries == [SLOT_ID]
    assert "zecalibrator" not in active_prov

    # -- The loaded selection still validates against the 5-product catalog
    #    (no destructive migration, no unknown-id failure).
    validate_selection_against_catalog(
        catalog, selection_store.current_selection()
    )
