"""Minimal pip wheelhouse acquirer (M1-2D.4.2B — LOT 1).

Single-responsibility transport: turns a verified local product wheel
+ active extras into a local dependency wheelhouse usable by the
existing resolver.

No ABC/protocol/generic framework.  No inventory file.  No multi-product
handling.  No GPU/CUDA/progress/QThread.  No RuntimeLock generation.

ZA-M1-3A.3 LOT C.2: when handed a shared :class:`~zealfie.runtime.artifact_cache.ArtifactCacheStore`
and proven ``(distribution, version)`` identities from the installed
runtime lock, exact cached wheels (digest + size + tags re-verified,
fail-closed) are seeded as ``--find-links`` candidates so pip satisfies
those requirements without network.  Without both, the call is
byte-identical to the pre-cache behaviour.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from zealfie.common.subprocess_platform import technical_subprocess_platform_kwargs
from zealfie.runtime.artifact_cache import (
    ArtifactCacheStore,
    materialize_cached,
)

from .acquisition import (
    AcquiredWheel,
    AcquisitionTransportError,
    DependencyAcquisitionRequest,
    DependencyAcquisitionResult,
)
from .host_tags import default_compatible_tags


class PipWheelhouseAcquirer:
    """Minimal pip-based dependency acquisition into a local wheelhouse.

    Uses ``pip download`` (list argv, no shell) to pull transitive
    dependencies of a verified local product wheel.  Strips the product
    wheel copy from staging so the result contains ONLY dependencies.

    The existing resolver globs ``*.whl`` from *staging_wheelhouse*;
    ``acquired`` is the in-memory inventory for deterministic processing.

    Parameters
    ----------
    index_url:
        PEP 503 simple repository index URL passed to ``pip download
        --index-url``.  Defaults to PyPI (``https://pypi.org/simple``).
        Set to a ``file://`` local index for offline testing or private
        index for air-gapped environments.
    """

    def __init__(
        self,
        *,
        index_url: str = "https://pypi.org/simple",
    ) -> None:
        self._index_url = index_url

    def acquire(
        self,
        request: DependencyAcquisitionRequest,
        *,
        staging_dir: Path | None = None,
        timeout_seconds: int = 300,
        cache: ArtifactCacheStore | None = None,
        proven_requirements: tuple[tuple[str, str], ...] = (),
    ) -> DependencyAcquisitionResult:
        """Acquire transitive dependencies for a verified product wheel.

        Parameters
        ----------
        request:
            Validated acquisition request from
            :func:`~.build_acquisition_request`.
        staging_dir:
            Directory for the dependency wheelhouse.  If ``None``, a
            private temp directory is created.  The caller owns cleanup
            after its own lock/plan/apply/TOCTOU/install/activation
            window.
        timeout_seconds:
            Maximum subprocess time in seconds.
        cache:
            Optional shared verified artifact cache (ZA-M1-3A.3 LOT C.2).
            When provided together with *proven_requirements*, wheels whose
            exact identity is both proven by the installed lock and
            digest-verified in the cache are seeded as ``--find-links``
            candidates: pip then satisfies those requirements locally
            instead of downloading them.  Without both, the call is
            byte-identical to the pre-cache behaviour (a cache miss never
            changes the first run).
        proven_requirements:
            ``(canonical distribution, version)`` identities from the
            active installed-runtime lock.  The acquirer never invents
            these proofs itself: an empty tuple disables cache reuse
            entirely (normal pip acquisition).

        Returns
        -------
        DependencyAcquisitionResult
            Staging wheelhouse path + ordered tuple of acquired
            dependencies (product wheel excluded).

        Raises
        ------
        AcquisitionTransportError
            On any transport, validation, or cleanup failure.
        """
        # ── Staging lifecycle ──────────────────────────────────────────
        own_staging = staging_dir is None
        if own_staging:
            staging_dir = Path(
                tempfile.mkdtemp(prefix="zealfie-acq-")
            ).resolve()
        else:
            staging_dir = staging_dir.resolve()
            staging_dir.mkdir(parents=True, exist_ok=True)

        seed_dir: Path | None = None
        try:
            # ── Cache seeding (fail-closed; see module contract) ───────
            seed_dir = _seed_from_cache(
                request,
                proven_requirements=proven_requirements,
                cache=cache,
            )

            # ── Build pip argv ─────────────────────────────────────────
            argv = _build_pip_argv(
                product_wheel_path=request.product_wheel_path,
                active_extras=request.active_extras,
                dest=staging_dir,
                index_url=self._index_url,
                find_links=seed_dir,
            )

            # ── Build sanitised env ────────────────────────────────────
            env = _build_pip_env()

            # ── Execute pip download ───────────────────────────────────
            try:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=timeout_seconds,
                    env=env,
                    **technical_subprocess_platform_kwargs(),
                )
            except subprocess.TimeoutExpired as exc:
                raise AcquisitionTransportError(
                    "pip-timeout",
                    f"pip download timed out after {exc.timeout}s",
                ) from exc
            except FileNotFoundError as exc:
                raise AcquisitionTransportError(
                    "pip-invoke",
                    f"cannot invoke pip: {exc}",
                ) from exc
            except OSError as exc:
                raise AcquisitionTransportError(
                    "pip-invoke",
                    f"OS error invoking pip: {exc}",
                ) from exc

            if proc.returncode != 0:
                detail = _tail(proc.stderr) or _tail(proc.stdout) or "(no output)"
                raise AcquisitionTransportError("pip-download", detail)

            # ── Remove root product wheel from staging ─────────────────
            _remove_product_wheel_from_staging(
                staging_dir, request.product_wheel_path
            )

            # ── Validate remaining wheels ──────────────────────────────
            acquired = _collect_acquired(staging_dir)

            # ── Feed the verified result into the shared cache ─────────
            # (ZA-M1-3A.3 LOT C.2).  Best-effort: ``put`` never raises.
            if cache is not None:
                for wheel in acquired:
                    cache.put(
                        wheel.wheel_path,
                        kind="dependency",
                        distribution=wheel.name,
                        version=wheel.version,
                    )

            return DependencyAcquisitionResult(
                staging_wheelhouse=staging_dir,
                acquired=acquired,
                seeded_from_cache=seed_dir is not None,
            )

        except Exception:
            # Best-effort cleanup only for auto-created staging.
            if own_staging and staging_dir.exists():
                _rmtree_best_effort(staging_dir)
            raise
        finally:
            if seed_dir is not None:
                _rmtree_best_effort(seed_dir)


# =========================================================================
# Internal helpers
# =========================================================================


def _seed_from_cache(
    request: DependencyAcquisitionRequest,
    *,
    proven_requirements: tuple[tuple[str, str], ...],
    cache: ArtifactCacheStore | None,
) -> Path | None:
    """Seed a private find-links directory from cache-verified wheels.

    For each proven ``(name, version)`` identity (from the installed lock):

    * the current product wheel itself is skipped (it is the pip root
      requirement and is never a dependency of itself);
    * exactly one cache entry must match the identity AND its file must
      pass digest/size re-verification AND at least one wheel tag must be
      host compatible — otherwise the identity is skipped (never invented,
      never forced: pip falls back to the index for it).

    Returns the seed directory when at least one wheel was seeded, else
    ``None``.  Never raises.
    """
    if cache is None or not proven_requirements:
        return None
    try:
        product = AcquiredWheel.from_wheel_file(request.product_wheel_path)
        product_identity = (product.name, product.version)
    except Exception:
        product_identity = None
    try:
        host_tags = default_compatible_tags()
    except Exception:
        host_tags = None

    seed_dir = Path(tempfile.mkdtemp(prefix="zealfie-seed-")).resolve()
    seeded_any = False
    for name, version in sorted(set(proven_requirements)):
        if product_identity is not None and (
            name, version
        ) == product_identity:
            continue
        cached = cache.resolve_dependency(
            name, version, compatible_tags=host_tags
        )
        if cached is None:
            continue
        dest = seed_dir / cached.name
        if dest.is_file():
            continue
        if materialize_cached(cached, dest):
            seeded_any = True
    if not seeded_any:
        _rmtree_best_effort(seed_dir)
        return None
    return seed_dir


def _build_pip_argv(
    *,
    product_wheel_path: Path,
    active_extras: frozenset[str],
    dest: Path,
    index_url: str = "https://pypi.org/simple",
    find_links: Path | None = None,
) -> list[str]:
    """Build the ``pip download`` arg list (no shell, no --no-deps).

    *find_links* (cache-seeded candidates) is added only when non-empty so
    a cache-miss run keeps the exact pre-cache argv shape.
    """
    root_req = str(product_wheel_path)
    if active_extras:
        sorted_extras = ",".join(sorted(active_extras))
        root_req = f"{root_req}[{sorted_extras}]"

    argv = [
        sys.executable,
        "-m",
        "pip",
        "download",
        "--isolated",
        "--no-input",
        "--disable-pip-version-check",
        "--only-binary=:all:",
        "--index-url",
        index_url,
        "--dest",
        str(dest),
    ]
    if find_links is not None:
        argv += ["--find-links", str(find_links)]
    argv.append(root_req)
    return argv


def _build_pip_env() -> dict[str, str]:
    """Return a copy of ``os.environ`` with all ``PIP_*`` variables removed.

    Does NOT mutate ``os.environ``.  ``--isolated`` is the primary
    guarantee; ``PIP_*`` stripping is defence in depth.
    """
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("PIP_"):
            del env[key]
    return env


def _tail(text: str, lines: int = 20) -> str:
    """Return at most the last *lines* non-empty lines of *text*."""
    text = text.strip()
    if not text:
        return ""
    all_lines = text.splitlines()
    return "\n".join(all_lines[-lines:])


def _remove_product_wheel_from_staging(
    staging: Path,
    product_wheel_path: Path,
) -> None:
    """Remove the product wheel copy from staging by name+version+sha256.

    Raises ``AcquisitionTransportError("root-cleanup", ...)`` if more than
    one exact match is found.  Zero matches is OK (pip may not copy the
    product wheel, or tests simulate dependency-only output).
    """
    product = AcquiredWheel.from_wheel_file(product_wheel_path)

    matches: list[Path] = []
    for whl_path in sorted(staging.glob("*.whl")):
        try:
            candidate = AcquiredWheel.from_wheel_file(whl_path)
        except Exception:
            # Unparseable → not a product match (validation catches later).
            continue
        if (
            candidate.name == product.name
            and candidate.version == product.version
            and candidate.sha256 == product.sha256
        ):
            matches.append(whl_path)

    if len(matches) > 1:
        raise AcquisitionTransportError(
            "root-cleanup",
            f"found {len(matches)} exact product copies in staging: "
            + ", ".join(str(m.name) for m in matches),
        )

    for path in matches:
        path.unlink()


def _collect_acquired(staging: Path) -> tuple[AcquiredWheel, ...]:
    """Scan remaining wheels in staging, validate, and sort deterministically.

    Raises ``AcquisitionTransportError("validate", ...)`` if any wheel
    cannot be parsed.
    """
    wheels: list[AcquiredWheel] = []
    for whl_path in sorted(staging.glob("*.whl")):
        try:
            wheels.append(AcquiredWheel.from_wheel_file(whl_path))
        except Exception as exc:
            raise AcquisitionTransportError(
                "validate",
                f"cannot parse staged wheel {whl_path.name}: {exc}",
            ) from exc

    # Deterministic order: (name, version, filename)
    wheels.sort(key=lambda w: (w.name, w.version, w.filename))
    return tuple(wheels)


def _rmtree_best_effort(directory: Path) -> None:
    """Best-effort recursive removal; ignore all errors."""
    import shutil

    try:
        shutil.rmtree(directory)
    except Exception:
        pass
