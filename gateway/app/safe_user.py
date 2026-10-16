"""用户 ID 规范化 — 沿用 eido-ai 的规则。

CAS 用户 ID → K8s 资源名安全字符集（Pod/PVC 名只允许 [a-z0-9-] 且小写）：
仅保留 [A-Za-z0-9_-]，其余替换为 -，截断后追加 hash 后缀防冲突。
"""

from __future__ import annotations

import hashlib
import re

_USER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
_SAFE_REPLACE_RE = re.compile(r"[^A-Za-z0-9_\-]")


def safe_user_id(user_id: str) -> str:
    if not user_id:
        raise ValueError("user_id 为空")
    if _USER_ID_RE.fullmatch(user_id) and len(user_id) <= 48:
        base = user_id.lower()
    else:
        base = _SAFE_REPLACE_RE.sub("-", user_id)[:32].strip("-").lower() or "user"
    suffix = hashlib.sha1(user_id.encode("utf-8")).hexdigest()[:8]
    return f"{base}-{suffix}"
