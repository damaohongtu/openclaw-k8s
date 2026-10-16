# openclaw-k8s

CAS 登录后，为每个用户自动拉起一个**原生 OpenClaw 实例**（每用户 1 Pod + 1 PVC）跑在 Kubernetes 上。

只借用了 eido-ai 的"登录 → 拉起容器"模式；Control UI、模型配置、频道、会话等全部保持 OpenClaw 原生能力，用户在浏览器里获得的体验与自建 OpenClaw 完全一致。

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
                    │  ┌─────▼──────┐ ┌───────┐│  每用户 1 Pod
                    │  │ openclaw-  │ │openclaw││  + 1 PVC(状态永存)
                    │  │ user-<id>  │ │-user-…││
                    │  └────────────┘ └───────┘│
   NodePort 31444 ──┼─▶ CAS                      │
                    └─────────────────────────┘
```

- **网关**（`gateway/`）：CAS 单点登录 → 按需创建用户 Pod → HTTP+WebSocket 反向代理；闲置 15 分钟回收 Pod，PVC 保留
- **用户 Pod**（官方 `openclaw/openclaw` 镜像原样运行）：OpenClaw 官方 trusted-proxy 认证模式，CAS 登录后免 token 自动进入原生 Control UI；模型（如 GLM）、MCP、频道等在 UI 里原生配置，持久化在每用户 PVC
- **隔离**：每用户独立 PVC + NetworkPolicy（ingress 仅放行网关）+ 归属校验（拒绝挂载他人数据卷）

## 快速开始

```bash
cd k8s
./gen-secret.sh                # 生成真实凭据（可选预置模型）
# 完整部署步骤见 k8s/README.md
```

详见 [k8s/README.md](k8s/README.md)（部署指南、配置参考、排障）。

## 目录

```
gateway/   FastAPI 网关（CAS 认证、K8s 编排、HTTP+WS 反代）
k8s/       部署清单（kind、RBAC、存储、NetworkPolicy、CAS）+ 部署文档
```
