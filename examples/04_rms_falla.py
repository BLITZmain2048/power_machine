"""Simulate the documented IEEE 39 classical fault and clearing event."""
from __future__ import annotations

from power_machine import load_case, rms


def main() -> None:
    events = [
        {"time_s": 1.0, "kind": "short_circuit", "target_id": "B16"},
        {"time_s": 1.1, "kind": "clear_short_circuit", "target_id": "B16"},
    ]
    result = rms(load_case("ieee39"), events, t_end_s=0.2, step_s=0.01)
    print(f"RMS status: {result.status}, samples: {len(result.samples.to_records())}")


if __name__ == "__main__":
    main()
