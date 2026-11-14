"""Keep tests away from the real machine: its library registry (~/.config/photos), the
catalogue next to it, the mounted disks and the default library."""
import tempfile
from pathlib import Path
from unittest.mock import patch

import photo_library


def isolate(test) -> Path:
    """Point everything machine-wide at a temporary folder for the duration of `test`."""
    temp = tempfile.TemporaryDirectory()
    test.addCleanup(temp.cleanup)
    base = Path(temp.name)
    for name, value in (("REGISTRY", base / "config" / "libraries.json"),
                        ("PHOTO_DIR", base / "no-default-library"),
                        ("_candidate_dirs", lambda known: [Path(p) for p in known])):
        patcher = patch.object(photo_library, name, value)
        patcher.start()
        test.addCleanup(patcher.stop)
    photo_library._scan.cache = None
    test.addCleanup(setattr, photo_library._scan, "cache", None)
    return base
