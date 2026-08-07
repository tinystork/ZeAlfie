"""Tests for the runtime metadata probe."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from zealfie.runtime.probe import (
    _PROBE_TIMEOUT_SECONDS,
    probe_runtime_distribution,
    probe_runtime_python_version,
)


# ---------------------------------------------------------------------------
# probe_runtime_distribution — normal cases
# ---------------------------------------------------------------------------


def test_probe_distribution_missing(monkeypatch) -> None:
    """Probe returns installed=False for a missing distribution."""
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"python_version": "3.13", "installed": False, "version": None, "entry_points": []}),
            stderr="",
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    result = probe_runtime_distribution("/fake/python", "missing-pkg")
    assert result["installed"] is False
    assert result["version"] is None


def test_probe_distribution_present(monkeypatch) -> None:
    """Probe returns installed=True with version and entry points."""
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({
                "python_version": "3.13",
                "installed": True,
                "version": "0.0.1",
                "entry_points": [
                    {"group": "console_scripts", "name": "zewitness", "value": "zewitness.__main__:main"}
                ],
            }),
            stderr="",
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    result = probe_runtime_distribution("/fake/python", "zealfie-witness")
    assert result["installed"] is True
    assert result["version"] == "0.0.1"
    assert len(result["entry_points"]) == 1
    assert result["entry_points"][0]["group"] == "console_scripts"
    assert result["entry_points"][0]["name"] == "zewitness"


def test_probe_calls_python_with_distribution_name(monkeypatch) -> None:
    """The probe passes the distribution name as first arg to the script."""
    import subprocess as sp_mod

    captured_cmd: list[str] = []

    def fake_run(cmd, **kwargs):
        nonlocal captured_cmd
        captured_cmd = list(cmd)
        return sp_mod.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout=json.dumps({"python_version": "3.13", "installed": True, "version": "x", "entry_points": []}),
            stderr="",
        )

    monkeypatch.setattr(sp_mod, "run", fake_run)

    probe_runtime_distribution("/my/python", "my-pkg")
    assert "my-pkg" in captured_cmd


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


def test_probe_nonzero_return_code_raises(monkeypatch) -> None:
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="boom")

    monkeypatch.setattr(sp_mod, "run", fake_run)

    with pytest.raises(RuntimeError, match="probe failed"):
        probe_runtime_distribution("/fake/python", "pkg")


def test_probe_invalid_json_raises(monkeypatch) -> None:
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(args=cmd, returncode=0, stdout="not json", stderr="")

    monkeypatch.setattr(sp_mod, "run", fake_run)

    with pytest.raises(RuntimeError, match="invalid JSON"):
        probe_runtime_distribution("/fake/python", "pkg")


def test_probe_timeout(monkeypatch) -> None:
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(sp_mod, "run", fake_run)

    with pytest.raises(subprocess.TimeoutExpired):
        probe_runtime_distribution("/fake/python", "pkg", timeout=1)


# ---------------------------------------------------------------------------
# probe_runtime_python_version
# ---------------------------------------------------------------------------


def test_python_version_ok(monkeypatch) -> None:
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(args=cmd, returncode=0, stdout="3.13.5\n", stderr="")

    monkeypatch.setattr(sp_mod, "run", fake_run)

    v = probe_runtime_python_version("/fake/python")
    assert v == "3.13.5"


def test_python_version_failure_returns_none(monkeypatch) -> None:
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        return sp_mod.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="err")

    monkeypatch.setattr(sp_mod, "run", fake_run)

    v = probe_runtime_python_version("/fake/python")
    assert v is None


def test_python_version_timeout_returns_none(monkeypatch) -> None:
    import subprocess as sp_mod

    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 1)

    monkeypatch.setattr(sp_mod, "run", fake_run)

    v = probe_runtime_python_version("/fake/python", timeout=1)
    assert v is None
