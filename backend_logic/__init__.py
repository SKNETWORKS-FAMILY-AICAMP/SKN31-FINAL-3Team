import sys
from pathlib import Path

# Add backend_logic directory to sys.path so sibling imports work smoothly
_pkg_dir = str(Path(__file__).resolve().parent)
if _pkg_dir not in sys.path:
    sys.path.insert(0, _pkg_dir)
