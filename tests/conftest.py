"""Shared pytest config: make `app` importable when running `pytest` from the
backend folder without installing the package."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
