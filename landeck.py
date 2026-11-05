"""Entry point for the packaged exe (PyInstaller needs a script, not -m)."""
import sys

from agent.main import main

if __name__ == "__main__":
    sys.exit(main())
