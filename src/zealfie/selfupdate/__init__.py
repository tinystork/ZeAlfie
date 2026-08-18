"""ZeAlfie self-update subsystem (ZA-M1-4 LOT D).

resolve → acquire → build → verify → stage → persist pending marker →
controlled restart handoff → standalone activator replacement.

The running GUI/CLI process NEVER installs into its own environment; the
actual replacement is performed by the standalone activator
(:func:`zealfie.selfupdate.activator.apply_pending_update`) when the GUI is
not active.
"""

from __future__ import annotations

from .activator import (
    ApplyStatus,
    SelfUpdateApplyError,
    SelfUpdateApplyResult,
    apply_pending_update,
)
from .identity import (
    InstallMode,
    ZeAlfieIdentity,
    detect_identity,
    self_update_supported,
)
from .plan import SelfUpdatePlan, SelfUpdateStatus, build_self_update_plan
from .resolver import (
    DEFAULT_SOURCE_OWNER,
    DEFAULT_SOURCE_REPO,
    GitHubTagsLister,
    SelfUpdateResolutionError,
    UpdateResolution,
    resolve_available_update,
)
from .state import (
    PENDING_FILENAME,
    PENDING_SCHEMA_VERSION,
    PendingMarkerError,
    PendingSelfUpdate,
    clear_pending_marker,
    load_pending_marker,
    pending_marker_path,
    stage_and_persist,
    write_pending_marker,
)
from .verify import (
    SelfUpdateStagingError,
    StagedSelfUpdate,
    compute_sha256,
    stage_update,
)

__all__ = [
    "ApplyStatus",
    "DEFAULT_SOURCE_OWNER",
    "DEFAULT_SOURCE_REPO",
    "GitHubTagsLister",
    "InstallMode",
    "PENDING_FILENAME",
    "PENDING_SCHEMA_VERSION",
    "PendingMarkerError",
    "PendingSelfUpdate",
    "SelfUpdateApplyError",
    "SelfUpdateApplyResult",
    "SelfUpdatePlan",
    "SelfUpdateResolutionError",
    "SelfUpdateStagingError",
    "SelfUpdateStatus",
    "StagedSelfUpdate",
    "UpdateResolution",
    "ZeAlfieIdentity",
    "apply_pending_update",
    "build_self_update_plan",
    "clear_pending_marker",
    "compute_sha256",
    "detect_identity",
    "load_pending_marker",
    "pending_marker_path",
    "resolve_available_update",
    "self_update_supported",
    "stage_and_persist",
    "stage_update",
    "write_pending_marker",
]
