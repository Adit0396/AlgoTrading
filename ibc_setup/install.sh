#!/bin/bash
# One-time setup: installs IB Gateway + IBC (the auto-login/keep-alive
# controller) and registers both the Gateway and the momentum bot as
# macOS LaunchAgents so they start automatically at login and restart
# themselves if they ever crash.
#
# Run this yourself in Terminal.app on your Mac (not through Claude):
#   cd ~/Documents/Repos/AlgoTrading/ibc_setup
#   chmod +x install.sh
#   ./install.sh
#
# You will be prompted once to fill in config.ini with your IBKR paper
# login. After that, everything below runs unattended forever.

set -e

REPO_DIR="$HOME/Documents/Repos/AlgoTrading"
IBC_DIR="$HOME/ibc"
LOGDIR="$HOME/Library/Logs/momentum-autotrader"
mkdir -p "$LOGDIR"

echo "== Step 1: IB Gateway =="
if [ ! -d "/Applications/IB Gateway 10.x" ] && [ ! -d "/Applications/Trader Workstation" ]; then
  echo "IB Gateway not found in /Applications."
  echo "Download and install it manually from:"
  echo "  https://www.interactivebrokers.com/en/trading/ibgateway-stable.php"
  echo "Then re-run this script."
  exit 1
fi

echo "== Step 2: Installing IBC =="
if [ ! -d "$IBC_DIR" ]; then
  mkdir -p "$IBC_DIR"
  cd /tmp
  IBC_URL=$(curl -sL https://api.github.com/repos/IbcAlpha/IBC/releases/latest \
      | grep -o '"browser_download_url": *"[^"]*Macos[^"]*\.zip"' \
      | head -n1 \
      | sed -e 's/.*"browser_download_url": *"//' -e 's/"$//')
  echo "Resolved IBC download URL: $IBC_URL"
  curl -L -o ibc.zip "$IBC_URL"
  unzip -o ibc.zip -d "$IBC_DIR"
  chmod +x "$IBC_DIR"/*.sh
fi

echo "== Step 3: Config =="
if [ ! -f "$REPO_DIR/ibc_setup/config.ini" ]; then
  cp "$REPO_DIR/ibc_setup/config.ini.template" "$REPO_DIR/ibc_setup/config.ini"
  echo ""
  echo ">>> Open $REPO_DIR/ibc_setup/config.ini now and fill in IbLoginId"
  echo ">>> and IbPassword with your IBKR PAPER account login, then re-run"
  echo ">>> this script to continue."
  open -e "$REPO_DIR/ibc_setup/config.ini"
  exit 0
fi

echo "== Step 4: LaunchAgent for IB Gateway (via IBC) =="
cat > "$HOME/Library/LaunchAgents/com.aditya.ibgateway.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.aditya.ibgateway</string>
  <key>ProgramArguments</key>
  <array>
    <string>$IBC_DIR/gatewaystartmacos.sh</string>
    <string>-inline</string>
    <string>--config=$REPO_DIR/ibc_setup/config.ini</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/gateway.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/gateway-error.log</string>
</dict>
</plist>
PLIST

echo "== Step 5: LaunchAgent for the momentum bot =="
cat > "$HOME/Library/LaunchAgents/com.aditya.momentumbot.plist" << PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.aditya.momentumbot</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$REPO_DIR/momentum_autotrader.py</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>DASHBOARD_URL</key><string>https://momentum-dashboard-jxd6.onrender.com/api/events</string>
    <key>DASHBOARD_API_KEY</key><string>mom-scan-a17f92c8d64e</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$LOGDIR/bot.log</string>
  <key>StandardErrorPath</key><string>$LOGDIR/bot-error.log</string>
</dict>
</plist>
PLIST

echo "== Step 6: Loading both agents =="
launchctl unload "$HOME/Library/LaunchAgents/com.aditya.ibgateway.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.aditya.ibgateway.plist"
sleep 20   # give Gateway time to fully log in before the bot tries to connect
launchctl unload "$HOME/Library/LaunchAgents/com.aditya.momentumbot.plist" 2>/dev/null || true
launchctl load "$HOME/Library/LaunchAgents/com.aditya.momentumbot.plist"

echo ""
echo "Done. Gateway and the bot now start automatically every time you log"
echo "into this Mac, and macOS will restart either one if it ever crashes."
echo "Logs: $LOGDIR"
