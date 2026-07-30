from importlib.metadata import version


def test_distribution_and_import_use_moco_name() -> None:
    import moco

    assert moco.__version__ == version("moco")
