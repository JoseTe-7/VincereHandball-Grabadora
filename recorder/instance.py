"""Una sola instancia "anfitriona" por ordenador.

Una cámara y una carpeta de clips no se pueden compartir con seguridad entre dos
procesos: los dos abrirían el mismo dispositivo y escribirían ficheros a la vez.
Por eso el primer proceso en arrancar se queda con un cerrojo del sistema operativo y
se convierte en el anfitrión. Si se abre la aplicación otra vez, ese segundo proceso
no toca ninguna cámara: se conecta al anfitrión y abre otra ventana (por ejemplo, la
pantalla del proyector).

El cerrojo es del propio sistema operativo (`flock` / `LockFileEx`), así que desaparece
solo si el anfitrión se cierra o se cuelga: nunca queda un candado huérfano.
"""
from __future__ import annotations

import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import HOST_INFO_PATH, LOCK_PATH

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl


class InstanceLock:
    def __init__(self, path: Path = LOCK_PATH) -> None:
        self.path = Path(path)
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Intenta ser el anfitrión. No espera: devuelve False si ya hay otro."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            if sys.platform == "win32":
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if sys.platform == "win32":
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


def new_key() -> str:
    return secrets.token_urlsafe(24)


def write_host_info(port: int, operator_key: str, viewer_key: str, path: Path = HOST_INFO_PATH) -> None:
    """Datos para que otra ventana del mismo usuario encuentre al anfitrión. Solo legible por su dueño."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump({"pid": os.getpid(), "port": port, "operator_key": operator_key, "viewer_key": viewer_key}, handle)
    os.replace(tmp, path)


def remove_host_info(path: Path = HOST_INFO_PATH) -> None:
    path.unlink(missing_ok=True)


def read_host_info(path: Path = HOST_INFO_PATH) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    ok = isinstance(data, dict) and isinstance(data.get("port"), int) and all(
        isinstance(data.get(k), str) for k in ("operator_key", "viewer_key")
    )
    return data if ok else None


def host_is_alive(port: int) -> bool:
    request = urllib.request.Request(f"http://127.0.0.1:{port}/api/ping")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            return json.load(response).get("app") == "extremos-grabador"
    except (OSError, ValueError, urllib.error.URLError):
        return False


def wait_for_host(timeout: float = 10.0, path: Path = HOST_INFO_PATH) -> dict[str, Any] | None:
    """Espera a que el anfitrión (que puede estar arrancando) publique sus datos y responda."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = read_host_info(path)
        if info and host_is_alive(info["port"]):
            return info
        time.sleep(0.25)
    return None
