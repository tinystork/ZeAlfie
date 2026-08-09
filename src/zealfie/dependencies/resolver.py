"""Pure dependency resolution from a local wheelhouse (M1-1A).

This module is PURE PLANNING.  It does not install, mutate, pip,
or activate anything.  Output is a :class:`RuntimeLock` for
consumption by M1-1B materialization.

Primary wheel paths (``primary_wheels``) are **provisional /
test-facing** in M1-1A.  Production M1-1B must supply primary wheels
from already-verified component artifacts (M0-7/M0-8
``VerifiedArtifact`` chain), never raw unverified ``Path`` objects.
This is not yet enforced at the type level (``Path`` is the
simplest integration surface for tests), but the contract is
documented here so that the B product path cannot reasonably bypass
``VerifiedArtifact`` by accident.

Algorithm (fail-closed, M1-1D hardened for order independence):

1. Scan the wheelhouse and build a name→wheel index.
2. **Phase 1 — Lock primaries.**  Read METADATA for each primary
   component wheel, verify filename↔METADATA identity, validate extras,
   lock the entry, and **do NOT yet enqueue its Requires-Dist**.  Collect
   the set of primary names.
3. **Phase 2 — Enqueue primary requirements.**  Only after every
   explicit primary version is known, traverse the Requires-Dist of each
   primary.  Primary→primary constraints are now checked against the
   already-known primary version — the resolution result is independent
   of primary wheel ordering.
4. Validate requested extras against ``Provides-Extra``.
5. Resolve transitive closure from the wheelhouse:
   * match by normalised distribution name
   * filter by version specifier(s)
   * filter by environment markers
   * filter by wheel tags (host compatibility)
   * reject zero matches, multiple matches, or incompatible tags.
6. Handle transitive extras: ``foo[bar]`` → resolve ``foo``, then
   activate its ``bar`` extra requirements.
7. Aggregate version constraints for the same distribution;
   reject incompatibilities.
8. Iterate until the dependency closure stabilises.
9. Return a ``RuntimeLock`` with explicit ``primary_names``.
"""

from __future__ import annotations

import hashlib
from collections import deque
from pathlib import Path

from packaging.metadata import parse_email
from packaging.requirements import Requirement
from packaging.tags import Tag
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import Version

from zealfie.building import WheelInspectionError, read_wheel_metadata_raw

from .host_tags import default_compatible_tags, default_marker_env
from .models import (
    AmbiguousDependency,
    ConstraintConflict,
    DependencyResolutionError,
    ExtraNotFound,
    IncompatibleWheelTag,
    LockedDependency,
    MetadataError,
    MissingDependency,
    RuntimeLock,
    WheelIdentityMismatch,
)


def resolve_runtime_dependencies(
    primary_wheels: list[tuple[Path, frozenset[str]]],
    wheelhouse: Path,
    *,
    compatible_tags: frozenset[Tag] | None = None,
    marker_env: dict[str, str] | None = None,
) -> RuntimeLock:
    """Resolve all transitive dependencies from a local wheelhouse.

    Parameters
    ----------
    primary_wheels:
        ``(wheel_path, active_extras)`` for each primary component.
        Extras should be the normalised extra names (as declared in
        ``Provides-Extra``).

        **M1-1A provisional path:** primary wheel ``Path`` objects
        are accepted for test-facing convenience.  Production M1-1B
        must supply wheels from already-verified component artifacts
        (M0-7/M0-8 ``VerifiedArtifact`` chain), never raw unverified
        paths.  The filename→METADATA identity check is **always**
        enforced (even for primary wheels) before lock creation.
    wheelhouse:
        Directory containing ``.whl`` files for all dependencies.
        Local-only; no network access.
    compatible_tags:
        Host-compatible wheel tags for filtering.  Defaults to
        ``packaging.tags.sys_tags()`` on the current host.
    marker_env:
        Environment dictionary for marker evaluation.  Must include
        at least ``python_version``, ``platform_system``, and
        ``sys_platform``.  Defaults to current host.

    Returns
    -------
    RuntimeLock
        The complete transitive closure, mapping normalised
        distribution names to :class:`LockedDependency`, with
        explicit ``primary_names``.

    Raises
    ------
    MissingDependency
        A required distribution has no matching wheel in the wheelhouse.
    AmbiguousDependency
        Multiple compatible wheels for one requirement.
    IncompatibleWheelTag
        A wheel's tags are not compatible with the host.
    ExtraNotFound
        A requested extra is not declared in ``Provides-Extra``.
    ConstraintConflict
        Incompatible version constraints for the same distribution.
    MetadataError
        A wheel's METADATA cannot be read or parsed (structural).
    WheelIdentityMismatch
        A wheel's filename identity does not match its METADATA
        identity (name or version mismatch).
    """
    if compatible_tags is None:
        compatible_tags = default_compatible_tags()
    if marker_env is None:
        marker_env = default_marker_env()

    wheelhouse = wheelhouse.resolve(strict=True)

    # --- 1. Build wheelhouse index ------------------------------------------
    wheelhouse_index = _build_wheelhouse_index(wheelhouse)

    # --- 2. Initialise state ------------------------------------------------
    locked: dict[str, LockedDependency] = {}
    # pending: (dist_name, active_extras, parent_name)
    # parent_name is None for primary-enqueued items.
    pending: deque[tuple[str, frozenset[str], str | None]] = deque()
    # Track the requirement specifiers we've seen for each dist
    seen_specs: dict[str, list[str]] = {}
    # Track active extras per dist (for transitive extra triggering)
    active_extras_per_dist: dict[str, set[str]] = {}
    # Collect primary names explicitly (M1-1D hardened).
    primary_names: set[str] = set()
    # Collect post-primary enqueue work: (parent_name, req_strs, active_extras).
    # Stored during phase 1, drained during phase 2.
    _primary_reqs: list[tuple[str, list[str] | tuple[str, ...], frozenset[str]]] = []

    # --- 3. PHASE 1 — Lock all primaries (M1-1D: order-independent) ---------
    for wheel_path, extras in primary_wheels:
        active_extras = frozenset(canonicalize_name(extra) for extra in extras)

        # Parse filename for identity verification
        try:
            parsed = parse_wheel_filename(wheel_path.name)
            filename_name = canonicalize_name(parsed[0])
            filename_version = str(parsed[1])
        except Exception as exc:
            raise MetadataError(
                wheel_path, f"cannot parse wheel filename: {exc}"
            ) from exc

        raw_meta = _read_wheel_metadata(wheel_path)
        meta = parse_email(raw_meta)[0]
        meta_name_raw: str = meta.get("name", "")
        meta_version_raw: str = meta.get("version", "")

        # --- Identity verification (before any lock mutation) --------------
        _verify_wheel_identity(
            wheel_path,
            filename_name,
            meta_name_raw,
            filename_version,
            meta_version_raw,
        )

        raw_name = meta_name_raw
        version = meta_version_raw
        name = canonicalize_name(raw_name)
        if not name or not version:
            raise MetadataError(wheel_path, "missing Name or Version in METADATA")

        provides = _canonical_set(meta.get("provides_extra", ()))
        _validate_extras(name, active_extras, provides)
        active_extras_per_dist.setdefault(name, set()).update(active_extras)

        _lock_primary(locked, name, version, wheel_path, active_extras)
        primary_names.add(name)

        # --- Collect requirements for phase 2 (M1-1D hardening) ------------
        req_strs = meta.get("requires_dist", ())
        _primary_reqs.append((name, req_strs, active_extras))

    # --- 4. PHASE 2 — Enqueue primary requirements (all primaries known) ----
    for parent_name, req_strs, active_extras in _primary_reqs:
        _enqueue_requirements(
            parent_name, req_strs, active_extras, marker_env, seen_specs,
            locked, pending,
        )

    # Clear intermediate storage now that phase 2 is done.
    _primary_reqs.clear()

    # --- 5. Resolve transitive closure --------------------------------------
    while pending:
        dep_name, dep_extras, parent_name = pending.popleft()

        # Check if this dependency is already locked
        if dep_name in locked:
            # Record required_by relationship (immutably)
            if parent_name is not None:
                _update_required_by(locked, dep_name, parent_name)

            # Check if new extras activate anything previously missed
            existing_extras = active_extras_per_dist.get(dep_name, set())
            new_extras = set(dep_extras) - existing_extras
            if new_extras:
                # Validate new extras against Provides-Extra BEFORE enqueue
                wheel_path = locked[dep_name].wheel_path
                meta = parse_email(_read_wheel_metadata(wheel_path))[0]
                provides = _canonical_set(meta.get("provides_extra", ()))
                _validate_extras(dep_name, frozenset(new_extras), provides)

                existing_extras.update(new_extras)

                # Update locked entry with cumulative extras (immutably)
                _update_extras(locked, dep_name, new_extras)

                # Re-read METADATA to get newly activated requirements
                _enqueue_requirements(
                    dep_name,
                    meta.get("requires_dist", ()),
                    new_extras,
                    marker_env,
                    seen_specs,
                    locked,
                    pending,
                )
            continue

        # Resolve from wheelhouse
        candidates = wheelhouse_index.get(dep_name, [])

        # Collect aggregated specifiers
        specs = seen_specs.get(dep_name, [])

        if not candidates:
            # No wheel for this distribution name at all
            spec_str = ", ".join(specs) if specs else "any"
            raise MissingDependency(dep_name, spec_str)

        # Filter by version
        compatible = _filter_by_specifiers(candidates, specs)

        if not compatible:
            spec_filtered = _filter_by_specifiers(candidates, specs)
            if spec_filtered:
                raise IncompatibleWheelTag(dep_name, spec_filtered[0][0])
            else:
                spec_str = ", ".join(specs) if specs else "any"
                raise MissingDependency(dep_name, spec_str)

        # Filter by host tags
        compatible = _filter_by_tags(compatible, compatible_tags)
        if not compatible:
            spec_filtered = _filter_by_specifiers(candidates, specs)
            raise IncompatibleWheelTag(dep_name, spec_filtered[0][0])

        if len(compatible) > 1:
            raise AmbiguousDependency(dep_name, [c[0] for c in compatible])

        selected_path, selected_version, _selected_tags = compatible[0]

        # --- Identity verification BEFORE lock creation ---------------------
        meta_version = _verify_wheelhouse_candidate_identity(
            selected_path, dep_name, selected_version,
        )

        # Lock it (METADATA version, verified to match filename version)
        _lock_dependency(locked, dep_name, meta_version, selected_path, dep_extras)
        active_extras_per_dist.setdefault(dep_name, set()).update(dep_extras)

        # Record required_by from parent (immutably)
        if parent_name is not None:
            _update_required_by(locked, dep_name, parent_name)

        # Read its METADATA for transitive requirements
        raw_meta = _read_wheel_metadata(selected_path)
        meta = parse_email(raw_meta)[0]
        provides = _canonical_set(meta.get("provides_extra", ()))
        _validate_extras(dep_name, dep_extras, provides)

        # Queue its requirements with applicable extras
        req_strs = meta.get("requires_dist", ())
        _enqueue_requirements(
            dep_name, req_strs, dep_extras, marker_env, seen_specs, locked, pending,
        )

    return RuntimeLock(locked=locked, primary_names=frozenset(primary_names))


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_wheelhouse_index(
    wheelhouse: Path,
) -> dict[str, list[tuple[Path, str, frozenset[Tag]]]]:
    """Scan a wheelhouse directory.

    Returns ``{canonicalised_name: [(path, version, tag_set), ...]}``.
    """
    index: dict[str, list[tuple[Path, str, frozenset[Tag]]]] = {}
    for whl in sorted(wheelhouse.glob("*.whl")):
        try:
            name, version, _build, tags = parse_wheel_filename(whl.name)
            norm = canonicalize_name(name)
            version = str(version)
        except Exception:
            # Malformed filename → skip silently in a trusted wheelhouse.
            continue
        index.setdefault(norm, []).append((whl, version, frozenset(tags)))
    return index


def _read_wheel_metadata(wheel_path: Path) -> str:
    """Read METADATA via ``zealfie.building.read_wheel_metadata_raw``.

    Reuses the stricter canonical wheel inspection primitive from
    ``zealfie.building`` (single ``.dist-info``, duplicate METADATA
    detection via ``infolist()``, valid UTF-8) instead of maintaining
    a second weaker ZIP/METADATA path in the resolver.

    Wraps ``WheelInspectionError`` into ``MetadataError`` for
    consistent resolver error types.
    """
    try:
        return read_wheel_metadata_raw(wheel_path)
    except WheelInspectionError as exc:
        raise MetadataError(wheel_path, str(exc)) from exc


def _verify_wheel_identity(
    wheel_path: Path,
    filename_name: str,
    metadata_name_raw: str,
    filename_version: str,
    metadata_version_raw: str,
) -> None:
    """Verify filename identity matches METADATA identity.

    Comparison uses canonicalised names (PEP 503) and
    ``packaging.version.Version`` (PEP 440) for robust version
    normalisation.

    Raises :class:`WheelIdentityMismatch` or :class:`MetadataError`
    on failure — **never** proceeds silently.
    """
    if not metadata_name_raw or not metadata_name_raw.strip():
        raise MetadataError(wheel_path, "METADATA has no Name field")
    if not metadata_version_raw or not metadata_version_raw.strip():
        raise MetadataError(wheel_path, "METADATA has no Version field")

    meta_name = canonicalize_name(metadata_name_raw)

    # --- Name comparison ---------------------------------------------------
    if meta_name != canonicalize_name(filename_name):
        raise WheelIdentityMismatch(
            wheel_path=wheel_path,
            filename_name=filename_name,
            metadata_name=metadata_name_raw,
            filename_version=filename_version,
            metadata_version=metadata_version_raw,
            kind="name",
        )

    # --- Version comparison (packaging.version.Version) -------------------
    try:
        fv = Version(filename_version)
        mv = Version(metadata_version_raw)
    except Exception as exc:
        raise WheelIdentityMismatch(
            wheel_path=wheel_path,
            filename_name=filename_name,
            metadata_name=metadata_name_raw,
            filename_version=filename_version,
            metadata_version=metadata_version_raw,
            kind="version_parse",
        ) from exc

    if fv != mv:
        raise WheelIdentityMismatch(
            wheel_path=wheel_path,
            filename_name=filename_name,
            metadata_name=metadata_name_raw,
            filename_version=filename_version,
            metadata_version=metadata_version_raw,
            kind="version",
        )


def _verify_wheelhouse_candidate_identity(
    selected_path: Path,
    canonical_name: str,  # canonical name from wheelhouse index key
    filename_version: str,  # version str from wheelhouse index
) -> str:
    """Read METADATA and verify identity for a wheelhouse candidate.

    Reads METADATA via the stricter ``read_wheel_metadata_raw`` path
    (single ``.dist-info``, duplicate METADATA detection, UTF-8),
    then compares canonical name and ``Version`` so that a wheel
    whose METADATA declares a different distribution or version
    than its filename cannot be selected or locked.

    *canonical_name* is the canonical distribution name from parsing
    the wheel filename (the key in the wheelhouse index).
    *filename_version* is the version string from parsing the wheel
    filename.

    Returns the METADATA Version string (authoritative, verified to
    match *filename_version* via ``packaging.version.Version``).
    """
    raw_meta = _read_wheel_metadata(selected_path)
    meta = parse_email(raw_meta)[0]
    meta_name_raw: str = meta.get("name", "")
    meta_version_raw: str = meta.get("version", "")

    _verify_wheel_identity(
        selected_path,
        canonical_name,
        meta_name_raw,
        filename_version,
        meta_version_raw,
    )

    return meta_version_raw


def _canonical_set(values: list[str] | tuple[str, ...] | None) -> frozenset[str]:
    """Canonicalise a collection of extra/dependency names to a frozenset.

    Uses ``packaging.utils.canonicalize_name`` for PEP 685 compliance:
    lowercase + underscores → dashes.
    """
    if not values:
        return frozenset()
    return frozenset(canonicalize_name(v.strip()) for v in values if v.strip())


def _validate_extras(
    name: str,
    requested: frozenset[str],
    available: frozenset[str],
) -> None:
    """Raise ``ExtraNotFound`` if any requested extra is missing."""
    for extra in requested:
        if extra not in available:
            raise ExtraNotFound(name, extra, available)


def _lock_primary(
    locked: dict[str, LockedDependency],
    name: str,
    version: str,
    wheel_path: Path,
    extras: frozenset[str],
) -> None:
    """Lock a primary component wheel (path is already known)."""
    size = wheel_path.stat().st_size
    sha256 = _sha256_of(wheel_path)
    locked[name] = LockedDependency(
        name=name, version=version, wheel_path=wheel_path,
        size=size, sha256=sha256, extras=extras,
    )


def _lock_dependency(
    locked: dict[str, LockedDependency],
    name: str,
    version: str,
    wheel_path: Path,
    extras: frozenset[str],
) -> None:
    """Lock a dependency resolved from the wheelhouse."""
    size = wheel_path.stat().st_size
    sha256 = _sha256_of(wheel_path)
    locked[name] = LockedDependency(
        name=name, version=version, wheel_path=wheel_path,
        size=size, sha256=sha256, extras=extras,
    )


def _update_required_by(
    locked: dict[str, LockedDependency],
    dep_name: str,
    parent_name: str,
) -> None:
    """Immutably add *parent_name* to the ``required_by`` set of *dep_name*."""
    existing = locked[dep_name]
    if parent_name not in existing.required_by:
        locked[dep_name] = LockedDependency(
            name=existing.name,
            version=existing.version,
            wheel_path=existing.wheel_path,
            size=existing.size,
            sha256=existing.sha256,
            extras=existing.extras,
            required_by=frozenset(existing.required_by | {parent_name}),
        )


def _update_extras(
    locked: dict[str, LockedDependency],
    dep_name: str,
    new_extras: frozenset[str] | set[str],
) -> None:
    """Immutably add *new_extras* to the ``extras`` set of *dep_name*."""
    existing = locked[dep_name]
    updated_extras = frozenset(existing.extras | set(new_extras))
    locked[dep_name] = LockedDependency(
        name=existing.name,
        version=existing.version,
        wheel_path=existing.wheel_path,
        size=existing.size,
        sha256=existing.sha256,
        extras=updated_extras,
        required_by=existing.required_by,
    )


def _enqueue_requirements(
    parent_name: str,
    req_strs: list[str] | tuple[str, ...],
    active_extras: frozenset[str] | set[str],
    marker_env: dict[str, str],
    seen_specs: dict[str, list[str]],
    locked: dict[str, LockedDependency],
    pending: deque[tuple[str, frozenset[str], str | None]],
) -> None:
    """Parse ``Requires-Dist`` lines and enqueue applicable dependencies.

    Each enqueued item carries the *parent_name* so that
    ``required_by`` relationships can be built accurately during
    actual resolution (not post-hoc).
    """
    for req_str in req_strs:
        try:
            req = Requirement(req_str)
        except Exception:
            # Malformed requirement in trusted METADATA → raise immediately.
            raise DependencyResolutionError(
                f"invalid Requires-Dist in {parent_name}: {req_str!r}"
            ) from None

        # --- Marker evaluation ------------------------------------------
        if req.marker is not None:
            applies = False
            for extra in active_extras:
                env = {**marker_env, "extra": extra}
                try:
                    if req.marker.evaluate(env):
                        applies = True
                        break
                except Exception:
                    raise DependencyResolutionError(
                        f"marker evaluation failed for {req_str!r} "
                        f"in {parent_name}"
                    ) from None
            if not applies:
                # Also check with no extra (may match e.g. platform marker)
                env = {**marker_env, "extra": ""}
                try:
                    if not req.marker.evaluate(env):
                        continue
                except Exception:
                    raise DependencyResolutionError(
                        f"marker evaluation failed for {req_str!r} "
                        f"in {parent_name}"
                    ) from None

        dep_name = canonicalize_name(req.name)

        # --- Check constraint compatibility if already locked ------------
        if dep_name in locked and req.specifier:
            existing_version = locked[dep_name].version
            if not req.specifier.contains(existing_version):
                raise ConstraintConflict(
                    dep_name, existing_version, str(req.specifier)
                )

        # --- Record specifier for aggregation ----------------------------
        if req.specifier:
            seen_specs.setdefault(dep_name, []).append(str(req.specifier))

        # --- Queue for resolution (with transitive extras, parent tracking)
        dep_extras = frozenset(canonicalize_name(e) for e in (req.extras or ()))
        if dep_name not in locked:
            pending.append((dep_name, dep_extras, parent_name))
        else:
            # Record required_by for already-locked dependency
            _update_required_by(locked, dep_name, parent_name)
            # Already locked but transitive extras may activate new reqs
            if dep_extras:
                pending.append((dep_name, dep_extras, parent_name))


def _filter_by_specifiers(
    candidates: list[tuple[Path, str, frozenset[Tag]]],
    specs: list[str],
) -> list[tuple[Path, str, frozenset[Tag]]]:
    """Filter candidates by version specifier strings.

    If *specs* is empty, all candidates pass (no version constraint).
    """
    if not specs:
        return list(candidates)

    result: list[tuple[Path, str, frozenset[Tag]]] = []
    for path, version, tags in candidates:
        ok = True
        for spec_str in specs:
            try:
                from packaging.specifiers import SpecifierSet
                if not SpecifierSet(spec_str).contains(version):
                    ok = False
                    break
            except Exception:
                # Malformed specifier -> treat as non-matching
                ok = False
                break
        if ok:
            result.append((path, version, tags))
    return result


def _filter_by_tags(
    candidates: list[tuple[Path, str, frozenset[Tag]]],
    compatible_tags: frozenset[Tag],
) -> list[tuple[Path, str, frozenset[Tag]]]:
    """Filter candidates whose wheel tags are compatible with the host."""
    result: list[tuple[Path, str, frozenset[Tag]]] = []
    for path, version, tags in candidates:
        if tags & compatible_tags:
            result.append((path, version, tags))
    return result


def _sha256_of(path: Path) -> str:
    """Compute the SHA-256 hex digest of a file."""
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()
