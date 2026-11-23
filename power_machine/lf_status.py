"""Criterio común de convergencia del flujo de potencia por islas.

No altera el motor ``power_machine.loadflow``. El campo global ``converged`` del
resultado es falso cuando existe alguna isla sin referencia, aunque la isla
energizada haya convergido. Igual que PowerFactory, una isla sin fuente se
trata como desenergizada y se informa, sin invalidar el resultado de las islas
energizadas. Los cálculos que parten del flujo base deben usar
``energized_status`` en lugar de leer ``converged`` directamente.
"""

SOLVED = "resuelto"
DEENERGIZED = "sin_referencia"
INJECTION_GROUPS = ("loads", "gens", "shunts")


def energized_status(result):
    """Resume el flujo por islas.

    Devuelve un diccionario con ``ok`` (todas las islas con referencia
    convergieron y existe al menos una), ``n_solved``, ``n_failed``,
    ``n_deenergized``, ``deenergized_injections`` (identificadores de cargas,
    generadores y compensadores que quedan sin suministro, por grupo),
    ``max_mismatch_mva`` de las islas resueltas y ``message`` en español.
    """
    islands = result.get("islands") or []
    if not islands:
        ok = bool(result.get("converged"))
        return {"ok": ok, "n_solved": int(ok), "n_failed": int(not ok),
                "n_deenergized": 0, "deenergized_injections": {},
                "max_mismatch_mva": result.get("max_mismatch_mva"),
                "message": "Load flow converged" if ok else "Load flow did not converge"}
    solved = [i for i in islands if i.get("status") == SOLVED]
    dead = [i for i in islands if i.get("status") == DEENERGIZED]
    failed = [i for i in islands if i.get("status") not in (SOLVED, DEENERGIZED)]
    injections = {}
    for island in dead:
        elements = island.get("elements") or {}
        for group in INJECTION_GROUPS:
            ids = list(elements.get(group) or [])
            if ids:
                injections.setdefault(group, []).extend(ids)
    mismatches = [i.get("max_mismatch_mva") for i in solved
                  if i.get("max_mismatch_mva") is not None]
    ok = bool(solved) and not failed
    if ok:
        message = "Load flow converged in %d energized island(s)" % len(solved)
        if dead:
            message += ", %d de-energized island(s)" % len(dead)
        unserved = sum(len(v) for v in injections.values())
        if unserved:
            message += ", %d injection element(s) without supply" % unserved
    elif failed:
        message = "Load flow did not converge in %d energized island(s)" % len(failed)
    else:
        message = "No island has a reference"
    return {"ok": ok, "n_solved": len(solved), "n_failed": len(failed),
            "n_deenergized": len(dead), "deenergized_injections": injections,
            "max_mismatch_mva": max(mismatches) if mismatches else None,
            "message": message}
