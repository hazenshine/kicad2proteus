"""Compare the circuit object lists of two Proteus files, ignoring absolute file offsets.

Wire pointers are replaced by the index of the wire they point to, so a file shifted by a different header
size compares equal when its objects, order and references are the same. Prints the first differences.
usage: python compare_objs.py A.pdsprj B.pdsprj
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from dsnwalk import load, circuits, walk, describe


def norm(objs):
    """Objects as comparable tuples; pointers become object indices (None for 0)."""
    idx = {o['at']: i for i, o in enumerate(objs)}
    ref = lambda p: None if p == 0 else idx.get(p, f'?{p:#x}')
    out = []
    for o in objs:
        k = o['kind']
        labels = tuple(tuple(sorted((key, v if not isinstance(v, (bytes, bytearray)) else v.hex())
                                    for key, v in l.items())) for l in o.get('labels', []))
        if k == 'dot':
            out.append((k, o['x'], o['y'], tuple(ref(p) for p in o['ptrs'])))
        elif k == 'wire':
            wl = tuple(tuple(sorted((key, v if not isinstance(v, (bytes, bytearray)) else v.hex())
                                    for key, v in l.items())) for l in o['wlabels'])
            out.append((k, o['tag'], wl, tuple(o['pts']), labels))
        elif k == 'term':
            out.append((k, o['x'], o['y'], o['rot'], o['sym'], o['k'], o['b1'], labels, ref(o['ptr'])))
        elif k == 'text2d':
            out.append((k, o['x'], o['y'], o['rot'], o['body'].hex()))
        elif k == 'sheet':
            out.append((k, tuple(o['box']), o['n'], labels, o['tail']))
        else:
            out.append((k, o['tag'], o['device'], o['x'], o['y'], o['rot'], o['id'], o['res'].hex(), o['proplen'],
                        o['npins'], tuple(ref(p) for p in o['pins']), labels))
    return out


def lists(path):
    d = load(path)
    return {name: walk(d, start)[0] for name, start in circuits(d)}


def main(a, b, show=12):
    la, lb = lists(a), lists(b)
    same_all = True
    for name in sorted(set(la) | set(lb)):
        if name not in la or name not in lb:
            print(f'{name}: only in {"A" if name in la else "B"}')
            same_all = False
            continue
        oa, ob = la[name], lb[name]
        na, nb = norm(oa), norm(ob)
        diffs = [i for i in range(max(len(na), len(nb))) if i >= len(na) or i >= len(nb) or na[i] != nb[i]]
        print(f'{name}: A {len(na)} objects, B {len(nb)} objects, differing positions: {len(diffs)}')
        for i in diffs[:show]:
            print(f'  [{i}] A: {describe(oa[i]) if i < len(oa) else "-"}')
            print(f'  [{i}] B: {describe(ob[i]) if i < len(ob) else "-"}')
            if i < len(na) and i < len(nb) and na[i][0] == nb[i][0]:
                fields = [j for j in range(len(na[i])) if na[i][j] != nb[i][j]]
                print(f'       differing tuple fields: {fields}')
        same_all &= not diffs
    print('IDENTICAL' if same_all else 'DIFFERENT')
    return same_all


if __name__ == '__main__':
    main(_sys.argv[1], _sys.argv[2])
