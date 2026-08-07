"""Wheel building and inspection for ZeAlfie."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True, slots=True)
class InspectedWheel:
    """Result of inspecting a wheel archive without executing its code."""

    wheel_path: Path
    top_level_packages: tuple[str, ...]
    dist_info_dir: str | None
    version: str | None
    entry_points: tuple[InspectedEntryPoint, ...]


@dataclass(frozen=True, slots=True)
class InspectedEntryPoint:
    group: str
    name: str
    value: str | None = None


def build_wheel(
    source_dir: str | Path,
    *,
    output_dir: str | Path | None = None,
) -> Path:
    """Build a wheel from a source directory.

    Uses ``python -m build --wheel`` in a temporary directory when
    *output_dir* is not supplied, so the repository working tree is
    never polluted.

    Returns the path to the built wheel.
    """
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"source directory not found: {source}")

    tmp_own = output_dir is None
    out = Path(output_dir) if output_dir is not None else Path(tempfile.mkdtemp(prefix="zealfie-build-"))
    try:
        build_env = {
            "PIP_NO_INDEX": "1",
            "PIP_INDEX_URL": "",
        }
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "build",
                str(source),
                "--wheel",
                "--no-isolation",
                "--outdir",
                str(out),
            ],
            capture_output=True,
            text=True,
            timeout=120,
            env={**__import__("os").environ, **build_env},
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"wheel build failed for {source}:\n{result.stderr.strip()}"
            )
    except Exception:
        if tmp_own:
            _remove_tree_safe(out)
        raise

    wheels = sorted(out.glob("*.whl"))
    if len(wheels) == 0:
        if tmp_own:
            _remove_tree_safe(out)
        raise RuntimeError(
            f"no wheel produced in {out}; "
            f"build succeeded but did not generate a wheel file"
        )
    if len(wheels) > 1:
        if tmp_own:
            _remove_tree_safe(out)
        raise RuntimeError(
            f"ambiguous build: {len(wheels)} wheels produced in {out}; "
            f"expected exactly one wheel"
        )
    return wheels[0]


def inspect_wheel(wheel_path: str | Path) -> InspectedWheel:
    """Open a wheel as a ZIP archive and inspect its contents.

    No Python code from the wheel is loaded or executed.
    """
    wheel = Path(wheel_path)
    if not wheel.is_file():
        raise FileNotFoundError(f"wheel not found: {wheel}")

    top_level_packages: list[str] = []
    dist_info_dir: str | None = None
    version: str | None = None
    entry_points_raw: str | None = None

    with zipfile.ZipFile(wheel, "r") as zf:
        for name in zf.namelist():
            parts = name.rstrip("/").split("/")

            # Top-level packages (e.g. "zealfie/__init__.py")
            if len(parts) == 2 and parts[1] == "__init__.py" and parts[0].endswith(".dist-info") is False:
                top_level_packages.append(parts[0])

            # .dist-info directory
            if not dist_info_dir and len(parts) >= 1 and parts[0].endswith(".dist-info"):
                dist_info_dir = parts[0]

            # METADATA inside .dist-info
            if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "METADATA":
                metadata_text = zf.read(name).decode("utf-8")
                for line in metadata_text.splitlines():
                    if line.startswith("Version:"):
                        version = line.split(":", 1)[1].strip()

            # entry_points.txt inside .dist-info
            if len(parts) == 2 and parts[0].endswith(".dist-info") and parts[1] == "entry_points.txt":
                entry_points_raw = zf.read(name).decode("utf-8")

    entry_points = _parse_entry_points_text(entry_points_raw)

    return InspectedWheel(
        wheel_path=wheel,
        top_level_packages=tuple(sorted(top_level_packages)),
        dist_info_dir=dist_info_dir,
        version=version,
        entry_points=entry_points,
    )


def _parse_entry_points_text(text: str | None) -> tuple[InspectedEntryPoint, ...]:
    """Parse an entry_points.txt file into structured entries.

    Handles INI-style section headers like ``[console_scripts]``.
    """
    if not text:
        return ()

    entries: list[InspectedEntryPoint] = []
    current_group = ""

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1]
            continue
        if "=" in line:
            name, value = line.split("=", 1)
            entries.append(
                InspectedEntryPoint(
                    group=current_group.strip(),
                    name=name.strip(),
                    value=value.strip(),
                )
            )

    return tuple(entries)


def _remove_tree_safe(path: Path) -> None:
    """Best-effort directory removal."""
    import shutil

    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
