# -*- coding: utf-8 -*-
"""Shared infrastructure for campus-SSO-protected resource fetching.

Uses only dependencies already present in this repository: ``requests`` plus
the Python standard library.  No new packages, no new runtimes.

Provides:
  * file-lock rate limiting shared across processes (default 1 req/s per host)
  * an HTTP client with exponential-backoff retry and actionable error classes
  * login-state probing and atomic session persistence
  * config / template helpers reused by every ``sso_fetch.py`` subcommand
"""
import json
import os
import re
import sys
import tempfile
import time
import uuid
from urllib.parse import urlparse

import requests

DEFAULT_GAP = 1.0          # documented fallbacks: no public rate limit for these campus APIs
DEFAULT_RETRIES = 4
DEFAULT_TIMEOUT = 45
RETRY_STATUS = (429, 500, 502, 503, 504)
LOGIN_STATUS = (401, 403)


class SsoError(Exception):
    """Base error. The message always carries the URL, status and body excerpt."""

    def __init__(self, message, url='', status=None, body=''):
        self.url = url
        self.status = status
        self.body = body
        parts = [message]
        if url:
            parts.append('url=' + str(url))
        if status is not None:
            parts.append('status=' + str(status))
        if body:
            parts.append('body=' + str(body)[:300])
        super(SsoError, self).__init__(' | '.join(parts))


class AuthError(SsoError):
    """Not logged in, session expired, wrong credentials, or wrong IdP."""


class RateLimitError(SsoError):
    """HTTP 429 after retries were exhausted."""


class ResourceError(SsoError):
    """Protected resource could not be located or downloaded."""


class _Throttle(object):
    """Cross-process minimum gap per host, via an O_EXCL lock + shared stamp.

    Complexity: O(1) per call, contention polled at 50 ms.  The lock serialises
    access; the *timestamp itself* lives in a persistent stamp file so every
    process reads/writes the same value.  An in-process dict would let N
    concurrent processes each fire 1 req/s and collectively exceed the budget.

    ``time.time()`` (wall clock) is required here: ``time.monotonic()`` is
    per-process and cannot be shared.  A backwards clock jump only lengthens
    one gap (capped at ``self.gap``) and self-corrects on the next call.
    """

    def __init__(self, gap=DEFAULT_GAP, prefix='campus_sso'):
        self.gap = float(gap)
        self.prefix = prefix

    def wait(self, url):
        host = urlparse(url).netloc or 'default'
        key = os.path.join(tempfile.gettempdir(),
                           '%s_%s' % (self.prefix, re.sub(r'\W', '_', host)))
        lock, stamp = key + '.lock', key + '.stamp'
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                time.sleep(0.05)
            except OSError:
                return
        try:
            try:
                with open(stamp, 'r') as f:
                    last = float(f.read().strip())
            except (OSError, ValueError):
                last = 0.0
            delay = min(self.gap, self.gap - (time.time() - last))
            if delay > 0:
                time.sleep(delay)
            # Written while holding the lock, so the next process sees this
            # exact timestamp instead of its own private clock.
            with open(stamp, 'w') as f:
                f.write('%.6f' % time.time())
        finally:
            os.close(fd)
            os.unlink(lock)


class HttpClient(object):
    """requests.Session + throttling + retry with exponential backoff.

    Non-retriable 4xx errors include the response body in the message: campus
    gateways put the actionable reason there ("not logged in", "no permission").
    """

    def __init__(self, gap=DEFAULT_GAP, retries=DEFAULT_RETRIES,
                 timeout=DEFAULT_TIMEOUT, headers=None, verify=True):
        self.throttle = _Throttle(gap)
        self.retries = int(retries)
        self.timeout = timeout
        self.s = requests.Session()
        self.s.verify = verify
        self.s.headers.update(headers or {})

    def request(self, method, url, **kw):
        last = None
        kw.setdefault('timeout', self.timeout)
        for attempt in range(1, self.retries + 1):
            self.throttle.wait(url)
            try:
                r = self.s.request(method, url, **kw)
            except requests.RequestException as e:
                last = SsoError('network error: ' + type(e).__name__, url=url)
                if attempt < self.retries:
                    self._backoff(attempt, url, str(last))
                    continue
                raise last
            body = r.text[:300] if r.content else ''
            if r.status_code in RETRY_STATUS:
                if r.status_code == 429:
                    last = RateLimitError('rate limited', url=url,
                                          status=r.status_code, body=body)
                else:
                    last = SsoError('transient HTTP error', url=url,
                                    status=r.status_code, body=body)
                if attempt < self.retries:
                    self._backoff(attempt, url, str(last))
                    continue
                raise last
            if r.status_code >= 400:
                raise SsoError('HTTP error', url=url, status=r.status_code, body=body)
            return r
        raise last or SsoError('request exhausted', url=url)

    def _backoff(self, attempt, url, detail):
        delay = min(30.0, 2 ** (attempt - 1))
        print('[retry %d/%d] %.1fs %s :: %s'
              % (attempt, self.retries, delay, url[:110], detail[:120]),
              file=sys.stderr, flush=True)
        time.sleep(delay)

    def get(self, url, **kw):
        return self.request('GET', url, **kw)

    def post(self, url, **kw):
        return self.request('POST', url, **kw)


def load_config(path):
    """Load a JSON site profile. Raises SsoError with the offending key."""
    try:
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise SsoError('cannot read config', body=str(e))
    for key in ('name', 'base'):
        if not cfg.get(key):
            raise SsoError('config missing required key: ' + key)
    return cfg


def atomic_write(path, data, binary=False):
    """Write via a temporary file then os.replace, so a crash never leaves a
    half-written token/session/manifest file behind."""
    d = os.path.dirname(os.path.abspath(path))
    if d and not os.path.isdir(d):
        os.makedirs(d, exist_ok=True)
    tmp = path + '.%s.tmp' % uuid.uuid4().hex[:8]
    if binary:
        with open(tmp, 'wb') as f:
            f.write(data)
    else:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(data)
    os.replace(tmp, path)


def save_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2) + '\n')


def load_session(path):
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (ValueError, OSError) as e:
        raise AuthError('session file unreadable; run `login` again', body=str(e))


def save_session(path, data):
    save_json(path, data)


def probe_login(client, url, ok_marker='', login_marker='', headers=None):
    """Structured login-state report. Never raises on 401/403/redirect.

    Returns {logged_in, status, url, final_url, reason, body_excerpt}.
    """
    out = {'logged_in': False, 'status': None, 'url': url,
           'final_url': '', 'reason': '', 'body_excerpt': ''}
    client.throttle.wait(url)
    try:
        r = client.s.request('GET', url, headers=headers or {},
                             timeout=client.timeout, allow_redirects=True)
    except requests.RequestException as e:
        out['reason'] = 'network error: ' + type(e).__name__
        return out
    out['status'] = r.status_code
    out['final_url'] = r.url
    body = r.text[:400] if r.content else ''
    out['body_excerpt'] = body[:200]
    if r.status_code in LOGIN_STATUS:
        out['reason'] = 'HTTP %d (not authenticated or forbidden)' % r.status_code
        return out
    if 'login' in (r.url or '').lower() or (login_marker and login_marker in body):
        out['reason'] = 'redirected to a login page'
        return out
    if r.status_code != 200:
        out['reason'] = 'HTTP %d' % r.status_code
        return out
    if ok_marker and ok_marker not in body:
        out['reason'] = 'expected marker %r not found' % ok_marker
        return out
    out['logged_in'] = True
    out['reason'] = 'OK'
    return out


def expand(template, **ctx):
    """Fill ``{placeholders}`` and ``${ENV_VARS}`` in a config template.

    ``{name}`` resolves from the call context, ``${NAME}`` from the process
    environment.  Missing values become ``''`` so a literal placeholder can
    never leak into a URL or header.  This lets operators keep site-specific
    identifiers (e.g. an OAuth client_id) out of a public checkout entirely.
    """
    def repl(m):
        if m.group(1) is not None:
            return str(ctx.get(m.group(1), ''))
        return os.environ.get(m.group(2), '')

    return re.sub(r'\{(\w+)\}|\$\{(\w+)\}', repl, template or '')


def urljoin(base, path):
    if not path:
        return base
    if path.startswith('http://') or path.startswith('https://'):
        return path
    return base.rstrip('/') + '/' + path.lstrip('/')


def quote_path(value):
    """URL-encode a path segment but keep '/' so directory structure survives."""
    from urllib.parse import quote
    return quote(str(value), safe='/')
