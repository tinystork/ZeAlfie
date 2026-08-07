"""Tests for wheel building and inspection."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from zealfie.building import (
    InspectedWheel,
    build_wheel,
    inspect_wheel,
)


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
    assert info.version == "0.0.5"


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
        # Pretend a single wheel was produced.
        (tmp_path / "fake-0.0.1-py3-none-any.whl").write_text("")
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
        (tmp_path / "fake-0.0.1-py3-none-any.whl").write_text("")
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
        # Simulate build success but multiple wheels appear.
        (tmp_path / "a-1.0.0-py3-none-any.whl").write_text("")
        (tmp_path / "b-2.0.0-py3-none-any.whl").write_text("")
        return sp_mod.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    witness_dir = Path(__file__).resolve().parent / "fixtures" / "witness_component"
    with pytest.raises(RuntimeError, match="ambiguous build"):
        build_wheel(witness_dir, output_dir=tmp_path)
