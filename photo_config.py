import os
from pathlib import Path

PHOTO_DIR = Path(os.environ.get("PHOTO_DIR", "/var/lib/photos"))
