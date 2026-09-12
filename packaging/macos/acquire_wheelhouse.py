"""ZeAlfie macOS bundle — deterministic wheelhouse acquisition (ZA-MAC-BOOT-01).

Thin runnable entrypoint that materialises the EXACT offline wheelhouse the
bundle installs into ``Contents/Resources/app``:

1. **load+validate** the committed lock (``wheelhouse.lock.toml``) — fail
   closed on malformed/drifted lock;
2. **download** every pinned wheel with ``pip download --no-deps
   --only-binary=:all: --platform macosx_13_0_universal2 --python-version
   3.13 --implementation cp --abi cp313 <name>==<version>`` — deterministic
   pins, never "latest";
3. **verify** the staging directory against the lock EXACTLY: same file set,
   every SHA-256 and size matches, no extras (fail closed on drift);
4. **add** the freshly-built zealfie wheel (from ``--zealfie-wheel``) and
   verify the final wheelhouse again;
5. print a compact provenance summary (JSON) for the build artefact.

stdlib + sibling ``wheelhouse`` module only (never imports ZeAlfie or the
PyPI ``packaging`` distribution).  Every step exits non-zero on failure.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path

import wheelhouse  # type: ignore[import-not-found]  # sibling module

__all__ = ["AcquireError", "step_download", "step_add_zealfie_wheel", "main"]

#: Resolution flags that MUST match the lock generation flags.
_DOWNLOAD_FLAGS = [
    "--only-binary=:all:",
    "--platform",
    wheelhouse.PLATFORM_TAG,
    "--python-version",
    "3.13",
    "--implementation",
    "cp",
    "--abi",
    wheelhouse.ABI_TAG,
    "--disable-pip-version-check",
]


class AcquireError(RuntimeError):
    """A wheelhouse acquisition step failed (fail closed)."""


def _log(msg: str) -> None:
    print(msg, flush=True)


def step_download(lock: wheelhouse.WheelhouseLock, staging: Path) -> None:
    staging.mkdir(parents=True, exist_ok=True)
    pip = sys.executable
    specs = wheelhouse.pinned_download_specs(lock)
    _log(f"[acquire] downloading {len(specs)} pinned wheels with pip {pip}")
    for spec in specs:
        argv = [
            pip, "-m", "pip", "download", "--dest", str(staging),
            *_DOWNLOAD_FLAGS, "--no-deps", spec,
        ]
        _log(f"[acquire]   pip download --no-deps {spec}")
        proc = subprocess.run(
            argv, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=1800,
        )
        if proc.returncode != 0:
            raise AcquireError(
                f"pip download failed for {spec} rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
    wheelhouse.verify_pinned_subset(staging, lock)
    _log("[acquire] staged wheelhouse verified against the lock "
         "(file set + SHA-256 + sizes)")


def step_add_zealfie_wheel(
    lock: wheelhouse.WheelhouseLock,
    staging: Path,
    zealfie_wheel: Path,
) -> str:
    if not zealfie_wheel.is_file():
        raise AcquireError(f"zealfie wheel not found: {zealfie_wheel}")
    want = lock.zealfie_wheel.filename
    if zealfie_wheel.name != want:
        raise AcquireError(
            f"zealfie wheel filename mismatch: expected {want!r}, got "
            f"{zealfie_wheel.name!r}"
        )
    dest = staging / want
    if not (dest.exists() and dest.read_bytes() == zealfie_wheel.read_bytes()):
        shutil.copyfile(zealfie_wheel, dest)
        _log(f"[acquire] added freshly-built zealfie wheel: {dest}")
    digest = wheelhouse.sha256_file(dest)
    _log(f"[acquire] zealfie wheel sha256={digest}")
    return digest


def _write_provenance(summary: dict, out_path: Path | None) -> None:
    payload = json.dumps(summary, indent=2)
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
    else:
        print(payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python packaging/macos/acquire_wheelhouse.py",
        description="ZeAlfie macOS bundle wheelhouse acquisition (ZA-MAC-BOOT-01)",
    )
    parser.add_argument("--lock", type=Path, default=None)
    parser.add_argument("--dest", type=Path, required=True)
    parser.add_argument("--zealfie-wheel", type=Path, required=True)
    parser.add_argument("--provenance-out", type=Path, default=None)
    args = parser.parse_args(argv)

    lock_path = args.lock or wheelhouse.default_lock_path()
    try:
        lock = wheelhouse.load_lock(lock_path)
    except wheelhouse.WheelhouseLockError as exc:
        print(f"[acquire] LOCK ERROR: {exc}", file=sys.stderr)
        return 1

    _log(f"[acquire] zealfie {lock.zealfie_version} @ "
         f"{lock.source_commit[:12]} ({lock.generated})")
    _log(f"[acquire] target: {lock.platform_tag} / CPython "
         f"{lock.cpython_version} (python {lock.python_tag}, abi {lock.abi_tag})")
    for entry in lock.wheels:
        _log(f"[acquire]   pinned {entry.filename} ({entry.size} bytes)")

    staging = Path(args.dest)
    try:
        step_download(lock, staging)
        digest = step_add_zealfie_wheel(lock, staging, Path(args.zealfie_wheel))
        summary = wheelhouse.verify_wheelhouse_dir(staging, lock)
    except (AcquireError, wheelhouse.WheelhouseLockError) as exc:
        print(f"[acquire] FAILED: {exc}", file=sys.stderr)
        return 1

    zealfie_size = (staging / lock.zealfie_wheel.filename).stat().st_size
    provenance_summary = {
        "status": "ok",
        "zealfie_version": lock.zealfie_version,
        "source_commit": lock.source_commit,
        "platform_tag": lock.platform_tag,
        "wheel_count": summary["wheel_count"],
        "total_locked_bytes": summary["total_locked_bytes"],
        "wheels": [
            {
                "filename": entry.filename,
                "name": entry.name,
                "version": entry.version,
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in lock.wheels
        ],
        "zealfie_wheel": {
            "filename": lock.zealfie_wheel.filename,
            "sha256": digest,
            "size": zealfie_size,
        },
        "wheelhouse": str(staging.resolve()),
    }
    _write_provenance(provenance_summary, args.provenance_out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
