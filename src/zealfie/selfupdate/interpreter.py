"""Windows self-update install-interpreter resolution (ZA-M1-4.2 CORR-3).

Root cause of the Windows GUI self-update regression: when the update is
initiated from ``zealfie-gui.exe``, ``sys.executable`` is the *windowed* venv
interpreter (``...\\Scripts\\pythonw.exe``).  If pip is then run with that
interpreter, distlib regenerates the console/gui launchers from ``pythonw.exe``
— producing ``zealfie.exe → pythonw.exe`` and ``zealfie-gui.exe → pythonww.exe``
shebangs, which silently breaks the console entry point (``zealfie.exe
--version`` prints nothing, exit 0).

The fix: on Windows only, resolve the *console* sibling interpreter of the
same venv (``...\\Scripts\\python.exe``) and use it for the self-update
install.  The same-venv proof is structural and never looks outside the venv:

* the parent of ``sys.executable`` must equal ``Path(sys.prefix) / "Scripts"``
  (the venv's own ``Scripts`` directory — a base/system Python has no
  ``Scripts`` child of its prefix on Windows, so this excludes it by
  construction);
* ``Path(sys.executable).with_name("python.exe")`` must exist and be a file.

Either condition failing raises (fail closed) — there is no silent fallback to
``pythonw.exe`` or to a system/base Python, and no ``PATH``/``shutil.which``
lookup is ever performed.  On non-Windows platforms ``sys.executable`` is
returned unchanged, so POSIX behaviour is identical to before.
"""

from __future__ import annotations

import sys
from pathlib import Path

__all__ = ["InterpreterResolutionError", "resolve_install_interpreter"]


class InterpreterResolutionError(RuntimeError):
    """Raised when the same-venv console interpreter cannot be proven."""


def resolve_install_interpreter(
    *,
    python: str | Path | None = None,
    sys_executable: str | Path | None = None,
    sys_prefix: str | Path | None = None,
    _is_windows: bool | None = None,
) -> str:
    """Resolve the interpreter to run the self-update install with.

    Returns a ``str`` path:

    * ``python`` (explicit injection) wins unchanged — a caller that already
      knows the right interpreter (or a test) is never second-guessed;
    * on non-Windows platforms ``sys.executable`` is returned unchanged;
    * on Windows, a *console* interpreter (``python.exe`` or any unexpected
      name we deliberately do not rewrite) is returned unchanged (idempotent),
      while a *windowed* interpreter (``pythonw.exe`` / ``pythonww.exe``) is
      resolved to its same-venv ``python.exe`` sibling.

    Raises :class:`InterpreterResolutionError` (fail closed) when a windowed
    interpreter cannot be proven to have a same-venv console sibling.

    ``sys_executable`` / ``sys_prefix`` / ``_is_windows`` are injectable seams
    for hermetic tests; they default to the real ``sys.executable`` /
    ``sys.prefix`` / ``sys.platform == "win32"``.
    """
    if python is not None:
        return str(python)

    executable = str(
        sys_executable if sys_executable is not None else sys.executable
    )
    prefix = str(sys_prefix if sys_prefix is not None else sys.prefix)
    is_windows = (
        _is_windows if _is_windows is not None else (sys.platform == "win32")
    )

    if not is_windows:
        return executable

    exe_path = Path(executable)
    if not _is_windowed_python(exe_path.name):
        # Already a console interpreter (python.exe) or an unexpected name we
        # never rewrite: return as-is (idempotent, never pythonw*.exe).
        return executable

    return _resolve_sibling_console_interpreter(exe_path, Path(prefix))


def _is_windowed_python(name: str) -> bool:
    """Return ``True`` when *name* is a windowed Python launcher name.

    distlib names windowed launchers ``pythonw.exe`` (gui_scripts) and, when
    pip itself runs under ``pythonw.exe``, ``pythonww.exe``.  Any ``pythonw*``
    ``.exe`` must never be used as the install interpreter.
    """
    lowered = name.lower()
    return lowered.startswith("pythonw") and lowered.endswith(".exe")


def _resolve_sibling_console_interpreter(exe_path: Path, prefix: Path) -> str:
    """Prove and return the same-venv ``python.exe`` sibling (fail closed).

    Two structural proofs, both required:

    1. ``sys.executable``'s parent is the venv's own ``Scripts`` directory
       (``Path(sys.prefix) / "Scripts"``) — this excludes any system/base
       Python by construction;
    2. the sibling ``python.exe`` exists and is a regular file.

    No ``PATH`` lookup and no ``shutil.which`` is performed, so the resolver
    can never reach outside the venv.
    """
    scripts_dir = Path(prefix) / "Scripts"
    if exe_path.parent != scripts_dir:
        raise InterpreterResolutionError(
            "refusing to self-update: the windowed interpreter "
            f"{str(exe_path)!r} is not inside the venv Scripts directory "
            f"{str(scripts_dir)!r}; no same-venv console interpreter was "
            "resolved"
        )
    sibling = exe_path.with_name("python.exe")
    if not sibling.is_file():
        raise InterpreterResolutionError(
            "refusing to self-update: the same-venv console interpreter "
            f"{str(sibling)!r} does not exist or is not a file"
        )
    return str(sibling)
