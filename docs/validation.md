# Validation

Run `python -m validation` from the repository root. Without external data it runs analytical checks and classical temporal convergence and reports PowerFactory comparisons as **not included**. Configure `POWER_MACHINE_OUTPUT_DIR` or pass `--output` to select the destination. Existing runs are preserved.

Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory to enable optional comparisons. Tolerances are copied before execution. An unavailable comparison does not establish agreement.

See [capabilities](../validation/CAPABILITIES.md) and [dynamic provenance](../validation/DYNAMIC_DATA_PROVENANCE.md).
