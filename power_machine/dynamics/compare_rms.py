from power_machine.dynamics.errors import coded_error as _coded_error
from pathlib import Path
'Boundary for a retired external-case comparator'

def compare_case(root: Path, case: str, *, result: Path | None=None, significance_thresholds: dict[str, float] | None=None, normalized_limit_percent: float=2.0):
    raise _coded_error(NotImplementedError, 'unsupported_compare_rms_data', 'The historical comparator was retired. Use refcompare.rms_replay with explicit user references')

def summary_table(comparisons: list[tuple[str, dict]]):
    raise _coded_error(NotImplementedError, 'unsupported_compare_rms_data', 'The historical comparator was retired. Use refcompare.rms_replay with explicit user references')

def render_report(report: dict):
    raise _coded_error(NotImplementedError, 'unsupported_compare_rms_data', 'The historical comparator was retired. Use refcompare.rms_replay with explicit user references')

