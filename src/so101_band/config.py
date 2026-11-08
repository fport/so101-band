"""The TOML config: arm settings, poses, routines, the tap sequence and what each band button does.

Angles are degrees from the calibration middle pose (the arm standing in its "L"); the gripper is percent open,
0 = closed. This is so101-remote's convention, so its gestures (wave, dance, yes, no, ready, rest) are available
as routines next to your own.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

from so101_remote.arm import JOINTS
from so101_remote.gestures import GESTURES, READY, REST
from so101_remote.motion import SPEEDS, Keyframe

BUTTONS = ("find", "play", "pause", "previous", "next", "volume_up", "volume_down")
SIMPLE_ACTIONS = ("next", "previous", "torque", "stop", "joint", "none")
BUILTIN_POSES = {"ready": dict(READY), "rest": dict(REST)}
MAX_SPEED_DEG_S = max(SPEEDS.values())  # so101-remote's fastest level
NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_LISTEN = "uv run --project ~/www/openband openband listen"
DEFAULT_NOTIFY = "uv run --project ~/www/openband openband notify"


class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class Step:
    pose: str
    hold: float = 0.0


@dataclass
class Config:
    path: Path | None = None
    port: str = "auto"
    max_speed_deg_s: float = 30.0
    limits: dict[str, tuple[float, float]] = field(default_factory=dict)
    listen: str = DEFAULT_LISTEN
    notify: str = DEFAULT_NOTIFY
    title: str = "SO-101"
    poses: dict[str, dict[str, float]] = field(default_factory=dict)  # the user's own
    routines: dict[str, list[Step]] = field(default_factory=dict)  # the user's own
    sequence: list[str] = field(default_factory=list)
    buttons: dict[str, str] = field(default_factory=dict)
    nudge_joint: str = "wrist_roll"

    def all_poses(self) -> dict[str, dict[str, float]]:
        return {**BUILTIN_POSES, **self.poses}

    def kind(self, name: str) -> str | None:
        """What a name in the sequence or a pose:/routine: action refers to. Your own names come first;
        so101-remote's gestures come before the two built-in poses, so "rest" folds the wrist first."""
        if name in self.poses:
            return "pose"
        if name in self.routines or name in GESTURES:
            return "routine"
        if name in BUILTIN_POSES:
            return "pose"
        return None

    def frames(self, name: str) -> list[Keyframe]:
        if name in self.poses:
            return [Keyframe(pose=dict(self.poses[name]))]
        if name in self.routines:
            poses = self.all_poses()
            return [Keyframe(pose=dict(poses[s.pose]), hold=s.hold) for s in self.routines[name]]
        if name in GESTURES:
            return GESTURES[name]()
        if name in BUILTIN_POSES:
            return [Keyframe(pose=dict(BUILTIN_POSES[name]))]
        raise KeyError(name)


def default_path() -> Path:
    env = os.environ.get("SO101_BAND_CONFIG")
    return Path(env).expanduser() if env else Path.home() / ".config" / "so101-band" / "config.toml"


def example_text() -> str:
    return resources.files("so101_band").joinpath("config.example.toml").read_text()


def load(path: Path | None) -> Config:
    """The config at path; the bundled example when path is None."""
    text = example_text() if path is None else Path(path).read_text()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path or 'example config'}: {e}") from e
    cfg = parse(data)
    cfg.path = path
    return cfg


def _number(where: str, value) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(f"{where}: expected a number, got {value!r}")
    return float(value)


def _pose(where: str, raw) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise ConfigError(f"{where}: expected a table of joint = value")
    pose = {}
    for j, v in raw.items():
        if j not in JOINTS:
            raise ConfigError(f"{where}: unknown joint {j!r} (joints: {', '.join(JOINTS)})")
        v = _number(f"{where}.{j}", v)
        lo, hi = (0, 100) if j == "gripper" else (-180, 180)
        if not lo <= v <= hi:
            raise ConfigError(f"{where}.{j}: {v:g} is outside {lo}..{hi}")
        pose[j] = v
    return pose


def _action(where: str, action, cfg: Config) -> str:
    if not isinstance(action, str):
        raise ConfigError(f"{where}: expected a string")
    kind, sep, arg = action.partition(":")
    if not sep and kind in SIMPLE_ACTIONS:
        return action
    if kind in ("pose", "routine") and arg:
        if cfg.kind(arg) is None:
            raise ConfigError(f"{where}: no pose or routine called {arg!r}")
        return action
    if kind == "nudge" and arg:
        try:
            float(arg)
        except ValueError:
            raise ConfigError(f"{where}: nudge needs degrees, like nudge:+5") from None
        return action
    raise ConfigError(f"{where}: unknown action {action!r} (next, previous, pose:NAME, routine:NAME, torque, "
                      "stop, joint, nudge:+N, none)")


def parse(data: dict) -> Config:
    cfg = Config()
    arm = data.get("arm", {})
    cfg.port = str(arm.get("port", cfg.port))
    cfg.max_speed_deg_s = _number("arm.max_speed_deg_s", arm.get("max_speed_deg_s", cfg.max_speed_deg_s))
    if not 1 <= cfg.max_speed_deg_s <= MAX_SPEED_DEG_S:
        raise ConfigError(f"arm.max_speed_deg_s: keep it between 1 and {MAX_SPEED_DEG_S:g}")
    for j, pair in arm.get("limits", {}).items():
        where = f"arm.limits.{j}"
        if j not in JOINTS:
            raise ConfigError(f"{where}: unknown joint")
        if j == "gripper":
            raise ConfigError(f"{where}: the gripper keeps its calibrated range (and runs at 50 % torque)")
        if not isinstance(pair, list) or len(pair) != 2:
            raise ConfigError(f"{where}: expected [low, high] in degrees")
        lo, hi = _number(where, pair[0]), _number(where, pair[1])
        if not -180 <= lo < hi <= 180:
            raise ConfigError(f"{where}: expected -180 <= low < high <= 180")
        cfg.limits[j] = (lo, hi)

    band = data.get("openband", {})
    cfg.listen = str(band.get("listen", cfg.listen))
    cfg.notify = str(band.get("notify", cfg.notify))
    cfg.title = str(band.get("title", cfg.title))

    for name, raw in data.get("poses", {}).items():
        if not NAME_RE.match(name):
            raise ConfigError(f"poses.{name}: use letters, digits, _ and -")
        if name == "rest":
            raise ConfigError("poses.rest: 'rest' is the parking pose from so101-remote; pick another name")
        cfg.poses[name] = _pose(f"poses.{name}", raw)

    poses = cfg.all_poses()
    for name, items in data.get("routines", {}).items():
        where = f"routines.{name}"
        if name in cfg.poses:
            raise ConfigError(f"{where}: there is a pose with the same name")
        if not isinstance(items, list) or not items:
            raise ConfigError(f"{where}: expected a list of pose names")
        steps = []
        for i, item in enumerate(items):
            if isinstance(item, str):
                step = Step(item)
            elif isinstance(item, dict) and isinstance(item.get("pose"), str):
                step = Step(item["pose"], _number(f"{where}[{i}].hold", item.get("hold", 0.0)))
            else:
                raise ConfigError(f"{where}[{i}]: expected a pose name or {{ pose = NAME, hold = SECONDS }}")
            if step.pose not in poses:
                raise ConfigError(f"{where}[{i}]: no pose called {step.pose!r}")
            steps.append(step)
        cfg.routines[name] = steps

    seq = data.get("sequence", {}).get("steps", ["ready", "wave", "ready"])
    if not isinstance(seq, list) or not seq:
        raise ConfigError("sequence.steps: expected a list of names")
    for name in seq:
        if not isinstance(name, str) or cfg.kind(name) is None:
            raise ConfigError(f"sequence.steps: no pose or routine called {name!r}")
    cfg.sequence = list(seq)

    buttons = {"find": "next", "next": "next", "previous": "previous"}
    buttons.update(data.get("buttons", {}))
    for button, action in buttons.items():
        if button not in BUTTONS:
            raise ConfigError(f"buttons.{button}: unknown button (buttons: {', '.join(BUTTONS)})")
        cfg.buttons[button] = _action(f"buttons.{button}", action, cfg)

    joint = data.get("nudge", {}).get("joint", cfg.nudge_joint)
    if joint not in JOINTS:
        raise ConfigError(f"nudge.joint: unknown joint {joint!r}")
    cfg.nudge_joint = joint
    return cfg


def _format_value(v: float) -> str:
    return f"{v:.1f}".rstrip("0").rstrip(".") if v != int(v) else str(int(v))


def pose_line(name: str, pose: dict[str, float]) -> str:
    body = ", ".join(f"{j} = {_format_value(pose[j])}" for j in JOINTS if j in pose)
    return f"{name} = {{ {body} }}"


def record_pose(path: Path, name: str, pose: dict[str, float]) -> bool:
    """Write the pose into the [poses] table of the config file, keeping everything else (comments too).
    Returns True when a pose of that name was replaced."""
    if not NAME_RE.match(name):
        raise ConfigError(f"{name!r}: use letters, digits, _ and -")
    if name == "rest":
        raise ConfigError("'rest' is the parking pose from so101-remote; pick another name")
    lines = Path(path).read_text().splitlines()
    new = pose_line(name, pose)
    start = next((i for i, ln in enumerate(lines) if ln.strip() == "[poses]"), None)
    replaced = False
    if start is None:
        lines += ["", "[poses]", new]
    else:
        end = next((i for i in range(start + 1, len(lines)) if lines[i].lstrip().startswith("[")), len(lines))
        key = re.compile(rf"^\s*{re.escape(name)}\s*=")
        hit = next((i for i in range(start + 1, end) if key.match(lines[i])), None)
        if hit is not None:
            lines[hit], replaced = new, True
        else:
            at = end
            while at > start + 1 and (not lines[at - 1].strip() or lines[at - 1].lstrip().startswith("#")):
                at -= 1  # before the blank lines and the comments that introduce the next table
            lines.insert(at, new)
    text = "\n".join(lines) + "\n"
    parse(tomllib.loads(text))  # never leave a broken config behind
    Path(path).write_text(text)
    return replaced
