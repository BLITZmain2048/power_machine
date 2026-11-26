"""Reproduce static study timings without external cases or new dependencies.

Example: python benchmarks/run_benchmarks.py --backend both --output timings.json
Preparation and one warm-up are excluded. Five wall/CPU samples are recorded
per study/backend/size. Synthetic data are a timing fixture, not a grid model.
Use --sizes 5 --repeats 1 for a short smoke run. Sparse runs require SciPy.
"""
from __future__ import annotations
import argparse
import contextlib
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import platform
import statistics
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
THREAD_KEYS = ("OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "OMP_NUM_THREADS")

def synthetic(n):
    """Ring/chord network, 100 MVA, 100 kV, known-voltage net injections."""
    import numpy as np
    if n < 5:
        raise ValueError("Synthetic network requires at least five buses")
    edges=sorted({tuple(sorted((i,(i+jump)%n))) for i in range(n) for jump in (1,max(2,n//17))})
    voltage=(1+.01*np.cos(np.arange(n)*2*np.pi/n))*np.exp(1j*.03*np.sin(np.arange(n)*2*np.pi/n))
    current=-.02j*voltage.copy()
    for a,b in edges:
        branch=(voltage[a]-voltage[b])/complex(.01,.1)
        current[a]+=branch
        current[b]-=branch
    power=voltage*np.conj(current)*100
    model={'meta':{'s_base_mva':100.,'f_nom_hz':50.,'benchmark':'synthetic_timing_only'},
      'buses':[{'id':str(i),'name':str(i),'un_kv':100.,'outserv':0} for i in range(n)],
      'branches':[{'id':'L'+str(i),'name':'L'+str(i),'class':'ElmLne','bus_from':str(a),'bus_to':str(b),'outserv':0,
                   'params':{'R1':1.,'X1':10.,'B1':-100.,'G1':0.,'nlnum':1}} for i,(a,b) in enumerate(edges)],
      'branches3':[],'switches':[],'shunts':[],
      'loads':[{'id':'D'+str(i),'name':'D'+str(i),'class':'ElmLod','bus':str(i),
                'params':{'plini':float(-power[i].real),'qlini':float(-power[i].imag),'scale0':1.}} for i in range(1,n)],
      'gens':[{'id':'G0','name':'G0','class':'ElmSym','bus':'0','ref':True,'mode':'constv','v_set_pu':1.01,
               'params':{'pgini':0.,'qgini':0.,'ngnum':1.,'scale0':1.}}]}
    return model


def make_job(study, size, backend, island_reference):
    from power_machine.benchmarks.ieee39 import build_model
    from power_machine.loadflow import run_loadflow
    model = synthetic(size) if study == "synthetic" else build_model()
    if study == "n1":
        from power_machine.contingency import run_contingencies, ContingencyOptions
        options = ContingencyOptions(q_limits=False, island_reference=island_reference)
        return lambda: run_contingencies(model, {}, options,
            selected_ids=[b["id"] for b in model["branches"]],
            filter_reason="All 46 included IEEE 39 branches", ldf_options={"backend": backend})
    if study == "sc":
        from power_machine.sc_sweep import run_sweep
        from power_machine.resources import required_readonly_path, output_root
        data = json.loads(required_readonly_path("cases", "ieee39", "shortcircuit_data.json").read_text(encoding="utf-8"))
        options = {"method": "iec60909", "case": "maximum", "frequency_hz": 60.,
                   "profile": "iec_radial_v1", "fault_kind": "3ph", "backend": backend}
        return lambda: run_sweep(model, {}, options, data, root=output_root())
    return lambda: run_loadflow(model, options={"backend": backend, "q_limits": False})


def summarize(result):
    if "converged" in result:
        if not result["converged"]:
            raise RuntimeError("Load flow did not converge")
        return {"converged": True, "iterations": result["iterations"],
                "residual_mva": result["max_mismatch_mva"], "backend": result.get("backend")}
    if "payload" in result:
        count = result["payload"]["completed_count"]
        if count != 46:
            raise RuntimeError(f"Expected 46 contingencies, obtained {count}")
        return {"status": result["status"], "cases": count}
    if len(result["rows"]) != 39:
        raise RuntimeError("Expected 39 short-circuit rows")
    return {"status": result["status"], "buses": 39}


def environment(np, scipy_version):
    cpu = platform.processor() or platform.machine()
    cpuinfo = Path("/proc/cpuinfo")
    if sys.platform.startswith("linux") and cpuinfo.is_file():
        cpu = next((line.split(":", 1)[1].strip() for line in cpuinfo.read_text().splitlines()
                    if line.startswith("model name")), cpu)
    config = io.StringIO()
    with contextlib.redirect_stdout(config):
        np.show_config()
    return {"cpu": cpu, "logical_cpus": os.cpu_count(), "platform": platform.platform(),
            "python": platform.python_version(), "numpy": np.__version__, "scipy": scipy_version,
            "threads": {key: os.environ.get(key) for key in THREAD_KEYS}, "numpy_config": config.getvalue()}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--study", choices=("all", "lf", "n1", "sc", "synthetic"), default="all")
    parser.add_argument("--backend", choices=("dense", "sparse", "both"), default="both")
    parser.add_argument("--sizes", type=int, nargs="+", default=[200, 1000, 2000, 5000, 10000])
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--threads", type=int, default=1)
    parser.add_argument("--dense-max-buses", type=int, default=2000)
    parser.add_argument("--island-reference", choices=("none", "largest_generator"), default="none")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.repeats < 1 or args.warmup < 0 or args.threads < 1 or min(args.sizes) < 5:
        parser.error("Require positive repeats/threads, nonnegative warmup and sizes >= 5")
    if args.output.exists():
        parser.error("Output already exists, choose a new JSON path")
    for key in THREAD_KEYS:
        os.environ[key] = str(args.threads)
    import numpy as np
    scipy_version = None
    if importlib.util.find_spec("scipy") is not None:
        import scipy
        scipy_version = scipy.__version__
    studies = ["lf", "n1", "sc", "synthetic"] if args.study == "all" else [args.study]
    backends = ["dense", "sparse"] if args.backend == "both" else [args.backend]
    records = []
    for study in studies:
        for size in (args.sizes if study == "synthetic" else [39]):
            for backend in backends:
                row = {"study": study, "buses": size, "backend": backend, "samples": []}
                records.append(row)
                if backend == "sparse" and scipy_version is None:
                    row.update(status="unavailable", reason="scipy_not_installed")
                    continue
                if backend == "dense" and size > args.dense_max_buses:
                    row.update(status="not_measured", reason="dense_size_limit")
                    continue
                try:
                    start = time.perf_counter()
                    job = make_job(study, size, backend, args.island_reference)
                    row["preparation_s"] = time.perf_counter() - start
                    for _ in range(args.warmup):
                        summarize(job())
                    for _ in range(args.repeats):
                        start, cpu = time.perf_counter(), time.process_time()
                        result = job()
                        wall, used = time.perf_counter() - start, time.process_time() - cpu
                        row["samples"].append({"wall_s": wall, "cpu_s": used, **summarize(result)})
                    row.update(status="completed", median_wall_s=statistics.median(x["wall_s"] for x in row["samples"]),
                               median_cpu_s=statistics.median(x["cpu_s"] for x in row["samples"]))
                except Exception as exc:
                    row.update(status="failed", error=str(exc))
    result = {"schema": "power-machine-benchmarks-v1", "repeats": args.repeats, "warmup": args.warmup,
              "island_reference": args.island_reference, "environment": environment(np, scipy_version),
              "synthetic_definition": {"purpose": "timing only", "s_base_mva": 100, "voltage_kv": 100,
                 "frequency_hz": 50, "series_r_pu": .01, "series_x_pu": .1,
                 "branch_b_us": -100, "edges": "undirected ring plus jump=max(2,n//17) chords, unique sorted pairs",
                 "voltage": "(1+.01*cos(2*pi*i/n))*exp(1j*.03*sin(2*pi*i/n))",
                 "injection": "100*V*conj(YV), -.02j*V net shunt current term, PQ injections on all nonslack buses",
                 "initial_voltage": "solver defaults, known solution is used only to construct injections"},
              "source_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(), "records": records}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")
    return 1 if any(row["status"] == "failed" for row in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
