"""Servidor HTTP local: páginas, API JSON, tiempo real (SSE) y vista previa MJPEG."""
from __future__ import annotations

import base64
import io
import json
import mimetypes
import secrets
import select
import socket
import ssl
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, quote, urlparse

from .clients import OPERATOR, VIEWER, Client
from .db import DbError
from .instance import new_key
from .service import RecorderService, ServiceError

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_FILES = {
    "/style.css": "style.css",
    "/app.js": "app.js",
    "/display.css": "display.css",
    "/display.js": "display.js",
}
PREVIEW_FPS = 30
HEARTBEAT_SECONDS = 2.0
MAX_BODY = 64 * 1024
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
    "script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
)


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, service: RecorderService, operator_key: str, viewer_key: str, lan: bool = False) -> None:
        super().__init__(("0.0.0.0" if lan else "127.0.0.1", 0), _Handler)
        port = self.server_address[1]
        self.service = service
        self.lan = bool(lan)
        self.operator_key = operator_key
        self.viewer_key = viewer_key
        self.allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if lan:
            from .security import local_addresses
            self.allowed_hosts.update(f"{ip}:{port}" for ip in local_addresses())
            self.allowed_hosts.add(f"extremos.local:{port}")
        self.closing = False


class LocalServer:
    """Local UI listener plus an optional HTTPS listener for tablets on the LAN.

    Keeping the operator UI on loopback HTTP avoids encrypting every MJPEG preview
    frame twice on the same PC. Browsers consider http://127.0.0.1 a secure context
    for camera APIs; remote tablets still use HTTPS on the separate LAN listener.
    """

    def __init__(self, service: RecorderService, operator_key: str | None = None, viewer_key: str | None = None, lan: bool = False) -> None:
        self.lan = bool(lan)
        operator_key = operator_key or new_key()
        viewer_key = viewer_key or new_key()
        # Local UI always uses loopback HTTP, whether or not LAN mode is enabled.
        self._httpd = _Server(service, operator_key, viewer_key, lan=False)
        self._lan_httpd: _Server | None = None
        self._lan_thread: threading.Thread | None = None
        if self.lan:
            self._lan_httpd = _Server(service, operator_key, viewer_key, lan=True)
            from .security import tls_context
            self._lan_httpd.socket = tls_context().wrap_socket(self._lan_httpd.socket, server_side=True)
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="http-server", daemon=True)
        self._started = False
        if self._lan_httpd is not None:
            self._lan_thread = threading.Thread(
                target=self._lan_httpd.serve_forever, name="https-lan-server", daemon=True
            )

    @property
    def port(self) -> int:
        """Loopback port used by main.py and attached local windows."""
        return self._httpd.server_address[1]

    @property
    def lan_port(self) -> int | None:
        return self._lan_httpd.server_address[1] if self._lan_httpd is not None else None

    @property
    def operator_key(self) -> str:
        return self._httpd.operator_key

    @property
    def viewer_key(self) -> str:
        return self._httpd.viewer_key

    def operator_url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?k={self.operator_key}"

    def lan_urls(self) -> list[str]:
        if self._lan_httpd is None or self.lan_port is None:
            return []
        from .security import local_addresses
        return [
            f"https://{ip}:{self.lan_port}/?k={self.operator_key}&mode=hidden"
            for ip in local_addresses() if ip != "127.0.0.1"
        ]

    def display_url(self, session_id: str | None = None) -> str:
        # Local display stays on loopback HTTP to keep preview overhead low.
        return display_url(self.port, self.viewer_key, session_id)

    def start(self) -> None:
        self._thread.start()
        if self._lan_thread is not None:
            self._lan_thread.start()
        self._started = True
        self._zeroconf = None
        self._service_info = None
        if self._lan_httpd is not None:
            try:
                from zeroconf import IPVersion, ServiceInfo, Zeroconf
                from .security import local_addresses
                addresses = []
                for ip in local_addresses():
                    if ip != "127.0.0.1":
                        import ipaddress
                        addresses.append(ipaddress.ip_address(ip).packed)
                if addresses:
                    self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
                    self._service_info = ServiceInfo(
                        "_https._tcp.local.", "Extremos._https._tcp.local.",
                        addresses=addresses, port=self.lan_port,
                        properties={"path": "/", "app": "Extremos"}, server="extremos.local.")
                    self._zeroconf.register_service(self._service_info)
            except Exception:
                self._zeroconf = None

    def stop(self) -> None:
        servers = [self._httpd]
        if self._lan_httpd is not None:
            servers.append(self._lan_httpd)
        for httpd in servers:
            httpd.closing = True
        for httpd, thread in ((self._httpd, self._thread), (self._lan_httpd, self._lan_thread)):
            if httpd is None:
                continue
            try:
                if thread is None or thread.is_alive():
                    httpd.shutdown()
            except Exception:
                pass
            try:
                httpd.server_close()
            except Exception:
                pass
        if getattr(self, "_zeroconf", None) is not None:
            try:
                self._zeroconf.unregister_all_services()
                self._zeroconf.close()
            except Exception:
                pass


def display_url(port: int, viewer_key: str, session_id: str | None = None) -> str:
    url = f"http://127.0.0.1:{port}/display?k={viewer_key}"
    return url + (f"&s={quote(session_id)}" if session_id else "")


def operator_url(port: int, operator_key: str) -> str:
    return f"http://127.0.0.1:{port}/?k={operator_key}"


class _Handler(BaseHTTPRequestHandler):
    server: _Server
    server_version = "ExtremosRecorder"

    def log_message(self, format: str, *args: Any) -> None:
        pass

    # ------------------------------------------------------------------ utilidades
    def _host_ok(self) -> bool:
        return self.headers.get("Host", "").lower() in self.server.allowed_hosts

    def _key_role(self, url) -> str | None:
        key = parse_qs(url.query).get("k", [None])[0]
        if not key:
            return None
        if secrets.compare_digest(key, self.server.operator_key):
            return OPERATOR
        if secrets.compare_digest(key, self.server.viewer_key):
            return VIEWER
        return None

    def _client(self, url) -> Client | None:
        token = self.headers.get("X-Token") or parse_qs(url.query).get("t", [None])[0]
        return self.server.service.clients.get(token)

    def _send(self, status: int, body: bytes, content_type: str, html: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if html:
            self.send_header("Content-Security-Policy", CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

    def _deny(self, message: str = "Acceso denegado.", status: int = 403) -> None:
        self._json({"ok": False, "error": message}, status)

    def _page(self, filename: str, client: Client, **extra: str) -> None:
        html = (WEB_DIR / filename).read_text(encoding="utf-8").replace("__TOKEN__", client.token)
        for name, value in extra.items():
            html = html.replace(name, value)
        self._send(200, html.encode("utf-8"), "text/html; charset=utf-8", html=True)

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        if not self._host_ok():
            return self._deny()
        url = urlparse(self.path)
        service = self.server.service

        if url.path == "/api/ping":
            return self._json({"ok": True, "app": "extremos-grabador"})

        if url.path in ("/", "/index.html"):
            if self._key_role(url) != OPERATOR:
                return self._send(403, "Acceso no autorizado. Abre la aplicación desde el programa.".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._page("index.html", service.clients.register(OPERATOR), __VIEWER_KEY__=self.server.viewer_key)

        if url.path == "/display":
            if self._key_role(url) is None:
                return self._send(403, "Acceso no autorizado. Abre la aplicación desde el programa.".encode("utf-8"),
                                  "text/plain; charset=utf-8")
            return self._page("display.html", service.clients.register(VIEWER))

        if url.path == "/favicon.ico":
            file = WEB_DIR / "favicon.ico"
            if not file.is_file():
                return self._send(404, b"Logo no configurado", "text/plain; charset=utf-8")
            return self._send(200, file.read_bytes(), "image/x-icon")

        if url.path in STATIC_FILES:
            file = WEB_DIR / STATIC_FILES[url.path]
            ctype = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self._send(200, file.read_bytes(), f"{ctype}; charset=utf-8")

        client = self._client(url)
        if url.path == "/api/cameras":
            if client is None:
                return self._deny()
            # Solo exponemos cámaras físicas/capturadoras que han sido fiables en uso real.
            # DroidCam por IP tiene su control dedicado; clientes virtuales no se ofrecen.
            cameras = [c for c in service.get_available_cameras() if c.get("kind") != "virtual"]
            return self._json({"ok": True, "cameras": cameras})

        if url.path == "/api/device-connect":
            if client is None or client.role != OPERATOR:
                return self._deny()
            if not self.server.lan:
                return self._json({"ok": True, "enabled": False})
            from .security import local_addresses
            detected_ips = list(dict.fromkeys(ip for ip in local_addresses() if ip and ip != "127.0.0.1"))
            if not detected_ips:
                return self._json({"ok": False, "error": "No se ha detectado ninguna dirección IP de la red local. Comprueba que el ordenador está conectado a la misma Wi-Fi o red que las tablets."}, 503)
            port = self.server.server_address[1]
            unique_urls = [f"https://{ip}:{port}/?k={self.server.operator_key}&mode=hidden" for ip in detected_ips]
            urls = [unique_urls[i % len(unique_urls)] for i in range(6)]
            qr_codes = []
            qr_error = None
            try:
                import qrcode
                for address in urls:
                    image = qrcode.make(address)
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    qr_codes.append("data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode("ascii"))
            except Exception as exc:
                qr_error = str(exc)
                qr_codes = []
            return self._json({"ok": True, "enabled": True, "urls": urls, "qrs": qr_codes, "unique_addresses": len(unique_urls), "qr_error": qr_error})

        if client is None:
            return self._deny() if url.path.startswith(("/api/", "/events", "/stream")) else self._deny("No encontrado.", 404)

        if url.path == "/api/state":
            return self._json({"ok": True, "state": service.snapshot(client.role)})
        if url.path == "/events":
            return self._events(client)
        if url.path == "/stream":
            return self._stream(parse_qs(url.query).get("s", [""])[0])

        self._deny("No encontrado.", 404)

    # ------------------------------------------------------------------ POST
    def do_POST(self) -> None:
        if not self._host_ok():
            return self._deny()
        url = urlparse(self.path)
        client = self._client(url)
        if url.path == "/api/remote/frame":
            if client is None or client.role != OPERATOR:
                return self._deny()
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > 2_000_000:
                return self._deny("Fotograma vacío o demasiado grande.", 413)
            payload = self.rfile.read(length)
            if not self.headers.get("Content-Type", "").lower().startswith("image/jpeg"):
                return self._deny("Se esperaba un fotograma JPEG.", 415)
            sid = parse_qs(url.query).get("session", [None])[0]
            try:
                self.server.service.push_remote_frame(sid, payload)
            except (ServiceError, DbError) as exc:
                return self._json({"ok": False, "error": str(exc)}, 400)
            return self._json({"ok": True})
        if client is None:
            return self._deny()
        if client.role != OPERATOR:
            return self._deny("Esta ventana es de solo lectura.")
        if not url.path.startswith("/api/"):
            return self._deny("No encontrado.", 404)
        action = url.path[len("/api/"):]

        try:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._deny("Petición demasiado grande.", 413)
            body = json.loads(self.rfile.read(length) or b"{}")
            if not isinstance(body, dict):
                raise ValueError
        except ValueError:
            return self._deny("Petición no válida.", 400)

        service = self.server.service
        try:
            extra = self._dispatch(service, action, body)
        except (ServiceError, DbError) as exc:
            return self._json({"ok": False, "error": str(exc), "state": service.snapshot()})
        except Exception:
            traceback.print_exc(file=sys.stderr)
            return self._json({"ok": False, "error": "Error interno de la aplicación.", "state": service.snapshot()}, 500)
        self._json({"ok": True, "state": service.snapshot(), **(extra or {})})

    @staticmethod
    def _dispatch(service: RecorderService, action: str, b: dict[str, Any]) -> dict[str, Any] | None:
        sid = b.get("session")
        if action == "select":
            service.select(sid, **{k: b[k] for k in ("player", "exercise") if k in b})
        elif action == "start":
            service.start(sid)
        elif action == "stop":
            return {"clip": service.stop(sid)}
        elif action == "cancel":
            service.cancel(sid)
        elif action == "retrospective":
            return {"clip": service.save_retrospective(sid, b.get("seconds", 10))}
        elif action == "connect":
            service.connect(sid, b.get("index"), b.get("width"), b.get("height"), b.get("camera_backend"))
        elif action == "connect_droidcam_direct":
            service.connect_droidcam_direct(
                sid, b.get("host"), b.get("port", 4747), b.get("width", 1280), b.get("height", 720)
            )
        elif action == "connect_remote":
            service.connect_remote(sid)
        elif action == "connect_youtube":
            service.connect_youtube(sid, b.get("url"), b.get("crop_type", "full"), delay_seconds=b.get("delay_seconds", 10))
        elif action == "switch_active_camera":
            service.switch_active_camera(sid, b.get("camera_name"))
        elif action == "add_session":
            return {"session": service.add_session()}
        elif action == "remove_session":
            service.remove_session(sid)
        elif action == "rename_session":
            service.rename_session(sid, b.get("label"))
        elif action == "create_pavilion":
            return {"pavilion": service.create_pavilion(b.get("name"))}
        elif action == "rename_pavilion":
            service.rename_pavilion(b.get("pavilion"), b.get("name"))
        elif action == "delete_pavilion":
            service.delete_pavilion(b.get("pavilion"))
        elif action == "assign_pavilion":
            service.assign_pavilion(sid, b.get("pavilion"))
        elif action == "open_profile":
            service.open_profile(sid, b.get("profile"))
        elif action == "create_profile":
            return {"profile": service.create_profile(b.get("name"), sid)}
        elif action == "rename_profile":
            service.rename_profile(b.get("profile"), b.get("name"))
        elif action == "duplicate_profile":
            return {"profile": service.duplicate_profile(b.get("profile"), sid)}
        elif action == "delete_profile":
            service.delete_profile(b.get("profile"))
        elif action == "export_profile":
            return {"path": service.export_profile(b.get("profile"))}
        elif action == "import_profile":
            return {"profile": service.import_profile(b.get("path"), sid)}
        elif action == "add_category":
            service.add_category(b.get("profile"), b.get("name"))
        elif action == "remove_category":
            service.remove_category(b.get("category"))
        elif action == "add_player":
            service.add_player(b.get("profile"), b.get("name"), b.get("category"))
        elif action == "remove_player":
            service.remove_player(b.get("player"))
        elif action == "move_player":
            service.move_player(b.get("player"), b.get("category"))
        elif action == "add_exercise_category":
            service.add_exercise_category(b.get("profile"), b.get("name"))
        elif action == "remove_exercise_category":
            service.remove_exercise_category(b.get("category"))
        elif action == "add_exercise":
            service.add_exercise(b.get("profile"), b.get("name"), b.get("category"))
        elif action == "move_exercise":
            service.move_exercise(b.get("exercise"), b.get("category"))
        elif action == "remove_exercise":
            service.remove_exercise(b.get("exercise"))
        elif action == "set_base_dir":
            service.set_base_dir(b.get("path"))
        elif action == "open_folder":
            service.open_folder(b.get("path"))
        else:
            raise ServiceError("Acción desconocida.")
        return None

    # ------------------------------------------------------------------ tiempo real (SSE)
    def _events(self, client: Client) -> None:
        service = self.server.service
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        service.clients.attach(client)
        try:
            revision = -1
            while not self.server.closing and not service.closed:
                current = service.wait_for_change(revision, HEARTBEAT_SECONDS)
                if self._peer_closed():
                    break
                if current == revision:
                    self.wfile.write(b": ping\n\n")
                else:
                    revision = current
                    payload = json.dumps({"state": service.snapshot(client.role)}, ensure_ascii=False)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionError, OSError):
            pass
        finally:
            service.clients.detach(client)

    def _peer_closed(self) -> bool:
        try:
            if isinstance(self.connection, ssl.SSLSocket):
                return False
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except (OSError, ValueError):
            return True

    # ------------------------------------------------------------------ vista previa MJPEG
    def _stream(self, session_id: str) -> None:
        service = self.server.service
        camera, generation = service.stream_source(session_id)
        if camera is None:
            return self._deny("No hay cámara conectada en esa sesión.", 404)

        self.send_response(200)
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

        last_id = 0
        # Fuentes YouTube pueden llegar a 60 FPS. Mantener su cadencia nativa
        # evita que la propia vista previa descarte la mitad de los frames de un
        # directo 60 FPS; las otras cámaras conservan el límite global habitual.
        if getattr(camera, "source_type", None) == "youtube":
            preview_fps = min(60.0, max(1.0, float(camera.fps)))
        else:
            preview_fps = min(float(PREVIEW_FPS), max(1.0, float(camera.fps)))
        interval = 1.0 / preview_fps
        try:
            while not self.server.closing and not service.closed:
                current, gen = service.stream_source(session_id)
                if gen != generation or current is not camera or not camera.connected:
                    break
                started = time.monotonic()
                frame_id, data = camera.preview_jpeg()
                # La compresión JPEG ocurre en otro hilo para no frenar la captura.
                # Comparar el id del JPEG (no el id del frame crudo) evita reenviar
                # repetidamente la imagen anterior mientras el codificador se pone al día.
                if data is None or frame_id == last_id:
                    time.sleep(0.004)
                    continue
                last_id = frame_id
                if data is not None:
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(data)).encode()
                        + b"\r\n\r\n"
                        + data
                        + b"\r\n"
                    )
                time.sleep(max(0.0, interval - (time.monotonic() - started)))
        except (BrokenPipeError, ConnectionError, OSError):
            pass