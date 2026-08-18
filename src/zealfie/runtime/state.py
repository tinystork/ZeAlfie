"""Atomic persistence of the active-slot pointer."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from .model import ActiveRuntimeState, RuntimeReasonCode, RuntimeState, RuntimeStatus

_CURRENT_SCHEMA_VERSION = 1


def _unlink_best_effort(path: str) -> None:
    """Best-effort ``os.unlink``; never raises (temp-file cleanup)."""
    try:
        os.unlink(path)
    except OSError:
        pass

# Re-export the canonical slot-id validator for use by other modules.
from .layout import validate_slot_id  # noqa: E402


def load_active_state(
    pointer_path: Path, *, layout_root: Path
) -> RuntimeStatus:
    """Load the active-slot pointer from *pointer_path* (``active.json``).

    Returns a :class:`RuntimeStatus` indicating the global state.
    Never raises for IO or parse errors — those produce ``BROKEN``.
    Slot IDs from the file are validated with the same canonical
    validator used everywhere else.
    """
    if not pointer_path.is_file():
        return RuntimeStatus(
            state=RuntimeState.ABSENT,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_NOT_FOUND,
            reason="active state file does not exist",
        )

    try:
        text = pointer_path.read_text(encoding="utf-8")
        payload = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
            reason=f"active state file is unreadable or invalid JSON: {exc}",
        )

    if not isinstance(payload, dict):
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
            reason=f"active state root must be a JSON object, got {type(payload).__name__}",
        )

    schema = payload.get("schema_version")
    if schema != _CURRENT_SCHEMA_VERSION:
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
            reason=f"unsupported state schema version: {schema}",
        )

    active_id = payload.get("active_slot")
    previous_id = payload.get("previous_slot")

    # active_slot is mandatory when the file exists.
    if not isinstance(active_id, str):
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
            reason="active_slot must be a non-null string",
        )

    # Validate active_slot with canonical validator.
    try:
        validate_slot_id(active_id)
    except ValueError as exc:
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
            reason=f"active_slot is invalid: {exc}",
        )

    # previous_slot is optional, but if present must be a valid string.
    if previous_id is not None:
        if not isinstance(previous_id, str):
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout_root,
                reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
                reason="previous_slot must be a string or null",
            )
        try:
            validate_slot_id(previous_id)
        except ValueError as exc:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=layout_root,
                reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
                reason=f"previous_slot is invalid: {exc}",
            )

    if active_id == previous_id:
        return RuntimeStatus(
            state=RuntimeState.BROKEN,
            runtime_root=layout_root,
            reason_code=RuntimeReasonCode.RUNTIME_STATE_FILE_INVALID,
            reason="active_slot and previous_slot must not be the same",
        )

    return RuntimeStatus(
        state=RuntimeState.READY,
        runtime_root=layout_root,
        active_slot_id=active_id,
        previous_slot_id=previous_id,
        reason_code=RuntimeReasonCode.RUNTIME_READY,
    )


def save_active_state(
    pointer_path: Path,
    active_slot_id: str,
    previous_slot_id: str | None,
) -> None:
    """Atomically write the active-slot pointer.

    Validates *active_slot_id* and *previous_slot_id* with the canonical
    slot-id validator before touching the filesystem.  Writes to a
    temporary file, then calls ``os.replace()``.
    """
    # Validate before any filesystem mutation.
    validate_slot_id(active_slot_id)
    if previous_slot_id is not None:
        validate_slot_id(previous_slot_id)
    if active_slot_id == previous_slot_id:
        raise ValueError(
            f"active_slot and previous_slot must not be the same: "
            f"{active_slot_id!r}"
        )

    payload: dict[str, object] = {
        "schema_version": _CURRENT_SCHEMA_VERSION,
        "active_slot": active_slot_id,
    }
    if previous_slot_id is not None:
        payload["previous_slot"] = previous_slot_id

    text = json.dumps(payload, indent=2) + "\n"

    pointer_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        suffix=".json", prefix=".active-", dir=str(pointer_path.parent)
    )
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    os.replace(tmp_name, str(pointer_path))


def clear_previous_slot(pointer_path: Path) -> None:
    """Atomically remove ``previous_slot`` from the active pointer.

    Reads the current ``active.json``, validates it is READY (an
    ``active_slot`` is present and valid), and rewrites it with
    ``active_slot`` unchanged and NO ``previous_slot`` key (atomic
    mkstemp + fsync + ``os.replace``, the same pattern as
    :func:`save_active_state`).

    Hardening (MINOR-1): the raw bytes read at the start are re-read
    immediately before ``os.replace``; if they differ (a non-cooperating
    writer raced us), a :class:`ValueError` is raised instead of
    clobbering the writer's update.

    Raises :class:`ValueError` when the pointer is absent, broken, or
    otherwise not READY -- the caller must fail closed (preserve the
    rollback slot) rather than proceeding with a release.

    Used by :func:`zealfie.runtime.gc.apply_gc_plan` when releasing a
    ``PREVIOUS_RELEASABLE`` rollback slot (ZA-M1-4 LOT A): the pointer is
    cleared strictly BEFORE the slot directory is removed, so
    ``active.json`` can never end up pointing at a deleted slot.
    """
    layout_root = pointer_path.parent.parent
    try:
        original_raw = pointer_path.read_bytes()
    except OSError as exc:
        raise ValueError(
            f"cannot clear previous_slot: active state file is "
            f"unreadable ({exc})"
        ) from exc

    status = load_active_state(pointer_path, layout_root=layout_root)
    if status.state != RuntimeState.READY or status.active_slot_id is None:
        raise ValueError(
            f"cannot clear previous_slot: active state is not READY "
            f"({status.state.value}: {status.reason})"
        )

    payload: dict[str, object] = {
        "schema_version": _CURRENT_SCHEMA_VERSION,
        "active_slot": status.active_slot_id,
    }
    text = json.dumps(payload, indent=2) + "\n"

    pointer_path.parent.mkdir(parents=True, exist_ok=True)

    fd, tmp_name = tempfile.mkstemp(
        suffix=".json", prefix=".active-", dir=str(pointer_path.parent)
    )
    try:
        os.write(fd, text.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)

    # Re-read immediately before replace (MINOR-1): a non-cooperating
    # writer may have changed the pointer between the initial read and
    # now.  If the bytes differ, refuse rather than clobber.
    try:
        current_raw = pointer_path.read_bytes()
    except OSError as exc:
        _unlink_best_effort(tmp_name)
        raise ValueError(
            f"cannot clear previous_slot: active pointer re-read failed "
            f"during clear: {exc}"
        ) from exc
    if current_raw != original_raw:
        _unlink_best_effort(tmp_name)
        raise ValueError("active pointer changed during clear; refusing")

    os.replace(tmp_name, str(pointer_path))
