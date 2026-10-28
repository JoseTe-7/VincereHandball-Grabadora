"""Captura de vídeo y grabación con OpenCV.

Sincronización por reloj real (Wall-Clock Sync): garantiza que 1 segundo grabado
corresponda exactamente a 1 segundo de reproducción en cualquier cámara, capturadora
o tablet, independientemente de variaciones de FPS del hardware.
"""
from __future__ import annotations

import platform
import queue
import shutil
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Union

import cv2
import numpy as np

Source = Union[int, str]

MIN_FPS, MAX_FPS = 5.0, 120.0
MAX_CONSECUTIVE_FAILURES = 60
CAMERA_JPEG_QUALITY = 82  # Una sola codificación por fotograma: preview + búfer retrospectivo.


class CameraError(Exception):
    """Error al abrir la cámara o al grabar."""

class _FFmpegDShowCapture:
    """Captura una cámara virtual DirectShow por nombre exacto, no por índice.

    Solo se usa como ruta alternativa para cámaras virtuales conocidas; la ruta
    OpenCV de las cámaras físicas permanece intacta. La cola acotada evita retrasos
    acumulados y el consumidor conserva la cadencia real de los fotogramas recibidos.
    """

    def __init__(self, name: str, width: int, height: int, fps: int):
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        command = [
            executable, "-hide_banner", "-loglevel", "error",
            "-f", "dshow", "-rtbufsize", "8M",
            "-video_size", f"{width}x{height}", "-framerate", str(fps),
            "-i", f"video={name}", "-an", "-sn", "-dn",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL, bufsize=0, creationflags=creationflags,
        )
        self._width, self._height, self._fps = width, height, fps
        self._frame_bytes = width * height * 3
        self._frames: queue.Queue[np.ndarray | None] = queue.Queue(maxsize=2)
        self._reader = threading.Thread(target=self._read_stdout, name="dshow-named-reader", daemon=True)
        self._reader.start()

    def _read_stdout(self) -> None:
        try:
            assert self._process.stdout is not None
            while self._process.poll() is None:
                raw = bytearray()
                while len(raw) < self._frame_bytes:
                    chunk = self._process.stdout.read(self._frame_bytes - len(raw))
                    if not chunk:
                        return
                    raw.extend(chunk)
                frame = np.frombuffer(raw, dtype=np.uint8).reshape(self._height, self._width, 3).copy()
                if self._frames.full():
                    try:
                        self._frames.get_nowait()
                    except queue.Empty:
                        pass
                try:
                    self._frames.put_nowait(frame)
                except queue.Full:
                    pass
        except Exception:
            pass
        finally:
            try:
                self._frames.put_nowait(None)
            except queue.Full:
                pass

    def read(self, timeout: float = 2.0) -> tuple[bool, np.ndarray | None]:
        """Devuelve el fotograma más reciente, descartando los que hayan quedado atrás.

        Para una vista previa en directo importa más mostrar el instante actual que
        reproducir una cola de imágenes antiguas. Esto no modifica la cadencia de
        captura ni los FPS usados por la grabación: solo evita latencia acumulada.
        """
        try:
            frame = self._frames.get(timeout=timeout)
        except queue.Empty:
            return False, None
        if frame is None:
            return False, None
        # Si ya hay más fotogramas esperando, conservar el último disponible.
        while True:
            try:
                newer = self._frames.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                return False, None
            frame = newer
        return True, frame

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._height)
        if prop == cv2.CAP_PROP_FPS:
            return float(self._fps)
        return 0.0

    def release(self) -> None:
        process = getattr(self, "_process", None)
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
        try:
            if process and process.stdout:
                process.stdout.close()
        except Exception:
            pass
        reader = getattr(self, "_reader", None)
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1)


class _FFmpegMaxQualityMp4Writer:
    """Encoder H.264 de cuantización cero para la fuente IP Cam de DroidCam.

    La ruta normal de cámaras conserva cv2.VideoWriter(mp4v). Solo se utiliza para
    la fuente directa de móvil, donde se prioriza conservar el detalle recibido sobre
    el tamaño del archivo. H.264 QP 0 evita pérdida de cuantización del códec; el
    JPEG original del teléfono ya es comprimido y la conversión BGR/YUV puede variar
    ligeramente el color. Los archivos pueden ser muy grandes.
    """

    def __init__(self, path: Path, width: int, height: int, fps: float):
        import imageio_ffmpeg

        executable = imageio_ffmpeg.get_ffmpeg_exe()
        command = [
            executable, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pixel_format", "bgr24",
            "-video_size", f"{width}x{height}", "-framerate", f"{fps:.6f}",
            "-i", "pipe:0", "-an", "-c:v", "libx264", "-preset", "ultrafast",
            "-qp", "0", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            str(path),
        ]
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = subprocess.Popen(
            command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, bufsize=0, creationflags=creationflags,
        )
        self._width, self._height = int(width), int(height)
        time.sleep(0.05)
        if self._process.poll() is not None or self._process.stdin is None:
            raise CameraError(
                "No se pudo iniciar el codificador H.264 de máxima calidad para DroidCam. "
                "Comprueba que el paquete FFmpeg incluido sea compatible."
            )

    def isOpened(self) -> bool:  # noqa: N802 - interfaz compatible con VideoWriter
        return self._process.poll() is None and self._process.stdin is not None

    def write(self, frame: np.ndarray) -> None:
        if not self.isOpened():
            raise CameraError("El codificador de máxima calidad se ha cerrado inesperadamente.")
        if frame.shape[:2] != (self._height, self._width):
            frame = cv2.resize(frame, (self._width, self._height))
        try:
            self._process.stdin.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
        except (BrokenPipeError, OSError) as exc:
            raise CameraError("FFmpeg dejó de aceptar fotogramas durante la grabación DroidCam.") from exc

    def release(self) -> None:
        process = getattr(self, "_process", None)
        if process is None:
            return
        try:
            if process.stdin and not process.stdin.closed:
                process.stdin.close()
        except Exception:
            pass
        try:
            process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


class _DroidCamHttpCapture:
    """Lee directamente el flujo MJPEG HTTP del modo IP Cam de DroidCam.

    No usa el cliente virtual de Windows, DirectShow ni un proceso FFmpeg. El hilo
    lector conserva solamente el JPEG más reciente y descarta fotogramas atrasados.
    """

    def __init__(self, url: str, width: int, height: int, fps: int):
        import urllib.request

        self.url = url
        self._width, self._height, self._fps = int(width), int(height), int(fps)
        self._frames: queue.Queue[tuple[np.ndarray, bytes] | None] = queue.Queue(maxsize=1)
        self._payload_lock = threading.Lock()
        self._last_payload: bytes | None = None
        self._stop = threading.Event()
        self._response = None
        self._error: str | None = None
        request = urllib.request.Request(
            url, headers={"User-Agent": "VinceGraba/2.10", "Accept": "multipart/x-mixed-replace,image/jpeg,*/*"}
        )
        try:
            self._response = urllib.request.urlopen(request, timeout=5.0)
        except Exception as exc:
            raise CameraError(
                "No se pudo abrir el flujo directo de DroidCam. Comprueba que la app del móvil "
                "tenga activa la función IP Cam, que IP y puerto sean correctos y que ambos equipos "
                f"estén en la misma red. Detalle: {type(exc).__name__}: {exc}"
            ) from exc
        status = getattr(self._response, "status", 200)
        if status != 200:
            self._response.close()
            raise CameraError(f"DroidCam respondió con HTTP {status} al solicitar el vídeo directo.")
        self._reader = threading.Thread(target=self._reader_loop, name="droidcam-http-reader", daemon=True)
        self._reader.start()

    def _push_latest(self, frame: np.ndarray, jpeg_payload: bytes) -> None:
        if self._frames.full():
            try:
                self._frames.get_nowait()
            except queue.Empty:
                pass
        try:
            self._frames.put_nowait((frame, jpeg_payload))
        except queue.Full:
            pass

    def _reader_loop(self) -> None:
        import socket
        response = self._response
        pending = bytearray()
        try:
            while not self._stop.is_set():
                try:
                    # read1 evita esperar a llenar un bloque grande: importante en vivo.
                    reader = getattr(response, "read1", response.read)
                    chunk = reader(8192)
                except (TimeoutError, socket.timeout):
                    continue
                if not chunk:
                    break
                pending.extend(chunk)
                latest_jpeg = None
                # Descartar cualquier basura multipart y solo decodificar el JPEG más
                # reciente que haya llegado en este bloque, no toda la cola histórica.
                while True:
                    start = pending.find(b"\xff\xd8")
                    if start < 0:
                        if len(pending) > 2:
                            del pending[:-2]
                        break
                    if start:
                        del pending[:start]
                    end = pending.find(b"\xff\xd9", 2)
                    if end < 0:
                        break
                    latest_jpeg = bytes(pending[:end + 2])
                    del pending[:end + 2]
                if latest_jpeg:
                    encoded = np.frombuffer(latest_jpeg, dtype=np.uint8)
                    frame = cv2.imdecode(encoded, cv2.IMREAD_COLOR)
                    if frame is not None and frame.size:
                        self._push_latest(frame, latest_jpeg)
                # Si el dispositivo deja de enviar marcadores JPEG, acotar memoria.
                if len(pending) > 16 * 1024 * 1024:
                    del pending[:-2]
        except Exception as exc:
            if not self._stop.is_set():
                self._error = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                self._frames.put_nowait(None)
            except queue.Full:
                try:
                    self._frames.get_nowait()
                except queue.Empty:
                    pass
                try:
                    self._frames.put_nowait(None)
                except queue.Full:
                    pass

    def read(self, timeout: float = 0.1) -> tuple[bool, np.ndarray | None]:
        try:
            item = self._frames.get(timeout=timeout)
        except queue.Empty:
            return False, None
        if item is None:
            return False, None
        # Si ya hay un cuadro más nuevo esperando, devolver el más reciente.
        while True:
            try:
                newer = self._frames.get_nowait()
            except queue.Empty:
                break
            if newer is None:
                return False, None
            item = newer
        frame, payload = item
        with self._payload_lock:
            self._last_payload = payload
        return True, frame

    def take_latest_jpeg(self) -> bytes | None:
        """JPEG original asociado al último read(); evita codificarlo otra vez en Python."""
        with self._payload_lock:
            payload, self._last_payload = self._last_payload, None
        return payload

    def get(self, prop: int) -> float:
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self._width)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self._height)
        if prop == cv2.CAP_PROP_FPS:
            return float(self._fps)
        return 0.0

    def release(self) -> None:
        self._stop.set()
        try:
            if self._response is not None:
                self._response.close()
        except Exception:
            pass
        reader = getattr(self, "_reader", None)
        if reader and reader.is_alive() and reader is not threading.current_thread():
            reader.join(timeout=1.0)



def _windows_media_foundation_devices() -> list[dict[str, Any]]:
    """Enumera fuentes de cámara de Media Foundation sin abrirlas ni leer frames.

    Se llama a MFEnumDeviceSources directamente: no se usa CAP_ANY ni se prueba una
    ristra de índices con VideoCapture, porque ambos enfoques pueden asignar a un nombre
    una cámara distinta. Los índices MSMF se mantienen en su propio espacio.
    """
    if platform.system() != "Windows":
        return []

    import ctypes
    import uuid

    HRESULT = ctypes.c_long
    ULONG = ctypes.c_ulong
    UINT32 = ctypes.c_uint32

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32), ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16), ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_text(cls, value: str):
            parsed = uuid.UUID(value)
            raw = parsed.bytes_le
            return cls.from_buffer_copy(raw)

    def failed(hr: int) -> bool:
        return int(hr) < 0

    def vtable_method(pointer, index, restype, argtypes):
        vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        return factory(restype, *argtypes)(vtable[index])

    # GUIDs documentados por Microsoft en mfapi.h.
    source_type_key = GUID.from_text("C60AC5FE-252A-478F-A0EF-BC8FA5F7CAD3")
    video_capture_type = GUID.from_text("8AC3587A-4AE7-42D8-99E0-0A6013EEF90F")
    friendly_name_key = GUID.from_text("60D0E559-52F8-4FA2-BBCE-ACDB34A8EC01")
    symbolic_link_key = GUID.from_text("58F0AAD8-22BF-4F8A-BB3D-D2C4978C6E2F")

    devices: list[dict[str, Any]] = []
    ole32 = ctypes.OleDLL("ole32")
    mfplat = ctypes.WinDLL("mfplat.dll")
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    ole32.CoInitializeEx.restype = HRESULT
    ole32.CoUninitialize.argtypes = []
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    mfplat.MFStartup.argtypes = [UINT32, UINT32]
    mfplat.MFStartup.restype = HRESULT
    mfplat.MFShutdown.argtypes = []
    mfplat.MFShutdown.restype = HRESULT
    mfplat.MFCreateAttributes.argtypes = [ctypes.POINTER(ctypes.c_void_p), UINT32]
    mfplat.MFCreateAttributes.restype = HRESULT
    mfplat.MFEnumDeviceSources.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)), ctypes.POINTER(UINT32)
    ]
    mfplat.MFEnumDeviceSources.restype = HRESULT

    # RPC_E_CHANGED_MODE indica que otro componente ya inicializó COM en ese hilo;
    # COM sigue utilizable y en ese caso no debemos llamar a CoUninitialize.
    co_hr = int(ole32.CoInitializeEx(None, 0x2))  # COINIT_APARTMENTTHREADED
    com_initialized_here = co_hr in (0, 1)
    if failed(co_hr) and co_hr != -2147417850:  # RPC_E_CHANGED_MODE
        return []

    mf_started = False
    attributes = ctypes.c_void_p()
    source_array = ctypes.POINTER(ctypes.c_void_p)()
    source_count = UINT32(0)
    try:
        # MF_VERSION = (MF_SDK_VERSION << 16) | MF_API_VERSION = 0x00020070.
        if failed(mfplat.MFStartup(0x00020070, 0)):
            return []
        mf_started = True
        if failed(mfplat.MFCreateAttributes(ctypes.byref(attributes), 1)):
            return []
        set_guid = vtable_method(
            attributes, 24, HRESULT,
            [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(GUID)],
        )
        if failed(set_guid(attributes, ctypes.byref(source_type_key), ctypes.byref(video_capture_type))):
            return []
        if failed(mfplat.MFEnumDeviceSources(attributes, ctypes.byref(source_array), ctypes.byref(source_count))):
            return []

        for index in range(int(source_count.value)):
            activate = source_array[index]
            if not activate:
                continue
            try:
                get_string = vtable_method(
                    activate, 13, HRESULT,
                    [ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_wchar_p), ctypes.POINTER(UINT32)],
                )
                def read_attribute(key):
                    text_ptr = ctypes.c_wchar_p()
                    length = UINT32(0)
                    hr = get_string(activate, ctypes.byref(key), ctypes.byref(text_ptr), ctypes.byref(length))
                    if failed(hr) or not text_ptr:
                        return ""
                    try:
                        return str(text_ptr.value or "").strip()
                    finally:
                        ole32.CoTaskMemFree(ctypes.cast(text_ptr, ctypes.c_void_p))

                name = read_attribute(friendly_name_key) or f"Cámara Media Foundation {index}"
                symbolic_link = read_attribute(symbolic_link_key)
                devices.append({
                    "index": index,
                    "name": name,
                    "backend": "msmf",
                    "symbolic_link": symbolic_link,
                })
            except Exception:
                # Un dispositivo con metadatos malformados no debe ocultar el resto.
                continue
            finally:
                try:
                    vtable_method(activate, 2, ULONG, [ctypes.c_void_p])(activate)
                except Exception:
                    pass
        return devices
    except Exception:
        # Media Foundation es una ruta adicional: DirectShow seguirá disponible si falla.
        return []
    finally:
        if source_array:
            try:
                ole32.CoTaskMemFree(ctypes.cast(source_array, ctypes.c_void_p))
            except Exception:
                pass
        if attributes:
            try:
                vtable_method(attributes, 2, ULONG, [ctypes.c_void_p])(attributes)
            except Exception:
                pass
        if mf_started:
            try:
                mfplat.MFShutdown()
            except Exception:
                pass
        if com_initialized_here:
            ole32.CoUninitialize()


def _camera_kind(name: str) -> str:
    """Clasificación informativa; nunca se usa para descartar ni renombrar dispositivos."""
    normalized = str(name or "").casefold()
    virtual_markers = (
        "droidcam", "iriun", "camo camera", "epoccam", "obs virtual", "obs camera",
        "manycam", "splitcam", "xsplit", "ndi video", "ivcam", "virtual camera",
        "virtualcam", "phone camera", "phone webcam",
    )
    return "virtual" if any(marker in normalized for marker in virtual_markers) else "camera"


def _camera_group_ids(devices: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Añade IDs estables para endpoints y grupos visuales para una interfaz clara.

    No se presupone que dos dispositivos con el mismo nombre sean el mismo hardware:
    el ID operativo siempre incluye backend e índice. Solo se agrupan visualmente nombres
    que aparecen una sola vez por backend, para ofrecer DirectShow/MSMF como alternativas
    de apertura sin mezclar sus espacios de índices.
    """
    from collections import Counter
    import re
    from unicodedata import normalize

    def key(name: str) -> str:
        text = normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode("ascii")
        return re.sub(r"[^a-z0-9]+", " ", text.casefold()).strip()

    counts = Counter((key(d.get("name", "")), d.get("backend", "")) for d in devices)
    result = []
    for device in devices:
        copy = dict(device)
        backend = str(copy.get("backend") or "auto")
        index = int(copy.get("index", 0))
        name_key = key(copy.get("name", "")) or f"device {backend} {index}"
        copy["device_id"] = f"{backend}:{index}"
        if counts[(name_key, backend)] == 1:
            # Heurística: si el nombre solo aparece una vez por API, se tratan sus
            # endpoints como posibles métodos de apertura del mismo dispositivo. La
            # selección sigue abriendo el endpoint exacto guardado en device_id.
            copy["display_group"] = f"name:{name_key}"
        else:
            # Con nombres repetidos no se puede afirmar que los endpoints sean aliases.
            copy["display_group"] = copy["device_id"]
        copy["device_group"] = copy["display_group"]
        copy["kind"] = _camera_kind(copy.get("name", ""))
        copy["label"] = copy.get("name", "")
        result.append(copy)
    return result


def list_available_cameras() -> list[dict[str, Any]]:
    """Enumera endpoints de vídeo DirectShow y Media Foundation en Windows.

    La enumeración no abre las cámaras ni lee frames. Cada índice conserva el backend
    que lo generó; no se cruzan índices de distintas APIs.
    """
    devices: list[dict[str, Any]] = []

    if platform.system() == "Windows":
        try:
            from pygrabber.dshow_graph import FilterGraph
            graph = FilterGraph()
            dshow_names = graph.get_input_devices()
            for idx, name in enumerate(dshow_names):
                clean_name = str(name or "").strip() or f"Dispositivo DirectShow {idx}"
                devices.append({"index": idx, "name": clean_name, "backend": "dshow"})
        except Exception:
            # Un fallo de pygrabber no debe impedir encontrar cámaras MSMF.
            pass

        # Media Foundation detecta además algunas cámaras virtuales no expuestas a DirectShow.
        try:
            devices.extend(_windows_media_foundation_devices())
        except Exception:
            pass
        return _camera_group_ids(devices)

    for idx in range(6):
        cap = cv2.VideoCapture(idx, cv2.CAP_ANY)
        try:
            if cap.isOpened():
                devices.append({"index": idx, "name": f"Dispositivo de vídeo {idx}", "backend": "auto"})
        finally:
            cap.release()

    return _camera_group_ids(devices)

class Camera:
    def __init__(
        self,
        source: Source = 0,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        camera_backend: str = "auto",
        crop: tuple[float, float, float, float] | None = None,
        device_name: str | None = None,
        prefer_named_dshow: bool = False,
    ):
        self.source = source
        self.camera_backend = camera_backend
        self.device_name = device_name
        self.prefer_named_dshow = bool(prefer_named_dshow)
        self.source_type = "usb"
        self.supports_retrospective = True
        self.crop = crop  # (x, y, width, height) como fracciones de la imagen original
        self._req_width, self._req_height, self._req_fps = width, height, fps

        self._cap: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._lost = False

        self._frame_lock = threading.Lock()
        self._frame: np.ndarray | None = None
        self._frame_id = 0

        self._jpeg_lock = threading.Lock()
        self._jpeg_cache: tuple[int, bytes | None] = (0, None)
        self._fps_timestamps: deque[float] = deque(maxlen=90)
        self._capture_details: dict[str, Any] = {}

        self._writer_lock = threading.Lock()
        self._writer: Any | None = None
        self._frames_written = 0
        self._recording_started_at: float | None = None

        # Búfer para clips retrospectivos
        self._ring_lock = threading.Lock()
        self._ring: deque[tuple[float, bytes]] = deque()
        self._ring_bytes = 0
        self._ring_seconds = 60.0
        self._ring_max_bytes = 512 * 1024 * 1024

        self._buffer_queue: queue.Queue[tuple[int, float, np.ndarray] | None] = queue.Queue(maxsize=2)
        self._buffer_thread: threading.Thread | None = None
        self._buffer_stop = threading.Event()

        self.frame_size: tuple[int, int] = (width, height)
        self._measured_fps = float(fps)

    def open(self) -> None:
        """Abre una cámara física y negocia primero un modo eficiente de captura.

        En Windows se mantiene el backend asociado al inventario (normalmente
        DirectShow); nunca se reintenta el mismo índice con otro backend porque
        el orden de enumeración puede ser distinto. MJPG se solicita primero para
        evitar el ancho de banda de vídeo sin comprimir a 720p/1080p; si el modo
        no entrega fotogramas, se prueba YUY2 y finalmente el formato del driver.
        """
        backend_names = {
            cv2.CAP_DSHOW: "DirectShow",
            cv2.CAP_MSMF: "Media Foundation",
            cv2.CAP_ANY: "automático",
        }
        if platform.system() == "Windows" and isinstance(self.source, int):
            if self.camera_backend == "msmf":
                backend = cv2.CAP_MSMF
            else:
                # Los índices de pygrabber proceden de DirectShow.
                backend = cv2.CAP_DSHOW
            formats: tuple[str | None, ...] = ("MJPG", "YUY2", None)
        else:
            backend = cv2.CAP_ANY
            formats = (None,)

        cap = None
        frame = None
        attempted: list[str] = []
        capture_backend_label = backend_names.get(backend, str(backend))

        # Ruta realmente directa desde el teléfono: el flujo HTTP MJPEG del modo IP Cam
        # llega a Python sin cliente de cámara virtual, DirectShow ni FFmpeg.
        is_droidcam_http = isinstance(self.source, str) and self.source.lower().startswith("http://")
        if is_droidcam_http:
            formats = ()  # No recurrir silenciosamente al backend de cámara virtual.
            try:
                direct_cap = _DroidCamHttpCapture(
                    self.source, self._req_width, self._req_height, self._req_fps
                )
                direct_ok, direct_frame = direct_cap.read(timeout=8.0)
                if direct_ok and direct_frame is not None:
                    cap, frame = direct_cap, direct_frame
                    capture_backend_label = "DroidCam HTTP directo (MJPEG)"
                else:
                    direct_cap.release()
                    attempted.append("flujo IP Cam: conexión abierta, pero no llegaron fotogramas JPEG en 8 s")
            except Exception as exc:
                attempted.append(f"flujo IP Cam: {type(exc).__name__}: {exc}")

        # Los índices de pygrabber y los que usa OpenCV no siempre coinciden en
        # instalaciones con varias cámaras virtuales. Para DroidCam/Iriun y otras
        # cámaras virtuales conocidas, intenta primero DirectShow por nombre exacto.
        # Si FFmpeg no puede abrirla o no entrega imagen, conserva la ruta OpenCV.
        if (
            platform.system() == "Windows"
            and self.prefer_named_dshow
            and self.camera_backend == "dshow"
            and self.device_name
        ):
            # La ruta por nombre es la única válida para este endpoint virtual;
            # evita un fallback silencioso a otro dispositivo por índice.
            formats = ()
            named_cap = None
            try:
                named_cap = _FFmpegDShowCapture(
                    self.device_name, self._req_width, self._req_height, self._req_fps
                )
                named_ok, named_frame = named_cap.read(timeout=5.0)
                # No volver a un índice OpenCV si la apertura por nombre falla: podría
                # abrir otra cámara con el mismo índice y ocultar el problema real.
                formats = ()
                if named_ok and named_frame is not None:
                    cap, frame = named_cap, named_frame
                    capture_backend_label = "FFmpeg DirectShow (nombre exacto)"
                else:
                    named_cap.release()
                    attempted.append(f"DirectShow por nombre '{self.device_name}': no llegaron fotogramas en 5 s")
            except Exception as exc:
                if named_cap is not None:
                    try:
                        named_cap.release()
                    except Exception:
                        pass
                attempted.append(f"DirectShow por nombre: {type(exc).__name__}: {exc}")

        for fourcc_name in (() if cap is not None or is_droidcam_http else formats):
            candidate = cv2.VideoCapture(self.source, backend)
            if not candidate.isOpened():
                candidate.release()
                attempted.append(f"{fourcc_name or 'formato del driver'}: no se pudo abrir")
                # Si ni siquiera se puede abrir el dispositivo, cambiar el FOURCC
                # no lo arregla; evita abrir y cerrar el mismo índice a ciegas.
                break

            requested_fourcc = None
            if isinstance(self.source, int):
                if fourcc_name:
                    requested_fourcc = cv2.VideoWriter_fourcc(*fourcc_name)
                    accepted = candidate.set(cv2.CAP_PROP_FOURCC, requested_fourcc)
                    actual_fourcc = int(candidate.get(cv2.CAP_PROP_FOURCC) or 0)
                    if accepted is False and actual_fourcc != requested_fourcc:
                        candidate.release()
                        attempted.append(f"{fourcc_name}: el driver rechazó el formato")
                        continue
                # El orden importa en muchos controladores: formato, resolución y FPS.
                candidate.set(cv2.CAP_PROP_FRAME_WIDTH, self._req_width)
                candidate.set(cv2.CAP_PROP_FRAME_HEIGHT, self._req_height)
                candidate.set(cv2.CAP_PROP_FPS, self._req_fps)
                # Una cola de captura grande añade latencia y deja que se acumulen
                # imágenes antiguas si la aplicación trabaja más despacio.
                candidate.set(cv2.CAP_PROP_BUFFERSIZE, 1)

            ok, candidate_frame = False, None
            probe_times: list[float] = []
            # Medimos unos frames reales antes de decidir el formato. Pedir 30 FPS
            # al driver no significa que haya aceptado 30; esto detecta modos lentos.
            probe_frames = max(8, min(20, int(max(5, self._req_fps) * 0.4)))
            for _ in range(probe_frames):
                ok, candidate_frame = candidate.read()
                if not ok or candidate_frame is None:
                    break
                probe_times.append(time.perf_counter())
            probe_fps = 0.0
            if len(probe_times) >= 2 and probe_times[-1] > probe_times[0]:
                probe_fps = (len(probe_times) - 1) / (probe_times[-1] - probe_times[0])
            threshold_fps = max(5.0, float(self._req_fps) * 0.90)
            if ok and candidate_frame is not None and (
                fourcc_name is None or probe_fps >= threshold_fps
            ):
                cap, frame = candidate, candidate_frame
                self._capture_probe_fps = probe_fps
                break

            candidate.release()
            if candidate_frame is None or not ok:
                attempted.append(f"{fourcc_name or 'formato del driver'}: no llegaron frames")
            else:
                attempted.append(
                    f"{fourcc_name or 'formato del driver'}: {probe_fps:.1f} FPS "
                    f"(se esperaban al menos {threshold_fps:.1f})"
                )

        if cap is None or frame is None:
            attempted_names = "; ".join(attempted)
            if is_droidcam_http:
                raise CameraError(
                    "No se pudo recibir vídeo de DroidCam directo. Comprueba que la app del móvil "
                    "ofrezca la función IP Cam, que la IP y el puerto coincidan con los mostrados en el móvil "
                    "y que el PC pueda abrir esa dirección en un navegador. " + (attempted_names or "")
                )
            if attempted_names:
                device_hint = (
                    f" El dispositivo '{self.device_name}' está instalado, pero no entrega vídeo. "
                    "Abre el cliente de DroidCam/Iriun en Windows, conecta el móvil y comprueba "
                    "que no esté usando la cámara otra aplicación."
                    if self.prefer_named_dshow and self.device_name else ""
                )
                raise CameraError(
                    f"No se pudo abrir la fuente {self.source} ni obtener imagen "
                    f"({attempted_names}).{device_hint}"
                )
            raise CameraError(f"No se pudo abrir la fuente {self.source}.")

        frame = self._apply_crop(frame)
        self.frame_size = (frame.shape[1], frame.shape[0])
        reported_fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if MIN_FPS <= reported_fps <= MAX_FPS:
            self._measured_fps = reported_fps
        else:
            self._measured_fps = float(self._req_fps)
        actual_fourcc_value = int(cap.get(cv2.CAP_PROP_FOURCC) or 0)
        actual_fourcc = "".join(chr((actual_fourcc_value >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")
        self._capture_details = {
            "backend": capture_backend_label,
            "fourcc": actual_fourcc or "driver/default",
            "requested_width": self._req_width,
            "requested_height": self._req_height,
            "requested_fps": self._req_fps,
            "actual_width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH) or frame.shape[1]),
            "actual_height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT) or frame.shape[0]),
            "driver_fps": round(reported_fps, 2) if reported_fps > 0 else None,
            "probe_fps": round(float(getattr(self, "_capture_probe_fps", 0.0)), 2),
        }
        self._fps_timestamps.clear()
        now = time.monotonic()
        self._fps_timestamps.append(now)
        with self._frame_lock:
            self._frame = frame
            self._frame_id += 1

        self._cap = cap
        self._running = True
        self._lost = False
        self._start_buffer_worker()
        self._thread = threading.Thread(target=self._capture_loop, name="camera-capture", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self.stop_recording()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        self._stop_buffer_worker()
        if self._cap is not None:
            self._cap.release()
            self._cap = None

    @property
    def connected(self) -> bool:
        return self._running and not self._lost

    @property
    def fps(self) -> float:
        return max(0.0, min(MAX_FPS, self._measured_fps))

    @property
    def recording(self) -> bool:
        with self._writer_lock:
            return self._writer is not None

    def _update_fps_measurement(self, timestamp: float) -> None:
        """Actual receive FPS over a short rolling window, not a driver guess."""
        self._fps_timestamps.append(timestamp)
        while len(self._fps_timestamps) > 2 and timestamp - self._fps_timestamps[0] > 1.5:
            self._fps_timestamps.popleft()
        if len(self._fps_timestamps) >= 2:
            span = self._fps_timestamps[-1] - self._fps_timestamps[0]
            if span > 0:
                self._measured_fps = max(
                    0.0, min(MAX_FPS, (len(self._fps_timestamps) - 1) / span)
                )

    def latest_frame(self) -> np.ndarray | None:
        with self._frame_lock:
            return self._frame

    @property
    def frame_id(self) -> int:
        return self._frame_id

    def preview_jpeg(self, max_width: int = 1280, quality: int = CAMERA_JPEG_QUALITY) -> tuple[int, bytes | None]:
        """Devuelve el JPEG ya codificado por el trabajador de búfer si existe.

        Para cámaras USB/tablet, el mismo JPEG alimenta la vista previa y el
        búfer retrospectivo. Así no se hacen dos compresiones por fotograma.
        YouTube no usa el búfer local, por lo que conserva aquí su codificación
        perezosa de la última imagen.
        """
        worker = self._buffer_thread
        if worker is not None and worker.is_alive():
            with self._jpeg_lock:
                return self._jpeg_cache

        with self._frame_lock:
            frame_id, frame = self._frame_id, self._frame
        with self._jpeg_lock:
            if self._jpeg_cache[0] == frame_id or frame is None:
                return self._jpeg_cache
            h, w = frame.shape[:2]
            if max_width > 0 and w > max_width:
                frame = cv2.resize(frame, (max_width, int(h * max_width / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
            self._jpeg_cache = (frame_id, buf.tobytes() if ok else None)
            return self._jpeg_cache

    def start_recording(self, path: Path) -> None:
        if not self.connected:
            raise CameraError("No hay cámara conectada.")
        
        target_fps = max(MIN_FPS, min(MAX_FPS, float(self._req_fps)))
        if self.source_type == "droidcam_direct":
            # Solo DroidCam IP Cam usa H.264 QP 0; las cámaras físicas siguen con
            # la ruta de grabación estable de v2.6 sin cambios.
            writer = _FFmpegMaxQualityMp4Writer(
                path, self.frame_size[0], self.frame_size[1], target_fps
            )
        else:
            writer = cv2.VideoWriter(
                str(path), cv2.VideoWriter_fourcc(*"mp4v"), target_fps, self.frame_size
            )

        if not writer.isOpened():
            writer.release()
            raise CameraError(f"No se pudo crear el archivo de vídeo:\n{path}")
            
        with self._writer_lock:
            if self._writer is not None:
                writer.release()
                raise CameraError("Ya hay una grabación en curso.")
            self._frames_written = 0
            self._recording_started_at = time.monotonic()
            self._writer = writer

    def stop_recording(self) -> int:
        with self._writer_lock:
            writer, self._writer = self._writer, None
            frames = self._frames_written
            self._recording_started_at = None
        if writer is not None:
            writer.release()
        return frames

    def _write_frame_time_synced(self, frame: np.ndarray) -> None:
        """Sincroniza el vídeo con el reloj real del ordenador para evitar aceleraciones."""
        with self._writer_lock:
            if self._writer is None or self._recording_started_at is None:
                return

            target_fps = max(MIN_FPS, min(MAX_FPS, float(self._req_fps)))
            elapsed = max(0.0, time.monotonic() - self._recording_started_at)
            expected_frames = int(elapsed * target_fps)

            if expected_frames <= self._frames_written:
                return

            if (frame.shape[1], frame.shape[0]) != self.frame_size:
                frame = cv2.resize(frame, self.frame_size)

            while self._frames_written < expected_frames:
                self._writer.write(frame)
                self._frames_written += 1

    def retrospective_clip(self, path: Path, seconds: float = 10.0) -> dict[str, int]:
        cutoff = time.monotonic() - max(0.1, min(float(seconds), self._ring_seconds))
        with self._ring_lock:
            selected = [(ts, payload) for ts, payload in self._ring if ts >= cutoff]
            frames = [payload for _, payload in selected]
        if len(frames) < 2:
            raise CameraError("Aún no hay suficientes fotogramas en el búfer; espera unos segundos.")
        first = cv2.imdecode(np.frombuffer(frames[0], dtype=np.uint8), cv2.IMREAD_COLOR)
        if first is None:
            raise CameraError("No se pudo decodificar el búfer de vídeo.")
        h, w = first.shape[:2]
        span = max(0.1, selected[-1][0] - selected[0][0])
        fps = max(MIN_FPS, min(MAX_FPS, (len(frames) - 1) / span))
        writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), fps, (w, h))
        if not writer.isOpened():
            writer.release()
            raise CameraError(f"No se pudo crear el clip retrospectivo: {path}")
        written = 0
        try:
            for payload in frames:
                frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
                if frame is not None:
                    if (frame.shape[1], frame.shape[0]) != (w, h):
                        frame = cv2.resize(frame, (w, h))
                    writer.write(frame)
                    written += 1
        finally:
            writer.release()
        if written < 2:
            path.unlink(missing_ok=True)
            raise CameraError("El búfer no contenía suficientes fotogramas válidos.")
        return {"frames": written, "duration": round(written / fps, 2)}

    def _start_buffer_worker(self) -> None:
        self._buffer_stop.clear()
        if self._buffer_thread is None or not self._buffer_thread.is_alive():
            self._buffer_thread = threading.Thread(
                target=self._buffer_loop, name="camera-retrospective-buffer", daemon=True
            )
            self._buffer_thread.start()

    def _stop_buffer_worker(self) -> None:
        self._buffer_stop.set()
        try:
            self._buffer_queue.put_nowait(None)
        except queue.Full:
            try:
                self._buffer_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._buffer_queue.put_nowait(None)
            except queue.Full:
                pass
        if self._buffer_thread is not None:
            self._buffer_thread.join(timeout=2.0)
            self._buffer_thread = None
        while not self._buffer_queue.empty():
            try:
                self._buffer_queue.get_nowait()
            except queue.Empty:
                break

    def _buffer_frame(
        self, frame: np.ndarray, frame_id: int | None = None, captured_at: float | None = None
    ) -> None:
        item = (self.frame_id if frame_id is None else frame_id,
                time.monotonic() if captured_at is None else captured_at, frame)
        try:
            self._buffer_queue.put_nowait(item)
        except queue.Full:
            # Captura nunca espera a JPEG: conservamos los frames más recientes para
            # que el preview sea en directo si la codificación va momentáneamente lenta.
            try:
                self._buffer_queue.get_nowait()
            except queue.Empty:
                pass
            try:
                self._buffer_queue.put_nowait(item)
            except queue.Full:
                pass

    def _buffer_encoded_jpeg(self, frame_id: int, captured_at: float, payload: bytes) -> None:
        """Actualiza preview y retrospectiva con JPEG ya comprimido por la fuente MJPEG."""
        with self._jpeg_lock:
            if frame_id >= self._jpeg_cache[0]:
                self._jpeg_cache = (frame_id, payload)
        with self._ring_lock:
            self._ring.append((captured_at, payload))
            self._ring_bytes += len(payload)
            while self._ring and (
                captured_at - self._ring[0][0] > self._ring_seconds
                or self._ring_bytes > self._ring_max_bytes
            ):
                _, old = self._ring.popleft()
                self._ring_bytes -= len(old)

    def _buffer_loop(self) -> None:
        while not self._buffer_stop.is_set():
            try:
                item = self._buffer_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if item is None:
                break
            frame_id, captured_at, frame = item
            ok, encoded = cv2.imencode(
                ".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, CAMERA_JPEG_QUALITY]
            )
            if not ok:
                continue
            payload = encoded.tobytes()
            # La vista previa y la retrospectiva reutilizan exactamente el mismo JPEG.
            with self._jpeg_lock:
                if frame_id >= self._jpeg_cache[0]:
                    self._jpeg_cache = (frame_id, payload)
            with self._ring_lock:
                self._ring.append((captured_at, payload))
                self._ring_bytes += len(payload)
                while self._ring and (
                    captured_at - self._ring[0][0] > self._ring_seconds
                    or self._ring_bytes > self._ring_max_bytes
                ):
                    _, old = self._ring.popleft()
                    self._ring_bytes -= len(old)

    def _apply_crop(self, frame: np.ndarray) -> np.ndarray:
        if not self.crop:
            return frame
        h, w = frame.shape[:2]
        x, y, cw, ch = self.crop
        x1, y1 = int(w*x), int(h*y)
        x2, y2 = int(w*(x+cw)), int(h*(y+ch))
        x1, y1 = max(0, min(w-1, x1)), max(0, min(h-1, y1))
        x2, y2 = max(x1+1, min(w, x2)), max(y1+1, min(h, y2))
        return frame[y1:y2, x1:x2].copy()

    def _capture_loop(self) -> None:
        assert self._cap is not None
        failures = 0

        while self._running:
            ok, frame = self._cap.read()
            if not ok or frame is None:
                failures += 1
                if failures >= MAX_CONSECUTIVE_FAILURES:
                    self._lost = True
                    break
                time.sleep(0.01)
                continue
            failures = 0

            now = time.monotonic()
            # FPS medidos sobre una ventana real; no usar el número del driver ni
            # un promedio lento que esconda caídas durante varios segundos.
            self._update_fps_measurement(now)

            frame = self._apply_crop(frame)

            with self._frame_lock:
                self._frame = frame
                self._frame_id += 1
                frame_id = self._frame_id

            captured_jpeg = None
            take_jpeg = getattr(self._cap, "take_latest_jpeg", None)
            if callable(take_jpeg):
                captured_jpeg = take_jpeg()
            if captured_jpeg is not None and self.crop is None:
                # En la ruta IP Cam el JPEG ya viene comprimido desde el teléfono;
                # conservarlo evita una segunda compresión para vista previa/retrospectiva.
                self._buffer_encoded_jpeg(frame_id, now, captured_jpeg)
            else:
                self._buffer_frame(frame, frame_id=frame_id, captured_at=now)
            self._write_frame_time_synced(frame)


class YouTubeCamera(Camera):
    """Reproduce una fuente de YouTube con un reloj de presentación estable.

    FFmpeg decodifica a una cadencia CFR y entrega frames a una cola acotada.
    Un hilo independiente presenta los frames al ritmo del reloj local. Si la red
    se atasca, el reproductor espera/rebufferiza: no intenta recuperar el retraso
    acelerando los frames. La cola ejerce contrapresión sobre FFmpeg y evita que
    se acumule una cola ilimitada de vídeo atrasado.
    """

    _PLAYOUT_BUFFER_SECONDS = 2.0

    def __init__(
        self,
        stream_url: str,
        width: int = 1280,
        height: int = 720,
        fps: int = 30,
        crop=None,
        source_fps: float = 30.0,
    ):
        super().__init__(stream_url, width=width, height=height, fps=fps, crop=crop)
        try:
            measured_source_fps = float(source_fps)
        except (TypeError, ValueError):
            measured_source_fps = 30.0
        if not MIN_FPS <= measured_source_fps <= MAX_FPS:
            measured_source_fps = 30.0
        self._source_fps = measured_source_fps
        self.source_type = "youtube"
        self.supports_retrospective = False
        self._ffmpeg: subprocess.Popen | None = None
        self._ffmpeg_lock = threading.Lock()
        self._reader_thread: threading.Thread | None = None
        self._reader_done = threading.Event()
        self._playout_stop = threading.Event()
        queue_frames = max(4, int(max(1, fps) * self._PLAYOUT_BUFFER_SECONDS))
        self._live_queue: queue.Queue[bytes] = queue.Queue(maxsize=queue_frames)

    def open(self) -> None:
        if self._running:
            if self.latest_frame() is not None:
                return
            raise CameraError("La conexión de YouTube ya está iniciada.")

        executable = shutil.which("ffmpeg")
        if not executable:
            try:
                import imageio_ffmpeg
                executable = imageio_ffmpeg.get_ffmpeg_exe()
            except Exception:
                executable = None
        if not executable:
            raise CameraError(
                "No se encuentra FFmpeg. Reinstala las dependencias de VinceGraba "
                "para reproducir directos de YouTube."
            )

        width, height = self._req_width, self._req_height
        fps = max(1, int(self._req_fps))
        # No usar -fflags nobuffer / -flags low_delay ni vf=realtime aquí. El
        # directo HLS/DASH necesita su buffer de demux normal; el filtro realtime
        # reinicia su referencia temporal ante discontinuidades y puede liberar
        # frames en ráfaga. El reloj de salida lo controla el hilo de playout.
        command = [
            executable, "-hide_banner", "-loglevel", "error",
            "-i", str(self.source), "-an", "-sn", "-dn",
            "-vf",
            f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            # Normalizar las PTS usando el FPS real del formato evita que los
            # saltos de timestamps entre segmentos HLS se conviertan en segundos
            # de frames duplicados o que FFmpeg intente recuperar el retraso.
            f"setpts=N/({self._source_fps:g}*TB),fps={fps}",
            "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        # The output is raw BGR (width * height * 3 bytes per frame). An unbuffered
        # pipe makes Python service hundreds of tiny OS reads per 720p frame, and that
        # overhead can collapse live playback to single-digit FPS. BufferedReader
        # performs those reads in C and returns complete frame-sized chunks.
        frame_bytes = width * height * 3
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=max(64 * 1024, frame_bytes),
            )
        except OSError as exc:
            raise CameraError(f"No se pudo iniciar FFmpeg: {exc}") from exc
        if process.stdout is None:
            process.kill()
            raise CameraError("FFmpeg no ha creado la salida de vídeo.")

        self._ffmpeg = process
        self._running = True
        self._lost = False
        self._reader_done.clear()
        self._playout_stop.clear()
        self._live_queue = queue.Queue(
            maxsize=max(4, int(max(1, fps) * self._PLAYOUT_BUFFER_SECONDS))
        )
        self.frame_size = (width, height)
        self._measured_fps = float(fps)
        self._reader_thread = threading.Thread(
            target=self._ffmpeg_reader_loop, name="youtube-live-reader", daemon=True
        )
        self._thread = threading.Thread(
            target=self._ffmpeg_capture_loop, name="youtube-live-playout", daemon=True
        )
        self._reader_thread.start()
        self._thread.start()

        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            if self.latest_frame() is not None:
                return
            if self._reader_done.is_set() and self._live_queue.empty():
                self.close()
                raise CameraError(
                    "FFmpeg no pudo recibir fotogramas del directo de YouTube. "
                    "Comprueba el enlace y que el directo siga activo."
                )
            time.sleep(0.05)

        self.close()
        raise CameraError("YouTube no entregó fotogramas en 15 segundos.")

    @staticmethod
    def _read_exact(stream, size: int) -> bytes:
        chunks = bytearray()
        while len(chunks) < size:
            chunk = stream.read(size - len(chunks))
            if not chunk:
                return b""
            chunks.extend(chunk)
        return bytes(chunks)

    def _ffmpeg_reader_loop(self) -> None:
        """Drena la tubería de FFmpeg sin imponer un reloj en el hilo lector.

        La cola es acotada y ``put`` espera cuando está llena: así no se descartan
        frames silenciosamente ni se permite una cola ilimitada tras una ráfaga.
        """
        process = self._ffmpeg
        try:
            if process is None or process.stdout is None:
                return
            width, height = self._req_width, self._req_height
            frame_bytes = width * height * 3
            while self._running and not self._playout_stop.is_set():
                # Leer hasta EOF incluso si el proceso ya terminó: la tubería aún
                # puede contener frames finales que deben llegar a la cola.
                payload = self._read_exact(process.stdout, frame_bytes)
                if not payload:
                    break
                while self._running and not self._playout_stop.is_set():
                    try:
                        self._live_queue.put(payload, timeout=0.1)
                        break
                    except queue.Full:
                        # Backpressure: no se desechan frames ni se consume la
                        # entrada más rápido de lo que el reloj de presentación permite.
                        continue
        finally:
            self._reader_done.set()

    def _publish_decoded_frame(self, payload: bytes) -> None:
        width, height = self._req_width, self._req_height
        frame = np.frombuffer(payload, dtype=np.uint8).reshape((height, width, 3)).copy()
        frame = self._apply_crop(frame)
        self.frame_size = (frame.shape[1], frame.shape[0])
        # Unlike the requested/output FPS, this measurement reflects the frames that
        # actually arrive at the Python publication stage.
        self._update_fps_measurement(time.monotonic())
        with self._frame_lock:
            self._frame = frame
            self._frame_id += 1
        self._write_frame_time_synced(frame)

    def _ffmpeg_capture_loop(self) -> None:
        """Presenta a FPS constantes; tras una interrupción, reancla el reloj.

        El primer frame tras un vacío puede mostrarse inmediatamente. Los frames
        siguientes se espacian a 1/FPS; nunca se reproducen varios frames seguidos
        para intentar compensar el tiempo que la red estuvo detenida.
        """
        period = 1.0 / max(1.0, float(self._req_fps))
        next_due: float | None = None
        try:
            while self._running and not self._playout_stop.is_set():
                try:
                    payload = self._live_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._reader_done.is_set() and self._live_queue.empty():
                        break
                    continue

                now = time.monotonic()
                # No reproducir a ráfagas si el hilo se retrasó o la fuente se
                # quedó sin datos: se establece un nuevo origen temporal.
                if next_due is None or now - next_due > period * 1.5:
                    next_due = now
                delay = next_due - now
                if delay > 0 and self._playout_stop.wait(delay):
                    break
                if not self._running or self._playout_stop.is_set():
                    break

                self._publish_decoded_frame(payload)
                presented_at = time.monotonic()
                next_due = max(next_due, presented_at) + period
        finally:
            # Solo declarar caída cuando el decodificador agotó el flujo; una
            # espera temporal por red deja el reproductor conectado y en buffering.
            if self._reader_done.is_set() and self._live_queue.empty():
                self._lost = True
                self._running = False

    def close(self) -> None:
        self.stop_recording()
        self._running = False
        self._playout_stop.set()
        process, self._ffmpeg = self._ffmpeg, None
        if process is not None:
            try:
                process.terminate()
                process.wait(timeout=2.0)
            except Exception:
                try:
                    process.kill()
                    process.wait(timeout=1.0)
                except Exception:
                    pass
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass

        reader = self._reader_thread
        presenter = self._thread
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.0)
        if process is not None:
            try:
                if process.stdout is not None:
                    process.stdout.close()
            except Exception:
                pass
        for worker in (reader, presenter):
            if worker is not None and worker is not threading.current_thread():
                worker.join(timeout=2.0)
        self._reader_thread = None
        self._thread = None
        self._reader_done.set()
        while True:
            try:
                self._live_queue.get_nowait()
            except queue.Empty:
                break
        with self._ring_lock:
            self._ring.clear()
            self._ring_bytes = 0

    def _buffer_frame(self, frame: np.ndarray) -> None:
        # YouTube ya viene retrasado desde el emisor y no admite pregrabación local.
        return

    def retrospective_clip(self, path: Path, seconds: float = 10.0) -> dict[str, int]:
        raise CameraError("La grabación retrospectiva no está disponible para directos de YouTube.")


class RemoteCamera(Camera):
    """Adaptador de cámara alimentado por fotogramas JPEG desde un navegador de tablet."""
    def open(self) -> None:
        self._running = True
        self._lost = False
        self._cap = None
        self._thread = None
        # Tablet frames arrive as JPEG already; no extra JPEG worker is needed.
        self._buffer_thread = None
        self._buffer_stop.clear()

    def feed_jpeg(self, payload: bytes) -> None:
        if not self._running or len(payload) > 2_000_000:
            return
        frame = cv2.imdecode(np.frombuffer(payload, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            return
        self.frame_size = (frame.shape[1], frame.shape[0])
        now = time.monotonic()
        self._update_fps_measurement(now)
        self._remote_last = now
        with self._frame_lock:
            self._frame = frame
            self._frame_id += 1
            frame_id = self._frame_id
        # The browser already encoded this frame as JPEG. Reuse that exact payload
        # for preview and retrospective storage instead of doing another JPEG encode
        # on the host CPU for every tablet frame.
        self._buffer_encoded_jpeg(frame_id, now, payload)
        self._write_frame_time_synced(frame)

    @property
    def connected(self) -> bool:
        last = getattr(self, "_remote_last", 0.0)
        return self._running and (time.perf_counter() - last < 5.0)

    def close(self) -> None:
        self.stop_recording()
        self._running = False
        self._stop_buffer_worker()