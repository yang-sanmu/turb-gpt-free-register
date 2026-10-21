# GptAccount 注册适配：第一阶段

## 范围（2026-10-19）

- 当前项目继续拥有 WebUI、注册步骤、邮箱接码、账号数据库和后台 2FA。
- GptAccount 只提供真实 Chrome 的启动、CDP 连接及进程树回收。
- 每次任务使用临时目录；关闭前保存 Cookies 和 Local Storage，确认 Chrome 已退出后删除目录。
- 不新增 UPI、注册＋UPI 连续执行或最大任务并发控制。QQ 实现及网络排查暂缓。
- 代理池刷新、代理独占、订阅节点、GptAccount 网络模式和账号导入为后续事项；本期沿用上游代理池及前置代理。

## 实现边界

使用 GptAccount 独立 stdio 桥接脚本，复用 ChromeLauncherBroker、ChromeProcessLauncher 和 BrowserRunRegistry；无需启动桌面 UI，也不改变已有账号、Profile 或网络配置。

Python 端新增 gptaccount 驱动，复用 Cloak 路径的页面流程及 Selenium 风格 Playwright 适配层。通过显式依赖参数替换浏览器创建和凭证快照，不使用全局 monkey patch，不复制注册流程。

代理由 Python 的现有本地中继处理（包括用户名密码和前置代理），桥接只接收回环代理地址。2FA 沿用保存账号后自动入队的上游机制，其成功状态独立回写。

## 使用

本机已配置 `REGISTRATION_DRIVER=gptaccount`、项目目录 `E:/GptAccount`、Node 路径 `C:/Program Files/nodejs/node.exe`，默认显示 Chrome 窗口。其余 `.env` 配置（邮箱、域名、代理、2FA 等）保持原值。

首次使用更新后的代码需重启当前项目的 WebUI；无需启动 GptAccount 桌面应用。在 PowerShell 中启动：

```powershell
cd E:\gpt-free-register
.\.venv\Scripts\python.exe web.py --open-browser
```

在“配置 → 注册方式”选择 **GptAccount 临时 Chrome**；“GptAccount”配置组可以更改项目目录、Node 路径、有头/无头、语言、时区和启动等待时间。随后照常从注册页面发起单个或批量任务。切换回其他驱动仍走上游原来的流程。

前提是 Windows、本机 Chrome、Node 24，以及已安装依赖和 `chrome-launcher.exe` 的 GptAccount 源码目录。本机这些条件已通过真实启动验证。换电脑时需同时携带 GptAccount 新增的 `scripts/registration-browser.mjs`，不能只复制本项目。

### 代理与邮箱

- 沿用现有代理池，支持逐行粘贴 `hostname:port:username:password`；GptAccount 驱动经现有本地中继使用 HTTP、SOCKS5/SOCKS5H 目标代理。
- `PROXY_POOL_UPSTREAM_PROXY` 可填写本地 Clash 等代理入口，形成“Chrome → 本地中继 → 前置代理 → 代理池目标 → 网站”。当前节点需能连接目标代理；本期不自动切换订阅节点。
- 没有目标代理时，Chrome 使用应用层直连，不读取系统 HTTP 代理。操作系统的 TUN/VPN 路由仍可能影响实际出口。
- 域名邮箱和 QQ 收码完全沿用现有实现，不新增网络模式。代理不会在注册中途自动刷新；刷新地址、规则、开始前刷新和代理独占调度留待后续。

### 保存内容和生命周期

业务数据保存在本项目的 `turb.sqlite3`，不是 GptAccount 的账号库。`accounts` 表的 `payload` 中保留：

| 内容 | 字段 |
| --- | --- |
| 邮箱、Token | `email`、`access_token` |
| 注册密码 | `extra_json` 解码后的 `registration_password` |
| 2FA | `totp_secret`；启用 `ENABLE_2FA` 后由现有后台任务设置并回写 |
| Cookies、Local Storage | `extra_json` 解码后的 `browser_session.storage_state` |
| 快照版本和时间 | `browser_session.schema_version`、`browser_session.captured_at` |
| 本次目标代理 | `proxy_used` |

这里的 Token 是 ChatGPT 会话 `accessToken`。GptAccount 驱动本阶段跳过 Codex OAuth，不生成 Codex refresh token。2FA 不是浏览器注册成功的同步完成条件，可在账号列表查看后台状态。

每个任务建立 `%TEMP%\gptaccount-registration-*` 临时目录，拿到 Token 后读取 Cookies（包含 HttpOnly）和 Local Storage 并保存账号；任务结束时关闭自有 Chrome 进程树，确认退出后删除整个目录。启动失败、正常取消和客户端断开也执行清理。不向 GptAccount 创建持久账号或 Profile。

Cookies 读取失败时仍保存已有账号及 Token，并明确返回部分失败；清理未确认时返回失败并保留临时目录路径，不将其误报为成功。断电或整个进程树被强杀仍可能遗留临时目录，不能承诺这种情况下自动回收。此阶段不提供 Cookies 导出按钮或导入 GptAccount 的功能，但快照已完整入库供后续使用。

## 验收结果（2026-10-19）

- [x] 调研两个仓库，确认当前工作树干净。
- [x] GptAccount 桥接：启动、正常关闭、启动失败、stdin 断开后的清理。
- [x] Python 驱动：代理传递、CDP 连接、失败及取消清理。
- [x] 共用注册流程，保存凭证快照，保留已有驱动行为。
- [x] WebUI 配置入口、使用说明与本地配置。
- [x] 针对性回归及真实 Chrome 本地集成测试。

验证通过：注册适配测试 12 项、相关 Python 回归 57 项、GptAccount Node 检查 44 项、真实 Chrome 本地集成测试 4 项。有头和无头均验证了带认证代理、表单操作、时区、HttpOnly Cookies、Local Storage 和临时目录删除；另覆盖启动失败、启动中取消和客户端 EOF。最后一次真实浏览器测试无未关闭管道的 ResourceWarning。WebUI 页面渲染、JavaScript 语法、6 个配置字段、配置热加载及主入口分派也通过检查。

同日复查修复：缺少 Chrome 时按“从未启动”清理临时目录，清理异常不再覆盖原始启动报错；邮箱等待返回或报错后检查停止请求，停止时不继续提交或重发验证码；账号已保存后不再因后续异常改写邮箱为失败；删除桥接脚本硬编码的 60 秒启动计时，由 Python 按 `GPTACCOUNT_START_TIMEOUT` 统一控制。邮箱正在进行的阻塞等待仍沿用上游实现，本轮没有修改 QQ 客户端。

复跑新增测试：

```powershell
# E:\gpt-free-register
.\.venv\Scripts\python.exe -m unittest discover -s tests -p test_gptaccount_registration.py -v
$env:GPTACCOUNT_BROWSER_TEST = '1'
.\.venv\Scripts\python.exe -W error::ResourceWarning -m unittest discover -s tests -p test_gptaccount_browser.py -v
Remove-Item Env:\GPTACCOUNT_BROWSER_TEST

# E:\GptAccount
node --test test/registrationBridge.test.js test/browserArgs.test.js test/browserRuns.test.js test/manualBrowser.test.js
```

真实服务注册、QQ 接码和 2FA 的在线成功率需另行验收；本地测试不能替代它们。Cookies/Token 可能过期，保存快照不保证未来免登录恢复。

## 真实注册验收

1. 确保已配置的前置代理正在运行，当前节点能够连接代理池目标；重启 WebUI。
2. 保持 GptAccount 有头模式和现有域名邮箱／QQ 配置，从注册页面提交 **1 个账号、1 个工作线程**。无需先启动 GptAccount 桌面应用。
3. 观察实际页面与任务日志：打开注册页、填写邮箱和密码、自动收取 QQ 中的验证码、完成注册并保存账号。若出现需要人工完成的人机验证，由用户在 Chrome 窗口操作。
4. 核对 SQLite 中本次账号的邮箱、注册密码、accessToken、Cookies 快照及代理记录均存在，只报告完整性，不在聊天中输出凭证。
5. 等待已启用的后台 2FA 任务结束，单独确认 `totp_secret` 已回写；最后核对本次 Chrome 已关闭，临时 Profile 已删除。

启动任务、查看日志、检查数据库和清理结果可由助手完成。通常用户只需在出现人机验证时介入；若 QQ IMAP 鉴权失败，需在本机确认 IMAP 已开启并更新授权码。第一次验收失败时按实际卡点修复，不立即批量重试。

## 后续合并上游

GptAccount 仅新增桥接脚本和协议测试，没有修改其原有业务文件；对应桥接提交为 `d03d06b`。当前项目主要新增驱动、配置和测试，只在注册主入口、共用流程及 WebUI 插入适配入口。更新上游时重点检查 `run_cloak_registration` 的页面步骤及 GptAccount 启动器接口；无需维护注册流程的第二份副本。两个仓库分别保存第一阶段提交，后续修改可独立比较和回退。

省流接入在 `cloakbrowser_registration.py` 中增加可选的 `network_factory`，默认仍执行原有 CloakBrowser 网络初始化。GptAccount 的 CDP/缓存实现放在独立文件中。这仍是对上游文件的局部修改；若上游也修改函数参数或网络初始化段，合并时可能需要处理冲突，不能保证零冲突。

## GptAccount 省流（2026-10-21）

开启现有 `BROWSER_DATA_SAVER_MODE` 后，GptAccount 通过 CDP 复用 Roxy 的 URL 拦截、静态资源缓存与流量统计。共用注册流程仅增加 `network_factory` 接入参数，实现集中在 `core/gptaccount_network.py`，GptAccount 项目无需额外修改。

- 沿用 `BROWSER_DATA_SAVER_BLOCKED_RESOURCE_TYPES`、`BROWSER_DATA_SAVER_BLOCKED_URL_PATTERNS`；使用 Roxy 的 URL 规则，具有相同的后缀匹配限制。
- 沿用 `ROXY_LOCAL_ASSET_CACHE_DIR`（默认 `./cache/roxy-assets`）、`ROXY_LOCAL_ASSET_CACHE_MODE`（默认 `auto`）、有效期和单文件大小配置。首次下载记录静态资源，之后新建的临时 Profile 可复用；认证/API/验证码请求沿用现有缓存排除规则，Cookies 保持独立。
- `recorded` 只统计新增缓存文件；已有有效缓存时，即使使用 `record` 模式重新下载，也不会重复写入或增加此计数。
- GptAccount 不安装 Playwright `context.route()`。取得凭证快照后进入 `about:blank`，停止页面后台下载，并保留 CDP target 直到统计结束。
- `network_traffic.local_asset_cache` 保存命中、新增和错误信息。`bytes_saved` 是缓存响应解码后的正文大小（`bytes_saved_basis=decoded_response_body`），不能直接当作代理节省字节。总流量在使用代理中继时仍以中继实际传输为准；直连时使用页面 CDP 统计，缓存命中不计入联网字节。
- 关闭省流模式后，GptAccount 停用这套资源拦截和共享缓存。首次运行、资源 URL 更新或缓存过期时会联网下载，不能保证每次固定 2 MB。

本地真实 Chrome 验证（仅访问本机测试代理提供的页面）覆盖压缩脚本、跨临时 Profile 命中、资源 URL 更新、关闭省流、Cookie 隔离和 API 始终联网：首次传输 133093 B，缓存命中 2575 B，URL 更新 133093 B，关闭省流 134683 B。复跑：

```powershell
$env:GPTACCOUNT_BROWSER_TEST = '1'
.\.venv\Scripts\python.exe -m unittest tests.test_gptaccount_browser.GptAccountCacheBrowserTests -v
Remove-Item Env:\GPTACCOUNT_BROWSER_TEST
```

本机在线验证使用当前直连配置，以下数字均为页面 CDP 统计，不代表代理商计费流量：

- 全新缓存加载 ChatGPT 首页：首次 1.557 MiB，下一次全新 Profile 加载 0.297 MiB，命中 295 次，缓存无错误。
- 已有缓存加载登录页：0.238 MiB，命中 37 次。
- 完整注册任务 #12 成功，密码、Token 和 36 条 Cookie 已保存；当时缓存尚未覆盖登录后的全部资源，总计 7.421 MiB，命中 99 次、新增 38 个资源。
- 缓存充分后的完整注册任务 #14 成功：1.488 MiB，缓存命中 213 次、未命中 1 次、缓存无错误；密码、Token 和 38 条 Cookie 均已保存。该次在验证码输入框暂未显示时由原有流程刷新一次后完成。

相关单元测试 59 项、本地真实 Chrome 测试 5 项通过。4 次在线注册中 #12、#14 成功，#11、#13 在密码页失败，失败原因尚未定位；失败样本不计入完整注册的省流结论。#12、#14 的密码提交、验证码验证和创建账号接口均返回 200。以上仅证明本机热缓存条件下能达到约 2 MB 的量级，不保证首次运行、缓存过期或站点更新后的流量和注册成功率。

Review 修复：GptAccount 的 CDP 统计初始化失败时，记录警告、尝试保留 URL 拦截并继续注册，本轮不启用共享缓存；Cookie 快照失败时也尝试停止页面后台下载，仍向主流程报告原快照错误。两处修复均在 GptAccount 独立文件中，相关单元测试增至 60 项。
