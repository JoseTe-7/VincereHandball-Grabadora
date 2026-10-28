from pathlib import Path
import tempfile
import unittest

from recorder.destinations import default_video_directory, prepare_video_directory


class DestinationTests(unittest.TestCase):
    def test_default_is_inside_user_videos(self):
        path = default_video_directory()
        self.assertEqual(path.name, "Entrenamientos_Extremos")
        self.assertEqual(path.parent.name, "Videos")

    def test_prepare_creates_selected_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "clips" / "partidos"
            result = prepare_video_directory(target)
            self.assertEqual(result, target.resolve())
            self.assertTrue(result.is_dir())

    def test_empty_path_is_rejected(self):
        with self.assertRaises(ValueError):
            prepare_video_directory("  ")


if __name__ == "__main__":
    unittest.main()
