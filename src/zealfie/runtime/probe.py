"""Runtime probe: inspect a shared runtime via its own Python interpreter.

The probe runs a small standard-library-only script inside the runtime's
Python and returns structured JSON.  No application code (ZeSolver,
PySide6, etc.) is imported.
"""

from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# Probe script (executed by the *runtime's* Python, not by the dev venv)
# ---------------------------------------------------------------------------

_PROBE_SCRIPT = textwrap.dedent("""\
import importlib.metadata
import json
import sys

result = {"python_version": sys.version}

# Distribution info for a specific package name (passed as first arg).
dist_name = sys.argv[1]
try:
    dist = importlib.metadata.distribution(dist_name)
    result["installed"] = True
    result["version"] = dist.version
    eps = []
    for ep in dist.entry_points:
        eps.append({
            "group": ep.group,
            "name": ep.name,
            "value": getattr(ep, "value", None),
        })
    result["entry_points"] = eps
except importlib.metadata.PackageNotFoundError:
    result["installed"] = False
    result["version"] = None
    result["entry_points"] = []

json.dump(result, sys.stdout)
""")

# Timeout when communicating with the runtime's Python interpreter.
_PROBE_TIMEOUT_SECONDS: float = 30


def probe_runtime_distribution(
    runtime_python: str | Path,
    distribution_name: str,
    *,
    timeout: float | None = _PROBE_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Run the metadata probe against a distribution inside the runtime.

    Parameters
    ----------
    runtime_python:
        Absolute path to the Python interpreter inside the runtime venv.
    distribution_name:
        The distribution to query (e.g. ``"zealfie-witness"``).
    timeout:
        Seconds to wait for the probe; ``None`` means no limit.

    Returns
    -------
    dict with keys ``python_version``, ``installed``, ``version``,
    ``entry_points``.

    Raises
    ------
    subprocess.TimeoutExpired
        If the probe times out.
    RuntimeError
        If the probe returns a non-zero exit code or produces invalid JSON.
    """
    argv = [
        str(runtime_python),
        "-c",
        _PROBE_SCRIPT,
        distribution_name,
    ]
    result = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        timeout=timeout,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"runtime probe failed (rc={result.returncode}):\n{result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"runtime probe returned invalid JSON: {exc}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        ) from exc


def probe_runtime_python_version(
    runtime_python: str | Path,
    *,
    timeout: float | None = _PROBE_TIMEOUT_SECONDS,
) -> str | None:
    """Return ``sys.version`` as reported by the runtime's Python, or ``None``.

    This is a lightweight probe that does not import any distribution.
    """
    try:
        result = subprocess.run(
            [str(runtime_python), "-c", "import sys; print(sys.version)"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None

    if result.returncode != 0:
        return None
    return result.stdout.strip()
