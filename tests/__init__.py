"""The player's tests, run from the project's folder:

    python3 -m unittest discover -s tests -t .

Standard library only; no window, no network, no service. Before any of the
player's modules is imported, the folders it reads and writes (settings,
cache, data, runtime, home) are pointed at a temporary one, so nothing of the
user's own player is touched."""
import atexit
import os
import shutil
import sys
import tempfile

SCRATCH = tempfile.mkdtemp(prefix="ocean-mist-tests-")
atexit.register(shutil.rmtree, SCRATCH, ignore_errors=True)
for name in ("HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
    os.environ[name] = os.path.join(SCRATCH, name.lower())
    os.makedirs(os.environ[name], mode=0o700)
os.environ["OCEAN_MIST_APP_ID"] = "io.github.nahuelgenchi.OceanMistTest.Tests"
os.environ.pop("OCEAN_MIST_DEBUG", None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
