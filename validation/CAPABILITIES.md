# Study capabilities

| Study | Data and scope |
| --- | --- |
| AC load flow | Included MATPOWER case39 and user networks |
| Classical RMS IEEE 39 | Included provisional classical machine data. It has **no** PF comparison label |
| Native RMS IEEE 39 | Requires user-supplied network and dynamic exports |
| Short circuit | Explicit source and sequence data; included case39 assumptions are provisional |
| Modal | Requires compatible dynamic models |

Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed.
Set POWER_MACHINE_PF_IEEE39 to supply external data. Missing optional data is reported as pf_reference_data_not_included.
