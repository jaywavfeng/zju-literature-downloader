# 高校统一身份认证资源获取：通用模式（实测）

本文件把「浙大通行证下载文献」和「实验虚拟机 / 在线工作区文件下载」两次实践里
反复奏效的做法抽象成与学校、与资源类型无关的通用模式。所有端点、字段名、
凭据都外置到 `--config` 的 JSON profile，本文只讲机制与坑。

## 0. 适用形态矩阵

| 场景 | 认证形态 | 资源定位 | 内容通道 | 入口 |
| --- | --- | --- | --- | --- |
| 文献批量下载 | CAS 表单 / Shibboleth / WebVPN | DOI → PII → 出版社直链 | OA 瀑布 + 浏览器 PDF | `zju_fetch.py` / `zju_sd_fetch.py` / `zju_webvpn.py` |
| 实验虚拟机 / 在线工作区文件 | CAS OAuth2 → JWT | 元数据 API + Contents API 递归 | `api/contents/` + `/files/` | `sso_fetch.py` |
| 课程平台附件、校内资源 | 统一身份认证 / 校园 IP 代理 | 目录树 API 或页面改写链接 | 原始流 / 代理 URL | `sso_fetch.py` / `zju_webvpn.py` |
| 任意受 SSO 保护的静态资源 | 可配置 | 可配置 | 可配置 | `sso_fetch.py` + 自定义 profile |

## 1. 统一身份认证的四种形态

1. **CAS 表单登录**：`GET /cas/v2/getPubKey` 拿 modulus/exponent → 密码**反转** →
   无 padding RSA 分块转 hex → 提交 `username/password/execution/_eventId=submit`。
   见 `zju_access_notes.md` §1。连续失败会启用验证码甚至锁定账号。
2. **CAS OAuth2（SPA）**：`/cas/oauth2.0/authorize?response_type=code&client_id=…&
   redirect_uri=…` → 登录 → 回调带 `code` → 前端用 code 换 token → 存
   `localStorage.token`（JWT）。见下方"关键坑"。
3. **Shibboleth / SAML**：机构选择 → IdP → SAML POST → 目标站建立身份会话。
   `zju_sd_fetch.py` 完整实现了机构精确匹配、SAML 自动提交和结果校验。
4. **WebVPN / IP 代理**：用上网账号登录网瑞达，密码 AES-CFB-128 加密，只能走页面
   里已经改写好的代理链接（密钥 ≠ IV，无法自行构造新主机）。见 `zju_webvpn.py`。

判断方法：看受保护 URL 的 302 落到哪个域名；再在前端 bundle 里搜
`localStorage`、`token`、`Authorization`、`auth_hub`，确定 token 存在哪、怎么带。

## 2. 登录态获取与检查（关键坑）

- **`redirect_uri` 必须是 `https://`**。浙大 CAS 授权 URL 里写死的 `http://` 回调
  会断链：浏览器落到 `chrome-error://chromewebdata/`，SPA 拿不到 code，
  `/pyapi/user/oauth/zju` 一直 500。`sso_fetch.py login` 会自动把 http 升级为 https。
- **用真实 Chrome 完整跑一遍 SPA**，从 `localStorage` 取 JWT；只用 `urllib` 跟随
  302 会在明文 http 回调处断掉，code 还会被提前消费。
- **cookie jar 与 token 一起持久化**（`save_session` 原子写），后续请求同时带
  `Authorization: Bearer <token>` 和会话 cookie。
- **登录态三查**：探测受保护端点（HTTP 200 且含业务字段）、401/403 判定失效、
  302 到登录页判定失效。`sso_fetch.py check` 输出结构化报告。
- **token 是资源级的**：A 项目的 `hub_token` 访问 B 项目的 hub 一律
  `403 Forbidden`。每阶段开始重新读元数据，顺带刷新本资源的 token。
- 凭据只从环境变量读（`ZJU_USER` / `ZJU_PASS` 或 profile 指定的名字），
  绝不写进仓库、配置文件或报告。

## 3. 人机验证快速处理

**预防**（`zju_sd_fetch.py` 已实现，直接复用）：

1. 真实 Chrome（`channel='chrome'`），不用 bundled Chromium。
2. `--disable-blink-features=AutomationControlled`、
   `--exclude-switches=enable-automation`，init script 删 `navigator.webdriver`。
3. 持久化 profile，warm-up 首页并接受 cookie 横幅后再进深链。
4. 拟人节奏：1–3 s 随机微停顿、文章间隔 ≥6 s、每 5 篇冷却 20–30 s。
5. 一次登录复用会话，避免反复冷 SAML 登录。
6. 按浏览器实例切 egress（登录走校园直连，下载走代理），**绝不去 taskkill VPN 客户端**。
7. 组织名精确匹配 IdP 域名，加排除词防止误入他校。

**恢复**：检测 `Are you a robot` / `cf-turnstile` / `challenge-platform` →
等组件渲染（最多 40 s，别急着 reload）→ 递归收集 `challenges.cloudflare.com`
与 `blob:` 子框架 → 拟人移动鼠标后点击复选框 → 复查 60 s → 仍不过则保持
headed 窗口等人（HITL），`--no-hitl` 用于无人值守。

**图片验证码**：放大/二值化后识别或直接人工；连续失败次数剩 2 次时停止猜测，
换认证通道，避免锁号。验证码失败不等于功能缺失，不要用它掩盖实现。

**冷却是策略**：重度自动化会把整个校园 NAT 打进黑名单，此时所有 egress 都返回
验证页。停下来等几小时，记录 leftovers，用慢节奏续跑。

## 4. 受保护资源定位

- 目标页 HTML 常是 **SPA 壳**，不是文件服务器。先下载前端 bundle 静态分析真实
  API 路径，别对 `/workspace/<id>/app` 之类的页面路径硬试。
- 元数据 API 给出 `display_name`、`hub_name`、`hub_token` 等。**命名按元数据确认**，
  不按链接顺序或截图列表猜（实测链接顺序 1/2/3 对应名称序号 2/4/3）。
- **平台"文件树"接口登记数常远小于实际**：一次实测登记 10 / 4 / 15 个文件，
  Jupyter Contents API 递归拿到 2489 / 8 / 17 个。以内容 API 递归为主，
  文件树用于 size 校验、空目录和差集补全。
- 目录列表里 `utils/__pycache__` 显示为空，但平台文件树登记了 4 个 `.pyc`：
  用登记路径直接走 `/files/` 取（差集补全）。
- 目录列表请求 `?content=1` 只返回 children 的 name/path/type/size，不返回文件内容；
  对每个文件再单独请求一次 `?content=1` 才拿到 content。

## 5. 下载

**小文件 / notebook — Contents API**

| 类型 | content 形态 | 落盘方式 |
| --- | --- | --- |
| `notebook` | dict | `json.dumps(c, ensure_ascii=False, indent=1) + "\n"` |
| 文本 / Markdown / Python | str | UTF-8 直接写 |
| PDF / 二进制 | base64 str 或 `mimetype` 为 pdf/octet-stream | `base64.b64decode` |

notebook 重新序列化后字节数与平台 `size` 不等属正常，校验改用
`nbformat == 4` 与 cell 数。

**大文件 — `/files/` 原始流**（阈值默认 8 MB，可用 `--big-threshold` 覆盖）

- `Accept-Ranges: bytes`；本地已有 N 字节则发 `Range: bytes=N-`。
- 206 → `ab` 追加，200 → `wb` 重来；按 `Content-Length` / 平台 size 判断是否完成。
- 用 `stream=True` + `iter_content(1 MB)`，避免 base64 膨胀把服务端打挂。
- 实测 133 MB `model.safetensors`、63 MB `train.jsonl`、47 MB MNIST、
  29 MB `.ckpt` 均一次通过。

**通用要求**

- 保持站点原有相对路径，空目录也建出来。
- 原子写：临时文件 + `os.replace`，中断可续跑。
- 容器回收（503 `server no longer running`）→ 调 `restart_api`
  （`action=start&hub_token=<本资源 token>&window_id=…`）拉起后重试。

## 6. 稳健性要求

| 要求 | 实现 |
| --- | --- |
| 限流 | 文件锁 1 req/s per host，`time.monotonic()`，跨进程共享；429 指数退避 |
| 失败重试 | 网络错误 / 5xx / 429 指数退避（最多 `retries` 次）；非可重试 4xx 立即失败并带响应体 |
| 登录状态检查 | `check` 独立子命令；`locate`/`fetch` 每阶段重读元数据顺带验证 |
| 清晰错误反馈 | `SsoError` 一律含 `url`、`status`、`body` 片段和下一步动作 |
| 结果校验 | 字节数、`%PDF` 头、`nbformat=4`、未获取清单 |
| 报告 | 完整文件清单 + 目录树 + 未获取项 + 错误明细 |
| 输出 | 全部写文件（`--output`），stdout 只留短状态 |

## 7. 故障排查矩阵

| 现象 | 原因 | 动作 |
| --- | --- | --- |
| hub contents 返回 `403 Forbidden` | 用了别的资源的 token / JWT | 重新读该资源元数据的 `hub_token` |
| `503 JupyterHub server no longer running` | 容器被回收或大文件 base64 打挂 | `restart_api` 拉起；大文件改 `/files/` 流式 |
| 拿到 OAuth `code` 但没有 token | `redirect_uri` 是 `http://` | 改为 `https://` 并用真实浏览器重跑 |
| 实际文件数远多于登记数 | 平台文件树接口不全 | 以 Contents API 递归为准 |
| 目录列表空但文件树有该文件 | 隐藏文件（`.pyc` 等） | 用登记路径走 `/files/` 直取 |
| notebook 字节数与 `size` 不符 | JSON 重新序列化差异 | 校验 `nbformat` 与 cells，不比字节 |
| 匹配到别校 IdP | `Zhejiang University` 是他校名称的子串 | 精确匹配 `idp.<school>.edu.cn` + 排除词 |
| 全站 `Are you a robot` | 校园 NAT 被整体标记 | 停止、冷却数小时、慢节奏续跑、记录 leftovers |
| `ERR_PROXY_CONNECTION_FAILED` | 代理端口失效 / VPN 被关 | 按实例设置 `--proxy-server`，不要动系统代理/VPN 客户端 |

## 8. 配置模板

`references/profiles/zju-mo-workspace.json` 是默认 ZJU profile，字段含义：

- `auth`：授权端点、client_id、redirect_uri、表单选择器、token 存储位置、
  凭据环境变量名、探测 URL 与成功标记。
- `metadata_api` / `metadata_fields`：资源元数据端点与字段映射。
- `tree_api`：平台登记树（可选，用于 size / 空目录 / 差集）。
- `contents_api` / `files_api` / `hub_headers`：小文件与大文件通道。
- `restart_api`：容器自愈端点。
- `big_threshold` / `rate_gap` / `retries` / `timeout`：可调参数。

换学校只改这个 JSON，不动脚本；模板里 `{resource_id}`、`{hub_name}`、
`{hub_token}`、`{path}`、`{window_id}` 由 `sso_common.expand` 填充，
路径值会按 `quote(safe='/')` 编码以保留目录结构。

## 9. 与原有能力的关系

通用层是**新增**，不改文献下载链路：`zju_fetch.py`、`literature_sources.py`、
`zju_sd_fetch.py`、`zju_webvpn.py` 的命令、参数、环境变量与输出格式全部保留。
`sources.py` 保留为兼容 shim，旧调用方式不变。默认 profile 仍是浙大通行证。
