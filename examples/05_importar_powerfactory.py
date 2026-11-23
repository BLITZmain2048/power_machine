"""Import a PowerFactory CSV export supplied as a command-line argument."""
from __future__ import annotations

from pathlib import Path
import sys

from power_machine import import_powerfactory


def main(argv: list[str] | None = None) -> int:
    values = sys.argv[1:] if argv is None else argv
    if not values:
        print("Usage: python 05_importar_powerfactory.py PATH/TO/POWERFACTORY_CSV_EXPORT [network.json]")
        return 0
    network = import_powerfactory(values[0])
    destination = Path(values[1]) if len(values) > 1 else Path("powerfactory_network.json")
    print(network.save(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
