"""Launch plan – an immutable representation of a prepared launch."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LaunchPlan:
    """A launch that has been prepared but **not yet executed**.

    The executable and arguments are structured (no shell string).
    ``shell=False`` is the only accepted execution mode.
    """

    component_id: str
    executable: Path
    arguments: tuple[str, ...] = ()
    working_directory: Path | None = None

    def __post_init__(self) -> None:
        # Ensure arguments are always a tuple.
        object.__setattr__(self, "arguments", tuple(self.arguments))
        if not str(self.component_id).strip():
            raise ValueError("component_id is required")
