"""网关配置 — 环境变量前缀 OCGW_（OpenClaw GateWay）。

命名空间划分：
- OCGW_SANDBOX_* / OCGW_K8S_*  沙盒编排（参考 eido-ai 的 EIDO_ 对应项）
- OCGW_USER_*                  每用户 OpenClaw Pod 的资源与寻址
- CAS_* / AUTH_DISABLED        认证（与 eido-ai 语义一致）
- ANTHROPIC_*                  透传给用户 OpenClaw 的模型凭据（原生环境变量）
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_cas_server_url(url: str) -> str:
    """规范化 CAS 服务地址：去尾部空白、补尾部 /（拼接 login/serviceValidate 用）。"""
    u = (url or "").strip().rstrip("/")
    return f"{u}/" if u else u


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="OCGW_", extra="ignore")

    # ---------- 沙盒编排 ---------- #
    sandbox_mode: str = "k8s"          # k8s | local（本地调试，不拉 Pod）
    k8s_namespace: str = "openclaw-system"
    k8s_storage_class: str = ""        # 空 = 集群默认 StorageClass
    k8s_image_pull_secret: str = ""    # 私有仓库时网关会把它注入用户 Pod
    k8s_pod_ready_timeout: int = 180   # OpenClaw Pod 首次拉镜像 + 启动的窗口
    k8s_user_storage: str = "5Gi"      # 每用户 PVC（OpenClaw state 目录）
    # 受信代理 CIDR（写入用户 openclaw.json 的 gateway.trustedProxies）。
    # 留空 = 由 OCGW_POD_IP 推导 /16（kind 默认 10.244.0.0/16 正确；
    # 生产环境建议显式配置为集群 Pod CIDR）
    k8s_trusted_proxy_cidrs: str = ""

    # ---------- 用户 OpenClaw Pod ---------- #
    user_image: str = "openclaw:local"
    user_internal_port: int = 18789    # OpenClaw 网关原生端口
    user_mem: str = "2Gi"
    user_cpus: str = "1.0"
    user_tmpfs_size: str = "512Mi"
    sandbox_idle_ttl: int = 900        # 闲置秒数，超过回收 Pod（PVC 保留）
    sandbox_health_ttl: int = 15       # 健康句柄缓存秒数
    registry_db: str = "/data/ocgw/sandbox_registry.db"  # 本地调试可指到可写目录

    # ---------- 认证 ---------- #
    auth_disabled: bool = False
    default_dev_user_id: str = "dev"
    cas_version: str = "3"
    cas_server_url: str = ""
    cas_service_url: str = ""
    frontend_url: str = "/"            # 登录成功后的落地页（默认网关自身根路径）
    session_secret_key: str = ""

    # ---------- 模型凭据（注入用户 Pod，OpenClaw 原生环境变量；可留空） ---------- #
    anthropic_base_url: str = ""
    anthropic_api_key: str = ""
    anthropic_model: str = ""
    anthropic_small_fast_model: str = ""

    @property
    def cas_server_url_normalized(self) -> str:
        return normalize_cas_server_url(self.cas_server_url)


@lru_cache
def get_settings() -> Settings:
    return Settings()
