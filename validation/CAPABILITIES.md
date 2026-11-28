# Study capabilities

| Study | Data and scope |
| --- | --- |
| AC load flow | Included MATPOWER case39 and user networks |
| Classical RMS IEEE 39 | Included provisional classical machine data. It has **no** external comparison label |
| Native RMS IEEE 39 | Requires user-supplied network and dynamic exports |
| Short circuit | Explicit source and sequence data; included case39 assumptions are provisional |
| Modal | Requires compatible dynamic models |

The importers accept user-supplied CSV element-table exports. Comparisons use external references supplied by the user. External reference data and results are not distributed.
Set POWER_MACHINE_REF_IEEE39 to supply external data. Missing optional data is reported as reference_data_not_included.
