"""Read preserved references kept outside the installed distribution."""
from pathlib import Path
import csv
import gzip
import hashlib
import io
import json


def read_reference(name, root=None):
    """Verify SHA-256 and open compressed CSV or JSON without extraction."""
    root = Path(root) if root is not None else Path(__file__).parent / "data" / "ieee39"
    entries = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    entry = next((e for e in entries if e["file"] == name + ".gz"), None)
    if entry is None:
        raise ValueError("Unknown reference: " + name)
    packed = (root / entry["file"]).read_bytes()
    if hashlib.sha256(packed).hexdigest() != entry["gzip_sha256"]:
        raise ValueError("Incorrect gzip hash")
    raw = gzip.decompress(packed)
    if hashlib.sha256(raw).hexdigest() != entry["sha256"]:
        raise ValueError("Incorrect reference hash")
    with gzip.open(root / entry["file"], "rt", encoding="utf-8-sig", newline="") as stream:
        return json.load(stream) if name.endswith(".json") else list(csv.DictReader(stream))
