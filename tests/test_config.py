from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from iot_node.config import Config, load_config


def write(path, text: str) -> None:
    path.write_text(text.strip() + "\n")


def test_defaults_when_no_file(tmp_path):
    cfg = load_config(tmp_path / "missing.yaml", root=tmp_path)
    assert cfg.device.id == "iot-node"
    assert cfg.upload.chunk_size_mib == 10


def test_local_overlay_is_deep_merged(tmp_path):
    write(tmp_path / "config.yaml", """
device:
  id: base-node
  site: field
sensors:
  i2c_bus: 1
  bme280:
    address: 0x76
""")
    write(tmp_path / "config.local.yaml", """
device:
  id: override-node
sensors:
  bme280:
    address: 0x77
""")
    cfg = load_config(tmp_path / "config.yaml", root=tmp_path)

    assert cfg.device.id == "override-node"
    assert cfg.device.site == "field"  # untouched keys survive the merge
    assert cfg.sensors.bme280.address == 0x77
    assert cfg.sensors.i2c_bus == 1


def test_env_refs_expand(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_CLIENT_ID", "client-abc")
    monkeypatch.delenv("GRAPH_TENANT_ID", raising=False)
    write(tmp_path / "config.yaml", """
cloud:
  onedrive:
    client_id: ${GRAPH_CLIENT_ID}
    tenant_id: ${GRAPH_TENANT_ID}
""")
    cfg = load_config(tmp_path / "config.yaml", root=tmp_path)

    assert cfg.cloud.onedrive.client_id == "client-abc"
    assert cfg.cloud.onedrive.tenant_id == ""  # unset vars collapse, not crash


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("GRAPH_CLIENT_ID", "from-environment")
    write(tmp_path / ".env", "GRAPH_CLIENT_ID=from-dotenv\nGRAPH_TENANT_ID=tenant-xyz")
    write(tmp_path / "config.yaml", """
cloud:
  onedrive:
    client_id: ${GRAPH_CLIENT_ID}
    tenant_id: ${GRAPH_TENANT_ID}
""")
    cfg = load_config(tmp_path / "config.yaml", root=tmp_path)

    assert cfg.cloud.onedrive.client_id == "from-environment"
    assert cfg.cloud.onedrive.tenant_id == "tenant-xyz"


def test_chunk_size_must_suit_graph():
    with pytest.raises(ValidationError, match="320 KiB"):
        Config.model_validate({"upload": {"chunk_size_mib": 7}})

    assert Config.model_validate({"upload": {"chunk_size_mib": 5}}).upload.chunk_size_mib == 5


def test_relative_paths_resolve_against_root(tmp_path):
    cfg = Config(root=tmp_path)
    assert cfg.spool_dir == tmp_path / "var/spool"
    assert cfg.resolve(Path("/absolute/path")) == Path("/absolute/path")


def test_ensure_dirs_creates_spool_layout(tmp_path):
    cfg = Config(root=tmp_path)
    cfg.ensure_dirs()
    assert (cfg.spool_dir / "audio").is_dir()
    assert (cfg.spool_dir / "sensors").is_dir()
    assert cfg.state_db.parent.is_dir()
