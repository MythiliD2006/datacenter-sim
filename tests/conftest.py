import pytest

from dcsim import config, paths


@pytest.fixture()
def home(tmp_path):
    paths.set_home(tmp_path / "dcsim-home")
    paths.ensure_home()
    yield paths.home()
    paths.set_home(None)


@pytest.fixture()
def cfg(home):
    c = config.load_config()
    c["server"]["password_hash_rounds"] = 4
    return c
