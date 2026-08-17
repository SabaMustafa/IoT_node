"""OneDrive uploads over the Microsoft Graph API.

Files under 4 MiB go up in a single PUT. Anything larger — which is every
AudioMoth recording — uses a resumable upload session: Graph hands back a
pre-authenticated URL, we push fixed-size chunks at it, and if the link drops we
ask the service which byte it got to and carry on from there rather than
restarting a 30 MiB transfer.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests

from ..config import OneDriveConfig
from .auth import GRAPH_HOST, GraphAuth
from .backend import UploadError

log = logging.getLogger(__name__)

GRAPH_V1 = f"{GRAPH_HOST}/v1.0"

SIMPLE_UPLOAD_LIMIT = 4 * 1024 * 1024
CHUNK_MULTIPLE = 320 * 1024  # Graph requires chunk sizes in multiples of this

_TIMEOUT = (10, 120)  # (connect, read) seconds
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})


class GraphError(UploadError):
    """A Graph request failed. Retryable ones carry ``retryable=True``."""

    def __init__(self, message: str, *, status: int | None = None, retryable: bool = False) -> None:
        super().__init__(message, retryable=retryable)
        self.status = status


class OneDriveClient:
    def __init__(self, cfg: OneDriveConfig, auth: GraphAuth, *, max_retries: int = 5) -> None:
        self.cfg = cfg
        self.auth = auth
        self.max_retries = max_retries
        self.session = requests.Session()
        self._known_folders: set[str] = set()

    def close(self) -> None:
        self.session.close()

    # --- addressing ----------------------------------------------------------

    @property
    def drive_base(self) -> str:
        """The Graph URL prefix for the configured drive."""
        target = self.cfg.target
        if target == "me":
            return f"{GRAPH_V1}/me/drive"
        if target == "user":
            if not self.cfg.user_principal_name:
                raise GraphError("cloud.onedrive.user_principal_name is not set")
            return f"{GRAPH_V1}/users/{quote(self.cfg.user_principal_name)}/drive"
        if not self.cfg.drive_id:
            raise GraphError("cloud.onedrive.drive_id is not set")
        return f"{GRAPH_V1}/drives/{quote(self.cfg.drive_id)}"

    def item_url(self, remote_path: str, suffix: str = "") -> str:
        """Build a path-addressed item URL: /drive/root:/a/b.wav:/content"""
        path = remote_path.strip("/")
        if not path:
            return f"{self.drive_base}/root{suffix}"
        encoded = quote(path, safe="/")
        return f"{self.drive_base}/root:/{encoded}:{suffix}" if suffix else (
            f"{self.drive_base}/root:/{encoded}"
        )

    # --- plumbing ------------------------------------------------------------

    def _request(
        self, method: str, url: str, *, authed: bool = True, **kwargs: Any
    ) -> requests.Response:
        """Issue a Graph request, retrying throttles and transient failures."""
        kwargs.setdefault("timeout", _TIMEOUT)
        last_error: str = ""

        for attempt in range(self.max_retries + 1):
            headers = dict(kwargs.pop("headers", {}) or {})
            if authed:
                headers["Authorization"] = f"Bearer {self.auth.token()}"
            try:
                response = self.session.request(method, url, headers=headers, **kwargs)
            except requests.RequestException as exc:
                last_error = str(exc)
                if attempt >= self.max_retries:
                    raise GraphError(f"{method} {url} failed: {exc}", retryable=True) from exc
                self._sleep_backoff(attempt)
                continue

            if response.status_code not in _RETRY_STATUS:
                return response

            last_error = f"HTTP {response.status_code}: {response.text[:200]}"
            if attempt >= self.max_retries:
                break
            # Graph tells us how long to wait when it throttles; honour it.
            retry_after = response.headers.get("Retry-After")
            if retry_after and retry_after.isdigit():
                delay = min(int(retry_after), 300)
                log.info("throttled by Graph, waiting %ds", delay)
                time.sleep(delay)
            else:
                self._sleep_backoff(attempt)

        raise GraphError(f"{method} {url} gave up after retries: {last_error}", retryable=True)

    @staticmethod
    def _sleep_backoff(attempt: int) -> None:
        time.sleep(min(2**attempt, 60))

    @staticmethod
    def _check(response: requests.Response, what: str) -> dict[str, Any]:
        if response.status_code >= 400:
            retryable = response.status_code in _RETRY_STATUS or response.status_code >= 500
            raise GraphError(
                f"{what} failed: HTTP {response.status_code} {response.text[:300]}",
                status=response.status_code,
                retryable=retryable,
            )
        if not response.content:
            return {}
        try:
            return dict(response.json())
        except ValueError:
            return {}

    # --- drive operations ----------------------------------------------------

    def whoami(self) -> dict[str, Any]:
        """Fetch the target drive — the cheapest end-to-end credential check."""
        return self._check(self._request("GET", self.drive_base), "drive lookup")

    def ensure_folder(self, folder_path: str) -> None:
        """Create the remote folder tree if it is not already there."""
        folder_path = folder_path.strip("/")
        if not folder_path or folder_path in self._known_folders:
            return

        built = ""
        for segment in folder_path.split("/"):
            parent, built = built, f"{built}/{segment}" if built else segment
            if built in self._known_folders:
                continue
            parent_url = self.item_url(parent, "/children") if parent else (
                f"{self.drive_base}/root/children"
            )
            response = self._request(
                "POST",
                parent_url,
                json={
                    "name": segment,
                    "folder": {},
                    # Someone else may have created it between our check and now.
                    "@microsoft.graph.conflictBehavior": "fail",
                },
            )
            if response.status_code == 409:
                self._known_folders.add(built)
                continue
            self._check(response, f"create folder {built}")
            self._known_folders.add(built)
            log.info("created remote folder %s", built)

    def get_item(self, remote_path: str) -> dict[str, Any] | None:
        response = self._request("GET", self.item_url(remote_path))
        if response.status_code == 404:
            return None
        return self._check(response, f"lookup {remote_path}")

    def upload(self, local_path: Path, remote_path: str, *, chunk_size: int) -> dict[str, Any]:
        """Upload a file, choosing the simple or resumable path by size."""
        size = local_path.stat().st_size
        parent = remote_path.rsplit("/", 1)[0] if "/" in remote_path else ""
        if parent:
            self.ensure_folder(parent)

        if size <= SIMPLE_UPLOAD_LIMIT:
            return self._upload_simple(local_path, remote_path)
        return self._upload_session(local_path, remote_path, size=size, chunk_size=chunk_size)

    def _upload_simple(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        with local_path.open("rb") as fh:
            response = self._request(
                "PUT",
                self.item_url(remote_path, "/content"),
                params={"@microsoft.graph.conflictBehavior": self.cfg.conflict_behavior},
                data=fh,
                headers={"Content-Type": "application/octet-stream"},
            )
        return self._check(response, f"upload {remote_path}")

    def _create_session(self, remote_path: str) -> str:
        response = self._request(
            "POST",
            self.item_url(remote_path, "/createUploadSession"),
            json={
                "item": {
                    "@microsoft.graph.conflictBehavior": self.cfg.conflict_behavior,
                    "name": remote_path.rsplit("/", 1)[-1],
                }
            },
        )
        data = self._check(response, f"create upload session for {remote_path}")
        url = data.get("uploadUrl")
        if not url:
            raise GraphError(f"no uploadUrl in session response for {remote_path}")
        return str(url)

    def _upload_session(
        self, local_path: Path, remote_path: str, *, size: int, chunk_size: int
    ) -> dict[str, Any]:
        if chunk_size % CHUNK_MULTIPLE:
            raise GraphError(f"chunk size {chunk_size} is not a multiple of 320 KiB")

        upload_url = self._create_session(remote_path)
        start = 0
        attempts = 0

        with local_path.open("rb") as fh:
            while start < size:
                end = min(start + chunk_size, size) - 1
                fh.seek(start)
                chunk = fh.read(end - start + 1)

                try:
                    # The upload URL is pre-authenticated: sending our bearer
                    # token here makes Graph reject the chunk.
                    response = self._request(
                        "PUT",
                        upload_url,
                        authed=False,
                        data=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                            "Content-Range": f"bytes {start}-{end}/{size}",
                        },
                    )
                except GraphError:
                    attempts += 1
                    if attempts > self.max_retries:
                        raise
                    start = self._resume_offset(upload_url, fallback=start)
                    continue

                if response.status_code in (200, 201):
                    log.debug("%s uploaded in full", remote_path)
                    return self._check(response, f"upload {remote_path}")

                if response.status_code == 202:
                    data = self._check(response, f"chunk {start}-{end}")
                    start = _next_expected(data, default=end + 1)
                    attempts = 0
                    continue

                if response.status_code == 404:
                    # Session expired (they last ~15 minutes past the last chunk).
                    attempts += 1
                    if attempts > self.max_retries:
                        raise GraphError(
                            f"upload session for {remote_path} kept expiring", retryable=True
                        )
                    log.warning("upload session expired for %s — restarting", remote_path)
                    upload_url = self._create_session(remote_path)
                    start = 0
                    continue

                if response.status_code == 409:
                    raise GraphError(
                        f"{remote_path} already exists and conflict_behavior is "
                        f"{self.cfg.conflict_behavior!r}",
                        status=409,
                    )

                raise GraphError(
                    f"chunk {start}-{end} of {remote_path}: HTTP {response.status_code} "
                    f"{response.text[:200]}",
                    status=response.status_code,
                    retryable=response.status_code >= 500,
                )

        # Every byte was accepted but Graph never returned the finished item.
        item = self.get_item(remote_path)
        if item is None:
            raise GraphError(f"{remote_path} vanished after upload", retryable=True)
        return item

    def cancel_session(self, upload_url: str) -> None:
        try:
            self._request("DELETE", upload_url, authed=False)
        except GraphError as exc:
            log.debug("could not cancel upload session: %s", exc)

    def _resume_offset(self, upload_url: str, *, fallback: int) -> int:
        """Ask the service how much of the file it already holds."""
        try:
            response = self._request("GET", upload_url, authed=False)
        except GraphError:
            return fallback
        if response.status_code >= 400:
            return fallback
        try:
            data = dict(response.json())
        except ValueError:
            return fallback
        offset = _next_expected(data, default=fallback)
        log.info("resuming upload at byte %d", offset)
        return offset


def _next_expected(data: dict[str, Any], *, default: int) -> int:
    """Parse Graph's nextExpectedRanges (e.g. ["12345-"]) into a start offset."""
    ranges = data.get("nextExpectedRanges") or []
    for entry in ranges:
        start = str(entry).split("-", 1)[0].strip()
        if start.isdigit():
            return int(start)
    return default


def iter_chunks(path: Path, chunk_size: int) -> Iterator[bytes]:
    """Yield a file in fixed-size chunks (used by tests and ad-hoc scripts)."""
    with path.open("rb") as fh:
        while chunk := fh.read(chunk_size):
            yield chunk
