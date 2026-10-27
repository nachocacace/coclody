"""Punto de entrada de PyInstaller.

Vive fuera del paquete a propósito: PyInstaller agrega la carpeta del script
de entrada a sys.path, y si esa carpeta fuera ``coclody/`` sus módulos podrían
tapar a módulos de la biblioteca estándar con el mismo nombre.
"""

import sys

from coclody.cli import main

if __name__ == "__main__":
    sys.exit(main())
