# Validation

Run `python -m validation` from the repository root. Without external data it runs analytical checks and classical temporal convergence and reports reference simulator comparisons as **not included**. Configure `POWER_MACHINE_OUTPUT_DIR` or pass `--output` to select the destination. Existing runs are preserved.

The importers accept user-supplied CSV element-table exports. Comparisons use external references supplied by the user. External reference data and results are not distributed. Set `POWER_MACHINE_REF_IEEE39` to your export directory to enable optional comparisons. Tolerances are copied before execution. An unavailable comparison does not establish agreement.

See [capabilities](../validation/CAPABILITIES.md) and [dynamic provenance](../validation/DYNAMIC_DATA_PROVENANCE.md).
