"""M0-7A hardened: release manifest, artifact verification, adversarial tests."""

from __future__ import annotations

import hashlib
import textwrap
import zipfile
from pathlib import Path

import pytest

from zealfie.building import WheelInspectionError, build_wheel, inspect_wheel
from zealfie.common import normalise_distribution_name
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.releases import (
    ArtifactRejectionError,
    ReleaseManifest,
    ReleaseManifestError,
    VerifiedArtifact,
    parse_release_manifest,
    verify_artifact,
)
from zealfie.runtime import (
    InstallOutcome,
    RuntimeLayout,
    RuntimeState,
    SharedRuntime,
)

WITNESS_DEF = ComponentDefinition(
    "zewitness", "ZeWitness", "zealfie-witness",
    (EntryPointContract("console_scripts", "zewitness"),),
)
OTHER_DEF = ComponentDefinition(
    "other", "Other", "other-dist",
    (EntryPointContract("console_scripts", "other"),),
)

_WITNESS_TOML = """\
schema_version = 1
component_id = "zewitness"
version = "0.0.1"

[[artifacts]]
filename = "{filename}"
size = {size}
sha256 = "{sha256}"
"""


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("7a2-wheel")
    return build_wheel(d, output_dir=t)


@pytest.fixture()
def witness_registry() -> ComponentRegistry:
    return ComponentRegistry([WITNESS_DEF])


def _make_manifest(wheel_path: Path, **overrides) -> tuple[ReleaseManifest, str]:
    sha = _sha256(wheel_path)
    size = wheel_path.stat().st_size
    params = {"filename": wheel_path.name, "size": str(size), "sha256": sha, **overrides}
    toml_text = _WITNESS_TOML.format(**params)
    manifest = parse_release_manifest(toml_text)
    return manifest, toml_text


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _synthetic_wheel(tmp_path: Path, name: str, version: str, extras: dict | None = None) -> Path:
    """Build a minimal synthetic wheel ZIP for adversarial testing."""
    p = tmp_path / "test.whl"
    z = zipfile.ZipFile(p, "w")
    dist = f"{name}-{version}.dist-info"
    meta = f"Name: {name}\nVersion: {version}\n"
    z.writestr(f"{dist}/METADATA", meta)
    z.writestr(f"{dist}/entry_points.txt", extras.get("entry_points", "") if extras else "")
    z.writestr(f"{dist}/RECORD", "")
    if extras and extras.get("second_dist_info"):
        z.writestr("other-1.0.dist-info/METADATA", "Name: other\nVersion: 1.0\n")
        z.writestr("other-1.0.dist-info/RECORD", "")
    z.close()
    return p


# ===================================================================
# Wheel inspection hardening
# ===================================================================


def test_inspect_wheel_has_distribution_name(witness_wheel):
    info = inspect_wheel(witness_wheel)
    assert info.distribution_name == "zealfie-witness"
    assert info.version == "0.0.1"


def test_inspect_wheel_missing_name_rejected(tmp_path):
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("bad-1.0.dist-info/METADATA", "Version: 1.0\n")
    with pytest.raises(WheelInspectionError, match="Name"):
        inspect_wheel(p)


def test_inspect_wheel_missing_version_rejected(tmp_path):
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("bad-1.0.dist-info/METADATA", "Name: bad\n")
    with pytest.raises(WheelInspectionError, match="Version"):
        inspect_wheel(p)


def test_inspect_wheel_multiple_dist_info_rejected(tmp_path):
    p = _synthetic_wheel(tmp_path, "test", "1.0", {"second_dist_info": True})
    with pytest.raises(WheelInspectionError, match="multiple"):
        inspect_wheel(p)


def test_inspect_wheel_no_dist_info_rejected(tmp_path):
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("something.txt", "hello")
    with pytest.raises(WheelInspectionError, match="no .dist-info"):
        inspect_wheel(p)


def test_inspect_wheel_missing_metadata_rejected(tmp_path):
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("test-1.0.dist-info/RECORD", "")
    with pytest.raises(WheelInspectionError, match="METADATA"):
        inspect_wheel(p)


def test_inspect_wheel_corrupt_zip_rejected(tmp_path):
    p = tmp_path / "corrupt.whl"
    p.write_text("not a zip")
    with pytest.raises(WheelInspectionError, match="invalid"):
        inspect_wheel(p)


# -------------------------------------------------------------------
# M0-7A Final Micro-Hardening — duplicate critical members
# -------------------------------------------------------------------


def _synthetic_dup_wheel(tmp_path: Path, name: str, version: str, *, dup_metadata: bool = False, dup_entry_points: bool = False) -> Path:
    """Build a synthetic wheel with optional duplicate ZIP members."""
    p = tmp_path / "test.whl"
    import warnings
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        z = zipfile.ZipFile(p, "w")
    dist = f"{name}-{version}.dist-info"
    meta = f"Name: {name}\nVersion: {version}\n"
    ep = "[console_scripts]\nzewitness = zewitness.__main__:main\n"
    z.writestr(f"{dist}/METADATA", meta)
    if dup_metadata:
        z.writestr(f"{dist}/METADATA", meta)
    z.writestr(f"{dist}/entry_points.txt", ep)
    if dup_entry_points:
        z.writestr(f"{dist}/entry_points.txt", ep)
    z.writestr(f"{dist}/RECORD", "")
    z.close()
    return p


def test_duplicate_metadata_zip_member_rejected(tmp_path):
    """Duplicate METADATA ZIP member → WheelInspectionError."""
    p = _synthetic_dup_wheel(tmp_path, "test", "1.0", dup_metadata=True)
    with pytest.raises(WheelInspectionError, match="duplicate critical member.*METADATA"):
        inspect_wheel(p)


def test_duplicate_entry_points_zip_member_rejected(tmp_path):
    """Duplicate entry_points.txt ZIP member → WheelInspectionError."""
    p = _synthetic_dup_wheel(tmp_path, "test", "1.0", dup_entry_points=True)
    with pytest.raises(WheelInspectionError, match="duplicate critical member.*entry_points.txt"):
        inspect_wheel(p)


def test_duplicate_name_field_rejected(tmp_path):
    """Duplicate Name field in METADATA → WheelInspectionError."""
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("test-1.0.dist-info/METADATA", "Name: expected\nName: other\nVersion: 1.0\n")
        z.writestr("test-1.0.dist-info/RECORD", "")
    with pytest.raises(WheelInspectionError, match="duplicate canonical METADATA field: Name"):
        inspect_wheel(p)


def test_duplicate_version_field_rejected(tmp_path):
    """Duplicate Version field in METADATA → WheelInspectionError."""
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("test-1.0.dist-info/METADATA", "Name: test\nVersion: 1.0\nVersion: 2.0\n")
        z.writestr("test-1.0.dist-info/RECORD", "")
    with pytest.raises(WheelInspectionError, match="duplicate canonical METADATA field: Version"):
        inspect_wheel(p)


def test_duplicate_entry_point_contract_rejected(tmp_path):
    """Duplicate group:name entry point → WheelInspectionError."""
    p = tmp_path / "bad.whl"
    with zipfile.ZipFile(p, "w") as z:
        meta = "Name: test\nVersion: 1.0\n"
        ep = "[console_scripts]\nzewitness = evil:main\nzewitness = expected:main\n"
        z.writestr("test-1.0.dist-info/METADATA", meta)
        z.writestr("test-1.0.dist-info/entry_points.txt", ep)
        z.writestr("test-1.0.dist-info/RECORD", "")
    with pytest.raises(WheelInspectionError, match="duplicate entry-point contract: console_scripts:zewitness"):
        inspect_wheel(p)


def test_non_whl_artifact_rejected(witness_wheel, witness_registry):
    """Artifact filename not ending in .whl → ArtifactRejectionError."""
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "filename", "artifact.zip")
    with pytest.raises(ArtifactRejectionError, match="must end with .whl"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)


# ===================================================================
# Schema_version strict type
# ===================================================================


def test_schema_version_bool_rejected():
    with pytest.raises(ReleaseManifestError, match="integer"):
        parse_release_manifest(
            'schema_version = true\ncomponent_id="x"\nversion="1"\n'
            '[[artifacts]]\nfilename="f.whl"\nsize=0\n'
            'sha256="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"\n'
        )


# ===================================================================
# Component binding
# ===================================================================


def test_unknown_component_rejected(witness_wheel, witness_registry):
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "component_id", "unknown")
    with pytest.raises(ArtifactRejectionError, match="unknown"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)


def test_component_mismatch_rejected(witness_wheel):
    registry = ComponentRegistry([OTHER_DEF])
    manifest, _ = _make_manifest(witness_wheel)
    with pytest.raises(ArtifactRejectionError, match="unknown"):
        verify_artifact(manifest, registry=registry, artifact_root=witness_wheel.parent)


# ===================================================================
# Artifact path safety (extended)
# ===================================================================


def test_symlink_inside_root_rejected(tmp_path, witness_wheel):
    """A symlink artifact is rejected even if target is inside root."""
    root = tmp_path / "root"
    root.mkdir()
    real = root / "real.whl"
    real.write_bytes(witness_wheel.read_bytes())
    sym = root / witness_wheel.name
    sym.symlink_to("real.whl")
    manifest, _ = _make_manifest(real)
    object.__setattr__(manifest, "filename", witness_wheel.name)
    with pytest.raises(ArtifactRejectionError, match="symlink"):
        verify_artifact(manifest, registry=ComponentRegistry([WITNESS_DEF]), artifact_root=root)


def test_symlink_outside_root_rejected(tmp_path, witness_wheel):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.whl"
    outside.write_bytes(witness_wheel.read_bytes())
    sym = root / witness_wheel.name
    try:
        sym.symlink_to(outside)
    except OSError:
        pytest.skip("symlink not allowed")
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "filename", witness_wheel.name)
    with pytest.raises(ArtifactRejectionError, match="symlink"):
        verify_artifact(manifest, registry=ComponentRegistry([WITNESS_DEF]), artifact_root=root)


def test_backslash_filename_rejected(witness_wheel, witness_registry):
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "filename", "foo\\bar")
    with pytest.raises(ArtifactRejectionError, match="path separators"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=Path("/tmp"))


def test_windows_drive_filename_rejected(witness_wheel, witness_registry):
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "filename", "C:\\foo.whl")
    # Backslash triggers "path separators", drive letter is also rejected.
    with pytest.raises(ArtifactRejectionError):
        verify_artifact(manifest, registry=witness_registry, artifact_root=Path("/tmp"))


def test_missing_artifact_rejected(witness_wheel, witness_registry):
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "filename", "nonexistent.whl")
    with pytest.raises(ArtifactRejectionError, match="not found"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=Path("/tmp"))


# ===================================================================
# Integrity
# ===================================================================


def test_version_mismatch_rejected(witness_wheel, witness_registry):
    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "version", "9.9.9")
    with pytest.raises(ArtifactRejectionError, match="version mismatch"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)


# ===================================================================
# Unknown manifest keys
# ===================================================================


def test_unknown_artifact_key_rejected():
    with pytest.raises(ReleaseManifestError, match="unknown key"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            extra_field = 1
        """))


def test_missing_filename_rejected():
    with pytest.raises(ReleaseManifestError, match="filename"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """))


def test_missing_size_rejected():
    with pytest.raises(ReleaseManifestError, match="size"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """))


def test_missing_sha256_rejected():
    with pytest.raises(ReleaseManifestError, match="sha256"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
        """))


# ===================================================================
# Contract failure preserves active runtime
# ===================================================================


def test_contract_failure_preserves_runtime(tmp_path, witness_wheel, witness_registry):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    pointer_before = layout.active_pointer.read_bytes()

    manifest, _ = _make_manifest(witness_wheel)
    object.__setattr__(manifest, "component_id", "zewitness")

    # Use a registry with a definition that has a wrong contract.
    wrong_def = ComponentDefinition(
        "zewitness", "ZeWitness", "zealfie-witness",
        (EntryPointContract("gui_scripts", "zesolver"),),
    )
    wrong_registry = ComponentRegistry([wrong_def])

    with pytest.raises(ArtifactRejectionError, match="launch contract"):
        verify_artifact(manifest, registry=wrong_registry, artifact_root=witness_wheel.parent)

    assert layout.active_pointer.read_bytes() == pointer_before


# ===================================================================
# M0-6 handoff: release → verify → candidate slot → activate
# ===================================================================


def test_release_to_transaction_with_candidate_slot(tmp_path, witness_wheel, witness_registry):
    """Full cycle using explicit candidate slot (never active)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)

    # Create initial runtime.
    rt.create()

    # Verify release.
    manifest, _ = _make_manifest(witness_wheel)
    va = verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)

    # Begin transaction for a candidate.
    txn = rt.begin_transaction()
    import venv
    slot_b = txn.candidate_slot_id
    layout.slot_path(slot_b).parent.mkdir(parents=True, exist_ok=True)
    venv.create(layout.slot_path(slot_b), with_pip=True, clear=True)

    # Install into candidate slot explicitly.
    r = rt.install_local_wheel(va.path, slot_id=slot_b, component_definition=WITNESS_DEF)
    assert r.outcome == InstallOutcome.INSTALLED

    # Active must still be the original slot.
    st = rt.status()
    assert st.active_slot_id != slot_b

    # Validate and activate.
    rt.validate_candidate(txn, component_definition=WITNESS_DEF)
    act = rt.activate(txn)
    assert act.active_slot_id == slot_b
    assert act.previous_slot_id is not None
