"""Backend adapter for the direct Microsoft Graph client.

Kept behind the same interface as the rclone backend so the two are
interchangeable: switch `cloud.provider` and nothing else in the node changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ..config import Config
from .auth import GraphAuth
from .onedrive import OneDriveClient


class GraphBackend:
    name = "graph"

    def __init__(self, cfg: Config) -> None:
        onedrive = cfg.cloud.onedrive
        self.auth = GraphAuth(onedrive, cfg.resolve(onedrive.token_cache))
        self.client = OneDriveClient(onedrive, self.auth, max_retries=cfg.upload.max_retries)
        self.chunk_size = cfg.upload.chunk_size_mib * 1024 * 1024

    def close(self) -> None:
        self.client.close()

    def check(self) -> str:
        drive = self.client.whoami()
        owner = drive.get("owner", {}).get("user", {}).get("displayName", "?")
        free_gb = drive.get("quota", {}).get("remaining", 0) / 1e9
        return f"{drive.get('name', 'drive')} (owner {owner}, {free_gb:.1f} GB free)"

    def upload(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        return self.client.upload(local_path, remote_path, chunk_size=self.chunk_size)

    def download(self, remote_path: str, local_path: Path) -> bool:
        response = self.client._request("GET", self.client.item_url(remote_path, "/content"))
        if response.status_code == 404:
            return False
        self.client._check(response, f"download {remote_path}")
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(response.content)
        return True

    def remote_size(self, remote_path: str) -> int | None:
        item = self.client.get_item(remote_path)
        size = (item or {}).get("size")
        return int(size) if size is not None else None
