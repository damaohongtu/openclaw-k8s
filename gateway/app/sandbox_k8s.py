"""K8s 沙盒编排 — 每用户一个原生 OpenClaw Pod + 一个 PVC。

改编自 eido-ai backend/app/gateway/sandbox_k8s.py，语义保持：
- Pod `openclaw-user-<safe>` + PVC `openclaw-user-<safe>`；PVC 永远保留（stop 只删 Pod）
- 归属校验：Pod / PVC 均带 io.openclaw.user_id label，拒绝复用他人资源
- 资源限额 → resources.limits；requests 较小以允许超卖
- Pod IP 寻址（registry 记录 IP，Pod 重建后刷新）

OpenClaw 原生保持的部分：
- 镜像与入口：官方镜像 + `gateway --bind lan --port 18789`（上游 docker-compose 同款）
- 状态目录：OPENCLAW_STATE_DIR=/home/node/.openclaw（挂 PVC）
- init 种子配置：seed-if-missing 最小 openclaw.json（官方 manifests 同款模板），
  空状态目录下网关拒绝启动，必须预置 gateway.mode=local + allowedOrigins
- 探针：官方 exec 探针（node -e 断言 /healthz /readyz JSON body，防止 UI 兜底
  200 导致的假就绪）
- 安全上下文对齐上游 compose：drop NET_RAW/NET_ADMIN、no-new-privileges
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

from app.config import get_settings

logger = logging.getLogger(__name__)

_LABEL_ROLE = "io.openclaw.role"
_LABEL_USER = "io.openclaw.user_id"
_LABEL_ISOLATION = "io.openclaw.isolation_version"

OPENCLAW_PORT = 18789

# 首次启动种子配置（对齐官方 scripts/k8s/manifests/configmap.yaml，多用户适配三处）：
# - gateway.mode=local：空状态目录下网关拒绝启动，必须预置（官方同款守卫）
# - controlUi.allowedOrigins=["*"]：非 loopback 绑定若未显式配 origins 会在启动时硬失败；
#   容器内只会自动种 loopback origins
# - auth.mode=trusted-proxy：OpenClaw 官方"前置反代认证"模式。本网关注入
#   x-forwarded-user（CAS 用户名）等身份头，浏览器用原生设备身份（WebCrypto/
#   纯 JS 密钥对，deviceAutoApprove 自动批准）免 token 直连——CAS 登录后即
#   自动进入 Control UI，无需粘贴 token
# 其余（模型、频道、agents）全部留给用户在原生 Control UI 里自行配置。
#
# 模型预置（可选）：OCGW_ANTHROPIC_BASE_URL/API_KEY/MODEL 三者齐全时注册
# 自定义 provider 并设为默认模型；密钥经 ${ANTHROPIC_API_KEY} env 引用（Pod 内
# 由网关注入），不落盘到 PVC。Anthropic 兼容端点必须用 api: anthropic-messages，
# 且需要显式模型条目——环境变量不会往模型目录里注册任何模型。

# GLM 模型目录元数据（对齐上游 extensions/zai 扩展的 glm-5.2 条目）
_GLM_MODEL_META = {
    "reasoning": True,
    "input": ["text"],
    "contextWindow": 1000000,
    "maxTokens": 131072,
    "cost": {"input": 1.4, "output": 4.4, "cacheRead": 0.26, "cacheWrite": 0},
}
_MODEL_PROVIDER_ID = "glm"

# 对齐上游 CLI_DEFAULT_OPERATOR_SCOPES（src/gateway/method-scopes.ts）：
# 自动批准的浏览器设备拿到的最大 scopes，含 admin——每用户独享实例，本人即管理员
_DEVICE_AUTO_APPROVE_SCOPES = (
    "operator.admin",
    "operator.read",
    "operator.write",
    "operator.approvals",
    "operator.questions",
    "operator.pairing",
    "operator.talk.secrets",
)


def _seed_openclaw_config() -> dict:
    settings = get_settings()
    cfg = {
        "gateway": {
            "mode": "local",
            "bind": "lan",
            "port": OPENCLAW_PORT,
            "auth": {
                "mode": "trusted-proxy",
                "trustedProxy": {
                    "userHeader": "x-forwarded-user",
                    "requiredHeaders": ["x-forwarded-proto"],
                    "allowLoopback": True,
                    # 每用户独享一个 OpenClaw 实例，CAS 是唯一入口：设备自动批准
                    # 直接给全量 operator scopes。默认 scopes 不含 operator.admin，
                    # UI 会按非管理员渲染——设置侧边栏的 MCP/Labs/Automation 等页直接消失
                    "deviceAutoApprove": {
                        "enabled": True,
                        "scopes": list(_DEVICE_AUTO_APPROVE_SCOPES),
                    },
                },
            },
            "trustedProxies": resolve_trusted_proxy_cidrs(),
            "controlUi": {"enabled": True, "allowedOrigins": ["*"]},
        },
        "agents": {
            "defaults": {"workspace": "~/.openclaw/workspace"},
            "list": [
                {"id": "default", "name": "OpenClaw Assistant", "workspace": "~/.openclaw/workspace"}
            ],
        },
        "cron": {"enabled": False},
    }

    if settings.anthropic_base_url and settings.anthropic_api_key and settings.anthropic_model:
        def model_entry(mid: str) -> dict:
            return {"id": mid, "name": mid.upper(), **_GLM_MODEL_META}

        model_ids = [settings.anthropic_model]
        utility = settings.anthropic_small_fast_model or settings.anthropic_model
        if utility not in model_ids:
            model_ids.append(utility)

        cfg["models"] = {
            "mode": "merge",
            "providers": {
                _MODEL_PROVIDER_ID: {
                    "baseUrl": settings.anthropic_base_url,
                    "api": "anthropic-messages",
                    "apiKey": "${ANTHROPIC_API_KEY}",
                    "models": [model_entry(m) for m in model_ids],
                }
            },
        }
        cfg["agents"]["defaults"]["model"] = f"{_MODEL_PROVIDER_ID}/{settings.anthropic_model}"
        cfg["agents"]["defaults"]["utilityModel"] = f"{_MODEL_PROVIDER_ID}/{utility}"
    return cfg

# seed-if-missing：PVC 上的副本在首次引导后即归用户所有（UI/onboard/channels 的修改跨重启保留）
_SEED_SCRIPT_TEMPLATE = """set -e
mkdir -p /home/node/.openclaw/workspace /home/node/.openclaw/auth-profile-secrets
if [ ! -f /home/node/.openclaw/openclaw.json ]; then
cat > /home/node/.openclaw/openclaw.json <<'OPENCLAW_JSON'
__CONFIG__
OPENCLAW_JSON
echo '[seed-config] wrote initial openclaw.json'
fi"""

# 官方探针契约：断言 JSON body 而非仅状态码——Control UI 对未知路径兜底返回 200，
# 纯状态码探针在路由缺失时会永远通过（官方 manifests 同款 node -e 写法）
_PROBE_READY_JS = (
    "require('http').get('http://127.0.0.1:__PORT__/readyz', r => { let b = '';"
    " r.on('data', d => b += d); r.on('end', () => { try { process.exit("
    "r.statusCode < 400 && JSON.parse(b).ready === true ? 0 : 1); } catch { process.exit(1); } }); })"
    ".on('error', () => process.exit(1))"
)
_PROBE_HEALTH_JS = (
    "require('http').get('http://127.0.0.1:__PORT__/healthz', r => { let b = '';"
    " r.on('data', d => b += d); r.on('end', () => { try { process.exit("
    "r.statusCode < 400 && JSON.parse(b).ok === true ? 0 : 1); } catch { process.exit(1); } }); })"
    ".on('error', () => process.exit(1))"
)


def resolve_trusted_proxy_cidrs() -> list[str]:
    """解析受信代理 CIDR 列表（写入用户 openclaw.json 的 gateway.trustedProxies）。

    显式配置优先；否则由本网关 Pod IP（downward API 注入 OCGW_POD_IP）推导 /16。
    """
    import os

    configured = get_settings().k8s_trusted_proxy_cidrs.strip()
    if configured:
        return [c.strip() for c in configured.split(",") if c.strip()]

    pod_ip = os.environ.get("OCGW_POD_IP", "").strip()
    parts = pod_ip.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return [f"{parts[0]}.{parts[1]}.0.0/16"]
    return ["127.0.0.1"]


def _seed_script() -> str:
    import json

    config = json.dumps(_seed_openclaw_config(), indent=2)
    return _SEED_SCRIPT_TEMPLATE.replace("__CONFIG__", config)


def _probe_script(template: str) -> list[str]:
    return ["node", "-e", template.replace("__PORT__", str(OPENCLAW_PORT))]


@dataclass(frozen=True)
class PodState:
    phase: str
    pod_ip: Optional[str]
    ready: bool


def _pod_state(pod) -> PodState:
    phase = pod.status.phase or "Unknown"
    pod_ip = (pod.status.pod_ip or "").strip() or None
    ready = False
    for cond in pod.status.conditions or []:
        if cond.type == "Ready":
            ready = bool(cond.status == "True")
            break
    return PodState(phase=phase, pod_ip=pod_ip, ready=ready)


def _k8s_quantity(value: str, default: str) -> str:
    """兼容 docker 风格后缀（2g/512m）→ K8s quantity（2Gi/512Mi）。"""
    v = (value or "").strip() or default
    return v.replace("g", "Gi").replace("m", "Mi")


class K8sSandboxClient:
    """通过 Kubernetes API 管理每用户 OpenClaw Pod / PVC。"""

    def __init__(self, *, namespace: Optional[str] = None):
        self._namespace = namespace or get_settings().k8s_namespace
        self._core = None

    # -------------------------------------------------------------- #
    #  连接                                                           #
    # -------------------------------------------------------------- #

    def connect(self) -> None:
        try:
            import urllib3

            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        except Exception:  # pragma: no cover - 仅静音告警
            pass

        try:
            from kubernetes import client as k8s_client
            from kubernetes import config as k8s_config

            try:
                k8s_config.load_incluster_config()
                logger.info("K8s 沙盒后端：使用 in-cluster ServiceAccount 凭据")
            except Exception:
                k8s_config.load_kube_config()
                logger.warning("K8s 沙盒后端：in-cluster 凭据不可用，回退本机 kubeconfig（仅限调试）")
            self._core = k8s_client.CoreV1Api()
            self._core.list_namespaced_pod(self._namespace, limit=1, _request_timeout=10)
        except Exception as e:
            self._core = None
            raise RuntimeError(
                f"Kubernetes API 不可用（namespace={self._namespace}），拒绝以 k8s 沙盒模式启动"
            ) from e
        logger.info(f"✓ K8s 沙盒后端就绪 namespace={self._namespace}")

    def close(self) -> None:
        self._core = None

    @property
    def namespace(self) -> str:
        return self._namespace

    # -------------------------------------------------------------- #
    #  PVC                                                            #
    # -------------------------------------------------------------- #

    def _ensure_pvc(self, user_id: str, safe: str) -> str:
        from kubernetes import client as k8s_client
        from kubernetes.client import ApiException

        settings = get_settings()
        name = f"openclaw-user-{safe}"
        try:
            pvc = self._core.read_namespaced_persistent_volume_claim(name, self._namespace)
            owner = (pvc.metadata.labels or {}).get(_LABEL_USER)
            if owner and owner != user_id:
                raise RuntimeError("拒绝挂载其他用户的数据卷")
            if not owner:
                raise RuntimeError("无法确认旧数据卷归属，拒绝自动挂载")
            return name
        except ApiException as e:
            if e.status != 404:
                raise

        pvc = k8s_client.V1PersistentVolumeClaim(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                labels={_LABEL_ROLE: "user-data", _LABEL_USER: user_id},
            ),
            spec=k8s_client.V1PersistentVolumeClaimSpec(
                access_modes=["ReadWriteOnce"],
                storage_class_name=settings.k8s_storage_class or None,
                resources=k8s_client.V1ResourceRequirements(
                    requests={"storage": _k8s_quantity(settings.k8s_user_storage, "5Gi")}
                ),
            ),
        )
        try:
            self._core.create_namespaced_persistent_volume_claim(self._namespace, pvc)
            logger.info(f"创建用户 PVC {name}（{settings.k8s_user_storage}）")
        except ApiException as e:
            if e.status != 409:  # 并发创建竞态：已存在则复用（重新读取并校验归属）
                raise
            created = self._core.read_namespaced_persistent_volume_claim(name, self._namespace)
            owner = (created.metadata.labels or {}).get(_LABEL_USER)
            if owner != user_id:
                raise RuntimeError("拒绝挂载其他用户的数据卷")
        return name

    # -------------------------------------------------------------- #
    #  Pod                                                            #
    # -------------------------------------------------------------- #

    def _get_pod(self, name: str):
        from kubernetes.client import ApiException

        try:
            return self._core.read_namespaced_pod(name, self._namespace)
        except ApiException as e:
            if e.status == 404:
                return None
            raise

    def _build_pod(self, user_id: str, safe: str, name: str, env: dict):
        from kubernetes import client as k8s_client

        settings = get_settings()
        try:
            cpus = float(settings.user_cpus or 1.0)
        except Exception:
            cpus = 1.0
        mem = _k8s_quantity(settings.user_mem, "2Gi")
        tmpfs = _k8s_quantity(settings.user_tmpfs_size, "512Mi")

        env_vars = [k8s_client.V1EnvVar(name=k, value=str(v)) for k, v in env.items()]
        pull_secrets = (
            [k8s_client.V1LocalObjectReference(name=settings.k8s_image_pull_secret)]
            if settings.k8s_image_pull_secret.strip()
            else []
        )

        container = k8s_client.V1Container(
            name="openclaw",
            image=settings.user_image,
            image_pull_policy="IfNotPresent",
            # 覆盖镜像 CMD（ENTRYPOINT tini 保留）——上游 docker-compose 同款启动参数
            args=["node", "openclaw.mjs", "gateway", "--bind", "lan", "--port", str(OPENCLAW_PORT)],
            env=env_vars,
            ports=[k8s_client.V1ContainerPort(container_port=OPENCLAW_PORT, name="http")],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": "100m", "memory": "512Mi"},
                limits={"cpu": cpus, "memory": mem},
            ),
            # startup 用 /readyz（官方清单同款；startupz 需镜像版本对齐，readyz 语义等价且保守）
            startup_probe=k8s_client.V1Probe(
                _exec=k8s_client.V1ExecAction(command=_probe_script(_PROBE_READY_JS)),
                period_seconds=10,
                timeout_seconds=5,
                failure_threshold=30,
            ),
            readiness_probe=k8s_client.V1Probe(
                _exec=k8s_client.V1ExecAction(command=_probe_script(_PROBE_READY_JS)),
                initial_delay_seconds=15,
                period_seconds=10,
                timeout_seconds=5,
            ),
            liveness_probe=k8s_client.V1Probe(
                _exec=k8s_client.V1ExecAction(command=_probe_script(_PROBE_HEALTH_JS)),
                initial_delay_seconds=60,
                period_seconds=30,
                timeout_seconds=10,
            ),
            security_context=k8s_client.V1SecurityContext(
                allow_privilege_escalation=False,
                capabilities=k8s_client.V1Capabilities(drop=["NET_RAW", "NET_ADMIN"]),
            ),
            volume_mounts=[
                # OpenClaw 原生状态目录（openclaw.json / credentials / sessions / logs）
                k8s_client.V1VolumeMount(name="state", mount_path="/home/node/.openclaw"),
                # 原生 auth profile secrets（上游 compose 单独挂卷，这里放同一 PVC 的子目录）
                k8s_client.V1VolumeMount(
                    name="state",
                    mount_path="/home/node/.config/openclaw",
                    sub_path="auth-profile-secrets",
                ),
                k8s_client.V1VolumeMount(name="tmp", mount_path="/tmp"),
            ],
        )

        # init：seed-if-missing 最小 openclaw.json（复用 OpenClaw 镜像，节点上已缓存，免拉 busybox）
        init_container = k8s_client.V1Container(
            name="seed-config",
            image=settings.user_image,
            image_pull_policy="IfNotPresent",
            command=["sh", "-c", _seed_script()],
            resources=k8s_client.V1ResourceRequirements(
                requests={"cpu": "10m", "memory": "32Mi"},
                limits={"cpu": "100m", "memory": "64Mi"},
            ),
            security_context=k8s_client.V1SecurityContext(allow_privilege_escalation=False),
            volume_mounts=[
                k8s_client.V1VolumeMount(name="state", mount_path="/home/node/.openclaw"),
            ],
        )

        return k8s_client.V1Pod(
            metadata=k8s_client.V1ObjectMeta(
                name=name,
                labels={
                    "app.kubernetes.io/name": "openclaw-user",
                    "app.kubernetes.io/managed-by": "openclaw-gateway",
                    _LABEL_ROLE: "user-sandbox",
                    _LABEL_ISOLATION: "1",
                    _LABEL_USER: user_id,
                },
            ),
            spec=k8s_client.V1PodSpec(
                restart_policy="Always",
                image_pull_secrets=pull_secrets or None,
                service_account_name=None,
                # OpenClaw 镜像原生 node 用户 uid/gid 1000；fsGroup 让 PVC 可写
                security_context=k8s_client.V1PodSecurityContext(
                    run_as_user=1000,
                    run_as_group=1000,
                    fs_group=1000,
                ),
                init_containers=[init_container],
                containers=[container],
                volumes=[
                    k8s_client.V1Volume(
                        name="state",
                        persistent_volume_claim=k8s_client.V1PersistentVolumeClaimVolumeSource(
                            claim_name=f"openclaw-user-{safe}"
                        ),
                    ),
                    k8s_client.V1Volume(
                        name="tmp",
                        empty_dir=k8s_client.V1EmptyDirVolumeSource(medium="Memory", size_limit=tmpfs),
                    ),
                ],
            ),
        )

    def _wait_ready(self, name: str, *, timeout: float) -> PodState:
        from kubernetes.client import ApiException

        deadline = time.monotonic() + timeout
        last: Optional[PodState] = None
        while time.monotonic() < deadline:
            try:
                pod = self._get_pod(name)
            except ApiException as e:
                raise RuntimeError(f"读取 user Pod 失败: {name} ({e.reason})") from e
            if pod is None:
                raise RuntimeError(f"user Pod 意外消失: {name}")
            state = _pod_state(pod)
            last = state
            if state.ready and state.pod_ip:
                return state
            if state.phase in ("Failed", "Succeeded"):
                break
            time.sleep(1.0)
        phase = last.phase if last else "Unknown"
        raise RuntimeError(
            f"user Pod 未在 {int(timeout)}s 内就绪: {name} phase={phase} "
            f"(events: kubectl -n {self._namespace} describe pod {name})"
        )

    def ensure(self, user_id: str, safe: str, env: dict) -> str:
        """幂等启动 user Pod，返回可寻址的 Pod IP。"""
        from kubernetes.client import ApiException

        settings = get_settings()
        name = f"openclaw-user-{safe}"
        self._ensure_pvc(user_id, safe)

        existing = self._get_pod(name)
        if existing is not None:
            labels = existing.metadata.labels or {}
            if labels.get(_LABEL_USER) != user_id:
                raise RuntimeError("拒绝复用不属于当前用户的容器")
            if existing.metadata.deletion_timestamp is None and _pod_state(existing).phase in (
                "Failed",
                "Succeeded",
            ):
                self._delete_pod(name)  # 终态 Pod 不会恢复，删除后走重建
            else:
                state = self._wait_ready(name, timeout=settings.k8s_pod_ready_timeout)
                logger.info(f"复用 user Pod {name} ip={state.pod_ip}")
                return state.pod_ip

        pod = self._build_pod(user_id, safe, name, env)
        try:
            self._core.create_namespaced_pod(self._namespace, pod)
        except ApiException as e:
            if e.status != 409:
                raise
            logger.info(f"user Pod 已被并发创建: {name}")
        state = self._wait_ready(name, timeout=settings.k8s_pod_ready_timeout)
        logger.info(f"启动 user Pod user={user_id} name={name} ip={state.pod_ip}")
        return state.pod_ip

    def _delete_pod(self, name: str) -> None:
        from kubernetes.client import ApiException

        try:
            self._core.delete_namespaced_pod(name, self._namespace, grace_period_seconds=30)
        except ApiException as e:
            if e.status != 404:
                raise

    def stop(self, user_id: str, safe: str) -> bool:
        """删除 user Pod（PVC 保留）。归属不符时抛错。"""
        name = f"openclaw-user-{safe}"
        pod = self._get_pod(name)
        if pod is None:
            return False
        labels = pod.metadata.labels or {}
        if labels.get(_LABEL_USER) != user_id:
            raise RuntimeError("拒绝停止归属不匹配的容器")
        self._delete_pod(name)
        return True

    def is_running(self, user_id: str, safe: str) -> bool:
        name = f"openclaw-user-{safe}"
        pod = self._get_pod(name)
        if pod is None:
            return False
        return pod.metadata.deletion_timestamp is None and _pod_state(pod).phase in (
            "Pending",
            "Running",
        )
