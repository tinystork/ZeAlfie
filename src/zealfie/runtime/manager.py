"""Shared runtime manager — create, inspect, install into the persistent runtime."""

from __future__ import annotations

import subprocess
import sys
import venv
from pathlib import Path

from zealfie.building import inspect_wheel
from zealfie.components.model import ComponentDefinition, EntryPointContract

from .layout import RuntimeLayout, default_runtime_layout
from .model import (
    InstallOutcome,
    InstallResult,
    RuntimeReasonCode,
    RuntimeState,
    RuntimeStatus,
)
from .probe import probe_runtime_distribution, probe_runtime_python_version

# Default timeout for subprocess calls during wheel installation.
_INSTALL_TIMEOUT_SECONDS: float = 120


class SharedRuntimeError(Exception):
    """Raised when a runtime operation cannot proceed."""


class SharedRuntime:
    """Manage the persistent ZeAlfie shared runtime.

    The runtime lives at a platform-appropriate user-data path and hosts
    installed ZeSoftware components.  For M0-5 only the witness component
    is installed.

    Usage::

        rt = SharedRuntime()
        status = rt.status()
        if status.state == RuntimeState.ABSENT:
            rt.create()
        rt.install_local_wheel("/path/to/witness.whl")
    """

    def __init__(self, layout: RuntimeLayout | None = None) -> None:
        self._layout = layout or default_runtime_layout()

    @property
    def layout(self) -> RuntimeLayout:
        return self._layout

    # -- status ---------------------------------------------------------------

    def status(self) -> RuntimeStatus:
        """Inspect the runtime and return a structured status.

        The status is determined by checking, in order:

        1. Does the ``current`` directory exist?
        2. Does a usable Python interpreter exist inside it?
        3. Can we retrieve its Python version?

        The result never raises; even a broken runtime returns a status.
        """
        current = self._layout.current

        if not current.is_dir():
            return RuntimeStatus(
                state=RuntimeState.ABSENT,
                runtime_root=self._layout.root,
                current=current,
                reason_code=RuntimeReasonCode.RUNTIME_NOT_FOUND,
                reason="runtime directory does not exist",
            )

        python = _runtime_python(current)
        if not python or not python.is_file():
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                current=current,
                python_executable=None,
                reason_code=RuntimeReasonCode.RUNTIME_PYTHON_NOT_FOUND,
                reason="runtime Python interpreter not found",
            )

        version = probe_runtime_python_version(python)
        if version is None:
            return RuntimeStatus(
                state=RuntimeState.BROKEN,
                runtime_root=self._layout.root,
                current=current,
                python_executable=python,
                reason_code=RuntimeReasonCode.RUNTIME_PYTHON_UNUSABLE,
                reason="runtime Python is not usable",
            )

        return RuntimeStatus(
            state=RuntimeState.READY,
            runtime_root=self._layout.root,
            current=current,
            python_executable=python,
            python_version=version,
            reason_code=RuntimeReasonCode.RUNTIME_READY,
            reason=None,
        )

    # -- create ---------------------------------------------------------------

    def create(self) -> RuntimeStatus:
        """Create or verify the shared runtime.

        - If the runtime is ``ABSENT``, it is created.
        - If the runtime is ``READY``, nothing is done (idempotent).
        - If the runtime is ``BROKEN``, an error is raised; the runtime
          is **not** destroyed or recreated automatically.

        Returns the status **after** the operation.
        """
        current = self._layout.current

        # Already ready → nothing to do.
        st = self.status()
        if st.state == RuntimeState.READY:
            return st

        # Broken → do not destroy.
        if st.state == RuntimeState.BROKEN:
            raise SharedRuntimeError(
                f"shared runtime at {current} is BROKEN ({st.reason}). "
                f"It must be repaired or removed manually before re-creation."
            )

        # ABSENT → create.
        current.parent.mkdir(parents=True, exist_ok=True)
        venv.create(current, with_pip=True, clear=True)

        return self.status()

    # -- install local wheel --------------------------------------------------

    def install_local_wheel(
        self,
        wheel_path: str | Path,
        *,
        component_definition: ComponentDefinition | None = None,
    ) -> InstallResult:
        """Install a local wheel into the shared runtime.

        The wheel is inspected before installation; after installation
        the runtime's Python is probed to confirm the distribution,
        version, and entry points.

        When *component_definition* is supplied, the installed
        distribution must also satisfy the expected launch entry point
        contract.  ``ALREADY_INSTALLED`` is only returned when the
        existing installation matches both the version **and** the
        expected contract.

        Probe errors are **not** silently swallowed — if the runtime
        probe fails, installation is aborted.

        Returns a structured :class:`InstallResult`.
        """
        wp = Path(wheel_path)
        if not wp.is_file():
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="?",
                detail=f"wheel file not found: {wp}",
            )

        # -- inspect wheel before touching the runtime --
        try:
            info = inspect_wheel(wp)
        except Exception as exc:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="?",
                detail=f"wheel inspection failed: {exc}",
            )

        if info.dist_info_dir is None:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="?",
                detail="wheel has no .dist-info directory",
            )

        wheel_version = info.version
        if wheel_version is None:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name="?",
                detail="wheel has no version in METADATA",
            )
        suffix = f"-{wheel_version}.dist-info"
        if info.dist_info_dir.endswith(suffix):
            dist_name = _normalise_distribution_name(info.dist_info_dir[: -len(suffix)])
        else:
            dist_name = _normalise_distribution_name(
                info.dist_info_dir.removesuffix(".dist-info").rsplit("-", 1)[0]
            )

        # -- ensure runtime is ready --
        st = self.status()
        if st.state != RuntimeState.READY:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"shared runtime is not READY (state={st.state.value})",
            )

        # -- pre-install contract validation (if definition supplied) --
        if component_definition is not None:
            # Distribution name must match (normalised).
            expected_dist = _normalise_distribution_name(
                component_definition.distribution_name
            )
            if dist_name != expected_dist:
                return InstallResult(
                    outcome=InstallOutcome.CONTRACT_MISMATCH,
                    distribution_name=dist_name,
                    detail=(
                        f"wheel distribution {dist_name!r} does not match "
                        f"expected {expected_dist!r}"
                    ),
                )
            # At least one expected entry point contract must be present
            # in the wheel metadata.
            if not _wheel_has_expected_contract(info, component_definition):
                expected_str = _format_expected_contracts(component_definition)
                observed_str = _format_observed_entry_points(info)
                return InstallResult(
                    outcome=InstallOutcome.CONTRACT_MISMATCH,
                    distribution_name=dist_name,
                    detail=(
                        f"wheel does not declare any expected launch contract. "
                        f"Expected: [{expected_str}]. "
                        f"Observed in wheel: [{observed_str}]"
                    ),
                )

        # -- probe existing installation (fail-closed) --
        try:
            probe = probe_runtime_distribution(st.python_executable, dist_name)  # type: ignore[arg-type]
        except Exception as exc:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"runtime probe failed: {exc}",
            )

        if probe.get("installed"):
            installed_version = probe.get("version")
            if installed_version != wheel_version:
                return InstallResult(
                    outcome=InstallOutcome.VERSION_MISMATCH,
                    distribution_name=dist_name,
                    version=installed_version,
                    detail=(
                        f"installed {installed_version}, "
                        f"requested wheel {wheel_version}"
                    ),
                )
            # Same version — verify contract if requested.
            if component_definition is not None:
                contract_ok = _check_contract_from_probe(
                    probe, component_definition
                )
                if not contract_ok:
                    return InstallResult(
                        outcome=InstallOutcome.CONTRACT_MISMATCH,
                        distribution_name=dist_name,
                        version=installed_version,
                        detail=f"installed {dist_name} {installed_version} does not satisfy the expected launch contract",
                    )
            return InstallResult(
                outcome=InstallOutcome.ALREADY_INSTALLED,
                distribution_name=dist_name,
                version=installed_version,
                detail=f"{dist_name} {installed_version} is already installed",
            )

        # -- install --
        result = subprocess.run(
            [
                str(st.python_executable),
                "-m",
                "pip",
                "install",
                "--no-index",
                "--no-deps",
                str(wp),
            ],
            capture_output=True,
            text=True,
            timeout=_INSTALL_TIMEOUT_SECONDS,
        )
        if result.returncode != 0:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"pip install failed (rc={result.returncode}):\n{result.stderr.strip()}",
            )

        # -- post-validation --
        try:
            probe = probe_runtime_distribution(st.python_executable, dist_name)  # type: ignore[arg-type]
        except Exception as exc:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=f"post-install probe failed: {exc}",
            )

        if not probe.get("installed"):
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail="distribution not found after install",
            )

        installed_version = probe.get("version")
        if installed_version != wheel_version:
            return InstallResult(
                outcome=InstallOutcome.FAILED,
                distribution_name=dist_name,
                detail=(
                    f"version mismatch after install: "
                    f"expected {wheel_version}, got {installed_version}"
                ),
            )

        # -- validate contract if requested --
        if component_definition is not None:
            contract_ok = _check_contract_from_probe(probe, component_definition)
            if not contract_ok:
                return InstallResult(
                    outcome=InstallOutcome.CONTRACT_MISMATCH,
                    distribution_name=dist_name,
                    version=installed_version,
                    detail=f"installed {dist_name} {installed_version} does not satisfy the expected launch contract",
                )

        return InstallResult(
            outcome=InstallOutcome.INSTALLED,
            distribution_name=dist_name,
            version=installed_version,
        )

    # -- convenience ----------------------------------------------------------

    def python(self) -> Path | None:
        """Return the runtime Python path, or ``None`` if not ready."""
        st = self.status()
        return st.python_executable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise_distribution_name(name: str) -> str:
    """Normalise a distribution name to its canonical form.

    Follows the PyPA packaging specification: lowercase, and replace every
    run of ``.``, ``_``, or ``-`` with a single ``-``.

    >>> _normalise_distribution_name("zealfie.witness")
    'zealfie-witness'
    >>> _normalise_distribution_name("ZeAlfie-._-Witness")
    'zealfie-witness'
    """
    import re

    return re.sub(r"[-_.]+", "-", name.strip()).lower()


def _check_contract_from_probe(
    probe: dict[str, object],
    definition: ComponentDefinition,
) -> bool:
    """Check whether the probe result satisfies the component definition's
    launch entry point contract.

    Returns ``True`` when at least one expected contract is matched by an
    entry point in the probe.  If the definition declares no launch entry
    points, the result is always ``True``.
    """
    expected = set(definition.launch_entry_points)
    if not expected:
        return True

    observed_eps = probe.get("entry_points", [])
    if not isinstance(observed_eps, list):
        return False

    for ep in observed_eps:
        contract = EntryPointContract(
            group=str(ep.get("group", "")),
            name=str(ep.get("name", "")),
        )
        if contract in expected:
            return True
    return False


def _wheel_has_expected_contract(
    info: "zealfie.building.InspectedWheel",
    definition: ComponentDefinition,
) -> bool:
    """Check whether the inspected wheel declares at least one expected
    launch entry point contract."""
    expected = set(definition.launch_entry_points)
    if not expected:
        return True
    for ep in info.entry_points:
        contract = EntryPointContract(group=ep.group, name=ep.name)
        if contract in expected:
            return True
    return False


def _format_expected_contracts(definition: ComponentDefinition) -> str:
    return ", ".join(
        f"{ep.group}:{ep.name}" for ep in definition.launch_entry_points
    )


def _format_observed_entry_points(
    info: "zealfie.building.InspectedWheel",
) -> str:
    return ", ".join(
        f"{ep.group}:{ep.name}" for ep in info.entry_points
    )


def _runtime_python(venv_dir: Path) -> Path | None:
    """Return the Python interpreter path for a given venv directory, or ``None``."""
    if sys.platform == "win32":
        candidate = venv_dir / "Scripts" / "python.exe"
    else:
        candidate = venv_dir / "bin" / "python"
    return candidate if candidate.is_file() else None
