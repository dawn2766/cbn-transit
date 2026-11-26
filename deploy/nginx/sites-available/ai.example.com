# /etc/nginx/sites-available/ai.example.com
#
# Public entry point of the relay. Replace every `ai.example.com` with your own
# fixed domain, then symlink into sites-enabled/ and run `nginx -t`.
#
# Request path (POST /v1/chat/completions):
#   client -> nginx :443 -> cnb_imgsanitize (127.0.0.1:9004, imgsanitize.py)
#          -> https://$cnb_ai_upstream/v1/chat/completions (CNB workspace proxy)
# Everything else (/health, /v1/models, /v1/messages, ...) goes straight to the
# workspace proxy via the same map variable.
#
# Registration path (workspace -> relay):
#   POST /ops/register -> 127.0.0.1:9003 (cnb-register.py) : X-Reg-Token

server {
    listen 80;
    server_name ai.example.com;

    # keep ACME http-01 reachable for certbot renewals
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot-temp;
        default_type text/plain;
    }

    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl http2;
    server_name ai.example.com;

    # The relay and the workspace proxy both do their own request-body admission
    # (413/503 JSON). Do not add an independent upload cap here: an nginx 413
    # would arrive as an HTML page before the relay could answer, and buffering
    # would spool whole uploads into nginx before that admission runs.
    client_max_body_size 0;
    proxy_request_buffering off;

    ssl_certificate     /etc/letsencrypt/live/ai.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ai.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;

    # proxy_pass uses a VARIABLE ($cnb_ai_upstream), so nginx resolves the
    # upstream at request time and requires an explicit resolver.
    resolver 223.5.5.5 119.29.29.29 8.8.8.8 valid=10s ipv6=off;
    # A dead CNB subdomain can hang DNS resolution; fail fast instead.
    resolver_timeout 2s;

    # workspace -> relay self-registration (internet-facing by necessity), so
    # throttle token guessing instead of pinning a client IP.
    location = /ops/register {
        limit_req zone=cnbreg burst=10 nodelay;
        client_max_body_size 8k;
        proxy_pass http://127.0.0.1:9003/register;
    }

    # Completion requests go through the request relay so that:
    #   * inline images are reduced to fit the CNB gateway's 10 MiB body limit,
    #   * the two client prompt fingerprints refused with 400/11128 are
    #     neutralised in system/developer/assistant text,
    #   * a 400/11133 tool-pairing rejection gets exactly one repaired retry.
    # The relay forwards to the same $cnb_ai_upstream after admission.
    #
    # Never retry the POST from here (no `error_page ... @chat_direct`): a named
    # location keeps method and buffered body, and the first upstream may already
    # have accepted the completion. Only transport failures become a 503. The
    # relay's own bounded JSON errors (413/503 + Retry-After) must reach the
    # client unchanged, so only 502/504 are intercepted.
    location = /v1/chat/completions {
        proxy_pass http://cnb_imgsanitize;
        proxy_http_version 1.1;
        proxy_set_header Connection '';
        proxy_set_header Host $host;
        proxy_set_header Content-Type $content_type;
        proxy_set_header Authorization $http_authorization;
        proxy_set_header Accept $http_accept;
        proxy_set_header User-Agent $http_user_agent;
        # No buffering: responses are SSE streams.
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        proxy_connect_timeout 3s;
        proxy_next_upstream off;
        proxy_intercept_errors on;
        error_page 502 504 =503 @upstream_unavailable;
    }

    # CNB force-recycles the workspace on a fixed lifetime (measured 2026-11-15:
    # alternating 12 h / 18 h).  A dying subdomain answers 302 to the cnb.cool
    # error page; mid-recycle it answers 401 "invalid api key", which clients
    # treat as fatal.  Intercept redirects + gateway failures here and return a
    # retryable JSON 503 so clients back off while the watchdog re-registers.
    # The /v1/chat/completions path gets the same translation one layer earlier
    # in imgsanitize.py (it can tell a recycle 401 from a wrong key).
    location @upstream_unavailable {
        default_type application/json;
        add_header Retry-After 30 always;
        add_header Cache-Control "no-store" always;
        return 503 '{"error":{"message":"upstream workspace unavailable; retry shortly","type":"upstream_unavailable"}}';
    }

    # Everything else: straight to the current workspace port proxy.
    location / {
        proxy_pass https://$cnb_ai_upstream;
        proxy_http_version 1.1;
        proxy_ssl_server_name on;
        proxy_ssl_name $cnb_ai_upstream;
        proxy_set_header Host $cnb_ai_upstream;
        proxy_set_header Connection '';
        proxy_buffering off;
        proxy_cache off;
        proxy_read_timeout 3600s;
        proxy_send_timeout 3600s;
        # Fail fast on a dead upstream, then hand the failure to error_page
        # instead of returning nginx's default HTML error page.
        proxy_connect_timeout 5s;
        proxy_intercept_errors on;
        # 503 is deliberately absent: the workspace proxy answers 503
        # ("server_busy" + Retry-After) for its own admission pressure, and that
        # body must survive instead of being replaced by a generic message.
        error_page 301 302 303 307 308 502 504 =503 @upstream_unavailable;
    }
}
