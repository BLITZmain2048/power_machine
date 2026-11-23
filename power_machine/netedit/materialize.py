"""Materialización compatible con power_machine-model-v1 y topología existente."""

from .commands import replay_commands
from .schema import VariantError
from .validate import check_variant
from ..topology import build_topology


def materialize_variant(base_model, variant):
    checks = check_variant(base_model, variant)
    errors = tuple(c for c in checks if c.severity == "error")
    if errors:
        raise VariantError(errors)
    model, _ = replay_commands(base_model, variant)
    # Regenerar topología comprueba que se conserva el contrato del motor.
    build_topology(model)
    return model


def materialized_layout(base_model, variant):
    materialize_variant(base_model, variant)
    return replay_commands(base_model, variant)[1]
