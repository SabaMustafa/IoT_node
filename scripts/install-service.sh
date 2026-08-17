#!/usr/bin/env bash
# Install the Bee Node services so everything comes up at boot:
#
#   iot-node.service        system service — sensors, recording, upload
#   bee-node-screen.service user service   — the fullscreen dashboard
#   gdrive-mount.service    user service   — ~/GoogleDrive (if configured)
#
# Idempotent: safe to re-run after changing a unit file.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="$HOME/.config/systemd/user"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

if [[ ! -x "$ROOT/.venv/bin/iot-node" ]]; then
	echo "error: $ROOT/.venv/bin/iot-node not found — run: .venv/bin/pip install -e ." >&2
	exit 1
fi

# --- the node itself (system service, starts at boot without a login) ------

step "Installing iot-node.service"
sudo cp "$ROOT/systemd/iot-node.service" /etc/systemd/system/iot-node.service
sudo systemctl daemon-reload
sudo systemctl enable --now iot-node.service

# --- the screen (user service, needs a graphical session) ------------------

step "Installing the Bee Node dashboard"
mkdir -p "$UNIT_DIR"
cp "$ROOT/systemd/bee-node-screen.service" "$UNIT_DIR/bee-node-screen.service"
systemctl --user daemon-reload
systemctl --user enable bee-node-screen.service

# Lingering lets user services (the screen, the Drive mount) start at boot
# rather than waiting for someone to log in.
step "Enabling start-at-boot for user services"
sudo loginctl enable-linger "$USER"

if [[ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]]; then
	systemctl --user restart bee-node-screen.service || true
else
	echo "no display in this session; the screen will start with the desktop"
fi

step "Status"
sudo systemctl status --no-pager iot-node.service | head -12 || true
echo
systemctl --user status --no-pager bee-node-screen.service | head -8 || true

cat <<EOF

Installed. Useful commands:

  journalctl -u iot-node -f                 watch the node
  journalctl --user -u bee-node-screen -f   watch the screen
  systemctl status iot-node                 is it healthy?
  iot-node status                           what is it doing right now?

Both come back automatically after a reboot or a crash.
EOF
