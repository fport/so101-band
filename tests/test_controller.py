import json

import pytest
from so101_remote import bridge as remote_bridge
from so101_remote.arm import JOINTS, FakeArm
from so101_remote.bridge import TICK_S
from so101_remote.gestures import REST
from so101_remote.motion import SPEED_SCALE, TICKS_PER_DEG, limits_from_eeprom

from so101_band import config
from so101_band.controller import BandBridge, CappedMover, SourceEnded, pose_warnings, tighten
from so101_band.dryrun import DryRunArm


class FakeRemote:
    heartbeat = False
    end_reason = "listener gone"

    def __init__(self):
        self.inbox: list[str] = []
        self.sent: list[str] = []
        self.ended = False

    def poll(self):
        out, self.inbox = self.inbox, []
        return out

    def send(self, line):
        self.sent.append(line)

    def messages(self):
        return [s[2:] for s in self.sent if s.startswith("M ")]


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(cfg=None, arm=None):
    clock, remote = Clock(), FakeRemote()
    bridge = BandBridge(arm or FakeArm(), remote, cfg or config.load(None), clock=clock, log=lambda _: None)
    bridge.start()
    return bridge, remote, clock


def press(bridge, button):
    bridge.remote.inbox.append(json.dumps({"ts": 1, "button": button, "code": 0, "volume": None}))


def run(bridge, clock, seconds):
    for _ in range(round(seconds / TICK_S)):
        clock.t += TICK_S
        bridge.tick()


def degrees(bridge):
    return bridge.mover.degrees()


def close_to(actual, pose, tol=1.0):
    return all(abs(actual[j] - v) <= tol for j, v in pose.items())


def test_nothing_moves_before_a_tap():
    bridge, remote, clock = make()
    run(bridge, clock, 1.0)
    assert not bridge.arm.torque
    assert bridge.arm.goals == []


def test_first_tap_turns_torque_on_where_the_arm_is_then_goes_to_the_first_pose():
    bridge, remote, clock = make()
    present = bridge.arm.positions()
    press(bridge, "find")
    run(bridge, clock, TICK_S)
    assert bridge.arm.torque
    assert dict(bridge.arm.goals[: len(JOINTS)]) == present  # torque on holds the present position: no jump
    run(bridge, clock, 10)
    cfg = bridge.config
    assert close_to(degrees(bridge), cfg.poses["home"])
    assert remote.messages()[-1] == "pose 1/4: home"


def test_sequence_steps_forward_wraps_and_steps_back():
    bridge, remote, clock = make()
    for _ in range(5):
        press(bridge, "find")
        run(bridge, clock, 12)
    assert [m for m in remote.messages() if "/4" in m] == [
        "pose 1/4: home", "routine 2/4: wave", "pose 3/4: reach", "pose 4/4: home", "pose 1/4: home"]
    press(bridge, "previous")
    run(bridge, clock, 12)
    assert remote.messages()[-1] == "pose 4/4: home"


def test_previous_before_any_next_starts_at_the_last_step():
    bridge, remote, clock = make()
    press(bridge, "previous")
    run(bridge, clock, 0.1)
    assert remote.messages()[-1] == "pose 4/4: home"


def test_mover_respects_each_joint_speed_and_all_joints_arrive_together():
    limits = limits_from_eeprom(FakeArm().limits())
    mover = CappedMover(limits, max_speed_deg_s=30)
    mover.reset(FakeArm().positions())
    mover.set_pose({"shoulder_pan": 20, "shoulder_lift": 0, "elbow_flex": 0, "wrist_flex": 0, "wrist_roll": 40,
                    "gripper": 50})
    origin = dict(mover.cmd)
    way = {j: mover.target[j] - origin[j] for j in JOINTS}
    prev, dt, steps = dict(mover.cmd), 0.02, 0
    while not mover.arrived(tol=0.0):
        mover.step(dt)
        steps += 1
        # straight line in joint space: every joint has covered the same share of its way
        shares = {(mover.cmd[j] - origin[j]) / way[j] for j in JOINTS if way[j]}
        assert max(shares) - min(shares) < 1e-9
        for j in JOINTS:
            assert abs(mover.cmd[j] - prev[j]) <= 30 * SPEED_SCALE[j] * TICKS_PER_DEG * dt + 1e-6
        prev = dict(mover.cmd)
        assert steps < 2000
    # shoulder_lift has the longest way (about 104 degrees at 30 deg/s): about 3.5 s
    assert 3.0 < steps * dt < 4.0


def test_config_speed_is_capped_while_parking_at_so101_remote_slowest_level():
    limits = limits_from_eeprom(FakeArm().limits())
    mover = CappedMover(limits, max_speed_deg_s=80)
    assert mover.speed_deg_s == 80
    mover.speed_level = 1  # what the Bridge does before parking
    assert mover.speed_deg_s == 25


def test_config_limits_narrow_the_calibration_and_never_widen_it():
    base = limits_from_eeprom(FakeArm().limits())
    assert tighten(base, {"shoulder_pan": (-180, 180)}) == base
    narrow = tighten(base, {"shoulder_pan": (-20, 20)})
    assert narrow["shoulder_pan"].hi < base["shoulder_pan"].hi
    with pytest.raises(ValueError):
        tighten(base, {"shoulder_lift": (170, 179)})


def test_a_pose_past_a_config_limit_stops_at_the_limit():
    cfg = config.load(None)
    cfg.limits["shoulder_pan"] = (-20, 20)
    bridge, remote, clock = make(cfg)
    assert any("pose left: shoulder_pan 35" in w for w in pose_warnings(cfg, bridge.limits))
    bridge.do("pose:left")
    run(bridge, clock, 10)
    assert degrees(bridge)["shoulder_pan"] == pytest.approx(20, abs=0.5)
    assert remote.messages()[-1] == "pose: left"


def test_volume_buttons_nudge_the_selected_joint_and_stop_at_its_limit():
    bridge, remote, clock = make()
    press(bridge, "find")
    run(bridge, clock, 10)
    for _ in range(40):
        press(bridge, "volume_up")
        run(bridge, clock, 0.5)
    roll = degrees(bridge)["wrist_roll"]
    assert 85 <= roll <= 90  # the example config limits wrist_roll to -90..90
    assert remote.messages()[-1].startswith("wrist_roll +5: ")
    bridge.do("joint")
    assert remote.messages()[-1] == "nudge joint: gripper"
    press(bridge, "volume_down")
    run(bridge, clock, 2)
    assert degrees(bridge)["gripper"] == pytest.approx(5, abs=1)


def test_torque_button_parks_then_lets_go_and_turns_back_on_without_moving():
    bridge, remote, clock = make()
    press(bridge, "find")
    run(bridge, clock, 10)
    press(bridge, "pause")
    run(bridge, clock, 0.1)
    assert remote.messages()[-1] == "parking, then torque off"
    assert bridge.arm.torque
    run(bridge, clock, 15)
    assert not bridge.arm.torque
    assert remote.messages()[-1] == "torque off"
    assert close_to(degrees(bridge), {j: REST[j] for j in ("shoulder_pan", "wrist_flex", "wrist_roll")})

    n = len(bridge.arm.goals)
    present = bridge.arm.positions()
    press(bridge, "play")
    run(bridge, clock, 1)
    assert bridge.arm.torque
    assert remote.messages()[-1] == "torque on: holding"
    assert all(g == present[j] for j, g in bridge.arm.goals[n:])


def test_stop_while_parking_keeps_torque_on():
    bridge, remote, clock = make()
    press(bridge, "find")
    run(bridge, clock, 10)
    bridge.do("torque")
    run(bridge, clock, 0.5)
    bridge.do("stop")
    run(bridge, clock, 15)
    assert bridge.arm.torque
    assert remote.messages()[-1] == "stopped: holding"


def test_a_blocked_joint_holds_and_the_next_tap_only_acknowledges_it():
    # the forearm hits something on its way up from rest
    bridge, remote, clock = make(arm=FakeArm(mech={"elbow_flex": (2600, 3076)}))
    press(bridge, "find")
    run(bridge, clock, 6)
    assert bridge.error == "elbow_flex blocked"
    assert remote.messages()[-1] == "elbow_flex blocked: holding"

    held = dict(bridge.mover.cmd)
    press(bridge, "find")
    run(bridge, clock, 2)
    assert bridge.error == ""
    assert remote.messages()[-1] == "elbow_flex blocked cleared: holding, tap again to move"
    assert bridge.mover.cmd == held

    press(bridge, "find")
    run(bridge, clock, 0.1)
    assert remote.messages()[-1] == "routine 2/4: wave"


def test_status_lines_and_unmapped_buttons_do_nothing():
    bridge, remote, clock = make()
    bridge.remote.inbox += ["Listening. Tap \"Find phone\" on the band", "notified: SO-101", "{not json",
                            json.dumps({"button": "volume", "code": 0, "volume": 7})]
    run(bridge, clock, 1)
    assert not bridge.arm.torque
    assert remote.messages() == ["arm ready, 12.4 V"]


def test_when_the_listener_ends_the_move_finishes_first():
    bridge, remote, clock = make()
    press(bridge, "find")
    run(bridge, clock, 0.1)
    remote.ended = True
    run(bridge, clock, 0.5)  # still moving: no exception yet
    with pytest.raises(SourceEnded, match="listener gone"):
        run(bridge, clock, 10)
    assert close_to(degrees(bridge), bridge.config.poses["home"])


def test_shutdown_parks_and_releases(monkeypatch):
    monkeypatch.setattr(remote_bridge.time, "sleep", lambda _s: None)
    bridge, remote, clock = make()
    press(bridge, "find")
    run(bridge, clock, 10)
    orig = bridge._control

    def control(dt):  # the parking loop sleeps on the real clock; move the fake one with it
        clock.t += dt
        orig(dt)

    bridge._control = control
    bridge.shutdown()
    assert not bridge.arm.torque
    assert remote.messages()[-2:] == ["parking", "arm released"]
    assert close_to(degrees(bridge), {"wrist_flex": REST["wrist_flex"], "shoulder_pan": 0})


def test_dry_run_arm_prints_instead_of_sending():
    lines = []
    clock = Clock()
    arm = DryRunArm(log=lines.append, clock=clock)
    remote = FakeRemote()
    bridge = BandBridge(arm, remote, config.load(None), clock=clock, log=lambda _: None)
    bridge.start()
    press(bridge, "find")
    run(bridge, clock, 10)
    assert lines[0].startswith("dry run: no serial port opened")
    assert any(ln.startswith("dry run: would turn torque on, holding pan +9 lift -104") for ln in lines)
    sends = [ln for ln in lines if ln.startswith("dry run: would send")]
    assert 10 <= len(sends) <= 60  # throttled to about 4 lines a second, not 50


def test_menu_picks_on_taps_runs_after_a_pause_and_a_tap_stops_it():
    from so101_band.controller import MENU_CONFIRM_S

    bridge, remote, clock = make()
    bridge.do("menu")
    bridge.do("menu")
    assert remote.messages()[-1].startswith("> wave (2/4)")
    assert not bridge.torque  # nothing moves while picking
    run(bridge, clock, MENU_CONFIRM_S + 0.1)
    assert remote.messages()[-1] == "running wave" and bridge.player is not None
    bridge.do("menu")  # a tap while it moves stops it
    assert bridge.player is None and remote.messages()[-1].startswith("stopped")


def test_music_buttons_pick_then_go_and_go_again_stops():
    bridge, remote, clock = make()
    bridge.do("pick_next")
    bridge.do("pick_next")
    assert remote.messages()[-1] == "> wave 2/4" and not bridge.torque
    bridge.do("pick_previous")
    bridge.do("pick_previous")
    assert remote.messages()[-1] == "> home 4/4"
    bridge.do("go")
    assert remote.messages()[-1] == "running home" and bridge.torque
    run(bridge, clock, 0.1)
    bridge.do("go")
    assert remote.messages()[-1] == "stopped"
