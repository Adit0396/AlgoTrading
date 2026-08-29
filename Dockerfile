# Momentum Auto-Trader — headless cloud container
# Runs IB Gateway (via IBC) + the Python scanning bot together in one
# container, so this can live on a server instead of your laptop.
#
# Deploy: this repo as a Docker web service on Render (or any Docker host).
# Set IBC_USER, IBC_PASSWORD, DASHBOARD_URL, DASHBOARD_API_KEY as
# environment variables in that platform's dashboard — never in this file,
# never committed to git.

FROM eclipse-temurin:17-jre-jammy

RUN apt-get update && apt-get install -y --no-install-recommends \
    xvfb x11vnc wget unzip python3 python3-pip ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ARG IBGATEWAY_VERSION=10.30.1t
RUN wget -q "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh" \
    -O /tmp/ibgateway-install.sh \
    && chmod +x /tmp/ibgateway-install.sh \
    && /tmp/ibgateway-install.sh -q -dir /opt/ibgateway \
    && rm /tmp/ibgateway-install.sh

RUN wget -q "https://github.com/IbcAlpha/IBC/releases/latest/download/IBCLinux-3.20.0.zip" \
    -O /tmp/ibc.zip \
    && mkdir -p /opt/ibc \
    && unzip -q /tmp/ibc.zip -d /opt/ibc \
    && chmod +x /opt/ibc/*.sh /opt/ibc/scripts/*.sh \
    && rm /tmp/ibc.zip

WORKDIR /app
COPY momentum_autotrader.py /app/momentum_autotrader.py
RUN pip3 install --no-cache-dir ib_insync pandas numpy requests

COPY docker/config.ini.template /opt/ibc/config.ini.template
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

ENV DISPLAY=:1
CMD ["/app/entrypoint.sh"]
