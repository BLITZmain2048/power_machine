"""Run an IEEE 39 short circuit with explicit, shipped study data."""
from __future__ import annotations

from pathlib import Path

from power_machine import load_case, shortcircuit


def main() -> None:
    sc_data = Path(__file__).parents[1] / "power_machine" / "cases" / "ieee39" / "shortcircuit_data.json"
    result = shortcircuit(load_case("ieee39"), bus="B01", fault="3ph", sc_data=sc_data)
    print(f"Short-circuit status: {result.status}")
    for fault in result.faults.to_records():
        print(fault)


if __name__ == "__main__":
    main()
