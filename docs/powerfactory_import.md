# Importing a PowerFactory CSV export

`import_powerfactory(csv_dir, overrides=None)` builds a portable `power_machine-model-v1` network from a user-provided CSV export. It does not open a project file or infer electrical, sequence, control, or dynamic parameters that are absent from the export.

The importer recognizes terminals, lines, two- and three-winding transformers, couplers, loads, generators, shunts, controllers, DSL objects, and related inventory where documented fields are present. Unsupported objects can remain as inventory rather than becoming calculations.

## Expected tables

Provide UTF-8 CSV tables named `objects.csv`, `parameters.csv`, `references.csv`, and `ports.csv`, either at the selected export root or under a portable CSV subdirectory. Objects need stable identifiers, classes, names, and relative paths. Parameters retain value and unit. References and ports preserve connectivity.

For a dynamic user profile, place `dynamic/manifest.json`, `objects.csv`, `attributes.csv`, and `references.csv` beside the imported model. The manifest hashes the tables. Missing or invalid dynamic information makes the relevant RMS request unavailable.

## Use

Pass the export directory to `import_powerfactory`, `load_case`, or `power-machine import-pf`. Save the resulting `Network` only to a user output destination. Preserve source identifiers, units, and provenance. Review importer warnings before relying on an interface or derived network. PowerFactory is a trademark of its owner. This project is not affiliated with or endorsed by that owner.

## Native passive elements (PF 2024)

The balanced positive-sequence LF and RMS network stamps use physical ohms and siemens before conversion to the system MVA base and each terminal's kV base. All data below are input attributes, never `m:` result channels. The reference circuits are DIgSILENT PowerFactory 2024 Technical References `Series Capacitor (ElmScap)`, sections 1-4, `Series Reactor (ElmSind)`, sections 1-4, and `Filter/Shunt (ElmShnt)`, sections 1.1-1.5 and 4. These manuals are distributed with PowerFactory. They are not redistributed here.

| Class | Connections | Supported native attributes and units |
| --- | --- | --- |
| `ElmScap` | `bus1`, `bus2` | `xcap` (positive magnitude, ohm), or `bcap` (S, explicit `uS` also accepted). Series reactance is negative. `gcap` (S, explicit `uS` accepted) or `tandc` gives parallel capacitor losses. Optional explicit series resistance `rcap` or `rrea` is in ohms. `ucn` (kV) and `Sn` (MVA) are ratings, not an impedance base when `xcap` is exported. |
| `ElmSind` | `bus1`, `bus2` | `xrea`, `rrea` (ohm), or `ucn` (kV), `Sn` (MVA), `uk` (% impedance magnitude), `Pcu` (kW copper loss). With `Zb=ucn**2/Sn`, `R=Pcu/(1000 Sn) Zb`, `X=sqrt((uk Zb/100)**2-R**2)`. Explicit layout components take precedence over rating-derived components. |
| `ElmShnt` | `bus1` | `shtype`, `ushnm` (kV), `ncapa` active steps, `ncapx` maximum steps. Layout fields: `xrea`, `rrea`, `rpara` (ohm), `bcap`, `gparac` (uS), `c1`, `c2` (uF). Design alternatives: `qrean`, `qcapn` (Mvar), `grea` quality factor, `tandc` loss tangent. |

No bypass attribute is listed in the PF 2024 Series Capacitor parameter table. If an export supplies an explicit boolean `bypass`, `bypassed`, or `is_bypassed`, the importer preserves it and the network contracts the branch as an ideal connection when it equals 1. These are accepted export aliases, not claimed universal PF native names. Missing bypass means inserted. Out-of-service elements and open terminal switches remain disconnected. Capacitor losses and declared series resistance are included. Nonlinear MOV and spark-gap protection are outside the static LF/RMS network stamp.

### Shunt circuits

The native `shtype` enumeration is:

| Value | Circuit | Required circuit data |
| --- | --- | --- |
| 0 | Series R-L-C | Reactor data and capacitor data |
| 1 | Series R-L | `xrea` or `qrean`, and `rrea` or `grea` |
| 2 | C with optional dielectric losses | `bcap` or `qcapn`, optional `gparac` or `tandc` |
| 3 | C in series with `(R-L) parallel Rp` | Reactor data, capacitor data, `rpara` |
| 4 | Main C2 in series with `((R-L-C1) parallel Rp)` | `xrea` or `qrean`, `rrea` or `grea`, `c1`, `c2`, `rpara`, nominal frequency |

`rpara=0` means the parallel resistor is absent, as defined by PF. It does not short the R-L arm. `grea=0` explicitly selects zero reactor resistance. The model's `meta.f_nom_hz` supplies the C1/C2 circuit frequency to LF and RMS. Direct `y_shunt` calls can supply `f_nom_hz` as a keyword or an element parameter. SC uses the study frequency.

For three-phase star connections (`ctech=1` YN or `2` Y), `B=qcapn/ushnm**2` and `X=ushnm**2/qrean`. Delta (`ctech=0`) uses `B=qcapn/(3 ushnm**2)` and `X=3 ushnm**2/qrean`, with three times the per-phase layout admittance in the positive-sequence stamp. String aliases `3PH-D`, `3PH-YN`, `3PH-Y` are also accepted. Legacy exports without `ctech` use the star equivalent. Single-phase technologies and measurement-table shunts (`iTaps`) require a different model and raise an explicit error.

The total stamp is `ncapa * Y_step * U_bus**2/S_base`, including the delta multiplier when appropriate. `ncapx` bounds the active count and is not a second multiplier. Missing required circuit components raise `MissingParameter`. They never become a zero reactor or an unconnected capacitor. Optional undeclared capacitor losses select the ideal capacitor circuit.

### Exact zero arm of a three-winding transformer

The existing pair-test conversion gives `z_h=(z_hm+z_lh-z_ml)/2` and cyclic permutations on the common system base. For nonzero arms, the internal star node is eliminated by a Schur complement. If exactly one arm `k` is zero, its ideal winding imposes `v_star=v_k/a_k`, including nominal ratio, phase shift and tap factor.

For every other arm `i`, define a row `c_i` with entries `1/a_i` at `i` and `-1/a_k` at `k`. The terminal matrix is

`Y = sum_i conjugate(c_i)^T c_i / z_i + (ym/|a_k|**2) e_k e_k^T`.

This is direct substitution of the ideal constraint. It adds no small impedance and retains terminal currents. Numerical cancellation within the floating-point summation error (`machine_epsilon * sum(abs(z_pair))`) is classified as exactly zero before selecting this formulation. Multiple ideal arms need additional ratio constraints and raise an explicit error. LF, compiled LF, RMS and the native positive-sequence fault path share this formulation.

### Short-circuit scope

For `ElmScap`, `ElmSind` and `ElmTr3`, native positive-sequence parameters are used when no supplementary branch record is supplied. Three-phase and line-to-line faults use these matrices. Negative sequence uses the transposed matrix to reverse transformer phase shift. Explicit shunts contribute to the sequence admittance matrix. Sources still require identified supplementary SC data. Transformer magnetization and ordinary loads are omitted in SC.

Ground faults require zero-sequence and grounding models. These are not inferred from positive sequence. Missing native zero-sequence support is reported explicitly. Series capacitor and reactor supplementary impedance records remain supported with explicit `z1`, `z2`, `z0` as required by the fault. Native Tr3 is supported through the matrix path. IEC equivalent-frequency peak and breaking corrections are not implemented for these native matrix circuits or explicit shunts. Initial symmetrical current remains available and unsupported additional quantities remain null with diagnostics.

### Missing line conductance

`ElmLne` still requires exported conductance. Export either `G1` (total uS), or `TypLne.gline` (uS/km) together with `dline` (km). The latter is multiplied by length before applying `nlnum` once. A missing-data error names both alternatives. Missing conductance is never silently set to zero.
# Native external grid (ElmXnet)

ElmXnet is imported into `gens` through its native `bus1` port. Original parameters and units are preserved, including `bustp`, `usetp`, `phiini`, `pgini`, `qgini`, `snss`, `snssmin`, `rntxn`, `rntxnmin` and `cgnd`. SL fixes voltage magnitude and angle, PV fixes P and voltage magnitude, PQ fixes P and Q. Public powers use MW and Mvar. External grids do not require a fictitious machine count or rating for load flow.

Balanced initial short-circuit calculations use `Z1 = c Un² / Sk` and the declared R/X ratio. `ikss` and `ikssmin` in kA are alternatives to Sk in MVA. `xntrn` and `xntrnmin` are X/R alternatives when R/X is absent. Minimum data never fall back to maximum data. Supplementary source records take precedence when present. Native ground-fault sequence and neutral models remain explicitly unsupported. Preserving `cgnd` does not infer a grounding impedance. Negative sequence currently uses the reciprocal balanced equivalent.

The RMS adapter initializes its internal EMF from the accepted load flow using `E = V + Z conj(S/V)`. The default is a fixed internal voltage behind the declared positive-sequence impedance. With exported `tag` in seconds, it can use a classical two-state swing model. The PF2024 acceleration base is `tag = 2H` and its equivalent rating is `0.2 sqrt(1+(R/X)²) Sk/cmax`. Thus inertia requires explicit `snss`, `rntxn` and `cmax`. `rms_fixed_voltage` selects fixed frequency even when tag is exported. This adapter does not reproduce PF's subtransient flux model, excitation, primary or secondary frequency control. Unsupported control fidelity must not be interpreted as validated PowerFactory RMS equivalence.

Checked against the installed PowerFactory 2024 Technical Reference, External Grid, sections 2, 3 and 5, especially equations 38 and 75. No manual is distributed with this repository. Public references are [DIgSILENT RMS external grid](https://www.digsilent.de/en/faq-reader-powerfactory/how-is-the-external-grid-modeled-for-rms-simulation.html), [DIgSILENT short-circuit equivalent](https://www.digsilent.de/en/faq-reader-powerfactory/how-to-set-up-a-model-of-hv-grid-as-an-external-grid-for-short-circuit-calculation.html) and [pandapower's native PowerFactory importer](https://github.com/e2nIEE/pandapower/blob/develop/pandapower/converter/powerfactory/pp_import_functions.py).

