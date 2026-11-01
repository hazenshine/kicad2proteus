"""Reader/writer for the root-sheet object list of a Proteus 8 ISIS ROOT.DSN.

Layout learned from the reference design (BDT_ODEV.pdsprj):
  object list := { tag object } 0xFF
  tag 0x00    -> element: { 0xFF label } core   (component or wire)
  tag 0x01    -> junction dot: x y wire_ptr[4]
Pointers are absolute file offsets of the referenced wire's tag byte.
"""
import struct

INCH = 2540000  # internal units per inch (10 nm)

WIRE_STYLE = bytes.fromhex('1d000000' '00c09e0000' '00400000' '01' 'ffffff00' 'ffffff00' '02' '7f') + b'WIRE\x00'


class Reader:
    def __init__(self, data, pos):
        self.d = data
        self.p = pos

    def u8(self):
        v = self.d[self.p]
        self.p += 1
        return v

    def u16(self):
        v = struct.unpack_from('<H', self.d, self.p)[0]
        self.p += 2
        return v

    def u32(self):
        v = struct.unpack_from('<I', self.d, self.p)[0]
        self.p += 4
        return v

    def i32(self):
        v = struct.unpack_from('<i', self.d, self.p)[0]
        self.p += 4
        return v

    def pstr(self):
        n = self.u8()
        s = self.d[self.p:self.p + n]
        self.p += n
        return s.decode('latin-1')

    def cstr(self):
        e = self.d.index(b'\x00', self.p)
        s = self.d[self.p:e]
        self.p = e + 1
        return s.decode('latin-1')

    def raw(self, n):
        s = self.d[self.p:self.p + n]
        self.p += n
        return s


def read_label(r):
    lab = {}
    lab['text'] = r.pstr()
    lab['x'] = r.i32()
    lab['y'] = r.i32()
    lab['u0'] = r.u32()
    lab['just'] = r.u8()
    lab['sflag'] = r.u8()
    lab['mask'] = r.u32()
    lab['height'] = r.u32()
    lab['width'] = r.u32()
    lab['u1'] = r.u32()
    lab['attr'] = r.raw(4)
    lab['font'] = r.cstr()
    lab['style'] = r.cstr()
    lab['tail'] = r.u32()
    return lab


def read_objects(data, start, npins_of):
    """Parse the object list starting at the first tag byte. Returns (objects, end_pos_after_ff)."""
    r = Reader(data, start)
    objs = []
    while True:
        at = r.p
        tag = r.u8()
        if tag == 0xFF:
            return objs, r.p
        if tag == 0x01:
            o = {'kind': 'dot', 'at': at, 'x': r.i32(), 'y': r.i32(), 'ptrs': [r.u32() for _ in range(4)]}
            objs.append(o)
            continue
        assert tag == 0x00, (hex(at), tag)
        labels = []
        while r.d[r.p] == 0xFF:
            r.p += 1
            labels.append(read_label(r))
        if r.d[r.p:r.p + len(WIRE_STYLE)] == WIRE_STYLE and not labels:
            r.p += len(WIRE_STYLE)
            pre = r.u16()
            n = r.u16()
            pts = [(r.i32(), r.i32()) for _ in range(n)]
            objs.append({'kind': 'wire', 'at': at, 'pre': pre, 'pts': pts})
            continue
        c = {'kind': 'comp', 'at': at, 'labels': labels}
        c['proplen'] = r.u32()
        c['npins'] = r.u16()
        c['device'] = r.pstr()
        c['x'] = r.i32()
        c['y'] = r.i32()
        c['rot'] = r.u32()
        c['id'] = r.u8()
        c['res'] = r.raw(12)
        c['pins'] = [r.u32() for _ in range(c['npins'])]
        objs.append(c)


def w_label(lab):
    t = lab['text'].encode('latin-1')
    assert len(t) < 256
    return (b'\xff' + bytes([len(t)]) + t
            + struct.pack('<iiIBBIIII', lab['x'], lab['y'], lab['u0'], lab['just'], lab['sflag'],
                          lab['mask'], lab['height'], lab['width'], lab['u1'])
            + lab['attr'] + lab['font'].encode('latin-1') + b'\x00'
            + lab['style'].encode('latin-1') + b'\x00' + struct.pack('<I', lab['tail']))


def w_comp(c):
    dev = c['device'].encode('latin-1')
    out = b'\x00' + b''.join(w_label(l) for l in c['labels'])
    out += struct.pack('<IH', c['proplen'], c['npins']) + bytes([len(dev)]) + dev
    out += struct.pack('<iiIB', c['x'], c['y'], c['rot'], c['id']) + c['res']
    out += b''.join(struct.pack('<I', p) for p in c['pins'])
    return out


def w_wire(w):
    out = b'\x00' + WIRE_STYLE + struct.pack('<HH', w['pre'], len(w['pts']))
    out += b''.join(struct.pack('<ii', x, y) for x, y in w['pts'])
    return out


def w_dot(o):
    assert len(o['ptrs']) == 4
    return b'\x01' + struct.pack('<ii', o['x'], o['y']) + b''.join(struct.pack('<I', p) for p in o['ptrs'])


def wire_size(npts):
    return 1 + len(WIRE_STYLE) + 4 + 8 * npts
