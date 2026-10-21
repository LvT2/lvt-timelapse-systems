"""
Timelapse snapshot capture for the König CMP-NWIPCAM10, using konig_camera.py.

Takes one picture every few seconds and saves each one as a .jpg in a
folder, named after the date and time it was taken (20261017-203810.jpg).
It keeps going until you press Ctrl+C, unless you give it a duration,
a number of pictures or an end time. Run it again with the same folder
and it carries on where it left off.

Turn the pictures into a video afterwards with make_video.sh.

Needs:  pip install requests
(keep konig_camera.py in the same folder as this file)

Examples:
    # every 10 seconds until Ctrl+C
    python timelapse.py 192.168.1.50:8080 --interval 10

    # every 30 seconds for 8 hours
    python timelapse.py 192.168.1.50:8080 --interval 30 --duration 8h

    # every minute, only in daylight, every day, until Ctrl+C
    python timelapse.py 192.168.1.50:8080 --interval 1m --between 07:00-19:30

    # pan one step every 20 pictures: 31 steps right, then back left
    python timelapse.py 192.168.1.50:8080 --interval 10 --pan-every 20
    (or set PAN_EVERY_N_PHOTOS / PAN_STEPS below instead of using the options)

From Python:
    from konig_camera import KonigCamera
    from timelapse import TimelapseRecorder

    with KonigCamera("192.168.1.50:8080", "admin", "password") as cam:
        TimelapseRecorder(cam, "frames", interval=10).capture()   # until Ctrl+C
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Callable, Optional

import requests

from konig_camera import CameraAuthError, CameraError, KonigCamera

STAMP_FORMAT = "%Y%m%d-%H%M%S"
# 20261017-203810.jpg, plus _1, _2 … when several pictures fall in one second.
# ("_" sorts after ".", so the files stay in time order when sorted by name.)
FRAME_RE = re.compile(r"^\d{8}-\d{6}(?:_\d+)?\.jpg$")

# ---------------------------------------------------------------- panning
# Every PAN_EVERY_N_PHOTOS pictures the camera moves one step. It goes
# right PAN_STEPS times, then back left PAN_STEPS times, and so on.
# Set PAN_EVERY_N_PHOTOS = 0 to switch panning off.
# (--pan-every and --pan-steps on the command line override these.)
PAN_EVERY_N_PHOTOS = 0
PAN_STEPS = 32
PAN_RIGHT_PATH = "/motor_control_right"
PAN_LEFT_PATH = "/motor_control_left"
# Seconds to wait after a move so the motor has stopped before the next picture.
PAN_SETTLE_SECONDS = 2.0
# False: come back left one step at a time, like the way out (a sweep).
# True:  after the last step right, go all the way back left at once
#        (PAN_STEPS left moves in a row) and start again.
PAN_RETURN_ALL_AT_ONCE = False
# Remembers where the camera is pointing, so a restart carries on the sweep.
PAN_STATE_FILE = "pan_state.json"


class TimelapseRecorder:
    def __init__(
        self,
        camera: KonigCamera,
        folder: str | Path = "timelapse_frames",
        interval: float = 10.0,
        between: Optional[tuple[dtime, dtime]] = None,
        log: Callable[[str], None] = print,
        pan_every: int = PAN_EVERY_N_PHOTOS,
        pan_steps: int = PAN_STEPS,
    ):
        """
        camera:   a KonigCamera
        folder:   where the pictures go (created if needed)
        interval: seconds between pictures
        between:  optional (start, end) daily time window, e.g.
                  (time(7, 0), time(19, 30)); pictures are only taken
                  inside it. A window like 22:00-06:00 crosses midnight.
        pan_every: move the camera one step every this many pictures
                   (0 = no panning)
        pan_steps: steps right before turning back left
        """
        if interval <= 0:
            raise ValueError("interval must be more than 0 seconds")
        if pan_every < 0 or pan_steps < 1:
            raise ValueError("pan_every must be 0 or more and pan_steps at least 1")
        self.pan_every = int(pan_every)
        self.pan_steps = int(pan_steps)
        self._since_pan = 0
        self._pan_position = 0       # steps right of the starting point
        self._pan_direction = "right"
        self.camera = camera
        self.folder = Path(folder)
        self.interval = float(interval)
        self.between = between
        self.log = log
        self._stop = False

    def stop(self) -> None:
        """Ask a running capture() to finish (Ctrl+C does this too)."""
        self._stop = True

    def frame_files(self) -> list[Path]:
        """All pictures in the folder, oldest first."""
        if not self.folder.is_dir():
            return []
        return sorted(p for p in self.folder.iterdir() if FRAME_RE.match(p.name))

    def capture(
        self,
        duration: Optional[float] = None,
        frames: Optional[int] = None,
        until: Optional[datetime] = None,
    ) -> int:
        """
        Take pictures until `duration` seconds have passed, `frames` new
        pictures were taken, or the clock reaches `until`. With none of
        these it runs until stop() or Ctrl+C.
        Returns the number of new pictures saved.
        """
        self.folder.mkdir(parents=True, exist_ok=True)
        self._stop = False
        end = datetime.now() + timedelta(seconds=duration) if duration else None
        if until and (end is None or until < end):
            end = until

        existing = len(self.frame_files())
        self.log(
            f"Saving a picture every {_fmt_secs(self.interval)} to {self.folder.resolve()}"
            + (f" ({existing} already there)" if existing else "")
        )
        if end:
            self.log(f"Stops at {end:%Y-%m-%d %H:%M:%S}, or press Ctrl+C.")
        elif frames:
            self.log(f"Stops after {frames} pictures, or press Ctrl+C.")
        else:
            self.log("Runs until you press Ctrl+C.")
        if self.between:
            a, b = self.between
            self.log(f"Only between {a:%H:%M} and {b:%H:%M}.")
        if self.pan_every:
            self._load_pan_state()
            back = "all at once" if PAN_RETURN_ALL_AT_ONCE else "step by step"
            self.log(
                f"Panning one step every {self.pan_every} pictures: {self.pan_steps} steps right, "
                f"then back left {back}. Now at step {self._pan_position}, "
                f"going {self._pan_direction}."
            )

        saved = 0
        failures = 0
        next_shot = time.monotonic()
        waiting_logged = False

        try:
            while not self._stop:
                now = datetime.now()
                if end and now >= end:
                    break
                if frames and saved >= frames:
                    break

                # Outside the daily window: wait until it opens (or until the end).
                if self.between and not _in_window(now.time(), *self.between):
                    opens = _next_window_start(now, self.between[0])
                    if end and opens >= end:
                        break
                    if not waiting_logged:
                        self.log(f"Outside the time window, waiting until {opens:%Y-%m-%d %H:%M}")
                        waiting_logged = True
                    self._sleep(min(60.0, (opens - now).total_seconds()))
                    next_shot = time.monotonic()
                    continue
                waiting_logged = False

                try:
                    jpeg = self.camera.snapshot()
                    path = self._frame_path(datetime.now())
                    tmp = path.with_suffix(".part")
                    tmp.write_bytes(jpeg)
                    tmp.replace(path)  # never leaves half-written pictures
                    saved += 1
                    if failures:
                        self.log("Camera is back.")
                    failures = 0
                    self.log(f"[{saved + existing:>6}] {path.name}  ({len(jpeg) // 1024} KB)")

                    if self.pan_every:
                        self._since_pan += 1
                        if self._since_pan >= self.pan_every:
                            if self._pan_step():
                                # Give the motor time to stop before the next picture.
                                self._sleep(PAN_SETTLE_SECONDS)
                        else:
                            self._save_pan_state()
                except CameraAuthError:
                    raise
                except (CameraError, requests.RequestException, OSError) as e:
                    failures += 1
                    if failures in (1, 5) or failures % 30 == 0:
                        self.log(f"Missed a picture ({failures} in a row): {_short_error(e)}")
                    # The camera may have rebooted onto another address: look again.
                    if failures % 5 == 0:
                        self.camera._detected = False

                # Fixed schedule, so timing doesn't drift; if we fell behind
                # (slow camera, computer asleep), skip ahead instead of rushing.
                next_shot += self.interval
                behind = time.monotonic() - next_shot
                if behind > 0:
                    next_shot += (behind // self.interval + 1) * self.interval
                self._sleep(next_shot - time.monotonic())
        except KeyboardInterrupt:
            pass  # Ctrl+C when not run from the command line

        self.log(f"Stopped: {saved} new pictures, {saved + existing} in total.")
        return saved

    # ------------------------------------------------------------ panning

    def _pan_step(self) -> bool:
        """Move one step (or all the way back). Returns False if the camera refused."""
        try:
            if self._pan_direction == "right":
                self.camera.command(PAN_RIGHT_PATH)
                self._pan_position += 1
                self.log(f"         panned right ({self._pan_position}/{self.pan_steps})")
                if self._pan_position >= self.pan_steps:
                    self._pan_direction = "left"
                    if PAN_RETURN_ALL_AT_ONCE:
                        self._return_all_the_way()
            elif PAN_RETURN_ALL_AT_ONCE:
                self._return_all_the_way()  # carries on if an earlier return was cut short
            else:
                self.camera.command(PAN_LEFT_PATH)
                self._pan_position -= 1
                self.log(f"         panned left  ({self._pan_position}/{self.pan_steps})")
                if self._pan_position <= 0:
                    self._pan_position = 0
                    self._pan_direction = "right"
            self._since_pan = 0
            return True
        except CameraAuthError:
            raise
        except (CameraError, requests.RequestException) as e:
            self.log(f"         couldn't pan {self._pan_direction} ({_short_error(e)}), trying again next picture")
            return False
        finally:
            self._save_pan_state()

    def _return_all_the_way(self) -> None:
        self.log(f"         going back left {self._pan_position} steps…")
        while self._pan_position > 0 and not self._stop:
            self.camera.command(PAN_LEFT_PATH)
            self._pan_position -= 1
            self._save_pan_state()
            # Small gap so the camera doesn't drop commands sent too fast.
            self._sleep(0.3)
        if self._pan_position <= 0:
            self._pan_direction = "right"
            self.log("         back at the start")

    def _load_pan_state(self) -> None:
        try:
            data = json.loads((self.folder / PAN_STATE_FILE).read_text())
            pos = int(data["position"])
            direction = data["direction"]
            if direction in ("right", "left") and 0 <= pos <= self.pan_steps:
                self._pan_position, self._pan_direction = pos, direction
                self._since_pan = max(0, int(data.get("since_pan", 0)))
            if pos > self.pan_steps:  # steps were lowered since last run
                self._pan_position, self._pan_direction = self.pan_steps, "left"
        except (OSError, ValueError, KeyError, TypeError):
            pass  # first run: start position, going right

    def _save_pan_state(self) -> None:
        try:
            tmp = self.folder / (PAN_STATE_FILE + ".part")
            tmp.write_text(json.dumps({
                "position": self._pan_position,
                "direction": self._pan_direction,
                "since_pan": self._since_pan,
            }))
            tmp.replace(self.folder / PAN_STATE_FILE)
        except OSError:
            pass

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, seconds)
        while not self._stop:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.5))

    def _frame_path(self, when: datetime) -> Path:
        base = when.strftime(STAMP_FORMAT)
        path = self.folder / f"{base}.jpg"
        n = 1
        while path.exists():  # several pictures in the same second
            path = self.folder / f"{base}_{n}.jpg"
            n += 1
        return path


# ---------------------------------------------------------------- helpers


def _in_window(t: dtime, start: dtime, end: dtime) -> bool:
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # crosses midnight


def _next_window_start(now: datetime, start: dtime) -> datetime:
    opens = datetime.combine(now.date(), start)
    return opens if opens > now else opens + timedelta(days=1)


def _short_error(e: Exception) -> str:
    if isinstance(e, requests.Timeout):
        return "the camera didn't answer in time"
    if isinstance(e, requests.ConnectionError):
        return "can't reach the camera"
    return str(e)


def _fmt_secs(s: float) -> str:
    if s < 10 and s != int(s):
        return f"{s:.1f}s"
    s = int(round(s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s" if s % 60 else f"{s // 60}m"
    h, rest = divmod(s, 3600)
    return f"{h}h{rest // 60:02d}m" if rest else f"{h}h"


def parse_duration(text: str) -> float:
    """'90', '90s', '30m', '8h', '2d', '1h30m' -> seconds."""
    text = text.strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([smhd])", text)
    if not parts or "".join(n + u for n, u in parts) != text.replace(" ", ""):
        raise argparse.ArgumentTypeError(f"can't read {text!r}; use e.g. 30s, 10m, 8h, 2d")
    mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    return sum(float(n) * mult[u] for n, u in parts)


def parse_window(text: str) -> tuple[dtime, dtime]:
    """'07:00-19:30' -> (time(7, 0), time(19, 30))."""
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*", text)
    if not m:
        raise argparse.ArgumentTypeError(f"can't read time window {text!r}; use e.g. 07:00-19:30")
    h1, m1, h2, m2 = map(int, m.groups())
    try:
        return dtime(h1, m1), dtime(h2, m2)
    except ValueError as e:
        raise argparse.ArgumentTypeError(str(e)) from e


def parse_until(text: str) -> datetime:
    """'18:00' (the next time it's 18:00) or '2026-10-18 18:00'."""
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    try:
        t = datetime.strptime(text, "%H:%M").time()
    except ValueError as e:
        raise argparse.ArgumentTypeError(
            f"can't read {text!r}; use e.g. 18:00 or '2026-10-18 18:00'"
        ) from e
    return _next_window_start(datetime.now(), t)


# ---------------------------------------------------------------- CLI


def main() -> None:
    p = argparse.ArgumentParser(
        description="Take timelapse pictures from a König CMP-NWIPCAM10. "
                    "Runs until Ctrl+C unless --duration, --frames or --until is given."
    )
    p.add_argument("host", help="camera address, e.g. 192.168.1.50:8080")
    p.add_argument("-u", "--user", default="admin")
    p.add_argument("-p", "--password", default="password")
    p.add_argument("-o", "--out", default="timelapse_frames", help="folder for the pictures")
    p.add_argument("-i", "--interval", type=parse_duration, default=10.0,
                   help="time between pictures, e.g. 10, 30s, 5m (default 10s)")
    p.add_argument("-d", "--duration", type=parse_duration, help="how long, e.g. 30m, 8h, 2d")
    p.add_argument("-n", "--frames", type=int, help="stop after this many new pictures")
    p.add_argument("--until", type=parse_until, help="stop at this time, e.g. 18:00")
    p.add_argument("--between", type=parse_window,
                   help="only take pictures in this daily window, e.g. 07:00-19:30")
    p.add_argument("--pan-every", type=int, default=PAN_EVERY_N_PHOTOS, metavar="N",
                   help="pan one step every N pictures (default %(default)s; 0 = off)")
    p.add_argument("--pan-steps", type=int, default=PAN_STEPS, metavar="N",
                   help="steps right before turning back left (default %(default)s)")
    args = p.parse_args()
    if args.pan_every < 0:
        p.error("--pan-every can't be negative")
    if args.pan_steps < 1:
        p.error("--pan-steps must be at least 1")

    try:
        with KonigCamera(args.host, args.user, args.password) as cam:
            cam.detect()
            tl = TimelapseRecorder(
                cam, args.out, interval=args.interval, between=args.between,
                pan_every=args.pan_every, pan_steps=args.pan_steps,
            )

            def on_signal(*_):
                print("\nStopping…")
                tl.stop()
            signal.signal(signal.SIGINT, on_signal)
            if hasattr(signal, "SIGTERM"):
                signal.signal(signal.SIGTERM, on_signal)

            tl.capture(duration=args.duration, frames=args.frames, until=args.until)
    except CameraError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
