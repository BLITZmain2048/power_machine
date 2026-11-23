"""Disposición por niveles y bahías, sin alterar la topología eléctrica.

Los propietarios geométricos de nodos internos se buscan a través de
acopladores, incluso abiertos. Esta asociación solo sirve para dibujar.
Los estados eléctricos y las identidades originales permanecen intactos.
"""
from power_machine.gui.i18n import tr

from collections import defaultdict, deque
from itertools import combinations
import math
import re

from .layout import (Scene, Symbol, Wire, GROUPS, clean_label, diagram_groups,
                     element_index, terminals, apply_positions)


def usage(bus):
    return bus.get('params', {}).get('iUsage', bus.get('iUsage', 0))


def short_name(item, limit=24):
    name = clean_label(item.get('name', item['id']))
    name = name.replace('Breaker/Switch', 'Acopl.').replace('TRAFO', 'TR')
    match = re.search(r'(Z\d{3})', name)
    if match:
        return match.group(1)
    return name if len(name) <= limit else name[:limit - 1].rstrip() + '…'


def _order(ids, adjacent):
    """Recorrido determinista que deja juntas las secciones acopladas."""
    remaining = set(ids)
    ordered = []
    while remaining:
        start = min(remaining, key=lambda x: (len(adjacent[x] & remaining), x))
        while start in remaining:
            remaining.remove(start)
            ordered.append(start)
            choices = adjacent[start] & remaining
            if not choices:
                break
            start = min(choices, key=lambda x: (len(adjacent[x] & remaining), x))
    return ordered


def make_scene(model, group=None, *, result_boxes=False):
    groups = diagram_groups(model)
    by_bus = {b: g for g in groups for b in g['bus_ids']}
    index = element_index(model)
    if group is None:
        return overview(model, groups, by_bus, index)
    selected = next((g for g in groups if g['id'] == group), None)
    if selected is None:
        raise ValueError(tr('integration.unknown_diagram_group', value0=group))
    local = set(selected['bus_ids'])
    crossing = any(local.intersection(terminals(item)) and set(terminals(item)) - local
                   for category in GROUPS[1:] for item in model.get(category, []))
    if len(local) >= 20 and not crossing and all(usage(index[b]) == 0 for b in local):
        from .mesh import make_mesh
        return make_mesh(model, selected, index, result_boxes)
    bars = {b for b in local if usage(index[b]) == 0}
    nodes = local - bars
    graph = defaultdict(set)
    all_graph = defaultdict(set)
    equipment = []
    ports = defaultdict(list)
    for category in GROUPS[1:]:
        for item in sorted(model.get(category, []), key=lambda x: x['id']):
            if not local.intersection(terminals(item)):
                continue
            if item.get('class') == 'StaSwitch' and item.get('element_id') in index:
                ports[(item['element_id'], item.get('bus1'))].append(item)
                continue
            equipment.append(item)
            for a, b in combinations([b for b in terminals(item) if b in local], 2):
                all_graph[a].add(b)
                all_graph[b].add(a)
            if item.get('class') == 'ElmCoup':
                for a, b in combinations([b for b in terminals(item) if b in local], 2):
                    graph[a].add(b)
                    graph[b].add(a)
    owner = {b: b for b in bars}
    for node in sorted(nodes):
        queue = deque([(node, 0)])
        seen = {node}
        while queue:
            bid, depth = queue.popleft()
            if bid in bars:
                owner[node] = bid
                break
            for target in sorted(graph[bid] - seen):
                seen.add(target)
                queue.append((target, depth + 1))
        # Los nodos de impedancias se anclan por proximidad, sin fusionarlos.
        if node not in owner:
            queue = deque([node])
            seen = {node}
            while queue:
                bid = queue.popleft()
                if bid in bars:
                    owner[node] = bid
                    break
                for target in sorted(all_graph[bid] - seen):
                    seen.add(target)
                    queue.append(target)
            owner.setdefault(node, node)
    anchors = sorted(bars | {owner[n] for n in nodes})
    adjacent = defaultdict(set)
    for item in equipment:
        ends = list(dict.fromkeys(owner[b] for b in terminals(item) if b in local))
        for a, b in combinations(ends, 2):
            adjacent[a].add(b)
            adjacent[b].add(a)
    levels = defaultdict(list)
    for b in anchors:
        levels[round(float(index[b].get('un_kv') or 0), 3)].append(b)
    rows = [_order(levels[kv], adjacent) for kv in sorted(levels, reverse=True)]
    rank = {b: (r, i) for r, row in enumerate(rows) for i, b in enumerate(row)}
    feeders = defaultdict(list)
    ties = []
    for item in equipment:
        ends = list(dict.fromkeys(owner[b] for b in terminals(item) if b in local))
        if item.get('class') == 'ElmCoup' and len(ends) == 2 and rank[ends[0]][0] == rank[ends[1]][0]:
            ties.append((item, ends))
        else:
            own = min(ends, key=rank.get)
            feeders[own].append(item)
    scene = Scene(group, selected['name'])
    # Hasta seis equipos por columna. Cada columna reserva rótulo y resultados.
    max_rows = 3 if len(rows) > 2 else 6
    columns = {b: max(1, math.ceil(len(feeders[b]) / max_rows)) for b in anchors}
    max_columns = max(sum(columns[b] for b in row) for row in rows)
    pitch = 126
    total_width = max(1120, max_columns * pitch + 32 * max(len(r) for r in rows))
    y = 150.0
    row_info = []
    for row in rows:
        weights = sum(columns[b] for b in row)
        available = total_width - 32 * (len(row) - 1)
        row_height = max((math.ceil(len(feeders[b]) / columns[b]) for b in row), default=0)
        x = 0.0
        for b in row:
            width = available * columns[b] / weights
            item = index[b]
            label = clean_label(item.get('name', b)) + f"\n{item.get('un_kv', 0):g} kV"
            symbol = Symbol(b, 'bus' if b in bars else 'node', x + width / 2, y,
                            width - 12 if b in bars else 5, 4, label if b in bars else '',
                            item.get('un_kv'), item,
                            (x + 4, y - 115, x + width - 4, y - 47),
                            (x + 4, y - 43, x + width - 4, y - 7) if result_boxes and b in bars else None)
            scene.symbols[b] = symbol
            row_info.append((b, x, width, y))
            x += width + 32
        y += 180 + row_height * (91 if result_boxes else 66)
    node_slots = defaultdict(list)
    for node in sorted(nodes):
        if node not in scene.symbols:
            node_slots[owner[node]].append(node)
    for b, x, width, by in row_info:
        bar = scene.symbols[b]
        for n, item in enumerate(feeders[b]):
            col, row = n % columns[b], n // columns[b]
            cell_w = width / columns[b]
            fx = x + (col + .5) * cell_w
            fy = by + 42 + row * (91 if result_boxes else 66)
            ends = terminals(item)
            remote = [bid for bid in ends if bid in by_bus and bid not in local]
            cls = item.get('class')
            kind = ('outgoing' if remote else 'transformer3' if cls == 'ElmTr3' else
                    'transformer' if cls == 'ElmTr2' else 'switch' if cls in ('ElmCoup', 'StaSwitch') else
                    'generator' if cls in ('ElmSym', 'ElmGenstat', 'ElmPvsys', 'ElmXnet') else
                    'load' if cls == 'ElmLod' else 'shunt' if cls in ('ElmShnt', 'ElmNec') else 'branch')
            label = short_name(item, 18)
            remote_group = by_bus[remote[0]]['id'] if remote else None
            if remote:
                remote_name = by_bus[remote[0]]['name'].replace(tr('integration.internal_network_prefix'), '')
                label += '\n→ ' + clean_label(remote_name)
            label_width = min(140, cell_w - 12)
            label_box = (fx - label_width / 2, fy + 15, fx + label_width / 2, fy + 53)
            result_box = (label_box[0], fy + 53, label_box[2], fy + 83) if result_boxes else None
            local_ends = [bid for bid in ends if bid in local]
            meta = dict(item, _local_bus=local_ends[0])
            symbol = Symbol(item['id'], kind, fx, fy, 18, 24, label, bar.un_kv, meta,
                            label_box, result_box, remote_group)
            scene.symbols[item['id']] = symbol
            # Ramal común vertical. Cada equipo conserva sus puertos reales.
            for j, bid in enumerate(local_ends):
                target = scene.symbols.get(owner[bid])
                tx = max(target.x - target.width / 2 + 4, min(fx, target.x + target.width / 2 - 4))
                if j == 0:
                    start_id, end_id = bid, item['id']
                    trunk = x + col * cell_w + 4
                    points = [(trunk, target.y), (trunk, fy - 22), (fx, fy - 22), (fx, fy - 13)]
                else:
                    start_id, end_id = item['id'], bid
                    output_x = fx + (5 if j == 2 else -5) if kind == 'transformer3' else fx
                    points = [(output_x, fy + 13), (tx, fy + 13), (tx, target.y)]
                scene.wires.append(Wire(item['id'], start_id, end_id, points, target.un_kv))
    # Transformadores entre sus barras, con la bajada alineada al devanado bajo.
    for symbol in list(scene.symbols.values()):
        if symbol.kind not in ('transformer', 'transformer3'):
            continue
        ends = [scene.symbols[owner[b]] for b in terminals(symbol.item) if b in local]
        if len(ends) < 2 or ends[0].y == ends[1].y:
            continue
        high, low = min(ends, key=lambda s: s.y), max(ends, key=lambda s: s.y)
        old_x, old_y = symbol.x, symbol.y
        symbol.x = low.x
        symbol.y = (high.y + low.y) / 2
        dx, dy = symbol.x - old_x, symbol.y - old_y
        symbol.label_box = (symbol.x - 60, symbol.y + 15, symbol.x + 60, symbol.y + 53)
        if result_boxes:
            symbol.result_box = (symbol.x - 60, symbol.y + 53, symbol.x + 60, symbol.y + 83)
        for wire in scene.wires:
            if wire.id != symbol.id:
                continue
            if wire.end == symbol.id:
                tx = min(high.x + high.width / 2, max(high.x - high.width / 2, symbol.x))
                wire.points = [(tx, high.y), (symbol.x, high.y), (symbol.x, symbol.y - 13)]
            else:
                target = scene.symbols[owner[wire.end]]
                sx = symbol.x + (5 if wire.end == terminals(symbol.item)[-1] else -5) if symbol.kind == 'transformer3' else symbol.x
                wire.points = [(sx, symbol.y + 13), (sx, target.y), (target.x, target.y)]
    # Los nodos internos se sitúan sobre una conexión incidente, nunca como barras.
    for b, ids in node_slots.items():
        for n, node in enumerate(ids):
            incident = next((w for w in scene.wires if w.start == node or w.end == node), None)
            if incident:
                a, z = incident.points[:2] if incident.start == node else incident.points[-2:]
                if a == z:
                    a, z = incident.points[1:3]
                px, py = a[0] * .7 + z[0] * .3, a[1] * .7 + z[1] * .3
            else:
                bar = scene.symbols[b]
                px, py = bar.x + n * 8, bar.y + 18
            scene.symbols[node] = Symbol(node, 'node', px, py, 4, 4, '', index[node].get('un_kv'), index[node], (px, py, px, py))
    tie_lanes = defaultdict(int)
    for n, (item, ends) in enumerate(ties):
        a, b = sorted((scene.symbols[e] for e in ends), key=lambda s: s.x)
        sx, ex = a.x + a.width / 2, b.x - b.width / 2
        lane = tie_lanes[(a.id, b.id)]
        tie_lanes[(a.id, b.id)] += 1
        fy = a.y + lane * 20 if ex - sx <= 50 else a.y + 16 + n * 8
        fx = (sx + ex) / 2
        scene.symbols[item['id']] = Symbol(item['id'], 'switch', fx, fy, 10, 10,
            short_name(item, 14), a.un_kv, item, (fx - 42, fy + 8, fx + 42, fy + 24))
        original_a = next(bid for bid in terminals(item) if owner.get(bid) == a.id)
        original_b = next(bid for bid in terminals(item) if owner.get(bid) == b.id)
        scene.wires.extend([Wire(item['id'], original_a, item['id'], [(sx, a.y), (sx, fy), (fx - 6, fy)], a.un_kv),
                            Wire(item['id'], item['id'], original_b, [(fx + 6, fy), (ex, fy), (ex, b.y)], b.un_kv)])
    # Las conexiones remotas terminan en el borde derecho del esquema.
    last_y = -100.0
    outgoing = sorted((s for s in scene.symbols.values() if s.remote_group),
                      key=lambda s: (scene.symbols[owner[s.item['_local_bus']]].y, s.id))
    for symbol in outgoing:
        local_bar = scene.symbols[owner[symbol.item['_local_bus']]]
        symbol.x = total_width + 56
        symbol.y = max(local_bar.y + 36, last_y + (110 if result_boxes else 85))
        last_y = symbol.y
        symbol.label_box = (symbol.x - 40, symbol.y + 16, symbol.x + 140, symbol.y + 64)
        symbol.result_box = ((symbol.x - 40, symbol.y + 65, symbol.x + 140, symbol.y + 97)
                             if result_boxes else None)
        for wire in scene.wires:
            if wire.id == symbol.id and wire.end == symbol.id:
                start = wire.points[0]
                wire.points = [start, (symbol.x - 24, start[1]),
                               (symbol.x - 24, symbol.y), (symbol.x - 12, symbol.y)]
    # Resolver choques de símbolos antes de reservar los textos en pantalla.
    placed = []
    def collides(box):
        return any(min(box[2], b[2]) + 4 > max(box[0], b[0]) and
                   min(box[3], b[3]) + 4 > max(box[1], b[1]) for b in placed)
    for symbol in scene.symbols.values():
        if symbol.kind == 'node':
            continue
        original = symbol.bbox
        if symbol.kind != 'bus' and collides(original):
            for distance in range(24, 240, 24):
                found = False
                for dx, dy in ((distance, 0), (-distance, 0), (0, distance), (0, -distance)):
                    box = (original[0] + dx, original[1] + dy, original[2] + dx, original[3] + dy)
                    if not collides(box):
                        apply_positions(scene, {symbol.id: (symbol.x + dx, symbol.y + dy)})
                        found = True
                        break
                if found:
                    break
        placed.append(symbol.bbox)
    # Cada nodo es un punto real común a todos sus conductores incidentes.
    for node in sorted(nodes):
        symbol = scene.symbols[node]
        for wire in scene.wires:
            if wire.start == node:
                old = wire.points[0]
                wire.points = [(symbol.x, symbol.y), (symbol.x, old[1])] + wire.points
            if wire.end == node:
                old = wire.points[-1]
                wire.points = wire.points + [(symbol.x, old[1]), (symbol.x, symbol.y)]
    # Interruptores de cubículo insertados en su propio conductor.
    for (eid, bid), switches in sorted(ports.items()):
        wire = next((w for w in scene.wires if w.id == eid and bid in (w.start, w.end)), None)
        if wire is None:
            continue
        for n, item in enumerate(switches):
            candidates = []
            for j, (a, z) in enumerate(zip(wire.points, wire.points[1:])):
                length = abs(z[0] - a[0]) + abs(z[1] - a[1])
                if length < 12:
                    continue
                for fraction in (.35, .65, .2, .8, .5):
                    px, py = a[0] + (z[0] - a[0]) * fraction, a[1] + (z[1] - a[1]) * fraction
                    box = (px - 4, py - 4, px + 4, py + 4)
                    candidates.append((collides(box), -length, j, px, py))
            if not candidates:
                a, z = wire.points[0], wire.points[-1]
                candidates.append((True, 0, 0, (a[0] + z[0]) / 2, (a[1] + z[1]) / 2))
            _, _, j, px, py = min(candidates)
            scene.symbols[item['id']] = Symbol(item['id'], 'port_switch', px, py, 7, 7, '',
                index[bid].get('un_kv'), item, (px, py, px, py))
            placed.append(scene.symbols[item['id']].bbox)
            a, z = wire.points[j:j + 2]
            length = max(1, abs(z[0] - a[0]) + abs(z[1] - a[1]))
            vx, vy = (z[0] - a[0]) / length, (z[1] - a[1]) / length
            before, after = (px - 4 * vx, py - 4 * vy), (px + 4 * vx, py + 4 * vy)
            first = Wire(wire.id, wire.start, item['id'], wire.points[:j + 1] + [before], wire.un_kv)
            second = Wire(wire.id, item['id'], wire.end, [after] + wire.points[j + 1:], wire.un_kv)
            scene.wires.remove(wire)
            scene.wires.extend((first, second))
            wire = first if bid == first.start else second
    return scene


def overview(model, groups, by_bus, index):
    scene = Scene(None, tr('integration.network_overview_diagram'))
    bundles = defaultdict(list)
    adjacent = defaultdict(set)
    for category in ('branches', 'branches3', 'switches'):
        for item in sorted(model.get(category, []), key=lambda i: i['id']):
            ends = sorted({by_bus[b]['id'] for b in terminals(item) if b in by_bus})
            for a, b in combinations(ends, 2):
                bundles[a, b].append(item)
                adjacent[a].add(b)
                adjacent[b].add(a)
    layers = defaultdict(list)
    lookup = {g['id']: g for g in groups}
    for g in groups:
        kv = max(g['un_kv_levels'], default=0)
        layer = 0 if 'Equivalente externo' in g['name'] or 'interface' in g['name'] else 1 if kv >= 200 else 2 if kv >= 66 else 3
        layers[layer].append(g['id'])
    orders = {r: _order(ids, adjacent) for r, ids in layers.items()}
    # Barridos de baricentro para reducir cruces entre capas.
    for _ in range(5):
        rank = {key: i / max(1, len(row) - 1) for row in orders.values() for i, key in enumerate(row)}
        for layer in sorted(orders):
            orders[layer].sort(key=lambda k: (sum(rank[n] for n in adjacent[k]) / max(1, len(adjacent[k])), k))
    width = 1540
    for layer, ids in sorted(orders.items()):
        cell = width / len(ids)
        for i, key in enumerate(ids):
            g = lookup[key]
            label = clean_label(g['name']) + '\n' + ' / '.join(f'{v:g}' for v in g['un_kv_levels']) + ' kV'
            count = sum(usage(index[b]) == 0 for b in g['bus_ids'])
            label += tr('integration.busbars', value0=count)
            x, y = cell * (i + .5), 80 + layer * 270
            box_w = min(cell - 24, max(220, min(360, len(g['name']) * 7 + 20)))
            scene.symbols[key] = Symbol(key, 'group', x, y, box_w, 105, label,
                max(g['un_kv_levels'], default=None), g, (x - box_w / 2 + 9, y - 44, x + box_w / 2 - 9, y + 44))
    for n, ((a, b), items) in enumerate(sorted(bundles.items())):
        sa, sb = scene.symbols[a], scene.symbols[b]
        needs_bend = False
        kvs = sorted({round(float(index[bid].get('un_kv') or 0), 3) for item in items for bid in terminals(item) if bid in index}, reverse=True)
        kv = kvs[0] if kvs else 0
        if sa.y == sb.y:
            left, right = sorted((sa, sb), key=lambda s: s.x)
            points = [(left.x + left.width / 2, left.y), (right.x - right.width / 2, right.y)]
            if any(s.y == sa.y and left.x < s.x < right.x for s in scene.symbols.values()):
                points = [(left.x, left.y + left.height / 2), (right.x, right.y + right.height / 2)]
                needs_bend = True
        else:
            top, bottom = sorted((sa, sb), key=lambda s: s.y)
            points = [(top.x, top.y + top.height / 2), (bottom.x, bottom.y - bottom.height / 2)]
        obstacles = [s.bbox for key, s in scene.symbols.items() if key not in (a, b)]
        obstacles += [(s.bbox[0] + 6, s.bbox[1] + 6, s.bbox[2] - 6, s.bbox[3] - 6)
                      for s in (sa, sb)]
        def crosses_box(p, q, box):
            # Intersección paramétrica segmento contra interior de rectángulo.
            lo, hi = 0.0, 1.0
            for axis in (0, 1):
                delta = q[axis] - p[axis]
                lower, upper = box[axis] - 5, box[axis + 2] + 5
                if abs(delta) < 1e-12:
                    if not lower < p[axis] < upper:
                        return False
                    continue
                ta, tb = sorted(((lower - p[axis]) / delta, (upper - p[axis]) / delta))
                lo, hi = max(lo, ta), min(hi, tb)
                if lo >= hi:
                    return False
            return lo < hi
        if needs_bend or any(crosses_box(*points, box) for box in obstacles):
            xs = [s.x - s.width / 2 - 18 for s in scene.symbols.values()]
            xs += [s.x + s.width / 2 + 18 for s in scene.symbols.values()]
            ys = [s.y - s.height / 2 - 45 for s in scene.symbols.values()]
            ys += [s.y + s.height / 2 + 45 for s in scene.symbols.values()]
            candidates = []
            for x in sorted(set(xs)):
                for y in sorted(set(ys)):
                    bend = (x, y)
                    if not any(crosses_box(points[0], bend, box) or crosses_box(bend, points[1], box)
                               for box in obstacles):
                        length = math.dist(points[0], bend) + math.dist(bend, points[1])
                        candidates.append((length, bend))
            if candidates:
                points.insert(1, min(candidates)[1])
        label = f"{len(items)} {tr('integration.line') if len(items) == 1 else tr('integration.lines')} {kv:g} kV"
        if len(points) == 3:
            mx, my = points[1]
        else:
            mx, my = sum(p[0] for p in points) / 2, sum(p[1] for p in points) / 2
        label_y = my - 10
        if sa.y == sb.y and len(points) == 2 and abs(points[1][0] - points[0][0]) < 126:
            # Enlaces cortos: texto sobre la fila, con guía a la traza en render.
            label_y = sa.y - sa.height / 2 - 35
        scene.wires.append(Wire(f'pair:{n}', a, b, points, kv, label,
                                 tuple(i['id'] for i in items), (mx - 63, label_y, mx + 63, label_y + 20)))
    return scene
