# Python API

Import the public surface as `power_machine`. Every study result has `status`, `diagnostics`, `coverage`, and `provenance`. Stable result tables expose `to_records()`, `select()`, `to_csv()`, and optional `to_dataframe()`.

```python
import power_machine as pm
net = pm.load_case("ieee39")
result = pm.loadflow(net)
result.require_complete()
print(result.buses.select("id", "u_pu").to_records()[0])
print(result.summary["max_mismatch_mva"])
print(net.summary()["counts"])
```

## Networks and errors

`list_cases()` lists bundled cases and `load_case(name_or_path)` loads `ieee39`, a model JSON file, or a documented CSV export. `Network` is editable in memory: use `get`, `elements`, `set`, `switch`, `copy`, `to_model`, and `save`. `to_model()` always returns a deep copy.

Public exceptions are `MissingStudyDataError`, `UnsupportedStudyError`, `InvalidModelError`, and `UnknownElementError`. Each provides a stable `code`, affected `field`, and suggested `action` through `to_dict()`.

## Study functions

| Function | Result | Important inputs |
| --- | --- | --- |
| `loadflow(net, options=None)` | `LoadflowResult` | `LoadflowOptions` uses p.u. tolerance and Q limits |
| `contingency(net, elements=None, criterion="N-1", options=None)` | `ContingencyResult` | `ContingencyOptions`, including `island_reference` |
| `shortcircuit(net, bus=None, fault="3ph", sc_data=..., options=None)` | `ShortCircuitResult` | Explicit source document and a `ShortCircuitOptions` profile |
| `pv_curve`, `qv_curve`, `sensitivities` | `GenericStudyResult` | Explicit study inputs |
| `rms(net, events, t_end_s=..., step_s=..., options=None)` | `RmsResult` | Seconds and an eligible dynamic profile |
| `cct(net, fault, options=None)` | `CctResult` | Explicit fault and documented classical IEEE 39 profile |
| `quasi_dynamic(net, series, options=None)` | `QuasiDynamicResult` | User `Series`, complete load factors, new output destination |
| `compare_powerfactory(net, directory, study="loadflow")` | `GenericStudyResult` | Complete compatible export |

Use `result.require_complete()` only when the intended decision requires full coverage. Electrical limit violations and missing study coverage are different conditions.

## Options and tables

Options are dataclasses with units in their names and docstrings: `LoadflowOptions`, `ContingencyOptions`, `ShortCircuitOptions`, `RmsOptions`, `CctOptions`, and `QuasiDynamicOptions`. `RmsOptions.linear_solver` accepts `compatibility`, `numpy`, `scipy`, or `auto`. The SciPy choice requires the optional extra.

`StudyTable.to_csv()` writes relative paths below the configured user output directory. `StudyTable.to_dataframe()` needs `power-machine[tables]`.

## Short circuit and output data

Short circuit never creates source or sequence parameters. Pass a `ShortCircuitData`-compatible object or JSON path through `sc_data`. The included IEEE 39 supplement is appropriate only for the documented comparison profile, not as data for an arbitrary imported network.

Set `POWER_MACHINE_OUTPUT_DIR` to choose where user-created networks, tables, RMS, QSS, and batch outputs are written. Otherwise the library uses its default user result directory. Keep generated results outside the source checkout.

For CLI equivalents, see [cli.md](cli.md). For the portable model schema, see [network_format.md](network_format.md).
