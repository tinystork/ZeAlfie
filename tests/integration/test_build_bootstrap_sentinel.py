"""Sentinel: a plain ``pip install zealfie`` can build wheels out of the box.

This test proves the M1 bootstrap defect fix.  In a fresh virtual
environment where ZeAlfie is installed **normally** (dependencies resolved
from the declared project metadata, no ``[dev]`` extras, no manual
``pip install build`` afterwards), the PyPA ``build`` frontend,
``setuptools`` backend, and ``wheel`` package must all be importable, and
``zealfie.building.build_wheel()`` must be able to build a tiny source
package through the installed ZeAlfie code.

``build_wheel()`` runs ``python -m build --no-isolation``, which does not
install the source package's build requirements.  A real source package
(e.g. ZeSolver) declares ``wheel`` in its ``[build-system].requires``, so
``wheel`` must already be present in the runtime environment for the build
to succeed.  ``wheel`` is therefore a runtime dependency, not a dev-only
extra.

The test performs a real network-backed install (mirroring a real user
``pip install zealfie``), so it is slow and excluded from FAST.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.integration, pytest.mark.zealfie_slow]

from zealfie.building import build_wheel
from zealfie.environment import TemporaryVenv


@pytest.fixture(scope="session")
def zealfie_wheel(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Build the ZeAlfie wheel once from the project root."""
    project_root = Path(__file__).resolve().parents[2]  # integration -> tests -> project
    tmp = tmp_path_factory.mktemp("sentinel-zealfie-wheel")
    return build_wheel(project_root, output_dir=tmp)


def _write_tiny_source(root: Path) -> Path:
    """Create a minimal setuptools source package under *root* and return it.

    The tiny package mirrors a real ZeSolver-style source build: its
    ``[build-system].requires`` declares both ``setuptools>=77`` and
    ``wheel>=0.45``.  With ``--no-isolation`` these are not auto-installed,
    so they must already be present in the runtime environment.
    """
    src = root / "tiny_pkg"
    src.mkdir(parents=True, exist_ok=True)
    (src / "__init__.py").write_text('"""Tiny bootstrap sentinel package."""\n', encoding="utf-8")
    (root / "pyproject.toml").write_text(
        "[build-system]\n"
        'requires = ["setuptools>=77", "wheel>=0.45"]\n'
        'build-backend = "setuptools.build_meta"\n'
        "\n"
        "[project]\n"
        'name = "bootstrap-sentinel-tiny"\n'
        'version = "0.0.1"\n',
        encoding="utf-8",
    )
    return root


def test_fresh_normal_install_builds_wheel_without_dev_extras(
    zealfie_wheel: Path,
    tmp_path: Path,
) -> None:
    """Fresh venv + normal ZeAlfie install -> ``build_wheel()`` works.

    Assertions:

    * ZeAlfie's declared runtime dependencies include ``build>=1.2``,
      ``setuptools>=77``, and ``wheel>=0.45``.
    * ``build``, ``setuptools``, and ``wheel`` are all importable in the
      venv.
    * ``zealfie.building.build_wheel()`` (executed with the venv's own
      interpreter) builds a wheel from a tiny source package whose
      ``[build-system].requires`` includes ``wheel``.
    """
    assert zealfie_wheel.is_file(), f"ZeAlfie wheel missing: {zealfie_wheel}"

    with TemporaryVenv() as venv:
        # Normal user-style install: deps resolved from declared metadata.
        # no_index=False + no_deps=False => real ``pip install <wheel>``.
        r_install = venv.install_wheel(zealfie_wheel, no_index=False, no_deps=False)
        assert r_install.returncode == 0, (
            f"ZeAlfie normal install failed:\n{r_install.stderr}"
        )

        # 1. Declared runtime metadata carries the build tooling, including wheel.
        r_meta = venv.run_python([
            "-c",
            "import importlib.metadata\n"
            "raw = importlib.metadata.requires('zealfie') or []\n"
            "# Runtime deps only: ignore extras-marked requirements.\n"
            "reqs = [r for r in raw if 'extra ==' not in r]\n"
            "print('\\n'.join(sorted(reqs)))\n",
        ])
        assert r_meta.returncode == 0, f"metadata check failed:\n{r_meta.stderr}"
        requires = r_meta.stdout

        assert "build>=1.2" in requires
        assert "setuptools>=77" in requires
        assert "wheel>=0.45" in requires
        assert "packaging>=24" in requires
        assert "PySide6>=6" in requires

        # 2. build/setuptools/wheel all present in the fresh venv.
        r_pkgs = venv.run_python([
            "-c",
            "import importlib.metadata\n"
            "print('build', importlib.metadata.version('build'))\n"
            "print('setuptools', importlib.metadata.version('setuptools'))\n"
            "print('wheel', importlib.metadata.version('wheel'))\n",
        ])
        assert r_pkgs.returncode == 0, f"package presence check failed:\n{r_pkgs.stderr}"
        pkgs_out = r_pkgs.stdout

        assert "build " in pkgs_out
        assert "setuptools " in pkgs_out
        assert "wheel " in pkgs_out, f"wheel should be installed:\n{pkgs_out}"

        # 3. Real build through the installed ZeAlfie on a tiny source package
        #    whose build-system requires wheel (mirrors ZeSolver).
        tiny_source = _write_tiny_source(tmp_path / "tiny_src")
        out_dir = tmp_path / "out"
        r_build = venv.run_python([
            "-c",
            "import sys\n"
            "from zealfie.building import build_wheel\n"
            "p = build_wheel(sys.argv[1], output_dir=sys.argv[2])\n"
            "print(p)\n",
            str(tiny_source),
            str(out_dir),
        ])
        assert r_build.returncode == 0, (
            f"build_wheel failed in fresh venv:\n{r_build.stderr}"
        )

        built_wheel = Path(r_build.stdout.strip())
        assert built_wheel.is_file(), f"built wheel missing: {built_wheel}"
        assert built_wheel.suffix == ".whl"
        assert "bootstrap_sentinel_tiny" in built_wheel.name
