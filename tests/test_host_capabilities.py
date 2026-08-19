"""Hermetic tests for M1-2G host capability models, probes, and recommender.

No test here touches real hardware — every probe is injected via fakes.
"""

from __future__ import annotations

from zealfie.host import (
    HostProber,
    build_gpu_setup_intent,
    recommend,
)
from zealfie.host.models import (
    CapabilityStatus,
    GpuInfo,
    GpuKind,
    HostCapabilities,
    HostReasonCode,
    RecommendationStatus,
)
from zealfie.host.probes import (
    CommandFailedError,
    CommandUnavailableError,
    LspciGpu,
    _determine_nvidia_hardware,
    parse_driver_version,
    parse_lspci,
    parse_nvidia_smi,
)


# ===========================================================================
# Probe fake builder
# ===========================================================================


def make_prober(
    *,
    platform=("Linux", "x86_64"),
    platform_raises=None,
    smi_stdout=None,
    smi_raises=None,
    smi_unavailable=False,
    lspci_stdout="",
    lspci_raises=None,
    lspci_unavailable=False,
    proc_content=None,
    proc_raises=None,
    dev_exists=False,
    sysfs_vendors=(),
    sysfs_raises=None,
) -> HostProber:
    """Build a HostProber with fully injected signals."""

    def platform_provider():
        if platform_raises is not None:
            raise platform_raises
        return platform

    def command_runner(argv):
        name = argv[0]
        if name == "nvidia-smi":
            if smi_unavailable:
                raise CommandUnavailableError("nvidia-smi not installed")
            if smi_raises is not None:
                raise smi_raises
            return smi_stdout or ""
        if name == "lspci":
            if lspci_unavailable:
                raise CommandUnavailableError("lspci not installed")
            if lspci_raises is not None:
                raise lspci_raises
            return lspci_stdout
        raise CommandUnavailableError(name)

    def file_reader(path):
        prefix = "/sys/bus/pci/devices/"
        if path.startswith(prefix) and path.endswith("/vendor"):
            idx_part = path[len(prefix):].split("/")[0]
            try:
                idx = int(idx_part)
            except ValueError:
                return None
            if 0 <= idx < len(sysfs_vendors):
                return sysfs_vendors[idx]
            return None
        if path == "/proc/driver/nvidia/version":
            if proc_raises is not None:
                raise proc_raises
            return proc_content
        return None

    def path_exists(path):
        return bool(dev_exists)

    def dir_lister(path):
        if sysfs_raises is not None:
            raise sysfs_raises
        return [str(i) for i in range(len(sysfs_vendors))]

    return HostProber(
        platform_provider=platform_provider,
        command_runner=command_runner,
        file_reader=file_reader,
        path_exists=path_exists,
        dir_lister=dir_lister,
    )


# ===========================================================================
# 1) CPU-only
# ===========================================================================


def test_cpu_only_not_applicable():
    prober = make_prober(
        sysfs_vendors=(),
        smi_unavailable=True,
        lspci_stdout="",
        proc_content=None,
        dev_exists=False,
    )
    caps = prober.collect()
    assert caps.gpu_count == 0
    assert caps.has_gpu is False
    assert caps.partial is False

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.NOT_APPLICABLE
    assert rec.backend == "NVIDIA_CUDA"


# ===========================================================================
# 2) NVIDIA GPU + driver operational
# ===========================================================================


def test_nvidia_driver_operational_offer_setup():
    prober = make_prober(
        smi_stdout="NVIDIA GeForce RTX 4090, 560.35.03\n",
    )
    caps = prober.collect()
    assert caps.gpu_count == 1
    gpu = caps.gpus[0]
    assert gpu.vendor == "NVIDIA"
    assert gpu.model == "NVIDIA GeForce RTX 4090"
    assert gpu.driver_status is CapabilityStatus.AVAILABLE
    assert gpu.driver_version == "560.35.03"

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.OFFER_SETUP
    assert rec.offer_setup is True


# ===========================================================================
# 3) NVIDIA detected + driver absent
# ===========================================================================


def test_nvidia_driver_absent_blocked():
    prober = make_prober(
        sysfs_vendors=("0x10de",),
        smi_unavailable=True,
        proc_content=None,
        dev_exists=False,
        lspci_stdout="",
    )
    caps = prober.collect()
    assert caps.gpu_count == 1
    assert caps.gpus[0].driver_status is CapabilityStatus.UNAVAILABLE

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.BLOCKED
    assert rec.blocked is True


# ===========================================================================
# 4) NVIDIA detected + driver probe error
# ===========================================================================


def test_nvidia_driver_probe_error_unknown():
    prober = make_prober(
        sysfs_vendors=("0x10de",),
        smi_unavailable=True,
        proc_raises=PermissionError("permission denied"),
        dev_exists=False,
        lspci_stdout="",
    )
    caps = prober.collect()
    assert caps.gpus[0].driver_status is CapabilityStatus.UNKNOWN

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN


# ===========================================================================
# 5) nvidia-smi absent (driver still detectable via /proc)
# ===========================================================================


def test_nvidia_smi_absent_but_driver_detected_via_proc():
    prober = make_prober(
        sysfs_vendors=("0x10de",),
        smi_unavailable=True,
        proc_content="NVRM version: NVIDIA UNIX x86_64 Kernel Module  550.163.01",
        dev_exists=False,
        lspci_stdout="",
    )
    caps = prober.collect()
    assert caps.gpus[0].driver_status is CapabilityStatus.AVAILABLE
    assert caps.gpus[0].driver_version == "550.163.01"
    assert caps.gpus[0].nvidia_smi_available is False

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.OFFER_SETUP


def test_nvidia_driver_detected_via_device_node():
    # C1 Linux fallback evidence: /dev/nvidiactl present, no /proc content,
    # no nvidia-smi — the device node alone signals a loaded driver.
    prober = make_prober(
        sysfs_vendors=("0x10de",),
        smi_unavailable=True,
        proc_content=None,
        dev_exists=True,
        lspci_stdout="",
    )
    caps = prober.collect()
    assert caps.gpu_count == 1
    assert caps.gpus[0].driver_status is CapabilityStatus.AVAILABLE
    assert caps.gpus[0].driver_version is None
    assert caps.gpus[0].nvidia_smi_available is False

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.OFFER_SETUP


# ===========================================================================
# 6) invalid nvidia-smi output
# ===========================================================================


def test_invalid_nvidia_smi_output_unknown():
    prober = make_prober(
        sysfs_vendors=("0x10de",),
        smi_stdout="this is not a csv line\n",
        proc_content=None,
        dev_exists=False,
        lspci_stdout="",
    )
    caps = prober.collect()
    assert caps.gpus[0].driver_status is CapabilityStatus.UNKNOWN

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN


# ===========================================================================
# 7) multiple GPUs
# ===========================================================================


def test_multiple_gpus():
    prober = make_prober(
        smi_stdout=(
            "NVIDIA GeForce RTX 4090, 560.35.03\n"
            "NVIDIA GeForce RTX 4080, 560.35.03\n"
        ),
    )
    caps = prober.collect()
    assert caps.gpu_count == 2
    assert [g.model for g in caps.gpus] == [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 4080",
    ]


# ===========================================================================
# 8) Intel integrated + NVIDIA discrete
# ===========================================================================


def test_intel_integrated_plus_nvidia_discrete():
    prober = make_prober(
        smi_stdout="NVIDIA GeForce MX150, 550.163.01\n",
        lspci_stdout=(
            "00:02.0 VGA compatible controller: Intel Corporation UHD Graphics 620\n"
            "01:00.0 3D controller: NVIDIA Corporation GP108M [GeForce MX150] (rev a1)\n"
        ),
    )
    caps = prober.collect()
    # NVIDIA from smi + Intel from lspci (NVIDIA lspci entry is deduped).
    nvidia = [g for g in caps.gpus if g.vendor == "NVIDIA"]
    intel = [g for g in caps.gpus if g.vendor == "Intel"]
    assert len(nvidia) == 1
    assert len(intel) == 1
    assert nvidia[0].kind is GpuKind.DISCRETE
    assert intel[0].kind is GpuKind.INTEGRATED


# ===========================================================================
# 9) OS probe unavailable
# ===========================================================================


def test_os_probe_unavailable_unknown():
    prober = make_prober(platform_raises=RuntimeError("no platform module"))
    caps = prober.collect()
    assert caps.platform_status is CapabilityStatus.UNKNOWN
    assert caps.partial is True

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN


# ===========================================================================
# 10) partial result -> UNKNOWN
# ===========================================================================


def test_partial_evidence_unknown():
    # Both sysfs and lspci error -> hardware presence unknown -> partial.
    prober = make_prober(
        sysfs_raises=OSError("cannot read sysfs"),
        lspci_raises=RuntimeError("lspci crashed"),
        smi_unavailable=True,
        proc_content=None,
        dev_exists=False,
    )
    caps = prober.collect()
    assert caps.gpu_count == 0
    assert caps.partial is True

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN


# ===========================================================================
# 11) sysfs error + lspci unavailable -> partial UNKNOWN (edge case)
# ===========================================================================


def test_sysfs_error_lspci_unavailable_partial_unknown():
    # Primary sysfs probe errored and the optional fallback command is not
    # installed — neither a positive nor a negative observation is available.
    prober = make_prober(
        sysfs_raises=OSError("cannot read sysfs"),
        lspci_unavailable=True,
        smi_unavailable=True,
        proc_content=None,
        dev_exists=False,
    )
    caps = prober.collect()
    assert caps.gpu_count == 0
    assert caps.partial is True
    assert HostReasonCode.GPU_HARDWARE_UNKNOWN in caps.reason_codes
    assert HostReasonCode.PARTIAL_EVIDENCE in caps.reason_codes

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN


def test_sysfs_clean_empty_lspci_unavailable_not_applicable():
    # sysfs is readable and empty (a definitive negative) even though the
    # optional lspci fallback is absent — clean negative, not partial.
    prober = make_prober(
        sysfs_vendors=(),
        lspci_unavailable=True,
        smi_unavailable=True,
        proc_content=None,
        dev_exists=False,
    )
    caps = prober.collect()
    assert caps.gpu_count == 0
    assert caps.partial is False
    assert HostReasonCode.NO_ACCELERATOR_HARDWARE in caps.reason_codes

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.NOT_APPLICABLE


def test_sysfs_error_lspci_nvidia_driver_absent_blocked():
    # sysfs errored but lspci observed NVIDIA hardware; driver absent -> BLOCKED.
    prober = make_prober(
        sysfs_raises=OSError("cannot read sysfs"),
        lspci_stdout=(
            "01:00.0 3D controller: NVIDIA Corporation GA102 [GeForce RTX 3080]\n"
        ),
        smi_unavailable=True,
        proc_content=None,
        dev_exists=False,
    )
    caps = prober.collect()
    assert caps.partial is False
    assert caps.gpu_count == 1
    assert caps.gpus[0].vendor == "NVIDIA"
    assert caps.gpus[0].driver_status is CapabilityStatus.UNAVAILABLE

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.BLOCKED


def test_sysfs_error_lspci_nvidia_driver_available_offer_setup():
    # sysfs errored but lspci observed NVIDIA hardware; driver detected via
    # /proc -> OFFER_SETUP.
    prober = make_prober(
        sysfs_raises=OSError("cannot read sysfs"),
        lspci_stdout=(
            "01:00.0 3D controller: NVIDIA Corporation GA102 [GeForce RTX 3080]\n"
        ),
        smi_unavailable=True,
        proc_content=(
            "NVRM version: NVIDIA UNIX x86_64 Kernel Module  550.163.01"
        ),
        dev_exists=False,
    )
    caps = prober.collect()
    assert caps.partial is False
    assert caps.gpu_count == 1
    assert caps.gpus[0].vendor == "NVIDIA"
    assert caps.gpus[0].driver_status is CapabilityStatus.AVAILABLE

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.OFFER_SETUP


# ===========================================================================
# Platform-aware probing: Windows relies on nvidia-smi only
# ===========================================================================


def make_windows_prober(
    *,
    smi_stdout=None,
    smi_raises=None,
    smi_unavailable=False,
) -> HostProber:
    """Build a HostProber whose POSIX-only probes must never be called.

    On Windows only the platform probe and ``nvidia-smi`` are evidence.  The
    file_reader / path_exists / dir_lister injectables and any command other
    than ``nvidia-smi`` raise :class:`AssertionError`, proving they are not
    consulted.
    """

    def platform_provider():
        return ("Windows", "AMD64")

    def command_runner(argv):
        if argv[0] == "nvidia-smi":
            if smi_unavailable:
                raise CommandUnavailableError("nvidia-smi not installed")
            if smi_raises is not None:
                raise smi_raises
            return smi_stdout or ""
        raise AssertionError(f"unexpected command on Windows: {argv}")

    def file_reader(path):
        raise AssertionError(f"file_reader must not be consulted on Windows: {path}")

    def path_exists(path):
        raise AssertionError(f"path_exists must not be consulted on Windows: {path}")

    def dir_lister(path):
        raise AssertionError(f"dir_lister must not be consulted on Windows: {path}")

    return HostProber(
        platform_provider=platform_provider,
        command_runner=command_runner,
        file_reader=file_reader,
        path_exists=path_exists,
        dir_lister=dir_lister,
    )


def test_windows_smi_success_single_gpu_detected():
    prober = make_windows_prober(
        smi_stdout="NVIDIA GeForce RTX 4090, 560.35.03\n",
    )
    caps = prober.collect()
    assert caps.os_name == "Windows"
    assert caps.gpu_count == 1
    gpu = caps.gpus[0]
    assert gpu.vendor == "NVIDIA"
    assert gpu.model == "NVIDIA GeForce RTX 4090"
    assert gpu.driver_status is CapabilityStatus.AVAILABLE
    assert gpu.driver_version == "560.35.03"
    assert gpu.nvidia_smi_available is True
    assert HostReasonCode.GPU_HARDWARE_DETECTED in caps.reason_codes
    assert HostReasonCode.NVIDIA_DRIVER_AVAILABLE in caps.reason_codes
    assert caps.partial is False
    assert HostReasonCode.NO_ACCELERATOR_HARDWARE not in caps.reason_codes

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.OFFER_SETUP


def test_windows_smi_success_two_gpus_both_represented():
    prober = make_windows_prober(
        smi_stdout=(
            "NVIDIA GeForce RTX 4090, 560.35.03\n"
            "NVIDIA GeForce RTX 4080, 560.35.03\n"
        ),
    )
    caps = prober.collect()
    assert caps.gpu_count == 2
    assert [g.model for g in caps.gpus] == [
        "NVIDIA GeForce RTX 4090",
        "NVIDIA GeForce RTX 4080",
    ]
    assert all(g.vendor == "NVIDIA" for g in caps.gpus)
    assert caps.partial is False


def test_windows_smi_malformed_honest_unknown_never_unavailable():
    # Garbage output must yield an honest UNKNOWN — never a fabricated
    # "driver absent" or "no hardware" conclusion, and never an exception.
    prober = make_windows_prober(smi_stdout="this is not a csv line\n")
    caps = prober.collect()
    assert HostReasonCode.NVIDIA_SMI_MALFORMED in caps.reason_codes
    assert HostReasonCode.NVIDIA_DRIVER_UNAVAILABLE not in caps.reason_codes
    assert HostReasonCode.NO_ACCELERATOR_HARDWARE not in caps.reason_codes
    assert all(g.driver_status is not CapabilityStatus.AVAILABLE for g in caps.gpus)
    assert all(
        g.driver_status is not CapabilityStatus.UNAVAILABLE for g in caps.gpus
    )
    for gpu in caps.gpus:
        assert gpu.driver_status is CapabilityStatus.UNKNOWN

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN


def test_windows_smi_command_error_driver_and_hardware_unknown():
    # nvidia-smi ran but failed (e.g. exit code / no devices): on Windows
    # that is still *no* evidence — never a negative conclusion.
    prober = make_windows_prober(
        smi_raises=CommandFailedError("nvidia-smi exited with code 6"),
    )
    caps = prober.collect()
    assert caps.gpus == ()
    assert HostReasonCode.NVIDIA_DRIVER_UNKNOWN in caps.reason_codes
    assert HostReasonCode.GPU_HARDWARE_UNKNOWN in caps.reason_codes
    assert caps.partial is True
    assert HostReasonCode.NVIDIA_DRIVER_UNAVAILABLE not in caps.reason_codes
    assert HostReasonCode.NO_ACCELERATOR_HARDWARE not in caps.reason_codes


def test_windows_no_nvidia_evidence_honest_unknown_not_blocked():
    # nvidia-smi not installed: absence of POSIX probe results must not be
    # mistaken for "driver absent" or "no accelerator hardware".
    prober = make_windows_prober(smi_unavailable=True)
    caps = prober.collect()
    assert caps.gpus == ()
    assert HostReasonCode.NVIDIA_DRIVER_UNKNOWN in caps.reason_codes
    assert HostReasonCode.GPU_HARDWARE_UNKNOWN in caps.reason_codes
    assert HostReasonCode.PARTIAL_EVIDENCE in caps.reason_codes
    assert HostReasonCode.NVIDIA_DRIVER_UNAVAILABLE not in caps.reason_codes
    assert HostReasonCode.NO_ACCELERATOR_HARDWARE not in caps.reason_codes
    assert caps.partial is True

    rec = recommend(caps)
    assert rec.status is RecommendationStatus.UNKNOWN
    assert rec.status is not RecommendationStatus.BLOCKED
    assert rec.status is not RecommendationStatus.NOT_APPLICABLE


def test_windows_smi_invocation_uses_nounits_format_argv():
    """W-BUG-01 regression guard: the ``nvidia-smi`` query argv must carry
    ``nounits`` (never the invalid ``nouuid``) so real Windows drivers
    produce parseable ``name, driver_version`` CSV lines.

    A recording command runner captures the observable argv handed to the
    injected CommandRunner seam; every other injectable raises, proving
    nothing but the platform probe and ``nvidia-smi`` is consulted on
    Windows.
    """
    recorded: list[tuple[str, ...]] = []

    def command_runner(argv):
        recorded.append(tuple(argv))
        if argv[0] == "nvidia-smi":
            return "NVIDIA GeForce RTX 3070 Laptop GPU, 576.80\n"
        raise AssertionError(f"unexpected command on Windows: {argv}")

    def file_reader(path):
        raise AssertionError(
            f"file_reader must not be consulted on Windows: {path}"
        )

    def path_exists(path):
        raise AssertionError(
            f"path_exists must not be consulted on Windows: {path}"
        )

    def dir_lister(path):
        raise AssertionError(
            f"dir_lister must not be consulted on Windows: {path}"
        )

    prober = HostProber(
        platform_provider=lambda: ("Windows", "AMD64"),
        command_runner=command_runner,
        file_reader=file_reader,
        path_exists=path_exists,
        dir_lister=dir_lister,
    )
    caps = prober.collect()

    # collect() sees the GPU from the realistic working smi output.
    assert caps.gpu_count == 1
    gpu = caps.gpus[0]
    assert gpu.vendor == "NVIDIA"
    assert gpu.model == "NVIDIA GeForce RTX 3070 Laptop GPU"
    assert gpu.driver_version == "576.80"
    assert gpu.driver_status is CapabilityStatus.AVAILABLE

    # The observable argv passed to the command runner is the fixed one.
    assert len(recorded) == 1
    argv = recorded[0]
    assert argv == (
        "nvidia-smi",
        "--query-gpu=name,driver_version",
        "--format=csv,noheader,nounits",
    )
    assert any("nounits" in part for part in argv)
    assert not any("nouuid" in part for part in argv)


# ===========================================================================
# Direct hardware-determination helper tests
# ===========================================================================


def test_determine_hardware_sysfs_error_lspci_unavailable_is_unknown():
    result = _determine_nvidia_hardware(
        sysfs_present=None,
        sysfs_error=True,
        lspci_gpus=[],
        lspci_error=False,
        lspci_unavailable=True,
        smi_ran=False,
        driver_seen=False,
    )
    assert result is None


def test_determine_hardware_sysfs_clean_empty_lspci_unavailable_is_false():
    result = _determine_nvidia_hardware(
        sysfs_present=False,
        sysfs_error=False,
        lspci_gpus=[],
        lspci_error=False,
        lspci_unavailable=True,
        smi_ran=False,
        driver_seen=False,
    )
    assert result is False


def test_determine_hardware_sysfs_error_lspci_nvidia_is_true():
    result = _determine_nvidia_hardware(
        sysfs_present=None,
        sysfs_error=True,
        lspci_gpus=[
            LspciGpu(vendor="NVIDIA", kind=GpuKind.DISCRETE, model="RTX 3080")
        ],
        lspci_error=False,
        lspci_unavailable=False,
        smi_ran=False,
        driver_seen=False,
    )
    assert result is True


# ===========================================================================
# Pure parser helpers
# ===========================================================================


def test_parse_driver_version_extracts_semver():
    assert parse_driver_version("NVRM version: ...  550.163.01  ...") == "550.163.01"
    assert parse_driver_version("") is None
    assert parse_driver_version(None) is None


def test_parse_nvidia_smi_multiple_lines():
    entries, malformed = parse_nvidia_smi(
        "NVIDIA GeForce RTX 4090, 560.35.03\nNVIDIA GeForce RTX 4080, 560.35.03\n"
    )
    assert malformed is False
    assert len(entries) == 2


def test_parse_nvidia_smi_invalid():
    entries, malformed = parse_nvidia_smi("garbage-without-comma\n")
    assert malformed is True
    assert entries == ()


def test_parse_lspci_nvidia_and_intel():
    gpus = parse_lspci(
        "01:00.0 3D controller: NVIDIA Corporation GA102 [GeForce RTX 3080]\n"
        "00:02.0 VGA compatible controller: Intel Corporation CometLake-U\n"
    )
    vendors = {g.vendor for g in gpus}
    assert vendors == {"NVIDIA", "Intel"}


# ===========================================================================
# GpuSetupIntent is no-mutation and honest
# ===========================================================================


def test_gpu_setup_intent_never_claims_install():
    prober = make_prober(
        smi_stdout="NVIDIA GeForce RTX 4090, 560.35.03\n",
    )
    rec = recommend(prober.collect())
    intent = build_gpu_setup_intent(rec)
    assert intent.actionable is True
    assert intent.performed_any_mutation is False
    message = intent.message.lower()
    # The intent must never claim a toolkit/runtime was installed, and must
    # never claim this version cannot install one.
    assert "did not install" not in message
    assert "not performed by this version" not in message
    assert "installed a" not in message
