# 🏮 Lantern

[![tests](https://github.com/Badn1nja/luxador/actions/workflows/tests.yml/badge.svg)](https://github.com/Badn1nja/luxador/actions/workflows/tests.yml)

**Lights that do the obvious thing on their own** for Home Assistant: on when someone's in a room and it's dark
enough to need them, as bright as the darkness calls for, warm in the evening, off when you leave, and only one
thing controlling each light. Learned from your house, never a black box.

Three parts, one loop:

| | Part | Does |
|---|---|---|
| 📊 | **Echo** · the data | Remembers what the house did: Home Assistant history, live presence and zones, lux and media devices, on a 5-minute grid. |
| 🐱 | **Luxie** · the learner | Finds what goes together and when ("TV on → sofa, weekdays 20:00–22:00"), and turns it into suggestions. Never changes anything silently. |
| 🔥 | **Flint** · the engine | presence × darkness → the general curve → each light's settings → Home Assistant. Explains every level it sets. |

## Install as a Home Assistant app

1. **Settings › Apps › App store › ⋮ › Repositories**, add `https://github.com/Badn1nja/luxador`.
2. Install **Lantern**, start it, and open **Lantern** from the sidebar.
3. It starts in **dry run**: Flint computes and logs, but never touches a bulb until you turn that off.
   Rooms live in `/addon_configs/<id>_lantern/rooms/` (see the app's Documentation tab).

## How it's built

* **[docs/VISION.md](docs/VISION.md)**: the vision, the phases, and an honest inventory of what's real.
* **[docs/STANDARD.md](docs/STANDARD.md)**: HOUSE › ROOM › ZONE › LIGHT, every setting with its default and range,
  who wins when settings disagree, and the exact pipeline from sensors to a light level. If the code and the
  standard disagree, it's a bug.
* **[docs/phase1-data.md](docs/phase1-data.md)**: what Echo records, how long it keeps it, and what Luxie computes.

```
repository.yaml          # makes this repo a Home Assistant app repository
lantern/                 # the app (HA builds from this folder)
  config.yaml  Dockerfile  DOCS.md  requirements.txt
  src/lantern/           # Python package: echo.py (store), together.py + states.py (Luxie), standard.py + resolver.py (Flint)
  src/web/               # the UI
  src/config/            # example config, copied to the app's config folder on first start
tests/                   # pytest suite (run from the repo root)
```

## Local development

```powershell
cd C:\Users\KWN\Projects\luxador
$env:PYTHONPATH = "lantern/src"
.\.venv\Scripts\python.exe -m lantern.main
```

Open **http://127.0.0.1:8099**. Without Home Assistant every screen says "not connected" rather than showing
made-up numbers. To connect, edit `lantern/src/config/settings.yaml` or set:

- `LANTERN_HA_URL="https://ha.example.com"` and `LANTERN_HA_TOKEN="<long-lived token of a dedicated HA user>"`
- `LANTERN_DRY_RUN="true"` (the default)

(The old `LUXADOR_*` names still work.)

## Tests

```powershell
.\.venv\Scripts\pip.exe install -r requirements-dev.txt
.\.venv\Scripts\pytest.exe -v
```

GitHub Actions runs the same suite on Python 3.11–3.13 for every push and pull request.
