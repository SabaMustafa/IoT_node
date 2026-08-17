#!/usr/bin/env bash
#
#  Bee Node — one-command install on a fresh Raspberry Pi.
#
#  Unpack the release zip anywhere and run this. It brings the whole node up:
#  system packages, Python environment, I2C, rclone, the Google Drive link, and
#  both systemd services so everything starts at boot.
#
#      unzip bee-node-v0.2.0-*.zip
#      cd bee-node-v0.2.0
#      ./install.sh
#
#  Safe to re-run: every step checks whether it has already been done, so this
#  doubles as the upgrade path.
#
#  Options:
#      --no-cloud     skip rclone install and Google sign-in
#      --no-service   install everything but do not enable the services
#      --dev          development setup only (venv + deps + tests)
#
#  Developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(cat "$ROOT/VERSION" 2>/dev/null || echo "unknown")"

WANT_CLOUD=1
WANT_SERVICE=1
DEV_ONLY=0
for arg in "$@"; do
	case "$arg" in
	--no-cloud) WANT_CLOUD=0 ;;
	--no-service) WANT_SERVICE=0 ;;
	--dev) DEV_ONLY=1; WANT_CLOUD=0; WANT_SERVICE=0 ;;
	-h | --help) sed -n '2,25p' "${BASH_SOURCE[0]}" | sed 's/^#//'; exit 0 ;;
	*) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
	esac
done

step() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }
note() { printf '    %s\n' "$1"; }
warn() { printf '\033[1;31m    ! %s\033[0m\n' "$1"; }

cat <<BANNER

    ██████  ███████ ███████   ███    ██  ██████  ██████  ███████
    ██   ██ ██      ██        ████   ██ ██    ██ ██   ██ ██
    ██████  █████   █████     ██ ██  ██ ██    ██ ██   ██ █████
    ██   ██ ██      ██        ██  ██ ██ ██    ██ ██   ██ ██
    ██████  ███████ ███████   ██   ████  ██████  ██████  ███████

    hive environmental and acoustic monitoring        version ${VERSION}
    Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>

BANNER

# --- 1. sanity ---------------------------------------------------------------

step "Checking the machine"
if [[ -r /proc/device-tree/model ]]; then
	note "$(tr -d '\0' </proc/device-tree/model)"
else
	warn "not a Raspberry Pi — sensors and audio will not work, but the rest will"
fi
note "user: $USER    arch: $(dpkg --print-architecture 2>/dev/null || uname -m)"

if ! command -v python3 >/dev/null; then
	echo "error: python3 is not installed" >&2
	exit 1
fi
PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
note "python: $PYVER"
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || {
	echo "error: Python 3.12 or newer is required (found $PYVER)" >&2
	exit 1
}

# --- 2. system packages ------------------------------------------------------

step "Installing system packages"
NEEDED=(python3-venv python3-tk alsa-utils i2c-tools fuse3 curl unzip)
MISSING=()
for pkg in "${NEEDED[@]}"; do
	dpkg -s "$pkg" >/dev/null 2>&1 || MISSING+=("$pkg")
done
if ((${#MISSING[@]})); then
	note "installing: ${MISSING[*]}"
	sudo apt-get update -qq
	sudo apt-get install -y "${MISSING[@]}"
else
	note "all present"
fi

# --- 3. python environment ---------------------------------------------------

step "Building the Python environment"
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
	python3 -m venv "$ROOT/.venv"
	note "created $ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install --quiet --upgrade pip
if [[ $DEV_ONLY -eq 1 ]]; then
	"$ROOT/.venv/bin/pip" install --quiet -e "$ROOT[dev]"
else
	"$ROOT/.venv/bin/pip" install --quiet -e "$ROOT"
fi
note "$("$ROOT/.venv/bin/iot-node" --help >/dev/null 2>&1 && echo 'iot-node installed' || echo 'install FAILED')"

if [[ $DEV_ONLY -eq 1 ]]; then
	step "Running the test suite"
	"$ROOT/.venv/bin/python" -m pytest -q
	step "Done (development setup)"
	note "run the node:   $ROOT/.venv/bin/iot-node run"
	exit 0
fi

# --- 4. I2C ------------------------------------------------------------------

step "Enabling I2C for the sensors"
if [[ -e /dev/i2c-1 ]]; then
	note "/dev/i2c-1 already present"
else
	if command -v raspi-config >/dev/null; then
		sudo raspi-config nonint do_i2c 0 || true
		sudo dtparam i2c_arm=on 2>/dev/null || true
	fi
	if [[ -e /dev/i2c-1 ]]; then
		note "/dev/i2c-1 enabled"
	else
		warn "/dev/i2c-1 still missing — reboot, then re-run this script"
	fi
fi
id -nG "$USER" | grep -qw i2c || { sudo usermod -aG i2c "$USER"; note "added $USER to the i2c group"; }
id -nG "$USER" | grep -qw audio || { sudo usermod -aG audio "$USER"; note "added $USER to the audio group"; }

if [[ -e /dev/i2c-1 ]]; then
	"$ROOT/.venv/bin/python" "$ROOT/scripts/i2c-scan.py" --bus 1 2>/dev/null | sed 's/^/    /' || true
fi

# --- 5. cloud ----------------------------------------------------------------

if [[ $WANT_CLOUD -eq 1 ]]; then
	step "Setting up the Google Drive link"
	if ! command -v rclone >/dev/null; then
		note "installing rclone"
		curl -fsSL https://rclone.org/install.sh | sudo bash
	fi
	note "rclone $(rclone version | head -1 | awk '{print $2}')"

	REMOTE="$(sed -n 's/^[[:space:]]*remote:[[:space:]]*\([A-Za-z0-9_-]*\).*/\1/p' \
		"$ROOT/config.yaml" | head -1)"
	REMOTE="${REMOTE:-gdrive}"

	if rclone listremotes 2>/dev/null | grep -qx "$REMOTE:"; then
		note "remote '$REMOTE' already configured"
	else
		cat <<EOF

    No rclone remote named '$REMOTE' yet. You need one before the node can
    upload. Create it with your own Google OAuth client (recommended — the
    shared one is heavily rate limited):

      1. console.cloud.google.com -> new project -> enable "Google Drive API"
      2. Google Auth Platform -> Audience -> PUBLISH APP
         (in Testing mode Google expires refresh tokens after 7 days)
      3. Clients -> Create client -> Desktop app -> copy id and secret
      4. rclone config
           n -> name: $REMOTE -> Storage: drive
           paste the client id and secret, scope 1, advanced n, browser y

    Then re-run this script.

EOF
		warn "skipping the cloud steps until '$REMOTE' exists"
		WANT_CLOUD=0
	fi
fi

if [[ $WANT_CLOUD -eq 1 ]]; then
	if rclone about "$REMOTE:" >/dev/null 2>&1; then
		note "$(rclone about "$REMOTE:" | tr '\n' ' ')"
	else
		warn "cannot reach $REMOTE: — check 'rclone config reconnect $REMOTE:'"
	fi

	step "Mounting Google Drive at ~/GoogleDrive"
	mkdir -p "$HOME/GoogleDrive" "$HOME/.config/systemd/user"
	sed -e "s|/usr/bin/rclone|$(command -v rclone)|" \
		-e "s|mount [A-Za-z0-9_-]*:|mount $REMOTE:|" \
		"$ROOT/systemd/gdrive-mount.service" >"$HOME/.config/systemd/user/gdrive-mount.service"
	systemctl --user daemon-reload
	systemctl --user enable --now gdrive-mount.service || warn "mount did not start"
fi

# --- 6. services -------------------------------------------------------------

if [[ $WANT_SERVICE -eq 1 ]]; then
	step "Installing the services"
	# The unit files carry absolute paths; rewrite them for wherever this
	# release was unpacked, so the node need not live in /home/saba/iot-node.
	TMP=$(mktemp -d)
	trap 'rm -rf "$TMP"' EXIT
	sed -e "s|/home/saba/iot-node|$ROOT|g" -e "s|^User=.*|User=$USER|" \
		-e "s|^Group=.*|Group=$(id -gn)|" \
		"$ROOT/systemd/iot-node.service" >"$TMP/iot-node.service"
	sudo cp "$TMP/iot-node.service" /etc/systemd/system/iot-node.service
	sudo systemctl daemon-reload
	sudo systemctl enable --now iot-node.service
	note "iot-node.service enabled"

	mkdir -p "$HOME/.config/systemd/user"
	sed -e "s|/home/saba/iot-node|$ROOT|g" \
		"$ROOT/systemd/bee-node-screen.service" >"$HOME/.config/systemd/user/bee-node-screen.service"
	systemctl --user daemon-reload
	systemctl --user enable bee-node-screen.service
	systemctl --user start bee-node-screen.service 2>/dev/null || \
		note "screen will start with the desktop session"
	note "bee-node-screen.service enabled"

	# Without lingering, user services wait for a login instead of starting at boot.
	sudo loginctl enable-linger "$USER"
	note "start-at-boot enabled for user services"
fi

# --- 7. done -----------------------------------------------------------------

step "Installed"
cat <<EOF

    THE NODE IS RUNNING. It starts automatically on every boot.

    Check it:
      systemctl status iot-node             is it healthy?
      $ROOT/.venv/bin/iot-node status       what is it doing now?
      $ROOT/.venv/bin/iot-node config       what timetable is in force?
      journalctl -u iot-node -f             watch it work

    The screen:
      $ROOT/.venv/bin/iot-node-screen       (also starts at boot)

    Change what it does — edit config.ini in Google Drive, under IoT/.
    The node re-reads it every hour; no need to touch this machine.

    Full documentation: $ROOT/docs/ARCHITECTURE.md

EOF
sudo systemctl status --no-pager iot-node.service 2>/dev/null | head -8 || true
