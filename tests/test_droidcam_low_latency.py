from __future__ import annotations

import queue
import unittest

import numpy as np

from recorder.camera import _FFmpegDShowCapture


class DroidCamLowLatencyTests(unittest.TestCase):
    def test_read_returns_newest_queued_frame(self):
        capture = _FFmpegDShowCapture.__new__(_FFmpegDShowCapture)
        capture._frames = queue.Queue(maxsize=2)
        older = np.full((2, 2, 3), 1, dtype=np.uint8)
        newest = np.full((2, 2, 3), 2, dtype=np.uint8)
        capture._frames.put_nowait(older)
        capture._frames.put_nowait(newest)

        ok, frame = capture.read(timeout=0.01)

        self.assertTrue(ok)
        self.assertIsNotNone(frame)
        self.assertTrue(np.all(frame == 2))
        self.assertTrue(capture._frames.empty())

    def test_read_empty_queue_times_out_cleanly(self):
        capture = _FFmpegDShowCapture.__new__(_FFmpegDShowCapture)
        capture._frames = queue.Queue(maxsize=2)
        ok, frame = capture.read(timeout=0.01)
        self.assertFalse(ok)
        self.assertIsNone(frame)


if __name__ == '__main__':
    unittest.main()
