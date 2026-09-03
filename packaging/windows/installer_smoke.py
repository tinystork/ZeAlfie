"""ZeAlfie Windows installer — installed-layout install/provenance smoke (ZA-WIN-BOOT-02).

Runs INSIDE the INSTALLED appenv interpreter
(``{app}\\appenv\\Scripts\\python.exe``) against the INSTALLED layout
(``--install-root {app}``), so every ``sys.*`` value observed is a live
reading of the user installation — never of the checkout and never of the
driving (runner/system) python.  Assertions (fail closed, exit code 0 only
when every one passes):

1. ``{app}\\python\\python.exe`` exists and reports the PINNED CPython
   version from the reproducibility record;
2. the appenv is COMPLETE: ``{app}\\appenv\\Scripts\\{python.exe,
   pythonw.exe, zealfie.exe, zealfie-gui.exe}`` all exist;
3. the running interpreter's provenance is exactly the installer layout:
   ``sys.executable -> {app}\\appenv\\Scripts\\python.exe``,
   ``sys.prefix -> {app}\\appenv``, ``sys.base_prefix -> {app}\\python``,
   ``pyvenv.cfg home -> {app}\\python`` — and NO recorded anchor resolves to
   a runner/system preinstalled Python root (path provenance only);
4. the installed ZeAlfie is importable from the appenv's own site-packages
   (never the checkout/source tree);
5. CLI smoke: ``{app}\\appenv\\Scripts\\zealfie.exe --version`` and
   ``--help`` exit 0;
6. bounded offscreen GUI smoke through the INSTALLED appenv interpreter
   (never the checkout);
7. the appenv install was OFFLINE: the pip log
   (``{app}\\logs\\appenv-pip-install.log``) shows the bundled-wheelhouse
   path (a recorded ``[zealfie-offline] argv:`` banner with
   ``--no-index --find-links``, pip's ``Looking in links`` output, and no
   http(s) URL evidence — newer pip no longer prints ``Ignoring indexes``)
   and the bundled
   wheelhouse (``{app}\\assets\\wheelhouse``) still contains the zealfie
   wheel — proving ``--no-index --find-links`` was used and that a repair /
   reinstall can be offline too.

Design rules (mirror the BOOT-01 witness): stdlib-only apart from the
sibling ``provision`` module; ``CREATE_NO_WINDOW`` for subprocesses; every
failure printed to stderr with evidence before a non-zero exit.
"""

from __future__ import annotations

import argparse
import json
import ntpath
import os
import subprocess
import sys
from pathlib import Path

# sys.path[0] is this script's directory ({app}\assets\bootstrap when run
# from the installed copy), so the pure provisioning module is importable.
import provision  # type: ignore[import-not-found]  # same-directory module

CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)

_EXPECTED_LOG_MARKERS = ("Looking in links", "[zealfie-offline] argv:")
_OFFLINE_ARGV_TOKENS = ("--no-index", "--find-links")


class SmokeError(RuntimeError):
    """An installer smoke assertion failed (fail closed)."""


def _run(argv: list[str], *, timeout_s: int = 600, **kwargs) -> subprocess.CompletedProcess:
    subprocess_kwargs = dict(kwargs)
    if sys.platform == "win32" and "creationflags" not in subprocess_kwargs:
        subprocess_kwargs["creationflags"] = CREATE_NO_WINDOW
    return subprocess.run(argv, timeout=timeout_s, **subprocess_kwargs)


def _probe(python_exe: Path) -> dict:
    probe = (
        "import json,sys;"
        "print(json.dumps({"
        "'executable': sys.executable,"
        "'prefix': sys.prefix,"
        "'base_prefix': sys.base_prefix,"
        "'base_executable': getattr(sys, '_base_executable', None),"
        "'version': sys.version.split()[0]}))"
    )
    proc = _run(
        [str(python_exe), "-c", probe],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SmokeError(
            f"interpreter probe failed for {python_exe}: rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _log(msg: str) -> None:
    print(msg, flush=True)


def _smoke_private_python(install_root: Path, record) -> None:
    private_exe = provision.private_python_exe(install_root)
    if not private_exe.is_file():
        raise SmokeError(
            f"private CPython missing at the expected installer path: "
            f"{private_exe}"
        )
    report = _probe(private_exe)
    observed = report["version"]
    if observed != record.cpython_version:
        raise SmokeError(
            f"private CPython version mismatch: expected "
            f"{record.cpython_version}, got {observed} at {private_exe}"
        )
    _log(f"[smoke] private CPython OK: {private_exe} (Python {observed})")


def _smoke_appenv_complete(install_root: Path) -> None:
    missing = provision.missing_appenv_launchers(install_root)
    if missing:
        raise SmokeError(
            "appenv incomplete — missing launchers: " + ", ".join(missing)
        )
    _log("[smoke] appenv complete: python.exe, pythonw.exe, zealfie.exe, "
         "zealfie-gui.exe all present under appenv\\Scripts")


def _smoke_self_provenance(install_root: Path, record) -> None:
    """Assert the RUNNING interpreter is the installed appenv on the
    installer's private CPython (``{app}\\python``)."""
    appenv = provision.appenv_dir(install_root)
    appenv_python = provision.appenv_python_exe(install_root)
    cfg_home = provision.pyvenv_cfg_home(appenv)
    observed = {
        "sys.executable": sys.executable,
        "sys.prefix": sys.prefix,
        "sys.base_prefix": sys.base_prefix,
        "sys._base_executable": getattr(sys, "_base_executable", None),
        "pyvenv.cfg home": cfg_home,
    }
    _log("[smoke] running interpreter observations:")
    for key, value in observed.items():
        _log(f"[smoke]   {key} = {value}")

    provision.assert_appenv_provenance(
        sys_executable=observed["sys.executable"],
        sys_prefix=observed["sys.prefix"],
        sys_base_prefix=observed["sys.base_prefix"],
        witness_root=install_root,
        label="installed appenv (installer smoke)",
    )
    provision.assert_no_runner_python(
        executable=observed["sys.executable"],
        prefix=observed["sys.prefix"],
        base_prefix=observed["sys.base_prefix"],
        base_executable=observed["sys._base_executable"],
        pyvenv_cfg_home=cfg_home,
        localappdata=os.environ.get("LOCALAPPDATA"),
    )
    _log("[smoke] installed appenv provenance OK: base_prefix = "
         f"{ntpath.normcase(str(observed['sys.base_prefix']))} == "
         f"{ntpath.normcase(str(install_root / 'python'))} "
         "(the installer's private CPython, not a runner/system python)")


def _smoke_import_location(install_root: Path) -> None:
    """The installed zealfie must import from the appenv site-packages."""
    import zealfie  # noqa: PLC0415 — must resolve from the appenv

    expected_prefix = ntpath.normcase(
        str(install_root / "appenv" / "Lib" / "site-packages")
    )
    file_path = ntpath.normcase(os.path.abspath(zealfie.__file__))
    if not file_path.startswith(expected_prefix):
        raise SmokeError(
            "installed zealfie imports from OUTSIDE the appenv site-packages: "
            f"{zealfie.__file__} (expected under {expected_prefix})"
        )
    _log(f"[smoke] installed zealfie import OK: {zealfie.__file__}")


def _smoke_cli(install_root: Path) -> None:
    appenv = provision.appenv_dir(install_root)
    zealfie_exe = appenv / "Scripts" / "zealfie.exe"
    for label, flag in (("--version", "--version"), ("--help", "--help")):
        proc = _run([str(zealfie_exe), flag], capture_output=True, text=True)
        if proc.returncode != 0:
            raise SmokeError(
                f"zealfie.exe {flag} failed rc={proc.returncode}\n"
                f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        head = "\n".join(proc.stdout.splitlines()[:3]) or "(empty stdout)"
        _log(f"[smoke] zealfie.exe {label} PASS (rc=0): {head}")


def _smoke_gui(install_root: Path) -> None:
    """Bounded offscreen GUI smoke through the INSTALLED appenv python."""
    smoke_script = Path(__file__).resolve().parent / "gui_smoke_offscreen.py"
    if not smoke_script.is_file():
        raise SmokeError(f"gui smoke script missing next to installer_smoke: "
                         f"{smoke_script}")
    work_root = install_root / "logs" / "gui-smoke-work"
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    proc = _run(
        [sys.executable, str(smoke_script), "--work-root", str(work_root)],
        env=env, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout_s=600,
    )
    if proc.returncode != 0:
        raise SmokeError(
            f"offscreen GUI smoke failed rc={proc.returncode}\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    _log(f"[smoke] offscreen GUI PASS: "
         f"{proc.stdout.strip().splitlines()[-1]}")


def _smoke_offline_provenance(install_root: Path, record) -> None:
    """The appenv install used the bundled wheelhouse (--no-index)."""
    logs = install_root / "logs"
    pip_log = logs / "appenv-pip-install.log"
    if not pip_log.is_file():
        raise SmokeError(
            f"appenv pip install log missing: {pip_log} — cannot prove the "
            "offline (--no-index --find-links) install path"
        )
    content = pip_log.read_text(encoding="utf-8", errors="replace")
    for marker in _EXPECTED_LOG_MARKERS:
        if marker not in content:
            raise SmokeError(
                f"appenv pip install log does not prove the offline install: "
                f"marker {marker!r} not found in {pip_log} (PyPI may have "
                "been contacted)"
            )
    banner = next(
        (line for line in content.splitlines()
         if line.startswith("[zealfie-offline] argv:")), ""
    )
    for token in _OFFLINE_ARGV_TOKENS:
        if token not in banner:
            raise SmokeError(
                f"offline argv banner missing {token!r}: {banner!r}"
            )
    # A wheelhouse-only install never touches a URL: reject any http(s)
    # evidence in the pip output (newer pip no longer prints an explicit
    # "Ignoring indexes" line, so absence-of-URL is part of the proof).
    if "https://" in content or "http://" in content:
        raise SmokeError(
            "appenv pip install log contains URL evidence — the install was "
            "NOT fully offline"
        )
    wheelhouse = install_root / "assets" / "wheelhouse"
    zealfie_wheel = wheelhouse / f"zealfie-{record.zealfie_version}-py3-none-any.whl"
    if not wheelhouse.is_dir():
        raise SmokeError(f"bundled wheelhouse missing: {wheelhouse}")
    if not zealfie_wheel.is_file():
        raise SmokeError(
            f"bundled wheelhouse has no zealfie wheel: {zealfie_wheel}"
        )
    count = len(list(wheelhouse.glob("*.whl")))
    _log(f"[smoke] offline provenance OK: pip log shows "
         f"{_EXPECTED_LOG_MARKERS[0]!r} + a recorded offline argv banner "
         f"(--no-index --find-links, no URL evidence); wheelhouse intact "
         f"({count} wheels incl. zealfie) — repair/reinstall can run offline")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="zealfie-installer-smoke",
        description="ZeAlfie installer installed-layout smoke (ZA-WIN-BOOT-02)",
    )
    parser.add_argument(
        "--install-root", type=Path, default=None,
        help=r"{app} of the installed ZeAlfie (default: %ZEALFIE_INSTALL_ROOT%)",
    )
    parser.add_argument(
        "--record", type=Path, default=None,
        help="path to reproducibility.toml (default: sibling of this script)",
    )
    args = parser.parse_args(argv)

    install_root = args.install_root
    if install_root is None:
        env_root = os.environ.get("ZEALFIE_INSTALL_ROOT")
        if not env_root:
            print("error: --install-root or ZEALFIE_INSTALL_ROOT required",
                  file=sys.stderr)
            return 2
        install_root = Path(env_root)
    install_root = install_root.resolve()

    record_path = args.record or (
        Path(__file__).resolve().parent / "reproducibility.toml"
    )
    try:
        record = provision.load_record(record_path)
    except provision.RecordError as exc:
        print(f"[smoke] record error: {exc}", file=sys.stderr)
        return 1

    _log(f"[smoke] installer smoke on install root {install_root}")
    _log(f"[smoke] pinned CPython {record.cpython_version} "
         f"(sha256 {record.sha256[:16]}...)")
    try:
        _smoke_private_python(install_root, record)
        _smoke_appenv_complete(install_root)
        _smoke_self_provenance(install_root, record)
        _smoke_import_location(install_root)
        _smoke_cli(install_root)
        _smoke_gui(install_root)
        _smoke_offline_provenance(install_root, record)
    except SmokeError as exc:
        print(f"[smoke] FAILED: {exc}", file=sys.stderr)
        return 1
    print("[smoke] ALL INSTALLER SMOKE ASSERTIONS PASS")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001 — fail closed with evidence
        print(f"[smoke] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)
