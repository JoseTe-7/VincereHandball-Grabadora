"""Gestión de la carpeta de destino de vídeos. No interviene en la captura ni en la codificación."""
from __future__ import annotations

import os
from pathlib import Path


def default_video_directory() -> Path:
    """Carpeta predeterminada de clips del usuario."""
    return Path.home() / "Videos" / "Entrenamientos_Extremos"


def prepare_video_directory(value: str | Path) -> Path:
    """Crea y valida una carpeta elegida para guardar vídeos."""
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Selecciona una carpeta para guardar los vídeos.")
    folder = Path(raw).expanduser()
    try:
        folder.mkdir(parents=True, exist_ok=True)
        resolved = folder.resolve()
        if not resolved.is_dir():
            raise OSError("La ruta no es una carpeta.")
        if not os.access(resolved, os.W_OK):
            raise PermissionError("No hay permiso de escritura en esa carpeta.")
    except OSError as exc:
        raise ValueError(f"No se pudo usar la carpeta seleccionada: {exc}") from None
    return resolved
