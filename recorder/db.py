"""Equipos (perfiles de trabajo) en SQLite: categorías, jugadores y ejercicios.

Diseño pensado para la concurrencia:
  * modo WAL: las lecturas no bloquean a las escrituras;
  * una conexión nueva por operación, con `busy_timeout`, así que varios hilos
    o procesos pueden usar el mismo fichero sin pisarse;
  * escrituras en transacciones `BEGIN IMMEDIATE` (todo o nada);
  * la unicidad de nombres la garantiza la base de datos (restricción UNIQUE),
    no un "comprobar y luego insertar" que dos clientes podrían saltarse a la vez.

Todo se guarda al instante: no hay que pulsar "guardar".
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import DEFAULT_CATEGORIES, DEFAULT_EXERCISES
from .storage import sanitize

MAX_NAME_LENGTH = 40
MAX_IMPORT_ITEMS = 500
ANALISIS_ETT_EXERCISES = {
    "SITUACIONES": ["1c0", "1c1", "2c1", "2c2", "3c3", "4c4", "5c6", "6c5", "6c6"],
    "INICIOS": ["Permuta alta", "Permuta baja", "Cruce central-pivote", "Cruce corto", "Cruce largo"],
    "ACCION ESPECIFICA": ["Lanzamiento extremo", "Lanzamiento pivote", "Lanzamiento 9m", "Lanzamiento 7m"],
}
EXPORT_FORMAT = "extremos-equipo"
EXPORT_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE (profile_id, key)
);
CREATE TABLE IF NOT EXISTS exercise_categories (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE (profile_id, key)
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE (profile_id, key)
);
CREATE TABLE IF NOT EXISTS exercises (
    id INTEGER PRIMARY KEY,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    category_id INTEGER REFERENCES exercise_categories(id) ON DELETE SET NULL,
    name TEXT NOT NULL,
    key TEXT NOT NULL,
    position INTEGER NOT NULL,
    UNIQUE (profile_id, key)
);
"""


class DbError(Exception):
    """Error pensado para mostrarse tal cual al usuario."""


def clean_name(raw: Any, what: str) -> str:
    name = " ".join(str(raw or "").split())
    if not name:
        raise DbError(f"Escribe el nombre del {what}.")
    if len(name) > MAX_NAME_LENGTH:
        raise DbError(f"El nombre del {what} no puede superar {MAX_NAME_LENGTH} caracteres.")
    return name


def _key(name: str) -> str:
    """Clave de unicidad: sin tildes, sin mayúsculas, con las mismas reglas que las carpetas."""
    return sanitize(name).lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._read() as conn:               # `with sqlite3.connect()` NO cierra la conexión: por eso se usa _read()
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript(SCHEMA)
            # Migración compatible con bases de datos existentes.
            columns = {r[1] for r in conn.execute("PRAGMA table_info(exercises)")}
            if "category_id" not in columns:
                conn.execute("ALTER TABLE exercises ADD COLUMN category_id INTEGER REFERENCES exercise_categories(id) ON DELETE SET NULL")

    # ------------------------------------------------------------------ conexión
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=15, isolation_level=None)  # autocommit: las transacciones son explícitas
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=15000")
        return conn

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")      # coge el cerrojo de escritura desde el principio
            yield conn
            conn.execute("COMMIT")
        except sqlite3.IntegrityError as exc:
            self._rollback(conn)
            raise DbError("Ya existe un elemento con ese nombre.") from exc
        except BaseException:
            self._rollback(conn)
            raise
        finally:
            conn.close()

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        if conn.in_transaction:
            conn.execute("ROLLBACK")

    # ------------------------------------------------------------------ lectura
    def catalog(self) -> list[dict[str, Any]]:
        """Todos los equipos con sus categorías, jugadores y ejercicios (en el orden en que se crearon)."""
        with self._read() as conn:
            profiles = [
                {"id": r["id"], "name": r["name"], "categories": [], "exercise_categories": [], "players": [], "exercises": []}
                for r in conn.execute("SELECT id, name FROM profiles ORDER BY id")
            ]
            by_id = {p["id"]: p for p in profiles}
            for r in conn.execute("SELECT id, profile_id, name FROM categories ORDER BY position, id"):
                by_id[r["profile_id"]]["categories"].append({"id": r["id"], "name": r["name"]})
            for r in conn.execute("SELECT id, profile_id, name FROM exercise_categories ORDER BY position, id"):
                by_id[r["profile_id"]]["exercise_categories"].append({"id": r["id"], "name": r["name"]})
            for r in conn.execute("SELECT id, profile_id, category_id, name FROM players ORDER BY position, id"):
                by_id[r["profile_id"]]["players"].append(
                    {"id": r["id"], "name": r["name"], "category_id": r["category_id"]}
                )
            for r in conn.execute("SELECT id, profile_id, category_id, name FROM exercises ORDER BY position, id"):
                by_id[r["profile_id"]]["exercises"].append({"id": r["id"], "name": r["name"], "category_id": r["category_id"]})
        return profiles

    def profile_count(self) -> int:
        with self._read() as conn:
            return conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0]

    # ------------------------------------------------------------------ equipos
    def create_profile(
        self,
        name: Any,
        categories: list[str] | tuple[str, ...] = (),
        exercises: list[str] | tuple[str, ...] = (),
        players: list[tuple[str, str | None]] | tuple = (),
    ) -> int:
        """Crea un equipo, opcionalmente con categorías, ejercicios y jugadores `(nombre, categoría|None)`."""
        name = clean_name(name, "equipo")
        with self._write() as conn:
            try:
                cur = conn.execute(
                    "INSERT INTO profiles (name, key, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (name, _key(name), _now(), _now()),
                )
            except sqlite3.IntegrityError:
                raise DbError("Ya existe un equipo con ese nombre.") from None
            pid = cur.lastrowid
            category_ids: dict[str, int] = {}
            for position, cat in enumerate(categories):
                cat = clean_name(cat, "categoría")
                if _key(cat) in category_ids:
                    continue
                category_ids[_key(cat)] = conn.execute(
                    "INSERT INTO categories (profile_id, name, key, position) VALUES (?, ?, ?, ?)",
                    (pid, cat, _key(cat), position),
                ).lastrowid
            seen: set[str] = set()
            for position, (player, category) in enumerate(players):
                player = clean_name(player, "jugador")
                if _key(player) in seen:
                    continue
                seen.add(_key(player))
                conn.execute(
                    "INSERT INTO players (profile_id, category_id, name, key, position) VALUES (?, ?, ?, ?, ?)",
                    (pid, category_ids.get(_key(category)) if category else None, player, _key(player), position),
                )
            seen = set()
            for position, exercise in enumerate(exercises):
                exercise = clean_name(exercise, "ejercicio")
                if _key(exercise) in seen:
                    continue
                seen.add(_key(exercise))
                conn.execute(
                    "INSERT INTO exercises (profile_id, name, key, position) VALUES (?, ?, ?, ?)",
                    (pid, exercise, _key(exercise), position),
                )
        return pid

    def rename_profile(self, profile_id: int, name: Any) -> None:
        name = clean_name(name, "equipo")
        with self._write() as conn:
            try:
                cur = conn.execute(
                    "UPDATE profiles SET name = ?, key = ?, updated_at = ? WHERE id = ?",
                    (name, _key(name), _now(), profile_id),
                )
            except sqlite3.IntegrityError:
                raise DbError("Ya existe un equipo con ese nombre.") from None
            self._must_change(cur, "equipo")

    def delete_profile(self, profile_id: int) -> None:
        with self._write() as conn:
            if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] <= 1:
                raise DbError("Debe quedar al menos un equipo.")
            self._must_change(conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,)), "equipo")

    def duplicate_profile(self, profile_id: int, new_name: str | None = None) -> int:
        source = self._profile(profile_id)
        name = new_name or self._free_profile_name(f"{source['name']} (copia)")
        categories = [c["name"] for c in source["categories"]]
        names = {c["id"]: c["name"] for c in source["categories"]}
        players = [(p["name"], names.get(p["category_id"])) for p in source["players"]]
        return self.create_profile(name, categories, [e["name"] for e in source["exercises"]], players)

    # ------------------------------------------------------------------ categorías
    def add_category(self, profile_id: int, name: Any) -> int:
        name = clean_name(name, "categoría")
        with self._write() as conn:
            self._require_profile(conn, profile_id)
            try:
                return conn.execute(
                    "INSERT INTO categories (profile_id, name, key, position) "
                    "VALUES (?, ?, ?, COALESCE((SELECT MAX(position) + 1 FROM categories WHERE profile_id = ?), 0))",
                    (profile_id, name, _key(name), profile_id),
                ).lastrowid
            except sqlite3.IntegrityError:
                raise DbError("Ya existe una categoría con ese nombre.") from None

    def remove_category(self, category_id: int) -> None:
        """Quita la categoría; sus jugadores pasan a "Sin categoría" (no se borran)."""
        with self._write() as conn:
            self._must_change(conn.execute("DELETE FROM categories WHERE id = ?", (category_id,)), "categoría")

    # ------------------------------------------------------------------ jugadores
    def add_player(self, profile_id: int, name: Any, category_id: int | None = None) -> int:
        name = clean_name(name, "jugador")
        with self._write() as conn:
            self._require_profile(conn, profile_id)
            self._require_category(conn, profile_id, category_id)
            try:
                return conn.execute(
                    "INSERT INTO players (profile_id, category_id, name, key, position) "
                    "VALUES (?, ?, ?, ?, COALESCE((SELECT MAX(position) + 1 FROM players WHERE profile_id = ?), 0))",
                    (profile_id, category_id, name, _key(name), profile_id),
                ).lastrowid
            except sqlite3.IntegrityError:
                raise DbError("Ya existe un jugador con ese nombre en este equipo.") from None

    def remove_player(self, player_id: int) -> None:
        with self._write() as conn:
            self._must_change(conn.execute("DELETE FROM players WHERE id = ?", (player_id,)), "jugador")

    def move_player(self, player_id: int, category_id: int | None) -> None:
        with self._write() as conn:
            row = conn.execute("SELECT profile_id FROM players WHERE id = ?", (player_id,)).fetchone()
            if row is None:
                raise DbError("Ese jugador ya no existe.")
            self._require_category(conn, row["profile_id"], category_id)
            conn.execute("UPDATE players SET category_id = ? WHERE id = ?", (category_id, player_id))

    # ------------------------------------------------------------------ categorías de ejercicios
    def add_exercise_category(self, profile_id: int, name: Any) -> int:
        name = clean_name(name, "categoría de ejercicios")
        with self._write() as conn:
            self._require_profile(conn, profile_id)
            try:
                return conn.execute(
                    "INSERT INTO exercise_categories (profile_id, name, key, position) VALUES (?, ?, ?, COALESCE((SELECT MAX(position)+1 FROM exercise_categories WHERE profile_id=?),0))",
                    (profile_id, name, _key(name), profile_id),
                ).lastrowid
            except sqlite3.IntegrityError:
                raise DbError("Ya existe una categoría de ejercicios con ese nombre.") from None

    def remove_exercise_category(self, category_id: int) -> None:
        with self._write() as conn:
            self._must_change(conn.execute("DELETE FROM exercise_categories WHERE id = ?", (category_id,)), "categoría de ejercicios")

    # ------------------------------------------------------------------ ejercicios
    def add_exercise(self, profile_id: int, name: Any, category_id: int | None = None) -> int:
        name = clean_name(name, "ejercicio")
        with self._write() as conn:
            self._require_profile(conn, profile_id)
            if category_id is not None:
                row = conn.execute("SELECT profile_id FROM exercise_categories WHERE id=?", (category_id,)).fetchone()
                if row is None or row["profile_id"] != profile_id:
                    raise DbError("La categoría de ejercicios no pertenece a este equipo.")
            try:
                return conn.execute(
                    "INSERT INTO exercises (profile_id, category_id, name, key, position) "
                    "VALUES (?, ?, ?, ?, COALESCE((SELECT MAX(position) + 1 FROM exercises WHERE profile_id = ?), 0))",
                    (profile_id, category_id, name, _key(name), profile_id),
                ).lastrowid
            except sqlite3.IntegrityError:
                raise DbError("Ya existe un ejercicio con ese nombre en este equipo.") from None

    def move_exercise(self, exercise_id: int, category_id: int | None) -> None:
        with self._write() as conn:
            row = conn.execute("SELECT profile_id FROM exercises WHERE id = ?", (exercise_id,)).fetchone()
            if row is None:
                raise DbError("Ese ejercicio ya no existe.")
            if category_id is not None:
                category = conn.execute("SELECT profile_id FROM exercise_categories WHERE id = ?", (category_id,)).fetchone()
                if category is None or category["profile_id"] != row["profile_id"]:
                    raise DbError("La categoría de ejercicios no pertenece a este equipo.")
            conn.execute("UPDATE exercises SET category_id = ? WHERE id = ?", (category_id, exercise_id))

    def remove_exercise(self, exercise_id: int) -> None:
        with self._write() as conn:
            self._must_change(conn.execute("DELETE FROM exercises WHERE id = ?", (exercise_id,)), "ejercicio")

    # ------------------------------------------------------------------ copia en JSON
    def export_profile(self, profile_id: int) -> dict[str, Any]:
        profile = self._profile(profile_id)
        names = {c["id"]: c["name"] for c in profile["categories"]}
        return {
            "format": EXPORT_FORMAT,
            "version": EXPORT_VERSION,
            "name": profile["name"],
            "categories": [c["name"] for c in profile["categories"]],
            "players": [{"name": p["name"], "category": names.get(p["category_id"])} for p in profile["players"]],
            "exercises": [e["name"] for e in profile["exercises"]],
        }

    def import_profile(self, data: Any) -> int:
        """Crea un equipo nuevo a partir de un JSON exportado. Valida todo: el fichero no es de fiar."""
        if not isinstance(data, dict) or data.get("format") != EXPORT_FORMAT:
            raise DbError("Ese archivo no es una copia de equipo de esta aplicación.")
        if data.get("version") != EXPORT_VERSION:
            raise DbError("La copia es de una versión que esta aplicación no reconoce.")

        def strings(value: Any, what: str) -> list[str]:
            if not isinstance(value, list) or len(value) > MAX_IMPORT_ITEMS or not all(isinstance(v, str) for v in value):
                raise DbError(f"La copia tiene una lista de {what} no válida.")
            return value

        categories = strings(data.get("categories", []), "categorías")
        exercises = strings(data.get("exercises", []), "ejercicios")
        raw_players = data.get("players", [])
        if not isinstance(raw_players, list) or len(raw_players) > MAX_IMPORT_ITEMS:
            raise DbError("La copia tiene una lista de jugadores no válida.")
        players: list[tuple[str, str | None]] = []
        for item in raw_players:
            if not isinstance(item, dict) or not isinstance(item.get("name"), str):
                raise DbError("La copia tiene un jugador no válido.")
            category = item.get("category")
            if category is not None and not isinstance(category, str):
                raise DbError("La copia tiene un jugador con una categoría no válida.")
            players.append((item["name"], category))
        name = self._free_profile_name(clean_name(data.get("name"), "equipo"))
        return self.create_profile(name, categories, exercises, players)

    # ------------------------------------------------------------------ primer arranque y migración
    def ensure_initial(self, legacy_players: list[str], legacy_exercises: list[str]) -> bool:
        """Si no hay ningún equipo crea "Mi equipo" (migrando los datos de la versión 1 si los había)."""
        if self.profile_count() > 0:
            return False
        migrated = bool(legacy_players or legacy_exercises)
        if migrated:
            self.create_profile("Mi equipo", categories=[], exercises=legacy_exercises or DEFAULT_EXERCISES,
                                players=[(p, None) for p in legacy_players])
        else:
            # Perfil inicial neutro: no imponer un equipo concreto en instalaciones nuevas.
            self.create_profile(
                "Mi equipo",
                categories=DEFAULT_CATEGORIES,
                exercises=DEFAULT_EXERCISES,
            )
        return True

    # ------------------------------------------------------------------ utilidades
    def _profile(self, profile_id: int) -> dict[str, Any]:
        for profile in self.catalog():
            if profile["id"] == profile_id:
                return profile
        raise DbError("Ese equipo ya no existe.")

    def _free_profile_name(self, wanted: str) -> str:
        taken = {_key(p["name"]) for p in self.catalog()}
        name, n = wanted, 2
        while _key(name) in taken:
            suffix = f" ({n})"
            name = wanted[: MAX_NAME_LENGTH - len(suffix)] + suffix
            n += 1
        return name

    @staticmethod
    def _require_profile(conn: sqlite3.Connection, profile_id: int) -> None:
        if conn.execute("SELECT 1 FROM profiles WHERE id = ?", (profile_id,)).fetchone() is None:
            raise DbError("Ese equipo ya no existe.")

    @staticmethod
    def _require_category(conn: sqlite3.Connection, profile_id: int, category_id: int | None) -> None:
        if category_id is None:
            return
        row = conn.execute("SELECT profile_id FROM categories WHERE id = ?", (category_id,)).fetchone()
        if row is None or row["profile_id"] != profile_id:
            raise DbError("Esa categoría ya no existe en este equipo.")

    @staticmethod
    def _must_change(cursor: sqlite3.Cursor, what: str) -> None:
        if cursor.rowcount == 0:
            raise DbError(f"Ese {what} ya no existe.")
