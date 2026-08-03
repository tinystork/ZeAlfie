"""Built-in component definitions."""

from __future__ import annotations

from .model import ComponentDefinition


ZESOLVER = ComponentDefinition(
    component_id="zesolver",
    display_name="ZeSolver",
    distribution_name="ZeSolver",
    supported_entry_points=("zesolver",),
)

KNOWN_COMPONENTS = (ZESOLVER,)
