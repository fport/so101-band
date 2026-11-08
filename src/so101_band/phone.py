"""The phone as a remote: a small HTTP server on the local network that the Openband iPhone app talks to.

The app sends what the ESP32 screen of so101-remote sends, as JSON, and reads back what the screen shows:

    GET  /state                   -> {"state", "speed", "joints": {name: degrees}, "volts", "gesture",
                                      "message", "live", "sequence", "poses", "routines", "joint_names"}
    POST /command {"line": "G wave"}        so101-remote's protocol: J <joint 1..6> <delta>, G <gesture>,
                                            S (stop), R (release), V <1..3>
    POST /command {"action": "next"}        so101-band's actions: next, previous, pose:NAME, routine:NAME,
                                            torque, stop, joint, nudge:+N
    POST /command {"button": "find"}        as if the band had sent that press

Every request needs the header `X-Token: <token>`; the token is printed at start and kept in the config folder,
so the phone enters it once. The server announces itself over Bonjour as `_so101band._tcp`, so the app finds the
Mac by itself. Nothing leaves the local network.
"""

from __future__ import annotations

import json
import queue
import secrets
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from so101_remote.arm import JOINTS

SERVICE = "_so101band._tcp"
DEFAULT_PORT = 8765
ESP_COMMANDS = {"J", "G", "S", "R", "V"}


def load_token(folder: Path) -> str:
    path = folder / "phone-token"
    if path.exists():
        token = path.read_text().strip()
        if token:
            return token
    folder.mkdir(parents=True, exist_ok=True)
    token = f"{secrets.randbelow(10**6):06d}"
    path.write_text(token + "\n")
    path.chmod(0o600)
    return token


def parse_telemetry(line: str) -> dict:
    """`T <state> <speed> <6 joint values> <volts*10> <gesture>` from so101-remote's bridge."""
    parts = line.split()
    if len(parts) < 11 or parts[0] != "T":
        return {}
    try:
        values = [float(v) for v in parts[3:9]]
        return {
            "state": parts[1],
            "speed": int(parts[2]),
            "joints": dict(zip(JOINTS, values, strict=True)),
            "volts": int(parts[9]) / 10,
            "gesture": None if parts[10] == "-" else parts[10],
        }
    except ValueError:
        return {}


def to_line(body: dict) -> str | None:
    """A request body as one line for the bridge, or None when it is not a command we take."""
    if isinstance(body.get("line"), str):
        line = body["line"].strip()
        return line if line.split()[:1] and line.split()[0].upper() in ESP_COMMANDS else None
    if isinstance(body.get("action"), str) and body["action"].strip():
        return json.dumps({"action": body["action"].strip()})
    if isinstance(body.get("button"), str) and body["button"].strip():
        return json.dumps({"button": body["button"].strip()})
    return None


class PhoneServer:
    def __init__(self, token: str, info: dict, port: int = DEFAULT_PORT, host: str = "0.0.0.0", log=print):
        self.token = token
        self.info = info  # live, sequence, poses, routines: static for the run
        self.log = log
        self.lines: queue.Queue[str] = queue.Queue()
        self._state: dict = {}
        self._message = ""
        self._lock = threading.Lock()
        self._bonjour: subprocess.Popen | None = None
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args) -> None:  # quiet
                pass

            def _reply(self, code: int, body: dict) -> None:
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def _authorized(self) -> bool:
                if secrets.compare_digest(self.headers.get("X-Token", ""), server.token):
                    return True
                self._reply(401, {"error": "wrong or missing X-Token"})
                return False

            def do_GET(self) -> None:  # noqa: N802
                if self.path.rstrip("/") == "/hello":
                    self._reply(200, {"service": "so101-band", "live": server.info.get("live", False)})
                    return
                if not self._authorized():
                    return
                if self.path.rstrip("/") == "/state":
                    self._reply(200, server.snapshot())
                else:
                    self._reply(404, {"error": "not found"})

            def do_POST(self) -> None:  # noqa: N802
                if not self._authorized():
                    return
                if self.path.rstrip("/") != "/command":
                    self._reply(404, {"error": "not found"})
                    return
                try:
                    size = min(int(self.headers.get("Content-Length", "0")), 4096)
                    body = json.loads(self.rfile.read(size) or b"{}")
                except (ValueError, json.JSONDecodeError):
                    self._reply(400, {"error": "bad json"})
                    return
                line = to_line(body) if isinstance(body, dict) else None
                if line is None:
                    self._reply(400, {"error": "expected line, action or button"})
                    return
                server.lines.put(line)
                server.log(f"phone: {line}")
                self._reply(200, {"ok": True})

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.port = self.httpd.server_address[1]
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def start(self, name: str = "SO-101") -> None:
        self._thread.start()
        if shutil.which("dns-sd"):
            self._bonjour = subprocess.Popen(["dns-sd", "-R", name, SERVICE, "local", str(self.port)],
                                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        if self._bonjour:
            self._bonjour.terminate()
        self.httpd.shutdown()
        self.httpd.server_close()

    # what the bridge reports
    def report(self, line: str) -> None:
        with self._lock:
            if line.startswith("T "):
                parsed = parse_telemetry(line)
                if parsed:
                    self._state = parsed
            elif line.startswith("M "):
                self._message = line[2:]

    def snapshot(self) -> dict:
        with self._lock:
            return {**self.info, **self._state, "message": self._message, "joint_names": list(JOINTS)}

    def poll(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self.lines.get_nowait())
            except queue.Empty:
                return out


class NoBand:
    """The band source when the band is with the phone: never says anything, never ends."""

    ended = False
    end_reason = ""

    def start(self) -> None:
        pass

    def lines(self) -> list[str]:
        return []

    def stop(self) -> None:
        pass


class PhoneAndBand:
    """so101-remote's Remote protocol over the band remote and the phone server together. The bridge's
    telemetry goes to the phone (the band would only get noise); messages go to both."""

    def __init__(self, band, phone: PhoneServer):
        self.band = band
        self.phone = phone

    heartbeat = False

    def poll(self) -> list[str]:
        return self.band.poll() + self.phone.poll()

    def send(self, line: str) -> None:
        self.phone.report(line)
        if line.startswith("M "):
            self.band.send(line)

    @property
    def ended(self) -> bool:
        return self.band.ended

    @property
    def end_reason(self) -> str:
        return self.band.end_reason
