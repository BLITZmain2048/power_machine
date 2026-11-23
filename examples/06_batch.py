"""Run two independent IEEE 39 load-flow scenarios in a local batch."""
from __future__ import annotations

from power_machine import BatchOptions, Scenario, default_output_dir, load_case, run_batch


def main() -> None:
    scenarios = [Scenario("base"), Scenario("line-out", out_of_service=("L01-02",))]
    result = run_batch(load_case("ieee39"), "loadflow", scenarios, options=BatchOptions(workers=1),
                       output=default_output_dir() / "examples" / "batch")
    for row in result.to_records():
        print(row)


if __name__ == "__main__":
    main()
