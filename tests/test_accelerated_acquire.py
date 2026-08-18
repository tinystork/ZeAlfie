"""Tests for accelerated artifact acquisition primitives (M1-2I).

Covers:

* the fail-closed default acquirer (always raises
  ``AcceleratedAcquisitionUnavailable`` — no real artifact source is
  configured yet);
* ``AcquiredAcceleratedVariant`` integrity validation at construction
  (missing file, size mismatch, sha256 mismatch, bad name/version,
  distribution canonicalization);
* a synthetic directory-based acquirer that copies fake wheels from a
  fixture directory into ``work_root`` and verifies size + sha256.

Synthetic distribution names (``fake-accel``, ``accelerated-lib``) are
used everywhere — ZeAlfie never selects a concrete accelerated
framework.  No real venv, pip, network, or hardware is involved here.
"""

from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from zealfie.acceleration import (
    AcceleratedAcquisitionUnavailable,
    AcceleratedArtifactEntry,
    AcceleratedArtifactManifest,
    AcceleratedDeploymentPlan,
    AcceleratedPlanStatus,
    AcceleratedVariant,
    HardwareCompatibility,
    HardwareCompatibilityReasonCode,
    HardwareCompatibilityStatus,
    InvalidArtifactManifestError,
    ManifestAcceleratedArtifactAcquirer,
    PlannedAcceleratedDependency,
    VariantStatus,
    default_accelerated_artifact_acquirer,
    default_accelerated_artifact_manifest,
    load_accelerated_artifact_manifest,
)
from zealfie.acceleration.deployment import (
    AcceleratedAcquisitionError,
    AcquiredAcceleratedVariant,
)


# ---------------------------------------------------------------------------
# Synthetic plan helpers (built directly — never real hardware)
# ---------------------------------------------------------------------------


def _hardware() -> HardwareCompatibility:
    return HardwareCompatibility(
        status=HardwareCompatibilityStatus.SUPPORTED,
        reason_code=HardwareCompatibilityReasonCode.COMPATIBLE.value,
        reason="compatible",
        products_concerned=("zebench",),
    )


def _plan(
    *distributions: tuple[str, str | None],
    variant_version: str = "1.0.0",
) -> AcceleratedDeploymentPlan:
    """A synthetic PLAN_READY accelerated plan over the given
    ``(distribution, specifier)`` pairs."""
    entries = tuple(
        PlannedAcceleratedDependency(
            distribution=distribution,
            specifier=specifier,
            extras=(),
            declaring_products=("zebench",),
            variant=AcceleratedVariant(
                distribution=distribution,
                version=variant_version,
                backend="NVIDIA_CUDA",
            ),
            variant_status=VariantStatus.SELECTED,
        )
        for distribution, specifier in distributions
    )
    return AcceleratedDeploymentPlan(
        status=AcceleratedPlanStatus.PLAN_READY,
        hardware=_hardware(),
        backend="NVIDIA_CUDA",
        products_concerned=("zebench",),
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


def _minimal_wheel(output: Path, name: str, version: str) -> Path:
    """Create a tiny synthetic wheel via zipfile (no pip involved)."""
    safe_name = name.replace("-", "_").replace(".", "_")
    wheel_path = output / f"{safe_name}-{version}-py3-none-any.whl"
    dist_info = f"{safe_name}-{version}.dist-info"
    wheelfile = (
        "Wheel-Version: 1.0\n"
        "Generator: test\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n"
    )
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    record = (
        f"{dist_info}/WHEEL,,\n"
        f"{dist_info}/METADATA,,\n"
        f"{dist_info}/RECORD,,\n"
    )
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/WHEEL", wheelfile)
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)
    return wheel_path


def _size_sha(path: Path) -> tuple[int, str]:
    sha = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(65536):
            sha.update(chunk)
    return path.stat().st_size, sha.hexdigest()


# ---------------------------------------------------------------------------
# Default acquirer is fail-closed
# ---------------------------------------------------------------------------


def test_default_acquirer_always_raises_unavailable(tmp_path: Path) -> None:
    """The production default acquirer refuses everything: no real
    accelerated artifact source is configured yet."""
    acquirer = default_accelerated_artifact_acquirer()
    plan = _plan(("fake-accel", None))
    work_root = tmp_path / "work"
    work_root.mkdir()

    with pytest.raises(AcceleratedAcquisitionUnavailable) as exc:
        acquirer.acquire(plan, work_root)
    assert "no accelerated artifact source configured" in str(exc.value)

    # The acquirer is also callable directly.
    with pytest.raises(AcceleratedAcquisitionUnavailable):
        acquirer(plan, work_root)


def test_default_acquirer_error_is_acquisition_error() -> None:
    """AcceleratedAcquisitionUnavailable subclasses
    AcceleratedAcquisitionError."""
    assert issubclass(
        AcceleratedAcquisitionUnavailable, AcceleratedAcquisitionError
    )


# ---------------------------------------------------------------------------
# AcquiredAcceleratedVariant validation
# ---------------------------------------------------------------------------


def test_acquired_variant_valid_and_canonicalized(tmp_path: Path) -> None:
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    size, sha = _size_sha(wheel)
    variant = AcquiredAcceleratedVariant(
        distribution="Fake.Accel",
        version="1.0.0",
        wheel_path=wheel,
        size=size,
        sha256=sha,
    )
    assert variant.distribution == "fake-accel"
    assert variant.version == "1.0.0"
    assert variant.wheel_path == wheel
    assert variant.size == size
    assert variant.sha256 == sha


def test_acquired_variant_missing_file_rejected(tmp_path: Path) -> None:
    ghost = tmp_path / "ghost-1.0.0-py3-none-any.whl"
    with pytest.raises(ValueError, match="not an existing file"):
        AcquiredAcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            wheel_path=ghost,
            size=123,
            sha256="a" * 64,
        )


def test_acquired_variant_size_mismatch_rejected(tmp_path: Path) -> None:
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    size, sha = _size_sha(wheel)
    with pytest.raises(ValueError, match="size mismatch"):
        AcquiredAcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            wheel_path=wheel,
            size=size + 1,
            sha256=sha,
        )


def test_acquired_variant_sha256_mismatch_rejected(tmp_path: Path) -> None:
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    size, _ = _size_sha(wheel)
    with pytest.raises(ValueError, match="sha256 mismatch"):
        AcquiredAcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            wheel_path=wheel,
            size=size,
            sha256="b" * 64,
        )


def test_acquired_variant_empty_distribution_rejected(tmp_path: Path) -> None:
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    size, sha = _size_sha(wheel)
    with pytest.raises(ValueError, match="distribution must be a non-empty string"):
        AcquiredAcceleratedVariant(
            distribution="   ",
            version="1.0.0",
            wheel_path=wheel,
            size=size,
            sha256=sha,
        )


def test_acquired_variant_empty_version_rejected(tmp_path: Path) -> None:
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    size, sha = _size_sha(wheel)
    with pytest.raises(ValueError, match="version must be a non-empty string"):
        AcquiredAcceleratedVariant(
            distribution="fake-accel",
            version="",
            wheel_path=wheel,
            size=size,
            sha256=sha,
        )


def test_acquired_variant_non_int_size_rejected(tmp_path: Path) -> None:
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    _, sha = _size_sha(wheel)
    with pytest.raises(ValueError, match="size must be an int"):
        AcquiredAcceleratedVariant(  # type: ignore[arg-type]
            distribution="fake-accel",
            version="1.0.0",
            wheel_path=wheel,
            size=123.5,  # type: ignore[arg-type]
            sha256=sha,
        )


def test_acquired_variant_tampered_file_rejected_after_construction(
    tmp_path: Path,
) -> None:
    """A wheel tampered with after acquisition no longer matches — the
    construction-time verification means the object cannot describe a
    lie, and a fresh construction over the tampered file is rejected."""
    wheel = _minimal_wheel(tmp_path, "fake-accel", "1.0.0")
    size, sha = _size_sha(wheel)
    variant = AcquiredAcceleratedVariant(
        distribution="fake-accel",
        version="1.0.0",
        wheel_path=wheel,
        size=size,
        sha256=sha,
    )
    # Tamper with the file on disk.
    with open(wheel, "ab") as fh:
        fh.write(b"tampered")
    with pytest.raises(ValueError, match="size mismatch"):
        AcquiredAcceleratedVariant(
            distribution="fake-accel",
            version="1.0.0",
            wheel_path=wheel,
            size=size,
            sha256=sha,
        )
    # The previously-constructed instance still records the original facts.
    assert variant.sha256 == sha


# ---------------------------------------------------------------------------
# Synthetic directory-based acquirer
# ---------------------------------------------------------------------------


class _FixtureAcquirer:
    """Directory-based test acquirer.

    For every planned dependency, copies the matching fake wheel from a
    fixture directory into ``work_root``, verifies size + sha256, and
    returns one :class:`AcquiredAcceleratedVariant` per planned
    dependency (distribution key), sorted by distribution.
    """

    def __init__(self, fixture_dir: Path) -> None:
        self._fixture_dir = fixture_dir
        self.cancel_calls: int = 0

    def acquire(self, plan, work_root, *, cancel_check=None):
        self.cancel_calls += 1
        acquired = []
        for entry in sorted(
            plan.added_requirements, key=lambda e: e.distribution
        ):
            if cancel_check is not None:
                cancel_check()
            safe_name = entry.distribution.replace("-", "_")
            wheel = (
                self._fixture_dir
                / f"{safe_name}-{entry.variant.version}-py3-none-any.whl"
            )
            if not wheel.is_file():
                raise FileNotFoundError(
                    f"no fake wheel for {entry.distribution!r}: {wheel}"
                )
            dest = work_root / wheel.name
            shutil.copyfile(wheel, dest)
            size, sha = _size_sha(dest)
            acquired.append(
                AcquiredAcceleratedVariant(
                    distribution=entry.distribution,
                    version=entry.variant.version,
                    wheel_path=dest,
                    size=size,
                    sha256=sha,
                )
            )
        return tuple(acquired)


def test_directory_acquirer_returns_verified_variants(tmp_path: Path) -> None:
    """The synthetic acquirer copies wheels into work_root and returns
    exactly one integrity-verified variant per planned dependency."""
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    _minimal_wheel(fixture_dir, "fake-accel", "1.0.0")
    _minimal_wheel(fixture_dir, "accelerated-lib", "1.0.0")

    plan = _plan(("fake-accel", "==1.0.0"), ("accelerated-lib", ">=1.0"))
    work_root = tmp_path / "work"
    work_root.mkdir()

    cancels: list[int] = []
    acquirer = _FixtureAcquirer(fixture_dir)
    acquired = acquirer.acquire(
        plan, work_root, cancel_check=lambda: cancels.append(1)
    )

    assert acquirer.cancel_calls == 1
    assert len(cancels) == 2  # once per planned dependency

    assert [v.distribution for v in acquired] == [
        "accelerated-lib",
        "fake-accel",
    ]
    for variant in acquired:
        assert variant.version == "1.0.0"
        assert variant.wheel_path.is_file()
        assert variant.wheel_path.parent == work_root
        size, sha = _size_sha(variant.wheel_path)
        assert variant.size == size
        assert variant.sha256 == sha
        assert len(sha) == 64


def test_directory_acquirer_missing_fixture_wheel_raises(tmp_path: Path) -> None:
    """An acquirer that cannot produce an artifact raises honestly —
    acquisition failure is the caller's to surface."""
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    _minimal_wheel(fixture_dir, "fake-accel", "1.0.0")

    plan = _plan(("fake-accel", None), ("accelerated-lib", None))
    work_root = tmp_path / "work"
    work_root.mkdir()

    acquirer = _FixtureAcquirer(fixture_dir)
    with pytest.raises(FileNotFoundError):
        acquirer.acquire(plan, work_root)

# ---------------------------------------------------------------------------
# M1-2L M3 — Windows accelerated artifact closure (win_amd64 / cp313)
# ---------------------------------------------------------------------------

WIN_CLOSURE = {
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


def _two_platform_manifest(
    *,
    url_override: str | None = None,
    size_override: int | None = None,
    sha256_override: str | None = None,
) -> str:
    """Inline manifest with the same immutable wheel under two platform
    rows; the win_amd64 row optionally differs in one metadata field."""
    url = (
        "https://files.pythonhosted.org/packages/fc/b4/"
        "d088047afe39827556df21118cac9ffd20cc3f968c99a7681494d1eb333c/"
        "cuda_pathfinder-1.6.0-py3-none-any.whl"
    )
    size = 54591
    sha256 = "1503af579d8379c24bdd65528379bc57039b0455be9f5f9686cf8e473a1fce51"

    def entry(platform: str, *, differ: bool) -> str:
        e_url = url_override if (differ and url_override is not None) else url
        e_size = size_override if (differ and size_override is not None) else size
        e_sha = (
            sha256_override if (differ and sha256_override is not None) else sha256
        )
        return f"""
[[artifacts]]
distribution = "cuda-pathfinder"
version = "1.6.0"
backend = "NVIDIA_CUDA"
platform = "{platform}"
python = "py3"
requires_python = ">=3.10"
filename = "cuda_pathfinder-1.6.0-py3-none-any.whl"
url = "{e_url}"
size = {e_size}
sha256 = "{e_sha}"
"""

    return (
        "schema_version = 1\n"
        + entry("linux_x86_64", differ=False)
        + entry("win_amd64", differ=True)
    )


def test_manifest_allows_same_immutable_artifact_across_platforms() -> None:
    """Two platform rows may reference the same immutable wheel bytes
    (same filename + url + size + sha256) — the manifest accepts both."""
    manifest = load_accelerated_artifact_manifest(_two_platform_manifest())
    assert len(manifest.entries) == 2
    assert (
        manifest.find("cuda-pathfinder", "NVIDIA_CUDA", "linux_x86_64")
        is not None
    )
    assert (
        manifest.find("cuda-pathfinder", "NVIDIA_CUDA", "win_amd64")
        is not None
    )


@pytest.mark.parametrize(
    ("url_override", "size_override", "sha256_override"),
    [
        (
            "https://files.pythonhosted.org/packages/ab/cd/"
            "deadbeefcuda_pathfinder-1.6.0-py3-none-any.whl",
            None,
            None,
        ),
        (None, 54592, None),
        (None, None, "a" * 64),
    ],
    ids=["url-differs", "size-differs", "sha256-differs"],
)
def test_manifest_rejects_same_filename_with_different_metadata(
    url_override: str | None,
    size_override: int | None,
    sha256_override: str | None,
) -> None:
    """The same filename under two platform rows is rejected unless the
    (url, size, sha256) bytes reference is identical — fail-closed."""
    with pytest.raises(
        InvalidArtifactManifestError, match="duplicate artifact filename"
    ):
        load_accelerated_artifact_manifest(
            _two_platform_manifest(
                url_override=url_override,
                size_override=size_override,
                sha256_override=sha256_override,
            )
        )


def test_win_amd64_entries_resolve_via_find() -> None:
    """Each of the 10 closure distributions resolves on win_amd64 with
    the pinned version, and the linux lookup still returns the linux
    platform entry (no cross-platform leak)."""
    manifest = default_accelerated_artifact_manifest()
    for distribution, version in WIN_CLOSURE.items():
        win = manifest.find(
            distribution, "NVIDIA_CUDA", "win_amd64", python_tag="cp313"
        )
        assert win is not None, distribution
        assert win.version == version, distribution
        assert win.platform == "win_amd64", distribution
        linux = manifest.find(
            distribution, "NVIDIA_CUDA", "linux_x86_64", python_tag="cp313"
        )
        assert linux is not None, distribution
        assert linux.platform == "linux_x86_64", distribution
        assert linux.version == version, distribution


def test_win_closure_versions_match_linux_pins() -> None:
    """The 10 win_amd64 versions equal the 10 linux_x86_64 versions."""
    manifest = default_accelerated_artifact_manifest()
    win = {
        e.distribution: e.version
        for e in manifest.entries
        if e.platform == "win_amd64"
    }
    linux = {
        e.distribution: e.version
        for e in manifest.entries
        if e.platform == "linux_x86_64"
    }
    assert win == WIN_CLOSURE
    assert linux == WIN_CLOSURE


def test_acquirer_resolves_win_platform_with_override() -> None:
    """The acquirer with platform_tag="win_amd64" / python_tag="cp313"
    resolves the Windows wheel facts (resolution only — no download)."""
    acquirer = ManifestAcceleratedArtifactAcquirer(
        manifest=default_accelerated_artifact_manifest(),
        platform_tag="win_amd64",
        python_tag="cp313",
    )
    cupy = acquirer._resolve_entry("cupy-cuda12x", "NVIDIA_CUDA")
    assert cupy.platform == "win_amd64"
    assert cupy.filename == "cupy_cuda12x-14.1.1-cp313-cp313-win_amd64.whl"
    assert (
        cupy.sha256
        == "64072f4139b44df38215f0519a6badc14138fa0e4bb5b2db44fe94d05f8b9c8b"
    )
    cublas = acquirer._resolve_entry("nvidia-cublas-cu12", "NVIDIA_CUDA")
    assert cublas.platform == "win_amd64"
    assert (
        cublas.filename
        == "nvidia_cublas_cu12-12.4.5.8-py3-none-win_amd64.whl"
    )
    assert (
        cublas.sha256
        == "5a796786da89203a0657eda402bcdcec6180254a8ac22d72213abc42069522dc"
    )


def test_manifest_rejects_plaintext_http_url() -> None:
    """Plaintext ``http://`` URLs are rejected (fail-closed; no TLS
    downgrade).  ``file://`` (hermetic tests) and ``https://`` remain."""
    text = (
        "schema_version = 1\n"
        "[[artifacts]]\n"
        'distribution = "fake-accel"\n'
        'version = "1.0.0"\n'
        'backend = "NVIDIA_CUDA"\n'
        'platform = "linux_x86_64"\n'
        'python = "py3"\n'
        'filename = "fake_accel-1.0.0-py3-none-any.whl"\n'
        'url = "http://files.example.org/fake_accel-1.0.0-py3-none-any.whl"\n'
        "size = 123\n"
        'sha256 = "' + ("a" * 64) + '"\n'
    )
    with pytest.raises(InvalidArtifactManifestError, match="scheme"):
        load_accelerated_artifact_manifest(text)
