"""Windows bootstrap runtime witness — runs INSIDE the appenv interpreter.

ZA-WIN-BOOT-01 isolation + runtime-child capability witness.  This script is
executed with the **appenv** python (``<witness-root>\\appenv\\Scripts\\python.exe``)
so that every ``sys.*`` value it records is a live observation of the
provisioned application runtime — never of the driving (runner) python.

Two consecutive witnesses:

1. **Isolation assertions** — record and assert ``sys.executable``,
   ``sys.prefix``, ``sys.base_prefix``, ``sys._base_executable`` plus the
   appenv's ``pyvenv.cfg home``.  Fails closed when the appenv is not a venv
   of the private CPython (``<witness-root>\\python``) or when any anchor
   resolves to a forbidden runner/system Python root
   (``C:\\hostedtoolcache\\windows\\Python\\*``,
   ``C:\\Program Files\\Python*``, ``%LOCALAPPDATA%\\Programs\\Python\\*``).
   Path provenance only — never version-string comparison.

2. **Runtime-child capability witness** — from the appenv python, create a
   child venv into an isolated CI test root via the SAME mechanism class the
   shared runtime uses (``venv.create(path, with_pip=True)``,
   cf. ``src/zealfie/runtime/deployment.py``), then assert the child's
   ``Scripts\\python.exe`` exists, pip works, and the child's
   ``sys.base_prefix`` / ``pyvenv.cfg home`` derive from the private CPython.

The real user shared runtime (``%LOCALAPPDATA%\\zealfie\\runtime``) is never
touched: no import of ``zealfie.runtime`` state, no layout access, and the
child root is fully isolated under the witness tree.

Exit code 0 only when every assertion passes; each failure is printed to
stderr with the recorded evidence before a non-zero exit (fail closed).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import venv
from pathlib import Path

# The witness always runs with its own directory on sys.path (invoked as a
# script by the entrypoint), so the pure provisioning module is importable.
import provision  # type: ignore[import-not-found]  # same-directory module


def _run(python_exe: Path, argv_extra: list[str]) -> str:
    """Run *python_exe* with a probe argv; return stdout.  Fail closed."""
    proc = subprocess.run(
        [str(python_exe), *argv_extra],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if proc.returncode != 0:
        raise SystemExit(
            f"probe failed with rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc.stdout


def _probe_json(python_exe: Path) -> dict:
    """Probe an interpreter's provenance and decode its JSON report."""
    out = _run(python_exe, ["-c", _PROBE_SOURCE])
    payload = json.loads(out.strip().splitlines()[-1])
    return {
        "executable": payload["executable"],
        "prefix": payload["prefix"],
        "base_prefix": payload["base_prefix"],
        "base_executable": payload["base_executable"],
        "version": payload["version"],
    }


_PROBE_SOURCE = (
    "import json,sys;"
    "print(json.dumps({"
    "'executable': sys.executable,"
    "'prefix': sys.prefix,"
    "'base_prefix': sys.base_prefix,"
    "'base_executable': getattr(sys, '_base_executable', None),"
    "'version': sys.version.split()[0]}))"
)


def _print_evidence(tag: str, **values) -> None:
    print(f"[witness] {tag}")
    for key, value in values.items():
        print(f"[witness]   {key} = {value}")


def _witness_isolation(appenv_python: Path, witness_root: Path) -> None:
    """Assert the running appenv interpreter is a venv of the private python."""
    print("[witness] == Isolation assertions ==")
    assert Path(sys.executable).resolve() == appenv_python.resolve(), (
        "witness must run under the appenv python, got "
        f"{sys.executable}"
    )
    report = _probe_json(appenv_python)

    private_py_dir = provision.private_python_dir(witness_root)
    appenv = provision.appenv_dir(witness_root)
    cfg_home = provision.pyvenv_cfg_home(appenv)

    _print_evidence(
        "appenv interpreter observation",
        sys_executable=report["executable"],
        sys_prefix=report["prefix"],
        sys_base_prefix=report["base_prefix"],
        sys_base_executable=report["base_executable"],
        pyvenv_cfg_home=cfg_home,
        private_cpython_dir=str(private_py_dir),
        python_version=report["version"],
    )

    provision.assert_appenv_provenance(
        sys_executable=report["executable"],
        sys_prefix=report["prefix"],
        sys_base_prefix=report["base_prefix"],
        witness_root=witness_root,
        label="appenv (isolation)",
    )
    # The decisive rejection: NONE of the recorded anchors may sit under a
    # forbidden runner/system Python root.  The private install dir lives
    # under the witness root, so it can never collide with the canonical
    # forbidden roots (hostedtoolcache / Program Files / per-user Programs).
    provision.assert_no_runner_python(
        executable=report["executable"],
        prefix=report["prefix"],
        base_prefix=report["base_prefix"],
        base_executable=report["base_executable"],
        pyvenv_cfg_home=cfg_home,
        localappdata=os.environ.get("LOCALAPPDATA"),
    )
    print("[witness] isolation assertions PASS")


def _witness_child(appenv_python: Path, child_root: Path, witness_root: Path) -> None:
    """Create a runtime-style child venv and prove it derives from the private python."""
    print("[witness] == Runtime-child capability witness ==")
    private_py_dir = provision.private_python_dir(witness_root)

    child_root = child_root / "child-slot"
    if child_root.exists():
        # venv.create(..., clear=False) semantics: a pre-existing directory
        # is a conflict for a fresh witness — remove only OUR isolated root.
        import shutil

        shutil.rmtree(child_root)

    # The SAME mechanism class the shared runtime uses for candidate slots:
    # venv.create(path, with_pip=True, clear=False).
    venv.create(child_root, with_pip=True, clear=False)

    child_python = (
        child_root / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else child_root / "bin" / "python"
    )
    if not child_python.is_file():
        raise SystemExit(
            f"child venv has no Scripts/python.exe: {child_python!s}"
        )
    print(f"[witness] child venv created at {child_root}")
    print(f"[witness] child interpreter present: {child_python}")

    # pip works inside the child.
    pip_out = subprocess.run(
        [str(child_python), "-m", "pip", "--version"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    if pip_out.returncode != 0:
        raise SystemExit(
            f"child pip --version failed rc={pip_out.returncode}\n"
            f"stdout: {pip_out.stdout}\nstderr: {pip_out.stderr}"
        )
    print(f"[witness] child pip works: {pip_out.stdout.strip()}")

    child_report = _probe_json(child_python)
    child_cfg_home = provision.pyvenv_cfg_home(child_root)

    _print_evidence(
        "child interpreter observation",
        sys_executable=child_report["executable"],
        sys_base_prefix=child_report["base_prefix"],
        sys_base_executable=child_report["base_executable"],
        pyvenv_cfg_home=child_cfg_home,
        private_cpython_dir=str(private_py_dir),
    )

    provision.assert_child_venv_provenance(
        pyvenv_cfg_home=child_cfg_home,
        sys_base_prefix=child_report["base_prefix"],
        child_scripts_python=child_report["executable"],
        private_python_dir_path=private_py_dir,
        label="child venv (runtime mechanism)",
    )
    provision.assert_no_runner_python(
        executable=child_report["executable"],
        prefix=child_report["prefix"],
        base_prefix=child_report["base_prefix"],
        base_executable=child_report["base_executable"],
        pyvenv_cfg_home=child_cfg_home,
        localappdata=os.environ.get("LOCALAPPDATA"),
    )
    print("[witness] runtime-child capability witness PASS")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zealfie-boot-witness",
        description="ZeAlfie Windows bootstrap isolation + child-venv witness",
    )
    parser.add_argument("--witness-root", required=True, type=Path)
    parser.add_argument("--child-root", required=True, type=Path)
    args = parser.parse_args(argv)

    witness_root = args.witness_root.resolve()
    appenv_python = provision.appenv_python_exe(witness_root)
    _witness_isolation(appenv_python, witness_root)
    _witness_child(appenv_python, args.child_root.resolve(), witness_root)
    print("[witness] ALL WITNESS ASSERTIONS PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as exc:  # noqa: BLE001 — fail closed with evidence
        print(f"[witness] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
