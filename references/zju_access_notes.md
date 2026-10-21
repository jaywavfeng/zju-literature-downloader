# ZJU 访问协议逆向笔记（实测）

## 1. 浙大统一身份认证 / CAS（zjuam.zju.edu.cn）

表单字段：`username` `password` `execution` `ys` `_eventId=submit` (`authcode` 仅当需要验证码)。

密码加密（站点自带 `cas/js/login/security.js`）：

1. `GET /cas/v2/getPubKey` → `{"modulus": "<hex>", "exponent": "10001"}`
2. 明文 = 密码**反转**：`password.split("").reverse().join("")`
3. 教科书式 RSA（无 padding，分块后转 hex）：`RSAUtils.encryptedString(key, reversedPwd)`

最省事的做法：用 Node 直接跑站点自己的 security.js（`})(this)` 的 UMD，在 CJS 里
`this` 即 `module.exports`）。

首次登录通常无验证码；连续失败后会启用，且可能锁定。

## 2. 浙大 WebVPN（webvpn.zju.edu.cn，网瑞达）

- 用**上网账号**登录（与通行证不同密码）。
- `POST /do-login`，字段：`_csrf` `auth_type=local` `username` `sms_code` `password`
  `captcha` `needCaptcha` `captcha_id`。
- 密码加密：AES-CFB-128（segment 16），`key = iv = "wrdvpnisawesome!"`，
  明文用 `'0'` 补到 16 的倍数，输出 `hex(iv) + hex(ct)[:len*2]`。
- 已在别处登录 → 返回 `NEED_CONFIRM`，需 `POST /do-confirm-login`（会踢掉旧会话）。
- 可用资源列表：`GET /user/portal_groups`（分组：校内资源导航 / **电子资源导航** /
  网络工具导航）。电子资源导航里有 Wiley、IEEE Xplore、Springer、WoS、JSTOR、
  Reaxys、SciFinder 等，但**不含 ScienceDirect / Wiley 之外的 Elsevier**。
- 代理 URL 形如
  `/https/77726476706e69737468656265737421<AES(url)>/<path>`，
  前缀是 IV `"wrdvpnisthebest!"` 的 hex；**密钥不等于 IV**，所以无法自行构造新主机，
  只能用页面里已经改写好的链接。任意主机请求会返回 `HOST_NOT_FOUND`。
- 图书馆数据库导航 `https://libdb.zju.edu.cn/`（经 WebVPN 打开）里能按字母找到
  各数据库条目，链接形如 `/s/lib/libtb/turning/<id>`，重定向后即已授权的目标库。

## 3. ScienceDirect / Elsevier

- ZJU 是 **基于身份** 授权（图书馆导航原话："基于身份认证的访问，不要使用RVPN"），
  所以 WebVPN 换 IP 没用，必须走 Shibboleth → 浙大通行证。
- 流程：
  1. `https://www.sciencedirect.com/user/institution/login?targetUrl=/science/article/pii/<PII>`
  2. 填 `#bdd-email` = `Zhejiang University` → 点 `#bdd-els-searchBtn`
  3. 结果里选 **浙江大学 / Zhejiang University Library**（隐藏字段含
     `entityId=https://idp.zju.edu.cn/idp/shibboleth`）
  4. 自动跳转 `auth.elsevier.com/ShibAuth/institutionLogin` → `instDataForm` 自动提交 →
     `zjuam.zju.edu.cn/cas/login?service=https://idp.zju.edu.cn/idp/Authn/ExtCas?...`
  5. CAS 登录 → IdP 返回 SAML → POST 到 `auth.elsevier.com/SHIRE/SAML2/POST` →
     `deliverInstCredentials` → 回到 ScienceDirect
- 成功标志：页面顶部出现 `Brought to you by: Zhejiang University Library`，
  且文章页出现本文章自己的 `pdfft?md5=...&pid=1-s2.0-<PII>-main.pdf` 链接。
- 注意 DOM 里 `form.action` 是对象，要用 `getAttribute('action')`；SAML 页的 action
  带 HTML 实体（`&#x3a;`），需要 unescape。
- PDF 下载有两道门：Cloudflare（页面级）和 Elsevier 自己的
  `/craft/challenge/pdf/`（下载级）。后者必须靠浏览器顶层导航解。

## 4. 取 PDF 的 URL 规律

| 出版社 | 可用直链 |
|---|---|
| Nature 系列 | `https://www.nature.com/articles/<suffix>.pdf` |
| MDPI | `https://mdpi-res.com/d_attachment/<abbr>/<abbr>-<vol>-<art5>/article_deploy/<...>.pdf`（`www.mdpi.com/.../pdf` 被 Akamai 403） |
| Wiley | `https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>`（经校园 IP / WebVPN） |
| OSTI | `https://www.osti.gov/servlets/purl/<osti_id>`，`osti_id` 由 `https://www.osti.gov/api/v1/records?doi=<DOI>` 查得 |
| Elsevier | 文章页内的 `pdfft?md5=…&pid=…` |

MDPI DOI 后缀拆分：`10.3390/<缩写><卷2位><期2位><文章号>`，
文章号补零到 5 位；缩写映射 `app→applsci`、`s→sensors`，其余同 DOI。

## 5. 反爬

- `www.sciencedirect.com` 对普通 `requests` 固定 403（约 832 KB 的 Akamai 拒绝页）。
  装 `curl_cffi` 后 `impersonate='chrome'` 即可 200。
- Wiley / Cloudflare 需要真实浏览器指纹。
- Sci-Hub 可用镜像（随时变化）：`sci-hub.ru`、`sci-hub.box`；`.se`、`.st`、
  `libgen` 在本机网络下不可达；`.ee` / `.ren` 有验证码。sci-hub 对 2025 年之后的
  新文献常常没有副本。
