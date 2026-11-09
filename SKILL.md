---
name: campus-sso-resource-fetcher
description: >-
  获取受高校统一身份认证（CAS / OAuth2 / Shibboleth / SAML / WebVPN）保护的资源：
  快速登录与人机验证处理、登录状态检查、受保护资源定位、保持目录结构的完整下载、
  失败重试与清晰错误反馈。适用于实验虚拟机与在线工作区文件、课程平台附件、
  校内 IP 授权数据库、文献批量下载等场景，不局限于某一学校或某一资源类型。
  内置浙大通行证（ZJU CAS）适配器，默认仍支持通过浙大通行证下载文献，原有
  zju_fetch.py 命令与参数完全兼容。触发词：统一身份认证、校园账号登录、SSO 登录、
  CAS、OAuth2、Shibboleth、WebVPN、人机验证、验证码、登录态检查、登录失效、
  受保护资源、工作区文件下载、实验虚拟机文件、课程资源、校内资源、文献下载、
  参考文献下载、批量下载 PDF、下载失败重试、403 Forbidden、保持目录结构。
---

# 高校统一身份认证资源获取

## Overview

把「用学校账号登录 → 找到真正受保护的资源 → 完整取回并校验」做成一条与学校、
与资源类型无关的通用链路。站点相关的端点、字段名、凭据全部外置在 JSON profile
里，脚本只实现机制。

两类已验证场景开箱可用：

- **文献批量下载**（原有能力，完全保留）：参考文献列表 → OA 瀑布 → 浙大通行证
  CAS / Shibboleth / WebVPN → PDF 落盘 → 清单与待手动下载列表。
- **实验虚拟机 / 在线工作区文件**（本次实践新增）：CAS OAuth2 登录取 JWT →
  项目元数据取资源级 token → Contents API 递归定位 → 小文件走 API、大文件走
  `/files/` 原始流 + Range → 保持原有目录结构 → 校验与报告。

同一套 `check / login / locate / fetch / verify / report` 子命令也覆盖课程平台
附件、校内 IP 授权数据库等任意受统一身份认证保护的资源：复制一份 profile 改端点
即可，不需要改脚本。

## Dependencies

Python 包（均为本仓库原有依赖，未引入新栈）：`requests`、`playwright`
（登录与文献链路使用 `channel='chrome'`）、`cryptography`（WebVPN 密码加密）。
仅文献直链伪装会用到 `curl_cffi`，非必需。标准库：`argparse` `json` `os` `re`
`sys` `time` `csv` `base64` `zipfile` `subprocess` `unicodedata` `winreg`
`tempfile` `urllib`。

外部：**真实 Google Chrome** —— 统一身份认证回调、SAML 自动提交、Cloudflare /
Turnstile 人机验证都依赖真实浏览器指纹，bundled Chromium 通过率低。

凭据只读环境变量，绝不入库：

```bash
export ZJU_USER=<学号>            # 浙大通行证（默认 profile）
export ZJU_PASS=<通行证密码>
export ZJU_VPN_USER=<上网账号>     # WebVPN，密码可能与通行证不同
export ZJU_VPN_PASS=<上网账号密码>
# 可选：覆盖默认 profile 里的 OAuth public client_id（非 secret）
export SSO_CLIENT_ID=<client_id>
# 其他学校在 profile 的 auth.user_env / auth.pass_env 里指定变量名
```

profile 中任何字符串值都支持 `${ENV_VAR}` 占位（`sso_common.expand()` 展开），
`auth.client_id_env` 另可指定注入 client_id 的变量名。`references/zju_access_notes.md`
里的 WebVPN KEY/IV 是厂商前端 bundle 中的站点公开常量，不是用户凭据或私有密钥，
可用 `ZJU_WEBVPN_KEY` / `ZJU_WEBVPN_IV` 覆盖。仓库内不保存任何凭据、JWT、
token、cookie 或个人隐私数据。

本技能自包含，不依赖其他已安装 skill；人机验证、egress 切换、SAML 流程复用
仓库内 `zju_sd_fetch.py` 的既有实现。

## Quick Start

**A. 通用：受统一身份认证保护的工作区 / 实验虚拟机文件**

```bash
CFG=references/profiles/zju-mo-workspace.json
ID=<项目或资源 ID>

python scripts/sso_fetch.py check   --config "$CFG" --resource-id "$ID" --output check.json
python scripts/sso_fetch.py login   --config "$CFG" --headed --output login.json
python scripts/sso_fetch.py locate  --config "$CFG" --resource-id "$ID" --output tree.json
python scripts/sso_fetch.py fetch   --config "$CFG" --tree tree.json --out "输出目录" --output manifest.json
python scripts/sso_fetch.py verify  --config "$CFG" --tree tree.json --manifest manifest.json --out "输出目录" --output verify.json
python scripts/sso_fetch.py report  --config "$CFG" --tree tree.json --manifest manifest.json --verify verify.json --out "输出目录" --output 下载报告.md
```

换学校：复制 `references/profiles/zju-mo-workspace.json`，改 `base` / `auth` /
`metadata_api` / `contents_api` / `files_api` / `restart_api` 与 `metadata_fields`。

**B. 兼容：浙大通行证批量下载文献（原有用法，一字未改）**

```bash
python scripts/zju_fetch.py prepare --src 文献.txt --out items.json
python scripts/zju_fetch.py fetch   --list items.json --out 文献PDF --gap 6
python scripts/zju_fetch.py verify  --list items.json --out 文献PDF
python scripts/zju_fetch.py report  --list items.json --out 文献PDF
```

## Utility Scripts

**通用层（新增）**

- `scripts/sso_common.py` — 共享基础设施：文件锁限流、指数退避重试、
  `SsoError` / `AuthError` / `RateLimitError` / `ResourceError` 带 URL+状态+响应体、
  登录态探测、原子写与 session 持久化、模板展开。
- `scripts/sso_fetch.py` — 通用 CLI，六个子命令：
  - `check` 探测登录态与受保护端点可达性，输出结构化报告；
  - `login` 真实 Chrome 走 CAS/OAuth2，自动把 `http://` redirect_uri 升级为
    `https://`，从 `localStorage.token` 取 JWT 并持久化；
  - `locate` 读元数据拿资源级 token，递归 Contents API 枚举，补全平台文件树
    登记的隐藏文件与空目录；
  - `fetch` 按类型落盘（notebook→JSON、文本→UTF-8、PDF/二进制→base64），
    大文件走 `/files/` 流式 + Range 断点续传，503 时调 `restart_api` 自愈；
  - `verify` 校验字节数、`%PDF` 头、`nbformat=4`、未获取项；
  - `report` 生成 Markdown 清单 + 目录树 + 未获取项 + 错误明细。
  所有子命令都写 `--output` 文件，stdout 只留短状态。

**文献链路（原有，保持不变）**

- `scripts/zju_fetch.py` — 文献编排器 `prepare` / `fetch` / `verify` / `report`。
- `scripts/literature_sources.py` — 原 `sources.py`，OA / 出版社直链 / 仓储 /
  镜像瀑布，`fetch_plain(doi)` 返回 PDF 字节。
- `scripts/sources.py` — 兼容 shim，`import sources` 与
  `python scripts/sources.py --doi ... --out ...` 仍可用。
- `scripts/zju_sd_fetch.py` — CAS → Shibboleth → ScienceDirect，内含完整
  Cloudflare/Turnstile 预防与人机协同恢复；`--profile`、`--min-gap`、
  `--login-egress` / `--download-egress`、`--no-hitl`。
- `scripts/zju_webvpn.py` — WebVPN `login` / `resources` / `get`。

## Workflow

### 1. 先判断认证形态与出口
看受保护 URL 的 302 落到哪个域名、前端 bundle 把 token 存哪；确定是 CAS 表单、
CAS OAuth2、Shibboleth 还是 WebVPN。按实例切 egress（登录走校园直连、下载走代理），
**不要 taskkill VPN 客户端**，也不要改系统代理。

### 2. 登录态检查与登录
`check` 先跑：200 且含业务字段 = 有效；401/403 或 302 到登录页 = 失效，执行
`login`。凭据从环境变量读。遇到人机验证按 `sso_patterns.md` §3 处理：
预防 → 自动点击 → 保持 headed 窗口等人（HITL）→ 仍不过则冷却并记录。

### 3. 定位资源
读元数据拿 `display_name` 与**本资源自己的** token（跨资源复用必 403）；
以内容 API 递归枚举为主，平台文件树接口用于 size、空目录与差集补全。
命名按元数据确认，不按链接顺序猜。

### 4. 下载
小文件走 Contents API 并按类型反序列化；≥8 MB 走 `/files/` 原始流 + Range；
原子写、断点续传、保持相对路径。503 时 `restart_api` 拉起容器后重试。

### 5. 校验与报告
`verify` 检查字节数 / PDF 头 / notebook 结构；`report` 输出清单、目录树、
未获取项与错误。任何一步失败都换等价载体继续（换通道、换 egress、换端点），
不空手返回。

### 6. 文献场景的额外瀑布
`zju_fetch.py fetch` 依次尝试 OpenAlex/Unpaywall、出版社直链、仓储副本、
镜像、WebVPN、浙大通行证 CAS、人工验证、待手动下载列表，任一失败不中断整体。

## Rate Limiting

校园网关与出版商接口均无公开限额，默认按 **1 请求/秒/主机** 保守执行：
`sso_common._Throttle` 用临时目录里的 O_EXCL 锁串行化，并把时间戳写进共享
stamp 文件，使多个进程真正共用同一份预算（进程内字典会各算各的）；计时用
`time.time()`（墙钟，可跨进程共享），单次休眠封顶一个 gap。429/5xx/网络错误
指数退避（最多 `retries` 次，封顶 30 s），429 抛 `RateLimitError`；非可重试
4xx 立即失败并在错误里带上响应体。流式 `/files/` 下载绕过重试客户端，故在
`fetch_raw()` 内对 429/5xx 单独重试，503 额外触发 `restart_api` 自愈。
文献链路另有 `--min-gap`（默认 6 s）与每 5 篇 20–30 s 冷却。

## Common Mistakes

1. **`redirect_uri` 用 `http://`** —— SPA 回调断链，OAuth code 换不到 token；
   必须 `https://`（`login` 已自动升级）。
2. **拿别的资源的 token 去访问** —— 统一 403；每次从该资源元数据重新取。
3. **把平台文件树接口当完整清单** —— 实测登记 10/4/15，实际 2489/8/17；
   以 Contents API 递归为准。
4. **大文件走 Contents API base64** —— 会把服务端打挂并返回 503；改 `/files/` 流式。
5. **按链接顺序或截图列表猜名称** —— 实测顺序是乱的，必须读 `display_name`。
6. **误选他校 IdP** —— `Zhejiang University` 是 `…of Technology` 的子串；
   精确匹配 IdP 域名并加排除词，否则凭据会提交给错误的身份提供方。
7. **混淆两套密码** —— 通行证与上网账号/WebVPN 可能不同。
8. **notebook 字节数与平台 size 不符就判失败** —— 重新序列化必然有差异，
   改校验 `nbformat` 与 cell 数。
9. **`buf[:5] == b'%PDF'` 永远为假** —— 用 `[:4]` 或 `startswith`。
10. **杀掉 VPN 客户端或改系统代理** —— 无法非交互恢复，改用浏览器实例级
    `--proxy-server`。
11. **重物不落盘** —— 凭据、token、session 文件绝不写进仓库；`atomic_write`
    保证中断后不残留半截文件。
12. **把验证失败当功能缺失** —— 剩余尝试次数少时停手换通道，别硬猜到锁号。

## References

- `references/sso_patterns.md` — 通用协议模式：认证形态、登录态检查、人机验证、
  资源定位、下载、稳健性要求与故障排查矩阵（两次实测结论）。
- `references/profiles/zju-mo-workspace.json` — 默认 ZJU profile，含全部可配端点。
- `references/zju_access_notes.md` — 内置 ZJU 适配器协议逆向笔记：CAS RSA 加密、
  WebVPN AES-CFB 登录、代理 URL 规律、ScienceDirect Shibboleth 流程、PDF 直链与反爬。
