#!/usr/bin/env bash
# 生成 k8s/30-secret.yaml（含真实凭据，已被 .gitignore 排除）
#
# 用法：cd k8s && ./gen-secret.sh
# 模型凭据（OCGW_ANTHROPIC_*）可选：留空则用户在 OpenClaw Control UI 里自行配置。
set -euo pipefail
cd "$(dirname "$0")"

SESSION_SECRET_KEY=$(openssl rand -hex 32)

ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-}"
ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY:-}"
ANTHROPIC_MODEL="${ANTHROPIC_MODEL:-}"
ANTHROPIC_SMALL_FAST_MODEL="${ANTHROPIC_SMALL_FAST_MODEL:-${ANTHROPIC_MODEL:-}}"

cat > 30-secret.yaml <<EOF
# 由 gen-secret.sh 生成 $(date '+%Y-%m-%d %H:%M:%S')，勿提交 git
apiVersion: v1
kind: Secret
metadata:
  name: openclaw-secrets
  namespace: openclaw-system
type: Opaque
stringData:
  OCGW_SESSION_SECRET_KEY: "${SESSION_SECRET_KEY}"
  OCGW_ANTHROPIC_BASE_URL: "${ANTHROPIC_BASE_URL}"
  OCGW_ANTHROPIC_API_KEY: "${ANTHROPIC_API_KEY}"
  OCGW_ANTHROPIC_MODEL: "${ANTHROPIC_MODEL}"
  OCGW_ANTHROPIC_SMALL_FAST_MODEL: "${ANTHROPIC_SMALL_FAST_MODEL}"
EOF

chmod 600 30-secret.yaml
echo "已生成 30-secret.yaml"
[ -n "$ANTHROPIC_API_KEY" ] || echo "提示：ANTHROPIC_API_KEY 为空，用户需在 Control UI 自行配置模型"
[ -z "$ANTHROPIC_BASE_URL" ] || [ -n "$ANTHROPIC_MODEL" ] || echo "提示：非官方 ANTHROPIC_BASE_URL 建议同时设置 ANTHROPIC_MODEL"
