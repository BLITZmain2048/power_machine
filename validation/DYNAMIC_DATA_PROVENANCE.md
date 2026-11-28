# IEEE 39 classical dynamic-data provenance review

`cases/ieee39/dynamic_data.json` labels every inertia constant `H` and transient reactance `X'd` as `new_england_39_classical_parameters`. It also points at the package-relative case locator `cases/ieee39/case39.py#GEN[n]`.

The repository contains neither of the digitised primary tables from the
candidate references. The exact table number, page, edition,
machine ordering, and a verbatim primary-source transcription are therefore
not verified. Every value in the table below remains provisional.

| Machine | H [s] | X'd [p.u., 100 MVA] | Literature review status |
| --- | ---: | ---: | --- |
| G30 | 42.0 | 0.0310 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G31 | 30.3 | 0.0697 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G32 | 35.8 | 0.0531 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G33 | 28.6 | 0.0436 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G34 | 26.0 | 0.1320 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G35 | 34.8 | 0.0500 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G36 | 26.4 | 0.0490 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G37 | 24.3 | 0.0570 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G38 | 34.5 | 0.0570 | Provisional value. Primary-table source and machine ordering have not been verified. |
| G39 | 500.0 | 0.0060 | Provisional value. Primary-table source and machine ordering have not been verified. |

The candidate primary sources are *A Practical Method for the Direct Analysis of Transient Stability* (IEEE PAS, 1979) and *Energy Function Analysis for Power System Stability* (1989). Full citations appear in [NOTICE](../NOTICE).

The source record remains provisional until the exact edition, table or page and machine ordering have been checked against primary material. The neutral locator is a mapping aid and is not a primary source for inertia or transient reactance. Numerical values remain unchanged.


The importers accept user-supplied CSV element-table exports. Comparisons use external references supplied by the user. External reference data and results are not distributed. Set `POWER_MACHINE_REF_IEEE39` to your export directory. Dependent features report `reference_data_not_included` when data is absent.
