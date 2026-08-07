#!/usr/bin/env python3
"""Generate a review bundle for ZeAlfie.

Produces a clean ZIP archive containing the tracked source tree plus review
metadata (diff, diffstat, commit info).  The archive is built from
``git ls-files`` so only version-controlled files are included — no
``.venv``, caches, wheels, or IDE artefacts.

Usage::

    python tools/make_review_bundle.py --base <commit-ish>
    python tools/make_review_bundle.py --base HEAD~1 --report path/to/report.md
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path


def _git_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=True,
    )
    return Path(result.stdout.strip())


def _git(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _git_maybe(*args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
    )
    return result.stdout


def _is_dirty() -> bool:
    return bool(_git("status", "--porcelain").strip())


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a ZeAlfie review bundle.")
    parser.add_argument(
        "--base",
        required=True,
        help="Base commit for the diff (e.g. HEAD~1 or a SHA)",
    )
    parser.add_argument(
        "--report",
        help="Path to a mission report to include in REVIEW/mission_report.md",
    )
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Directory to write the ZIP into (default: AGENT/review/)",
    )
    args = parser.parse_args()

    root = _git_root()
    head_sha = _git("rev-parse", "HEAD").strip()
    short_sha = _git("rev-parse", "--short", "HEAD").strip()
    branch = _git("rev-parse", "--abbrev-ref", "HEAD").strip()
    dirty = _is_dirty()

    # -- output path ----------------------------------------------------------
    if args.output_dir:
        out_dir = Path(args.output_dir)
    else:
        out_dir = root / "AGENT" / "review"
    out_dir.mkdir(parents=True, exist_ok=True)

    suffix = "_dirty" if dirty else ""
    zip_name = f"ZeAlfie_review_{short_sha}{suffix}.zip"
    zip_path = out_dir / zip_name

    # -- collect tracked files ------------------------------------------------
    tracked = _git("ls-files", "-z").rstrip("\0")
    tracked_paths = [
        Path(p) for p in tracked.split("\0") if p.strip()
    ]

    # -- security: reject files that escape the repo --------------------------
    for tp in tracked_paths:
        abs_path = (root / tp).resolve()
        try:
            abs_path.relative_to(root.resolve())
        except ValueError:
            print(f"ERROR: tracked file escapes repo: {tp}", file=sys.stderr)
            sys.exit(1)

    # -- build ZIP ------------------------------------------------------------
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for tp in tracked_paths:
            full = root / tp
            if full.is_symlink():
                target = os.readlink(str(full))
                if target.startswith("/") or ".." in target:
                    print(f"WARNING: skipping external symlink: {tp}", file=sys.stderr)
                    continue
            if full.is_file():
                zf.write(full, str(tp))

        # REVIEW/ metadata
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

        metadata = "\n".join([
            f"Repository: ZeAlfie",
            f"Branch: {branch}",
            f"HEAD: {head_sha}",
            f"Base: {args.base}",
            f"Generated at: {now}",
            f"Python: {sys.version.split()[0]}",
            f"Platform: {sys.platform}",
            f"Worktree: {'DIRTY' if dirty else 'clean'}",
            "",
            "Git status:",
            _git_maybe("status", "--porcelain") or "(clean)",
        ])
        zf.writestr("REVIEW/metadata.txt", metadata)

        diff = _git_maybe("diff", f"{args.base}..HEAD")
        if diff.strip():
            zf.writestr("REVIEW/changes.patch", diff)

        diffstat = _git_maybe("diff", f"{args.base}..HEAD", "--stat")
        if diffstat.strip():
            zf.writestr("REVIEW/diffstat.txt", diffstat)

        # Optional mission report
        if args.report:
            report_path = Path(args.report)
            if not report_path.is_absolute():
                report_path = Path.cwd() / report_path
            if report_path.is_file():
                zf.write(report_path, "REVIEW/mission_report.md")

    print(f"Review bundle written to {zip_path}")


if __name__ == "__main__":
    main()
