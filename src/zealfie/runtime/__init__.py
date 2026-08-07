"""Shared runtime management for ZeAlfie.

The ``runtime`` package provides the persistent, shared Python
environment that hosts ZeSoftware components.  This is distinct from:

* the development virtual environment (``.venv``);
* the temporary test-only environment (``TemporaryVenv``).
"""

from __future__ import annotations

from .layout import RuntimeLayout, default_runtime_layout, default_runtime_root
from .manager import SharedRuntime, SharedRuntimeError
from .model import (
    InstallOutcome,
    InstallResult,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from .probe import probe_runtime_distribution, probe_runtime_python_version

__all__ = [
    "InstallOutcome",
    "InstallResult",
    "RuntimeLayout",
    "RuntimeReasonCode",
    "RuntimeState",
    "RuntimeStatus",
    "SharedRuntime",
    "SharedRuntimeError",
    "default_runtime_layout",
    "default_runtime_root",
    "probe_runtime_distribution",
    "probe_runtime_python_version",
]
