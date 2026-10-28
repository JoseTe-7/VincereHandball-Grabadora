"""Las pruebas usan una carpeta de datos propia: nunca tocan los equipos ni los ajustes reales."""
import os
import sys
import tempfile
from pathlib import Path

os.environ["HANDBALL_RECORDER_HOME"] = tempfile.mkdtemp(prefix="hbr-tests-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
