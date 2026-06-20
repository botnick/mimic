#!/usr/bin/env bash
# Built-in MITM proxy for Mimic — mitmweb with an easy web password.
#
# With the SSL bypass on (mimic_ssl bypass=true, or mimic_unpin per app) you do NOT need to
# trust the mitmproxy CA: the app skips cert validation entirely. Then set the phone's Wi-Fi
# HTTP proxy to the address printed below and open the target app.
#
# Env overrides: MITM_PORT (8080) · MITM_WEBPORT (8081) · MITM_PASS (Aa1234) · MITM_FLOWS
set -e
PORT="${MITM_PORT:-8080}"
WEBPORT="${MITM_WEBPORT:-8081}"
PASS="${MITM_PASS:-Aa1234}"
FLOWS="${MITM_FLOWS:-/tmp/mimic_mitm.flows}"
MITMWEB="$(command -v mitmweb || echo "$HOME/homebrew/bin/mitmweb")"
IP="$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)"

echo "== Mimic MITM =="
echo "  phone Wi-Fi HTTP proxy : $IP:$PORT"
echo "  web UI                 : http://127.0.0.1:$WEBPORT   (password: $PASS)"
echo "  flows saved to         : $FLOWS"
echo "  CA (only if not bypassing): ~/.mitmproxy/mitmproxy-ca-cert.pem"
echo "  tip: turn the SSL bypass on first — mimic_ssl bypass=true, or mimic_unpin per app"
echo

exec "$MITMWEB" --listen-host 0.0.0.0 -p "$PORT" \
  --web-host 127.0.0.1 --web-port "$WEBPORT" --no-web-open-browser \
  --set web_password="$PASS" -w "$FLOWS"
