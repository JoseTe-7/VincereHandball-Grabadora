"""Ajustes persistentes de la aplicación (JSON) y rutas de datos.

Los equipos y jugadores viven en SQLite (`db.py`); aquí solo queda lo que es
del ordenador: carpeta de clips, FPS y la organización de las sesiones.
La detección y selección de cámaras se renueva en cada ejecución.
"""
from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .destinations import default_video_directory

APP_NAME = "VincereHandball Grabador"

# Se puede mover con la variable de entorno (útil para pruebas o para un disco compartido).
DATA_DIR = Path(os.environ.get("HANDBALL_RECORDER_HOME") or Path.home() / ".handball_recorder")
CONFIG_PATH = DATA_DIR / "config.json"
DB_PATH = DATA_DIR / "data.db"
LOCK_PATH = DATA_DIR / "host.lock"
HOST_INFO_PATH = DATA_DIR / "host.json"

DEFAULT_BASE_DIR = default_video_directory()

DEFAULT_EXERCISES = [
    "Lanzamiento en apoyo",
    "Lanzamiento en suspensión",
    "Finta y lanzamiento",
    "Contraataque",
    "Siete metros",
]
DEFAULT_CATEGORIES = ["Jugadores"]

# Valores de la versión 1 (sin equipos): solo sirven para detectar una configuración sin tocar al migrar.
_V1_DEFAULT_PLAYERS = ["Jugador 1", "Jugador 2", "Jugador 3"]


@dataclass
class Settings:
    base_dir: str = str(DEFAULT_BASE_DIR)
    fps: int = 30
    group_by_profile: bool = True          # Base/Equipo/Jugador/Ejercicio (False: Base/Jugador/Ejercicio)
    open_display_on_start: bool = False    # abrir la pantalla del proyector al arrancar
    max_sessions: int = 4
    sessions: list[dict[str, Any]] = field(default_factory=list)
    pavilions: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path = CONFIG_PATH) -> "Settings":
        """Carga los ajustes; si el fichero no existe o está corrupto usa los de por defecto."""
        data = _read_json(path)
        known = {f.name for f in fields(cls)}
        values = {k: v for k, v in data.items() if k in known}
        settings = cls(**values)
        if not isinstance(settings.sessions, list):
            settings.sessions = []
        if not isinstance(settings.pavilions, list):
            settings.pavilions = []
        settings.pavilions = [p for p in settings.pavilions if isinstance(p, dict) and p.get("id") and p.get("name")]
        settings.sessions = [s for s in settings.sessions if isinstance(s, dict)]
        settings.max_sessions = max(1, min(8, int(settings.max_sessions or 4)))
        return settings

    def save(self, path: Path = CONFIG_PATH) -> None:
        """Escritura atómica: un corte de luz a mitad nunca deja el JSON a medias."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".config-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(asdict(self), handle, ensure_ascii=False, indent=2)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        except OSError:
            pass  # no es crítico: la app sigue funcionando sin persistir


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_legacy(path: Path = CONFIG_PATH) -> dict[str, Any]:
    """Datos de la versión 1 (jugadores y ejercicios en el JSON) para migrarlos a un equipo."""
    data = _read_json(path)
    players = [p for p in data.get("players", []) if isinstance(p, str)]
    exercises = [e for e in data.get("exercises", []) if isinstance(e, str)]
    if players == _V1_DEFAULT_PLAYERS:
        players = []                       # eran los de ejemplo: no merece la pena migrarlos
    if exercises == DEFAULT_EXERCISES:
        exercises = []
    return {
        "players": players,
        "exercises": exercises,
    }
