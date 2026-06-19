"""
Add app/ to sys.path so tests can import services.epgenius etc. directly.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))
