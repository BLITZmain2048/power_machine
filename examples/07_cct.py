"""Estimate IEEE 39 classical critical clearing time for the documented B16 fault."""
from __future__ import annotations

from power_machine import CctOptions, cct, load_case


def main() -> None:
    options = CctOptions(lower_clearing_s=0.02, upper_clearing_s=0.30, resolution_ms=20.0,
                         end_s=3.0, step_ms=5.0, recovery_min_voltage_pu=0.5, recovery_window_s=0.1)
    result = cct(load_case("ieee39"), {"engine": "ieee39_classical", "fault_bus_id": "B16"}, options=options)
    print(f"CCT status: {result.status}")
    print(f"Stable lower bound: {result.summary.get('stable_lower_s')} s")
    print(f"Unstable upper bound: {result.summary.get('unstable_upper_s')} s")


if __name__ == "__main__":
    main()
