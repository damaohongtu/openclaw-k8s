"""Sandbox Manager — 每用户 OpenClaw Pod 编排（K8s 后端）。

改编自 eido-ai backend/app/gateway/sandbox_manager.py（仅保留 k8s 路径）：
- ensure_running(user_id) 幂等启动并返回 SandboxHandle
- retain/release 活跃租约；idle_gc_loop 按 OCGW_SANDBOX_IDLE_TTL 回收
- registry（SQLite）记录 Pod IP，Pod 重建后自动刷新

与 eido-ai 的差异：user 容器是原生 OpenClaw 网关（trusted-proxy 认证，
身份由网关注入的 x-forwarded-* 头提供）；环境变量只注入 OpenClaw 原生项。
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from app.config import get_settings
from app.safe_user import safe_user_id
from app.sandbox_k8s import K8sSandboxClient, OPENCLAW_PORT

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SandboxHandle:
    user_id: str
    pod_name: str
    internal_host: str
    internal_port: int
    status: str
    last_active_at: float

    @property
    def base_url(self) -> str:
        return f"http://{self.internal_host}:{self.internal_port}"


_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS sandbox_registry (
    user_id TEXT PRIMARY KEY,
    safe_user_id TEXT NOT NULL,
    pod_name TEXT NOT NULL,
    internal_host TEXT NOT NULL,
    internal_port INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'unknown',
    last_active_at REAL NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class SandboxManager:
    def __init__(self, *, mode: Optional[str] = None):
        self._mode = (mode or get_settings().sandbox_mode or "local").lower()
        self._db_path = get_settings().registry_db
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn: Optional[sqlite3.Connection] = None
        self._k8s: Optional[K8sSandboxClient] = None
        self._lock = threading.RLock()
        self._gc_task: Optional[asyncio.Task] = None
        self._gc_stop = asyncio.Event()
        self._user_locks: dict[str, threading.RLock] = {}
        self._active_requests: dict[str, int] = {}
        self._healthy: dict[str, tuple[float, SandboxHandle]] = {}

    # -------------------------------------------------------------- #
    #  Lifecycle                                                       #
    # -------------------------------------------------------------- #

    def connect(self) -> None:
        settings = get_settings()
        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(_REGISTRY_SCHEMA)
        self._conn.commit()
        logger.info(f"SandboxManager registry connected: {self._db_path} mode={self._mode}")

        if self._mode == "k8s":
            if not settings.session_secret_key or settings.session_secret_key == "change-me":
                raise RuntimeError("k8s 沙盒模式禁止使用默认 SESSION_SECRET_KEY，请显式配置随机密钥")
            client = K8sSandboxClient()
            try:
                client.connect()
            except Exception as e:
                self.close()
                raise RuntimeError("Kubernetes API 不可用，拒绝退回共享进程") from e
            self._k8s = client

    def close(self) -> None:
        if self._k8s:
            self._k8s.close()
            self._k8s = None
        if self._conn:
            self._conn.close()
            self._conn = None

    @property
    def mode(self) -> str:
        return self._mode

    # -------------------------------------------------------------- #
    #  Public API                                                      #
    # -------------------------------------------------------------- #

    async def ensure_running(self, user_id: str) -> SandboxHandle:
        if not user_id:
            raise ValueError("user_id 为空")
        if self._mode != "k8s":
            raise RuntimeError(f"未知沙盒模式: {self._mode}")

        cached = self._healthy.get(user_id)
        if cached and time.monotonic() - cached[0] < get_settings().sandbox_health_ttl:
            self._touch(user_id)
            return cached[1]
        handle = await asyncio.to_thread(self._ensure_running_k8s, user_id)
        await asyncio.to_thread(self._wait_health, handle)
        self._healthy[user_id] = (time.monotonic(), handle)
        return handle

    def retain(self, user_id: str) -> None:
        with self._lock:
            self._active_requests[user_id] = self._active_requests.get(user_id, 0) + 1
            self._touch(user_id)

    def release(self, user_id: str) -> None:
        with self._lock:
            count = self._active_requests.get(user_id, 0)
            if count > 1:
                self._active_requests[user_id] = count - 1
            else:
                self._active_requests.pop(user_id, None)
            self._touch(user_id)

    def invalidate_health(self, user_id: str) -> None:
        self._healthy.pop(user_id, None)

    async def stop(self, user_id: str) -> bool:
        if self._mode != "k8s":
            return False
        return await asyncio.to_thread(self._stop_k8s, user_id, idle_before=None)

    # -------------------------------------------------------------- #
    #  Idle GC                                                         #
    # -------------------------------------------------------------- #

    async def start_idle_gc(self) -> None:
        if self._gc_task is not None and not self._gc_task.done():
            return
        self._gc_stop.clear()
        self._gc_task = asyncio.create_task(self._idle_gc_loop())
        logger.info(f"sandbox idle gc started, ttl={get_settings().sandbox_idle_ttl}s")

    async def stop_idle_gc(self) -> None:
        self._gc_stop.set()
        if self._gc_task:
            try:
                await asyncio.wait_for(self._gc_task, timeout=5)
            except Exception:
                pass
            self._gc_task = None

    async def _idle_gc_loop(self) -> None:
        ttl = max(60, int(get_settings().sandbox_idle_ttl or 900))
        try:
            while not self._gc_stop.is_set():
                try:
                    await asyncio.wait_for(self._gc_stop.wait(), timeout=60)
                    break
                except asyncio.TimeoutError:
                    pass
                try:
                    await asyncio.to_thread(self._run_gc_pass, ttl)
                except Exception as e:
                    logger.warning(f"sandbox gc 异常：{e}")
        except asyncio.CancelledError:
            pass

    def _run_gc_pass(self, ttl: int) -> None:
        if not self._conn:
            return
        cutoff = time.time() - ttl
        with self._lock:
            rows = self._conn.execute(
                "SELECT user_id FROM sandbox_registry WHERE status = 'running' AND last_active_at < ?",
                (cutoff,),
            ).fetchall()
        for row in rows:
            uid = row["user_id"]
            try:
                if self._stop_k8s(uid, idle_before=cutoff):
                    logger.info(f"[sandbox-gc] 回收闲置沙盒 user={uid}")
            except Exception as e:
                logger.warning(f"[sandbox-gc] 回收 {uid} 失败: {e}")

    # -------------------------------------------------------------- #
    #  Internal                                                        #
    # -------------------------------------------------------------- #

    def _ensure_running_k8s(self, user_id: str) -> SandboxHandle:
        assert self._k8s is not None
        with self._user_locks.setdefault(user_id, threading.RLock()):
            safe = safe_user_id(user_id)
            name = f"openclaw-user-{safe}"
            env = self._build_user_env(user_id)
            pod_ip = self._k8s.ensure(user_id, safe, env)
            self._upsert_row(user_id, safe, name, pod_ip)
            return self._build_handle_from_row(user_id)

    def _build_user_env(self, user_id: str) -> dict[str, str]:
        """注入 OpenClaw 原生环境变量（与上游 docker-compose 对齐）。"""
        settings = get_settings()
        env = {
            "HOME": "/home/node",
            "TERM": "xterm-256color",
            "OPENCLAW_STATE_DIR": "/home/node/.openclaw",
            "OPENCLAW_CONFIG_PATH": "/home/node/.openclaw/openclaw.json",
            "OPENCLAW_CONFIG_DIR": "/home/node/.openclaw",
            "OPENCLAW_WORKSPACE_DIR": "/home/node/.openclaw/workspace",
            "OPENCLAW_GATEWAY_PORT": str(OPENCLAW_PORT),
            # 容器内 Bonjour 自动关闭，显式确认
            "OPENCLAW_DISABLE_BONJOUR": "1",
        }
        if settings.anthropic_base_url:
            env["ANTHROPIC_BASE_URL"] = settings.anthropic_base_url
        if settings.anthropic_api_key:
            env["ANTHROPIC_API_KEY"] = settings.anthropic_api_key
        # 非 Anthropic 官方端点必须显式指定模型名，否则默认请求 claude-* 型号被拒
        if settings.anthropic_model:
            env["ANTHROPIC_MODEL"] = settings.anthropic_model
        if settings.anthropic_small_fast_model:
            env["ANTHROPIC_SMALL_FAST_MODEL"] = settings.anthropic_small_fast_model
        return env

    def _acquire_stop_slot(self, user_id: str, *, idle_before: float | None):
        """租约与闲置条件全部通过时返回 registry 行，否则 None。"""
        with self._lock:
            row = self._select_row(user_id)
            if not row or self._active_requests.get(user_id, 0):
                return None
            if idle_before is not None and row["last_active_at"] >= idle_before:
                return None
            self.invalidate_health(user_id)
            return row

    def _stop_k8s(self, user_id: str, *, idle_before: float | None = None) -> bool:
        assert self._k8s is not None
        with self._user_locks.setdefault(user_id, threading.RLock()):
            row = self._acquire_stop_slot(user_id, idle_before=idle_before)
            if row is None:
                return False
            self._k8s.stop(user_id, row["safe_user_id"])  # 只删 Pod；PVC 永远保留
            self._mark_status(user_id, "stopped")
            return True

    def _wait_health(self, handle: SandboxHandle, *, timeout: float | None = None) -> None:
        """轮询用户 OpenClaw 的 /healthz，并断言 JSON body（官方探针契约）。

        仅看状态码不够：Control UI 对未知路径兜底返回 200，镜像与路由不匹配时
        纯状态码会永远通过（官方 manifests 用 node -e 断言 body，同因）。
        """
        if timeout is None:
            timeout = float(get_settings().k8s_pod_ready_timeout)
        import httpx

        deadline = time.monotonic() + timeout
        url = f"{handle.base_url}/healthz"
        last_err: Exception | None = None
        with httpx.Client(timeout=2.0, trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    r = client.get(url)
                    if r.status_code == 200 and r.json().get("ok") is True:
                        self._mark_status(handle.user_id, "running")
                        return
                except Exception as e:
                    last_err = e
                time.sleep(0.5)
        raise RuntimeError(f"user OpenClaw 健康检查超时: {handle.pod_name} ({last_err})")

    # -------------------------------------------------------------- #
    #  SQLite helpers                                                  #
    # -------------------------------------------------------------- #

    def _upsert_row(self, user_id: str, safe: str, name: str, host: str) -> None:
        if not self._conn:
            return
        now_iso = _now_iso()
        now_ts = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sandbox_registry (user_id, safe_user_id, pod_name, internal_host,"
                " internal_port, status, last_active_at, created_at, updated_at) VALUES"
                " (?, ?, ?, ?, ?, 'running', ?, ?, ?)"
                " ON CONFLICT(user_id) DO UPDATE SET"
                " safe_user_id=excluded.safe_user_id,"
                " pod_name=excluded.pod_name,"
                " internal_host=excluded.internal_host,"
                " internal_port=excluded.internal_port,"
                " status='running',"
                " last_active_at=excluded.last_active_at,"
                " updated_at=excluded.updated_at",
                (user_id, safe, name, host, OPENCLAW_PORT, now_ts, now_iso, now_iso),
            )
            self._conn.commit()

    def _select_row(self, user_id: str):
        if not self._conn:
            return None
        return self._conn.execute(
            "SELECT * FROM sandbox_registry WHERE user_id = ?", (user_id,)
        ).fetchone()

    def _touch(self, user_id: str) -> None:
        if not self._conn:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE sandbox_registry SET last_active_at=?, updated_at=? WHERE user_id=?",
                (time.time(), _now_iso(), user_id),
            )
            self._conn.commit()

    def _mark_status(self, user_id: str, status: str) -> None:
        if not self._conn:
            return
        with self._lock:
            self._conn.execute(
                "UPDATE sandbox_registry SET status=?, updated_at=? WHERE user_id=?",
                (status, _now_iso(), user_id),
            )
            self._conn.commit()

    def _build_handle_from_row(self, user_id: str) -> SandboxHandle:
        row = self._select_row(user_id)
        if not row:
            raise RuntimeError(f"sandbox registry 缺失 user_id={user_id}")
        return SandboxHandle(
            user_id=row["user_id"],
            pod_name=row["pod_name"],
            internal_host=row["internal_host"],
            internal_port=int(row["internal_port"] or OPENCLAW_PORT),
            status=row["status"] or "unknown",
            last_active_at=float(row["last_active_at"] or 0),
        )


_manager: Optional[SandboxManager] = None


def get_sandbox_manager() -> SandboxManager:
    if _manager is None:
        raise RuntimeError("SandboxManager 尚未初始化")
    return _manager


def init_sandbox_manager(*, mode: Optional[str] = None) -> SandboxManager:
    global _manager
    _manager = SandboxManager(mode=mode)
    _manager.connect()
    return _manager
