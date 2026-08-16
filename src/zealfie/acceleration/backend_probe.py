"""Backend compute probes (ZA-M1-2J.2 Phase F).

Registry of pre-activation *compute probes* — self-contained scripts
executed inside the candidate venv by the accelerated deployment gate
(see :mod:`zealfie.acceleration.deployment`) — plus the generic lookup
function.

A compute probe proves that the freshly built candidate runtime can do
REAL backend work: import the accelerated framework, allocate and
reduce device memory, and compile + launch a JIT kernel (the exact
failure mode of the M1-2J.1 witness: ``libnvrtc.so.12`` missing, so the
first real computation failed although the wheel installed fine).

Design constraints (enforced by review):

* the registry is the ONLY place that knows about concrete accelerated
  frameworks — ``deployment.py`` stays generic (no cupy, no CUDA names);
* each probe script is executed by the candidate's own interpreter via
  stdin (``<candidate_python> -``), never imports ZeAlfie code, and
  prints ``BACKEND_COMPUTE_PROBE_OK`` on success; on any exception it
  prints ``BACKEND_COMPUTE_PROBE_FAIL: <type>: <msg>`` + the traceback
  tail and exits 1;
* a backend without a registered probe keeps the previous
  distribution/version-only gate behaviour.

The real NVIDIA_CUDA probe below was validated on the physical MX150
(driver 550.163.01) in the Phase C/D sandbox; the unit tests never
execute it against real hardware — they inject synthetic scripts.
"""

from __future__ import annotations

import textwrap

# ---------------------------------------------------------------------------
# Probe scripts (executed inside the candidate venv — no zealfie imports)
# ---------------------------------------------------------------------------

_NVIDIA_CUDA_PROBE_SCRIPT = textwrap.dedent(
    """\
    import sys


    def main() -> None:
        # Import the accelerated framework itself (M1-2J.1 lesson: a
        # green install is not a green compute path).
        import cupy as cp  # noqa: F401  (candidate-venv import)

        # A CUDA device must be visible to this runtime.
        device_count = cp.cuda.runtime.getDeviceCount()
        if device_count < 1:
            raise RuntimeError(
                "no CUDA device visible to the runtime "
                f"(getDeviceCount={device_count})"
            )

        # Real compute: allocate + arange + reduction, with an exact
        # expected value (sum of arange(64) == 2016.0).
        values = cp.zeros(64, dtype=cp.float64) + cp.arange(64, dtype=cp.float64)
        total = float(cp.sum(values))
        if total != 2016.0:
            raise RuntimeError(
                f"unexpected reduction result: expected 2016.0, got {total!r}"
            )

        # NVRTC JIT: compile a minimal RawKernel, launch it, synchronize
        # and verify the result (this is the path that failed on the
        # M1-2J.1 witness because libnvrtc.so.12 was absent).
        kernel = cp.RawKernel(
            r'''
    extern "C" __global__
    void probe_double(const float* in, float* out, int n) {
        int i = blockIdx.x * blockDim.x + threadIdx.x;
        if (i < n) {
            out[i] = in[i] * 2.0f;
        }
    }
    ''',
            "probe_double",
        )
        src = cp.arange(32, dtype=cp.float32)
        dst = cp.empty_like(src)
        kernel((1,), (32,), (src, dst, 32))
        cp.cuda.Device().synchronize()
        if not bool(cp.all(dst == src * 2.0)):
            raise RuntimeError("RawKernel result mismatch after synchronize")

        print("BACKEND_COMPUTE_PROBE_OK")


    if __name__ == "__main__":
        try:
            main()
        except Exception as exc:  # noqa: BLE001 — report, never swallow
            import traceback

            print(
                f"BACKEND_COMPUTE_PROBE_FAIL: "
                f"{type(exc).__name__}: {exc}"
            )
            traceback.print_exc()
            sys.exit(1)
    """
)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

#: Registered compute probes, keyed by accelerator backend.
#: ``script`` is the self-contained probe executed via the candidate
#: interpreter's stdin; ``label`` is a human-readable description used
#: in gate error messages.
BACKEND_COMPUTE_PROBES: dict[str, dict[str, str]] = {
    "NVIDIA_CUDA": {
        "label": (
            "CUDA compute probe (cupy import, device count, "
            "arange/sum, RawKernel NVRTC)"
        ),
        "script": _NVIDIA_CUDA_PROBE_SCRIPT,
    },
}


def get_backend_compute_probe(backend: str | None) -> dict[str, str] | None:
    """Return the registered compute probe for *backend*, or ``None``.

    ``None`` (or any non-registered backend string) means the gate keeps
    its previous distribution/version-only behaviour — the registry is
    purely additive, never a forced requirement.
    """
    if not isinstance(backend, str) or not backend.strip():
        return None
    return BACKEND_COMPUTE_PROBES.get(backend.strip())
