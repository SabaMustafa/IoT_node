#!/usr/bin/env bash
# Enable the GPIO-header I2C bus (/dev/i2c-1) on a Raspberry Pi.
#
# Out of the box, dtparam=i2c_arm is commented out in /boot/firmware/config.txt,
# so the only I2C buses present are the HDMI DDC ones (i2c-13/i2c-14) — those
# have no pull-ups and report every address as present, which is why a scan of
# them looks like the bus is full of devices.
#
# Requires sudo and a reboot.
set -euo pipefail

CONFIG=/boot/firmware/config.txt
[[ -f $CONFIG ]] || CONFIG=/boot/config.txt

if [[ ! -f $CONFIG ]]; then
	echo "error: no Raspberry Pi config.txt found" >&2
	exit 1
fi

if grep -qE '^\s*dtparam=i2c_arm=on' "$CONFIG"; then
	echo "i2c_arm is already enabled in $CONFIG"
else
	echo "enabling dtparam=i2c_arm=on in $CONFIG (sudo required)"
	sudo cp "$CONFIG" "$CONFIG.bak.$(date +%Y%m%d%H%M%S)"
	if grep -qE '^\s*#\s*dtparam=i2c_arm=on' "$CONFIG"; then
		sudo sed -i 's/^\s*#\s*dtparam=i2c_arm=on/dtparam=i2c_arm=on/' "$CONFIG"
	else
		echo 'dtparam=i2c_arm=on' | sudo tee -a "$CONFIG" >/dev/null
	fi
fi

# Load the userspace interface now and on every boot.
sudo modprobe i2c-dev || true
if ! grep -qE '^\s*i2c-dev' /etc/modules 2>/dev/null; then
	echo 'i2c-dev' | sudo tee -a /etc/modules >/dev/null
fi

if ! id -nG "$USER" | grep -qw i2c; then
	echo "adding $USER to the i2c group"
	sudo usermod -aG i2c "$USER"
fi

echo
echo "Done. Reboot for /dev/i2c-1 to appear:  sudo reboot"
echo "After rebooting, check the sensors with: scripts/i2c-scan.py"
