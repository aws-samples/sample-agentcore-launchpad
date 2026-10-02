# 在控制台之外调用 JWT 入站认证的 Agent

[English version](README.md)

部署为**入站认证 = JWT** 的 Agent，其 AgentCore Runtime 上带有
`customJWTAuthorizer`。这样的 Runtime 会拒绝 SigV4——所有调用方改为通过
Runtime 数据面 HTTPS 端点发送 OAuth2/OIDC **Bearer 令牌**（AWS SDK 无法发送
Bearer 令牌）：

```
POST https://bedrock-agentcore.{region}.amazonaws.com/runtimes/{URL 编码的 Runtime ARN}/invocations?qualifier=DEFAULT
Authorization: Bearer <jwt>
Content-Type: application/json
X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: <会话 id，≥ 33 字符>

{"prompt": "...", "actor_id": "..."}
```

调用本身不涉及任何 AWS 凭证——JWT 就是全部认证。授权器校验的内容：

| 授权器字段 | 令牌声明 | 含义 |
|---|---|---|
| `discoveryUrl` → issuer | `iss` | 令牌必须由该 IdP 签发 |
| `allowedClients` | `client_id` | 签发令牌的应用客户端 |
| `allowedAudience` | `aud` | 目标受众 |
| `allowedScopes` | `scope` | 授予的作用域 |
| `customClaims` | 任意声明 | 例如 `cognito:groups CONTAINS_ANY [platform-admin]` |

令牌不满足任一已配置规则的请求返回 **HTTP 403**；未携带令牌的请求返回
**HTTP 401**，并带有指向该 Runtime 受保护资源元数据的 `WWW-Authenticate` 头。

## 文件

- `invoke_with_jwt.py`——获取 Cognito 令牌（client_credentials **或**
  USER_PASSWORD_AUTH，也接受现成的 `--token`）并调用 Runtime，流式增量随到随打印。
- `invoke_with_jwt.sh`——同一请求的 curl 版本；令牌获取方法见文件头注释。

## 使用 Launchpad 工作区用户池快速开始

Launchpad 引导过程会创建一个 Cognito 用户池和两个应用客户端，控制台建议的
JWT 默认配置已把它们预先列入 `allowedClients`：

- **控制台客户端**（无密钥，`USER_PASSWORD_AUTH`）——人类身份，令牌携带
  `username` 与 `cognito:groups`；
- **M2M 客户端**（有密钥，`client_credentials`）——机器身份，也是平台自身
  非交互调用（公共 `/v1` API、评测运行）使用的客户端。

机器调用方（client_credentials）：

```bash
python3 invoke_with_jwt.py \
  --agent-arn "$AGENT_ARN" --region us-west-2 \
  --client-id "$M2M_CLIENT_ID" --client-secret "$M2M_CLIENT_SECRET" \
  --token-url "https://<domain>.auth.us-west-2.amazoncognito.com/oauth2/token" \
  --prompt "hello"
```

令牌端点主机是用户池的托管 UI 域名——可从用户池的发现文档读取
（`https://cognito-idp.<region>.amazonaws.com/<pool id>/.well-known/openid-configuration`
中的 `token_endpoint` 字段）。

人类调用方（USER_PASSWORD_AUTH；客户端须允许该流程）：

```bash
python3 invoke_with_jwt.py \
  --agent-arn "$AGENT_ARN" --region us-west-2 \
  --client-id "$CONSOLE_CLIENT_ID" --username demo --password '…' \
  --prompt "hello"
```

## 记忆 actor 与控制台

Runtime 从请求体的 `actor_id` 读取记忆 actor，而不是从令牌中读取。Launchpad
按 Agent 隔离记忆：控制台对话在两种模式下都发送 `<agent id>__<username>`，
即“以用户身份调用”开关（用户自己的 Cognito JWT）和平台 M2M 令牌。因此直接
调用方传入 `--actor-id <agent id>__<username>`（Shell 脚本用 `ACTOR_ID=`）即可
共享该控制台用户的短期与长期记忆，其他取值则各自独立分区。授权方不会把
`actor_id` 与令牌主体绑定；若调用方之间不得互访记忆，请在服务端推导 actor id
（见 [docs/identity.md](../../docs/identity.md)）。

控制台中 Agent 页面的“入站认证”卡片会给出针对该 Runtime 的现成
client_credentials curl 示例。

## 自定义声明

诸如 `cognito:groups` `STRING_ARRAY` `CONTAINS_ANY` `[platform-admin]` 的
`custom_claims` 规则，只放行组列表包含任一给定值的令牌。Cognito **用户**令牌
携带 `cognito:groups`，而 **client_credentials** 令牌没有——因此这类规则可以
干净地区分人类调用方与机器调用方（除非另有规则放行，机器会被拒绝）。

## 适配企业 IdP（Entra ID／Okta／任意 OIDC 提供方）

> 通用指引——本仓库未针对真实企业租户测试过，请在你自己的环境中验证。

1. **发现 URL**——你租户的 OIDC 发现文档，例如
   `https://login.microsoftonline.com/<tenant-id>/v2.0/.well-known/openid-configuration`
   （Entra ID）或 `https://<org>.okta.com/oauth2/default/.well-known/openid-configuration`
   （Okta）。必须以 `/.well-known/openid-configuration` 结尾并暴露
   `jwks_uri`——控制台在保存时会探测该文档。若该 IdP 已是工作区的 OAuth2
   连接，可在任一 JWT 编辑处（向导、工作区默认值、智能体页面的“切换为 JWT”
   对话框）使用“从连接选择”自动填入发现 URL。
2. **允许的客户端／受众**——为每个**调用方**注册一个应用（Entra 的 "app
   registration"、Okta 的 "app integration"），把它的客户端 id 列入
   `allowed_clients`；若 IdP 签发的令牌带有独立的 `aud`（Entra 常用
   Application ID URI），把它列入 `allowed_audience`。不要填连接自身的客户端
   id：那是智能体的出站客户端（调用工具时使用），不是入站调用方。
3. **机器调用方**——对你租户的令牌端点使用 client-credentials 授权；
   `invoke_with_jwt.py` 的 `--token-url` 参数接受任何 OAuth2 令牌端点。
4. **人类调用方**——通过常规 OIDC 流程（授权码 + PKCE）获取用户访问令牌，
   以 `--token` 传入。
5. **声明**——把 IdP 的组／角色声明（Entra：`groups` 或 `roles`；Okta：自定义
   声明）映射为 `custom_claims` 规则；先解码一个令牌确认确切的声明名称，
   因为不同 IdP 和租户配置的名称各不相同。

> **控制台无法调用使用其他 IdP 的智能体。** 控制台对话（“以用户身份调用”和
> 默认的 M2M 路径）、`/v1` API、直接调用和评估都携带**工作区 Cognito** 令牌，
> 因此授权器信任其他签发者的智能体会拒绝它们。Launchpad 会提前以
> `409 agent.inbound_issuer_mismatch` 明确报错，并在每个 JWT 编辑处给出警告。
> 这样的智能体只能由持有其 IdP 令牌的外部调用方调用，例如使用本目录中的脚本。
