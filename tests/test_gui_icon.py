"""ZA-ICON-01 — headless tests for the ZeAlfie application-icon layer.

All tests run headless via ``QT_QPA_PLATFORM=offscreen`` (matching the
tests/test_gui.py conventions).  Covers:

* (a) the canonical resolver returns a QIcon when the packaged asset is
  present;
* (b) the resolver returns None / never raises when the resource is
  missing (asset absent, package absent);
* (c) the QApplication-level apply and the main-window apply never crash
  when the resource is absent (and apply an icon when present);
* package-data inclusion of ``zealfie/icon/*`` in a built wheel
  (slow marker, excluded from FAST runs).
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

import pytest

try:
    from PySide6.QtGui import QIcon
    from PySide6.QtWidgets import QApplication
    HAS_PYSIDE6 = True
except ImportError:
    HAS_PYSIDE6 = False

from zealfie.gui.icon import (
    ICON_PACKAGE,
    ICON_RESOURCE,
    apply_app_icon,
    apply_window_icon,
    load_app_icon,
)

pytestmark = pytest.mark.skipif(not HAS_PYSIDE6, reason="PySide6 not available")


# ===========================================================================
# Helpers
# ===========================================================================


@pytest.fixture(scope="session")
def qapp():
    """Session-scoped QApplication for headless (offscreen) GUI tests."""
    if "QT_QPA_PLATFORM" not in os.environ:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"

    app = QApplication.instance()
    if app is None:
        app = QApplication([])

    yield app


def _packaged_icon_present() -> bool:
    """True when the packaged icon asset exists in this environment."""
    import importlib.resources

    try:
        return importlib.resources.files(ICON_PACKAGE).joinpath(
            ICON_RESOURCE
        ).is_file()
    except Exception:
        return False


class _FakeService:
    """Minimal service stub sufficient to construct ZeAlfieMainWindow."""

    def __init__(self) -> None:
        from zealfie.products.state import ProductShellState
        from zealfie.runtime.model import RuntimeState

        self._shell = ProductShellState(
            runtime_state=RuntimeState.ABSENT,
            runtime_root=Path("/fake/runtime"),
            products=(),
        )

    def list_products(self):
        return ()

    def collect_product_state(self):
        return self._shell


def _make_main_window():
    """Construct a real ZeAlfieMainWindow over the minimal fake service."""
    from zealfie.gui.main_window import ZeAlfieMainWindow

    return ZeAlfieMainWindow(service=_FakeService())


# ===========================================================================
# (a) Resolver returns a QIcon when the packaged resource is present
# ===========================================================================


class TestLoadAppIconPresent:
    def test_resolver_returns_qicon_when_packaged_resource_present(self, qapp):
        """load_app_icon() returns a usable, non-null QIcon."""
        assert _packaged_icon_present(), (
            "packaged icon asset must exist for this test to be meaningful"
        )
        icon = load_app_icon()
        assert icon is not None
        assert isinstance(icon, QIcon)
        assert not icon.isNull()

    def test_apply_app_icon_sets_application_window_icon(self, qapp):
        """apply_app_icon() makes QApplication.windowIcon() non-null."""
        assert _packaged_icon_present()
        qapp.setWindowIcon(QIcon())  # reset to a null icon first
        apply_app_icon(qapp)
        assert not qapp.windowIcon().isNull()

    def test_apply_window_icon_sets_top_level_window_icon(self, qapp):
        """apply_window_icon() gives a bare QMainWindow a non-null icon."""
        from PySide6.QtWidgets import QMainWindow

        assert _packaged_icon_present()
        window = QMainWindow()
        try:
            apply_window_icon(window)
            assert not window.windowIcon().isNull()
        finally:
            window.close()

    def test_main_window_explicitly_receives_zealfie_icon(self, qapp):
        """A real ZeAlfieMainWindow ends up with a non-null window icon."""
        window = _make_main_window()
        try:
            assert not window.windowIcon().isNull()
        finally:
            window.close()


# ===========================================================================
# (b) Resolver returns None / never raises when the resource is missing
# ===========================================================================


class TestLoadAppIconMissing:
    def test_resolver_returns_none_when_asset_missing(self, qapp, monkeypatch):
        """Missing asset -> None, and no exception escapes."""
        monkeypatch.setattr(
            "zealfie.gui.icon.ICON_RESOURCE", "definitely_missing.png"
        )
        assert load_app_icon() is None

    def test_resolver_returns_none_when_package_missing(
        self, qapp, monkeypatch
    ):
        """Unimportable icon package -> None, and no exception escapes."""
        monkeypatch.setattr(
            "zealfie.gui.icon.ICON_PACKAGE", "zealfie.icon.does_not_exist"
        )
        assert load_app_icon() is None

    def test_apply_app_icon_noop_when_asset_missing(self, qapp, monkeypatch):
        """App-level apply with a missing asset: no crash, icon stays null."""
        qapp.setWindowIcon(QIcon())  # reset to a null icon first
        monkeypatch.setattr(
            "zealfie.gui.icon.ICON_RESOURCE", "definitely_missing.png"
        )
        apply_app_icon(qapp)  # must not raise
        assert qapp.windowIcon().isNull()

    def test_apply_window_icon_noop_when_asset_missing(
        self, qapp, monkeypatch
    ):
        """Window-level apply with a missing asset: no crash."""
        from PySide6.QtWidgets import QMainWindow

        monkeypatch.setattr(
            "zealfie.gui.icon.ICON_RESOURCE", "definitely_missing.png"
        )
        window = QMainWindow()
        try:
            apply_window_icon(window)  # must not raise
            # No application icon was set, so the window stays icon-less.
            assert window.windowIcon().isNull()
        finally:
            window.close()

    def test_main_window_constructs_without_icon_when_asset_missing(
        self, qapp, monkeypatch
    ):
        """ZeAlfieMainWindow still constructs cleanly with no icon asset."""
        from zealfie.gui.main_window import ZeAlfieMainWindow

        qapp.setWindowIcon(QIcon())  # reset to a null icon first
        monkeypatch.setattr(
            "zealfie.gui.icon.ICON_RESOURCE", "definitely_missing.png"
        )
        window = _make_main_window()  # must not raise
        try:
            assert isinstance(window, ZeAlfieMainWindow)
            assert window.windowIcon().isNull()
        finally:
            window.close()


# ===========================================================================
# Package-data inclusion in a built wheel (slow; excluded from FAST)
# ===========================================================================


@pytest.mark.zealfie_slow
def test_zealfie_wheel_packages_icon_assets(tmp_path: Path) -> None:
    """The built zealfie wheel must ship zealfie/icon/* inside itself."""
    from zealfie.building import build_wheel

    project_root = Path(__file__).resolve().parents[1]
    wheel = build_wheel(project_root, output_dir=tmp_path)

    assert wheel.is_file()
    with zipfile.ZipFile(wheel) as zf:
        names = set(zf.namelist())

    expected = {
        "zealfie/icon/zealfie_32.png",
        "zealfie/icon/zealfie_48.png",
        "zealfie/icon/zealfie_64.png",
        "zealfie/icon/zealfie_128.png",
        "zealfie/icon/zealfie_256.png",
        "zealfie/icon/zealfie_512.png",
        "zealfie/icon/icon_large.png",
        "zealfie/icon/zealfie.ico",
        "zealfie/icon/zealfie.icns",
    }
    missing = expected - names
    assert not missing, f"icon assets missing from wheel: {sorted(missing)}"
