"""Command-line interface for ZeAlfie."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import get_version
from .app import collect_status, format_component_status, format_status, startup_message
from .components import UnknownComponentError, default_registry
from .runtime import (
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    SharedRuntimeError,
    default_runtime_layout,
)


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

    # -- runtime subcommand --------------------------------------------------
    runtime_parser = subparsers.add_parser("runtime", help="manage the shared runtime")
    runtime_subs = runtime_parser.add_subparsers(dest="runtime_command")
    runtime_status = runtime_subs.add_parser("status", help="show shared runtime status")
    runtime_create = runtime_subs.add_parser("create", help="create the shared runtime")
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

    if args.command == "runtime":
        layout = default_runtime_layout()
        rt = SharedRuntime(layout=layout)

        if args.runtime_command == "status":
            print(_format_runtime_status(rt.status()), file=stdout)
            return 0

        if args.runtime_command == "create":
            try:
                st = rt.create()
                print(_format_runtime_status(st), file=stdout)
                return 0
            except SharedRuntimeError as exc:
                print(f"Cannot create shared runtime: {exc}", file=sys.stderr)
                return 3

        # No runtime subcommand given → show help.
        runtime_parser.print_help(file=stdout)
        return 0

    print(startup_message(), file=stdout)
    return 0


def _format_runtime_status(st: "RuntimeStatus") -> str:
    lines = [
        f"Shared runtime:",
        f" State: {st.state.value}",
        f" Runtime root: {st.runtime_root}",
    ]
    if st.active_slot_id:
        lines.append(f" Active slot: {st.active_slot_id}")
        if st.active_path:
            lines.append(f" Active path: {st.active_path}")
    if st.previous_slot_id:
        lines.append(f" Previous slot: {st.previous_slot_id}")
    if st.python_executable is not None:
        lines.append(f" Python: {st.python_executable}")
    if st.python_version:
        lines.append(f" Python version: {st.python_version}")
    if st.reason:
        lines.append(f" Reason: {st.reason}")
    if st.reason_code is not None:
        lines.append(f" Reason code: {st.reason_code.value}")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 1
