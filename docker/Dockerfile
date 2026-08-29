# Momentum Auto-Trader — headless cloud container
# Runs IB Gateway (via IBC) + the Python scanning bot together in one
# container, so this can live on a server instead of your laptop.
#
# Deploy: push this repo to Render (or any Docker host — Fly.io, a VPS,
# DigitalOcean App Platform all work the same way) as a Docker web/worker
# service. Set IBC_USER, IBC_PASSWORD, DASHBOARD_URL, DASHBOARD_API_KEY as
# environment variables in that platform's dashboard — never in this file,
# never committed to git.

FROM eclipse-temurin:17-jre-jammy

# --- System deps: Xvfb for a headless virtual display, Python, unzip ------
RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc wget unzip python3 python3-pip ca-certificates \
    libxtst6 libxrender1 libxi6 libxrandr2 libnss3 libasound2 \
    fontconfig fonts-dejavu-core procps \
    && rm -rf /var/lib/apt/lists/*

# --- IB Gateway --------------------------------------------------------------
ARG IBGATEWAY_VERSION=10.30.1t
RUN wget -q "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh" \
    -O /tmp/ibgateway-install.sh \
    && chmod +x /tmp/ibgateway-install.sh \
    && /tmp/ibgateway-install.sh -q -dir /root/Jts/ibgateway/1045 \
    && rm /tmp/ibgateway-install.sh

# --- IBC (auto-login / keep-alive controller) --------------------------------
RUN IBC_URL=$(wget -qO- https://api.github.com/repos/IbcAlpha/IBC/releases/latest \
        | grep -o '"browser_download_url": *"[^"]*Linux[^"]*\.zip"' \
        | head -n1 \
        | sed -e 's/.*"browser_download_url": *"//' -e 's/"$//') \
    && echo "Resolved IBC download URL: $IBC_URL" \
    && wget -q "$IBC_URL" -O /tmp/ibc.zip \
    && mkdir -p /opt/ibc \
    && unzip -q /tmp/ibc.zip -d /opt/ibc \
    && chmod +x /opt/ibc/*.sh /opt/ibc/scripts/*.sh \
    && rm /tmp/ibc.zip

# --- The momentum bot itself ---------------------------------------------
WORKDIR /app
COPY ../momentum_autotrader.py /app/momentum_autotrader.py
RUN pip3 install --no-cache-dir ib_insync pandas numpy requests lxml

# --- Config template (values injected via env vars at runtime, see entrypoint) --
COPY config.ini.template /opt/ibc/config.ini.template
COPY entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV DISPLAY=:1
CMD ["/app/entrypoint.sh"]
