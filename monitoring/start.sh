#!/usr/bin/env bash
# Start Prometheus and Grafana for RootIQ. Stop both with Ctrl+C. Port 9090. Port 3000.
# Install them first: brew install prometheus grafana. And loki, for the Loki log source.
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$PWD"
read_env() { grep -E "^$1=" backend/.env | tail -1 | cut -d= -f2- || true; }

export ROOTIQ_ALERT_TOKEN="$(read_env ROOTIQ_ALERT_TOKEN)"
export ROOTIQ_WEBHOOK_URL="${ROOTIQ_WEBHOOK_URL:-http://127.0.0.1:8011/api/alerts/grafana}"
export PROMETHEUS_URL="${PROMETHEUS_URL:-http://127.0.0.1:9090}"
export LOKI_URL="${LOKI_URL:-http://127.0.0.1:3100}"
export ROOTIQ_DASHBOARDS="$ROOT/monitoring/grafana/dashboards"
export GF_SECURITY_ADMIN_PASSWORD="$(read_env ROOTIQ_GRAFANA_PASSWORD)"
[ -n "$ROOTIQ_ALERT_TOKEN" ] || { echo "Set ROOTIQ_ALERT_TOKEN in backend/.env first."; exit 1; }
DATA="$ROOT/backend/data/monitoring"
mkdir -p "$DATA/prometheus" "$DATA/grafana"

prometheus --config.file="$ROOT/monitoring/prometheus.yml" --storage.tsdb.path="$DATA/prometheus" \
  --web.listen-address=127.0.0.1:9090 --log.level=warn &
PROM=$!
grafana server --homepath "$(brew --prefix grafana)/share/grafana" --config "$ROOT/monitoring/grafana/grafana.ini" \
  cfg:default.paths.data="$DATA/grafana" cfg:default.paths.logs="$DATA/grafana/logs" \
  cfg:default.paths.plugins="$DATA/grafana/plugins" cfg:default.paths.provisioning="$ROOT/monitoring/grafana/provisioning" &
GRAF=$!
LOKI_PIDS=""
if command -v loki >/dev/null; then  # optional: logs in Loki, read by the Loki data source
  export LOKI_DATA="$DATA/loki"
  mkdir -p "$LOKI_DATA"
  loki -config.file="$ROOT/monitoring/loki.yml" -config.expand-env=true >"$DATA/loki.log" 2>&1 &
  LOKI_PIDS="$!"
  "$ROOT/.venv/bin/python" -m backend.app.ship_logs >"$DATA/ship_logs.log" 2>&1 &
  LOKI_PIDS="$LOKI_PIDS $!"
fi
trap 'kill $PROM $GRAF $LOKI_PIDS 2>/dev/null' EXIT INT TERM
echo "Prometheus: http://127.0.0.1:9090   Grafana: http://127.0.0.1:3000/d/rootiq-agents   Loki: ${LOKI_PIDS:+http://127.0.0.1:3100}"
wait
