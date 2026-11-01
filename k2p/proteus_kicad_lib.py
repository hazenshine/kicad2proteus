"""KiCad symbols drawn from Proteus library devices (logic state sources, probes, 7-segment display).

The KiCad symbol is the Proteus device's own drawing (base items, no active states) with its pins at the Proteus pin
ends, so k2p can embed the Proteus device unchanged and the schematic still looks the same in both programs.
`graphics()` gives the drawing in a normalised form that k2p compares with the symbol found in a KiCad schematic.
The symbols are 'not in BOM' but 'on board': KiCad leaves symbols that are not on the board out of its netlist, and
k2p checks every connection against that netlist.
usage: python k2p/proteus_kicad_lib.py      -> kicad_library/Proteus_Sim.kicad_sym
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import struct
from decimal import Decimal
import proteus_lib
from sexp import first, find

HERE = _os.path.dirname(_os.path.abspath(__file__))
OUT = _os.path.join(HERE, '..', 'kicad_library', 'Proteus_Sim.kicad_sym')
LIB = 'Proteus_Sim'
# KiCad symbol name -> (Proteus device, KiCad reference prefix)
SYMBOLS = {'LOGICSTATE': ('LOGICSTATE', 'LS'), 'LOGICPROBE': ('LOGICPROBE', 'LP'),
           'LOGICPROBE_BIG': ('LOGICPROBE (BIG)', 'LP'), '7SEG-COM-CATHODE': ('7SEG-COM-CATHODE', 'DS')}
PIN_LEN = {'$PINDEFAULT': 508000, '$PINSHORT': 254000}      # $MKRNODE of Proteus's pin symbols (0.2 / 0.1 in)
KIND = {0: 'passive', 1: 'input', 2: 'output'}
NUL = b'\x00'


def mm(v):
    """Proteus units (10 nm) -> KiCad mm as a Decimal."""
    return (Decimal(v) / 100000).normalize() + 0


def fmt(d):
    s = format(Decimal(d).normalize(), 'f')
    return '0' if s in ('-0', '0') else s


def items(rec):
    n = rec[0]
    ts, L, symlen, npins, u1, u2, glen, nitems, one = struct.unpack_from('<IIIIHHIII', rec, 1 + n)
    q, out = 1 + n + 32, []
    for _ in range(nitems):
        ln = struct.unpack_from('<I', rec, q)[0]
        out.append(rec[q:q + ln])
        q += ln
    return out


def graphics(rec):
    """Normalised drawing of a Proteus device: ('polyline', fill, points) / ('rectangle', fill, p0, p1) /
    ('text', text, point), mm Decimals. Markers and hotspot items are not drawn."""
    out = []
    for it in items(rec):
        t = it[8]
        if t in (6, 8):
            continue
        bl = struct.unpack_from('<I', it, 10)[0]
        style = it[10 + 24:10 + bl].split(NUL)[0].decode('latin-1')
        if style != 'COMPONENT':
            raise ValueError(f'style {style} not handled')
        data = it[10 + bl:]
        if t == 1:                                  # box: x, y, 0, width, height
            x, y, _r, w, h = struct.unpack_from('<5i', data, 0)
            out.append(('rectangle', 'background', (mm(x), mm(y)), (mm(x + w), mm(y + h))))
        elif t == 0:
            x1, y1, x2, y2 = struct.unpack_from('<4i', data, 0)
            out.append(('polyline', 'none', ((mm(x1), mm(y1)), (mm(x2), mm(y2)))))
        elif t == 7:                                # path: lines only (a COMPONENT path is drawn filled)
            n = struct.unpack_from('<I', data, 0)[0]
            if struct.unpack_from('<H', data, 20)[0]:
                raise ValueError('curved path not handled')
            pts = tuple((mm(x), mm(y)) for x, y in (struct.unpack_from('<2i', data, 22 + 8 * i) for i in range(n)))
            out.append(('polyline', 'background', pts))
        elif t == 4:
            x, y = struct.unpack_from('<2i', data, 0)
            out.append(('text', data.rstrip(NUL).split(NUL)[-1].decode('latin-1'), (mm(x), mm(y))))
        else:
            raise ValueError(f'item type {t} not handled')
    return out


def text_size(rec):
    for it in items(rec):
        if it[8] == 4:
            bl = struct.unpack_from('<I', it, 10)[0]
            h, w = struct.unpack_from('<II', it, 10 + bl + 16)
            return mm(h), mm(w)
    return None


def pins(rec):
    """[(Proteus pin name, KiCad electrical type, end x, end y (10 nm), KiCad angle, length)] in record order. The end
    is where a wire attaches: the pin base moved by the pin symbol's length away from the body."""
    out = []
    for kind, flags, x, y, o, sym, name, number in proteus_lib.pins(rec):
        if sym not in PIN_LEN or kind not in KIND or (o & 0xffff) not in (0, 900, 1800, 2700):
            raise ValueError(f'pin {name}: {sym} kind {kind} orientation {o:#x} not handled')
        L = PIN_LEN[sym]
        dx, dy = (-1, 0)                            # orientation 0: the pin leaves the base to the left
        if o & 0x10000:
            dx = -dx
        for _ in range((o & 0xffff) // 900):
            dx, dy = -dy, dx
        angle = {(1, 0): 180, (-1, 0): 0, (0, 1): 270, (0, -1): 90}[(dx, dy)]     # KiCad: from the end to the body
        out.append((name, KIND[kind], x + dx * L, y + dy * L, angle, L))
    return out


def kicad_graphics(lib):
    """The same normalised form read from a KiCad lib symbol node (all units and body style 1)."""
    out = []
    for sub in find(lib, 'symbol'):
        for c in sub[2:]:
            if not isinstance(c, list) or c[0] == 'pin':
                continue
            fill = first(first(c, 'fill'), 'type')[1] if first(c, 'fill') else 'none'
            if c[0] == 'polyline':
                out.append(('polyline', fill, tuple((Decimal(p[1]).normalize() + 0, Decimal(p[2]).normalize() + 0)
                                                    for p in find(first(c, 'pts'), 'xy'))))
            elif c[0] == 'rectangle':
                s, e = first(c, 'start'), first(c, 'end')
                out.append(('rectangle', fill, (Decimal(s[1]).normalize() + 0, Decimal(s[2]).normalize() + 0),
                            (Decimal(e[1]).normalize() + 0, Decimal(e[2]).normalize() + 0)))
            elif c[0] == 'text':
                at = first(c, 'at')
                out.append(('text', c[1], (Decimal(at[1]).normalize() + 0, Decimal(at[2]).normalize() + 0)))
            else:
                out.append((c[0],))
    return out


def symbol_text(kname, pname, prefix):
    _, rec = proteus_lib.find_device(pname)
    desc = next((l[6:-1] for l in proteus_lib.props(rec).splitlines() if l.startswith('{DESC=')), pname)
    fld = lambda n, v: (f'\t\t(property "{n}" "{v}"\n\t\t\t(at 0 0 0)\n\t\t\t(show_name no)\n\t\t\t(do_not_autoplace no)\n'
                        f'\t\t\t(hide yes)\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t)\n'
                        f'\t\t\t)\n\t\t)\n')
    out = (f'\t(symbol "{kname}"\n\t\t(pin_numbers\n\t\t\t(hide yes)\n\t\t)\n\t\t(pin_names\n\t\t\t(offset 0)\n'
           f'\t\t\t(hide yes)\n\t\t)\n\t\t(exclude_from_sim yes)\n\t\t(in_bom no)\n\t\t(on_board yes)\n'
           f'\t\t(in_pos_files no)\n\t\t(duplicate_pin_numbers_are_jumpers no)\n'
           + fld('Reference', prefix) + fld('Value', kname) + fld('Footprint', '') + fld('Datasheet', '')
           + fld('Description', f'Proteus {pname}: {desc} (k2p bu parçayı Proteus aygıtı olarak aktarır)')
           + f'\t\t(symbol "{kname}_0_1"\n')
    size = text_size(rec)
    for g in graphics(rec):
        if g[0] == 'polyline':
            pts = ' '.join(f'(xy {fmt(x)} {fmt(y)})' for x, y in g[2])
            out += (f'\t\t\t(polyline\n\t\t\t\t(pts\n\t\t\t\t\t{pts}\n\t\t\t\t)\n\t\t\t\t(stroke\n\t\t\t\t\t(width 0)\n'
                    f'\t\t\t\t\t(type default)\n\t\t\t\t)\n\t\t\t\t(fill\n\t\t\t\t\t(type {g[1]})\n\t\t\t\t)\n\t\t\t)\n')
        elif g[0] == 'rectangle':
            out += (f'\t\t\t(rectangle\n\t\t\t\t(start {fmt(g[2][0])} {fmt(g[2][1])})\n\t\t\t\t(end {fmt(g[3][0])} '
                    f'{fmt(g[3][1])})\n\t\t\t\t(stroke\n\t\t\t\t\t(width 0)\n\t\t\t\t\t(type default)\n\t\t\t\t)\n'
                    f'\t\t\t\t(fill\n\t\t\t\t\t(type {g[1]})\n\t\t\t\t)\n\t\t\t)\n')
        else:
            out += (f'\t\t\t(text "{g[1]}"\n\t\t\t\t(at {fmt(g[2][0])} {fmt(g[2][1])} 0)\n\t\t\t\t(effects\n'
                    f'\t\t\t\t\t(font\n\t\t\t\t\t\t(size {fmt(size[0])} {fmt(size[1])})\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t)\n')
    out += f'\t\t)\n\t\t(symbol "{kname}_1_1"\n'
    for i, (name, kind, x, y, angle, L) in enumerate(pins(rec), 1):
        out += (f'\t\t\t(pin {kind} line\n\t\t\t\t(at {fmt(mm(x))} {fmt(mm(y))} {angle})\n\t\t\t\t(length {fmt(mm(L))})\n'
                f'\t\t\t\t(name "{name}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n\t\t\t\t\t\t\t(size 1.27 1.27)\n'
                f'\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t\t(number "{i}"\n\t\t\t\t\t(effects\n\t\t\t\t\t\t(font\n'
                f'\t\t\t\t\t\t\t(size 1.27 1.27)\n\t\t\t\t\t\t)\n\t\t\t\t\t)\n\t\t\t\t)\n\t\t\t)\n')
    return out + '\t\t)\n\t\t(embedded_fonts no)\n\t)\n'


def main():
    body = ''.join(symbol_text(k, p, pre) for k, (p, pre) in SYMBOLS.items())
    _os.makedirs(_os.path.dirname(OUT), exist_ok=True)
    open(OUT, 'w', encoding='utf-8', newline='\n').write(
        '(kicad_symbol_lib\n\t(version 20251024)\n\t(generator "kicad_symbol_editor")\n\t(generator_version "10.0")\n'
        + body + ')\n')
    for k, (p, _) in SYMBOLS.items():
        print(k, '<-', p, [(n, nm, fmt(mm(x)), fmt(mm(y)), a) for n, (nm, _k, x, y, a, _L) in
                            enumerate(pins(proteus_lib.find_device(p)[1]), 1)])
    print('yazıldı:', _os.path.abspath(OUT))


if __name__ == '__main__':
    main()
