"""Compare a netlist written by Proteus (Tool > Netlist Compiler, SDF, physical, flatten) with the KiCad netlist.

Proteus pin names are mapped to KiCad pin numbers with PMAP (device from the SDF part list, value from KiCad).
Prints unmatched nets on either side and exits 1 on any difference.
usage: python compare_sdf.py PROTEUS.SDF KICAD.net
"""
import csv, os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from sexp import parse, find, first, val

PMAP = {'AND': {'D0': '1', 'D1': '2', 'Q': '7'}, 'OR': {'D0': '1', 'D1': '2', 'Q': '7'},
        'NOR': {'D0': '1', 'D1': '2', 'Q': '7'}, 'NOT': {'D': '1', 'Q': '6'}}
DEVMAP = {'74AUC2G08': 'AND', '74AUC2G32': 'OR', '74AUC2G02': 'NOR', '74AUC2G04': 'NOT'}


def read_sdf(path):
    """(parts {ref: device}, nets {name: frozenset((ref, pin_name))}) from an ISIS SDF netlist; the part values
    are kept in read_sdf.values."""
    lines = [l.rstrip() for l in open(path, encoding='latin-1')]
    parts, nets, section, net = {}, {}, None, None
    for l in lines:
        if l.startswith('*'):
            section = l[1:].split(',')[0]
            continue
        if not l:
            net = None
            continue
        if section == 'PARTLIST':
            f = next(csv.reader([l]))
            parts[f[0]] = f[1]
            read_sdf.values[f[0]] = f[2]
        elif section == 'NETLIST':
            if net is None:
                net = l.split(',')[0]
                nets[net] = set()
            elif l.split(',')[1] not in ('LBL', 'PR', 'GT', 'IT', 'OT', 'BT'):   # labels, power rails, terminals
                ref, _kind, pin = l.split(',')[:3]
                nets[net].add((ref, pin))
    return parts, {k: frozenset(v) for k, v in nets.items()}


read_sdf.values = {}


def read_kicad(path):
    t = parse(open(path, encoding='utf-8').read())
    parts = {val(c, 'ref'): val(c, 'value') for c in find(first(t, 'components'), 'comp')}
    nets = {val(n, 'name'): frozenset((val(x, 'ref'), val(x, 'pin')) for x in find(n, 'node'))
            for n in find(first(t, 'nets'), 'net')}
    return parts, nets


class _Same(dict):
    def __getitem__(self, k):
        return k


def from_k2p(pparts, pnets):
    """Output of k2p.py: device '74AUC2G08_A' is KiCad 74AUC2G08 unit A, the part 'U40A' is KiCad's U40; pin names
    go back to KiCad pin numbers through k2p.PARTS."""
    import re
    from k2p import PARTS
    by_name = {lib.split(':')[1]: units for lib, (_, units) in PARTS.items()}
    parts, pins = {}, {}
    for ref, dev in pparts.items():
        m = re.fullmatch(r'(.+)_([A-H])(_R\d+)?', dev)
        unit = ord(m.group(2)) - 64
        kref = ref[:-1] if ref.endswith(m.group(2)) and m.group(1) in by_name else ref
        parts[kref] = read_sdf.values[ref]
        # parts outside the mapping table keep KiCad's pin numbers as Proteus pin names
        table = by_name.get(m.group(1), {}).get(unit)
        pins[ref] = (kref, {name: num for num, name in table.items()} if table else _Same())
    nets = {n: frozenset((pins[r][0], pins[r][1][p]) for r, p in g) for n, g in pnets.items()}
    return parts, nets


def main(sdf, net):
    pparts, pnets = read_sdf(sdf)
    kparts, knets = read_kicad(net)
    ok = True
    if all('_' in d for d in pparts.values()):
        parts, mapped = from_k2p(pparts, pnets)
        # a value label Proteus cannot read as a number ("3.7V LiPo") is kept and k2p writes the number into the
        # property text; Proteus's netlist then shows that number as the part's value
        sim = {r for r, v in parts.items() if r in kparts and v != kparts[r] and kparts[r].startswith(v + ' ')}
        if parts.keys() != kparts.keys() or any(parts[r] != kparts[r] for r in parts if r not in sim):
            ok = False
            print('parts differ:', sorted(set(parts.items()) ^ set(kparts.items()))[:20])
        else:
            print(f'parts: {len(parts)} identical (ref and value)' +
                  ''.join(f'; {r} simulation value {parts[r]} (KiCad "{kparts[r]}")' for r in sorted(sim)))
        kset, pset = set(knets.values()), set(mapped.values())
        print(f'nets: Proteus {len(pnets)}, KiCad {len(knets)}, matching {len(kset & pset)}')
        for n, g in knets.items():
            if g not in pset:
                ok = False
                print('  only in KiCad :', n, sorted(g))
        for n, g in mapped.items():
            if g not in kset:
                ok = False
                print('  only in Proteus:', n, sorted(g))
        print('IDENTICAL' if ok else 'DIFFERENT')
        return ok
    want = {r: DEVMAP.get(v, '?' + v) for r, v in kparts.items()}
    if pparts != want:
        ok = False
        print('parts differ:', sorted(set(pparts.items()) ^ set(want.items()))[:20])
    else:
        print(f'parts: {len(pparts)} identical (ref and device)')
    mapped = {name: frozenset((r, PMAP[pparts[r]][p]) for r, p in g) for name, g in pnets.items()}
    kset, pset = set(knets.values()), set(mapped.values())
    only_k = [n for n, g in knets.items() if g not in pset]
    only_p = [n for n, g in mapped.items() if g not in kset]
    print(f'nets: Proteus {len(pnets)}, KiCad {len(knets)}, matching {len(kset & pset)}')
    for n in only_k:
        ok = False
        print('  only in KiCad :', n, sorted(knets[n]))
    for n in only_p:
        ok = False
        print('  only in Proteus:', n, sorted(mapped[n]))
    print('IDENTICAL' if ok else 'DIFFERENT')
    return ok


if __name__ == '__main__':
    _sys.exit(0 if main(_sys.argv[1], _sys.argv[2]) else 1)
