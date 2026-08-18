#!/usr/bin/env bash
#
#  Check that everything came up by itself. Run this after a reboot — it takes
#  a couple of seconds and tells you plainly whether the node is collecting
#  data, whether the screen is on, and whether the microphone is the real one.
#
#      scripts/verify-boot.sh
#
#  Developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>
#
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PASS=0
FAIL=0

ok()   { printf '  \033[32m✓\033[0m %-42s %s\n' "$1" "${2:-}"; PASS=$((PASS+1)); }
bad()  { printf '  \033[31m✗\033[0m %-42s %s\n' "$1" "${2:-}"; FAIL=$((FAIL+1)); }
warn() { printf '  \033[33m!\033[0m %-42s %s\n' "$1" "${2:-}"; }

echo
echo "  BEE NODE — post-boot check          $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "  up for $(uptime -p 2>/dev/null || echo '?')"
echo

# --- the node ---------------------------------------------------------------

[[ "$(systemctl is-enabled iot-node 2>/dev/null)" == "enabled" ]] \
	&& ok "node starts at boot" || bad "node starts at boot" "run scripts/install-service.sh"

if [[ "$(systemctl is-active iot-node 2>/dev/null)" == "active" ]]; then
	ok "node running" "$(systemctl show iot-node -p StatusText --value)"
else
	bad "node running" "journalctl -u iot-node -n 50"
fi

RESTARTS=$(systemctl show iot-node -p NRestarts --value 2>/dev/null || echo '?')
[[ "$RESTARTS" == "0" ]] && ok "no restarts since boot" \
	|| warn "restarts since boot: $RESTARTS" "repeated restarts mean something is wrong"

# Exactly one node, or two copies fight over the same files.
COPIES=$(pgrep -cf 'iot-node (run|record)' 2>/dev/null || echo 0)
[[ "$COPIES" -le 1 ]] && ok "single node process" \
	|| bad "single node process" "$COPIES copies running — kill the strays"

# --- the screen -------------------------------------------------------------

[[ "$(systemctl --user is-enabled bee-node-screen 2>/dev/null)" == "enabled" ]] \
	&& ok "screen starts at boot" || bad "screen starts at boot"

[[ "$(systemctl --user is-active bee-node-screen 2>/dev/null)" == "active" ]] \
	&& ok "screen running" || bad "screen running" "systemctl --user status bee-node-screen"

[[ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" == "yes" ]] \
	&& ok "user services start without a login" \
	|| bad "lingering enabled" "sudo loginctl enable-linger $USER"

# --- hardware ---------------------------------------------------------------

[[ -e /dev/i2c-1 ]] && ok "I2C bus present" || bad "I2C bus present" "scripts/enable-i2c.sh"

MIC=$("$ROOT/.venv/bin/python" -c "
from iot_node.audio.recorder import select_device
d = select_device('auto')
print(d if d else '')" 2>/dev/null)
if [[ -n "$MIC" ]]; then
	ok "AudioMoth connected" "$MIC"
else
	bad "AudioMoth connected" "$("$ROOT/.venv/bin/python" -c "
from iot_node.audio.recorder import diagnose_missing_audiomoth
print(diagnose_missing_audiomoth())" 2>/dev/null | cut -c1-60)"
fi

# --- data path --------------------------------------------------------------

[[ "$(systemctl --user is-active gdrive-mount 2>/dev/null)" == "active" ]] \
	&& ok "Google Drive mounted" "~/GoogleDrive" || warn "Drive mount not running" "browsing only; uploads are unaffected"

if timeout 45 rclone about gdrive: >/dev/null 2>&1; then
	FREE=$(timeout 45 rclone about gdrive: 2>/dev/null | awk '/^Free:/{print $2, $3}')
	ok "cloud reachable" "${FREE:-} free"
else
	warn "cloud unreachable" "data will queue locally until it returns"
fi

STATUS="$ROOT/var/state/status.json"
if [[ -f "$STATUS" ]]; then
	AGE=$(( $(date +%s) - $(stat -c %Y "$STATUS") ))
	[[ "$AGE" -lt 120 ]] && ok "publishing live status" "${AGE}s ago" \
		|| bad "status is stale" "${AGE}s old — the node may be stuck"
else
	bad "status file" "the node has not published yet"
fi

# --- verdict ----------------------------------------------------------------

echo
if [[ "$FAIL" -eq 0 ]]; then
	printf '  \033[32mALL %d CHECKS PASSED\033[0m — the system came up on its own.\n\n' "$PASS"
	exit 0
fi
printf '  \033[31m%d CHECK(S) FAILED\033[0m (%d passed). See docs/OPERATING.md.\n\n' "$FAIL" "$PASS"
exit 1
