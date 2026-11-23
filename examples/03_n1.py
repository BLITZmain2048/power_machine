"""Rank selected IEEE 39 N-1 outages by explicit severity criteria."""
from __future__ import annotations

from power_machine import ContingencyOptions, contingency, load_case


def main() -> None:
    selected = ["L01-02", "L01-39", "L02-03"]
    result = contingency(load_case("ieee39"), elements=selected, options=ContingencyOptions(max_cases=len(selected)))
    print("Severity criterion: unserved MW, maximum loading percent, then minimum voltage p.u.")
    for row in result.rank(("unserved_mw", "max_loading_pct", "min_voltage_pu")).to_records():
        print(row.get("case_id", row.get("id")), row.get("unserved_mw"), row.get("max_loading_pct"), row.get("min_voltage_pu"))


if __name__ == "__main__":
    main()
