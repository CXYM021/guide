#!/usr/bin/env bash
# 同一 WiFi 下的手机 / 他人电脑可访问（监听全部网卡）。
set -euo pipefail
cd "$(dirname "$0")"
export PROF_HOST=0.0.0.0
if [[ -z "${PROF_INVITE_CODE:-}" ]]; then
  echo "WARNING: no invite code. Anyone on this network can register an account."
  echo "Set one with:  PROF_INVITE_CODE=yourcode ./start_lan.sh"
  echo
fi
echo "Starting for other devices; use the LAN address printed below."
echo "If a firewall is enabled, allow inbound TCP on the port shown."
exec python3 server.py "$@"
