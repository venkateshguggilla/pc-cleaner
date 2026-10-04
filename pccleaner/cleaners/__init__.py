"""All available cleaning targets, in display order."""
from __future__ import annotations

from ..core import Target
from . import apps, browsers, explorer, media, system, winapp2


def build_catalog() -> list[Target]:
    """Targets for what is installed on this PC (missing browsers/players are left out)."""
    result: list[Target] = []
    for module in (system, explorer, browsers, media, apps, winapp2):
        result.extend(module.targets())
    return result
