# Comparison evidence and limitations

The repository includes specific IEEE 39 comparisons, not a global claim for arbitrary networks or profiles. Run the reproducible evidence command from the repository root:

```console
python -m validation
```

Versioned tolerances are copied before the run. The command writes a new report under `validation/results/`. That generated output is not publication source material.

| Study and profile | Evidence | Result |
| --- | --- | --- |
| AC load flow IEEE 39 | Included PowerFactory comparison E39-LF-01 | 1.215e-9 p.u. maximum voltage error, 4.237e-7 degrees, and 4.205e-6 Mvar branch reactive-flow error |
| Network editing | Export E39-NET-01 absent from resources | Not compared |
| 3-phase short circuit `iec_pf2016_c1_v1` | Included 39-bus PowerFactory comparison | 2.600e-8 maximum relative Ik'', 2.587e-8 ip, and 7.176e-8 Ib error |
| Classical RMS | Internal parity and analytical balance | Not compared with PowerFactory and distinct from native RMS |
| Native RMS, 166 states | E39-DIN-01/02/03 through 1.2 s | Partial: 26/29, 26/29, and 27/29 channels meet their published maximum criterion |
| Analytical cases | Closed-form checks | Seven cases pass at 1e-9 absolute and relative tolerance |

The aggregate evidence outcome remains false by design because the network-editing reference is absent and native RMS has incomplete or out-of-criterion channels. Do not replace those states with a global label. h, h/2, and h/4 checks are published for smooth intervals and CCT B16 resolution, not as a universal event-crossing order claim.

See `validation/CAPABILITIES.md` for profile-level coverage and `validation/DYNAMIC_DATA_PROVENANCE.md` for dynamic-parameter provenance.
