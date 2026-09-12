"""Hermetic tests for the ZeAlfie macOS ARM64 unsigned bundle (ZA-MAC-BOOT-01).

These tests run on ANY platform (Linux included) and cover, from
``packaging/macos/``:

* ``reproducibility.toml`` loading + anti-substitution validation, and the
  REAL committed pin (exact PBS filename/URL/size/SHA-256);
* archive digest verification (pass + fail closed);
* the mandatory bundle layout helpers and :func:`assert_bundle_layout`;
* the exact ``Info.plist`` contract;
* the POSIX launcher contract — both statically (content) and BEHAVIOURALLY,
  by running the real launcher against a stub bundle on Linux ``/bin/sh``
  (relocation, spaces in the path, hostile inherited Python environment);
* the Mach-O parser (thin/FAT/x86_64/arm64 classification), the recursive
  ARM64-only audit, and the lipo-thinning contract with an injected lipo;
* safe extraction of synthetic install_only tarballs (layout + arm64
  interpreter check, unsafe-member rejection);
* bundle zipping (top-level ``ZeAlfie.app``, exec bit preserved);
* the macOS wheelhouse lock (load + verify against synthetic content);
* the manual workflow's static contract.

No network, no macOS, no private Python, no venv, no real lipo.
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
import struct
import subprocess
import sys
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_MACOS_PKG = _REPO_ROOT / "packaging" / "macos"
_LAUNCHER = _MACOS_PKG / "launcher" / "ZeAlfie"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "macos-packaging-arm64.yml"

_ARM64 = 0x0100000C
_X86_64 = 0x01000007


def _load_module(filename: str, module_name: str):
    """Load a packaging/macos module by file path (unique module name)."""
    spec = importlib.util.spec_from_file_location(module_name, _MACOS_PKG / filename)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


macho = _load_module("macho.py", "zealfie_macos_macho_test")
wheelhouse = _load_module("wheelhouse.py", "zealfie_macos_wheelhouse_test")
# macpack imports the sibling `macho` by name; register it so the import works.
sys.modules.setdefault("macho", macho)
macpack = _load_module("macpack.py", "zealfie_macos_macpack_test")


# ---------------------------------------------------------------------------
# Synthetic Mach-O builders
# ---------------------------------------------------------------------------


def _thin_macho(cputype: int, subtype: int = 0, payload: bytes = b"") -> bytes:
    return b"\xcf\xfa\xed\xfe" + struct.pack("<ii", cputype, subtype) + payload


def _fat_macho(archs: list[tuple[int, int]], payloads: list[bytes] | None = None) -> bytes:
    payloads = payloads or [b"\x00" * 8 for _ in archs]
    header = b"\xca\xfe\xba\xbe" + struct.pack(">I", len(archs))
    offset = 8 + 20 * len(archs)
    entries = b""
    body = b""
    for (cputype, subtype), payload in zip(archs, payloads):
        entries += struct.pack(">iiIII", cputype, subtype, offset + len(body), len(payload), 2)
        body += payload
    return header + entries + body


def _arm64_thin_bytes() -> bytes:
    return _thin_macho(_ARM64)


# ---------------------------------------------------------------------------
# Reproducibility record
# ---------------------------------------------------------------------------


_EXPECTED_RECORD = {
    "cpython_version": "3.13.15",
    "substrate": "python-build-standalone",
    "upstream_repo": "astral-sh/python-build-standalone",
    "release_tag": "20260901",
    "target_triple": "aarch64-apple-darwin",
    "archive_filename": "cpython-3.13.15+20260901-aarch64-apple-darwin-install_only.tar.gz",
    "archive_url": (
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
        "20260901/cpython-3.13.15%2B20260901-aarch64-apple-darwin-install_only.tar.gz"
    ),
    "sha256": "b9054a9d3d54f4cb5573d44907fddb29874b08909bde73f29f2868cf872223ee",
    "size": 25293188,
}


def test_record_pins_real_pbs_asset() -> None:
    record = macpack.load_record()
    for field, expected in _EXPECTED_RECORD.items():
        assert getattr(record, field) == expected, field
    assert record.zealfie_version == "0.1.1"
    assert record.python_dir_name == "python"


def _record_toml() -> str:
    return (_MACOS_PKG / "reproducibility.toml").read_text(encoding="utf-8")


def test_record_is_real_committed_file() -> None:
    data = tomllib.loads(_record_toml())
    assert data["cpython"]["sha256"] == _EXPECTED_RECORD["sha256"]
    assert data["cpython"]["size"] == _EXPECTED_RECORD["size"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('target_triple = "aarch64-apple-darwin"', 'target_triple = "x86_64-apple-darwin"'),
        ('substrate = "python-build-standalone"', 'substrate = "python.org"'),
        (
            'upstream_repo = "astral-sh/python-build-standalone"',
            'upstream_repo = "someone-else/python"',
        ),
        ('release_tag = "20260901"', 'release_tag = "20250101"'),
        ('version = "3.13.15"', 'version = "3.12.0"'),
        (
            'archive_filename = "cpython-3.13.15+20260901-aarch64-apple-darwin-install_only.tar.gz"',
            'archive_filename = "cpython-3.13.15+20260901-aarch64-apple-darwin-install_only_stripped.tar.gz"',
        ),
        (
            'archive_filename = "cpython-3.13.15+20260901-aarch64-apple-darwin-install_only.tar.gz"',
            'archive_filename = "cpython-3.13.15+20260901-aarch64-apple-darwin-freethreaded-install_only.tar.gz"',
        ),
    ],
)
def test_record_rejects_substitution(tmp_path: Path, old: str, new: str) -> None:
    text = _record_toml()
    assert old in text
    mutated = tmp_path / "reproducibility.toml"
    mutated.write_text(text.replace(old, new), encoding="utf-8")
    with pytest.raises(macpack.RecordError):
        macpack.load_record(mutated)


def test_record_rejects_bad_sha_and_size(tmp_path: Path) -> None:
    text = _record_toml()
    bad_sha = tmp_path / "bad-sha.toml"
    bad_sha.write_text(
        text.replace(_EXPECTED_RECORD["sha256"], "zz" * 32), encoding="utf-8"
    )
    with pytest.raises(macpack.RecordError):
        macpack.load_record(bad_sha)

    zero_size = tmp_path / "zero-size.toml"
    zero_size.write_text(
        text.replace("size = 25293188", "size = 0"), encoding="utf-8"
    )
    with pytest.raises(macpack.RecordError):
        macpack.load_record(zero_size)


def test_record_rejects_url_drift(tmp_path: Path) -> None:
    text = _record_toml()
    mutated = tmp_path / "url.toml"
    mutated.write_text(
        text.replace(
            "releases/download/20260901/", "releases/download/20250101/"
        ),
        encoding="utf-8",
    )
    with pytest.raises(macpack.RecordError):
        macpack.load_record(mutated)


def test_sha256_verification_pass_and_fail(tmp_path: Path) -> None:
    blob = tmp_path / "archive.tar.gz"
    blob.write_bytes(b"not really a tarball")
    digest = macpack.sha256_file(blob)
    assert macpack.verify_archive_sha256(blob, expected_sha256=digest) == digest
    with pytest.raises(macpack.HashMismatchError):
        macpack.verify_archive_sha256(blob, expected_sha256="a" * 64)


# ---------------------------------------------------------------------------
# Bundle layout + plist
# ---------------------------------------------------------------------------


def _make_layout(app: Path) -> None:
    for directory in (
        macpack.bundle_macos_dir(app),
        macpack.bundle_resources_dir(app),
        macpack.bundle_python_dir(app) / "bin",
        macpack.bundle_app_dir(app) / "zealfie",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    macpack.bundle_plist(app).write_text(macpack.render_plist(), encoding="utf-8")
    macpack.bundle_launcher(app).write_text("#!/bin/sh\n", encoding="utf-8")
    macpack.bundle_icns(app).write_bytes(b"icns")
    (macpack.bundle_python_dir(app) / "bin" / "python3.13").write_bytes(
        _arm64_thin_bytes()
    )


def test_mandatory_layout_paths() -> None:
    app = Path("/tmp/x/ZeAlfie.app")
    assert macpack.bundle_plist(app).relative_to(app).as_posix() == "Contents/Info.plist"
    assert (
        macpack.bundle_launcher(app).relative_to(app).as_posix()
        == "Contents/MacOS/ZeAlfie"
    )
    assert (
        macpack.bundle_python_dir(app).relative_to(app).as_posix()
        == "Contents/Resources/python"
    )
    assert (
        macpack.bundle_app_dir(app).relative_to(app).as_posix()
        == "Contents/Resources/app"
    )
    assert (
        macpack.bundle_icns(app).relative_to(app).as_posix()
        == "Contents/Resources/zealfie.icns"
    )


def test_assert_bundle_layout_pass_and_fail(tmp_path: Path) -> None:
    app = tmp_path / "ZeAlfie.app"
    _make_layout(app)
    macpack.assert_bundle_layout(app)

    (macpack.bundle_icns(app)).unlink()
    with pytest.raises(macpack.BundleLayoutError):
        macpack.assert_bundle_layout(app)


def test_render_plist_exact_contract() -> None:
    text = macpack.render_plist()
    assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>')
    for key, value in macpack.PLIST_VALUES:
        assert f"<key>{key}</key>" in text
        if value is True:
            assert "<true/>" in text
        else:
            assert f"<string>{value}</string>" in text
    assert "<key>CFBundleIdentifier</key>\n  <string>com.zesoftware.zealfie</string>" in text
    assert "<key>CFBundleVersion</key>\n  <string>0.1.1</string>" in text
    assert "<key>LSMinimumSystemVersion</key>\n  <string>13.0</string>" in text
    assert "<key>NSHighResolutionCapable</key>\n  <true/>" in text
    assert "CFBundleIconFile" in text and "<string>zealfie.icns</string>" in text


def test_render_plist_rejects_key_drift() -> None:
    with pytest.raises(macpack.MacPackError):
        macpack.render_plist((("CFBundleIdentifier", "com.evil"),))


# ---------------------------------------------------------------------------
# Mach-O parser + audit + thinning
# ---------------------------------------------------------------------------


def test_macho_thin_classification(tmp_path: Path) -> None:
    arm = tmp_path / "arm"
    arm.write_bytes(_arm64_thin_bytes())
    info = macho.read_macho_info(arm)
    assert info.is_macho and info.is_thin and info.archs == ("arm64",)

    x86 = tmp_path / "x86"
    x86.write_bytes(_thin_macho(_X86_64))
    assert macho.read_macho_info(x86).archs == ("x86_64",)

    text = tmp_path / "readme.txt"
    text.write_text("hello", encoding="utf-8")
    assert not macho.read_macho_info(text).is_macho


def test_macho_fat_classification(tmp_path: Path) -> None:
    fat = tmp_path / "fat"
    fat.write_bytes(_fat_macho([(_ARM64, 0), (_X86_64, 3)]))
    info = macho.read_macho_info(fat)
    assert info.is_macho and info.is_fat
    assert info.archs == ("arm64", "x86_64")
    assert info.contains("arm64") and info.contains("x86_64")

    arm_only_fat = tmp_path / "fat-arm"
    arm_only_fat.write_bytes(_fat_macho([(_ARM64, 0), (_ARM64, 2)]))
    assert macho.read_macho_info(arm_only_fat).archs == ("arm64",)


def test_audit_tree_detects_residues(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "ok").write_bytes(_arm64_thin_bytes())
    (root / "lib" / "fat").write_bytes(_fat_macho([(_ARM64, 0), (_X86_64, 3)]))
    (root / "lib" / "intel").write_bytes(_thin_macho(_X86_64))
    (root / "lib" / "plain.txt").write_text("data", encoding="utf-8")

    audit = macho.audit_tree(root)
    assert audit["files_checked"] == 4
    assert len(audit["macho_files"]) == 3
    assert audit["X86_64_RESIDUES"] == 2
    assert audit["ARM64_ONLY"] == "FAIL"
    assert "lib/intel" in audit["x86_64_files"]

    clean = tmp_path / "clean"
    (clean / "a").mkdir(parents=True)
    (clean / "a" / "ok").write_bytes(_arm64_thin_bytes())
    audit_clean = macho.audit_tree(clean)
    assert audit_clean["ARM64_ONLY"] == "PASS"
    assert audit_clean["X86_64_RESIDUES"] == 0


def test_thin_to_arm64_thins_fat_and_preserves_mode(tmp_path: Path) -> None:
    target = tmp_path / "universal"
    target.write_bytes(_fat_macho([(_ARM64, 0), (_X86_64, 3)]))
    target.chmod(0o755)

    def fake_lipo(source: Path, destination: Path, arch: str) -> None:
        assert arch == "arm64"
        destination.write_bytes(_arm64_thin_bytes())

    assert macho.thin_to_arm64(target, _run_lipo=fake_lipo) == "thinned"
    info = macho.read_macho_info(target)
    assert info.is_thin and info.archs == ("arm64",)
    assert target.stat().st_mode & 0o777 == 0o755
    # No temporary files left behind.
    assert sorted(p.name for p in tmp_path.iterdir()) == ["universal"]


def test_thin_to_arm64_unchanged_and_skipped(tmp_path: Path) -> None:
    thin = tmp_path / "arm"
    thin.write_bytes(_arm64_thin_bytes())
    assert macho.thin_to_arm64(thin) == "unchanged"
    text = tmp_path / "notes.md"
    text.write_text("hi", encoding="utf-8")
    assert macho.thin_to_arm64(text) == "skipped"


def test_thin_to_arm64_fails_closed(tmp_path: Path) -> None:
    intel = tmp_path / "intel"
    intel.write_bytes(_thin_macho(_X86_64))
    with pytest.raises(macho.ThinningError):
        macho.thin_to_arm64(intel)

    no_arm = tmp_path / "no-arm"
    no_arm.write_bytes(_fat_macho([(_X86_64, 3), (_X86_64, 3)]))
    with pytest.raises(macho.ThinningError):
        macho.thin_to_arm64(no_arm)

    universal = tmp_path / "universal"
    universal.write_bytes(_fat_macho([(_ARM64, 0), (_X86_64, 3)]))

    def failing_lipo(source: Path, destination: Path, arch: str) -> None:
        raise macho.ThinningError("lipo exploded")

    with pytest.raises(macho.ThinningError):
        macho.thin_to_arm64(universal, _run_lipo=failing_lipo)

    def bad_result(source: Path, destination: Path, arch: str) -> None:
        destination.write_bytes(_thin_macho(_X86_64))

    with pytest.raises(macho.ThinningError):
        macho.thin_to_arm64(universal, _run_lipo=bad_result)


def test_thin_tree_to_arm64(tmp_path: Path) -> None:
    root = tmp_path / "tree"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a").write_bytes(_fat_macho([(_ARM64, 0), (_X86_64, 3)]))
    (root / "pkg" / "b").write_bytes(_arm64_thin_bytes())
    (root / "pkg" / "c.txt").write_text("x", encoding="utf-8")

    def fake_lipo(source: Path, destination: Path, arch: str) -> None:
        destination.write_bytes(_arm64_thin_bytes())

    summary = macho.thin_tree_to_arm64(root, _run_lipo=fake_lipo)
    assert len(summary["files_thinned"]) == 1
    assert len(summary["files_unchanged"]) == 1
    assert macho.audit_tree(root)["ARM64_ONLY"] == "PASS"


# ---------------------------------------------------------------------------
# Extraction of synthetic install_only tarballs
# ---------------------------------------------------------------------------


def _write_tarball(path: Path, members: dict[str, tuple[bytes, int]]) -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, (payload, mode) in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = mode
            info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(payload))


def test_extract_python_tarball_pass(tmp_path: Path) -> None:
    archive = tmp_path / "pbs.tar.gz"
    _write_tarball(
        archive,
        {
            "python/bin/python3.13": (_arm64_thin_bytes(), 0o755),
            "python/lib/python3.13/os.py": (b"# stdlib", 0o644),
        },
    )
    resources = tmp_path / "Resources"
    python_dir = macpack.extract_python_tarball(archive, resources)
    assert python_dir == resources / "python"
    assert (python_dir / "bin" / "python3.13").is_file()


def test_extract_python_tarball_rejects_intel_interpreter(tmp_path: Path) -> None:
    archive = tmp_path / "pbs.tar.gz"
    _write_tarball(
        archive, {"python/bin/python3.13": (_thin_macho(_X86_64), 0o755)}
    )
    with pytest.raises(macpack.ExtractionError):
        macpack.extract_python_tarball(archive, tmp_path / "Resources")


def test_extract_python_tarball_rejects_universal_interpreter(tmp_path: Path) -> None:
    archive = tmp_path / "pbs.tar.gz"
    _write_tarball(
        archive,
        {
            "python/bin/python3.13": (
                _fat_macho([(_ARM64, 0), (_X86_64, 3)]),
                0o755,
            )
        },
    )
    with pytest.raises(macpack.ExtractionError):
        macpack.extract_python_tarball(archive, tmp_path / "Resources")


def test_extract_python_tarball_rejects_missing_interpreter(tmp_path: Path) -> None:
    archive = tmp_path / "pbs.tar.gz"
    _write_tarball(archive, {"python/lib/readme": (b"x", 0o644)})
    with pytest.raises(macpack.ExtractionError):
        macpack.extract_python_tarball(archive, tmp_path / "Resources")


def test_extract_python_tarball_rejects_escape_member(tmp_path: Path) -> None:
    archive = tmp_path / "evil.tar.gz"
    _write_tarball(
        archive,
        {
            "python/bin/python3.13": (_arm64_thin_bytes(), 0o755),
            "../evil.txt": (b"pwned", 0o644),
        },
    )
    with pytest.raises(macpack.ExtractionError):
        macpack.extract_python_tarball(archive, tmp_path / "Resources")
    assert not (tmp_path / "evil.txt").exists()


# ---------------------------------------------------------------------------
# Artifact zip
# ---------------------------------------------------------------------------


def test_zip_app_top_level_and_exec_bit(tmp_path: Path) -> None:
    app = tmp_path / "ZeAlfie.app"
    _make_layout(app)
    macpack.bundle_launcher(app).chmod(0o755)

    out = tmp_path / "ZeAlfie-macOS-arm64-unsigned.zip"
    summary = macpack.zip_app(app, out)
    assert summary["name"] == "ZeAlfie-macOS-arm64-unsigned.zip"
    assert summary["size"] > 0 and len(summary["sha256"]) == 64
    with zipfile.ZipFile(out) as zf:
        names = zf.namelist()
    assert "ZeAlfie.app/Contents/Info.plist" in names
    assert all(name.startswith("ZeAlfie.app/") for name in names)


def test_zip_app_rejects_wrong_bundle_name(tmp_path: Path) -> None:
    app = tmp_path / "NotZeAlfie.app"
    app.mkdir()
    with pytest.raises(macpack.MacPackError):
        macpack.zip_app(app, tmp_path / "o.zip")


# ---------------------------------------------------------------------------
# macOS wheelhouse lock
# ---------------------------------------------------------------------------


def test_macos_wheelhouse_lock_real_closure() -> None:
    lock = wheelhouse.load_lock()
    assert lock.platform_tag == "macosx_13_0_universal2"
    assert lock.python_tag == "cp313" and lock.abi_tag == "cp313"
    assert lock.cpython_version == "3.13.15"
    names = {entry.name for entry in lock.wheels}
    assert {
        "PySide6",
        "PySide6-Essentials",
        "PySide6-Addons",
        "shiboken6",
        "packaging",
        "build",
        "pyproject-hooks",
        "setuptools",
        "wheel",
    } <= names
    for required in ("PySide6", "PySide6-Essentials", "PySide6-Addons", "shiboken6"):
        assert required in names
    assert lock.zealfie_wheel.filename == "zealfie-0.1.1-py3-none-any.whl"
    assert lock.zealfie_wheel.sha256 is None
    assert wheelhouse.expected_filenames(lock) == (
        wheelhouse.pinned_filenames(lock) | {"zealfie-0.1.1-py3-none-any.whl"}
    )
    assert "PySide6==6.11.2" in wheelhouse.pinned_download_specs(lock)


def test_macos_wheelhouse_verify_fail_closed(tmp_path: Path) -> None:
    lock = wheelhouse.load_lock()
    wh = tmp_path / "wheelhouse"
    wh.mkdir()
    # Wrong SHA-256 for the first pinned wheel.
    entry = lock.wheels[0]
    blob = wh / entry.filename
    blob.write_bytes(b"corruption")
    with pytest.raises(wheelhouse.WheelhouseLockError):
        wheelhouse.verify_pinned_subset(wh, lock)

    # Extra/missing wheels are rejected before hashing.
    with pytest.raises(wheelhouse.WheelhouseVerificationError):
        wheelhouse.verify_pinned_subset(
            wh, lock, _list_wheels=lambda _d: []
        )


def test_macos_lock_rejects_tag_drift(tmp_path: Path) -> None:
    text = (_MACOS_PKG / "wheelhouse.lock.toml").read_text(encoding="utf-8")
    mutated = tmp_path / "lock.toml"
    mutated.write_text(
        text.replace('platform_tag = "macosx_13_0_universal2"', 'platform_tag = "win_amd64"'),
        encoding="utf-8",
    )
    with pytest.raises(wheelhouse.LockError):
        wheelhouse.load_lock(mutated)


def test_parse_wheel_filename() -> None:
    assert wheelhouse.parse_wheel_filename(
        "pyside6_addons-6.11.2-cp310-abi3-macosx_13_0_universal2.whl"
    ) == ("pyside6_addons", "6.11.2")
    assert wheelhouse.parse_wheel_filename(
        "build-1.6.1-py3-none-any.whl"
    ) == ("build", "1.6.1")


# ---------------------------------------------------------------------------
# Launcher — static contract
# ---------------------------------------------------------------------------


def test_launcher_is_posix_script_with_bundled_python() -> None:
    text = _LAUNCHER.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert '"$RESOURCES_DIR/python/bin/python3.13"' in text
    for var in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV"):
        assert f"unset {var}" in text
    assert "PYTHONNOUSERSITE=1" in text
    assert "export PYTHONNOUSERSITE" in text
    assert '"$RESOURCES_DIR/app"' in text
    assert 'exec "$PYTHON"' in text
    # No PATH resolution of an interpreter, ever.
    for forbidden in ("command -v python", "/usr/bin/env python", "python3 -m pip"):
        assert forbidden not in text
    assert "zealfie.gui" in text


def test_launcher_avoids_external_path_helpers() -> None:
    text = _LAUNCHER.read_text(encoding="utf-8")
    # The launcher may MENTION these tools in comments, but must never call
    # them: paths are derived with shell parameter expansion only.
    assert "$(dirname" not in text
    assert "$(basename" not in text
    assert "dirname \"" not in text
    assert "basename \"" not in text


# ---------------------------------------------------------------------------
# Launcher — BEHAVIOURAL contract (runs the real script on Linux /bin/sh)
# ---------------------------------------------------------------------------


def _stub_app(root: Path) -> Path:
    """A stub bundle whose `python3.13` is an executable shell script."""
    app = root / "ZeAlfie.app"
    (app / "Contents" / "MacOS").mkdir(parents=True)
    (app / "Contents" / "Resources" / "app").mkdir(parents=True)
    (app / "Contents" / "Resources" / "python" / "bin").mkdir(parents=True)
    launcher = app / "Contents" / "MacOS" / "ZeAlfie"
    launcher.write_bytes(_LAUNCHER.read_bytes())
    launcher.chmod(0o755)
    stub = app / "Contents" / "Resources" / "python" / "bin" / "python3.13"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "STUB=$0"\n'
        'echo "STUB_ARGS=$*"\n'
        'echo "PYTHONPATH=${PYTHONPATH:-<unset>}"\n'
        'echo "PYTHONHOME=${PYTHONHOME:-<unset>}"\n'
        'echo "VIRTUAL_ENV=${VIRTUAL_ENV:-<unset>}"\n'
        'echo "PYTHONNOUSERSITE=${PYTHONNOUSERSITE:-<unset>}"\n',
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return app


def _run_launcher(app: Path, cwd: str = "/tmp") -> subprocess.CompletedProcess:
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(Path.home()),
        "PYTHONHOME": "/hostile/python-home",
        "PYTHONPATH": "/hostile/pythonpath",
        "VIRTUAL_ENV": "/hostile/venv",
    }
    return subprocess.run(
        [str(app / "Contents" / "MacOS" / "ZeAlfie")],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_launcher_behaviour_uses_bundled_python(tmp_path: Path) -> None:
    app = _stub_app(tmp_path)
    proc = _run_launcher(app)
    assert proc.returncode == 0, proc.stderr
    stub = app / "Contents" / "Resources" / "python" / "bin" / "python3.13"
    assert f"STUB={stub}" in proc.stdout
    assert "PYTHONPATH=" + str(app / "Contents" / "Resources" / "app") in proc.stdout
    assert "PYTHONHOME=<unset>" in proc.stdout
    assert "VIRTUAL_ENV=<unset>" in proc.stdout
    assert "PYTHONNOUSERSITE=1" in proc.stdout
    # The GUI entry point is invoked with -s and the -c bootstrap.
    assert "STUB_ARGS=-s -c" in proc.stdout
    assert "zealfie.gui" in proc.stdout


def test_launcher_behaviour_after_relocation_and_with_spaces(tmp_path: Path) -> None:
    original = _stub_app(tmp_path / "build dir")
    relocated_parent = tmp_path / "relocated dir"
    relocated_parent.mkdir()
    relocated = relocated_parent / "ZeAlfie.app"
    shutil.copytree(original, relocated, symlinks=True)

    proc = _run_launcher(relocated)
    assert proc.returncode == 0, proc.stderr
    stub = relocated / "Contents" / "Resources" / "python" / "bin" / "python3.13"
    assert f"STUB={stub}" in proc.stdout
    assert "PYTHONPATH=" + str(relocated / "Contents" / "Resources" / "app") in proc.stdout
    assert "PYTHONPATH=/hostile" not in proc.stdout


def test_launcher_fails_closed_without_interpreter(tmp_path: Path) -> None:
    app = _stub_app(tmp_path)
    stub = app / "Contents" / "Resources" / "python" / "bin" / "python3.13"
    stub.rename(stub.with_name("python3.13.disabled"))

    # A host `python3.13` on PATH must never be used as a fallback.
    host_bin = tmp_path / "host-bin"
    host_bin.mkdir()
    marker = host_bin / "HOST_PYTHON_USED"
    fake = host_bin / "python3.13"
    fake.write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)

    env = {
        "PATH": f"{host_bin}:/usr/bin:/bin",
        "HOME": str(Path.home()),
    }
    proc = subprocess.run(
        [str(app / "Contents" / "MacOS" / "ZeAlfie")],
        cwd="/tmp",
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode != 0
    assert "bundled interpreter missing" in (proc.stderr + proc.stdout)
    assert not marker.exists()


def test_launcher_follows_symlinked_launcher(tmp_path: Path) -> None:
    app = _stub_app(tmp_path)
    linked_dir = tmp_path / "bin"
    linked_dir.mkdir()
    link = linked_dir / "ZeAlfie"
    os.symlink(app / "Contents" / "MacOS" / "ZeAlfie", link)

    env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home())}
    proc = subprocess.run(
        [str(link)], cwd="/tmp", env=env, capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    stub = app / "Contents" / "Resources" / "python" / "bin" / "python3.13"
    assert f"STUB={stub}" in proc.stdout


# ---------------------------------------------------------------------------
# Packaging isolation rules
# ---------------------------------------------------------------------------


def test_packaging_modules_do_not_import_zealfie() -> None:
    for name in ("macpack.py", "macho.py", "wheelhouse.py", "acquire_wheelhouse.py", "build_app.py"):
        text = (_MACOS_PKG / name).read_text(encoding="utf-8")
        assert "import zealfie" not in text, name
        assert "from zealfie" not in text, name


def test_build_app_refuses_on_non_darwin() -> None:
    if sys.platform == "darwin":  # pragma: no cover - runner is macOS
        pytest.skip("build_app is allowed on macOS")
    proc = subprocess.run(
        [
            sys.executable,
            str(_MACOS_PKG / "build_app.py"),
            "--work", "/tmp/unused-work",
            "--wheelhouse", "/tmp/unused-wheelhouse",
            "--zealfie-wheel", "/tmp/unused.whl",
            "--out-zip", "/tmp/unused.zip",
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 2
    assert "must run on macOS" in proc.stderr


def test_windows_packaging_untouched() -> None:
    """The mission must not alter the Windows packaging surface."""
    proc = subprocess.run(
        ["git", "status", "--porcelain", "--", "packaging/windows"],
        cwd=_REPO_ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0
    assert proc.stdout.strip() == ""


# ---------------------------------------------------------------------------
# Workflow static contract
# ---------------------------------------------------------------------------


def test_workflow_static_contract() -> None:
    text = _WORKFLOW.read_text(encoding="utf-8")
    assert "workflow_dispatch:" in text
    assert "push:" not in text and "pull_request:" not in text and "schedule:" not in text
    assert 'runs-on: "macos-15"' in text
    assert "continue-on-error:" not in text
    assert "ZeAlfie-macOS-arm64-unsigned.zip" in text
    assert "acquire_wheelhouse.py" in text
    assert "build_app.py" in text
    assert "witnesses.py all" in text
    assert "actions/upload-artifact@" in text
    assert "ARM64_ONLY=PASS" in text
    assert "X86_64_RESIDUES=0" in text
    # No release/publish surface.
    for forbidden in ("softprops/action-gh-release", "gh release", "npm publish"):
        assert forbidden not in text


def test_workflow_parses_and_has_expected_shape() -> None:
    yaml = pytest.importorskip("yaml")
    data = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    assert data["name"] == "macOS ARM64 packaging (unsigned)"
    # PyYAML (YAML 1.1) parses the bare key ``on`` as the boolean True.
    triggers = data.get("on", data.get(True))
    assert set(triggers.keys()) == {"workflow_dispatch"}
    job = data["jobs"]["macos-packaging-arm64"]
    assert job["runs-on"] == "macos-15"
    step_ids = [step.get("id") for step in job["steps"] if isinstance(step, dict)]
    for required in (
        "arch-check", "checkout", "setup-python", "build-wheel",
        "acquire-wheelhouse", "build-app", "macho-audit", "witnesses",
        "artifact", "upload",
    ):
        assert required in step_ids, required
    for step in job["steps"]:
        assert "continue-on-error" not in step
