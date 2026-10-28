"""Estructura de carpetas y nomenclatura de los clips.

Directorio_Base/Equipo/Jugador/Ejercicio/AAAA-MM-DD_HH-MM-SS_Jugador_Ejercicio.mp4
(sin la carpeta de equipo si `profile` es None).
"""
from __future__ import annotations

import os
import re
import unicodedata
from datetime import datetime
from pathlib import Path

NO_EXERCISE_FOLDER = "Sin_ejercicio"
VIDEO_EXTENSION = ".mp4"


def sanitize(name: str) -> str:
    """Convierte un nombre libre en uno seguro para cualquier sistema de ficheros."""
    text = unicodedata.normalize("NFKD", name)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\- ]+", "", text)          # quita símbolos y caracteres ilegales
    text = re.sub(r"\s+", "_", text.strip())
    return text.strip("._") or "SinNombre"


def target_folder(
    base_dir: Path | str,
    player: str,
    exercise: str | None,
    profile: str | None = None,
) -> Path:
    folder = Path(base_dir)
    if profile:
        folder /= sanitize(profile)
    folder /= sanitize(player)
    return folder / (sanitize(exercise) if exercise else NO_EXERCISE_FOLDER)


def build_target(
    base_dir: Path | str,
    player: str,
    exercise: str | None,
    now: datetime | None = None,
    profile: str | None = None,
) -> Path:
    """Crea las carpetas y RESERVA el nombre del clip con una creación exclusiva.

    `O_CREAT | O_EXCL` es atómico en el sistema de ficheros: si dos sesiones (o dos procesos)
    piden el mismo nombre en el mismo segundo, solo una lo consigue y la otra recibe `_2`.
    """
    now = now or datetime.now()
    folder = target_folder(base_dir, player, exercise, profile)
    folder.mkdir(parents=True, exist_ok=True)

    parts = [now.strftime("%Y-%m-%d_%H-%M-%S"), sanitize(player)]
    if exercise:
        parts.append(sanitize(exercise))
    stem = "_".join(parts)

    counter = 1
    while True:
        suffix = "" if counter == 1 else f"_{counter}"
        path = folder / f"{stem}{suffix}{VIDEO_EXTENSION}"
        try:
            os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
        except FileExistsError:
            counter += 1
            continue
        return path
