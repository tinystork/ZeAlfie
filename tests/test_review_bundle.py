"""Tests for the review bundle generator."""

from __future__ import annotations

import subprocess
import tempfile
import zipfile
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures: tiny git repo
# ---------------------------------------------------------------------------


@pytest.fixture()
def mini_repo(tmp_path: Path) -> Path:
    """Create a minimal git repository with tracked and ignored files."""
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args):
        subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=True,
        )

    git("init")
    git("config", "user.email", "test@test.test")
    git("config", "user.name", "Test")

    # Tracked file
    (repo / "tracked.txt").write_text("hello")

    # Ignored file (via .gitignore)
    (repo / ".gitignore").write_text(".venv/\nignored.txt\nAGENT/\n")

    (repo / "ignored.txt").write_text("secret")

    venv_dir = repo / ".venv"
    venv_dir.mkdir()
    (venv_dir / "noise.txt").write_text("venv noise")

    agent_dir = repo / "AGENT"
    agent_dir.mkdir()
    (agent_dir / "note.txt").write_text("agent note")

    git("add", "tracked.txt", ".gitignore")
    git("commit", "-m", "initial")

    # Second commit so there is a real diff range.
    (repo / "tracked.txt").write_text("hello v2")
    git("add", "tracked.txt")
    git("commit", "-m", "second commit")

    return repo


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_bundle_contains_tracked_file(mini_repo: Path, tmp_path: Path) -> None:
    """Tracked files must appear in the bundle."""
    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    assert "tracked.txt" in names


def test_bundle_excludes_ignored_file(mini_repo: Path, tmp_path: Path) -> None:
    """Git-ignored files must NOT appear in the bundle."""
    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    assert "ignored.txt" not in names


def test_bundle_excludes_dot_venv(mini_repo: Path, tmp_path: Path) -> None:
    """.venv contents must NOT appear in the bundle."""
    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    venv_files = [n for n in names if ".venv" in n]
    assert not venv_files, f"venv leaked: {venv_files}"


def test_bundle_excludes_agent_dir(mini_repo: Path, tmp_path: Path) -> None:
    """AGENT/ must NOT appear in the bundle (unless explicitly requested)."""
    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")

    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()
    agent_files = [n for n in names if n.startswith("AGENT/")]
    assert not agent_files, f"AGENT leaked: {agent_files}"


def test_bundle_contains_review_metadata(mini_repo: Path, tmp_path: Path) -> None:
    """REVIEW/metadata.txt must be present with expected fields."""
    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")

    with zipfile.ZipFile(zip_path, "r") as zf:
        metadata = zf.read("REVIEW/metadata.txt").decode("utf-8")

    assert "Repository: ZeAlfie" in metadata
    assert "Branch:" in metadata
    assert "HEAD:" in metadata


def test_bundle_contains_diffstat(mini_repo: Path, tmp_path: Path) -> None:
    """REVIEW/diffstat.txt must exist."""
    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")

    with zipfile.ZipFile(zip_path, "r") as zf:
        assert "REVIEW/diffstat.txt" in zf.namelist()


def test_bundle_dirty_worktree_suffixed(mini_repo: Path, tmp_path: Path) -> None:
    """A dirty worktree creates a _dirty suffix."""
    # Make the worktree dirty.
    (mini_repo / "tracked.txt").write_text("modified")

    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1")
    assert "_dirty" in zip_path.name


def test_bundle_with_report(mini_repo: Path, tmp_path: Path) -> None:
    """When --report is given, the report is included in REVIEW/."""
    report = tmp_path / "test_report.md"
    report.write_text("# Test Report\n\nContent.")

    zip_path = _run_tool(mini_repo, tmp_path, base="HEAD~1", report=str(report))

    with zipfile.ZipFile(zip_path, "r") as zf:
        content = zf.read("REVIEW/mission_report.md").decode("utf-8")
    assert "# Test Report" in content


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_tool(
    repo: Path,
    tmp: Path,
    *,
    base: str,
    report: str | None = None,
) -> Path:
    """Run make_review_bundle via direct import (avoids subprocess argv issues)."""
    import sys
    import os

    tool_dir = Path(__file__).resolve().parents[1] / "tools"
    sys.path.insert(0, str(tool_dir))
    try:
        from make_review_bundle import main as bundle_main
        # Patch sys.argv
        out_dir = tmp / "bundle-out"
        out_dir.mkdir()
        argv = [
            "make_review_bundle.py",
            "--base", base,
            "--output-dir", str(out_dir),
        ]
        if report:
            argv.extend(["--report", report])

        old_argv = sys.argv
        old_cwd = os.getcwd()
        try:
            os.chdir(str(repo))
            sys.argv = argv
            bundle_main()
        finally:
            sys.argv = old_argv
            os.chdir(old_cwd)
    finally:
        sys.path.pop(0)

    zips = sorted(out_dir.glob("*.zip"))
    assert zips, f"no ZIP produced in {out_dir}"
    return zips[0]
