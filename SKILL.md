---
name: zju-literature-downloader
description: >-
  Batch-download journal articles for a reference list by leveraging Zhejiang
  University institutional access (浙大通行证 CAS SSO, ZJU WebVPN, library
  subscriptions). Walks a source waterfall - open access, publisher direct,
  repository copies, campus-IP proxy, ScienceDirect Shibboleth login, mirror
  fallback - with Cloudflare/Turnstile prevention and human-in-the-loop
  recovery, then renames files to "<reference number>-<title>.pdf" and reports
  anything that still needs manual downloading.
---

# ZJU Literature Downloader

## Overview

Given a reference list (a `文献.txt` with `[1] ... DOI: ...` blocks, a `.docx`
reference section, or a JSON list), download **every** article into one folder,
named `<编号>-<标题>.pdf`, and produce a manifest plus a manual-download list for
whatever the automation could not obtain.

ZJU buys the major publishers, but each one is licensed differently, so the skill
tries routes in this order and never stops at the first failure:

| # | Route | Covers | Needs |
|---|-------|--------|-------|
| 1 | OpenAlex / Unpaywall | gold+green OA | nothing |
| 2 | Publisher direct | Nature, MDPI, Wiley (`pdfdirect`), OSTI | nothing |
| 3 | Repository copies | HAL, PMC, university repos | nothing |
| 4 | Sci-Hub mirrors | paywalled leftovers | nothing (last resort) |
| 5 | **ZJU WebVPN** | Wiley, IEEE Xplore, Springer, WoS… (IP-licensed) | 上网账号 |
| 6 | **浙大通行证 CAS** | ScienceDirect / Elsevier (identity-licensed) | 学号 + 通行证密码 |
| 7 | Human-in-the-loop captcha | when Cloudflare/Turnstile blocks you | a person at the keyboard |
| 8 | Manual list | true leftovers | the user |

## Dependencies

Python packages: `requests`, `playwright` (+ `playwright install chromium`),
`curl_cffi` (browser-TLS impersonation), `cryptography` (WebVPN login).

External: a **real Google Chrome** (Playwright `channel='chrome'` is used, not
bundled Chromium — Chrome's fingerprint survives Cloudflare far better).

Credentials are **never** stored in this skill. Read them from environment
variables; keep the actual values in a file outside any git repo, e.g.
`~/.zju_credentials.txt`:

```bash
export ZJU_USER=<学号>          # 浙大通行证
export ZJU_PASS=<通行证密码>
export ZJU_VPN_USER=<上网账号>   # WebVPN（密码与通行证可能不同！）
export ZJU_VPN_PASS=<上网账号密码>
```

## Quick Start

```bash
python scripts/zju_fetch.py prepare --src 文献.txt --out items.json
python scripts/zju_fetch.py fetch   --list items.json --out 文献PDF --gap 6
python scripts/zju_fetch.py verify  --list items.json --out 文献PDF
python scripts/zju_fetch.py report  --list items.json --out 文献PDF
```

## Utility Scripts

- `scripts/zju_fetch.py` — orchestrator (`prepare` / `fetch` / `verify` / `report`).
- `scripts/sources.py` — route 1–4; `fetch_plain(doi)` returns PDF bytes or `None`.
- `scripts/zju_sd_fetch.py` — route 6 + 7: drives real Chrome through
  institution → 浙大通行证 CAS → SAML, then grabs `pdfft` URLs.
  `--profile` keeps a persistent profile (strongly recommended),
  `--min-gap` sets pacing, `--no-hitl` disables waiting for a human.
- `scripts/zju_webvpn.py` — route 5: `login` / `resources` / `get`.

## Workflow

### 0. Pick the right network egress (do this first)

ZJU licenses split by **IP** or by **identity**, and the choice changes which
egress you want:

- **VPN/ladder ON (foreign exit)** — identity routes (CAS login, `id.elsevier.com`)
  work fine, and a foreign IP is usually *not* yet rate-limited.
- **VPN OFF (direct campus egress, e.g. 210.32.x.x)** — required for the WebVPN
  login page and for IP-licensed publishers; but a heavily used campus NAT IP is
  more likely to be flagged by Cloudflare.

**Empirically:** `zjuam.zju.edu.cn` (浙大统一身份认证) is **unreachable through a
foreign VPN exit** — selecting the organisation then dies with a network error.
But ScienceDirect article pages are often *cleaner* from the VPN exit than from the
campus NAT. So the winning combination is **login on the campus egress, download on
the VPN egress**.

### ⚠️ Never kill the VPN client

Do **not** `taskkill` the ladder. Electron VPN clients (e.g. `SDKDNS.exe`) cannot be
re-connected non-interactively: detached/elevated launches exit immediately, the
tunnel stays down, and only a human click restores it. Instead **switch the egress
per browser instance** with launch flags, which touches nothing and needs no
restore:

```python
# campus / ZJU direct  (for the CAS login)
args.append('--no-proxy-server')
# ladder exit          (for article pages and PDFs)
args.append('--proxy-server=' + <system ProxyServer, e.g. http://127.0.0.1:7897>)
```

`zju_sd_fetch.py` implements exactly this as two phases:
`--login-egress direct` (default) then `--download-egress proxy` (default), sharing
one persistent profile so the identity session carries over. Read the current system
proxy with `system_proxy_url()` (WinINET registry).

If you must manipulate the system proxy instead, use
`HKCU\Software\Microsoft\Windows\CurrentVersion\Internet Settings\ProxyEnable`
(0 = direct, 1 = ladder) and remember `reg.exe` is often blacklisted — use `winreg`.

### 1. prepare → items.json
Parse the source list; for every `10.1016/…` DOI resolve the Elsevier **PII**
(`https://doi.org/<doi>` redirects to `…/pii/Sxxxxxxxxxxxxx`). PII is what the
ScienceDirect route needs.

### 2. fetch → waterfall
Phase 1 runs routes 1–4 for all items (cheap, no credentials). Phase 2 batches the
Elsevier leftovers through `zju_sd_fetch.py` in **one** browser session — log in
once, download many. Phase 3 writes `_待手动下载.md`.

### 3. verify / 4. report
`verify` re-checks every file is a real PDF (`%PDF`, >15 KB); `report` writes
`_下载清单.csv` with number / title / DOI / filename / size / status.

## Beating the human-verification gate (防人机验证)

**Prevention** (all of these measurably matter):

1. Real Chrome via `channel='chrome'`, never bundled Chromium.
2. Launch flags `--disable-blink-features=AutomationControlled`,
   `--exclude-switches=enable-automation`; init script deleting `navigator.webdriver`.
3. **Persistent profile** (`--profile`) — a warm profile with consent cookies and
   an existing Elsevier session is far less likely to be challenged than a cold one.
4. Warm-up: load `https://www.sciencedirect.com/` first, accept the cookie banner,
   *then* go to deep links.
5. Human-like pacing: randomised 1–3 s micro-pauses, ≥6 s between article
   downloads (`--min-gap`), and a 20–30 s breather every 5 articles.
6. Log in **once** per run and reuse the session; repeated cold SAML logins are the
   fastest way to get flagged.
7. If Cloudflare blocks you on one egress, switch egress (step 0).

**Recovery** when the gate still appears:

- Detect via body text / HTML markers: `Are you a robot`, `cf-turnstile`,
  `challenge-platform`.
- Try an automatic click on the Turnstile box (top document *and* all frames).
- Otherwise keep the **headed** browser open and poll for up to ~4 minutes, telling
  the user to tick the box — then continue automatically. Use `--no-hitl` only for
  unattended runs.
- Elsevier has a *second*, separate gate for PDFs: the response is an HTML page
  containing `/craft/challenge/pdf/`. It is not Cloudflare and XHR cannot solve it.
  Escalate: in-page `fetch()` → **top-level navigation** (lets Chrome play the
  challenge) → retry `fetch()` (the navigation usually plants a clearance cookie).
- **Cooldown is a real strategy.** Heavy automation gets the *whole* campus NAT
  flagged; afterwards every egress (direct, VPN, even the WebVPN) returns
  `Are you a robot` / "There was a problem providing the content you requested".
  When that happens: stop, wait hours, then resume with slow pacing. Continuing to
  hammer makes it strictly worse. Record the leftovers with links and retry later.

## Rate Limiting

No documented public limits for OpenAlex / Unpaywall / Crossref. The scripts
default to **1 request/second per host** with a file-lock so parallel agents on the
same machine share the budget. Publisher sites are paced by `--min-gap` (default 6 s)
plus the every-5-articles cooldown. On HTTP 429, back off exponentially.

## Common Mistakes

1. **Matching the wrong university.** `Zhejiang University` is a *substring* of
   `Zhejiang University of Technology` (浙江工业大学), `…of Finance and Economics`
   (浙江财经大学) etc. Always match on `浙江大学`, `Zhejiang University Library`, or
   `zju.edu.cn`, and add a guard that goes back if the browser lands on another
   school's IdP (`zjut`, `zufe`, …). Clicking the wrong one sends your credentials
   to the wrong identity provider.
2. **Confusing the two passwords.** 浙大通行证 and 上网账号/WebVPN are different
   accounts and may have different passwords.
3. **Forgetting the system proxy after killing the VPN** — the browser then points
   at a dead port and everything fails with `ERR_PROXY_CONNECTION_FAILED`.
4. `buf[:5] == b'%PDF'` is always False (5 bytes vs a 4-byte literal); use
   `buf[:4]` or `startswith`.
5. Bash heredocs eat backslashes (`\S`, `\d`); write probe scripts to a file
   instead of piping them into `python -`.
6. Never name a local script `inspect.py` — it shadows the stdlib and breaks
   `requests`.

## References

- `references/zju_access_notes.md` — reverse-engineered protocol details:
  CAS RSA encryption, WebVPN AES-CFB login, the WebVPN proxy URL scheme, and the
  library database-navigation API.
