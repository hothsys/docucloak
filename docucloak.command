#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
source ./app.conf
APP_TITLE="${APP_TITLE:-$APP_NAME}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
./server.sh start || { echo; read -r -p "Press Return to close."; exit 1; }
trap './server.sh stop; exit' HUP INT TERM
echo
read -r -p "$APP_TITLE is running. Press Return to stop it. "
./server.sh stop
