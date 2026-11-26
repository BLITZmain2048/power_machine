# MCP server

Power Machine exposes a stdio Model Context Protocol server through `power-machine-mcp` or `python -m power_machine.mcp`. Standard output is reserved for JSON-RPC. Diagnostics go to standard error.

## MCP client configuration

After installing the package into the selected Python environment, add this to your desktop MCP client:

```json
{
  "mcpServers": {
    "power-machine": {
      "command": "power-machine-mcp"
    }
  }
}
```

For a command-line MCP client:

```console
power-machine-mcp
```

For an uninstalled checkout, replace `power-machine-mcp` with the absolute path to the intended Python interpreter followed by `-m power_machine.mcp`, and set `PYTHONPATH` to the checkout root. Do not put a personal or project-workspace path in shared configuration.

## Tools, resources, and prompts

The server provides case and network lifecycle tools (`list_cases`, `load_case`, `list_networks`, `copy_network`, `close_network`), element inspection and editing, study preflight, and study tools for load flow, contingencies, short circuit, PV/QV, sensitivities, RMS, CCT, QSS, and batches. `get_result` supports row filtering, selected columns, cursors, and limits. `compare_results` compares stored results without judging an alternative feasible.

Resources use `power-machine://`: bundled IEEE 39, the network schema, a network summary, and persisted results. Prompts are `study_n1_contingencies`, `compare_operating_alternatives`, and `explain_missing_data`.

Call `study_capabilities` before an unfamiliar study. It reports missing data rather than making electrical assumptions. Tool schemas state required fields, units, enums, and examples through `tools/list`.

## Typical sequence

1. Call `load_case` with `name_or_path: "ieee39"` and retain the `network_id`.
2. Call `study_capabilities` for the selected study.
3. Run the study with explicit options and retain its `result_id`.
4. Page `get_result` when result tables are large.
5. Create an alternative with `copy_network`, edit only explicit attributes, then use `compare_results`.

The server keeps open networks in its session and never treats a comparison as engineering acceptance. Close a network when it is no longer needed.


Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory. Dependent features report `pf_reference_data_not_included` when data is absent.
