# Command-line interface

The installed command is `power-machine`. JSON output uses schema `power-machine-cli-v1`. Exit codes are 0 for success, 2 for invalid input, 3 for a failed study, and 4 for partial coverage.

```console
power-machine --version
power-machine cases --json
power-machine doctor --json
power-machine lf ieee39 --json
```

## Commands

`sc` requires a source JSON file. `rms`, `cct`, and `qds` each require their documented input document. The command returns partial coverage instead of fabricating unavailable data.

## Batch manifest

`batch` reads a JSON object with `case`, `study`, `scenarios`, optional `study_options`, and optional `options`. The study is `loadflow` or `contingency`. Scenario fields are documented in [batch.md](batch.md). Outputs default below the configured user output directory.

Use [mcp.md](mcp.md) for an agent-facing alternative and [gui.md](gui.md) for the desktop workflow.


Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory. Dependent features report `pf_reference_data_not_included` when data is absent.
