from __future__ import annotations

import hashlib
import http.client
import os
import ssl
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np
from types import ModuleType

from recorder.camera import RemoteCamera
from recorder.config import CONFIG_PATH, Settings
from recorder.db import Database
from recorder.security import tls_context
from recorder.server import LocalServer
from recorder.service import RecorderService


class TabletStreamingRegressionTests(unittest.TestCase):
    def test_remote_camera_profile_is_720p30_not_360p10(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings(base_dir=str(Path(td) / "clips"), sessions=[{"id": "s1", "label": "A"}])
            service = RecorderService(cfg, Database(Path(td) / "d.db"))
            try:
                service.connect_remote("s1")
                cam = service.sessions["s1"].camera
                self.assertIsInstance(cam, RemoteCamera)
                self.assertEqual((cam._req_width, cam._req_height, cam._req_fps), (1280, 720, 30))
                self.assertEqual((service.sessions["s1"].width, service.sessions["s1"].height), (1280, 720))
            finally:
                service.shutdown()

    def test_remote_camera_reuses_original_jpeg_instead_of_reencoding_it(self):
        cam = RemoteCamera("remote_tablet", width=1280, height=720, fps=30)
        cam.open()
        try:
            frame = np.zeros((720, 1280, 3), dtype=np.uint8)
            ok, encoded = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 82])
            self.assertTrue(ok)
            payload = encoded.tobytes()
            cam.feed_jpeg(payload)
            frame_id, displayed = cam.preview_jpeg()
            self.assertGreater(frame_id, 0)
            self.assertEqual(displayed, payload)
        finally:
            cam.close()

    def test_browser_tablet_pump_has_no_ten_fps_sleep_and_tracks_real_video_frames(self):
        source = (Path(__file__).parent.parent / "web" / "app.js").read_text("utf-8")
        pump = source[source.index("  function tabletFramePump() {"):source.index("\n  async function toggleTabletCamera()", source.index("  function tabletFramePump() {"))]
        self.assertIn("requestVideoFrameCallback", pump)
        self.assertIn("minFrameIntervalMs = 1000 / 30", pump)
        self.assertIn("const maxWidth = 1280", pump)
        self.assertIn("'image/jpeg', 0.82", pump)
        self.assertNotIn("setTimeout(resolve, 100)", pump)


class YouTubePerformanceRegressionTests(unittest.TestCase):
    def test_youtube_prefers_decode_friendly_720p30_and_caps_output_rate(self):
        captured = {}

        class FakeYoutubeCamera:
            def __init__(self, source, *args, **kwargs):
                captured["camera_kwargs"] = kwargs
                self.source = source
                self.connected = False
                self.source_type = None
                self.name = None
                self.frame_size = (1280, 720)
                self.fps = 30.0
            def open(self):
                self.connected = True
            def close(self):
                self.connected = False

        class FakeYoutubeDL:
            def __init__(self, options):
                captured["options"] = options
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download=False):
                return {"url": "https://stream.example/live.m3u8", "fps": 60.0, "height": 1080}

        yt_dlp = ModuleType("yt_dlp")
        yt_dlp.YoutubeDL = FakeYoutubeDL
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings(base_dir=str(Path(td) / "clips"), sessions=[{"id": "s1", "label": "A"}])
            service = RecorderService(cfg, Database(Path(td) / "d.db"))
            try:
                with mock.patch("recorder.service.YouTubeCamera", FakeYoutubeCamera), \
                     mock.patch.dict("sys.modules", {"yt_dlp": yt_dlp}):
                    camera = service.connect_youtube("s1", "https://www.youtube.com/watch?v=abc123")
                fmt = captured["options"]["format"]
                self.assertIn("height<=720", fmt)
                self.assertIn("fps<=30", fmt)
                self.assertIn("vcodec^=avc1", fmt)
                self.assertEqual(captured["camera_kwargs"]["fps"], 30)
                self.assertEqual(captured["camera_kwargs"]["source_fps"], 60.0)
                self.assertTrue(camera.connected)
            finally:
                service.shutdown()


class LanListenerRegressionTests(unittest.TestCase):
    def test_lan_keeps_local_ui_on_http_and_remote_tablets_on_separate_https_port(self):
        class PassthroughTls:
            @staticmethod
            def wrap_socket(sock, server_side=True):
                return sock

        with tempfile.TemporaryDirectory() as td:
            cfg = Settings(base_dir=str(Path(td) / "clips"), sessions=[{"id": "s1", "label": "A"}])
            service = RecorderService(cfg, Database(Path(td) / "d.db"))
            server = None
            try:
                with mock.patch("recorder.security.tls_context", return_value=PassthroughTls()):
                    server = LocalServer(service, operator_key="OPTEST", viewer_key="VWTEST", lan=True)
                    server.start()
                    self.assertNotEqual(server.port, server.lan_port)
                    self.assertTrue(server.operator_url().startswith(f"http://127.0.0.1:{server.port}/"))
                    urls = server.lan_urls()
                    self.assertTrue(urls)
                    self.assertTrue(all(url.startswith("https://") and f":{server.lan_port}/" in url for url in urls))
                    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=3)
                    try:
                        conn.request("GET", "/?k=OPTEST", headers={"Host": f"127.0.0.1:{server.port}"})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read()
                    finally:
                        conn.close()
            finally:
                if server is not None:
                    server.stop()
                service.shutdown()

    def test_lan_certificate_identity_is_stable_across_starts(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {"HOME": td}), \
                 mock.patch("recorder.security.local_addresses", return_value=["127.0.0.1", "192.168.20.55"]):
                tls_context()
                cert_path = Path(td) / ".handball_recorder" / "lan-cert.pem"
                first = hashlib.sha256(cert_path.read_bytes()).hexdigest()
                tls_context()
                second = hashlib.sha256(cert_path.read_bytes()).hexdigest()
                self.assertEqual(first, second)

    def test_lan_listener_serves_a_real_https_response(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Settings(base_dir=str(Path(td) / "clips"), sessions=[{"id": "s1", "label": "A"}])
            service = RecorderService(cfg, Database(Path(td) / "d.db"))
            server = None
            try:
                with mock.patch.dict(os.environ, {"HOME": td}), \
                     mock.patch("recorder.security.local_addresses", return_value=["127.0.0.1", "192.168.20.55"]):
                    server = LocalServer(service, operator_key="OPTEST", viewer_key="VWTEST", lan=True)
                    server.start()
                    conn = http.client.HTTPSConnection(
                        "127.0.0.1", server.lan_port, timeout=3,
                        context=ssl._create_unverified_context()
                    )
                    try:
                        conn.request("GET", "/?k=OPTEST", headers={"Host": f"192.168.20.55:{server.lan_port}"})
                        response = conn.getresponse()
                        self.assertEqual(response.status, 200)
                        response.read()
                    finally:
                        conn.close()
            finally:
                if server is not None:
                    server.stop()
                service.shutdown()


if __name__ == "__main__":
    unittest.main()
