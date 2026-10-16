# openclaw-k8s 部署指南

登录用户自动获得**专属原生 OpenClaw 实例**(每用户一个 Pod + 一个 PVC),网关只做三件事:CAS 认证、Pod/PVC 编排、反向代理(HTTP + WebSocket)。Control UI、模型配置、频道、会话等全部由用户自己的 OpenClaw 原生提供。

## 架构

```
浏览器 ──http://localhost:19081──┐
                                 ▼
                    ┌─────────────────────────┐
                    │  kind: openclaw          │
                    │  ┌────────────────────┐  │
   NodePort 30080 ──┼─▶│ openclaw-gateway   │  │  FastAPI(单副本)
                    │  │ CAS 认证 / 编排 /  │  │  SQLite 注册表(自身 PVC)
                    │  │ HTTP+WS 反向代理   │  │
                    │  └─────┬──────────────┘  │
                    │        │ Pod IP:18789    │
                    │  ┌─────▼──────┐ ┌───────┐│
                    │  │ openclaw-  │ │openclaw││  每用户 1 Pod
                    │  │ user-<id>  │ │-user-…││  + 1 PVC(状态永存)
                    │  └────────────┘ └───────┘│
   NodePort 31444 ──┼─▶ CAS(test1/123456)      │
                    └─────────────────────────┘
```

- **网关**(`openclaw-gateway`):CAS 登录 → 按需拉起用户 Pod → 把请求(含 WS)代理到 `PodIP:18789`;闲置 15 分钟回收 Pod(PVC 保留,数据不丢)
- **用户 Pod**(`openclaw-user-<safe-id>`):官方 OpenClaw 镜像原样运行,`--bind lan --port 18789`,init 容器仅在首次引导时写入最小 `openclaw.json`(可选预置默认模型,其余配置由用户在原生 UI 里改,持久化在 PVC)
- **免 token 自动进入**:用户 Pod 的 OpenClaw 以官方 trusted-proxy 认证模式运行,网关注入 `x-forwarded-user` 等受信身份头;浏览器用 Control UI 原生设备身份(自动生成的密钥对,deviceAutoApprove 自动批准)直连。CAS 登录成功即进入 Control UI,全程无 token 复制粘贴

## 前置条件

- Docker Desktop(≥ 4GB 可用内存)
- kind、kubectl
- 本地 OpenClaw 源码在 `/Users/mao/workspace/openclaw`(仅本文档示例路径引用)

## 部署步骤

### 1. 生成凭据

```bash
cd k8s
./gen-secret.sh                # 生成 30-secret.yaml(已被 gitignore)
# 可选:预置模型凭据(留空则用户在 Control UI 自行配置,原生方式)
# 非 Anthropic 官方端点(GLM/DeepSeek 等)需同时指定模型名
ANTHROPIC_BASE_URL=https://open.bigmodel.cn/api/anthropic ANTHROPIC_API_KEY=... ANTHROPIC_MODEL=glm-5.2 ./gen-secret.sh
```

### 2. 创建 kind 集群并拉镜像

```bash
kind create cluster --config k8s/kind-openclaw.yaml

# 网关镜像:构建后导入
docker build -t damaohongtu/openclaw-gateway:latest -f gateway/Dockerfile gateway/
kind load docker-image damaohongtu/openclaw-gateway:latest --name openclaw

# 用户 OpenClaw 镜像:官方镜像导入(改用镜像站可加前缀 docker.1ms.run/)
docker pull openclaw/openclaw:latest
kind load docker-image openclaw/openclaw:latest --name openclaw
```

> 40-gateway.yaml 中 `OCGW_USER_IMAGE` 必须与导入镜像名一致。

### 3. 应用清单

```bash
kubectl apply -f k8s/00-namespace.yaml
kubectl apply -f k8s/10-rbac.yaml
kubectl apply -f k8s/20-storage.yaml
kubectl apply -f k8s/25-user-netpol.yaml
kubectl apply -f k8s/30-secret.yaml
kubectl apply -f k8s/40-gateway.yaml
kubectl apply -f k8s/50-cas.yaml
kubectl -n openclaw-system rollout status deploy/openclaw-gateway --timeout=300s
```

### 4. 本机 hosts(浏览器可达 CAS)

```bash
sudo sh -c 'echo "127.0.0.1 cas.openclaw-system.svc.cluster.local" >> /etc/hosts'
```

浏览器与 Pod 必须用**同一 URL** 访问 CAS:浏览器经 hosts + NodePort 31444,Pod 经集群 DNS + 同一 NodePort(50-cas.yaml 用 `port == nodePort` 保证两边一致)。

### 5. 验证端到端

```bash
kubectl -n openclaw-system get pods -w
```

浏览器打开 <http://localhost:19081/>(注意必须 19081,10080 是浏览器封禁端口):

1. 跳转 CAS → 输入 `test1` / `123456` → 回跳
2. 网关按需拉起 `openclaw-user-*` Pod(首次拉镜像可能需要 1-2 分钟)
3. 回跳后**自动进入**原生 Control UI(无需任何 token);模型已预置时直接发消息即可对话,频道(Telegram 默认长轮询,无需入站)、会话、技能等在 UI 里原生配置

## 配置参考(40-gateway.yaml 环境变量)

| 变量 | 默认 | 说明 |
|---|---|---|
| `OCGW_USER_IMAGE` | `openclaw/openclaw:2026.8.1` | 用户 Pod 镜像(与 kind load 名一致) |
| `OCGW_K8S_USER_STORAGE` | `5Gi` | 每用户 PVC 容量 |
| `OCGW_USER_MEM` / `OCGW_USER_CPUS` | `2Gi` / `1.0` | 用户 Pod 资源限额 |
| `OCGW_SANDBOX_IDLE_TTL` | `900` | 闲置秒数后回收 Pod(PVC 保留) |
| `OCGW_CAS_SERVER_URL` | 集群内 CAS | 浏览器与 Pod 都需可达 |
| `OCGW_CAS_SERVICE_URL` | `http://localhost:19081/ocgw/auth/callback` | CAS 回调地址 |
| `OCGW_FRONTEND_URL` | `http://localhost:19081/` | 登录后落地页 |
| `OCGW_K8S_TRUSTED_PROXY_CIDRS` | 空(由 Pod IP 推导 /16) | 写入用户 openclaw.json 的 `gateway.trustedProxies`;生产环境应显式设为集群 Pod CIDR |

Secret 键(30-secret.yaml,均带 `OCGW_` 前缀经 envFrom 注入):

- `OCGW_SESSION_SECRET_KEY` — 会话签名;重生成 = 所有登录失效
- `OCGW_ANTHROPIC_BASE_URL` / `OCGW_ANTHROPIC_API_KEY` — 可选,注入用户 Pod(OpenClaw 原生环境变量)
- `OCGW_ANTHROPIC_MODEL` / `OCGW_ANTHROPIC_SMALL_FAST_MODEL` — 可选,模型名;非 Anthropic 官方端点必须设置,否则默认请求 claude-* 型号会被拒

## 设计要点(为什么这样保持原生)

- **单副本 + Recreate**:网关注册表是 PVC 上的 SQLite,多副本脑裂
- **trusted-proxy 认证(OpenClaw 官方模式)**:用户 Pod 种子配置 `auth.mode: "trusted-proxy"` + `deviceAutoApprove`;网关注入 `x-forwarded-user`(CAS 登录名)/`x-forwarded-proto`/`x-forwarded-for`,浏览器用 UI 原生设备身份(自动生成密钥对)免 token 直连。认证是 OpenClaw 自己做的,网关只是被信任的反代
- **deviceAutoApprove.scopes 显式给全量(含 operator.admin)**:每用户独享实例,本人即管理员。默认 scopes 不含 `operator.admin`,UI 会按"非管理员"渲染——设置侧边栏的 MCP/Labs/Automation/Secrets 等页全部隐藏。显式含 admin 会触发 OpenClaw 启动时的 SECURITY WARNING(针对多用户共享网关的场景),本架构下 CAS 是唯一入口、每用户一个 Pod,属预期;需要更细粒度可改用 `gateway.auth.identityScopes` 按身份授权
- **防头伪造**:代理剥离客户端带来的 `X-Forwarded-*`/`Forwarded`/`X-Real-IP`/`X-Openclaw-Scopes` 等受信头后再注入网关自己的——浏览器不可能冒充别人或自报权限
- **trustedProxies CIDR**:网关 Pod IP(downward API `OCGW_POD_IP`)推导同 /16;跨网段部署时用 `OCGW_K8S_TRUSTED_PROXY_CIDRS` 显式配置
- **种子配置多用户适配**(其余照抄官方 k8s 模板):`gateway.mode: "local"`(空状态目录拒绝启动的官方守卫)、`controlUi.allowedOrigins: ["*"]`(非 loopback 绑定必须显式 origins;对外仍有 CAS 会话层)、可选**预置默认模型**(见下)
- **默认模型预置(可选)**:`OCGW_ANTHROPIC_BASE_URL/API_KEY/MODEL` 三件套齐全时,种子配置注册自定义 provider(`api: anthropic-messages`,密钥经 `${ANTHROPIC_API_KEY}` env 引用不落盘 PVC)并写入 `agents.defaults.model`/`utilityModel`——登录即可对话。环境变量**不会**往 OpenClaw 模型目录注册模型,必须走 `models.providers` 配置;留空则完全交给用户在 Control UI 里原生配置
- **NetworkPolicy 隔离用户 Pod**(25-user-netpol.yaml):ingress 只放行网关;kind 默认 CNI 不执行,生产集群生效
- **exec 探针断言 JSON body**:Control UI 对未知路径兜底 200,纯状态码探针会假就绪(官方 manifests 同款处理)
- **PVC 永不删除**:回收只删 Pod;用户在 UI 里改的配置、凭据、会话历史跨重启完整保留

## 排障

```bash
# 网关日志(CAS/PVC/Pod 编排问题)
kubectl -n openclaw-system logs deploy/openclaw-gateway --tail=100

# 用户 Pod 卡在 Init
kubectl -n openclaw-system describe pod -l io.openclaw.role=user-sandbox

# 用户 OpenClaw 启动失败(配置)
kubectl -n openclaw-system logs -l io.openclaw.role=user-sandbox -c openclaw --tail=50

# 手动触发某用户沙盒重建(数据在 PVC,不丢)
kubectl -n openclaw-system delete pod -l io.openclaw.user_id=test1
```

常见问题:

- **403 proxy_attribution_required / 登录门反复出现**:网关 Pod IP 不在用户 openclaw.json 的 `gateway.trustedProxies` CIDR 内(网关被调度到别的网段,或用户改过该配置)。临时解法删用户 Pod 重建不会重写配置(PVC 里有旧文件);根治是设 `OCGW_K8S_TRUSTED_PROXY_CIDRS` 为完整 Pod CIDR 并重置该用户 PVC 里的 openclaw.json
- **浏览器报"无法连接"但 Pod 正常**:看网关日志 WS 桥接是否 403——同上,trustedProxies 不匹配
- **用户 Pod 起不来 + 日志提 `allowedOrigins`**:PVC 里的 openclaw.json 被改坏,删 PVC 内该字段或整个重置(会丢该用户配置)
- **CAS 回调 401**:检查 hosts 配置与 `OCGW_CAS_SERVICE_URL` 是否精确一致
- **回收后又立即被拉起**:正常——浏览器还开着 Control UI(WS 长连接触发 ensure),关页面即闲置
