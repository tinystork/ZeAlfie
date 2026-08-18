"""ZeAlfie self-update pending marker persistence (ZA-M1-4 LOT D §E).

The pending marker records a *staged* (built + verified) self-update wheel
so a separate, standalone activator process can later apply it while the
GUI is not running.  Writing the marker never installs anything.

The marker is written atomically (mkstemp + fsync + os.replace) and read
leniently (corrupt/absent → refuse).
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from zealfie.runtime.layout import RuntimeLayout

from .resolver import UpdateResolution
from .verify import StagedSelfUpdate, stage_update

__all__ = [
    "PENDING_FILENAME",
    "PENDING_SCHEMA_VERSION",
    "PendingMarkerError",
    "PendingSelfUpdate",
    "clear_pending_marker",
    "load_pending_marker",
    "pending_marker_path",
    "stage_and_persist",
    "write_pending_marker",
]

PENDING_FILENAME = "self-update-pending.json"
PENDING_SCHEMA_VERSION = 1

_REQUIRED_KEYS = (
    "target_version",
    "channel",
    "commit_sha",
    "wheel_path",
    "wheel_sha256",
    "size",
    "created_at",
)


class PendingMarkerError(ValueError):
    """Raised when a pending marker is corrupt or structurally invalid."""


@dataclass(frozen=True, slots=True)
class PendingSelfUpdate:
    """A persisted, staged self-update awaiting activation."""

    target_version: str
    channel: str
    commit_sha: str
    wheel_path: str
    wheel_sha256: str
    size: int
    created_at: str


def pending_marker_path(layout: RuntimeLayout) -> Path:
    """Absolute path of the pending marker under the runtime state dir."""
    return layout.state_dir / PENDING_FILENAME


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Write (atomic)
# ---------------------------------------------------------------------------


def write_pending_marker(layout: RuntimeLayout, pending: PendingSelfUpdate) -> None:
    """Atomically persist *pending* to ``layout.state_dir``.

    Uses mkstemp + fsync + os.replace so a reader never observes a
    half-written marker.
    """
    path = pending_marker_path(layout)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PENDING_SCHEMA_VERSION,
        "target_version": pending.target_version,
        "channel": pending.channel,
        "commit_sha": pending.commit_sha,
        "wheel_path": pending.wheel_path,
        "wheel_sha256": pending.wheel_sha256,
        "size": pending.size,
        "created_at": pending.created_at,
    }
    tmp_path: str | None = None
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path is not None and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
    except OSError as exc:
        raise PendingMarkerError(
            f"cannot write pending self-update marker {path}: {exc}"
        ) from exc


def clear_pending_marker(layout: RuntimeLayout) -> None:
    """Remove the pending marker (idempotent; missing file is a no-op)."""
    try:
        pending_marker_path(layout).unlink(missing_ok=True)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Read (lenient)
# ---------------------------------------------------------------------------


def load_pending_marker(layout: RuntimeLayout) -> PendingSelfUpdate | None:
    """Load the pending marker, or ``None`` when absent.

    Raises :class:`PendingMarkerError` when the marker exists but is
    unreadable, not JSON, has an unsupported schema, or is missing/ill-typed
    fields.  Callers (the activator) treat ``None`` and :class:`PendingMarkerError`
    alike: refuse, never trust a broken marker.
    """
    path = pending_marker_path(layout)
    if not path.is_file():
        return None

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PendingMarkerError(
            f"pending self-update marker is unreadable: {exc}"
        ) from exc
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PendingMarkerError(
            f"pending self-update marker is not valid JSON: {exc}"
        ) from exc

    if not isinstance(payload, dict):
        raise PendingMarkerError(
            "pending self-update marker root must be a JSON object"
        )

    schema = payload.get("schema_version")
    if schema != PENDING_SCHEMA_VERSION:
        raise PendingMarkerError(
            f"unsupported pending marker schema_version: {schema!r} "
            f"(supported: {PENDING_SCHEMA_VERSION})"
        )

    target_version = _required_str(payload, "target_version")
    channel = _required_str(payload, "channel")
    commit_sha = _required_hex40(payload, "commit_sha")
    wheel_path = _required_str(payload, "wheel_path")
    wheel_sha256 = _required_hex64(payload, "wheel_sha256")
    size = _required_int(payload, "size")
    created_at = _required_str(payload, "created_at")

    return PendingSelfUpdate(
        target_version=target_version,
        channel=channel,
        commit_sha=commit_sha,
        wheel_path=wheel_path,
        wheel_sha256=wheel_sha256,
        size=size,
        created_at=created_at,
    )


# ---------------------------------------------------------------------------
# Stage + persist (never installs)
# ---------------------------------------------------------------------------


def stage_and_persist(
    resolution: UpdateResolution,
    *,
    fetcher,
    work_root: str | Path,
    layout: RuntimeLayout,
) -> StagedSelfUpdate:
    """Stage (build + verify) *resolution*, then persist the pending marker.

    Performs the §C staging then writes the pending marker.  Never installs.
    """
    staged = stage_update(resolution, fetcher=fetcher, work_root=work_root)
    pending = PendingSelfUpdate(
        target_version=staged.wheel_version,
        channel=resolution.channel,
        commit_sha=resolution.commit_sha,
        wheel_path=str(staged.wheel_path),
        wheel_sha256=staged.wheel_sha256,
        size=staged.size,
        created_at=_utc_now_iso(),
    )
    write_pending_marker(layout, pending)
    return staged


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def _required_str(payload: dict, key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise PendingMarkerError(
            f"pending marker field {key!r} must be a non-empty string"
        )
    return value


def _required_int(payload: dict, key: str) -> int:
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PendingMarkerError(
            f"pending marker field {key!r} must be a non-negative integer"
        )
    return value


def _required_hex40(payload: dict, key: str) -> str:
    value = _required_str(payload, key).strip().lower()
    if len(value) != 40 or not all(c in "0123456789abcdef" for c in value):
        raise PendingMarkerError(
            f"pending marker field {key!r} must be 40 hex characters"
        )
    return value


def _required_hex64(payload: dict, key: str) -> str:
    value = _required_str(payload, key).strip().lower()
    if len(value) != 64 or not all(c in "0123456789abcdef" for c in value):
        raise PendingMarkerError(
            f"pending marker field {key!r} must be 64 hex characters"
        )
    return value
