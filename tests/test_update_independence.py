"""Update independence + honest progress wording tests (ZA-M1-3A.3 LOT E+F).

F (independence): updating ONE managed product must preserve every OTHER
managed product at its exact installed identity (commit SHA / wheel
digest / version).  The full-state transaction re-materializes KEEP
products from active provenance - it must NEVER pull a second product's
newer release.

E (honest UX): progress messages must never qualify a preserved (KEEP)
product as "Updating" or "Installing".  The update target carries
``Updating <old> -> <new>`` when both versions are known; a KEEP product
that was not served by the artifact cache is announced as
``Reacquiring <display> <version> for runtime rebuild``.

All tests are FAST and hermetic: faked resolver/fetcher/prepare/apply,
no network, no venv, no pip, no subprocess.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.app import (
    InstallProgress,
    PreparedProductArtifact,
    ProductCatalog,
    ProductDescriptor,
    ProductPolicyStore,
    ProductProvenance,
    ProductProvenanceStore,
    SelectionStore,
    UpdateStatus,
    ZeAlfieService,
)
from zealfie.compatibility import CompatibilityReport, CompatibilityVerdict
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.layout import RuntimeLayout
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.runtime.state import save_active_state
from zealfie.sources import RemoteSource, ResolvedSource


# Distinct 40-hex commit SHAs and 64-hex wheel digests.
SHA_A = "abcdef0123456789abcdef0123456789abcdef01"  # zesolver installed
SHA_B = "bcdef0123456789abcdef0123456789abcdef012"  # zemosaic installed
SHA_A2 = "cdef0123456789abcdef0123456789abcdef0123"  # zesolver newer (remote)
SHA_B2 = "def0123456789abcdef0123456789abcdef01234"  # zemosaic newer (remote)
WHEEL_A = "a" * 64
WHEEL_B = "b" * 64

VERSION_A = "1.0.0"   # zesolver installed
VERSION_A2 = "1.1.1"  # zesolver remote (newer)
VERSION_B = "2.0.0"   # zemosaic installed
VERSION_B2 = "2.1.0"  # zemosaic remote (newer) - must NOT be pulled

_EP = (EntryPointContract("console_scripts", "zesolver"),)


def _catalog() -> ProductCatalog:
    return ProductCatalog((
        ProductDescriptor(
            product_id="zesolver",
            display_name="ZeSolver",
            distribution_name="zealfie-solver",
            launch_entry_points=(EntryPointContract("console_scripts", "zesolver"),),
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork", repo="ZeSolver", ref="main",
            ),
            channel_refs=(("stable", "main"),),
        ),
        ProductDescriptor(
            product_id="zemosaic",
            display_name="ZeMosaic",
            distribution_name="zealfie-mosaic",
            launch_entry_points=(EntryPointContract("console_scripts", "zemosaic"),),
            required_extras=(),
            remote_source=RemoteSource(
                owner="tinystork", repo="ZeMosaic", ref="main",
            ),
            channel_refs=(("stable", "main"),),
        ),
    ))


def _prov(
    product_id: str,
    *,
    version: str,
    commit_sha: str,
    wheel_sha256: str,
) -> ProductProvenance:
    repo = "ZeSolver" if product_id == "zesolver" else "ZeMosaic"
    return ProductProvenance(
        product_id=product_id,
        version=version,
        source_owner="tinystork",
        source_repo=repo,
        requested_ref="main",
        commit_sha=commit_sha,
        wheel_sha256=wheel_sha256,
    )


def _resolver(shas: dict[tuple[str, str, str], str]):
    """Fake resolver keyed by (owner, repo, ref); records every call."""
    calls: list[tuple[str, str, str]] = []

    def resolve(owner: str, repo: str, ref: str) -> str:
        calls.append((owner, repo, ref))
        return shas[(owner, repo, ref)]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


class _FakeAbsentRt:
    """Fake runtime without a layout → provenance is only ever injected."""

    def status(self):
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


def _make_ppa(
    tmp_path: Path,
    product_id: str,
    *,
    commit_sha: str,
    version: str,
) -> PreparedProductArtifact:
    repo = "ZeSolver" if product_id == "zesolver" else "ZeMosaic"
    distribution = (
        "zealfie-solver" if product_id == "zesolver" else "zealfie-mosaic"
    )
    remote = RemoteSource(owner="tinystork", repo=repo, ref="main")
    resolved = ResolvedSource(source=remote, commit_sha=commit_sha)
    wheel = tmp_path / f"{product_id}-{version}.whl"
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version=version,
            path=wheel,
            size=100,
            sha256="f" * 64,
            distribution_name=distribution,
            wheel_version=version,
        ),
    )


# ---------------------------------------------------------------------------
# F + E: full update_product scenario with message collection
# ---------------------------------------------------------------------------


def test_update_zesolver_preserves_zemosaic_exact_identity_and_honest_messages(
    tmp_path: Path, monkeypatch,
) -> None:
    """Updating zesolver re-materializes zemosaic from its exact installed
    provenance (commit SHA_B) — never from the newer remote release (B2) —
    and no emitted progress message qualifies the KEEP product as
    "Updating" or "Installing"."""
    import zealfie.app.service as svc_mod

    layout = RuntimeLayout(root=tmp_path / "rt")
    prov_store = ProductProvenanceStore(layout)
    prov_store.record(
        "rt-1",
        [
            _prov("zesolver", version=VERSION_A, commit_sha=SHA_A,
                  wheel_sha256=WHEEL_A),
            _prov("zemosaic", version=VERSION_B, commit_sha=SHA_B,
                  wheel_sha256=WHEEL_B),
        ],
    )
    save_active_state(layout.active_pointer, "rt-1", None)

    service = ZeAlfieService(
        catalog=_catalog(),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        provenance_store=prov_store,
        policy_store=ProductPolicyStore(path=tmp_path / "policy.toml"),
    )

    # --- Fakes: target resolves to A2/1.1.1; KEEP echoes its exact SHA ----
    keep_calls: list[tuple[str, str]] = []
    target_calls: list[str] = []

    def _fake_prepare_target(
        product_id, *, resolver, fetcher, work_root,
        progress_callback=None, source_ref=None,
    ):
        target_calls.append(product_id)
        return _make_ppa(
            tmp_path, product_id, commit_sha=SHA_A2, version=VERSION_A2,
        )

    def _fake_prepare_keep(
        product_id, *, commit_sha, source_owner, source_repo, requested_ref,
        fetcher, work_root, progress_callback=None,
    ):
        keep_calls.append((product_id, commit_sha))
        assert product_id == "zemosaic"
        return _make_ppa(
            tmp_path, product_id, commit_sha=commit_sha, version=VERSION_B,
        )

    monkeypatch.setattr(service, "prepare_product_artifact", _fake_prepare_target)
    monkeypatch.setattr(service, "prepare_product_artifact_at_commit", _fake_prepare_keep)
    monkeypatch.setattr(
        service, "evaluate_prepared_compatibility",
        lambda prepared_artifacts: CompatibilityReport(
            verdict=CompatibilityVerdict.COMPATIBLE, findings=(),
        ),
    )

    applied_plans: list = []

    def _fake_apply(plan, *, registry, runtime, progress_callback=None):
        applied_plans.append(plan)
        return DeploymentResult(success=True, active_slot_id="rt-2")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _fake_apply)

    # --- Act: update zesolver only ---------------------------------------
    resolver = _resolver({
        ("tinystork", "ZeSolver", "main"): SHA_A2,
        ("tinystork", "ZeMosaic", "main"): SHA_B2,
    })
    events: list[InstallProgress] = []

    result = service.update_product(
        "zesolver",
        resolver=resolver,
        fetcher=lambda owner, repo, sha: b"",
        work_root=tmp_path / "work",
        dependency_wheelhouse=Path("/nonexistent-wheelhouse"),
        progress_callback=events.append,
    )

    assert result.success is True

    # The engine's activation would flip the pointer; simulate it for the
    # read-back below (the faked apply performs no filesystem mutation).
    save_active_state(layout.active_pointer, result.active_slot_id, None)

    # --- KEEP was re-materialized from its exact installed commit SHA -----
    assert keep_calls == [("zemosaic", SHA_B)], (
        "KEEP product must be rebuilt from its exact installed commit SHA"
    )
    assert target_calls == ["zesolver"]

    # --- Plan carries the service-level origins ---------------------------
    assert len(applied_plans) == 1
    origins = {
        dc.component_id: dc.origin
        for dc in applied_plans[0].desired_state.components
    }
    assert origins == {"zemosaic": "keep", "zesolver": "update"}

    # --- Active provenance independence -----------------------------------
    active = service.active_provenance()
    assert active["zesolver"].commit_sha == SHA_A2
    assert active["zesolver"].version == VERSION_A2
    assert active["zemosaic"].commit_sha == SHA_B, (
        "zemosaic provenance must stay at its installed commit — "
        "the newer remote release (B2) must NOT be pulled"
    )
    assert active["zemosaic"].version == VERSION_B

    # --- zemosaic is still update-eligible ---------------------------------
    check = service.check_product_update("zemosaic", resolver=resolver)
    assert check.status is UpdateStatus.UPDATE_AVAILABLE
    assert check.installed_commit_sha == SHA_B
    assert check.latest_commit_sha == SHA_B2

    # --- Honest messages --------------------------------------------------
    messages = [e.message for e in events]
    assert "Updating ZeSolver 1.0.0 -> 1.1.1" in messages, (
        f"update target message missing in {messages!r}"
    )
    assert "Reacquiring ZeMosaic 2.0.0 for runtime rebuild" in messages, (
        f"KEEP re-acquisition message missing in {messages!r}"
    )
    mosaic_updating = [
        m for m in messages
        if "updat" in m.lower() and "mosaic" in m.lower()
    ]
    assert mosaic_updating == [], (
        f"KEEP product must never be qualified as updating: {messages!r}"
    )
    mosaic_installing = [
        m for m in messages
        if "installing" in m.lower() and "mosaic" in m.lower()
    ]
    assert mosaic_installing == [], (
        f"KEEP product must never be qualified as installing: {messages!r}"
    )

    # The resolver was only ever consulted for the update preflight and the
    # post-update check of zemosaic — never during KEEP re-materialization.
    assert resolver.calls == [
        ("tinystork", "ZeSolver", "main"),
        ("tinystork", "ZeMosaic", "main"),
    ]


# ---------------------------------------------------------------------------
# E: engine install-loop wording (pure, no venv)
# ---------------------------------------------------------------------------


def _component_message_case():
    from zealfie.runtime.deployment import _component_install_message
    from zealfie.runtime.planning import (
        DeploymentAction,
        DeploymentReasonCode,
        DeploymentStep,
        DesiredComponent,
    )

    def artifact(component_id: str, version: str) -> VerifiedArtifact:
        return VerifiedArtifact(
            component_id=component_id,
            version=version,
            path=Path(f"/fake/{component_id}-{version}.whl"),
            size=100,
            sha256="e" * 64,
            distribution_name=f"zealfie-{component_id}",
            wheel_version=version,
        )

    def definition(component_id: str, display_name: str) -> ComponentDefinition:
        return ComponentDefinition(
            component_id=component_id,
            display_name=display_name,
            distribution_name=f"zealfie-{component_id}",
            launch_entry_points=(),
            required_extras=(),
        )

    return _component_install_message, artifact, definition, (
        DeploymentAction,
        DeploymentReasonCode,
        DeploymentStep,
        DesiredComponent,
    )


def test_component_install_message_honest_origins() -> None:
    """The engine install-loop message depends on the component origin:
    keep -> Preserving, update -> Updating (old->new when known),
    install -> Installing.  Never 'Updating'/'Installing' for a KEEP."""
    (
        message_fn, artifact, definition,
        (DeploymentAction, DeploymentReasonCode, DeploymentStep,
         DesiredComponent),
    ) = _component_message_case()

    # KEEP → Preserving.
    keep = DesiredComponent("zemosaic", "4.6.0", artifact("zemosaic", "4.6.0"),
                            origin="keep")
    assert message_fn(keep, definition("zemosaic", "ZeMosaic"), None) == (
        "Preserving ZeMosaic 4.6.0"
    )

    # UPDATE with observed old version → old -> new.
    update = DesiredComponent("zesolver", "1.1.1",
                              artifact("zesolver", "1.1.1"), origin="update")
    step = DeploymentStep(
        component_id="zesolver",
        desired_version="1.1.1",
        artifact=artifact("zesolver", "1.1.1"),
        action=DeploymentAction.INSTALL,
        current_version="1.1.0",
        reason_code=DeploymentReasonCode.VERSION_MISMATCH,
        reason="version mismatch",
    )
    assert message_fn(update, definition("zesolver", "ZeSolver"), step) == (
        "Updating ZeSolver 1.1.0 -> 1.1.1"
    )

    # UPDATE without an observed old version (absent runtime) → version only.
    assert message_fn(update, definition("zesolver", "ZeSolver"), None) == (
        "Updating ZeSolver 1.1.1"
    )

    # INSTALL → Installing with version.
    install = DesiredComponent("zeanalyser", "3.3.1",
                               artifact("zeanalyser", "3.3.1"), origin="install")
    assert message_fn(install, definition("zeanalyser", "ZeAnalyser"), None) == (
        "Installing ZeAnalyser 3.3.1"
    )

    # Fallback to component id when no definition is available.
    assert message_fn(install, None, None) == "Installing zeanalyser 3.3.1"


def test_desired_component_origin_defaults_and_validation() -> None:
    """DesiredComponent.origin defaults to 'install' (backward compatible)
    and rejects unknown values."""
    from zealfie.runtime.planning import DesiredComponent

    artifact = VerifiedArtifact(
        component_id="zesolver",
        version="1.0.0",
        path=Path("/fake/zesolver-1.0.0.whl"),
        size=100,
        sha256="e" * 64,
        distribution_name="zealfie-solver",
        wheel_version="1.0.0",
    )

    assert DesiredComponent("zesolver", "1.0.0", artifact).origin == "install"
    for origin in ("keep", "install", "update"):
        dc = DesiredComponent("zesolver", "1.0.0", artifact, origin=origin)
        assert dc.origin == origin
    with pytest.raises(ValueError):
        DesiredComponent("zesolver", "1.0.0", artifact, origin="replace")
