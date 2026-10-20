"""All available cleaning targets, in display order."""
from __future__ import annotations

import sys

from ..core import Target


def build_catalog() -> list[Target]:
    """Targets for what is installed on this PC (missing browsers/players are left out).

    Catalogs are per operating system: Windows below, Linux in linux.py.
    """
    if sys.platform != "win32":
        from . import linux

        return list(linux.targets())
    from . import apps, browsers, explorer, media, system, winapp2

    result: list[Target] = []
    for module in (system, explorer, browsers, media, apps, winapp2):
        result.extend(module.targets())
    return result
