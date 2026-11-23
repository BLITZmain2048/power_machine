"""A ten-minute IEEE 39 walkthrough: base flow, N-1, and one alternative."""
from __future__ import annotations

from power_machine import ContingencyOptions, contingency, load_case, loadflow


def main() -> None:
    network = load_case("ieee39")
    base = loadflow(network).require_complete()
    print("Base load flow")
    print("id       voltage (p.u.)")
    for row in base.buses.to_records()[:5]:
        print(f"{row['id']:8} {row['u_pu']:.4f}")
    print(f"Loads: {len(base.loads.to_records())}, branches: {len(base.branches.to_records())}")

    outages = ["L01-02", "L01-39", "L02-03"]
    n1 = contingency(network, elements=outages, options=ContingencyOptions(max_cases=len(outages)))
    print("\nN-1 severity ranking")
    print("Criterion: descending unserved MW, then maximum branch loading percent.")
    for row in n1.rank().to_records():
        print(row.get("case_id", row.get("id")), row.get("unserved_mw"), row.get("max_loading_pct"))

    alternative = network.copy().set("L01-02", outserv=True)
    compared = loadflow(alternative)
    print(f"\nAlternative L01-02 out of service: {compared.status}")
    print(f"Base mismatch: {base.summary.get('max_mismatch_mva')} MVA")
    print(f"Alternative mismatch: {compared.summary.get('max_mismatch_mva')} MVA")


if __name__ == "__main__":
    main()
