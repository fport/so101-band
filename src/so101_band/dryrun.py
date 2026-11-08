"""The dry-run arm: so101-remote's FakeArm (calibrated limits and folded rest pose of the real follower), which
prints what would go to the motors instead of sending it."""

from __future__ import annotations

import time
from collections.abc import Callable

from so101_remote.arm import JOINTS, FakeArm
from so101_remote.motion import from_ticks, limits_from_eeprom

from .controller import fmt_pose


class DryRunArm(FakeArm):
    def __init__(self, log: Callable[[str], None] = print, every_s: float = 0.25,
                 clock: Callable[[], float] = time.monotonic):
        super().__init__()
        self.port = "dry-run"
        self.log = log
        self.every_s = every_s
        self.clock = clock
        self._lims = limits_from_eeprom(self.limits())
        self._goal = {j: float(v) for j, v in self.pos.items()}
        self._last_print = float("-inf")

    def _degrees(self) -> dict[str, float]:
        return {j: from_ticks(j, self._goal[j], self._lims) for j in JOINTS}

    def connect(self) -> None:
        self.log("dry run: no serial port opened; a simulated follower stands in, folded in its rest pose")

    def torque_on(self, goals, acceleration, torque_limits) -> None:
        self._goal.update(goals)
        self.log(f"dry run: would turn torque on, holding {fmt_pose(self._degrees())}")
        super().torque_on(goals, acceleration, torque_limits)

    def torque_off(self) -> list[str]:
        if self.torque:
            self.log("dry run: would turn torque off")
        return super().torque_off()

    def write_goals(self, goals) -> None:
        super().write_goals(goals)
        self._goal.update(goals)
        now = self.clock()
        if now - self._last_print >= self.every_s:
            self._last_print = now
            self.log(f"dry run: would send {fmt_pose(self._degrees())}")
