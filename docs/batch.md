# Batch campaigns

`power_machine.batch` runs independent load-flow or contingency scenarios using a persistent `spawn` process pool. Each worker initializes its own model snapshot, limits BLAS threads, and can prepare the network once. Scenario IDs, seeds, checkpoints, cancellation, and isolated failures remain explicit.

```python
from power_machine import load_case
from power_machine.batch import BatchOptions, Scenario, run_batch

def main():
    model = load_case("ieee39")
    scenarios = [Scenario(id="base"), Scenario(id="line-out", out_of_service=("L01-02",))]
    result = run_batch(model, "loadflow", scenarios, options=BatchOptions(workers=1))
    print([row["status"] for row in result.to_records()])

if __name__ == "__main__":
    main()
```

On Windows, `spawn` requires this entry-point guard when `BatchOptions(workers > 1)` starts child processes. Keep the guard even when first testing with one worker.

`Scenario` accepts `out_of_service`, `p_mw`, `q_mvar`, `setpoints`, `load_multipliers`, `seed`, and low-level supported overrides. `BatchOptions` controls `workers`, `chunk_size`, `result_detail`, failure policy, `resume`, seed, BLAS threads, and timeout. `BatchResult` provides `to_records()`, `to_csv()`, and `worst()`.

Only independent `loadflow` and `contingency` scenarios are supported. A sequential quasi-dynamic series is a different workflow and uses `quasi_dynamic`. Results from a resumed run are accepted only when the model, study options, and scenario hashes agree.

One recorded IEEE 39 experiment produced identical result fingerprints at 1, 2, 4, and 8 processes. Its shared-machine throughput varied, so it is evidence of deterministic execution rather than a general scaling claim.


Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory. Dependent features report `pf_reference_data_not_included` when data is absent.
