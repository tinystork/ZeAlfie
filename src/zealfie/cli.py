"""Command-line interface for ZeAlfie.

The default install work root is ``$XDG_CACHE_HOME/zealfie/work`` on Linux,
``~/Library/Caches/zealfie/work`` on macOS, and
``%LOCALAPPDATA%/zealfie/work`` on Windows.  Tests override it via the
injectable ``_make_work_root`` / ``_make_install_deps`` helpers.
"""

from __future__ import annotations

import argparse
import math
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TextIO

from . import get_version
from .app import (
    ComponentNotInstalledError,
    LaunchContractNotSatisfiedError,
    LaunchPreparationError,
    LaunchScriptNotFoundError,
    ManagedStatus,
    ProductDeploymentPlanningError,
    OfflineReleaseError,
    ProductInstallPreparationError,
    ProductShellState,
    ProductState,
    RemoteSourceUnavailableError,
    UnknownProductError,
    ZeAlfieService,
    collect_status,
    format_component_status,
    format_status,
    startup_message,
)
from .app.install_defaults import default_install_work_root
from .components import UnknownComponentError, default_registry
from .launching import LaunchError, LaunchResult
from .products.catalog import ProductCatalog
from .products.selection import CorruptSelectionError
from .releases import ArtifactRejectionError
from .runtime import (
    DeploymentPlan,
    DeploymentResult,
    DeploymentStep,
    PlanningError,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
    SharedRuntime,
    SharedRuntimeError,
    default_runtime_layout,
)

from .sources import SourceResolutionError
from .sources.acquisition import AcquisitionError


def _positive_finite_float(value: str) -> float:
    """Validate a finite, strictly positive float for --timeout.

    Raises :class:`argparse.ArgumentTypeError` for nan, inf, zero,
    and negative values so the parser produces a clean error message
    and exits rather than propagating garbage downstream.
    """
    f = float(value)
    if f <= 0 or not math.isfinite(f):
        raise argparse.ArgumentTypeError(
            f"timeout must be a positive finite number, got {value!r}"
        )
    return f


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

    # -- launch subcommand ---------------------------------------------------
    launch_parser = subparsers.add_parser("launch", help="launch a managed component from the shared runtime")
    launch_parser.add_argument("component_id", help="component id to launch")
    launch_parser.add_argument(
        "--timeout", type=_positive_finite_float, default=None, dest="timeout_seconds",
        help="seconds to wait before killing the process (default: no timeout)",
    )

    # -- runtime subcommand --------------------------------------------------
    runtime_parser = subparsers.add_parser("runtime", help="manage the shared runtime")
    runtime_subs = runtime_parser.add_subparsers(dest="runtime_command")
    runtime_subs.add_parser("status", help="show shared runtime status")
    runtime_subs.add_parser("create", help="create the shared runtime")
    runtime_plan = runtime_subs.add_parser("plan", help="plan an offline deployment (read-only)")
    runtime_plan.add_argument(
        "--release-dir", required=True, type=Path, dest="release_dir",
        help="path to the offline release directory",
    )
    runtime_apply = runtime_subs.add_parser("apply", help="apply an offline deployment")
    runtime_apply.add_argument(
        "--release-dir", required=True, type=Path, dest="release_dir",
        help="path to the offline release directory",
    )
    runtime_subs.add_parser("rollback", help="rollback the shared runtime")

    # -- products subcommand (M1-2A) -----------------------------------------
    products_parser = subparsers.add_parser("products", help="show product catalog state")
    products_parser.add_argument(
        "product_id", nargs="?",
        help="optional product id to inspect",
    )


    # -- install subcommand (D.4.1G) -----------------------------------------
    install_parser = subparsers.add_parser(
        "install", help="install a product from its remote source",
    )
    install_parser.add_argument("product_id", help="product id to install")
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

    if args.command == "launch":
        return _handle_launch(args, stdout=stdout)

    if args.command == "runtime":
        return _handle_runtime(args, stdout=stdout)

    if args.command == "products":
        return _handle_products(args, stdout=stdout)

    if args.command == "install":
        return _handle_install(args, stdout=stdout)

    print(startup_message(), file=stdout)
    return 0


# ---------------------------------------------------------------------------
# M1-0A: launch handler
# ---------------------------------------------------------------------------


def _handle_launch(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie launch <component_id> [--timeout SECONDS]``."""
    service = _make_service()

    try:
        result = service.launch_component(
            args.component_id,
            timeout_seconds=args.timeout_seconds,
        )
    except UnknownComponentError:
        available = ", ".join(default_registry().available_ids()) or "none"
        print(
            f"Unknown component: {args.component_id}. Available components: {available}",
            file=sys.stderr,
        )
        return 5
    except LaunchPreparationError as exc:
        print(f"cannot launch {args.component_id!r}: {exc}", file=sys.stderr)
        return 6
    except LaunchError as exc:
        print(f"cannot launch {args.component_id!r}: {exc}", file=sys.stderr)
        return 6

    _print_launch_result(result, stdout=stdout)
    if result.timed_out:
        return 10
    return result.return_code


def _print_launch_result(result: LaunchResult, *, stdout: TextIO) -> None:
    """Print launch output to stdout, stderr to stderr."""
    if result.stdout:
        print(result.stdout, end="", file=stdout)
    if result.stderr:
        print(result.stderr, end="", file=sys.stderr)
    if result.timed_out:
        print("launch timed out", file=sys.stderr)


# ---------------------------------------------------------------------------
# M0-9.3: runtime handler
# ---------------------------------------------------------------------------


def _handle_runtime(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie runtime ...`` commands."""
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

    if args.runtime_command == "plan":
        service = _make_service()
        try:
            plan = service.plan_offline_deployment(args.release_dir)
            print(_format_deployment_plan(plan), file=stdout)
            if plan.blocked:
                return 1
            return 0
        except OfflineReleaseError as exc:
            print(f"plan failed: {exc}", file=sys.stderr)
            return 4

    if args.runtime_command == "apply":
        service = _make_service()
        try:
            result = service.apply_offline_deployment(args.release_dir)
            print(_format_deployment_result(result), file=stdout)
            return 0 if result.success else 3
        except OfflineReleaseError as exc:
            print(f"apply failed: {exc}", file=sys.stderr)
            return 4

    if args.runtime_command == "rollback":
        service = _make_service()
        status = service.rollback_runtime()
        print(_format_runtime_status(status), file=stdout)
        if (status.state == RuntimeState.READY
                and status.reason_code == RuntimeReasonCode.RUNTIME_READY):
            return 0
        return 3

    # No runtime subcommand given → show help.
    return 0


# ---------------------------------------------------------------------------
# M1-2A: products handler
# ---------------------------------------------------------------------------


def _handle_products(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie products [<product_id>]``."""
    service = _make_service()

    if args.product_id:
        try:
            state = service.get_product_state(args.product_id)
        except UnknownProductError:
            catalog_ids = ", ".join(service.catalog.available_ids()) or "none"
            print(
                f"Unknown product: {args.product_id}. Known products: {catalog_ids}",
                file=sys.stderr,
            )
            return 2
        print(_format_product_state(state), file=stdout)
        return 0
    else:
        shell_state = service.collect_product_state()
        print(_format_product_shell_state(shell_state), file=stdout)
        return 0


def _format_product_shell_state(shell: ProductShellState) -> str:
    """Format a ProductShellState for CLI output."""
    lines = [
        "Product shell state:",
        f" Runtime state: {shell.runtime_state.value}",
        f" Runtime root: {shell.runtime_root}",
        f" Known products: {len(shell.products)}",
        f" Managed: {shell.managed_count}",
        f" Installed: {shell.installed_count}",
        "",
    ]
    for p in shell.products:
        lines.append(f" {p.product_id} ({p.display_name}):")
        lines.append(f"  Managed: {p.managed.value}")
        lines.append(f"  Installed: {'yes' if p.installed else 'no'}")
        if p.version:
            lines.append(f"  Version: {p.version}")
        lines.append(f"  Launchable: {'yes' if p.launchable else 'no'}")
        lines.append(f"  Reason code: {p.reason_code.value}")
        lines.append(f"  Reason: {p.reason}")
        lines.append("")
    return "\n".join(lines)


def _format_product_state(state: ProductState) -> str:
    """Format a single ProductState for CLI output."""
    lines = [
        f"Product: {state.product_id} ({state.display_name})",
        f" Managed: {state.managed.value}",
        f" Installed: {'yes' if state.installed else 'no'}",
    ]
    if state.version:
        lines.append(f" Version: {state.version}")
    lines.append(f" Launchable: {'yes' if state.launchable else 'no'}")
    lines.append(f" Reason code: {state.reason_code.value}")
    lines.append(f" Reason: {state.reason}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# D.4.1G: install handler
# ---------------------------------------------------------------------------


def _make_install_deps():
    """Return default install dependencies (resolver, fetcher, work_root).

    Tests monkeypatch this function or the private helpers
    ``_make_source_resolver`` and ``_make_source_fetcher`` to avoid
    any real network access.
    """
    return (
        _make_source_resolver(),
        _make_source_fetcher(),
        _make_work_root(),
    )


def _make_source_resolver():
    """Return a default :class:`SourceRefResolver`."""
    from .sources.github import GitHubSourceRefResolver

    return GitHubSourceRefResolver()


def _make_source_fetcher():
    """Return a default :class:`ArchiveFetcher`."""
    from .sources.github import GitHubArchiveFetcher

    return GitHubArchiveFetcher()


def _make_work_root() -> Path:
    """Return the default install work root."""
    return default_install_work_root()


def _handle_install(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie install <product_id>``."""
    service = _make_service()
    resolver, fetcher, work_root = _make_install_deps()

    # Ensure work root exists so the service can stage artifacts.
    work_root.mkdir(parents=True, exist_ok=True)

    try:
        result = service.install_product(
            args.product_id,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
    except UnknownProductError:
        catalog_ids = ", ".join(service.catalog.available_ids()) or "none"
        print(
            f"Unknown product: {args.product_id}. Known products: {catalog_ids}",
            file=sys.stderr,
        )
        return 2
    except RemoteSourceUnavailableError as exc:
        print(f"cannot install {args.product_id!r}: {exc}", file=sys.stderr)
        return 7
    except SourceResolutionError as exc:
        print(f"cannot resolve source for {args.product_id!r}: {exc}", file=sys.stderr)
        return 8
    except AcquisitionError as exc:
        print(f"cannot fetch source for {args.product_id!r}: {exc}", file=sys.stderr)
        return 9
    except (
        ArtifactRejectionError, CorruptSelectionError, ProductInstallPreparationError,
        ProductDeploymentPlanningError, PlanningError,
    ) as exc:
        print(f"install failed for {args.product_id!r}: {exc}", file=sys.stderr)
        return 3

    print(_format_deployment_result(result), file=stdout)
    return 0 if result.success else 3
# Helpers
# ---------------------------------------------------------------------------


def _format_runtime_status(st: RuntimeStatus) -> str:
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


def _format_deployment_plan(plan: DeploymentPlan) -> str:
    """Format a DeploymentPlan for CLI output."""
    lines = [
        f"Deployment plan:",
        f" Runtime state: {plan.runtime_state.value}",
    ]
    if plan.source_active_slot_id:
        lines.append(f" Source active slot: {plan.source_active_slot_id}")
    if plan.source_previous_slot_id:
        lines.append(f" Source previous slot: {plan.source_previous_slot_id}")
    if plan.blocked:
        lines.append(f" Blocked: {plan.blocked_reason or 'yes'}")
    lines.append("")
    lines.append("Components:")
    for step in plan.steps:
        lines.extend(_format_deployment_step(step))
    return "\n".join(lines)


def _format_deployment_step(step: DeploymentStep) -> list[str]:
    """Format a single DeploymentStep."""
    parts = [
        f" {step.component_id}:",
        f"  Action: {step.action.value}",
        f"  Desired version: {step.desired_version}",
    ]
    if step.current_version is not None:
        parts.append(f"  Current version: {step.current_version}")
    if step.reason_code is not None:
        parts.append(f"  Reason code: {step.reason_code.value}")
    if step.reason is not None:
        parts.append(f"  Reason: {step.reason}")
    return parts


def _format_deployment_result(result: DeploymentResult) -> str:
    """Format a DeploymentResult for CLI output."""
    lines = ["Deployment result:"]
    if result.success:
        lines.append(" Success: yes")
        if result.active_slot_id:
            lines.append(f" Active slot: {result.active_slot_id}")
        if result.previous_slot_id:
            lines.append(f" Previous slot: {result.previous_slot_id}")
    else:
        lines.append(" Success: no")
        if result.reason:
            lines.append(f" Reason: {result.reason}")
    return "\n".join(lines)


def _make_service() -> ZeAlfieService:
    """Construct the default application service.

    Isolated into a private factory so tests can monkeypatch without
    touching the real user runtime.
    """
    return ZeAlfieService(
        registry=default_registry(),
        runtime=SharedRuntime(default_runtime_layout()),
    )


def main(argv: Sequence[str] | None = None) -> int:
    try:
        return run(argv)
    except SystemExit as exc:
        code = exc.code
        if isinstance(code, int):
            return code
        return 1
