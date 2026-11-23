"""Geometría compacta de mallas, sin ecuaciones eléctricas."""
from power_machine.gui.i18n import tr
import math
import numpy as np
from .layout import Scene, Symbol, Wire, terminals


def make_mesh(model, selected, index, result_boxes=False):
    ids = sorted(selected['bus_ids'])
    rank = {key: i for i, key in enumerate(ids)}
    n = len(ids)
    adjacency = np.zeros((n, n))
    branches = [b for b in model.get('branches', []) if b.get('bus_from') in rank and b.get('bus_to') in rank]
    for b in branches:
        i, j = rank[b['bus_from']], rank[b['bus_to']]
        adjacency[i, j] = adjacency[j, i] = 1
    from .ieee39_layout import BUS_POSITIONS, BRANCH_ROUTES
    curated = model.get('meta', {}).get('benchmark') == 'ieee39' and set(BUS_POSITIONS).issubset(ids)
    if curated:
        positions = dict(BUS_POSITIONS)
        extra=[key for key in ids if key not in positions]
        for i,key in enumerate(extra):positions[key]=(100+(i%6)*200,100+(i//6)*160)
    else:
        angle = np.linspace(0, 2 * np.pi, n, endpoint=False)
        points = np.column_stack((np.cos(angle), np.sin(angle))) * max(250, n * 25)
        rng = np.random.default_rng(739)
        points += rng.normal(0, 10, points.shape)
        edges = np.argwhere(np.triu(adjacency) > 0)
        for iteration in range(100 if n < 200 else 25):
            force = np.zeros_like(points)
            for start in range(0,n,128):
                delta=points[start:start+128,None,:]-points[None,:,:]
                dist2=np.sum(delta*delta,axis=2)+1
                force[start:start+128]=(delta*(220**2/dist2)[:,:,None]).sum(axis=1)
            for i,j in edges:
                delta=points[j]-points[i]
                distance=max(1,float(np.linalg.norm(delta)))
                spring=delta*(distance-230)/distance*.7
                force[i]+=spring
                force[j]-=spring
            length=np.maximum(1,np.linalg.norm(force,axis=1))
            points+=force/length[:,None]*min(35,250/(iteration+5))
        points-=points.min(axis=0)
        positions={key:tuple(map(float,points[i]+100)) for i,key in enumerate(ids)}
    scene = Scene(selected['id'], tr('integration.ieee_39_curated_layout') if curated else selected['name'])
    scene.curated = curated
    for key, (x, y) in positions.items():
        item = index[key]
        scene.symbols[key] = Symbol(key, 'bus', x, y, 85, 4,
            (key if curated and key in BUS_POSITIONS else item.get('name', key)) + ('' if curated else f"\n{item.get('un_kv', 0):g} kV"), item.get('un_kv'), dict(item, _curated=curated),
            (x - 42, y - 35, x + 90, y - 10),
            (x - 42, y + 10, x + 130, y + 65) if result_boxes else None)
    for k, b in enumerate(branches):
        a, z = b['bus_from'], b['bus_to']
        x, y = positions[a]
        tx, ty = positions[z]
        middle_y = (y + ty) / 2
        path = (BRANCH_ROUTES.get(b['id']) if curated else None) or ([(x,y),(tx,ty)] if x == tx or y == ty else [(x,y),(x,middle_y),(tx,middle_y),(tx,ty)])
        scene.wires.append(Wire(b['id'], a, z, path, index[a].get('un_kv')))
        mx, my = ((path[1][0]+path[2][0])/2, (path[1][1]+path[2][1])/2) if len(path)>2 else ((x+tx)/2,(y+ty)/2)
        kind = 'transformer' if b.get('class') == 'ElmTr2' else 'branch'
        scene.symbols[b['id']] = Symbol(b['id'], kind, mx, my, 16, 20,
            b.get('name', b['id']), index[a].get('un_kv'), dict(b, _local_bus=a),
            (mx + 12, my - 24, mx + 130, my - 4),
            (mx + 12, my + 4, mx + 145, my + 66) if result_boxes else None)
    kinds = {'gens': 'generator', 'loads': 'load', 'shunts': 'shunt', 'switches': 'switch'}
    counts = {}
    for category, kind in kinds.items():
        for item in model.get(category, []):
            bus = next((b for b in terminals(item) if b in positions), None)
            if bus is None:
                continue
            counts[bus] = counts.get(bus, 0) + 1
            x, y = positions[bus]
            ex, ey = (x, y + 75) if category == 'gens' else (x + 55 + counts[bus] * 22, y + 60)
            key = item['id']
            scene.symbols[key] = Symbol(key, kind, ex, ey, 16, 20, item.get('name', key),
                index[bus].get('un_kv'), item, (ex - 30, ey + 14, ex + 65, ey + 40))
            scene.wires.append(Wire(key, bus, key, [(x, y), (ex, y), (ex, ey)], index[bus].get('un_kv')))
    return scene
