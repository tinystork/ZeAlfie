"""ZeAlfie self-update plan (ZA-M1-4 LOT D §D).

Builds a read-only plan describing whether a self-update is available,
supported, up-to-date, or failed.  Building a plan never stages, never
writes a pending marker, and never installs.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .identity import ZeAlfieIdentity, self_update_supported
from .resolver import UpdateResolution, resolve_available_update

__all__ = [
    "SelfUpdatePlan",
    "SelfUpdateStatus",
    "build_self_update_plan",
]

_VALID_CHANNELS = ("stable", "beta")


class SelfUpdateStatus(StrEnum):
    UP_TO_DATE = "UP_TO_DATE"
    UPDATE_AVAILABLE = "UPDATE_AVAILABLE"
    NOT_SUPPORTED = "NOT_SUPPORTED"
    CHECK_FAILED = "CHECK_FAILED"


@dataclass(frozen=True, slots=True)
class SelfUpdatePlan:
    """Read-only result of a self-update availability check.

    ``error`` is the caught exception on ``CHECK_FAILED`` (may carry a
    :attr:`~zealfie.net.NetworkReasonCode` + ``proxy_hint`` for CLI
    reason-code printing); it is ``None`` otherwise.
    """

    status: SelfUpdateStatus
    identity: ZeAlfieIdentity
    resolution: UpdateResolution | None
    reason: str | None
    error: BaseException | None = None


def build_self_update_plan(
    identity: ZeAlfieIdentity,
    channel: str = "stable",
    *,
    resolver,
    tags_lister,
) -> SelfUpdatePlan:
    """Build a read-only self-update plan for *identity*.

    * ``NOT_SUPPORTED`` for non-installed modes (honest reason).
    * ``CHECK_FAILED`` on resolver/tag-listing errors (honest reason).
    * ``UP_TO_DATE`` / ``UPDATE_AVAILABLE`` otherwise.

    Never stages, never mutates.
    """
    if channel not in _VALID_CHANNELS:
        raise ValueError(
            f"invalid channel {channel!r}; expected one of {_VALID_CHANNELS}"
        )

    supported, reason = self_update_supported(identity)
    if not supported:
        return SelfUpdatePlan(
            status=SelfUpdateStatus.NOT_SUPPORTED,
            identity=identity,
            resolution=None,
            reason=reason,
        )

    try:
        resolution = resolve_available_update(
            identity,
            channel=channel,
            resolver=resolver,
            tags_lister=tags_lister,
        )
    except Exception as exc:  # noqa: BLE001 - honest CHECK_FAILED with reason
        return SelfUpdatePlan(
            status=SelfUpdateStatus.CHECK_FAILED,
            identity=identity,
            resolution=None,
            reason=str(exc),
            error=exc,
        )

    status = (
        SelfUpdateStatus.UP_TO_DATE
        if resolution.up_to_date
        else SelfUpdateStatus.UPDATE_AVAILABLE
    )
    return SelfUpdatePlan(
        status=status,
        identity=identity,
        resolution=resolution,
        reason=None,
    )
