from __future__ import annotations

import sys

import pytest

from moco import config as config_module


@pytest.fixture(autouse=True)
def _isolate_config_content_tests_from_windows_host_acl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Keep ordinary YAML tests independent from the runner's inherited temp DACL."""
    if sys.platform == "win32":
        monkeypatch.setattr(config_module, "_validate_windows_config_path", lambda _path: None)
