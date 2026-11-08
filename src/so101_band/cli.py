"""so101-band: drive an SO-101 arm from a Huawei Band 6.

  so101-band init                   copy the example config to ~/.config/so101-band/config.toml
  so101-band poses                  list poses, routines, the tap sequence and the buttons
  so101-band run                    dry run: taps print what would be sent to the arm
  so101-band run --live             taps move the arm
  so101-band run --stdin            type button names instead of tapping the band
  so101-band record NAME            save the arm's present position as a pose (reads only)
"""

from __future__ import annotations

import argparse
import shlex
import signal
import sys
from pathlib import Path

from . import __version__
from . import config as cfgmod
from .config import ConfigError


def out(msg: str = "") -> None:
    print(msg, flush=True)


def _config_path(args) -> Path:
    return Path(args.config).expanduser() if args.config else cfgmod.default_path()


def _load(args) -> cfgmod.Config:
    path = _config_path(args)
    if path.exists():
        return cfgmod.load(path)
    if args.config:
        raise ConfigError(f"{path} does not exist")
    out(f"No config at {path}: using the example (`so101-band init` makes an editable copy).")
    return cfgmod.load(None)


def _port(args, cfg) -> str | None:
    port = args.port or (cfg.port if cfg.port != "auto" else None)
    if port:
        return port
    from so101_remote.cli import find_follower  # opens each serial port and reads the supply voltage

    return find_follower()


# -- commands ------------------------------------------------------------------------------------


def do_init(args) -> int:
    path = _config_path(args)
    if path.exists() and not args.force:
        out(f"{path} already exists (--force overwrites it).")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfgmod.example_text())
    out(f"Wrote {path}")
    return 0


def do_poses(args) -> int:
    from so101_remote.arm import JOINTS
    from so101_remote.gestures import GESTURES

    cfg = _load(args)
    out(f"config: {cfg.path or 'built-in example'}")
    out("\nPoses (degrees from the calibration middle pose; gripper % open)")
    out("  " + f"{'name':<14}" + "".join(f"{j[:13]:>14}" for j in JOINTS))
    for name, pose in cfg.all_poses().items():
        tag = " *" if name in cfgmod.BUILTIN_POSES and name not in cfg.poses else ""
        cells = "".join(f"{pose[j]:>14g}" if j in pose else f"{'-':>14}" for j in JOINTS)
        out(f"  {name + tag:<14}{cells}")
    out("\nRoutines")
    for name, steps in cfg.routines.items():
        out(f"  {name:<14}" + " -> ".join(s.pose + (f" ({s.hold:g} s)" if s.hold else "") for s in steps))
    out(f"  {', '.join(g for g in GESTURES if g not in cfg.routines)} *")
    out("  * built in, from so101-remote")
    n = len(cfg.sequence)
    out(f"\nSequence (find / next / previous), {n} steps")
    out("  " + " -> ".join(f"{i}. {s}" for i, s in enumerate(cfg.sequence, 1)))
    out("\nButtons")
    for button in cfgmod.BUTTONS:
        out(f"  {button:<12} {cfg.buttons.get(button, 'none')}")
    out(f"\nnudge joint: {cfg.nudge_joint}; top speed {cfg.max_speed_deg_s:g} deg/s; port {cfg.port}")
    if cfg.limits:
        out("extra limits: " + ", ".join(f"{j} {lo:g}..{hi:g}" for j, (lo, hi) in cfg.limits.items()))
    return 0


def do_record(args) -> int:
    from so101_remote.arm import JOINTS, FakeArm, FeetechArm
    from so101_remote.motion import from_ticks, limits_from_eeprom

    path = _config_path(args)
    if args.mock:
        arm = FakeArm()
    else:
        cfg = cfgmod.load(path) if path.exists() else cfgmod.load(None)
        port = _port(args, cfg)
        if not port:
            out("No follower found: plug in its USB cable and its 12 V supply, or pass --port.")
            return 1
        arm = FeetechArm(port)
        out(f"reading the follower on {port} (read only: nothing is written to the motors)")
    arm.connect()
    try:
        limits = limits_from_eeprom(arm.limits())
        ticks = arm.positions()
    finally:
        arm.close()
    pose = {}
    for j in JOINTS:
        v = from_ticks(j, ticks[j], limits)
        lo, hi = (0, 100) if j == "gripper" else (-180, 180)
        pose[j] = round(min(hi, max(lo, v)), 1)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cfgmod.example_text())
        out(f"Wrote {path} from the example")
    replaced = cfgmod.record_pose(path, args.name, pose)
    out(f"{'Replaced' if replaced else 'Added'} in {path}:\n  {cfgmod.pose_line(args.name, pose)}")
    return 0


def do_run(args) -> int:
    from so101_remote.arm import ArmError

    from .band import BandRemote, Listener, Notifier, StdinSource, split_command
    from .controller import BandBridge, SourceEnded
    from .dryrun import DryRunArm

    cfg = _load(args)
    if args.max_speed is not None:
        if not 1 <= args.max_speed <= cfgmod.MAX_SPEED_DEG_S:
            out(f"--max-speed: keep it between 1 and {cfgmod.MAX_SPEED_DEG_S:g}")
            return 2
        cfg.max_speed_deg_s = args.max_speed
    if args.live:
        from so101_remote.arm import FeetechArm

        port = _port(args, cfg)
        if not port:
            out("No follower found: plug in its USB cable and its 12 V supply, or pass --port.")
            return 1
        arm = FeetechArm(port)
        out(f"LIVE: the follower on {port} moves on every tap. Keep the workspace clear.")
    else:
        arm = DryRunArm(log=out)
        out("DRY RUN: nothing goes to the arm. Pass --live to move it.")

    title = cfg.title if args.live else f"{cfg.title} (dry run)"
    notifier = Notifier(None if args.no_notify else split_command(args.notify_cmd or cfg.notify), title, log=out)
    if args.stdin:
        source = StdinSource()
        out("Type a button per line: find, next, previous, play, pause, volume_up, volume_down. Ctrl+D ends.")
    else:
        command = split_command(args.listen_cmd or cfg.listen)
        source = Listener(command, log=out)
        out(f"starting: {shlex.join(command)}")
    bridge = BandBridge(arm, BandRemote(source, notifier), cfg, log=out)

    # Ctrl+C and `kill` both park the arm and let go (see so101-remote). Restored explicitly: a shell that
    # starts us in the background ignores SIGINT, and Python then never raises KeyboardInterrupt.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    code = 0
    source.start()
    try:
        bridge.run()
    except SourceEnded as e:
        out(str(e))
        code = 0 if args.stdin else 1
    except ArmError as e:
        out(f"arm error: {e}")
        code = 1
    except ValueError as e:  # [arm.limits] that leave nothing of the calibrated range
        out(f"error: {e}")
        arm.close()
        code = 2
    finally:
        notifier.close()
        source.stop()
    return code


# -- entry point ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="so101-band", description="Drive an SO-101 arm from a Huawei Band 6.")
    p.add_argument("--version", action="version", version=f"so101-band {__version__}")
    p.add_argument("--config", help="config file (default: $SO101_BAND_CONFIG or ~/.config/so101-band/config.toml)")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("init", help="copy the example config to the config path")
    s.add_argument("--force", action="store_true", help="overwrite an existing config")

    sub.add_parser("poses", help="list poses, routines, the tap sequence and the buttons")

    s = sub.add_parser("run", help="map band taps to arm motion (a dry run unless --live)")
    mode = s.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="live", action="store_false", help="print instead of moving (the default)")
    mode.add_argument("--live", dest="live", action="store_true", help="move the real arm")
    s.set_defaults(live=False)
    s.add_argument("--port", help="the follower's serial port (default: config, else the arm on 12 V)")
    s.add_argument("--max-speed", type=float, help="top speed in deg/s for this run (config: arm.max_speed_deg_s)")
    s.add_argument("--listen-cmd", help="command that prints band taps (config: openband.listen)")
    s.add_argument("--notify-cmd", help="command that shows a text on the band (config: openband.notify)")
    s.add_argument("--no-notify", action="store_true", help="do not send feedback to the band")
    s.add_argument("--stdin", action="store_true", help="read button names from stdin instead of the band")

    s = sub.add_parser("record", help="save the arm's present position as a pose (reads only)")
    s.add_argument("name")
    s.add_argument("--port", help="the follower's serial port (default: config, else the arm on 12 V)")
    s.add_argument("--mock", action="store_true", help="read a simulated arm (folded rest pose)")
    return p


COMMANDS = {"init": do_init, "poses": do_poses, "run": do_run, "record": do_record}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](args)
    except ConfigError as e:
        out(f"config error: {e}")
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
