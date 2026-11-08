"""The band side: `openband listen` as a child process, `openband notify` for feedback, and a stdin stand-in.

`openband listen` prints one JSON line per tap, for example
    {"ts": 1794152946, "button": "find", "code": 0, "volume": null}
and plain status lines ("Listening. ...", "notified: ...") in between. `openband notify TITLE TEXT --no-bluetooth`
hands a notification to that running listener over its local socket, so it never opens Bluetooth itself.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Press:
    button: str
    code: int | None = None
    volume: int | None = None


def parse_line(line: str) -> Press | None:
    """A tap from one line of `openband listen`, or None for status lines and anything malformed."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    button = data.get("button") if isinstance(data, dict) else None
    if not isinstance(button, str) or not button:
        return None
    return Press(button, data.get("code"), data.get("volume"))


def split_command(command: str) -> list[str]:
    """shlex.split, with ~ expanded in every word (also after '=', as in --project=~/x)."""
    words = []
    for word in shlex.split(command):
        key, eq, value = word.partition("=")
        if eq and value.startswith("~") and key.startswith("-"):
            word = f"{key}={os.path.expanduser(value)}"
        words.append(os.path.expanduser(word))
    return words


class Listener:
    """Runs `openband listen` and collects its stdout lines on a thread.

    It gets its own process group: Ctrl+C reaches us only, so the band still hears "parking" and "released"
    while the arm folds, and we stop the listener ourselves at the very end."""

    def __init__(self, command: list[str], log: Callable[[str], None] = print):
        self.command = command
        self.log = log
        self.proc: subprocess.Popen | None = None
        self._q: queue.Queue[str] = queue.Queue()
        self._done = threading.Event()

    def start(self) -> None:
        try:
            # Its log (stderr) goes to a file next to ours, so a stuck step can be read back afterwards.
            log_path = Path.home() / ".config" / "so101-band" / "listen.log"
            try:
                log_path.parent.mkdir(parents=True, exist_ok=True)
                stderr = log_path.open("a")
            except OSError:
                stderr = None
            self.proc = subprocess.Popen(self.command, stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True,
                                         bufsize=1, start_new_session=True, stderr=stderr)
            # Its own session keeps Ctrl+C away from it, so make sure it goes with us even when a second
            # Ctrl+C cuts our own cleanup short.
            atexit.register(self.stop, 1.0)
        except OSError as e:
            self.log(f"cannot start {shlex.join(self.command)}: {e}")
            self._done.set()
            return
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc and self.proc.stdout
        for line in self.proc.stdout:
            self._q.put(line.rstrip("\n"))
        self.proc.wait()
        self._done.set()

    def lines(self) -> list[str]:
        out = []
        while not self._q.empty():
            out.append(self._q.get())
        return out

    @property
    def ended(self) -> bool:
        return self._done.is_set() and self._q.empty()

    @property
    def end_reason(self) -> str:
        code = self.proc.returncode if self.proc else None
        hint = ""
        if code == 134:
            hint = " (macOS stops a process without Bluetooth permission: run from Terminal or iTerm)"
        return f"openband listen exited with code {code}{hint}"

    def stop(self, timeout: float = 3.0) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(self.proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                return
            try:
                self.proc.wait(timeout)
                return
            except subprocess.TimeoutExpired:
                continue


class StdinSource:
    """Type button names (find, next, previous, play, pause, volume_up, volume_down), one per line, or paste the
    JSON lines `openband listen` prints. For trying everything without the band."""

    def __init__(self, stream=None):
        self.stream = stream
        self._q: queue.Queue[str] = queue.Queue()
        self._done = threading.Event()

    def start(self) -> None:
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        for line in self.stream or sys.stdin:
            word = line.strip()
            if word and not word.startswith("{"):
                word = json.dumps({"button": word})
            if word:
                self._q.put(word)
        self._done.set()

    def lines(self) -> list[str]:
        out = []
        while not self._q.empty():
            out.append(self._q.get())
        return out

    @property
    def ended(self) -> bool:
        return self._done.is_set() and self._q.empty()

    end_reason = "end of input"

    def stop(self) -> None:
        pass


class Notifier:
    """Sends short texts to the band with `openband notify`, one at a time, on a worker thread, so the arm's
    control loop never waits for it. With no command it only drops them."""

    MAX_QUEUED = 4

    def __init__(self, command: list[str] | None, title: str, log: Callable[[str], None] = print,
                 runner: Callable[..., subprocess.CompletedProcess] = subprocess.run, music: bool = False):
        self.command = command
        self.title = title
        self.music = music  # the band's music screen title instead of a notification
        # Whether the arm is moving: the band shows pause while "playing" and play otherwise, so the one
        # button always offers what it will do. Set by the bridge.
        self.playing: Callable[[], bool] = lambda: False
        self.log = log
        self.runner = runner
        self._q: queue.Queue[str | None] = queue.Queue()
        self._thread: threading.Thread | None = None

    def argv(self, text: str) -> list[str]:
        assert self.command
        # "--" so a text such as "-5 deg" is not read as an option
        if self.music:
            return [*self.command, "--music", "--", text]
        return [*self.command, "--no-bluetooth", "--", self.title, text]

    def notify(self, text: str) -> None:
        if not self.command:
            return
        if self._q.qsize() >= self.MAX_QUEUED:
            return  # the band is behind; the newest news is not worth a pile-up
        if self._thread is None:
            self._thread = threading.Thread(target=self._work, daemon=True)
            self._thread.start()
        self._q.put(text)

    def _socket_path(self) -> Path:
        home = Path(os.environ.get("OPENBAND_HOME", Path.home() / ".config" / "openband"))
        return home / "listen.sock"

    def _via_socket(self, text: str) -> bool:
        """Straight to the running `openband listen` (what `openband notify` would do, minus starting a
        Python process per text, which took a second or two per press). False when there is no listener."""
        path = self._socket_path()
        if not path.exists():
            return False
        payload = ({"music": text, "playing": self.playing()} if self.music
                   else {"title": self.title, "text": text, "sender": self.title})
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5.0)
                s.connect(str(path))
                s.sendall((json.dumps(payload) + "\n").encode())
                answer = s.recv(256).decode(errors="replace").strip()
        except (FileNotFoundError, ConnectionRefusedError):
            return False  # no listener: fall back to `openband notify`
        except OSError as e:
            # The listener is there but the band is not connected yet: drop the text rather than queue
            # behind it, so later presses stay quick.
            self.log(f"band notify skipped: {e}")
            return True
        if answer != "ok":
            self.log(f"band notify failed: {answer or 'no answer'}")
        else:
            self.log(f"  (on the band at {time.time():.3f})")
        return True

    def _work(self) -> None:
        while (text := self._q.get()) is not None:
            if self._via_socket(text):
                continue
            if self.music:
                continue  # the music screen only exists through a running listener; none yet: skip it
            try:
                res = self.runner(self.argv(text), capture_output=True, text=True, timeout=30)
                if res.returncode != 0:
                    msg = (res.stdout or res.stderr or "").strip().splitlines()
                    self.log(f"band notify failed: {msg[-1] if msg else f'exit {res.returncode}'}")
            except (OSError, subprocess.TimeoutExpired) as e:
                self.log(f"band notify failed: {e}")

    def close(self, timeout: float = 5.0) -> None:
        """Let queued notifications go out (up to timeout seconds), then stop the worker."""
        if self._thread is None:
            return
        self._q.put(None)
        self._thread.join(timeout)


class BandRemote:
    """so101-remote's Remote protocol: the bridge polls it for lines and sends it "M <text>" messages, which go
    to the band. Telemetry ("T ...") is for the ESP32 screen and is dropped. No heartbeat: a wrist is not a screen
    that pings every 250 ms, and the arm only moves on a tap anyway."""

    heartbeat = False

    def __init__(self, source: Listener | StdinSource, notifier: Notifier):
        self.source = source
        self.notifier = notifier

    def poll(self) -> list[str]:
        return self.source.lines()

    def send(self, line: str) -> None:
        if line.startswith("M "):
            self.notifier.notify(line[2:])

    @property
    def ended(self) -> bool:
        return self.source.ended

    @property
    def end_reason(self) -> str:
        return self.source.end_reason
