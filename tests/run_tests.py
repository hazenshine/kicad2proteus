"""Regression tests: convert every test schematic and example, and compare each result with Proteus 8.17's own
re-save of it, byte for byte apart from the save time. A pass therefore also means "Proteus would change nothing".
Cases marked 'stop' must not be converted, and the report must give the reason.
usage: python tests/run_tests.py
"""
import contextlib, io, os, re, sys, tempfile, zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, '..')
sys.path.insert(0, os.path.join(ROOT, 'k2p'))
import k2p

S = lambda n: os.path.join(HERE, 'schematics', n, n + '.kicad_sch')
E = lambda n: os.path.join(ROOT, 'examples', n, n + '.kicad_sch')
X = lambda n: os.path.join(HERE, 'expected', n + '.pdsprj')
CASES = [
    ('7seg_decoder', E('7seg_decoder'), 'write', X('7seg_decoder')),
    ('7seg_decoder_sim', E('7seg_decoder_sim'), 'write', X('7seg_decoder_sim')),
    ('cny70', E('cny70'), 'write', X('cny70')),
    ('T01_tek_kapi', S('T01_tek_kapi'), 'write', X('T01_tek_kapi')),
    ('T02_t_baglanti', S('T02_t_baglanti'), 'write', X('T02_t_baglanti')),
    ('T03_pin_junction', S('T03_pin_junction'), 'write', X('T03_pin_junction')),
    ('T04_metin_etiket', S('T04_metin_etiket'), 'write', X('T04_metin_etiket')),
    ('T05_aynali', S('T05_aynali'), 'write', X('T05_aynali')),
    ('T06_noktasiz_uclu', S('T06_noktasiz_uclu'), 'stop', 'junction olmadan 3 tel ucu'),
    ('T07_dondurulmus_kapilar', S('T07_dondurulmus_kapilar'), 'write', X('T07_dondurulmus_kapilar')),
    ('T08_cny70_guc_yok', S('T08_cny70_guc_yok'), 'write', X('T08_cny70_guc_yok')),
    ('T09_lojik_giris_cikis', S('T09_lojik_giris_cikis'), 'write', X('T09_lojik_giris_cikis')),
    ('T11_dikey_etiket', S('T11_dikey_etiket'), 'write', X('T11_dikey_etiket')),
    ('T12_coklu_birim', S('T12_coklu_birim'), 'write', X('T12_coklu_birim')),
    ('T13_global_etiket', S('T13_global_etiket'), 'write', X('T13_global_etiket')),
]


def entries(path):
    """Zip members with the save time blanked (ROOT.DSN 0xb1..0xb4 and its header dates, PROJECT.XML times)."""
    z = zipfile.ZipFile(path)
    out = {}
    for n in z.namelist():
        b = z.read(n)
        if n == 'ROOT.DSN':
            b = b[:0xb1] + bytes(4) + b[0xb5:]
            b = re.sub(rb'(\d\d/\d\d/\d\d\x00.{5,60}?)\d\d/\d\d/\d\d', rb'\1xx/xx/xx', b[:0x100], flags=re.S) + b[0x100:]
        elif n == 'PROJECT.XML':
            b = re.sub(rb'(MODIFIED|CREATED)="\d+"', rb'\1="0"', b)
        out[n] = b
    return out


def run(name, sch, mode, want, tmp):
    out = os.path.join(tmp, name + '.pdsprj')
    with contextlib.redirect_stdout(io.StringIO()):
        ok = k2p.main(sch, out)
    report = open(os.path.splitext(out)[0] + '.rapor.txt', encoding='utf-8').read()
    result = re.search(r'SONUÇ  : (.*)', report).group(1)
    if mode == 'stop':
        return (not ok and want in report), ('stopped as expected' if not ok else 'should have stopped')
    if not ok:
        return False, result
    a, b = entries(out), entries(want)
    diff = [n for n in sorted(set(a) | set(b)) if a.get(n) != b.get(n)]
    return not diff, ('same as Proteus\'s re-save' if not diff else f'differs: {diff}')


if __name__ == '__main__':
    fails = 0
    with tempfile.TemporaryDirectory() as tmp:
        for case in CASES:
            ok, msg = run(*case, tmp)
            fails += not ok
            print(f'{"PASS" if ok else "FAIL":5} {case[0]:26} {msg}')
    print(f'\n{len(CASES) - fails}/{len(CASES)} passed')
    sys.exit(1 if fails else 0)
