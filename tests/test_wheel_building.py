"""Tests for wheel building and inspection."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

pytestmark = pytest.mark.zealfie_slow

from zealfie.building import (
    InspectedWheel,
    build_wheel,
    inspect_wheel,
)


def _outdir_from_cmd(cmd: list[str]) -> str:
    """Return the ``--outdir`` argument from a ``python -m build`` command."""
    idx = cmd.index("--outdir")
    return cmd[idx + 1]


# ---------------------------------------------------------------------------
# Wheel building
# ---------------------------------------------------------------------------


def test_build_zealfie_wheel_from_project_root(tmp_path: Path) -> None:
    """Building from the project root produces a wheel file."""
    project_root = Path(__file__).resolve().parents[1]
    wheel = build_wheel(project_root, output_dir=tmp_path)

    assert wheel.is_file()
    assert wheel.suffix == ".whl"
    assert "zealfie" in wheel.name


def test_build_witness_wheel(tmp_path: Path) -> None:
    """Building the witness fixture produces a wheel file."""
    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    wheel = build_wheel(witness_dir, output_dir=tmp_path)

    assert wheel.is_file()
    assert wheel.suffix == ".whl"
    assert "zealfie-witness" in wheel.name or "zealfie_witness" in wheel.name


def test_build_nonexistent_source_raises() -> None:
    with pytest.raises(FileNotFoundError):
        build_wheel("/nonexistent/path/for/sure")


# ---------------------------------------------------------------------------
# Wheel inspection – ZeAlfie
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def zealfie_wheel(tmp_path_factory) -> Path:
    project_root = Path(__file__).resolve().parents[1]
    tmp = tmp_path_factory.mktemp("zealfie-wheel")
    return build_wheel(project_root, output_dir=tmp)


def test_zealfie_wheel_contains_package(zealfie_wheel: Path) -> None:
    info = inspect_wheel(zealfie_wheel)
    assert "zealfie" in info.top_level_packages


def test_zealfie_wheel_contains_dist_info(zealfie_wheel: Path) -> None:
    info = inspect_wheel(zealfie_wheel)
    assert info.dist_info_dir is not None
    assert "zealfie" in info.dist_info_dir


def test_zealfie_wheel_version_is_0_0_5(zealfie_wheel: Path) -> None:
    info = inspect_wheel(zealfie_wheel)
    assert info.version == "0.0.6"


def test_zealfie_wheel_has_cli_entry_point(zealfie_wheel: Path) -> None:
    info = inspect_wheel(zealfie_wheel)
    console = [e for e in info.entry_points if e.group == "console_scripts"]
    names = {e.name for e in console}
    assert "zealfie" in names


def test_zealfie_wheel_manifest_included(zealfie_wheel: Path) -> None:
    """The packaged manifest TOML must be present inside the wheel."""
    import zipfile

    with zipfile.ZipFile(zealfie_wheel, "r") as zf:
        names = zf.namelist()
    manifest_candidates = [n for n in names if "manifests" in n and n.endswith(".toml")]
    assert len(manifest_candidates) >= 1, f"no manifest TOML found in {names}"


# ---------------------------------------------------------------------------
# Wheel inspection – witness
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def witness_wheel(tmp_path_factory) -> Path:
    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    tmp = tmp_path_factory.mktemp("witness-wheel")
    return build_wheel(witness_dir, output_dir=tmp)


def test_witness_wheel_contains_package(witness_wheel: Path) -> None:
    info = inspect_wheel(witness_wheel)
    assert "zewitness" in info.top_level_packages


def test_witness_wheel_contains_dist_info(witness_wheel: Path) -> None:
    info = inspect_wheel(witness_wheel)
    assert info.dist_info_dir is not None
    assert "zealfie-witness" in info.dist_info_dir or "zealfie_witness" in info.dist_info_dir


def test_witness_wheel_version_is_0_0_1(witness_wheel: Path) -> None:
    info = inspect_wheel(witness_wheel)
    assert info.version == "0.0.1"


def test_witness_wheel_has_entry_points_file_present(witness_wheel: Path) -> None:
    """The wheel must contain an entry_points.txt in its .dist-info."""
    import zipfile

    with zipfile.ZipFile(witness_wheel, "r") as zf:
        names = zf.namelist()
    ep_files = [n for n in names if n.endswith("entry_points.txt")]
    assert ep_files, f"no entry_points.txt found in wheel: {names}"


def test_witness_wheel_entry_point_group_is_console_scripts(witness_wheel: Path) -> None:
    info = inspect_wheel(witness_wheel)
    assert any(e.group == "console_scripts" for e in info.entry_points)


def test_witness_wheel_entry_point_name_is_zewitness(witness_wheel: Path) -> None:
    info = inspect_wheel(witness_wheel)
    assert any(e.name == "zewitness" for e in info.entry_points)


def test_witness_wheel_entry_point_target_correct(witness_wheel: Path) -> None:
    info = inspect_wheel(witness_wheel)
    console = [e for e in info.entry_points if e.group == "console_scripts" and e.name == "zewitness"]
    assert len(console) == 1
    assert console[0].value == "zewitness.__main__:main"


def test_witness_wheel_no_zesolver(witness_wheel: Path) -> None:
    """The witness wheel must not reference ZeSolver."""
    info = inspect_wheel(witness_wheel)
    assert "zesolver" not in info.top_level_packages
    assert all("zesolver" not in e.name and "zesolver" not in e.group
               for e in info.entry_points)


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_inspect_nonexistent_wheel_raises() -> None:
    with pytest.raises(FileNotFoundError):
        inspect_wheel("/nonexistent/for/sure.whl")


def test_inspect_empty_wheel_is_inspected_gracefully(tmp_path: Path) -> None:
    """A zero-byte file is not a valid ZIP but should not crash the process."""
    fake = tmp_path / "empty.whl"
    fake.write_text("")
    with pytest.raises(Exception):
        inspect_wheel(fake)


# ---------------------------------------------------------------------------
# Hardening: offline build (--no-isolation, PIP_NO_INDEX)
# ---------------------------------------------------------------------------


def test_build_passes_no_isolation_flag(monkeypatch, tmp_path: Path) -> None:
    """The build subprocess must pass --no-isolation."""
    import subprocess as sp_mod

    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        # Pretend a single wheel was produced in the private outdir.
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "fake-0.0.1-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    build_wheel(witness_dir, output_dir=tmp_path)

    cmd_flat = " ".join(calls[0])
    assert "--no-isolation" in cmd_flat, f"expected --no-isolation in: {cmd_flat}"


def test_build_sets_pip_no_index_env(monkeypatch, tmp_path: Path) -> None:
    """The build subprocess must set PIP_NO_INDEX=1 in the environment."""
    import subprocess as sp_mod

    captured_env: dict[str, str] = {}

    def fake_run(cmd, **kwargs):
        nonlocal captured_env
        captured_env = kwargs.get("env", {})
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "fake-0.0.1-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    build_wheel(witness_dir, output_dir=tmp_path)

    assert captured_env.get("PIP_NO_INDEX") == "1", f"PIP_NO_INDEX not set; env keys: {sorted(captured_env.keys())}"
    assert captured_env.get("PIP_INDEX_URL") == "", f"PIP_INDEX_URL not set to empty"


def test_build_with_no_isolation_still_succeeds(tmp_path: Path) -> None:
    """Build with --no-isolation must succeed using locally installed tools."""
    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    wheel = build_wheel(witness_dir, output_dir=tmp_path)
    assert wheel.is_file()
    assert wheel.suffix == ".whl"


# ---------------------------------------------------------------------------
# Hardening: deterministic wheel selection
# ---------------------------------------------------------------------------


def test_build_exactly_one_wheel_succeeds(tmp_path: Path) -> None:
    """Normal build produces exactly one wheel and returns it."""
    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    wheel = build_wheel(witness_dir, output_dir=tmp_path)
    assert wheel.is_file()
    # The output directory should contain exactly one .whl file.
    wheels = sorted(tmp_path.glob("*.whl"))
    assert len(wheels) == 1, f"expected 1 wheel, got {len(wheels)}: {wheels}"


def test_build_zero_wheels_raises(monkeypatch, tmp_path: Path) -> None:
    """If the build produces no wheel file, an error is raised."""
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        # Simulate build success but produce no wheel file.
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    with pytest.raises(RuntimeError, match="no wheel produced"):
        build_wheel(witness_dir, output_dir=tmp_path)


def test_build_multiple_wheels_raises(monkeypatch, tmp_path: Path) -> None:
    """If the build produces multiple wheel files, an error is raised."""
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        # Simulate build success but multiple wheels appear in the
        # private outdir (discovered from the command, not tmp_path).
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "a-1.0.0-py3-none-any.whl").write_text("")
        (Path(outdir) / "b-2.0.0-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    with pytest.raises(RuntimeError, match="ambiguous build"):
        build_wheel(witness_dir, output_dir=tmp_path)


# ---------------------------------------------------------------------------
# Hardening: CWD shadowing (local build/ directory must not mask PyPA build)
# ---------------------------------------------------------------------------


def test_build_wheel_uses_neutral_cwd(monkeypatch, tmp_path: Path) -> None:
    """build_wheel sets cwd to a private child of the output directory.

    The cwd must not be the repo root (a local ``build/`` directory
    there would mask the PyPA ``build`` package).  It must also not be
    the output directory itself, because stale wheels in a persistent
    output dir would be discovered as current-build outputs.  The cwd
    is a unique private child under the output dir, and ``--outdir``
    points at that same child.
    """
    import subprocess as sp_mod

    capture: dict = {}

    def fake_run(cmd, **kwargs):
        capture["cwd"] = kwargs.get("cwd")
        capture["cmd"] = cmd
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "fake-0.0.1-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    build_wheel(witness_dir, output_dir=tmp_path)

    # The cwd must be a private child of the output directory.
    cwd = Path(capture["cwd"])
    assert cwd.parent.resolve() == tmp_path.resolve(), (
        f"expected cwd to be a child of {tmp_path}, got {cwd}"
    )
    assert cwd.name.startswith("zealfie-build-"), (
        f"expected a private zealfie-build-* child, got {cwd.name}"
    )
    # --outdir must point at that same private child.
    assert _outdir_from_cmd(capture["cmd"]) == str(cwd), (
        f"expected --outdir to match cwd {cwd}, got "
        f"{_outdir_from_cmd(capture['cmd'])!r}"
    )


def test_build_wheel_tempdir_uses_neutral_cwd(monkeypatch, tmp_path: Path) -> None:
    """When no output_dir is given, cwd is set to the temp directory."""
    import subprocess as sp_mod
    import tempfile

    capture: dict = {}

    def fake_run(cmd, **kwargs):
        capture["cwd"] = kwargs.get("cwd")
        # Produce a wheel in the temp cwd.
        cwd = Path(capture["cwd"])
        (cwd / "fake-0.0.1-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    # Prevent mkdtemp from creating real dirs; we just want to check cwd.
    fake_tmp = tmp_path / "zealfie-build-faketmp"
    fake_tmp.mkdir()

    monkeypatch.setattr(sp_mod, "run", fake_run)
    monkeypatch.setattr(tempfile, "mkdtemp", lambda prefix: str(fake_tmp))

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    build_wheel(witness_dir)

    assert capture["cwd"] == str(fake_tmp), (
        f"expected cwd={fake_tmp}, got cwd={capture.get('cwd')}; "
        f"a repo-local build/ directory would mask PyPA build"
    )


# ---------------------------------------------------------------------------
# M0-7B Hardening — relative source_dir survives neutral CWD
# ---------------------------------------------------------------------------


def test_build_wheel_with_relative_source_from_repo_root(tmp_path, monkeypatch):
    """Relative source_dir must work even though cwd is the output dir.

    The hardening of build_wheel() sets cwd to the output directory to
    prevent a local build/ from masking the PyPA build package.  But if
    source_dir is relative, python -m build would resolve it from out/,
    not from the repo root.  The fix resolves source_dir to an absolute
    path before changing cwd.
    """
    import subprocess as sp_mod

    # We use monkeypatch to verify the source path passed to subprocess
    # is absolute, and the build succeeds.
    captured_source: str = ""
    captured_cwd: str = ""

    def fake_run(cmd, **kwargs):
        nonlocal captured_source, captured_cwd
        captured_cwd = kwargs.get("cwd", "")
        # source_dir is the positional arg after --wheel in the cmd list
        try:
            wheel_idx = cmd.index("build")
            captured_source = str(cmd[wheel_idx + 1])
        except (ValueError, IndexError):
            pass
        # Produce a fake wheel in the cwd
        cwd = Path(captured_cwd)
        (cwd / "fake-0.0.1-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    # Call with a relative path from the project root.
    project_root = Path(__file__).resolve().parents[1]
    import os
    old_cwd = os.getcwd()
    try:
        os.chdir(project_root)
        result = build_wheel("tests/fixtures/witness_component", output_dir=tmp_path)
    finally:
        os.chdir(old_cwd)

    # The source passed to subprocess must be an absolute path.
    assert Path(captured_source).is_absolute(), (
        f"source_dir must be absolute after resolve, "
        f"got relative: {captured_source!r}"
    )

    # The cwd must still be a neutral private child of the output dir.
    cwd = Path(captured_cwd)
    assert cwd.parent.resolve() == tmp_path.resolve(), (
        f"expected cwd to be a child of {tmp_path}, got {cwd}"
    )
    assert cwd.name.startswith("zealfie-build-"), (
        f"expected a private zealfie-build-* child, got {cwd.name}"
    )

    assert result.is_file()


def test_build_wheel_with_relative_output_dir_from_repo_root(tmp_path, monkeypatch):
    """Relative output_dir is resolved once, so build does not nest out/out."""
    import shutil

    fixture_src = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    fixture_dst = tmp_path / "tests" / "fixtures" / "witness_component"
    fixture_dst.parent.mkdir(parents=True)
    shutil.copytree(fixture_src, fixture_dst)

    monkeypatch.chdir(tmp_path)
    output = Path("relative-output")
    assert not output.exists()

    wheel = build_wheel("tests/fixtures/witness_component", output_dir=output)

    expected_output = tmp_path / "relative-output"
    assert wheel.is_file()
    assert wheel.parent == expected_output
    assert not (expected_output / "relative-output").exists()
    assert len(list(expected_output.glob("*.whl"))) == 1


# ---------------------------------------------------------------------------
# M1-2E Hardening — per-invocation output isolation (stale wheel immunity)
# ---------------------------------------------------------------------------


def test_stale_previous_version_not_ambiguous(monkeypatch, tmp_path: Path) -> None:
    """A stale previous version in a persistent output_dir is not counted."""
    import subprocess as sp_mod

    stale = tmp_path / "product-1.0.0.whl"
    stale.write_bytes(b"old-1.0.0")

    def fake_run(cmd, **kwargs):
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "product-1.0.1.whl").write_bytes(b"new-1.0.1")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    wheel = build_wheel(witness_dir, output_dir=tmp_path)

    assert wheel == tmp_path / "product-1.0.1.whl"
    assert wheel.read_bytes() == b"new-1.0.1"
    assert stale.read_bytes() == b"old-1.0.0"
    # The private build child must have been cleaned up.
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("zealfie-build-")]


def test_unrelated_product_wheel_preserved(monkeypatch, tmp_path: Path) -> None:
    """An unrelated product's wheel in output_dir is left untouched."""
    import subprocess as sp_mod

    unrelated = tmp_path / "zemosaic-0.9.0.whl"
    unrelated.write_bytes(b"zemosaic")

    def fake_run(cmd, **kwargs):
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "zesolver-1.0.0-py3-none-any.whl").write_bytes(b"zesolver")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    wheel = build_wheel(witness_dir, output_dir=tmp_path)

    assert wheel.name == "zesolver-1.0.0-py3-none-any.whl"
    assert unrelated.read_bytes() == b"zemosaic"


def test_same_filename_replaced_with_current_content(
    monkeypatch, tmp_path: Path,
) -> None:
    """A same-name artifact is replaced only after the current build validates."""
    import subprocess as sp_mod

    existing = tmp_path / "zesolver-1.0.1.whl"
    existing.write_bytes(b"OLD-CONTENT")

    def fake_run(cmd, **kwargs):
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "zesolver-1.0.1.whl").write_bytes(b"NEW-CONTENT")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    wheel = build_wheel(witness_dir, output_dir=tmp_path)

    # Returned artifact carries current content (not inferred from mtime).
    assert wheel == tmp_path / "zesolver-1.0.1.whl"
    assert wheel.read_bytes() == b"NEW-CONTENT"


def test_multiple_current_wheels_error_ignores_stale(
    monkeypatch, tmp_path: Path,
) -> None:
    """True multiple current-build wheels error; stale wheels outside the
    private build dir must not be counted."""
    import subprocess as sp_mod

    stale = tmp_path / "stale-0.0.1.whl"
    stale.write_bytes(b"stale")

    def fake_run(cmd, **kwargs):
        outdir = _outdir_from_cmd(cmd)
        (Path(outdir) / "a-1.0.0-py3-none-any.whl").write_text("")
        (Path(outdir) / "b-2.0.0-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    with pytest.raises(RuntimeError, match="ambiguous build result"):
        build_wheel(witness_dir, output_dir=tmp_path)

    assert stale.read_bytes() == b"stale"


def test_build_failure_preserves_existing_artifacts(
    monkeypatch, tmp_path: Path,
) -> None:
    """A failing build leaves existing artifacts untouched and cleans the
    private child directory."""
    import subprocess as sp_mod

    old = tmp_path / "product-1.0.0.whl"
    old.write_bytes(b"old")

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(
            args=cmd, returncode=1, stdout="", stderr="boom"
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    with pytest.raises(RuntimeError, match="wheel build failed"):
        build_wheel(witness_dir, output_dir=tmp_path)

    assert old.read_bytes() == b"old"
    assert not [p for p in tmp_path.iterdir() if p.name.startswith("zealfie-build-")]
