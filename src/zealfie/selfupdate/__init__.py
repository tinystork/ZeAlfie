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
from .orchestration import (
    GuiSelfUpdateResult,
    GuiSelfUpdateStatus,
    make_self_update_apply_fn,
    make_self_update_check_fn,
    run_self_update_check,
)
from .restart import (
    restart_gui_after_update,
    spawn_gui_process,
    spawn_restart_supervisor,
)

__all__ = [
    "ApplyStatus",
    "DEFAULT_SOURCE_OWNER",
    "DEFAULT_SOURCE_REPO",
    "GitHubTagsLister",
    "GuiSelfUpdateResult",
    "GuiSelfUpdateStatus",
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
    "make_self_update_apply_fn",
    "make_self_update_check_fn",
    "compute_sha256",
    "detect_identity",
    "load_pending_marker",
    "pending_marker_path",
    "resolve_available_update",
    "restart_gui_after_update",
    "run_self_update_check",
    "self_update_supported",
    "spawn_gui_process",
    "spawn_restart_supervisor",
    "stage_and_persist",
    "stage_update",
    "write_pending_marker",
]
