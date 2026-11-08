import json
import urllib.request

import pytest

from so101_band.phone import NoBand, PhoneAndBand, PhoneServer, load_token, parse_telemetry, to_line


def test_telemetry_line():
    t = parse_telemetry("T HOLD 2 0.0 -10.5 20.0 3.0 -4.0 50.0 121 wave")
    assert t["state"] == "HOLD" and t["speed"] == 2 and t["volts"] == 12.1 and t["gesture"] == "wave"
    assert t["joints"]["shoulder_lift"] == -10.5 and t["joints"]["gripper"] == 50.0
    assert parse_telemetry("M hello") == {}


def test_bodies_become_bridge_lines():
    assert to_line({"line": "G wave"}) == "G wave"
    assert to_line({"line": "J 2 -2"}) == "J 2 -2"
    assert to_line({"line": "X nope"}) is None
    assert json.loads(to_line({"action": "next"})) == {"action": "next"}
    assert json.loads(to_line({"button": "find"})) == {"button": "find"}
    assert to_line({}) is None


def test_token_is_kept(tmp_path):
    a = load_token(tmp_path)
    assert len(a) == 6 and a.isdigit()
    assert load_token(tmp_path) == a
    assert oct((tmp_path / "phone-token").stat().st_mode & 0o777) == "0o600"


@pytest.fixture
def server():
    s = PhoneServer("123456", {"live": False, "sequence": ["ready"]}, port=0, host="127.0.0.1", log=lambda *_: None)
    s._thread.start()
    yield s
    s.stop()


def call(server, method, path, body=None, token="123456"):
    req = urllib.request.Request(f"http://127.0.0.1:{server.port}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Token": token, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_commands_need_the_token(server):
    assert call(server, "POST", "/command", {"line": "S"}, token="000000")[0] == 401
    assert call(server, "GET", "/hello", token="")[0] == 200
    assert server.poll() == []


def test_commands_reach_the_bridge_and_state_comes_back(server):
    assert call(server, "POST", "/command", {"line": "G wave"}) == (200, {"ok": True})
    assert call(server, "POST", "/command", {"action": "next"})[0] == 200
    assert server.poll() == ["G wave", '{"action": "next"}']
    class Band:
        ended = False
        end_reason = ""

        def poll(self):
            return []

        def send(self, line):
            pass

    remote = PhoneAndBand(Band(), server)
    remote.send("T MOVE 3 1.0 2.0 3.0 4.0 5.0 6.0 120 wave")
    remote.send("M wave")
    code, state = call(server, "GET", "/state")
    assert code == 200 and state["state"] == "MOVE" and state["message"] == "wave" and state["sequence"] == ["ready"]
    assert state["joints"]["wrist_roll"] == 5.0


def test_no_band_never_ends():
    b = NoBand()
    assert b.lines() == [] and not b.ended
