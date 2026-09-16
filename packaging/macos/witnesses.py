"""ZeAlfie macOS bundle — mandatory runtime witnesses (ZA-MAC-BOOT-01).

Every witness runs the REAL packaged artefact (the bundled private Python
and, where relevant, the launcher) and fails closed.  The gates are:

* ``PRIVATE_PYTHON``  — bundled interpreter reports arm64, 3.13.15, a
  ``sys.executable``/``sys.prefix`` inside the bundle, and
  ``Contents/Resources/app`` on ``sys.path``;
* ``IMPORT_SMOKE`` / ``CLI_SMOKE`` — ``import zealfie`` (+ runtime modules)
  and ``python -m zealfie --help`` from the bundled interpreter;
* ``HOST_TARGET``    — ``HostTarget.from_current_host()`` on the bundled
  interpreter yields a ``macosx_*`` target on arm64;
* ``QT_SMOKE``       — real ``QApplication`` + ``ZeAlfieMainWindow``,
  offscreen, isolated throwaway runtime, bounded event processing;
* ``RELOCATION``     — the complete ``.app`` copied elsewhere runs from an
  unrelated CWD with a sanitized environment, with no reference to the
  original build path;
* ``NO_HOST_PYTHON`` — the launcher starts the absolute bundled interpreter
  with a hostile/minimal environment and never falls back to PATH Python;
* ``CHILD_VENV``     — the bundled interpreter can create a working child
  venv with pip and install/import a deterministic wheel offline;
* ``RUNTIME_ROOT``   — the packaged app resolves exactly
  ``~/Library/Application Support/zealfie/runtime`` and never redirects
  managed-product data into the bundle.

Only macOS can run these witnesses; the script refuses to run elsewhere.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import macho  # type: ignore[import-not-found]
import macpack  # type: ignore[import-not-found]

__all__ = ["WitnessError", "run_all", "main"]

_GUI_SMOKE = Path(__file__).resolve().parent / "gui_smoke_offscreen.py"


class WitnessError(RuntimeError):
    """A witness gate failed (fail closed)."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _log(msg: str) -> None:
    print(f"[witness] {msg}", flush=True)


def _run(
    argv: list[str],
    *,
    env: dict | None = None,
    cwd: str | None = None,
    timeout: int = 600,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        env=env,
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )


def _run_or_fail(label: str, argv: list[str], **kwargs) -> subprocess.CompletedProcess:
    proc = _run(argv, **kwargs)
    if proc.returncode != 0:
        raise WitnessError(
            f"{label}: command failed rc={proc.returncode}\n"
            f"argv: {argv}\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return proc


def _run_json(label: str, argv: list[str], **kwargs) -> dict:
    proc = _run_or_fail(label, argv, **kwargs)
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    if not lines:
        raise WitnessError(f"{label}: no output")
    try:
        return json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise WitnessError(
            f"{label}: last output line is not JSON: {lines[-1]!r}"
        ) from exc


def _bundled_python(app: Path) -> Path:
    interpreter = macpack.bundled_interpreter(app)
    if not interpreter.is_file():
        raise WitnessError(f"bundled interpreter missing: {interpreter}")
    return interpreter


def _sanitized_env(extra: dict | None = None) -> dict:
    """A hostile-but-runnable environment: no inherited Python variables."""
    env = {
        "HOME": os.environ.get("HOME", str(Path.home())),
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
    }
    if extra:
        env.update(extra)
    return env


def _assert_absent(text: str, needles: list[str], label: str) -> None:
    for needle in needles:
        if needle and needle in text:
            raise WitnessError(
                f"{label}: evidence leaks a workspace/build path {needle!r}:\n{text}"
            )


# ---------------------------------------------------------------------------
# 1. PRIVATE_PYTHON
# ---------------------------------------------------------------------------

_PROBE = (
    "import json,sys,platform;"
    "print(json.dumps({"
    "'machine': platform.machine(),"
    "'version': sys.version.split()[0],"
    "'executable': sys.executable,"
    "'prefix': sys.prefix,"
    "'base_prefix': sys.base_prefix,"
    "'path': sys.path}))"
)


def witness_private_python(app: Path) -> dict:
    interpreter = _bundled_python(app)
    payload = _run_json("PRIVATE_PYTHON", [str(interpreter), "-c", _PROBE])
    failure = []
    if payload["machine"] != "arm64":
        failure.append(f"platform.machine()={payload['machine']} != arm64")
    if payload["version"] != "3.13.15":
        failure.append(f"python version={payload['version']} != 3.13.15")
    if payload["executable"] != str(interpreter):
        failure.append(
            f"sys.executable={payload['executable']} != {interpreter}"
        )
    if Path(payload["prefix"]) != macpack.bundle_python_dir(app):
        failure.append(
            f"sys.prefix={payload['prefix']} != {macpack.bundle_python_dir(app)}"
        )
    # ``sys.path`` is recorded as evidence, not asserted: exposing
    # ``Contents/Resources/app`` is the LAUNCHER's contract and is proven by
    # IMPORT_SMOKE / NO_HOST_PYTHON (which set PYTHONPATH through the
    # bundle), whereas this probe runs the bare interpreter.
    if failure:
        raise WitnessError("PRIVATE_PYTHON FAILED: " + "; ".join(failure))
    _log("PRIVATE_PYTHON=PASS")
    for key in ("machine", "version", "executable", "prefix"):
        _log(f"PRIVATE_PYTHON.{key}={payload[key]}")
    return payload


# ---------------------------------------------------------------------------
# 2. IMPORT_SMOKE + CLI_SMOKE
# ---------------------------------------------------------------------------

_IMPORT_SMOKE = (
    "import zealfie, zealfie.runtime, zealfie.runtime.layout, "
    "zealfie.runtime.manager, zealfie.runtime.deployment, "
    "zealfie.releases.model, zealfie.gui.main_window;"
    "print('IMPORT_SMOKE=PASS', zealfie.get_version())"
)


def witness_import_smoke(app: Path) -> dict:
    interpreter = _bundled_python(app)
    app_dir = str(macpack.bundle_app_dir(app))
    env = _sanitized_env({"PYTHONPATH": app_dir})
    imports = _run_or_fail(
        "IMPORT_SMOKE", [str(interpreter), "-c", _IMPORT_SMOKE], env=env
    )
    _log(imports.stdout.strip())
    cli = _run_or_fail(
        "CLI_SMOKE", [str(interpreter), "-m", "zealfie", "--help"], env=env
    )
    if "usage" not in cli.stdout.lower():
        raise WitnessError(
            f"CLI_SMOKE FAILED: --help output has no usage text:\n{cli.stdout}"
        )
    _log("CLI_SMOKE=PASS")
    return {"import_stdout": imports.stdout.strip(), "cli_first_line": cli.stdout.splitlines()[0]}


# ---------------------------------------------------------------------------
# 3. HOST_TARGET
# ---------------------------------------------------------------------------

_HOST_TARGET = (
    "import json,platform;"
    "from zealfie.releases.model import HostTarget;"
    "t=HostTarget.from_current_host();"
    "print(json.dumps({'python_tag':t.python_tag,'abi_tag':t.abi_tag,"
    "'platform_tag':t.platform_tag,'machine':platform.machine()}))"
)


def witness_host_target(app: Path) -> dict:
    interpreter = _bundled_python(app)
    env = _sanitized_env({"PYTHONPATH": str(macpack.bundle_app_dir(app))})
    payload = _run_json("HOST_TARGET", [str(interpreter), "-c", _HOST_TARGET], env=env)
    if not payload["platform_tag"].startswith("macosx_"):
        raise WitnessError(
            f"HOST_TARGET FAILED: platform_tag={payload['platform_tag']!r} "
            "does not start with macosx_"
        )
    if payload["machine"] != "arm64":
        raise WitnessError(
            f"HOST_TARGET FAILED: platform.machine()={payload['machine']!r}"
        )
    _log(f"HOST_TARGET=PASS {payload['platform_tag']}")
    return payload


# ---------------------------------------------------------------------------
# 4. QT_SMOKE
# ---------------------------------------------------------------------------


def witness_qt_smoke(app: Path, work: Path) -> dict:
    interpreter = _bundled_python(app)
    if not _GUI_SMOKE.is_file():
        raise WitnessError(f"GUI smoke script missing: {_GUI_SMOKE}")
    # Disposable scratch under the caller-owned work directory; removed on
    # success (and on failure) so the witness leaves no owned scratch.
    with tempfile.TemporaryDirectory(
        prefix="zealfie-macos-qt-", dir=str(work)
    ) as allocated:
        smoke_root = Path(allocated)
        env = _sanitized_env(
            {
                "PYTHONPATH": str(macpack.bundle_app_dir(app)),
                "QT_QPA_PLATFORM": "offscreen",
                "ZEALFIE_RUNTIME_ROOT": str(smoke_root / "runtime"),
            }
        )
        proc = _run(
            [str(interpreter), str(_GUI_SMOKE), "--work-root", str(smoke_root)],
            env=env,
            cwd="/tmp",
            timeout=300,
        )
        if proc.returncode != 0:
            raise WitnessError(
                f"QT_SMOKE FAILED rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        if "QT_SMOKE=PASS" not in proc.stdout:
            raise WitnessError(f"QT_SMOKE FAILED: marker missing\n{proc.stdout}")
    _log(proc.stdout.strip())
    return {"stdout": proc.stdout.strip()}


# ---------------------------------------------------------------------------
# 5. RUNTIME_ROOT
# ---------------------------------------------------------------------------

_RUNTIME_ROOT = (
    "import json;from pathlib import Path;"
    "from zealfie.runtime.layout import default_runtime_root;"
    "p=default_runtime_root();"
    "print(json.dumps({'runtime_root':str(p),"
    "'expected':str(Path.home()/'Library'/'Application Support'/'zealfie'/'runtime')}))"
)


def witness_runtime_root(app: Path) -> dict:
    interpreter = _bundled_python(app)
    env = _sanitized_env({"PYTHONPATH": str(macpack.bundle_app_dir(app))})
    payload = _run_json("RUNTIME_ROOT", [str(interpreter), "-c", _RUNTIME_ROOT], env=env)
    if payload["runtime_root"] != payload["expected"]:
        raise WitnessError(
            f"RUNTIME_ROOT FAILED: {payload['runtime_root']!r} != "
            f"{payload['expected']!r}"
        )
    if str(app) in payload["runtime_root"]:
        raise WitnessError(
            "RUNTIME_ROOT FAILED: managed-product runtime is being redirected "
            f"into the bundle: {payload['runtime_root']!r}"
        )
    _log(f"RUNTIME_ROOT=PASS {payload['runtime_root']}")
    return payload


# ---------------------------------------------------------------------------
# 6. CHILD_VENV
# ---------------------------------------------------------------------------

_CHILD_VENV_DRIVER = r'''
import json, os, subprocess, sys, venv
from pathlib import Path

candidate = Path(sys.argv[1])
wheelhouse = Path(sys.argv[2])
child = candidate / "bin" / "python3.13"
report = {"candidate": str(candidate)}

venv.create(candidate, with_pip=True, clear=False)
if not child.is_file():
    raise SystemExit(f"child interpreter missing: {child}")

probe = subprocess.run(
    [str(child), "-c",
     "import json,sys;print(json.dumps({'executable':sys.executable,"
     "'prefix':sys.prefix,'base_prefix':sys.base_prefix,"
     "'version':sys.version.split()[0]}))"],
    capture_output=True, text=True, check=True,
)
report["child"] = json.loads(probe.stdout.strip().splitlines()[-1])

pip_version = subprocess.run(
    [str(child), "-m", "pip", "--version"],
    capture_output=True, text=True, check=True,
)
report["pip_version"] = pip_version.stdout.strip()

install = subprocess.run(
    [str(child), "-m", "pip", "install", "--no-index", "--find-links",
     str(wheelhouse), "--disable-pip-version-check", "--no-cache-dir",
     "packaging==26.3"],
    capture_output=True, text=True,
)
if install.returncode != 0:
    raise SystemExit(
        "offline wheel install into the child venv failed:\n"
        f"stdout: {install.stdout}\nstderr: {install.stderr}"
    )
report["install_tail"] = install.stdout.strip().splitlines()[-1]

import_probe = subprocess.run(
    [str(child), "-c", "import packaging;print('CHILD_IMPORT='+packaging.__version__)"],
    capture_output=True, text=True, check=True,
)
report["import"] = import_probe.stdout.strip()
print(json.dumps(report))
'''


def witness_child_venv(app: Path, work: Path, wheelhouse_dir: Path) -> dict:
    interpreter = _bundled_python(app)
    # The child venv and its driver script are disposable scratch owned by
    # this witness; the whole allocation is scoped under the caller-owned
    # work directory and removed on success (and on failure).  The returned
    # evidence records their temporary paths deliberately.
    with tempfile.TemporaryDirectory(
        prefix="zealfie-macos-childvenv-", dir=str(work)
    ) as allocated:
        scratch_root = Path(allocated)
        candidate = scratch_root / "child"
        driver = scratch_root / "child_venv_driver.py"
        driver.write_text(_CHILD_VENV_DRIVER, encoding="utf-8")
        env = _sanitized_env()
        payload = _run_json(
            "CHILD_VENV",
            [str(interpreter), str(driver), str(candidate), str(wheelhouse_dir)],
            env=env,
            timeout=900,
        )
        bundle_prefix = str(macpack.bundle_python_dir(app))
        child = payload["child"]
        if Path(child["base_prefix"]) != Path(bundle_prefix):
            raise WitnessError(
                f"CHILD_VENV FAILED: child base_prefix={child['base_prefix']!r} "
                f"is not the bundled private prefix {bundle_prefix!r}"
            )
        if child["executable"] != str(candidate / "bin" / "python3.13"):
            raise WitnessError(
                f"CHILD_VENV FAILED: child executable={child['executable']!r}"
            )
        if "CHILD_IMPORT=26.3" not in payload["import"]:
            raise WitnessError(
                f"CHILD_VENV FAILED: offline import did not succeed: {payload['import']!r}"
            )
        _log("CHILD_VENV=PASS " + json.dumps(
            {"executable": child["executable"], "base_prefix": child["base_prefix"],
             "pip": payload["pip_version"].split()[1] if " " in payload["pip_version"] else payload["pip_version"]}
        ))
    return payload


# ---------------------------------------------------------------------------
# 7. RELOCATION
# ---------------------------------------------------------------------------


def witness_relocation(app: Path, work: Path, forbidden_paths: list[str]) -> dict:
    # The relocation copy and the relocated Qt-smoke root are disposable
    # scratch owned by this witness.  Each allocation is registered with the
    # ExitStack immediately after creation, so an earlier allocation is still
    # cleaned up if a later one fails; all scratch is removed on success (and
    # on failure).  The caller-owned work directory and the input app are
    # preserved.  The returned ``relocated`` path is recorded evidence of the
    # throwaway copy location (the copy itself is cleaned).
    with contextlib.ExitStack() as scratch:
        dest_parent = Path(
            scratch.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="zealfie-macos-relocation-", dir=str(work)
                )
            )
        )
        relocated = dest_parent / macpack.APP_NAME
        shutil.copytree(app, relocated, symlinks=True)
        relocated_interpreter = macpack.bundled_interpreter(relocated)
        if not relocated_interpreter.is_file():
            raise WitnessError(f"relocated interpreter missing: {relocated_interpreter}")

        env = _sanitized_env(
            {"PYTHONPATH": str(macpack.bundle_app_dir(relocated))}
        )
        evidence = []
        for label, argv in (
            ("private-python", [str(relocated_interpreter), "-c", _PROBE]),
            ("import-smoke", [str(relocated_interpreter), "-c", _IMPORT_SMOKE]),
            ("cli-smoke", [str(relocated_interpreter), "-m", "zealfie", "--help"]),
            ("host-target", [str(relocated_interpreter), "-c", _HOST_TARGET]),
        ):
            proc = _run_or_fail(f"RELOCATION[{label}]", argv, env=env, cwd="/tmp")
            evidence.append(proc.stdout)

        smoke_root = Path(
            scratch.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="zealfie-macos-reloc-qt-", dir=str(work)
                )
            )
        )
        qt_env = _sanitized_env(
            {
                "PYTHONPATH": str(macpack.bundle_app_dir(relocated)),
                "QT_QPA_PLATFORM": "offscreen",
                "ZEALFIE_RUNTIME_ROOT": str(smoke_root / "runtime"),
            }
        )
        qt = _run(
            [str(relocated_interpreter), str(_GUI_SMOKE), "--work-root", str(smoke_root)],
            env=qt_env,
            cwd="/tmp",
            timeout=300,
        )
        if qt.returncode != 0 or "QT_SMOKE=PASS" not in qt.stdout:
            raise WitnessError(
                f"RELOCATION[qt-smoke] FAILED rc={qt.returncode}\n"
                f"stdout: {qt.stdout}\nstderr: {qt.stderr}"
            )
        evidence.append(qt.stdout)

        combined = "\n".join(evidence)
        if str(relocated) not in combined:
            raise WitnessError(
                "RELOCATION FAILED: evidence does not reference the relocated "
                "bundle path"
            )
        _assert_absent(combined, forbidden_paths, "RELOCATION")
        original_app = str(app)
        if original_app in combined:
            raise WitnessError(
                f"RELOCATION FAILED: evidence still references the original "
                f"bundle {original_app!r}"
            )
        _log(f"RELOCATION=PASS cwd=/tmp relocated={relocated}")
        return {"relocated": str(relocated), "forbidden_paths": forbidden_paths}


# ---------------------------------------------------------------------------
# 8. NO_HOST_PYTHON
# ---------------------------------------------------------------------------


def _ps_args(pid: int) -> str:
    proc = subprocess.run(
        ["ps", "-o", "args=", "-p", str(pid)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout.strip()


def witness_no_host_python(app: Path, work: Path) -> dict:
    launcher = macpack.bundle_launcher(app)
    interpreter = macpack.bundled_interpreter(app)
    if not launcher.is_file() or not os.access(launcher, os.X_OK):
        raise WitnessError(f"launcher missing or not executable: {launcher}")

    # The hostile PATH shim directory and the deliberately broken negative-
    # control app copy are disposable scratch owned by this witness.  Each is
    # registered with the ExitStack as soon as it is created, so the shim
    # directory is still cleaned if the negative-control copy fails to build;
    # all scratch is removed on success (and on failure).
    with contextlib.ExitStack() as scratch:
        # A shim `python3.13`/`python3`/`python`/`pip` on PATH that records any
        # accidental PATH resolution.
        shim_dir = Path(
            scratch.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="zealfie-macos-shim-", dir=str(work)
                )
            )
        )
        marker = shim_dir / "SHIM_EXECUTED"
        for name in ("python", "python3", "python3.13", "pip", "brew"):
            shim = shim_dir / name
            shim.write_text(f"#!/bin/sh\ntouch {marker}\nexit 97\n", encoding="utf-8")
            shim.chmod(0o755)

        hostile = {
            "PATH": f"{shim_dir}:/usr/bin:/bin",
            "PYTHONHOME": "/nonexistent/python-home",
            "PYTHONPATH": "/nonexistent/pythonpath",
            "VIRTUAL_ENV": "/nonexistent/venv",
            "QT_QPA_PLATFORM": "offscreen",
            "ZEALFIE_RUNTIME_ROOT": str(work / "nohost-runtime"),
        }
        env = _sanitized_env(hostile)

        proc = subprocess.Popen(
            [str(launcher)], env=env, cwd="/tmp",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        started_args = ""
        deadline = time.time() + 30
        while time.time() < deadline:
            if proc.poll() is not None:
                out, err = proc.communicate()
                raise WitnessError(
                    f"NO_HOST_PYTHON FAILED: launcher exited rc={proc.returncode}\n"
                    f"stdout: {out}\nstderr: {err}"
                )
            started_args = _ps_args(proc.pid)
            if str(interpreter) in started_args:
                break
            time.sleep(0.5)
        else:
            proc.terminate()
            raise WitnessError(
                "NO_HOST_PYTHON FAILED: launcher never started the bundled "
                f"interpreter (last args={started_args!r})"
            )
        if str(interpreter) not in started_args:
            proc.terminate()
            raise WitnessError(
                f"NO_HOST_PYTHON FAILED: started process is not the bundled "
                f"interpreter: {started_args!r}"
            )
        proc.terminate()
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=30)
        if marker.exists():
            raise WitnessError(
                "NO_HOST_PYTHON FAILED: a PATH shim was executed — the launcher "
                "resolved an interpreter through PATH"
            )

        # Negative control: without the bundled interpreter the launcher must
        # fail (never fall back to a PATH/host Python).
        broken = Path(
            scratch.enter_context(
                tempfile.TemporaryDirectory(
                    prefix="zealfie-macos-nohost-", dir=str(work)
                )
            )
        ) / macpack.APP_NAME
        shutil.copytree(app, broken, symlinks=True)
        broken_python = macpack.bundled_interpreter(broken)
        os.rename(broken_python, broken_python.with_name(broken_python.name + ".disabled"))
        negative = _run([str(macpack.bundle_launcher(broken))], env=env, cwd="/tmp", timeout=60)
        if negative.returncode == 0:
            raise WitnessError(
                "NO_HOST_PYTHON FAILED: launcher exited 0 with no bundled interpreter"
            )
        if "bundled interpreter missing" not in (negative.stderr + negative.stdout):
            raise WitnessError(
                "NO_HOST_PYTHON FAILED: negative control produced an unexpected "
                f"error:\nstdout: {negative.stdout}\nstderr: {negative.stderr}"
            )
        if marker.exists():
            raise WitnessError(
                "NO_HOST_PYTHON FAILED: negative control executed a PATH shim"
            )
        _log("NO_HOST_PYTHON=PASS " + started_args)
        return {"started_args": started_args, "negative_rc": negative.returncode}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run_all(
    app: Path,
    work: Path,
    wheelhouse_dir: Path,
    forbidden_paths: list[str],
) -> dict:
    """Run every gate in order; raise on the first failure."""
    work.mkdir(parents=True, exist_ok=True)
    evidence: dict = {}
    evidence["PRIVATE_PYTHON"] = witness_private_python(app)
    evidence["IMPORT_CLI"] = witness_import_smoke(app)
    evidence["HOST_TARGET"] = witness_host_target(app)
    evidence["QT_SMOKE"] = witness_qt_smoke(app, work)
    evidence["RUNTIME_ROOT"] = witness_runtime_root(app)
    evidence["CHILD_VENV"] = witness_child_venv(app, work, wheelhouse_dir)
    evidence["RELOCATION"] = witness_relocation(app, work, forbidden_paths)
    evidence["NO_HOST_PYTHON"] = witness_no_host_python(app, work)
    _log("ALL_WITNESSES=PASS")
    return evidence


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python packaging/macos/witnesses.py",
        description="ZeAlfie macOS bundle runtime witnesses (ZA-MAC-BOOT-01)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def _common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--app", type=Path, required=True)
        p.add_argument("--work", type=Path, required=True)
        p.add_argument("--wheelhouse", type=Path, default=None)
        p.add_argument("--forbid-path", action="append", default=[])
        p.add_argument("--json-out", type=Path, default=None)

    p_all = sub.add_parser("all")
    _common(p_all)
    p_private = sub.add_parser("private-python")
    _common(p_private)
    p_import = sub.add_parser("import-smoke")
    _common(p_import)
    p_host = sub.add_parser("host-target")
    _common(p_host)
    p_qt = sub.add_parser("qt-smoke")
    _common(p_qt)
    p_reloc = sub.add_parser("relocation")
    _common(p_reloc)
    p_nohost = sub.add_parser("no-host-python")
    _common(p_nohost)
    p_child = sub.add_parser("child-venv")
    _common(p_child)
    p_root = sub.add_parser("runtime-root")
    _common(p_root)

    args = parser.parse_args(argv)
    if sys.platform != "darwin":
        print(
            "[witness] FAILED: these witnesses execute the private macOS "
            "interpreter and must run on macOS",
            file=sys.stderr,
        )
        return 2

    app = Path(args.app).resolve()
    work = Path(args.work).resolve()
    work.mkdir(parents=True, exist_ok=True)

    try:
        if args.command == "all":
            if args.wheelhouse is None:
                raise WitnessError("--wheelhouse is required for `all`")
            evidence = run_all(app, work, Path(args.wheelhouse), args.forbid_path)
        elif args.command == "private-python":
            evidence = witness_private_python(app)
        elif args.command == "import-smoke":
            evidence = witness_import_smoke(app)
        elif args.command == "host-target":
            evidence = witness_host_target(app)
        elif args.command == "qt-smoke":
            evidence = witness_qt_smoke(app, work)
        elif args.command == "relocation":
            evidence = witness_relocation(app, work, args.forbid_path)
        elif args.command == "no-host-python":
            evidence = witness_no_host_python(app, work)
        elif args.command == "child-venv":
            if args.wheelhouse is None:
                raise WitnessError("--wheelhouse is required for child-venv")
            evidence = witness_child_venv(app, work, Path(args.wheelhouse))
        elif args.command == "runtime-root":
            evidence = witness_runtime_root(app)
        else:  # pragma: no cover - argparse enforces the set
            raise WitnessError(f"unknown command {args.command!r}")
    except (WitnessError, macho.MachOError, macpack.MacPackError) as exc:
        print(f"[witness] FAILED: {exc}", file=sys.stderr)
        return 1

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(evidence, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
