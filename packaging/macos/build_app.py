"""Build the native ARM64 unsigned ``ZeAlfie.app`` bundle (ZA-MAC-BOOT-01).

Runnable entrypoint used by ``.github/workflows/macos-packaging-arm64.yml``
and by a human on an Apple-silicon Mac.  It performs, in order:

1. load + validate the pinned PBS record (fail closed);
2. obtain the pinned install_only archive (or verify a supplied one);
3. stage ``ZeAlfie.app/Contents/{MacOS,Resources}``;
4. extract the archive so its top-level ``python/`` becomes
   ``Contents/Resources/python`` (and assert the interpreter is arm64);
5. install the locked wheelhouse into ``Contents/Resources/app`` with the
   PRIVATE bundled Python — offline (``--no-index --find-links``), NO
   application venv, NO live dependency resolution;
6. write ``Info.plist``, the POSIX launcher and ``zealfie.icns``;
7. normalise every universal Mach-O file to thin arm64 (``lipo``);
8. audit ``Resources/python`` + ``Resources/app`` (ARM64_ONLY / residues);
9. zip the bundle as the engineering RC artefact (top-level ``ZeAlfie.app``).

All steps fail closed.  This builder MUST run on macOS (it executes the
private macOS interpreter); it refuses to run elsewhere.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import macho  # type: ignore[import-not-found]
import macpack  # type: ignore[import-not-found]
import wheelhouse  # type: ignore[import-not-found]

__all__ = [
    "BuildError",
    "stage_bundle",
    "install_application",
    "write_identity_files",
    "normalise_and_audit",
    "build",
    "main",
]

_LAUNCHER_TEMPLATE = Path(__file__).resolve().parent / "launcher" / "ZeAlfie"
_DEFAULT_ICNS = (
    Path(__file__).resolve().parents[2] / "src" / "zealfie" / "icon" / "zealfie.icns"
)


class BuildError(RuntimeError):
    """A bundle build step failed (fail closed)."""


def _log(msg: str) -> None:
    print(msg, flush=True)


def stage_bundle(app: Path) -> None:
    """Create a fresh, empty bundle skeleton (removing any previous one)."""
    if app.exists():
        shutil.rmtree(app)
    macpack.bundle_macos_dir(app).mkdir(parents=True, exist_ok=True)
    macpack.bundle_resources_dir(app).mkdir(parents=True, exist_ok=True)


def install_application(
    app: Path,
    lock: wheelhouse.WheelhouseLock,
    wheelhouse_dir: Path,
) -> None:
    """Install the locked wheelhouse into ``Contents/Resources/app``.

    Uses ONLY the private bundled interpreter and the offline wheelhouse.
    ``--no-index`` forbids PyPI, ``--find-links`` restricts the source to the
    verified wheelhouse, and ``--target`` installs directly with no venv.
    """
    interpreter = macpack.bundled_interpreter(app)
    target = macpack.bundle_app_dir(app)
    target.mkdir(parents=True, exist_ok=True)
    argv = [
        str(interpreter), "-m", "pip", "install",
        "--disable-pip-version-check",
        "--no-cache-dir",
        "--no-index",
        "--find-links", str(wheelhouse_dir),
        "--target", str(target),
        str(wheelhouse_dir / lock.zealfie_wheel.filename),
    ]
    _log("[build] " + " ".join(argv))
    proc = subprocess.run(argv, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise BuildError(
            f"offline wheelhouse install failed (rc={proc.returncode})"
        )
    if not (target / "zealfie").is_dir():
        raise BuildError(
            f"application tree missing after install: {target / 'zealfie'}"
        )


def write_identity_files(app: Path, icns_source: Path | None = None) -> None:
    """Write Info.plist, the launcher (0755) and zealfie.icns."""
    plist = macpack.bundle_plist(app)
    plist.write_text(macpack.render_plist(), encoding="utf-8")

    launcher = macpack.bundle_launcher(app)
    launcher.write_bytes(_LAUNCHER_TEMPLATE.read_bytes())
    launcher.chmod(0o755)

    icns = macpack.bundle_icns(app)
    source = icns_source if icns_source is not None else _DEFAULT_ICNS
    if not source.is_file():
        raise BuildError(f"icon source missing: {source}")
    shutil.copyfile(source, icns)


def normalise_and_audit(app: Path) -> dict:
    """Thin every universal Mach-O file to arm64, then audit ALL trees.

    Fails closed on any lipo error, any non-arm64 result, or any x86_64
    residue in ``Resources/python`` / ``Resources/app``.
    """
    resources = macpack.bundle_resources_dir(app)
    thinning = macho.thin_tree_to_arm64(resources)
    _log(f"[build] Mach-O files thinned: {len(thinning['files_thinned'])}")

    audits: dict[str, dict] = {}
    for label, subtree in (
        ("python", macpack.bundle_python_dir(app)),
        ("app", macpack.bundle_app_dir(app)),
    ):
        audits[label] = macho.audit_tree(subtree)
        _log(f"[build] audit[{label}]:\n" + macho.format_audit(audits[label]))

    files_checked = sum(a["files_checked"] for a in audits.values())
    residues = sum(a["X86_64_RESIDUES"] for a in audits.values())
    arm64_only = "PASS" if all(a["ARM64_ONLY"] == "PASS" for a in audits.values()) else "FAIL"
    summary = {
        "files_thinned": len(thinning["files_thinned"]),
        "files_unchanged": len(thinning["files_unchanged"]),
        "thinned_files": thinning["files_thinned"],
        "files_checked": files_checked,
        "macho_files": sum(len(a["macho_files"]) for a in audits.values()),
        "fat_files_remaining": sum(len(a["fat_files"]) for a in audits.values()),
        "arch_histogram": {
            arch: sum(a["arch_histogram"].get(arch, 0) for a in audits.values())
            for arch in sorted(
                {k for a in audits.values() for k in a["arch_histogram"]}
            )
        },
        "x86_64_files": sorted(
            {f for a in audits.values() for f in a["x86_64_files"]}
        ),
        "non_arm64_macho_files": sorted(
            {f for a in audits.values() for f in a["non_arm64_macho_files"]}
        ),
        "ARM64_ONLY": arm64_only,
        "X86_64_RESIDUES": residues,
    }
    if arm64_only != "PASS" or residues != 0:
        raise BuildError(
            "ARM64-only normalisation failed: "
            f"ARM64_ONLY={arm64_only} X86_64_RESIDUES={residues} "
            f"residues={summary['x86_64_files']} "
            f"non_arm64={summary['non_arm64_macho_files']}"
        )
    return summary


def build(
    work: Path,
    wheelhouse_dir: Path,
    zealfie_wheel: Path,
    out_zip: Path,
    *,
    icns_source: Path | None = None,
    pbs_archive: Path | None = None,
) -> dict:
    """Perform the full build and return a provenance summary."""
    record = macpack.load_record()
    work.mkdir(parents=True, exist_ok=True)
    app = work / macpack.APP_NAME

    lock = wheelhouse.load_lock()
    wheelhouse.verify_wheelhouse_dir(wheelhouse_dir, lock)
    if (wheelhouse_dir / lock.zealfie_wheel.filename).name != zealfie_wheel.name:
        raise BuildError(
            f"zealfie wheel mismatch: lock wants "
            f"{lock.zealfie_wheel.filename!r}, got {zealfie_wheel.name!r}"
        )

    if pbs_archive is None:
        cached = work / "cache" / record.archive_filename
        if cached.is_file():
            macpack.verify_archive_sha256(cached, record=record)
        else:
            _log(f"[build] downloading {record.archive_url}")
            macpack.download_archive(record, cached)
        pbs_archive = cached
    else:
        macpack.verify_archive_sha256(pbs_archive, record=record)
    archive_sha = macpack.sha256_file(pbs_archive)
    _log(f"[build] PBS archive verified: {pbs_archive.name} sha256={archive_sha}")

    stage_bundle(app)
    _log("[build] extracting private CPython into Contents/Resources/python")
    macpack.extract_python_tarball(pbs_archive, macpack.bundle_resources_dir(app))

    install_application(app, lock, wheelhouse_dir)
    write_identity_files(app, icns_source)

    thinning = normalise_and_audit(app)
    macpack.assert_bundle_layout(app)

    artifact = macpack.zip_app(app, out_zip)
    summary = {
        "status": "ok",
        "app_bundle": str(app),
        "bundle_layout": [
            str(p.relative_to(app)) for p in macpack.required_bundle_paths(app)
        ],
        "pbs": {
            "filename": record.archive_filename,
            "url": record.archive_url,
            "size": record.size,
            "sha256": record.sha256,
            "target_triple": record.target_triple,
            "cpython_version": record.cpython_version,
            "release_tag": record.release_tag,
            "verified_sha256": archive_sha,
        },
        "wheelhouse": {
            "path": str(wheelhouse_dir),
            "wheel_count": len(lock.wheels) + 1,
            "platform_tag": lock.platform_tag,
        },
        "macho": thinning,
        "artifact": artifact,
    }
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python packaging/macos/build_app.py",
        description="Build the ZeAlfie macOS ARM64 unsigned .app bundle",
    )
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--wheelhouse", type=Path, required=True)
    parser.add_argument("--zealfie-wheel", type=Path, required=True)
    parser.add_argument("--out-zip", type=Path, required=True)
    parser.add_argument("--icns", type=Path, default=None)
    parser.add_argument("--pbs-archive", type=Path, default=None)
    parser.add_argument("--provenance-out", type=Path, default=None)
    args = parser.parse_args(argv)

    if sys.platform != "darwin":
        print(
            "[build] FAILED: this builder executes the private macOS "
            "interpreter and must run on macOS",
            file=sys.stderr,
        )
        return 2

    try:
        summary = build(
            args.work,
            args.wheelhouse,
            args.zealfie_wheel,
            args.out_zip,
            icns_source=args.icns,
            pbs_archive=args.pbs_archive,
        )
    except (BuildError, macpack.MacPackError, wheelhouse.WheelhouseLockError,
            macho.MachOError) as exc:
        print(f"[build] FAILED: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(summary, indent=2)
    if args.provenance_out is not None:
        args.provenance_out.parent.mkdir(parents=True, exist_ok=True)
        args.provenance_out.write_text(payload + "\n", encoding="utf-8")
        _log(f"[build] provenance written: {args.provenance_out}")
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
