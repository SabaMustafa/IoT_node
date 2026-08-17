"""Typed configuration, loaded from config.yaml with a local override layer."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class DeviceConfig(BaseModel):
    id: str = "iot-node"
    site: str = ""
    timezone: str = "UTC"


class PathsConfig(BaseModel):
    spool: Path = Path("var/spool")
    state: Path = Path("var/state/iot-node.db")
    logs: Path = Path("logs")


class BME280Config(BaseModel):
    enabled: bool = True
    address: int = 0x76


class SGP40Config(BaseModel):
    enabled: bool = True
    address: int = 0x59
    default_humidity_pct: float = 50.0
    default_temperature_c: float = 25.0


class SensorsConfig(BaseModel):
    enabled: bool = True
    i2c_bus: int = 1
    sample_interval_s: float = 60.0
    rollover: Literal["hourly", "daily"] = "hourly"
    bme280: BME280Config = Field(default_factory=BME280Config)
    sgp40: SGP40Config = Field(default_factory=SGP40Config)


class AudioConfig(BaseModel):
    enabled: bool = True
    source_dirs: list[Path] = Field(default_factory=list)
    auto_detect: bool = True
    patterns: list[str] = Field(default_factory=lambda: ["*.WAV", "*.wav"])
    min_age_s: float = 30.0
    min_size_bytes: int = 1024
    action: Literal["copy", "move"] = "copy"


class OneDriveConfig(BaseModel):
    tenant_id: str = ""
    client_id: str = ""
    auth_mode: Literal["device_code", "client_credentials"] = "device_code"
    client_secret: str = ""
    token_cache: Path = Path("secrets/token_cache.json")
    target: Literal["me", "user", "drive"] = "me"
    user_principal_name: str = ""
    drive_id: str = ""
    remote_root: str = "IoT/{device_id}"
    conflict_behavior: Literal["replace", "rename", "fail"] = "replace"


class RcloneConfig(BaseModel):
    """Upload through a configured rclone remote — no Azure app registration."""

    binary: str = "rclone"
    remote: str = "onedrive"  # the remote name from `rclone config`
    config_path: Path | str = ""  # blank = rclone's own default location
    remote_root: str = "IoT/{device_id}"
    retries: int = 3
    timeout_s: float = 1800.0  # a 30 MiB WAV on a slow uplink
    bwlimit: str = ""  # e.g. "2M" to leave headroom for other traffic
    extra_args: list[str] = Field(default_factory=list)


class CloudConfig(BaseModel):
    provider: Literal["rclone", "onedrive"] = "rclone"
    rclone: RcloneConfig = Field(default_factory=RcloneConfig)
    onedrive: OneDriveConfig = Field(default_factory=OneDriveConfig)

    @property
    def remote_root(self) -> str:
        """The remote path template belonging to whichever provider is active."""
        return self.rclone.remote_root if self.provider == "rclone" else self.onedrive.remote_root


class UploadConfig(BaseModel):
    enabled: bool = True
    interval_s: float = 300.0
    chunk_size_mib: int = 10
    max_retries: int = 5
    verify_size: bool = True
    delete_after_upload: bool = True
    retention_days: float = 3.0
    min_free_disk_pct: float = 10.0

    @field_validator("chunk_size_mib")
    @classmethod
    def _graph_chunk_multiple(cls, v: int) -> int:
        # Graph requires every chunk but the last to be a multiple of 320 KiB.
        if (v * 1024 * 1024) % (320 * 1024) != 0:
            raise ValueError(f"chunk_size_mib={v} is not a multiple of 320 KiB")
        return v


class LoggingConfig(BaseModel):
    level: str = "INFO"
    max_bytes: int = 5 * 1024 * 1024
    backups: int = 5


class Config(BaseModel):
    device: DeviceConfig = Field(default_factory=DeviceConfig)
    paths: PathsConfig = Field(default_factory=PathsConfig)
    sensors: SensorsConfig = Field(default_factory=SensorsConfig)
    audio: AudioConfig = Field(default_factory=AudioConfig)
    cloud: CloudConfig = Field(default_factory=CloudConfig)
    upload: UploadConfig = Field(default_factory=UploadConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)

    root: Path = PROJECT_ROOT

    def resolve(self, p: Path | str) -> Path:
        """Absolutise a configured path against the project root."""
        p = Path(p)
        return p if p.is_absolute() else (self.root / p)

    @property
    def spool_dir(self) -> Path:
        return self.resolve(self.paths.spool)

    @property
    def state_db(self) -> Path:
        return self.resolve(self.paths.state)

    @property
    def log_dir(self) -> Path:
        return self.resolve(self.paths.logs)

    def ensure_dirs(self) -> None:
        for d in (self.spool_dir, self.state_db.parent, self.log_dir):
            d.mkdir(parents=True, exist_ok=True)
        for kind in ("audio", "sensors"):
            (self.spool_dir / kind).mkdir(parents=True, exist_ok=True)


def _load_dotenv(root: Path) -> None:
    """Populate os.environ from a .env file without clobbering real env vars."""
    env_file = root / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def _expand(node: Any) -> Any:
    """Recursively expand ${ENV_VAR} references; unset vars become empty strings."""
    if isinstance(node, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), node)
    if isinstance(node, dict):
        return {k: _expand(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_expand(v) for v in node]
    return node


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_config(path: Path | str | None = None, *, root: Path | None = None) -> Config:
    """Load config.yaml, overlay config.local.yaml, expand env refs, validate."""
    root = Path(root) if root else PROJECT_ROOT
    _load_dotenv(root)

    main = Path(path) if path else root / "config.yaml"
    data: dict[str, Any] = {}
    if main.is_file():
        data = yaml.safe_load(main.read_text()) or {}

    local = main.with_name("config.local.yaml")
    if local.is_file():
        data = _deep_merge(data, yaml.safe_load(local.read_text()) or {})

    data = _expand(data)
    data["root"] = root
    return Config.model_validate(data)
