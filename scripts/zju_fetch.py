# -*- coding: utf-8 -*-
"""Main CLI: turn a reference list into a folder of downloaded PDFs.

    prepare  parse a reference list (文献.txt / docx / json) -> items.json
    fetch    run the source waterfall for every item -> PDFs
    verify   check that every expected PDF exists and is a valid PDF
    report   write the manifest CSV + a manual-download list for leftovers

Design rule: never stop at the first failure. Every item walks the full
waterfall, and whatever is left is written to a manual-download list.
"""
import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
import zipfile

import requests

HERE = os.path.dirname(os.path.abspath(__file__))
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')

sys.path.insert(0, HERE)
import sources  # noqa: E402


def sanitize(name):
    name = unicodedata.normalize('NFKC', name)
    name = ILLEGAL.sub(' ', name)
    name = re.sub(r'\s+', ' ', name).strip().rstrip('. ')
    return (name[:100] if len(name) > 100 else name) or 'untitled'


# ------------------------------------------------------------------ prepare
def parse_txt(path):
    items, cur = [], None
    for line in open(path, encoding='utf-8'):
        s = line.strip()
        m = re.match(r'^\[(\d+)\]\s+(.*)$', s)
        if m:
            parts = [p.strip() for p in m.group(2).split('|')]
            cur = {'num': int(m.group(1)), 'title': parts[-1]}
            items.append(cur)
            continue
        m2 = re.match(r'^DOI:\s*(\S+)', s)
        if m2 and cur is not None:
            cur['doi'] = m2.group(1).replace('https://doi.org/', '')
    return [i for i in items if i.get('doi')]


def parse_docx(path):
    """Reference paragraphs look like: [1] Authors. Title[J]. Journal, year... DOI: 10.x/y"""
    z = zipfile.ZipFile(path)
    xml = z.read('word/document.xml').decode('utf-8', 'replace')
    paras = re.findall(r'<w:p\b[^>]*>(.*?)</w:p>', xml, re.S)
    items = []
    for p in paras:
        t = ''.join(re.findall(r'<w:t[^>]*>(.*?)</w:t>', p, re.S))
        for a, b in (('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"')):
            t = t.replace(a, b)
        m = re.match(r'^\s*\[(\d+)\]\s+(.+)$', t)
        if not m:
            continue
        num, text = int(m.group(1)), m.group(2).strip()
        dm = re.search(r'DOI:\s*(\S+)', text)
        left = re.split(r'\[[JC]\]', text)[0]
        title = left.split('et al.', 1)[-1] if ' et al.' in left else re.split(r'\.\s+', left)[-1]
        items.append({'num': num, 'title': title.strip(' .'), 'doi': dm.group(1) if dm else None})
    return items


def resolve_pii(doi):
    try:
        r = requests.get('https://doi.org/' + doi, headers={'User-Agent': UA},
                         timeout=30, allow_redirects=True)
        m = re.search(r'/pii/(S[A-Za-z0-9()]+)', r.url) or \
            re.search(r'/pii/(S[A-Za-z0-9()]+)', r.text[:20000])
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def cmd_prepare(a):
    if a.src.endswith('.docx'):
        items = parse_docx(a.src)
    elif a.src.endswith('.json'):
        items = json.load(open(a.src, encoding='utf-8'))
    else:
        items = parse_txt(a.src)
    sess = requests.Session()
    sess.headers.update({'User-Agent': UA})
    for it in items:
        if it.get('doi') and it['doi'].startswith('10.1016/'):
            it['pii'] = resolve_pii(it['doi'])
        it['filename'] = '%d-%s.pdf' % (it['num'], sanitize(it['title']))
        time.sleep(0.4)
    json.dump(items, open(a.out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('prepared %d items -> %s' % (len(items), a.out))


# ------------------------------------------------------------------ fetch
def cmd_fetch(a):
    items = json.load(open(a.list, encoding='utf-8'))
    os.makedirs(a.out, exist_ok=True)
    sess = requests.Session()
    sess.headers.update({'User-Agent': UA})

    todo = [i for i in items if not os.path.exists(os.path.join(a.out, i['filename']))]
    print('total %d, already have %d, to fetch %d'
          % (len(items), len(items) - len(todo), len(todo)))

    # Phase 1: cheap routes (OA / publisher direct / sci-hub)
    remaining = []
    for it in todo:
        buf, log = sources.fetch_plain(it['doi'], sess, use_sci_hub=not a.no_sci_hub)
        if buf:
            with open(os.path.join(a.out, it['filename']), 'wb') as f:
                f.write(buf)
            print('[OA ] %-56s %.1f KB' % (it['filename'][:56], len(buf) / 1024))
        else:
            remaining.append(it)
        time.sleep(a.gap)
    print('\nphase1 done: %d ok, %d left' % (len(todo) - len(remaining), len(remaining)))

    # Phase 2: Elsevier via 浙大通行证 (real browser)
    sd = [i for i in remaining if i.get('pii')]
    if sd and not a.no_cas:
        print('\nphase2: ScienceDirect via ZJU CAS for %d items' % len(sd))
        payload = [{'pii': i['pii'], 'filename': i['filename']} for i in sd]
        tmp = os.path.join(a.out, '__sd_batch.json')
        json.dump(payload, open(tmp, 'w', encoding='utf-8'), ensure_ascii=False)
        env = dict(os.environ)
        env.setdefault('ZJU_USER', a.user or '')
        env.setdefault('ZJU_PASS', a.pwd or '')
        env['ZJU_SD_DEBUG_DIR'] = os.path.join(a.out, '__debug')
        r = subprocess.run(
            [sys.executable, os.path.join(HERE, 'zju_sd_fetch.py'),
             '--list', tmp, '--out', a.out, '--min-gap', str(a.gap)]
            + (['--profile', a.profile] if a.profile else [])
            + (['--no-hitl'] if a.no_hitl else []),
            env=env)
        print('  zju_sd_fetch exit=%s' % r.returncode)
        remaining = [i for i in remaining
                     if not os.path.exists(os.path.join(a.out, i['filename']))]
        os.remove(tmp)

    # Phase 3: whatever is left -> manual list
    if remaining:
        p = os.path.join(a.out, '_待手动下载.md')
        with open(p, 'w', encoding='utf-8') as f:
            f.write('# 待手动下载（%d 篇）\n\n' % len(remaining))
            for i in remaining:
                url = ('https://www.sciencedirect.com/science/article/pii/' + i['pii']
                       if i.get('pii') else 'https://doi.org/' + i['doi'])
                f.write('- [%d] %s\n  - %s\n  - 保存为 `%s`\n'
                        % (i['num'], i['title'], url, i['filename']))
        print('\nleft %d -> %s' % (len(remaining), p))
    print('\nfetched: %d/%d' % (len(items) - len(remaining), len(items)))


# ------------------------------------------------------------------ verify / report
def cmd_verify(a):
    items = json.load(open(a.list, encoding='utf-8'))
    ok, bad, miss = [], [], []
    for it in items:
        p = os.path.join(a.out, it['filename'])
        if os.path.exists(p) and open(p, 'rb').read(4) == b'%PDF' \
                and os.path.getsize(p) > 15000:
            ok.append(it['num'])
        elif os.path.exists(p):
            bad.append(it['filename'])
        else:
            miss.append((it['num'], it['title']))
    print('ok %d | corrupt %d | missing %d' % (len(ok), len(bad), len(miss)))
    print('missing:', miss)
    print('corrupt:', bad)


def cmd_report(a):
    items = json.load(open(a.list, encoding='utf-8'))
    rows = [['编号', '标题', 'DOI', '文件名', '大小(KB)', '状态']]
    for it in items:
        p = os.path.join(a.out, it['filename'])
        if os.path.exists(p):
            rows.append([it['num'], it['title'], it.get('doi', ''), it['filename'],
                         round(os.path.getsize(p) / 1024, 1), '成功'])
        else:
            rows.append([it['num'], it['title'], it.get('doi', ''), '', '', '缺失'])
    out = os.path.join(a.out, '_下载清单.csv')
    with open(out, 'w', newline='', encoding='utf-8-sig') as f:
        csv.writer(f).writerows(rows)
    print('manifest ->', out)


def main():
    ap = argparse.ArgumentParser(prog='zju_fetch')
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('prepare')
    p.add_argument('--src', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser('fetch')
    p.add_argument('--list', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--gap', type=float, default=6.0)
    p.add_argument('--profile', default=os.path.expanduser('~/.zju_sd_profile'))
    p.add_argument('--user', default=os.environ.get('ZJU_USER', ''))
    p.add_argument('--pwd', default=os.environ.get('ZJU_PASS', ''))
    p.add_argument('--no-sci-hub', action='store_true')
    p.add_argument('--no-cas', action='store_true')
    p.add_argument('--no-hitl', action='store_true')
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser('verify')
    p.add_argument('--list', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('report')
    p.add_argument('--list', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_report)

    a = ap.parse_args()
    a.func(a)


if __name__ == '__main__':
    main()
