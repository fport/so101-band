import tempfile

import pytest


@pytest.fixture(autouse=True)
def _no_real_listener(monkeypatch):
    """Keep tests off a real `openband listen` socket (and the real band) on this machine."""
    monkeypatch.setenv("OPENBAND_HOME", tempfile.mkdtemp(prefix="sbt", dir="/tmp"))


@pytest.fixture(autouse=True)
def _no_real_log(monkeypatch, tmp_path):
    from so101_band import cli

    monkeypatch.setattr(cli, "LOG_PATH", tmp_path / "run.log")
