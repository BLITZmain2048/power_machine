# PowerFactory composite dynamics

`DynamicCatalog.compile_frame(composite_path, adapters=...)` executes a frame
instance from a `pf-dynamic-v1` catalog. It returns a `Frame` with the same
`evaluate(t_s, x, inputs) -> (outputs, dx)` convention as compiled DSL.
`n_states` gives the length of the concatenated state vector.

## Binding and wiring

The ElmComp `typ_id` identifies its frame BlkDef. Each `pblk[i]` names a
BlkSlot and `pelm[i]` names its assigned object. Both vectors must have equal
length. Null entries remain visible in `Frame.bindings`. An unused null slot
is allowed, while a connected null slot is an error. Repeated element
references share one state allocation and one evaluation. Distinct input
drivers on aliases are rejected. Repeated slot references are invalid.

Port declarations retain their grouping: `a;b,c` has two groups, the first
with two members. `inodfrom` and `inodto` are zero based group indices.
`iconfrom` and `iconto` select 1 for sInput and 2 for sOutput. Corresponding
members of equal width groups are wired in order. The frame boundary reverses
input/output source roles. Reversed graphical segments are normalized by
these roles. No signal is inferred from a BlkSig graphical name.

Nested ElmComp assignments are expanded into the same signal graph. Recursive
composite references, unknown endpoints, invalid indices, unequal group
widths, connected nulls and competing drivers fail explicitly.

An Input BlkRef is a context alias, not an empty executable DSL block.
Its referenced type must declare `loc_name=Input` and sOutput ports. The
reference's sParams (or sOutput when sParams is absent) supplies the matching
parent input names positionally. Missing or unknown context bindings fail.
This is an explicit normalized import contract, not an inference from a
vendor macro's graphical name.

## Algebraic evaluation and initialization

Each evaluation starts with fresh signal values. Wires and ready algebraic
outputs are propagated until no further values can be computed. DSL outputs
that depend only on states can be published before their derivative inputs
arrive. A dynamic feedback connection therefore does not imply an algebraic
loop. Native adapters can publish the same information through `partial`.
Unresolved cycles or missing inputs raise FrameError with instance and port
names. No fixed point iteration silently substitutes delayed signals.

`initialize(known, tolerance=1e-8)` accepts load flow constraints keyed by
`(instance_path, 'in' or 'out', port_name)`. Constraints propagate both ways
along wires. Compiled DSL inc equations and native initializers infer states
and references from known outputs. For example an imposed machine ve fixes
an AVR efd, then `inc(x)=efd` fixes its lag state. An underdetermined inverse
is an error. After initialization all known values are checked against a
fresh evaluation and every derivative must satisfy the requested tolerance.

## Network and Python adapters

Adapters are registered by physical instance path. Textual ElmDsl objects
compile automatically. Native cores and external network objects require
explicit adapters, so an exported DLL containing only initializers is never
mistaken for an executable vendor controller.

`NativeBlock(inputs, outputs, n_states, run, start, partial=None, accept=None)`
wraps any existing Python core. `run(t,x,u)` returns a mapping and a derivative
vector. `start(known)` returns states and inferred signal values, or raises
KeyError until its required constraints are available. `partial(t,x,u)`
returns outputs whose dependencies are already available. Only `accept`
commits discrete memories. `Frame.accept_step` also forwards accepted steps
to compiled DSL blocks. This hook must be wired for IEC cores with discrete
FRT/protection memory. A trial evaluate must never commit such memory.

Built-in factories wrap PSS2B (`Vs1`, `Vs2` to `Vst`) and IEC Measurement
(`u,i,p,q,f`). Other IEC modules use the same explicit adapter protocol to
retain their mode choices, port bases, reference inputs and acceptance hooks.
No selection is inferred from a PF type name or a PF input enumeration.

`NativeBlock.synchronous(machine, voltage, power_mva)` integrates an existing
SynchronousMachine against an imposed bus phasor. It initializes from LF,
keeps mechanical power at its LF value and takes ve from the frame. Outputs
are ut, speed deviation from 1 pu, P, Q, id and iq. P/Q use total machine MVA
base, ut uses machine voltage base. A private machine copy keeps trial
commands separate from the caller's machine.

`NetworkBlock(kind, read, load_flow)` is a boundary adapter for a surrounding
network solver. `read(t)` supplies observations, and `load_flow` supplies
initial observations and commands. The solver retains responsibility for
updating network observations for each trial state. `Frame.commands()`
returns the most recently evaluated commands without applying them:

| PF class | Command inputs | Observation outputs |
| --- | --- | --- |
| ElmSym | ve, pt, fref | ut, speed, P, Q, f, id, iq |
| ElmGenstat / ElmPvsys | ip, iq, Pext, Qext | ut, P, Q, f |
| StaVmea | none | U, ur, ui |
| StaPqmea | none | P, Q |
| StaFrqmea | none | f |

Only wired command inputs are required on network boundaries. Observations
and commands use explicitly supplied equipment bases and positive generation.
The boundary adapter does not convert MW, Hz or rotor speed implicitly.

## Optional curves

`DynamicCatalog.curve_map` reads alphabetically ordered array_/oarray_
characteristics from paired matrix columns. The header's first column of
each pair contains the point count, followed by x,y rows. Data must be finite
and ordered. An oarray count of zero means absent, and an entirely absent
matrix is allowed when all declared curves are optional. Mandatory arrays
still require complete data. No default curve or zero curve is fabricated.
Inactive `select` branches need not supply their optional curves. Selecting
a branch that needs an absent curve fails explicitly. Native adapter factories
must enforce the same requirement for their selected curve modes.
General matrix_ and omatrix_ parameters remain unsupported and are rejected.

## Synthetic verification

`tests/test_frames.py` covers AVR/PSS feedback with both network observations
and an integrated synchronous machine against an infinite bus, LF backward
initialization, RK4 step refinement, grouped ports, a nested PPC and Input
context macro feeding a static generator, IEC Measurement, algebraic cycles,
nulls and aliased elements. These are synthetic tests, not vendor parity
or plant validation. Run from the repository root:

```text
python -m pytest -q --ignore=tests/gui
```


Includes importers and comparators for user-supplied PowerFactory exports; no PowerFactory data or results are distributed. Set `POWER_MACHINE_PF_IEEE39` to your export directory. Dependent features report `pf_reference_data_not_included` when data is absent.
