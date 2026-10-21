# -*- coding: utf-8 -*-
"""Source waterfall for a DOI: try every free/legal route before giving up.

Order (cheap -> expensive):
  1. OpenAlex / Unpaywall OA pdf
  2. Publisher-direct patterns that work without a campus IP
       - Nature family  : https://www.nature.com/articles/<suffix>.pdf
       - MDPI           : https://mdpi-res.com/d_attachment/<abbr>/<abbr>-<vol>-<art5>/article_deploy/<...>.pdf
                          (www.mdpi.com/.../pdf is Akamai-blocked; mdpi-res.com is not)
       - OSTI (DOE/PNNL): https://www.osti.gov/servlets/purl/<osti_id>
  3. Repository copies found in OpenAlex locations (HAL, PMC, university repos)
  4. Sci-Hub mirrors (last-resort fallback, only if policy allows)

All network calls are rate limited (file-lock based, default 1 req/s per host).
"""
import json
import os
import re
import sys
import time

import requests

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
MIN_PDF = 15000

MDPI_ABBR = {'app': 'applsci', 's': 'sensors', 'e': 'electronics', 'm': 'micromachines',
             'en': 'energies', 'ma': 'materials', 'ph': 'photonics', 'wa': 'water'}

_last_call = {}


def _throttle(host, gap=1.0):
    """Cross-process friendly: a lock file per host enforces a minimum gap."""
    import tempfile
    lock = os.path.join(tempfile.gettempdir(), 'zjfetch_%s.lock' % re.sub(r'\W', '_', host))
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                now = time.monotonic()
                prev = _last_call.get(host, 0.0)
                wait = gap - (now - prev)
                if wait > 0:
                    time.sleep(wait)
                _last_call[host] = time.monotonic()
            finally:
                os.close(fd)
                os.unlink(lock)
            return
        except FileExistsError:
            time.sleep(0.05)
        except Exception:
            return


def _get(sess, url, **kw):
    from urllib.parse import urlparse
    _throttle(urlparse(url).netloc)
    return sess.get(url, timeout=45, **kw)


def is_pdf(buf):
    return buf[:4] == b'%PDF' and len(buf) >= MIN_PDF


def mdpi_guess(doi):
    m = re.match(r'^10\.3390/([a-z]+)(\d{2})(\d{2})(\d+)$', doi)
    if not m:
        return None
    key, vol, _issue, art = m.groups()
    abbr = MDPI_ABBR.get(key, key)
    stem = '%s-%s-%s' % (abbr, vol, art.zfill(5))
    return 'https://mdpi-res.com/d_attachment/%s/%s/article_deploy/%s.pdf' % (abbr, stem, stem)


def candidates(doi, sess):
    """Return an ordered list of candidate PDF URLs for a DOI."""
    out = []

    def add(u):
        if u and u not in out:
            out.append(u)

    # 1) OpenAlex
    try:
        r = _get(sess, 'https://api.openalex.org/works/doi:' + doi)
        if r.status_code == 200:
            d = r.json()
            b = d.get('best_oa_location') or {}
            add(b.get('pdf_url'))
            for L in (d.get('locations') or []):
                add(L.get('pdf_url'))
    except Exception:
        pass

    # 2) Unpaywall
    try:
        r = _get(sess, 'https://api.unpaywall.org/v2/%s?email=zju.literature@example.org' % doi)
        if r.status_code == 200:
            for L in (r.json().get('oa_locations') or []):
                add(L.get('url_for_pdf'))
    except Exception:
        pass

    # 3) publisher patterns
    if doi.startswith('10.1038/'):
        add('https://www.nature.com/articles/%s.pdf' % doi.split('/', 1)[1])
    g = mdpi_guess(doi)
    if g:
        add(g)
    if doi.startswith('10.1002/'):
        add('https://onlinelibrary.wiley.com/doi/pdfdirect/' + doi)

    # 4) OSTI (DOE / PNNL reports)
    if doi.startswith('10.1016/') or doi.startswith('10.2172/'):
        try:
            r = _get(sess, 'https://www.osti.gov/api/v1/records?doi=' + doi)
            m = re.search(r'<osti_id>(\d+)</osti_id>', r.text)
            if m:
                add('https://www.osti.gov/servlets/purl/' + m.group(1))
        except Exception:
            pass
    return out


def sci_hub(doi, sess):
    for host in ('https://sci-hub.ru', 'https://sci-hub.box', 'https://sci-hub.red',
                 'https://sci-hub.yt', 'https://sci-hub.wf'):
        try:
            r = _get(sess, host + '/' + doi)
            if r.status_code != 200:
                continue
            body = r.text
            if 'not available' in body or 'Verification' in body[:3000]:
                continue
            links = re.findall(r'["\']((?:https?:)?//?[^"\']*?\.pdf[^"\']*)["\']', body)
            for l in links:
                u = l.split('#')[0]
                u = ('https:' + u) if u.startswith('//') else (host + u if u.startswith('/') else u)
                yield u
        except Exception:
            continue


def try_url(sess, url, referer=None):
    h = {'User-Agent': UA, 'Accept': 'application/pdf,*/*'}
    if referer:
        h['Referer'] = referer
    try:
        r = _get(sess, url, headers=h, allow_redirects=True)
    except Exception as e:
        return None, 'request failed: %s' % type(e).__name__
    if r.status_code != 200:
        return None, 'HTTP %d' % r.status_code
    if is_pdf(r.content):
        return r.content, ''
    return None, 'not a PDF (%s, %d B)' % ((r.headers.get('content-type') or '?')[:22], len(r.content))


def fetch_plain(doi, sess, use_sci_hub=True):
    """Try all non-browser routes. Returns (bytes|None, [log lines])."""
    log = []
    for u in candidates(doi, sess):
        buf, why = try_url(sess, u)
        log.append('%s -> %s' % (u[:90], 'OK' if buf else why))
        if buf:
            return buf, log
    if use_sci_hub:
        for u in sci_hub(doi, sess):
            buf, why = try_url(sess, u, referer='https://sci-hub.ru/')
            log.append('sci-hub -> %s' % ('OK' if buf else why))
            if buf:
                return buf, log
    return None, log


def main():
    ap = __import__('argparse').ArgumentParser()
    ap.add_argument('--doi', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--no-sci-hub', action='store_true')
    a = ap.parse_args()
    sess = requests.Session()
    sess.headers.update({'User-Agent': UA})
    buf, log = fetch_plain(a.doi, sess, use_sci_hub=not a.no_sci_hub)
    for line in log:
        print('  ', line, file=sys.stderr)
    if buf:
        with open(a.out, 'wb') as f:
            f.write(buf)
        print('saved %s (%d bytes)' % (a.out, len(buf)))
        return 0
    print('FAILED %s' % a.doi)
    return 1


if __name__ == '__main__':
    sys.exit(main())
