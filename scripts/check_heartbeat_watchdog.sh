#!/usr/bin/env bash
# 外部心跳 watchdog — Linux/macOS cron 用
# 建议每 1–5 分钟运行：
#   */5 * * * * /path/to/autoctp/scripts/check_heartbeat_watchdog.sh
#
# 用法:
#   ./scripts/check_heartbeat_watchdog.sh
#   ./scripts/check_heartbeat_watchdog.sh --dry-run

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${ROOT}/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  PYTHON="python3"
fi
exec "$PYTHON" "${ROOT}/scripts/check_heartbeat_watchdog.py" "$@"
