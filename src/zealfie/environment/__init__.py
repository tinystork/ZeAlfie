"""Temporary isolated Python environment for integration tests."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import venv
from pathlib import Path

from zealfie.common.subprocess_platform import technical_subprocess_platform_kwargs


_TEMPORARY_VENV_PREFIX = "zealfie-tmp-venv-"

# All subprocess calls inside TemporaryVenv respect this timeout by default.
# Individual calls can override it with an explicit *timeout* argument.
_DEFAULT_SUBPROCESS_TIMEOUT_SECONDS: float = 120


class TemporaryVenv:
    """An isolated temporary virtual environment.

    Use as a context manager to guarantee cleanup::

        with TemporaryVenv() as venv:
            venv.install_wheel("/path/to/component.whl")
            result = venv.run_python(["-c", "import mypkg"])

    The temporary directory and all its contents are removed when the
    context exits, even if an exception occurs.
    """

    def __init__(self) -> None:
        self._tmp_dir = Path(tempfile.mkdtemp(prefix=_TEMPORARY_VENV_PREFIX))
        self._env_dir = self._tmp_dir / "venv"
        self._built = False

    # -- paths ----------------------------------------------------------------

    @property
    def env_dir(self) -> Path:
        return self._env_dir

    @property
    def python(self) -> Path:
        return _python_exe(self._env_dir)

    @property
    def scripts_dir(self) -> Path:
        return _scripts_dir(self._env_dir)

    @property
    def executable(self, name: str) -> Path:
        """Path to a named executable inside the venv scripts directory.

        Appends the platform-specific suffix (e.g. ``.exe`` on Windows)
        automatically.
        """
        return self.scripts_dir / _script_name(name)

    # -- lifecycle ------------------------------------------------------------

    def create(self) -> None:
        """Create the virtual environment.

        Idempotent: calling this method a second time has no effect.
        """
        if self._built:
            return
        venv.create(self._env_dir, with_pip=True, clear=True)
        self._built = True

    def install_wheel(
        self,
        wheel_path: str | Path,
        *,
        no_index: bool = True,
        no_deps: bool = True,
        timeout: float | None = _DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
    ) -> subprocess.CompletedProcess[str]:
        """Install a wheel into the venv using its own pip.

        By default, ``--no-index`` and ``--no-deps`` are enabled so the
        install cannot accidentally reach the network.

        A timeout guards the subprocess; pass ``timeout`` to override.
        """
        self.create()
        args = [
            str(self.python),
            "-m",
            "pip",
            "install",
            str(wheel_path),
        ]
        if no_index:
            args.append("--no-index")
        if no_deps:
            args.append("--no-deps")

        return subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            **technical_subprocess_platform_kwargs(),
        )

    def run_python(
        self,
        argv: list[str],
        *,
        timeout: float | None = _DEFAULT_SUBPROCESS_TIMEOUT_SECONDS,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run a Python command inside the venv.

        ``argv`` should **not** include the interpreter path; it will be
        prefixed automatically.  For example::

            venv.run_python(["-c", "import zewitness"])

        A default timeout guards the subprocess; set ``timeout=None``
        explicitly when a long-running validation is intentionally
        unbounded.
        """
        self.create()
        env = os.environ.copy()
        if extra_env:
            env.update(extra_env)
        return subprocess.run(
            [str(self.python), *argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            **technical_subprocess_platform_kwargs(),
        )

    # -- cleanup --------------------------------------------------------------

    def cleanup(self) -> None:
        """Remove the temporary directory and everything inside it.

        Safe to call multiple times.
        """
        if self._tmp_dir.is_dir():
            shutil.rmtree(self._tmp_dir, ignore_errors=True)

    def __enter__(self) -> "TemporaryVenv":
        self.create()
        return self

    def __exit__(self, *args: object) -> None:
        self.cleanup()
        return None


# -- platform helpers ---------------------------------------------------------


def _python_exe(env_dir: Path) -> Path:
    """Return the Python interpreter inside a venv.

    Handles Linux/macOS (``bin/python``) and Windows (``Scripts/python.exe``).
    """
    if sys.platform == "win32":
        return env_dir / "Scripts" / "python.exe"
    return env_dir / "bin" / "python"


def _scripts_dir(env_dir: Path) -> Path:
    """Return the scripts directory inside a venv.

    Handles Linux/macOS (``bin``) and Windows (``Scripts``).
    """
    if sys.platform == "win32":
        return env_dir / "Scripts"
    return env_dir / "bin"


def _script_name(name: str) -> str:
    """Append the platform script suffix if needed."""
    if sys.platform == "win32" and not name.lower().endswith(".exe"):
        return f"{name}.exe"
    return name
