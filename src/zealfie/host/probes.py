"""Read-only host capability probes (M1-2G).

The prober gathers *observations* only.  It never mutates the system, never
installs anything, and never selects a framework package.

Signals used (all read-only and bounded):

* ``platform`` module / ``sysconfig``  -> OS name + CPU architecture.
* ``nvidia-smi --query-gpu``           -> model names + driver version.
* Linux sysfs PCI (``/sys/bus/pci/devices``) -> NVIDIA hardware presence.
  (POSIX-only)
* ``/proc/driver/nvidia/version``      -> NVIDIA driver presence/version.
  (POSIX-only)
* ``/dev/nvidiactl``                   -> NVIDIA driver node presence.
  (POSIX-only)
* ``lspci`` (optional)                 -> vendor/kind names.  (POSIX-only)

On Windows only ``nvidia-smi`` (plus the platform probe) is consulted: the
POSIX-only probes are skipped and their absence is never treated as negative
evidence — insufficient evidence reports ``UNKNOWN`` instead.

Every failure becomes an ``UNAVAILABLE`` / ``UNKNOWN`` state object, never an
exception that escapes to the GUI or CLI.

Injectability
-------------

``HostProber`` accepts ``platform_provider``, ``command_runner``,
``file_reader``, ``path_exists``, and ``dir_lister`` callables.  Tests inject
fakes; no test requires real GPU hardware.  A clean "not present" signal
(file absent / command not installed / non-zero exit) is distinguished from a
probe *error* (an unexpected exception), so "driver absent" (-> BLOCKED) and
"driver probe error" (-> UNKNOWN) are never conflated.
"""

from __future__ import annotations

import os
import platform as _platform
import re
import subprocess
import sysconfig
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import (
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostCapabilities,
    HostReasonCode,
)

# ---------------------------------------------------------------------------
# Injectables / protocols
# ---------------------------------------------------------------------------

PlatformProvider = Callable[[], tuple[str, str]]
CommandRunner = Callable[[Sequence[str]], str]
FileReader = Callable[[str], str | None]
PathExists = Callable[[str], bool]
DirLister = Callable[[str], list[str]]


class CommandUnavailableError(RuntimeError):
    """The command is not installed / not on PATH."""


class CommandFailedError(RuntimeError):
    """The command ran but exited non-zero."""


# ---------------------------------------------------------------------------
# Default (real) implementations
# ---------------------------------------------------------------------------


def default_platform_provider() -> tuple[str, str]:
    """Return ``(os_name, cpu_arch)`` from the running interpreter."""
    os_name = _platform.system() or sys.platform or None
    arch = _platform.machine() or sysconfig.get_platform() or None
    return os_name or None, arch or None


def default_command_runner(argv: Sequence[str]) -> str:
    """Run *argv* read-only and return stripped stdout.

    Raises :class:`CommandUnavailableError` when the command is missing and
    :class:`CommandFailedError` on non-zero exit / timeout.
    """
    try:
        result = subprocess.run(
            list(argv),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError as exc:
        raise CommandUnavailableError(f"command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise CommandFailedError(f"command timed out: {argv[0]}") from exc
    if result.returncode != 0:
        raise CommandFailedError(
            f"command {argv[0]!r} exited with code {result.returncode}"
        )
    return result.stdout


def default_file_reader(path: str) -> str | None:
    """Read a file, returning ``None`` when it does not exist.

    A missing file is a clean "absent" signal.  Other ``OSError`` variants
    (permission, etc.) propagate so the prober can classify them as probe
    errors.
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").strip()
    except FileNotFoundError:
        return None


def default_path_exists(path: str) -> bool:
    return os.path.exists(path)


def default_dir_lister(path: str) -> list[str]:
    return os.listdir(path)


# ---------------------------------------------------------------------------
# Parsing helpers (pure, testable)
# ---------------------------------------------------------------------------

_NVIDIA_VERSION_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")

# NVIDIA PCI vendor id (0x10de).
NVIDIA_VENDOR_IDS = frozenset({"0x10de", "10de"})

_DISPLAY_CLASS_TOKENS = (
    "vga compatible controller",
    "3d controller",
    "display controller",
)


def parse_driver_version(content: str | None) -> str | None:
    """Extract a driver version from ``/proc/driver/nvidia/version`` text."""
    if not content:
        return None
    match = _NVIDIA_VERSION_RE.search(content)
    if match:
        return match.group(1)
    cleaned = content.strip()
    return cleaned[:64] if cleaned else None


def parse_nvidia_smi(stdout: str) -> tuple[tuple[tuple[str, str | None], ...], bool]:
    """Parse ``nvidia-smi --query-gpu=name,driver_version`` CSV output.

    Returns ``(entries, malformed)`` where each entry is ``(name,
    driver_version)``.  ``malformed`` is ``True`` when the output is present
    but could not be parsed into ``name, driver_version`` pairs.
    """
    text = stdout.strip()
    if not text:
        return (), True

    entries: list[tuple[str, str | None]] = []
    malformed = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2:
            malformed = True
            continue
        name = parts[0] or None
        version = parts[1] or None
        if name is None:
            malformed = True
            continue
        entries.append((name, version))
    return tuple(entries), malformed


@dataclass(frozen=True, slots=True)
class LspciGpu:
    """A GPU entry parsed from ``lspci`` output."""

    vendor: str
    kind: GpuKind
    model: str | None


def parse_lspci(stdout: str) -> list[LspciGpu]:
    """Parse ``lspci`` output for display-class GPU entries.

    Only VGA / 3D / display controllers are considered.  Vendor and kind are
    derived from well-known substrings; anything unrecognised is skipped.
    """
    gpus: list[LspciGpu] = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        lowered = line.lower()
        if not any(token in lowered for token in _DISPLAY_CLASS_TOKENS):
            continue
        vendor = _lspci_vendor(line)
        if vendor is None:
            continue
        gpus.append(
            LspciGpu(
                vendor=vendor,
                kind=_lspci_kind(vendor),
                model=_lspci_model(line),
            )
        )
    return gpus


def _lspci_vendor(line: str) -> str | None:
    lowered = line.lower()
    if "nvidia" in lowered:
        return "NVIDIA"
    if "intel" in lowered:
        return "Intel"
    if "amd" in lowered or "advanced micro devices" in lowered:
        return "AMD"
    return None


def _lspci_kind(vendor: str) -> GpuKind:
    if vendor == "Intel":
        return GpuKind.INTEGRATED
    if vendor == "NVIDIA":
        return GpuKind.DISCRETE
    return GpuKind.UNKNOWN


def _lspci_model(line: str) -> str | None:
    """Extract a bracketed model name (e.g. ``[GeForce RTX 3080]``)."""
    start = line.find("[")
    end = line.find("]", start + 1)
    if start != -1 and end != -1:
        return line[start + 1 : end].strip() or None
    return None


# ---------------------------------------------------------------------------
# Internal probe result carrier for nvidia-smi
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SmiResult:
    ran: bool
    entries: tuple[tuple[str, str | None], ...]
    malformed: bool
    error: bool


@dataclass(frozen=True, slots=True)
class _LspciResult:
    """Result carrier for the optional ``lspci`` fallback probe.

    ``unavailable`` (command not installed) is distinguished from ``error``
    (command ran/raised unexpectedly) so the hardware decision can tell "no
    observation" apart from "observed, no GPUs" and "probe failed".
    """

    gpus: list[LspciGpu]
    unavailable: bool
    error: bool


# ---------------------------------------------------------------------------
# HostProber
# ---------------------------------------------------------------------------


class HostProber:
    """Read-only host capability collector.

    ``collect()`` returns a :class:`HostCapabilities` observation and never
    raises: any probe failure is folded into the returned state object.
    """

    def __init__(
        self,
        *,
        platform_provider: PlatformProvider | None = None,
        command_runner: CommandRunner | None = None,
        file_reader: FileReader | None = None,
        path_exists: PathExists | None = None,
        dir_lister: DirLister | None = None,
    ) -> None:
        self._platform_provider = platform_provider or default_platform_provider
        self._command_runner = command_runner or default_command_runner
        self._file_reader = file_reader or default_file_reader
        self._path_exists = path_exists or default_path_exists
        self._dir_lister = dir_lister or default_dir_lister

    # ------------------------------------------------------------------
    # Public
    # ------------------------------------------------------------------

    def collect(self) -> HostCapabilities:
        """Collect host capabilities (read-only, never raises)."""
        reason_codes: list[HostReasonCode] = []

        os_name, cpu_arch, platform_status, platform_code, platform_reason = (
            self._probe_platform()
        )
        if platform_code is not None:
            reason_codes.append(platform_code)

        windows = bool(os_name) and os_name.strip().lower().startswith("windows")

        smi = self._probe_nvidia_smi()
        if windows:
            # Only nvidia-smi is evidence on Windows.  The POSIX-only probes
            # are meaningless there and must not be consulted: their absence
            # is not negative evidence.
            proc_status = None
            proc_version = None
            dev_present = None
            sysfs_present = None
            sysfs_error = False
            lspci = _LspciResult(gpus=[], unavailable=False, error=False)
        else:
            proc_status, proc_version = self._probe_proc_driver()
            dev_present = self._probe_dev_nvidiactl()
            sysfs_present, sysfs_error = self._probe_sysfs_nvidia()
            lspci = self._probe_lspci_gpus()

        driver_status, driver_version, driver_code, driver_reason = (
            _determine_nvidia_driver(
                proc_status, proc_version, dev_present, smi, windows=windows
            )
        )
        if driver_code is not None:
            reason_codes.append(driver_code)

        driver_seen = driver_status is CapabilityStatus.AVAILABLE
        nvidia_hw_present = _determine_nvidia_hardware(
            sysfs_present=sysfs_present,
            sysfs_error=sysfs_error,
            lspci_gpus=lspci.gpus,
            lspci_error=lspci.error,
            lspci_unavailable=lspci.unavailable,
            smi_ran=smi.ran or bool(smi.entries),
            driver_seen=driver_seen,
            windows=windows,
        )
        hardware_unknown = nvidia_hw_present is None

        gpus = _compose_gpus(
            nvidia_hw_present=nvidia_hw_present,
            smi=smi,
            lspci_gpus=lspci.gpus,
            driver_status=driver_status,
            driver_version=driver_version,
            driver_code=driver_code,
            driver_reason=driver_reason,
        )

        if gpus:
            reason_codes.append(HostReasonCode.GPU_HARDWARE_DETECTED)
        elif not hardware_unknown:
            reason_codes.append(HostReasonCode.NO_ACCELERATOR_HARDWARE)
        else:
            reason_codes.append(HostReasonCode.GPU_HARDWARE_UNKNOWN)

        partial = hardware_unknown or platform_status is CapabilityStatus.UNKNOWN
        if partial:
            reason_codes.append(HostReasonCode.PARTIAL_EVIDENCE)

        runtime_hints = _runtime_hints(cpu_arch, gpus)

        return HostCapabilities(
            os_name=os_name,
            cpu_arch=cpu_arch,
            platform_status=platform_status,
            platform_reason_code=platform_code,
            platform_reason=platform_reason,
            gpus=gpus,
            partial=partial,
            reason_codes=tuple(reason_codes),
            runtime_hints=runtime_hints,
        )

    # ------------------------------------------------------------------
    # Individual probes
    # ------------------------------------------------------------------

    def _probe_platform(self):
        try:
            os_name, cpu_arch = self._platform_provider()
        except Exception as exc:
            return None, None, CapabilityStatus.UNKNOWN, HostReasonCode.OS_PROBE_FAILED, (
                f"platform probe failed: {exc}"
            )
        os_name = str(os_name or "").strip() or None
        cpu_arch = str(cpu_arch or "").strip() or None
        if os_name is None and cpu_arch is None:
            return None, None, CapabilityStatus.UNKNOWN, HostReasonCode.OS_PROBE_FAILED, (
                "platform probe returned no OS or architecture"
            )
        return os_name, cpu_arch, CapabilityStatus.AVAILABLE, HostReasonCode.OS_DETECTED, None

    def _probe_nvidia_smi(self) -> _SmiResult:
        try:
            stdout = self._command_runner(
                ("nvidia-smi", "--query-gpu=name,driver_version",
                 "--format=csv,noheader,nounits")
            )
        except CommandUnavailableError:
            return _SmiResult(ran=False, entries=(), malformed=False, error=False)
        except CommandFailedError:
            # Ran but non-zero (e.g. no driver / no devices) — clean "not
            # usable via smi", not a probe error.
            return _SmiResult(ran=False, entries=(), malformed=False, error=False)
        except Exception:
            return _SmiResult(ran=False, entries=(), malformed=False, error=True)

        entries, malformed = parse_nvidia_smi(stdout)
        return _SmiResult(ran=True, entries=entries, malformed=malformed, error=False)

    def _probe_proc_driver(self) -> tuple[CapabilityStatus, str | None]:
        try:
            content = self._file_reader("/proc/driver/nvidia/version")
        except Exception:
            return CapabilityStatus.UNKNOWN, None
        if content is None or content == "":
            return CapabilityStatus.UNAVAILABLE, None
        return CapabilityStatus.AVAILABLE, parse_driver_version(content)

    def _probe_dev_nvidiactl(self) -> bool | None:
        try:
            return self._path_exists("/dev/nvidiactl")
        except Exception:
            return None

    def _probe_sysfs_nvidia(self) -> tuple[bool | None, bool]:
        try:
            names = self._dir_lister("/sys/bus/pci/devices")
        except Exception:
            return None, True
        found = False
        for name in names:
            try:
                vendor = self._file_reader(f"/sys/bus/pci/devices/{name}/vendor")
            except Exception:
                continue  # unreadable entry — skip, not fatal
            if vendor is not None and vendor.strip().lower() in NVIDIA_VENDOR_IDS:
                found = True
                break
        return found, False

    def _probe_lspci_gpus(self) -> _LspciResult:
        try:
            stdout = self._command_runner(("lspci",))
        except CommandUnavailableError:
            # Command absent — no positive/negative hardware observation.
            return _LspciResult(gpus=[], unavailable=True, error=False)
        except Exception:
            return _LspciResult(gpus=[], unavailable=False, error=True)
        return _LspciResult(gpus=parse_lspci(stdout), unavailable=False, error=False)


# ---------------------------------------------------------------------------
# Composition helpers (pure)
# ---------------------------------------------------------------------------


def _determine_nvidia_hardware(
    *,
    sysfs_present: bool | None,
    sysfs_error: bool,
    lspci_gpus: list[LspciGpu],
    lspci_error: bool,
    lspci_unavailable: bool,
    smi_ran: bool,
    driver_seen: bool,
    windows: bool = False,
) -> bool | None:
    """Return ``True``/``False``/``None`` (unknown) for NVIDIA hardware.

    ``smi`` success and a loaded NVIDIA driver are definitive presence.  When
    the primary sysfs probe errors and the optional ``lspci`` fallback either
    errors or is not installed (and there is no driver/smi signal), we have no
    positive or negative hardware observation, so hardware presence is
    unknown.  Otherwise a positive signal wins.

    On Windows there are no sysfs/lspci channels: without an ``smi``
    observation the hardware presence is simply unknown.
    """
    if smi_ran or driver_seen:
        return True
    if windows:
        return None
    if sysfs_error and (lspci_error or lspci_unavailable):
        return None
    positive = (sysfs_present is True) or any(
        g.vendor == "NVIDIA" for g in lspci_gpus
    )
    return positive


def _determine_nvidia_driver(
    proc_status: CapabilityStatus | None,
    proc_version: str | None,
    dev_present: bool | None,
    smi: _SmiResult,
    windows: bool = False,
) -> tuple[CapabilityStatus, str | None, HostReasonCode | None, str | None]:
    """Determine the NVIDIA driver tri-state from all driver signals."""
    if smi.ran and smi.entries and not smi.malformed:
        version = smi.entries[0][1] or proc_version
        return (
            CapabilityStatus.AVAILABLE,
            version,
            HostReasonCode.NVIDIA_DRIVER_AVAILABLE,
            "NVIDIA driver available",
        )
    if smi.malformed:
        return (
            CapabilityStatus.UNKNOWN,
            proc_version,
            HostReasonCode.NVIDIA_SMI_MALFORMED,
            "nvidia-smi produced unparseable output",
        )
    if smi.error:
        return (
            CapabilityStatus.UNKNOWN,
            proc_version,
            HostReasonCode.NVIDIA_DRIVER_UNKNOWN,
            "nvidia-smi probe failed",
        )
    if windows:
        return (
            CapabilityStatus.UNKNOWN,
            None,
            HostReasonCode.NVIDIA_DRIVER_UNKNOWN,
            "no NVIDIA driver evidence on Windows (nvidia-smi unavailable); "
            "driver status unknown",
        )
    if proc_status is CapabilityStatus.AVAILABLE:
        return (
            CapabilityStatus.AVAILABLE,
            proc_version,
            HostReasonCode.NVIDIA_DRIVER_AVAILABLE,
            "NVIDIA driver available",
        )
    if proc_status is CapabilityStatus.UNKNOWN:
        return (
            CapabilityStatus.UNKNOWN,
            None,
            HostReasonCode.NVIDIA_DRIVER_UNKNOWN,
            "NVIDIA driver probe failed",
        )
    if dev_present is True:
        return (
            CapabilityStatus.AVAILABLE,
            proc_version,
            HostReasonCode.NVIDIA_DRIVER_AVAILABLE,
            "NVIDIA driver node present",
        )
    return (
        CapabilityStatus.UNAVAILABLE,
        None,
        HostReasonCode.NVIDIA_DRIVER_UNAVAILABLE,
        "no NVIDIA driver detected",
    )


def _nvidia_model_from_lspci(lspci_gpus: list[LspciGpu]) -> str | None:
    for g in lspci_gpus:
        if g.vendor == "NVIDIA" and g.model:
            return g.model
    return None


def _compose_gpus(
    *,
    nvidia_hw_present: bool | None,
    smi: _SmiResult,
    lspci_gpus: list[LspciGpu],
    driver_status: CapabilityStatus,
    driver_version: str | None,
    driver_code: HostReasonCode | None,
    driver_reason: str | None,
) -> tuple[GpuInfo, ...]:
    gpus: list[GpuInfo] = []
    cuda_driver_present = driver_status is CapabilityStatus.AVAILABLE

    # NVIDIA GPUs — richest source is nvidia-smi (model + driver version).
    if smi.entries:
        for name, ver in smi.entries:
            gpus.append(
                GpuInfo(
                    vendor="NVIDIA",
                    model=name,
                    kind=GpuKind.DISCRETE,
                    hardware_present=True,
                    driver_status=driver_status,
                    driver_version=ver or driver_version,
                    driver_reason_code=driver_code,
                    driver_reason=driver_reason,
                    nvidia_smi_available=True,
                    cuda_driver_present=cuda_driver_present,
                )
            )
    elif nvidia_hw_present:
        gpus.append(
            GpuInfo(
                vendor="NVIDIA",
                model=_nvidia_model_from_lspci(lspci_gpus),
                kind=GpuKind.DISCRETE,
                hardware_present=True,
                driver_status=driver_status,
                driver_version=driver_version,
                driver_reason_code=driver_code,
                driver_reason=driver_reason,
                nvidia_smi_available=False,
                cuda_driver_present=cuda_driver_present,
            )
        )

    # Non-NVIDIA GPUs observed via lspci (e.g. Intel integrated).
    for lg in lspci_gpus:
        if lg.vendor == "NVIDIA":
            continue
        gpus.append(
            GpuInfo(
                vendor=lg.vendor,
                model=lg.model,
                kind=lg.kind,
                hardware_present=True,
                driver_status=CapabilityStatus.UNKNOWN,
                driver_version=None,
                driver_reason_code=None,
                driver_reason=None,
                nvidia_smi_available=False,
                cuda_driver_present=False,
            )
        )

    return tuple(gpus)


def _runtime_hints(cpu_arch: str | None, gpus: tuple[GpuInfo, ...]) -> tuple[str, ...]:
    """Informational hints for *future* runtime selection (never authoritative)."""
    hints: list[str] = []
    if cpu_arch:
        hints.append(f"arch:{cpu_arch}")
    if any(g.is_nvidia for g in gpus):
        hints.append("backend:nvidia_cuda")
    return tuple(hints)
