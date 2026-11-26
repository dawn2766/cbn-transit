#!/bin/bash
# install.sh - install / refresh the CBN relay components on the relay VPS.
#
# Idempotent: safe to re-run after a `git pull`. It installs files, renders the
# nginx vhost for your domain, seeds the map file, and restarts the services.
# It never touches other vhosts on the box.
#
#   sudo ./install.sh --domain ai.example.com [--email you@example.com] [--yes]
#
# Options:
#   --domain DOMAIN      your fixed domain (A/AAAA record must already point here)
#   --email  EMAIL       if given, obtain/renew the certificate with certbot
#   --port   PORT        workspace proxy port (default 9001)
#   --skip-tls           do not touch certificates (use an existing pair)
#   --yes                non-interactive; also implied when stdin is not a terminal
#
# After a successful run:
#   1. put REG_TOKEN in /root/.cnb/register.env  (printed below if newly generated)
#   2. fill /etc/cnb-ai-watchdog.env from deploy/env/cnb-ai-watchdog.env.example
#   3. add the same REG_TOKEN to the CNB secrets repo, set REGISTER_URL in .cnb.yml
# ---------------------------------------------------------------------------
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOMAIN=""
EMAIL=""
PORT="9001"
SKIP_TLS=0
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --domain) DOMAIN="${2:?}"; shift 2 ;;
    --email)  EMAIL="${2:?}"; shift 2 ;;
    --port)   PORT="${2:?}"; shift 2 ;;
    --skip-tls) SKIP_TLS=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,/^# ---/p' "$0" | sed '$d'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# A non-interactive run (cron, `ssh host '...'`, CI) must never block on a prompt
# and must never abort the install because `read` hit EOF under `set -e`.
if [ ! -t 0 ]; then
  ASSUME_YES=1
fi

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (it installs systemd units and rewrites nginx config)" >&2
  exit 1
fi
if [ -z "$DOMAIN" ]; then
  echo "--domain is required, e.g. --domain ai.example.com" >&2
  exit 2
fi

say() { printf '\n== %s\n' "$*"; }

say "1/8 checking prerequisites"
for bin in nginx python3 curl openssl; do
  command -v "$bin" >/dev/null || { echo "missing: $bin" >&2; exit 1; }
done
# Pillow is required for image reduction; orjson is an optional accelerator.
python3 - <<'PY' || { echo "Pillow missing: pip3 install pillow (or apt install python3-pil)" >&2; exit 1; }
import PIL  # noqa: F401
PY
python3 -c 'import orjson' 2>/dev/null || echo "note: orjson not installed (optional, falls back to json)"

say "2/8 installing relay code to /opt/cnb-imgre"
install -d -m 755 /opt/cnb-imgre
install -m 755 "$SRC_DIR/deploy/imgsanitize.py"    /opt/cnb-imgre/imgsanitize.py
install -m 644 "$SRC_DIR/deploy/image_budget.py"   /opt/cnb-imgre/image_budget.py

say "3/8 installing register service"
REG_ENV=/root/.cnb/register.env
install -d -m 700 /root/.cnb
if [ ! -f "$REG_ENV" ]; then
  NEW_TOKEN="$(openssl rand -hex 24)"
  umask 077
  printf 'REG_TOKEN=%s\n' "$NEW_TOKEN" > "$REG_ENV"
  chmod 600 "$REG_ENV"
  echo "generated REG_TOKEN (also needed in the CNB secrets repo): $NEW_TOKEN"
else
  chmod 600 "$REG_ENV"
  echo "kept existing $REG_ENV"
fi
install -m 755 "$SRC_DIR/deploy/cnb-register.py" /usr/local/bin/cnb-register.py
install -m 644 "$SRC_DIR/deploy/systemd/cnb-register.service" /etc/systemd/system/
# Pin the workspace port the register service health-probes. Written every run (and
# removed when it is back to 9001) so a changed --port can never leave a stale
# drop-in that probes the wrong port.
DROPIN_DIR=/etc/systemd/system/cnb-register.service.d
if [ "$PORT" != "9001" ]; then
  install -d -m 755 "$DROPIN_DIR"
  printf '[Service]\nEnvironment=UPSTREAM_PORT=%s\n' "$PORT" > "$DROPIN_DIR/port.conf"
  chmod 644 "$DROPIN_DIR/port.conf"
  echo "register service pinned to workspace port $PORT (systemd drop-in)"
elif [ -f "$DROPIN_DIR/port.conf" ]; then
  rm -f "$DROPIN_DIR/port.conf"
  echo "removed the stale port drop-in (workspace port is back to 9001)"
fi

say "4/8 installing request relay service"
install -m 644 "$SRC_DIR/deploy/systemd/cnb-imgsanitize.service" /etc/systemd/system/

say "5/8 installing nginx pieces"
# Order matters: the map file must exist before the vhost that references it.
if [ ! -f /etc/nginx/conf.d/cnb-ai-upstream.conf ]; then
  install -m 644 "$SRC_DIR/deploy/nginx/conf.d/cnb-ai-upstream.conf" /etc/nginx/conf.d/
  echo "seeded /etc/nginx/conf.d/cnb-ai-upstream.conf (placeholder upstream)"
else
  echo "kept existing /etc/nginx/conf.d/cnb-ai-upstream.conf"
fi
install -m 644 "$SRC_DIR/deploy/nginx/conf.d/cnb-imgsanitize.conf" /etc/nginx/conf.d/
install -m 644 "$SRC_DIR/deploy/nginx/conf.d/cnb-register-ratelimit.conf" /etc/nginx/conf.d/

VHOST=/etc/nginx/sites-available/"$DOMAIN"
if [ -f "$VHOST" ] && [ "$ASSUME_YES" -ne 1 ]; then
  echo "$VHOST already exists; overwrite it? [y/N]"
  # `read` returns non-zero on EOF, which would abort the whole install under -e.
  answer=""
  read -r answer || true
  case "$answer" in y|Y) ;; *) echo "kept existing vhost"; VHOST_KEEP=1 ;; esac
fi
if [ "${VHOST_KEEP:-0}" -ne 1 ]; then
  install -d -m 755 /etc/nginx/sites-available /etc/nginx/sites-enabled
  sed -e "s#ai\.example\.com#${DOMAIN}#g" "$SRC_DIR/deploy/nginx/sites-available/ai.example.com" > "$VHOST"
  chmod 644 "$VHOST"
  echo "rendered $VHOST"
fi
ln -sfn "$VHOST" /etc/nginx/sites-enabled/"$DOMAIN"

say "6/8 checking TLS"
CERT=/etc/letsencrypt/live/"$DOMAIN"/fullchain.pem
if [ "$SKIP_TLS" -eq 1 ]; then
  echo "skipped (--skip-tls): make sure $CERT exists or edit ssl_certificate in $VHOST"
elif [ -f "$CERT" ]; then
  echo "certificate present: $CERT"
elif [ -n "$EMAIL" ]; then
  echo "obtaining Let's Encrypt certificate for $DOMAIN"
  # Serve the http-01 challenge from the port-80 vhost we already installed.
  certbot certonly --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "$EMAIL" || {
    echo "certbot failed; the vhost points at a certificate that does not exist yet." >&2
    echo "Fix DNS/TLS, then re-run: certbot certonly --nginx -d $DOMAIN" >&2
  }
else
  echo "no certificate and no --email; nginx -t will fail until $CERT exists" >&2
fi

say "7/8 enabling services"
systemctl daemon-reload
systemctl enable cnb-register cnb-imgsanitize >/dev/null 2>&1 || true
systemctl restart cnb-register cnb-imgsanitize
if nginx -t; then
  systemctl reload nginx 2>/dev/null || systemctl restart nginx
else
  echo "nginx -t FAILED; fix the reported error before traffic reaches the relay" >&2
  exit 1
fi

say "8/8 local health check"
sleep 1
curl -sf --max-time 5 http://127.0.0.1:9004/healthz >/dev/null && echo "relay :9004 responding" || echo "warning: relay :9004 not responding yet"
systemctl is-active cnb-register cnb-imgsanitize nginx

cat <<EOF

----------------------------------------------------------------
Installed. Remaining manual steps:

  * Watchdog config (recommended):
      cp $SRC_DIR/deploy/env/cnb-ai-watchdog.env.example /etc/cnb-ai-watchdog.env
      edit it (CNB_REPO, FIXED_URL, REGISTER_URL, CNB_TOKEN), chmod 600
      install -m 755 $SRC_DIR/deploy/cnb-ai-watchdog.sh /usr/local/bin/cnb-ai-watchdog.sh
      echo '* * * * * /usr/local/bin/cnb-ai-watchdog.sh >/dev/null 2>&1' | crontab -

  * CNB side: put the same REG_TOKEN in your private secrets repo and set
      REGISTER_URL=https://$DOMAIN/ops/register
    in the code repo's .cnb.yml (see README, "部署" step 2-3).

  * Verify from a client network:
      curl https://$DOMAIN/health
      ./smoke-test.sh    # BASE_URL=https://$DOMAIN PROXY_KEY=... ./smoke-test.sh

  * After a certbot renewal, re-check that the vhost still contains the
    resolver line and the upstream map variable, then: nginx -t && systemctl reload nginx
----------------------------------------------------------------
EOF
