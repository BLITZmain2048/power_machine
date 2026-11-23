"""Behavioral tests for independent parallel campaigns."""
from __future__ import annotations

import json
from pathlib import Path

from power_machine.batch import BatchOptions, Scenario, monte_carlo_load, run_batch


def _model() -> dict:
    return json.loads((Path(__file__).parents[1] / "power_machine" / "cases" / "ieee39" / "model.json").read_text(encoding="utf-8"))


def _scenarios(count: int = 200) -> list[Scenario]:
    return [Scenario(f"s-{number:03d}", load_multipliers={"LD03": 0.98 + number * 0.0001}) for number in range(count)]


def _comparison_rows(result):
    return [{key: value for key, value in row.items() if key not in {"duration_s", "detail_path"}}
            for row in result.to_records()]


def test_ieee39_results_are_invariant_to_worker_count():
    model, scenarios = _model(), _scenarios()
    serial = run_batch(model, "loadflow", scenarios, options=BatchOptions(workers=1, seed=101))
    dual = run_batch(model, "loadflow", scenarios, options=BatchOptions(workers=2, seed=101, chunk_size=16))
    quad = run_batch(model, "loadflow", scenarios, options=BatchOptions(workers=4, seed=101, chunk_size=16))
    assert _comparison_rows(serial) == _comparison_rows(dual) == _comparison_rows(quad)


def test_resume_reuses_atomic_checkpoints_after_cancellation(tmp_path):
    completed = 0
    def cancel():
        return completed >= 2
    def progress(event):
        nonlocal completed
        if event["code"] == "scenario_completed":
            completed += 1
    first = run_batch(_model(), "loadflow", _scenarios(5), output=tmp_path,
                      options=BatchOptions(workers=1), progress=progress, cancel=cancel)
    assert [row["status"] for row in first.records].count("completed") == 2
    resumed = run_batch(_model(), "loadflow", _scenarios(5), output=tmp_path,
                        options=BatchOptions(workers=1, resume=True))
    assert [row["status"] for row in resumed.records] == ["completed"] * 5
    assert len(list((tmp_path / "checkpoints").glob("*.json"))) == 5


def test_one_invalid_scenario_is_isolated():
    result = run_batch(_model(), "loadflow", [Scenario("good"), Scenario("bad", out_of_service=("missing",)), Scenario("later")],
                       options=BatchOptions(workers=1, on_failure="continue"))
    assert [row["id"] for row in result.records] == ["good", "bad", "later"]
    assert result.records[1]["status"] == "failed"
    assert result.records[2]["status"] == "completed"


def test_monte_carlo_seeds_are_stable():
    model = _model()
    first = list(monte_carlo_load(model, 12, {"kind": "normal", "mean": 1.0, "std": 0.02}, seed=7))
    second = list(monte_carlo_load(model, 12, {"kind": "normal", "mean": 1.0, "std": 0.02}, seed=7))
    assert [row.to_dict() for row in first] == [row.to_dict() for row in second]
    serial = run_batch(model, "loadflow", first, options=BatchOptions(workers=1, seed=44))
    parallel = run_batch(model, "loadflow", first, options=BatchOptions(workers=2, seed=44))
    assert _comparison_rows(serial) == _comparison_rows(parallel)
