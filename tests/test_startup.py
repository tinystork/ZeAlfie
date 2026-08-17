from __future__ import annotations


def test_package_imports() -> None:
    import zealfie

    assert zealfie.__version__


def test_startup_message_does_not_require_components() -> None:
    from zealfie.app import startup_message

    message = startup_message()

    assert "Hello, I'm ZeAlfie." in message
    assert "Astronomy Launcher For Imaging Engines" in message
    assert "Components:" not in message


def test_core_platform_surface_imports_without_windows_only_modules() -> None:
    """The core platform surface imports cleanly with no unconditional
    Windows-only (or POSIX-only) module dependency at import time.

    If any of these modules imported ``msvcrt`` / ``winreg`` at module
    level, this import would raise ``ModuleNotFoundError`` on Linux —
    the failure mode this test exists to catch.  Real macOS/Windows
    execution remains a human gate; this proves the import-time surface
    is platform-portable.

    (``fcntl``/``msvcrt`` stay function-local inside
    ``zealfie.runtime.mutation_lock`` — verified by audit, not asserted
    here because other test modules import ``fcntl`` in-process.)
    """
    import sys

    import zealfie
    import zealfie.acceleration
    import zealfie.common.subprocess_platform
    import zealfie.host
    import zealfie.launching
    import zealfie.runtime

    assert zealfie.__version__
    assert "msvcrt" not in sys.modules
    assert "winreg" not in sys.modules
