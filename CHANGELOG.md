# 变更记录

## 2026-11-09 — 通用化重构

- 技能重命名：`zju-literature-downloader` → `campus-sso-resource-fetcher`；
  frontmatter `name`、标题、描述、适用场景与触发词改为面向高校统一身份认证资源获取，
  不再局限于浙大或文献下载。
- 新增通用层：`scripts/sso_common.py`（限流 / 重试 / 错误分类 / 登录态 / 原子写）、
  `scripts/sso_fetch.py`（`check` / `login` / `locate` / `fetch` / `verify` / `report`）、
  `references/sso_patterns.md`（通用模式与故障排查矩阵）、
  `references/profiles/zju-mo-workspace.json`（默认 ZJU profile）。
- 重命名：`scripts/sources.py` → `scripts/literature_sources.py`；新增
  `scripts/sources.py` 兼容 shim，`import sources` 与
  `python scripts/sources.py --doi ... --out ...` 均保持不变。
- 内容修改：`SKILL.md` 重写；`zju_access_notes.md` 标注为 ZJU 适配器并指向通用笔记；
  `zju_fetch.py` docstring 标明文献适配器身份；`.gitignore` 补通用层运行产物。
- 兼容保留：`zju_fetch.py` / `zju_sd_fetch.py` / `zju_webvpn.py` 的命令、参数、
  `ZJU_USER` / `ZJU_PASS` / `ZJU_VPN_USER` / `ZJU_VPN_PASS` 环境变量与输出格式不变；
  默认 profile 仍是浙大通行证，文献批量下载照常可用。
- 依赖不变：仍为 `requests` / `playwright` / `cryptography` / 标准库 + 真实 Chrome，
  未引入新栈或外部依赖。

## 2026-11-09 — 推送前检查与修正

- 修复 `sso_fetch.cmd_locate()`：引用了未注册的 `--big-threshold`，`locate`
  子命令必然抛 `AttributeError`；补上参数定义，并改用 `getattr` 兼容程序化调用。
- 加固 `fetch_raw()`：429/500/502/504 与 503 一并纳入流式下载重试（流式通道
  绕过 `HttpClient` 的退避循环，原先只有 503 会触发 `restart_api` 自愈）。
- 修复 `sso_common._Throttle`：时间戳原存进程内字典，跨进程只串行化、不共享
  预算；改为写入共享 stamp 文件（`time.time()`，单次休眠封顶一个 gap）。
- `sso_common.expand()` 支持 `${ENV_VAR}`；`login` 的 client_id 支持
  `SSO_CLIENT_ID`（或 profile 的 `client_id_env`）运行时注入。
- `zju_webvpn.py` 的 KEY/IV 支持 `ZJU_WEBVPN_KEY` / `ZJU_WEBVPN_IV` 覆盖。
- 疑点定性：profile 的 `client_id` 是 OAuth2 public identifier（RFC 6749 §2.2，
  非 secret）；WebVPN KEY/IV 与代理 URL 前缀是厂商前端 bundle 里的站点公开
  常量，非用户凭据、非私有密钥。以上均可通过环境变量外置。
- 仓库不含账号、密码、JWT、token、cookie、个人隐私数据；全量敏感扫描 +
  离线 mock 端到端验证通过（16 项：`check`/`login`/`locate`/`fetch`/`verify`/
  `report` 全链路，含 401/403/302/429/5xx/网络异常、503 自愈、Range 断点续传、
  notebook/PDF/隐藏文件/空目录、跨进程限流与结构化错误）。
