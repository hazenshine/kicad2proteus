"""Walk and rewrite the circuit object lists in a Proteus ROOT.DSN.

Record tags seen so far: 00 element (component or wire), 08 component (seen on LOGICSTATE / 7SEG),
01 junction dot, 05 sub-sheet block, 10 terminal, FF end. walk + write_objs reproduce both circuits of the
user's submitted Odev.pdsprj and the reference byte for byte. An unknown tag raises ValueError.
usage: python dsnwalk.py FILE.pdsprj|ROOT.DSN [HEX_OFFSET_OF_FIRST_TAG]
  without an offset every circuit is listed (the __DEFAULT__ template is skipped)
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import struct, zipfile
from dsnfmt import Reader, read_label, w_label, WIRE_STYLE, INCH


def load(path):
    """ROOT.DSN bytes from a .pdsprj (zip) or from an unzipped ROOT.DSN."""
    if zipfile.is_zipfile(path):
        return zipfile.ZipFile(path).read('ROOT.DSN')
    return open(path, 'rb').read()


def circuits(d):
    """[(name, first_tag_offset)] for every circuit listed in the trailer, template excluded."""
    trailer = struct.unpack_from('<I', d, d.index(b'ISIS CIRCUIT FILE') - 4)[0]
    r = Reader(d, trailer)
    count = r.u16()
    r.u32()
    out = []
    for _ in range(count):
        name = r.pstr()
        r.u16()
        at = r.u32()
        r.u32()
        if name != '__DEFAULT__':
            out.append((name, d.index(b'OBJECT DATA\x00', at) + 12))
    return out


def walk(d, start):
    """Parse one object list. Returns (objects, offset after the 0xFF end byte)."""
    r = Reader(d, start)
    objs = []
    while True:
        at = r.p
        tag = r.u8()
        if tag == 0xFF:
            return objs, r.p
        if tag == 0x01:
            objs.append({'kind': 'dot', 'at': at, 'tag': tag, 'x': r.i32(), 'y': r.i32(), 'ptrs': [r.u32() for _ in range(4)]})
            continue
        if tag == 0x05:
            o = {'kind': 'sheet', 'at': at, 'tag': tag, 'box': [r.i32() for _ in range(4)], 'n': r.u16(), 'labels': []}
            while d[r.p] == 0xFF:
                r.p += 1
                o['labels'].append(read_label(r))
            o['tail'] = r.u32()
            objs.append(o)
            continue
        if tag == 0x10:
            o = {'kind': 'term', 'at': at, 'tag': tag, 'x': r.i32(), 'y': r.i32(), 'rot': r.u32(), 'sym': r.pstr(),
                 'k': r.u32(), 'b1': r.u16(), 'labels': []}
            # exactly one label (every terminal in the kit, 12 kinds); the wire pointer after it may start with 0xFF
            if r.u8() != 0xFF:
                raise ValueError(f'terminal without its label at {at:#x}')
            o['labels'].append(read_label(r))
            o['ptr'] = r.u32()
            objs.append(o)
            continue
        if tag == 0x20:
            # 2D graphics text: x y rot, u32 length (from itself to record end), body; the body starts with
            # u32 (length - 8), then just u8, kind u8, a length-prefixed graphics style, font block, and the
            # NUL-terminated text at the very end
            o = {'kind': 'text2d', 'at': at, 'tag': tag, 'x': r.i32(), 'y': r.i32(), 'rot': r.u32()}
            n = r.u32()
            o['body'] = r.raw(n - 4)
            o['text'] = o['body'][o['body'].rindex(b'\x00', 0, len(o['body']) - 1) + 1:-1].decode('latin-1')
            objs.append(o)
            continue
        if tag not in (0x00, 0x08):
            raise ValueError(f'unknown record tag {tag:#04x} at {at:#x}: {d[at:at + 48].hex()}')
        labels = []
        while d[r.p] == 0xFF:
            r.p += 1
            labels.append(read_label(r))
        if d[r.p:r.p + len(WIRE_STYLE)] == WIRE_STYLE:
            r.p += len(WIRE_STYLE)
            wlabels = []
            for _ in range(r.u16()):  # wire (net) labels sit after the style, each 0xFF + label
                if r.u8() != 0xFF:
                    raise ValueError(f'wire label without 0xFF at {r.p - 1:#x}')
                wlabels.append(read_label(r))
            n = r.u16()
            objs.append({'kind': 'wire', 'at': at, 'tag': tag, 'labels': labels, 'wlabels': wlabels,
                         'pts': [(r.i32(), r.i32()) for _ in range(n)]})
            continue
        c = {'kind': 'comp', 'at': at, 'tag': tag, 'labels': labels}
        c['proplen'] = r.u32()
        # the property text is the PROPERTIES label's text when it is at most 50 bytes, otherwise it is stored right
        # here and the label is empty (Proteus 8.17, any device: CELL with 50 bytes in the label, 51 here, F2_cell_c/d)
        c['props_inline'] = r.raw(c['proplen']) if c['proplen'] > 50 else None
        c['npins'] = r.u16()
        c['device'] = r.pstr()
        c['x'] = r.i32()
        c['y'] = r.i32()
        c['rot'] = r.u32()
        c['id'] = r.u8()
        c['res'] = r.raw(12)
        if c['npins'] > 64 or not c['device'].isprintable() or c['proplen'] > 4096:
            raise ValueError(f'unreadable component at {at:#x}: {d[at:at + 64].hex()}')
        c['pins'] = [r.u32() for _ in range(c['npins'])]
        objs.append(c)


def write_objs(objs):
    """Serialize parsed objects back to bytes, including the 0xFF end byte."""
    out = b''
    for o in objs:
        k = o['kind']
        if k == 'dot':
            out += b'\x01' + struct.pack('<ii4I', o['x'], o['y'], *o['ptrs'])
        elif k == 'sheet':
            out += (b'\x05' + struct.pack('<4iH', *o['box'], o['n']) + b''.join(w_label(l) for l in o['labels'])
                    + struct.pack('<I', o['tail']))
        elif k == 'term':
            s = o['sym'].encode('latin-1')
            out += (b'\x10' + struct.pack('<iiI', o['x'], o['y'], o['rot']) + bytes([len(s)]) + s
                    + struct.pack('<IH', o['k'], o['b1']) + b''.join(w_label(l) for l in o['labels'])
                    + struct.pack('<I', o['ptr']))
        elif k == 'text2d':
            out += b'\x20' + struct.pack('<iiII', o['x'], o['y'], o['rot'], len(o['body']) + 4) + o['body']
        elif k == 'wire':
            out += (bytes([o['tag']]) + b''.join(w_label(l) for l in o['labels']) + WIRE_STYLE
                    + struct.pack('<H', len(o['wlabels'])) + b''.join(w_label(l) for l in o['wlabels'])
                    + struct.pack('<H', len(o['pts'])) + b''.join(struct.pack('<ii', x, y) for x, y in o['pts']))
        else:
            dev = o['device'].encode('latin-1')
            out += bytes([o['tag']]) + b''.join(w_label(l) for l in o['labels'])
            out += struct.pack('<I', o['proplen']) + (o.get('props_inline') or b'')
            out += struct.pack('<H', o['npins']) + bytes([len(dev)]) + dev
            out += struct.pack('<iiIB', o['x'], o['y'], o['rot'], o['id']) + o['res']
            out += b''.join(struct.pack('<I', p) for p in o['pins'])
    return out + b'\xff'


def describe(o):
    inch = lambda v: round(v / INCH, 4)
    if o['kind'] == 'comp':
        return (f"COMP tag={o['tag']:#04x} {o['device']} ({inch(o['x'])}, {inch(o['y'])}) rot={o['rot']:#x} id={o['id']} "
                f"res={o['res'].hex()} pins={[hex(p) for p in o['pins']]} labels={[(l['text'], l['style']) for l in o['labels']]}")
    if o['kind'] == 'wire':
        wl = [(l['text'], inch(l['x']), inch(l['y']), l['u0'], l['just'], l['sflag'], l['mask'], l['height'], l['width'],
               l['u1'], l['attr'].hex(), l['font'], l['style']) for l in o['wlabels']]
        return f"WIRE {[(inch(x), inch(y)) for x, y in o['pts']]} labels={[l['text'] for l in o['labels']]} wlabels={wl}"
    if o['kind'] == 'dot':
        return f"DOT ({inch(o['x'])}, {inch(o['y'])}) ptrs={[hex(p) for p in o['ptrs']]}"
    if o['kind'] == 'term':
        return (f"TERM {o['sym']} ({inch(o['x'])}, {inch(o['y'])}) rot={o['rot']:#x} k={o['k']} "
                f"labels={[(l['text'], inch(l['x']), inch(l['y'])) for l in o['labels']]} ptr={o['ptr']:#x}")
    if o['kind'] == 'text2d':
        return f"TEXT2D ({inch(o['x'])}, {inch(o['y'])}) rot={o['rot']} text={o['text']!r} body={o['body'].hex()}"
    return f"SHEET box={[inch(v) for v in o['box']]} labels={[(l['text'], l['style']) for l in o['labels']]}"


if __name__ == '__main__':
    d = load(_sys.argv[1])
    todo = [('?', int(_sys.argv[2], 16))] if len(_sys.argv) > 2 else circuits(d)
    for name, start in todo:
        objs, end = walk(d, start)
        print(f'== circuit {name} at {start:#x}: {len(objs)} objects, '
              f'byte-exact round trip: {write_objs(objs) == d[start:end]}')
        for o in objs:
            print(f"{o['at']:#x}", describe(o))
