"""The interface the uploader talks to, and the factory that picks one.

Two backends reach the same OneDrive by different routes:

* ``rclone`` — shells out to a configured rclone remote. The credential is
  rclone's own, created by signing in once with an ordinary AUT account, so no
  Azure app registration is involved.
* ``onedrive`` — talks to the Microsoft Graph API directly through MSAL. Fewer
  moving parts at runtime, but it needs an app registration in the tenant.

Both raise :class:`UploadError`, so the uploader loop and the CLI never need to
know which one is in play.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..config import Config


class UploadError(RuntimeError):
    """An upload could not be completed.

    ``retryable`` distinguishes a bad link or a throttle — worth another pass —
    from a misconfiguration that will fail identically every time.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


@runtime_checkable
class Backend(Protocol):
    name: str

    def check(self) -> str:
        """Verify credentials and reachability; return a one-line description."""

    def upload(self, local_path: Path, remote_path: str) -> dict[str, Any]:
        """Upload a file, returning whatever metadata the backend knows."""

    def download(self, remote_path: str, local_path: Path) -> bool:
        """Fetch a remote file. Returns False if it is simply not there."""

    def remote_size(self, remote_path: str) -> int | None:
        """Size of the uploaded file, or None if the backend cannot say."""

    def close(self) -> None: ...


def make_backend(cfg: Config) -> Backend:
    """Build the backend named by ``cloud.provider``.

    Imported lazily so that choosing rclone does not require msal to be
    installed, and vice versa.
    """
    if cfg.cloud.provider == "rclone":
        from .rclone import RcloneBackend

        return RcloneBackend(cfg.cloud.rclone, root=cfg.root)

    from .graph_backend import GraphBackend

    return GraphBackend(cfg)
