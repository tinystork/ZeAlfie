"""Structured install progress contract (M1-2D.6).

Pure-Python, Qt-free progress model shared by the backend service and
the Qt GUI bridge.  Progress *observes* a transaction; it never controls
runtime behaviour, never exposes an ETA, and never parses pip output.

Phases map to real backend workflow boundaries.  Percent values are
monotone non-decreasing and cover the closed ``0..100`` range.  ``100``
is emitted only via :data:`InstallPhase.COMPLETED`, which the service
emits exclusively after a successful install and selection persistence.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class InstallPhase(Enum):
    """Coarse install workflow phases, in execution order."""

    PREPARING = "preparing"
    RESOLVING_SOURCE = "resolving_source"
    DOWNLOADING_SOURCE = "downloading_source"
    BUILDING_PRODUCT = "building_product"
    ACQUIRING_DEPENDENCIES = "acquiring_dependencies"
    PLANNING_RUNTIME = "planning_runtime"
    INSTALLING_RUNTIME = "installing_runtime"
    VALIDATING = "validating"
    ACTIVATING = "activating"
    COMPLETED = "completed"


@dataclass(frozen=True, slots=True)
class InstallProgress:
    """A single progress observation emitted by the backend.

    ``percent`` is a determinate ``0..100`` value.  ``message`` is a
    short human-readable phase description (no ETA, no pip parsing).
    """

    phase: InstallPhase
    percent: int
    message: str


# Monotone, deterministic base percentage for each phase.  Kept in
# execution order so the emitted sequence is non-decreasing by
# construction.
PHASE_PERCENT: dict[InstallPhase, int] = {
    InstallPhase.PREPARING: 0,
    InstallPhase.RESOLVING_SOURCE: 5,
    InstallPhase.DOWNLOADING_SOURCE: 12,
    InstallPhase.BUILDING_PRODUCT: 20,
    InstallPhase.ACQUIRING_DEPENDENCIES: 30,
    InstallPhase.PLANNING_RUNTIME: 45,
    InstallPhase.INSTALLING_RUNTIME: 60,
    InstallPhase.VALIDATING: 90,
    InstallPhase.ACTIVATING: 95,
    InstallPhase.COMPLETED: 100,
}

# Per-package install progress is interpolated within this band, between
# the INSTALLING_RUNTIME base and the VALIDATING milestone, so a long
# dependency/component materialization loop still moves the bar
# determinately without fabricating fine-grained pip progress.
INSTALL_LOOP_START = 62
INSTALL_LOOP_END = 88


def interpolate_percent(
    index: int,
    total: int,
    *,
    start: int = INSTALL_LOOP_START,
    end: int = INSTALL_LOOP_END,
) -> int:
    """Map the *index*-th of *total* install units into ``[start, end]``.

    Deterministic and monotone in *index*: ``0..total-1`` spread evenly
    across the band, clamped to the closed interval.  A zero or negative
    *total* (or an inverted band) yields *start*.
    """
    if total <= 0 or end <= start:
        return start
    span = end - start
    return start + round(span * (index + 1) / (total + 1))
