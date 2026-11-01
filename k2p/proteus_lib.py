"""Read Proteus 8 device libraries (DATA/LIBRARY/*.LIB, read-only).

A .LIB file: 'DEVICE LIBRARY\\x1a\\0', u32 capacity, u32 count, u32 entry size (0x5c?), u32 0, then `count` index
entries of 80 bytes (name[64], u32 offset, u32 timestamp, u32 size, u32 0). At `offset` is the device record from its
L field on: the same bytes Proteus embeds in a project (pstr name + u32 timestamp + these bytes).
usage: python proteus_lib.py NAME [NAME ...]     (prints library, pins and properties of each device)
"""
import os as _os, sys as _sys, glob, struct
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))

# Proteus's device libraries: K2P_PROTEUS_LIBRARY, else Proteus 8 Professional's default install
LIBDIR = _os.environ.get('K2P_PROTEUS_LIBRARY') or \
    r'C:\Program Files (x86)\Labcenter Electronics\Proteus 8 Professional\DATA\LIBRARY'


def index(path):
    d = open(path, 'rb').read()
    if not d.startswith(b'DEVICE LIBRARY\x1a'):
        return d, {}
    n = struct.unpack_from('<I', d, 20)[0]
    out = {}
    for i in range(n):
        q = 32 + 80 * i
        name = d[q:q + 64].split(b'\0')[0].decode('latin-1')
        off, ts, size, _ = struct.unpack_from('<IIII', d, q + 64)
        if name:
            out[name] = (off, ts, size)
    return d, out


def find_device(name):
    """(library file, embedded-style record bytes) of the first library that holds `name`."""
    for path in sorted(glob.glob(_os.path.join(LIBDIR, '*.LIB'))):
        d, idx = index(path)
        if name in idx:
            off, ts, _ = idx[name]
            L = struct.unpack_from('<I', d, off)[0]
            nb = name.encode('latin-1')
            return path, bytes([len(nb)]) + nb + struct.pack('<I', ts) + d[off:off + L]
    return None, None


def pins(rec):
    """Pin records of a device record: (kind, flags, x, y, orientation, symbol, name, number)."""
    n = rec[0]
    ts, L, symlen, npins, u1, u2, glen, nitems, one = struct.unpack_from('<IIIIHHIII', rec, 1 + n)
    p = 1 + n + 20 + glen
    out = []
    for _ in range(npins & 0xffff):
        kind, flags, _u, x, y, o = struct.unpack_from('<BBHiiI', rec, p)
        q = p + 16
        s = []
        for _ in range(3):
            e = rec.index(b'\x00', q)
            s.append(rec[q:e].decode('latin-1'))
            q = e + 1
        out.append((kind, flags, x, y, o, *s))
        p = q
    return out


def props(rec):
    n = rec[0]
    symlen = struct.unpack_from('<I', rec, 1 + n + 8)[0]
    return rec[1 + n + 8 + symlen:].rstrip(b'\x00').decode('latin-1')


if __name__ == '__main__':
    for nm in _sys.argv[1:]:
        path, rec = find_device(nm)
        if rec is None:
            print(f'== {nm}: not found')
            continue
        npins = struct.unpack_from('<I', rec, 1 + rec[0] + 12)[0]
        print(f'== {nm} ({_os.path.basename(path)}), npins field {npins:#x}')
        try:
            for p in pins(rec):
                print('   pin kind %d flags %#04x (%.3f, %.3f) o=%#x %s name=%r number=%r' %
                      (p[0], p[1], p[2] / 2540000, p[3] / 2540000, p[4], p[5], p[6], p[7]))
        except Exception as e:
            print('   pins: cannot parse', e)
        print('   ' + props(rec).replace('\n', '\n   '))
