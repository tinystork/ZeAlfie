"""ZA-M1-2J Phases C+D+E + ZA-M1-2J.2 Phase F — ZeMosaic real
acceleration contract, real artifact source, and targeted tests
(matrix A–R + closure coherence / host prerequisites).

Covers:

* the packaged catalog contract for ``zemosaic`` (NVIDIA_CUDA, the
  10-distribution CUDA runtime closure — fastrlock deliberately absent,
  optional, anti-drift snapshot 9A + closure table);
* planning outcomes for every hardware/requirement combination
  (fail-closed, CPU closure preserved);
* the manifest-backed variant catalog and artifact acquirer
  (file:// hermetic downloads, sha256/size verification, reuse
  re-verification, structured fail-closed errors, retry behaviour,
  work_root confinement);
* KEEP verbatim documentation and read-only planning;
* service/CLI wiring for the real source (defaults, PLAN_READY gate,
  pre-activation failure preservation).

Hermetic by construction: the only transport used is ``file://``
pointing at the committed tiny wheel fixture
``tests/fixtures/fake_accel_wheel/`` — never the network, never pip.
"""

from __future__ import annotations

import hashlib
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedAcquisitionError,
    AcceleratedDeploymentPhase,
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedRequirement,
    AcceleratedVariant,
    AcceleratedVariantCatalog,
    AcquiredAcceleratedVariant,
    AmbiguousVariantError,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    HostPrerequisitesStatus,
    HostPrerequisiteStatus,
    ManifestAcceleratedArtifactAcquirer,
    MissingArtifact,
    PlatformMismatch,
    PlannedAcceleratedDependency,
    PlannedKeepProduct,
    ProductAccelerationRequirements,
    Sha256Mismatch,
    TransportError,
    VariantStatus,
    VersionMismatch,
    build_accelerated_deployment_plan,
    default_accelerated_artifact_manifest,
    default_manifest_variant_catalog,
    load_accelerated_artifact_manifest,
)
from zealfie.acceleration.acquisition import (
    InvalidArtifactManifestError,
    variant_catalog_from_artifact_manifest,
)
from zealfie.app import (
    ProductCatalog,
    ProductDescriptor,
    PreparedProductArtifact,
    SelectionStore,
    ZeAlfieService,
)
from zealfie.building import inspect_wheel
from zealfie.components.model import EntryPointContract
from zealfie.host.models import (
    AccelerationRecommendation,
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.products.catalog import (
    InvalidCatalogError,
    default_catalog,
    load_catalog_from_text,
)
from zealfie.releases.model import HostTarget, VerifiedArtifact
from zealfie.runtime.model import RuntimeState, RuntimeStatus
from zealfie.runtime.provenance import ProductProvenance
from zealfie.runtime.state import save_active_state
from zealfie.runtime.layout import RuntimeLayout
from zealfie.sources import RemoteSource, ResolvedSource

_EP = (EntryPointContract("console_scripts", "zewitness"),)

FAKE_WHEEL_DIR = Path(__file__).resolve().parent / "fixtures" / "fake_accel_wheel"
FAKE_WHEEL_PATH = FAKE_WHEEL_DIR / "fake_accel-1.0.0-py3-none-any.whl"
FAKE_WHEEL_SHA256 = (
    "dab702cf8802b9364f611f50c4c16612b320ca79c2463405b8a94234991cf701"
)
FAKE_WHEEL_SIZE = 797
SNAPSHOT_PATH = (
    Path(__file__).resolve().parent / "fixtures" / "zemosaic_gpu_extra_snapshot.toml"
)

SHA_A = "a" * 40
WHEEL_A = "f" * 64


# ---------------------------------------------------------------------------
# Host / recommendation helpers (synthetic — never real hardware)
# ---------------------------------------------------------------------------


def _caps(*, gpus=(), partial: bool = False) -> HostCapabilities:
    return HostCapabilities(
        os_name="Linux",
        cpu_arch="x86_64",
        platform_status=CapabilityStatus.AVAILABLE,
        platform_reason_code=HostReasonCode.OS_DETECTED,
        platform_reason="os detected",
        gpus=gpus,
        partial=partial,
    )


def _nvidia_gpu() -> GpuInfo:
    return GpuInfo(
        vendor="NVIDIA",
        model="GeForce MX150",
        kind=GpuKind.DISCRETE,
        hardware_present=True,
        driver_status=CapabilityStatus.AVAILABLE,
        driver_version="550.163.01",
        driver_reason_code=None,
        driver_reason=None,
        nvidia_smi_available=True,
        cuda_driver_present=True,
    )


def _supported() -> HostCapabilities:
    return _caps(gpus=(_nvidia_gpu(),))


def _recommendation(
    status: RecommendationStatus = RecommendationStatus.OFFER_SETUP,
    backend: str = "NVIDIA_CUDA",
    reason: str = "supported accelerator detected; setup offered",
) -> AccelerationRecommendation:
    reason_code = {
        RecommendationStatus.OFFER_SETUP: HostReasonCode.ACCELERATION_OFFER_SETUP,
        RecommendationStatus.NOT_APPLICABLE: HostReasonCode.ACCELERATION_NOT_APPLICABLE,
        RecommendationStatus.BLOCKED: HostReasonCode.ACCELERATION_BLOCKED,
    }[status]
    return AccelerationRecommendation(
        status=status,
        backend=backend,
        reason_code=reason_code,
        reason=reason,
    )


def _runtime_status() -> RuntimeStatus:
    return RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=Path("/fake"),
    )


def _hardware_supported() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="host acceleration is compatible with all declared product requirements",
        products_concerned=("zemosaic",),
    )


# ---------------------------------------------------------------------------
# Pure plan builder for the real catalog + manifest variant catalog
# ---------------------------------------------------------------------------


def _build_real_plan(
    *, caps=None, recommendation=None, keep_products=None, catalog=None
) -> AcceleratedDeploymentPlan:
    return build_accelerated_deployment_plan(
        catalog=catalog or default_catalog(),
        capabilities=caps if caps is not None else _supported(),
        recommendation=recommendation or _recommendation(),
        runtime_status=_runtime_status(),
        variant_catalog=default_manifest_variant_catalog(),
        keep_products=keep_products or {},
        platform_tag="linux_x86_64",
    )


# =============================================================================
# A–H — contract + planning with the real packaged catalog
# =============================================================================


def test_A_contract_parses_from_default_catalog():
    """A. The packaged catalog parses the zemosaic acceleration contract:
    backend NVIDIA_CUDA, optional, the full 10-distribution CUDA runtime
    closure (Phase F) — fastrlock deliberately absent."""
    desc = default_catalog().get("zemosaic")
    acc = desc.acceleration
    assert acc is not None
    assert acc.product_id == "zemosaic"
    assert acc.backend == "NVIDIA_CUDA"
    assert acc.optional is True
    assert acc.incompatibilities == ()
    requirements = {r.distribution: r for r in acc.requirements}
    assert set(requirements) == {
        "cupy-cuda12x",
        "cuda-pathfinder",
        "nvidia-cuda-runtime-cu12",
        "nvidia-cuda-nvrtc-cu12",
        "nvidia-cublas-cu12",
        "nvidia-cufft-cu12",
        "nvidia-cusparse-cu12",
        "nvidia-curand-cu12",
        "nvidia-cusolver-cu12",
        "nvidia-nvjitlink-cu12",
    }
    assert requirements["cupy-cuda12x"].specifier == ">=14.1.1,<15"
    assert requirements["cuda-pathfinder"].specifier == ">=1.3.4,<2"
    assert (
        requirements["nvidia-cuda-runtime-cu12"].specifier == "==12.4.127"
    )
    assert (
        requirements["nvidia-cuda-nvrtc-cu12"].specifier == "==12.4.127"
    )
    assert requirements["nvidia-cublas-cu12"].specifier == "==12.4.5.8"
    assert requirements["nvidia-cufft-cu12"].specifier == "==11.2.1.3"
    assert requirements["nvidia-cusparse-cu12"].specifier == "==12.3.1.170"
    assert requirements["nvidia-curand-cu12"].specifier == "==10.3.5.147"
    assert requirements["nvidia-cusolver-cu12"].specifier == "==11.6.1.9"
    assert (
        requirements["nvidia-nvjitlink-cu12"].specifier == "==12.4.127"
    )
    assert "fastrlock" not in requirements


def test_B_catalog_without_contract_plan_no_accelerated_requirements():
    """B. A catalog without any acceleration contract still produces
    NO_ACCELERATED_REQUIREMENTS (previous behaviour intact)."""
    catalog = ProductCatalog((
        ProductDescriptor(
            product_id="zeplain",
            display_name="ZePlain",
            distribution_name="zeplain",
            launch_entry_points=_EP,
        ),
    ))
    plan = _build_real_plan(catalog=catalog)
    assert plan.status is AcceleratedPlanStatus.NO_ACCELERATED_REQUIREMENTS
    assert plan.backend is None
    assert plan.products_concerned == ()
    assert plan.added_requirements == ()
    assert plan.blocked is True
    assert "no product declares accelerated requirements" in (
        plan.blocked_reason or ""
    )
    assert plan.target_runtime == "no new runtime required"


def test_C_contract_products_concerned_is_zemosaic():
    """C. The zemosaic contract -> products_concerned == ("zemosaic",)."""
    plan = _build_real_plan()
    assert plan.products_concerned == ("zemosaic",)
    assert plan.status is AcceleratedPlanStatus.PLAN_READY


def test_D_plan_backend_is_nvidia_cuda():
    """D. The planned backend is NVIDIA_CUDA."""
    plan = _build_real_plan()
    assert plan.backend == "NVIDIA_CUDA"


def test_E_plan_specifiers_exact():
    """E. Merged specifiers are exactly the declared contract values."""
    plan = _build_real_plan()
    by_distribution = {e.distribution: e for e in plan.added_requirements}
    assert set(by_distribution) == {
        "cupy-cuda12x",
        "cuda-pathfinder",
        "nvidia-cuda-runtime-cu12",
        "nvidia-cuda-nvrtc-cu12",
        "nvidia-cublas-cu12",
        "nvidia-cufft-cu12",
        "nvidia-cusparse-cu12",
        "nvidia-curand-cu12",
        "nvidia-cusolver-cu12",
        "nvidia-nvjitlink-cu12",
    }
    assert by_distribution["cupy-cuda12x"].specifier == ">=14.1.1,<15"
    assert by_distribution["cupy-cuda12x"].variant is not None
    assert by_distribution["cupy-cuda12x"].variant.version == "14.1.1"
    assert by_distribution["cuda-pathfinder"].variant is not None
    assert by_distribution["cuda-pathfinder"].variant.version == "1.6.0"
    assert (
        by_distribution["nvidia-cuda-runtime-cu12"].variant.version
        == "12.4.127"
    )
    assert (
        by_distribution["nvidia-cuda-nvrtc-cu12"].variant.version
        == "12.4.127"
    )
    assert by_distribution["nvidia-cublas-cu12"].variant.version == "12.4.5.8"
    assert by_distribution["nvidia-cufft-cu12"].variant.version == "11.2.1.3"
    assert (
        by_distribution["nvidia-cusparse-cu12"].variant.version
        == "12.3.1.170"
    )
    assert (
        by_distribution["nvidia-curand-cu12"].variant.version == "10.3.5.147"
    )
    assert (
        by_distribution["nvidia-cusolver-cu12"].variant.version == "11.6.1.9"
    )
    assert (
        by_distribution["nvidia-nvjitlink-cu12"].variant.version
        == "12.4.127"
    )
    assert all(
        entry.variant_status is VariantStatus.SELECTED
        for entry in plan.added_requirements
    )
    assert all(
        entry.declaring_products == ("zemosaic",)
        for entry in plan.added_requirements
    )


def test_F1_catalog_rejects_unknown_backend():
    """F. Unknown backend in the contract -> catalog parse rejection."""
    text = '''
schema_version = 1

[[products]]
id = "zebench"
display_name = "ZeBench"
distribution_name = "zebench"

[[products.launch.entry_points]]
group = "gui_scripts"
name = "zebench"

[products.acceleration]
backend = "VULKAN"
optional = true
'''
    with pytest.raises(InvalidCatalogError, match="backend"):
        load_catalog_from_text(text)


def test_F2_ambiguous_variant_catalog_fails_closed():
    """F. Two variants matching the same need -> AmbiguousVariantError."""
    catalog = AcceleratedVariantCatalog((
        AcceleratedVariant(
            distribution="accel-lib", version="1.0.0",
            backend="NVIDIA_CUDA", platform=None,
        ),
        AcceleratedVariant(
            distribution="accel-lib", version="2.0.0",
            backend="NVIDIA_CUDA", platform="linux_x86_64",
        ),
    ))
    with pytest.raises(AmbiguousVariantError):
        catalog.find_variant("accel-lib", "NVIDIA_CUDA", "linux_x86_64")


def test_G_incompatible_hardware_blocks_plan():
    """G. Incompatible hardware (NOT_APPLICABLE) -> BLOCKED plan."""
    plan = _build_real_plan(
        caps=_caps(),
        recommendation=_recommendation(
            RecommendationStatus.NOT_APPLICABLE,
            reason="no supported accelerator hardware detected",
        ),
    )
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert "no supported accelerator hardware detected" in (
        plan.blocked_reason or ""
    )
    assert plan.added_requirements == ()
    assert plan.target_runtime == "no new runtime required"


def test_H_optional_contract_cpu_fallback_preserved():
    """H. optional==true is carried by the contract and a blocked plan
    demands no CPU runtime change — the CPU closure is independent of
    the acceleration."""
    acc = default_catalog().get("zemosaic").acceleration
    assert acc is not None and acc.optional is True
    # The CPU install contract is untouched: no `gpu` extra required.
    assert default_catalog().get("zemosaic").required_extras == ()
    plan = _build_real_plan(
        caps=_caps(),
        recommendation=_recommendation(
            RecommendationStatus.NOT_APPLICABLE,
            reason="no supported accelerator hardware detected",
        ),
    )
    assert plan.target_runtime == "no new runtime required"
    assert plan.added_requirements == ()


# =============================================================================
# I–L + O — manifest loader and acquirer (hermetic, file:// only)
# =============================================================================


def _manifest_toml(
    url: str,
    *,
    distribution: str = "fake-accel",
    version: str = "1.0.0",
    python: str = "cp313",
    size: int = FAKE_WHEEL_SIZE,
    sha256: str = FAKE_WHEEL_SHA256,
) -> str:
    return f'''
schema_version = 1

[[artifacts]]
distribution = "{distribution}"
version = "{version}"
backend = "NVIDIA_CUDA"
platform = "linux_x86_64"
python = "{python}"
requires_python = ">=3.9"
filename = "fake_accel-1.0.0-py3-none-any.whl"
url = "{url}"
size = {size}
sha256 = "{sha256}"
'''


def _manifest(url: str, **kwargs):
    return load_accelerated_artifact_manifest(_manifest_toml(url, **kwargs))


def _acquirer(manifest, **kwargs) -> ManifestAcceleratedArtifactAcquirer:
    defaults = dict(
        platform_tag="linux_x86_64",
        python_tag="cp313",
        retry_delay=0.0,
    )
    defaults.update(kwargs)
    return ManifestAcceleratedArtifactAcquirer(manifest, **defaults)


def _plan(
    *,
    distribution: str = "fake-accel",
    specifier: str = "==1.0.0",
) -> AcceleratedDeploymentPlan:
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware_supported(),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
        keep_products=(),
        added_requirements=(
            PlannedAcceleratedDependency(
                distribution=distribution,
                specifier=specifier,
                extras=(),
                declaring_products=("zebench",),
                variant=AcceleratedVariant(
                    distribution=distribution,
                    version="1.0.0",
                    backend="NVIDIA_CUDA",
                    platform="linux_x86_64",
                ),
                variant_status=VariantStatus.SELECTED,
            ),
        ),
        source_runtime_state="READY",
        source_active_slot_id=None,
        source_previous_slot_id=None,
        target_runtime="new shared runtime slot with accelerated "
        "NVIDIA_CUDA closure",
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )


def _file_url(path: Path) -> str:
    return path.resolve().as_uri()


def test_I_acquirer_downloads_file_url_and_validates_sha256(tmp_path):
    """I. file:// acquisition of the fixture wheel: sha256 validated,
    AcquiredAcceleratedVariant matches the manifest exactly, wheel
    deposited under work_root/artifacts/."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))
    acquirer = _acquirer(manifest)
    work_root = tmp_path / "work"
    acquired = acquirer.acquire(_plan(), work_root)

    assert len(acquired) == 1
    variant = acquired[0]
    assert isinstance(variant, AcquiredAcceleratedVariant)
    assert variant.distribution == "fake-accel"
    assert variant.version == "1.0.0"
    assert variant.wheel_path == work_root / "artifacts" / (
        "fake_accel-1.0.0-py3-none-any.whl"
    )
    assert variant.size == FAKE_WHEEL_SIZE
    assert variant.sha256 == FAKE_WHEEL_SHA256
    assert variant.wheel_path.is_file()
    assert not (work_root / "artifacts" / (
        "fake_accel-1.0.0-py3-none-any.whl.part"
    )).exists()


def test_I_reuse_requires_reverification_not_presence(tmp_path):
    """I. A second acquisition reuses the local file only after
    re-verification — the transport is never contacted again (counting
    urlopen), and the result is identical."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))
    calls: list[int] = []

    def counting_urlopen(url, timeout=None):
        calls.append(1)
        return urllib.request.urlopen(url, timeout=timeout)

    acquirer = _acquirer(manifest, urlopen=counting_urlopen)
    work_root = tmp_path / "work"
    first = acquirer.acquire(_plan(), work_root)
    second = acquirer.acquire(_plan(), work_root)
    assert calls == [1]  # downloaded exactly once, reused once
    assert first == second
    assert first[0].wheel_path == second[0].wheel_path


def test_J_corrupted_artifact_raises_sha256_mismatch(tmp_path):
    """J. A byte-altered wheel fails sha256 verification (fail-closed),
    both on fresh download and on reuse."""
    altered = tmp_path / "altered.whl"
    altered.write_bytes(FAKE_WHEEL_PATH.read_bytes() + b"X")
    manifest = _manifest(_file_url(altered))
    acquirer = _acquirer(manifest)
    with pytest.raises(Sha256Mismatch):
        acquirer.acquire(_plan(), tmp_path / "work")


def test_J_local_file_failing_reverification_refused(tmp_path):
    """J. A pre-existing local artifact with the wrong bytes is refused
    (never reused on presence alone) even when its name matches."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))
    acquirer = _acquirer(manifest)
    work_root = tmp_path / "work"
    dest = work_root / "artifacts" / "fake_accel-1.0.0-py3-none-any.whl"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"not the wheel")
    with pytest.raises(Sha256Mismatch):
        acquirer.acquire(_plan(), work_root)


def test_K_unknown_distribution_rejected(tmp_path):
    """K. A planned distribution with no manifest entry -> MissingArtifact."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))
    acquirer = _acquirer(manifest)
    with pytest.raises(MissingArtifact):
        acquirer.acquire(_plan(distribution="unknown-lib"), tmp_path / "work")


def test_L_version_outside_specifier_rejected(tmp_path):
    """L. Manifest version outside the merged specifier -> VersionMismatch."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))
    acquirer = _acquirer(manifest)
    with pytest.raises(VersionMismatch):
        acquirer.acquire(_plan(specifier="==9.9.9"), tmp_path / "work")


def test_L_python_tag_mismatch_rejected(tmp_path):
    """L (python coherence). An artifact tagged for another interpreter
    is refused fail-closed (PlatformMismatch) — never silently used."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH), python="cp312")
    acquirer = _acquirer(manifest, python_tag="cp313")
    with pytest.raises(PlatformMismatch):
        acquirer.acquire(_plan(), tmp_path / "work")


def test_L_python_tag_py3_accepted_on_cp313(tmp_path):
    """L (python coherence, Phase F). A ``py3``-tagged artifact (the
    closure's nvidia-*-cu12 wheels) is accepted on a ``cp313``
    interpreter — ``py3-none`` wheels are version-independent."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH), python="py3")
    acquirer = _acquirer(manifest, python_tag="cp313")
    acquired = acquirer.acquire(_plan(), tmp_path / "work")
    assert len(acquired) == 1
    assert acquired[0].distribution == "fake-accel"


def test_O_acquisition_writes_only_under_work_root(tmp_path):
    """O. Acquisition confines every write to work_root (fixture source
    lives outside tmp_path; nothing else is created)."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))
    acquirer = _acquirer(manifest)
    before = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
    )
    work_root = tmp_path / "work"
    acquirer.acquire(_plan(), work_root)
    after = sorted(
        str(p.relative_to(tmp_path)) for p in tmp_path.rglob("*") if p.is_file()
    )
    created = [p for p in after if p not in before]
    assert created == [
        str(work_root.relative_to(tmp_path) / "artifacts" / (
            "fake_accel-1.0.0-py3-none-any.whl"
        ))
    ]


# ---------------------------------------------------------------------------
# Loader / transport extras (fail-closed manifest, retries)
# ---------------------------------------------------------------------------


def test_manifest_loader_requires_sha256():
    """sha256 is mandatory (64 hex) — entries without it are rejected."""
    text = _manifest_toml(_file_url(FAKE_WHEEL_PATH), sha256="deadbeef")
    with pytest.raises(InvalidArtifactManifestError, match="sha256"):
        load_accelerated_artifact_manifest(text)
    text = _manifest_toml(_file_url(FAKE_WHEEL_PATH)).replace(
        f'sha256 = "{FAKE_WHEEL_SHA256}"', ""
    )
    with pytest.raises(InvalidArtifactManifestError, match="sha256"):
        load_accelerated_artifact_manifest(text)


def test_manifest_loader_rejects_duplicates_and_bad_scheme():
    """Duplicate entries and non-file/http(s) urls are rejected."""
    text = _manifest_toml(_file_url(FAKE_WHEEL_PATH))
    doubled = text + text.split("schema_version = 1\n", 1)[1]
    with pytest.raises(InvalidArtifactManifestError, match="duplicate"):
        load_accelerated_artifact_manifest(doubled)
    bad = _manifest_toml("ftp://example.invalid/wheel.whl")
    with pytest.raises(InvalidArtifactManifestError, match="scheme"):
        load_accelerated_artifact_manifest(bad)


def test_transport_retries_then_succeeds(tmp_path):
    """Transient HTTP 5xx is retried (short) and a later attempt wins."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, url, timeout=None):
            self.calls += 1
            if self.calls <= 2:
                raise urllib.error.HTTPError(
                    url, 503, "Service Unavailable", None, None
                )
            return urllib.request.urlopen(url, timeout=timeout)

    flaky = _Flaky()
    acquirer = _acquirer(manifest, urlopen=flaky)
    acquired = acquirer.acquire(_plan(), tmp_path / "work")
    assert flaky.calls == 3
    assert acquired[0].sha256 == FAKE_WHEEL_SHA256


def test_transport_retries_exhausted_transport_error(tmp_path):
    """Permanent transport failure (HTTP 404) and exhausted retries
    raise TransportError — never a silent fallback."""
    manifest = _manifest(_file_url(FAKE_WHEEL_PATH))

    def always_404(url, timeout=None):
        raise urllib.error.HTTPError(url, 404, "Not Found", None, None)

    acquirer = _acquirer(manifest, urlopen=always_404)
    with pytest.raises(TransportError):
        acquirer.acquire(_plan(), tmp_path / "work")


# =============================================================================
# M–N — KEEP verbatim + read-only planning
# =============================================================================


def test_M_keep_products_carried_verbatim():
    """M. KEEP products are documented verbatim (commit/wheel SHAs
    exactly preserved); the plan updates no product."""
    keep = PlannedKeepProduct(
        product_id="zemosaic",
        version="4.6.0",
        commit_sha=SHA_A,
        wheel_sha256=WHEEL_A,
    )
    plan = _build_real_plan(keep_products={"zemosaic": keep})
    assert plan.keep_products == (keep,)
    assert plan.keep_products[0].commit_sha == SHA_A
    assert plan.keep_products[0].wheel_sha256 == WHEEL_A
    assert plan.keep_products[0].version == "4.6.0"
    assert plan.keep_products[0].source == "provenance"
    # products_concerned only documents declarers; nothing is updated.
    assert plan.products_concerned == ("zemosaic",)


def test_N_planning_read_only_and_deterministic(tmp_path):
    """N. Two successive plan builds are identical objects and no state
    file changes (gpu-plan is 100% read-only)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    save_active_state(layout.active_pointer, "rt-abc123", None)
    service = ZeAlfieService(
        catalog=default_catalog(),
        runtime=_FakeRt(
            RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-abc123",
            )
        ),
        host=HostTarget(
            python_tag="py313",
            abi_tag="cp313",
            platform_tag="linux_x86_64",
        ),
        capability_collector=_supported,
        recommender=lambda caps: _recommendation(),
        provenance_store=_ActiveProvenanceStore(),
    )
    snapshot_before = _snapshot(tmp_path)
    first = service.build_accelerated_deployment_plan()
    second = service.build_accelerated_deployment_plan()
    snapshot_after = _snapshot(tmp_path)
    assert first == second
    assert first.status is AcceleratedPlanStatus.PLAN_READY
    assert snapshot_before == snapshot_after


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    if not root.exists():
        return out
    for path in sorted(root.rglob("*")):
        if path.is_file():
            out[str(path.relative_to(root))] = path.read_bytes()
    return out


class _FakeRt:
    """Minimal runtime exposing only status() (no real slots)."""

    def __init__(self, status: RuntimeStatus) -> None:
        self._status = status

    def status(self) -> RuntimeStatus:
        return self._status


class _ActiveProvenanceStore:
    """Fake provenance store marking ``zemosaic`` ACTIVE (hermetic)."""

    def load_active(self):
        return {
            "zemosaic": ProductProvenance(
                product_id="zemosaic",
                version="1.0.0",
                source_owner="zealfie",
                source_repo="ZeMosaic",
                requested_ref="main",
                commit_sha=SHA_A,
                wheel_sha256=WHEEL_A,
            )
        }


# =============================================================================
# P — pre-activation failure preserves the active runtime
# =============================================================================


class _FailingAcquirer:
    def acquire(self, plan, work_root, *, cancel_check=None):
        raise Sha256Mismatch("synthetic acquisition failure")


def test_P_acquisition_failure_preserves_active_runtime(
    tmp_path, witness_v1,
):
    """P. An acquirer failure (pre-activation) leaves the active runtime
    state untouched: phase ACQUIRE, non-zero mutation, pointer bytes
    unchanged."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    save_active_state(layout.active_pointer, "rt-abc123", None)
    pointer_before = layout.active_pointer.read_bytes()

    catalog = ProductCatalog((
        ProductDescriptor(
            product_id="zewitness",
            display_name="ZeWitness",
            distribution_name="zealfie-witness",
            launch_entry_points=_EP,
            remote_source=RemoteSource(
                owner="tinystork", repo="ZeWitness", ref="main"
            ),
            acceleration=ProductAccelerationRequirements(
                product_id="zewitness",
                backend="NVIDIA_CUDA",
                optional=True,
                requirements=(
                    AcceleratedRequirement(
                        distribution="fake-accel", specifier="==1.0.0"
                    ),
                ),
            ),
        ),
    ))
    service = ZeAlfieService(
        catalog=catalog,
        runtime=_FakeRt(
            RuntimeStatus(
                state=RuntimeState.READY,
                runtime_root=layout.root,
                active_slot_id="rt-abc123",
                python_executable=Path(sys.executable),
                python_version=f"{sys.version_info.major}.{sys.version_info.minor}",
            )
        ),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
        host=HostTarget(
            python_tag="py313",
            abi_tag="cp313",
            platform_tag="linux_x86_64",
        ),
    )
    keep = PlannedKeepProduct(
        product_id="zewitness",
        version="0.0.1",
        commit_sha="d" * 40,
        wheel_sha256=_sha256(witness_v1),
    )
    plan = _plan()
    plan = AcceleratedDeploymentPlan(
        status=plan.status,
        hardware=plan.hardware,
        backend=plan.backend,
        products_concerned=("zewitness",),
        keep_products=(keep,),
        added_requirements=plan.added_requirements,
        source_runtime_state="READY",
        source_active_slot_id="rt-abc123",
        source_previous_slot_id=None,
        target_runtime=plan.target_runtime,
        blocked=False,
        blocked_reason=None,
        closure_impact=(),
    )
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()

    result = service.install_accelerated_runtime(
        plan=plan,
        capabilities=_supported(),
        recommendation=_recommendation(),
        acquirer=_FailingAcquirer(),
        full_state_provider=lambda: [_witness_ppa(witness_v1)],
        dependency_wheelhouse=wheelhouse,
        work_root=tmp_path / "work",
    )

    assert result.success is False
    assert result.cancelled is False
    assert result.phase is AcceleratedDeploymentPhase.ACQUIRE
    assert "synthetic acquisition failure" in (result.reason or "")
    assert result.old_runtime_preserved is True
    # Active runtime untouched: pointer bytes unchanged, no slots created.
    assert layout.active_pointer.read_bytes() == pointer_before
    assert not (layout.slots.exists() and any(layout.slots.iterdir()))


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _witness_ppa(wheel_path: Path) -> PreparedProductArtifact:
    info = inspect_wheel(wheel_path)
    size = wheel_path.stat().st_size
    resolved = ResolvedSource(
        source=RemoteSource(owner="tinystork", repo="ZeWitness", ref="main"),
        commit_sha="d" * 40,
    )
    verified = VerifiedArtifact(
        component_id="zewitness",
        version="0.0.1",
        path=wheel_path,
        size=size,
        sha256=_sha256(wheel_path),
        distribution_name=info.distribution_name,
        wheel_version=info.version,
    )
    return PreparedProductArtifact(
        product_id="zewitness",
        component_id="zewitness",
        resolved_source=resolved,
        wheel_path=wheel_path,
        verified_artifact=verified,
    )


# =============================================================================
# Q–R — genericity + non-concerned products
# =============================================================================


def test_Q_products_without_acceleration_not_concerned():
    """Q. zesolver / zeseestarstacker / zeanalyser (no acceleration
    declared) are absent from products_concerned."""
    plan = _build_real_plan()
    assert plan.products_concerned == ("zemosaic",)
    assert "zesolver" not in plan.products_concerned
    assert "zeseestarstacker" not in plan.products_concerned
    assert "zeanalyser" not in plan.products_concerned


def test_R_second_product_merges_by_distribution():
    """R. A fictional second NVIDIA_CUDA product declaring cupy-cuda12x
    merges into ONE entry per distribution with both declarers."""
    zemosaic = default_catalog().get("zemosaic")
    second = ProductDescriptor(
        product_id="zefake2",
        display_name="ZeFake2",
        distribution_name="zefake2",
        launch_entry_points=_EP,
        acceleration=ProductAccelerationRequirements(
            product_id="zefake2",
            backend="NVIDIA_CUDA",
            optional=True,
            requirements=(
                AcceleratedRequirement(
                    distribution="cupy-cuda12x", specifier=">=14.1"
                ),
            ),
        ),
    )
    catalog = ProductCatalog((zemosaic, second))
    plan = _build_real_plan(catalog=catalog)
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    by_distribution = {e.distribution: e for e in plan.added_requirements}
    assert "cupy-cuda12x" in by_distribution
    cupy = by_distribution["cupy-cuda12x"]
    assert cupy.declaring_products == ("zefake2", "zemosaic")
    assert cupy.specifier == ">=14.1, >=14.1.1,<15"
    assert cupy.variant is not None
    assert cupy.variant.version == "14.1.1"


# =============================================================================
# 9A — anti-drift snapshot coherence
# =============================================================================


def test_anti_drift_snapshot_coherent_with_catalog_contract():
    """9A. The ZeMosaic stable gpu extra snapshot (f76f9cca...) is
    coherent with the packaged catalog contract: cupy-cuda12x present
    in the snapshot gpu extra, optional==true, backend NVIDIA_CUDA."""
    import tomllib

    snapshot = tomllib.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    assert snapshot["zemosaic_commit"] == (
        "f76f9ccaebbc6007b0965b061009871377a686fc"
    )
    assert snapshot["requires-python"] == ">=3.10"
    gpu_extra = snapshot["project"]["optional-dependencies"]["gpu"]
    assert gpu_extra == ["cupy-cuda12x"]

    acc = default_catalog().get("zemosaic").acceleration
    assert acc is not None
    assert acc.optional is True
    assert acc.backend == "NVIDIA_CUDA"
    distributions = {r.distribution for r in acc.requirements}
    # The contract requirement for cupy-cuda12x is present in the
    # snapshot's gpu extra (anti-drift: the extra and the contract
    # must name the same accelerator distribution).
    assert "cupy-cuda12x" in distributions
    assert "cupy-cuda12x" in gpu_extra


def test_variant_catalog_from_manifest_matches_packaged_manifest():
    """The manifest-derived variant catalog resolves the exact packaged
    versions for the real zemosaic contract."""
    catalog = default_manifest_variant_catalog()
    cupy = catalog.find_variant("cupy-cuda12x", "NVIDIA_CUDA", "linux_x86_64")
    assert cupy is not None
    assert cupy.version == "14.1.1"
    assert cupy.sha256 == (
        "76ea35469e2aa0a8332b88f72505ea2f7871a0bc8f9b0c87184f57e47c9aa3bf"
    )
    pathfinder = catalog.find_variant(
        "cuda-pathfinder", "NVIDIA_CUDA", "linux_x86_64"
    )
    assert pathfinder is not None
    assert pathfinder.version == "1.6.0"
    assert pathfinder.sha256 == (
        "1503af579d8379c24bdd65528379bc57039b0455be9f5f9686cf8e473a1fce51"
    )
    assert (
        catalog.find_variant(
            "nvidia-cuda-nvrtc-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "12.4.127"
    )
    assert (
        catalog.find_variant(
            "nvidia-cuda-runtime-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "12.4.127"
    )
    assert (
        catalog.find_variant(
            "nvidia-cublas-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "12.4.5.8"
    )
    assert (
        catalog.find_variant(
            "nvidia-cufft-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "11.2.1.3"
    )
    assert (
        catalog.find_variant(
            "nvidia-cusparse-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "12.3.1.170"
    )
    assert (
        catalog.find_variant(
            "nvidia-curand-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "10.3.5.147"
    )
    assert (
        catalog.find_variant(
            "nvidia-cusolver-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "11.6.1.9"
    )
    assert (
        catalog.find_variant(
            "nvidia-nvjitlink-cu12", "NVIDIA_CUDA", "linux_x86_64"
        ).version
        == "12.4.127"
    )
    assert (
        catalog.find_variant("fastrlock", "NVIDIA_CUDA", "linux_x86_64")
        is None
    )


# =============================================================================
# ZA-M1-2J.2 Phase F — closure coherence, host prerequisites, honest BLOCKED
# =============================================================================

CLOSURE = {
    "cupy-cuda12x": "14.1.1",
    "cuda-pathfinder": "1.6.0",
    "nvidia-cuda-runtime-cu12": "12.4.127",
    "nvidia-cuda-nvrtc-cu12": "12.4.127",
    "nvidia-cublas-cu12": "12.4.5.8",
    "nvidia-cufft-cu12": "11.2.1.3",
    "nvidia-cusparse-cu12": "12.3.1.170",
    "nvidia-curand-cu12": "10.3.5.147",
    "nvidia-cusolver-cu12": "11.6.1.9",
    "nvidia-nvjitlink-cu12": "12.4.127",
}


def test_manifest_parses_full_closure_both_platforms():
    """(e) The packaged manifest parses the full closure on both
    platforms: exactly 20 artifacts, each sha256 exactly 64 hex, python
    tags coherent (cp313 for cupy-cuda12x, py3 for the rest)."""
    manifest = default_accelerated_artifact_manifest()
    assert len(manifest.entries) == 20
    by_distribution = {
        e.distribution: e
        for e in manifest.entries
        if e.platform == "linux_x86_64"
    }
    assert set(by_distribution) == set(CLOSURE)
    for distribution, version in CLOSURE.items():
        entry = by_distribution[distribution]
        assert entry.version == version
        assert len(entry.sha256) == 64
    assert by_distribution["cupy-cuda12x"].python == "cp313"
    for distribution in CLOSURE:
        if distribution != "cupy-cuda12x":
            assert by_distribution[distribution].python == "py3"
    assert "fastrlock" not in by_distribution

    win_by_distribution = {
        e.distribution: e
        for e in manifest.entries
        if e.platform == "win_amd64"
    }
    assert set(win_by_distribution) == set(CLOSURE)
    for distribution, version in CLOSURE.items():
        entry = win_by_distribution[distribution]
        assert entry.version == version
        assert entry.platform == "win_amd64"
        assert len(entry.sha256) == 64
        assert all(c in "0123456789abcdef" for c in entry.sha256)
    assert win_by_distribution["cupy-cuda12x"].python == "cp313"
    for distribution in CLOSURE:
        if distribution != "cupy-cuda12x":
            assert win_by_distribution[distribution].python == "py3"
    assert "fastrlock" not in win_by_distribution
    assert sum(e.size for e in win_by_distribution.values()) == 1204421859


def test_manifest_variant_catalog_has_twenty_variants():
    """(e) The variant catalog derived from the manifest resolves all 10
    closure distributions on both platforms (and nothing for fastrlock)."""
    catalog = default_manifest_variant_catalog()
    assert len(catalog.variants) == 20
    for distribution, version in CLOSURE.items():
        variant = catalog.find_variant(
            distribution, "NVIDIA_CUDA", "linux_x86_64"
        )
        assert variant is not None, distribution
        assert variant.version == version
    for distribution, version in CLOSURE.items():
        variant = catalog.find_variant(
            distribution, "NVIDIA_CUDA", "win_amd64"
        )
        assert variant is not None, distribution
        assert variant.version == version


def test_anti_drift_closure_snapshot_matches_manifest():
    """(e) The Phase F closure tables in the anti-drift snapshot match
    the packaged manifest exactly (distributions, versions, and the
    per-platform total download bytes = the sum of the manifest sizes)."""
    import tomllib

    snapshot = tomllib.loads(SNAPSHOT_PATH.read_text(encoding="utf-8"))
    closure = snapshot["accelerated_closure"]
    snapshot_distributions = {}
    for line in closure["distributions"]:
        distribution, _, version = line.partition("==")
        distribution = distribution.strip()
        version = version.strip()
        snapshot_distributions[distribution] = version
    assert snapshot_distributions == CLOSURE

    windows = snapshot["accelerated_closure_windows"]
    win_snapshot_distributions = {}
    for line in windows["distributions"]:
        distribution, _, version = line.partition("==")
        distribution = distribution.strip()
        version = version.strip()
        win_snapshot_distributions[distribution] = version
    assert win_snapshot_distributions == CLOSURE

    manifest = default_accelerated_artifact_manifest()
    assert sum(
        entry.size
        for entry in manifest.entries
        if entry.platform == "linux_x86_64"
    ) == closure["total_download_bytes"]
    assert sum(
        entry.size
        for entry in manifest.entries
        if entry.platform == "win_amd64"
    ) == windows["total_download_bytes"]


def test_plan_ready_carries_host_prerequisites_classification():
    """(d) A PLAN_READY plan for the real contract carries the host
    prerequisites classification: REQUIRED_HOST (driver OK observed,
    CC documented NOT_OBSERVED) + MANAGED_RUNTIME listing the whole
    10-distribution closure with exact pins and the cost note."""
    plan = _build_real_plan()
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    prereqs = plan.host_prerequisites
    assert prereqs is not None
    assert prereqs.status is HostPrerequisitesStatus.OK

    required = {entry.entry: entry for entry in prereqs.required_host}
    driver = required["nvidia-driver"]
    assert driver.status is HostPrerequisiteStatus.OK
    assert driver.observed == "550.163.01"
    assert "550.54.14" in driver.requirement
    cc = required["nvidia-gpu-cc"]
    assert cc.status is HostPrerequisiteStatus.NOT_OBSERVED
    assert "6.0" in cc.requirement

    managed = {
        entry.entry: entry
        for entry in prereqs.managed_runtime
        if entry.entry != "total"
    }
    assert set(managed) == set(CLOSURE)
    for distribution, version in CLOSURE.items():
        assert managed[distribution].requirement == f"=={version}"
        assert managed[distribution].status is HostPrerequisiteStatus.MANAGED
    totals = [
        entry for entry in prereqs.managed_runtime if entry.entry == "total"
    ]
    assert len(totals) == 1
    assert "download" in totals[0].requirement


def test_driver_below_floor_blocks_plan_honestly():
    """(d) A missing host precondition (observed driver below the
    550.54.14 floor) => BLOCKED — never PLAN_READY — with the honest
    reason naming the observed version."""
    old_driver = _nvidia_gpu()
    import dataclasses

    old_driver = dataclasses.replace(old_driver, driver_version="550.54.13")
    caps = _caps(gpus=(old_driver,))
    plan = _build_real_plan(caps=caps)
    assert plan.status is AcceleratedPlanStatus.BLOCKED
    assert plan.blocked is True
    assert plan.added_requirements == ()
    assert plan.target_runtime == "no new runtime required"
    reason = plan.blocked_reason or ""
    assert "host prerequisite" in reason
    assert "nvidia-driver 550.54.13" in reason
    assert "550.54.14" in reason


def test_driver_version_absent_never_fabricates_verdict():
    """A driver whose version was not observed is documented NOT_OBSERVED
    and never fabricates a BLOCKED verdict (absence of a usable driver
    is gated upstream by the recommendation)."""
    no_version = _nvidia_gpu()
    import dataclasses

    no_version = dataclasses.replace(no_version, driver_version=None)
    caps = _caps(gpus=(no_version,))
    plan = _build_real_plan(caps=caps)
    assert plan.status is AcceleratedPlanStatus.PLAN_READY
    prereqs = plan.host_prerequisites
    assert prereqs is not None
    driver = {
        entry.entry: entry for entry in prereqs.required_host
    }["nvidia-driver"]
    assert driver.status is HostPrerequisiteStatus.NOT_OBSERVED
    assert driver.observed is None
