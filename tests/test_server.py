"""Pruebas del servidor: claves, roles, aislamiento entre ventanas, tiempo real y vista previa.

    python -m unittest discover -s tests -t . -v
"""
from __future__ import annotations

import http.client
import json
import queue
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import cv2

from recorder.config import CONFIG_PATH, Settings
from recorder.db import Database
from recorder.server import LocalServer
from recorder.service import RecorderService

from .test_core import FakeCapture


class SSEClient:
    """Una ventana con la conexión de tiempo real abierta (socket directo, como haría un navegador)."""

    def __init__(self, port: int, token: str):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        request = f"GET /events?t={token} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAccept: text/event-stream\r\n\r\n"
        self.sock.sendall(request.encode())
        self.stream = self.sock.makefile("rb")
        self.status = int(self.stream.readline().split()[1])
        while self.stream.readline() not in (b"\r\n", b""):       # cabeceras
            pass
        self.events: queue.Queue = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        try:
            while True:
                line = self.stream.readline()
                if not line:
                    break
                if line.startswith(b"data: "):
                    self.events.put(json.loads(line[6:])["state"])
        except (OSError, ValueError):
            pass  # al cerrar la conexión la lectura falla: es lo esperado

    def wait_for(self, predicate, timeout: float = 3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.events.get(timeout=max(0.01, deadline - time.monotonic()))
            except queue.Empty:
                break
            if predicate(state):
                return state
        return None

    def close(self):
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.stream.close()
        self.sock.close()


class ServerTests(unittest.TestCase):
    def setUp(self):
        FakeCapture.dead = set()
        patcher = mock.patch.object(cv2, "VideoCapture", FakeCapture)
        patcher.start()
        self.addCleanup(patcher.stop)
        CONFIG_PATH.unlink(missing_ok=True)

        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        settings = Settings(
            base_dir=str(self.tmp / "clips"),
            sessions=[{"id": "s1", "label": "Pista A", "camera_index": 0},
                      {"id": "s2", "label": "Pista B", "camera_index": 1}],
        )
        self.service = RecorderService(settings, Database(self.tmp / "d.db"))
        self.server = LocalServer(self.service, operator_key="OPKEY", viewer_key="VWKEY")
        self.server.start()
        self.addCleanup(self.server.stop)
        self.addCleanup(self.service.shutdown)
        self.port = self.server.port
        self.service.connect("s1")
        self.service.connect("s2", index=1)
        team = self.service.snapshot()["profiles"][0]["id"]
        self.service.select("s1", player=self.service.add_player(team, "Ana"))
        self.service.select("s2", player=self.service.add_player(team, "Beto"))
        self._clients: list[SSEClient] = []
        self.addCleanup(lambda: [c.close() for c in self._clients])

    # ------------------------------------------------------------------ ayudas
    def request(self, method, path, headers=None, body=None, host=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True)
            conn.putheader("Host", host or f"127.0.0.1:{self.port}")
            for key, value in (headers or {}).items():
                conn.putheader(key, value)
            payload = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else b"")
            conn.putheader("Content-Length", str(len(payload)))
            conn.endheaders(payload)
            response = conn.getresponse()
            return response.status, response.read(), dict(response.getheaders())
        finally:
            conn.close()

    def token_from(self, path):
        status, body, _ = self.request("GET", path)
        self.assertEqual(status, 200, body[:200])
        match = re.search(rb'name="token" content="([^"]+)"', body)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    def operator(self):
        return self.token_from("/?k=OPKEY")

    def viewer(self):
        return self.token_from("/display?k=VWKEY")

    def post(self, token, action, body=None):
        status, raw, _ = self.request("POST", f"/api/{action}", {"X-Token": token}, body or {})
        return status, json.loads(raw)

    def sse(self, token) -> SSEClient:
        client = SSEClient(self.port, token)
        self._clients.append(client)
        return client

    # ------------------------------------------------------------------ claves y aislamiento
    def test_pages_need_the_right_key(self):
        for path in ("/", "/?k=nope", "/?k=VWKEY", "/display", "/display?k=nope"):
            status, _, _ = self.request("GET", path)
            self.assertEqual(status, 403, path)

    def test_viewer_key_cannot_open_the_control_page_but_operator_key_can_open_the_display(self):
        self.assertEqual(self.request("GET", "/?k=VWKEY")[0], 403)
        token = self.token_from("/display?k=OPKEY")
        status, payload = self.post(token, "start", {"session": "s1"})
        self.assertEqual(status, 403, "abrir la pantalla con la clave de control sigue siendo solo lectura")

    def test_foreign_host_header_is_refused(self):
        status, _, _ = self.request("GET", "/?k=OPKEY", host="evil.example.com")
        self.assertEqual(status, 403)

    def test_control_page_carries_token_viewer_key_and_csp_and_no_placeholders(self):
        status, body, headers = self.request("GET", "/?k=OPKEY")
        self.assertEqual(status, 200)
        self.assertNotIn(b"__TOKEN__", body)
        self.assertNotIn(b"__VIEWER_KEY__", body)
        self.assertIn(b'name="viewer-key" content="VWKEY"', body)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
        self.assertNotIn(b"OPKEY", body, "la clave de control no se reenvía dentro de la página")

    def test_each_window_gets_its_own_token(self):
        self.assertNotEqual(self.operator(), self.operator())

    def test_api_needs_a_valid_token(self):
        self.assertEqual(self.request("GET", "/api/state")[0], 403)
        self.assertEqual(self.request("GET", "/api/state", {"X-Token": "inventado"})[0], 403)
        self.assertEqual(self.request("POST", "/api/start")[0], 403)
        self.assertEqual(self.request("GET", "/events?t=inventado")[0], 403)
        self.assertEqual(self.request("GET", "/stream?t=inventado&s=s1")[0], 403)

    def test_viewer_is_read_only_and_sees_a_reduced_state(self):
        token = self.viewer()
        status, raw, _ = self.request("GET", "/api/state", {"X-Token": token})
        state = json.loads(raw)["state"]
        self.assertEqual(status, 200)
        self.assertNotIn("base_dir", state)
        self.assertNotIn("profiles", state)
        self.assertEqual([s["label"] for s in state["sessions"]], ["Pista A", "Pista B"])
        for action in ("start", "stop", "select", "add_player", "set_base_dir", "remove_session"):
            status, payload = self.post(token, action, {"session": "s1"})
            self.assertEqual(status, 403, action)
            self.assertFalse(payload["ok"])
        self.assertFalse(self.service.sessions["s1"].recording)

    def test_unknown_action_and_bad_session_give_clear_errors(self):
        token = self.operator()
        status, payload = self.post(token, "formatear")
        self.assertFalse(payload["ok"])
        status, payload = self.post(token, "start", {"session": "s9"})
        self.assertFalse(payload["ok"])
        self.assertIn("ya no existe", payload["error"])
        status, raw, _ = self.request("POST", "/api/start", {"X-Token": token}, None)
        self.assertEqual(status, 200)                       # cuerpo vacío válido: error de negocio, no caída

    # ------------------------------------------------------------------ control y conflictos
    def test_operator_records_through_the_api(self):
        token = self.operator()
        status, payload = self.post(token, "start", {"session": "s1"})
        self.assertTrue(payload["ok"], payload)
        self.assertTrue(next(s for s in payload["state"]["sessions"] if s["id"] == "s1")["recording"])
        time.sleep(0.5)
        status, payload = self.post(token, "stop", {"session": "s1"})
        self.assertTrue(payload["ok"], payload)
        self.assertGreater(payload["clip"]["duration"] + 1, 0)
        self.assertTrue(Path(payload["clip"]["path"]).exists())

    def test_two_control_windows_pressing_record_at_once_start_exactly_one_recording(self):
        tokens = [self.operator(), self.operator()]
        barrier, results = threading.Barrier(2), []

        def press(token):
            barrier.wait()
            results.append(self.post(token, "start", {"session": "s1"})[1]["ok"])

        threads = [threading.Thread(target=press, args=(t,)) for t in tokens]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(sorted(results), [False, True])
        time.sleep(0.3)
        self.service.stop("s1")

    # ------------------------------------------------------------------ tiempo real
    def test_a_change_in_one_window_reaches_the_projector_immediately(self):
        projector = self.sse(self.viewer())
        control = self.sse(self.operator())
        self.assertIsNotNone(projector.wait_for(lambda s: True))
        started = time.monotonic()
        self.post(self.operator(), "start", {"session": "s1"})
        state = projector.wait_for(lambda s: next(x for x in s["sessions"] if x["id"] == "s1")["recording"])
        self.assertIsNotNone(state, "la pantalla no recibió la grabación")
        self.assertLess(time.monotonic() - started, 1.5)
        mine = control.wait_for(lambda s: next(x for x in s["sessions"] if x["id"] == "s1")["recording"])
        self.assertIsNotNone(mine)
        time.sleep(0.3)
        self.service.stop("s1")

    def test_presence_is_shared_and_cleared_when_a_window_closes(self):
        control = self.sse(self.operator())
        self.assertIsNotNone(control.wait_for(lambda s: s["clients"]["operators"] == 1))
        projector = self.sse(self.viewer())
        self.assertIsNotNone(control.wait_for(lambda s: s["clients"]["viewers"] == 1), "no se vio la pantalla conectada")
        projector.close()
        self.assertIsNotNone(control.wait_for(lambda s: s["clients"]["viewers"] == 0, timeout=5), "no se vio que se cerró")

    def test_projector_never_receives_private_fields_in_the_stream(self):
        projector = self.sse(self.viewer())
        state = projector.wait_for(lambda s: True)
        self.assertNotIn("base_dir", state)
        self.assertNotIn("recent", state)
        self.assertNotIn("profiles", state)

    # ------------------------------------------------------------------ vista previa
    def test_preview_streams_jpeg_and_two_windows_can_watch_the_same_camera(self):
        def grab(token):
            conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
            conn.request("GET", f"/stream?t={token}&s=s1")
            response = conn.getresponse()
            self.assertEqual(response.status, 200)
            self.assertIn("multipart/x-mixed-replace", response.getheader("Content-Type"))
            data = response.fp.read(4000)
            conn.close()
            return data

        results = []
        threads = [threading.Thread(target=lambda t=t: results.append(grab(t))) for t in (self.operator(), self.viewer())]
        [t.start() for t in threads]
        [t.join() for t in threads]
        self.assertEqual(len(results), 2)
        for data in results:
            self.assertIn(b"\xff\xd8", data, "debe haber un JPEG")

    def test_preview_of_a_session_without_camera_is_a_clean_404(self):
        self.service.remove_session("s2")
        status, _, _ = self.request("GET", f"/stream?t={self.operator()}&s=s2")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()
