"""Microsoft Graph authentication via MSAL.

Two modes, because which one is available depends on what the tenant admin will
approve:

* ``device_code`` — you sign in once from any browser, MSAL caches the refresh
  token, and the node renews silently from then on. Nothing beyond ordinary user
  consent is needed, but the refresh token dies if it goes unused for ~90 days
  or a Conditional Access policy revokes it, and someone must sign in again.
* ``client_credentials`` — the app authenticates as itself with a secret. Truly
  unattended, but needs an admin to grant the application permission
  Files.ReadWrite.All. There is no signed-in user, so the target drive must be
  named explicitly (``target: user`` or ``target: drive``).
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import msal

from ..config import OneDriveConfig
from .backend import UploadError

log = logging.getLogger(__name__)

AUTHORITY_HOST = "https://login.microsoftonline.com"
GRAPH_HOST = "https://graph.microsoft.com"


class GraphAuthError(UploadError):
    """Authentication failed in a way the caller cannot retry past."""


class GraphAuth:
    """Acquires and refreshes Graph access tokens, caching them on disk."""

    def __init__(self, cfg: OneDriveConfig, cache_path: Path) -> None:
        if not cfg.client_id:
            raise GraphAuthError("cloud.onedrive.client_id is not set (see GRAPH_CLIENT_ID)")
        if not cfg.tenant_id:
            raise GraphAuthError("cloud.onedrive.tenant_id is not set (see GRAPH_TENANT_ID)")

        self.cfg = cfg
        self.cache_path = cache_path
        self._lock = threading.Lock()
        self._cache = msal.SerializableTokenCache()
        self._load_cache()
        self._app = self._build_app()

    @property
    def authority(self) -> str:
        return f"{AUTHORITY_HOST}/{self.cfg.tenant_id}"

    @property
    def scopes(self) -> list[str]:
        if self.cfg.auth_mode == "client_credentials":
            # App-only tokens always use .default against the resource.
            return [f"{GRAPH_HOST}/.default"]
        # Least privilege: writing to your own OneDrive needs no admin consent,
        # writing to somebody else's does.
        if self.cfg.target == "me":
            return ["Files.ReadWrite"]
        return ["Files.ReadWrite.All"]

    def _build_app(self) -> msal.ClientApplication:
        if self.cfg.auth_mode == "client_credentials":
            if not self.cfg.client_secret:
                raise GraphAuthError(
                    "client_credentials mode needs cloud.onedrive.client_secret "
                    "(see GRAPH_CLIENT_SECRET)"
                )
            if self.cfg.target == "me":
                raise GraphAuthError(
                    "client_credentials has no signed-in user: set cloud.onedrive.target "
                    "to 'user' (with user_principal_name) or 'drive' (with drive_id)"
                )
            return msal.ConfidentialClientApplication(
                self.cfg.client_id,
                authority=self.authority,
                client_credential=self.cfg.client_secret,
                token_cache=self._cache,
            )
        return msal.PublicClientApplication(
            self.cfg.client_id, authority=self.authority, token_cache=self._cache
        )

    # --- token cache ---------------------------------------------------------

    def _load_cache(self) -> None:
        if self.cache_path.is_file():
            try:
                self._cache.deserialize(self.cache_path.read_text())
            except (OSError, ValueError) as exc:
                log.warning("ignoring unreadable token cache %s: %s", self.cache_path, exc)

    def _save_cache(self) -> None:
        if not self._cache.has_state_changed:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(self._cache.serialize())
            # The cache holds a refresh token — keep it owner-only.
            os.chmod(tmp, 0o600)
            tmp.replace(self.cache_path)
        except OSError as exc:
            log.error("could not save token cache: %s", exc)

    # --- tokens --------------------------------------------------------------

    def token(self) -> str:
        """Return a valid access token, refreshing silently when possible."""
        with self._lock:
            result = self._acquire_silent()
            if result is None:
                raise GraphAuthError(
                    "no cached credentials — run 'iot-node-auth' once to sign in"
                    if self.cfg.auth_mode == "device_code"
                    else "client credentials rejected; check the secret and admin consent"
                )
            self._save_cache()
            return str(result["access_token"])

    def _acquire_silent(self) -> dict[str, Any] | None:
        if self.cfg.auth_mode == "client_credentials":
            assert isinstance(self._app, msal.ConfidentialClientApplication)
            result = self._app.acquire_token_for_client(scopes=self.scopes)
        else:
            accounts = self._app.get_accounts()
            if not accounts:
                return None
            result = self._app.acquire_token_silent(self.scopes, account=accounts[0])

        if not result or "access_token" not in result:
            if result and result.get("error"):
                log.error(
                    "token request failed: %s — %s",
                    result.get("error"),
                    str(result.get("error_description", ""))[:300],
                )
            return None
        return dict(result)

    def sign_in_device_code(self, prompt: Callable[[str], None] | None = None) -> dict[str, Any]:
        """Run the interactive device-code flow. Blocks until the user signs in."""
        if self.cfg.auth_mode != "device_code":
            raise GraphAuthError(f"auth_mode is {self.cfg.auth_mode!r}, not device_code")
        assert isinstance(self._app, msal.PublicClientApplication)

        flow = self._app.initiate_device_flow(scopes=self.scopes)
        if "user_code" not in flow:
            raise GraphAuthError(
                f"could not start device flow: {flow.get('error_description', flow)}"
            )

        message = str(flow.get("message", ""))
        (prompt or print)(message)

        result = self._app.acquire_token_by_device_flow(flow)  # blocks until done or expired
        if "access_token" not in result:
            raise GraphAuthError(
                f"sign-in failed: {result.get('error')} — {result.get('error_description')}"
            )
        self._save_cache()
        return dict(result)

    def signed_in_account(self) -> str | None:
        if self.cfg.auth_mode == "client_credentials":
            return f"app:{self.cfg.client_id}"
        accounts = self._app.get_accounts()
        return str(accounts[0].get("username")) if accounts else None

    def forget(self) -> None:
        """Drop cached credentials (sign out)."""
        for account in self._app.get_accounts():
            self._app.remove_account(account)
        self._cache = msal.SerializableTokenCache()
        self._app = self._build_app()
        self.cache_path.unlink(missing_ok=True)
