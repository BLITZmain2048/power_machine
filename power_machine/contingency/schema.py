"Explicit data, numerical methods and traceable results for contingency studies."
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class Outage:
    object_id: str
    relative_path: str
    kind: str
    member_ids: tuple[str, ...] = ()

    @property
    def physical_ids(self) -> tuple[str, ...]:
        return self.member_ids or (self.object_id,)


@dataclass(frozen=True)
class ContingencyOptions:
    """Static screening limits and island policy.

    largest_generator selects a declared-capacity generator for each loaded
    unreferenced island. none preserves the historical reference and metric policy.
    Powers are MW/Mvar, voltage is pu and loading limits are percent.
    """
    orders: tuple[int, ...] = (1,)
    max_cases: int = 10000
    voltage_min_pu: float = 0.95
    voltage_max_pu: float = 1.05
    loading_limit_pct: float = 100.0
    q_limits: bool = True
    continue_on_failure: bool = True
    island_reference: str = "largest_generator"

    def __post_init__(self):
        if self.island_reference not in ("none", "largest_generator"):
            raise ValueError("[invalid_island_reference] island_reference must be none or largest_generator")
        if not self.orders or any(type(n) is not int or n not in (1, 2) for n in self.orders):
            raise ValueError('[invalid_input] ' + "orders supports N-1 and N-2 only")
        object.__setattr__(self, "orders", tuple(sorted(set(self.orders))))
        if type(self.max_cases) is not int or self.max_cases < 1:
            raise ValueError('[invalid_input] ' + "max_cases must be a positive integer")
        values = (self.voltage_min_pu, self.voltage_max_pu, self.loading_limit_pct)
        if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in values):
            raise ValueError('[invalid_input] ' + "Thresholds must be positive and finite")
        if self.voltage_min_pu >= self.voltage_max_pu:
            raise ValueError('[invalid_input] ' + "Minimum voltage must be below maximum voltage")
        if type(self.q_limits) is not bool or type(self.continue_on_failure) is not bool:
            raise ValueError('[invalid_input] ' + "Limit and continuation options must be boolean")


@dataclass(frozen=True)
class ContingencyCaseResult:
    case_id: str
    outages: tuple[Outage, ...]
    status: str
    metrics: dict
    violations: tuple[dict, ...]
    loadflow: dict | None


class BudgetExceeded(ValueError):
    def __init__(self, total_expected: int, max_cases: int):
        self.total_expected = total_expected
        super().__init__(f"budget_exceeded: {total_expected} cases exceed max_cases={max_cases}")
