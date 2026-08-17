"""`iot-node-auth` — one-time interactive sign-in for the OneDrive uploader.

Run this once on the node (or anywhere with the same config and cache path):

    iot-node-auth              # sign in and store the refresh token
    iot-node-auth --status     # show who is signed in and which drive we hit
    iot-node-auth --logout     # forget the cached credentials
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..config import load_config
from ..logging_setup import setup_logging
from .auth import GraphAuth, GraphAuthError
from .onedrive import GraphError, OneDriveClient


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-c", "--config", type=Path, help="path to config.yaml")
    parser.add_argument("--status", action="store_true", help="show current sign-in state")
    parser.add_argument("--logout", action="store_true", help="delete the cached credentials")
    args = parser.parse_args(argv)

    cfg = load_config(args.config)
    setup_logging(cfg)
    onedrive = cfg.cloud.onedrive

    try:
        auth = GraphAuth(onedrive, cfg.resolve(onedrive.token_cache))
    except GraphAuthError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.logout:
        auth.forget()
        print("signed out; cached credentials removed")
        return 0

    if not args.status:
        if onedrive.auth_mode == "device_code":
            print(f"Signing in to tenant {onedrive.tenant_id} as app {onedrive.client_id}\n")
            try:
                auth.sign_in_device_code()
            except GraphAuthError as exc:
                print(f"\nerror: {exc}", file=sys.stderr)
                return 1
            print("\nsigned in; refresh token cached")
        else:
            print("auth_mode is client_credentials — no interactive sign-in needed")

    account = auth.signed_in_account()
    print(f"account: {account or '(none)'}")

    client = OneDriveClient(onedrive, auth)
    try:
        drive = client.whoami()
    except (GraphAuthError, GraphError) as exc:
        print(f"drive check failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()

    quota = drive.get("quota", {})
    print(f"drive:   {drive.get('name')} ({drive.get('driveType')}) id={drive.get('id')}")
    print(
        f"quota:   {quota.get('remaining', 0) / 1e9:.1f} GB free "
        f"of {quota.get('total', 0) / 1e9:.1f} GB"
    )
    print(f"target:  {onedrive.remote_root.format(device_id=cfg.device.id, site=cfg.device.site)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
