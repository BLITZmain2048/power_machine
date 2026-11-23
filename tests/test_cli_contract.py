"""CLI behavior owned by the public API facade."""
from __future__ import annotations

import json

import pytest

from power_machine.cli import EXIT_FAILED, EXIT_INVALID, EXIT_SUCCESS, _parser, main


def test_cli_version_and_documented_commands(capsys):
    with pytest.raises(SystemExit) as exited:
        main(["--version"])
    assert exited.value.code == EXIT_SUCCESS
    assert "0.1.0" in capsys.readouterr().out
    commands = _parser()._subparsers._group_actions[0].choices
    assert {"doctor", "batch", "gui", "mcp", "lf", "sc", "rms", "cct", "qds"} <= set(commands)


def test_doctor_runs_the_included_case_and_reports_runtime(capsys):
    assert main(["doctor", "--json"]) == EXIT_SUCCESS
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == "power-machine-cli-v1"
    result = payload["result"]
    assert result["status"] == "completed"
    assert result["resources"]["ieee39"]["loaded"] is True
    assert result["resources"]["ieee39"]["converged"] is True
    assert {"python", "numpy", "scipy", "tkinter", "matplotlib"} <= set(result["environment"])


def test_doctor_human_output_reports_the_health_check(capsys):
    assert main(["doctor"]) == EXIT_SUCCESS
    output = capsys.readouterr().out
    assert "Power Machine doctor" in output
    assert "IEEE 39: loaded" in output
    assert "load flow: completed" in output
    assert "evaluated cases" not in output


def test_doctor_fails_when_the_package_cannot_be_imported(monkeypatch, capsys):
    import power_machine.cli as cli
    original = cli.importlib.import_module

    def unavailable(name, *args, **kwargs):
        if name == "power_machine":
            raise ModuleNotFoundError("simulated incomplete installation")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(cli.importlib, "import_module", unavailable)
    assert main(["doctor", "--json"]) == EXIT_FAILED
    payload = json.loads(capsys.readouterr().out)
    assert payload["result"]["status"] == "failed"
    assert payload["result"]["diagnostics"][0]["code"] == "doctor_package_or_case_failed"


def test_cli_invalid_shortcircuit_file_returns_two(capsys):
    assert main(["sc", "ieee39", "--sc-data", "not-found.json"]) == EXIT_INVALID
    assert "invalid_input" in capsys.readouterr().out
