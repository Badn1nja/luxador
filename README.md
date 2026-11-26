# El Luxador & Luxie

A systemic, YAML-driven Home Assistant lighting controller + live HTTP sandbox & telemetry engine:

- **El Luxador (Core Engine):** Compiles compact `ROLE :: TIME :: BRIGHTNESS/LUX :: SCHEMA` YAML maps into a deterministic layer stack with closed-loop `target_lux`, event time-warping (e.g. TV on early pulls wind-down forward), and decaying "punching-bag" manual overrides.
- **Luxie (Sidekick Telemetry & Learning):** SQLite 3-tier data pipeline (raw -> clean hourly -> yearly seasonal baselines), causal audit logs (*"Why am I at 70%?"*), lux sensor drift alerts, empirical bulb lux-gain calibration, and human-confirmed state tracking.

## Quick Start (Local Windows / Dev Mode)

```powershell
cd C:\Users\KWN\Projects\luxador
.\.venv\Scripts\python.exe -m luxador.main
```

Open **http://127.0.0.1:8099** to explore the Live Debugger, 24h Curve & Event Simulator, YAML Map Studio, and Luxie Data Lab.

### Connecting to Live Home Assistant
Edit `config/settings.yaml` or set environment variables:
- `LUXADOR_HA_URL="https://ha.example.com"` (or `http://homeassistant.local:8123`)
- `LUXADOR_HA_TOKEN="<long-lived-access-token>"`
- `LUXADOR_DRY_RUN="true"` (logs intended bulb changes without touching physical lights until you're ready!)

## Build Phases

| Phase | What | Status |
|---|---|---|
| **1 · Data** | Record the truth: every signal change (`signal_events`), every settled presence pattern and how long it lasted (`episodes`), what each light *actually* was (`light_samples`), and who else touched a light (`foreign_writes`). Live HA only; the sandbox never writes these. | Built, starts when HA is connected |
| **2 · Learn presence** | Luxie lists the recurring patterns (e.g. `desk+desk.laptop`, weekdays 13:00–18:00) and you name each one once. Named patterns become states: `state(working)` in rules, `when: "state == working"` in overlays. A new combination that is close to a named one (Jaccard ≥ 0.5) inherits its name. | Built |
| **3 · Learn lighting** | Weekly rolling median of actual levels per state/role/hour, so a room can start from an empty YAML. | Read-only preview (`/api/learn/{room}/weekly`) |

## ROOM > PRESENCE > ZONE

```yaml
presence:
  sensors: [binary_sensor.room_radar]          # someone is in the room
  clear_delay_seconds: 180                      # holds room AND zones through sensor dropouts
  zones:
    desk:
      presence: [binary_sensor.radar_zone_1]   # someone is at the desk
      signals:                                  # what they're doing
        laptop: { source: opnsense, host: "aa:bb:cc:dd:ee:ff", active_kbps: 40 }
    sofa:
      presence: [binary_sensor.radar_zone_2]
      signals:
        tv: { entity: media_player.tv, states: [playing, paused] }
```

States are never configured; they are distilled from what actually happens. A pattern must hold for
`presence_settle_seconds` (default 45s) to count, and blips under 2 minutes are left out of the pattern list.

**Soft, not snappy:** overlays have `blend_minutes` (default 4). Brightness, colour and time-warp all ease in
and out by weight, so the TV turning on is a gentle lean, not a scene change.

**One controller per light:** Luxador only sends a command when its target changes and never re-asserts.
Changes from anything else go to *Contested Lights*. Changes from a person (HA user or a physical switch)
become punches. Changes from an automation are flagged as **fighting** and never learned from, so take that
light out of the other controller (Magic Areas, Adaptive Lighting, etc.).

## Running Tests

```powershell
.\.venv\Scripts\pytest.exe -v
```
