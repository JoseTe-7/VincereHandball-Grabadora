from __future__ import annotations

import io
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import numpy as np

from recorder.camera import CameraError, YouTubeCamera


class YouTubeLiveTests(unittest.TestCase):
    @staticmethod
    def _payload(width: int = 2, height: int = 2) -> bytes:
        return np.zeros((height, width, 3), dtype=np.uint8).tobytes()

    def test_youtube_source_disables_retrospective_buffer(self):
        camera = YouTubeCamera("https://example.invalid/live", width=2, height=2, fps=30)
        self.assertEqual(camera.source_type, "youtube")
        self.assertFalse(camera.supports_retrospective)
        with self.assertRaisesRegex(CameraError, "retrospectiva.*no está disponible"):
            camera.retrospective_clip(Path("unused.mp4"), 10)

    def test_burst_frames_are_presented_at_the_normal_playout_cadence(self):
        payload = self._payload()
        proc = mock.MagicMock()
        proc.poll.return_value = 0
        camera = YouTubeCamera("https://example.invalid/live", width=2, height=2, fps=30)
        camera._ffmpeg = proc
        camera._running = True
        camera.frame_size = (2, 2)
        camera._reader_done.set()
        camera._live_queue = queue.Queue(maxsize=8)
        for _ in range(3):
            camera._live_queue.put_nowait(payload)

        started = time.monotonic()
        camera._ffmpeg_capture_loop()
        elapsed = time.monotonic() - started
        self.assertEqual(camera.frame_id, 3)
        # Tres frames requieren dos intervalos de 1/30s; se permite margen de scheduling.
        self.assertGreaterEqual(elapsed, 0.045)

    def test_after_a_network_gap_the_clock_does_not_catch_up_at_high_speed(self):
        payload = self._payload()
        camera = YouTubeCamera("https://example.invalid/live", width=2, height=2, fps=30)
        camera._ffmpeg = mock.MagicMock()
        camera._ffmpeg.poll.return_value = None
        camera._running = True
        camera.frame_size = (2, 2)
        camera._reader_done.clear()
        camera._playout_stop.clear()
        camera._live_queue = queue.Queue(maxsize=8)
        published_at: list[float] = []
        original_publish = camera._publish_decoded_frame

        def record_publish(frame: bytes) -> None:
            published_at.append(time.monotonic())
            original_publish(frame)

        camera._publish_decoded_frame = record_publish  # type: ignore[method-assign]
        worker = threading.Thread(target=camera._ffmpeg_capture_loop, daemon=True)
        worker.start()
        for _ in range(3):
            camera._live_queue.put(payload)
        time.sleep(0.28)  # simula una interrupción del flujo/red
        for _ in range(3):
            camera._live_queue.put(payload)
        camera._reader_done.set()
        worker.join(timeout=2.0)

        self.assertFalse(worker.is_alive(), "el hilo de presentación debe terminar al agotarse el flujo")
        self.assertEqual(len(published_at), 6)
        intervals = [b - a for a, b in zip(published_at, published_at[1:])]
        # Al recuperarse no se deben presentar frames en ráfaga para compensar el hueco.
        self.assertTrue(all(interval >= 0.020 for interval in intervals), intervals)
        self.assertGreaterEqual(published_at[-1] - published_at[0], 0.30)

    def test_ffmpeg_uses_normal_demux_buffer_and_clocked_output_queue(self):
        payload = self._payload()
        proc = mock.MagicMock()
        proc.stdout = io.BytesIO(payload)
        proc.poll.return_value = None
        with mock.patch("recorder.camera.shutil.which", return_value="ffmpeg.exe"), \
             mock.patch("recorder.camera.subprocess.Popen", return_value=proc) as popen, \
             mock.patch("recorder.camera.cv2.VideoCapture", side_effect=AssertionError("YouTube must not use OpenCV capture")):
            camera = YouTubeCamera("https://example.invalid/live", width=2, height=2, fps=60, source_fps=60)
            camera.open()
            self.assertIsNotNone(camera.latest_frame())
            args = popen.call_args.args[0]
            self.assertGreaterEqual(popen.call_args.kwargs["bufsize"], 64 * 1024)
            # No usar el filtro realtime: en discontinuidades >2 s reinicia su reloj.
            self.assertNotIn("realtime", " ".join(args))
            self.assertNotIn("nobuffer", args)
            self.assertNotIn("low_delay", args)
            filter_graph = args[args.index("-vf") + 1]
            self.assertIn("setpts=N/(60*TB),fps=60", filter_graph)
            self.assertEqual(camera._live_queue.maxsize, 120)
            camera.close()


    def test_real_ffmpeg_playout_stays_near_30fps_for_a_synthetic_video(self):
        """Integration smoke test: decode real frames and verify wall-clock output cadence."""
        executable = shutil.which("ffmpeg")
        if not executable:
            try:
                import imageio_ffmpeg
                executable = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                executable = None
        if not executable:
            self.skipTest("FFmpeg no está instalado; se mantiene la prueba unitaria del reloj")

        with tempfile.TemporaryDirectory() as temp_dir:
            video_path = Path(temp_dir) / "synthetic_live.mp4"
            generated = subprocess.run(
                [
                    executable, "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "testsrc=size=160x90:rate=30:duration=4",
                    "-c:v", "mpeg4", "-pix_fmt", "yuv420p", "-y", str(video_path),
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                timeout=20,
                check=False,
            )
            if generated.returncode != 0 or not video_path.exists():
                detail = generated.stderr.decode("utf-8", "replace")[-300:]
                self.skipTest(f"Esta compilación de FFmpeg no puede crear el vídeo de prueba: {detail}")

            camera = YouTubeCamera(str(video_path), width=160, height=90, fps=30)
            try:
                camera.open()
                baseline = camera.frame_id
                started = time.monotonic()
                time.sleep(0.6)
                elapsed = time.monotonic() - started
                shown = camera.frame_id - baseline
            finally:
                camera.close()

            observed_fps = shown / max(0.001, elapsed)
            self.assertGreaterEqual(observed_fps, 24.0, f"FPS observado demasiado bajo: {observed_fps:.1f}")
            self.assertLessEqual(observed_fps, 36.0, f"FPS observado demasiado alto: {observed_fps:.1f}")

    def test_missing_ffmpeg_fails_with_actionable_error(self):
        camera = YouTubeCamera("https://example.invalid/live", width=2, height=2)
        with mock.patch("recorder.camera.shutil.which", return_value=None), \
             mock.patch.dict("sys.modules", {"imageio_ffmpeg": None}):
            with self.assertRaisesRegex(CameraError, "FFmpeg"):
                camera.open()


if __name__ == "__main__":
    unittest.main()
