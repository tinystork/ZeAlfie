"""ZeAlfie self-update verification + staging (ZA-M1-4 LOT D §C).

Turns a resolved update into a locally *built* and *verified* wheel, staged
under a work root.  Staging is fail-closed: any version/distribution/size
mismatch aborts and nothing is persisted.

This module only builds and verifies — it never installs anything.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from zealfie.building import WheelInspectionError, build_wheel, inspect_wheel
from zealfie.common import normalise_distribution_name
from zealfie.sources import RemoteSource, ResolvedSource
from zealfie.sources.acquisition import acquire_source

from .resolver import UpdateResolution

__all__ = [
    "SelfUpdateStagingError",
    "StagedSelfUpdate",
    "compute_sha256",
    "stage_update",
]

_DISTRIBUTION_NAME = "zealfie"


class SelfUpdateStagingError(RuntimeError):
    """Raised when a self-update wheel cannot be staged fail-closed."""


@dataclass(frozen=True, slots=True)
class StagedSelfUpdate:
    """A built and verified self-update wheel awaiting activation.

    ``wheel_sha256`` + ``size`` are the recorded integrity proof, re-verified
    byte-for-byte by the activator before any install.
    """

    resolution: UpdateResolution
    wheel_path: Path
    wheel_sha256: str
    size: int
    wheel_version: str


def compute_sha256(path: Path) -> str:
    """Streaming SHA-256 of *path* (never loads the whole file into RAM)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(1 << 20)  # 1 MiB
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def stage_update(
    resolution: UpdateResolution,
    *,
    fetcher,
    work_root: str | Path,
) -> StagedSelfUpdate:
    """Acquire, build, and verify a self-update wheel for *resolution*.

    * Acquires the exact ``resolution.commit_sha`` source archive (immutable
      provenance) via the injected *fetcher* + :func:`acquire_source`;
    * builds a wheel via :func:`zealfie.building.build_wheel`;
    * inspects the wheel and fails closed unless the wheel version matches
      ``resolution.available_version`` and the distribution is ``zealfie``;
    * records the wheel's SHA-256 + size for later re-verification.

    Raises :class:`SelfUpdateStagingError` on any mismatch or build failure.
    Nothing is persisted on failure.
    """
    work = Path(work_root)
    try:
        work.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise SelfUpdateStagingError(
            f"cannot create self-update work root {work}: {exc}"
        ) from exc

    source = RemoteSource(
        owner=resolution.source_owner,
        repo=resolution.source_repo,
        ref=resolution.requested_ref,
    )
    resolved = ResolvedSource(
        source=source,
        commit_sha=resolution.commit_sha,
    )

    with acquire_source(resolved, fetcher=fetcher, stage_root=work) as staged:
        wheel_dir = work / "wheels"
        wheel_dir.mkdir(parents=True, exist_ok=True)
        wheel_path = build_wheel(staged.stage_dir, output_dir=wheel_dir)

        try:
            info = inspect_wheel(wheel_path)
        except WheelInspectionError as exc:
            raise SelfUpdateStagingError(
                f"built wheel inspection failed: {exc}"
            ) from exc

        if info.version != resolution.available_version:
            raise SelfUpdateStagingError(
                f"wheel version mismatch: expected "
                f"{resolution.available_version!r}, built {info.version!r}"
            )
        if info.distribution_name != normalise_distribution_name(_DISTRIBUTION_NAME):
            raise SelfUpdateStagingError(
                f"wheel distribution mismatch: expected {_DISTRIBUTION_NAME!r}, "
                f"built {info.distribution_name!r}"
            )

        sha256 = compute_sha256(wheel_path)
        size = wheel_path.stat().st_size

        return StagedSelfUpdate(
            resolution=resolution,
            wheel_path=wheel_path,
            wheel_sha256=sha256,
            size=size,
            wheel_version=info.version,
        )
