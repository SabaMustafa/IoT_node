#!/usr/bin/env bash
#
#  Build a versioned, redeployable release zip and publish it to Google Drive
#  under IoT/releases/.
#
#      scripts/make-release.sh              build and upload
#      scripts/make-release.sh --local      build only, do not upload
#
#  The zip contains everything needed to stand the node up on a bare Pi: source,
#  scripts, systemd units, documentation, tests, and install.sh. It deliberately
#  excludes the virtualenv, collected data, and anything holding a credential.
#
#  Developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>
#
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UPLOAD=1
[[ "${1:-}" == "--local" ]] && UPLOAD=0

VERSION="$("$ROOT/.venv/bin/python" -c \
	"import tomllib,pathlib; print(tomllib.loads(pathlib.Path('$ROOT/pyproject.toml').read_text())['project']['version'])")"
STAMP="$(date +%Y%m%d-%H%M)"
NAME="bee-node-v${VERSION}"
ARCHIVE="${NAME}-${STAMP}.zip"

REMOTE="$(sed -n 's/^[[:space:]]*remote:[[:space:]]*\([A-Za-z0-9_-]*\).*/\1/p' \
	"$ROOT/config.yaml" | head -1)"
REMOTE="${REMOTE:-gdrive}"
REMOTE_ROOT="$(sed -n 's/^[[:space:]]*remote_root:[[:space:]]*"\{0,1\}\([A-Za-z0-9_/-]*\).*/\1/p' \
	"$ROOT/config.yaml" | head -1)"
REMOTE_ROOT="${REMOTE_ROOT:-IoT}"

step() { printf '\n\033[1;33m==> %s\033[0m\n' "$1"; }

step "Building $ARCHIVE"

BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
STAGE="$BUILD/$NAME"
mkdir -p "$STAGE"

# Everything a fresh machine needs, and nothing that is data or a secret.
for item in src scripts systemd docs tests config.yaml pyproject.toml \
	README.md install.sh .env.example .gitignore; do
	[[ -e "$ROOT/$item" ]] && cp -r "$ROOT/$item" "$STAGE/"
done

# Drop caches that may have crept into the source tree.
find "$STAGE" -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
find "$STAGE" -type d -name '*.egg-info' -prune -exec rm -rf {} + 2>/dev/null || true

echo "$VERSION" >"$STAGE/VERSION"
chmod +x "$STAGE/install.sh" "$STAGE"/scripts/*.sh "$STAGE"/scripts/*.py 2>/dev/null || true

# A guard against shipping a credential by accident. It looks for secret-shaped
# *values*, not the words: source that merely handles tokens says
# "refresh_token" all over the place and must not trip this.
SECRET_PATTERNS=(
	'GOCSPX-[A-Za-z0-9_-]{15,}'                       # Google client secret
	'"(access|refresh)_token"[[:space:]]*:[[:space:]]*"[A-Za-z0-9._-]{20,}"'
	'client_secret[[:space:]]*=[[:space:]]*[A-Za-z0-9._~-]{15,}'
	'-----BEGIN [A-Z ]*PRIVATE KEY-----'
)
LEAKS=""
for pattern in "${SECRET_PATTERNS[@]}"; do
	found=$(grep -rlIE "$pattern" "$STAGE" 2>/dev/null || true)
	[[ -n "$found" ]] && LEAKS+="$found"$'\n'
done
if [[ -n "${LEAKS//[[:space:]]/}" ]]; then
	echo "error: the staged release appears to contain a credential — aborting" >&2
	echo "$LEAKS" | sed 's/^/    /' >&2
	exit 1
fi
echo "    no credentials found in the staged tree"

cat >"$STAGE/MANIFEST.txt" <<EOF
Bee Node ${VERSION}
built ${STAMP} on $(hostname)

Hive environmental and acoustic monitoring for a Raspberry Pi.
Developed by Mohammad (Amin) BarzegarTabrizi <mabiran@gmail.com>

TO DEPLOY ON A FRESH PI
    unzip ${ARCHIVE}
    cd ${NAME}
    ./install.sh

  install.sh does everything: system packages, Python environment, I2C,
  rclone, the Google Drive mount, and both systemd services set to start at
  boot. It is safe to re-run, so it is also the upgrade path.

  Options: --no-cloud   --no-service   --dev

WHAT STARTS THE SYSTEM
    iot-node run              the whole node (this is what the service runs)
    iot-node-screen           the Bee Node display

  Under systemd:
    iot-node.service          the node, starts at boot, watchdogged
    bee-node-screen.service   the display, starts with the desktop
    gdrive-mount.service      ~/GoogleDrive

CONTENTS
    install.sh                one-command deploy
    src/iot_node/             the application
    scripts/                  setup and diagnostic helpers
    systemd/                  the three unit files
    docs/ARCHITECTURE.md      full system map — read this first
    tests/                    $(ls "$STAGE/tests"/test_*.py 2>/dev/null | wc -l) test files, no hardware needed
    config.yaml               deployment plumbing (paths, remote name)
    .env.example              template for credentials, if the Graph path is used

  config.ini is NOT in this archive: it lives in Google Drive under
  ${REMOTE_ROOT}/ and is written there automatically on first run.

NOT INCLUDED (deliberately)
    .venv/                    rebuilt by install.sh
    var/                      collected data and local state
    secrets/, .env            credentials
EOF

( cd "$BUILD" && zip -qr "$ARCHIVE" "$NAME" )
mkdir -p "$ROOT/dist"
mv "$BUILD/$ARCHIVE" "$ROOT/dist/$ARCHIVE"

SIZE=$(du -h "$ROOT/dist/$ARCHIVE" | cut -f1)
FILES=$(unzip -l "$ROOT/dist/$ARCHIVE" | tail -1 | awk '{print $2}')
echo "    dist/$ARCHIVE  ($SIZE, $FILES files)"

# Prove the archive is complete before it is published anywhere.
step "Verifying the archive"
CHECK="$BUILD/check"
mkdir -p "$CHECK"
unzip -q "$ROOT/dist/$ARCHIVE" -d "$CHECK"
for required in install.sh VERSION MANIFEST.txt src/iot_node/main.py \
	docs/ARCHITECTURE.md systemd/iot-node.service pyproject.toml; do
	if [[ ! -e "$CHECK/$NAME/$required" ]]; then
		echo "error: $required missing from the archive" >&2
		exit 1
	fi
done
bash -n "$CHECK/$NAME/install.sh"
echo "    contents and install.sh syntax OK"

if [[ $UPLOAD -eq 0 ]]; then
	step "Built (not uploaded)"
	exit 0
fi

step "Publishing to ${REMOTE}:${REMOTE_ROOT}/releases/"
rclone copyto "$ROOT/dist/$ARCHIVE" "${REMOTE}:${REMOTE_ROOT}/releases/$ARCHIVE" --stats 0
# A stable name so "the latest release" is always one predictable path.
rclone copyto "$ROOT/dist/$ARCHIVE" "${REMOTE}:${REMOTE_ROOT}/releases/bee-node-latest.zip" --stats 0
cp "$STAGE/MANIFEST.txt" "$BUILD/README-releases.txt"
rclone copyto "$BUILD/README-releases.txt" "${REMOTE}:${REMOTE_ROOT}/releases/README.txt" --stats 0

step "Published"
rclone lsl "${REMOTE}:${REMOTE_ROOT}/releases/" | sed 's/^/    /'
