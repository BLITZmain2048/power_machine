# RMS boundary playback

`power_machine.playback.run` simulates a subsystem driven by measured boundary
voltages. It uses only the standard library and NumPy and requires no external
simulator. This is an RMS phasor experiment, not an electromagnetic transient model.

```python
from power_machine.playback import BoundarySeries, run
from power_machine.api import RmsOptions

# network is an API Network with explicit dynamic data and a dynamic manifest.
voltages = {
    "boundary_bus_id": BoundarySeries(
        times=[0., .02, .02, .06, .06, .10],
        angular_reference="common nominal 60 Hz frame, angle zero at bus R at t=0",
        u_pu=[1., 1., .8, .8, 1., 1.],
        angle_deg=[0., 0., -5., -5., 2., 2.],
        discontinuities=(.02, .06),
    )
}
result = run(network, voltages, step_s=.001, t_end_s=.10,
             options=RmsOptions(linear_solver="numpy"))
rows = result.samples.to_records()
print(result.summary["ms_per_step"])
```

Inputs are keyed by model bus ID. Each value is a `BoundarySeries` or a dictionary
with its constructor fields. Use either `u_pu` and `angle_deg`, or `phasors`
(complex p.u. samples). Voltage is on the individual bus nominal line-to-line kV
base. All signals must declare the same nonempty angular reference. The label
records the caller's declaration, it does not transform independently referenced
angles. Use the nominal synchronous frame of the dynamic model. Do not subtract
a moving reference-bus angle from recorded voltages.

Times are finite and ordered. Each declared discontinuity must have exactly two
samples at the same interior time, first the left limit and then the right limit.
Other duplicate times are rejected. The solver lands on each discontinuity,
records both limits, preserves differential states and resolves algebraic states.
It interpolates magnitude and unwrapped angle linearly inside each continuous
segment, including for complex inputs. It never interpolates across a jump.
NaNs, missing samples, incompatible shapes and extrapolation are errors. Angular
sampling must resolve changes below 180 degrees between adjacent samples.

Simulation starts at zero. `t_end_s` defaults to the earliest last sample among
boundaries. `step_s` is the maximum continuous step, events may shorten it.
`events` accepts engine `dynamics.events.Event` records, with its validated kinds
and units. Events outside the interval are rejected. A bolted fault at a prescribed
boundary is rejected. Apply faults outside the extracted subsystem through the
recorded boundary signal instead.

At each boundary node the real and imaginary KCL equations are replaced by
`Re(V)-Re(Vb(t))=0` and `Im(V)-Im(Vb(t))=0`. The corresponding Jacobian rows are
identity rows. Ybus retains physical admittances. The replaced complex KCL
residual `I_devices - Ybus @ V` is the current exported by the subsystem.

Results are the public `RmsResult` with an in-memory samples table. Channels are:

- `time_s`
- `boundary.<bus>.P_MW`, `Q_Mvar`, `I_kA`
- `bus.<bus>.U_pu`, `angle_deg`
- `state.<device>.<local_index>` for all differential states
- `device.<device>.P_MW`, `Q_Mvar` for single-port device injections

Boundary P and Q are positive **export from the subsystem**. Current magnitude
is nonnegative. `S = V * conj(I) * Sbase` and
`I_kA = abs(I_pu) * Sbase_MVA / (sqrt(3) * Un_kV)`. Passive loads therefore have
negative boundary P and Q. Device powers are positive injection into the network.
The summary includes wall time, time per accepted step, step retries and event
sample indices. `output_every` decimates continuous samples but preserves both
event limits. With `options.output` explicitly set, CSV and JSON are written to
that directory. Without it, playback performs no file writes. `monitored` is not
used by playback, which always returns the documented channels.

For an API `Network`, the initialization solves subsystem PQ/PV load flow with
all boundary phasors fixed to their values at zero. It does not enforce generator
Q limits. Local generators at boundary nodes retain their scheduled injections,
with the prescribed source supplying the imbalance. Conflicting PV setpoints are
rejected. The dynamic manifest is required just as for the ordinary RMS runner.

For an already assembled dynamic model, pass an object with `.solver` and
`.initial` (or `.state`), for example `SimpleNamespace(solver=solver,
initial=initial)`. This path supports specialized devices such as the native
IEEE 39 machines without translating their model. The initial state must already
match the subsystem load flow with fixed boundary phasors. Playback checks initial
voltage constraints, remaining KCL, local algebraic residuals and derivatives
against 1e-7. It rejects an inconsistent initial condition rather than silently
moving machine states. The assembled solver belongs to this run and must not be
shared concurrently. The input initial-state arrays are copied.

Fused boundary buses generate warnings. Two prescribed boundaries on the same
fused node are rejected because individual currents are not identifiable.
Multiple islands generate a warning and every island must have a prescribed
boundary. Topology is checked again after network events. Prescribed voltage
sources keep passive islands energized when switching removes other sources.

Reproduce the native validation from the repository root:

```console
python -m benchmarks.playback_ieee39
python -m pytest -q tests/test_playback.py
```

The IEEE 39 experiment uses the native `stage="loads"` model, including machine
flux states and dynamic loads, with excitation and mechanical power held fixed.
It retains buses 6, 7, 8, 10, 11, 31 and 32 and imposes voltages on buses 5, 9,
12 and 13. Only branches with an interior endpoint are retained. Loads and
machines belong to the interior. The full system has a bolted fault on bus 16
at 0.02 s and clearance at 0.06 s, ending at 0.10 s with a 0.01 s step. Both
event limits are recorded. Cut powers are compared with retained-branch terminal
powers evaluated on the full trajectory and all 15 retained states are compared.
This checks replay equivalence at the same discrete integration schedule. It does
not establish time-step convergence or validation of the full AVR/governor model.
