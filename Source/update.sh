#!/bin/bash
set -e

PYTHON_BIN="python3"
INSTALLER_URL="https://raw.githubusercontent.com/hoseinlolready/Pingtunnel_manager/refs/heads/main/Source/Pingtunnel.py" 
INSTALLER_PATH="/usr/local/bin/updater_pingtunnel.py"

check_root() {
  if [ "$(id -u)" -ne 0 ]; then
    echo "❌ Please run as root (sudo)."
    exit 1
  fi
}

download_updater() {
  echo "⬇️ Downloading Python updater..."
  apt install python3 python3-pip wget curl -y
  pip3 install colorama
  curl -fsSL "$INSTALLER_URL" -o "$INSTALLER_PATH"
  chmod +x "$INSTALLER_PATH"
  echo "✅ Installer saved at $INSTALLER_PATH"
}

updater_pingtunnel() {
  check_root
  download_updater
  echo "🚀 Running Updater..."
  pingtunnel stop
  $PYTHON_BIN "$INSTALLER_PATH"
  read -n 1 -s -r -p "Press any key to continue..."
}
echo Starting To Update

updater_pingtunnel()
