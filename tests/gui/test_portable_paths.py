from gui_support import ieee_model, ieee_root
"""Regresiones de empaquetado y rutas absolutas para study."""
import json
import re
from pathlib import Path


from gui_support import ROOT
SOURCE_ROOTS = (ROOT / "gui",)
WINDOWS_ABSOLUTE = re.compile(r"(?i)(?<![A-Za-z0-9_])[A-Z]:[\\\\/]")
KNOWN_ABSOLUTE_PATHS = set()


def absolute_path_locations():
    assert all(p.is_dir() for p in SOURCE_ROOTS)
    locations = set()
    for source_root in SOURCE_ROOTS:
        for path in source_root.rglob("*.py"):
            relative = path.relative_to(ROOT).as_posix()
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if WINDOWS_ABSOLUTE.search(line):
                    locations.add((relative, number))
    return locations


def test_no_new_windows_absolute_paths_in_application_code():
    assert absolute_path_locations() <= KNOWN_ABSOLUTE_PATHS


