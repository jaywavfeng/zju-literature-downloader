# -*- coding: utf-8 -*-
"""ZJU WebVPN (webvpn.zju.edu.cn, 网瑞达) login + fetch.

Why it exists: some publishers are licensed by **campus IP** (Wiley, IEEE Xplore,
Springer, Web of Science...). From off campus you can borrow that IP through the
WebVPN. Login uses the 上网账号 (NOT always the same password as 浙大通行证).

The login form Encrypts the password with AES-CFB-128, key = iv = "wrdvpnisawesome!"
(same construction as the site's own aes-js bundle).

Usage:
  python zju_webvpn.py login            # prints session info / verifies
  python zju_webvpn.py resources        # list available databases + proxy URLs
  python zju_webvpn.py get --url <https://...> --out file.pdf
Only pre-registered hosts are reachable through the VPN; see `resources`.
"""
import argparse
import json
import os
import re
import sys
import time

import requests

ROOT = 'https://webvpn.zju.edu.cn'
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36')
KEY = b'wrdvpnisawesome!'
IV = b'wrdvpnisawesome!'


def _encrypt_password(pwd: str) -> str:
    """AES-CFB-128 (segment=16) then hex(iv) + hex(ct)[:len*2]; '0'-padded to 16."""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    except ImportError:
        raise SystemExit('需要 cryptography：pip install cryptography')
    text = pwd
    if len(text) % 16:
        text += '0' * (16 - len(text) % 16)
    cipher = Cipher(algorithms.AES(KEY), modes.CFB(IV))
    enc = cipher.encryptor()
    ct = enc.update(text.encode('utf-8')) + enc.finalize()
    return IV.hex() + ct[:len(pwd) * 2].hex()


def session(user, pwd):
    s = requests.Session()
    s.headers.update({'User-Agent': UA, 'Referer': ROOT + '/login'})
    r = s.get(ROOT + '/login', timeout=30)
    h = r.text
    csrf = re.search(r'name="_csrf"\s+value="([^"]+)"', h)
    cid = re.search(r'name="captcha_id"\s+value="([^"]+)"', h)
    need = re.search(r'name="needCaptcha"\s+value="([^"]+)"', h)
    if not csrf:
        raise SystemExit('cannot parse WebVPN login page')
    data = {'_csrf': csrf.group(1), 'auth_type': 'local', 'username': user,
            'sms_code': '', 'password': _encrypt_password(pwd), 'captcha': '',
            'needCaptcha': need.group(1) if need else 'false',
            'captcha_id': cid.group(1) if cid else ''}
    p = s.post(ROOT + '/do-login', data=data, timeout=40)
    ok = '"success":true' in p.text or '"success": true' in p.text
    if not ok and 'NEED_CONFIRM' in p.text:
        c = s.post(ROOT + '/do-confirm-login', timeout=40,
                   headers={'X-Requested-With': 'XMLHttpRequest'})
        ok = '"success":true' in c.text or '"success": true' in c.text
    if not ok:
        raise SystemExit('WebVPN login failed: %s' % p.text[:160])
    return s


def resources(s):
    r = s.get(ROOT + '/user/portal_groups', timeout=30)
    out = []
    for g in (r.json().get('data') or []):
        gname = (g.get('group') or {}).get('group_name')
        for it in (g.get('resource') or []):
            out.append({'group': gname, 'name': it.get('name'),
                        'url': it.get('url'), 'redirect': it.get('redirect')})
    return out


def find(res, keyword):
    k = keyword.lower()
    return [r for r in res
            if k in (r['name'] or '').lower() or k in (r['url'] or '').lower()]


def get(s, url, out=None, referer=None):
    """url may be a full external URL (must already be a pre-registered VPN path)
    or a path under webvpn.zju.edu.cn."""
    full = url if url.startswith('http') else ROOT + url
    r = s.get(full, timeout=90, allow_redirects=True,
              headers={'Referer': referer or ROOT + '/'})
    if out:
        with open(out, 'wb') as f:
            f.write(r.content)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('cmd', choices=['login', 'resources', 'get'])
    ap.add_argument('--user', default=os.environ.get('ZJU_VPN_USER', ''))
    ap.add_argument('--pwd', default=os.environ.get('ZJU_VPN_PASS', ''))
    ap.add_argument('--url')
    ap.add_argument('--out')
    ap.add_argument('--find')
    a = ap.parse_args()

    if not a.user or not a.pwd:
        raise SystemExit('set ZJU_VPN_USER / ZJU_VPN_PASS')
    s = session(a.user, a.pwd)
    if a.cmd == 'login':
        info = s.get(ROOT + '/user/info', timeout=30).json()
        print(json.dumps(info, ensure_ascii=False, indent=1))
    elif a.cmd == 'resources':
        res = resources(s)
        if a.find:
            res = find(res, a.find)
        for r in res:
            print('%-22s %-46s %s' % (r['group'] or '', r['name'] or '', r['url'] or ''))
    elif a.cmd == 'get':
        if not a.url or not a.out:
            raise SystemExit('--url and --out are required')
        r = get(s, a.url, a.out)
        head = open(a.out, 'rb').read(4)
        print('%s %d bytes pdf=%s' % (r.status_code, len(r.content), head == b'%PDF'))


if __name__ == '__main__':
    main()
