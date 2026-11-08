import tomllib

import pytest

from so101_band import config
from so101_band.config import ConfigError, load, parse, record_pose


def test_example_config_loads_and_every_step_has_frames():
    cfg = load(None)
    assert cfg.sequence == ["home", "wave", "reach", "home"]
    assert cfg.buttons["find"] == "next"
    assert cfg.buttons["play"] == cfg.buttons["pause"] == "torque"
    assert cfg.limits == {"shoulder_pan": (-60, 60), "wrist_roll": (-90, 90)}
    for name in [*cfg.sequence, *cfg.routines, "rest", "ready", "dance"]:
        assert cfg.frames(name)
    assert cfg.kind("wave") == "routine" and cfg.kind("reach") == "pose" and cfg.kind("rest") == "routine"


def test_defaults_without_any_tables():
    cfg = parse({})
    assert cfg.sequence == ["ready", "wave", "ready"]
    assert cfg.buttons == {"find": "next", "next": "next", "previous": "previous"}
    assert cfg.max_speed_deg_s == 30


@pytest.mark.parametrize("data, message", [
    ({"poses": {"a": {"knee": 3}}}, "unknown joint 'knee'"),
    ({"poses": {"a": {"gripper": 120}}}, "outside 0..100"),
    ({"poses": {"a": {"elbow_flex": "up"}}}, "expected a number"),
    ({"poses": {"rest": {"elbow_flex": 0}}}, "parking pose"),
    ({"poses": {"a b": {"elbow_flex": 0}}}, "letters, digits"),
    ({"sequence": {"steps": ["nowhere"]}}, "no pose or routine called 'nowhere'"),
    ({"routines": {"r": ["nowhere"]}}, "no pose called 'nowhere'"),
    ({"buttons": {"find": "fly"}}, "unknown action 'fly'"),
    ({"buttons": {"find": "nudge:lots"}}, "nudge needs degrees"),
    ({"buttons": {"find": "pose:nowhere"}}, "no pose or routine"),
    ({"buttons": {"jump": "next"}}, "unknown button"),
    ({"arm": {"max_speed_deg_s": 200}}, "between 1 and 90"),
    ({"arm": {"limits": {"gripper": [0, 50]}}}, "gripper keeps its calibrated range"),
    ({"arm": {"limits": {"elbow_flex": [30, 10]}}}, "low < high"),
    ({"nudge": {"joint": "knee"}}, "nudge.joint"),
])
def test_bad_configs_say_what_is_wrong(data, message):
    with pytest.raises(ConfigError, match=message):
        parse(data)


def test_routine_items_take_an_optional_hold():
    cfg = parse({"poses": {"a": {"elbow_flex": 10}},
                 "routines": {"r": ["a", {"pose": "ready", "hold": 0.5}]}})
    frames = cfg.frames("r")
    assert [f.hold for f in frames] == [0.0, 0.5]
    assert frames[0].pose == {"elbow_flex": 10}


def test_record_pose_adds_to_the_poses_table_and_keeps_comments(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config.example_text())
    pose = {"shoulder_pan": 1.25, "shoulder_lift": -10, "elbow_flex": 20.5, "wrist_flex": 0, "wrist_roll": 3,
            "gripper": 40}
    assert record_pose(path, "grab", pose) is False
    text = path.read_text()
    assert "# so101-band config." in text
    data = tomllib.loads(text)
    assert data["poses"]["grab"] == {"shoulder_pan": 1.2, "shoulder_lift": -10, "elbow_flex": 20.5,
                                     "wrist_flex": 0, "wrist_roll": 3, "gripper": 40}
    # inserted with the other poses, before the comment block that follows them
    assert text.index("grab = {") < text.index("# Built in:")
    load(path)

    assert record_pose(path, "grab", {**pose, "gripper": 0}) is True
    assert tomllib.loads(path.read_text())["poses"]["grab"]["gripper"] == 0
    assert path.read_text().count("grab = {") == 1


def test_record_pose_into_a_config_without_poses(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[arm]\nport = "auto"\n')
    record_pose(path, "p", {"elbow_flex": 5})
    assert tomllib.loads(path.read_text())["poses"]["p"] == {"elbow_flex": 5}


def test_record_pose_refuses_bad_names_and_never_writes_a_broken_file(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(config.example_text())
    before = path.read_text()
    with pytest.raises(ConfigError):
        record_pose(path, "two words", {"elbow_flex": 0})
    with pytest.raises(ConfigError):
        record_pose(path, "rest", {"elbow_flex": 0})
    with pytest.raises(ConfigError):
        record_pose(path, "x", {"gripper": 500})
    assert path.read_text() == before
