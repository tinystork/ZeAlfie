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
    """Result of inspecting a wheel archive without executing its code.

    ``distribution_name`` and ``version`` come from the **canonical**
    ``.dist-info/METADATA`` file, not from the directory name.
    """

    wheel_path: Path
    top_level_packages: tuple[str, ...]
    dist_info_dir: str
    distribution_name: str
    version: str
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
    """Build a wheel from a source directory, safe from CWD shadowing.

    The subprocess runs from the output directory (or a temporary
    directory) so that a local ``build/`` folder in the repo checkout
    cannot mask the PyPA ``build`` package.  Python prepends the CWD
    to ``sys.path``, and ``python -m build`` would otherwise find the
    local directory instead of the installed ``build`` module.

    *source_dir* is resolved to an absolute path before changing the
    CWD, so relative source directories work correctly.

    Uses ``python -m build --wheel`` in a temporary directory when
    *output_dir* is not supplied, so the repository working tree is
    never polluted.

    Returns the path to the built wheel.
    """
    source = Path(source_dir)
    if not source.is_dir():
        raise FileNotFoundError(f"source directory not found: {source}")

    # Resolve to absolute path before changing CWD; relative sources
    # would fail when resolved from the output directory.
    source = source.resolve(strict=True)

    tmp_own = output_dir is None
    if output_dir is None:
        out = Path(tempfile.mkdtemp(prefix="zealfie-build-")).resolve(strict=True)
    else:
        out = Path(output_dir).resolve(strict=False)
        out.mkdir(parents=True, exist_ok=True)
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
            cwd=str(out),  # Avoid CWD masking of the PyPA build package.
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


class WheelInspectionError(ValueError):
    """Raised when a wheel archive is structurally invalid or ambiguous."""


def inspect_wheel(wheel_path: str | Path) -> InspectedWheel:
    """Open a wheel as a ZIP archive and inspect its contents.

    Validates structural invariants:

    * Exactly one top-level ``.dist-info`` directory.
    * ``METADATA`` must exist and contain ``Name`` and ``Version`` fields.
    * ``entry_points.txt`` must belong to the same ``.dist-info``.

    No Python code from the wheel is loaded or executed.
    """
    from zealfie.common import normalise_distribution_name

    wheel = Path(wheel_path)
    if not wheel.is_file():
        raise FileNotFoundError(f"wheel not found: {wheel}")

    try:
        zf = zipfile.ZipFile(wheel, "r")
    except zipfile.BadZipFile as exc:
        raise WheelInspectionError(f"invalid wheel ZIP: {exc}") from exc

    with zf:
        names = zf.namelist()
        # infolist() preserves duplicate entries; used for critical-member
        # duplicate detection (namelist deduplicates).
        all_names = [zi.filename for zi in zf.infolist()]

        # Find the single dist-info directory.
        dist_info_dirs: list[str] = []
        for name in names:
            parts = name.rstrip("/").split("/")
            if len(parts) >= 1 and parts[0].endswith(".dist-info"):
                di = parts[0]
                if di not in dist_info_dirs:
                    dist_info_dirs.append(di)

        if len(dist_info_dirs) == 0:
            raise WheelInspectionError("wheel has no .dist-info directory")
        if len(dist_info_dirs) > 1:
            raise WheelInspectionError(
                f"ambiguous wheel: multiple .dist-info directories: {dist_info_dirs}"
            )
        dist_info_dir = dist_info_dirs[0]

        # Read METADATA for Name + Version.
        metadata_name = f"{dist_info_dir}/METADATA"
        metadata_count = all_names.count(metadata_name)
        if metadata_count == 0:
            raise WheelInspectionError(f"wheel has no {metadata_name}")
        if metadata_count > 1:
            raise WheelInspectionError(
                f"duplicate critical member: {metadata_name} "
                f"appears {metadata_count} times"
            )
        try:
            metadata_text = zf.read(metadata_name).decode("utf-8")
        except UnicodeDecodeError as exc:
            raise WheelInspectionError(f"METADATA is not valid UTF-8: {exc}") from exc

        raw_name: str | None = None
        raw_version: str | None = None
        name_seen = 0
        version_seen = 0
        for line in metadata_text.splitlines():
            if line.startswith("Name:"):
                name_seen += 1
                if name_seen == 1:
                    raw_name = line.split(":", 1)[1].strip()
                elif name_seen > 1:
                    raise WheelInspectionError(
                        "duplicate canonical METADATA field: Name"
                    )
            elif line.startswith("Version:"):
                version_seen += 1
                if version_seen == 1:
                    raw_version = line.split(":", 1)[1].strip()
                elif version_seen > 1:
                    raise WheelInspectionError(
                        "duplicate canonical METADATA field: Version"
                    )

        if not raw_name:
            raise WheelInspectionError("METADATA has no Name field")
        if not raw_version:
            raise WheelInspectionError("METADATA has no Version field")

        distribution_name = normalise_distribution_name(raw_name)
        version = raw_version

        # Top-level packages.
        top_level_packages: list[str] = []
        for name in names:
            parts = name.rstrip("/").split("/")
            if len(parts) == 2 and parts[1] == "__init__.py" and not parts[0].endswith(".dist-info"):
                if parts[0] not in top_level_packages:
                    top_level_packages.append(parts[0])

        # entry_points.txt from the *same* dist-info.
        ep_name = f"{dist_info_dir}/entry_points.txt"
        ep_count = all_names.count(ep_name)
        entry_points_raw: str | None = None
        if ep_count > 1:
            raise WheelInspectionError(
                f"duplicate critical member: {ep_name} "
                f"appears {ep_count} times"
            )
        if ep_count == 1:
            try:
                entry_points_raw = zf.read(ep_name).decode("utf-8")
            except UnicodeDecodeError as exc:
                raise WheelInspectionError(f"entry_points.txt is not valid UTF-8: {exc}") from exc

    entry_points = _parse_entry_points_text(entry_points_raw)

    return InspectedWheel(
        wheel_path=wheel,
        top_level_packages=tuple(sorted(top_level_packages)),
        dist_info_dir=dist_info_dir,
        distribution_name=distribution_name,
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
    seen_contracts: set[tuple[str, str]] = set()

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            current_group = line[1:-1]
            continue
        if "=" in line:
            name_raw, value_raw = line.split("=", 1)
            group = current_group.strip()
            name = name_raw.strip()
            value = value_raw.strip()
            contract = (group, name)
            if contract in seen_contracts:
                raise WheelInspectionError(
                    f"duplicate entry-point contract: {group}:{name}"
                )
            seen_contracts.add(contract)
            entries.append(
                InspectedEntryPoint(
                    group=group,
                    name=name,
                    value=value,
                )
            )

    return tuple(entries)


def _remove_tree_safe(path: Path) -> None:
    """Best-effort directory removal."""
    import shutil

    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
