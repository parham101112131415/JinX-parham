#!/bin/sh
# parham mahsa init (oneshot): prepare everything before services start.
export PYTHONUNBUFFERED=1
exec 2>&1   # everything to normal log (Railway shows stderr in red)
ulimit -n "$(ulimit -Hn 2>/dev/null || echo 65535)" 2>/dev/null || ulimit -n 65535 2>/dev/null || true
mkdir -p /run/jinx /etc/x-ui /var/log/x-ui
for d in ngx-body ngx-proxy ngx-fcgi ngx-uwsgi ngx-scgi; do mkdir -p "/tmp/$d"; chmod 1777 "/tmp/$d"; done
i=0
until python3 /opt/jinx/bootstrap.py; do
  i=$((i+1))
  if [ "$i" -ge 3 ]; then echo "[jinx] bootstrap failed 3 times"; exit 1; fi
  echo "[jinx] bootstrap retry $i"; sleep 3
done
if ! nginx -t -c /run/jinx/nginx.conf; then
  echo "[jinx] nginx rejected the tuned config -> switching to safe config"
  python3 /opt/jinx/render.py --safe || exit 1
  nginx -t -c /run/jinx/nginx.conf || { echo "[jinx] nginx config invalid"; exit 1; }
fi
exit 0
