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
