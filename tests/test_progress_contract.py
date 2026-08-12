"""Unit tests for the M1-2D.6 structured install progress contract.

Pure-Python, Qt-free.  Verifies the phase/percent mapping is monotone and
bounded to 0..100, that 100 belongs only to COMPLETED, and that the
per-package interpolation helper is deterministic and monotone.

No fixtures, no network, no Qt, no subprocess.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from zealfie.app.progress import (
    INSTALL_LOOP_END,
    INSTALL_LOOP_START,
    PHASE_PERCENT,
    InstallPhase,
    InstallProgress,
    interpolate_percent,
)


def test_install_progress_is_frozen_dataclass() -> None:
    p = InstallProgress(InstallPhase.PREPARING, 0, "preparing")
    assert p.phase is InstallPhase.PREPARING
    assert p.percent == 0
    assert p.message == "preparing"

    with pytest.raises(FrozenInstanceError):
        p.percent = 5  # type: ignore[misc]


def test_phase_percent_bounded_and_monotone() -> None:
    """Every phase maps into 0..100, strictly non-decreasing in enum order,
    with 100 exclusively on COMPLETED."""
    phases = list(InstallPhase)
    assert PHASE_PERCENT[InstallPhase.PREPARING] == 0
    assert PHASE_PERCENT[InstallPhase.COMPLETED] == 100

    prev = -1
    for phase in phases:
        pct = PHASE_PERCENT[phase]
        assert 0 <= pct <= 100
        assert pct >= prev, f"{phase} percent {pct} < previous {prev}"
        if phase is not InstallPhase.COMPLETED:
            assert pct < 100, f"{phase} must not emit 100"
        prev = pct


def test_install_loop_band_sits_between_phase_bounds() -> None:
    """The interpolation band lives inside (INSTALLING_RUNTIME, VALIDATING)."""
    assert INSTALL_LOOP_START > PHASE_PERCENT[InstallPhase.INSTALLING_RUNTIME]
    assert INSTALL_LOOP_END < PHASE_PERCENT[InstallPhase.VALIDATING]


def test_interpolate_percent_zero_total_returns_start() -> None:
    assert interpolate_percent(0, 0) == INSTALL_LOOP_START
    assert interpolate_percent(3, -1) == INSTALL_LOOP_START


def test_interpolate_percent_monotone_within_band() -> None:
    total = 7
    prev = INSTALL_LOOP_START - 1
    for i in range(total):
        pct = interpolate_percent(i, total)
        assert INSTALL_LOOP_START <= pct <= INSTALL_LOOP_END
        assert pct >= prev
        prev = pct


def test_interpolate_percent_inverted_band_returns_start() -> None:
    assert interpolate_percent(0, 5, start=90, end=70) == 90
