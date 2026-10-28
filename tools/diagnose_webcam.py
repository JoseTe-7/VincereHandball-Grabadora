"""Mide la captura real de una webcam en Windows sin abrir VinceGraba.

Uso desde la carpeta del proyecto:
    python tools\\diagnose_webcam.py --index 0 --seconds 10 --width 1280 --height 720

Usa DirectShow porque los índices del inventario de VinceGraba provienen de ese backend.
"""
from __future__ import annotations

import argparse
import platform
import time

import cv2


def fourcc_string(value: float) -> str:
    raw = int(value or 0)
    return "".join(chr((raw >> (8 * shift)) & 0xFF) for shift in range(4)).strip("\x00 ") or "driver/default"


def main() -> int:
    parser = argparse.ArgumentParser(description="Diagnóstico real de FPS de una cámara Windows")
    parser.add_argument("--index", type=int, default=0, help="Índice mostrado por VinceGraba; por defecto 0")
    parser.add_argument("--seconds", type=float, default=10.0, help="Duración de la medición; por defecto 10 s")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=720)
    parser.add_argument("--fps", type=int, default=30)
    args = parser.parse_args()

    if platform.system() != "Windows":
        parser.error("Este diagnóstico debe ejecutarse en Windows, en el mismo ordenador que VinceGraba.")
    if args.seconds < 2 or args.width < 1 or args.height < 1 or args.fps < 1:
        parser.error("Resolución, FPS y segundos deben ser valores positivos; la medición mínima es de 2 s.")

    cap = cv2.VideoCapture(args.index, cv2.CAP_DSHOW)
    if not cap.isOpened():
        print(f"ERROR: DirectShow no pudo abrir la cámara {args.index}.")
        cap.release()
        return 2
    try:
        # Negociar codec primero, exactamente como la ruta recomendada para VinceGraba.
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*"MJPG"))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, args.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, args.height)
        cap.set(cv2.CAP_PROP_FPS, args.fps)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

        frame = None
        for _ in range(8):
            ok, frame = cap.read()
            if not ok or frame is None:
                print("ERROR: no se reciben fotogramas después de abrir la cámara.")
                return 3

        frames = 0
        start = time.perf_counter()
        end = start + args.seconds
        while time.perf_counter() < end:
            ok, frame = cap.read()
            if ok and frame is not None:
                frames += 1
        elapsed = time.perf_counter() - start
        actual_fps = frames / elapsed if elapsed > 0 else 0.0
        actual_h, actual_w = frame.shape[:2] if frame is not None else (0, 0)

        print("\nDiagnóstico de captura de cámara")
        print(f"Backend:            {cap.getBackendName()}")
        print(f"Índice:             {args.index}")
        print(f"Formato negociado:  {fourcc_string(cap.get(cv2.CAP_PROP_FOURCC))}")
        print(f"Resolución real:    {actual_w}x{actual_h} (solicitada {args.width}x{args.height})")
        print(f"FPS declarados:     {cap.get(cv2.CAP_PROP_FPS):.2f}")
        print(f"FPS medidos:        {actual_fps:.2f} durante {elapsed:.2f} s")
        print(f"Fotogramas válidos: {frames}")
        if actual_fps >= args.fps * 0.95:
            print("RESULTADO: la captura directa alcanza la cadencia solicitada.")
            print("Si VinceGraba muestra menos FPS, el cuello de botella está después de VideoCapture (procesamiento o vista previa).")
        else:
            print("RESULTADO: la captura directa ya va por debajo del objetivo antes de entrar en VinceGraba.")
            print("Prueba 640x480 o 1280x720 y revisa la aplicación Cámara de Windows, el controlador y el puerto USB.")
        return 0
    finally:
        cap.release()


if __name__ == "__main__":
    raise SystemExit(main())
