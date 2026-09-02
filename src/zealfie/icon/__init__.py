"""Packaged ZeAlfie icon assets.

Regular subpackage carrying the ZeAlfie icon assets:

* PNG raster set (``zealfie_*.png``, ``icon_large.png``) — used at
  runtime by :mod:`zealfie.gui.icon` as the Qt application/window icon.
* ``zealfie.ico`` (multi-size Windows icon) — reserved for the future
  Windows EXE/installer packaging stage (EXE-embedded icon +
  AppUserModelID).  NOT consumed by the Qt runtime layer.
* ``zealfie.icns`` (macOS icon) — reserved for the future macOS bundle
  packaging stage (``Contents/Resources`` + ``CFBundleIconFile``).
  NOT consumed by the Qt runtime layer.

Resources are resolved via ``importlib.resources`` (never absolute
paths), so the same assets work from a source checkout and from an
installed wheel.  See ``pyproject.toml`` ``[tool.setuptools.package-data]``.
"""
