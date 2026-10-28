"""Adaptador entre la interfaz web y funciones nativas del escritorio."""
from __future__ import annotations

from typing import Callable

from .config import APP_NAME

FLOOR_COLOR = "#0a1126"


def secondary_screen():
    """Devuelve un monitor secundario cuando pywebview puede detectarlo."""
    import webview
    try:
        screens = list(webview.screens)
    except Exception:
        return None
    for screen in screens:
        if not (screen.x <= 0 < screen.x + screen.width and screen.y <= 0 < screen.y + screen.height):
            return screen
    return None


def create_display_window(url: str):
    """Abre la pantalla de proyección, preferentemente en el segundo monitor."""
    import webview
    screen = secondary_screen()
    common = {"background_color": FLOOR_COLOR, "text_select": False}
    if screen is not None:
        return webview.create_window(f"{APP_NAME} · Pantalla", url, screen=screen, fullscreen=True, **common)
    return webview.create_window(f"{APP_NAME} · Pantalla", url, width=1280, height=720, min_size=(640, 360), **common)


class DesktopApi:
    """Funciones nativas que la interfaz web no puede hacer sola."""

    def __init__(self, display_url_for: Callable[[str | None], str] | None = None) -> None:
        self._window = None          # privado: pywebview no expone lo que empieza por "_"
        self._display_url_for = display_url_for

    def choose_dir(self, current: str = "") -> str | None:
        import webview

        result = self._window.create_file_dialog(webview.FileDialog.FOLDER, directory=current or "")
        return result[0] if result else None

    def choose_file(self) -> str | None:
        import webview

        result = self._window.create_file_dialog(webview.FileDialog.OPEN, file_types=("Copia de equipo (*.json)",))
        return result[0] if result else None

    def open_display(self, session_id: str | None = None) -> dict:
        """Abre la pantalla del proyector (en el segundo monitor si existe)."""
        if self._display_url_for is None:
            return {"ok": False, "error": "Esta ventana no puede abrir la pantalla."}
        try:
            create_display_window(self._display_url_for(session_id))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"No se pudo abrir la pantalla: {exc}"}
        return {"ok": True, "second_monitor": secondary_screen() is not None}

