#!/bin/bash
set -e

# --- Sanity check: credentials must come from the platform's env vars, ----
# never from a file in this image. Fail loudly rather than silently run
# with blank credentials.
if [ -z "$IBC_USER" ] || [ -z "$IBC_PASSWORD" ]; then
  echo "FATAL: IBC_USER and IBC_PASSWORD must be set as environment"
  echo "variables in your hosting platform's dashboard (Render, Fly.io,"
  echo "etc). This container will not start without them."
  exit 1
fi

# --- Render the real config.ini from the template + env vars --------------
# IBC's default --ibc-ini path is ~/ibc/config.ini (i.e. /root/ibc/config.ini
# when running as root) — write it there rather than under /opt/ibc, since
# the --config= flag passed to gatewaystart.sh below isn't actually honored.
mkdir -p /root/ibc
sed -e "s/__IBC_USER__/$IBC_USER/" -e "s/__IBC_PASSWORD__/$IBC_PASSWORD/" \
    /opt/ibc/config.ini.template > /root/ibc/config.ini

# --- Start the virtual display Gateway needs to run headlessly ------------
# Wait for Xvfb's own lock file to actually appear rather than a fixed sleep
# — a fixed 2s guess was too short and Gateway tried to connect to a display
# that wasn't listening yet ("Can't connect to X11 window server").
rm -f /tmp/.X1-lock
Xvfb :1 -screen 0 1024x768x16 &
for i in $(seq 1 30); do
  if [ -e /tmp/.X1-lock ]; then
    echo "Xvfb is up on display :1."
    break
  fi
  sleep 1
done
sleep 1

# --- Bind Render's expected HTTP port immediately -------------------------
# Render's web-service health check needs SOMETHING listening on $PORT right
# away, or it keeps "scanning" and eventually kills the deploy — even though
# the real work here (Gateway + the bot) has nothing to do with HTTP. This
# tiny server just answers 200 so the platform sees the container as alive;
# it isn't used for anything else.
PORT="${PORT:-10000}"
python3 -c "
import http.server, threading
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'ok')
    def log_message(self, *a): pass
http.server.HTTPServer(('0.0.0.0', $PORT), H).serve_forever()
" &

# --- Start IB Gateway via IBC (this handles login + keep-alive) -----------
/opt/ibc/gatewaystart.sh -inline --ibc-ini=/root/ibc/config.ini &

# --- Stream IBC/Gateway's own diagnostic log to stdout ---------------------
# IBC writes the real reason for any crash to a log file inside the
# container (path printed in its own startup banner, e.g.
# /root/ibc/logs/ibc-<ver>_GATEWAY-<build>_<Day>.txt) rather than stdout, so
# a failure otherwise shows only a generic "An error has occurred" banner
# on the platform's logs with no way to diagnose it. Find that file as soon
# as it appears and tail it so the real error is visible remotely.
(
  for i in $(seq 1 30); do
    LOGFILE=$(ls -t /root/ibc/logs/*.txt 2>/dev/null | head -n1)
    if [ -n "$LOGFILE" ]; then
      echo "--- streaming IBC diagnostic log: $LOGFILE ---"
      tail -n +1 -F "$LOGFILE" &
      break
    fi
    sleep 1
  done
) &

# --- Wait for Gateway's API port to actually come up before the bot tries --
echo "Waiting for IB Gateway to finish logging in..."
for i in $(seq 1 60); do
  if (echo > /dev/tcp/127.0.0.1/4002) 2>/dev/null; then
    echo "Gateway is up."
    break
  fi
  sleep 5
done

# --- Start the bot ----------------------------------------------------------
export DASHBOARD_URL="${DASHBOARD_URL}"
export DASHBOARD_API_KEY="${DASHBOARD_API_KEY}"
exec python3 /app/momentum_autotrader.py
