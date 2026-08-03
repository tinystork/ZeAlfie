from __future__ import annotations

import importlib.metadata
from dataclasses import dataclass

from zealfie.components.metadata import inspect_component
from zealfie.components.model import ComponentDefinition, ReasonCode


@dataclass(frozen=True, slots=True)
class FakeEntryPoint:
    name: str
    group: str
    value: str = "fake.module:main"


class FakeDistribution:
    def __init__(
        self,
        *,
        version: str = "1.0.0",
        entry_points: tuple[FakeEntryPoint, ...] = (),
        fail_version: Exception | None = None,
        fail_entry_points: Exception | None = None,
    ) -> None:
        self._version = version
        self._entry_points = entry_points
        self._fail_version = fail_version
        self._fail_entry_points = fail_entry_points

    @property
    def version(self) -> str:
        if self._fail_version:
            raise self._fail_version
        return self._version

    @property
    def entry_points(self) -> tuple[FakeEntryPoint, ...]:
        if self._fail_entry_points:
            raise self._fail_entry_points
        return self._entry_points


class FakeProvider:
    def __init__(self, result: object) -> None:
        self.result = result

    def distribution(self, distribution_name: str) -> object:
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


ZESOLVER = ComponentDefinition("zesolver", "ZeSolver", "ZeSolver", ("zesolver",))


def test_distribution_absent_returns_not_installed_status() -> None:
    status = inspect_component(
        ZESOLVER,
        metadata_provider=FakeProvider(importlib.metadata.PackageNotFoundError("ZeSolver")),
    )

    assert status.installed is False
    assert status.version is None
    assert status.launchable is False
    assert status.reason_code is ReasonCode.DISTRIBUTION_NOT_INSTALLED


def test_distribution_present_reads_version() -> None:
    status = inspect_component(
        ZESOLVER,
        metadata_provider=FakeProvider(FakeDistribution(version="1.0.0")),
    )

    assert status.installed is True
    assert status.version == "1.0.0"


def test_zesolver_scripts_are_not_gui_launch_contract() -> None:
    status = inspect_component(
        ZESOLVER,
        metadata_provider=FakeProvider(
            FakeDistribution(
                version="1.0.0",
                entry_points=(
                    FakeEntryPoint("zeblindsolver", "console_scripts"),
                    FakeEntryPoint("zeblindsolve", "console_scripts"),
                    FakeEntryPoint("zebuildindex", "console_scripts"),
                ),
            )
        ),
    )

    assert status.installed is True
    assert status.version == "1.0.0"
    assert status.launchable is False
    assert status.reason_code is ReasonCode.PUBLIC_ENTRY_POINT_NOT_FOUND


def test_fictitious_distribution_with_supported_entry_point_is_launchable() -> None:
    definition = ComponentDefinition("fake", "Fake App", "FakeApp", ("fake-app",))

    status = inspect_component(
        definition,
        metadata_provider=FakeProvider(
            FakeDistribution(
                version="0.0.1",
                entry_points=(FakeEntryPoint("fake-app", "gui_scripts"),),
            )
        ),
    )

    assert status.installed is True
    assert status.version == "0.0.1"
    assert status.launchable is True
    assert status.reason_code is None
    assert status.reason is None


def test_metadata_distribution_error_returns_explicit_status() -> None:
    status = inspect_component(
        ZESOLVER,
        metadata_provider=FakeProvider(RuntimeError("broken metadata")),
    )

    assert status.installed is False
    assert status.launchable is False
    assert status.reason_code is ReasonCode.DISTRIBUTION_METADATA_ERROR


def test_metadata_entry_points_error_returns_explicit_status() -> None:
    status = inspect_component(
        ZESOLVER,
        metadata_provider=FakeProvider(
            FakeDistribution(version="1.0.0", fail_entry_points=RuntimeError("bad entry points"))
        ),
    )

    assert status.installed is True
    assert status.version == "1.0.0"
    assert status.launchable is False
    assert status.reason_code is ReasonCode.DISTRIBUTION_METADATA_ERROR


def test_metadata_version_error_returns_explicit_status() -> None:
    status = inspect_component(
        ZESOLVER,
        metadata_provider=FakeProvider(
            FakeDistribution(fail_version=RuntimeError("bad version"))
        ),
    )

    assert status.installed is True
    assert status.version is None
    assert status.launchable is False
    assert status.reason_code is ReasonCode.VERSION_UNAVAILABLE
