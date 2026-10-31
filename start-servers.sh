#!/usr/bin/env bash
# Start the Task 2-4 servers and their mock upstreams in one go.
# Task 1 runs over STDIO, so it is launched by MCP Inspector instead (see the
# command printed at the end). Logs go to .logs/. Press Ctrl+C to stop everything.

set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
LOG_DIR="$ROOT/.logs"
TASK2="$ROOT/task2-mcp-security-gateway"
TASK3="$ROOT/task3-llm-guardrail-gateway"
TASK4="$ROOT/task4-llm-fallback-router"
PIDS=()

cleanup() {
  trap - INT TERM EXIT
  echo
  echo "Stopping servers..."
  for pid in "${PIDS[@]}"; do kill "$pid" 2>/dev/null || true; done
  wait 2>/dev/null || true
}
trap cleanup INT TERM EXIT

for dir in "$TASK2" "$TASK3" "$TASK4"; do
  if [ ! -x "$dir/.venv/bin/python" ]; then
    echo "Missing $dir/.venv. Run: cd $dir && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'" >&2
    exit 1
  fi
done

for port in 9001 8080 9100 8090 9201 9202 8070; do
  if lsof -nP -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
    echo "Port $port is already in use. Stop whatever is on it and try again." >&2
    exit 1
  fi
done

mkdir -p "$LOG_DIR"

# start <name> <dir> <command> [env assignments...]
start() {
  local name=$1 dir=$2 cmd=$3
  shift 3
  (cd "$dir" && exec env "$@" "$dir/.venv/bin/$cmd") >"$LOG_DIR/$name.log" 2>&1 &
  PIDS+=($!)
}

wait_for_port() {
  local name=$1 port=$2
  for _ in $(seq 1 50); do
    if (exec 3<>"/dev/tcp/127.0.0.1/$port") 2>/dev/null; then
      printf "  %-22s http://localhost:%s\n" "$name" "$port"
      return
    fi
    sleep 0.2
  done
  echo "$name did not start on port $port. See $LOG_DIR/$name.log" >&2
  exit 1
}

export GATEWAY_JWT_SECRET="${GATEWAY_JWT_SECRET:-$(python3 -c 'import secrets; print(secrets.token_hex(32))')}"

echo "Starting servers (logs in .logs/)..."

start task2-mock "$TASK2" mcp-gateway-mock-server MOCK_PORT=9001
start task2-gateway "$TASK2" mcp-gateway GATEWAY_PORT=8080
start task3-mock "$TASK3" llm-mock-provider MOCK_PROVIDER_PORT=9100
start task3-gateway "$TASK3" llm-guardrail-gateway GATEWAY_PORT=8090
start task4-primary "$TASK4" llm-router-mock-provider \
  MOCK_NAME=primary MOCK_BEHAVIOR=slow MOCK_DELAY=10 MOCK_PORT=9201
start task4-secondary "$TASK4" llm-router-mock-provider MOCK_NAME=secondary MOCK_PORT=9202
start task4-router "$TASK4" llm-router ROUTER_TENANT_KEYS=key-acme,key-globex ROUTER_PORT=8070

wait_for_port task2-mock 9001
wait_for_port task2-gateway 8080
wait_for_port task3-mock 9100
wait_for_port task3-gateway 8090
wait_for_port task4-primary 9201
wait_for_port task4-secondary 9202
wait_for_port task4-router 8070

VIEWER=$("$TASK2/.venv/bin/mcp-gateway-token" --role viewer --sub alice)
ADMIN=$("$TASK2/.venv/bin/mcp-gateway-token" --role admin --sub root)

cat <<EOF

All servers are up.

Task 2  MCP gateway     http://localhost:8080/mcp   (health: /healthz)
        Viewer token:   $VIEWER
        Admin token:    $ADMIN
Task 3  Guardrail       http://localhost:8090/v1/chat/completions   (health: /healthz)
Task 4  Router          http://localhost:8070/v1/chat/completions   (health: /healthz)
        Tenant keys:    key-acme, key-globex

Task 1 (STDIO), in another terminal:
  cd task1-customer-mcp-server && npx @modelcontextprotocol/inspector .venv/bin/python -m customer_mcp_server

Press Ctrl+C to stop all servers.
EOF

wait
