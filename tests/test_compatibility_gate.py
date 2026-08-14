"""Phase 2B — compatibility gate integration tests.

These tests verify that the product-agnostic interoperability evaluator is
wired into the prepared-artifact deployment path *before* activation:

1. A blocking incompatibility raises ``ProductCompatibilityBlockedError``
   and prevents ``apply_deployment_plan`` / runtime mutation / selection
   persistence.
2. A degraded (optional provider absent) candidate set does **not** block.
3. The evaluator receives the **full** prepared product candidate set in a
   multi-product deployment (not just the target product).
4. The raised error message carries stable reason codes
   (``API_VERSION_MISMATCH``, ``PROVIDER_METADATA_UNAVAILABLE``, etc.)
   without depending on long prose.

Fixture wheels are minimal ZIP archives (``.dist-info/METADATA`` +
``zesoftware_interop.json``); no product code is imported.  Product names
appear only as fixture data strings, exactly as permitted by the mission.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from pathlib import Path

import pytest

import zealfie.app.service as svc_mod
from zealfie.app import (
    PreparedProductArtifact,
    ProductCatalog,
    ProductCompatibilityBlockedError,
    ProductDescriptor,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.compatibility import (
    CompatibilityReport,
    CompatibilityVerdict,
)
from zealfie.components.model import EntryPointContract
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime.model import DeploymentResult, RuntimeState, RuntimeStatus
from zealfie.sources import RemoteSource, ResolvedSource


# ---------------------------------------------------------------------------
# Fixture-wheel helpers (mirrors tests/test_compatibility.py)
# ---------------------------------------------------------------------------

_ALL_CAPS = ["near_solve", "blind_solve", "wcs_write", "gpu", "cancel"]


def _provider_json(**overrides: object) -> dict:
    data = {
        "schema": "zesoftware.interop.v1",
        "product_id": "zesolver",
        "distribution_name": "ZeSolver",
        "provides": [
            {
                "api_module": "zesolver.api.v1",
                "api_version": "1.0",
                "capabilities": list(_ALL_CAPS),
            }
        ],
        "consumes": [],
    }
    data.update(overrides)
    return data


def _consumer_json(**overrides: object) -> dict:
    data = {
        "schema": "zesoftware.interop.v1",
        "product_id": "zemosaic",
        "distribution_name": "ZeMosaic",
        "provides": [],
        "consumes": [
            {
                "provider_product_id": "zesolver",
                "provider_distribution_name": "ZeSolver",
                "optional": True,
                "api_module": "zesolver.api.v1",
                "api_version": ">=1,<2",
                "required_capabilities": ["wcs_write"],
                "any_of_capabilities": [
                    {
                        "id": "solve_backend",
                        "capabilities": ["near_solve", "blind_solve"],
                        "required": True,
                    }
                ],
                "optional_capabilities": ["cancel", "gpu"],
            }
        ],
    }
    data.update(overrides)
    return data


def _build_wheel(
    tmp_path: Path,
    *,
    name: str,
    version: str,
    top_level: str,
    interop: dict | None = None,
) -> Path:
    """Build a minimal valid wheel ZIP without executing any product code."""
    dist_info = f"{name.replace(' ', '_').lower()}-{version}.dist-info"
    wheel_name = f"{name.replace(' ', '_').lower()}-{version}-py3-none-any.whl"
    wheel_path = Path(tmp_path) / wheel_name
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            f"{dist_info}/METADATA",
            f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n",
        )
        zf.writestr(
            f"{dist_info}/WHEEL",
            "Wheel-Version: 1.0\nGenerator: test\n"
            "Root-Is-Purelib: true\nTag: py3-none-any\n",
        )
        if interop is not None:
            zf.writestr(
                f"{top_level}/zesoftware_interop.json", json.dumps(interop)
            )
    return wheel_path


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _make_ppa(
    product_id: str,
    wheel_path: Path,
    *,
    version: str = "1.0",
    dist_name: str | None = None,
) -> PreparedProductArtifact:
    """Build a ``PreparedProductArtifact`` around a fixture wheel."""
    if dist_name is None:
        dist_name = product_id
    remote = RemoteSource(owner="tinystork", repo=f"Ze{product_id}", ref="main")
    resolved = ResolvedSource(
        source=remote, commit_sha="d4a0f1e2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8"
    )
    return PreparedProductArtifact(
        product_id=product_id,
        component_id=product_id,
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=VerifiedArtifact(
            component_id=product_id,
            version=version,
            path=wheel_path,
            size=wheel_path.stat().st_size,
            sha256=_sha256(wheel_path),
            distribution_name=dist_name,
            wheel_version=version,
        ),
    )


def _catalog(*descriptors: ProductDescriptor) -> ProductCatalog:
    return ProductCatalog(tuple(descriptors))


def _descriptor(product_id: str, dist_name: str | None = None) -> ProductDescriptor:
    return ProductDescriptor(
        product_id=product_id,
        display_name=product_id.capitalize(),
        distribution_name=dist_name or product_id,
        launch_entry_points=(EntryPointContract("console_scripts", product_id),),
    )


class _FakeAbsentRt:
    def status(self) -> RuntimeStatus:
        return RuntimeStatus(state=RuntimeState.ABSENT, runtime_root=Path("/fake"))


# ---------------------------------------------------------------------------
# 1. Blocking incompatibility prevents apply / mutation / selection
# ---------------------------------------------------------------------------


def test_blocking_incompatibility_prevents_apply_and_selection(
    tmp_path: Path, monkeypatch,
) -> None:
    """An INCOMPATIBLE candidate set fails closed before apply and before
    any selection persistence.  The error carries the stable reason code."""
    # Provider at API 2.0 is incompatible with the consumer's >=1,<2 range.
    provider_wheel = _build_wheel(
        tmp_path,
        name="ZeSolver",
        version="2.0.0",
        top_level="zesolver",
        interop=_provider_json(
            provides=[
                {
                    "api_module": "zesolver.api.v1",
                    "api_version": "2.0",
                    "capabilities": list(_ALL_CAPS),
                }
            ]
        ),
    )
    consumer_wheel = _build_wheel(
        tmp_path,
        name="ZeMosaic",
        version="4.6.0",
        top_level="zemosaic",
        interop=_consumer_json(),
    )
    provider_ppa = _make_ppa("zesolver", provider_wheel, version="2.0.0")
    consumer_ppa = _make_ppa("zemosaic", consumer_wheel, version="4.6.0")

    apply_calls: list = []

    def _explosive_apply(plan, *, registry, runtime, **kwargs):
        apply_calls.append(plan)
        raise AssertionError("apply_deployment_plan must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_catalog(_descriptor("zesolver"), _descriptor("zemosaic")),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=sel_path),
    )

    with pytest.raises(ProductCompatibilityBlockedError) as exc_info:
        service.install_prepared_product_deployment([provider_ppa, consumer_ppa])

    # --- No runtime mutation, no apply ---
    assert apply_calls == [], "apply_deployment_plan must not be called"

    # --- No selection persistence ---
    assert not sel_path.exists(), "selection file must not be created on block"

    # --- Stable reason code surfaced without long-prose dependency ---
    report = exc_info.value.report
    assert report.blocked is True
    assert report.verdict is CompatibilityVerdict.INCOMPATIBLE
    codes = {f.code for f in report.findings}
    assert "API_VERSION_MISMATCH" in codes
    assert "API_VERSION_MISMATCH" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 2. Degraded (optional provider absent) does not block
# ---------------------------------------------------------------------------


def test_optional_provider_absent_does_not_block(tmp_path: Path, monkeypatch) -> None:
    """A consumer with an optional, absent provider is COMPATIBLE_WITH_DEGRADED:
    the gate must not block and apply must proceed."""
    consumer_wheel = _build_wheel(
        tmp_path,
        name="ZeMosaic",
        version="4.6.0",
        top_level="zemosaic",
        interop=_consumer_json(),
    )
    consumer_ppa = _make_ppa("zemosaic", consumer_wheel, version="4.6.0")

    apply_calls: list = []

    def _fake_apply(plan, *, registry, runtime, **kwargs):
        apply_calls.append(plan)
        return DeploymentResult(success=True, active_slot_id="rt-test-degraded")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _fake_apply)

    sel_path = tmp_path / "desired-products.toml"
    service = ZeAlfieService(
        catalog=_catalog(_descriptor("zemosaic")),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=sel_path),
    )

    result = service.install_prepared_product_deployment([consumer_ppa])

    assert result.success is True
    assert len(apply_calls) == 1, "apply_deployment_plan must be called once"
    assert sel_path.exists(), "selection must persist on non-blocking success"


# ---------------------------------------------------------------------------
# 3. Evaluator receives the full prepared product set (multi-product)
# ---------------------------------------------------------------------------


def test_compatibility_sees_full_prepared_set(tmp_path: Path, monkeypatch) -> None:
    """In a multi-product deployment the evaluator must receive every
    primary prepared product wheel, not just the target product."""
    wheels = {
        "zesolver": _build_wheel(
            tmp_path, name="ZeSolver", version="1.1.0",
            top_level="zesolver", interop=_provider_json(),
        ),
        "zemosaic": _build_wheel(
            tmp_path, name="ZeMosaic", version="4.6.0",
            top_level="zemosaic", interop=_consumer_json(),
        ),
        "zewitness": _build_wheel(
            tmp_path, name="ZeWitness", version="0.0.1",
            top_level="zewitness", interop=None,
        ),
    }
    ppas = [
        _make_ppa("zesolver", wheels["zesolver"], version="1.1.0"),
        _make_ppa("zemosaic", wheels["zemosaic"], version="4.6.0"),
        _make_ppa("zewitness", wheels["zewitness"], version="0.0.1"),
    ]

    captured_wheel_paths: list = []

    def _capture_evaluate(wheel_paths):
        captured_wheel_paths.append(list(wheel_paths))
        return CompatibilityReport(
            verdict=CompatibilityVerdict.COMPATIBLE, findings=()
        )

    monkeypatch.setattr(svc_mod, "evaluate_wheels", _capture_evaluate)
    monkeypatch.setattr(
        svc_mod,
        "apply_deployment_plan",
        lambda plan, *, registry, runtime, **kwargs: DeploymentResult(
            success=True, active_slot_id="rt-test-full"
        ),
    )

    service = ZeAlfieService(
        catalog=_catalog(
            _descriptor("zesolver"),
            _descriptor("zemosaic"),
            _descriptor("zewitness"),
        ),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
    )

    result = service.install_prepared_product_deployment(ppas)

    assert result.success is True
    assert len(captured_wheel_paths) == 1, "evaluate_wheels must be called once"
    got = set(captured_wheel_paths[0])
    expected = {pa.wheel_path for pa in ppas}
    assert got == expected, "evaluator must receive the full prepared candidate set"
    assert len(captured_wheel_paths[0]) == 3


# ---------------------------------------------------------------------------
# 4. Blocking metadata-unavailable carries a stable reason code
# ---------------------------------------------------------------------------


def test_provider_metadata_unavailable_reason_code(tmp_path: Path, monkeypatch) -> None:
    """A present provider whose metadata is unavailable but referenced by a
    consumer yields METADATA_UNAVAILABLE and the raised error surfaces the
    stable PROVIDER_METADATA_UNAVAILABLE code."""
    # Provider wheel carries valid METADATA Name but NO interop declaration.
    provider_wheel = _build_wheel(
        tmp_path, name="ZeSolver", version="1.1.0", top_level="zesolver",
        interop=None,
    )
    consumer_wheel = _build_wheel(
        tmp_path, name="ZeMosaic", version="4.6.0",
        top_level="zemosaic", interop=_consumer_json(),
    )
    provider_ppa = _make_ppa("zesolver", provider_wheel, version="1.1.0")
    consumer_ppa = _make_ppa("zemosaic", consumer_wheel, version="4.6.0")

    apply_calls: list = []

    def _explosive_apply(plan, *, registry, runtime, **kwargs):
        apply_calls.append(plan)
        raise AssertionError("apply_deployment_plan must not be called")

    monkeypatch.setattr(svc_mod, "apply_deployment_plan", _explosive_apply)

    service = ZeAlfieService(
        catalog=_catalog(_descriptor("zesolver"), _descriptor("zemosaic")),
        runtime=_FakeAbsentRt(),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
    )

    with pytest.raises(ProductCompatibilityBlockedError) as exc_info:
        service.install_prepared_product_deployment([provider_ppa, consumer_ppa])

    assert apply_calls == []
    report = exc_info.value.report
    assert report.verdict is CompatibilityVerdict.METADATA_UNAVAILABLE
    codes = {f.code for f in report.findings}
    assert "PROVIDER_METADATA_UNAVAILABLE" in codes
    assert "PROVIDER_METADATA_UNAVAILABLE" in str(exc_info.value)
