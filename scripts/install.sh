#!/usr/bin/env bash
# Install baby_presence as a systemd service on Debian.
# Run as root from the repo root: sudo ./scripts/install.sh
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
    echo "Must be run as root" >&2
    exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
INSTALL_DIR=/opt/baby_presence
DATA_DIR=/var/lib/baby_presence
CONFIG_DIR=/etc/baby_presence
SERVICE_USER=baby_presence

echo ">>> Installing system dependencies"
apt-get update
apt-get install -y python3 python3-venv python3-pip libglib2.0-0 libgl1

echo ">>> Creating service user"
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
fi

echo ">>> Creating directories"
install -d -o "$SERVICE_USER" -g "$SERVICE_USER" \
    "$INSTALL_DIR" "$DATA_DIR" "$DATA_DIR/frames" \
    "$DATA_DIR/.config" "$DATA_DIR/.config/Ultralytics"
install -d -o root -g "$SERVICE_USER" -m 750 "$CONFIG_DIR"

echo ">>> Copying code"
install -o "$SERVICE_USER" -g "$SERVICE_USER" -m 644 \
    "$REPO_DIR"/config.py \
    "$REPO_DIR"/detector.py \
    "$REPO_DIR"/main.py \
    "$REPO_DIR"/mqtt_client.py \
    "$REPO_DIR"/rtsp.py \
    "$REPO_DIR"/requirements.txt \
    "$INSTALL_DIR/"

echo ">>> Creating virtualenv"
if [[ ! -d "$INSTALL_DIR/.venv" ]]; then
    runuser -u "$SERVICE_USER" -- python3 -m venv "$INSTALL_DIR/.venv"
fi
runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/pip" install --upgrade pip
runuser -u "$SERVICE_USER" -- "$INSTALL_DIR/.venv/bin/pip" install -r "$INSTALL_DIR/requirements.txt"

echo ">>> Pre-downloading YOLO weights (yolov8s.pt)"
runuser -u "$SERVICE_USER" -- bash -c \
    "cd '$INSTALL_DIR' && '$INSTALL_DIR/.venv/bin/python' -c 'from ultralytics import YOLO; YOLO(\"yolov8s.pt\")'"

echo ">>> Installing config"
if [[ ! -f "$CONFIG_DIR/baby_presence.env" ]]; then
    install -o root -g "$SERVICE_USER" -m 640 "$REPO_DIR/.env.example" "$CONFIG_DIR/baby_presence.env"
    echo ">>> *** Edit $CONFIG_DIR/baby_presence.env before starting the service ***"
else
    echo ">>> Existing config preserved at $CONFIG_DIR/baby_presence.env"
fi

echo ">>> Installing systemd unit"
install -o root -g root -m 644 "$REPO_DIR/systemd/baby_presence.service" /etc/systemd/system/
systemctl daemon-reload

echo
echo ">>> Done. Next steps:"
echo ">>>   sudoedit $CONFIG_DIR/baby_presence.env"
echo ">>>   sudo systemctl enable --now baby_presence"
echo ">>>   sudo journalctl -u baby_presence -f"
