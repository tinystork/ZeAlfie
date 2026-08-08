"""M0-7B: release manifest, artifact verification, host compatibility + selection."""

from __future__ import annotations

import hashlib
import shutil
import textwrap
import zipfile
from pathlib import Path

import pytest

from zealfie.building import WheelInspectionError, build_wheel, inspect_wheel
from zealfie.common import normalise_distribution_name
from zealfie.components.model import ComponentDefinition, EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.releases import (
    ArtifactEntry,
    ArtifactRejectionError,
    ArtifactSelectionError,
    HostTarget,
    ReleaseManifest,
    ReleaseManifestError,
    ReleaseResolutionError,
    VerifiedArtifact,
    parse_release_manifest,
    resolve_local_release,
    select_artifact,
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

# M0-7B: support optional host-compatibility tags in the TOML template.
_WITNESS_TOML_SINGLE = """\
schema_version = 1
component_id = "zewitness"
version = "0.0.1"

[[artifacts]]
filename = "{filename}"
size = {size}
sha256 = "{sha256}"
"""

_WITNESS_TOML_MULTI = """\
schema_version = 1
component_id = "zewitness"
version = "0.0.1"

[[artifacts]]
filename = "{filename}"
size = {size}
sha256 = "{sha256}"
python_tag = "{python_tag}"
abi_tag = "none"
platform_tag = "any"

[[artifacts]]
filename = "{filename2}"
size = {size2}
sha256 = "{sha2562}"
python_tag = "{python_tag2}"
abi_tag = "{abi_tag2}"
platform_tag = "{platform_tag2}"
"""


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    d = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    t = tmp_path_factory.mktemp("7b-wheel")
    return build_wheel(d, output_dir=t)


@pytest.fixture()
def witness_registry() -> ComponentRegistry:
    return ComponentRegistry([WITNESS_DEF])


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def _copy_wheel_as(wheel_path: Path, root: Path, filename: str) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    copied = root / filename
    shutil.copy2(wheel_path, copied)
    return copied


def _make_manifest(wheel_path: Path, **overrides) -> ReleaseManifest:
    """Build a single-artifact manifest, optionally overriding fields."""
    sha = overrides.get("sha256", _sha256(wheel_path))
    size = overrides.get("size", wheel_path.stat().st_size)
    filename = overrides.get("filename", wheel_path.name)

    params = {
        "filename": filename,
        "size": str(size),
        "sha256": sha,
    }
    toml_text = _WITNESS_TOML_SINGLE.format(**params)
    manifest = parse_release_manifest(toml_text)

    # Apply field overrides for component_id/version (still direct fields).
    if "component_id" in overrides:
        object.__setattr__(manifest, "component_id", overrides["component_id"])
    if "version" in overrides:
        object.__setattr__(manifest, "version", overrides["version"])

    return manifest


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
    """Artifact filename not ending in .whl → ArtifactRejectionError (via TOML)."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "artifact.zip"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """)
    manifest = parse_release_manifest(toml_text)
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
    manifest = _make_manifest(witness_wheel, component_id="unknown")
    with pytest.raises(ArtifactRejectionError, match="unknown"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)


def test_component_mismatch_rejected(witness_wheel):
    registry = ComponentRegistry([OTHER_DEF])
    manifest = _make_manifest(witness_wheel)
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
    sha = _sha256(real)
    size = real.stat().st_size
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{witness_wheel.name}"
        size = {size}
        sha256 = "{sha}"
    """)
    manifest = parse_release_manifest(toml_text)
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
    sha = _sha256(witness_wheel)
    size = witness_wheel.stat().st_size
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{witness_wheel.name}"
        size = {size}
        sha256 = "{sha}"
    """)
    manifest = parse_release_manifest(toml_text)
    with pytest.raises(ArtifactRejectionError, match="symlink"):
        verify_artifact(manifest, registry=ComponentRegistry([WITNESS_DEF]), artifact_root=root)


def test_backslash_filename_rejected(witness_wheel, witness_registry):
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "foo\\\\bar"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """)
    manifest = parse_release_manifest(toml_text)
    with pytest.raises(ArtifactRejectionError, match="path separators"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=Path("/tmp"))


def test_windows_drive_filename_rejected(witness_wheel, witness_registry):
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "C:\\\\foo.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """)
    manifest = parse_release_manifest(toml_text)
    with pytest.raises(ArtifactRejectionError):
        verify_artifact(manifest, registry=witness_registry, artifact_root=Path("/tmp"))


def test_missing_artifact_rejected(witness_wheel, witness_registry):
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "nonexistent.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """)
    manifest = parse_release_manifest(toml_text)
    with pytest.raises(ArtifactRejectionError, match="not found"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=Path("/tmp"))


# ===================================================================
# Integrity
# ===================================================================


def test_version_mismatch_rejected(witness_wheel, witness_registry):
    manifest = _make_manifest(witness_wheel, version="9.9.9")
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
# M0-7B: Multi-artifact manifest parsing
# ===================================================================


def test_manifest_with_two_artifacts_parsed():
    """A manifest with two distinct artifacts parses correctly."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "zewitness-0.0.1-py3-none-any.whl"
        size = 1000
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"

        [[artifacts]]
        filename = "zewitness-0.0.1-py3-none-win_amd64.whl"
        size = 2000
        sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "win_amd64"
    """)
    manifest = parse_release_manifest(toml_text)
    assert len(manifest.artifacts) == 2
    assert manifest.artifacts[0].filename == "zewitness-0.0.1-py3-none-any.whl"
    assert manifest.artifacts[0].python_tag == "py3"
    assert manifest.artifacts[1].platform_tag == "win_amd64"

    # Backward-compat accessors raise on multi-artifact.
    with pytest.raises(AttributeError):
        _ = manifest.filename


def test_manifest_with_optional_tags_parsed():
    """Tags are optional; missing tags produce None."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """)
    manifest = parse_release_manifest(toml_text)
    ae = manifest.artifacts[0]
    assert ae.python_tag is None
    assert ae.abi_tag is None
    assert ae.platform_tag is None


def test_manifest_zero_artifacts_rejected():
    with pytest.raises(ReleaseManifestError, match="at least one"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"
            artifacts = []
        """))


def test_duplicate_artifact_filename_rejected():
    with pytest.raises(ReleaseManifestError, match="duplicate artifact filename"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "same.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

            [[artifacts]]
            filename = "same.whl"
            size = 0
            sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        """))


def test_unknown_release_manifest_key_rejected():
    with pytest.raises(ReleaseManifestError, match="unknown key.*release manifest"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"
            remote_url = "https://example.com"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        """))


def test_invalid_tag_type_rejected():
    """Tags must be strings if present."""
    with pytest.raises(ReleaseManifestError, match="python_tag must be a string"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            python_tag = 3
        """))


def test_empty_tag_rejected():
    """Tags must not be empty if present."""
    with pytest.raises(ReleaseManifestError, match="abi_tag must not be empty"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            abi_tag = ""
        """))


# ===================================================================
# M0-7B: HostTarget
# ===================================================================


def test_host_target_from_current_host():
    """from_current_host() returns a valid HostTarget."""
    host = HostTarget.from_current_host()
    assert host.python_tag.startswith("py")
    assert host.abi_tag.startswith("cp")
    assert host.platform_tag  # non-empty


def test_host_target_synthetic():
    """Synthetic HostTarget works for testing without real platform."""
    host = HostTarget(python_tag="py312", abi_tag="cp312", platform_tag="linux_x86_64")
    assert host.python_tag == "py312"
    assert host.abi_tag == "cp312"
    assert host.platform_tag == "linux_x86_64"


def test_host_target_is_immutable():
    host = HostTarget(python_tag="py312", abi_tag="cp312", platform_tag="linux_x86_64")
    with pytest.raises(Exception):
        host.python_tag = "py311"  # type: ignore[misc]


# ===================================================================
# M0-7B: Artifact selection — success
# ===================================================================


def test_single_universal_artifact_selected():
    """A single artifact without tags (universal) matches any host."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    idx = select_artifact(manifest, host)
    assert idx == 0


def test_exact_tag_match_selected():
    """Exact tag match selects the matching artifact."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f-linux.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        abi_tag = "cp312"
        platform_tag = "linux_x86_64"

        [[artifacts]]
        filename = "f-win.whl"
        size = 0
        sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        python_tag = "py312"
        abi_tag = "cp312"
        platform_tag = "win_amd64"
    """)
    manifest = parse_release_manifest(toml_text)
    host_linux = HostTarget("py312", "cp312", "linux_x86_64")
    idx = select_artifact(manifest, host_linux)
    assert idx == 0
    assert manifest.artifacts[idx].filename == "f-linux.whl"

    host_win = HostTarget("py312", "cp312", "win_amd64")
    idx = select_artifact(manifest, host_win)
    assert idx == 1
    assert manifest.artifacts[idx].filename == "f-win.whl"


def test_python_generic_major_match():
    """py3 tag on artifact matches py312 host (generic major)."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    idx = select_artifact(manifest, host)
    assert idx == 0


@pytest.mark.parametrize(
    ("artifact_python_tag", "host_python_tag"),
    [
        ("py31", "py313"),
        ("py312", "py313"),
        ("cp31", "cp313"),
    ],
)
def test_python_tag_long_prefixes_do_not_match(artifact_python_tag, host_python_tag):
    """Only exact python_tag or generic py-major matching is allowed."""
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "{artifact_python_tag}"
        abi_tag = "none"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget(host_python_tag, "cp313", "linux_x86_64")
    with pytest.raises(ArtifactSelectionError, match="no artifact compatible"):
        select_artifact(manifest, host)


def test_python_tag_py313_exact_match_selected():
    """py313 remains compatible with py313 by exact match."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py313"
        abi_tag = "none"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py313", "cp313", "linux_x86_64")
    idx = select_artifact(manifest, host)
    assert idx == 0


def test_none_abi_matches_any_host():
    """artifact abi_tag='none' matches any host ABI."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        abi_tag = "none"
        platform_tag = "linux_x86_64"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    idx = select_artifact(manifest, host)
    assert idx == 0


def test_any_platform_matches_any_host():
    """artifact platform_tag='any' matches any host platform."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        abi_tag = "cp312"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "win_amd64")
    idx = select_artifact(manifest, host)
    assert idx == 0


# ===================================================================
# M0-7B: Artifact selection — failures (fail-closed)
# ===================================================================


def test_zero_compatible_raises():
    """No compatible artifact → ArtifactSelectionError."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        abi_tag = "cp312"
        platform_tag = "win_amd64"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    with pytest.raises(ArtifactSelectionError, match="no artifact compatible"):
        select_artifact(manifest, host)


def test_multiple_compatible_ambiguous():
    """Two indistinguishable compatible artifacts → ArtifactSelectionError."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f-a.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"

        [[artifacts]]
        filename = "f-b.whl"
        size = 0
        sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    with pytest.raises(ArtifactSelectionError, match="ambiguous selection"):
        select_artifact(manifest, host)


def test_partial_tags_fail_closed_missing_python():
    """Partially-tagged artifact with missing python_tag is incompatible."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        abi_tag = "none"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    with pytest.raises(ArtifactSelectionError, match="no artifact compatible"):
        select_artifact(manifest, host)


def test_partial_tags_fail_closed_missing_abi():
    """Partially-tagged artifact with missing abi_tag is incompatible."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        platform_tag = "any"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    with pytest.raises(ArtifactSelectionError, match="no artifact compatible"):
        select_artifact(manifest, host)


def test_partial_tags_fail_closed_missing_platform():
    """Partially-tagged artifact with missing platform_tag is incompatible."""
    toml_text = textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        abi_tag = "cp312"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget("py312", "cp312", "linux_x86_64")
    with pytest.raises(ArtifactSelectionError, match="no artifact compatible"):
        select_artifact(manifest, host)


def test_artifact_index_out_of_range_rejected(witness_wheel, witness_registry):
    manifest = _make_manifest(witness_wheel)
    with pytest.raises(ArtifactRejectionError, match="out of range"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent, artifact_index=99)


def test_negative_artifact_index_rejected(witness_wheel, witness_registry):
    manifest = _make_manifest(witness_wheel)
    with pytest.raises(ArtifactRejectionError, match="out of range"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent, artifact_index=-1)


# ===================================================================
# M0-7B: Selection → verification integration
# ===================================================================


def test_select_then_verify_with_multi_artifact(witness_wheel, witness_registry):
    """Select the correct artifact, then verify it via artifact_index."""
    sha = _sha256(witness_wheel)
    size = witness_wheel.stat().st_size
    fn = witness_wheel.name

    # One compatible (correct tags) + one incompatible (wrong platform).
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{fn}"
        size = {size}
        sha256 = "{sha}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"

        [[artifacts]]
        filename = "nonexistent.whl"
        size = 0
        sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "win_amd64"
    """)
    manifest = parse_release_manifest(toml_text)
    host = HostTarget.from_current_host()

    # Select
    idx = select_artifact(manifest, host)
    assert idx == 0

    # Verify
    va = verify_artifact(
        manifest,
        registry=witness_registry,
        artifact_root=witness_wheel.parent,
        artifact_index=idx,
    )
    assert va.component_id == "zewitness"
    assert va.version == "0.0.1"


def test_verify_correct_artifact_index_multi(witness_wheel, witness_registry):
    """verify_artifact with artifact_index=0 works on multi-artifact manifest."""
    sha = _sha256(witness_wheel)
    size = witness_wheel.stat().st_size
    fn = witness_wheel.name

    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{fn}"
        size = {size}
        sha256 = "{sha}"

        [[artifacts]]
        filename = "distractor-wheel.whl"
        size = {size}
        sha256 = "{sha}"
    """)
    manifest = parse_release_manifest(toml_text)
    va = verify_artifact(
        manifest,
        registry=witness_registry,
        artifact_root=witness_wheel.parent,
        artifact_index=0,
    )
    assert va.component_id == "zewitness"


# ===================================================================
# Contract failure preserves active runtime
# ===================================================================


def test_contract_failure_preserves_runtime(tmp_path, witness_wheel, witness_registry):
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    rt.create()
    pointer_before = layout.active_pointer.read_bytes()

    manifest = _make_manifest(witness_wheel, component_id="zewitness")

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
    manifest = _make_manifest(witness_wheel)
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

# ===================================================================
# M0-7B Hardening — Finding 1: verify_artifact bypass fix
# ===================================================================


def test_multi_artifact_without_index_rejected(witness_wheel, witness_registry):
    """Multi-artifact manifest without explicit artifact_index → rejected."""
    sha = _sha256(witness_wheel)
    size = witness_wheel.stat().st_size
    fn = witness_wheel.name

    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{fn}"
        size = {size}
        sha256 = "{sha}"

        [[artifacts]]
        filename = "other.whl"
        size = {size}
        sha256 = "{sha}"
    """)
    manifest = parse_release_manifest(toml_text)
    with pytest.raises(ArtifactRejectionError, match="explicit artifact_index required"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)


def test_single_artifact_without_index_still_works(witness_wheel, witness_registry):
    """Single-artifact manifest without index continues to work (M0-7A compat)."""
    manifest = _make_manifest(witness_wheel)
    va = verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)
    assert va.component_id == "zewitness"


def test_multi_artifact_with_explicit_index_works(witness_wheel, witness_registry):
    """Multi-artifact manifest with explicit artifact_index=0 works."""
    sha = _sha256(witness_wheel)
    size = witness_wheel.stat().st_size
    fn = witness_wheel.name

    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{fn}"
        size = {size}
        sha256 = "{sha}"

        [[artifacts]]
        filename = "other.whl"
        size = {size}
        sha256 = "{sha}"
    """)
    manifest = parse_release_manifest(toml_text)
    va = verify_artifact(
        manifest, registry=witness_registry,
        artifact_root=witness_wheel.parent, artifact_index=0,
    )
    assert va.component_id == "zewitness"


def test_adversarial_artifact0_wrong_host_bypassed_default(witness_wheel, witness_registry):
    """Adversarial: artifact 0 tagged win_amd64, no index → reject.

    Even though artifact 0 would be valid on Windows, on Linux the
    verifier must refuse to silently verify it.  The old default of
    artifact_index=0 would have let this pass.
    """
    sha = _sha256(witness_wheel)
    size = witness_wheel.stat().st_size
    fn = witness_wheel.name

    # Artifact 0 is tagged win_amd64 (incompatible on Linux).
    # Artifact 1 is tagged linux_x86_64 (correct for Linux).
    toml_text = textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{fn}"
        size = {size}
        sha256 = "{sha}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "win_amd64"

        [[artifacts]]
        filename = "adversarial-copy.whl"
        size = {size}
        sha256 = "{sha}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "linux_x86_64"
    """)
    manifest = parse_release_manifest(toml_text)

    # Without explicit index → MUST reject.
    with pytest.raises(ArtifactRejectionError, match="explicit artifact_index required"):
        verify_artifact(manifest, registry=witness_registry, artifact_root=witness_wheel.parent)

    import shutil
    shutil.copy2(witness_wheel, witness_wheel.parent / "adversarial-copy.whl")
    # With explicit index 1 (correct for Linux host) → succeeds.
    host = HostTarget.from_current_host()
    idx = select_artifact(manifest, host)
    assert idx == 1  # the linux one
    va = verify_artifact(
        manifest, registry=witness_registry,
        artifact_root=witness_wheel.parent, artifact_index=idx,
    )
    assert va.component_id == "zewitness"


# ===================================================================
# M0-7C: Safe local release resolution
# ===================================================================


def test_resolve_local_release_single_untagged_witness(tmp_path, witness_wheel, witness_registry):
    """M0-7A-style untagged single artifact resolves to a VerifiedArtifact."""
    root = tmp_path / "release"
    artifact = _copy_wheel_as(witness_wheel, root, witness_wheel.name)
    manifest = _make_manifest(artifact)
    host = HostTarget("py312", "cp312", "linux_x86_64")

    verified = resolve_local_release(
        manifest,
        registry=witness_registry,
        artifact_root=root,
        host=host,
    )

    assert isinstance(verified, VerifiedArtifact)
    assert verified.path == artifact
    assert verified.component_id == "zewitness"


def test_resolve_local_release_tagged_witness_matches_filename(tmp_path, witness_wheel, witness_registry):
    """Declared py3/none/any tags matching the wheel filename resolve."""
    root = tmp_path / "release"
    filename = "zealfie_witness-0.0.1-py3-none-any.whl"
    artifact = _copy_wheel_as(witness_wheel, root, filename)
    sha = _sha256(artifact)
    size = artifact.stat().st_size
    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{filename}"
        size = {size}
        sha256 = "{sha}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """))

    verified = resolve_local_release(
        manifest,
        registry=witness_registry,
        artifact_root=root,
        host=HostTarget("py312", "cp312", "linux_x86_64"),
    )

    assert verified.path == artifact


def test_resolve_local_release_multi_artifact_verifies_selected_entry(tmp_path, witness_wheel, witness_registry):
    """One incompatible + one compatible artifact resolves and verifies the compatible file."""
    root = tmp_path / "release"
    win_name = "zealfie_witness-0.0.1-py3-none-win_amd64.whl"
    any_name = "zealfie_witness-0.0.1-py3-none-any.whl"
    win_artifact = _copy_wheel_as(witness_wheel, root, win_name)
    any_artifact = _copy_wheel_as(witness_wheel, root, any_name)
    sha_win = _sha256(win_artifact)
    sha_any = _sha256(any_artifact)
    size_win = win_artifact.stat().st_size
    size_any = any_artifact.stat().st_size

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{win_name}"
        size = {size_win}
        sha256 = "{sha_win}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "win_amd64"

        [[artifacts]]
        filename = "{any_name}"
        size = {size_any}
        sha256 = "{sha_any}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """))

    verified = resolve_local_release(
        manifest,
        registry=witness_registry,
        artifact_root=root,
        host=HostTarget("py312", "cp312", "linux_x86_64"),
    )

    assert verified.path == any_artifact
    assert verified.sha256 == sha_any


def test_resolve_local_release_tampered_selected_artifact_rejected(tmp_path, witness_wheel, witness_registry):
    """Selected artifact with sha/size borrowed from another file is rejected."""
    root = tmp_path / "release"
    filename = "zealfie_witness-0.0.1-py3-none-any.whl"
    _copy_wheel_as(witness_wheel, root, filename)
    wrong_wheel = _synthetic_wheel(tmp_path, "other", "0.0.1")
    wrong_sha = _sha256(wrong_wheel)
    wrong_size = wrong_wheel.stat().st_size

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{filename}"
        size = {wrong_size}
        sha256 = "{wrong_sha}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """))

    with pytest.raises(ReleaseResolutionError, match="size mismatch|SHA256 mismatch"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


def test_resolve_local_release_select_a_verify_b_bypass_impossible(tmp_path, witness_wheel, witness_registry):
    """Resolver cannot select artifact 1 but accidentally verify artifact 0."""
    root = tmp_path / "release"
    valid_incompatible_name = "zealfie_witness-0.0.1-py3-none-win_amd64.whl"
    compatible_name = "zealfie_witness-0.0.1-py3-none-manylinux_x86_64.whl"
    valid_incompatible = _copy_wheel_as(witness_wheel, root, valid_incompatible_name)
    compatible = _copy_wheel_as(witness_wheel, root, compatible_name)

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{valid_incompatible_name}"
        size = {valid_incompatible.stat().st_size}
        sha256 = "{_sha256(valid_incompatible)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "win_amd64"

        [[artifacts]]
        filename = "{compatible_name}"
        size = {compatible.stat().st_size}
        sha256 = "{_sha256(compatible)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "manylinux_x86_64"
    """))

    verified = resolve_local_release(
        manifest,
        registry=witness_registry,
        artifact_root=root,
        host=HostTarget("py312", "cp312", "manylinux_x86_64"),
    )

    assert verified.path == compatible
    assert verified.path.name != valid_incompatible_name


def test_resolve_local_release_rejects_non_selected_filename_tag_mismatch(
    tmp_path, witness_wheel, witness_registry
):
    """A non-selected artifact with inconsistent tags fails the whole release."""
    root = tmp_path / "release"
    inconsistent_name = "zealfie_witness-0.0.1-py3-none-linux_x86_64.whl"
    compatible_name = "zealfie_witness-0.0.1-py3-none-any.whl"
    inconsistent = _copy_wheel_as(witness_wheel, root, inconsistent_name)
    compatible = _copy_wheel_as(witness_wheel, root, compatible_name)

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{inconsistent_name}"
        size = {inconsistent.stat().st_size}
        sha256 = "{_sha256(inconsistent)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "win_amd64"

        [[artifacts]]
        filename = "{compatible_name}"
        size = {compatible.stat().st_size}
        sha256 = "{_sha256(compatible)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """))

    with pytest.raises(ReleaseResolutionError, match="platform_tag mismatch"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


def test_resolve_local_release_platform_tag_must_match_filename(tmp_path, witness_wheel, witness_registry):
    """Manifest platform_tag linux_x86_64 with filename py3-none-any is rejected."""
    root = tmp_path / "release"
    filename = "zealfie_witness-0.0.1-py3-none-any.whl"
    artifact = _copy_wheel_as(witness_wheel, root, filename)

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{filename}"
        size = {artifact.stat().st_size}
        sha256 = "{_sha256(artifact)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "linux_x86_64"
    """))

    with pytest.raises(ReleaseResolutionError, match="platform_tag mismatch"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


def test_resolve_local_release_python_tag_must_match_filename(tmp_path, witness_wheel, witness_registry):
    """Manifest python_tag py313 with filename py3-none-any is rejected."""
    root = tmp_path / "release"
    filename = "zealfie_witness-0.0.1-py3-none-any.whl"
    artifact = _copy_wheel_as(witness_wheel, root, filename)

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{filename}"
        size = {artifact.stat().st_size}
        sha256 = "{_sha256(artifact)}"
        python_tag = "py313"
        abi_tag = "none"
        platform_tag = "any"
    """))

    with pytest.raises(ReleaseResolutionError, match="python_tag mismatch"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py313", "cp313", "linux_x86_64"),
        )


def test_resolve_local_release_ambiguous_compatible_artifacts_rejected(tmp_path, witness_wheel, witness_registry):
    """Two compatible artifacts remain ambiguous through the resolver."""
    root = tmp_path / "release"
    first = _copy_wheel_as(witness_wheel, root, "zewitness-0.0.1-1-py3-none-any.whl")
    second = _copy_wheel_as(witness_wheel, root, "zewitness-0.0.1-2-py3-none-any.whl")

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{first.name}"
        size = {first.stat().st_size}
        sha256 = "{_sha256(first)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"

        [[artifacts]]
        filename = "{second.name}"
        size = {second.stat().st_size}
        sha256 = "{_sha256(second)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """))

    with pytest.raises(ReleaseResolutionError, match="ambiguous selection"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


def test_resolve_local_release_unknown_component_rejected(tmp_path, witness_wheel, witness_registry):
    """Unknown component ids fail before artifact verification."""
    root = tmp_path / "release"
    artifact = _copy_wheel_as(witness_wheel, root, witness_wheel.name)
    manifest = _make_manifest(artifact, component_id="unknown")

    with pytest.raises(ReleaseResolutionError, match="unknown component"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


def test_resolve_local_release_wrong_wheel_distribution_rejected(tmp_path, witness_registry):
    """Wheel distribution identity is still enforced through the resolver."""
    root = tmp_path / "release"
    raw = _synthetic_wheel(tmp_path, "other-dist", "0.0.1")
    artifact = _copy_wheel_as(raw, root, "other_dist-0.0.1-py3-none-any.whl")

    manifest = parse_release_manifest(textwrap.dedent(f"""\
        schema_version = 1
        component_id = "zewitness"
        version = "0.0.1"

        [[artifacts]]
        filename = "{artifact.name}"
        size = {artifact.stat().st_size}
        sha256 = "{_sha256(artifact)}"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"
    """))

    with pytest.raises(ReleaseResolutionError, match="distribution mismatch"):
        resolve_local_release(
            manifest,
            registry=witness_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


def test_resolve_local_release_wrong_entry_point_contract_rejected(tmp_path, witness_wheel):
    """Entry-point contract mismatch is still enforced through the resolver."""
    root = tmp_path / "release"
    artifact = _copy_wheel_as(witness_wheel, root, witness_wheel.name)
    manifest = _make_manifest(artifact)
    wrong_registry = ComponentRegistry([
        ComponentDefinition(
            "zewitness",
            "ZeWitness",
            "zealfie-witness",
            (EntryPointContract("gui_scripts", "zesolver"),),
        )
    ])

    with pytest.raises(ReleaseResolutionError, match="launch contract"):
        resolve_local_release(
            manifest,
            registry=wrong_registry,
            artifact_root=root,
            host=HostTarget("py312", "cp312", "linux_x86_64"),
        )


# ===================================================================
# M0-7B Hardening — tag validation (fail-closed patterns)
# ===================================================================


def test_python_tag_single_letter_rejected():
    """python_tag='p' (prefix-match risk) → rejected at parse time."""
    with pytest.raises(ReleaseManifestError, match="python_tag.*not a recognised tag pattern"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            python_tag = "p"
            abi_tag = "none"
            platform_tag = "any"
        """))


def test_python_tag_empty_string_rejected():
    """python_tag='' → rejected at parse time."""
    with pytest.raises(ReleaseManifestError, match="abi_tag must not be empty"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            abi_tag = ""
        """))


def test_abi_tag_garbage_rejected():
    """abi_tag='totally_wrong' → rejected at parse time."""
    with pytest.raises(ReleaseManifestError, match="abi_tag.*not a recognised tag pattern"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            python_tag = "py312"
            abi_tag = "totally_wrong"
            platform_tag = "any"
        """))


def test_valid_tags_accepted():
    """Well-formed tags are accepted at parse time."""
    manifest = parse_release_manifest(textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py312"
        abi_tag = "cp312"
        platform_tag = "linux_x86_64"
    """))
    ae = manifest.artifacts[0]
    assert ae.python_tag == "py312"
    assert ae.abi_tag == "cp312"
    assert ae.platform_tag == "linux_x86_64"


def test_valid_tag_variants_accepted():
    """Various valid tag forms pass validation."""
    manifest = parse_release_manifest(textwrap.dedent("""\
        schema_version = 1
        component_id = "x"
        version = "1"

        [[artifacts]]
        filename = "f.whl"
        size = 0
        sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        python_tag = "py3"
        abi_tag = "none"
        platform_tag = "any"

        [[artifacts]]
        filename = "g.whl"
        size = 0
        sha256 = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        python_tag = "cp312"
        abi_tag = "abi3"
        platform_tag = "macosx_14_0_arm64"
    """))
    assert len(manifest.artifacts) == 2


def test_platform_tag_empty_rejected():
    """platform_tag with no valid chars → rejected."""
    with pytest.raises(ReleaseManifestError, match="platform_tag.*not a recognised tag pattern"):
        parse_release_manifest(textwrap.dedent("""\
            schema_version = 1
            component_id = "x"
            version = "1"

            [[artifacts]]
            filename = "f.whl"
            size = 0
            sha256 = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            python_tag = "py3"
            abi_tag = "none"
            platform_tag = "!!!"
        """))


def test_host_target_empty_python_tag_rejected():
    """HostTarget with empty python_tag → ValueError."""
    with pytest.raises(ValueError, match="HostTarget.python_tag must be a non-empty string"):
        HostTarget(python_tag="", abi_tag="cp312", platform_tag="linux_x86_64")


def test_host_target_empty_abi_tag_rejected():
    """HostTarget with empty abi_tag → ValueError."""
    with pytest.raises(ValueError, match="HostTarget.abi_tag must be a non-empty string"):
        HostTarget(python_tag="py312", abi_tag="", platform_tag="linux_x86_64")


def test_host_target_empty_platform_tag_rejected():
    """HostTarget with empty platform_tag → ValueError."""
    with pytest.raises(ValueError, match="HostTarget.platform_tag must be a non-empty string"):
        HostTarget(python_tag="py312", abi_tag="cp312", platform_tag="")


def test_host_target_whitespace_only_rejected():
    """HostTarget with whitespace-only field → ValueError."""
    with pytest.raises(ValueError, match="HostTarget.python_tag must be a non-empty string"):
        HostTarget(python_tag="   ", abi_tag="cp312", platform_tag="linux_x86_64")
