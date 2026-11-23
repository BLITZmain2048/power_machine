# Performance

The values below are clean, configuration-specific medians from 18 November 2026.

## Environment

- CPU: Intel64 Family 6 Model 186, 10 physical cores and 16 logical processors
- Memory: 15.6 GiB
- System: Windows 10
- Python: 3.10.6
- NumPy: 2.2.6
- BLAS threads: `OPENBLAS_NUM_THREADS=1`, `MKL_NUM_THREADS=1`, and `OMP_NUM_THREADS=1`

## Protocol and reproduction

Each series used five samples. Before each series, `psutil.cpu_percent(interval=30)` had to remain below 15%. The before and after measurements alternated on every repetition. Reproduce a new measurement set with `python benchmarks/run_benchmarks.py --study all --backend both --repeats 5 --output outputs/benchmarks.json`. The benchmark prepares each case and performs one warm-up outside the measured samples. N-1 evaluates the 46 IEEE 39 branches and sparse sizes require SciPy. The command measures the current environment and does not reconstruct a historical engine snapshot or guarantee a median from different hardware.

| Workload | Median |
| --- | ---: |
| IEEE 39 warm load flow | 2.76 ms |
| IEEE 39 N-1, `island_reference="none"` | 170.59 ms |
| IEEE 39 N-1, `largest_generator` | 174.14 ms |
| IEEE 39 39-bus short-circuit sweep | 26.66 ms |
| Synthetic 200-bus load flow | 21.77 ms |
| Synthetic 1,000-bus load flow | 645.81 ms |
| Synthetic 2,000-bus load flow | 3.91 s |

Synthetic networks are generated only for scaling observation. Each is a ring with chords `i → i+1` and `i → i+floor(n/17)`, branch impedance `0.01+j0.10` p.u., shunt admittance `-j0.02` p.u. per bus, bus 0 as the reference, no PV buses, and injections constructed from a known voltage state. They are not electrical reference cases.

## Linux SciPy measurements

The public [Linux benchmark samples](benchmarks/linux_2026-11-18.json) were collected on Linux x86_64 with an Intel Xeon at 2.10 GHz, 2 vCPU, Python 3.13.18, NumPy 2.5.3, and SciPy 1.18.1. The exact command was `python benchmarks/run_benchmarks.py --study all --backend both --repeats 5`. It uses the same synthetic generator described above, records one warm-up outside the five measured samples, and holds BLAS threads at one. Preparation and compilation time are excluded from the reported samples. These dense and sparse results are measurements on this Linux system only and are not compared with the Windows measurements above.

| Workload | Dense | Sparse |
| --- | ---: | ---: |
| IEEE 39 load flow | 3.1 ms | 6.9 ms |
| IEEE 39 N-1 | 0.154 s | 0.177 s |
| IEEE 39 39-bus short-circuit sweep | 19.1 ms | 19.6 ms |
| Synthetic 200-bus load flow | 20.6 ms | 17.2 ms |
| Synthetic 1,000-bus load flow | 0.690 s | 0.067 s |
| Synthetic 2,000-bus load flow | 4.010 s | 0.125 s |
| Synthetic 5,000-bus load flow | Not measured | 0.323 s |
| Synthetic 10,000-bus load flow | Not measured | 0.618 s |

All completed load-flow runs converged. The 5,000- and 10,000-bus sparse runs converged in three iterations. On the same Linux environment, the wheel built and installed in a clean environment outside the repository, `power-machine doctor` loaded IEEE 39, and the README tour reported `2.18e-10`, `partial 56`, and `L21-22 157.72`.

Earlier profiling identified model copying, result materialization, and diagnostic condition-number work as important costs in static campaigns. Use the same model, options, hardware, thread limits, and output detail before comparing runs. These measurements do not compare Power Machine with another product.
