"""Build synthetic test files: take a Proteus-saved .pdsprj, edit its root object list, write a new .pdsprj.

Pointers are handled symbolically: after `load`, every pointer field holds the referenced object (a dict) or
None; `save` lays the objects out again, recomputes every offset, patches the end pointer and the trailer's
template pointer, and writes all zip entries of the base file in their original order.
Used for experiments the Proteus GUI cannot draw (e.g. two wires joined end to end without a dot).
usage (as a library): f = load(base); objs = f.objs; ...edit...; save(f, out_path)
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import struct, time, zipfile, zlib, copy
from dsnwalk import circuits, walk, write_objs
from dsnfmt import WIRE_STYLE


class File:
    pass


def load(path):
    z = zipfile.ZipFile(path)
    f = File()
    f.entries = [(i.filename, z.read(i.filename)) for i in z.infolist()]
    d = dict(f.entries)['ROOT.DSN']
    f.dsn = d
    (name, start), = [c for c in circuits(d)]
    objs, end = walk(d, start)
    by_at = {o['at']: o for o in objs}
    ref = lambda p: by_at[p] if p else None
    for o in objs:
        if o['kind'] == 'comp':
            o['pins'] = [ref(p) for p in o['pins']]
        elif o['kind'] == 'dot':
            o['ptrs'] = [ref(p) for p in o['ptrs']]
        elif o['kind'] == 'term':
            o['ptr'] = ref(o['ptr'])
    f.start, f.end, f.objs = start, end, objs
    f.end_ptr_at = d.index(b'ISIS CIRCUIT FILE') - 4
    f.trailer_at = struct.unpack_from('<I', d, f.end_ptr_at)[0]
    t = d[f.trailer_at:]
    k = t.index(b'\x0b__DEFAULT__') + 12 + 2
    assert struct.unpack_from('<I', t, k)[0] == end, 'template must follow the root list'
    f.tpl_ptr_in_trailer = k
    return f


def insert_prefix(f, at, data):
    """Insert bytes into the part before the root object list (e.g. a device record into the embedded library) and
    shift every stored offset behind it: the root list position and the trailer's circuit offsets."""
    d = f.dsn
    assert at < f.start
    n = len(data)
    t = bytearray(d[f.trailer_at:])
    r = 2 + 4
    for _ in range(struct.unpack_from('<H', t, 0)[0]):
        ln = t[r]
        r += 1 + ln + 2
        v = struct.unpack_from('<I', t, r)[0]
        if v > at:
            struct.pack_into('<I', t, r, v + n)
        r += 8
    f.dsn = d[:at] + data + d[at:f.trailer_at] + bytes(t)
    f.start += n
    f.end += n
    f.end_ptr_at += n
    f.trailer_at += n
    f.tpl_ptr_in_trailer = f.tpl_ptr_in_trailer  # relative to the trailer, unchanged


def wire(pts, wlabels=()):
    return {'kind': 'wire', 'tag': 0, 'labels': [], 'wlabels': list(wlabels), 'pts': list(pts)}


def dot(x, y, wires):
    return {'kind': 'dot', 'tag': 1, 'x': x, 'y': y, 'ptrs': list(wires) + [None] * (4 - len(wires))}


def serialize(objs, base):
    """Bytes of the object list (with 0xFF) starting at file offset base; pointers resolved."""
    def concrete(off):
        out = []
        for o in objs:
            c = dict(o)
            c['at'] = off.get(id(o), 0)
            p = lambda r: off.get(id(r), 0) if r is not None else 0
            if o['kind'] == 'comp':
                c['pins'] = [p(r) for r in o['pins']]
            elif o['kind'] == 'dot':
                c['ptrs'] = [p(r) for r in o['ptrs']]
            elif o['kind'] == 'term':
                c['ptr'] = p(o['ptr'])
            out.append(c)
        return out
    sizes = [len(write_objs([c])) - 1 for c in concrete({})]
    off, pos = {}, base
    for o, n in zip(objs, sizes):
        off[id(o)] = pos
        pos += n
    data = write_objs(concrete(off))
    assert len(data) == pos - base + 1
    return data


def save(f, path, objs=None):
    objs = f.objs if objs is None else objs
    d = f.dsn
    data = serialize(objs, f.start)
    root_end = f.start + len(data)
    template = d[f.end:f.trailer_at]
    trailer = bytearray(d[f.trailer_at:])
    struct.pack_into('<I', trailer, f.tpl_ptr_in_trailer, root_end)
    prefix = bytearray(d[:f.start])
    struct.pack_into('<I', prefix, f.end_ptr_at, root_end + len(template))
    dsn = bytes(prefix) + data + template + bytes(trailer)
    write_zip(path, [(n, dsn if n == 'ROOT.DSN' else b) for n, b in f.entries])
    return dsn


def write_zip(path, entries, now=None):
    """Zip like Proteus's container (made-by 0.0, flags 0x0002, deflate, no attributes)."""
    now = now or int(time.time())
    t = time.localtime(now)
    dtime = (t.tm_hour << 11) | (t.tm_min << 5) | (t.tm_sec // 2)
    ddate = ((t.tm_year - 1980) << 9) | (t.tm_mon << 5) | t.tm_mday
    local, central = b'', b''
    for name, data in entries:
        co = zlib.compressobj(9, zlib.DEFLATED, -15)
        comp = co.compress(data) + co.flush()
        crc = zlib.crc32(data) & 0xffffffff
        nm = name.encode('ascii')
        off = len(local)
        local += struct.pack('<IHHHHHIIIHH', 0x04034b50, 20, 2, 8, dtime, ddate, crc, len(comp), len(data), len(nm), 0)
        local += nm + comp
        central += struct.pack('<IHHHHHHIIIHHHHHII', 0x02014b50, 0, 20, 2, 8, dtime, ddate, crc, len(comp),
                               len(data), len(nm), 0, 0, 0, 1, 0, off) + nm
    end = struct.pack('<IHHHHIIH', 0x06054b50, 0, 0, len(entries), len(entries), len(central), len(local), 0)
    open(path, 'wb').write(local + central + end)


if __name__ == '__main__':
    # self-test: re-saving an unmodified file must reproduce ROOT.DSN byte for byte
    f = load(_sys.argv[1])
    dsn = save(f, _sys.argv[2])
    print('ROOT.DSN identical:', dsn == f.dsn)
