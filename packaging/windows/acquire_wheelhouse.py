"""ZeAlfie Windows installer — deterministic wheelhouse acquisition (ZA-WIN-BOOT-02).

Thin runnable entrypoint that materialises the EXACT offline wheelhouse the
installer bundles.  Driven by the CI workflow (``windows-installer-build.yml``)
or by a human:

1. **load+validate** the committed lock (``wheelhouse.lock.toml``) — fail
   closed on malformed/drifted lock;
2. **download** every pinned wheel with
   ``pip download --no-deps --only-binary=:all: --platform win_amd64
   --python-version 3.13 --implementation cp --abi cp313 <name>==<version>``
   into a staging directory — deterministic pins, never "latest";
3. **verify** the staging directory against the lock EXACTLY: same file set,
   every SHA-256 matches, no extras (fail closed on drift);
4. **add** the freshly-built zealfie wheel (from ``--zealfie-wheel``) and
   verify the final wheelhouse again;
5. print a compact provenance summary (JSON) for the CI artifact.

Design rules (mirror ``provision_windows.py``):

* stdlib + sibling ``provision``/``wheelhouse_lock`` only (never imports
  ZeAlfie or the PyPI ``packaging`` distribution);
* subprocesses use ``CREATE_NO_WINDOW`` on Windows, never ``shell=True``;
* fail closed: every step exits non-zero with evidence on stderr; a lock
  drift, hash mismatch, or network hiccup NEVER degrades to a partial
  wheelhouse.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

# sys.path[0] is this script's directory when run as
# ``python packaging/windows/acquire_wheelhouse.py``.
import provision  # type: ignore[import-not-found]  # same-directory module
import wheelhouse_lock  # type: ignore[import-not-found]  # same-directory module

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

#: Resolution flags that MUST match the lock generation flags (win_amd64,
#: CPython 3.13).  The lock's own metadata is validated against these.
_DOWNLOAD_FLAGS = [    "--only-binary=:all:",
    "--platform",
    "win_amd64",
    "--python-version",
    "3.13",
    "--implementation",
    "cp",
    "--abi",
    "cp313",
    "--disable-pip-version-check",
]


class AcquireError(RuntimeError):
    """A wheelhouse acquisition step failed (fail closed)."""


def _log(msg: str) -> None:
    print(msg, flush=True)


def _run(argv: list[str], *, timeout_s: int = 900, **kwargs) -> subprocess.CompletedProcess:
    subprocess_kwargs = dict(kwargs)
    if sys.platform == "win32" and "creationflags" not in subprocess_kwargs:
        subprocess_kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(argv, timeout=timeout_s, **subprocess_kwargs)


def _resolve_pip() -> str:
    """Return the pip executable that must be used for acquisition.

    The interpreter running this script is the DRIVER python (CI
    setup-python); ``python -m pip`` is therefore the exact pip of the
    driver, which is the same class of pip used to generate the lock.
    """
    return sys.executable


def step_download(lock: wheelhouse_lock.WheelhouseLock, staging: Path) -> None:
    """Download every pinned wheel (--no-deps, exact pins) into staging."""
    staging.mkdir(parents=True, exist_ok=True)
    pip = _resolve_pip()
    specs = wheelhouse_lock.pinned_download_specs(lock)
    _log(f"[acquire] downloading {len(specs)} pinned wheels with pip {pip}")
    for spec in specs:
        argv = [
            pip, "-m", "pip", "download", "--dest", str(staging),
            *_DOWNLOAD_FLAGS, "--no-deps", spec,
        ]
        _log(f"[acquire]   pip download --no-deps {spec}")
        proc = _run(argv, capture_output=True, text=True, encoding="utf-8",
                    errors="replace")
        if proc.returncode != 0:
            raise AcquireError(
                f"pip download failed for {spec} rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
    # Fail closed on drift BEFORE the zealfie wheel is added: the staging
    # dir must hold EXACTLY the pinned downloadable wheels, each with its
    # locked SHA-256.
    wheelhouse_lock.verify_pinned_subset(staging, lock)
    _log("[acquire] staged wheelhouse verified against the lock "
         "(file set + SHA-256 + sizes)")


def step_add_zealfie_wheel(
    lock: wheelhouse_lock.WheelhouseLock,
    staging: Path,
    zealfie_wheel: Path,
) -> str:
    """Copy the freshly-built zealfie wheel into the wheelhouse."""
    if not zealfie_wheel.is_file():
        raise AcquireError(f"zealfie wheel not found: {zealfie_wheel}")
    want = lock.zealfie_wheel.filename
    if zealfie_wheel.name != want:
        raise AcquireError(
            f"zealfie wheel filename mismatch: expected {want!r}, got "
            f"{zealfie_wheel.name!r}"
        )
    dest = staging / want
    if dest.exists() and dest.read_bytes() == zealfie_wheel.read_bytes():
        _log(f"[acquire] zealfie wheel already present: {dest}")
    else:
        shutil.copyfile(zealfie_wheel, dest)
        _log(f"[acquire] added freshly-built zealfie wheel: {dest}")
    digest = provision.sha256_file(dest)
    _log(f"[acquire] zealfie wheel sha256={digest}")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python packaging/windows/acquire_wheelhouse.py",
        description="ZeAlfie Windows installer wheelhouse acquisition (ZA-WIN-BOOT-02)",
    )
    parser.add_argument("--lock", type=Path, default=None,
                        help="path to wheelhouse.lock.toml (default: sibling)")
    parser.add_argument("--dest", type=Path, required=True,
                        help="output wheelhouse directory (created if absent)")
    parser.add_argument("--zealfie-wheel", type=Path, required=True,
                        help="freshly-built zealfie wheel to add")
    args = parser.parse_args(argv)

    lock_path = args.lock or wheelhouse_lock.default_lock_path()
    try:
        lock = wheelhouse_lock.load_lock(lock_path)
    except wheelhouse_lock.WheelhouseLockError as exc:
        print(f"[acquire] LOCK ERROR: {exc}", file=sys.stderr)
        return 1

    _log(f"[acquire] zealfie {lock.zealfie_version} @ "
         f"{lock.source_commit[:12]} ({lock.generated})")
    _log(f"[acquire] target: {lock.platform_tag} / CPython {lock.cpython_version} "
         f"(python {lock.python_tag}, abi {lock.abi_tag})")
    _log(f"[acquire] {len(lock.wheels)} pinned wheels + local zealfie wheel")

    staging = Path(args.dest)
    try:
        step_download(lock, staging)
        digest = step_add_zealfie_wheel(lock, staging, Path(args.zealfie_wheel))
        summary = wheelhouse_lock.verify_wheelhouse_dir(staging, lock)
    except (AcquireError, wheelhouse_lock.WheelhouseLockError) as exc:
        print(f"[acquire] FAILED: {exc}", file=sys.stderr)
        return 1

    zealfie_size = (staging / lock.zealfie_wheel.filename).stat().st_size
    print(json.dumps({
        "status": "ok",
        "zealfie_version": lock.zealfie_version,
        "source_commit": lock.source_commit,
        "wheel_count": summary["wheel_count"],
        "total_locked_bytes": summary["total_locked_bytes"],
        "zealfie_wheel": {
            "filename": lock.zealfie_wheel.filename,
            "sha256": digest,
            "size": zealfie_size,
        },
        "wheelhouse": str(staging.resolve()),
    }, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
