"""Entry script (used by PyInstaller and for double-click launching)."""
import sys

from pccleaner.cli import main

if __name__ == "__main__":
    sys.exit(main())
