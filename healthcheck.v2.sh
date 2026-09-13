#!/usr/bin/env bash
set -u

APP_DIR="/mnt/HC_Volume_106446438/books/app"
cd "$APP_DIR" || { echo "[ERR] Cannot cd to $APP_DIR"; exit 1; }

fail=0
warn_count=0

ok()   { echo -e "\e[32m[OK]\e[0m $*"; }
warn() { echo -e "\e[33m[WARN]\e[0m $*"; warn_count=$((warn_count+1)); }
err()  { echo -e "\e[31m[ERR]\e[0m $*"; fail=1; }

echo "== Books stack healthcheck (strict) =="

# 1) Containers status
PS_OUT="$(docker compose ps 2>/dev/null)"
if [ -z "$PS_OUT" ]; then
  err "docker compose ps returned no output"
else
  echo "$PS_OUT"
fi

check_service_up() {
  local svc="$1"
  local line
  line="$(echo "$PS_OUT" | grep "app-$svc-1" || true)"
  if [ -z "$line" ]; then
    err "Service '$svc' not found"
    return
  fi
  if echo "$line" | grep -q "Up"; then
    ok "Service '$svc' is Up"
  else
    err "Service '$svc' is not Up"
  fi
}

check_service_up database
check_service_up frontend
check_service_up pgadmin

# 2) Frontend logs: hard-fail patterns
FRONT_LOGS="$(docker compose logs --tail=160 frontend 2>/dev/null || true)"
if echo "$FRONT_LOGS" | grep -Eqi "Exception in ASGI application|Traceback|NameError|ProgrammingError|OperationalError"; then
  err "Frontend logs contain runtime errors"
  echo "$FRONT_LOGS" | tail -n 50
else
  ok "Frontend logs: no critical error patterns found"
fi

# 3) Local HTTP checks
check_http_ok() {
  local url="$1"
  local label="$2"
  local first
  first="$(curl -sSI --max-time 10 "$url" | head -n1 || true)"
  if echo "$first" | grep -Eq "200|301|302"; then
    ok "$label reachable ($first)"
  else
    err "$label not healthy (got: ${first:-no response})"
  fi
}

check_http_ok "http://127.0.0.1:5050" "pgAdmin local"
check_http_ok "http://127.0.0.1:4000" "Frontend local"

# 4) DB query
if docker compose exec -T database psql -U admin -d buecherdb -c "SELECT 1;" >/dev/null 2>&1; then
  ok "Database query successful"
else
  err "Database query failed"
fi

# 5) Nginx syntax
if sudo nginx -t >/dev/null 2>&1; then
  ok "nginx syntax OK"
else
  err "nginx syntax invalid"
fi

# 6) Public HTTPS checks
check_http_ok "https://pgadmin.breviarium.de" "pgAdmin public HTTPS"
#check_http_ok "https://www.breviarium.de" "Main site public HTTPS"

echo "== Summary =="
if [ "$fail" -eq 0 ]; then
  if [ "$warn_count" -gt 0 ]; then
    echo "[PASS with warnings] warnings=$warn_count"
  else
    echo "[PASS]"
  fi
  exit 0
else
  echo "[FAIL] One or more critical checks failed."
  exit 1
fi