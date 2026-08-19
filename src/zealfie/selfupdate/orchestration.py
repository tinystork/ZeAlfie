"""Qt-free GUI self-update orchestration (ZA-M1-4.2).

A thin state machine that REUSES the existing self-update engine (identity →
plan → stage → pending → apply) and reduces it to the small set of outcomes
the GUI shell needs:

* ``NOT_SUPPORTED`` — SOURCE / EDITABLE / UNKNOWN install → nothing attempted,
  nothing shown (a developer must never see an anxiety-inducing error);
* ``UP_TO_DATE``     — nothing to propose;
* ``UPDATE_READY``   — a *verified* candidate is staged (or a valid pending
  already exists) → the GUI may propose it;
* ``FAILED``         — network / GitHub / build / verify / stage failure →
  silent, the GUI stays usable, no mutation.

This module performs no network/build/pip work on the caller thread: callers
run :func:`run_self_update_check` on a background worker.  It never installs
and never mutates the running environment — it only reuses the engine's
``build_self_update_plan`` (read-only) and ``stage_and_persist`` (build +
verify + persist a pending marker; never install).

The engine's check → stage → apply conceptual separation is preserved:
:func:`run_self_update_check` composes *check* (plan) then *stage*
(stage_and_persist) and reuses a valid pending marker instead of re-staging;
the apply is intentionally left to the caller (the GUI invokes
:func:`zealfie.selfupdate.activator.apply_pending_update` separately, exactly
once, via a worker).
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from zealfie.runtime.layout import RuntimeLayout

from .activator import apply_pending_update
from .identity import ZeAlfieIdentity, detect_identity, self_update_supported
from .plan import SelfUpdateStatus, build_self_update_plan
from .state import (
    PendingMarkerError,
    PendingSelfUpdate,
    load_pending_marker,
    stage_and_persist,
)

__all__ = [
    "GuiSelfUpdateResult",
    "GuiSelfUpdateStatus",
    "make_self_update_apply_fn",
    "make_self_update_check_fn",
    "run_self_update_check",
]


class GuiSelfUpdateStatus(StrEnum):
    """Terminal outcomes the GUI shell cares about (silent unless UPDATE_READY)."""

    NOT_SUPPORTED = "NOT_SUPPORTED"
    UP_TO_DATE = "UP_TO_DATE"
    UPDATE_READY = "UPDATE_READY"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class GuiSelfUpdateResult:
    """Reduced self-update outcome for the GUI shell.

    ``version`` is the target version only for ``UPDATE_READY``.  ``reason``
    is diagnostics for logs (never rendered to the user verbatim).
    """

    status: GuiSelfUpdateStatus
    version: str | None = None
    reason: str | None = None


def _pending_matches_resolution(
    pending: PendingSelfUpdate | None,
    resolution,
) -> bool:
    """Return ``True`` when *pending* targets exactly *resolution* (reuse it).

    A pending marker is only reused when it is a *valid* marker (the loader
    already rejected corrupt markers) whose channel and target version match
    the currently-resolved stable target.  Anything else is stale and a fresh
    candidate is staged.
    """
    if pending is None:
        return False
    return (
        pending.channel == resolution.channel
        and pending.target_version == resolution.available_version
    )


def run_self_update_check(
    *,
    channel: str = "stable",
    resolver,
    tags_lister,
    fetcher,
    work_root: str | Path,
    layout: RuntimeLayout,
    identity: ZeAlfieIdentity | None = None,
    _detect=detect_identity,
    _supported=self_update_supported,
    _plan=build_self_update_plan,
    _stage=stage_and_persist,
    _load_pending=load_pending_marker,
) -> GuiSelfUpdateResult:
    """Run the GUI self-update check + stage pipeline, fail-closed.

    Sequence (reusing the existing engine, never installing):

    1. detect the install identity and refuse non-installed modes honestly;
    2. build the read-only stable plan (network resolve + tag listing);
    3. ``UP_TO_DATE`` / ``NOT_SUPPORTED`` / ``CHECK_FAILED`` → terminal;
    4. ``UPDATE_AVAILABLE`` → reuse a valid matching pending marker when one
       exists (no redundant network/build work), otherwise stage + persist a
       fresh verified candidate;
    5. any stage/build/verify failure → ``FAILED`` (silent).

    Every injected primitive is a seam for hermetic tests; the defaults are
    the real engine functions.
    """
    if identity is None:
        identity = _detect()

    supported, reason = _supported(identity)
    if not supported:
        return GuiSelfUpdateResult(
            GuiSelfUpdateStatus.NOT_SUPPORTED, reason=reason
        )

    plan = _plan(
        identity, channel=channel, resolver=resolver, tags_lister=tags_lister
    )

    if plan.status is SelfUpdateStatus.NOT_SUPPORTED:
        return GuiSelfUpdateResult(
            GuiSelfUpdateStatus.NOT_SUPPORTED, reason=plan.reason
        )
    if plan.status is SelfUpdateStatus.CHECK_FAILED:
        return GuiSelfUpdateResult(GuiSelfUpdateStatus.FAILED, reason=plan.reason)
    if plan.status is SelfUpdateStatus.UP_TO_DATE:
        return GuiSelfUpdateResult(GuiSelfUpdateStatus.UP_TO_DATE)

    # UPDATE_AVAILABLE
    resolution = plan.resolution
    try:
        pending = _load_pending(layout)
    except PendingMarkerError:
        # A corrupt marker must never crash the shell; treat as absent and
        # stage a fresh candidate (which overwrites it atomically).
        pending = None

    if _pending_matches_resolution(pending, resolution):
        return GuiSelfUpdateResult(
            GuiSelfUpdateStatus.UPDATE_READY,
            version=pending.target_version,
        )

    try:
        staged = _stage(
            resolution, fetcher=fetcher, work_root=work_root, layout=layout
        )
    except Exception as exc:  # noqa: BLE001 - honest silent failure
        return GuiSelfUpdateResult(GuiSelfUpdateStatus.FAILED, reason=str(exc))

    return GuiSelfUpdateResult(
        GuiSelfUpdateStatus.UPDATE_READY,
        version=staged.wheel_version,
    )


def make_self_update_check_fn(
    *,
    resolver,
    tags_lister,
    fetcher,
    work_root: str | Path,
    layout: RuntimeLayout,
    channel: str = "stable",
):
    """Bind real engine deps into a zero-arg check callable for the GUI worker.

    The returned callable performs the full check → (reuse-pending-or-stage)
    pipeline when called; the GUI invokes it off the Qt thread.
    """
    return functools.partial(
        run_self_update_check,
        channel=channel,
        resolver=resolver,
        tags_lister=tags_lister,
        fetcher=fetcher,
        work_root=work_root,
        layout=layout,
    )


def make_self_update_apply_fn(
    *,
    layout: RuntimeLayout,
    runtime_root: str | Path | None = None,
):
    """Bind the standalone activator into a zero-arg apply callable.

    The returned callable invokes :func:`apply_pending_update` (the existing
    engine apply; never reimplemented).  The GUI invokes it exactly once, on
    a worker thread, after the user accepts the update.
    """
    root = runtime_root if runtime_root is not None else layout.root
    return functools.partial(apply_pending_update, layout=layout, runtime_root=root)
