"""Tests for M1-1A pure dependency resolution.

These tests use synthetic wheel fixtures built once per session.
No network, no mutation, no runtime slots — purely the resolver.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from packaging.tags import Tag, sys_tags

from zealfie.building import build_wheel, read_wheel_metadata_raw
from zealfie.dependencies import (
    AmbiguousDependency,
    ConstraintConflict,
    ExtraNotFound,
    IncompatibleWheelTag,
    LockedDependency,
    MetadataError,
    MissingDependency,
    WheelIdentityMismatch,
    RuntimeLock,
    resolve_runtime_dependencies,
)
from zealfie.dependencies.host_tags import SysTagProvider


# ---------------------------------------------------------------------------
# Session-scoped wheel fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def leaf_wheel(session_wheelhouse: Path) -> Path:
    return session_wheelhouse / "leaf_lib-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def mid_wheel(session_wheelhouse: Path) -> Path:
    return session_wheelhouse / "mid_lib-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def mid_extra_wheel(session_wheelhouse: Path) -> Path:
    return session_wheelhouse / "mid_lib_extra-1.0.0-py3-none-any.whl"


@pytest.fixture(scope="session")
def session_wheelhouse(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build all synthetic fixture wheels into a shared wheelhouse."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    wheelhouse = tmp_path_factory.mktemp("wheelhouse")

    for name in ("leaf_lib", "mid_lib", "mid_lib_extra"):
        src = fixtures / name
        # Build into a temp dir, then copy wheel to shared wheelhouse
        tmp = tmp_path_factory.mktemp(f"build-{name}")
        wheel = build_wheel(src, output_dir=tmp)
        import shutil
        shutil.copy(wheel, wheelhouse / wheel.name)

    return wheelhouse


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------


def test_imports() -> None:
    """Verify all expected public names are importable."""
    assert resolve_runtime_dependencies is not None
    assert RuntimeLock is not None
    assert LockedDependency is not None
    assert IncompatibleWheelTag is not None


# ---------------------------------------------------------------------------
# No-dependency component resolves to just itself
# ---------------------------------------------------------------------------


def test_leaf_no_deps_produces_single_lock(
    leaf_wheel: Path, session_wheelhouse: Path
) -> None:
    lock = resolve_runtime_dependencies(
        primary_wheels=[(leaf_wheel, frozenset())],
        wheelhouse=session_wheelhouse,
    )

    assert len(lock) == 1
    assert "leaf-lib" in lock
    dep = lock["leaf-lib"]
    assert dep.name == "leaf-lib"
    assert dep.version == "1.0.0"
    assert dep.wheel_path == leaf_wheel
    assert dep.size > 0
    assert len(dep.sha256) == 64
    assert dep.extras == frozenset()
    assert dep.required_by == frozenset()  # primary, no dependants


# ---------------------------------------------------------------------------
# Single transitive dependency
# ---------------------------------------------------------------------------


def test_mid_lib_depends_on_leaf(
    mid_wheel: Path, session_wheelhouse: Path, leaf_wheel: Path
) -> None:
    lock = resolve_runtime_dependencies(
        primary_wheels=[(mid_wheel, frozenset())],
        wheelhouse=session_wheelhouse,
    )

    assert len(lock) == 2
    assert "mid-lib" in lock
    assert "leaf-lib" in lock

    mid_dep = lock["mid-lib"]
    assert mid_dep.name == "mid-lib"
    assert mid_dep.version == "1.0.0"
    assert mid_dep.required_by == frozenset()  # primary

    leaf_dep = lock["leaf-lib"]
    assert leaf_dep.name == "leaf-lib"
    assert leaf_dep.version == "1.0.0"
    assert leaf_dep.required_by == frozenset({"mid-lib"})

    # primary / dependency properties
    assert lock.primary_names == frozenset({"mid-lib"})
    assert lock.dependency_names == frozenset({"leaf-lib"})


# ---------------------------------------------------------------------------
# Missing dependency
# ---------------------------------------------------------------------------


def test_missing_dependency_raises(
    leaf_wheel: Path, tmp_path: Path
) -> None:
    """Resolver must block when a dependency wheel is not in the wheelhouse."""
    empty = tmp_path / "empty_wh"
    empty.mkdir()

    with pytest.raises(MissingDependency, match="leaf-lib"):
        resolve_runtime_dependencies(
            primary_wheels=[(mid_wheel := _build_mid(tmp_path), frozenset())],
            wheelhouse=empty,
        )


# ---------------------------------------------------------------------------
# Ambiguous multiple compatible wheels
# ---------------------------------------------------------------------------


def test_ambiguous_multiple_wheels_raises(
    leaf_wheel: Path, session_wheelhouse: Path, tmp_path: Path
) -> None:
    """Two wheels for the same dist name → ambiguous."""
    # Create a second copy of the leaf wheel with a different version
    wh = tmp_path / "ambiguous_wh"
    wh.mkdir()
    import shutil
    shutil.copy(leaf_wheel, wh / "leaf_lib-1.0.0-py3-none-any.whl")
    shutil.copy(leaf_wheel, wh / "leaf_lib-1.0.1-py3-none-any.whl")

    # mid-lib depends on leaf-lib>=1.0 — both 1.0.0 and 1.0.1 match
    mid_path = _build_mid(tmp_path)

    with pytest.raises(AmbiguousDependency, match="leaf-lib"):
        resolve_runtime_dependencies(
            primary_wheels=[(mid_path, frozenset())],
            wheelhouse=wh,
        )


# ---------------------------------------------------------------------------
# Incompatible wheel tag (H4: distinguish from MissingDependency)
# ---------------------------------------------------------------------------


def test_incompatible_tag_raises_incompatible_wheel_tag(
    mid_wheel: Path, session_wheelhouse: Path
) -> None:
    """A wheelhouse containing only incompatible-tagged wheels → IncompatibleWheelTag."""
    incompatible = frozenset({Tag("cp99", "cp99", "linux_x86_64")})

    with pytest.raises(IncompatibleWheelTag, match="leaf-lib"):
        resolve_runtime_dependencies(
            primary_wheels=[(mid_wheel, frozenset())],
            wheelhouse=session_wheelhouse,
            compatible_tags=incompatible,
        )


def test_missing_dependency_no_wheel_at_all(tmp_path: Path) -> None:
    """No wheel files for a dependency → MissingDependency, not IncompatibleWheelTag."""
    wh = tmp_path / "wh"
    wh.mkdir()
    primary = _build_metadata_wheel("primary", "1.0.0", ["nonexistent-lib"], wh)

    with pytest.raises(MissingDependency, match="nonexistent-lib"):
        resolve_runtime_dependencies(
            primary_wheels=[(primary, frozenset())],
            wheelhouse=wh,
        )


def test_incompatible_tag_after_version_match(tmp_path: Path) -> None:
    """Wheel exists and matches version spec, but tag is incompatible → IncompatibleWheelTag."""
    wh = tmp_path / "wh"
    wh.mkdir()

    # Create a wheel for B with a very platform-specific tag
    b_path = _build_wheel_with_tag(wh, "b", "1.0.0", "cp39-cp39-linux_x86_64")
    primary = _build_metadata_wheel("primary", "1.0.0", ["b>=1.0"], wh)

    # Our host tags won't match cp39-cp39-linux_x86_64 on this machine
    with pytest.raises(IncompatibleWheelTag, match="b"):
        resolve_runtime_dependencies(
            primary_wheels=[(primary, frozenset())],
            wheelhouse=wh,
        )


# ---------------------------------------------------------------------------
# Extras: base + selected extra
# ---------------------------------------------------------------------------


def test_mid_lib_extra_base_extras(
    mid_extra_wheel: Path, session_wheelhouse: Path
) -> None:
    """Base extras only: leaf-lib resolved but NOT mid-lib (extra not activated)."""
    lock = resolve_runtime_dependencies(
        primary_wheels=[(mid_extra_wheel, frozenset())],
        wheelhouse=session_wheelhouse,
    )

    assert len(lock) == 2  # mid-lib-extra + leaf-lib
    assert "mid-lib-extra" in lock
    assert "leaf-lib" in lock
    assert "mid-lib" not in lock  # only activated via "feature" extra


def test_mid_lib_extra_activated(
    mid_extra_wheel: Path, session_wheelhouse: Path
) -> None:
    """When 'feature' extra is activated, mid-lib is pulled in too."""
    lock = resolve_runtime_dependencies(
        primary_wheels=[(mid_extra_wheel, frozenset({"feature"}))],
        wheelhouse=session_wheelhouse,
    )

    assert len(lock) == 3  # mid-lib-extra + leaf-lib + mid-lib
    assert "mid-lib-extra" in lock
    assert "leaf-lib" in lock
    assert "mid-lib" in lock

    # mid-lib is required by mid-lib-extra (via feature extra)
    mid_dep = lock["mid-lib"]
    assert "mid-lib-extra" in mid_dep.required_by

    # leaf-lib is required by both
    leaf_dep = lock["leaf-lib"]
    assert leaf_dep.required_by == frozenset({"mid-lib-extra", "mid-lib"})


# ---------------------------------------------------------------------------
# Extra not in Provides-Extra
# ---------------------------------------------------------------------------


def test_requested_extra_not_available_raises(
    mid_wheel: Path, session_wheelhouse: Path
) -> None:
    """Requesting an extra not in Provides-Extra must block."""
    with pytest.raises(ExtraNotFound, match="nonexistent"):
        resolve_runtime_dependencies(
            primary_wheels=[(mid_wheel, frozenset({"nonexistent"}))],
            wheelhouse=session_wheelhouse,
        )


# ---------------------------------------------------------------------------
# Constraint conflict
# ---------------------------------------------------------------------------


def test_constraint_conflict_transitive(
    tmp_path: Path
) -> None:
    """Transitive constraint conflict: bar locked at 0.9.0, transitively
    required bar>=2.0 → ConstraintConflict."""
    wh = tmp_path / "wh"
    wh.mkdir()

    # dep-b: depends on bar>=2.0
    _build_metadata_wheel(
        "depb", "1.0.0", ["bar>=2.0"], wh
    )

    # primary-a: depends on bar<1.0 AND dep-b
    primary = _build_metadata_wheel(
        "primaryconflict", "1.0.0", ["bar<1.0", "depb"], wh
    )

    # bar is available at 0.9.0
    _build_metadata_wheel("bar", "0.9.0", [], wh)

    # Resolution: bar<1.0 satisfied by bar-0.9.0, locks bar.
    # Then depb resolves, requiring bar>=2.0 → conflict with locked 0.9.0.
    with pytest.raises(ConstraintConflict, match="bar"):
        resolve_runtime_dependencies(
            primary_wheels=[(primary, frozenset())],
            wheelhouse=wh,
        )


# ---------------------------------------------------------------------------
# Marker-based filtering
# ---------------------------------------------------------------------------


def test_platform_marker_filters_deps(
    mid_wheel: Path, session_wheelhouse: Path
) -> None:
    """Requirements with unsatisfiable platform markers are skipped."""
    # mid-lib has leaf-lib>=1.0 with NO marker, so it resolves.
    # Use a marker env where all real platform deps (if any) would be filtered.
    lock = resolve_runtime_dependencies(
        primary_wheels=[(mid_wheel, frozenset())],
        wheelhouse=session_wheelhouse,
        marker_env={
            "python_version": "3.12",
            "platform_system": "NonexistentOS",
            "sys_platform": "nonexistent_os",
            "extra": "",
        },
    )
    # leaf-lib has no marker, so still resolved
    assert "leaf-lib" in lock


# ---------------------------------------------------------------------------
# LockedDependency model invariants
# ---------------------------------------------------------------------------


def test_locked_dependency_rejects_empty_name() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        LockedDependency("", "1.0.0", Path("/tmp/x.whl"), 0, "0" * 64)


def test_locked_dependency_rejects_empty_version() -> None:
    with pytest.raises(ValueError, match="must not be empty"):
        LockedDependency("foo", "", Path("/tmp/x.whl"), 0, "0" * 64)


def test_runtime_lock_properties() -> None:
    lock = RuntimeLock({
        "a": LockedDependency("a", "1.0", Path("/a.whl"), 1, "a" * 64),
        "b": LockedDependency("b", "1.0", Path("/b.whl"), 1, "b" * 64,
                              required_by=frozenset({"a"})),
    })
    assert lock.primary_names == frozenset({"a"})
    assert lock.dependency_names == frozenset({"b"})
    assert len(lock) == 2
    assert "a" in lock
    assert "z" not in lock
    assert lock.get("a") is not None
    assert lock.get("z") is None


# ---------------------------------------------------------------------------
# Host tag provider
# ---------------------------------------------------------------------------


def test_sys_tag_provider_returns_frozenset() -> None:
    provider = SysTagProvider()
    tags = provider.get_compatible_tags()
    assert isinstance(tags, frozenset)
    assert len(tags) > 0
    for tag in tags:
        assert isinstance(tag, Tag)


# ---------------------------------------------------------------------------
# Resolved data: size + sha256
# ---------------------------------------------------------------------------


def test_locked_deps_have_size_and_sha256(
    leaf_wheel: Path, session_wheelhouse: Path
) -> None:
    lock = resolve_runtime_dependencies(
        primary_wheels=[(leaf_wheel, frozenset())],
        wheelhouse=session_wheelhouse,
    )
    dep = lock["leaf-lib"]
    assert dep.size == leaf_wheel.stat().st_size
    assert len(dep.sha256) == 64
    assert all(c in "0123456789abcdef" for c in dep.sha256)


# ---------------------------------------------------------------------------
# Existing witness/no-dependency behavior unchanged
# ---------------------------------------------------------------------------


def test_witness_wheel_no_deps_resolves(
    witness_wheel: Path, tmp_path: Path
) -> None:
    """The existing witness wheel (no deps) resolves to a single lock entry."""
    wh = tmp_path / "wh"
    wh.mkdir()
    # witness has no Requires-Dist, so empty wheelhouse is fine
    lock = resolve_runtime_dependencies(
        primary_wheels=[(witness_wheel, frozenset())],
        wheelhouse=wh,
    )
    assert len(lock) == 1
    dep_entries = list(lock.locked.values())
    assert dep_entries[0].name == "zealfie-witness"
    assert dep_entries[0].version == "0.0.1"


# ---------------------------------------------------------------------------
# ZeSolver wheel resolves its gui-extra dependencies
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def zesolver_wheel() -> Path:
    return Path("/home/tristan/.openclaw/workspace/projects/ZeSolver/dist/zesolver-1.0.0-py3-none-any.whl")


def test_zesolver_reads_metadata(
    zesolver_wheel: Path,
) -> None:
    """Verify we can read ZeSolver's METADATA correctly."""
    from packaging.metadata import parse_email
    raw = read_wheel_metadata_raw(zesolver_wheel)
    meta = parse_email(raw)[0]
    assert meta["name"] == "ZeSolver"
    assert meta["version"] == "1.0.0"
    reqs = meta.get("requires_dist", ())
    assert any("numpy" in r for r in reqs)
    assert any("PySide6" in r for r in reqs)
    assert any("gui" in r for r in reqs)
    assert "gui" in meta.get("provides_extra", ())


def test_zesolver_gui_extra_missing_deps_blocks(
    zesolver_wheel: Path, tmp_path: Path
) -> None:
    """ZeSolver with gui extra but no dep wheels → blocks on missing deps."""
    wh = tmp_path / "wh"
    wh.mkdir()

    with pytest.raises(MissingDependency):
        resolve_runtime_dependencies(
            primary_wheels=[(zesolver_wheel, frozenset({"gui"}))],
            wheelhouse=wh,
        )


def test_zesolver_base_no_extras_missing_deps_blocks(
    zesolver_wheel: Path, tmp_path: Path
) -> None:
    """ZeSolver without extras still blocks on base deps."""
    wh = tmp_path / "wh"
    wh.mkdir()

    with pytest.raises(MissingDependency):
        resolve_runtime_dependencies(
            primary_wheels=[(zesolver_wheel, frozenset())],
            wheelhouse=wh,
        )


# ---------------------------------------------------------------------------
# Sanity: resolution does not mutate wheelhouse
# ---------------------------------------------------------------------------


def test_resolution_does_not_mutate_wheelhouse(
    mid_wheel: Path, session_wheelhouse: Path
) -> None:
    """Calling the resolver must not create/modify files in the wheelhouse."""
    before = sorted(session_wheelhouse.glob("*.whl"))
    before_mtimes = {w: w.stat().st_mtime for w in before}

    resolve_runtime_dependencies(
        primary_wheels=[(mid_wheel, frozenset())],
        wheelhouse=session_wheelhouse,
    )

    after = sorted(session_wheelhouse.glob("*.whl"))
    assert [w.name for w in before] == [w.name for w in after]
    for w in after:
        assert w.stat().st_mtime == before_mtimes[w]


# ===========================================================================
# H1: Extras canonicalization (PEP 685)
# ===========================================================================


def test_extras_normalized_lowercase_in_resolver(tmp_path: Path) -> None:
    """Extras with uppercase variants are canonicalised and matched."""
    wh = tmp_path / "wh"
    wh.mkdir()
    _build_metadata_wheel_with_extras(
        "depa", "1.0.0", ["leaf-lib>=1.0"], wh,
        provides_extra=["Feature", "Debug"],
    )
    _build_metadata_wheel("leaf-lib", "1.0.0", [], wh)

    lock = resolve_runtime_dependencies(
        primary_wheels=[(wh / "depa-1.0.0-py3-none-any.whl", frozenset({"Feature"}))],
        wheelhouse=wh,
    )
    # Should not raise ExtraNotFound despite "Feature" vs "feature"
    assert "depa" in lock
    assert lock["depa"].extras == frozenset({"feature"})  # canonicalised


def test_extras_normalized_dash_underscore_in_resolver(tmp_path: Path) -> None:
    """Extras with dash/underscore variants are canonicalised and matched."""
    wh = tmp_path / "wh"
    wh.mkdir()
    _build_metadata_wheel_with_extras(
        "depa", "1.0.0", [], wh,
        provides_extra=["my_extra", "other-extra"],
    )
    depa = wh / "depa-1.0.0-py3-none-any.whl"

    # Request "my-extra" (dash form) when declared as "my_extra" (underscore)
    lock = resolve_runtime_dependencies(
        primary_wheels=[(depa, frozenset({"my-extra"}))],
        wheelhouse=wh,
    )
    assert "depa" in lock
    assert lock["depa"].extras == frozenset({"my-extra"})  # canonicalised


def test_extra_not_found_normalized(tmp_path: Path) -> None:
    """ExtraNotFound after normalization for a genuinely missing extra."""
    wh = tmp_path / "wh"
    wh.mkdir()
    _build_metadata_wheel_with_extras("depa", "1.0.0", [], wh, provides_extra=["feature"])
    depa = wh / "depa-1.0.0-py3-none-any.whl"

    with pytest.raises(ExtraNotFound, match="missing-extra"):
        resolve_runtime_dependencies(
            primary_wheels=[(depa, frozenset({"missing-extra"}))],
            wheelhouse=wh,
        )


# ===========================================================================
# H2: RuntimeLock records active extras accurately
# ===========================================================================


def test_primary_extras_recorded(tmp_path: Path) -> None:
    """Primary wheel extras are recorded in LockedDependency.extras."""
    wh = tmp_path / "wh"
    wh.mkdir()
    _build_metadata_wheel_with_extras(
        "primarya", "1.0.0", [], wh, provides_extra=["feature", "dev"]
    )
    primary = wh / "primarya-1.0.0-py3-none-any.whl"

    lock = resolve_runtime_dependencies(
        primary_wheels=[(primary, frozenset({"feature", "dev"}))],
        wheelhouse=wh,
    )
    assert lock["primarya"].extras == frozenset({"feature", "dev"})


def test_cumulative_extras_on_already_locked(tmp_path: Path) -> None:
    """When a dependency is required by two parents with different extras,
    the locked extras accumulate."""
    wh = tmp_path / "wh"
    wh.mkdir()

    # Shared dep with two extras: feature and debug
    _build_metadata_wheel_with_extras(
        "shared", "1.0.0", [], wh, provides_extra=["feature", "debug"]
    )
    # Parent A requires shared[feature]
    _build_metadata_wheel(
        "parenta", "1.0.0", ["shared[feature]"], wh
    )
    # Parent B requires shared AND shared[debug]
    _build_metadata_wheel(
        "parentb", "1.0.0", ["shared", "shared[debug]"], wh
    )

    # Primary = parenta + parentb
    lock = resolve_runtime_dependencies(
        primary_wheels=[
            (wh / "parenta-1.0.0-py3-none-any.whl", frozenset()),
            (wh / "parentb-1.0.0-py3-none-any.whl", frozenset()),
        ],
        wheelhouse=wh,
    )

    assert "shared" in lock
    # shared was requested by parenta with [feature], then by parentb with
    # base + [debug].  Cumulative: feature + debug.
    assert lock["shared"].extras == frozenset({"feature", "debug"})
    # required_by includes both parents
    assert lock["shared"].required_by == frozenset({"parenta", "parentb"})


# ===========================================================================
# H3: Validate transitive extras even when dep is already locked
# ===========================================================================


def test_transitive_extra_not_found_on_already_locked_dep(tmp_path: Path) -> None:
    """Requires-Dist: B[missing_extra] must raise ExtraNotFound after
    reading B METADATA, even when B is already locked."""
    wh = tmp_path / "wh"
    wh.mkdir()

    # B has provides-extra: feature ONLY
    _build_metadata_wheel_with_extras(
        "b", "1.0.0", [], wh, provides_extra=["feature"]
    )
    # A requires B (base) → B gets locked with no extras
    _build_metadata_wheel("a", "1.0.0", ["b"], wh)

    # C requires B[missing_extra] → should raise ExtraNotFound on already-locked B
    _build_metadata_wheel("c", "1.0.0", ["b[missing_extra]"], wh)

    with pytest.raises(ExtraNotFound, match="missing-extra"):
        resolve_runtime_dependencies(
            primary_wheels=[
                (wh / "a-1.0.0-py3-none-any.whl", frozenset()),
                (wh / "c-1.0.0-py3-none-any.whl", frozenset()),
            ],
            wheelhouse=wh,
        )


# ===========================================================================
# H4: Distinguish IncompatibleWheelTag from MissingDependency (done above)
# ===========================================================================

# Tests for H4 are: test_incompatible_tag_raises_incompatible_wheel_tag,
# test_missing_dependency_no_wheel_at_all, test_incompatible_tag_after_version_match.

# ===========================================================================
# H5: required_by excludes inactive extra/marker-gated edges
# ===========================================================================


def test_required_by_excludes_inactive_extra_edge(tmp_path: Path) -> None:
    """An inactive extra requirement does not create a spurious required_by edge.

    Setup:
    - A has Requires-Dist: B; extra == 'feature' (inactive: A has no active extras)
    - C has Requires-Dist: B (no marker, always active)
    - B is locked because C requires it.
    - B's required_by should contain C but NOT A.
    """
    wh = tmp_path / "wh"
    wh.mkdir()

    # B: a simple leaf dep
    _build_metadata_wheel("b", "1.0.0", [], wh)

    # A: depends on B only via an inactive extra
    _build_metadata_wheel_with_extras(
        "a", "1.0.0",
        requires_dist=["b; extra == 'feature'"],
        provides_extra=["feature"],
        output=wh,
    )

    # C: depends on B unconditionally
    _build_metadata_wheel("c", "1.0.0", ["b"], wh)

    lock = resolve_runtime_dependencies(
        primary_wheels=[
            (wh / "a-1.0.0-py3-none-any.whl", frozenset()),    # no active extras
            (wh / "c-1.0.0-py3-none-any.whl", frozenset()),
        ],
        wheelhouse=wh,
    )

    assert "b" in lock
    # A should NOT be in required_by because its req was inactive
    assert "a" not in lock["b"].required_by
    # C should be in required_by
    assert "c" in lock["b"].required_by


def test_required_by_includes_active_extra_edge(tmp_path: Path) -> None:
    """When an extra IS activated, the required_by edge IS created.

    Same setup as above but A has 'feature' activated.
    """
    wh = tmp_path / "wh"
    wh.mkdir()

    _build_metadata_wheel("b", "1.0.0", [], wh)
    _build_metadata_wheel_with_extras(
        "a", "1.0.0",
        requires_dist=["b; extra == 'feature'"],
        provides_extra=["feature"],
        output=wh,
    )

    lock = resolve_runtime_dependencies(
        primary_wheels=[
            (wh / "a-1.0.0-py3-none-any.whl", frozenset({"feature"})),  # active!
        ],
        wheelhouse=wh,
    )

    assert "b" in lock
    # A's requirement is active, so A should be in required_by
    assert "a" in lock["b"].required_by


# ===========================================================================
# H6: Wheel identity verification — filename ↔ METADATA
# ===========================================================================


def test_primary_wheel_name_mismatch_blocks(tmp_path: Path) -> None:
    """Primary wheel with filename distribution != METADATA Name → WheelIdentityMismatch before lock."""
    wh = tmp_path / "wh"
    wh.mkdir()
    bad = _build_identity_mismatch_wheel(
        wh, filename_name="goodlib", meta_name="evillib",
        filename_version="1.0.0", meta_version="1.0.0",
    )

    with pytest.raises(WheelIdentityMismatch, match="goodlib.*evillib"):
        resolve_runtime_dependencies(
            primary_wheels=[(bad, frozenset())],
            wheelhouse=wh,
        )


def test_primary_wheel_version_mismatch_blocks(tmp_path: Path) -> None:
    """Primary wheel with filename version != METADATA Version → WheelIdentityMismatch before lock."""
    wh = tmp_path / "wh"
    wh.mkdir()
    bad = _build_identity_mismatch_wheel(
        wh, filename_name="goodlib", meta_name="goodlib",
        filename_version="1.0.0", meta_version="9.9.9",
    )

    with pytest.raises(WheelIdentityMismatch, match="1.0.0.*9.9.9"):
        resolve_runtime_dependencies(
            primary_wheels=[(bad, frozenset())],
            wheelhouse=wh,
        )


def test_wheelhouse_candidate_name_mismatch_blocks(tmp_path: Path) -> None:
    """Dependency resolved from wheelhouse with filename name != METADATA Name → WheelIdentityMismatch."""
    wh = tmp_path / "wh"
    wh.mkdir()

    primary = _build_metadata_wheel("primary", "1.0.0", ["leaf-lib"], wh)

    _build_identity_mismatch_wheel(
        wh, filename_name="leaf_lib", meta_name="evil_lib",
        filename_version="1.0.0", meta_version="1.0.0",
    )

    with pytest.raises(WheelIdentityMismatch, match="evil_lib"):
        resolve_runtime_dependencies(
            primary_wheels=[(primary, frozenset())],
            wheelhouse=wh,
        )


def test_wheelhouse_candidate_version_mismatch_blocks(tmp_path: Path) -> None:
    """Filename claims 1.0.0, METADATA claims 9.9.0 → WheelIdentityMismatch."""
    wh = tmp_path / "wh"
    wh.mkdir()

    primary = _build_metadata_wheel("primary", "1.0.0", ["leaf-lib"], wh)
    _build_identity_mismatch_wheel(
        wh, filename_name="leaf_lib", meta_name="leaf_lib",
        filename_version="1.0.0", meta_version="9.9.0",
    )

    with pytest.raises(WheelIdentityMismatch, match="1.0.0.*9.9.0"):
        resolve_runtime_dependencies(
            primary_wheels=[(primary, frozenset())],
            wheelhouse=wh,
        )


def test_wheelhouse_candidate_version_normalized_match(tmp_path: Path) -> None:
    """Version 1.0 == Version 1.0.0 via packaging.version.Version normalisation — should pass."""
    wh = tmp_path / "wh"
    wh.mkdir()

    primary = _build_metadata_wheel("primary", "1.0.0", ["leaf-lib"], wh)
    _build_identity_mismatch_wheel(
        wh, filename_name="leaf_lib", meta_name="leaf_lib",
        filename_version="1.0", meta_version="1.0.0",
    )
    lock = resolve_runtime_dependencies(
        primary_wheels=[(primary, frozenset())],
        wheelhouse=wh,
    )
    assert "leaf-lib" in lock
    assert lock["leaf-lib"].version == "1.0.0"  # from METADATA


def test_identity_mismatch_includes_kind_attribute(tmp_path: Path) -> None:
    """WheelIdentityMismatch carries structured kind/field attributes."""
    wh = tmp_path / "wh"
    wh.mkdir()

    bad = _build_identity_mismatch_wheel(
        wh, filename_name="a", meta_name="b",
        filename_version="1.0.0", meta_version="1.0.0",
    )

    with pytest.raises(WheelIdentityMismatch) as exc_info:
        resolve_runtime_dependencies(
            primary_wheels=[(bad, frozenset())],
            wheelhouse=wh,
        )
    assert exc_info.value.kind == "name"
    assert exc_info.value.filename_name == "a"
    assert exc_info.value.metadata_name == "b"


def test_resolver_missing_name_in_metadata_after_identity_parse(tmp_path: Path) -> None:
    """METADATA with blank Name field → MetadataError (preserved from M1-1A baseline)."""
    wh = tmp_path / "wh"
    wh.mkdir()

    import zipfile
    wheel_path = wh / "blank_name-1.0.0-py3-none-any.whl"
    dist_info = "blank_name-1.0.0.dist-info"
    metadata = "Metadata-Version: 2.4\nName: \nVersion: 1.0.0\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)

    with pytest.raises(MetadataError, match="Name"):
        resolve_runtime_dependencies(
            primary_wheels=[(wheel_path, frozenset())],
            wheelhouse=wh,
        )


def test_duplicate_metadata_blocks_via_resolver_path(tmp_path: Path) -> None:
    """Wheel with duplicate METADATA entries in ZIP → MetadataError via read_wheel_metadata_raw."""
    wh = tmp_path / "wh"
    wh.mkdir()

    import zipfile
    wheel_path = wh / "double_meta-1.0.0-py3-none-any.whl"
    dist_info = "double_meta-1.0.0.dist-info"
    metadata = "Metadata-Version: 2.4\nName: double-meta\nVersion: 1.0.0\n"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        # Write duplicate METADATA entry with different content
        zf.writestr(f"{dist_info}/METADATA", metadata + "# extra\n")
        zf.writestr(f"{dist_info}/RECORD", f"{dist_info}/METADATA,,\n")

    with pytest.raises(MetadataError, match="duplicate"):
        resolve_runtime_dependencies(
            primary_wheels=[(wheel_path, frozenset())],
            wheelhouse=wh,
        )


def test_duplicate_name_field_blocks_via_resolver_path(tmp_path: Path) -> None:
    """METADATA with duplicate Name fields is ambiguous and blocks via resolver."""
    wh = tmp_path / "wh"
    wh.mkdir()

    import zipfile
    wheel_path = wh / "duplicate_name-1.0.0-py3-none-any.whl"
    dist_info = "duplicate_name-1.0.0.dist-info"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: duplicate-name\n"
        "Name: other-name\n"
        "Version: 1.0.0\n"
    )
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", f"{dist_info}/METADATA,,\n")

    with pytest.raises(MetadataError, match="duplicate canonical METADATA field: Name"):
        resolve_runtime_dependencies(
            primary_wheels=[(wheel_path, frozenset())],
            wheelhouse=wh,
        )


def test_duplicate_version_field_blocks_via_resolver_path(tmp_path: Path) -> None:
    """METADATA with duplicate Version fields is ambiguous and blocks via resolver."""
    wh = tmp_path / "wh"
    wh.mkdir()

    import zipfile
    wheel_path = wh / "duplicate_version-1.0.0-py3-none-any.whl"
    dist_info = "duplicate_version-1.0.0.dist-info"
    metadata = (
        "Metadata-Version: 2.4\n"
        "Name: duplicate-version\n"
        "Version: 1.0.0\n"
        "Version: 2.0.0\n"
    )
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", f"{dist_info}/METADATA,,\n")

    with pytest.raises(MetadataError, match="duplicate canonical METADATA field: Version"):
        resolve_runtime_dependencies(
            primary_wheels=[(wheel_path, frozenset())],
            wheelhouse=wh,
        )


def test_multiple_dist_info_blocks_via_resolver_path(tmp_path: Path) -> None:
    """Wheel with multiple .dist-info directories blocks via resolver."""
    wh = tmp_path / "wh"
    wh.mkdir()

    import zipfile
    wheel_path = wh / "multi_dist-1.0.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "multi_dist-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: multi-dist\nVersion: 1.0.0\n",
        )
        zf.writestr("multi_dist-1.0.0.dist-info/RECORD", "")
        zf.writestr(
            "other-1.0.0.dist-info/METADATA",
            "Metadata-Version: 2.4\nName: other\nVersion: 1.0.0\n",
        )
        zf.writestr("other-1.0.0.dist-info/RECORD", "")

    with pytest.raises(MetadataError, match=r"multiple \.dist-info"):
        resolve_runtime_dependencies(
            primary_wheels=[(wheel_path, frozenset())],
            wheelhouse=wh,
        )


def test_invalid_utf8_metadata_blocks_via_resolver_path(tmp_path: Path) -> None:
    """METADATA that is not valid UTF-8 blocks via resolver."""
    wh = tmp_path / "wh"
    wh.mkdir()

    import zipfile
    wheel_path = wh / "bad_utf8-1.0.0-py3-none-any.whl"
    dist_info = "bad_utf8-1.0.0.dist-info"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", b"Name: bad-utf8\nVersion: 1.0.0\n\xff")
        zf.writestr(f"{dist_info}/RECORD", b"")

    with pytest.raises(MetadataError, match="not valid UTF-8"):
        resolve_runtime_dependencies(
            primary_wheels=[(wheel_path, frozenset())],
            wheelhouse=wh,
        )


def test_metadata_error_for_primary_wheel_bad_zip(tmp_path: Path) -> None:
    """Non-ZIP file given as primary wheel → MetadataError."""
    wh = tmp_path / "wh"
    wh.mkdir()
    bad = wh / "not_a_wheel-1.0.0-py3-none-any.whl"
    bad.write_text("not a zip file")

    with pytest.raises(MetadataError, match="invalid wheel ZIP"):
        resolve_runtime_dependencies(
            primary_wheels=[(bad, frozenset())],
            wheelhouse=wh,
        )



# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_mid(tmp: Path) -> Path:
    """Build the mid_lib test fixture wheel in a temp dir."""
    fixtures = Path(__file__).resolve().parent / "fixtures"
    return build_wheel(fixtures / "mid_lib", output_dir=tmp / "mid")


def _build_metadata_wheel(
    name: str, version: str, requires_dist: list[str], output: Path
) -> Path:
    """Create a minimal wheel with given name, version, and Requires-Dist."""
    import zipfile
    safe_name = name.replace("-", "_")
    wheel_name = f"{safe_name}-{version}-py3-none-any.whl"
    wheel_path = output / wheel_name
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    for req in requires_dist:
        metadata += f"Requires-Dist: {req}\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)
    return wheel_path


def _build_metadata_wheel_with_extras(
    name: str,
    version: str,
    requires_dist: list[str],
    output: Path,
    provides_extra: list[str] | None = None,
) -> Path:
    """Create a minimal wheel with Provides-Extra metadata."""
    import zipfile
    safe_name = name.replace("-", "_")
    wheel_name = f"{safe_name}-{version}-py3-none-any.whl"
    wheel_path = output / wheel_name
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    for req in requires_dist:
        metadata += f"Requires-Dist: {req}\n"
    for extra in (provides_extra or ()):
        metadata += f"Provides-Extra: {extra}\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)
    return wheel_path


def _build_wheel_with_tag(
    output: Path,
    name: str,
    version: str,
    tag: str,
) -> Path:
    """Create a minimal wheel with a specific tag in the filename."""
    import zipfile
    wheel_name = f"{name}-{version}-{tag}.whl"
    wheel_path = output / wheel_name
    dist_info = f"{name}-{version}.dist-info"
    metadata = f"Metadata-Version: 2.4\nName: {name}\nVersion: {version}\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n"
    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)
    return wheel_path

def _build_identity_mismatch_wheel(
    output: Path,
    filename_name: str,
    meta_name: str,
    filename_version: str,
    meta_version: str,
) -> Path:
    """Build a wheel where the filename identity differs from METADATA identity.

    The wheel filename declares *filename_name* / *filename_version*
    (e.g. evil-1.0-py3-none-any.whl), but the .dist-info
    directory and METADATA declare *meta_name* / *meta_version*
    (e.g. Name: other, Version: 9.9).

    The .dist-info directory name always matches the METADATA
    identity (as real wheels do), while the wheel filename diverges.
    """
    import zipfile
    safe_filename = filename_name.replace("-", "_")
    wheel_name = f"{safe_filename}-{filename_version}-py3-none-any.whl"
    wheel_path = output / wheel_name

    # .dist-info dir matches METADATA identity (canonical)
    safe_meta = meta_name.replace("-", "_")
    dist_info = f"{safe_meta}-{meta_version}.dist-info"

    metadata_lines = [
        "Metadata-Version: 2.4",
        f"Name: {meta_name}",
        f"Version: {meta_version}",
    ]
    metadata = "\n".join(metadata_lines) + "\n"
    record = f"{dist_info}/METADATA,,\n{dist_info}/RECORD,,\n"

    with zipfile.ZipFile(wheel_path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{dist_info}/METADATA", metadata)
        zf.writestr(f"{dist_info}/RECORD", record)
    return wheel_path
