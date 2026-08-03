"""Command-line interface for ZeAlfie."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from . import get_version
from .app import collect_status, format_status, startup_message


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="zealfie",
        description="Astronomy Launcher For Imaging Engines",
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="show the ZeAlfie version and exit",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("status", help="show current ZeAlfie runtime status")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"ZeAlfie {get_version()}", file=stdout)
        return 0

    if args.command == "status":
        print(format_status(collect_status()), file=stdout)
        return 0

    print(startup_message(), file=stdout)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 1
