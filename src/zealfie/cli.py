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
    ProductChannelUnavailableError,
    ProductDeploymentPlanningError,
    ProductDependencyAcquisitionError,
    OfflineReleaseError,
    ProductInstallPreparationError,
    ProductPolicy,
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
from .acceleration import AcceleratedPlanStatus, HostPrerequisiteStatus
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
    GcPlan,
    GcResult,
    GcStatus,
    SlotCategory,
    PlanningError,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
    RuntimeMutationBusyError,
    RuntimeMutationLock,
    RuntimeMutationLockError,
    SharedRuntime,
    SharedRuntimeError,
    apply_gc_plan,
    build_gc_plan,
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


# ---------------------------------------------------------------------------
# ZA-M1-2L: mutation-lock exit codes and refusal formatting
# ---------------------------------------------------------------------------

#: Exit code for a refused mutation because another ZeAlfie writer holds
#: the runtime mutation lease (D8).  Used by every mutating handler where
#: exit code 4 is free: ``runtime create`` (0/3), ``runtime rollback``
#: (0/3), ``runtime gc`` (0/1/2/3), ``install`` (2/3/7/8/9/11).
BUSY_EXIT = 4

#: BUSY exit code for the handlers where 4 is already taken by another
#: meaning (D8): ``runtime apply`` (4 = OfflineReleaseError) and
#: ``system gpu-install`` (4 = plan failure).
BUSY_EXIT_ALT = 5

#: Exit code when the mutation lock primitive itself fails (D6, fail
#: closed).  6 is free in every mutating handler (``launch`` uses 6, but
#: launch never acquires the lock and the contract is per-handler).
LOCK_ERROR_EXIT = 6


def _format_mutation_busy(exc: RuntimeMutationBusyError) -> str:
    """Format a refused mutation for the user — clean message, no traceback.

    Mission ZA-M1-2L §24 format: "Runtime mutation refused:" / "Status:
    BUSY" / "Current operation: <op>" (diagnostic only) / "No changes have
    been applied."
    """
    lines = ["Runtime mutation refused:"]
    lines.append(f" {exc}")
    lines.append("Status: BUSY")
    if exc.operation:
        lines.append(f"Current operation: {exc.operation}")
    if exc.pid is not None:
        lines.append(f"Owner pid: {exc.pid}")
    lines.append("No changes have been applied.")
    return "\n".join(lines)


def _format_mutation_lock_unavailable(exc: RuntimeMutationLockError) -> str:
    """Format a lock-primitive failure (fail closed, never mutate)."""
    return f"Runtime mutation lock unavailable: {exc}"


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
    runtime_subs.add_parser(
        "gc-plan",
        help="preview safe runtime garbage collection (read-only)",
    )
    runtime_subs.add_parser(
        "gc",
        help="apply safe runtime garbage collection",
    )

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
    install_policy_group = install_parser.add_mutually_exclusive_group()
    install_policy_group.add_argument(
        "--channel",
        dest="channel",
        help="discovery channel to follow for this product "
             "(mutually exclusive with --pin)",
    )
    install_policy_group.add_argument(
        "--pin",
        dest="pin_sha",
        help="exact 40-hex commit SHA to install "
             "(mutually exclusive with --channel)",
    )

    # -- system subcommand (M1-2G) -----------------------------------------
    system_parser = subparsers.add_parser(
        "system", help="inspect host system capabilities",
    )
    system_subs = system_parser.add_subparsers(dest="system_command")
    system_subs.add_parser(
        "capabilities",
        help="show host capabilities and acceleration recommendation "
             "(read-only, no mutation)",
    )
    system_subs.add_parser(
        "gpu-plan",
        help="preview the accelerated GPU deployment plan "
             "(read-only, no mutation)",
    )
    system_subs.add_parser(
        "gpu-install",
        help="install an accelerated GPU runtime "
             "(fail-closed: no accelerated artifact source is configured; "
             "a real deployment requires explicit authorization)",
    )
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

    if args.command == "system":
        return _handle_system(args, stdout=stdout)

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
        except RuntimeMutationBusyError as exc:
            # D8: BUSY = 4 (free for runtime create: 0/3).
            print(_format_mutation_busy(exc), file=sys.stderr)
            return BUSY_EXIT
        except RuntimeMutationLockError as exc:
            print(_format_mutation_lock_unavailable(exc), file=sys.stderr)
            return LOCK_ERROR_EXIT
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
        except RuntimeMutationBusyError as exc:
            # D8: BUSY = 5 (4 is OfflineReleaseError for runtime apply).
            print(_format_mutation_busy(exc), file=sys.stderr)
            return BUSY_EXIT_ALT
        except RuntimeMutationLockError as exc:
            print(_format_mutation_lock_unavailable(exc), file=sys.stderr)
            return LOCK_ERROR_EXIT
        except OfflineReleaseError as exc:
            print(f"apply failed: {exc}", file=sys.stderr)
            return 4

    if args.runtime_command == "rollback":
        service = _make_service()
        try:
            status = service.rollback_runtime()
        except RuntimeMutationBusyError as exc:
            # D8: BUSY = 4 (free for runtime rollback: 0/3).
            print(_format_mutation_busy(exc), file=sys.stderr)
            return BUSY_EXIT
        except RuntimeMutationLockError as exc:
            print(_format_mutation_lock_unavailable(exc), file=sys.stderr)
            return LOCK_ERROR_EXIT
        print(_format_runtime_status(status), file=stdout)
        if (status.state == RuntimeState.READY
                and status.reason_code == RuntimeReasonCode.RUNTIME_READY):
            return 0
        return 3

    if args.runtime_command == "gc-plan":
        return _handle_runtime_gc_plan(args, stdout=stdout)

    if args.runtime_command == "gc":
        return _handle_runtime_gc(args, stdout=stdout)

    # No runtime subcommand given → show help.
    return 0


# ---------------------------------------------------------------------------
# ZA-M1-2K: safe runtime GC handlers
# ---------------------------------------------------------------------------


def _handle_runtime_gc_plan(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie runtime gc-plan`` — STRICTLY read-only preview.

    Prints the plan (status, active/previous, one paragraph per slot,
    recoverable total, blocking reasons, stale-metadata warnings).
    Exit code 0 when the plan is READY, 1 when BLOCKED (same convention
    as ``runtime plan`` for blocked previews).  Never mutates anything.

    ZA-M1-2L (D9): probes the mutation lock (try-acquire + immediate
    release, strictly read-only, never raises); when a writer is active,
    an honest warning line is printed to stderr — the snapshot may change.
    Exit codes are unchanged.
    """
    layout = default_runtime_layout()
    plan = build_gc_plan(layout.root)
    busy = RuntimeMutationLock(layout.root).probe_busy()
    if busy is not None:
        operation = busy.get("operation")
        pid = busy.get("pid")
        print(
            f"Warning: runtime mutation in progress (operation={operation}, "
            f"pid={pid}). Snapshot may change.",
            file=sys.stderr,
        )
    print(_format_gc_plan(plan), file=stdout)
    return 0 if plan.status == GcStatus.READY else 1


def _handle_runtime_gc(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie runtime gc``.

    Builds a fresh plan, refuses to apply when BLOCKED, then hands the
    plan to :func:`apply_gc_plan` (which re-plans and stale-checks
    itself).  No interactive prompt — the human gate is the validation.
    Exit codes: 0 success, 1 fresh plan BLOCKED, 2 stale plan refused,
    3 apply completed with per-slot/metadata errors, 4 mutation BUSY
    (D8), 6 mutation lock unavailable (fail closed).
    """
    layout = default_runtime_layout()
    plan = build_gc_plan(layout.root)
    if plan.status == GcStatus.BLOCKED:
        print(_format_gc_plan(plan), file=stdout)
        return 1
    try:
        result = apply_gc_plan(layout.root, plan)
    except RuntimeMutationBusyError as exc:
        # D8: BUSY = 4 (free for runtime gc: 0/1/2/3).
        print(_format_mutation_busy(exc), file=sys.stderr)
        return BUSY_EXIT
    except RuntimeMutationLockError as exc:
        print(_format_mutation_lock_unavailable(exc), file=sys.stderr)
        return LOCK_ERROR_EXIT
    print(_format_gc_result(result), file=stdout)
    if result.stale:
        return 2
    if result.errors:
        return 3
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
        lines = _format_product_state(state)
        policy_lines = _format_product_channels_and_policy(
            service, args.product_id,
        )
        if policy_lines:
            lines += "\n" + policy_lines
        print(lines, file=stdout)
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


def _format_product_channels_and_policy(
    service, product_id: str,
) -> str:
    """Return a lightweight channels/policy display for a product.

    Tolerates service fakes that predate the Phase 5 policy API by returning
    ``""`` when the relevant methods are unavailable.
    """
    get_channels = getattr(service, "available_product_channels", None)
    get_policy = getattr(service, "product_policy", None)
    if not callable(get_channels) or not callable(get_policy):
        return ""
    try:
        channels = get_channels(product_id)
        policy = get_policy(product_id)
    except Exception:
        return ""

    lines: list[str] = []
    if channels:
        channel_text = ", ".join(
            f"{channel} -> {ref}" for channel, ref in channels
        )
    else:
        channel_text = "none"
    lines.append(f" Available channels: {channel_text}")
    if policy.policy == "pin":
        lines.append(f" Policy: pin (sha {policy.pin_sha})")
    else:
        lines.append(f" Policy: follow (channel {policy.channel})")
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
    """Handle ``zealfie install <product_id> [--channel C | --pin SHA]``."""
    service = _make_service()
    resolver, fetcher, work_root = _make_install_deps()

    # Ensure work root exists so the service can stage artifacts.
    work_root.mkdir(parents=True, exist_ok=True)

    # Persist the requested policy before installing so the same config is
    # observed by later update checks (GUI/CLI) and by the install
    # orchestration itself (which reads the persisted policy).  Policy-value
    # errors (invalid pin SHA, undeclared channel) are surfaced here, before
    # any install work, with clean messages.
    try:
        if args.channel:
            service.set_product_channel(args.product_id, args.channel)
        elif args.pin_sha:
            service.set_product_policy(
                ProductPolicy(
                    product_id=args.product_id,
                    policy="pin",
                    pin_sha=args.pin_sha,
                )
            )
    except UnknownProductError:
        catalog_ids = ", ".join(service.catalog.available_ids()) or "none"
        print(
            f"Unknown product: {args.product_id}. Known products: {catalog_ids}",
            file=sys.stderr,
        )
        return 2
    except ProductChannelUnavailableError as exc:
        print(f"cannot install {args.product_id!r}: {exc}", file=sys.stderr)
        return 3
    except RemoteSourceUnavailableError as exc:
        print(f"cannot install {args.product_id!r}: {exc}", file=sys.stderr)
        return 7
    except ValueError as exc:
        # Invalid pin_sha (or other policy-value failure) surfaced cleanly.
        print(f"invalid policy for {args.product_id!r}: {exc}", file=sys.stderr)
        return 3

    try:
        result = service.install_product(
            args.product_id,
            resolver=resolver,
            fetcher=fetcher,
            work_root=work_root,
        )
    except RuntimeMutationBusyError as exc:
        # D8: BUSY = 4 (free for install: 2/3/7/8/9/11).
        print(_format_mutation_busy(exc), file=sys.stderr)
        return BUSY_EXIT
    except RuntimeMutationLockError as exc:
        print(_format_mutation_lock_unavailable(exc), file=sys.stderr)
        return LOCK_ERROR_EXIT
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
    except ProductDependencyAcquisitionError as exc:
        print(f"cannot acquire dependencies for {args.product_id!r}: {exc}", file=sys.stderr)
        return 11
    except (
        ArtifactRejectionError, CorruptSelectionError, ProductInstallPreparationError,
        ProductDeploymentPlanningError, PlanningError,
    ) as exc:
        print(f"install failed for {args.product_id!r}: {exc}", file=sys.stderr)
        return 3

    print(_format_deployment_result(result), file=stdout)
    return 0 if result.success else 3


# ---------------------------------------------------------------------------
# ZA-M1-2J Phase D: gpu-install handler (transactional, real artifact source)
# ---------------------------------------------------------------------------


def _handle_gpu_install(*, stdout: TextIO) -> int:
    """Handle ``zealfie system gpu-install``.

    Builds the read-only accelerated plan first; a non-``PLAN_READY``
    plan is reported honestly with a non-zero exit and performs NO
    acquisition and NO runtime work.  A ``PLAN_READY`` plan is handed
    to :meth:`~zealfie.app.service.ZeAlfieService.install_accelerated_runtime`
    with the service default acquirer (the packaged artifact manifest)
    and a simple progress line per phase on stdout.

    Exit codes: 0 success, 3 deployment failure, 4 plan failure, 5
    mutation BUSY (D8), 6 mutation lock unavailable (fail closed).  The composition
    root's archive fetcher and install work root (the existing
    ``_make_install_deps`` factories — same transports as the normal
    install path, no new transport) are transmitted so the service can
    re-acquire the KEEP base runtime at the exact provenance commit
    SHA; without them the transactional install fails closed at
    PREPARE with "no artifact fetcher configured".  The final result is
    always reported honestly (success or the phase where the deployment
    stopped).
    """
    service = _make_service()
    try:
        plan = service.build_accelerated_deployment_plan()
    except Exception as exc:
        print(f"gpu install failed: {exc}", file=sys.stderr)
        return 4

    if plan.status is not AcceleratedPlanStatus.PLAN_READY:
        detail = plan.blocked_reason or "no accelerated deployment planned"
        print(
            "accelerated GPU deployment is not available: plan status "
            f"{plan.status.value}: {detail}",
            file=sys.stderr,
        )
        return 4

    # Production wiring (ZA-M1-2J.1): the transactional install needs
    # the same composition-root transports as ``install`` — the archive
    # fetcher for KEEP re-acquisition at the exact provenance SHA and
    # the platform install work root.  Reuse the existing factories;
    # never construct a new transport here and never let the engine
    # reach for GitHub itself.
    _resolver, fetcher, work_root = _make_install_deps()
    work_root.mkdir(parents=True, exist_ok=True)

    def _progress(progress) -> None:
        percent = getattr(progress, "percent", None)
        message = getattr(progress, "message", "")
        label = f"{percent}%" if isinstance(percent, int) else "..."
        print(f"  [{label}] {message}", file=stdout)

    try:
        result = service.install_accelerated_runtime(
            plan=plan,
            fetcher=fetcher,
            work_root=work_root,
            progress_callback=_progress,
        )
    except RuntimeMutationBusyError as exc:
        # D8: BUSY = 5 (4 is plan failure for gpu-install).
        print(_format_mutation_busy(exc), file=sys.stderr)
        return BUSY_EXIT_ALT
    except RuntimeMutationLockError as exc:
        print(_format_mutation_lock_unavailable(exc), file=sys.stderr)
        return LOCK_ERROR_EXIT
    except Exception as exc:
        print(f"gpu install failed: {exc}", file=sys.stderr)
        return 4

    print(_format_accelerated_deployment_result(result), file=stdout)
    return 0 if result.success else 3


def _format_accelerated_deployment_result(result) -> str:
    """Format an AcceleratedDeploymentResult for CLI output."""
    lines = ["Accelerated deployment result:"]
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


# ---------------------------------------------------------------------------
# M1-2G: system capabilities handler (read-only)
# ---------------------------------------------------------------------------


def _handle_system(args, *, stdout: TextIO) -> int:
    """Handle ``zealfie system capabilities``, ``gpu-plan``, ``gpu-install``."""
    if args.system_command == "capabilities":
        service = _make_service()
        capabilities = service.collect_host_capabilities()
        recommendation = service.get_acceleration_recommendation(capabilities)
        print(
            _format_host_capabilities(capabilities, recommendation),
            file=stdout,
        )
        return 0

    if args.system_command == "gpu-plan":
        service = _make_service()
        try:
            plan = service.build_accelerated_deployment_plan()
        except Exception as exc:
            print(f"gpu plan failed: {exc}", file=sys.stderr)
            return 4
        print(_format_accelerated_deployment_plan(plan), file=stdout)
        return 0

    if args.system_command == "gpu-install":
        return _handle_gpu_install(stdout=stdout)

    # No system subcommand given → show help.
    return 0


def _format_host_capabilities(capabilities, recommendation) -> str:
    """Format host capabilities + acceleration recommendation for CLI output."""
    lines = [
        "Host capabilities:",
        f" OS: {capabilities.os_name or 'unknown'}",
        f" CPU architecture: {capabilities.cpu_arch or 'unknown'}",
        f" Platform status: {capabilities.platform_status.value}",
        f" GPUs: {capabilities.gpu_count}",
    ]
    for gpu in capabilities.gpus:
        model = gpu.model or gpu.vendor
        driver = f", driver {gpu.driver_version}" if gpu.driver_version else ""
        lines.append(
            f"  - {gpu.vendor} {model} ({gpu.kind.value.lower()}{driver})"
        )
    lines.append("")
    lines.append(
        f"Acceleration recommendation: {recommendation.status.value}"
    )
    lines.append(f" Backend: {recommendation.backend}")
    lines.append(f" Reason: {recommendation.reason}")
    if recommendation.reason_code is not None:
        lines.append(f" Reason code: {recommendation.reason_code.value}")
    return "\n".join(lines)


def _format_accelerated_deployment_plan(plan) -> str:
    """Format an accelerated GPU deployment plan for CLI output.

    Pure and deterministic: reads only the plan fields (the planner
    already sorts every collection).  A blocked plan is a preview, not
    an error — the summary reports the blocked reason honestly.
    """
    lines = [
        "Accelerated GPU deployment plan:",
        f" Status: {plan.status.value}",
        f" Hardware status: {plan.hardware.status.value}",
        f" Hardware reason: {plan.hardware.reason}",
    ]
    if plan.backend is not None:
        lines.append(f" Backend: {plan.backend}")
    else:
        lines.append(" Backend: none")
    if plan.products_concerned:
        lines.append(
            f" Products concerned: {', '.join(plan.products_concerned)}"
        )
    else:
        lines.append(" Products concerned: none")
    if plan.keep_products:
        for keep in plan.keep_products:
            commit = keep.commit_sha or "unknown"
            lines.append(
                f" KEEP {keep.product_id} version {keep.version} "
                f"(commit {commit})"
            )
    else:
        lines.append(" KEEP products: none")
    if plan.added_requirements:
        for entry in plan.added_requirements:
            specifier = entry.specifier or "any version"
            extras = (
                f" extras [{', '.join(entry.extras)}]" if entry.extras else ""
            )
            variant_version = (
                entry.variant.version if entry.variant is not None else "none"
            )
            lines.append(
                f" Accelerated dependency: {entry.distribution}{extras} "
                f"({specifier}) [variant version {variant_version}]"
            )
    else:
        lines.append(" Accelerated dependencies: none")
    if plan.host_prerequisites is not None:
        lines.append(" Host prerequisites:")
        for entry in plan.host_prerequisites.required_host:
            observed = (
                f" (observed {entry.observed})" if entry.observed else ""
            )
            status = (
                ""
                if entry.status is HostPrerequisiteStatus.OK
                else f" [{entry.status.display}]"
            )
            lines.append(
                f"  - REQUIRED_HOST {entry.entry} "
                f"{entry.requirement}{observed}{status}"
            )
        for entry in plan.host_prerequisites.managed_runtime:
            lines.append(
                f"  - MANAGED_RUNTIME {entry.entry} {entry.requirement}"
            )
    if plan.blocked:
        lines.append(f" Blocked: {plan.blocked_reason or 'yes'}")
    if plan.closure_impact:
        lines.append(" Closure impact:")
        lines.extend(f"  - {impact}" for impact in plan.closure_impact)
    else:
        lines.append(" Closure impact: none")
    lines.append("No changes have been applied (read-only preview).")
    return "\n".join(lines)


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


def _format_gc_plan(plan: GcPlan) -> str:
    """Format a GcPlan for CLI output (pure, deterministic)."""
    lines = [
        "Safe runtime GC plan:",
        f" Status: {plan.status.value}",
        f" Runtime root: {plan.runtime_root}",
    ]
    if plan.active_slot_id:
        lines.append(f" Active slot: {plan.active_slot_id}")
    if plan.previous_slot_id:
        lines.append(f" Previous slot: {plan.previous_slot_id}")
    lines.append("")
    if plan.slots:
        lines.append("Slots:")
        for entry in plan.slots:
            if entry.category in (
                SlotCategory.PRUNABLE,
                SlotCategory.PRUNABLE_CLEAN_METADATA,
            ):
                action = "PRUNE"
            elif entry.category in (
                SlotCategory.ACTIVE,
                SlotCategory.PREVIOUS,
                SlotCategory.REFERENCED,
            ):
                action = "KEEP"
            else:
                action = "BLOCKED"
            refs = ", ".join(entry.references) or "none"
            lines.append(f" - {entry.slot_id}:")
            lines.append(f"    Action: {action}")
            lines.append(f"    Category: {entry.category.value}")
            lines.append(f"    Reason: {entry.reason}")
            lines.append(f"    References: {refs}")
            lines.append(f"    Estimated bytes: {entry.estimated_bytes}")
            if entry.metadata_actions:
                lines.append(
                    f"    Metadata actions: {', '.join(entry.metadata_actions)}"
                )
    else:
        lines.append("Slots: none")
    lines.append("")
    lines.append(
        f" Total recoverable bytes (estimated): {plan.total_recoverable_bytes}"
    )
    if plan.blocking_reasons:
        lines.append(" Blocking reasons:")
        for reason in plan.blocking_reasons:
            lines.append(f"  - {reason}")
    else:
        lines.append(" Blocking reasons: none")
    if plan.stale_metadata:
        lines.append(" Stale metadata warnings:")
        for warning in plan.stale_metadata:
            lines.append(f"  - {warning}")
    lines.append("No changes have been applied (read-only preview).")
    return "\n".join(lines)


def _format_gc_result(result: GcResult) -> str:
    """Format a GcResult for CLI output."""
    lines = [
        "Safe runtime GC result:",
        f" Stale plan: {'yes' if result.stale else 'no'}",
    ]
    if result.deleted_slots:
        lines.append(f" Deleted slots: {', '.join(result.deleted_slots)}")
    else:
        lines.append(" Deleted slots: none")
    lines.append(
        f" Reclaimed bytes (estimated): {result.reclaimed_bytes}"
    )
    if result.preserved_slots:
        lines.append(
            f" Preserved slots: {', '.join(result.preserved_slots)}"
        )
    else:
        lines.append(" Preserved slots: none")
    if result.errors:
        lines.append(" Errors:")
        for error in result.errors:
            lines.append(f"  - {error}")
    else:
        lines.append(" Errors: none")
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
