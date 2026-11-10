# HTTP lab application profile

The explicit `console_http_lab: true` setting (or
`LAUNCHPAD_CONSOLE_HTTP_LAB=true`) allows an isolated demonstration console to use
HTTP as its primary origin while retaining an HTTPS compatibility entry. Keep
`run_mode: prod`: this flag changes browser transport compatibility, not console
identity, authorization, workspace access, password policy, API-key authentication,
or local execution permissions. Its default is `false`; normal production still
uses Secure cookies and HSTS.

## One account and session

In this profile the application reads, writes, and clears one host-only cookie,
`launchpad_lab_session`, with `HttpOnly`, `SameSite=Lax`, `Path=/`, and without
`Secure`. This intentionally overrides `auth_cookie_secure` for the whole profile.
HTTP and HTTPS entries on the **same hostname** share that cookie, regardless of
port. There is no separate session audience, account store, permission model, or
protocol-header-based authentication path. Different IP addresses/hostnames do not
share a host-only cookie.

The distinct cookie name avoids attempting to overwrite an existing Secure
`launchpad_session` from HTTP. Users sign in once after enabling the profile.
The old Secure cookie is ignored while this profile is active; logout clears the
lab cookie across both entries, and password/session epoch invalidation applies
to both. HTTP does not attempt to delete the old Secure cookie. Disabling the
profile restores the old cookie name; an old, unexpired Secure session may still
be valid unless its password/epoch/signing secret has changed. Switching profiles
is not a global session revocation mechanism.

Set `LAUNCHPAD_PUBLIC_BASE_URL` to the HTTP primary origin. Leave
`LAUNCHPAD_OAUTH_RETURN_URL` empty to derive `<primary-origin>/auth/return`.
Application HSTS middleware is disabled in this profile; the reverse proxy must
also omit persistent HSTS and clear any existing policy via `max-age=0` on the
HTTPS compatibility entry. A browser that already cached HSTS may need to visit
that HTTPS entry once before HTTP works on the same hostname. The deployment
profile owns listener, proxy, network, and update/rollback configuration.

## Browser compatibility

Workspace external ID suggestions and chat attachment IDs use native
`crypto.randomUUID` where available and cryptographic `getRandomValues` UUIDv4
otherwise. They do not use a weak random generator.

Copy actions first try the browser clipboard API, then its user-gesture copy
command. If both are unavailable/denied, a dialog offers selected, read-only text
for manual copying. It never reports success without a successful automatic copy.
Closing the dialog clears its text and restores focus; Escape closes only this
dialog, preserving any underlying password-reset result. Text is not stored in
localStorage or another persistent clipboard fallback.

The N13 restore guard enumerates one additional CN edit, category (j):
`frontend/src/v2/pages/skilllab/TasksetEditor.tsx`, introduced in `35a5e229`.
Its copy-example action uses the shared clipboard helper and only shows the success
toast when copying succeeds. Taskset content, validation, submission, and evaluation
are unchanged. The baseline `4e27b1f4`, all other verbatim checks, and executor-removal
checks remain intact.

## External tool authorization

Local console password login does not require OAuth. Future `as_user` tools use
the configured primary return URL; no application change forces a return to an
old HTTPS IP. Runtime/workload identity callback allowlists must be updated when
that URL changes. Providers that refuse HTTP callbacks still require an acceptable
HTTPS callback architecture; this profile cannot waive a provider's rules.
An environment with no active `as_user` specifications does not establish external
OAuth compatibility merely by passing password-login tests. Existing external
Browser Live View endpoints retain their own protocol and SDK requirements.

## Validation

`backend/tests/test_http_lab.py` covers default HTTPS behavior, the explicit flag,
production authentication, shared cookie/login/logout, old cookie coexistence,
password rotation, untrusted forwarded-protocol headers, and the derived HTTP
OAuth return URL. Run existing user authorization tests with
`LAUNCHPAD_CONSOLE_HTTP_LAB=true LAUNCHPAD_RUN_MODE=prod` as an additional check.
`frontend/src/lib/browser-compat.test.ts` covers cryptographic UUIDs and successful,
denied, and manual copy paths, including Escape/focus cleanup. Existing Chat,
workspace, registry, recommendations, and control-inventory checks remain relevant.

Browser acceptance must use a non-localhost HTTP origin: localhost is a browser
secure-context exception and would hide the missing APIs. Verify real login,
refresh, logout, member access, attachment selection, and manual copying; verify
the same host's HTTPS compatibility entry receives the same lab session. Avoid
real model invocations when only transport and UI behavior are under test.

## 中文说明

显式设置 `console_http_lab: true`（环境变量
`LAUNCHPAD_CONSOLE_HTTP_LAB=true`），并继续使用 `run_mode: prod`。默认关闭此模式，
原 HTTPS 部署保持 Secure Cookie 和 HSTS。启用后仍保留生产环境的登录、权限、工作区、
密码策略和 API Key 校验，不会开启本地代码执行权限。

HTTP 与同一主机名的 HTTPS 入口共用 `launchpad_lab_session`，Cookie 保留
`HttpOnly`、`SameSite=Lax`，不带 `Secure`。两个端口共用账号、会话、权限和数据；
不同 IP 或主机名不会共享此 Cookie。此模式全局覆盖 `auth_cookie_secure`，不根据
客户端可伪造的协议头选择认证逻辑。

切换后需要首次重新登录。旧 `launchpad_session` 在实验模式中不读取，也不试图从
HTTP 删除浏览器保护的旧 Secure Cookie。退出会清除两个入口共用的实验 Cookie；
改密与会话版本失效逻辑也同时生效。回滚到原模式后，尚未过期且未撤销的旧 HTTPS
会话仍可能有效；切换模式本身并不代表撤销所有会话。

将 `LAUNCHPAD_PUBLIC_BASE_URL` 设为 HTTP 主入口，清空
`LAUNCHPAD_OAUTH_RETURN_URL` 以继承该入口的 `/auth/return`。应用停止发送 HSTS，
反向代理也应在 HTTPS 兼容入口发送 `max-age=0` 清理历史策略。已有 HSTS 缓存的
浏览器可能需要先访问一次 HTTPS 兼容入口。监听器、网络、升级与回滚由部署配置管理。

前端 UUID 使用密码学安全随机数兼容 HTTP。复制优先使用浏览器 API，其次尝试传统
复制命令；均不可用时显示可选中文本供手工复制，不误报“已复制”。关闭时清空文本、
移除弹窗、恢复焦点；Escape 不会关闭下层密码重置结果框。不会持久化待复制内容。

N13 恢复保护仅新增登记 (j) 项：`TasksetEditor.tsx` 在 `35a5e229` 中的示例复制兼容改动，
改用共享复制方法且仅在成功时提示。题集内容、校验、提交与评估不变；
`4e27b1f4` 基线、其他文件逐字节检查和已删除执行器的保护全部保留。

控制台密码登录与外部工具 OAuth 是不同流程。未来配置 `as_user` 工具时，应更新
运行时的回调白名单；要求 HTTPS 回调的供应商限制仍需单独解决。没有现存
`as_user` 工具的环境不能据此宣称外部 OAuth 已验收；浏览器直播功能也保留其外部
端点与 SDK 协议要求。

验收须使用非 localhost 的 HTTP 地址，因为 localhost 的安全上下文例外会掩盖问题。
验证登录、刷新、退出、成员权限、附件选择、复制，以及同主机名 HTTPS 入口共享会话；
仅验收传输和 UI 时不需要调用真实模型。


## Browser Live View decoder assets

The V2 route `/v2/governance` needs decoder workers beneath
`/v2/governance/nice-dcv-web-client-sdk/dcvjs-esm/`. AgentCore's installed
`BrowserLiveView` wrapper exposes only `signedUrl` and viewport dimensions; its
internal `baseUrl` is fixed. The DCV client normalizes that base into a path relative
to the current route. The build therefore distributes the SDK assets at the root,
the existing classic route, and the V2 route, without patching the SDK.

HTTP uses the SDK's fallback decoder where browser WebCodecs are unavailable.
Missing V2 workers previously returned the SPA HTML with status 200, which
`importScripts` rejected. A same-session comparison showed HTTP stuck connecting,
while HTTPS rendered the remote page. Redirecting only the worker assets restored
the HTTP picture; changing CSP alone did not. The SDK also tries to fetch embedded
`data:` WebAssembly bytes. `connect-src` now permits that one additional scheme to
avoid recoverable decoder fetch errors; script, worker, frame, and object rules stay
unchanged. This is not a general relaxation of script execution.

`npm run build` now runs `scripts/check-dcv-assets.mjs` afterward. It serves the
actual build through Vite's HTTP preview server and checks both decoder workers at
all three URL roots for JavaScript MIME and exact vendor bytes. A status-200 HTML
fallback fails the build. The nginx regression checks the exact CSP source list on
both HTTP and HTTPS. Live acceptance must still verify an actual remote picture on
HTTP, not merely a canvas element or a successful WebSocket handshake.

References: [DCVViewer baseUrl](https://docs.aws.amazon.com/dcv/latest/websdkguide/dcv-viewer.html),
[Vite 6 preview API](https://v6.vite.dev/guide/api-javascript), and
[worker CSP behavior](https://developer.mozilla.org/en-US/docs/Web/HTTP/Reference/Headers/Content-Security-Policy#csp_in_workers).

### 浏览器直播解码兼容

V2 网页需要 `/v2/governance/nice-dcv-web-client-sdk/dcvjs-esm/` 下的解码资源。
安装版 AgentCore 包装组件不提供可配置的 `baseUrl` 参数，DCV 会把内部固定值转换为
当前页面的相对路径。因此构建增加 V2 路径资源副本，保留根路径和原 classic 路径，
不修改 SDK。

HTTP 无法使用浏览器 WebCodecs 时会走回退解码器；缺失的 worker 原本返回状态 200
的 SPA HTML，导致 `importScripts` 失败。同一远端会话的对照已确认：单独修正资源
路径即恢复 HTTP 画面，只修改 CSP 不能恢复。CSP 额外允许 `connect-src data:`，
仅解决 SDK 读取内嵌 WASM 时可恢复的加载错误，不放宽脚本、worker、frame 或对象规则。

构建后的 HTTP 回归验证三个路径的两种 worker 都返回 JavaScript MIME 且字节与
供应商原文件一致；nginx 测试同时覆盖 HTTP 和 HTTPS 的精确 CSP。实际验收还必须
看到 HTTP 下的远端画面，不能以 canvas 存在或 WebSocket 连接成功代替。
