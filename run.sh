#!/usr/bin/env bash
# Start all of RootIQ with one command:
#   ./run.sh                    backend, website, Prometheus and Grafana
#   ./run.sh --no-monitoring    backend and website only
# Stop everything with Ctrl+C. The logs are in backend/data/logs.
set -uo pipefail
cd "$(dirname "$0")"
ROOT="$PWD"
LOGS="$ROOT/backend/data/logs"
mkdir -p "$LOGS"
MONITORING=1
[ "${1:-}" = "--no-monitoring" ] && MONITORING=0

PIDS=()
stop_all() {
  trap - EXIT INT TERM
  echo
  echo "Stopping RootIQ..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null; done
  wait 2>/dev/null
  echo "Stopped."
}
trap stop_all EXIT INT TERM

port_free() {
  if lsof -nP -iTCP:"$1" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $1 ($2) is in use. Stop the program that uses it, then run ./run.sh again."
    exit 1
  fi
}
wait_for() {  # wait_for <name> <url> <seconds>
  for _ in $(seq 1 "$3"); do
    code=$(curl -s -m 2 -o /dev/null -w "%{http_code}" "$2" || true)
    if [ "$code" != "000" ]; then return 0; fi
    sleep 1
  done
  echo "  $1 did not start. Read $LOGS/$1.log"
  return 1
}

# Checks before the start
[ -x .venv/bin/uvicorn ] || { echo "No Python environment. Run the setup in README.md (Run it)."; exit 1; }
[ -x frontend/node_modules/.bin/vite ] || { echo "No website packages. Run: cd frontend && npm install"; exit 1; }
if ! curl -s -m 2 -o /dev/null http://127.0.0.1:11434/api/version; then
  echo "Note: Ollama does not answer. Start the Ollama app: the local model is the backup for the agents."
fi
port_free 8011 "backend"
port_free 5174 "website"
if [ "$MONITORING" = 1 ]; then
  if command -v prometheus >/dev/null && command -v grafana >/dev/null; then
    port_free 9090 "Prometheus"
    port_free 3000 "Grafana"
    command -v loki >/dev/null && port_free 3100 "Loki"
  else
    echo "Note: Prometheus or Grafana is not installed (brew install prometheus grafana). Starting without them."
    MONITORING=0
  fi
fi

echo "Starting RootIQ..."
.venv/bin/uvicorn backend.app.main:app --host 127.0.0.1 --port 8011 >"$LOGS/backend.log" 2>&1 &
PIDS+=($!)
(cd frontend && ROOTIQ_API_TARGET=http://127.0.0.1:8011 exec node_modules/.bin/vite --host 127.0.0.1 --port 5174 --strictPort) >"$LOGS/website.log" 2>&1 &
PIDS+=($!)
if [ "$MONITORING" = 1 ]; then
  ./monitoring/start.sh >"$LOGS/monitoring.log" 2>&1 &
  PIDS+=($!)
fi

ok=1
wait_for backend http://127.0.0.1:8011/api/health 60 || ok=0
wait_for website http://127.0.0.1:5174/ 60 || ok=0
if [ "$MONITORING" = 1 ]; then
  wait_for monitoring http://127.0.0.1:3000/api/health 90 || ok=0
fi
[ "$ok" = 1 ] || exit 1

echo
echo "RootIQ is running:"
echo "  Website      http://127.0.0.1:5174   (sign in with ROOTIQ_AUTH_USERNAME / _PASSWORD in backend/.env)"
echo "  Backend      http://127.0.0.1:8011"
if [ "$MONITORING" = 1 ]; then
  echo "  Grafana      http://127.0.0.1:3000/d/rootiq-agents"
  echo "  Prometheus   http://127.0.0.1:9090"
  command -v loki >/dev/null && echo "  Loki         http://127.0.0.1:3100   (logs; choose it on Settings > Data sources)"
fi
echo "  Logs         $LOGS"
echo
echo "Press Ctrl+C to stop everything."
wait
