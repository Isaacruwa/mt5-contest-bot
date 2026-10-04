#!/usr/bin/env bash
# Installs Wine, MetaTrader 5 and Windows Python (with the MetaTrader5 package) on the runner.
set -euxo pipefail
source "$(dirname "$0")/wine_env.sh"

sudo dpkg --add-architecture i386
sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq wine wine64 wine32:i386 xvfb winbind cabextract wget > /tmp/apt.log 2>&1 || { tail -30 /tmp/apt.log; exit 1; }
wine --version

if [ -f "$WINEPREFIX/.setup_done" ]; then
  echo "== wine prefix restored from cache, skipping MT5/Python install"
  exit 0
fi

echo "== init wine prefix"
wineboot -u || true
wineserver -w || true

echo "== install MetaTrader 5"
wget -q -O /tmp/mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
wine /tmp/mt5setup.exe /auto &
T="$WINEPREFIX/drive_c/Program Files/MetaTrader 5/terminal64.exe"
for i in $(seq 1 120); do [ -f "$T" ] && break; sleep 5; done
[ -f "$T" ] || { echo "MT5 install failed"; exit 1; }
sleep 30
wineserver -k || true
sleep 3

echo "== install Python in Wine"
wget -q -O /tmp/python.exe "https://www.python.org/ftp/python/3.11.9/python-3.11.9-amd64.exe"
wine /tmp/python.exe /quiet InstallAllUsers=1 TargetDir='C:\Python311' PrependPath=1 Include_launcher=0 Include_test=0
wineserver -w || true
wine 'C:\Python311\python.exe' -m pip install --no-warn-script-location --upgrade pip
wine 'C:\Python311\python.exe' -m pip install --no-warn-script-location MetaTrader5 aiohttp
wine 'C:\Python311\python.exe' -c "import MetaTrader5, aiohttp; print('python packages ok', MetaTrader5.__version__)"

touch "$WINEPREFIX/.setup_done"
echo "== setup complete"
