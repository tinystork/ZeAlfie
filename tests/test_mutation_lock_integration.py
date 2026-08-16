"""Mutation-lock integration matrix (ZA-M1-2L Phase D, mission §33 items 10-34).

Covers the integration of the runtime mutation lease into every production
mutator (the 8 D1 acquisition points), the D2 low-level contract, the
CLI/GUI BUSY handling, and the §30 synthetic multiprocess E2E.  The lock is
a *defence additive*: every existing guard (stale transaction, GC
fingerprint, atomic writes, active/previous protection) is still exercised
and asserted here.

Matrix → test mapping (§33):

  (10)  test_runtime_create_busy_no_mutation
  (11)  test_deployment_apply_busy_no_candidate
  (12)  test_activate_without_lease_raises_lease_required
        test_activate_with_foreign_root_lease_raises_lease_required
  (13)  test_rollback_busy_pointer_unchanged
  (14)  test_discard_busy_directory_intact
  (15)  test_gc_busy_zero_deletion_zero_metadata_write
  (16)  test_product_install_busy
  (17)  test_product_update_busy
  (18)  test_gpu_install_busy
  (19-23) test_product_mutation_busy_leaves_state_stores_unchanged
  (24)  test_gc_busy_zero_deletion_zero_metadata_write (merged with 15)
  (25)  test_stale_transaction_still_enforced_under_lease
  (26)  test_stale_gc_plan_still_enforced_under_lease
  (27)  test_rollback_ping_pong_semantics_unchanged_under_lease
  (28)  pre-activation GPU compute gate unchanged — covered by the
        existing gate suites (tests/test_accelerated_deployment_engine.py,
        tests/test_acceleration_compatibility.py), asserted green in
        validation; not duplicated here.
  (29)  test_runtime_status_available_during_busy
  (30)  test_cli_gc_plan_warning_during_busy
  (31)  test_cli_busy_exit_codes_and_messages (create/rollback/gc=4,
        install=4, apply=5, gpu-install=5) + test_cli_lock_unavailable_exit_6
  (32)  test_gui_install_worker_busy_mapping
        test_gui_install_worker_lock_unavailable_mapping
        test_gui_accelerated_worker_busy_mapping
  (33)  exactly-one-owner stress — proven in tests/test_mutation_lock.py
        (Phase B+C, referenced only, not duplicated).
  (34)  test_lease_released_on_exception_integrated_flow

§30 synthetic multiprocess E2E:
  test_e2e_multiprocess_busy_then_recovery

§20 two concurrent synthetic flows (single owner, no divergent provenance):
  test_two_concurrent_product_flows_single_owner

Synchronization discipline (mission §35): pipe / READY handshakes for
subprocesses, ``threading.Event`` for threads — **zero sleeps** used as a
synchronization mechanism.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import subprocess
import sys
import textwrap
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from zealfie.app import ZeAlfieService
from zealfie.acceleration import AcceleratedPlanStatus
from zealfie.components.model import EntryPointContract
from zealfie.components.registry import ComponentRegistry
from zealfie.products.catalog import ProductCatalog, ProductDescriptor
from zealfie.products.selection import SelectionStore
from zealfie.releases.model import VerifiedArtifact
from zealfie.runtime import (
    DeploymentAction,
    DeploymentPlan,
    DeploymentReasonCode,
    DeploymentStep,
    DesiredComponent,
    DesiredRuntimeState,
    OPERATION_GPU_INSTALL,
    OPERATION_PRODUCT_INSTALL,
    OPERATION_PRODUCT_UPDATE,
    OPERATION_RUNTIME_APPLY,
    OPERATION_RUNTIME_CREATE,
    OPERATION_RUNTIME_DISCARD,
    OPERATION_RUNTIME_GC,
    OPERATION_RUNTIME_ROLLBACK,
    RuntimeLayout,
    RuntimeMutationBusyError,
    RuntimeMutationLeaseRequired,
    RuntimeMutationLock,
    RuntimeMutationLockError,
    RuntimeReasonCode,
    RuntimeState,
    SharedRuntime,
    apply_deployment_plan,
    apply_gc_plan,
    build_gc_plan,
    load_active_state,
    save_active_state,
)
from zealfie.runtime import mutation_lock as mutation_lock_module
from zealfie.sources import RemoteSource, SourceResolutionError

needs_posix = pytest.mark.skipif(
    os.name != "posix", reason="fcntl.flock backend is POSIX-only"
)

BUSY_MESSAGE_CORE = (
    "Runtime is busy with another ZeAlfie mutation. "
    "No changes have been applied."
)

ACTIVE_ID = "rt-aaa111111111"
PREV_ID = "rt-bbb222222222"
ORPHAN_ID = "rt-000000000001"
ORPHAN2_ID = "rt-000000000002"

try:
    from PySide6.QtCore import QObject, Signal  # noqa: F401
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False


# ---------------------------------------------------------------------------
# Synthetic runtime helpers (hermetic — no venv, no pip)
# ---------------------------------------------------------------------------


def _write_active_state(layout: RuntimeLayout, active_id, previous_id=None) -> None:
    save_active_state(layout.active_pointer, active_id, previous_id)


def _make_slot_dir(layout: RuntimeLayout, slot_id: str, *, python_link: bool = False) -> Path:
    path = layout.slot_path(slot_id)
    path.mkdir(parents=True, exist_ok=True)
    if python_link:
        bin_dir = path / "bin"
        bin_dir.mkdir(exist_ok=True)
        target = bin_dir / "python"
        if not target.exists():
            target.symlink_to(sys.executable)
    return path


def _synthesize_ready_runtime(
    tmp_path: Path,
    *,
    active_id: str = ACTIVE_ID,
    previous_id: str | None = None,
    orphans: tuple[str, ...] = (),
    python_link: bool = False,
) -> RuntimeLayout:
    """Create a synthetic READY runtime root without venv creation."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    _write_active_state(layout, active_id, previous_id)
    _make_slot_dir(layout, active_id, python_link=python_link)
    if previous_id is not None:
        _make_slot_dir(layout, previous_id, python_link=python_link)
    for orphan in orphans:
        _make_slot_dir(layout, orphan)
    return layout


def _write_accelerated_metadata(layout: RuntimeLayout, slot_id: str) -> bytes:
    """Write an observational accelerated-metadata record referencing one slot."""
    payload = {
        "schema_version": 1,
        "slots": {slot_id: {"backend": "cuda", "probed": True}},
    }
    raw = (json.dumps(payload, sort_keys=True) + "\n").encode("utf-8")
    layout.state_dir.mkdir(parents=True, exist_ok=True)
    (layout.state_dir / "accelerated-metadata.json").write_bytes(raw)
    return raw


def _blocked_plan() -> DeploymentPlan:
    """A valid, blocked DeploymentPlan (fails before any mutation)."""
    fake = VerifiedArtifact(
        component_id="zewitness",
        version="0.0.1",
        path=Path("/fake/witness.whl"),
        size=100,
        sha256="a" * 64,
        distribution_name="zealfie-witness",
        wheel_version="0.0.1",
    )
    desired = DesiredRuntimeState(
        components=(DesiredComponent("zewitness", "0.0.1", fake),)
    )
    steps = (
        DeploymentStep(
            component_id="zewitness",
            desired_version="0.0.1",
            artifact=fake,
            action=DeploymentAction.BLOCKED,
            reason_code=DeploymentReasonCode.RUNTIME_BROKEN,
            reason="synthetic blocked plan",
        ),
    )
    return DeploymentPlan(
        desired_state=desired,
        runtime_state=RuntimeState.BROKEN,
        steps=steps,
        blocked=True,
        blocked_reason="synthetic blocked plan",
    )


# ---------------------------------------------------------------------------
# Thread lease holder — deterministic Event synchronization, zero sleeps
# ---------------------------------------------------------------------------


def _thread_holder(root: Path, operation: str):
    """Acquire the lease in a separate thread (virgin context → real flock).

    Returns ``(thread, release_event)``.  The thread sets ``acquired`` (via
    the returned handle) only after the flock is held; the main thread must
    wait on that event before exercising BUSY paths.
    """
    lock = RuntimeMutationLock(root)
    acquired = threading.Event()
    release = threading.Event()
    errors: list[BaseException] = []

    def _target() -> None:
        try:
            lease = lock.acquire(operation)
        except BaseException as exc:  # noqa: BLE001 — captured for assertion
            errors.append(exc)
            acquired.set()
            return
        acquired.set()
        release.wait(timeout=60)
        lease.release()

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    assert acquired.wait(timeout=60), "holder thread never acquired the lease"
    assert not errors, f"holder thread failed: {errors[0]!r}"
    return thread, release


def _release_holder(thread: threading.Thread, release: threading.Event) -> None:
    release.set()
    thread.join(timeout=60)
    assert not thread.is_alive(), "holder thread did not release the lease"


# ---------------------------------------------------------------------------
# Subprocess holder — pipe / READY handshake, no sleeps (mission §35)
# ---------------------------------------------------------------------------


_HOLD_SCRIPT = textwrap.dedent(
    """
    import sys
    from zealfie.runtime.mutation_lock import RuntimeMutationLock
    lock = RuntimeMutationLock(sys.argv[1])
    lease = lock.acquire(sys.argv[2])
    print("READY", flush=True)
    sys.stdin.readline()
    lease.release()
    print("RELEASED", flush=True)
    """
).strip()


def _spawn(script: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )


def _read_line(proc: subprocess.Popen, timeout: float = 60.0) -> str:
    box: queue.Queue[str] = queue.Queue()

    def _target() -> None:
        box.put(proc.stdout.readline())

    reader = threading.Thread(target=_target, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        raise AssertionError(
            f"timed out after {timeout}s waiting for child output"
        )
    return box.get()


def _expect_line(proc: subprocess.Popen, prefixes: tuple[str, ...]) -> str:
    line = _read_line(proc)
    if not line.startswith(prefixes):
        proc.kill()
        proc.wait()
        raise AssertionError(
            f"expected line starting with one of {prefixes!r}, got {line!r}"
        )
    return line


def _send(proc: subprocess.Popen, text: str) -> None:
    proc.stdin.write(text + "\n")
    proc.stdin.flush()


@contextlib.contextmanager
def _subprocess_holding(root: Path, operation: str):
    child = _spawn(_HOLD_SCRIPT, str(root), operation)
    try:
        _expect_line(child, ("READY",))
        yield child
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


# ---------------------------------------------------------------------------
# Synthetic service / catalog helpers
# ---------------------------------------------------------------------------


def _test_catalog() -> ProductCatalog:
    desc = ProductDescriptor(
        product_id="testx",
        display_name="TestX",
        distribution_name="testx",
        launch_entry_points=(EntryPointContract("console_scripts", "testx"),),
        remote_source=RemoteSource(owner="o", repo="r", ref="main"),
    )
    desc2 = ProductDescriptor(
        product_id="testy",
        display_name="TestY",
        distribution_name="testy",
        launch_entry_points=(EntryPointContract("console_scripts", "testy"),),
        remote_source=RemoteSource(owner="o", repo="r2", ref="main"),
    )
    return ProductCatalog((desc, desc2))


def _service_with_tmp_runtime(
    tmp_path: Path,
) -> tuple[ZeAlfieService, RuntimeLayout]:
    layout = RuntimeLayout(root=tmp_path / "rt")
    service = ZeAlfieService(
        catalog=_test_catalog(),
        runtime=SharedRuntime(layout=layout),
        selection_store=SelectionStore(path=tmp_path / "desired-products.toml"),
    )
    return service, layout


# ===========================================================================
# (10) runtime create locked → BUSY, no slot/root created
# ===========================================================================


@needs_posix
def test_runtime_create_busy_no_mutation(tmp_path: Path) -> None:
    layout = RuntimeLayout(root=tmp_path / "rt")
    rt = SharedRuntime(layout=layout)
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_CREATE)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            rt.create()
        exc = excinfo.value
        assert BUSY_MESSAGE_CORE in str(exc)
        assert exc.operation == OPERATION_RUNTIME_CREATE
        assert exc.pid == os.getpid()
        # Nothing was created: no root, no slots, no state.
        assert not layout.root.exists()
    finally:
        _release_holder(thread, release)
    # After release the same create path can acquire normally.
    with RuntimeMutationLock(layout.root).acquire(OPERATION_RUNTIME_CREATE):
        pass


# ===========================================================================
# (11) deployment apply locked → BUSY, no candidate created
# ===========================================================================


@needs_posix
def test_deployment_apply_busy_no_candidate(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path)
    rt = SharedRuntime(layout=layout)
    slots_before = sorted(p.name for p in layout.slots.iterdir())
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_APPLY)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            apply_deployment_plan(
                _blocked_plan(), registry=ComponentRegistry(()), runtime=rt
            )
        assert excinfo.value.operation == OPERATION_RUNTIME_APPLY
        # No candidate slot was created.
        assert sorted(p.name for p in layout.slots.iterdir()) == slots_before
        assert (
            load_active_state(layout.active_pointer, layout_root=layout.root)
            .active_slot_id
            == ACTIVE_ID
        )
    finally:
        _release_holder(thread, release)
    # After release the same apply acquires normally and fails cleanly on
    # the blocked plan (no BusyError, no mutation).
    result = apply_deployment_plan(
        _blocked_plan(), registry=ComponentRegistry(()), runtime=rt
    )
    assert result.success is False
    assert "deployment plan is blocked" in result.reason


# ===========================================================================
# (12) activate without lease → LeaseRequired (+ foreign root → LeaseRequired)
# ===========================================================================


def test_activate_without_lease_raises_lease_required(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path)
    rt = SharedRuntime(layout=layout)
    txn = rt.begin_transaction()
    with pytest.raises(RuntimeMutationLeaseRequired):
        txn.activate()
    # The pointer is untouched.
    assert (
        load_active_state(layout.active_pointer, layout_root=layout.root)
        .active_slot_id
        == ACTIVE_ID
    )


@needs_posix
def test_activate_with_foreign_root_lease_raises_lease_required(
    tmp_path: Path,
) -> None:
    layout = _synthesize_ready_runtime(tmp_path)
    rt = SharedRuntime(layout=layout)
    txn = rt.begin_transaction()
    other_root = tmp_path / "other_runtime"
    with RuntimeMutationLock(other_root).acquire(OPERATION_RUNTIME_APPLY):
        with pytest.raises(RuntimeMutationLeaseRequired):
            txn.activate()
    assert (
        load_active_state(layout.active_pointer, layout_root=layout.root)
        .active_slot_id
        == ACTIVE_ID
    )


# ===========================================================================
# (13) rollback locked → BUSY, active.json unchanged
# ===========================================================================


@needs_posix
def test_rollback_busy_pointer_unchanged(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path, previous_id=PREV_ID)
    rt = SharedRuntime(layout=layout)
    pointer_before = layout.active_pointer.read_bytes()
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_ROLLBACK)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            rt.rollback()
        assert excinfo.value.operation == OPERATION_RUNTIME_ROLLBACK
        assert layout.active_pointer.read_bytes() == pointer_before
    finally:
        _release_holder(thread, release)


# ===========================================================================
# (14) discard locked → BUSY, directory intact
# ===========================================================================


@needs_posix
def test_discard_busy_directory_intact(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path, orphans=(ORPHAN_ID,))
    rt = SharedRuntime(layout=layout)
    orphan_path = layout.slot_path(ORPHAN_ID)
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_DISCARD)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            rt.discard_slot(ORPHAN_ID)
        assert excinfo.value.operation == OPERATION_RUNTIME_DISCARD
        assert orphan_path.is_dir()
    finally:
        _release_holder(thread, release)


# ===========================================================================
# (15)+(24) gc locked → BUSY, zero deletion, zero metadata write
# ===========================================================================


@needs_posix
def test_gc_busy_zero_deletion_zero_metadata_write(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path, orphans=(ORPHAN_ID,))
    metadata_raw = _write_accelerated_metadata(layout, ORPHAN_ID)
    plan = build_gc_plan(layout.root)
    assert plan.status.value == "READY"
    pointer_before = layout.active_pointer.read_bytes()
    orphan_path = layout.slot_path(ORPHAN_ID)
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_GC)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            apply_gc_plan(layout.root, plan)
        assert excinfo.value.operation == OPERATION_RUNTIME_GC
        # Zero deletion, zero metadata write, pointer untouched.
        assert orphan_path.is_dir()
        assert (
            layout.state_dir / "accelerated-metadata.json"
        ).read_bytes() == metadata_raw
        assert layout.active_pointer.read_bytes() == pointer_before
    finally:
        _release_holder(thread, release)


# ===========================================================================
# (16)(17)(18) product install / update / gpu-install locked → BUSY
# ===========================================================================


@needs_posix
def test_product_install_busy(tmp_path: Path) -> None:
    service, layout = _service_with_tmp_runtime(tmp_path)
    thread, release = _thread_holder(layout.root, OPERATION_PRODUCT_INSTALL)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            service.install_product(
                "testx",
                resolver=lambda o, r, ref: "a" * 40,
                fetcher=lambda o, r, sha: b"zip",
                work_root=tmp_path / "work",
            )
        assert excinfo.value.operation == OPERATION_PRODUCT_INSTALL
    finally:
        _release_holder(thread, release)


@needs_posix
def test_product_update_busy(tmp_path: Path) -> None:
    service, layout = _service_with_tmp_runtime(tmp_path)
    thread, release = _thread_holder(layout.root, OPERATION_PRODUCT_UPDATE)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            service.update_product(
                "testx",
                resolver=lambda o, r, ref: "a" * 40,
                fetcher=lambda o, r, sha: b"zip",
                work_root=tmp_path / "work",
            )
        assert excinfo.value.operation == OPERATION_PRODUCT_UPDATE
    finally:
        _release_holder(thread, release)


@needs_posix
def test_gpu_install_busy(tmp_path: Path) -> None:
    service, layout = _service_with_tmp_runtime(tmp_path)
    thread, release = _thread_holder(layout.root, OPERATION_GPU_INSTALL)
    try:
        with pytest.raises(RuntimeMutationBusyError) as excinfo:
            service.install_accelerated_runtime()
        assert excinfo.value.operation == OPERATION_GPU_INSTALL
    finally:
        _release_holder(thread, release)


# ===========================================================================
# (19-23) state stores unchanged on product-mutation BUSY
# ===========================================================================


@needs_posix
def test_product_mutation_busy_leaves_state_stores_unchanged(
    tmp_path: Path,
) -> None:
    service, layout = _service_with_tmp_runtime(tmp_path)
    selection_path = tmp_path / "desired-products.toml"
    provenance_path = layout.state_dir / "product-provenance.json"
    installed_lock_path = layout.state_dir / "installed-lock.json"
    thread, release = _thread_holder(layout.root, OPERATION_PRODUCT_INSTALL)
    try:
        with pytest.raises(RuntimeMutationBusyError):
            service.install_product(
                "testx",
                resolver=lambda o, r, ref: "a" * 40,
                fetcher=lambda o, r, sha: b"zip",
                work_root=tmp_path / "work",
            )
    finally:
        _release_holder(thread, release)
    # active.json, provenance, installed-lock and the selection store are
    # all untouched on BUSY (no write happened before the refusal).
    assert not layout.active_pointer.exists()
    assert not provenance_path.exists()
    assert not installed_lock_path.exists()
    assert not selection_path.exists()
    assert not layout.slots.exists()


# ===========================================================================
# (25) stale transaction still enforced under a held lease
# ===========================================================================


@needs_posix
def test_stale_transaction_still_enforced_under_lease(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path)
    rt = SharedRuntime(layout=layout)
    txn = rt.begin_transaction()
    txn._mark_valid()
    # Another activation changes the active slot after the snapshot.
    _write_active_state(layout, "rt-ccc333333333", None)
    with RuntimeMutationLock(layout.root).acquire(OPERATION_RUNTIME_APPLY):
        result = txn.activate()
    assert result.reason_code == RuntimeReasonCode.STALE_TRANSACTION
    assert (
        load_active_state(layout.active_pointer, layout_root=layout.root)
        .active_slot_id
        == "rt-ccc333333333"
    )


# ===========================================================================
# (26) stale-plan GC still enforced under a held lease
# ===========================================================================


@needs_posix
def test_stale_gc_plan_still_enforced_under_lease(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path, orphans=(ORPHAN_ID,))
    plan = build_gc_plan(layout.root)
    assert plan.status.value == "READY"
    # The world changes between plan build and apply.
    _make_slot_dir(layout, ORPHAN2_ID)
    result = apply_gc_plan(layout.root, plan)
    assert result.stale is True
    assert result.deleted_slots == ()
    assert layout.slot_path(ORPHAN_ID).is_dir()
    assert layout.slot_path(ORPHAN2_ID).is_dir()


# ===========================================================================
# (27) rollback ping-pong semantics unchanged under the lease
# ===========================================================================


@needs_posix
def test_rollback_ping_pong_semantics_unchanged_under_lease(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(
        tmp_path, active_id=ACTIVE_ID, previous_id=PREV_ID, python_link=True
    )
    rt = SharedRuntime(layout=layout)
    assert rt.status().state == RuntimeState.READY

    rb = rt.rollback()
    assert rb.state == RuntimeState.READY
    assert rb.reason_code == RuntimeReasonCode.RUNTIME_READY
    assert rb.active_slot_id == PREV_ID
    assert rb.previous_slot_id == ACTIVE_ID

    rb2 = rt.rollback()
    assert rb2.active_slot_id == ACTIVE_ID
    assert rb2.previous_slot_id == PREV_ID


# ===========================================================================
# (29) runtime status remains available during busy
# ===========================================================================


@needs_posix
def test_runtime_status_available_during_busy(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(tmp_path, python_link=True)
    rt = SharedRuntime(layout=layout)
    assert rt.status().state == RuntimeState.READY
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_APPLY)
    try:
        # Read-only status is never blocked by the mutation lease.
        st = rt.status()
        assert st.state == RuntimeState.READY
        assert st.active_slot_id == ACTIVE_ID
        # The lock object itself reports the holder (different thread).
        info = RuntimeMutationLock(layout.root).probe_busy()
        assert info is not None
        assert info["operation"] == OPERATION_RUNTIME_APPLY
    finally:
        _release_holder(thread, release)


# ===========================================================================
# (30) gc-plan honest warning during busy (D9) — exit codes unchanged
# ===========================================================================


@needs_posix
def test_cli_gc_plan_warning_during_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import zealfie.cli as cli

    layout = _synthesize_ready_runtime(tmp_path, python_link=True)
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    import io

    plan_stdout = io.StringIO()
    thread, release = _thread_holder(layout.root, OPERATION_RUNTIME_APPLY)
    try:
        code = cli.run(["runtime", "gc-plan"], stdout=plan_stdout)
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == 0
    assert (
        f"Warning: runtime mutation in progress (operation="
        f"{OPERATION_RUNTIME_APPLY}, pid={os.getpid()}). Snapshot may change."
    ) in captured.err
    assert "Safe runtime GC plan:" in plan_stdout.getvalue()


# ===========================================================================
# (31) CLI BUSY exit codes and messages (4 / 5) + lock unavailable (6)
# ===========================================================================


def _run_cli(cli, argv, stdout=None):
    import io

    return cli.run(argv, stdout=stdout or io.StringIO())


@needs_posix
def test_cli_busy_exit_codes_and_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import zealfie.cli as cli

    # ---- runtime create → BUSY exit 4 ----
    layout_create = RuntimeLayout(root=tmp_path / "rt-create")
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout_create)
    thread, release = _thread_holder(layout_create.root, OPERATION_RUNTIME_CREATE)
    try:
        code = _run_cli(cli, ["runtime", "create"])
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == cli.BUSY_EXIT
    assert "Runtime mutation refused:" in captured.err
    assert "Status: BUSY" in captured.err
    assert f"Current operation: {OPERATION_RUNTIME_CREATE}" in captured.err
    assert "No changes have been applied." in captured.err
    assert not layout_create.root.exists()

    # ---- runtime rollback → BUSY exit 4 ----
    layout_rb = _synthesize_ready_runtime(tmp_path / "rb")
    service_rb = ZeAlfieService(runtime=SharedRuntime(layout=layout_rb))
    monkeypatch.setattr(cli, "_make_service", lambda: service_rb)
    pointer_before = layout_rb.active_pointer.read_bytes()
    thread, release = _thread_holder(layout_rb.root, OPERATION_RUNTIME_ROLLBACK)
    try:
        code = _run_cli(cli, ["runtime", "rollback"])
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == cli.BUSY_EXIT
    assert "Runtime mutation refused:" in captured.err
    assert f"Current operation: {OPERATION_RUNTIME_ROLLBACK}" in captured.err
    assert layout_rb.active_pointer.read_bytes() == pointer_before

    # ---- runtime gc → BUSY exit 4 ----
    layout_gc = _synthesize_ready_runtime(tmp_path / "gc", orphans=(ORPHAN_ID,))
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout_gc)
    thread, release = _thread_holder(layout_gc.root, OPERATION_RUNTIME_GC)
    try:
        code = _run_cli(cli, ["runtime", "gc"])
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == cli.BUSY_EXIT
    assert "Runtime mutation refused:" in captured.err
    assert f"Current operation: {OPERATION_RUNTIME_GC}" in captured.err
    assert layout_gc.slot_path(ORPHAN_ID).is_dir()

    # ---- install → BUSY exit 4 ----
    service_in, layout_in = _service_with_tmp_runtime(tmp_path / "install")
    monkeypatch.setattr(cli, "_make_service", lambda: service_in)
    monkeypatch.setattr(
        cli, "_make_install_deps", lambda: (None, None, tmp_path / "install-work")
    )
    thread, release = _thread_holder(layout_in.root, OPERATION_PRODUCT_INSTALL)
    try:
        code = _run_cli(cli, ["install", "testx"])
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == cli.BUSY_EXIT
    assert "Runtime mutation refused:" in captured.err
    assert f"Current operation: {OPERATION_PRODUCT_INSTALL}" in captured.err

    # ---- runtime apply → BUSY exit 5 (4 is OfflineReleaseError) ----
    layout_ap = _synthesize_ready_runtime(tmp_path / "apply")

    class _ApplyLockService:
        def __init__(self, root: Path) -> None:
            self._lock = RuntimeMutationLock(root)

        def apply_offline_deployment(self, release_dir):
            with self._lock.acquire(OPERATION_RUNTIME_APPLY):
                raise AssertionError("lease must not be acquirable during BUSY")

    monkeypatch.setattr(
        cli, "_make_service", lambda: _ApplyLockService(layout_ap.root)
    )
    thread, release = _thread_holder(layout_ap.root, OPERATION_RUNTIME_APPLY)
    try:
        code = _run_cli(cli, ["runtime", "apply", "--release-dir", "/nonexistent"])
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == cli.BUSY_EXIT_ALT
    assert "Runtime mutation refused:" in captured.err
    assert f"Current operation: {OPERATION_RUNTIME_APPLY}" in captured.err

    # ---- system gpu-install → BUSY exit 5 (4 is plan failure) ----
    service_gpu, layout_gpu = _service_with_tmp_runtime(tmp_path / "gpu")
    fake_plan = SimpleNamespace(
        status=AcceleratedPlanStatus.PLAN_READY,
        backend="cuda",
        products_concerned=(),
        blocked_reason=None,
    )
    monkeypatch.setattr(
        service_gpu, "build_accelerated_deployment_plan", lambda: fake_plan
    )
    monkeypatch.setattr(cli, "_make_service", lambda: service_gpu)
    monkeypatch.setattr(
        cli, "_make_install_deps", lambda: (None, None, tmp_path / "gpu-work")
    )
    thread, release = _thread_holder(layout_gpu.root, OPERATION_GPU_INSTALL)
    try:
        code = _run_cli(cli, ["system", "gpu-install"])
        captured = capsys.readouterr()
    finally:
        _release_holder(thread, release)
    assert code == cli.BUSY_EXIT_ALT
    assert "Runtime mutation refused:" in captured.err
    assert f"Current operation: {OPERATION_GPU_INSTALL}" in captured.err


@needs_posix
def test_cli_lock_unavailable_exit_6(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    import zealfie.cli as cli

    layout = RuntimeLayout(root=tmp_path / "rt-lockerr")
    monkeypatch.setattr(cli, "default_runtime_layout", lambda: layout)
    # Simulate a non-POSIX platform: the primitive fails → fail closed.
    monkeypatch.setattr(mutation_lock_module, "_platform_is_posix", lambda: False)
    code = _run_cli(cli, ["runtime", "create"])
    captured = capsys.readouterr()
    assert code == cli.LOCK_ERROR_EXIT
    assert "Runtime mutation lock unavailable:" in captured.err
    assert not layout.root.exists()


# ===========================================================================
# (32) GUI BUSY mapping — worker → message, no crash, clean finish
# ===========================================================================


@pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")
def test_gui_install_worker_busy_mapping(tmp_path: Path) -> None:
    from zealfie.gui.install_worker import InstallWorker

    class _BusyService:
        def install_product(self, *args, **kwargs):
            raise RuntimeMutationBusyError(
                lock_path=Path("/tmp/x.zealfie-mutation.lock"),
                operation=OPERATION_PRODUCT_INSTALL,
                pid=os.getpid(),
            )

    worker = InstallWorker(
        "testx",
        _BusyService(),
        resolver=lambda o, r, ref: "a" * 40,
        fetcher=lambda o, r, sha: b"zip",
        work_root=tmp_path,
        operation="install",
    )
    failed: list[tuple[str, str]] = []
    finished: list[bool] = []
    worker.install_failed.connect(lambda pid, msg: failed.append((pid, msg)))
    worker.finished.connect(lambda: finished.append(True))
    worker.run()
    assert failed == [("testx", "another ZeAlfie runtime operation is in progress")]
    assert finished == [True]


@pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")
def test_gui_install_worker_lock_unavailable_mapping(tmp_path: Path) -> None:
    from zealfie.gui.install_worker import InstallWorker

    class _LockBrokenService:
        def install_product(self, *args, **kwargs):
            raise RuntimeMutationLockError("fcntl unavailable")

    worker = InstallWorker(
        "testx",
        _LockBrokenService(),
        resolver=lambda o, r, ref: "a" * 40,
        fetcher=lambda o, r, sha: b"zip",
        work_root=tmp_path,
        operation="install",
    )
    failed: list[tuple[str, str]] = []
    finished: list[bool] = []
    worker.install_failed.connect(lambda pid, msg: failed.append((pid, msg)))
    worker.finished.connect(lambda: finished.append(True))
    worker.run()
    assert len(failed) == 1
    assert failed[0][0] == "testx"
    assert "runtime mutation lock is unavailable" in failed[0][1]
    assert finished == [True]


@pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")
def test_gui_accelerated_worker_busy_mapping(tmp_path: Path) -> None:
    from zealfie.gui.accelerated_install_worker import AcceleratedInstallWorker

    class _BusyService:
        def install_accelerated_runtime(self, **kwargs):
            raise RuntimeMutationBusyError(
                lock_path=Path("/tmp/x.zealfie-mutation.lock"),
                operation=OPERATION_GPU_INSTALL,
                pid=os.getpid(),
            )

    worker = AcceleratedInstallWorker(_BusyService(), work_root=tmp_path)
    finished: list[object] = []
    worker.finished.connect(finished.append)
    worker.run()
    assert len(finished) == 1
    result = finished[0]
    assert result.success is False
    assert "another ZeAlfie runtime operation is in progress" in result.reason

# ===========================================================================
# (34) lease released on exception in an integrated flow → re-acquisition OK
# ===========================================================================


@needs_posix
def test_lease_released_on_exception_integrated_flow(tmp_path: Path) -> None:
    service, layout = _service_with_tmp_runtime(tmp_path)

    def boom_resolver(owner, repo, ref):
        raise SourceResolutionError("synthetic resolution failure")

    with pytest.raises(SourceResolutionError):
        service.install_product(
            "testx",
            resolver=boom_resolver,
            fetcher=lambda o, r, sha: b"zip",
            work_root=tmp_path / "work",
        )
    lock = RuntimeMutationLock(layout.root)
    assert lock.probe_busy() is None
    assert RuntimeMutationLock.current_lease() is None
    # A fresh mutation can acquire immediately — the failed install left
    # the lease released on every path (wrapper context manager).
    with lock.acquire(OPERATION_PRODUCT_INSTALL):
        pass


# ===========================================================================
# §20 two concurrent synthetic flows — exactly one owner, no divergent state
# ===========================================================================


@needs_posix
def test_two_concurrent_product_flows_single_owner(tmp_path: Path) -> None:
    """A: install product X (holds the lease, blocked in the resolver);
    B: update product Y (same root) → BUSY.  After A fails and releases,
    B acquires normally.  Provenance never diverges (never written)."""
    layout = RuntimeLayout(root=tmp_path / "rt")
    service_a = ZeAlfieService(
        catalog=_test_catalog(),
        runtime=SharedRuntime(layout=layout),
        selection_store=SelectionStore(path=tmp_path / "sel-a.toml"),
    )
    service_b = ZeAlfieService(
        catalog=_test_catalog(),
        runtime=SharedRuntime(layout=layout),
        selection_store=SelectionStore(path=tmp_path / "sel-b.toml"),
    )

    a_resolved = threading.Event()
    release_a = threading.Event()
    a_outcome: list[BaseException | None] = []

    def a_resolver(owner, repo, ref):
        # Called only AFTER install_product acquired the lease.
        a_resolved.set()
        release_a.wait(timeout=60)
        raise SourceResolutionError("flow A aborted after holding the lease")

    def _flow_a() -> None:
        try:
            service_a.install_product(
                "testx",
                resolver=a_resolver,
                fetcher=lambda o, r, sha: b"zip",
                work_root=tmp_path / "work-a",
            )
        except BaseException as exc:  # noqa: BLE001 — captured for assertion
            a_outcome.append(exc)

    thread_a = threading.Thread(target=_flow_a, daemon=True)
    thread_a.start()
    assert a_resolved.wait(timeout=60), "flow A never acquired the lease"

    # While A owns the lease, B (different thread, same root) is BUSY.
    b_busy: list[BaseException | None] = []
    b_started = threading.Event()

    def _flow_b_first() -> None:
        b_started.set()
        try:
            service_b.update_product(
                "testy",
                resolver=lambda o, r, ref: "b" * 40,
                fetcher=lambda o, r, sha: b"zip",
                work_root=tmp_path / "work-b",
            )
        except BaseException as exc:  # noqa: BLE001 — captured for assertion
            b_busy.append(exc)

    thread_b = threading.Thread(target=_flow_b_first, daemon=True)
    thread_b.start()
    assert b_started.wait(timeout=60)
    thread_b.join(timeout=60)
    assert not thread_b.is_alive()
    assert len(b_busy) == 1
    assert isinstance(b_busy[0], RuntimeMutationBusyError)

    # No provenance / selection divergence: nothing was persisted.
    assert not (layout.state_dir / "product-provenance.json").exists()
    assert not (layout.state_dir / "installed-lock.json").exists()
    assert not layout.active_pointer.exists()

    # Release A → its flow fails (exception) and the lease is freed.
    release_a.set()
    thread_a.join(timeout=60)
    assert not thread_a.is_alive()
    assert len(a_outcome) == 1
    assert isinstance(a_outcome[0], SourceResolutionError)
    assert RuntimeMutationLock(layout.root).probe_busy() is None

    # B re-runs → acquires normally (no BUSY); with no active provenance the
    # update preflight raises ProductUpdateNotApplicableError, not BUSY.
    from zealfie.app.service import ProductUpdateNotApplicableError

    with pytest.raises(ProductUpdateNotApplicableError):
        service_b.update_product(
            "testy",
            resolver=lambda o, r, ref: "b" * 40,
            fetcher=lambda o, r, sha: b"zip",
            work_root=tmp_path / "work-b",
        )
    # Still no provenance was ever written.
    assert not (layout.state_dir / "product-provenance.json").exists()


# ===========================================================================
# §30 synthetic multiprocess E2E — pipe/READY sync, zero sleeps
# ===========================================================================


@needs_posix
def test_e2e_multiprocess_busy_then_recovery(tmp_path: Path) -> None:
    layout = _synthesize_ready_runtime(
        tmp_path, orphans=(ORPHAN_ID,), python_link=True
    )
    rt = SharedRuntime(layout=layout)
    pointer_before = layout.active_pointer.read_bytes()
    slots_before = sorted(p.name for p in layout.slots.iterdir())

    with _subprocess_holding(layout.root, OPERATION_RUNTIME_APPLY) as owner:
        # ---- B: rollback → BUSY, active.json unchanged ----
        with pytest.raises(RuntimeMutationBusyError):
            rt.rollback()
        assert layout.active_pointer.read_bytes() == pointer_before

        # ---- C: gc → BUSY, zero deletion ----
        plan = build_gc_plan(layout.root)
        with pytest.raises(RuntimeMutationBusyError):
            apply_gc_plan(layout.root, plan)
        assert layout.slot_path(ORPHAN_ID).is_dir()

        # ---- D: deployment apply → BUSY, no candidate ----
        with pytest.raises(RuntimeMutationBusyError):
            apply_deployment_plan(
                _blocked_plan(), registry=ComponentRegistry(()), runtime=rt
            )
        assert sorted(p.name for p in layout.slots.iterdir()) == slots_before

        # ---- read-only status keeps working during busy ----
        st = rt.status()
        assert st.state == RuntimeState.READY
        assert st.active_slot_id == ACTIVE_ID

        # ---- release A (READY/RELEASED handshake, no sleep) ----
        _send(owner, "RELEASE")
        _expect_line(owner, ("RELEASED",))
        owner.wait(timeout=60)

    assert RuntimeMutationLock(layout.root).probe_busy() is None

    # ---- B re-run: acquires normally (no BUSY) ----
    rb = rt.rollback()
    assert rb.state == RuntimeState.READY
    assert rb.reason_code == RuntimeReasonCode.ROLLBACK_TARGET_NOT_FOUND

    # ---- C re-run: acquires normally and applies the plan ----
    fresh = build_gc_plan(layout.root)
    assert fresh.status.value == "READY"
    result = apply_gc_plan(layout.root, fresh)
    assert result.deleted_slots == (ORPHAN_ID,)
    assert not layout.slot_path(ORPHAN_ID).is_dir()
    assert layout.slot_path(ACTIVE_ID).is_dir()

    # ---- D re-run: acquires normally; the blocked plan fails cleanly ----
    d_result = apply_deployment_plan(
        _blocked_plan(), registry=ComponentRegistry(()), runtime=rt
    )
    assert d_result.success is False
    assert "deployment plan is blocked" in d_result.reason
