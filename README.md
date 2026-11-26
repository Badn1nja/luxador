# El Luxador & Luxie

[![tests](https://github.com/Badn1nja/luxador/actions/workflows/tests.yml/badge.svg)](https://github.com/Badn1nja/luxador/actions/workflows/tests.yml)

A systemic, YAML-driven Home Assistant lighting controller + live HTTP sandbox & telemetry engine:

- **El Luxador (Core Engine):** Compiles compact `ROLE :: TIME :: BRIGHTNESS/LUX :: SCHEMA` YAML maps into a deterministic layer stack with closed-loop `target_lux`, event time-warping (e.g. TV on early pulls wind-down forward), and decaying "punching-bag" manual overrides.
- **Luxie (Sidekick Telemetry & Learning):** SQLite data pipeline (raw -> clean hourly -> yearly seasonal baselines), presence pattern learning, device-to-zone learning, causal audit logs (*"Why am I at 70%?"*) and lux sensor drift alerts.

## Install as a Home Assistant app

1. **Settings > Apps > App store > ⋮ > Repositories**, add `https://github.com/Badn1nja/luxador`.
2. Install **El Luxador & Luxie**, start it, and open **Luxador** from the sidebar.
3. It starts in dry run. Edit your rooms in `/addon_configs/<id>_luxador/rooms/` (see the app's Documentation tab).

## Repository layout

```
repository.yaml          # makes this repo a Home Assistant app repository
luxador/                 # the app (HA builds from this folder)
  config.yaml  Dockerfile  DOCS.md  requirements.txt
  src/luxador/           # Python package
  src/web/               # UI
  src/config/            # example config, copied to the app's config folder on first start
tests/                   # pytest suite (run from repo root)
```

## Quick Start (Local Windows / Dev Mode)

```powershell
cd C:\Users\KWN\Projects\luxador
$env:PYTHONPATH = "luxador/src"
.\.venv\Scripts\python.exe -m luxador.main
```

Open **http://127.0.0.1:8099** to explore the Live Debugger, 24h Curve & Event Simulator, YAML Map Studio, and Luxie Data Lab.

### Connecting to Live Home Assistant (dev mode)
Inside HA as an app this is automatic. For local dev, edit `luxador/src/config/settings.yaml` or set environment variables:
- `LUXADOR_HA_URL="https://ha.example.com"` (or `http://homeassistant.local:8123`)
- `LUXADOR_HA_TOKEN="<long-lived-access-token>"`
- `LUXADOR_DRY_RUN="true"` (logs intended bulb changes without touching physical lights until you're ready!)

## The Standard

Luxador is built against **[docs/STANDARD.md](docs/STANDARD.md)**: the HOUSE › ROOM › ZONE › LIGHT hierarchy,
every setting with its default and range, who wins when settings disagree, and the exact pipeline from sensors
to a light level. Each light carries a trace of that pipeline ("why is this light at 42 %?"), the whole model is
validated as one piece, and it exports/imports as one JSON file. If the code and the standard disagree, it's a bug.

## Build Phases

| Phase | What | Status |
|---|---|---|
| **1 · Data** | Record the truth: every signal change (`signal_events`), every settled presence pattern and how long it lasted (`episodes`), what each light *actually* was (`light_samples`), and who else touched a light (`foreign_writes`). Live HA only; the sandbox never writes these. | Built, starts when HA is connected |
| **2 · Learn presence** | Luxie lists the recurring patterns (e.g. `desk+desk.laptop`, weekdays 13:00–18:00) and you name each one once. Named patterns become states: `state(working)` in rules, `when: "state == working"` in overlays. A new combination that is close to a named one (Jaccard ≥ 0.5) inherits its name. | Built |
| **3 · Learn lighting** | Weekly rolling median of actual levels per state/role/hour, so a room can start from an empty YAML. | Read-only preview (`/api/learn/{room}/weekly`) |
| **Data Lab** | Traceback import from HA's recorder + long-term stats, a 5-minute grid, intent model with priority layering, up to 28 **discovered** states with per-light behaviour, and a Grafana-lite dashboard (today/week/month). See [docs/phase1-data.md](docs/phase1-data.md). | Built (read-only) |
| **4 · Custom event log** | Your own "trigger" / "suspected" markers as extra context. | Planned |

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
.\.venv\Scripts\pip.exe install -r requirements-dev.txt
.\.venv\Scripts\pytest.exe -v
```

GitHub Actions runs the same suite on Python 3.11–3.13 for every push and pull request
(`.github/workflows/tests.yml`).
