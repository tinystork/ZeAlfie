"""Command-line interface for ZeAlfie."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from typing import TextIO

from . import get_version
from .app import collect_status, format_component_status, format_status, startup_message
from .components import UnknownComponentError, default_registry


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
    status_parser = subparsers.add_parser("status", help="show current ZeAlfie runtime status")
    status_parser.add_argument("component_id", nargs="?", help="optional component id to inspect")
    return parser


def run(argv: Sequence[str] | None = None, *, stdout: TextIO = sys.stdout) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(f"ZeAlfie {get_version()}", file=stdout)
        return 0

    if args.command == "status":
        registry = default_registry()
        if args.component_id:
            try:
                print(format_component_status(registry.inspect(args.component_id)), file=stdout)
            except UnknownComponentError:
                available = ", ".join(registry.available_ids()) or "none"
                print(
                    f"Unknown component: {args.component_id}. Available components: {available}",
                    file=stdout,
                )
                return 2
        else:
            print(format_status(collect_status(registry)), file=stdout)
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
