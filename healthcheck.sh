#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/mnt/HC_Volume_106446438/books/app"
cd "$APP_DIR"

ok()   { echo -e "\e[32m[OK]\e[0m $*"; }
warn() { echo -e "\e[33m[WARN]\e[0m $*"; }
err()  { echo -e "\e[31m[ERR]\e[0m $*"; }

echo "== Books stack healthcheck =="

# 1) Containers
PS_OUT="$(docker compose ps)"
echo "$PS_OUT"

for svc in database frontend pgadmin; do
  if echo "$PS_OUT" | grep -q "app-$svc-1"; then
    if echo "$PS_OUT" | grep "app-$svc-1" | grep -q "Up"; then
      ok "Service '$svc' is Up"
    else
      err "Service '$svc' is not Up"
    fi
  else
    err "Service '$svc' not found"
  fi
done

# 2) Frontend logs (basic error scan)
FRONT_LOGS="$(docker compose logs --tail=120 frontend || true)"
if echo "$FRONT_LOGS" | grep -Eqi "Exception in ASGI application|Traceback|NameError|ProgrammingError|OperationalError"; then
  warn "Frontend logs contain possible errors (see below excerpt)"
  echo "$FRONT_LOGS" | tail -n 40
else
  ok "Frontend logs: no obvious Python/ASGI errors"
fi

# 3) Local endpoints
if curl -sSI --max-time 8 http://127.0.0.1:5050 | head -n1 | grep -Eq "HTTP/1.1 200|HTTP/1.1 302|HTTP/2 200|HTTP/2 302"; then
  ok "pgAdmin local endpoint reachable (127.0.0.1:5050)"
else
  err "pgAdmin local endpoint not reachable"
fi

#if curl -sSI --max-time 8 http://127.0.0.1:4000 | head -n1 | grep -Eq "HTTP/1.1 200|HTTP/1.1 302|HTTP/2 200|HTTP/2 302"; then
#  ok "Frontend local endpoint reachable (127.0.0.1:4000)"
#else
#  warn "Frontend local endpoint not returning 200/302"
#fi

# 4) DB check
if docker compose exec -T database psql -U admin -d buecherdb -c "SELECT 1;" >/dev/null 2>&1; then
  ok "Database query successful"
else
  err "Database query failed"
fi

# 5) Nginx syntax
if sudo nginx -t >/dev/null 2>&1; then
  ok "nginx config syntax OK"
else
  err "nginx config test failed"
fi

# 6) Public HTTPS checks
for host in pgadmin.breviarium.de; do
  if curl -sSIk --max-time 10 "https://$host" | head -n1 | grep -Eq "200|301|302"; then
    ok "HTTPS reachable: $host"
  else
    warn "HTTPS check failed or unexpected status: $host"
  fi
done

echo "== Done =="