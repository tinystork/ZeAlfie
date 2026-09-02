"""ZeAlfie Windows standalone bootstrap — thin runnable entrypoint.

ZA-WIN-BOOT-01.  Executes the REAL provisioning steps on a Windows machine
(driven by the GitHub ``windows-bootstrap-witness`` workflow or by a human):

    provision-python   download pinned CPython 3.13 installer -> verify its
                       SHA-256 against reproducibility.toml (fail closed) ->
                       silent per-user install into <root>\\python -> verify
                       the ACTUAL installed interpreter path, never assume.
    make-appenv        <root>\\python\\python.exe -m venv <root>\\appenv ->
                       install the built ZeAlfie wheel (+deps) into appenv.
    witness            isolation assertions + runtime-child capability
                       witness, executed BY the appenv python.
    smoke-cli          appenv\\Scripts\\zealfie.exe --version / --help.
    smoke-gui          bounded offscreen GUI instantiation (appenv python).
    all                provision-python -> make-appenv -> witness ->
                       smoke-cli -> smoke-gui (one-shot local run).
    diagnose           print interpreter identities + logs for failure
                       diagnostics.

Design rules:

* stdlib only — this module never imports ZeAlfie (the wheel under test is
  exercised only through subprocesses in the appenv) and never imports the
  PyPI ``packaging`` distribution (the sibling directory name is a plain
  folder, not an import package, so it can never shadow it).
* The runner's preinstalled Python may DRIVE provisioning; the application
  runtime (private python + appenv) never depends on it afterwards — the
  witness proves it with path provenance.
* Subprocesses on Windows use ``CREATE_NO_WINDOW`` (0x08000000) so nothing
  introduced here makes a terminal window architecturally mandatory
  (preserving the existing no-console behaviour).  No ``shell=True`` ever.
* Fail closed: any step failure exits non-zero with evidence to stderr.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# sys.path[0] is this script's directory when run as
# ``python packaging/windows/provision_windows.py``.
import provision  # type: ignore[import-not-found]  # same-directory module

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_DOWNLOAD_CHUNK = 65536
_INSTALLER_OK_RETURNCODES = (0, 3010)  # 3010 = success, reboot advised


class StepError(RuntimeError):
    """A provisioning step failed (fail closed)."""


def _log_dir(witness_root: Path) -> Path:
    return witness_root / "logs"


def _default_witness_root() -> Path:
    """Resolve the witness root.

    ``ZEALFIE_WITNESS_ROOT`` (when set, e.g. by the CI ``witness-root``
    step) is honored as the FULL witness root — it already carries the
    ``zealfie-windows-boot-witness`` subdirectory.  The local fallback
    appends that subdirectory to ``RUNNER_TEMP``/the platform temp dir so
    local and no-env runs keep a single, self-describing root.
    """
    explicit = os.environ.get("ZEALFIE_WITNESS_ROOT")
    if explicit:
        return Path(explicit)
    base = os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    return Path(base) / "zealfie-windows-boot-witness"


def _run(argv: list[str], *, timeout_s: int = 1500, **kwargs) -> subprocess.CompletedProcess:
    """Run a provisioning subprocess with no console window on Windows."""
    subprocess_kwargs = dict(kwargs)
    if sys.platform == "win32" and "creationflags" not in subprocess_kwargs:
        subprocess_kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(argv, timeout=timeout_s, **subprocess_kwargs)


def _probe(python_exe: Path) -> dict:
    proc = _run(
        provision.interpreter_probe_argv(python_exe),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise StepError(
            f"interpreter probe failed for {python_exe}: rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------


def step_provision_python(record, witness_root: Path, installer: Path | None) -> None:
    """Download (or reuse) the pinned installer, verify SHA-256, install."""
    private_dir = provision.private_python_dir(witness_root)
    private_exe = provision.private_python_exe(witness_root)
    downloads = witness_root / "downloads"
    logs = _log_dir(witness_root)
    downloads.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)

    if installer is None:
        installer = downloads / record.installer_filename
        if not installer.is_file():
            print(f"[provision-python] downloading {record.installer_url}")
            urllib.request.urlretrieve(record.installer_url, installer)  # noqa: S310 - pinned https URL
            print(f"[provision-python] downloaded {installer} "
                  f"({installer.stat().st_size} bytes)")
        else:
            print(f"[provision-python] reusing cached installer {installer}")

    # Fail closed on hash mismatch — never install an unverified payload.
    actual = provision.verify_installer_sha256(installer, record=record)
    print(f"[provision-python] sha256 verified: {actual}")

    install_log = logs / "python-install.log"
    argv = [str(installer), *provision.build_install_argv(
        record, target_dir=private_dir, log_path=install_log
    )]
    print("[provision-python] running silent per-user install into "
          f"{private_dir}")
    proc = _run(argv)
    if proc.returncode not in _INSTALLER_OK_RETURNCODES:
        tail = ""
        if install_log.is_file():
            tail = "\n".join(install_log.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()[-40:])
        raise StepError(
            f"python.org installer failed rc={proc.returncode}\n{tail}"
        )
    print(f"[provision-python] installer rc={proc.returncode} (0/3010 = ok)")

    # Verify the ACTUAL installed interpreter path — never assume.
    if not private_exe.is_file():
        listing = "\n".join(
            str(p) for p in sorted(private_dir.rglob("python*.exe"))
        ) or "(no python*.exe found under the target dir)"
        raise StepError(
            f"private interpreter not found at expected path {private_exe}\n"
            f"found under {private_dir}:\n{listing}"
        )
    report = _probe(private_exe)
    print("[provision-python] private interpreter verified: "
          f"{private_exe} (Python {report['version']})")
    print(f"[provision-python] sys.base_prefix={report['base_prefix']!r}")


def step_make_appenv(record, witness_root: Path, wheel: Path) -> None:
    """Create the appenv from the private python and install the wheel."""
    private_exe = provision.private_python_exe(witness_root)
    appenv = provision.appenv_dir(witness_root)
    appenv_python = provision.appenv_python_exe(witness_root)
    logs = _log_dir(witness_root)
    logs.mkdir(parents=True, exist_ok=True)

    if not private_exe.is_file():
        raise StepError(f"private python missing: {private_exe} — run "
                        "provision-python first")
    if not wheel.is_file():
        raise StepError(f"wheel not found: {wheel}")

    print(f"[make-appenv] creating appenv at {appenv} from {private_exe}")
    venv_log = logs / "appenv-venv.log"
    proc = _run(
        provision.venv_create_argv(private_exe, appenv),
        stdout=open(venv_log, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        raise StepError(
            f"venv creation failed rc={proc.returncode}; log: {venv_log}"
        )
    if not appenv_python.is_file():
        raise StepError(
            f"appenv interpreter missing after venv creation: {appenv_python}"
        )
    print(f"[make-appenv] appenv interpreter present: {appenv_python}")

    print(f"[make-appenv] installing wheel {wheel} into appenv (deps from PyPI)")
    pip_log = logs / "appenv-pip-install.log"
    proc = _run(
        provision.pip_install_wheel_argv(appenv_python, wheel),
        stdout=open(pip_log, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
    )
    if proc.returncode != 0:
        tail = "\n".join(pip_log.read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()[-40:])
        raise StepError(f"wheel install failed rc={proc.returncode}\n{tail}")
    print("[make-appenv] wheel installed")

    for launcher in ("zealfie.exe", "zealfie-gui.exe"):
        path = appenv / "Scripts" / launcher
        if not path.is_file():
            raise StepError(
                f"expected appenv launcher missing after install: {path}"
            )
        print(f"[make-appenv] launcher present: {path}")


def step_witness(witness_root: Path, child_root: Path) -> None:
    """Run the isolation + runtime-child witness WITH the appenv python."""
    appenv_python = provision.appenv_python_exe(witness_root)
    witness_script = Path(__file__).resolve().parent / "witness_runtime.py"
    if not appenv_python.is_file():
        raise StepError(f"appenv python missing: {appenv_python}")
    if not witness_script.is_file():
        raise StepError(f"witness script missing: {witness_script}")
    print("[witness] running isolation + runtime-child witness under the "
          "appenv interpreter")
    proc = _run(
        [
            str(appenv_python),
            str(witness_script),
            "--witness-root", str(witness_root),
            "--child-root", str(child_root),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        raise StepError(
            f"witness failed rc={proc.returncode}\n{proc.stdout}\n{proc.stderr}"
        )
    print(proc.stdout)


def step_smoke_cli(witness_root: Path) -> None:
    """Bounded CLI smoke from the INSTALLED appenv (never the checkout)."""
    appenv = provision.appenv_dir(witness_root)
    zealfie_exe = appenv / "Scripts" / "zealfie.exe"
    if not zealfie_exe.is_file():
        raise StepError(f"zealfie.exe missing in appenv: {zealfie_exe}")
    for label, flag in (("--version", "--version"), ("--help", "--help")):
        proc = _run([str(zealfie_exe), flag], capture_output=True, text=True)
        if proc.returncode != 0:
            raise StepError(
                f"zealfie.exe {flag} failed rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        head = "\n".join(proc.stdout.splitlines()[:4]) or "(empty stdout)"
        print(f"[smoke-cli] zealfie.exe {label} PASS (rc=0)\n{head}")


def step_smoke_gui(witness_root: Path) -> None:
    """Bounded offscreen GUI instantiation smoke (appenv python)."""
    appenv_python = provision.appenv_python_exe(witness_root)
    smoke_script = Path(__file__).resolve().parent / "gui_smoke_offscreen.py"
    work_root = witness_root / "gui-smoke-work"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    print("[smoke-gui] bounded offscreen GUI instantiation "
          "(QT_QPA_PLATFORM=offscreen)")
    proc = _run(
        [
            str(appenv_python),
            str(smoke_script),
            "--work-root", str(work_root),
        ],
        env=env,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise StepError(
            f"GUI smoke failed rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    print(proc.stdout)


def step_diagnose(witness_root: Path) -> None:
    """Print interpreter identities + logs for failure diagnostics."""
    print("[diagnose] witness root: " + str(witness_root))
    for label, exe in (
        ("private python", provision.private_python_exe(witness_root)),
        ("appenv python", provision.appenv_python_exe(witness_root)),
    ):
        if exe.is_file():
            report = _probe(exe)
            print(f"[diagnose] {label}: {exe}")
            print(f"[diagnose]   version={report['version']} "
                  f"base_prefix={report['base_prefix']!r}")
        else:
            print(f"[diagnose] {label}: MISSING at {exe}")
    roots = provision.forbidden_python_roots(
        localappdata=os.environ.get("LOCALAPPDATA")
    )
    print("[diagnose] forbidden runner-python roots:")
    for root in roots:
        print(f"[diagnose]   - {root}")
    logs = _log_dir(witness_root)
    print(f"[diagnose] logs under {logs}:")
    if logs.is_dir():
        for path in sorted(logs.iterdir()):
            print(f"[diagnose]   - {path.name} ({path.stat().st_size} bytes)")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python packaging/windows/provision_windows.py",
        description="ZeAlfie Windows standalone bootstrap (ZA-WIN-BOOT-01)",
    )
    parser.add_argument(
        "--witness-root",
        type=Path,
        default=None,
        help=r"witness root (default: %%RUNNER_TEMP%%\zealfie-windows-boot-witness)",
    )
    parser.add_argument(
        "--record",
        type=Path,
        default=None,
        help="path to reproducibility.toml (default: sibling of this script)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prov = sub.add_parser("provision-python", help="download+verify+install private CPython")
    p_prov.add_argument("--installer", type=Path, default=None,
                        help="reuse a local installer copy instead of downloading")

    p_app = sub.add_parser("make-appenv", help="create appenv and install the wheel")
    p_app.add_argument("--wheel", type=Path, required=True)

    sub.add_parser("witness", help="isolation + runtime-child witness (appenv python)")

    sub.add_parser("smoke-cli", help="bounded CLI smoke from the installed appenv")
    sub.add_parser("smoke-gui", help="bounded offscreen GUI instantiation smoke")
    sub.add_parser("diagnose", help="print interpreter identities and logs")

    sub.add_parser("all", help="provision-python -> make-appenv -> witness -> smokes")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    record = provision.load_record(args.record)
    witness_root = (args.witness_root or _default_witness_root()).resolve()
    witness_root.mkdir(parents=True, exist_ok=True)
    child_root = witness_root / "child-witness"

    print(f"[bootstrap] zealfie {record.zealfie_version} @ {record.zealfie_revision[:12]}")
    print(f"[bootstrap] pinned CPython {record.cpython_version} "
          f"({record.architecture}) sha256={record.sha256[:16]}...")
    print(f"[bootstrap] witness root: {witness_root}")

    steps = {
        "provision-python": lambda: step_provision_python(
            record, witness_root, getattr(args, "installer", None)
        ),
        "make-appenv": lambda: step_make_appenv(
            record, witness_root, getattr(args, "wheel", None)
        ),
        "witness": lambda: step_witness(witness_root, child_root),
        "smoke-cli": lambda: step_smoke_cli(witness_root),
        "smoke-gui": lambda: step_smoke_gui(witness_root),
        "diagnose": lambda: step_diagnose(witness_root),
    }

    if args.command == "all":
        if getattr(args, "wheel", None) is None:
            print("error: 'all' requires --wheel", file=sys.stderr)
            return 2
        selected = ["provision-python", "make-appenv", "witness",
                    "smoke-cli", "smoke-gui"]
    else:
        selected = [args.command]

    for command in selected:
        print(f"\n=== step: {command} ===")
        try:
            steps[command]()
        except (StepError, provision.WindowsBootstrapError) as exc:
            print(f"[bootstrap] STEP {command} FAILED: {exc}", file=sys.stderr)
            print("[bootstrap] run 'diagnose' and upload the witness logs",
                  file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
