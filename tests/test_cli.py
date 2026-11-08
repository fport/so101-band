import io
import tomllib

import pytest

from so101_band import cli


@pytest.fixture
def cfg_path(tmp_path, monkeypatch):
    path = tmp_path / "so101-band" / "config.toml"
    monkeypatch.setenv("SO101_BAND_CONFIG", str(path))
    return path


def test_run_is_a_dry_run_unless_live():
    parse = cli.build_parser().parse_args
    assert parse(["run"]).live is False
    assert parse(["run", "--dry-run"]).live is False
    assert parse(["run", "--live"]).live is True
    with pytest.raises(SystemExit):
        parse(["run", "--live", "--dry-run"])


def test_init_writes_the_example_once(cfg_path, capsys):
    assert cli.main(["init"]) == 0
    assert "[sequence]" in cfg_path.read_text()
    assert cli.main(["init"]) == 1
    assert cli.main(["init", "--force"]) == 0


def test_poses_lists_the_example_without_a_config(cfg_path, capsys):
    assert cli.main(["poses"]) == 0
    text = capsys.readouterr().out
    assert "using the example" in text
    assert "1. home -> 2. wave -> 3. reach -> 4. home" in text
    assert "volume_up    nudge:+5" in text


def test_a_broken_config_is_reported(cfg_path, capsys):
    cfg_path.parent.mkdir(parents=True)
    cfg_path.write_text('[buttons]\nfind = "fly"\n')
    assert cli.main(["poses"]) == 2
    assert "config error: buttons.find: unknown action 'fly'" in capsys.readouterr().out


def test_record_from_the_mock_arm_saves_its_rest_pose(cfg_path, capsys):
    assert cli.main(["record", "parked", "--mock"]) == 0
    pose = tomllib.loads(cfg_path.read_text())["poses"]["parked"]
    assert pose["shoulder_lift"] == pytest.approx(-104, abs=1)
    assert 0 <= pose["gripper"] <= 100
    assert cli.main(["poses"]) == 0
    assert "parked" in capsys.readouterr().out


def test_dry_run_from_stdin_starts_and_ends_cleanly(cfg_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    assert cli.main(["run", "--stdin", "--no-notify"]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out
    assert "dry run: no serial port opened" in out
    assert "would turn torque on" not in out  # no tap, no torque
    assert out.rstrip().endswith("end of input")


def test_max_speed_is_bounded(cfg_path, capsys):
    assert cli.main(["run", "--stdin", "--max-speed", "500"]) == 2
