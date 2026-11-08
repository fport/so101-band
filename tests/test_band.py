import io
import os
import subprocess
import sys
import time

from so101_band.band import BandRemote, Listener, Notifier, Press, StdinSource, parse_line, split_command


def test_parse_line_reads_openband_listen_output():
    line = '{"ts": 1794152946, "button": "find", "code": 0, "volume": null}'
    assert parse_line(line) == Press("find", 0, None)
    assert parse_line('{"ts": 1, "button": "volume_up", "code": 5, "volume": 6}\n') == Press("volume_up", 5, 6)


def test_parse_line_ignores_status_lines_and_junk():
    for line in ["Listening. Tap \"Find phone\" on the band", "notified: SO-101", "", "{broken", "[1, 2]",
                 '{"button": ""}', '{"ts": 1}', '{"button": 3}']:
        assert parse_line(line) is None


def test_split_command_expands_home():
    home = os.path.expanduser("~")
    assert split_command("uv run --project ~/www/openband openband listen") == [
        "uv", "run", "--project", f"{home}/www/openband", "openband", "listen"]
    assert split_command("uv run --project=~/x 'a b'") == ["uv", "run", f"--project={home}/x", "a b"]


class Runs:
    def __init__(self, code=0, stdout="Sent.\n"):
        self.calls = []
        self.code, self.stdout = code, stdout

    def __call__(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, self.code, self.stdout, "")


def test_notifier_runs_openband_notify_without_bluetooth():
    runs, logs = Runs(), []
    n = Notifier(["openband", "notify"], "SO-101", log=logs.append, runner=runs)
    n.notify("pose 2/4: wave")
    n.notify("-5 deg")
    n.close()
    assert runs.calls == [["openband", "notify", "--no-bluetooth", "--", "SO-101", "pose 2/4: wave"],
                          ["openband", "notify", "--no-bluetooth", "--", "SO-101", "-5 deg"]]
    assert logs == []


def test_notifier_reports_a_failure_and_carries_on():
    runs, logs = Runs(code=2, stdout="error: `openband listen` is not running\n"), []
    n = Notifier(["openband", "notify"], "SO-101", log=logs.append, runner=runs)
    n.notify("a")
    n.close()
    assert logs == ["band notify failed: error: `openband listen` is not running"]


def test_notifier_without_a_command_sends_nothing():
    runs = Runs()
    n = Notifier(None, "SO-101", runner=runs)
    n.notify("a")
    n.close()
    assert runs.calls == []


def _wait(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        time.sleep(0.02)
    return cond()


def test_listener_collects_lines_and_reports_its_exit():
    script = "import json; print('Listening.'); print(json.dumps({'button': 'find'})); raise SystemExit(3)"
    lst = Listener([sys.executable, "-c", script], log=lambda _: None)
    lst.start()
    got = []
    assert _wait(lambda: (got.extend(lst.lines()) or lst.ended))
    assert got == ["Listening.", '{"button": "find"}']
    assert lst.end_reason == "openband listen exited with code 3"


def test_listener_stop_ends_a_running_listener():
    lst = Listener([sys.executable, "-c", "import time; print('Listening.', flush=True); time.sleep(60)"])
    lst.start()
    assert _wait(lambda: lst.lines() == ["Listening."] or True)
    t0 = time.monotonic()
    lst.stop()
    assert time.monotonic() - t0 < 5
    assert lst.proc.poll() is not None


def test_listener_that_cannot_start_ends_at_once():
    logs = []
    lst = Listener(["/nonexistent/openband", "listen"], log=logs.append)
    lst.start()
    assert lst.ended
    assert logs and logs[0].startswith("cannot start /nonexistent/openband listen")


def test_stdin_source_turns_button_names_into_listen_lines():
    src = StdinSource(io.StringIO("find\n\n  next \n{\"button\": \"play\"}\n"))
    src.start()
    got = []
    assert _wait(lambda: (got.extend(src.lines()) or src.ended))
    assert [parse_line(ln).button for ln in got] == ["find", "next", "play"]


def test_band_remote_forwards_messages_and_drops_telemetry():
    runs = Runs()
    remote = BandRemote(StdinSource(io.StringIO("")), Notifier(["n"], "T", runner=runs))
    remote.send("T HOLD 3 0 0 0 0 0 0 124 -")
    remote.send("M pose 1/4: home")
    remote.notifier.close()
    assert runs.calls == [["n", "--no-bluetooth", "--", "T", "pose 1/4: home"]]
    assert remote.heartbeat is False
