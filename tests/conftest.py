import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
PLUGIN_DIR = TESTS_DIR.parent / "plugin"
for path in (PLUGIN_DIR, TESTS_DIR):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))
