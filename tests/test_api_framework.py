"""API tests for the Overview -> Room [Presence | Targets | Time | Tweaks] framework."""

from pathlib import Path
from fastapi.testclient import TestClient

from luxador import main as lux_main


def test_overview_and_rule_tweaks_roundtrip(tmp_path, monkeypatch):
    # Copy config into tmp_path so tests don't mutate repo YAML files
    cfg_dst = tmp_path / "config"
    cfg_dst.mkdir()
    (cfg_dst / "rooms").mkdir()
    src_cfg = Path(__file__).resolve().parent.parent / "config"
    (cfg_dst / "settings.yaml").write_text((src_cfg / "settings.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dst / "schemas.yaml").write_text((src_cfg / "schemas.yaml").read_text(encoding="utf-8"), encoding="utf-8")
    (cfg_dst / "rooms" / "lounge.yaml").write_text(
        (src_cfg / "rooms" / "lounge.yaml").read_text(encoding="utf-8"), encoding="utf-8"
    )

    monkeypatch.setattr(lux_main, "CONFIG_DIR", cfg_dst)
    monkeypatch.setenv("LUXADOR_DB_PATH", str(tmp_path / "luxie.db"))

    with TestClient(lux_main.app) as client:
        ov = client.get("/api/overview")
        assert ov.status_code == 200
        ov_data = ov.json()
        assert ov_data["rooms"][0]["room"] == "lounge"
        assert ov_data["rooms"][0]["ambient_lux"] == 439.0

        st = client.get("/api/rooms/lounge/state").json()
        assert st["room_config"]["tweaks"]["presence_recent_minutes"] == 20
        assert "main" in st["room_config"]["traits"]["overhead"]

        # Add and remove a curve rule
        add_res = client.post(
            "/api/rooms/lounge/rules/add",
            json={"rule": "shoelight :: 18:00-22:00 :: 45% :: no_blue"},
        )
        assert add_res.status_code == 200
        rules_after_add = add_res.json()["rules"]
        assert "shoelight :: 18:00-22:00 :: 45% :: no_blue" in rules_after_add

        rm_res = client.post(
            "/api/rooms/lounge/rules/remove",
            json={"index": len(rules_after_add) - 1},
        )
        assert rm_res.status_code == 200
        assert len(rm_res.json()["rules"]) == len(rules_after_add) - 1

        # Update guardrail tweaks
        tw_res = client.post(
            "/api/rooms/lounge/tweaks",
            json={"punch_half_life_minutes": 60, "tweaks": {"presence_recent_minutes": 30}},
        )
        assert tw_res.status_code == 200
        assert tw_res.json()["tweaks"]["presence_recent_minutes"] == 30
