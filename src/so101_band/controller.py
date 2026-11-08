"""Band presses -> arm motion, on top of so101-remote's Bridge.

The Bridge already does the hard, safety-relevant part and is reused as is: the 50 Hz loop, limits from the
calibration in the motors (with a margin), torque on at the present position (no jump), a blocked-joint check,
50 % gripper torque, and on Ctrl+C folding into "rest" before letting go. This module adds what a band needs:
a JSON line in instead of the ESP32 protocol, a pose sequence, config limits and speed, and joint-space motion
where every joint arrives at the same time.
"""

from __future__ import annotations

from collections.abc import Callable

from so101_remote.arm import JOINTS
from so101_remote.bridge import Bridge
from so101_remote.gestures import GESTURES
from so101_remote.motion import SPEED_SCALE, SPEEDS, TICKS_PER_DEG, Limits, Mover, Player, from_ticks, to_ticks

from .band import parse_line
from .config import Config

SHORT = {"shoulder_pan": "pan", "shoulder_lift": "lift", "elbow_flex": "elbow", "wrist_flex": "wrist",
         "wrist_roll": "roll", "gripper": "grip"}


class SourceEnded(Exception):
    """The band listener (or stdin) is gone; the bridge parks the arm and lets go, as on Ctrl+C."""


def fmt_pose(pose: dict[str, float]) -> str:
    return " ".join(f"{SHORT[j]} {pose[j]:+.0f}" + ("%" if j == "gripper" else "") for j in JOINTS if j in pose)


class CappedMover(Mover):
    """so101-remote's Mover, at the configured top speed and in joint space: the joint with the longest way to
    go sets the pace and the others are scaled down, so every joint starts and arrives together and none goes
    faster than its own cap. The motors' acceleration setting (from the Bridge) softens start and stop."""

    def __init__(self, limits: dict[str, Limits], max_speed_deg_s: float, speed_level: int = 3):
        super().__init__(limits, speed_level)
        self.max_speed_deg_s = max_speed_deg_s

    @property
    def speed_deg_s(self) -> float:
        # The Bridge drops to level 1 (25 deg/s) to park; never faster than the config either way.
        return min(self.max_speed_deg_s, SPEEDS[self.speed_level])

    def rate(self, joint: str) -> float:
        """Top speed of one joint in ticks per second."""
        return self.speed_deg_s * SPEED_SCALE[joint] * TICKS_PER_DEG

    def step(self, dt: float) -> dict[str, int]:
        time_left = max(abs(self.target[j] - self.cmd[j]) / self.rate(j) for j in JOINTS)
        frac = 1.0 if time_left <= dt else dt / time_left
        for j in JOINTS:
            self.cmd[j] += (self.target[j] - self.cmd[j]) * frac
        return {j: round(v) for j, v in self.cmd.items()}


def tighten(limits: dict[str, Limits], extra: dict[str, tuple[float, float]]) -> dict[str, Limits]:
    """The calibrated limits, narrowed by the config's [arm.limits] (degrees); never widened."""
    out = dict(limits)
    for j, (lo, hi) in extra.items():
        base = limits[j]
        new = Limits(max(base.lo, round(to_ticks(j, lo, limits))), min(base.hi, round(to_ticks(j, hi, limits))))
        if new.hi <= new.lo:
            raise ValueError(f"arm.limits.{j}: [{lo:g}, {hi:g}] leaves nothing of the calibrated range")
        out[j] = new
    return out


def pose_warnings(config: Config, limits: dict[str, Limits]) -> list[str]:
    """Your poses that reach past a limit: the arm stops at the limit instead, which is safe but not the pose.
    (The built-in "rest" lies on the end stops on purpose; so101-remote parks it as close as the margin allows.)"""
    out = []
    for name, pose in config.poses.items():
        for j, v in pose.items():
            if j == "gripper":
                continue
            lim, t = limits[j], to_ticks(j, v, limits)
            if not lim.lo <= t <= lim.hi:
                lo, hi = from_ticks(j, lim.lo, limits), from_ticks(j, lim.hi, limits)
                out.append(f"pose {name}: {j} {v:g} is outside {lo:.0f}..{hi:.0f}; it will stop at the limit")
    return out


class BandBridge(Bridge):
    def __init__(self, arm, remote, config: Config, clock: Callable[[], float] | None = None,
                 log: Callable[[str], None] = print):
        kw = {"clock": clock} if clock else {}
        super().__init__(arm, remote, speed_level=3, log=log, **kw)
        self.config = config
        self.index = -1  # position in config.sequence
        self.joint = config.nudge_joint
        self._release_when_parked = False
        self._was_moving = False

    # -- lifecycle -------------------------------------------------------------------------------

    def start(self) -> None:
        super().start()
        self.limits = tighten(self.limits, self.config.limits)
        self.mover = CappedMover(self.limits, self.config.max_speed_deg_s)
        self.mover.reset(self.present)
        for w in pose_warnings(self.config, self.limits):
            self.log(w)
        self.log(f"at {fmt_pose(self.mover.degrees())}, torque off until the first tap")

    def shutdown(self, park_timeout_s: float = 12.0) -> None:
        self._release_when_parked = False
        super().shutdown(park_timeout_s)
        self.say("arm released")

    def tick(self) -> None:
        super().tick()
        if self.remote.ended and not self._busy():
            raise SourceEnded(self.remote.end_reason)

    # -- the band --------------------------------------------------------------------------------

    def handle(self, line: str) -> None:
        press = parse_line(line)
        if press is None:
            if line.strip():
                self.log(f"openband: {line.strip()}")
            return
        action = self.config.buttons.get(press.button, "none")
        if action == "none":
            self.log(f"{press.button}: no action")
            return
        self.log(f"{press.button} -> {action}")
        self.do(action)

    def do(self, action: str) -> None:
        kind, _, arg = action.partition(":")
        if kind == "stop":
            self.error = ""
            self.stop("stopped: holding")
            return
        if kind == "torque":
            self._toggle_torque()
            return
        if kind == "joint":
            self.joint = JOINTS[(JOINTS.index(self.joint) + 1) % len(JOINTS)]
            self.say(f"nudge joint: {self.joint}")
            return
        if self.error:
            # A blocked joint stopped the arm. The next tap only acknowledges it, so a find-only band (no stop
            # button) can recover, and the arm never moves on the same tap that cleared a fault.
            fault, self.error = self.error, ""
            self.stop(f"{fault} cleared: holding, tap again to move")
            return
        self._release_when_parked = False
        if not self.torque:
            self.torque_on()
        if kind == "nudge":
            self.player = None
            self.mover.nudge(self.joint, float(arg))
            value = from_ticks(self.joint, self.mover.target[self.joint], self.limits)
            unit = "%" if self.joint == "gripper" else " deg"
            self.say(f"{self.joint} {float(arg):+g}: {value:.0f}{unit}")
        elif kind in ("next", "previous"):
            n = len(self.config.sequence)
            if self.index < 0:
                self.index = 0 if kind == "next" else n - 1
            else:
                self.index = (self.index + (1 if kind == "next" else -1)) % n
            name = self.config.sequence[self.index]
            self._play(name)
            self.say(f"{self.config.kind(name)} {self.index + 1}/{n}: {name}")
        elif kind in ("pose", "routine"):
            self._play(arg)
            self.say(f"{kind}: {arg}")

    def _play(self, name: str) -> None:
        self.player = Player(name, self.config.frames(name))
        self.log(f"  -> {name}: " + " | ".join(fmt_pose(f.pose) for f in self.player.frames))

    def _toggle_torque(self) -> None:
        self.error = ""
        if not self.torque:
            self.torque_on()
            self.say("torque on: holding")
            return
        if self._release_when_parked:
            self.say("already parking")
            return
        self.player = Player("rest", GESTURES["rest"]())
        self._release_when_parked = True
        self.say("parking, then torque off")

    def stop(self, reason: str = "") -> None:
        self._release_when_parked = False
        super().stop(reason)

    def _busy(self) -> bool:
        return self.player is not None or not self.mover.arrived() or self._release_when_parked

    def _control(self, dt: float) -> None:
        super()._control(dt)
        if self._release_when_parked and self.player is None:
            self._release_when_parked = False
            if not self.error:  # blocked on the way down: hold rather than drop
                self.release()
                self.say("torque off")
        moving = self.torque and (self.player is not None or not self.mover.arrived())
        if self._was_moving and not moving and self.torque:
            self.log(f"  holding {fmt_pose(self.mover.degrees())}")
        self._was_moving = moving
