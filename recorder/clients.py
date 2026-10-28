"""Registro de ventanas conectadas y de sus permisos.

Cada ventana (la del entrenador, la del proyector…) recibe su propio token al cargar la
página, y ese token lleva el rol:

  operator  puede manejar la aplicación (grabar, editar equipos, cambiar de cámara);
  viewer    solo mira: estado en directo y vídeo. Nunca puede cambiar nada.

El rol lo decide el servidor al repartir el token; el cliente no puede elegirlo ni subirlo.
"""
from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass
from typing import Callable

OPERATOR = "operator"
VIEWER = "viewer"

MAX_CLIENTS = 200
IDLE_EXPIRY_SECONDS = 12 * 3600


@dataclass
class Client:
    token: str
    role: str
    created: float
    last_seen: float
    connections: int = 0          # conexiones de tiempo real abiertas ahora mismo


class ClientRegistry:
    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, Client] = {}
        self._on_change = on_change or (lambda: None)

    def register(self, role: str) -> Client:
        if role not in (OPERATOR, VIEWER):
            raise ValueError(role)
        now = time.monotonic()
        client = Client(secrets.token_urlsafe(24), role, now, now)
        with self._lock:
            self._clients[client.token] = client
            if len(self._clients) > MAX_CLIENTS:
                self._evict_locked()
        return client

    def get(self, token: str | None) -> Client | None:
        if not token:
            return None
        with self._lock:
            client = self._clients.get(token)
            if client is not None:
                client.last_seen = time.monotonic()
            return client

    def attach(self, client: Client) -> None:
        with self._lock:
            client.connections += 1
        self._on_change()                      # fuera del cerrojo: la presencia cambia en directo

    def detach(self, client: Client) -> None:
        with self._lock:
            client.connections = max(0, client.connections - 1)
            client.last_seen = time.monotonic()
        self._on_change()

    def counts(self) -> dict[str, int]:
        """Ventanas conectadas ahora mismo, por rol."""
        with self._lock:
            live = [c for c in self._clients.values() if c.connections > 0]
        return {
            "operators": sum(1 for c in live if c.role == OPERATOR),
            "viewers": sum(1 for c in live if c.role == VIEWER),
        }

    def prune(self) -> None:
        cutoff = time.monotonic() - IDLE_EXPIRY_SECONDS
        with self._lock:
            for token in [t for t, c in self._clients.items() if c.connections == 0 and c.last_seen < cutoff]:
                del self._clients[token]

    def _evict_locked(self) -> None:
        idle = sorted((c for c in self._clients.values() if c.connections == 0), key=lambda c: c.last_seen)
        for client in idle[: len(self._clients) - MAX_CLIENTS]:
            del self._clients[client.token]
