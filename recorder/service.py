"""Lógica de la aplicación: sesiones de cámara, grabación y equipos. Nada de interfaz."""
from __future__ import annotations

import json
import ipaddress
import os
import platform
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .camera import Camera, CameraError, RemoteCamera, YouTubeCamera, list_available_cameras
from .clients import ClientRegistry
from .config import DB_PATH, DEFAULT_CATEGORIES, DEFAULT_EXERCISES, Settings, read_legacy
from .db import Database
from .storage import build_target, sanitize, target_folder
from .destinations import prepare_video_directory

MAX_RECENT = 8
MAX_NOTICES = 8
MAX_LABEL_LENGTH = 20
MAX_IMPORT_BYTES = 1_000_000
SESSION_LABELS = ["Pista A", "Pista B", "Pista C", "Pista D", "Pista E", "Pista F", "Pista G", "Pista H"]
CAMERA_LOST = "Se perdió la conexión con la cámara. Comprueba el cable y vuelve a conectar."
CAMERA_RANGE = range(0, 10)


def _is_windows_platform() -> bool:
    """Detecta Windows sin que las pruebas deban parchear el módulo platform global."""
    return platform.system() == "Windows"


def _select_camera_device(
    devices: list[dict[str, Any]], index: int, requested_backend: str, *, windows: bool
) -> dict[str, Any] | None:
    """Resuelve un endpoint sin confundir índices de DirectShow y Media Foundation."""
    candidates = [device for device in devices if device.get("index") == index]
    if windows:
        if requested_backend == "auto":
            # Compatibilidad con llamadas antiguas: DirectShow conserva prioridad v2.6.
            selected = next((d for d in candidates if d.get("backend") == "dshow"), None)
            return selected or next((d for d in candidates if d.get("backend") == "msmf"), None)
        return next((d for d in candidates if d.get("backend") == requested_backend), None)
    # En otros sistemas la captura usa CAP_ANY; el inventario habitual marca backend auto.
    return candidates[0] if candidates else None

_UNSET: Any = object()


class ServiceError(Exception):
    """Error pensado para mostrarse tal cual al usuario."""


def _id(value: Any, what: str = "elemento", optional: bool = False) -> int | None:
    if value is None and optional:
        return None
    if isinstance(value, bool):
        raise ServiceError(f"Identificador de {what} no válido.")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ServiceError(f"Identificador de {what} no válido.") from None


def display_players(profile: dict[str, Any]) -> list[dict[str, Any]]:
    """Jugadores en el orden en que se ven en pantalla: por categoría y, al final, sin categoría."""
    category_ids = [c["id"] for c in profile["categories"]]
    ordered: list[dict[str, Any]] = []
    for cid in category_ids:
        ordered += [p for p in profile["players"] if p["category_id"] == cid]
    ordered += [p for p in profile["players"] if p["category_id"] not in category_ids]
    return ordered


@dataclass
class Session:
    id: str
    label: str
    camera_index: int = 0
    camera_backend: str = "auto"
    camera_device_group: str | None = None
    camera_source: str = "usb"
    youtube_view_index: int | None = None
    width: int = 1280
    height: int = 720
    profile_id: int | None = None
    player_id: int | None = None
    exercise_id: int | None = None
    pavilion_id: str | None = None

    camera: Camera | None = None
    cameras: list[Camera] = field(default_factory=list)
    generation: int = 0
    connecting: bool = False
    error: str | None = None

    recording: bool = False
    rec_started: float = 0.0
    current_path: Path | None = None
    current_meta: dict[str, Any] = field(default_factory=dict)

    def to_config(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "profile_id": self.profile_id,
            "player_id": self.player_id,
            "exercise_id": self.exercise_id,
            "pavilion_id": self.pavilion_id,
        }


class RecorderService:
    def __init__(self, settings: Settings | None = None, db: Database | None = None) -> None:
        legacy = read_legacy() if settings is None else {}
        self.settings = settings or Settings.load()
        self.db = db or Database(DB_PATH)
        self.db.ensure_initial(legacy.get("players", []), legacy.get("exercises", []))

        self._lock = threading.RLock()
        self._cond = threading.Condition(self._lock)
        self._rev = 1
        self._closed = False
        self.clients = ClientRegistry(on_change=self._changed)
        self._available_cameras = list_available_cameras()

        self._catalog = self.db.catalog()
        self.sessions: dict[str, Session] = {}
        self.recent: list[dict[str, Any]] = []
        self._clip_counter = 0
        self._notices: list[dict[str, Any]] = []
        self._notice_counter = 0

        self._load_sessions()
        self._stop = threading.Event()
        self._monitor = threading.Thread(target=self._monitor_loop, name="monitor", daemon=True)
        self._monitor.start()

    def get_available_cameras(self) -> list[dict[str, Any]]:
        """Devuelve la lista de cámaras físicas conectadas activas."""
        cameras = list_available_cameras()
        with self._lock:
            self._available_cameras = cameras
        return cameras

    # ================================================================== tiempo real
    def _changed(self) -> None:
        with self._cond:
            self._rev += 1
            self._cond.notify_all()

    def wait_for_change(self, last_rev: int, timeout: float) -> int:
        with self._cond:
            if self._rev == last_rev and not self._closed:
                self._cond.wait(timeout)
            return self._rev

    @property
    def closed(self) -> bool:
        return self._closed

    # ================================================================== estado
    def snapshot(self, role: str = "operator") -> dict[str, Any]:
        with self._lock:
            if role == "viewer":
                return self._viewer_snapshot()
            return {
                "revision": self._rev,
                "profiles": self._catalog,
                "pavilions": [dict(p) for p in self.settings.pavilions],
                "sessions": [self._session_state(s) for s in self.sessions.values()],
                "max_sessions": self.settings.max_sessions,
                "base_dir": self.settings.base_dir,
                "recent": [dict(c) for c in self.recent],
                "notices": [dict(n) for n in self._notices],
                "clients": self.clients.counts(),
            }

    def _viewer_snapshot(self) -> dict[str, Any]:
        sessions = []
        for s in self.sessions.values():
            state = self._session_state(s)
            profile = self._profile(s.profile_id)
            player = self._find(profile["players"], s.player_id)
            exercise = self._find(profile["exercises"], s.exercise_id)
            sessions.append({
                "id": s.id,
                "label": s.label,
                "recording": state["recording"],
                "elapsed": state["elapsed"],
                "connecting": s.connecting,
                "player": player["name"] if player else None,
                "exercise": exercise["name"] if exercise else None,
                "camera": {k: state["camera"][k] for k in ("connected", "generation", "width", "height")},
            })
        return {"revision": self._rev, "sessions": sessions, "clients": self.clients.counts(), "notices": []}

    def _session_state(self, s: Session) -> dict[str, Any]:
        cam = s.camera
        connected = cam is not None and cam.connected
        width, height = cam.frame_size if (connected and cam) else (s.width, s.height)
        profile = self._profile(s.profile_id)
        player = self._find(profile["players"], s.player_id)
        exercise = self._find(profile["exercises"], s.exercise_id)
        target = ""
        if player:
            folder = target_folder(
                "", player["name"], exercise["name"] if exercise else None,
                profile["name"] if self.settings.group_by_profile else None,
            )
            target = folder.as_posix()

        # Solo las fuentes YouTube vivas se muestran en este selector.
        active_cams = []
        live_youtube = []
        for c in getattr(s, "cameras", []):
            if getattr(c, "source_type", None) != "youtube" or not c.connected:
                continue
            live_youtube.append(c)
            active_cams.append({
                "name": getattr(c, "name", "Cámara YouTube"),
                "source": "youtube",
                "connected": True,
            })

        return {
            "id": s.id,
            "label": s.label,
            "pavilion_id": s.pavilion_id,
            "profile_id": s.profile_id,
            "player_id": s.player_id,
            "exercise_id": s.exercise_id,
            "target": target,
            "recording": s.recording,
            "elapsed": (time.monotonic() - s.rec_started) if s.recording else 0.0,
            "connecting": s.connecting,
            "cameras": active_cams,
            "camera": {
                "connected": connected,
                "index": s.camera_index,
                "backend": s.camera_backend,
                "source": s.camera_source,
                "name": getattr(cam, "name", None) if cam else None,
                "kind": getattr(cam, "device_kind", None) if cam else None,
                "device_group": getattr(cam, "device_group", None) if cam else s.camera_device_group,
                "source_index": s.youtube_view_index if s.camera_source == "youtube" else s.camera_index,
                "width": width,
                "height": height,
                "fps": round(cam.fps) if (connected and cam) else self.settings.fps,
                "error": s.error,
                "generation": s.generation,
            },
        }

    def stream_source(self, session_id: str) -> tuple[Camera | None, int]:
        with self._lock:
            s = self.sessions.get(session_id)
            return (s.camera, s.generation) if s else (None, -1)

    # ================================================================== sesiones
    def _load_sessions(self) -> None:
        entries = list(self.settings.sessions)[: self.settings.max_sessions]
        if not entries:
            entries = [{
                "id": "s1", "label": SESSION_LABELS[0],
            }]
        for entry in entries:
            sid = str(entry.get("id") or f"s{len(self.sessions) + 1}")
            if sid in self.sessions:
                continue
            self.sessions[sid] = Session(
                id=sid,
                label=str(entry.get("label") or SESSION_LABELS[len(self.sessions) % len(SESSION_LABELS)])[:MAX_LABEL_LENGTH],
                profile_id=entry.get("profile_id") if isinstance(entry.get("profile_id"), int) else None,
                player_id=entry.get("player_id") if isinstance(entry.get("player_id"), int) else None,
                exercise_id=entry.get("exercise_id") if isinstance(entry.get("exercise_id"), int) else None,
                pavilion_id=str(entry.get("pavilion_id")) if entry.get("pavilion_id") is not None and any(str(p.get("id")) == str(entry.get("pavilion_id")) for p in self.settings.pavilions) else None,
            )
        self._normalize_sessions()
        self._persist()

    @staticmethod
    def _clamp(value: Any, default: int, low: int, high: int) -> int:
        try:
            return max(low, min(high, int(value)))
        except (TypeError, ValueError):
            return default

    def add_session(self) -> str:
        with self._lock:
            if len(self.sessions) >= self.settings.max_sessions:
                raise ServiceError(f"Como máximo puede haber {self.settings.max_sessions} sesiones a la vez.")
            n = 1
            while f"s{n}" in self.sessions:
                n += 1
            sid = f"s{n}"
            used_labels = {s.label for s in self.sessions.values()}
            label = next((name for name in SESSION_LABELS if name not in used_labels), f"Sesión {n}")
            used_cameras = {s.camera_index for s in self.sessions.values()}
            index = next((i for i in CAMERA_RANGE if i not in used_cameras), 0)
            first = next(iter(self.sessions.values()), None)
            self.sessions[sid] = Session(
                id=sid, label=label, camera_index=index,
                profile_id=first.profile_id if first else None,
            )
            self._normalize_sessions()
            self._persist()
            self._changed()
        self.connect_async(sid)
        return sid

    def remove_session(self, session_id: str) -> None:
        with self._lock:
            s = self._session(session_id)
            if len(self.sessions) <= 1:
                raise ServiceError("Debe quedar al menos una sesión.")
            if s.recording:
                raise ServiceError("Para la grabación antes de quitar esta sesión.")
            camera = s.camera
            del self.sessions[session_id]
            self._persist()
            self._changed()
        if camera is not None:
            camera.close()

    def rename_session(self, session_id: str, label: Any) -> None:
        text = " ".join(str(label or "").split())[:MAX_LABEL_LENGTH]
        if not text:
            raise ServiceError("Escribe un nombre para la sesión.")
        with self._lock:
            s = self._session(session_id)
            if any(o.label.lower() == text.lower() for o in self.sessions.values() if o is not s):
                raise ServiceError("Ya hay una sesión con ese nombre.")
            s.label = text
            self._persist()
            self._changed()

    # ================================================================== pabellones
    def create_pavilion(self, name: Any) -> str:
        text = " ".join(str(name or "").split())
        if not text:
            raise ServiceError("Escribe el nombre del pabellón.")
        if len(text) > MAX_LABEL_LENGTH:
            raise ServiceError(f"El nombre del pabellón no puede superar {MAX_LABEL_LENGTH} caracteres.")
        with self._lock:
            if any(str(p.get("name", "")).casefold() == text.casefold() for p in self.settings.pavilions):
                raise ServiceError("Ya existe un pabellón con ese nombre.")
            used = {str(p.get("id")) for p in self.settings.pavilions}
            n = 1
            while f"p{n}" in used:
                n += 1
            pavilion_id = f"p{n}"
            self.settings.pavilions.append({"id": pavilion_id, "name": text})
            self._persist()
            self._changed()
            return pavilion_id

    def rename_pavilion(self, pavilion_id: Any, name: Any) -> None:
        text = " ".join(str(name or "").split())
        if not text:
            raise ServiceError("Escribe el nombre del pabellón.")
        if len(text) > MAX_LABEL_LENGTH:
            raise ServiceError(f"El nombre del pabellón no puede superar {MAX_LABEL_LENGTH} caracteres.")
        with self._lock:
            pavilion = next((p for p in self.settings.pavilions if str(p.get("id")) == str(pavilion_id)), None)
            if pavilion is None:
                raise ServiceError("Ese pabellón ya no existe.")
            if any(p is not pavilion and str(p.get("name", "")).casefold() == text.casefold() for p in self.settings.pavilions):
                raise ServiceError("Ya existe un pabellón con ese nombre.")
            pavilion["name"] = text
            self._persist()
            self._changed()

    def delete_pavilion(self, pavilion_id: Any) -> None:
        with self._lock:
            pavilion = next((p for p in self.settings.pavilions if str(p.get("id")) == str(pavilion_id)), None)
            if pavilion is None:
                raise ServiceError("Ese pabellón ya no existe.")
            self.settings.pavilions = [p for p in self.settings.pavilions if p is not pavilion]
            for session in self.sessions.values():
                if session.pavilion_id == str(pavilion_id):
                    session.pavilion_id = None
            self._persist()
            self._changed()

    def assign_pavilion(self, session_id: Any, pavilion_id: Any) -> None:
        with self._lock:
            session = self._session(session_id)
            if session.recording:
                raise ServiceError("Detén la grabación antes de cambiar el pabellón de una pista.")
            if pavilion_id in (None, "", "none"):
                session.pavilion_id = None
            else:
                pavilion = next((p for p in self.settings.pavilions if str(p.get("id")) == str(pavilion_id)), None)
                if pavilion is None:
                    raise ServiceError("Selecciona un pabellón válido.")
                session.pavilion_id = str(pavilion["id"])
            self._persist()
            self._changed()

    def _session(self, session_id: Any) -> Session:
        s = self.sessions.get(str(session_id))
        if s is None:
            raise ServiceError("Esa sesión ya no existe.")
        return s

    # ================================================================== cámara
    def connect_remote(self, session_id: str) -> None:
        with self._lock:
            s = self._session(session_id)
            if s.recording:
                raise ServiceError("Detén la grabación antes de cambiar la fuente de cámara.")
            old = s.camera
            # The browser pump requests up to 30 actual camera frames per second.
            # This is a tablet-only path; normal camera capture/recording is unchanged.
            camera = RemoteCamera(source="remote_tablet", width=1280, height=720, fps=30)
            camera.open()
            s.camera = camera
            s.camera_device_group = None
            s.camera_index = -1
            s.camera_source = "tablet"
            s.youtube_view_index = None
            s.width, s.height = 1280, 720
            s.generation += 1
            s.error = None
            self._changed()
        if old is not None:
            old.close()

    def push_remote_frame(self, session_id: str, jpeg: bytes) -> None:
        with self._lock:
            s = self._session(session_id)
            camera = s.camera
            if not isinstance(camera, RemoteCamera):
                raise ServiceError("Activa primero la cámara de la tablet en esta sesión.")
        was_connected = camera.connected
        camera.feed_jpeg(jpeg)
        if not was_connected and camera.connected:
            self._changed()

    def connect_youtube(
        self, 
        session_id: str, 
        url: Any, 
        crop_type: Any = "full", 
        camera_name: str | None = None,
        delay_seconds: Any = 10,
        index: Any = None,
    ) -> Camera:
        url = str(url or "").strip()
        if not url.startswith(("https://www.youtube.com/", "https://youtube.com/", "https://youtu.be/", "https://m.youtube.com/")):
            raise ServiceError("Pega un enlace HTTPS válido de YouTube (directo o vídeo).")

        with self._lock:
            s = self._session(session_id)
            if s.recording:
                raise ServiceError("Detén la grabación antes de añadir una nueva fuente de cámara.")
            if s.connecting:
                raise ServiceError("Ya se está conectando una fuente en esta sesión.")
            existing = next(
                (c for c in s.cameras
                 if getattr(c, "source_type", None) == "youtube"
                 and getattr(c, "source_url", None) == url and c.connected),
                None,
            )
            if existing is not None:
                previous = s.camera
                s.camera = existing
                s.camera_source = "youtube"
                s.youtube_view_index = _id(index, "vista de YouTube") if index is not None else None
                s.generation += 1
                s.error = None
                s.connecting = False
                self._changed()
                # Liberar USB/tablet anterior para que no quede una captura huérfana.
                if previous is not None and previous is not existing and getattr(previous, "source_type", "") != "youtube":
                    try:
                        previous.close()
                    except Exception:
                        pass
                return existing
            s.connecting = True
            s.error = None
            self._changed()

        try:
            try:
                import yt_dlp
            except ImportError as exc:
                raise CameraError("Falta el componente de YouTube. Ejecuta: pip install yt-dlp") from exc

            options = {
                "quiet": True,
                "no_warnings": True,
                "noplaylist": True,
                "live_from_start": False,
                "extractor_args": {
                    "youtube": {
                        "player_client": ["android", "web", "mweb"]
                    }
                },
                # Prefer a 720p-or-lower H.264 stream at <=30 FPS. Decoding a 1080p/60
                # AV1/VP9 HLS stream and sending raw BGR frames through a Python pipe is
                # unnecessarily expensive for live recording. Fallbacks cover streams
                # whose format metadata omits codec or FPS.
                "format": (
                    "best[protocol^=m3u8][height<=720][fps<=30][vcodec^=avc1]/"
                    "bestvideo[protocol^=m3u8][height<=720][fps<=30][vcodec^=avc1]/"
                    "best[protocol^=m3u8][height<=720][fps<=30]/"
                    "bestvideo[protocol^=m3u8][height<=720][fps<=30]/"
                    "best[height<=720][fps<=30][vcodec^=avc1]/"
                    "bestvideo[height<=720][fps<=30][vcodec^=avc1]/"
                    "best[height<=720][fps<=30]/bestvideo[height<=720][fps<=30]/"
                    "best[height<=720]/best"
                ),
                "extractor_retries": 5,
                "socket_timeout": 20,
            }

            try:
                with yt_dlp.YoutubeDL(options) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as exc:
                detail = str(exc).strip().splitlines()[-1] if str(exc).strip() else "YouTube no devolvió el vídeo."
                raise CameraError(f"No se pudo leer el directo de YouTube: {detail[:220]}") from exc

            stream_url = info.get("url")
            source_fps = info.get("fps") or 30.0
            if not stream_url:
                formats = [
                    f for f in info.get("formats", [])
                    if f.get("url") and f.get("vcodec") != "none"
                ]
                formats.sort(key=lambda f: (
                    f.get("protocol", "").startswith("m3u8"),
                    f.get("acodec") != "none",
                    f.get("height") or 0,
                    f.get("tbr") or 0,
                ))
                if formats:
                    selected_format = formats[-1]
                    stream_url = selected_format["url"]
                    source_fps = selected_format.get("fps") or source_fps

            if not stream_url:
                raise CameraError("YouTube no ofrece un flujo de vídeo reproducible para este enlace.")

            crop_key = str(crop_type if crop_type is not None else "full").strip().lower()

            crop_presets = {
                "top_left": (0.0, 0.0, 0.5, 0.5),
                "top_right": (0.5, 0.0, 0.5, 0.5),
                "bottom": (0.0, 0.5, 1.0, 0.5),
                "full": None,
                "0": (0.0, 0.0, 0.5, 0.5),
                "1": (0.5, 0.0, 0.5, 0.5),
                "2": (0.0, 0.5, 1.0, 0.5),
            }
            crop = crop_presets.get(crop_key)

            try:
                numeric_source_fps = float(source_fps)
            except (TypeError, ValueError):
                numeric_source_fps = 30.0
            if not 5.0 <= numeric_source_fps <= 60.0:
                numeric_source_fps = 30.0
            # Keep the YouTube processing pipeline bounded: prioritize a real-time
            # 30 FPS output instead of selecting a 60 FPS source that doubles raw-pipe
            # bandwidth. Do not synthesize a higher capture cadence than the source.
            output_fps = max(5, min(30, int(round(numeric_source_fps))))
            camera = YouTubeCamera(
                stream_url,
                width=1280,
                height=720,
                fps=output_fps,
                crop=crop,
                source_fps=numeric_source_fps,
            )
            camera.youtube_delay_seconds = 0.0  # Los directos no tienen retrospectiva local.
            camera.open()

        except Exception as exc:
            message = str(exc) or "No se pudo abrir el flujo de YouTube."
            self._fail_connect(s, message)
            raise ServiceError(message) from exc

        old_to_close = None
        with self._lock:
            if self._closed or s.id not in self.sessions:
                camera.close()
                return

            if not hasattr(s, "cameras") or not isinstance(s.cameras, list):
                s.cameras = []
            s.cameras = [
                c for c in s.cameras
                if getattr(c, "source_type", None) == "youtube" and c.connected
            ]
            used_names = {getattr(c, "name", "") for c in s.cameras}
            n = 1
            while f"CAMYT {n}" in used_names:
                n += 1

            camera.name = f"CAMYT {n}"
            camera.source_type = "youtube"
            camera.source_url = url
            camera.crop_type = crop_key

            previous = s.camera
            if previous is not None and getattr(previous, "source_type", "") != "youtube":
                old_to_close = previous
            s.cameras.append(camera)
            s.camera = camera
            s.camera_source = "youtube"
            # Índice lógico de la fuente online: no modifica camera_index (USB).
            if index is not None:
                try:
                    s.youtube_view_index = _id(index, "vista de YouTube")
                except ServiceError:
                    s.youtube_view_index = None
            else:
                s.youtube_view_index = None
            s.generation += 1
            s.error = None
            s.connecting = False
            self._changed()

        if old_to_close is not None:
            try:
                old_to_close.close()
            except Exception:
                pass
        return camera

    def connect_droidcam_direct(
        self, session_id: str, host: Any, port: Any = 4747, width: Any = 1280, height: Any = 720
    ) -> Camera:
        """Conecta con DroidCam mediante su flujo IP Cam directo (MJPEG HTTP).

        El flujo va del móvil a Python y evita el cliente/driver virtual de Windows.
        Solo se permiten direcciones IP locales explícitas para esta fuente.
        """
        host_text = str(host or "").strip()
        try:
            address = ipaddress.ip_address(host_text)
        except ValueError:
            raise ServiceError("Introduce la dirección IP que muestra DroidCam en el móvil (por ejemplo, 192.168.1.25).") from None
        if not (address.version == 4 and (address.is_private or address.is_loopback or address.is_link_local)):
            raise ServiceError("Por seguridad, la conexión directa solo admite una IP IPv4 local del móvil.")
        try:
            port_num = int(port)
        except (TypeError, ValueError):
            raise ServiceError("El puerto debe ser un número entre 1 y 65535.") from None
        if not 1 <= port_num <= 65535:
            raise ServiceError("El puerto debe ser un número entre 1 y 65535.")
        width_num = _id(width, "ancho")
        height_num = _id(height, "alto")
        if not (160 <= width_num <= 7680 and 120 <= height_num <= 4320):
            raise ServiceError("Resolución no válida.")
        url = f"http://{address.compressed}:{port_num}/video/force/{width_num}x{height_num}"
        device_group = f"droidcam-http:{address.compressed}:{port_num}"

        with self._lock:
            s = self._session(session_id)
            if s.recording:
                raise ServiceError("Detén la grabación antes de cambiar la fuente de cámara.")
            if s.connecting:
                raise ServiceError("Ya se está conectando una fuente en esta sesión.")
            for other in self.sessions.values():
                if other is s or other.camera is None or not other.camera.connected:
                    continue
                if getattr(other.camera, "device_group", None) == device_group:
                    raise ServiceError(f"Ese flujo de DroidCam ya lo está utilizando {other.label}.")
            s.connecting = True
            s.error = None
            old = s.camera
            s.camera = None
            s.camera_source = "droidcam_direct"
            s.camera_device_group = device_group
            s.generation += 1
            self._changed()

        if old is not None:
            try:
                old.close()
            except Exception:
                pass

        try:
            camera = Camera(url, width_num, height_num, self.settings.fps, camera_backend="direct-http")
            camera.open()
        except Exception as exc:
            message = str(exc) or "No se pudo abrir el flujo directo de DroidCam."
            self._fail_connect(s, message)

        with self._lock:
            if self._closed or s.id not in self.sessions:
                camera.close()
                return camera
            camera.name = "DroidCam directo · IP Cam"
            camera.source_type = "droidcam_direct"
            camera.source_url = url
            camera.device_group = device_group
            camera.device_kind = "virtual"
            s.camera = camera
            s.camera_source = "droidcam_direct"
            s.camera_device_group = device_group
            s.error = None
            s.connecting = False
            s.generation += 1
            self._changed()
        return camera

    def connect(
        self,
        session_id: str,
        index: Any = None,
        width: Any = None,
        height: Any = None,
        camera_backend: Any = None,
    ) -> None:
        with self._lock:
            s = self._session(session_id)
            new_index = s.camera_index if index is None else _id(index, "cámara")
            if new_index not in CAMERA_RANGE:
                raise ServiceError("El número de cámara debe estar entre 0 y 9.")
            new_backend = str(camera_backend or s.camera_backend)
            if new_backend not in {"auto", "dshow", "msmf"}:
                raise ServiceError("Selecciona un backend de cámara válido.")
            new_w = _id(width, "ancho", optional=True)
            new_h = _id(height, "alto", optional=True)
            if new_w and new_h and not (160 <= new_w <= 7680 and 120 <= new_h <= 4320):
                raise ServiceError("Resolución no válida.")
            if s.recording:
                raise ServiceError("Para cambiar de cámara, primero para la grabación.")
            if s.connecting:
                raise ServiceError("Ya se está conectando una cámara en esta sesión.")
            device = _select_camera_device(
                self._available_cameras,
                new_index,
                new_backend,
                windows=_is_windows_platform(),
            )
            if device is None:
                # No dejar una cámara anterior activa mientras la sesión muestra otro índice.
                stale = s.camera
                s.camera = None
                s.camera_device_group = None
                s.cameras = [c for c in getattr(s, "cameras", []) if c is not stale and getattr(c, "connected", False)]
                s.camera_index = new_index
                s.camera_source = "usb"
                s.youtube_view_index = None
                s.error = f"La cámara {new_index} no está disponible. Vuelve a buscar cámaras conectadas."
                s.generation += 1
                self._changed()
                if stale is not None:
                    try:
                        stale.close()
                    except Exception:
                        pass
                raise ServiceError(s.error)
            # El catálogo es la autoridad de identidad. En Windows, una cámara enumerada
            # por DirectShow se abre por DirectShow; nunca se cambia a MSMF/CAP_ANY para
            # ese mismo índice, ya que el índice puede referirse a otro dispositivo.
            if _is_windows_platform():
                new_backend = str(device.get("backend") or "dshow")
            selected_group = device.get("device_group", device.get("device_id", f"{new_backend}:{new_index}"))
            for other in self.sessions.values():
                other_busy = other.camera is not None or other.connecting
                other_group = (
                    getattr(other.camera, "device_group", None)
                    if other.camera is not None else other.camera_device_group
                )
                same_endpoint = (
                    other.camera_index == new_index and other.camera_backend == new_backend
                )
                same_logical_device = bool(selected_group and other_group == selected_group)
                if (
                    other is not s
                    and other.camera_source == "usb"
                    and other_busy
                    and (same_endpoint or same_logical_device)
                ):
                    raise ServiceError(
                        f"La fuente {device['name']} ({new_backend.upper()}) ya la está usando {other.label}."
                    )

            s.connecting = True
            s.error = None
            old, s.camera = s.camera, None
            s.cameras = [
                c for c in getattr(s, "cameras", [])
                if getattr(c, "source_type", None) == "youtube" and c is not old and c.connected
            ]
            s.generation += 1
            s.camera_index = new_index
            s.camera_backend = new_backend
            s.camera_device_group = selected_group
            s.camera_source = "usb"
            s.youtube_view_index = None
            if new_w and new_h:
                s.width, s.height = new_w, new_h
            request = (new_index, s.width, s.height, self.settings.fps, new_backend)
            device_name = str(device.get("name") or "")
            prefer_named_dshow = new_backend == "dshow" and device.get("kind") == "virtual"
            self._persist()
            self._changed()

        try:
            if old is not None:
                old.close()
            camera = Camera(
                *request, device_name=device_name, prefer_named_dshow=prefer_named_dshow
            )
            camera.open()
            camera.name = device["name"]
            camera.source_type = "usb"
            camera.device_group = selected_group
            camera.device_kind = device.get("kind", "camera")
        except CameraError as exc:
            self._fail_connect(s, f"{exc} Comprueba el cable y que ninguna otra aplicación la esté usando.")
        except Exception as exc:
            self._fail_connect(s, f"Error inesperado al abrir la cámara: {exc}")
        else:
            with self._lock:
                if self._closed or s.id not in self.sessions:
                    camera.close()
                    return
                s.camera = camera
                # No introducir cámaras USB en el selector de fuentes YouTube.
                if getattr(camera, "source_type", None) == "youtube" and camera not in s.cameras:
                    s.cameras.append(camera)
                s.generation += 1
                s.error = None
                s.connecting = False
                self._changed()

    def connect_async(self, session_id: str) -> None:
        def worker() -> None:
            try:
                self.connect(session_id)
            except ServiceError:
                pass

        threading.Thread(target=worker, name=f"connect-{session_id}", daemon=True).start()

    def switch_active_camera(self, session_id: str, camera_name: str) -> None:
        with self._lock:
            s = self._session(session_id)
            if s.recording:
                raise ServiceError("Para la grabación antes de cambiar de cámara.")
            
            target_cam = next(
                (c for c in getattr(s, "cameras", [])
                 if getattr(c, "source_type", None) == "youtube"
                 and getattr(c, "name", "") == camera_name and c.connected),
                None,
            )
            if not target_cam:
                dead = [
                    c for c in getattr(s, "cameras", [])
                    if getattr(c, "name", "") == camera_name
                    and getattr(c, "source_type", None) == "youtube"
                ]
                s.cameras = [
                    c for c in getattr(s, "cameras", [])
                    if getattr(c, "source_type", None) == "youtube" and c.connected
                ]
                for stale in dead:
                    try:
                        stale.close()
                    except Exception:
                        pass
                raise ServiceError(f"La fuente {camera_name} ya no está activa. Vuelve a conectarla mediante su enlace.")

            s.camera = target_cam
            s.camera_device_group = None
            s.camera_source = getattr(target_cam, "source_type", "usb")
            s.generation += 1
            self._changed()

    def save_retrospective(self, session_id: str, seconds: float = 10.0) -> dict[str, Any]:
        with self._lock:
            s = self._session(session_id)
            camera = s.camera
            if camera is None or not camera.connected:
                raise ServiceError("Conecta una cámara antes de guardar la última jugada.")
            if not getattr(camera, "supports_retrospective", True):
                raise ServiceError("La grabación retrospectiva está deshabilitada para directos de YouTube.")
            profile = self._profile(s.profile_id)
            player = self._find(profile["players"], s.player_id)
            if player is None:
                raise ServiceError("Selecciona un jugador antes de guardar la jugada.")
            exercise = self._find(profile["exercises"], s.exercise_id)
            session_label = s.label
            player_name = player["name"]
            exercise_name = exercise["name"] if exercise else "jugada"
            profile_name = profile["name"]
            
            cam_tag = getattr(camera, "name", "CAMARA").replace(" ", "_")

        total_seconds = float(seconds) + float(getattr(camera, "youtube_delay_seconds", 0.0))

        try:
            path = build_target(
                self.settings.base_dir, 
                player_name,
                f"{exercise_name}_{cam_tag}",
                profile=profile_name if self.settings.group_by_profile else None,
            )
            result = camera.retrospective_clip(path, seconds=min(60.0, total_seconds))
        except (OSError, CameraError) as exc:
            try:
                path.unlink(missing_ok=True)
            except (UnboundLocalError, OSError):
                pass
            raise ServiceError(str(exc)) from None

        with self._lock:
            self._clip_counter += 1
            try:
                rel = path.relative_to(self.settings.base_dir).as_posix()
            except ValueError:
                rel = path.name
            clip = {
                "id": self._clip_counter, "session": session_label, "player": player_name,
                "exercise": exercise_name, "duration": int(round(result["duration"])),
                "name": path.name, "rel": rel, "path": str(path), "retrospective": True,
            }
            self.recent.insert(0, clip)
            del self.recent[MAX_RECENT:]
            self._changed()
            return dict(clip)

    def _fail_connect(self, s: Session, message: str) -> None:
        with self._lock:
            s.connecting = False
            s.camera_device_group = None
            s.error = message
            self._changed()
        raise ServiceError(message)

    # ================================================================== selección
    def select(self, session_id: str, player: Any = _UNSET, exercise: Any = _UNSET) -> None:
        with self._lock:
            s = self._session(session_id)
            if s.recording:
                raise ServiceError("No se puede cambiar la selección mientras se graba.")
            profile = self._profile(s.profile_id)
            if player is not _UNSET:
                pid = _id(player, "jugador")
                if self._find(profile["players"], pid) is None:
                    raise ServiceError("Ese jugador ya no está en el equipo.")
                s.player_id = pid
            if exercise is not _UNSET:
                eid = _id(exercise, "ejercicio", optional=True)
                if eid is not None and self._find(profile["exercises"], eid) is None:
                    raise ServiceError("Ese ejercicio ya no está en el equipo.")
                s.exercise_id = eid
            self._persist()
            self._changed()

    # ================================================================== equipos
    def _profile(self, profile_id: int | None) -> dict[str, Any]:
        for p in self._catalog:
            if p["id"] == profile_id:
                return p
        return self._catalog[0]

    @staticmethod
    def _find(items: list[dict[str, Any]], item_id: int | None) -> dict[str, Any] | None:
        return next((i for i in items if i["id"] == item_id), None)

    def _normalize_sessions(self) -> None:
        known = {p["id"] for p in self._catalog}
        for s in self.sessions.values():
            if s.recording:
                continue
            if s.profile_id not in known:
                s.profile_id = self._catalog[0]["id"]
            profile = self._profile(s.profile_id)
            players = display_players(profile)
            if self._find(players, s.player_id) is None:
                s.player_id = players[0]["id"] if players else None
            if s.exercise_id is not None and self._find(profile["exercises"], s.exercise_id) is None:
                s.exercise_id = None

    def _edit(self, change, *args):
        with self._lock:
            result = change(*args)
            self._catalog = self.db.catalog()
            self._normalize_sessions()
            self._persist()
            self._changed()
            return result

    def _recording_session(self, **match: Any) -> Session | None:
        return next(
            (s for s in self.sessions.values()
             if s.recording and all(getattr(s, k) == v for k, v in match.items())),
            None,
        )

    def create_profile(self, name: Any, session_id: str | None = None) -> int:
        with self._lock:
            pid = self._edit(self.db.create_profile, name, DEFAULT_CATEGORIES, DEFAULT_EXERCISES)
            if session_id:
                self.open_profile(session_id, pid)
            return pid

    def open_profile(self, session_id: str, profile_id: Any) -> None:
        with self._lock:
            s = self._session(session_id)
            pid = _id(profile_id, "equipo")
            if s.recording:
                raise ServiceError("Para la grabación antes de cambiar de equipo.")
            if not any(p["id"] == pid for p in self._catalog):
                raise ServiceError("Ese equipo ya no existe.")
            s.profile_id = pid
            s.player_id = s.exercise_id = None
            self._normalize_sessions()
            self._persist()
            self._changed()

    def rename_profile(self, profile_id: Any, name: Any) -> None:
        self._edit(self.db.rename_profile, _id(profile_id, "equipo"), name)

    def duplicate_profile(self, profile_id: Any, session_id: str | None = None) -> int:
        with self._lock:
            pid = self._edit(self.db.duplicate_profile, _id(profile_id, "equipo"))
            if session_id:
                self.open_profile(session_id, pid)
            return pid

    def delete_profile(self, profile_id: Any) -> None:
        with self._lock:
            pid = _id(profile_id, "equipo")
            busy = self._recording_session(profile_id=pid)
            if busy:
                raise ServiceError(f"{busy.label} está grabando con este equipo. Para la grabación antes de eliminarlo.")
            self._edit(self.db.delete_profile, pid)

    def add_category(self, profile_id: Any, name: Any) -> int:
        return self._edit(self.db.add_category, _id(profile_id, "equipo"), name)

    def remove_category(self, category_id: Any) -> None:
        self._edit(self.db.remove_category, _id(category_id, "categoría"))

    def add_player(self, profile_id: Any, name: Any, category_id: Any = None) -> int:
        return self._edit(self.db.add_player, _id(profile_id, "equipo"), name, _id(category_id, "categoría", optional=True))

    def remove_player(self, player_id: Any) -> None:
        with self._lock:
            pid = _id(player_id, "jugador")
            busy = self._recording_session(player_id=pid)
            if busy:
                raise ServiceError(f"Este jugador está grabando en {busy.label}. Para la grabación antes de quitarlo.")
            self._edit(self.db.remove_player, pid)

    def move_player(self, player_id: Any, category_id: Any) -> None:
        self._edit(self.db.move_player, _id(player_id, "jugador"), _id(category_id, "categoría", optional=True))

    def add_exercise_category(self, profile_id: Any, name: Any) -> int:
        return self._edit(self.db.add_exercise_category, _id(profile_id, "equipo"), name)

    def remove_exercise_category(self, category_id: Any) -> None:
        self._edit(self.db.remove_exercise_category, _id(category_id, "categoría de ejercicios"))

    def add_exercise(self, profile_id: Any, name: Any, category_id: Any = None) -> int:
        return self._edit(self.db.add_exercise, _id(profile_id, "equipo"), name, _id(category_id, "categoría de ejercicios", optional=True))

    def move_exercise(self, exercise_id: Any, category_id: Any) -> None:
        self._edit(self.db.move_exercise, _id(exercise_id, "ejercicio"), _id(category_id, "categoría de ejercicios", optional=True))

    def remove_exercise(self, exercise_id: Any) -> None:
        with self._lock:
            eid = _id(exercise_id, "ejercicio")
            busy = self._recording_session(exercise_id=eid)
            if busy:
                raise ServiceError(f"Este ejercicio se está grabando en {busy.label}. Para la grabación antes de quitarlo.")
            self._edit(self.db.remove_exercise, eid)

    def export_profile(self, profile_id: Any) -> str:
        data = self.db.export_profile(_id(profile_id, "equipo"))
        folder = Path(self.settings.base_dir) / "Equipos"
        path = folder / f"{sanitize(data['name'])}.json"
        try:
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            raise ServiceError(f"No se pudo guardar la copia: {exc}") from None
        return str(path)

    def import_profile(self, path: Any, session_id: str | None = None) -> int:
        file = Path(str(path or "")).expanduser()
        try:
            if not file.is_file() or file.stat().st_size > MAX_IMPORT_BYTES:
                raise ServiceError("Elige un archivo .json de copia de equipo (de menos de 1 MB).")
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise ServiceError("No se pudo leer ese archivo como una copia de equipo.") from None
        with self._lock:
            pid = self._edit(self.db.import_profile, data)
            if session_id:
                self.open_profile(session_id, pid)
            return pid

    # ================================================================== grabación
    def start(self, session_id: str) -> None:
        with self._lock:
            s = self._session(session_id)
            if s.recording:
                raise ServiceError("Ya hay una grabación en curso en esta sesión.")
            self._check_cameras()
            camera = s.camera
            if camera is None or not camera.connected:
                raise ServiceError("Conecta una cámara antes de grabar.")
            profile = self._profile(s.profile_id)
            player = self._find(profile["players"], s.player_id)
            if player is None:
                raise ServiceError("Añade o elige un jugador antes de grabar.")
            other = self._recording_session(player_id=player["id"])
            if other:
                raise ServiceError(f"{player['name']} ya está grabando en {other.label}.")
            exercise = self._find(profile["exercises"], s.exercise_id)

            try:
                path = build_target(
                    self.settings.base_dir, player["name"], exercise["name"] if exercise else None,
                    profile=profile["name"] if self.settings.group_by_profile else None,
                )
            except OSError as exc:
                raise ServiceError(f"No se pudo crear la carpeta de destino: {exc}") from None
            try:
                camera.start_recording(path)
            except CameraError as exc:
                path.unlink(missing_ok=True)
                raise ServiceError(str(exc)) from None

            s.current_path = path
            s.current_meta = {
                "player": player["name"], "exercise": exercise["name"] if exercise else None, "profile": profile["name"],
            }
            s.rec_started = time.monotonic()
            s.recording = True
            self._changed()

    def stop(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            s = self._session(session_id)
            if not s.recording:
                raise ServiceError("No hay ninguna grabación en curso en esta sesión.")
            clip = self._finish_recording_locked(s)
            self._changed()
            if clip is None:
                raise ServiceError("El clip estaba vacío y se descartó. Comprueba la cámara.")
            return dict(clip)

    def cancel(self, session_id: str) -> None:
        with self._lock:
            s = self._session(session_id)
            if not s.recording:
                raise ServiceError("No hay ninguna grabación en curso para cancelar.")
            camera = s.camera
            path = s.current_path
            if camera is not None:
                camera.stop_recording()
            s.recording = False
            s.current_path = None
            s.current_meta = {}
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    raise ServiceError(f"La grabación se canceló, pero no se pudo borrar el archivo temporal: {exc}") from None
            self._changed()

    def _finish_recording_locked(self, s: Session) -> dict[str, Any] | None:
        frames = s.camera.stop_recording() if s.camera is not None else 0
        path, meta = s.current_path, s.current_meta
        duration = time.monotonic() - s.rec_started
        s.recording = False
        s.current_path, s.current_meta = None, {}
        if path is None:
            return None
        if frames == 0:
            path.unlink(missing_ok=True)
            return None
        self._clip_counter += 1
        try:
            rel = path.relative_to(self.settings.base_dir).as_posix()
        except ValueError:
            rel = path.name
        clip = {
            "id": self._clip_counter,
            "session": s.label,
            "player": meta.get("player", ""),
            "exercise": meta.get("exercise"),
            "duration": int(round(duration)),
            "name": path.name,
            "rel": rel,
            "path": str(path),
        }
        self.recent.insert(0, clip)
        del self.recent[MAX_RECENT:]
        return clip

    # ================================================================== carpeta de clips
    def set_base_dir(self, path: Any) -> None:
        """Cambia la carpeta de destino sin tocar la lógica de grabación."""
        with self._lock:
            if any(s.recording for s in self.sessions.values()):
                raise ServiceError("No se puede cambiar la carpeta mientras se graba.")
            try:
                folder = prepare_video_directory(path)
            except ValueError as exc:
                raise ServiceError(str(exc)) from None
            self.settings.base_dir = str(folder)
            self._persist()
            self._changed()

    def open_folder(self, path: Any = None) -> None:
        with self._lock:
            base = Path(self.settings.base_dir)
            target = base
            if path:
                candidate = Path(str(path)).resolve()
                try:
                    candidate.relative_to(base.resolve())
                    target = candidate.parent if candidate.is_file() else candidate
                except ValueError:
                    target = base
            elif self.recent:
                target = Path(self.recent[0]["path"]).parent
        target.mkdir(parents=True, exist_ok=True)
        system = platform.system()
        if system == "Windows":
            os.startfile(target)
        elif system == "Darwin":
            subprocess.Popen(["open", str(target)])
        else:
            subprocess.Popen(["xdg-open", str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # ================================================================== vigilante
    def _monitor_loop(self) -> None:
        ticks = 0
        while not self._stop.wait(0.5):
            try:
                with self._lock:
                    self._check_cameras()
                ticks += 1
                if ticks % 120 == 0:
                    self.clients.prune()
            except Exception:
                traceback.print_exc(file=sys.stderr)

    def _check_cameras(self) -> None:
        with self._lock:
            changed = False
            for s in list(self.sessions.values()):
                camera = s.camera
                # Las fuentes YouTube no seleccionadas también poseen hilos/capturas.
                # Recolectarlas evita que una fuente muerta quede retenida como zombie.
                stale_background = [
                    c for c in getattr(s, "cameras", [])
                    if c is not camera and not c.connected
                ]
                if stale_background:
                    s.cameras = [c for c in s.cameras if all(c is not dead for dead in stale_background)]
                    for dead in stale_background:
                        try:
                            dead.close()
                        except Exception:
                            pass
                    changed = True

                if camera is None or camera.connected:
                    continue
                saved = self._finish_recording_locked(s) if s.recording else None
                s.camera = None
                s.camera_device_group = None
                s.cameras = [c for c in getattr(s, "cameras", []) if c is not camera]
                camera.close()
                s.generation += 1
                s.error = CAMERA_LOST
                self._push_notice("error", f"{s.label}: {CAMERA_LOST}" + (" El clip en curso se guardó." if saved else ""))
                changed = True
            if changed:
                self._changed()

    def _push_notice(self, kind: str, text: str) -> None:
        self._notice_counter += 1
        self._notices.append({"id": self._notice_counter, "kind": kind, "text": text})
        del self._notices[:-MAX_NOTICES]

    # ================================================================== persistencia y cierre
    def _persist(self) -> None:
        self.settings.sessions = [s.to_config() for s in self.sessions.values()]
        self.settings.save()

    def shutdown(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            cameras = []
            for s in self.sessions.values():
                if s.recording:
                    self._finish_recording_locked(s)
                if s.camera is not None:
                    cameras.append(s.camera)
                    s.camera = None
                for c in getattr(s, "cameras", []):
                    if c not in cameras:
                        cameras.append(c)
                s.cameras = []
        self._stop.set()
        for camera in cameras:
            camera.close()
        self._persist()
        self._changed()
        self._monitor.join(timeout=2.0)