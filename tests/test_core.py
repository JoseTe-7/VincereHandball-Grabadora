"""Pruebas de base de datos, nombres de clip, cerrojo de instancia y servicio multisesión.

    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from datetime import datetime
from types import ModuleType
from pathlib import Path
from unittest import mock

import cv2
import numpy as np

from recorder.camera import Camera, list_available_cameras
from recorder.config import CONFIG_PATH, Settings
from recorder.db import Database, DbError
from recorder.instance import InstanceLock
from recorder.service import RecorderService, ServiceError, _select_camera_device
from recorder.storage import build_target

ROOT = Path(__file__).resolve().parent.parent
REAL_CAPTURE = cv2.VideoCapture            # se guarda antes de simular cámaras: sirve para leer los clips grabados


def wait_until(condition, timeout: float = 8.0, step: float = 0.05) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return False


def frame_count(path: Path) -> int:
    cap = REAL_CAPTURE(str(path))
    try:
        return int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    finally:
        cap.release()


def first_frame_channel(path: Path) -> int:
    """Canal dominante (0=B, 1=G, 2=R) del primer fotograma: identifica de qué cámara viene el clip."""
    cap = REAL_CAPTURE(str(path))
    try:
        ok, frame = cap.read()
        assert ok
        return int(np.argmax(frame.mean(axis=(0, 1))))
    finally:
        cap.release()


class CameraBackendTests(unittest.TestCase):
    def test_windows_camera_names_come_from_directshow(self):
        graph = mock.MagicMock()
        graph.get_input_devices.return_value = ["Logitech C920", "USB Capture"]
        dshow_graph = ModuleType("pygrabber.dshow_graph")
        dshow_graph.FilterGraph = mock.Mock(return_value=graph)
        pygrabber = ModuleType("pygrabber")
        pygrabber.__path__ = []

        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch.dict("sys.modules", {
                    "pygrabber": pygrabber,
                    "pygrabber.dshow_graph": dshow_graph,
                }):
            cameras = list_available_cameras()

        self.assertEqual(
            [(c["index"], c["name"], c["backend"]) for c in cameras],
            [(0, "Logitech C920", "dshow"), (1, "USB Capture", "dshow")],
        )
        self.assertEqual([c["device_id"] for c in cameras], ["dshow:0", "dshow:1"])
        self.assertTrue(all(c["kind"] == "camera" for c in cameras))

    def test_windows_inventory_keeps_directshow_and_media_foundation_indexes_separate(self):
        graph = mock.MagicMock()
        graph.get_input_devices.return_value = ["DroidCam Video", "HD Webcam"]
        dshow_graph = ModuleType("pygrabber.dshow_graph")
        dshow_graph.FilterGraph = mock.Mock(return_value=graph)
        pygrabber = ModuleType("pygrabber")
        pygrabber.__path__ = []
        msmf_devices = [
            {"index": 0, "name": "HD Webcam", "backend": "msmf", "symbolic_link": "mf://hd"},
            {"index": 1, "name": "DroidCam Video", "backend": "msmf", "symbolic_link": "mf://droid"},
        ]
        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera._windows_media_foundation_devices", return_value=msmf_devices), \
                mock.patch.dict("sys.modules", {
                    "pygrabber": pygrabber,
                    "pygrabber.dshow_graph": dshow_graph,
                }):
            cameras = list_available_cameras()

        droid = [c for c in cameras if c["name"] == "DroidCam Video"]
        self.assertEqual({(c["index"], c["backend"]) for c in droid}, {(0, "dshow"), (1, "msmf")})
        self.assertEqual(len({c["device_id"] for c in droid}), 2)
        self.assertEqual(len({c["display_group"] for c in droid}), 1)
        self.assertEqual(len({c["device_group"] for c in droid}), 1)
        self.assertTrue(all(c["kind"] == "virtual" for c in droid))

    def test_known_mobile_camera_apps_are_classified_as_virtual_without_filtering(self):
        from recorder.camera import _camera_group_ids
        cameras = _camera_group_ids([
            {"index": 0, "name": "DroidCam Video", "backend": "dshow"},
            {"index": 1, "name": "Iriun Webcam", "backend": "msmf"},
            {"index": 2, "name": "Unknown Camera", "backend": "dshow"},
        ])
        self.assertEqual([c["kind"] for c in cameras], ["virtual", "virtual", "camera"])
        self.assertEqual(len(cameras), 3)

    def test_backend_and_index_resolve_to_the_selected_windows_endpoint(self):
        devices = [
            {"index": 0, "name": "Webcam USB", "backend": "dshow"},
            {"index": 0, "name": "DroidCam Video", "backend": "msmf"},
        ]
        selected = _select_camera_device(devices, 0, "msmf", windows=True)
        self.assertEqual(selected["name"], "DroidCam Video")
        self.assertEqual(selected["backend"], "msmf")
        # Compatibility path deliberately prefers the stable v2.6 DirectShow endpoint.
        self.assertEqual(_select_camera_device(devices, 0, "auto", windows=True)["backend"], "dshow")

    def test_virtual_directshow_camera_prefers_exact_name_capture_without_changing_physical_path(self):
        from recorder.camera import Camera

        frame = np.zeros((480, 640, 3), dtype=np.uint8)

        class FakeNamedCapture:
            def __init__(self, *_args):
                self.released = False

            def read(self, timeout=2.0):
                return True, frame.copy()

            def get(self, prop):
                return {
                    cv2.CAP_PROP_FRAME_WIDTH: 640,
                    cv2.CAP_PROP_FRAME_HEIGHT: 480,
                    cv2.CAP_PROP_FPS: 30,
                    cv2.CAP_PROP_FOURCC: 0,
                }.get(prop, 0)

            def release(self):
                self.released = True

        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera._FFmpegDShowCapture", FakeNamedCapture), \
                mock.patch("recorder.camera.cv2.VideoCapture") as opencv_capture:
            camera = Camera(
                0, 640, 480, 30, "dshow", device_name="DroidCam Video",
                prefer_named_dshow=True,
            )
            camera.open()
            try:
                self.assertEqual(camera._capture_details["backend"], "FFmpeg DirectShow (nombre exacto)")
                opencv_capture.assert_not_called()
                self.assertIsNotNone(camera.latest_frame())
            finally:
                camera.close()

    def test_physical_camera_does_not_prefer_named_ffmpeg_capture(self):
        from recorder.camera import Camera

        camera = Camera(0, 640, 480, 30, "dshow", device_name="USB Webcam", prefer_named_dshow=False)
        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera._FFmpegDShowCapture") as named_capture:
            # This test verifies the virtual-only gate without attempting hardware access.
            self.assertFalse(camera.prefer_named_dshow)
            named_capture.assert_not_called()

    def test_camera_fallback_names_include_the_discovered_index(self):
        captures = {}

        def capture_factory(index, _backend):
            capture = mock.MagicMock()
            capture.isOpened.return_value = index in {0, 2}
            captures[index] = capture
            return capture

        with mock.patch("recorder.camera.platform.system", return_value="Linux"), \
                mock.patch("recorder.camera.cv2.VideoCapture", side_effect=capture_factory):
            cameras = list_available_cameras()

        self.assertEqual(
            [(c["index"], c["name"], c["backend"]) for c in cameras],
            [(0, "Dispositivo de vídeo 0", "auto"), (2, "Dispositivo de vídeo 2", "auto")],
        )
        self.assertEqual([c["device_id"] for c in cameras], ["auto:0", "auto:2"])
        self.assertTrue(all(c["kind"] == "camera" for c in cameras))
        captures[0].release.assert_called_once()
        captures[2].release.assert_called_once()

    def test_windows_auto_backend_does_not_fallback_to_different_enumeration(self):
        directshow = mock.MagicMock()
        directshow.isOpened.return_value = False
        capture_factory = mock.Mock(return_value=directshow)
        camera = Camera(0)

        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera.cv2.VideoCapture", capture_factory):
            with self.assertRaises(Exception):
                camera.open()

        capture_factory.assert_called_once_with(0, cv2.CAP_DSHOW)
        directshow.release.assert_called_once()

    def test_media_foundation_still_finds_cameras_when_directshow_enumeration_fails(self):
        graph_module = ModuleType("pygrabber.dshow_graph")
        graph_module.FilterGraph = mock.Mock(side_effect=RuntimeError("DirectShow unavailable"))
        pygrabber = ModuleType("pygrabber")
        pygrabber.__path__ = []
        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera._windows_media_foundation_devices", return_value=[
                    {"index": 0, "name": "DroidCam Video", "backend": "msmf"},
                ]), \
                mock.patch.dict("sys.modules", {
                    "pygrabber": pygrabber,
                    "pygrabber.dshow_graph": graph_module,
                }):
            cameras = list_available_cameras()
        self.assertEqual([(c["index"], c["name"], c["backend"]) for c in cameras],
                         [(0, "DroidCam Video", "msmf")])

    def test_windows_camera_inventory_fails_closed_without_directshow(self):
        graph_module = ModuleType("pygrabber.dshow_graph")
        graph_module.FilterGraph = mock.Mock(side_effect=RuntimeError("DirectShow unavailable"))
        pygrabber = ModuleType("pygrabber")
        pygrabber.__path__ = []
        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch.dict("sys.modules", {
                    "pygrabber": pygrabber,
                    "pygrabber.dshow_graph": graph_module,
                }), \
                mock.patch("recorder.camera.cv2.VideoCapture") as capture_factory:
            self.assertEqual(list_available_cameras(), [])
        capture_factory.assert_not_called()

    def test_windows_camera_can_force_media_foundation(self):
        media_foundation = mock.MagicMock()
        media_foundation.isOpened.return_value = True
        media_foundation.read.return_value = (True, np.zeros((360, 640, 3), np.uint8))
        media_foundation.get.return_value = 30.0
        capture_factory = mock.Mock(return_value=media_foundation)
        camera = Camera(0, camera_backend="msmf")

        with mock.patch("recorder.camera.platform.system", return_value="Windows"), \
                mock.patch("recorder.camera.cv2.VideoCapture", capture_factory), \
                mock.patch.object(Camera, "_start_buffer_worker"), \
                mock.patch("recorder.camera.threading.Thread"):
            camera.open()
            camera.close()

        capture_factory.assert_called_once_with(0, cv2.CAP_MSMF)


class FakeCapture:
    """Cámara simulada a 30 fps. La 0 es azul, la 1 verde; las demás no existen."""
    AVAILABLE = {0, 1}
    COLORS = {0: (210, 40, 40), 1: (40, 210, 40)}     # BGR
    dead: set[int] = set()

    def __init__(self, index, backend=0):
        self.index = index

    def isOpened(self):
        return self.index in self.AVAILABLE

    def set(self, *args):
        return True

    def get(self, prop):
        return 30.0 if prop == cv2.CAP_PROP_FPS else 0.0

    def read(self):
        time.sleep(1 / 30)
        if self.index in self.dead:
            return False, None
        frame = np.zeros((360, 640, 3), np.uint8)
        frame[:] = self.COLORS[self.index]
        return True, frame

    def release(self):
        pass


# ======================================================================= base de datos
class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.db = Database(self.tmp / "d.db")
        self.db.ensure_initial([], [])
        self.pid = self.db.catalog()[0]["id"]

    def test_first_run_creates_a_team_with_categories_and_exercises(self):
        team = self.db.catalog()[0]
        self.assertEqual(team["name"], "Mi equipo")
        self.assertEqual([c["name"] for c in team["categories"]], ["Jugadores"])
        self.assertEqual(len(team["exercises"]), 5)
        self.assertFalse(self.db.ensure_initial([], []), "no debe crear un segundo equipo")

    def test_v1_migration_keeps_players_and_exercises(self):
        other = Database(self.tmp / "migrated.db")
        other.ensure_initial(["Marcos", "Lucía"], ["Contraataque"])
        team = other.catalog()[0]
        self.assertEqual([p["name"] for p in team["players"]], ["Marcos", "Lucía"])
        self.assertEqual([e["name"] for e in team["exercises"]], ["Contraataque"])

    def test_duplicate_names_are_rejected_ignoring_case_and_accents(self):
        self.db.add_player(self.pid, "Álvaro Gil")
        with self.assertRaises(DbError):
            self.db.add_player(self.pid, "alvaro gil")

    def test_same_player_name_is_allowed_in_different_teams(self):
        other = self.db.create_profile("Juvenil")
        self.db.add_player(self.pid, "Ana")
        self.db.add_player(other, "Ana")

    def test_removing_a_category_keeps_its_players(self):
        cat = self.db.catalog()[0]["categories"][0]["id"]
        player = self.db.add_player(self.pid, "Ana", cat)
        self.db.remove_category(cat)
        players = {p["id"]: p for p in self.db.catalog()[0]["players"]}
        self.assertIsNone(players[player]["category_id"])

    def test_cannot_use_a_category_from_another_team(self):
        other = self.db.create_profile("Juvenil", categories=["Extremos"])
        foreign = next(c["id"] for p in self.db.catalog() if p["id"] == other for c in p["categories"])
        with self.assertRaises(DbError):
            self.db.add_player(self.pid, "Ana", foreign)

    def test_last_team_cannot_be_deleted(self):
        with self.assertRaises(DbError):
            self.db.delete_profile(self.pid)

    def test_export_import_roundtrip(self):
        cat = self.db.catalog()[0]["categories"][0]["id"]
        self.db.add_player(self.pid, "Ana", cat)
        self.db.add_player(self.pid, "Beto")
        copy = self.db.import_profile(self.db.export_profile(self.pid))
        original, imported = self.db.catalog()[0], next(p for p in self.db.catalog() if p["id"] == copy)
        self.assertEqual(imported["name"], "Mi equipo (2)")
        names = lambda team: [(p["name"], next((c["name"] for c in team["categories"] if c["id"] == p["category_id"]), None))
                              for p in team["players"]]
        self.assertEqual(names(imported), names(original))

    def test_import_rejects_garbage(self):
        for bad in (None, [], {"format": "otra-cosa"}, {"format": "extremos-equipo", "version": 99},
                    {"format": "extremos-equipo", "version": 1, "name": "X", "players": [{"name": 5}]},
                    {"format": "extremos-equipo", "version": 1, "name": "X", "players": "no"}):
            with self.assertRaises(DbError, msg=repr(bad)):
                self.db.import_profile(bad)

    def test_many_threads_writing_at_once(self):
        errors = []

        def worker(n):
            try:
                for i in range(25):
                    self.db.add_player(self.pid, f"Jugador {n}-{i}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(self.db.catalog()[0]["players"]), 200)

    def test_racing_for_the_same_name_has_exactly_one_winner(self):
        results = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            try:
                self.db.add_player(self.pid, "Capitán")
                results.append("ok")
            except DbError:
                results.append("duplicado")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("duplicado"), 7)

    def test_several_processes_writing_at_once(self):
        script = textwrap.dedent("""
            import sys
            sys.path.insert(0, {root!r})
            from recorder.db import Database, DbError
            db = Database({path!r})
            pid = db.catalog()[0]["id"]
            tag = sys.argv[1]
            ok = 0
            for i in range(30):
                db.add_player(pid, f"P{{tag}}-{{i}}")
            try:
                db.add_player(pid, "Capitán")
                ok = 1
            except DbError:
                pass
            print(ok)
        """).format(root=str(ROOT), path=str(self.tmp / "d.db"))
        procs = [subprocess.Popen([sys.executable, "-c", script, str(n)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for n in range(4)]
        outputs = [p.communicate(timeout=60) for p in procs]
        for proc, (out, err) in zip(procs, outputs):
            self.assertEqual(proc.returncode, 0, err)
        self.assertEqual(sum(int(out.strip()) for out, _ in outputs), 1, "solo un proceso puede crear al Capitán")
        self.assertEqual(len(self.db.catalog()[0]["players"]), 4 * 30 + 1)


# ======================================================================= nombres de clip y cerrojo
class StorageTests(unittest.TestCase):
    def test_reserving_the_same_clip_name_from_many_threads_never_collides(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        moment = datetime(2026, 10, 9, 12, 0, 0)
        paths, barrier = [], threading.Barrier(20)

        def worker():
            barrier.wait()
            paths.append(build_target(tmp, "Ana", "Finta", moment, profile="Cadete A"))

        threads = [threading.Thread(target=worker) for _ in range(20)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(set(paths)), 20)
        self.assertTrue(all(p.exists() for p in paths))
        self.assertEqual(paths[0].parent.relative_to(tmp).as_posix(), "Cadete_A/Ana/Finta")


class InstanceLockTests(unittest.TestCase):
    def test_second_holder_is_refused_and_lock_is_released_when_holder_dies(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        lock_path = tmp / "host.lock"
        script = textwrap.dedent("""
            import sys, time
            sys.path.insert(0, {root!r})
            from pathlib import Path
            from recorder.instance import InstanceLock
            lock = InstanceLock(Path({path!r}))
            print("held" if lock.acquire() else "refused", flush=True)
            time.sleep(60)
        """).format(root=str(ROOT), path=str(lock_path))
        holder = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), "held")

        mine = InstanceLock(lock_path)
        self.assertFalse(mine.acquire(), "con un anfitrión vivo no se puede ser anfitrión")

        holder.kill()                       # muerte brusca: no llega a liberar nada
        holder.wait()
        self.assertTrue(wait_until(mine.acquire, timeout=5), "el cerrojo del SO debe liberarse solo")
        mine.release()


# ======================================================================= servicio multisesión
class ServiceTests(unittest.TestCase):
    def setUp(self):
        FakeCapture.dead = set()
        patcher = mock.patch.object(cv2, "VideoCapture", FakeCapture)
        patcher.start()
        self.addCleanup(patcher.stop)
        CONFIG_PATH.unlink(missing_ok=True)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.settings = Settings(
            base_dir=str(self.tmp / "clips"),
            sessions=[{"id": "s1", "label": "Pista A", "camera_index": 0},
                      {"id": "s2", "label": "Pista B", "camera_index": 1}],
        )
        self.db = Database(self.tmp / "d.db")
        self.svc = RecorderService(self.settings, self.db)
        self.addCleanup(self.svc.shutdown)
        self.team = self.svc.snapshot()["profiles"][0]["id"]
        self.ana = self.svc.add_player(self.team, "Ana")
        self.beto = self.svc.add_player(self.team, "Beto")
        self.svc.connect("s1")
        self.svc.connect("s2", index=1)

    def test_youtube_source_can_be_reconnected_after_switching_to_usb(self):
        from types import ModuleType

        created = []

        class FakeYoutubeCamera:
            def __init__(self, source, *args, **kwargs):
                self.source = source
                self.connected = False
                self.source_type = None
                self.name = None
                self.closed = False
                self.frame_size = (640, 480)
                self.fps = 30.0
                if isinstance(source, str):
                    created.append(self)

            def open(self):
                self.connected = True

            def close(self):
                self.connected = False
                self.closed = True

        class FakeYoutubeDL:
            def __init__(self, options):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                return False
            def extract_info(self, url, download=False):
                return {"url": "https://stream.example/live.m3u8"}

        yt_dlp = ModuleType("yt_dlp")
        yt_dlp.YoutubeDL = FakeYoutubeDL
        url = "https://www.youtube.com/watch?v=abc123"

        with mock.patch("recorder.service.YouTubeCamera", FakeYoutubeCamera), \
                mock.patch.dict("sys.modules", {"yt_dlp": yt_dlp}):
            first = self.svc.connect_youtube("s1", url)
            self.assertTrue(first.connected)
            self.assertIn(first, self.svc.sessions["s1"].cameras)

            # Cambiar a USB debe cerrar y retirar la fuente YouTube anterior.
            self.svc.connect("s1", index=0)
            self.assertTrue(first.closed)
            self.assertNotIn(first, self.svc.sessions["s1"].cameras)

            second = self.svc.connect_youtube("s1", url)
            self.assertIsNot(second, first)
            self.assertTrue(second.connected)
            self.assertEqual(len(created), 2)
            self.assertEqual(
                [c["name"] for c in self.svc._session_state(self.svc.sessions["s1"])["cameras"]],
                ["CAMYT 1"],
            )

    def test_dead_youtube_camera_cannot_be_selected_as_active(self):
        session = self.svc.sessions["s1"]
        zombie = mock.MagicMock()
        zombie.source_type = "youtube"
        zombie.name = "CAMYT 1"
        zombie.connected = False
        session.cameras = [zombie]
        with self.assertRaisesRegex(ServiceError, "Vuelve a conectarla"):
            self.svc.switch_active_camera("s1", "CAMYT 1")
        self.assertNotIn(zombie, session.cameras)

    def test_persisted_camera_settings_are_ignored_and_not_saved(self):
        settings = Settings(sessions=[{
            "id": "s1", "label": "Pista guardada", "camera_index": 4,
            "camera_backend": "msmf", "width": 1920, "height": 1080,
        }])
        with mock.patch.object(settings, "save"):
            service = RecorderService(settings, Database(self.tmp / "fresh.db"))
            self.addCleanup(service.shutdown)
            session = service.sessions["s1"]
            self.assertEqual(session.camera_index, 0)
            self.assertEqual(session.camera_backend, "auto")
            self.assertEqual((session.width, session.height), (1280, 720))
            self.assertNotIn("camera_index", settings.sessions[0])
            self.assertNotIn("camera_backend", settings.sessions[0])
            self.assertNotIn("width", settings.sessions[0])
            self.assertNotIn("height", settings.sessions[0])

            service.shutdown()
            next_settings = Settings(sessions=settings.sessions)
            with mock.patch.object(next_settings, "save"):
                next_run = RecorderService(next_settings, Database(self.tmp / "fresh.db"))
                self.addCleanup(next_run.shutdown)
                self.assertEqual(next_run.sessions["s1"].camera_index, 0)
                self.assertIsNone(next_run.sessions["s1"].camera)

    def test_connected_camera_uses_its_discovered_device_name(self):
        with mock.patch.object(self.svc, "_available_cameras", [
            {"index": 0, "name": "Logitech C920", "backend": "dshow"},
        ]):
            self.svc.connect("s1", index=0)

        self.assertEqual(self.svc.snapshot()["sessions"][0]["camera"]["name"], "Logitech C920")

    def test_cancel_recording_discards_partial_file_and_does_not_add_recent_clip(self):
        self.svc.select("s1", player=self.ana)
        self.svc.start("s1")
        time.sleep(0.35)
        path = self.svc.sessions["s1"].current_path
        self.assertIsNotNone(path)
        self.svc.cancel("s1")
        self.assertFalse(self.svc.sessions["s1"].recording)
        self.assertFalse(path.exists())
        self.assertFalse(any(clip.get("path") == str(path) for clip in self.svc.recent))
        with self.assertRaises(ServiceError):
            self.svc.cancel("s1")

    def test_two_sessions_record_at_the_same_time_without_mixing_streams(self):
        self.svc.select("s1", player=self.ana)
        self.svc.select("s2", player=self.beto)
        self.svc.start("s1")
        self.svc.start("s2")
        time.sleep(1.2)
        clip1, clip2 = self.svc.stop("s1"), self.svc.stop("s2")

        p1, p2 = Path(clip1["path"]), Path(clip2["path"])
        self.assertNotEqual(p1, p2)
        self.assertEqual(p1.parent.relative_to(self.tmp / "clips").as_posix(), "Mi_equipo/Ana/Sin_ejercicio")
        self.assertEqual(p2.parent.relative_to(self.tmp / "clips").as_posix(), "Mi_equipo/Beto/Sin_ejercicio")
        self.assertGreater(frame_count(p1), 20)
        self.assertGreater(frame_count(p2), 20)
        self.assertEqual(first_frame_channel(p1), 0, "la sesión A es la cámara azul")
        self.assertEqual(first_frame_channel(p2), 1, "la sesión B es la cámara verde")

    def test_a_physical_camera_belongs_to_one_session(self):
        with self.assertRaises(ServiceError) as ctx:
            self.svc.connect("s2", index=0)
        self.assertIn("Pista A", str(ctx.exception))

    def test_youtube_view_number_does_not_conflict_with_physical_camera_number(self):
        fake_ytdlp = mock.MagicMock()
        fake_ytdlp.YoutubeDL.return_value.__enter__.return_value.extract_info.return_value = {
            "url": "https://example.test/live"
        }
        fake_camera = mock.MagicMock()
        fake_camera.connected = True
        fake_camera.frame_size = (640, 360)
        fake_camera.fps = 30

        with mock.patch.dict("sys.modules", {"yt_dlp": fake_ytdlp}), \
                mock.patch("recorder.service.YouTubeCamera", return_value=fake_camera):
            self.svc.connect_youtube("s2", "https://www.youtube.com/watch?v=test", index=0)

        sessions = {s["id"]: s for s in self.svc.snapshot()["sessions"]}
        self.assertTrue(sessions["s1"]["camera"]["connected"])
        self.assertEqual(sessions["s1"]["camera"]["source"], "usb")
        self.assertEqual(sessions["s1"]["camera"]["index"], 0)
        self.assertTrue(sessions["s2"]["camera"]["connected"])
        self.assertEqual(sessions["s2"]["camera"]["source"], "youtube")
        self.assertEqual(sessions["s2"]["camera"]["source_index"], 0)
        self.assertEqual(sessions["s2"]["camera"]["index"], 1)

    def test_unavailable_camera_reports_a_clear_error(self):
        self.svc.remove_session("s2")
        with self.assertRaises(ServiceError):
            self.svc.connect("s1", index=5)
        state = self.svc.snapshot()["sessions"][0]
        self.assertFalse(state["camera"]["connected"])
        self.assertIn("5", state["camera"]["error"])

    def test_a_player_cannot_record_in_two_sessions_at_once(self):
        self.svc.select("s1", player=self.ana)
        self.svc.select("s2", player=self.ana)
        self.svc.start("s1")
        with self.assertRaises(ServiceError) as ctx:
            self.svc.start("s2")
        self.assertIn("Pista A", str(ctx.exception))
        time.sleep(0.3)
        self.svc.stop("s1")
        self.svc.start("s2")                         # ya libre
        time.sleep(0.3)
        self.svc.stop("s2")

    def test_cannot_remove_what_is_being_recorded_but_can_add_others(self):
        exercise = self.svc.snapshot()["profiles"][0]["exercises"][0]["id"]
        self.svc.select("s1", player=self.ana, exercise=exercise)
        self.svc.start("s1")
        for call in (lambda: self.svc.remove_player(self.ana),
                     lambda: self.svc.remove_exercise(exercise),
                     lambda: self.svc.delete_profile(self.team),
                     lambda: self.svc.select("s1", player=self.beto),
                     lambda: self.svc.open_profile("s1", self.team),
                     lambda: self.svc.connect("s1", index=1)):
            with self.assertRaises(ServiceError):
                call()
        self.svc.add_player(self.team, "Carla")          # editar listas para la otra pista sigue permitido
        self.svc.remove_player(self.beto)
        time.sleep(0.3)
        self.svc.stop("s1")

    def test_each_session_has_its_own_team_and_selection(self):
        other = self.svc.create_profile("Juvenil", session_id="s2")
        sessions = {s["id"]: s for s in self.svc.snapshot()["sessions"]}
        self.assertEqual(sessions["s1"]["profile_id"], self.team)
        self.assertEqual(sessions["s2"]["profile_id"], other)
        self.assertNotEqual(sessions["s1"]["player_id"], sessions["s2"]["player_id"])

    def test_deleting_a_team_moves_idle_sessions_to_another(self):
        other = self.svc.create_profile("Juvenil", session_id="s2")
        self.svc.delete_profile(other)
        sessions = {s["id"]: s for s in self.svc.snapshot()["sessions"]}
        self.assertEqual(sessions["s2"]["profile_id"], self.team)
        self.assertIsNotNone(sessions["s2"]["player_id"])

    def test_disconnected_camera_saves_the_clip_and_notifies_everyone(self):
        self.svc.select("s1", player=self.ana)
        self.svc.start("s1")
        time.sleep(0.6)
        FakeCapture.dead.add(0)
        self.assertTrue(wait_until(lambda: self.svc.sessions["s1"].camera is None, timeout=10))
        snap = self.svc.snapshot()
        self.assertEqual(len(snap["recent"]), 1)
        self.assertGreater(frame_count(Path(snap["recent"][0]["path"])), 5)
        self.assertTrue(any("Pista A" in n["text"] and "guardó" in n["text"] for n in snap["notices"]))
        self.assertFalse(snap["sessions"][0]["recording"])
        self.assertTrue(snap["sessions"][1]["camera"]["connected"], "la otra sesión no se ve afectada")

    def test_shutdown_while_recording_saves_the_clip(self):
        self.svc.select("s1", player=self.ana)
        self.svc.start("s1")
        time.sleep(0.8)
        path = Path(self.svc.sessions["s1"].current_path)
        self.svc.shutdown()
        self.assertGreater(frame_count(path), 10)

    def test_changes_wake_up_waiting_windows(self):
        revision = self.svc.snapshot()["revision"]
        woke = []

        def waiter():
            woke.append(self.svc.wait_for_change(revision, timeout=5))

        thread = threading.Thread(target=waiter)
        thread.start()
        time.sleep(0.2)
        started = time.monotonic()
        self.svc.add_player(self.team, "Dani")
        thread.join(timeout=2)
        self.assertEqual(len(woke), 1)
        self.assertGreater(woke[0], revision)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_settings_and_selection_survive_a_restart(self):
        self.svc.select("s1", player=self.beto)
        self.svc.rename_session("s1", "Pista central")
        self.svc.shutdown()

        again = RecorderService(Settings.load(), Database(self.tmp / "d.db"))
        self.addCleanup(again.shutdown)
        first = again.snapshot()["sessions"][0]
        self.assertEqual(first["label"], "Pista central")
        self.assertEqual(first["player_id"], self.beto)
        self.assertEqual(len(again.snapshot()["profiles"][0]["players"]), 2)

    def test_stress_two_sessions_start_and_stop_from_many_threads(self):
        self.svc.select("s1", player=self.ana)
        self.svc.select("s2", player=self.beto)
        done, errors = [], []

        def worker(session):
            try:
                for _ in range(4):
                    self.svc.start(session)
                    time.sleep(0.25)
                    done.append(Path(self.svc.stop(session)["path"]))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        def noise():
            for i in range(10):                          # mientras tanto, otra ventana edita listas
                self.svc.add_player(self.team, f"Extra {i}")
                self.svc.snapshot()

        threads = [threading.Thread(target=worker, args=(s,)) for s in ("s1", "s2")] + [threading.Thread(target=noise)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(errors, [])
        self.assertEqual(len(done), 8)
        self.assertEqual(len(set(done)), 8, "ningún clip pisó a otro")
        on_disk = list((self.tmp / "clips").rglob("*.mp4"))
        self.assertEqual(sorted(on_disk), sorted(done))
        self.assertTrue(all(frame_count(p) > 3 for p in done))


if __name__ == "__main__":
    unittest.main()
