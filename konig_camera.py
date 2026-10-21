"""
Control a König CMP-NWIPCAM10 (Aviosys IP Kamera 9060A) from Python.

Needs:  pip install requests
Optional, for numpy frames and .mp4/.avi recording:  pip install opencv-python

Quick use:
    from konig_camera import KonigCamera

    with KonigCamera("192.168.1.50:8080", "admin", "password") as cam:
        cam.save_snapshot("now.jpg")
        cam.record("clip.mp4", seconds=10)
        for jpeg in cam.frames(max_frames=50):
            ...  # raw JPEG bytes, one per frame

Or from the command line:
    python konig_camera.py 192.168.1.50:8080 --snapshot now.jpg
    python konig_camera.py 192.168.1.50:8080 --record clip.mp4 --seconds 10
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

import requests
from requests.auth import HTTPBasicAuth, HTTPDigestAuth

SOI = b"\xff\xd8"  # JPEG start marker
EOI = b"\xff\xd9"  # JPEG end marker


class CameraError(Exception):
    """Raised when the camera can't be reached or gives no picture."""


class CameraAuthError(CameraError):
    """Raised when the camera rejects the username or password."""


@dataclass
class Source:
    kind: str  # "mjpeg" (continuous stream) or "jpeg" (single image)
    path: str


class KonigCamera:
    # Continuous MJPEG streams, tried first.
    STREAM_PATHS = [
        "/GetData.cgi?Status=false",
        "/GetData.cgi",
        "/GetData.cgi?Status=0",
        "/GetData.cgi?CH=1",
        "/videostream.cgi?user={user}&pwd={pwd}",
    ]
    # Single JPEG images, used for snapshots and as a fallback for video.
    SNAPSHOT_PATHS = [
        "/Jpeg/CamImg.jpg",
        "/snapshot.jpg?account={user}&password={pwd}",
        "/snapshot.cgi?user={user}&pwd={pwd}",
        "/image.jpg",
    ]

    def __init__(
        self,
        host: str,
        user: str = "admin",
        password: str = "password",
        timeout: float = 5.0,
    ):
        """
        host: camera address, e.g. "192.168.1.50" or "192.168.1.50:8080".
        """
        host = host.strip()
        for prefix in ("http://", "https://"):
            if host.startswith(prefix):
                host = host[len(prefix):]
        self.host = host.rstrip("/")
        self.user = user
        self.password = password
        self.timeout = timeout

        self.session = requests.Session()
        self.session.auth = HTTPBasicAuth(user, password)

        self.stream_source: Optional[Source] = None
        self.snapshot_source: Optional[Source] = None
        self._detected = False

    # ------------------------------------------------------------ basics

    def url(self, path: str) -> str:
        path = path.replace("{user}", requests.utils.quote(self.user)).replace(
            "{pwd}", requests.utils.quote(self.password)
        )
        return f"http://{self.host}{path}"

    def _get(self, path: str, stream: bool = False) -> requests.Response:
        r = self.session.get(self.url(path), timeout=self.timeout, stream=stream)
        # Some firmware wants Digest instead of Basic login: switch once and retry.
        if r.status_code == 401 and "digest" in r.headers.get("WWW-Authenticate", "").lower():
            r.close()
            self.session.auth = HTTPDigestAuth(self.user, self.password)
            r = self.session.get(self.url(path), timeout=self.timeout, stream=stream)
        if r.status_code == 401:
            r.close()
            raise CameraAuthError("Camera refused the username or password.")
        return r

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> "KonigCamera":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"KonigCamera({self.host!r}, stream={self.stream_source}, snapshot={self.snapshot_source})"

    # ------------------------------------------------------------ detection

    def detect(self) -> "KonigCamera":
        """Find which stream and snapshot addresses this camera answers on."""
        if self._detected:
            return self
        tried = []

        for path in self.SNAPSHOT_PATHS:
            tried.append(self.url(path))
            try:
                if _looks_like_jpeg(self._fetch_jpeg(path)):
                    self.snapshot_source = Source("jpeg", path)
                    break
            except CameraAuthError:
                raise
            except (CameraError, requests.RequestException):
                pass

        for path in self.STREAM_PATHS:
            tried.append(self.url(path))
            try:
                frame = next(self._mjpeg_frames(path), None)
                if frame and _looks_like_jpeg(frame):
                    self.stream_source = Source("mjpeg", path)
                    break
            except CameraAuthError:
                raise
            except (CameraError, requests.RequestException):
                pass

        if not (self.stream_source or self.snapshot_source):
            raise CameraError(
                f"No picture from {self.host}. Check the address and port. Tried:\n  "
                + "\n  ".join(tried)
            )
        self._detected = True
        return self

    # ------------------------------------------------------------ images

    def _fetch_jpeg(self, path: str) -> bytes:
        r = self._get(path)
        try:
            if r.status_code != 200:
                raise CameraError(f"HTTP {r.status_code} from {path}")
            return r.content
        finally:
            r.close()

    def snapshot(self) -> bytes:
        """Return one picture from the camera as JPEG bytes."""
        self.detect()
        if self.snapshot_source:
            return self._fetch_jpeg(self.snapshot_source.path)
        frame = next(self._mjpeg_frames(self.stream_source.path), None)
        if not frame:
            raise CameraError("Stream ended before a frame arrived.")
        return frame

    def command(self, path: str) -> None:
        """
        Call a control address on the camera, e.g. "/motor_control_right".
        Raises CameraError if the camera doesn't accept it.
        """
        if not path.startswith("/"):
            path = "/" + path
        r = self._get(path)
        r.close()
        if r.status_code in (404, 405, 501):
            # Some firmware only accepts these as a form post.
            r = self.session.post(self.url(path), timeout=self.timeout)
            r.close()
        if r.status_code == 401:
            raise CameraAuthError("Camera refused the username or password.")
        if r.status_code >= 400:
            raise CameraError(f"HTTP {r.status_code} from {path}")

    def pan_right(self) -> None:
        self.command("/motor_control_right")

    def pan_left(self) -> None:
        self.command("/motor_control_left")

    def save_snapshot(self, filename: str | Path) -> Path:
        """Save one picture to a .jpg file and return its path."""
        path = Path(filename)
        path.write_bytes(self.snapshot())
        return path

    def snapshot_array(self):
        """Return one picture as a numpy BGR array (needs opencv-python)."""
        return _decode(self.snapshot())

    # ------------------------------------------------------------ video

    def frames(
        self,
        max_frames: Optional[int] = None,
        seconds: Optional[float] = None,
        fps: float = 10.0,
    ) -> Iterator[bytes]:
        """
        Yield frames as JPEG bytes until max_frames or seconds is reached
        (or forever if neither is given). Uses the MJPEG stream when the
        camera has one, otherwise fetches single images at `fps`.
        """
        self.detect()
        end = time.monotonic() + seconds if seconds else None
        count = 0

        if self.stream_source:
            source = self._mjpeg_frames(self.stream_source.path)
        else:
            source = self._polled_frames(fps)

        try:
            for frame in source:
                yield frame
                count += 1
                if max_frames and count >= max_frames:
                    return
                if end and time.monotonic() >= end:
                    return
        finally:
            source.close()

    def frame_arrays(self, **kwargs):
        """Like frames(), but yields numpy BGR arrays (needs opencv-python)."""
        for jpeg in self.frames(**kwargs):
            yield _decode(jpeg)

    def _polled_frames(self, fps: float) -> Iterator[bytes]:
        interval = 1.0 / fps
        while True:
            started = time.monotonic()
            yield self._fetch_jpeg(self.snapshot_source.path)
            time.sleep(max(0.0, interval - (time.monotonic() - started)))

    def _mjpeg_frames(self, path: str) -> Iterator[bytes]:
        """Split a multipart MJPEG stream into individual JPEG frames."""
        r = self._get(path, stream=True)
        try:
            if r.status_code != 200:
                raise CameraError(f"HTTP {r.status_code} from {path}")
            ctype = r.headers.get("Content-Type", "").lower()
            if "multipart" not in ctype and "jpeg" not in ctype and "mjpeg" not in ctype:
                raise CameraError(f"{path} is not a video stream ({ctype or 'no type'})")

            buf = b""
            for chunk in r.iter_content(chunk_size=8192):
                buf += chunk
                while True:
                    start = buf.find(SOI)
                    if start < 0:
                        buf = buf[-1:]  # keep a possible half marker
                        break
                    end = buf.find(EOI, start + 2)
                    if end < 0:
                        buf = buf[start:]
                        break
                    yield buf[start:end + 2]
                    buf = buf[end + 2:]
        finally:
            r.close()

    def record(
        self,
        filename: str | Path,
        seconds: float = 10.0,
        fps: float = 10.0,
    ) -> Path:
        """
        Record video for `seconds`.

        .mp4 / .avi  -> encoded with OpenCV (needs opencv-python)
        .mjpeg / .mjpg -> raw MJPEG, no extra packages; plays in VLC or ffmpeg
        """
        path = Path(filename)
        suffix = path.suffix.lower()

        if suffix in (".mjpeg", ".mjpg"):
            with path.open("wb") as f:
                for jpeg in self.frames(seconds=seconds, fps=fps):
                    f.write(jpeg)
            return path

        cv2 = _cv2()
        fourcc = cv2.VideoWriter_fourcc(*("mp4v" if suffix == ".mp4" else "MJPG"))
        writer = None
        started = time.monotonic()
        count = 0
        try:
            for jpeg in self.frames(seconds=seconds, fps=fps):
                img = _decode(jpeg)
                if img is None:
                    continue
                if writer is None:
                    h, w = img.shape[:2]
                    writer = cv2.VideoWriter(str(path), fourcc, fps, (w, h))
                    if not writer.isOpened():
                        raise CameraError(f"OpenCV can't write {path.name}; try .avi or .mjpeg")
                writer.write(img)
                count += 1
        finally:
            if writer is not None:
                writer.release()

        # The camera may deliver a different frame rate than asked for;
        # rewrite the header rate so playback speed matches real time.
        elapsed = time.monotonic() - started
        if writer is not None and count > 1 and elapsed > 0:
            real_fps = count / elapsed
            if abs(real_fps - fps) / fps > 0.2:
                _retime(path, fourcc, real_fps)
        return path


# ---------------------------------------------------------------- helpers


def _looks_like_jpeg(data: bytes) -> bool:
    return len(data) > 100 and data.startswith(SOI)


def _cv2():
    try:
        import cv2  # noqa: WPS433
    except ImportError as e:
        raise CameraError(
            "This needs OpenCV: pip install opencv-python  "
            "(or record to a .mjpeg file, which needs nothing extra)"
        ) from e
    return cv2


def _decode(jpeg: bytes):
    cv2 = _cv2()
    import numpy as np

    return cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)


def _retime(path: Path, fourcc: int, fps: float) -> None:
    cv2 = _cv2()
    tmp = path.with_name(path.stem + ".retime" + path.suffix)
    cap = cv2.VideoCapture(str(path))
    writer = None
    try:
        while True:
            ok, img = cap.read()
            if not ok:
                break
            if writer is None:
                h, w = img.shape[:2]
                writer = cv2.VideoWriter(str(tmp), fourcc, fps, (w, h))
            writer.write(img)
    finally:
        cap.release()
        if writer is not None:
            writer.release()
    if tmp.exists() and tmp.stat().st_size > 0:
        tmp.replace(path)


# ---------------------------------------------------------------- CLI


def main() -> None:
    p = argparse.ArgumentParser(description="König CMP-NWIPCAM10 camera tool")
    p.add_argument("host", help="camera address, e.g. 192.168.1.50:8080")
    p.add_argument("-u", "--user", default="admin")
    p.add_argument("-p", "--password", default="password")
    p.add_argument("--snapshot", metavar="FILE.jpg", help="save one picture")
    p.add_argument("--record", metavar="FILE", help="record video (.mp4, .avi or .mjpeg)")
    p.add_argument("--seconds", type=float, default=10.0, help="recording length")
    p.add_argument("--fps", type=float, default=10.0)
    args = p.parse_args()

    with KonigCamera(args.host, args.user, args.password) as cam:
        cam.detect()
        print(f"Connected to {cam.host}")
        print(f"  stream:   {cam.stream_source.path if cam.stream_source else 'none'}")
        print(f"  snapshot: {cam.snapshot_source.path if cam.snapshot_source else 'none'}")
        if args.snapshot:
            print("Saved", cam.save_snapshot(args.snapshot))
        if args.record:
            print(f"Recording {args.seconds:g}s…")
            print("Saved", cam.record(args.record, seconds=args.seconds, fps=args.fps))


if __name__ == "__main__":
    main()
