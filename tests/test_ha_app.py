"""Running as a Home Assistant app: options.json wins, config is seeded once into /config, DB lives in /data."""

import json
from pathlib import Path

import yaml

from lantern import main as lux_main

REPO = Path(__file__).resolve().parent.parent
APP_DIR = REPO / "lantern"


def test_repository_layout_is_a_valid_ha_app_repo():
    repo = yaml.safe_load((REPO / "repository.yaml").read_text(encoding="utf-8"))
    assert repo["name"] and repo["url"].startswith("https://github.com/")
    cfg = yaml.safe_load((APP_DIR / "config.yaml").read_text(encoding="utf-8"))
    for key in ("name", "version", "slug", "description", "arch"):
        assert cfg.get(key), key
    assert (APP_DIR / "Dockerfile").exists()
    # every option has a schema entry, and the Dockerfile only copies paths inside the app folder
    assert set(cfg["options"]) <= set(cfg["schema"])
    for line in (APP_DIR / "Dockerfile").read_text(encoding="utf-8").splitlines():
        if line.startswith("COPY "):
            src = line.split()[1]
            assert (APP_DIR / src).exists(), f"Dockerfile COPY source missing: {src}"


def test_app_mode_reads_options_and_writes_no_config_files(tmp_path, monkeypatch):
    opts = tmp_path / "data" / "options.json"
    opts.parent.mkdir()
    opts.write_text(json.dumps({
        "dry_run": False, "tick_seconds": 9,
        "opnsense_url": "https://fw.example", "opnsense_api_key": "k", "opnsense_api_secret": "s",
        "opnsense_verify_ssl": False,
    }), encoding="utf-8")
    cfg_dir = tmp_path / "config"
    monkeypatch.setattr(lux_main, "IS_HA_APP", True)
    monkeypatch.setattr(lux_main, "HA_OPTIONS_PATH", opts)
    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg_dir)
    assert not hasattr(lux_main, "_seed_config_dir")  # the database is the source of truth; no files are seeded

    monkeypatch.delenv("LANTERN_DB_PATH", raising=False)
    monkeypatch.delenv("LANTERN_DRY_RUN", raising=False)
    monkeypatch.delenv("LANTERN_TICK_SECONDS", raising=False)
    s = lux_main._load_settings()
    assert s["dry_run"] is False and s["tick_seconds"] == 9
    assert s["db_path"] == "/data/lantern.db"
    assert s["opnsense"]["url"] == "https://fw.example" and s["opnsense"]["verify_ssl"] is False
