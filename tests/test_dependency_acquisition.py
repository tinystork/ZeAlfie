"""Tests for M1-2D.4.2A dependency acquisition contract layer.

These tests validate the typed models, request-building helpers, and
pre-flight validation.  No pip, no subprocess, no network, no real
venvs — pure contract plumbing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from zealfie.building import build_wheel
from zealfie.dependencies import (
    AcquiredWheel,
    AcquisitionTransportError,
    DependencyAcquisitionError,
    DependencyAcquisitionRequest,
    DependencyAcquisitionResult,
    ExtraNotFound,
    MetadataError,
    build_acquisition_request,
)


# ---------------------------------------------------------------------------
# Session-scoped wheel fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def product_wheel_no_extras(session_wheelhouse: Path) -> Path:
    """leaf_lib-1.0.0-py3-none-any.whl — no extras."""
    return session_wheelhouse / "leaf_lib-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def product_wheel_with_extras(session_wheelhouse: Path) -> Path:
    """mid_lib_extra-1.0.0-py3-none-any.whl — Provides-Extra: feature."""
    return session_wheelhouse / "mid_lib_extra-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def product_wheel_mid(session_wheelhouse: Path) -> Path:
    """mid_lib-1.0.0-py3-none-any.whl — depends on leaf-lib, no extras."""
    return session_wheelhouse / "mid_lib-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def session_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build all synthetic fixture wheels into a shared wheelhouse."""
    import shutil

    fixtures = Path(__file__).resolve().parent / "fixtures"
    wheelhouse = tmp_path_factory.mktemp("acq-wheelhouse")

    for name in ("leaf_lib", "mid_lib", "mid_lib_extra"):
        src = fixtures / name
        tmp = tmp_path_factory.mktemp(f"build-{name}")
        wheel = build_wheel(src, output_dir=tmp)
        shutil.copy(wheel, wheelhouse / wheel.name)

    return wheelhouse


# =========================================================================
# build_acquisition_request — pre-flight validation
# =========================================================================


class TestBuildAcquisitionRequest:
    """Builder stores product wheel path and canonical extras."""

    def test_stores_product_wheel_path(
        self, product_wheel_no_extras: Path,
    ) -> None:
        req = build_acquisition_request(product_wheel_no_extras)
        assert req.product_wheel_path == product_wheel_no_extras.resolve()

    def test_active_extras_defaults_to_empty(
        self, product_wheel_no_extras: Path,
    ) -> None:
        req = build_acquisition_request(product_wheel_no_extras)
        assert req.active_extras == frozenset()

    def test_active_extras_none_treated_as_empty(
        self, product_wheel_no_extras: Path,
    ) -> None:
        req = build_acquisition_request(
            product_wheel_no_extras, active_extras=None,
        )
        assert req.active_extras == frozenset()

    def test_empty_frozenset_remains_empty(
        self, product_wheel_no_extras: Path,
    ) -> None:
        req = build_acquisition_request(
            product_wheel_no_extras, active_extras=frozenset(),
        )
        assert req.active_extras == frozenset()

    def test_valid_extra_stored(
        self, product_wheel_with_extras: Path,
    ) -> None:
        req = build_acquisition_request(
            product_wheel_with_extras,
            active_extras=frozenset({"feature"}),
        )
        assert req.active_extras == frozenset({"feature"})


class TestBuildAcquisitionRequestExtrasValidation:
    """Builder validates extras against wheel Provides-Extra."""

    def test_unknown_extra_raises(
        self, product_wheel_with_extras: Path,
    ) -> None:
        with pytest.raises(ExtraNotFound) as exc_info:
            build_acquisition_request(
                product_wheel_with_extras,
                active_extras=frozenset({"nonexistent"}),
            )
        assert exc_info.value.requested == "nonexistent"

    def test_unknown_extra_with_no_provides_extra(
        self, product_wheel_no_extras: Path,
    ) -> None:
        """leaf_lib has no Provides-Extra; requesting any extra fails."""
        with pytest.raises(ExtraNotFound):
            build_acquisition_request(
                product_wheel_no_extras,
                active_extras=frozenset({"anything"}),
            )


class TestBuildAcquisitionRequestExtrasCanonicalization:
    """active_extras are canonicalised (PEP 685) before validation/storage."""

    def test_uppercase_extra_canonicalized(
        self, product_wheel_with_extras: Path,
    ) -> None:
        """frozenset({"Feature"}) → stored as frozenset({"feature"})."""
        req = build_acquisition_request(
            product_wheel_with_extras,
            active_extras=frozenset({"Feature"}),
        )
        assert req.active_extras == frozenset({"feature"})

    def test_mixed_case_extra_canonicalized(
        self, product_wheel_with_extras: Path,
    ) -> None:
        """FEATURE (all caps) properly resolves to the declared extra."""
        req = build_acquisition_request(
            product_wheel_with_extras,
            active_extras=frozenset({"FEATURE"}),
        )
        assert req.active_extras == frozenset({"feature"})


class TestBuildAcquisitionRequestErrors:
    """Builder raises expected errors for missing/malformed wheels."""

    def test_fails_on_nonexistent_wheel(self, tmp_path: Path) -> None:
        bogus = tmp_path / "nonexistent-1.0.0-py3-none-any.whl"
        with pytest.raises(FileNotFoundError):
            build_acquisition_request(bogus)

    def test_fails_on_non_wheel_file(self, tmp_path: Path) -> None:
        not_a_wheel = tmp_path / "readme.txt"
        not_a_wheel.write_text("not a wheel")
        with pytest.raises(MetadataError):
            build_acquisition_request(not_a_wheel)


# =========================================================================
# AcquiredWheel.from_wheel_file — derivation from actual file
# =========================================================================


class TestAcquiredWheelFromFile:
    """from_wheel_file derives identity/size/hash from the actual file."""

    def test_canonical_name_from_filename(
        self, product_wheel_no_extras: Path,
    ) -> None:
        w = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        assert w.name == "leaf-lib"

    def test_version_from_filename(
        self, product_wheel_no_extras: Path,
    ) -> None:
        w = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        assert w.version == "1.0.0"

    def test_filename_stored_as_basename(
        self, product_wheel_no_extras: Path,
    ) -> None:
        w = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        assert w.filename == product_wheel_no_extras.name

    def test_wheel_path_stored_as_resolved_absolute(
        self, product_wheel_no_extras: Path,
    ) -> None:
        w = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        assert w.wheel_path == product_wheel_no_extras.resolve()

    def test_size_matches_stat(
        self, product_wheel_no_extras: Path,
    ) -> None:
        w = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        assert w.size == product_wheel_no_extras.stat().st_size
        assert w.size > 0

    def test_sha256_deterministic(
        self, product_wheel_no_extras: Path,
    ) -> None:
        w1 = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        w2 = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        assert w1.sha256 == w2.sha256

    def test_different_wheels_have_different_hashes(
        self, product_wheel_no_extras: Path, product_wheel_mid: Path,
    ) -> None:
        w1 = AcquiredWheel.from_wheel_file(product_wheel_no_extras)
        w2 = AcquiredWheel.from_wheel_file(product_wheel_mid)
        assert w1.sha256 != w2.sha256

    def test_name_with_underscores_canonicalized(
        self, product_wheel_with_extras: Path,
    ) -> None:
        """mid_lib_extra → mid-lib-extra (PEP 503 canonicalization)."""
        w = AcquiredWheel.from_wheel_file(product_wheel_with_extras)
        assert w.name == "mid-lib-extra"

class TestAcquiredWheelPostInit:
    """Light validation in __post_init__."""

    def test_empty_name_rejected(self) -> None:
        with pytest.raises(ValueError, match="name must not be empty"):
            AcquiredWheel(
                name="", version="1.0", wheel_path=Path("/x.whl"),
                filename="x-1.0-py3-none-any.whl", size=100,
                sha256="a" * 64,
            )

    def test_empty_version_rejected(self) -> None:
        with pytest.raises(ValueError, match="version must not be empty"):
            AcquiredWheel(
                name="test", version="", wheel_path=Path("/x.whl"),
                filename="x-1.0-py3-none-any.whl", size=100,
                sha256="a" * 64,
            )

    def test_negative_size_rejected(self) -> None:
        with pytest.raises(ValueError, match="size must be non-negative"):
            AcquiredWheel(
                name="test", version="1.0", wheel_path=Path("/x.whl"),
                filename="x-1.0-py3-none-any.whl", size=-1,
                sha256="a" * 64,
            )

    def test_empty_sha256_rejected(self) -> None:
        with pytest.raises(ValueError, match="64-character hex"):
            AcquiredWheel(
                name="test", version="1.0", wheel_path=Path("/x.whl"),
                filename="x-1.0-py3-none-any.whl", size=100,
                sha256="",
            )

    def test_wrong_length_sha256_rejected(self) -> None:
        with pytest.raises(ValueError, match="64-character"):
            AcquiredWheel(
                name="test", version="1.0", wheel_path=Path("/x.whl"),
                filename="x-1.0-py3-none-any.whl", size=100,
                sha256="a" * 63,
            )


# =========================================================================
# DependencyAcquisitionResult
# =========================================================================


class TestAcquisitionResult:
    """Result carries staging path + acquired tuple."""

    def test_stores_staging_wheelhouse(
        self, product_wheel_no_extras: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging-wheelhouse"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_no_extras)

        result = DependencyAcquisitionResult(
            staging_wheelhouse=staging,
            acquired=(),
        )
        assert result.staging_wheelhouse == staging
        assert result.acquired == ()

    def test_stores_acquired_wheels(
        self, product_wheel_no_extras: Path, tmp_path: Path,
    ) -> None:
        import shutil

        staging = tmp_path / "staging"
        staging.mkdir()
        fixtures = Path(__file__).resolve().parent / "fixtures"
        w = build_wheel(fixtures / "leaf_lib", output_dir=tmp_path / "bld")
        shutil.copy(w, staging / w.name)
        wheel_staging = staging / w.name

        acquired = AcquiredWheel.from_wheel_file(wheel_staging)
        result = DependencyAcquisitionResult(
            staging_wheelhouse=staging,
            acquired=(acquired,),
        )

        assert len(result.acquired) == 1
        assert result.acquired[0].name == "leaf-lib"
        assert result.acquired[0].wheel_path == wheel_staging.resolve()


# =========================================================================
# Error hierarchy
# =========================================================================


class TestErrorHierarchy:
    """Structured error classes are defined and subclass correctly."""

    def test_dependency_acquisition_error_is_runtime_error(self) -> None:
        assert issubclass(DependencyAcquisitionError, RuntimeError)

    def test_transport_error_carries_stage_and_detail(self) -> None:
        err = AcquisitionTransportError("download", "timeout after 30s")
        assert err.stage == "download"
        assert err.detail == "timeout after 30s"
        assert isinstance(err, DependencyAcquisitionError)

    def test_extra_not_found_is_not_acquisition_error(self) -> None:
        # ExtraNotFound (from models.py) is a DependencyResolutionError,
        # not a DependencyAcquisitionError.  The builder raises it
        # directly as a pre-flight validation that blocks before
        # acquisition even starts.
        assert not issubclass(ExtraNotFound, DependencyAcquisitionError)


# =========================================================================
# Frozen dataclass sanity
# =========================================================================


class TestModelsFrozen:
    """All acquisition dataclasses are frozen/immutable."""

    def test_acquired_wheel_frozen(self) -> None:
        w = AcquiredWheel(
            name="test", version="1.0", wheel_path=Path("/tmp/x.whl"),
            filename="x-1.0-py3-none-any.whl", size=100,
            sha256="a" * 64,
        )
        with pytest.raises(Exception):
            w.name = "other"  # type: ignore[misc]

    def test_acquisition_request_frozen(self) -> None:
        req = DependencyAcquisitionRequest(
            product_wheel_path=Path("/tmp/x.whl"),
            active_extras=frozenset({"gui"}),
        )
        with pytest.raises(Exception):
            req.product_wheel_path = Path("/other.whl")  # type: ignore[misc]

    def test_acquisition_result_frozen(
        self, product_wheel_no_extras: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "s"
        staging.mkdir()
        result = DependencyAcquisitionResult(
            staging_wheelhouse=staging, acquired=(),
        )
        with pytest.raises(Exception):
            result.acquired = ()  # type: ignore[misc]
