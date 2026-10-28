from __future__ import annotations

import threading
import time
import unittest
import tempfile
from pathlib import Path
from unittest import mock
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np

from recorder.camera import Camera, _DroidCamHttpCapture, _FFmpegMaxQualityMp4Writer
from recorder.config import Settings
from recorder.db import Database
from recorder.service import RecorderService, ServiceError


class _MJPEGHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"
    stop_stream = threading.Event()

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        n = 0
        try:
            while not self.stop_stream.is_set():
                image = np.zeros((120, 160, 3), dtype=np.uint8)
                image[:, :, n % 3] = (n * 31) % 255
                ok, enc = cv2.imencode(".jpg", image)
                if not ok:
                    continue
                payload = enc.tobytes()
                self.wfile.write(
                    b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                    + str(len(payload)).encode("ascii") + b"\r\n\r\n" + payload + b"\r\n"
                )
                self.wfile.flush()
                n += 1
                time.sleep(0.02)
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    def log_message(self, *_args):
        pass


class DroidCamDirectTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _MJPEGHandler.stop_stream.clear()
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _MJPEGHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/video/force/160x120"

    @classmethod
    def tearDownClass(cls):
        _MJPEGHandler.stop_stream.set()
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def test_direct_http_mjpeg_returns_decoded_frames(self):
        cap = _DroidCamHttpCapture(self.url, 160, 120, 30)
        try:
            ok, frame = cap.read(timeout=2.0)
            self.assertTrue(ok)
            self.assertIsNotNone(frame)
            self.assertEqual(tuple(frame.shape), (120, 160, 3))
        finally:
            cap.release()

    def test_direct_http_reader_is_latest_frame_only(self):
        cap = _DroidCamHttpCapture(self.url, 160, 120, 30)
        try:
            time.sleep(0.12)
            self.assertLessEqual(cap._frames.qsize(), 1)
            ok, frame = cap.read(timeout=1.0)
            self.assertTrue(ok)
            self.assertIsNotNone(frame)
        finally:
            cap.release()

    def test_max_quality_writer_creates_readable_mp4(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "quality.mp4"
            writer = _FFmpegMaxQualityMp4Writer(output, 160, 120, 30)
            try:
                for i in range(12):
                    frame = np.full((120, 160, 3), i * 10, dtype=np.uint8)
                    writer.write(frame)
            finally:
                writer.release()
            self.assertTrue(output.exists())
            self.assertGreater(output.stat().st_size, 100)
            cap = cv2.VideoCapture(str(output))
            try:
                ok, frame = cap.read()
                self.assertTrue(ok)
                self.assertEqual(tuple(frame.shape), (120, 160, 3))
            finally:
                cap.release()

    def test_camera_pipeline_reuses_source_jpeg_for_preview(self):
        camera = Camera(self.url, width=160, height=120, fps=30)
        try:
            camera.open()
            deadline = time.monotonic() + 2.0
            frame_id, jpeg = camera.preview_jpeg()
            while jpeg is None and time.monotonic() < deadline:
                time.sleep(0.03)
                frame_id, jpeg = camera.preview_jpeg()
            self.assertGreater(frame_id, 0)
            self.assertIsNotNone(jpeg)
            decoded = cv2.imdecode(np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            self.assertIsNotNone(decoded)
            self.assertEqual(tuple(decoded.shape), (120, 160, 3))
        finally:
            camera.close()


class _FakeDirectCamera:
    def __init__(self, source, width, height, fps, camera_backend="auto", **_kwargs):
        self.source = source
        self.frame_size = (width, height)
        self.fps = float(fps)
        self.connected = False
        self.closed = False

    def open(self):
        self.connected = True

    def close(self):
        self.connected = False
        self.closed = True


class DroidCamDirectServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name)
        settings = Settings(base_dir=str(path / "clips"), sessions=[{"id": "s1", "label": "Pista A"}])
        db = Database(path / "db.sqlite3")
        self.service = RecorderService(settings, db)
        self.addCleanup(self.service.shutdown)

    def test_service_opens_exact_local_ip_mjpeg_url(self):
        with mock.patch("recorder.service.Camera", _FakeDirectCamera):
            camera = self.service.connect_droidcam_direct("s1", "192.168.1.25", 4747, 1280, 720)
        self.assertEqual(camera.source, "http://192.168.1.25:4747/video/force/1280x720")
        self.assertEqual(camera.source_type, "droidcam_direct")
        self.assertTrue(camera.connected)

    def test_service_rejects_public_ip_for_direct_source(self):
        with self.assertRaises(ServiceError):
            self.service.connect_droidcam_direct("s1", "8.8.8.8", 4747, 1280, 720)
        self.assertFalse(self.service.sessions["s1"].connecting)


if __name__ == "__main__":
    unittest.main()
