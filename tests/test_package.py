from __future__ import annotations

from importlib.metadata import version

import moco


def test_distribution_and_import_use_moco_name() -> None:
    assert moco.__version__ == version("moco")
