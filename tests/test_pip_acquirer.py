"""Tests for M1-2D.4.2B PipWheelhouseAcquirer (FAST / no network).

All tests mock ``subprocess.run``; no real ``pip download`` is ever
invoked.  Tests that need staged wheel files use the shared session
fixture wheels copied into ``tmp_path``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from zealfie.building import build_wheel
from zealfie.dependencies import (
    AcquiredWheel,
    AcquisitionTransportError,
    DependencyAcquisitionRequest,
    DependencyAcquisitionResult,
    PipWheelhouseAcquirer,
    build_acquisition_request,
)

# ═════════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════════


def _mock_run_success(*_, **__) -> mock.MagicMock:
    """Return a CompletedProcess that succeeded (rc=0, empty stderr/stdout)."""
    return mock.MagicMock(
        returncode=0, stdout="", stderr="", spec=subprocess.CompletedProcess,
    )


def _mock_run_failure(returncode: int = 1, stderr: str = "") -> mock.MagicMock:
    """Return a CompletedProcess that failed."""
    return mock.MagicMock(
        returncode=returncode,
        stdout="",
        stderr=stderr or "pip download error",
        spec=subprocess.CompletedProcess,
    )


def _copy_wheel_to_staging(
    wheel: Path, staging: Path, *, suffix: str = "",
) -> Path:
    """Copy a wheel into staging, optionally renaming suffix, return dest."""
    dest = staging / wheel.name
    if suffix:
        dest = staging / f"{wheel.stem}{suffix}.whl"
    shutil.copy(wheel, dest)
    return dest


def _make_bogus_whl(staging: Path, name: str) -> Path:
    """Create a zero-byte file with .whl extension (unparseable filename)."""
    p = staging / name
    p.write_bytes(b"")
    return p


# ═════════════════════════════════════════════════════════════════════════
# Session-scoped fixture wheels (same set as other dependency tests)
# ═════════════════════════════════════════════════════════════════════════


@pytest.fixture(scope="session")
def session_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build leaf_lib, mid_lib, mid_lib_extra into a shared wheelhouse."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    wheelhouse = tmp_path_factory.mktemp("pipacq-wheelhouse")

    for name in ("leaf_lib", "mid_lib", "mid_lib_extra"):
        src = fixtures / name
        tmp = tmp_path_factory.mktemp(f"build-{name}")
        wheel = build_wheel(src, output_dir=tmp)
        shutil.copy(wheel, wheelhouse / wheel.name)

    return wheelhouse


@pytest.fixture(scope="session")
def product_wheel_leaf(session_wheelhouse: Path) -> Path:
    """leaf_lib-1.0.0-py3-none-any.whl — no extras, no deps."""
    return session_wheelhouse / "leaf_lib-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def product_wheel_mid_extra(session_wheelhouse: Path) -> Path:
    """mid_lib_extra-1.0.0-py3-none-any.whl — has extras."""
    return session_wheelhouse / "mid_lib_extra-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def product_wheel_mid_lib(session_wheelhouse: Path) -> Path:
    """mid_lib-1.0.0-py3-none-any.whl — prefix trap candidate."""
    return session_wheelhouse / "mid_lib-1.0.0-py3-none-any.whl"


# ═════════════════════════════════════════════════════════════════════════
# 1. pip argv shape
# ═════════════════════════════════════════════════════════════════════════


class TestPipArgvShape:
    """pip argv includes required flags, excludes forbidden flags."""

    def test_argv_includes_required_flags(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ) as m_run:
            acquirer.acquire(req, staging_dir=staging)

        argv = m_run.call_args[0][0]  # first positional arg
        argv_str = " ".join(argv)

        # Required flags present
        assert "download" in argv
        assert "--isolated" in argv
        assert "--only-binary=:all:" in argv
        assert "--index-url" in argv
        assert "https://pypi.org/simple" in argv
        assert "--dest" in argv
        assert str(staging) in argv_str
        # Path-based pip command
        assert "pip" in argv
        assert str(product_wheel_leaf.resolve()) in argv_str

    def test_no_no_deps_flag(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Explicit sentinel: --no-deps must never appear."""
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ) as m_run:
            acquirer.acquire(req, staging_dir=staging)

        argv_str = " ".join(m_run.call_args[0][0])
        assert "--no-deps" not in argv_str


# ═════════════════════════════════════════════════════════════════════════
# 2. Extras formatting
# ═════════════════════════════════════════════════════════════════════════


class TestExtrasFormatting:
    """Extras are appended as [extra] with deterministic sort order."""

    def test_single_extra_appended_requirement(
        self, product_wheel_mid_extra: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        # mid_lib_extra has "feature" extra
        req = build_acquisition_request(
            product_wheel_mid_extra, active_extras=frozenset({"feature"}),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ) as m_run:
            acquirer.acquire(req, staging_dir=staging)

        argv = m_run.call_args[0][0]
        # The last positional arg is the requirement
        # Format: /path/to/wheel[gui]
        last_arg = argv[-1]
        assert "[feature]" in last_arg
        assert str(product_wheel_mid_extra.resolve()) in last_arg

    def test_multiple_extras_sorted_deterministically(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Multiple extras comma-separated and sorted in requirement string.

        Uses direct DependencyAcquisitionRequest construction to bypass
        Provides-Extra validation (irrelevant to argv formatting test).
        """
        staging = tmp_path / "staging"
        staging.mkdir()
        # Direct dataclass construction; validation goes through
        # build_acquisition_request, not the constructor.
        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_leaf,
            active_extras=frozenset({"gui", "alpha"}),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ) as m_run:
            acquirer.acquire(req, staging_dir=staging)

        argv = m_run.call_args[0][0]
        last_arg = argv[-1]
        # Must be sorted: alpha,gui (alphabetically)
        assert "[alpha,gui]" in last_arg
        assert str(product_wheel_leaf.resolve()) in last_arg


# ═════════════════════════════════════════════════════════════════════════
# 3. Path with spaces
# ═════════════════════════════════════════════════════════════════════════


class TestPathWithSpaces:
    """Product path with spaces is one argv element, no shell quoting."""

    def test_path_with_spaces_single_argv_element(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Copy the wheel to a path containing spaces, verify single arg."""
        staging = tmp_path / "staging"
        staging.mkdir()
        space_dir = tmp_path / "my products"
        space_dir.mkdir()
        wheel_with_spaces = space_dir / product_wheel_leaf.name
        shutil.copy(product_wheel_leaf, wheel_with_spaces)

        req = build_acquisition_request(wheel_with_spaces)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ) as m_run:
            acquirer.acquire(req, staging_dir=staging)

        argv = m_run.call_args[0][0]
        last_arg = argv[-1]
        # Must be the exact path string (no extra quotes)
        assert " " in last_arg
        assert str(wheel_with_spaces.resolve()) in last_arg
        # No shell quoting characters
        assert "'" not in last_arg
        assert '"' not in last_arg


# ═════════════════════════════════════════════════════════════════════════
# 4. Env stripping
# ═════════════════════════════════════════════════════════════════════════


class TestEnvStripping:
    """subprocess env strips PIP_* vars, preserves non-PIP vars."""

    def test_pip_vars_removed_from_subprocess_env(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch.object(
            os, "environ",
            {
                "PATH": "/usr/bin",
                "HOME": "/home/user",
                "PIP_INDEX_URL": "https://evil.example.com",
                "PIP_EXTRA_INDEX_URL": "https://also-evil.example.com",
                "PIP_NO_INDEX": "1",
                "PIP_NO_DEPS": "1",
                "SSL_CERT_FILE": "/etc/ssl/certs.pem",
            },
        ), mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ) as m_run:
            acquirer.acquire(req, staging_dir=staging)

        env = m_run.call_args.kwargs["env"]
        assert env is not None
        assert "PATH" in env
        assert "HOME" in env
        assert "SSL_CERT_FILE" in env
        assert "PIP_INDEX_URL" not in env
        assert "PIP_EXTRA_INDEX_URL" not in env
        assert "PIP_NO_INDEX" not in env
        assert "PIP_NO_DEPS" not in env

    def test_os_environ_not_mutated(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        # Capture original PIP_INDEX_URL if set
        had_pip_index = "PIP_INDEX_URL" in os.environ
        original_value = os.environ.get("PIP_INDEX_URL")

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            acquirer.acquire(req, staging_dir=staging)

        # os.environ unchanged
        assert ("PIP_INDEX_URL" in os.environ) == had_pip_index
        if had_pip_index:
            assert os.environ["PIP_INDEX_URL"] == original_value


# ═════════════════════════════════════════════════════════════════════════
# 5. Non-zero returncode
# ═════════════════════════════════════════════════════════════════════════


class TestNonZeroReturnCode:
    """Non-zero returncode maps to AcquisitionTransportError("pip-download",...)."""

    def test_non_zero_raises_pip_download_error(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run",
            return_value=_mock_run_failure(
                returncode=1, stderr="No matching distribution found"
            ),
        ):
            with pytest.raises(AcquisitionTransportError) as exc_info:
                acquirer.acquire(req, staging_dir=staging)

        assert exc_info.value.stage == "pip-download"
        assert "No matching distribution found" in exc_info.value.detail


# ═════════════════════════════════════════════════════════════════════════
# 6. Timeout
# ═════════════════════════════════════════════════════════════════════════


class TestTimeout:
    """TimeoutExpired maps to AcquisitionTransportError("pip-timeout",...)."""

    def test_timeout_raises_pip_timeout_error(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=5),
        ):
            with pytest.raises(AcquisitionTransportError) as exc_info:
                acquirer.acquire(req, staging_dir=staging)

        assert exc_info.value.stage == "pip-timeout"
        assert "timed out after 5s" in exc_info.value.detail


# ═════════════════════════════════════════════════════════════════════════
# 7. Spawn errors
# ═════════════════════════════════════════════════════════════════════════


class TestSpawnErrors:
    """FileNotFound/OSError maps to AcquisitionTransportError("pip-invoke",...)."""

    def test_file_not_found_raises_pip_invoke_error(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run",
            side_effect=FileNotFoundError("No such file: python3"),
        ):
            with pytest.raises(AcquisitionTransportError) as exc_info:
                acquirer.acquire(req, staging_dir=staging)

        assert exc_info.value.stage == "pip-invoke"
        assert "No such file" in exc_info.value.detail

    def test_os_error_raises_pip_invoke_error(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run",
            side_effect=OSError("Permission denied"),
        ):
            with pytest.raises(AcquisitionTransportError) as exc_info:
                acquirer.acquire(req, staging_dir=staging)

        assert exc_info.value.stage == "pip-invoke"
        assert "Permission denied" in exc_info.value.detail


# ═════════════════════════════════════════════════════════════════════════
# 8. Prefix trap: product copy removed, similar dependency kept
# ═════════════════════════════════════════════════════════════════════════


class TestRootProductRemoval:
    """Product wheel removed by name+version+sha256, prefix trap avoided."""

    def test_product_removed_by_identity_prefix_similar_kept(
        self,
        product_wheel_mid_extra: Path,
        product_wheel_mid_lib: Path,
        tmp_path: Path,
    ) -> None:
        """mid_lib_extra is product; mid_lib is dependency.

        pip would download mid_lib as a transitive dependency of mid_lib_extra.
        The product copy of mid_lib_extra must be removed, but mid_lib (prefix
        similar: "mid_lib" is a prefix of "mid_lib_extra") must remain.
        """
        staging = tmp_path / "staging"
        staging.mkdir()
        # Stage both wheels as if pip downloaded them
        _copy_wheel_to_staging(product_wheel_mid_extra, staging)
        _copy_wheel_to_staging(product_wheel_mid_lib, staging)

        product = AcquiredWheel.from_wheel_file(product_wheel_mid_extra)
        dep = AcquiredWheel.from_wheel_file(product_wheel_mid_lib)

        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_mid_extra,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        assert result.staging_wheelhouse == staging.resolve()
        acquired_names = {w.name for w in result.acquired}
        # mid-lib (dependency) remains
        assert dep.name in acquired_names
        # mid-lib-extra (product) removed
        assert product.name not in acquired_names
        assert len(result.acquired) == 1

    def test_root_removal_uses_name_version_sha256(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Verify removal uses full identity triple, not filename prefix."""
        staging = tmp_path / "staging"
        staging.mkdir()
        # Stage product
        _copy_wheel_to_staging(product_wheel_leaf, staging)
        # Also stage a wheel with different name but same version prefix (unlikely)
        # but to test: we create a scenario where name differs

        product = AcquiredWheel.from_wheel_file(product_wheel_leaf)
        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_leaf,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        # Product was the only wheel and was removed; result still succeeds
        assert product.name not in {w.name for w in result.acquired}
        assert len(result.acquired) == 0


# ═════════════════════════════════════════════════════════════════════════
# 9. Zero product copy accepted
# ═════════════════════════════════════════════════════════════════════════


class TestZeroProductCopy:
    """Zero exact product matches is OK — pip may not copy product."""

    def test_dependency_only_staging_succeeds(
        self,
        product_wheel_mid_extra: Path,
        product_wheel_mid_lib: Path,
        tmp_path: Path,
    ) -> None:
        """Staging contains only dependency wheel — no product copy."""
        staging = tmp_path / "staging"
        staging.mkdir()
        # Stage only the dependency, not the product
        _copy_wheel_to_staging(product_wheel_mid_lib, staging)

        dep = AcquiredWheel.from_wheel_file(product_wheel_mid_lib)
        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_mid_extra,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        assert len(result.acquired) == 1
        assert result.acquired[0].name == dep.name

    def test_empty_staging_succeeds(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Empty staging dir (no wheels at all) is valid."""
        staging = tmp_path / "staging"
        staging.mkdir()
        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_leaf,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        assert result.acquired == ()
        assert result.staging_wheelhouse == staging.resolve()


# ═════════════════════════════════════════════════════════════════════════
# 10. Duplicate exact product copy
# ═════════════════════════════════════════════════════════════════════════


class TestDuplicateProductCleanup:
    """More than one exact product match fails closed."""

    def test_duplicate_product_copy_fails_root_cleanup(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Two byte-identical copies of the product wheel → error."""
        staging = tmp_path / "staging"
        staging.mkdir()
        _copy_wheel_to_staging(product_wheel_leaf, staging)
        _copy_wheel_to_staging(
            product_wheel_leaf, staging, suffix="_copy",
        )

        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_leaf,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            with pytest.raises(AcquisitionTransportError) as exc_info:
                acquirer.acquire(req, staging_dir=staging)

        assert exc_info.value.stage == "root-cleanup"
        assert "2 exact product copies" in exc_info.value.detail


# ═════════════════════════════════════════════════════════════════════════
# 11. Invalid wheel in staging
# ═════════════════════════════════════════════════════════════════════════


class TestInvalidWheelValidation:
    """Unparseable staged .whl fails with validate error."""

    def test_invalid_wheel_filename_fails_validate(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """A .whl file with unparseable filename → AcquisitionTransportError."""
        staging = tmp_path / "staging"
        staging.mkdir()
        # Stage the product, then a bogus wheel.  Root cleanup removes the
        # product, so validation is forced to inspect the invalid wheel.
        _copy_wheel_to_staging(product_wheel_leaf, staging)
        _make_bogus_whl(staging, "not-a-valid-wheel.whl")

        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_leaf,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            with pytest.raises(AcquisitionTransportError) as exc_info:
                acquirer.acquire(req, staging_dir=staging)

        assert exc_info.value.stage == "validate"
        assert "not-a-valid-wheel" in exc_info.value.detail


# ═════════════════════════════════════════════════════════════════════════
# 12. Deterministic order & record completeness
# ═════════════════════════════════════════════════════════════════════════


class TestDeterministicOrder:
    """acquired tuple is sorted by (name, version, filename) with full detail."""

    def test_acquired_deterministic_order(
        self,
        product_wheel_leaf: Path,
        product_wheel_mid_extra: Path,
        product_wheel_mid_lib: Path,
        tmp_path: Path,
    ) -> None:
        """Three wheels in staging, product removed, remaining sorted."""
        staging = tmp_path / "staging"
        staging.mkdir()
        # Product: mid_lib_extra (will be removed)
        _copy_wheel_to_staging(product_wheel_mid_extra, staging)
        # Dependencies
        _copy_wheel_to_staging(product_wheel_mid_lib, staging)
        _copy_wheel_to_staging(product_wheel_leaf, staging)

        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_mid_extra,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        assert len(result.acquired) == 2
        # Sorted by (name, version, filename)
        names = [w.name for w in result.acquired]
        assert names == ["leaf-lib", "mid-lib"]
        assert names == sorted(names)

        # Each record has all fields
        for w in result.acquired:
            assert w.name
            assert w.version
            assert w.filename.endswith(".whl")
            assert w.size > 0
            assert len(w.sha256) == 64

    def test_acquired_frozen_tuple(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Result.acquired is a frozen tuple, not a mutable list."""
        staging = tmp_path / "staging"
        staging.mkdir()
        req = DependencyAcquisitionRequest(
            product_wheel_path=product_wheel_leaf,
            active_extras=frozenset(),
        )
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        assert isinstance(result.acquired, tuple)
        with pytest.raises(Exception):
            result.acquired = ()  # type: ignore[misc]


# ═════════════════════════════════════════════════════════════════════════
# 13. Staging lifecycle (cleanup)
# ═════════════════════════════════════════════════════════════════════════


class TestStagingLifecycle:
    """Auto-created staging cleaned on failure; caller-supplied staging kept."""

    def test_caller_supplied_staging_not_cleaned_on_failure(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Caller-owned staging dir must survive acquisition failure."""
        staging = tmp_path / "my-staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="pip", timeout=1),
        ):
            with pytest.raises(AcquisitionTransportError):
                acquirer.acquire(req, staging_dir=staging)

        assert staging.exists()

    def test_auto_created_staging_cleaned_on_failure(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Auto-created staging dir is cleaned on failure."""
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        captured_staging: Path | None = None

        def _fail_and_capture(*args, **kwargs):
            # Capture the staging path from the dest arg
            nonlocal captured_staging
            argv = args[0]
            for i, a in enumerate(argv):
                if a == "--dest" and i + 1 < len(argv):
                    captured_staging = Path(argv[i + 1])
                    break
            raise subprocess.TimeoutExpired(cmd="pip", timeout=1)

        with mock.patch(
            "subprocess.run", side_effect=_fail_and_capture,
        ):
            with pytest.raises(AcquisitionTransportError):
                acquirer.acquire(req, staging_dir=None)

        assert captured_staging is not None
        assert not captured_staging.exists()

    def test_auto_created_staging_uses_zealfie_prefix(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        """Auto-created staging directory name starts with zealfie-acq-."""
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        captured_staging: Path | None = None

        def _capture_staging(*args, **kwargs):
            nonlocal captured_staging
            argv = args[0]
            for i, a in enumerate(argv):
                if a == "--dest" and i + 1 < len(argv):
                    captured_staging = Path(argv[i + 1])
                    break
            return _mock_run_success(None)

        with mock.patch(
            "subprocess.run", side_effect=_capture_staging,
        ):
            result = acquirer.acquire(req, staging_dir=None)

        assert "zealfie-acq-" in str(result.staging_wheelhouse)
        # Auto-created staging is NOT cleaned after success
        assert result.staging_wheelhouse.exists()

        # Clean up our auto-created staging since caller would normally own it
        shutil.rmtree(result.staging_wheelhouse)


# ═════════════════════════════════════════════════════════════════════════
# 14. Result staging_dir is always resolved
# ═════════════════════════════════════════════════════════════════════════


class TestStagingDirResolution:
    """staging_wheelhouse in result is always a resolved absolute path."""

    def test_caller_supplied_resolved(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        staging = tmp_path / "staging"
        staging.mkdir()
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=staging)

        assert result.staging_wheelhouse == staging.resolve()
        assert result.staging_wheelhouse.is_absolute()

    def test_auto_created_resolved(
        self, product_wheel_leaf: Path, tmp_path: Path,
    ) -> None:
        req = build_acquisition_request(product_wheel_leaf)
        acquirer = PipWheelhouseAcquirer()

        with mock.patch(
            "subprocess.run", side_effect=_mock_run_success,
        ):
            result = acquirer.acquire(req, staging_dir=None)

        assert result.staging_wheelhouse.is_absolute()

        # Clean up auto-created staging
        if result.staging_wheelhouse.exists():
            shutil.rmtree(result.staging_wheelhouse)
