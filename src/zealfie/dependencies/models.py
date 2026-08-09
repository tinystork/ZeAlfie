"""Data models for the shared runtime dependency resolver (M1-1A).

These types are PURE data — they carry no mutation, no pip calls,
no installation logic.  M1-1B materialization will consume them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


# ---------------------------------------------------------------------------
# Locked dependency
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LockedDependency:
    """A single resolved dependency with a concrete wheel artifact.

    *name* is the **normalised** distribution name (PEP 503).
    *extras* is the set of extras that were activated for this
    distribution (may be empty).
    *required_by* names the distributions in the lock that directly
    depend on this one.
    """

    name: str
    version: str
    wheel_path: Path
    size: int
    sha256: str
    extras: frozenset[str] = field(default_factory=frozenset)
    required_by: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        name = self.name.strip()
        if not name:
            raise ValueError("LockedDependency.name must not be empty")
        object.__setattr__(self, "name", name)
        version = str(self.version).strip()
        if not version:
            raise ValueError("LockedDependency.version must not be empty")
        object.__setattr__(self, "version", version)


# ---------------------------------------------------------------------------
# Runtime lock
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeLock:
    """Complete resolved transitive dependency closure.

    *locked* maps normalised distribution name → :class:`LockedDependency`.
    The order of insertion is the resolution order (topological, roughly
    breadth-first).  Callers may iterate ``locked.values()`` for a
    deterministic processing order.

    This lock is a planning artifact only.  M1-1B will consume it for
    materialization (slot creation, wheel installation, activation).
    """

    locked: dict[str, LockedDependency]

    @property
    def primary_names(self) -> frozenset[str]:
        """Distributions with no ``required_by`` entries — the roots."""
        return frozenset(
            name for name, dep in self.locked.items() if not dep.required_by
        )

    @property
    def dependency_names(self) -> frozenset[str]:
        """Distributions that are NOT primary (have at least one required_by)."""
        return frozenset(
            name for name, dep in self.locked.items() if dep.required_by
        )

    def __len__(self) -> int:
        return len(self.locked)

    def __contains__(self, name: str) -> bool:
        return name in self.locked

    def __getitem__(self, name: str) -> LockedDependency:
        return self.locked[name]

    def get(self, name: str) -> LockedDependency | None:
        return self.locked.get(name)


# ---------------------------------------------------------------------------
# Resolution errors — structured, fail-closed.
# ---------------------------------------------------------------------------


class DependencyResolutionError(RuntimeError):
    """Base class for all dependency resolution failures.

    Every sub-error carries enough structured data for callers to
    produce a meaningful diagnostic.  No resolution error should be
    silently swallowed; M1-1A always blocks before mutation.
    """


class MissingDependency(DependencyResolutionError):
    """A required distribution has no matching wheel in the wheelhouse."""

    def __init__(self, name: str, specifier: str | None = None) -> None:
        self.name = name
        self.specifier = specifier
        msg = f"missing dependency: {name}"
        if specifier:
            msg += f" ({specifier})"
        msg += " — not found in local wheelhouse"
        super().__init__(msg)


class AmbiguousDependency(DependencyResolutionError):
    """Multiple compatible wheels exist for a single requirement."""

    def __init__(self, name: str, candidates: list[Path]) -> None:
        self.name = name
        self.candidates = candidates
        candidate_str = ", ".join(str(c.name) for c in candidates)
        super().__init__(
            f"ambiguous dependency: {name} has {len(candidates)} compatible "
            f"wheels in wheelhouse: {candidate_str}"
        )


class IncompatibleWheelTag(DependencyResolutionError):
    """No wheel for a distribution has tags compatible with the host."""

    def __init__(self, name: str, wheel_path: Path) -> None:
        self.name = name
        self.wheel_path = wheel_path
        super().__init__(
            f"incompatible wheel tag: {name} ({wheel_path.name}) "
            f"is not compatible with host tags"
        )


class ExtraNotFound(DependencyResolutionError):
    """A requested extra is not declared in the distribution's ``Provides-Extra``."""

    def __init__(
        self, name: str, requested: str, available: frozenset[str]
    ) -> None:
        self.name = name
        self.requested = requested
        self.available = available
        available_str = ", ".join(sorted(available)) if available else "(none)"
        super().__init__(
            f"extra {requested!r} not found in {name}; "
            f"available extras: {available_str}"
        )


class ConstraintConflict(DependencyResolutionError):
    """Two or more requirements constrain the same distribution incompatibly."""

    def __init__(
        self, name: str, existing_version: str, new_specifier: str
    ) -> None:
        self.name = name
        self.existing_version = existing_version
        self.new_specifier = new_specifier
        super().__init__(
            f"constraint conflict: {name} is locked at {existing_version} "
            f"but a requirement demands {new_specifier}"
        )


class WheelIdentityMismatch(DependencyResolutionError):
    """Wheel filename identity does not match METADATA identity.

    Raised **before lock creation / selection** when a wheel's filename
    claims a different distribution name or version than its
    ``.dist-info/METADATA`` declares.  This is a hard block: the
    resolver refuses to proceed with a mismatched wheel.

    *kind* identifies the mismatch:

    - ``"name"`` — canonicalised distribution names differ.
    - ``"version"`` — ``packaging.version.Version`` comparison differs.
    - ``"version_parse"`` — either side could not be parsed as a version.
    """

    def __init__(
        self,
        wheel_path: Path,
        filename_name: str,
        metadata_name: str,
        filename_version: str,
        metadata_version: str,
        kind: str,
    ) -> None:
        self.wheel_path = wheel_path
        self.filename_name = filename_name
        self.metadata_name = metadata_name
        self.filename_version = filename_version
        self.metadata_version = metadata_version
        self.kind = kind
        super().__init__(self._build_message())

    def _build_message(self) -> str:
        if self.kind == "name":
            return (
                f"wheel identity mismatch (name) in {self.wheel_path.name}: "
                f"filename declares {self.filename_name!r} but METADATA "
                f"declares {self.metadata_name!r}"
            )
        elif self.kind == "version":
            return (
                f"wheel identity mismatch (version) in {self.wheel_path.name}: "
                f"filename declares {self.filename_version!r} but METADATA "
                f"declares {self.metadata_version!r}"
            )
        else:
            return (
                f"wheel identity mismatch ({self.kind}) in {self.wheel_path.name}: "
                f"filename=({self.filename_name!r}, {self.filename_version!r}), "
                f"METADATA=({self.metadata_name!r}, {self.metadata_version!r})"
            )


class MetadataError(DependencyResolutionError):
    """A wheel's METADATA cannot be read or parsed."""

    def __init__(self, wheel_path: Path, detail: str) -> None:
        self.wheel_path = wheel_path
        self.detail = detail
        super().__init__(
            f"metadata error for {wheel_path.name}: {detail}"
        )
