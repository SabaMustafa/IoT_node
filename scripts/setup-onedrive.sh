#!/usr/bin/env bash
# Set up OneDrive on this Pi with rclone: install, sign in, mount at boot.
#
# Sign-in uses an ordinary AUT account through a browser — no Azure app
# registration, no client secret, nothing to paste into a config file. rclone
# stores a refresh token in ~/.config/rclone/rclone.conf and renews it itself.
#
# Run without arguments to do the whole thing; the steps are idempotent.
set -euo pipefail

MOUNT="$HOME/OneDrive"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_SRC="$ROOT/systemd/onedrive-mount.service"
UNIT_DIR="$HOME/.config/systemd/user"

# config.yaml is the single source of truth for the remote's name.
REMOTE="$(sed -n 's/^[[:space:]]*remote:[[:space:]]*\([A-Za-z0-9_-]*\).*/\1/p' \
	"$ROOT/config.yaml" | head -1)"
REMOTE="${REMOTE:-onedrive}"

step() { printf '\n\033[1m==> %s\033[0m\n' "$1"; }

# --- 1. rclone ---------------------------------------------------------------

step "Installing rclone"
if command -v rclone >/dev/null; then
	echo "already installed: $(rclone version | head -1)"
else
	# Debian's package lags badly; the official script tracks current releases.
	curl -fsSL https://rclone.org/install.sh | sudo bash
	echo "installed: $(rclone version | head -1)"
fi

# --- 2. sign in --------------------------------------------------------------

step "Configuring the '$REMOTE' remote"
if rclone listremotes | grep -qx "$REMOTE:"; then
	echo "remote '$REMOTE' already configured"
else
	cat <<'EOF'
rclone config will now ask a series of questions. The answers you want:

  n                        (new remote)
  name>       onedrive
  Storage>    onedrive

    ^ Type that word. Do not hunt for it in the list: the menu is sorted by
      description, so OneDrive is listed under M as "Microsoft OneDrive",
      not under O. Typing the name always works and survives version changes.

  client_id>               (leave blank — press Enter)
  client_secret>           (leave blank — press Enter)
  region>     global       (or press Enter for the default)
  Edit advanced config?    n
  Use web browser to automatically authenticate?  y

A browser opens: sign in with the AUT account and accept. rclone then asks
which drive to use:

  Your choice>  onedrive   (OneDrive Personal or Business)
  Chose drive to use>      (accept the one it found)
  Is this OK?              y
  q                        (quit config)

EOF
	read -rp "Press Enter to start rclone config..."
	rclone config
fi

step "Checking the connection"
rclone about "$REMOTE:" || {
	echo "error: could not reach $REMOTE: — re-run 'rclone config'" >&2
	exit 1
}

# --- 3. mount at boot --------------------------------------------------------

step "Installing the mount service"
mkdir -p "$UNIT_DIR" "$MOUNT"
# Substitute this machine's rclone path and the configured remote name.
sed -e "s|/usr/bin/rclone|$(command -v rclone)|" \
	-e "s|mount [A-Za-z0-9_-]*:|mount $REMOTE:|" \
	"$UNIT_SRC" >"$UNIT_DIR/onedrive-mount.service"

systemctl --user daemon-reload
systemctl --user enable --now onedrive-mount.service

# Without lingering, user services stop at logout and never start at boot.
step "Enabling start-at-boot (linger)"
sudo loginctl enable-linger "$USER"

sleep 3
step "Result"
if mountpoint -q "$MOUNT"; then
	echo "OneDrive is mounted at $MOUNT"
	ls "$MOUNT" | head -5
else
	echo "mount not up yet — check: systemctl --user status onedrive-mount" >&2
fi

cat <<EOF

Done. From now on:

  $MOUNT                                  browse it like any folder
  systemctl --user status onedrive-mount    check it
  journalctl --user -u onedrive-mount -f    watch the log

It remounts automatically on every boot, with no sign-in.
EOF
