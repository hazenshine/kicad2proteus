"""Strict KiCad -> Proteus 8 converter.

usage: python k2p/k2p.py KICAD.kicad_sch OUT.pdsprj
Reads the schematic, checks it, maps every item one to one and writes OUT.pdsprj plus OUT.rapor.txt. If anything cannot
be transferred exactly the .pdsprj is not written and the report says why (exit code 1). The only additions are the
four user-approved ones: a 1-wire dot on a free wire end, pin -> zero-length wire -> dot for a
junction on a pin end, no sheet frame, custom pin symbols with KiCad's pin length.

Frame: templates/base.pdsprj (saved by Proteus 8.17). Its object list, its five gate devices, its CDB and its title
are replaced; styles, system symbols, instruments and the template are kept.
"""
import os as _os, sys as _sys
HERE = _os.path.dirname(_os.path.abspath(__file__))
_sys.path.insert(0, HERE)
import copy, json, math, re, struct, subprocess, tempfile, time
from collections import defaultdict
from decimal import Decimal
import synth
from sexp import parse, find, first

# kicad-cli: K2P_KICAD_CLI, else KiCad 10's default install, else the PATH
KICAD_CLI = _os.environ.get('K2P_KICAD_CLI') or next(
    (p for p in (r'C:\Program Files\KiCad\10.0\bin\kicad-cli.exe',) if _os.path.exists(p)), 'kicad-cli')
# files saved by Proteus 8.17
TEMPLATES = _os.path.join(HERE, 'templates')
BASE = _os.path.join(TEMPLATES, 'base.pdsprj')
LABEL_BASE = _os.path.join(TEMPLATES, 'label.pdsprj')
TEXT_BASE = _os.path.join(TEMPLATES, 'text.pdsprj')
TERM_BASE = _os.path.join(TEMPLATES, 'power_terminal.pdsprj')
GND_BASE = _os.path.join(TEMPLATES, 'ground_terminal.pdsprj')       # Proteus 8.17's own ground terminal
U = 100000                      # Proteus units (10 nm) per mm
PAPER = {'A5': (210, 148), 'A4': (297, 210), 'A3': (420, 297), 'A2': (594, 420), 'A1': (841, 594), 'A0': (1189, 841)}
LIB_TS = 0x60d9867a             # library timestamp written in device records (as Proteus's own gate devices)
SYM_TS = 0x3385b3de             # timestamp of the system pin symbols
PIN_FLAGS = 0x0a                # pin record display flags as Proteus's gates (0x02 draws the pin, 0x04 shows its name)
PROPS_IN_LABEL_MAX = 50         # longest component property text kept in the PROPERTIES label (F2_cell_c/d)
# Proteus draws pin numbers only for packaged devices (S16: no flag shows them), so KiCad's pin numbers are written as
# text items of the symbol, where KiCad draws them: centred on the pin, bottom 14 mil above the pin line (measured in
# KiCad's SVG of BDT_Odev: pin line y 158.75 mm, number baseline 158.3944 mm).
NUMBER_GAP = 35560

# KiCad symbol -> (Proteus device whose simulation model is used, {unit: {KiCad pin number: Proteus pin name}}),
# read from k2p/eslesme.json so new parts are added without touching the code
def load_parts(path=_os.path.join(HERE, 'eslesme.json')):
    j = json.load(open(path, encoding='utf-8'))
    info = {k: v for k, v in j.items() if not k.startswith('_')}
    label = lambda v: v.get('proteus') or v.get('kutuphane') or v.get('proteus_aygit') or v['mdf']['ad']
    return {k: (label(v), {int(u): pins for u, pins in v['birimler'].items()}) for k, v in info.items()}, info


PARTS, PARTS_INFO = load_parts()


def mdf_text(m, names):
    """A LISA model (Proteus's text model format, as 74INV.MDF): one primitive per gate, gate pins as ports."""
    gates = [(names[a], names[y]) for a, y in m['kapilar']]      # ports = the device's pin names
    out = ['LISA MODEL DESCRIPTION FORMAT 6.1', '=================================',
           f'Design:   k2p {m["ad"]}', 'Doc. no.: <NONE>', 'Revision: <NONE>', 'Author:   <NONE>',
           'Created:  01/11/26', 'Modified: 01/11/26', '', '*PROPERTIES,0    ', '', '*MODELDEFS,0    ', '',
           f'*PARTLIST,{len(gates):<5}']
    out += [f'U{i},{m["eleman"]},{m["eleman"]},{m["ozellik"]}' for i in range(1, len(gates) + 1)]
    out += ['', f'*NETLIST,{2 * len(gates):<5}']
    for i, (a, y) in enumerate(gates, 1):
        out += [f'{a},2', f'{a},IT', f'U{i},IP,{m["giris"]}', '', f'{y},2', f'{y},OT', f'U{i},OP,{m["cikis"]}', '']
    out += ['*GATES,0    ', '']
    return '\r\n'.join(out)
# ERC violation types that mean a wiring fault; anything else is listed in the report but does not stop
ERC_STOP = {'multiple_net_names', 'label_multiple_wires', 'pin_to_pin', 'net_not_bus_member', 'bus_to_net_conflict',
            'four_way_junction', 'unconnected_wire_endpoint', 'wire_dangling', 'similar_labels', 'similar_power',
            'similar_label_and_power', 'same_local_global_label', 'duplicate_reference', 'endpoint_off_grid',
            'different_unit_net', 'different_unit_footprint', 'missing_bidi_pin'}


class Stop(Exception):
    """The schematic cannot be transferred exactly; no output is written."""


def mm(s):
    v = Decimal(s) * U
    if v != v.to_integral_value():
        raise Stop(f'koordinat 10 nm katı değil: {s} mm')
    return int(v)


def text_of(s):
    if re.search(r'\\[^n]', s):
        raise Stop(f'metinde çözülmeyen kaçış dizisi: {s!r}')
    return s.replace('\\n', '\n')


# ---------------------------------------------------------------- KiCad reading

SUPPORTED_TOP = {'version', 'generator', 'generator_version', 'uuid', 'paper', 'title_block', 'lib_symbols', 'symbol',
                 'wire', 'junction', 'label', 'text', 'sheet_instances', 'embedded_fonts', 'no_connect',
                 'global_label'}


def effects(node):
    e = first(node, 'effects')
    font = first(e, 'font') if e else None
    size = first(font, 'size') if font else None
    just = first(e, 'justify') if e else None
    hide = first(node, 'hide') or (first(e, 'hide') if e else None)
    if font and (first(font, 'bold') or first(font, 'italic') or first(font, 'face') or first(font, 'thickness')):
        raise Stop(f'desteklenmeyen yazı tipi ayarı: {font}')
    return {'size': (size[1], size[2]) if size else ('1.27', '1.27'), 'just': list(just[1:]) if just else [],
            'hide': bool(hide and (len(hide) == 1 or hide[1] == 'yes'))}


def read_lib_symbol(node):
    """{unit: {'shapes': [...], 'pins': [...]}, 'flags': {...}} in symbol coordinates (mm strings, +y up); unit 0 is
    common to all units."""
    if first(node, 'extends'):
        raise Stop(f'{node[1]}: desteklenmeyen sembol özelliği extends')
    pn, pnum = first(node, 'pin_names'), first(node, 'pin_numbers')
    hidden = lambda n: bool(n and first(n, 'hide') and first(n, 'hide')[1] == 'yes')
    offset = first(pn, 'offset')[1] if pn and first(pn, 'offset') else '0.508'      # KiCad's default name offset
    units = defaultdict(lambda: {'shapes': [], 'pins': []})
    for sub in find(node, 'symbol'):
        unit, style = (int(v) for v in sub[1].rsplit('_', 2)[-2:])
        if style not in (0, 1):
            continue                    # De Morgan body style 2 is never used here (instances carry no convert)
        u = units[unit]
        for c in sub[2:]:
            if not isinstance(c, list):
                continue
            kind = c[0]
            if kind in ('arc', 'polyline', 'circle', 'rectangle'):
                stroke = first(c, 'stroke')
                st = first(stroke, 'type') if stroke else None
                if st and st[1] not in ('default', 'solid'):
                    raise Stop(f'{node[1]}: kesikli çizgi desteklenmiyor')
                fill = first(first(c, 'fill'), 'type')[1] if first(c, 'fill') else 'none'
                sh = {'kind': kind, 'fill': fill}
                if kind == 'arc':
                    sh['pts'] = [(first(c, k)[1], first(c, k)[2]) for k in ('start', 'mid', 'end')]
                elif kind == 'polyline':
                    sh['pts'] = [(p[1], p[2]) for p in find(first(c, 'pts'), 'xy')]
                elif kind == 'circle':
                    sh['center'] = (first(c, 'center')[1], first(c, 'center')[2])
                    sh['radius'] = first(c, 'radius')[1]
                else:
                    sh['pts'] = [(first(c, k)[1], first(c, k)[2]) for k in ('start', 'end')]
                u['shapes'].append(sh)
            elif kind == 'text':
                at = first(c, 'at')
                u['shapes'].append({'kind': 'text', 'text': c[1], 'at': (at[1], at[2])})   # only in Proteus_Sim symbols
            elif kind == 'pin':
                at = first(c, 'at')
                name, number = first(c, 'name'), first(c, 'number')
                if first(c, 'alternate') or (first(c, 'hide') and not first(node, 'power')):
                    raise Stop(f'{node[1]}: gizli/alternatif pin desteklenmiyor')
                nm = name[1] if len(name) > 1 and isinstance(name[1], str) else ''
                u['pins'].append({'etype': c[1], 'shape': c[2], 'at': (at[1], at[2]), 'angle': int(at[3]),
                                  'length': first(c, 'length')[1], 'name': '' if nm == '~' else nm,
                                  'number': number[1], 'name_size': effects(name)['size'],
                                  'number_size': effects(number)['size']})
            else:
                raise Stop(f'{node[1]}: desteklenmeyen sembol çizimi {kind}')
    out = dict(units)
    out['flags'] = {'names_hidden': hidden(pn), 'numbers_hidden': hidden(pnum), 'names_offset': offset,
                    'power': bool(first(node, 'power')), 'nunits': max([u for u in units if u] or [1])}
    return out


def read_kicad(path):
    t = parse(open(path, encoding='utf-8').read())
    tops = defaultdict(int)
    for c in t[1:]:
        if isinstance(c, list):
            tops[c[0]] += 1
    bad = {k: v for k, v in tops.items() if k not in SUPPORTED_TOP}
    if bad:
        raise Stop('desteklenmeyen şema öğeleri: ' + ', '.join(f'{k} ×{v}' for k, v in sorted(bad.items())))
    paper = first(t, 'paper')
    if paper[1] not in PAPER:
        raise Stop(f'desteklenmeyen sayfa boyutu {paper[1:]}')
    w, h = PAPER[paper[1]]
    if 'portrait' in paper[2:]:
        w, h = h, w
    tb = first(t, 'title_block') or ['title_block']
    title = {c[0]: c[1] for c in tb[1:] if isinstance(c, list)}
    if set(title) - {'title', 'rev'}:
        raise Stop(f'başlık bloğunda eşlenmeyen alan: {sorted(set(title) - {"title", "rev"})}')
    libs = {s[1]: read_lib_symbol(s) for s in find(first(t, 'lib_symbols'), 'symbol')}
    syms = []
    for s in find(t, 'symbol'):
        at = first(s, 'at')
        bs = first(s, 'convert') or first(s, 'body_style')
        if bs and bs[1] != '1':
            raise Stop('De Morgan gövde biçimi desteklenmiyor')
        props = {}
        for p in find(s, 'property'):
            pa = first(p, 'at')
            props[p[1]] = {'text': p[2], 'at': (pa[1], pa[2]), 'angle': pa[3] if len(pa) > 3 else '0',
                           **effects(p)}
        syms.append({'lib_id': first(s, 'lib_id')[1], 'at': (at[1], at[2]), 'angle': int(at[3]),
                     'mirror': (first(s, 'mirror') or [None, None])[1], 'unit': int(first(s, 'unit')[1]),
                     'props': props, 'ref': props['Reference']['text']})
    wires = []
    for wn in find(t, 'wire'):
        pts = [(p[1], p[2]) for p in find(first(wn, 'pts'), 'xy')]
        stroke = first(wn, 'stroke')
        if len(pts) != 2 or (stroke and first(stroke, 'type')[1] not in ('default', 'solid')):
            raise Stop(f'desteklenmeyen tel: {wn}')
        wires.append(pts)
    junctions = [(first(j, 'at')[1], first(j, 'at')[2]) for j in find(t, 'junction')]
    labels = []
    for l in find(t, 'label'):
        at = first(l, 'at')
        labels.append({'text': text_of(l[1]), 'at': (at[1], at[2]), 'angle': int(at[3]), **effects(l)})
    texts = []
    for x in find(t, 'text'):
        at = first(x, 'at')
        texts.append({'text': text_of(x[1]), 'at': (at[1], at[2]), 'angle': int(at[3]), **effects(x)})
    no_connects = [(first(j, 'at')[1], first(j, 'at')[2]) for j in find(t, 'no_connect')]
    globals_ = []
    for l in find(t, 'global_label'):
        at = first(l, 'at')
        refs = next((p for p in find(l, 'property') if p[1] == 'Intersheetrefs'), None)
        globals_.append({'text': text_of(l[1]), 'shape': first(l, 'shape')[1], 'at': (at[1], at[2]),
                         'angle': int(at[3]), 'refs_shown': bool(refs) and not effects(refs)['hide'], **effects(l)})
    return {'size': (w, h), 'title': title, 'libs': libs, 'syms': syms, 'wires': wires, 'junctions': junctions,
            'labels': labels, 'texts': texts, 'no_connects': no_connects, 'globals': globals_}


ERC_TR = {'missing_power_pin': 'güç pinleri olan birim (VCC/GND) yerleştirilmemiş',
          'pin_not_driven': 'giriş pinini süren bir çıkış yok', 'isolated_pin_label': 'etiket tek bir pine bağlı',
          'lib_symbol_issues': 'sembol KiCad kütüphanesindekinden farklı / kütüphane bulunamadı',
          'missing_input_pin': 'entegrenin bazı birimleri yerleştirilmemiş', 'missing_unit': 'yerleştirilmemiş birim',
          'pin_to_pin': 'iki pin türü birbirine bağlanmamalı', 'footprint_link_issues': 'kılıf (footprint) bağlantısı',
          'multiple_net_names': 'bir ağa birden çok ad (kısa devre)', 'wire_dangling': 'boşta tel'}


def erc(path):
    """KiCad ERC. Returns (report lines, blocking violations). A wiring fault stops the conversion, except one
    caused only by a PWR_FLAG: that symbol is a KiCad ERC marker with no Proteus counterpart and is not transferred."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _os.path.join(tmp, 'erc.json')
        subprocess.run([KICAD_CLI, 'sch', 'erc', '--format', 'json', '--severity-all', '-o', out, path],
                       capture_output=True, stdin=subprocess.DEVNULL, check=False)
        j = json.load(open(out, encoding='utf-8'))
    viol = [v for s in j['sheets'] for v in s['violations']]
    flag = lambda v: any('#FLG' in i['description'] for i in v['items'])
    counts = defaultdict(int)
    for v in viol:
        counts[(v['severity'], v['type'])] += 1
    lines = [f'KiCad ERC {len(viol)} ihlal bildirdi:']
    for (sev, typ), n in sorted(counts.items()):
        lines.append(f'   {"hata" if sev == "error" else "uyarı":5} {typ:24} ×{n:<3} {ERC_TR.get(typ, "")}')
    stop, flagged = [], []
    for v in viol:
        if v['type'] in ERC_STOP:
            (flagged if flag(v) else stop).append(v)
    for v in flagged:
        lines.append(f'   Durdurmadı (yalnız PWR_FLAG kaynaklı; PWR_FLAG Proteus\'a taşınmıyor): {v["description"]} '
                     f'{[i["description"] for i in v["items"]]}')
    for v in stop:
        lines.append(f'   DURDURAN BAĞLANTI HATASI: {v["description"]} {[i["description"] for i in v["items"]]}')
    if not stop:
        lines.append('   Bağlantı hatası (kısa devre, çakışan çıkış vb.) yok; diğer ihlaller dönüşümü etkilemiyor.')
    return lines, stop


def scan(path):
    """Every item the converter cannot transfer yet, found in one pass (so the report lists all of them at once)."""
    t = parse(open(path, encoding='utf-8').read())
    issues = []
    add = lambda what, why: issues.append(f'{what}: {why}')
    names = {'no_connect': '"bağlantı yok" (X) işareti', 'global_label': 'global etiket',
             'hierarchical_label': 'hiyerarşik etiket', 'bus': 'veri yolu', 'bus_entry': 'veri yolu girişi',
             'sheet': 'alt sayfa', 'image': 'resim', 'polyline': 'çizgi çizimi', 'rectangle': 'dikdörtgen çizimi',
             'circle': 'daire çizimi', 'arc': 'yay çizimi', 'text_box': 'metin kutusu', 'table': 'tablo',
             'netclass_flag': 'ağ sınıfı işareti', 'directive_label': 'yönerge etiketi'}
    for c in t[1:]:
        if isinstance(c, list) and c[0] not in SUPPORTED_TOP:
            at = first(c, 'at')
            add(f'{names.get(c[0], c[0])}' + (f' ({at[1]}, {at[2]}) mm' if at else ''), 'henüz desteklenmiyor')
    paper = first(t, 'paper')
    if paper and paper[1] not in PAPER:
        add(f'sayfa {paper[1]}', 'bu sayfa boyutu desteklenmiyor')
    tb = first(t, 'title_block')
    for c in (tb[1:] if tb else []):
        if isinstance(c, list) and c[0] not in ('title', 'rev'):
            add(f'başlık bloğu alanı {c[0]}', 'Proteus alanına eşlenmedi')
    libs = {s[1]: s for s in find(first(t, 'lib_symbols'), 'symbol')}
    used = defaultdict(set)                       # only the units placed in the schematic matter
    for s in find(t, 'symbol'):
        used[first(s, 'lib_id')[1]].add(int(first(s, 'unit')[1]))
    seen_lib = set()
    for s in find(t, 'symbol'):
        lib_id = first(s, 'lib_id')[1]
        props = {p[1]: p for p in find(s, 'property')}
        ref = props['Reference'][2]
        at = first(s, 'at')
        where = f'{ref} ({lib_id}, {at[1]}, {at[2]} mm)'
        lib = libs.get(lib_id)
        if lib is not None and first(lib, 'power'):
            if first(s, 'mirror'):
                add(where, 'aynalanmış güç sembolü henüz doğrulanmadı')
            continue                     # power symbols become terminals (PWR_FLAG is not transferred)
        if 'proteus_aygit' in PARTS_INFO.get(lib_id, {}):
            # drawn from a Proteus device (kicad_library/Proteus_Sim): the device is embedded as it is, so the
            # KiCad drawing must be that device's drawing; fields are hidden, as Proteus shows none for these parts
            import proteus_lib, proteus_kicad_lib
            if len(at) > 3 and at[3] not in ('0', '90', '180', '270'):
                add(where, f'{at[3]}° açı desteklenmiyor')
            if first(s, 'mirror'):
                add(where, 'aynalanmış sembol henüz doğrulanmadı')
            if lib is not None and lib_id not in seen_lib:
                seen_lib.add(lib_id)
                rec = proteus_lib.find_device(PARTS_INFO[lib_id]['proteus_aygit'])[1]
                if rec is None:
                    add(f'sembol {lib_id}', 'Proteus kütüphanesinde aygıt yok')
                elif proteus_kicad_lib.kicad_graphics(lib) != proteus_kicad_lib.graphics(rec):
                    add(f'sembol {lib_id}', 'çizimi Proteus aygıtının çiziminden farklı (Proteus_Sim kütüphanesindeki '
                                            'sembol kullanılmalı)')
            continue
        if lib_id in PARTS and int(first(s, 'unit')[1]) not in PARTS[lib_id][1]:
            add(where, f'birim {first(s, "unit")[1]} eşleme tablosunda yok')
        if len(at) > 3 and at[3] not in ('0', '90', '180', '270'):
            add(where, f'{at[3]}° açı desteklenmiyor')
        for pn in ('Reference', 'Value'):
            p = props.get(pn)
            if p is not None and effects(p)['hide']:
                add(f'{where} {pn} alanı', 'gizli alan henüz desteklenmiyor')
        if lib is None or lib_id in seen_lib:
            continue
        seen_lib.add(lib_id)
        for sub in find(lib, 'symbol'):
            unit, style = (int(v) for v in sub[1].rsplit('_', 2)[-2:])
            if unit not in used[lib_id] | {0} or style not in (0, 1):
                continue
            for c in sub[2:]:
                if not isinstance(c, list):
                    continue
                if c[0] in ('bezier', 'text', 'text_box'):
                    add(f'sembol {lib_id}', f'{names.get(c[0], c[0])} henüz desteklenmiyor')
                elif c[0] in ('polyline', 'rectangle', 'circle', 'arc'):
                    f = first(c, 'fill')
                    ft = first(f, 'type')[1] if f else 'none'
                    if ft not in ('none', 'background', 'outline'):
                        add(f'sembol {lib_id}', f'dolgu türü {ft} desteklenmiyor')
                elif c[0] == 'pin':
                    num = first(c, 'number')[1]
                    if c[2] not in ('line', 'inverted'):
                        add(f'sembol {lib_id} pin {num}', f'pin biçimi {c[2]} henüz yok')
                    if c[1] not in PIN_KIND:
                        add(f'sembol {lib_id} pin {num}', f'pin türü {c[1]} henüz yok')
                    if first(c, 'at')[3] not in ('0', '90', '180', '270'):
                        add(f'sembol {lib_id} pin {num}', 'eğik pin desteklenmiyor')
    for l in find(t, 'label'):
        at = first(l, 'at')
        if len(at) > 3 and at[3] not in ('0', '90', '180', '270'):
            add(f'etiket "{l[1]}" ({at[1]}, {at[2]}) mm', f'{at[3]}° etiket desteklenmiyor')
    for x in find(t, 'text'):
        at = first(x, 'at')
        if len(at) > 3 and at[3] not in ('0', '0.0'):
            add(f'metin ({at[1]}, {at[2]}) mm', 'döndürülmüş metin henüz doğrulanmadı')
    out = []
    for i in issues:                      # keep order, drop repeats
        if i not in out:
            out.append(i)
    return out


_SVG = {}


def kicad_svg(path):
    """KiCad's own SVG plot of the schematic (kicad-cli), made once per file."""
    if path not in _SVG:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([KICAD_CLI, 'sch', 'export', 'svg', '-o', tmp, path], capture_output=True,
                           stdin=subprocess.DEVNULL, check=True)
            _SVG[path] = open(_os.path.join(tmp, _os.path.splitext(_os.path.basename(path))[0] + '.svg'),
                              encoding='utf-8').read()
    return _SVG[path]


def kicad_svg_outlines(path):
    """Closed outlines KiCad plots as 'M x,y x,y ... Z' paths (global label frames), as lists of (x, y) mm strings."""
    out = []
    for m in re.finditer(r'd="M ([\d.,\s-]+?)\s*Z"', kicad_svg(path)):
        out.append([tuple(p.split(',')) for p in m.group(1).split()])
    return out


def kicad_svg_texts(path):
    """Texts as KiCad plots them (kicad-cli SVG): text, x, y (bottom of the text cell, mm), anchor."""
    import html
    svg = kicad_svg(path)
    # a rotated text is wrapped in <g transform="rotate(-90 cx cy)">: rotated about the middle of its text line
    out = []
    for m in re.finditer(r'(<g transform="rotate\(([-\d.]+) [-\d.]+ [-\d.]+\)">\s*)?<text x="([\d.-]+)" '
                         r'y="([\d.-]+)"\s*textLength="([\d.]+)" font-size="([\d.]+)"[^>]*?text-anchor="(\w+)"'
                         r'[^>]*>([^<]*)</text>', svg):
        rot = round(-float(m.group(2))) % 360 if m.group(1) else 0
        out.append({'x': m.group(3), 'y': m.group(4), 'len': m.group(5), 'anchor': m.group(7), 'rot': rot,
                    'size': str(Decimal(m.group(6)) * 3 / 4), 'text': html.unescape(m.group(8))})
    return out


def kicad_netlist(path):
    """{frozenset((ref, pin number))} from kicad-cli's netlist (the truth the output is compared with)."""
    with tempfile.TemporaryDirectory() as tmp:
        out = _os.path.join(tmp, 'n.net')
        subprocess.run([KICAD_CLI, 'sch', 'export', 'netlist', '-o', out, path], capture_output=True,
                       stdin=subprocess.DEVNULL, check=True)
        t = parse(open(out, encoding='utf-8').read())
    nets = set()
    for n in find(first(t, 'nets'), 'net'):
        nets.add(frozenset((first(x, 'ref')[1], first(x, 'pin')[1]) for x in find(n, 'node')))
    return nets


# ---------------------------------------------------------------- Proteus frame pieces

def pstr(s):
    b = s.encode('latin-1')
    assert len(b) < 256
    return bytes([len(b)]) + b


def item(typ, const, data, style):
    """One graphics item of a symbol or device record: u32 length, u32 type constant, type, 02, style, data."""
    body = bytes([typ, 2]) + style + data
    return struct.pack('<II', 8 + len(body), const) + body


def parse_items(d, q, n):
    out = []
    for _ in range(n):
        ln = struct.unpack_from('<I', d, q)[0]
        out.append(d[q:q + ln])
        q += ln
    return out, q


def symbol_list(d):
    """(start, end, [(name, record bytes)]) of the embedded symbol list ($MKR..., $PIN..., HEADER)."""
    start = d.index(b'\xfe\xff\x0b$MKRBUSNODE') + 2
    a, recs = start, []
    while d[a]:
        n = d[a]
        L = struct.unpack_from('<I', d, a + 1 + n + 4)[0]
        recs.append((d[a + 1:a + 1 + n], d[a:a + 1 + n + 4 + L]))
        a += 1 + n + 4 + L
    return start, a, recs


def device_list(d, sym_end):
    start = sym_end + 3
    assert d[sym_end:start] == b'\x00\xfe\xff'
    a, recs = start, []
    while d[a]:
        n = d[a]
        L = struct.unpack_from('<I', d, a + 1 + n + 4)[0]
        recs.append((d[a + 1:a + 1 + n], d[a:a + 1 + n + 4 + L]))
        a += 1 + n + 4 + L
    return start, a, recs


def device_parts(rec):
    """Split a device record: name, items, pins (raw), property bytes."""
    n = rec[0]
    name = rec[1:1 + n]
    ts, L, symlen, npins, u1, u2, glen, nitems, one = struct.unpack_from('<IIIIHHIII', rec, 1 + n)
    q = 1 + n + 32
    items, q = parse_items(rec, q, nitems)
    prop_at = 1 + n + 8 + symlen
    pins = rec[q:prop_at - 4]
    props = rec[prop_at:]
    assert struct.unpack_from('<I', rec, prop_at - 4)[0] == len(props)
    return {'name': name, 'ts': ts, 'npins': npins, 'items': items, 'pins': pins, 'props': props}


def device_record(name, ts, npins, items, pins, props, states=()):
    """states: [(name, items)] drawn by an active model (npins high 16 bits = their count)."""
    g = b''.join(items)
    st = b''.join(n.encode('latin-1') + b'\x00' + struct.pack('<III', 12 + len(b''.join(it)), len(it), 1) + b''.join(it)
                  for n, it in states)
    sym = struct.pack('<IHH', npins | (len(states) << 16), 1, 1) + struct.pack('<III', 12 + len(g), len(items), 1) + g + \
        pins + st
    body = struct.pack('<I', 4 + len(sym) + 4) + sym + struct.pack('<I', len(props)) + props
    return pstr(name) + struct.pack('<II', ts, 4 + len(body)) + body


def symbol_record(name, ts, items):
    body = struct.pack('<II', len(items), 1) + b''.join(items)
    return pstr(name) + struct.pack('<II', ts, 4 + len(body)) + body


class Frame:
    def __init__(self):
        self.f = synth.load(BASE)
        d = self.f.dsn
        self.sym_start, self.sym_end, self.syms = symbol_list(d)
        self.dev_start, self.dev_end, self.devs = device_list(d, self.sym_end)
        self.dev = {n.decode('latin-1'): device_parts(r) for n, r in self.devs
                    if n in (b'AND', b'NAND', b'NOR', b'NOT', b'OR')}
        and_items = self.dev['AND']['items']
        self.style = and_items[0][10:10 + struct.unpack_from('<I', and_items[0], 10)[0]]   # COMPONENT style
        assert self.style.endswith(b'COMPONENT\x00')
        self.origin_item = and_items[1]
        assert b'$MKRORIGIN' in self.origin_item
        # self-check: the record writer reproduces Proteus's own AND record byte for byte
        p = self.dev['AND']
        assert device_record('AND', p['ts'], p['npins'], p['items'], p['pins'], p['props']) == dict(self.devs)[b'AND']
        self.sym = {n.decode('latin-1'): r for n, r in self.syms}
        self.comp_tpl = self.f.objs[0]
        assert self.comp_tpl['device'] == 'AND' and len(self.comp_tpl['labels']) == 4
        lf = synth.load(LABEL_BASE)
        self.label_tpl = next(o for o in lf.objs if o['kind'] == 'wire' and o['wlabels'])['wlabels'][0]
        tf = synth.load(TEXT_BASE)
        self.text_tpl = next(o for o in tf.objs if o['kind'] == 'text2d')
        rec = self.sym['$TERPOWER']                       # terminal style, markers and terminal label template
        n = rec[0]
        items, _ = parse_items(rec, 1 + n + 16, struct.unpack_from('<I', rec, 1 + n + 8)[0])
        line = next(i for i in items if i[8] == 0)
        self.term_style = line[10:10 + struct.unpack_from('<I', line, 10)[0]]
        assert self.term_style.endswith(b'TERMINAL\x00')
        self.term_markers = {i[28:i.index(b'\x00', 28)]: i for i in items if i[8] == 6}
        kf = synth.load(TERM_BASE)
        self.term_tpl = next(o for o in kf.objs if o['kind'] == 'term' and o['sym'] == '$TERPOWER')
        gf = synth.load(GND_BASE)
        self.gnd_tpl = next(o for o in gf.objs if o['kind'] == 'term' and o['sym'] == '$TERGROUND')


# ---------------------------------------------------------------- symbols and devices

def pin_symbol(frame, length, inverted, deco):
    """A pin symbol like $PINDEFAULT / $PININVERT whose line is `length` long (node at -length, number marker at the
    middle). Inverted: KiCad's bubble, radius `deco`, touching the body end."""
    base = frame.sym['$PININVERT' if inverted else '$PINDEFAULT']
    n = base[0]
    cnt = struct.unpack_from('<I', base, 1 + n + 8)[0]
    items, _ = parse_items(base, 1 + n + 16, cnt)
    out = []
    for it in items:
        typ = it[8]
        sl = struct.unpack_from('<I', it, 10)[0] if typ != 6 else 0
        if typ == 0:                                   # the pin line
            head = it[:10 + sl]
            x2 = -2 * deco if inverted else 0
            out.append(head + struct.pack('<iiii', -length, 0, x2, 0))
        elif typ == 2:                                 # the bubble
            out.append(it[:10 + sl] + struct.pack('<iii', -deco, 0, deco))
        elif typ == 6:                                 # markers: x at offset 16
            name = it[28:it.index(b'\x00', 28)]
            x = {b'$MKRNODE': -length, b'$MKRPINNUM': -(length // 2)}.get(name)
            it = bytearray(it)
            if x is not None:
                struct.pack_into('<i', it, 16, x)
            out.append(bytes(it))
        else:
            raise Stop(f'pin sembolünde beklenmeyen öğe {typ}')
    if inverted:
        assert [i[8] for i in out][:2] == [2, 0]
    return out


def circle_from(p0, pm, p1):
    (x1, y1), (x2, y2), (x3, y3) = p0, pm, p1
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    ux = ((x1 * x1 + y1 * y1) * (y2 - y3) + (x2 * x2 + y2 * y2) * (y3 - y1) + (x3 * x3 + y3 * y3) * (y1 - y2)) / d
    uy = ((x1 * x1 + y1 * y1) * (x3 - x2) + (x2 * x2 + y2 * y2) * (x1 - x3) + (x3 * x3 + y3 * y3) * (x2 - x1)) / d
    return ux, uy, math.hypot(x1 - ux, y1 - uy)


def arc_beziers(p0, pm, p1):
    """KiCad arc (start, mid, end; integer points) as cubic Bezier pieces of at most 90 degrees. End points exact,
    control points rounded to 10 nm (Proteus has no circular arc; its own arcs are Beziers too)."""
    cx, cy, r = circle_from(p0, pm, p1)
    a0, am, a1 = (math.atan2(p[1] - cy, p[0] - cx) for p in (p0, pm, p1))
    sweep0 = (am - a0) % (2 * math.pi)
    sweep1 = (a1 - a0) % (2 * math.pi)
    sweep = sweep1 if sweep0 <= sweep1 else sweep1 - 2 * math.pi      # direction that passes through mid
    n = max(1, math.ceil(abs(sweep) / (math.pi / 2) - 1e-9))
    pieces, start = [], p0
    for i in range(n):
        t0, t1 = a0 + sweep * i / n, a0 + sweep * (i + 1) / n
        end = p1 if i == n - 1 else (round(cx + r * math.cos(t1)), round(cy + r * math.sin(t1)))
        k = 4 / 3 * math.tan((t1 - t0) / 4) * r
        c1 = (round(start[0] - k * math.sin(t0)), round(start[1] + k * math.cos(t0)))
        c2 = (round(end[0] + k * math.sin(t1)), round(end[1] - k * math.cos(t1)))
        pieces.append((start, c1, c2, end))
        start = end
    return pieces


def shape_segments(sh):
    """A KiCad shape as a list of ('L', a, b) / ('C', a, c1, c2, b) segments in Proteus units (+y up)."""
    P = lambda p: (mm(p[0]), mm(p[1]))
    if sh['kind'] == 'polyline':
        pts = [P(p) for p in sh['pts']]
        return [('L', a, b) for a, b in zip(pts, pts[1:])]
    if sh['kind'] == 'rectangle':
        (x0, y0), (x1, y1) = P(sh['pts'][0]), P(sh['pts'][1])
        pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        return [('L', a, b) for a, b in zip(pts, pts[1:])]
    if sh['kind'] == 'arc':
        return [('C',) + bz for bz in arc_beziers(*(P(p) for p in sh['pts']))]
    if sh['kind'] == 'circle':                      # two half circles through the leftmost and rightmost points
        (cx, cy), r = P(sh['center']), mm(sh['radius'])
        a, b, c, d = (cx + r, cy), (cx, cy + r), (cx - r, cy), (cx, cy - r)
        return [('C',) + bz for bz in arc_beziers(a, b, c) + arc_beziers(c, d, a)]
    raise Stop(f'sembolde desteklenmeyen şekil {sh["kind"]}')


def has_area(sh):
    if sh['kind'] in ('arc', 'circle', 'rectangle'):
        return True
    pts = [(Decimal(x), Decimal(y)) for x, y in sh['pts']]
    a = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    return a != 0


def path_item(frame, segs, style=None):
    """Closed filled path from chained segments (Proteus's own gate bodies are such paths)."""
    pts, codes = [segs[0][1]], [(0, 1)]
    prev = None
    for s in segs:
        if s[0] == 'L':
            pts.append(s[2])
            codes.append((1, 3 if prev != 'L' else 1))
        else:
            pts += [s[2], s[3], s[4]]
            codes += [(2, 3), (2, 1), (2, 1)]
        prev = s[0]
    assert pts[0] == pts[-1]
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    data = struct.pack('<I4iH', len(pts), min(xs), min(ys), max(xs), max(ys), int(any(c[0] == 2 for c in codes)))
    data += b''.join(struct.pack('<ii', *p) for p in pts) + bytes(v for c in codes for v in c)
    return item(7, 0x60, data, style or frame.style)


def text_item(frame, x, y, just, height, text, rot=0):
    """Graphic text of a symbol (layout of the $MKRPINNUM / HEADER text items): x, y, rotation (0.1 degree),
    justification, height, width, font, 23 bytes, the NUL-terminated text. The type constant is 36 + the data size."""
    rest = (struct.pack('<iiIIII', x, y, rot, just, height, 203200) + b'Default Font\x00' + bytes(23)
            + text.encode('latin-1') + b'\x00')
    return item(4, 36 + len(rest), rest, frame.style)


def body_items(frame, shapes, where, style=None, fill_style=None):
    """KiCad unit drawing -> Proteus items. Area-filled shapes are chained into closed filled paths; unfilled (or
    zero-area) shapes become line and arc items, which Proteus does not fill."""
    if any(s['kind'] == 'text' for s in shapes):
        raise Stop(f'{where}: sembol içindeki metin henüz desteklenmiyor')
    filled = [shape_segments(s) for s in shapes if s['fill'] != 'none' and has_area(s)]
    open_ = [shape_segments(s) for s in shapes if not (s['fill'] != 'none' and has_area(s))]
    for s in shapes:
        if s['fill'] not in ('none', 'background', 'outline'):
            raise Stop(f'{where}: dolgu türü {s["fill"]} desteklenmiyor')
    items = []
    rev = lambda segs: [(('L', s[2], s[1]) if s[0] == 'L' else ('C', s[4], s[3], s[2], s[1])) for s in segs[::-1]]
    start = lambda segs: segs[0][1]
    end = lambda segs: segs[-1][2] if segs[-1][0] == 'L' else segs[-1][4]
    while filled:
        chain = filled.pop(0)
        while end(chain) != start(chain):
            for i, s in enumerate(filled):
                if start(s) == end(chain):
                    chain += filled.pop(i)
                    break
                if end(s) == end(chain):
                    chain += rev(filled.pop(i))
                    break
            else:
                raise Stop(f'{where}: dolgulu çizim kapalı bir şekil oluşturmuyor')
        items.append(path_item(frame, chain, fill_style or style))
    for segs in open_:
        for s in segs:
            if s[0] == 'L':
                items.append(item(0, 0x34, struct.pack('<4i', *s[1], *s[2]), style or frame.style))
            else:
                items.append(item(3, 0x44, struct.pack('<8i', *s[1], *s[2], *s[3], *s[4]), style or frame.style))
    return items


# KiCad electrical pin type -> Proteus pin record type (codes counted in Proteus's own libraries: 0 passive, 1 input,
# 2 output, 3 bidirectional, 4 tri-state, 5 pull-up / open collector, 6 pull-down / open emitter, 7 power)
PIN_KIND = {'input': 1, 'output': 2, 'bidirectional': 3, 'tri_state': 4, 'passive': 0, 'free': 0, 'unspecified': 0,
            'open_collector': 5, 'open_emitter': 6, 'power_in': 7, 'power_out': 7, 'no_connect': 0}
DIRS = {0: (1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, -1)}         # KiCad pin angle -> direction end -> body
ORIENT = {0: 0, 90: 900, 180: 0x10000, 270: 2700}                   # Proteus pin orientation (180 as Proteus: X mirror)


def turn(pt, deg):
    """Rotate a point counter-clockwise by a multiple of 90 degrees."""
    x, y = pt
    for _ in range((deg // 90) % 4):
        x, y = -y, x
    return x, y


def pin_texts(frame, p, end, base, flags, where, rot=0):
    """Pin number and name as symbol text items, where KiCad draws them (KiCad pin text rules: names inside the body
    at the name offset from the pin base, numbers centred on the pin 14 mil off the line; names outside the body when
    the offset is 0: name above, number below the line). KiCad applies the rules to the pin as it lies on the sheet
    and keeps texts readable; Proteus turns a symbol's texts with the part (T07). So the texts are worked out in the
    part's final orientation `rot` and turned back into symbol space."""
    out = []
    ang = (p['angle'] + rot) % 360
    end, base = turn(end, rot), turn(base, rot)
    text_item_ = globals()['text_item']

    def text_item(frame, x, y, just, height, text, trot=0):
        x, y = turn((x, y), -rot)
        return text_item_(frame, x, y, just, height, text, (trot - rot * 10) % 3600)

    dx, dy = DIRS[ang]
    vertical = ang in (90, 270)
    trot = 900 if vertical else 0              # text angle on the sheet
    mid2 = (end[0] + base[0], end[1] + base[1])
    if mid2[0] % 2 or mid2[1] % 2:
        raise Stop(f'{where}: pin ortası 10 nm katı değil')
    mid = (mid2[0] // 2, mid2[1] // 2)
    off = mm(flags['names_offset'])
    up = (0, 1) if not vertical else (-1, 0)     # "above the line" in the text's own frame
    show_name = bool(p['name']) and not flags['names_hidden']
    show_num = not flags['numbers_hidden']
    if show_num:
        sign = 1 if (off > 0 or not show_name) else -1
        just = 2 if sign > 0 else 2 + 4                      # centre-bottom above, centre-top below the line
        out.append(text_item(frame, mid[0] + up[0] * NUMBER_GAP * sign, mid[1] + up[1] * NUMBER_GAP * sign,
                             just, mm(p['number_size'][0]), p['number'], trot))
    if show_name:
        if off > 0:                                          # inside the body, reading away from the pin
            x, y = base[0] + dx * off, base[1] + dy * off
            h = 0 if ang in (0, 90) else 1
            out.append(text_item(frame, x, y, h + 8, mm(p['name_size'][0]), p['name'], trot))
        else:
            out.append(text_item(frame, mid[0] + up[0] * NUMBER_GAP, mid[1] + up[1] * NUMBER_GAP, 2,
                                 mm(p['name_size'][0]), p['name'], trot))
    return out


def marker(frame, name, x, y):
    it = bytearray(frame.term_markers[name])
    struct.pack_into('<ii', it, 16, x, y)
    return bytes(it)


def term_symbol(frame, k, lib_id):
    """A terminal symbol drawn like a KiCad power symbol (its shapes in the TERMINAL style); the node, where wires
    attach, is at the KiCad pin. Returns (name, items, node point in symbol space)."""
    lib = k['libs'][lib_id]
    pins = lib.get(0, {'pins': []})['pins'] + lib.get(1, {'pins': []})['pins']
    shapes = lib.get(0, {'shapes': []})['shapes'] + lib.get(1, {'shapes': []})['shapes']
    if len(pins) != 1:
        raise Stop(f'{lib_id}: güç sembolünde tek pin beklenir')
    node = (mm(pins[0]['at'][0]), mm(pins[0]['at'][1]))
    name = '$TERK_' + re.sub(r'[^A-Za-z0-9+\-_]', '_', lib_id.split(':')[1])
    items = body_items(frame, shapes, name, frame.term_style) + [
        marker(frame, b'$MKRNODE', *node), marker(frame, b'$MKRORIGIN', 0, 0), marker(frame, b'$MKRLABEL', *node)]
    return name, items, node


NC_HALF = 60960            # KiCad's no-connect cross: +-24 mil (measured in KiCad's SVG: 161.9504..163.1696 mm)


def nc_symbol(frame):
    """KiCad's no-connect cross as a terminal symbol, its node at the centre (Proteus's own $TERNC has its cross 0.1 in
    away from the node, on a stub)."""
    h = NC_HALF
    line = lambda a, b: item(0, 0x34, struct.pack('<4i', *a, *b), frame.term_style)
    return '$TERK_NC', [line((-h, -h), (h, h)), line((-h, h), (h, -h)), marker(frame, b'$MKRNODE', 0, 0),
                        marker(frame, b'$MKRORIGIN', 0, 0), marker(frame, b'$MKRLABEL', 0, 0)]


MDF_FILES = {}             # model files written next to the output (name -> text)


def lib_parts(rec):
    """(base items, [(state name, items)]) of a Proteus library device record."""
    n = rec[0]
    ts, L, symlen, npins, u1, u2, glen, nitems, one = struct.unpack_from('<IIIIHHIII', rec, 1 + n)
    base, _ = parse_items(rec, 1 + n + 32, nitems)
    q = 1 + n + 20 + glen
    for _ in range(npins & 0xffff):
        q += 16
        for _ in range(3):
            q = rec.index(b'\x00', q) + 1
    states, prop_at = [], 1 + n + 8 + symlen
    while q < prop_at - 4:
        e = rec.index(b'\x00', q)
        Ls, cnt, _ = struct.unpack_from('<III', rec, e + 1)
        states.append((rec[q:e].decode('latin-1'), parse_items(rec, e + 13, cnt)[0]))
        q = e + 1 + Ls
    return base, states


def shift_item(it, dx, dy):
    """Move a symbol item: boxes (x, y, 0, w, h), texts (x, y, ...), markers and sub-symbols (x, y at +16)."""
    it = bytearray(it)
    t = it[8]
    if t in (6, 8):
        q = 16
    else:
        q = 10 + struct.unpack_from('<I', it, 10)[0]
        if t not in (1, 4):
            raise Stop(f'kaydırılamayan öğe türü {t}')
    x, y = struct.unpack_from('<ii', it, q)
    struct.pack_into('<ii', it, q, x + dx, y + dy)
    return bytes(it)


def active_states(frame, kind, lib_rec, shapes, items, texts, where):
    """Drawing states for a Proteus active model, from the KiCad drawing (user decisions):
    'led'  : one state per brightness level of Proteus's LED; state 0 is the KiCad drawing as it is, the others fill
             the KiCad drawing's closed shapes with the colour of the same state of Proteus's LED (its path style);
    'ayar' : Proteus's setpoint control (the 'd (mm)' text, the indicator box, the increment / decrement markers),
             moved from Proteus's symbol to just below the KiCad body, centred; states as Proteus's (0: body + text,
             1: indicator). Returns (states, extra base items)."""
    base, lib_states = lib_parts(lib_rec)
    if kind == 'led':
        out = []
        closed = [sh for sh in shapes if sh['kind'] == 'polyline' and sh['pts'][0] == sh['pts'][-1]]
        if not closed:
            raise Stop(f'{where}: yanarken dolacak kapalı şekil yok')
        for name, st_items in lib_states:
            path = next(i for i in st_items if i[8] == 7)
            style = path[10:10 + struct.unpack_from('<I', path, 10)[0]]
            if name.endswith('_0'):
                out.append((name, list(items) + texts))
                continue
            lit = [dict(sh, fill='background') if sh in closed else sh for sh in shapes]
            out.append((name, body_items(frame, lit, where, None, style) + texts))
        return out, []
    if kind == 'ayar':
        widget = [i for i in base if (i[8] == 1 and b'INDICATOR' in i) or (i[8] == 8 and b'CREMENT' in i) or
                  (i[8] == 4 and i.rstrip(b'\x00').endswith(b'd (mm)'))]
        if len(widget) != 5:
            raise Stop(f'{where}: Proteus ayar kutusu öğeleri bulunamadı')
        # widget in Proteus's symbol: text at (0.2, -0.1) in, box 0.15..0.5 in wide; KiCad body: its drawing's bbox
        pts = [(mm(x), mm(y)) for sh in shapes for x, y in sh.get('pts', [])]
        cx = (min(p[0] for p in pts) + max(p[0] for p in pts)) // 2
        bottom = min(p[1] for p in pts)
        dx = cx - 825500                         # widget centre in Proteus's symbol: x 0.325 in
        dy = bottom - 152400 - (-254000)         # text line 0.06 in below the body (Proteus text at y -0.1 in)
        moved = [shift_item(i, dx, dy) for i in widget]
        text = [i for i in moved if i[8] == 4]
        boxes = [i for i in moved if i[8] == 1]
        return [(lib_states[0][0], list(items) + texts + text), (lib_states[1][0], boxes)], moved
    raise Stop(f'{where}: bilinmeyen durum türü {kind}')


def generic_props(ref_prefix):
    return f'{{*DEVICE}}\nPREFIX={ref_prefix}\n{{*COMPONENT}}\n\x00'.encode('latin-1')


def proteus_device(lib_id, unit, kpins):
    """A KiCad symbol drawn from a Proteus device (Proteus_Sim library: LOGICSTATE, LOGICPROBE, 7SEG...): the
    device is embedded exactly as in Proteus's library (Proteus does the same, V02), so its drawing, active states
    and model are Proteus's own. scan() has checked the drawing; here every KiCad pin must sit on its Proteus pin end."""
    import proteus_lib, proteus_kicad_lib
    name = PARTS_INFO[lib_id]['proteus_aygit']
    names = PARTS[lib_id][1].get(unit)
    rec = proteus_lib.find_device(name)[1]
    if rec is None:
        raise Stop(f'{lib_id}: Proteus kütüphanesinde {name} yok')
    if names is None or sorted(p['number'] for p in kpins) != sorted(names):
        raise Stop(f'{lib_id} birim {unit}: eşleme tablosundaki pinler semboldekilerle aynı değil')
    ppins = proteus_kicad_lib.pins(rec)
    by_name = {p['number']: p for p in kpins}
    order = []
    for pname, kind, x, y, angle, L in ppins:
        kp = next((by_name[n] for n, v in names.items() if v == pname), None)
        if kp is None or (mm(kp['at'][0]), mm(kp['at'][1])) != (x, y) or kp['angle'] != angle:
            raise Stop(f'{lib_id}: pin {pname} Proteus {name} aygıtındaki yerinde değil')
        order.append({'name': pname, 'number': kp['number'], 'end': (x, y)})
    props = rec[1 + rec[0] + 8 + struct.unpack_from('<I', rec, 1 + rec[0] + 8)[0]:]
    comp = re.search(rb'\{\*COMPONENT\}\r?\n(.*?)(?:\r?\n\s*\r?\n|\r?\n\*|\x00)', props + b'\x00', re.S)
    body = comp.group(1).decode('latin-1').replace('\r', '').rstrip('\n') if comp else ''
    return name, rec, order, body + '\n' if body else '', name


def mirrored(shapes, kpins, mirror):
    """A KiCad unit's shapes and pins mirrored in symbol space (+y up): KiCad 'mirror x' turns y into -y, 'mirror y'
    turns x into -x; pin angles follow (x: 90 <-> 270, y: 0 <-> 180)."""
    neg = lambda v: str(-Decimal(v))
    fx = (lambda p: (p[0], neg(p[1]))) if mirror == 'x' else (lambda p: (neg(p[0]), p[1]))
    out = []
    for sh in shapes:
        sh = dict(sh)
        for key in ('pts',):
            if key in sh:
                sh[key] = [fx(p) for p in sh[key]]
        for key in ('center', 'at'):
            if key in sh:
                sh[key] = fx(sh[key])
        out.append(sh)
    flip = {'x': {90: 270, 270: 90}, 'y': {0: 180, 180: 0}}[mirror]
    pins = [dict(p, at=fx(p['at']), angle=flip.get(p['angle'], p['angle'])) for p in kpins]
    return out, pins


def build_device(frame, k, lib_id, unit, pin_syms, ref, rot=0, mirror=None):
    """Device record for one KiCad symbol unit: KiCad's drawing and pins; simulation model and pin names from the
    mapping table when the part is in it, otherwise no model (pin names = KiCad pin numbers).
    Returns (name, record bytes, pins in record order, component property text, model or None)."""
    lib = k['libs'][lib_id]
    flags = lib['flags']
    shapes = lib.get(0, {'shapes': []})['shapes'] + lib.get(unit, {'shapes': []})['shapes']
    kpins = lib.get(0, {'pins': []})['pins'] + lib.get(unit, {'pins': []})['pins']
    if 'proteus_aygit' in PARTS_INFO.get(lib_id, {}):
        if mirror:
            raise Stop(f'{ref}: aynalanmış Proteus aygıtı henüz doğrulanmadı')
        return proteus_device(lib_id, unit, kpins)
    if mirror:
        shapes, kpins = mirrored(shapes, kpins, mirror)
    lib_rec = None
    if lib_id in PARTS:
        prim, units = PARTS[lib_id]
        info = PARTS_INFO[lib_id]
        names = units.get(unit)
        if names is None or sorted(p['number'] for p in kpins) != sorted(names):
            raise Stop(f'{lib_id} birim {unit}: eşleme tablosundaki pinler semboldekilerle aynı değil')
        if 'proteus' in info:
            props = frame.dev[prim]['props']
        elif 'kutuphane' in info:
            import proteus_lib
            path, rec = proteus_lib.find_device(info['kutuphane'])
            if rec is None:
                raise Stop(f'{lib_id}: Proteus kütüphanesinde {info["kutuphane"]} yok')
            n = rec[0]
            if struct.unpack_from('<I', rec, 1 + n + 12)[0] >> 16 and not info.get('durum'):
                raise Stop(f'{lib_id}: {info["kutuphane"]} etkileşimli/canlandırmalı bir Proteus parçası, eşlemede '
                           'durum çizimi tanımlı değil')
            lib_rec = rec
            symlen = struct.unpack_from('<I', rec, 1 + n + 8)[0]
            props = rec[1 + n + 8 + symlen:]
            if info.get('deger_birim') and info.get('deger'):
                # the value label keeps KiCad's text ("3.7V LiPo") and the number goes into the property text; with
                # the library's FLOAT definition the part's dialog rejects that label on OK ("Invalid expression"),
                # as a STRING it accepts it and keeps both (F2_deger_C1)
                props = re.sub(rb'\{' + info['deger'].encode() + rb'="([^"]*)",FLOAT[^}]*\}',
                               lambda m: b'{' + info['deger'].encode() + b'="' + m.group(1) + b'",STRING}', props)
            lib_pins = {pp[6] for pp in proteus_lib.pins(rec)}
            if set(names.values()) != lib_pins:
                raise Stop(f'{lib_id}: Proteus {info["kutuphane"]} pinleri {sorted(lib_pins)}, tabloda '
                           f'{sorted(names.values())}')
        else:
            m = info['mdf']
            props = ('{*DEVICE}\nPREFIX=U\n{*PROPDEFS}\n{MODFILE=LISA Model File,READONLY STRING}\n'
                     '{ITFMOD=Interface Model,HIDDEN STRING}\n{*COMPONENT}\n'
                     f'{{MODFILE={m["ad"]}.MDF}}\n{{ITFMOD={m["itfmod"]}}}\n\x00').encode('latin-1')
            MDF_FILES[m['ad'] + '.MDF'] = mdf_text(m, names)
    else:
        prim = None
        if len({p['number'] for p in kpins}) != len(kpins):
            raise Stop(f'{lib_id}: aynı numaralı iki pin')
        names = {p['number']: p['number'] for p in kpins}
        props = generic_props(re.match(r'[A-Za-z#]*', ref).group(0) or 'U')
    devname = f"{lib_id.split(':')[1]}_{'ABCDEFGH'[unit - 1]}" + (f'_M{mirror.upper()}' if mirror else '')
    items = body_items(frame, shapes, devname)
    texts = []
    pins, order = b'', []
    for p in kpins:
        if p['shape'] not in ('line', 'inverted'):
            raise Stop(f'{devname}: pin biçimi {p["shape"]} desteklenmiyor')
        if p['etype'] not in PIN_KIND:
            raise Stop(f'{devname}: pin türü {p["etype"]} desteklenmiyor')
        if p['angle'] not in DIRS:
            raise Stop(f'{devname}: pin açısı {p["angle"]}')
        L = mm(p['length'])
        ex, ey = mm(p['at'][0]), mm(p['at'][1])
        dx, dy = DIRS[p['angle']]
        bx, by = ex + dx * L, ey + dy * L
        # KiCad's pin decoration size: half the pin name text size (number size when the name size is 0)
        deco = mm(p['name_size'][0]) // 2 if Decimal(p['name_size'][0]) else mm(p['number_size'][0]) // 2
        inv = p['shape'] == 'inverted'
        sym = f"$PINK{'I' if inv else 'L'}{L}" + (f'_{deco}' if inv else '')
        pin_syms[(L, inv, deco if inv else 0)] = sym
        texts += pin_texts(frame, p, (ex, ey), (bx, by), flags, devname, rot)
        pins += (struct.pack('<BBHiiI', PIN_KIND[p['etype']], PIN_FLAGS, 0, bx, by, ORIENT[p['angle']])
                 + sym.encode() + b'\x00' + names[p['number']].encode('latin-1') + b'\x00'
                 + p['number'].encode('latin-1') + b'\x00')
        order.append({'name': names[p['number']], 'number': p['number'], 'end': (ex, ey)})
    states = []
    durum = PARTS_INFO.get(lib_id, {}).get('durum')
    if durum:                           # an active Proteus model: its drawing states, pin texts in each of them
        states, extra = active_states(frame, durum, lib_rec, shapes, items, texts, devname)
        items = items + extra
    items += texts + [frame.origin_item]
    rec = device_record(devname, LIB_TS, len(kpins), items, pins, props,
                        [(n_, it + [frame.origin_item]) for n_, it in states])
    # the component's default properties: the {*COMPONENT} section up to the next section (*PINOUT ...) or blank line
    comp = re.search(rb'\{\*COMPONENT\}\r?\n(.*?)(?:\r?\n\s*\r?\n|\r?\n\*|\x00)', props + b'\x00', re.S)
    body = comp.group(1).decode('latin-1').replace('\r', '').rstrip('\n') if comp else ''
    comp_props = body + '\n' if body else ''
    return devname, rec, order, comp_props, prim


# ---------------------------------------------------------------- schematic -> objects

ROT = {0: 0, 90: 900, 180: 1800, 270: 2700}      # KiCad symbol angle (counter-clockwise) -> Proteus rotation


def place(rot, mirror, dx, dy):
    """Symbol-space offset -> schematic offset, counter-clockwise rotation (Proteus and KiCad agree; +y up here)."""
    if mirror:
        raise Stop('aynalı sembol henüz doğrulanmadı')
    for _ in range(rot // 90):
        dx, dy = -dy, dx
    return dx, dy


def field_label(lab, prop, text, svg, PT, where):
    """A component label (reference, value) where KiCad draws the field: KiCad's SVG plot gives the displayed text,
    its anchor (start / middle / end), the bottom of its cell and whether it is turned 90 degrees (fields keep reading
    left-to-right or bottom-to-top whatever the symbol's rotation). The label is vertically centred on the cell."""
    h = Decimal(prop['size'][0])
    ax, ay = Decimal(prop['at'][0]), Decimal(prop['at'][1])
    cand = [t for t in svg if t['text'] == text]
    if not cand:
        raise Stop(f'{where}: "{text}" KiCad çiziminde bulunamadı')
    t = min(cand, key=lambda t: abs(Decimal(t['x']) - ax) + abs(Decimal(t['y']) - h / 2 - ay))
    hcode = {'start': 0, 'end': 1, 'middle': 2}[t['anchor']]
    x, y = Decimal(t['x']), Decimal(t['y']) - h / 2        # middle of the text line (also the rotation centre)
    if abs(x - ax) + abs(y - ay) > 20:
        raise Stop(f'{where}: KiCad çizimindeki konum alandan çok uzak')
    if t['rot'] not in (0, 90):
        raise Stop(f'{where}: yazı açısı {t["rot"]}')
    px, py = PT((str(x), str(y)))
    lab.update(text=text, x=px, y=py, u0=900 if t['rot'] == 90 else 0, just=hcode + 8)
    lab['height'] = mm(prop['size'][0])
    if lab['height'] != 254000:
        lab['attr'] = bytes([lab['attr'][0], lab['attr'][1], lab['attr'][2] & ~2, lab['attr'][3]])
    if prop['hide']:
        raise Stop(f'{where}: gizli alan henüz desteklenmiyor')


def label_dict(frame, text, x, y, just, size, rot=0):
    lab = copy.deepcopy(frame.label_tpl)
    lab.update(text=text, x=x, y=y, u0=rot, just=just)
    h = mm(size[0])
    lab['height'] = h
    if h != 254000:
        lab['attr'] = bytes([lab['attr'][0], lab['attr'][1], lab['attr'][2] & ~2, lab['attr'][3]])
    return lab


JUST_H = {'left': 0, 'right': 1}
JUST_V = {'bottom': 0, 'top': 4}


def just_code(just, default_h, default_v):
    h = next((JUST_H[j] for j in just if j in JUST_H), default_h)
    v = next((JUST_V[j] for j in just if j in JUST_V), default_v)
    if 'mirror' in just:
        raise Stop('aynalı yazı desteklenmiyor')
    return h + v


def convert(k, frame, det):
    """det collects the details for the report (lists keyed by topic)."""
    W, H = k['size']
    X = lambda s: mm(s) - W * U // 2
    Y = lambda s: H * U // 2 - mm(s)
    PT = lambda p: (X(p[0]), Y(p[1]))
    KM = lambda p: f'({(p[0] + W * U // 2) / U:g}, {(H * U // 2 - p[1]) / U:g}) mm'   # back to KiCad mm

    # devices and components
    pin_syms, devices, comps = {}, {}, []
    natural = lambda r: [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', r)]
    syms = sorted(k['syms'], key=lambda s: natural(s['ref']))
    if len(syms) > 250:
        raise Stop('250 üzeri bileşen için kimlik alanı doğrulanmadı')
    terms, term_syms = [], {}
    for s in [s for s in syms if k['libs'][s['lib_id']]['flags']['power']]:
        if s['lib_id'].endswith(':PWR_FLAG'):
            det['pwr_flag'].append(s)                 # a KiCad ERC marker: not transferred (user decision)
            continue
        if s['angle'] not in (0, 90, 180, 270) or s['mirror']:
            raise Stop(f'{s["ref"]}: güç sembolünün yönü desteklenmiyor')
        ox, oy = PT(s['at'])
        net = s['props']['Value']['text']
        if net.upper() == 'GND':
            # user decision (2026-11-01): Proteus's own ground terminal instead of KiCad's drawing, its node on the
            # KiCad pin. As Proteus writes it (K03, K04): node 0.1 in above the origin, empty label at (-0.1, -0.2),
            # both turned with the terminal; label vertical at 90/270, justification 9 at 180/270
            node = term_symbol(frame, k, s['lib_id'])[2]
            dx, dy = place(s['angle'], None, *node)
            nx, ny = place(s['angle'], None, 0, 254000)
            lx, ly = place(s['angle'], None, -254000, -508000)
            t = copy.deepcopy(frame.gnd_tpl)
            t.update(x=ox + dx - nx, y=oy + dy - ny, rot=ROT[s['angle']], k=13, b1=0, ptr=None, ref=s['ref'], net=net,
                     pins=[None], pin_info=[{'name': net, 'number': '1', 'pt': (ox + dx, oy + dy)}])
            t['labels'][0].update(x=t['x'] + lx, y=t['y'] + ly, u0=900 if s['angle'] in (90, 270) else 0,
                                  just=8 if s['angle'] in (0, 90) else 9)
            terms.append(t)
            continue
        if s['lib_id'] not in term_syms:
            term_syms[s['lib_id']] = term_symbol(frame, k, s['lib_id'])
        name, items, node = term_syms[s['lib_id']]
        t = copy.deepcopy(frame.term_tpl)
        dx, dy = place(s['angle'], None, *node)
        # any other KiCad power symbol is a global net name and becomes a generic terminal drawn like KiCad's symbol:
        # Proteus reads a power terminal "+<name>" as a supply voltage ("Value expected for property 'VALUE' in
        # 'PR0000_+BATT'"), F2_sim_A/B
        t.update(x=ox, y=oy, rot=ROT[s['angle']], sym=name, k=9, b1=0, ptr=None, ref=s['ref'], net=net,
                 pins=[None], pin_info=[{'name': net, 'number': '1', 'pt': (ox + dx, oy + dy)}])
        field_label(t['labels'][0], s['props']['Value'], net, k['svg_texts'], PT, f'{s["ref"]} Value')
        terms.append(t)
    # global labels: a terminal drawing KiCad's frame of the label (taken from KiCad's SVG plot, whose first corner is
    # the label's connection point), its node there and its label the net name where KiCad writes it; same-named
    # terminals are one net in Proteus, as global labels are in KiCad. The terminal type follows the label shape.
    gkind = {'input': 10, 'output': 11, 'bidirectional': 12, 'tri_state': 12, 'passive': 9}
    gsyms = {}
    outlines = k.get('svg_outlines', [])
    for g in k['globals']:
        where = f'global etiket "{g["text"]}"'
        if g['refs_shown']:
            raise Stop(f'{where}: sayfa referansı (Intersheetrefs) gösterimi desteklenmiyor')
        ax, ay = Decimal(g['at'][0]), Decimal(g['at'][1])
        pts = next((o for o in outlines if (Decimal(o[0][0]), Decimal(o[0][1])) == (ax, ay)), None)
        if pts is None:
            raise Stop(f'{where}: KiCad çiziminde çerçevesi bulunamadı')
        rel = [(str(Decimal(x) - ax), str(ay - Decimal(y))) for x, y in pts]
        items = body_items(frame, [{'kind': 'polyline', 'fill': 'none', 'pts': rel + [rel[0]]}], where,
                           frame.term_style) + [marker(frame, b'$MKRNODE', 0, 0), marker(frame, b'$MKRORIGIN', 0, 0),
                                                marker(frame, b'$MKRLABEL', 0, 0)]
        name = gsyms.setdefault(b''.join(items), f'$TERKG{len(gsyms) + 1}')
        term_syms[('global', name)] = (name, items, (0, 0))
        t = copy.deepcopy(frame.term_tpl)
        x, y = PT(g['at'])
        t.update(x=x, y=y, rot=0, sym=name, k=gkind[g['shape']], b1=0, ptr=None, ref='global', net=g['text'],
                 pins=[None], pin_info=[{'name': g['text'], 'number': '1', 'pt': (x, y)}], glob=g['shape'])
        field_label(t['labels'][0], {'size': g['size'], 'at': g['at'], 'hide': False}, g['text'], k['svg_texts'],
                    PT, where)
        terms.append(t)
    syms = [s for s in syms if not k['libs'][s['lib_id']]['flags']['power']]
    ncs = []
    if k['no_connects']:
        term_syms['NC'] = nc_symbol(frame) + ((0, 0),)
        for a in k['no_connects']:
            t = copy.deepcopy(frame.term_tpl)
            x, y = PT(a)
            t['labels'][0].update(text='', x=x, y=y)
            t.update(x=x, y=y, rot=0, sym='$TERK_NC', k=17, b1=0, ptr=None, ref='NC', net=None, pins=[],
                     pin_info=[])
            ncs.append(t)
    det['terms'], det['ncs'], det['term_syms'] = terms, ncs, term_syms
    for i, s in enumerate(syms, 1):
        lib = k['libs'][s['lib_id']]
        if s['angle'] not in (0, 90, 180, 270):
            raise Stop(f'{s["ref"]}: açı {s["angle"]}')
        # KiCad turns a symbol, then mirrors it (AYNA test: 24/24 pins); Proteus mirrors path items wrongly (A01: pins
        # mirrored, body not), so a mirror is drawn into the device (<name>_MX / _MY) and the part is only turned:
        # mirror . turn(a) = turn(-a) . mirror
        mir = s['mirror']
        ang = (-s['angle']) % 360 if mir else s['angle']
        key = (s['lib_id'], s['unit'], mir, ang)
        if key not in devices:
            d = build_device(frame, k, s['lib_id'], s['unit'], pin_syms, s['ref'], ang, mir)
            body = lambda rec: rec[1 + rec[0]:]
            same = next((v for kk, v in devices.items() if kk[:3] == key[:3] and body(v[1]) == body(d[1])), None)
            if same is None and any(kk[:3] == key[:3] for kk in devices):
                name = f'{d[0]}_R{ang}'       # pin texts turned for this orientation
                d = (name, pstr(name) + body(d[1])) + d[2:]
            devices[key] = same or d
        devname, _, order, comp_props, prim = devices[key]
        if prim is None:
            det['nomodel'].append(s['ref'])
        c = copy.deepcopy(frame.comp_tpl)
        ox, oy = PT(s['at'])
        shown_ref = s['ref'] + ('ABCDEFGH'[s['unit'] - 1] if lib['flags']['nunits'] > 1 else '')
        pdev = 'proteus_aygit' in PARTS_INFO.get(s['lib_id'], {})
        # a Proteus debugging part (LOGICSTATE, probes, 7SEG) is written as Proteus places one (V02): tag 8, byte 7 of
        # the reserved field 0x10, no reference and no value, and it is left out of the CDB
        c.update(device=devname, x=ox, y=oy, rot=ROT[ang], id=i, npins=len(order),
                 res=bytes(7) + b'\x10' + bytes(4) if pdev else bytes(12),
                 tag=8 if pdev or PARTS_INFO.get(s['lib_id'], {}).get('durum') else 0)   # active parts: tag 8
        c['ref'] = s['ref']
        c['cdb'] = not pdev
        c['pin_info'] = []
        for p in order:
            dx, dy = place(ang, None, *p['end'])
            c['pin_info'].append({'name': p['name'], 'number': p['number'], 'pt': (ox + dx, oy + dy)})
        c['pins'] = [None] * len(order)
        labs = c['labels']
        if pdev:
            labs[0].update(text='', x=ox, y=oy)
            labs[1].update(text='', x=ox, y=oy)
            det.setdefault('proteus_parts', []).append((s['ref'], devname))
        else:
            for lab, prop, text in ((labs[0], 'Reference', shown_ref),
                                    (labs[1], 'Value', s['props']['Value']['text'])):
                field_label(lab, s['props'][prop], text, k['svg_texts'], PT, f'{s["ref"]} {prop}')
        labs[2].update(text=devname, x=ox, y=oy)
        props_i = re.sub(r'\{PACKAGE=[^}]*\}\n', '', comp_props)     # KiCad footprint is not transferred
        info = PARTS_INFO.get(s['lib_id'], {})
        if info.get('deger'):
            # Proteus takes a part's value from its value label (its own RES/CAP/CELL keep no {VALUE=..} in the property
            # text). A label Proteus cannot read as a number ("3.7V LiPo": "Operator expected parsing property VALUE")
            # keeps KiCad's text and the number goes into the property text, which the simulator uses instead
            # (F2_sim_B: label "3.7V LiPo", {VALUE=3.7V}, Proteus netlist BT1 = 3.7V, re-save keeps both)
            v = s['props']['Value']['text']
            props_i = re.sub(r'\{' + info['deger'] + r'=[^}]*\}\n', '', props_i)
            how = f'{info["deger"]} = değer etiketi "{v}"'
            if info.get('deger_birim'):
                mv = re.search(r'(\d+(?:[.,]\d+)?)\s*([pnuµmkM]?)' + info['deger_birim'], v)
                if not mv:
                    raise Stop(f'{s["ref"]}: değer "{v}" içinde {info["deger_birim"]} cinsinden sayı yok')
                sim_v = mv.group(1).replace(',', '.') + mv.group(2).replace('µ', 'u') + info['deger_birim']
                if sim_v != v:
                    props_i += '{' + info['deger'] + '=' + sim_v + '}\n'
                    how = (f'{info["deger"]} = {sim_v} (özellik metninde, Proteus\'ta "Other Properties"; değer '
                           f'etiketi "{v}" olduğu gibi. Simülasyon değerini değiştirmek için Other Properties\'teki '
                           f'{{{info["deger"]}=..}} düzenlenir)')
            det['values'].append((s['ref'], v, how))
        if len(props_i) > PROPS_IN_LABEL_MAX:
            # a property text longer than 50 bytes goes after the labels, the PROPERTIES label empty; up to 50 it is
            # the label's text (Proteus 8.17 writes CELL with 50 bytes in the label, with 51 after it, F2_cell_c/d;
            # its loader reads it the same way: a 29-byte CELL text written after the labels does not load)
            labs[3].update(text='', x=ox, y=oy)
            c['props_inline'] = props_i.encode('latin-1')
        else:
            labs[3].update(text=props_i, x=ox, y=oy)
            c['props_inline'] = None
        c['props_text'] = props_i
        c['proplen'] = len(props_i)
        comps.append(c)

    # wire graph
    segs = []
    for a, b in k['wires']:
        pa, pb = PT(a), PT(b)
        if pa == pb:
            raise Stop(f'sıfır uzunluklu KiCad teli {a}')
        segs.append((pa, pb))
    deg = defaultdict(list)
    for i, (a, b) in enumerate(segs):
        deg[a].append(i)
        deg[b].append(i)
    pins_at = defaultdict(list)
    for c in comps + terms:
        for pi, p in enumerate(c['pin_info']):
            pins_at[p['pt']].append((c, pi))
    junc = {PT(j) for j in k['junctions']}

    def interior(p):
        out = []
        for i, (a, b) in enumerate(segs):
            if p in (a, b):
                continue
            if (b[0] - a[0]) * (p[1] - a[1]) == (b[1] - a[1]) * (p[0] - a[0]) and \
                    min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]):
                out.append(i)
        return out

    # KiCad connects wires and pins only where end points coincide (or at a junction); a pin or wire end lying on the
    # middle of another wire is drawn over it but not connected (BDT_Odev: U53 pin 7, KiCad netlist confirms). Proteus
    # behaves the same (connections are explicit references), so these are kept and listed; a junction in the middle of
    # a wire would connect in KiCad and is not supported.
    overlaps = []
    for p in sorted(set(deg) | set(pins_at) | junc):
        if interior(p):
            if p in junc:
                raise Stop(f'junction bir telin ortasında: {KM(p)}')
            overlaps.append(p)
    for p in overlaps:
        what = ', '.join(f'{c["ref"]} pin {c["pin_info"][pi]["number"]}' for c, pi in pins_at.get(p, [])) or 'tel ucu'
        det['overlap'].append(f'{what} ucunun üstünden başka bir ağın teli geçiyor; bağlantı yok (KiCad\'de de yok), '
                      f'aynen taşındı (KiCad {(p[0] + W * U // 2) / U} mm, {(H * U // 2 - p[1]) / U} mm)')
    for p, lst in pins_at.items():
        if len(lst) > 1:
            raise Stop(f'iki pin aynı noktada: {KM(p)}')
    for j in junc:
        if j not in deg:
            raise Stop(f'tel ucunda olmayan junction: {KM(j)}')

    # chains: maximal runs through points of degree 2 with no pin and no junction
    def through(p):
        return len(deg[p]) == 2 and p not in pins_at and p not in junc
    used, chains = set(), []
    for i in range(len(segs)):
        if i in used:
            continue
        # walk back to a chain end
        a, b = segs[i]
        path = [a, b]
        used.add(i)
        cur, seg = b, i
        while through(cur):
            nxt = [s for s in deg[cur] if s != seg][0]
            if nxt in used:
                raise Stop('kapalı tel döngüsü')
            used.add(nxt)
            na, nb = segs[nxt]
            cur = nb if na == cur else na
            path.append(cur)
            seg = nxt
        cur, seg = a, i
        while through(cur):
            nxt = [s for s in deg[cur] if s != seg][0]
            if nxt in used:
                raise Stop('kapalı tel döngüsü')
            used.add(nxt)
            na, nb = segs[nxt]
            cur = nb if na == cur else na
            path.insert(0, cur)
            seg = nxt
        chains.append(synth.wire(path))

    ends = defaultdict(list)            # point -> chains ending there
    for w in chains:
        ends[w['pts'][0]].append(w)
        ends[w['pts'][-1]].append(w)
        if w['pts'][0] == w['pts'][-1]:
            raise Stop('kendi üstüne kapanan tel')
    dots, extra = [], []
    counts = defaultdict(int)
    for p in sorted(set(ends) | set(pins_at)):
        ws = ends.get(p, [])
        pin = pins_at.get(p)
        if pin:
            c, pi = pin[0]
            if not ws:
                continue
            if len(ws) == 1 and p not in junc:
                c['pins'][pi] = ws[0]
                counts['pin'] += 1
                continue
            if p not in junc:
                raise Stop(f'{c["ref"]} pininde junction olmadan {len(ws)} tel birleşiyor: {KM(p)}')
            z = synth.wire([p, p])           # approved: pin -> zero-length wire -> dot
            c['pins'][pi] = z
            extra.append(z)
            ws = ws + [z]
            counts['pin_junction'] += 1
            det['pin_junction'].append((c['ref'], c['pin_info'][pi]['number'], p))
        elif len(ws) == 1 and p not in junc:
            counts['free_end'] += 1           # approved: 1-wire dot on a free wire end
            det['free_end'].append((p, ws[0]))
        elif p not in junc:
            raise Stop(f'junction olmadan {len(ws)} tel ucu birleşiyor: {KM(p)}')
        else:
            counts['junction'] += 1
        if len(ws) > 4:
            raise Stop(f'bir noktada 4ten fazla tel: {KM(p)}')
        dots.append(synth.dot(p[0], p[1], ws))
    wires = chains + extra
    for t in terms:
        t['ptr'] = t['pins'][0]
    for t in ncs:
        p = (t['x'], t['y'])
        if p not in pins_at:
            raise Stop(f'"bağlantı yok" işareti bir pinin ucunda değil: {KM(p)}')
        if ends.get(p):
            raise Stop(f'"bağlantı yok" işaretli pine tel bağlı: {KM(p)}')

    # labels: each on a wire
    for l in k['labels']:
        p = PT(l['at'])
        host = None
        for w in chains:
            for a, b in zip(w['pts'], w['pts'][1:]):
                if (b[0] - a[0]) * (p[1] - a[1]) == (b[1] - a[1]) * (p[0] - a[0]) and \
                        min(a[0], b[0]) <= p[0] <= max(a[0], b[0]) and min(a[1], b[1]) <= p[1] <= max(a[1], b[1]):
                    host = w
                    break
            if host:
                break
        if host is None:
            raise Stop(f'etiket bir telin üstünde değil: {l["text"]}')
        if l['angle'] not in (0, 90, 180, 270):
            raise Stop(f'etiket açısı {l["angle"]} desteklenmiyor: {l["text"]}')
        # vertical KiCad labels (90: left bottom, 270: right bottom, both reading bottom to top) are Proteus labels
        # turned 900 with the same justification code (L04, T11)
        host['wlabels'].append(label_dict(frame, l['text'], p[0], p[1], just_code(l['just'], 0, 0), l['size'],
                                          900 if l['angle'] in (90, 270) else 0))

    # texts. Proteus's 2D text is single-line (its dialog has one line; a written '\n' is drawn left-aligned and blank
    # lines are dropped, M02/S08), so a multi-line KiCad text becomes one centred Proteus text per non-blank line, at
    # the place KiCad draws that line (taken from KiCad's own SVG plot: y there is the bottom of the text cell).
    texts = []
    for t in k['texts']:
        if t['angle'] != 0:
            raise Stop('döndürülmüş metin henüz doğrulanmadı')
        hcode = just_code(t['just'], 2, 8) % 4
        anchor = {0: 'start', 1: 'end', 2: 'middle'}[hcode]
        lines = [l for l in t['text'].split('\n') if l.strip()]
        cand = sorted((Decimal(s['y']), s) for s in k['svg_texts'] if s['text'] in lines and s['anchor'] == anchor
                      and abs(Decimal(s['x']) - Decimal(t['at'][0])) < Decimal('0.001'))
        placed = []
        for line in lines:
            hit = next((c for c in cand if c[1]['text'] == line and c not in placed), None)
            if hit is None:
                raise Stop(f'metin satırı KiCad SVG çiziminde bulunamadı: {line!r}')
            placed.append(hit)
        if [c[0] for c in placed] != sorted(c[0] for c in placed):
            raise Stop('metin satırlarının sırası KiCad çizimiyle uyuşmuyor')
        h = Decimal(t['size'][0])
        for (ys, s), line in zip(placed, lines):
            o = copy.deepcopy(frame.text_tpl)
            x, y = PT((t['at'][0], str(ys - h / 2)))
            body = bytearray(o['body'][:o['body'].rindex(b'\x00', 0, len(o['body']) - 1) + 1])
            struct.pack_into('<I', body, 52, hcode + 8)            # horizontal as KiCad, vertically centred
            struct.pack_into('<I', body, 56, mm(t['size'][0]))
            body += line.encode('latin-1') + b'\x00'
            struct.pack_into('<I', body, 0, len(body) - 4)
            o.update(x=x, y=y, rot=0, body=bytes(body), text=line)
            texts.append(o)
        det['texts'].append((t, lines))
    det['counts'] = dict(counts, segs=len(segs), chains=len(chains))
    det['to_mm'] = lambda p: ((p[0] + W * U // 2) / U, (H * U // 2 - p[1]) / U)
    return comps, dots, wires, texts, devices, pin_syms


def order_objects(comps, dots, wires, texts):
    """Proteus's write order: each referrer is followed by the wires whose referrers are now all written (R3); a
    wire's first point is at its first referrer (R1); a dot's pointers ascend (R4)."""
    refs = defaultdict(set)
    for c in comps:
        for w in c['pins']:
            if w is not None:
                refs[id(w)].add(id(c))
    for d in dots:
        for w in d['ptrs']:
            if w is not None:
                refs[id(w)].add(id(d))
    out, done, written = [], set(), set()

    def put(o, ws):
        out.append(o)
        done.add(id(o))
        for w in ws:
            if w is not None and id(w) not in written and refs[id(w)] <= done:
                out.append(w)
                written.add(id(w))

    for c in comps:
        put(c, c['pins'])
    pending = list(dots)
    while pending:
        d = next((d for d in pending if any(w is not None and refs[id(w)] - done == {id(d)} for w in d['ptrs'])),
                 pending[0])
        pending.remove(d)
        put(d, d['ptrs'])
    if len(written) != len(wires):
        raise Stop('referanssız tel')
    first_pt = {}
    for o in out:
        if o['kind'] in ('comp', 'term'):
            for w, p in zip(o['pins'], o['pin_info']):
                if w is not None:
                    first_pt.setdefault(id(w), p['pt'])
        elif o['kind'] == 'dot':
            for w in o['ptrs']:
                if w is not None:
                    first_pt.setdefault(id(w), (o['x'], o['y']))
    for w in wires:
        if w['pts'][0] != first_pt[id(w)]:
            assert w['pts'][-1] == first_pt[id(w)]
            w['pts'] = w['pts'][::-1]
    pos = {id(o): i for i, o in enumerate(out)}
    for d in dots:
        ws = sorted((w for w in d['ptrs'] if w is not None), key=lambda w: pos[id(w)])
        d['ptrs'] = ws + [None] * (4 - len(ws))
    return out + texts


# ---------------------------------------------------------------- CDB, project, writing

def cdb(comps, base_cdb):
    # every component is in the pin table; a Proteus debugging part (LOGICSTATE, probes, 7SEG) has no reference, ends
    # with (0, 0, -1) instead of (0, id, 0) and has no entry in the property table (Proteus's re-save of T09)
    head_end = base_cdb.index(b'\x0cMaster Sheet') + 13 + 8
    out = base_cdb[:head_end] + struct.pack('<I', len(comps))
    for c in comps:
        i = c['id']
        out += struct.pack('<IIII', i, 1, 0, i) + pstr(c['labels'][0]['text']) + struct.pack('<I', len(c['pin_info']))
        for p in c['pin_info']:
            out += pstr(p['name']) + b'\x00'
        out += struct.pack('<III', 0, i, 0) if c.get('cdb', True) else struct.pack('<IIi', 0, 0, -1)
    comps = [c for c in comps if c.get('cdb', True)]
    out += bytes.fromhex('01000000' '01000000' '0000' '01000000') + struct.pack('<I', len(comps))
    for c in comps:
        props = c.get('props_text', c['labels'][3]['text'])
        out += struct.pack('<II', c['id'], 1) + bytes(12) + pstr(c['labels'][0]['text']) + pstr(c['labels'][1]['text'])
        out += pstr(c['device']) + b'\x00' + struct.pack('<I', len(props) + 5) + props.encode('latin-1') + b'\x00'
    return out + bytes(4)


def project_xml(xml, title):
    s = xml.decode('utf-8')
    vals = [(k, title.get(t)) for k, t in (('TITLE', 'title'), ('REV', 'rev')) if title.get(t)]
    if any(ch in v for _, v in vals for ch in '<>&"'):
        raise Stop('başlıkta XML özel karakteri')
    attrs = ''.join(f' {k}="{v}"' for k, v in vals)
    assert s.count('<PROJECT ENCODING_MIB="2252" ENCODING="Windows-1252">') == 1
    s = s.replace('<PROJECT ENCODING_MIB="2252" ENCODING="Windows-1252">',
                  f'<PROJECT ENCODING_MIB="2252"{attrs} ENCODING="Windows-1252">')
    return s.encode('utf-8')


def name_key(name):
    """Proteus's order of the embedded symbol and device lists: byte order, except that runs of digits met at the same
    place compare as numbers ('7SEG-COM-CATHODE' before '74AUC2G08_A', 'CNY70_A' before 'C_A'; T09, V02)."""
    # a digit run sorts as its first byte would ('0'), so '$' and ' ' come before it and letters after it
    return [(0x30, int(t), t) if t.isdigit() else (t[0], t) for t in re.split(rb'(\d+)', name) if t]


def assemble(frame, k, objs, comps, devices, pin_syms, path, term_syms=None):
    f = frame.f
    d = f.dsn
    # symbol list: system symbols + our pin symbols, in name order
    syms = dict(frame.syms)
    for (L, inv, deco), name in pin_syms.items():
        syms[name.encode()] = symbol_record(name, SYM_TS, pin_symbol(frame, L, inv, deco))
    for name, items, *_ in (term_syms or {}).values():
        syms[name.encode()] = symbol_record(name, SYM_TS, items)
    sym_bytes = b''.join(r for _, r in sorted(syms.items(), key=lambda kv: name_key(kv[0])))
    # device list: Proteus's instruments etc., minus the base file's gates, plus ours
    devs = {n: r for n, r in frame.devs if n not in (b'AND', b'NAND', b'NOR', b'NOT', b'OR')}
    for name, rec, *_ in {v[0]: v for v in devices.values()}.values():
        devs[name.encode()] = rec
    dev_bytes = b''.join(r for _, r in sorted(devs.items(), key=lambda kv: name_key(kv[0])))
    new_prefix = d[:frame.sym_start] + sym_bytes + d[frame.sym_end:frame.dev_start] + dev_bytes + d[frame.dev_end:f.start]
    delta = len(new_prefix) - f.start
    # shift the trailer's circuit offsets, the root list position and every pointer kept by synth
    t = bytearray(d[f.trailer_at:])
    r = 6
    for _ in range(struct.unpack_from('<H', t, 0)[0]):
        r += 1 + t[r] + 2
        struct.pack_into('<I', t, r, struct.unpack_from('<I', t, r)[0] + delta)
        r += 8
    f.dsn = new_prefix + d[f.start:f.trailer_at] + bytes(t)
    f.start += delta
    f.end += delta
    f.end_ptr_at += delta
    f.trailer_at += delta
    dd = bytearray(f.dsn)
    W, H = k['size']
    for a in (0x188, dd.index(b'OBJECT DATA\x00', f.start - 64) - 10):
        assert struct.unpack_from('<ii', dd, a) == (38100000, 25400000), a     # the base's 15 x 10 in sheet
        struct.pack_into('<ii', dd, a, W * U, H * U)
    title = k['title'].get('title', '').encode('latin-1')
    if len(title) > 15:
        raise Stop('15 karakterden uzun başlık için DSN alanı doğrulanmadı')
    dd[0x15:0x15 + len(title) + 1] = title + b'\x00'
    now = int(time.time())
    struct.pack_into('<I', dd, 0xb1, now)                  # save time (Proteus rewrites it on every save)
    dates = [m.start() for m in re.finditer(rb'\d\d/\d\d/\d\d\x00', bytes(dd[:0x100]))]
    assert len(dates) == 2, dates                          # created, modified (mm/dd/yy); Proteus sets the second
    dd[dates[1]:dates[1] + 8] = time.strftime('%m/%d/%y', time.localtime(now)).encode()
    f.dsn = bytes(dd)
    ents = dict(f.entries)
    ents['ROOT.CDB'] = cdb(comps, ents['ROOT.CDB'])
    xml = project_xml(ents['PROJECT.XML'], k['title'])
    ents['PROJECT.XML'] = re.sub(rb'(MODIFIED|CREATED)="\d+"', lambda m: m.group(1) + b'="%d"' % now, xml)
    f.entries = [(n, ents[n]) for n, _ in f.entries]
    return synth.save(f, path, objs)


# ---------------------------------------------------------------- validation

def proteus_nets(objs):
    """Nets of the written object list: pins, wires, dots and same-named wire labels."""
    parent = {}

    def find_(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        parent[find_(a)] = find_(b)
    for o in objs:
        if o['kind'] == 'comp':
            for w, p in zip(o['pins'], o['pin_info']):
                node = (o['ref'], p['number'])
                find_(node)
                if w is not None:
                    union(node, ('w', id(w)))
        elif o['kind'] == 'dot':
            ws = [w for w in o['ptrs'] if w is not None]
            for w in ws[1:]:
                union(('w', id(ws[0])), ('w', id(w)))
        elif o['kind'] == 'wire':
            for l in o['wlabels']:
                union(('w', id(o)), ('l', l['text']))
        elif o['kind'] == 'term' and o.get('ptr') is not None:
            union(('w', id(o['ptr'])), ('l', o['net']))
    nets = defaultdict(set)
    for a in list(parent):
        if a[0] not in ('w', 'l'):
            nets[find_(a)].add(a)
    return {frozenset(v) for v in nets.values()}


def geometry_check(objs):
    """Every wire end sits on the pin end, terminal node or dot that references it."""
    bad = 0
    for o in objs:
        if o['kind'] in ('comp', 'term'):
            for w, p in zip(o['pins'], o['pin_info']):
                if w is not None and p['pt'] not in (w['pts'][0], w['pts'][-1]):
                    bad += 1
        elif o['kind'] == 'dot':
            for w in o['ptrs']:
                if w is not None and (o['x'], o['y']) not in (w['pts'][0], w['pts'][-1]):
                    bad += 1
    return bad


def readback_check(path, objs):
    """Parse the written file again and compare every object with the model."""
    g = synth.load(path).objs
    if len(g) != len(objs):
        raise Stop('geri okumada nesne sayısı farklı')
    pos = {id(o): i for i, o in enumerate(objs)}
    gpos = {id(o): i for i, o in enumerate(g)}
    for a, b in zip(objs, g):
        same = a['kind'] == b['kind']
        if same and a['kind'] == 'comp':
            same = (a['device'], a['x'], a['y'], a['rot'], [l['text'] for l in a['labels']]) == \
                   (b['device'], b['x'], b['y'], b['rot'], [l['text'] for l in b['labels']]) and \
                   [pos.get(id(w)) for w in a['pins']] == [gpos.get(id(w)) for w in b['pins']]
        elif same and a['kind'] == 'wire':
            same = a['pts'] == b['pts'] and [(l['text'], l['x'], l['y'], l['just']) for l in a['wlabels']] == \
                   [(l['text'], l['x'], l['y'], l['just']) for l in b['wlabels']]
        elif same and a['kind'] == 'dot':
            same = (a['x'], a['y']) == (b['x'], b['y']) and \
                   [pos.get(id(w)) for w in a['ptrs']] == [gpos.get(id(w)) for w in b['ptrs']]
        elif same and a['kind'] == 'text2d':
            same = (a['x'], a['y'], a['body']) == (b['x'], b['y'], b['body'])
        elif same and a['kind'] == 'term':
            same = (a['x'], a['y'], a['rot'], a['sym'], a['k'], [l['text'] for l in a['labels']]) == \
                   (b['x'], b['y'], b['rot'], b['sym'], b['k'], [l['text'] for l in b['labels']]) and \
                   pos.get(id(a['ptr'])) == gpos.get(id(b['ptr']))
        if not same:
            raise Stop(f'geri okumada farklı nesne: {a["kind"]}')


SNAP = {50: (1, 127000), 100: (2, 254000)}      # KiCad connection grid (mil) -> Proteus snapidx, snap (W01, G01)


def workspace_snap(sch):
    pro = _os.path.splitext(sch)[0] + '.kicad_pro'
    g = json.load(open(pro, encoding='utf-8'))['schematic'].get('connection_grid_size', 50)
    if g not in SNAP:
        raise Stop(f'KiCad grid {g} mil için Proteus snap değeri doğrulanmadı')
    return SNAP[g]


def write_workspace(out, size, snap):
    """Proteus keeps grid/snap in '<project>.<computer>.<user>.workspace' (W01); written from the W01 sample with the
    sheet bounds and KiCad's grid. Proteus rewrites this file when the project is closed."""
    src = _os.path.join(TEMPLATES, 'snap50.workspace')
    s = open(src, encoding='utf-8').read()
    W, H = size
    s = re.sub(r'snapidx="\d+" snap="\d+"', f'snapidx="{snap[0]}" snap="{snap[1]}"', s)
    s = s.replace('y1="-14850000" x1="-21000000" y2="14850000" x2="21000000"',
                  f'y1="{-H * U // 2}" x1="{-W * U // 2}" y2="{H * U // 2}" x2="{W * U // 2}"')
    path = f"{out}.{_os.environ.get('COMPUTERNAME', 'PC')}.{_os.environ.get('USERNAME', 'user')}.workspace"
    open(path, 'w', encoding='utf-8', newline='\n').write(s)
    return path


def inventory(sch):
    """What the schematic contains, for section 1 of the report."""
    t = parse(open(sch, encoding='utf-8').read())
    names = {'wire': 'tel parçası', 'junction': 'bağlantı noktası (junction)', 'label': 'yerel etiket',
             'text': 'metin', 'no_connect': '"bağlantı yok" (X) işareti', 'global_label': 'global etiket',
             'hierarchical_label': 'hiyerarşik etiket', 'bus': 'veri yolu', 'sheet': 'alt sayfa'}
    lines = []
    libs = {s[1]: s for s in find(first(t, 'lib_symbols'), 'symbol')}
    by_lib = defaultdict(list)
    for s in find(t, 'symbol'):
        ref = next(p[2] for p in find(s, 'property') if p[1] == 'Reference')
        val = next((p[2] for p in find(s, 'property') if p[1] == 'Value'), '')
        by_lib[first(s, 'lib_id')[1]].append(f'{ref}' + (f' ({val})' if val and not ref.startswith('#') else ''))
    n_parts = sum(len(v) for k, v in by_lib.items() if not (libs.get(k) and first(libs[k], 'power')))
    lines.append(f'Semboller: {sum(len(v) for v in by_lib.values())} ({n_parts} parça, '
                 f'{sum(len(v) for v in by_lib.values()) - n_parts} güç sembolü)')
    for lib_id, refs in sorted(by_lib.items()):
        kind = 'güç sembolü' if libs.get(lib_id) and first(libs[lib_id], 'power') else 'parça'
        lines.append(f'   {lib_id:34} {kind:11} ×{len(refs):<3} {", ".join(refs)}')
    counts = defaultdict(int)
    for c in t[1:]:
        if isinstance(c, list) and c[0] not in ('version', 'generator', 'generator_version', 'uuid', 'paper',
                                                 'title_block', 'lib_symbols', 'symbol', 'sheet_instances',
                                                 'embedded_fonts'):
            counts[c[0]] += 1
    for k_, n in sorted(counts.items()):
        lines.append(f'{names.get(k_, k_)}: {n}')
    paper = first(t, 'paper')
    tb = first(t, 'title_block')
    lines.append(f'Sayfa: {" ".join(paper[1:])}; başlık bloğu: '
                 + (', '.join(f'{c[0]}="{c[1]}"' for c in tb[1:] if isinstance(c, list)) if tb else 'yok'))
    return lines


def order_rules(path):
    """R3 (each wire right after its last referrer) and R4 (dot pointers ascending) on the written file."""
    g = synth.load(path).objs
    pos = {id(o): i for i, o in enumerate(g)}
    last = {}
    for i, o in enumerate(g):
        for w in (o['pins'] if o['kind'] == 'comp' else o['ptrs'] if o['kind'] == 'dot' else
                  [o['ptr']] if o['kind'] == 'term' else []):
            if w is not None:
                last[id(w)] = i
    wires = [(i, o) for i, o in enumerate(g) if o['kind'] == 'wire']
    r3 = sum(1 for i, w in wires if last.get(id(w), i) < i and all(g[j]['kind'] == 'wire' for j in range(last[id(w)] + 1, i)))
    dots = [o for o in g if o['kind'] == 'dot']
    r4 = sum(1 for d in dots if [pos[id(w)] for w in d['ptrs'] if w] == sorted(pos[id(w)] for w in d['ptrs'] if w))
    return r3, len(wires), r4, len(dots)


def main(sch, out):
    import datetime
    _os.makedirs(_os.path.dirname(_os.path.abspath(out)), exist_ok=True)
    rapor = _os.path.splitext(out)[0] + '.rapor.txt'
    head = ['KiCad -> Proteus dönüştürme raporu', '=' * 34, f'Kaynak : {_os.path.abspath(sch)}',
            f'Çıktı  : {_os.path.abspath(out)}', f'Tarih  : {datetime.datetime.now():%Y-%m-%d %H:%M}',
            'Araç   : k2p/k2p.py']
    S = {}                                         # section title -> lines

    def sec(title, lines):
        S[title] = list(lines) or ['(yok)']

    result = None
    try:
        sec('1. Şemanın içeriği', inventory(sch))
        erc_lines, erc_stop = erc(sch)
        sec('2. Ön kontrol (KiCad ERC)', erc_lines)
        issues = scan(sch)
        sec('3. Aktarılamayan öğeler', [f'- {i}' for i in issues] + ([
            '', f'Toplam {len(issues)} madde. Araç yaklaşık sonuç üretmez: bunlar desteklenene (ya da şemada '
                'değiştirilene) kadar Proteus dosyası yazılmaz.'] if issues else
            ['Yok: şemadaki her öğenin birebir karşılığı var.']))
        if erc_stop:
            raise Stop(f'KiCad ERC {len(erc_stop)} bağlantı hatası buldu (bölüm 2)')
        if issues:
            raise Stop(f'{len(issues)} öğe henüz aktarılamıyor (bölüm 3)')
        k = read_kicad(sch)
        k['svg_texts'] = kicad_svg_texts(sch)
        k['svg_outlines'] = kicad_svg_outlines(sch)
        frame = Frame()
        det = defaultdict(list)
        comps, dots, wires, texts, devices, pin_syms = convert(k, frame, det)
        objs = order_objects(comps + det['terms'], dots, wires, det['ncs'] + texts)
        knets = kicad_netlist(sch)
        pnets = proteus_nets(objs)
        if pnets != knets:
            raise Stop(f'netlist farklı: yalnız KiCad {len(knets - pnets)}, yalnız Proteus {len(pnets - knets)}')
        bad = geometry_check(objs)
        if bad:
            raise Stop(f'{bad} tel ucu pin/noktayla çakışmıyor')
        grid = workspace_snap(sch)
        tmp = out + '.tmp'
        assemble(frame, k, objs, comps, devices, pin_syms, tmp, det['term_syms'])
        try:
            readback_check(tmp, objs)
            r3, nw, r4, nd = order_rules(tmp)
        except Stop:
            _os.remove(tmp)
            raise
        _os.replace(tmp, out)
        ws = write_workspace(out, k['size'], grid)
        for name, text in MDF_FILES.items():
            open(_os.path.join(_os.path.dirname(_os.path.abspath(out)), name), 'w', encoding='latin-1',
                 newline='').write(text)
        mmp = det['to_mm']
        c = det['counts']
        W, H = k['size']
        fmt = lambda v: f'{v:g}'
        # 4. transferred
        tr = [f'Sayfa: {W} x {H} mm (KiCad sayfasının köşeleri Proteus sayfasının köşeleri; koordinatlar 10 nm '
              f'hassasiyetle, yuvarlama yok). Başlık "{k["title"].get("title", "")}", revizyon '
              f'"{k["title"].get("rev", "")}" -> Proteus proje bilgileri (Design Properties).',
              f'Izgara: KiCad bağlantı ızgarası -> Proteus snap {grid[1] / 2540000:g} in ({_os.path.basename(ws)}).',
              '', f'Özel aygıtlar ({len({v[0] for v in devices.values()})}; KiCad sembol çizimi ve pinleri aynen):']
        listed = set()
        for (lib_id, unit, mir, ang), (name, _, order, props, prim) in devices.items():
            if name in listed:
                continue
            listed.add(name)
            model = f'simülasyon modeli Proteus {prim}: {" ".join(props.split())}' if prim else \
                'simülasyon modeli YOK (eşleme tablosunda değil; görüntü ve bağlantı için)'
            mtxt = f' (KiCad "mirror {mir}": çizim aygıta aynalanmış yazıldı)' if mir else ''
            tr.append(f'   {name:14} <- {lib_id} birim {"ABCDEFGH"[unit - 1]}{mtxt}; {model}; pinler '
                      + ', '.join(f'{p["number"]}->{p["name"]}' for p in order))
        tr.append(f'Pin sembolleri: {", ".join(sorted(pin_syms.values()))} (KiCad pin uzunluğu; daireli pin KiCad '
                  'ölçüsünde)')
        for r_, v_, pv in det['values']:
            tr.append(f'   Değer: {r_} "{v_}" -> model özelliği {pv}')
        for name in MDF_FILES:
            tr.append(f'   Model dosyası {name} çıktının yanına yazıldı (Proteus onu proje klasöründe arar); kaynak: '
                      + next(v['mdf']['kaynak'] for v in PARTS_INFO.values() if (v.get('mdf') or {}).get('ad', '') + '.MDF' == name))
        tr += ['', f'Bileşenler ({len(comps)}): referans, aygıt, değer, KiCad konumu (mm)']
        for cc in comps:
            s_ = next(s for s in k['syms'] if s['ref'] == cc['ref'])
            tr.append(f'   {cc["labels"][0]["text"]:7} {cc["device"]:14} {cc["labels"][1]["text"]:12} '
                      f'({s_["at"][0]}, {s_["at"][1]}) açı {s_["angle"]}')
        tr += ['', f'Teller: {c["segs"]} KiCad tel parçası, uç uca eklenenler birleştirilerek {c["chains"]} Proteus '
                   'teli (ara noktalar korunur, geometri aynı).',
               f'   pin bağlantısı {c.get("pin", 0)}, junction {c.get("junction", 0)} -> Proteus noktası',
               f'Etiketler: {sum(len(w["wlabels"]) for w in wires)} (KiCad konumu, yönü, boyutu aynen)']
        for w in wires:
            for l in w['wlabels']:
                x, y = mmp((l['x'], l['y']))
                tr.append(f'   "{l["text"]}" ({fmt(x)}, {fmt(y)}) mm')
        for t, lines in det['texts']:
            tr.append(f'Metin ({t["at"][0]}, {t["at"][1]}) mm: {len(lines)} satır')
        pterms = [t for t in det['terms'] if not t.get('glob')]
        gterms = [t for t in det['terms'] if t.get('glob')]
        if pterms:
            tr += ['', f'Güç sembolleri -> Proteus terminali ({len(pterms)}; aynı adlı terminaller aynı ağ; GND '
                       'Proteus\'un kendi toprak terminali, diğerleri KiCad\'deki çizimle genel terminal):']
            for t in pterms:
                x, y = mmp((t['x'], t['y']))
                tr.append(f'   {t["ref"]:8} "{t["net"]}" ({fmt(x)}, {fmt(y)}) mm, sembol {t["sym"]}, açı {t["rot"] // 10}')
        if gterms:
            tr += ['', f'Global etiketler -> KiCad\'deki çerçeveyi çizen Proteus terminali ({len(gterms)}; aynı adlılar '
                       'aynı ağ; terminal türü etiket biçiminden: input, output, bidirectional, tri_state -> çift yönlü, '
                       'passive -> genel):']
            for t in gterms:
                x, y = mmp((t['x'], t['y']))
                tr.append(f'   "{t["net"]}" ({fmt(x)}, {fmt(y)}) mm, biçim {t["glob"]}, sembol {t["sym"]}')
        if det['ncs']:
            tr += ['', f'"Bağlantı yok" işaretleri -> KiCad\'deki X ({len(det["ncs"])}, terminal $TERK_NC, pine bağlı değil):']
            tr += [f'   ({fmt(mmp((t["x"], t["y"]))[0])}, {fmt(mmp((t["x"], t["y"]))[1])}) mm' for t in det['ncs']]
        sec('4. Aktarılanlar', tr)
        sec('5. Onaylı eklemeler (KiCad\'de olmayan ama Proteus\'un zorunlu kıldığı)',
            [f'Boşta tel ucu -> tek telli nokta ({len(det["free_end"])}):'] +
            [f'   ({fmt(mmp(p)[0])}, {fmt(mmp(p)[1])}) mm' +
             (f'  etiket "{w["wlabels"][0]["text"]}"' if w['wlabels'] else '') for p, w in det['free_end']] +
            [f'Pin ucundaki junction -> pin, 0 mm tel, nokta ({len(det["pin_junction"])}):'] +
            [f'   {r} pin {n} ({fmt(mmp(p)[0])}, {fmt(mmp(p)[1])}) mm' for r, n, p in det['pin_junction']] +
            ['Sayfa çerçevesi ve başlık kutusu çizilmedi (karar 3).'] +
            ([f'PWR_FLAG taşınmadığı için ({len(det["pwr_flag"])}) onların bağlı olduğu tel ucu boşta kaldıysa yukarıdaki '
              'listede tek telli nokta olarak görünür.'] if det['pwr_flag'] else []))
        sec('6. Gösterim seçimleri (aynı görüntü, Proteus\'un sınırları yüzünden başka yolla)',
            ['- Pin numaraları sembolün içinde yazı (Proteus paketsiz aygıtta pin numarası çizmiyor).',
             '- Referans KiCad\'deki gibi birim harfiyle (U40A); netlist\'te KiCad U40 der, karşılaştırma eşler.',
             '- Döndürülmüş parçada pin yazıları KiCad gibi okunur kalsın diye, aynı sembolün yazıları farklı düşen her '
             'yönü için ayrı aygıt (adı _R180 gibi biter); Proteus sembol yazılarını parçayla birlikte döndürüyor.'] +
            (['- GND: Proteus\'un kendi toprak sembolü ($TERGROUND, Proteus\'un yazdığı gibi etiketsiz), bağlantı noktası '
              'KiCad\'deki pin ucunda (kullanıcı kararı 2026-11-01; KiCad\'in GND çizimi ve "GND" yazısı taşınmıyor).',
              '- Diğer güç sembolleri (+BATT gibi) KiCad çizimiyle özel genel terminal; ağ adı terminal yazısı. Proteus '
              '"+ad" yazılı güç terminalini gerilim kaynağı sanıyor (KiCad\'de bunlar yalnız ağ adı). Not: Proteus VCC, '
              'VDD, VEE ve VSS adlı ağları kendi varsayılan güç raylarına bağlar (VCC/VDD +5 V, VEE -5 V, VSS = GND).']
             if det['terms'] else []) +
            (['- "Bağlantı yok" işareti KiCad\'deki X ölçüsünde özel terminal (Proteus\'un kendi NC terminali X\'i '
              'pinden 0.1 in uzağa koyuyor).'] if det['ncs'] else []) +
            [f'- Çok satırlı metin ({len(lines)} satır) satır başına bir Proteus metni, KiCad\'in çizdiği yerlerde '
             '(Proteus metni tek satırlık).' for _, lines in det['texts'] if len(lines) > 1])
        sec('7. Taşınmayan bilgiler',
            ['- Gizli alanlar (Footprint, Datasheet, Description ve gizli diğer alanlar): KiCad\'de de görünmüyor, '
             'Proteus dosyasında yok.',
             '- Çizgi kalınlığı ve renkler: Proteus\'un kendi stilleri (COMPONENT, WIRE) kullanılıyor.',
             '- Sembollerin dolgu rengi Proteus\'un bileşen rengi (KiCad açık sarı).'] +
            ([f'- PWR_FLAG ({", ".join(s_["ref"] for s_ in det["pwr_flag"])}): KiCad\'in ERC işareti, Proteus\'ta '
              'karşılığı yok (kullanıcı kararı).'] if det['pwr_flag'] else []) +
            ([f'- Simülasyon modeli olmayan parçalar ({", ".join(det["nomodel"])}): eşleme tablosunda yok; Proteus\'ta '
              'görünür ve bağlanır, simülasyona girmez.'] if det['nomodel'] else []))
        sec('8. Bilgi notları', det['overlap'])
        sec('9. Doğrulama (araç içinde, yazmadan önce ve sonra)',
            [f'- Netlist: {len(knets)} ağ, KiCad netlist\'iyle (kicad-cli) birebir aynı.',
             '- Geometri: her tel ucu bağlı olduğu pin ucunda ya da noktada.',
             f'- Geri okuma: yazılan dosya yeniden okundu, {len(objs)} nesne modelle aynı.',
             f'- Proteus yazım sırası: tel son başvurandan hemen sonra {r3}/{nw}, nokta işaretçileri artan {r4}/{nd}.'])
        sec('10. Proteus\'ta elle yapılacak kontroller (henüz otomatik değil)',
            ['- Proteus 8.17\'de aç: mesaj çıkmamalı.',
             '- File > Save Project As ile farklı adla kaydet; k2p/compare_objs.py ile karşılaştır: IDENTICAL.',
             '- Tool > Netlist Compiler (SDF, dosya) ve k2p/compare_sdf.py: IDENTICAL.',
             '- Simülasyon: şemada giriş/gösterge parçası varsa çalıştır.'])
        result = f'YAZILDI. {len(comps)} bileşen, {len(wires)} tel, {len(dots)} nokta, ' \
                 f'{sum(len(w["wlabels"]) for w in wires)} etiket, {len(texts)} metin.'
        ok = True
    except Stop as e:
        result = f'YAZILMADI. Neden: {e}. Proteus dosyası üretilmedi.'
        ok = False
    lines = head + [f'SONUÇ  : {result}', '']
    for title, body in S.items():
        lines += [title, '-' * len(title)] + body + ['']
    open(rapor, 'w', encoding='utf-8').write('\n'.join(lines))
    print('\n'.join(lines))
    return ok


if __name__ == '__main__':
    _sys.exit(0 if main(_sys.argv[1], _sys.argv[2]) else 1)
