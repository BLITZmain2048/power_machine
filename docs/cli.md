# Command-line interface

The installed command is `power-machine`. JSON output uses schema `power-machine-cli-v1`. Exit codes are 0 for success, 2 for invalid input, 3 for a failed study, and 4 for partial coverage.

```console
power-machine --version
power-machine cases --json
power-machine doctor --json
power-machine lf ieee39 --json
```

## Commands

| Command | Purpose |
| --- | --- |
| `cases` | List included cases |
| `doctor` | Check the bundled IEEE 39 case and optional dependencies |
| `lf CASE` | Run AC load flow |
| `n1 CASE` | Run N-1 contingencies, optionally constrained with `--elements` and `--max-cases` |
| `sc CASE --sc-data FILE` | Run a single fault or a sweep with explicit source data |
| `rms CASE --events FILE --t-end-s S` | Run RMS with seconds-based times |
| `cct CASE --fault FILE` | Run the documented CCT profile |
| `qds CASE --series FILE` | Run a sequential quasi-dynamic series |
| `import-pf DIRECTORY -o PATH` | Build and save a network from a CSV export |
| `compare-pf CASE DIRECTORY` | Compare against a compatible export |
| `batch MANIFEST` | Run load-flow or contingency scenarios |
| `gui [CASE]` | Open the desktop application |
| `mcp` | Start the stdio MCP server |

`sc` requires a source JSON file. `rms`, `cct`, and `qds` each require their documented input document. The command returns partial coverage instead of fabricating unavailable data.

## Batch manifest

`batch` reads a JSON object with `case`, `study`, `scenarios`, optional `study_options`, and optional `options`. The study is `loadflow` or `contingency`. Scenario fields are documented in [batch.md](batch.md). Outputs default below the configured user output directory.

Use [mcp.md](mcp.md) for an agent-facing alternative and [gui.md](gui.md) for the desktop workflow.
