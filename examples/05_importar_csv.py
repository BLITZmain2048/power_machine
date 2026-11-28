"""Import a CSV element-table export supplied as a command-line argument."""
from __future__ import annotations

from pathlib import Path
import sys

from power_machine import import_csv_export


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if not values:
        print("Usage: python 05_importar_csv.py PATH/TO/CSV_EXPORT [network.json]")
        return 0
    network = import_csv_export(values[0])
    destination = Path(values[1]) if len(values) > 1 else Path("imported_network.json")
    print(network.save(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
