"""Logging: rotating file for history, stderr for journald/console."""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from .config import Config

_FORMAT = "%(asctime)s %(levelname)-7s %(name)-22s %(message)s"


def setup_logging(cfg: Config) -> None:
    level = getattr(logging, cfg.logging.level.upper(), logging.INFO)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # Under systemd, journald already timestamps every line.
    fmt = "%(levelname)-7s %(name)-22s %(message)s" if _under_systemd() else _FORMAT
    stream = logging.StreamHandler(sys.stderr)
    stream.setFormatter(logging.Formatter(fmt))
    root.addHandler(stream)

    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    rotating = RotatingFileHandler(
        cfg.log_dir / "iot-node.log",
        maxBytes=cfg.logging.max_bytes,
        backupCount=cfg.logging.backups,
    )
    rotating.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(rotating)

    # msal logs every token cache hit at INFO; urllib3 logs each connection.
    logging.getLogger("msal").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def _under_systemd() -> bool:
    return bool(os.environ.get("JOURNAL_STREAM") or os.environ.get("INVOCATION_ID"))
