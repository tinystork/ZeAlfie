"""Controlled subprocess execution for launch plans."""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from .plan import LaunchPlan


@dataclass(frozen=True, slots=True)
class LaunchResult:
    """The result of executing a :class:`LaunchPlan`."""

    return_code: int
    stdout: str
    stderr: str
    timed_out: bool = False


class LaunchError(RuntimeError):
    """Raised when a launch plan cannot be executed successfully."""


class InvalidEntryPointScriptNameError(LaunchError):
    """The entry-point name contains path separators or escape attempts."""


class EntryPointScriptNotFoundError(LaunchError):
    """The entry-point script is declared in metadata but the wrapper
    does not exist at the expected path inside the venv."""


def execute_launch_plan(
    plan: LaunchPlan,
    *,
    timeout_seconds: float = 30,
) -> LaunchResult:
    """Execute a prepared :class:`LaunchPlan` as a subprocess.

    The process always runs with ``shell=False``.  stdout and stderr are
    captured and decoded as UTF-8.  If the process does not complete
    within *timeout_seconds*, it is killed and the result will have
    ``timed_out=True``.

    Raises :class:`FileNotFoundError` when the executable does not exist.
    """
    cmd = [str(plan.executable), *(str(a) for a in plan.arguments)]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            cwd=str(plan.working_directory) if plan.working_directory is not None else None,
        )
    except subprocess.TimeoutExpired as exc:
        return LaunchResult(
            return_code=-1,
            stdout=(exc.stdout or b"").decode("utf-8", errors="replace") if exc.stdout else "",
            stderr=(exc.stderr or b"").decode("utf-8", errors="replace") if exc.stderr else "",
            timed_out=True,
        )
    except OSError as exc:
        raise LaunchError(f"could not execute {plan.executable}: {exc}") from exc

    return LaunchResult(
        return_code=result.returncode,
        stdout=result.stdout,
        stderr=result.stderr,
        timed_out=False,
    )


def resolve_script(
    scripts_dir: str | os.PathLike[str],
    entry_point_name: str,
) -> "pathlib.Path":
    """Resolve a named entry-point script inside a venv scripts directory.

    *entry_point_name* must be a plain script name, not a path.
    Names containing path separators (``/``, ``\\``), parent-directory
    references (``..``), or absolute paths are rejected.

    The resolved script is verified to be a regular file that lives
    inside *scripts_dir* (canonical comparison).

    Returns the absolute path, accounting for platform suffixes
    (``.exe`` on Windows).

    Raises :class:`InvalidEntryPointScriptNameError` when the name looks
    like a path rather than a plain script name.

    Raises :class:`EntryPointScriptNotFoundError` if the expected script
    does not exist or is not a regular file.
    """
    import sys
    from pathlib import Path

    # Reject names that look like paths.
    _validate_entry_point_name(entry_point_name)

    sd = Path(scripts_dir).resolve(strict=False)

    if sys.platform == "win32" and not entry_point_name.lower().endswith(".exe"):
        candidate = (sd / f"{entry_point_name}.exe").resolve(strict=False)
    else:
        candidate = (sd / entry_point_name).resolve(strict=False)

    # Definitively verify the resolved path is inside scripts_dir.
    try:
        candidate.relative_to(sd)
    except ValueError:
        raise InvalidEntryPointScriptNameError(
            f"resolved script path {candidate} is outside "
            f"the scripts directory {sd}"
        )

    if not candidate.is_file():
        raise EntryPointScriptNotFoundError(
            f"entry-point script not found: {candidate}"
        )
    return candidate


def _validate_entry_point_name(name: str) -> None:
    """Reject entry-point names that look like paths."""
    if not name or name.strip() != name:
        raise InvalidEntryPointScriptNameError(
            f"invalid entry-point script name: {name!r}"
        )
    if "\\" in name or "/" in name:
        raise InvalidEntryPointScriptNameError(
            f"entry-point script name must be a plain name, not a path: {name!r}"
        )
    if name.startswith(".") or ".." in name:
        raise InvalidEntryPointScriptNameError(
            f"entry-point script name must not contain relative path components: {name!r}"
        )
    if name.startswith("/") or (len(name) >= 2 and name[1] == ":"):
        # Absolute POSIX or Windows (C:\...) path.
        raise InvalidEntryPointScriptNameError(
            f"entry-point script name must not be an absolute path: {name!r}"
        )
