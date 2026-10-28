from __future__ import annotations

import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from recorder.camera import CAMERA_JPEG_QUALITY, Camera


class WebcamStabilityTests(unittest.TestCase):
    def test_windows_requests_mjpg_before_resolution_and_fps(self):
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        cap = mock.MagicMock()
        cap.isOpened.return_value = True
        cap.read.return_value = (True, frame)
        cap.get.side_effect = lambda prop: 30.0 if prop == cv2.CAP_PROP_FPS else 0
        set_calls = []

        def remember_set(prop, value):
            set_calls.append((prop, value))
            return True

        cap.set.side_effect = remember_set
        camera = Camera(0, width=1280, height=720, fps=30)
        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera.cv2.VideoCapture", return_value=cap) as factory, \
                mock.patch.object(Camera, "_start_buffer_worker"), \
                mock.patch("recorder.camera.threading.Thread"):
            camera.open()
            camera.close()

        factory.assert_called_once_with(0, cv2.CAP_DSHOW)
        self.assertGreaterEqual(len(set_calls), 4)
        self.assertEqual(set_calls[0][0], cv2.CAP_PROP_FOURCC)
        self.assertEqual(set_calls[0][1], cv2.VideoWriter_fourcc(*"MJPG"))
        self.assertEqual(set_calls[1], (cv2.CAP_PROP_FRAME_WIDTH, 1280))
        self.assertEqual(set_calls[2], (cv2.CAP_PROP_FRAME_HEIGHT, 720))
        self.assertEqual(set_calls[3], (cv2.CAP_PROP_FPS, 30))

    def test_preview_and_retrospective_reuse_the_same_encoded_jpeg(self):
        camera = Camera(0, width=32, height=24, fps=30)
        camera._buffer_stop.clear()
        worker = threading.Thread(target=camera._buffer_loop, daemon=True)
        camera._buffer_thread = worker
        frame = np.full((24, 32, 3), 127, dtype=np.uint8)
        captured_at = time.monotonic()

        with mock.patch("recorder.camera.cv2.imencode", wraps=cv2.imencode) as encoder:
            worker.start()
            camera._buffer_frame(frame, frame_id=9, captured_at=captured_at)
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                with camera._jpeg_lock:
                    if camera._jpeg_cache[0] == 9:
                        break
                time.sleep(0.005)
            frame_id, preview_payload = camera.preview_jpeg()
            with camera._ring_lock:
                self.assertEqual(len(camera._ring), 1)
                ring_timestamp, retrospective_payload = camera._ring[0]
            self.assertEqual(frame_id, 9)
            self.assertIsNotNone(preview_payload)
            self.assertEqual(preview_payload, retrospective_payload)
            self.assertEqual(ring_timestamp, captured_at)
            self.assertEqual(encoder.call_count, 1)

        camera._buffer_stop.set()
        worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_mode_probe_rejects_slow_mjpg_and_tries_yuy2(self):
        frame = np.zeros((360, 640, 3), dtype=np.uint8)
        created = []

        class TimedCapture:
            def __init__(self, delay: float):
                self.delay = delay
                self.fourcc = 0
                self.calls = []
                self.released = False

            def isOpened(self):
                return True

            def set(self, prop, value):
                self.calls.append((prop, value))
                if prop == cv2.CAP_PROP_FOURCC:
                    self.fourcc = int(value)
                return True

            def get(self, prop):
                if prop == cv2.CAP_PROP_FPS:
                    return 30.0
                if prop == cv2.CAP_PROP_FOURCC:
                    return float(self.fourcc)
                return 0.0

            def read(self):
                time.sleep(self.delay)
                return True, frame

            def release(self):
                self.released = True

        def factory(index, backend):
            # MJPG simulate 15 FPS, YUY2 simulate the requested 30 FPS.
            delay = (1 / 15) if not created else (1 / 30)
            cap = TimedCapture(delay)
            created.append(cap)
            return cap

        camera = Camera(0, width=1280, height=720, fps=30)
        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera.cv2.VideoCapture", side_effect=factory), \
                mock.patch.object(Camera, "_start_buffer_worker"), \
                mock.patch("recorder.camera.threading.Thread"):
            camera.open()
            camera.close()

        self.assertEqual(len(created), 2)
        self.assertTrue(created[0].released)
        self.assertEqual(camera._capture_details["fourcc"], "YUY2")
        self.assertGreaterEqual(camera._capture_details["probe_fps"], 24.0)

    def test_fps_measurement_tracks_received_frames_in_a_short_window(self):
        camera = Camera(0, fps=30)
        camera._fps_timestamps.clear()
        # 20 samples delivered every 50ms represent an actual 20 FPS stream.
        for i in range(20):
            camera._update_fps_measurement(i * 0.05)
        self.assertEqual(round(camera.fps), 20)

    def test_preview_fps_is_not_artificially_capped_to_30(self):
        camera = Camera(0, fps=60)
        camera._measured_fps = 59.5
        self.assertEqual(round(camera.fps), 60)

    def test_fps_display_does_not_fake_a_minimum_of_five(self):
        camera = Camera(0, fps=30)
        camera._fps_timestamps.clear()
        for i in range(5):
            camera._update_fps_measurement(i * 0.25)
        self.assertEqual(round(camera.fps), 4)


if __name__ == "__main__":
    unittest.main()
