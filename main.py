"""Punto de entrada.

    python main.py                  abre la aplicación (la primera vez, como anfitrión)
    python main.py                  ...otra vez: se une al anfitrión y abre la pantalla del proyector
    python main.py --operator       ...otra vez: abre otra ventana de control en vez de la pantalla
    python main.py --display        al arrancar, abre también la pantalla en el segundo monitor
    python main.py --session s2     la pantalla muestra solo esa sesión (por defecto, todas)
    python main.py --browser        usa el navegador en vez de una ventana propia
"""
from __future__ import annotations

import argparse
import sys
import time
import webbrowser

from recorder import instance
from recorder.config import APP_NAME
from recorder.desktop_api import DesktopApi, create_display_window
from recorder.server import LocalServer, display_url, operator_url
from recorder.service import RecorderService

FLOOR_COLOR = "#0a1126"


# ====================================================================== ventanas nativas
def _close_other_windows(main) -> None:
    import webview

    for window in list(webview.windows):
        if window is not main:
            try:
                window.destroy()
            except Exception:
                pass


# ====================================================================== modos
def run_host(args: argparse.Namespace, lock: instance.InstanceLock) -> int:
    instance.remove_host_info()                      # por si quedó un fichero de una ejecución anterior
    service = RecorderService()
    server = LocalServer(service, lan=args.lan)
    server.start()
    instance.write_host_info(server.port, server.operator_key, server.viewer_key)
    open_display = args.display or service.settings.open_display_on_start

    try:
        if args.browser:
            return _host_in_browser(server, open_display)
        try:
            import webview
        except ImportError:
            print("pywebview no está instalado (pip install -r requirements.txt); abriendo en el navegador.")
            return _host_in_browser(server, open_display)

        api = DesktopApi(server.display_url)
        main_window = webview.create_window(
            APP_NAME, server.operator_url(), width=1360, height=860, min_size=(1040, 660),
            background_color=FLOOR_COLOR, text_select=False, js_api=api,
        )
        api._window = main_window
        if open_display:
            create_display_window(server.display_url())
        main_window.events.closed += lambda: _close_other_windows(main_window)
        webview.start()
        return 0
    finally:
        service.shutdown()
        server.stop()
        instance.remove_host_info()
        lock.release()


def _host_in_browser(server: LocalServer, open_display: bool) -> int:
    print(f"Control:  {server.operator_url()}\nPantalla: {server.display_url()}\nPulsa Ctrl+C para salir.")
    for url in server.lan_urls(): print(f"Tablet LAN: {url}")
    if server.lan: print("AVISO: importa y confía en el certificado local en la tablet antes de permitir acceso a la cámara.")
    webbrowser.open(server.operator_url())
    if open_display:
        webbrowser.open(server.display_url())
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        return 0


def run_attached(args: argparse.Namespace) -> int:
    """Ya hay un anfitrión: esta ventana se conecta a él y no toca ninguna cámara."""
    info = instance.wait_for_host(timeout=10.0)
    if info is None:
        print("Hay otra instancia abierta, pero no responde. Ciérrala y vuelve a intentarlo.", file=sys.stderr)
        return 1
    if args.operator:
        url = operator_url(info["port"], info["operator_key"])
    else:
        url = display_url(info["port"], info["viewer_key"], args.session)

    if args.browser:
        webbrowser.open(url)
        return 0
    try:
        import webview
    except ImportError:
        webbrowser.open(url)
        return 0

    if args.operator:
        api = DesktopApi()
        window = webview.create_window(
            APP_NAME, url, width=1360, height=860, min_size=(1040, 660),
            background_color=FLOOR_COLOR, text_select=False, js_api=api,
        )
        api._window = window
    else:
        create_display_window(url)
    webview.start()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Grabador de entrenamientos de extremos")
    parser.add_argument("--browser", action="store_true", help="usar el navegador en vez de una ventana propia")
    parser.add_argument("--lan", action="store_true", help="exponer el servidor en la LAN mediante HTTPS (el certificado debe confiarse en la tablet)")
    parser.add_argument("--display", action="store_true", help="abrir también la pantalla del proyector al arrancar")
    parser.add_argument("--operator", action="store_true", help="si ya hay una instancia, abrir otra ventana de control")
    parser.add_argument("--session", help="la pantalla muestra solo esta sesión (por ejemplo s2)")
    args = parser.parse_args()

    lock = instance.InstanceLock()
    if lock.acquire():
        return run_host(args, lock)
    return run_attached(args)


if __name__ == "__main__":
    sys.exit(main())
