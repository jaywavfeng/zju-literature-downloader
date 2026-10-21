# -*- coding: utf-8 -*-
"""ZJU CAS (浙大通行证) -> ScienceDirect institutional login -> download PDFs.

Built-in Cloudflare/Turnstile handling:
  * PREVENT  - ZJU-campus egress (no foreign VPN), real Chrome, webdriver hidden,
               persistent profile, cookie warm-up, human-like pacing, session reuse.
  * RECOVER  - detect the challenge; try auto-click of the Turnstile box; if that is
               not possible keep the headed browser open and WAIT for a human to
               click it (human-in-the-loop), then continue automatically.

Usage:
  python zju_sd_fetch.py --pii S2211285525009644 --name "30-xxx.pdf" --out DIR --profile DIR
  python zju_sd_fetch.py --list items.json --out DIR --profile DIR

items.json: [{"pii": "...", "filename": "1-xxx.pdf"}, ...]
Env: ZJU_USER, ZJU_PASS
"""
import argparse
import base64
import json
import os
import random
import re
import sys
import time

from playwright.sync_api import sync_playwright

USER = os.environ.get('ZJU_USER', '')
PWD = os.environ.get('ZJU_PASS', '')

ARGS = ['--disable-blink-features=AutomationControlled',
        '--exclude-switches=enable-automation',
        '--no-first-run', '--no-default-browser-check',
        '--disable-features=IsolateOrigins,site-per-process']

INIT_JS = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
window.chrome = window.chrome || {runtime: {}};
"""

JS_FETCH = """
async (url) => {
    try {
        const r = await fetch(url, {credentials: 'include', redirect: 'follow'});
        const ct = r.headers.get('content-type') || '';
        const buf = await r.arrayBuffer();
        const bytes = new Uint8Array(buf);
        let s = ''; const CH = 0x8000;
        for (let i = 0; i < bytes.length; i += CH) {
            s += String.fromCharCode.apply(null, bytes.subarray(i, i + CH));
        }
        return {ok: true, ct, len: bytes.length, data: btoa(s)};
    } catch (e) { return {ok: false, err: String(e)}; }
}
"""

JS_CLICK_ZJU = """
() => {
    const forms = [...document.querySelectorAll('form')];
    for (const f of forms) {
        const hid = f.querySelector('input[name="els_institution"]');
        if (hid && decodeURIComponent(hid.value).includes('idp.zju.edu.cn')) {
            const btn = f.querySelector('button[name="action"]') || f.querySelector('button');
            if (btn) { btn.click(); return btn.textContent.trim().slice(0, 60); }
        }
    }
    return null;
}
"""

JS_CLICK_REMEMBERED = r"""
() => {
    // Only 浙江大学 / Zhejiang University Library / *.zju.edu.cn.
    // Explicitly excludes 浙江工业大学, 浙江财经大学, "Zhejiang University of Technology" ...
    const isZJU = t => /浙江大学/.test(t) ||
                       /Zhejiang University Library/i.test(t) ||
                       /zju\.edu\.cn/i.test(t);
    const els = [...document.querySelectorAll('button, a, input[type="submit"], span[role="button"]')];
    for (const e of els) {
        const t = (e.textContent || e.value || '');
        if (t.length < 140 && isZJU(t) && !/zjut|zufe|工业|财经|理工|师范/i.test(t)) {
            e.click();
            return t.trim().slice(0, 70);
        }
    }
    return null;
}
"""

JS_SUBMIT = """
() => {
    const act = f => f.getAttribute('action') || '';
    const forms = [...document.querySelectorAll('form')];
    for (const f of forms) {
        const names = [...f.querySelectorAll('input')].map(i => i.name || '');
        if (names.includes('SAMLResponse') || names.includes('SAMLRequest')) {
            f.submit(); return 'saml:' + act(f).slice(0, 50);
        }
    }
    for (const f of forms) {
        if (f.name === 'instDataForm' || act(f).includes('SHIRE')) {
            f.submit(); return 'shire:' + act(f).slice(0, 50);
        }
    }
    return null;
}
"""

JS_CHALLENGE_STATE = r"""
() => {
    const t = document.body ? document.body.innerText : '';
    const html = document.documentElement ? document.documentElement.innerHTML : '';
    return {
        robot: /Are you a robot|Enter the characters|cf-turnstile|challenge-platform/i.test(t + html),
        zju: /Zhejiang University Library/i.test(t),
        iframes: [...document.querySelectorAll('iframe')].map(f => (f.src || '').slice(0, 90))
    };
}
"""


def log(msg):
    print(msg, flush=True)


def system_proxy_url():
    """Read the WinINET system proxy (the ladder's local port), e.g. 127.0.0.1:7897."""
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                           r'Software\Microsoft\Windows\CurrentVersion\Internet Settings')
        enable = winreg.QueryValueEx(k, 'ProxyEnable')[0]
        server = winreg.QueryValueEx(k, 'ProxyServer')[0]
        if enable and server:
            return server if '://' in server else 'http://' + server
    except Exception:
        pass
    return None


def launch_ctx(p, profile, egress, headless=False):
    """Launch a persistent-context Chrome bound to one egress.

    egress='direct' -> --no-proxy-server   (campus/ZJU network: needed for CAS login)
    egress='proxy'  -> --proxy-server=<system proxy>  (the ladder: cleaner IP, better for PDFs)

    Nothing on the system is modified and the VPN client is never touched, so there
    is nothing to "restore" afterwards.
    """
    args = list(ARGS)
    if egress == 'direct':
        args.append('--no-proxy-server')
    elif egress == 'proxy':
        url = system_proxy_url()
        if url:
            args.append('--proxy-server=' + url)
        else:
            log('  [egress] 未检测到系统代理，按直连启动')
    os.makedirs(profile, exist_ok=True)
    return p.chromium.launch_persistent_context(
        profile, channel='chrome', headless=headless, args=args,
        accept_downloads=True, locale='zh-CN', timezone_id='Asia/Shanghai',
        viewport={'width': 1440, 'height': 900})


def human_pause(lo=1.2, hi=3.0):
    time.sleep(random.uniform(lo, hi))


def hitl_challenge(page, timeout=240, auto_click=True):
    """Handle a Cloudflare challenge: try auto-click, else wait for a human."""
    log('  [challenge] 检测到人机验证，尝试自动通过…')
    deadline = time.time() + timeout
    announced = False
    clicked = False
    while time.time() < deadline:
        try:
            st = page.evaluate(JS_CHALLENGE_STATE)
        except Exception:
            time.sleep(3)
            continue
        if not st['robot']:
            log('  [challenge] 已通过')
            return True
        if auto_click and not clicked:
            for sel in ['input[type="checkbox"]', '.cb-lb input', '#cf-turnstile input',
                        'span.cb-wrapper', '.ctp-checkbox-label']:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        el.click()
                        clicked = True
                        log('  [challenge] 已自动点击验证框 (%s)' % sel)
                        break
                except Exception:
                    pass
            for fr in page.frames:
                try:
                    box = fr.query_selector('input[type="checkbox"], .ctp-checkbox-label, span.cb-wrapper')
                    if box and box.is_visible():
                        box.click()
                        clicked = True
                        log('  [challenge] 已自动点击 iframe 内验证框')
                        break
                except Exception:
                    pass
        if not announced:
            announced = True
            log('  [challenge] >>> 需要人工：请在弹出的浏览器窗口里勾选 "I am human"，')
            log('                完成后脚本会自动继续（最多等待 %d 秒）' % timeout)
        page.wait_for_timeout(2500)
    st = page.evaluate(JS_CHALLENGE_STATE)
    return not st['robot']


def warmup(page):
    """Land on the homepage first so cookies/consent settle before deep links."""
    try:
        page.goto('https://www.sciencedirect.com/', wait_until='domcontentloaded', timeout=120000)
        page.wait_for_timeout(2500)
        for sel in ['button#onetrust-accept-btn-handler', 'button:has-text("Accept all")',
                    'button:has-text("接受所有")']:
            try:
                b = page.query_selector(sel)
                if b and b.is_visible():
                    b.click()
                    log('  [warmup] 已接受 cookie 提示')
                    break
            except Exception:
                pass
        human_pause(1.5, 3.0)
    except Exception as e:
        log('  [warmup] 跳过：%s' % type(e).__name__)


def login(page, pii):
    """Institution selection -> ZJU CAS -> SAML -> back to ScienceDirect."""
    page.goto('https://www.sciencedirect.com/user/institution/login?targetUrl=/science/article/pii/' + pii,
              wait_until='domcontentloaded', timeout=120000)
    page.wait_for_timeout(2500)
    st = page.evaluate(JS_CHALLENGE_STATE)
    if st['robot']:
        if not hitl_challenge(page):
            return False, '人机验证未通过'

    for _ in range(16):
        url = page.url
        # guard: never hand credentials to another university's IdP
        if re.search(r'(zjut|zufe|zstu|zjnu|zju\.edu\.cn)', url) and 'zju' not in url:
            log('  [login] 误入他校登录页 (%s)，回退重试' % url[:60])
            page.go_back()
            page.wait_for_timeout(2500)
            continue
        if 'zjuam.zju.edu.cn' in url:
            page.fill('#username', USER)
            page.fill('#password', PWD)
            human_pause(0.3, 0.8)
            try:
                page.click('#dl')
            except Exception:
                page.keyboard.press('Enter')
            page.wait_for_timeout(4500)
            continue
        if 'sciencedirect.com' in url and 'id.elsevier' not in url:
            return True, ''

        acted = False
        # (a) exact match: result form whose hidden value points at idp.zju.edu.cn
        try:
            if page.evaluate(JS_CLICK_ZJU):
                log('  [login] 已选择 浙江大学（精确匹配 idp.zju.edu.cn）')
                acted = True
        except Exception:
            acted = True
        # (b) remembered-organization page: click the ZJU entry
        if not acted:
            try:
                hit = page.evaluate(JS_CLICK_REMEMBERED)
                if hit:
                    log('  [login] 点击已记住的组织：%s' % hit[:40])
                    acted = True
            except Exception:
                acted = True
        # (c) search box: type and search
        if not acted:
            try:
                if page.query_selector('#bdd-email'):
                    page.fill('#bdd-email', 'Zhejiang University')
                    human_pause(0.4, 1.0)
                    try:
                        page.click('#bdd-els-searchBtn')
                    except Exception:
                        page.keyboard.press('Enter')
                    log('  [login] 已搜索机构')
                    acted = True
            except Exception:
                pass
        # (d) SAML auto-submit pages
        if not acted:
            try:
                if page.evaluate(JS_SUBMIT):
                    acted = True
            except Exception:
                acted = True
        page.wait_for_timeout(3000)
        if not acted:
            break
    ok = 'sciencedirect.com' in page.url and 'id.elsevier' not in page.url
    return ok, '' if ok else '登录流程未回到 ScienceDirect：' + page.url[:120]


def _try_fetch(page, url):
    """In-page XHR fetch of the pdfft URL."""
    res = page.evaluate(JS_FETCH, url)
    if not res.get('ok'):
        return None, 'fetch 失败：%s' % str(res.get('err'))[:60]
    data = base64.b64decode(res['data'])
    if data[:4] == b'%PDF' and len(data) > 15000:
        return data, ''
    return None, '非 PDF（%s, %d B）' % (res.get('ct', '?')[:24], len(data))


def _try_nav_download(ctx, url):
    """Top-level navigation: lets Chrome run Elsevier's /craft/challenge/pdf gate."""
    p2 = ctx.new_page()
    try:
        with p2.expect_download(timeout=70000) as dl:
            p2.goto(url, wait_until='domcontentloaded', timeout=70000)
        d = dl.value
        src = d.path()
        if src and os.path.exists(src):
            with open(src, 'rb') as f:
                return f.read(), ''
    except Exception:
        pass
    finally:
        try:
            p2.close()
        except Exception:
            pass
    return None, '导航未触发下载'


def fetch_pdf(page, pii, allow_hitl=True):
    art = 'https://www.sciencedirect.com/science/article/pii/' + pii
    page.goto(art, wait_until='domcontentloaded', timeout=120000)
    page.wait_for_timeout(2800)

    st = page.evaluate(JS_CHALLENGE_STATE)
    if st['robot']:
        if allow_hitl and hitl_challenge(page):
            page.wait_for_timeout(2000)
        else:
            return None, '文章页触发人机验证'
    link = page.evaluate("""(pii) => {
        const ls = [...document.querySelectorAll('a')]
            .map(a => a.getAttribute('href') || '')
            .filter(h => h.includes('pdfft') && h.includes(pii));
        return ls.length ? ls[0] : null;
    }""", pii)
    if not link:
        return None, '页面无 pdfft 链接（无全文权限）'
    url = link if link.startswith('http') else 'https://www.sciencedirect.com' + link

    # (A) plain XHR; if Elsevier serves its /craft/challenge/pdf gate, fall through
    data, why = _try_fetch(page, url)
    if data:
        return data, ''

    # (B) top-level navigation -> Chrome plays the challenge, then downloads the PDF
    data, why2 = _try_nav_download(page.context, url)
    if data:
        return data, ''

    # (C) the navigation usually plants a clearance cookie: retry the XHR
    page.wait_for_timeout(4000)
    data, why3 = _try_fetch(page, url)
    if data:
        return data, ''

    dbg = os.environ.get('ZJU_SD_DEBUG_DIR')
    if dbg:
        try:
            os.makedirs(dbg, exist_ok=True)
            r = page.evaluate(JS_FETCH, url)
            if r.get('ok'):
                with open(os.path.join(dbg, '%s_pdfft.html' % pii), 'wb') as f:
                    f.write(base64.b64decode(r['data']))
        except Exception:
            pass
    return None, 'PDF 端点被拦截 [%s | %s | %s]' % (why, why2, why3)


def save(buf, out_dir, filename):
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, filename)
    tmp = path + '.part'
    with open(tmp, 'wb') as f:
        f.write(buf)
    os.replace(tmp, path)
    return path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--pii')
    ap.add_argument('--name')
    ap.add_argument('--list')
    ap.add_argument('--out', required=True)
    ap.add_argument('--profile', help='persistent Chrome profile dir (recommended)')
    ap.add_argument('--headless', action='store_true')
    ap.add_argument('--no-hitl', action='store_true', help='disable human-in-the-loop waiting')
    ap.add_argument('--no-warmup', action='store_true', help='skip the homepage warm-up')
    ap.add_argument('--login-egress', choices=['direct', 'proxy'], default='direct',
                    help='egress used for the CAS login (zjuam is unreachable through a foreign VPN)')
    ap.add_argument('--download-egress', choices=['direct', 'proxy'], default='proxy',
                    help='egress used for article pages / PDFs')
    ap.add_argument('--min-gap', type=float, default=6.0,
                    help='minimum seconds between two article downloads (default 6)')
    a = ap.parse_args()

    if not USER or not PWD:
        log('ERROR: set ZJU_USER / ZJU_PASS')
        return 2

    items = json.load(open(a.list, encoding='utf-8')) if a.list else (
        [{'pii': a.pii, 'filename': a.name}] if a.pii else [])
    if not items:
        log('ERROR: give --pii/--name or --list')
        return 2

    hitl = not a.no_hitl and not a.headless
    profile = a.profile or os.path.join(os.path.expanduser('~'), '.zju_sd_profile')
    results = []
    with sync_playwright() as p:
        # ---- phase A: CAS login needs the campus/direct egress ----
        ctx = launch_ctx(p, profile, a.login_egress, a.headless)
        ctx.add_init_script(INIT_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        if not a.no_warmup:
            warmup(page)
        ok, why = login(page, items[0]['pii'])
        log('login: %s %s' % ('OK' if ok else 'FAIL', why))
        if not ok:
            ctx.close()
            return 3
        ctx.close()

        # ---- phase B: downloads may run on a different (cleaner) egress ----
        ctx = launch_ctx(p, profile, a.download_egress, a.headless)
        ctx.add_init_script(INIT_JS)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        for it in items:
            buf, why = fetch_pdf(page, it['pii'], allow_hitl=hitl)
            if buf:
                fname = it.get('filename') or ('%s.pdf' % it['pii'])
                path = save(buf, a.out, fname)
                log('[OK]   %-58s %6.1f KB' % (fname[:58], len(buf) / 1024))
                results.append({'pii': it['pii'], 'file': path, 'bytes': len(buf), 'ok': True})
            else:
                log('[FAIL] %-58s %s' % (it['pii'], why))
                results.append({'pii': it['pii'], 'ok': False, 'reason': why})
            # pacing: randomised gap, plus a longer breather every 5 articles
            human_pause(a.min_gap, a.min_gap * 1.8)
            if (results and len(results) % 5 == 0):
                log('  [pace] 已下 5 篇，冷却 25 秒…')
                human_pause(20, 30)
        ctx.close()

    ok_n = sum(1 for r in results if r['ok'])
    log('\nsummary: %d ok / %d fail' % (ok_n, len(results) - ok_n))
    return 0 if ok_n else 4


if __name__ == '__main__':
    sys.exit(main())
