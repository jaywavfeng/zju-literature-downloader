# -*- coding: utf-8 -*-
"""Generic CLI for resources behind a campus unified-identity (SSO) gateway.

    check    probe login state / protected-endpoint reachability
    login    obtain and persist an SSO session with a real Chrome (CAS / OAuth2)
    locate   resolve resource metadata, then recursively enumerate the tree
    fetch    download everything, preserving relative paths
    verify   validate downloaded files (size, PDF header, notebook nbformat)
    report   write a Markdown manifest with a directory tree and leftovers

Site-specific endpoints live in a JSON profile (--config), never in this file.
The bundled references/profiles/zju-mo-workspace.json is the default ZJU
profile, so 浙大通行证 downloading keeps working out of the box.  The same
machinery covers experiment-VM workspaces, course platforms and any other
resource protected by a campus unified identity.

All network calls are rate limited (file lock, 1 req/s per host by default),
retried with exponential backoff, and fail with URL + status + body excerpts.
"""
import argparse
import base64
import json
import os
import sys
import time
import uuid
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from sso_common import (  # noqa: E402
    AuthError, HttpClient, ResourceError, SsoError, atomic_write, expand,
    load_config, load_session, probe_login, quote_path, save_json,
    save_session, urljoin,
)

ARGS = ['--disable-blink-features=AutomationControlled',
        '--exclude-switches=enable-automation',
        '--no-first-run', '--no-default-browser-check']


# ----------------------------------------------------------------- helpers
def ctx_of(a):
    cfg = load_config(a.config)
    session_path = a.session or cfg.get('session_file', '.sso_session.json')
    return cfg, session_path, load_session(session_path)


def auth_headers(cfg, session):
    a = cfg.get('auth', {})
    if not session.get('token'):
        return {}
    return {a.get('token_header', 'Authorization'):
            a.get('token_prefix', 'Bearer ') + session['token']}


def hub_headers(cfg, hub_ctx):
    return {k: expand(v, **hub_ctx)
            for k, v in (cfg.get('hub_headers') or {}).items()}


def meta_client(cfg, session):
    return HttpClient(gap=cfg.get('rate_gap', 1.0),
                      retries=cfg.get('retries', 4),
                      timeout=cfg.get('timeout', 45),
                      headers=auth_headers(cfg, session))


def hub_client(cfg, hub_ctx):
    return HttpClient(gap=cfg.get('rate_gap', 1.0),
                      retries=cfg.get('retries', 4),
                      timeout=cfg.get('timeout', 45),
                      headers=hub_headers(cfg, hub_ctx))


def read_metadata(cfg, session, resource_id):
    """Re-read metadata at every stage: it doubles as a login-state check and
    refreshes the per-resource token (tokens are resource-scoped, a token from
    another resource returns 403)."""
    if not session.get('token'):
        raise AuthError('no session token; run `login` first')
    url = urljoin(cfg['base'],
                  expand(cfg['metadata_api'], resource_id=resource_id))
    r = meta_client(cfg, session).get(url)
    try:
        j = r.json()
    except ValueError:
        raise ResourceError('metadata is not JSON', url=url, body=r.text[:200])
    meta = j.get('response', j) if isinstance(j, dict) else {}
    if not isinstance(meta, dict):
        raise ResourceError('unexpected metadata shape', url=url, body=r.text[:200])
    fields = cfg.get('metadata_fields', {}) or {}
    hub_ctx = {}
    for key, source in fields.items():
        value = meta.get(source)
        if key in ('hub_name', 'hub_token'):
            value = quote(str(value or ''), safe='')
        hub_ctx[key] = value if value is not None else ''
    hub_ctx['resource_id'] = resource_id
    hub_ctx.setdefault('window_id', uuid.uuid4().hex)
    for required in ('hub_name', 'hub_token'):
        if not hub_ctx.get(required):
            raise ResourceError('metadata missing %r (login expired or no access)'
                                % required, url=url, body=r.text[:200])
    return meta, hub_ctx


def collect_tree(node, files, dirs):
    """Flatten the platform's registered tree: key -> size, dirs kept too."""
    key = (node.get('key') or '').lstrip('/')
    other = node.get('other') or {}
    children = node.get('children') or []
    if key:
        if 'size' in other:
            files[key] = int(other['size'])
        else:
            dirs.append(key)
    for c in children:
        collect_tree(c, files, dirs)


def contents_url(cfg, hub_ctx, path):
    return urljoin(cfg['base'],
                   expand(cfg['contents_api'], path=quote_path(path), **hub_ctx))


def files_url(cfg, hub_ctx, path):
    return urljoin(cfg['base'],
                   expand(cfg['files_api'], path=quote_path(path), **hub_ctx))


def stream_get(client, url, headers, timeout):
    """Streaming GET that still respects the shared rate limiter."""
    client.throttle.wait(url)
    return client.s.get(url, headers=headers, stream=True, timeout=timeout,
                        allow_redirects=False)


def restart_hub(cfg, session, hub_ctx):
    """Recover a recycled container/session (HTTP 503 pattern). Returns bool."""
    if not cfg.get('restart_api'):
        return False
    mc = meta_client(cfg, session)
    hc = hub_client(cfg, hub_ctx)
    ctx = dict(hub_ctx)
    ctx['window_id'] = uuid.uuid4().hex
    url = urljoin(cfg['base'], expand(cfg['restart_api'], **ctx))
    for i in range(6):
        try:
            mc.get(url)
        except SsoError as e:
            print('  [restart] start #%d: %s' % (i + 1, str(e)[:120]),
                  file=sys.stderr, flush=True)
        time.sleep(4)
        try:
            hc.get(contents_url(cfg, hub_ctx, ''))
            return True
        except SsoError:
            continue
    return False


# ----------------------------------------------------------------- check
def cmd_check(a):
    cfg, session_path, session = ctx_of(a)
    probe = expand(cfg['auth'].get('probe_url', cfg['base']),
                   resource_id=a.resource_id or cfg.get('default_resource_id', ''),
                   **session)
    url = urljoin(cfg['base'], probe)
    client = meta_client(cfg, session)
    rep = probe_login(client, url,
                      ok_marker=cfg['auth'].get('ok_marker', ''),
                      login_marker=cfg['auth'].get('login_marker', ''),
                      headers=auth_headers(cfg, session))
    rep['profile'] = cfg['name']
    rep['session_file'] = session_path
    rep['has_session'] = bool(session.get('token'))
    save_json(a.output, rep)
    print('check: %s (%s) -> %s'
          % ('LOGGED IN' if rep['logged_in'] else 'NOT LOGGED IN',
             rep['reason'], a.output))
    return 0 if rep['logged_in'] else 1


# ----------------------------------------------------------------- login
def cmd_login(a):
    cfg, session_path, _ = ctx_of(a)
    auth = cfg.get('auth', {})
    user_env = a.user_env or auth.get('user_env', 'SSO_USER')
    pass_env = a.pass_env or auth.get('pass_env', 'SSO_PASS')
    user, pwd = os.environ.get(user_env, ''), os.environ.get(pass_env, '')
    if not user or not pwd:
        raise AuthError('credentials missing: set %s / %s (never store them in the repo)'
                        % (user_env, pass_env))

    redirect_uri = auth.get('redirect_uri', '')
    # Verified fix: a plain-http redirect_uri breaks the SPA callback chain and
    # the OAuth code is never exchanged. Always upgrade it to https.
    if redirect_uri.startswith('http://') and cfg['base'].startswith('https://'):
        redirect_uri = 'https://' + redirect_uri[len('http://'):]
        print('[login] upgraded http:// redirect_uri to https:// (required by the SPA callback)',
              file=sys.stderr, flush=True)

    # client_id is a PUBLIC OAuth identifier, not a secret; still allow runtime
    # injection (profile's client_id_env or ${VAR}) so a site-specific value
    # never has to be committed to a public repo.
    client_id = (os.environ.get(auth.get('client_id_env', 'SSO_CLIENT_ID'))
                 or expand(auth.get('client_id', ''))
                 or '')
    authorize = (auth.get('authorize_url', '') + '?response_type=code&client_id='
                 + quote(client_id, safe='')
                 + '&redirect_uri=' + quote(redirect_uri, safe=''))
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise SsoError('playwright is required for login (already used by '
                       'zju_sd_fetch.py): pip install playwright')

    token_key = auth.get('token_key', 'token')
    storage = auth.get('token_storage', 'localStorage')
    profile = a.profile or os.path.join(os.path.expanduser('~'),
                                        '.campus_sso_profile')
    os.makedirs(profile, exist_ok=True)
    last = None
    for attempt in range(1, 3):
        try:
            with sync_playwright() as p:
                ctx = p.chromium.launch_persistent_context(
                    profile, channel='chrome', headless=not a.headed, args=ARGS,
                    accept_downloads=True, locale='zh-CN',
                    viewport={'width': 1440, 'height': 900})
                ctx.add_init_script(
                    "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
                page.goto(authorize, wait_until='domcontentloaded', timeout=60000)
                page.fill(auth.get('username_selector', '#username'), user)
                page.fill(auth.get('password_selector', '#password'), pwd)
                try:
                    page.click(auth.get('submit_selector', '#dl'), timeout=5000)
                except Exception:
                    page.keyboard.press('Enter')
                page.wait_for_function(
                    "(k) => !!(k.indexOf('localStorage')===0"
                    " ? localStorage.getItem(k.split(':').pop())"
                    " : sessionStorage.getItem(k.split(':').pop()))",
                    arg=storage + ':' + token_key, timeout=90000)
                expr = ("(k) => k.indexOf('localStorage')===0"
                        " ? localStorage.getItem(k.split(':').pop())"
                        " : sessionStorage.getItem(k.split(':').pop())")
                token = page.evaluate(expr, storage + ':' + token_key)
                cookies = ctx.cookies()
                ctx.close()
            if not token:
                raise AuthError('SSO completed but no token appeared in %s[%s]'
                                % (storage, token_key), url=authorize)
            save_session(session_path, {
                'profile': cfg['name'], 'token': token, 'cookies': cookies,
                'token_storage': storage, 'token_key': token_key,
                'obtained_at': time.strftime('%Y-%m-%dT%H:%M:%S')})
            save_json(a.output, {'profile': cfg['name'], 'logged_in': True,
                                 'session_file': session_path,
                                 'token_key': token_key, 'cookies': len(cookies)})
            print('login: OK -> %s (session: %s)' % (a.output, session_path))
            return 0
        except Exception as e:
            last = e
            print('[login] attempt %d failed: %s' % (attempt, str(e)[:200]),
                  file=sys.stderr, flush=True)
            time.sleep(3)
    raise AuthError('login failed after 2 attempts: %s' % str(last)[:200],
                    url=authorize)


# ----------------------------------------------------------------- locate
def cmd_locate(a):
    cfg, _, session = ctx_of(a)
    meta, hub_ctx = read_metadata(cfg, session, a.resource_id)
    mc, hc = meta_client(cfg, session), hub_client(cfg, hub_ctx)

    registered, reg_dirs = {}, []
    if cfg.get('tree_api'):
        url = urljoin(cfg['base'],
                      expand(cfg['tree_api'], resource_id=a.resource_id))
        collect_tree(mc.get(url).json(), registered, reg_dirs)

    files, dirs = [], []

    def walk(path):
        m = hc.get(contents_url(cfg, hub_ctx, path)).json()
        if m.get('type') == 'directory':
            if path:
                dirs.append(path)
            for ch in m.get('content') or []:
                p = ch.get('path') or (path + '/' + ch['name']).strip('/')
                if ch.get('type') == 'directory':
                    walk(p)
                else:
                    files.append({'path': p,
                                  'size': registered.get(p) or ch.get('size'),
                                  'type': ch.get('type'), 'via': 'contents'})
        else:
            files.append({'path': path, 'size': registered.get(path) or m.get('size'),
                          'type': m.get('type'), 'via': 'contents'})

    walk('')

    # getattr: locate is also invoked programmatically with a bare Namespace.
    big = getattr(a, 'big_threshold', None)
    if big is None:
        big = cfg.get('big_threshold', 8 * 1024 * 1024)
    for f in files:
        if f.get('size') and f['size'] >= big:
            f['via'] = 'files'

    # Registered-but-hidden files (e.g. __pycache__/*.pyc) are reachable only
    # through the raw /files/ endpoint, not through the directory listing.
    seen = set(f['path'] for f in files)
    for p, size in registered.items():
        if p not in seen:
            files.append({'path': p, 'size': size, 'type': 'file',
                          'via': 'files', 'gap': True})
    for d in reg_dirs:
        if d not in dirs:
            dirs.append(d)

    fields = cfg.get('metadata_fields', {}) or {}
    out = {'profile': cfg['name'], 'resource_id': a.resource_id,
           'display_name': meta.get(fields.get('display_name', 'display_name'))
           or cfg['name'],
           'hub_name': hub_ctx.get('hub_name', ''),
           'big_threshold': big,
           'registered_count': len(registered),
           'files': sorted(files, key=lambda x: x['path']),
           'dirs': sorted(dirs)}
    save_json(a.output, out)
    print('locate: %d files / %d dirs (registered %d, gaps %d) -> %s'
          % (len(files), len(dirs), len(registered),
             sum(1 for f in files if f.get('gap')), a.output))
    return 0


# ----------------------------------------------------------------- fetch
def fetch_content(client, cfg, rel, hub_ctx):
    url = contents_url(cfg, hub_ctx, rel)
    m = client.get(url).json()
    t, c, fmt = m.get('type'), m.get('content'), m.get('format')
    if t == 'notebook':
        return (json.dumps(c, ensure_ascii=False, indent=1) + '\n').encode('utf-8')
    if t == 'file':
        if fmt == 'base64' or (isinstance(c, str) and
                               m.get('mimetype') in ('application/pdf',
                                                     'application/octet-stream')):
            return base64.b64decode(c)
        return (c if isinstance(c, str)
                else json.dumps(c, ensure_ascii=False)).encode('utf-8')
    raise ResourceError('unknown content type %r' % t, url=url,
                        body=json.dumps(m, ensure_ascii=False)[:200])


def fetch_raw(cfg, session, hub_ctx, client, rel, expected, out, tries=5):
    """Raw stream + Range resume + container self-healing."""
    url = files_url(cfg, hub_ctx, rel)
    dst = os.path.join(out, *rel.split('/'))
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    have = os.path.getsize(dst) if os.path.isfile(dst) else 0
    if expected and have == expected:
        return {'bytes': have, 'skipped': True}
    hdr = dict(hub_headers(cfg, hub_ctx))
    hdr['Accept'] = '*/*'
    for attempt in range(1, tries + 1):
        if have:
            hdr['Range'] = 'bytes=%d-' % have
        else:
            hdr.pop('Range', None)
        r = None
        try:
            r = stream_get(client, url, hdr,
                           (25, cfg.get('stream_timeout', 180)))
            if r.status_code == 503:
                r.close()
                print('  [fetch] 503 on %s, restarting container' % rel[-60:],
                      file=sys.stderr, flush=True)
                restart_hub(cfg, session, hub_ctx)
                time.sleep(3)
                continue
            code = r.status_code
            # Transient gateway errors are retried here because stream_get()
            # bypasses HttpClient.request() and therefore its backoff loop.
            if code in (429, 500, 502, 503, 504):
                r.close()
                print('  [fetch] HTTP %d on %s, %s'
                      % (code, rel[-60:],
                         'restarting container' if code == 503 else 'retrying'),
                      file=sys.stderr, flush=True)
                if code == 503:
                    restart_hub(cfg, session, hub_ctx)
                time.sleep(3)
                continue
            if code not in (200, 206):
                raise ResourceError('raw stream HTTP %d' % code,
                                    url=url, status=code,
                                    body=r.text[:200])
            mode = 'ab' if (code == 206 and have) else 'wb'
            if mode == 'wb':
                have = 0
            # 'wb' / 'ab' are already binary modes: iter_content yields bytes.
            with open(dst, mode) as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
                        have += len(chunk)
            r.close()
            if not expected or have == expected:
                return {'bytes': have, 'skipped': False}
            print('  [fetch] %s incomplete %d/%d, resuming'
                  % (rel[-60:], have, expected), file=sys.stderr, flush=True)
            time.sleep(2)
        except SsoError:
            if r is not None:
                r.close()
            raise
        except Exception as e:
            if r is not None:
                r.close()
            print('  [fetch] %s error %s, retrying' % (rel[-60:], type(e).__name__),
                  file=sys.stderr, flush=True)
            have = os.path.getsize(dst) if os.path.isfile(dst) else 0
            time.sleep(3)
    raise ResourceError('raw fetch exhausted after %d attempts' % tries,
                        url=url, body='have=%d expected=%s' % (have, expected))


def cmd_fetch(a):
    cfg, _, session = ctx_of(a)
    tree = json.load(open(a.tree, encoding='utf-8'))
    resource_id = a.resource_id or tree['resource_id']
    _, hub_ctx = read_metadata(cfg, session, resource_id)
    hc = hub_client(cfg, hub_ctx)
    os.makedirs(a.out, exist_ok=True)
    for d in tree.get('dirs', []):
        os.makedirs(os.path.join(a.out, *d.split('/')), exist_ok=True)

    manifest, errors = [], []
    for f in tree['files']:
        rel, expected, via = f['path'], f.get('size'), f.get('via', 'contents')
        dst = os.path.join(a.out, *rel.split('/'))
        if expected and os.path.isfile(dst) and os.path.getsize(dst) == expected:
            manifest.append({'path': rel, 'bytes': expected, 'expected': expected,
                             'via': via, 'size_match': True, 'skipped': True})
            continue
        skipped = False
        try:
            if via == 'files':
                res = fetch_raw(cfg, session, hub_ctx, hc, rel, expected, a.out)
                size = res['bytes']
                skipped = bool(res.get('skipped'))
            else:
                data = fetch_content(hc, cfg, rel, hub_ctx)
                atomic_write(dst, data, binary=True)
                size = len(data)
            ok = (expected is None) or (size == expected) or f.get('type') == 'notebook'
            manifest.append({'path': rel, 'bytes': size, 'expected': expected,
                             'via': via, 'size_match': bool(ok),
                             'skipped': skipped})
            print('  %-64s %10d B %s' % (rel[-64:], size, 'OK' if ok else 'SIZE-DIFF'),
                  flush=True)
        except SsoError as e:
            errors.append({'path': rel, 'reason': str(e)})
            print('  !! FAIL %-58s %s' % (rel[-58:], str(e)[:140]),
                  file=sys.stderr, flush=True)
    save_json(a.output, {'profile': cfg['name'], 'resource_id': resource_id,
                         'out': a.out, 'manifest': manifest, 'errors': errors})
    print('fetch: %d ok / %d errors -> %s'
          % (len(manifest), len(errors), a.output))
    return 0 if not errors else 1


# ----------------------------------------------------------------- verify
def cmd_verify(a):
    tree = json.load(open(a.tree, encoding='utf-8'))
    mf = json.load(open(a.manifest, encoding='utf-8'))
    rows, bad = [], []
    for m in mf['manifest']:
        rel = m['path']
        p = os.path.join(a.out, *rel.split('/'))
        status = 'OK'
        if not os.path.isfile(p):
            status = '缺失'
        elif os.path.getsize(p) != m['bytes']:
            status = '字节不符'
        elif rel.lower().endswith('.pdf') and open(p, 'rb').read(4) != b'%PDF':
            status = 'PDF 头错误'
        elif rel.endswith('.ipynb'):
            try:
                nb = json.load(open(p, encoding='utf-8'))
                if nb.get('nbformat') != 4:
                    status = 'nbformat=%s' % nb.get('nbformat')
                else:
                    status = 'OK (nbformat=4, %d cells)' % len(nb.get('cells') or [])
            except ValueError as e:
                status = 'JSON 错误 ' + str(e)[:60]
        if not status.startswith('OK'):
            bad.append((rel, status))
        rows.append({'path': rel, 'bytes': m['bytes'], 'status': status})
    leftovers = [f['path'] for f in tree['files']
                 if f['path'] not in set(m['path'] for m in mf['manifest'])]
    save_json(a.output, {'rows': rows, 'bad': bad, 'leftovers': leftovers,
                         'errors': mf.get('errors', [])})
    print('verify: %d files, %d bad, %d leftovers -> %s'
          % (len(rows), len(bad), len(leftovers), a.output))
    return 0 if not bad and not leftovers else 1


# ----------------------------------------------------------------- report
def tree_lines(root, prefix=''):
    out = []
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return out
    dirs = [n for n in names if os.path.isdir(os.path.join(root, n))]
    files = [n for n in names if os.path.isfile(os.path.join(root, n))]
    entries = dirs + files
    for i, n in enumerate(entries):
        p = os.path.join(root, n)
        last = (i == len(entries) - 1)
        branch = '└── ' if last else '├── '
        if os.path.isdir(p):
            out.append(prefix + branch + n + '/')
            out += tree_lines(p, prefix + ('    ' if last else '│   '))
        else:
            out.append(prefix + branch + n + '  (%d B)' % os.path.getsize(p))
    return out


def cmd_report(a):
    tree = json.load(open(a.tree, encoding='utf-8'))
    mf = json.load(open(a.manifest, encoding='utf-8'))
    vf = json.load(open(a.verify, encoding='utf-8')) if a.verify else {'rows': [], 'bad': [], 'leftovers': []}
    status = {r['path']: r['status'] for r in vf['rows']}
    lines = ['# %s — 资源获取报告' % tree.get('display_name', tree['profile']), '',
             '- Profile: `%s`' % tree['profile'],
             '- Resource ID: `%s`' % tree['resource_id'],
             '- 目录数: %d' % len(tree.get('dirs', [])),
             '- 文件数: %d' % len(mf['manifest']),
             '- 总字节: %d' % sum(m['bytes'] for m in mf['manifest']),
             '- 校验异常: %d' % len(vf.get('bad', [])),
             '- 下载错误: %d' % len(mf.get('errors', [])), '',
             '## 文件清单', '',
             '| 相对路径 | 字节 | 平台 size | 通道 | 状态 |',
             '| --- | ---: | ---: | --- | --- |']
    for m in sorted(mf['manifest'], key=lambda x: x['path']):
        lines.append('| `%s` | %d | %s | %s | %s |'
                     % (m['path'], m['bytes'], m.get('expected'), m['via'],
                        status.get(m['path'], '未校验')))
    lines += ['', '## 目录结构', '', '```text', a.out.rstrip('/\\') + '/']
    lines += tree_lines(a.out)
    lines += ['```', '']
    if vf.get('leftovers'):
        lines += ['## 未获取', ''] + ['- `%s`' % p for p in vf['leftovers']] + ['']
    if mf.get('errors'):
        lines += ['## 错误', '', '```json',
                  json.dumps(mf['errors'], ensure_ascii=False, indent=2),
                  '```', '']
    atomic_write(a.output, '\n'.join(lines) + '\n')
    print('report -> %s' % a.output)
    return 0


# ----------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(prog='sso_fetch')
    sub = ap.add_subparsers(dest='cmd', required=True)

    def common(p):
        p.add_argument('--config', required=True, help='JSON site profile')
        p.add_argument('--output', required=True, help='write result to this file')
        p.add_argument('--session', help='override session file from the profile')

    p = sub.add_parser('check', help='probe login state')
    common(p)
    p.add_argument('--resource-id')
    p.set_defaults(func=cmd_check)

    p = sub.add_parser('login', help='obtain an SSO session via real Chrome')
    common(p)
    p.add_argument('--headed', action='store_true',
                   help='headed browser (needed when a human must solve a captcha)')
    p.add_argument('--profile', help='persistent Chrome profile dir')
    p.add_argument('--user-env', help='env var holding the username')
    p.add_argument('--pass-env', help='env var holding the password')
    p.set_defaults(func=cmd_login)

    p = sub.add_parser('locate', help='enumerate the protected resource tree')
    common(p)
    p.add_argument('--resource-id', required=True)
    p.add_argument('--big-threshold', type=int,
                   help='bytes; files >= this use the raw stream endpoint')
    p.set_defaults(func=cmd_locate)

    p = sub.add_parser('fetch', help='download all files, preserving paths')
    common(p)
    p.add_argument('--tree', required=True, help='tree.json from locate')
    p.add_argument('--out', required=True, help='output directory')
    p.add_argument('--resource-id')
    p.add_argument('--big-threshold', type=int,
                   help='bytes; files >= this use the raw stream endpoint')
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser('verify', help='validate downloaded files')
    common(p)
    p.add_argument('--tree', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--out', required=True)
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser('report', help='write a Markdown manifest')
    common(p)
    p.add_argument('--tree', required=True)
    p.add_argument('--manifest', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--verify', help='verify.json to fold status into the report')
    p.set_defaults(func=cmd_report)

    a = ap.parse_args()
    try:
        return a.func(a)
    except SsoError as e:
        print('ERROR: ' + str(e), file=sys.stderr, flush=True)
        return 1
    except (OSError, ValueError) as e:
        print('ERROR: %s: %s' % (type(e).__name__, str(e)[:300]),
              file=sys.stderr, flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
