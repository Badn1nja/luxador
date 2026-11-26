# The Lantern Standard (v1)

One page that says what every moving part is, who owns it, what it defaults to, and in which order things
happen. The code is built and tested against this document; if they disagree, that's a bug.

## 1. Hierarchy: HOUSE › ROOM › ZONE › LIGHT

| Level | Is | Owns |
|---|---|---|
| **House** | the home | the **general curve** (how much light a level of darkness needs), night cap, colour schema, seasonal lift |
| **Room** | a space with presence sensors and (ideally) a lux sensor | its zones, its lights' settings, room presence |
| **Zone** | a part of a room (desk, sofa…), with its own presence sensors | the lights it **owns**; whether it **joins** the room or acts on its **own** |
| **Light** | a *light group*: one or more HA light entities that always move together | its own settings (offset, scale, soft start…) |

Rules:
* Every light is owned by **exactly one** zone. Lights not assigned to a zone belong to the room's implicit
  zone `room`, which always **joins**.
* Zone `mode`:
  * `join`: the zone's lights follow **room** presence (they're on whenever someone is in the room).
  * `own`: the zone's lights follow **that zone's** presence. When the room is occupied but the zone isn't,
    they sit at the zone's `idle_pct` (0 = off).

## 2. Settings

Every setting has a default, a range, and one place it lives.

### House · general curve (`curve`)

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `dark_lux` | 15 | 0.1–2000 | indoor *daylight* at or below this counts as fully dark |
| `bright_lux` | 250 | > dark_lux | at or above this the room needs no light |
| `min_pct` | 10 | 0–100 | brightness the moment the room starts needing light |
| `max_pct` | 100 | min–100 | brightness when fully dark |
| `shape` | 0.8 | 0.3–3 | curve bend: < 1 rises early (gentle), > 1 rises late |
| `night_from` / `night_to` | 23:00 / 06:00 | HH:MM | the night window |
| `night_max_pct` | 35 | 0–100 | cap on brightness during the night window |
| `colour_schema` | `sun` | a schema name | colour by time of day for `natural` lights |
| `darkness_source` | `ambient` | `ambient` · `sun` | `ambient` = room lux minus what our own lights add; `sun` = sun elevation only |

**Darkness** `d` (0 = bright, 1 = dark) from ambient lux, on a log scale:
`d = clamp((log(bright_lux) − log(lux)) / (log(bright_lux) − log(dark_lux)), 0, 1)`.
With no usable lux sensor, the sun decides: elevation ≥ 10° → 0, ≤ −6° → 1, linear between.
**Curve:** `d = 0` → off; otherwise `min_pct + (max_pct − min_pct) × d^shape`.

### Zone (`zones.<name>`)

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `mode` | `join` | `join` · `own` | see §1 |
| `idle_pct` | 0 | 0–100 | `own` zones: level while the room is occupied but the zone isn't |
| `lights` | [] | light names | the lights this zone owns |

### Light (`lights.<name>`)

| Setting | Default | Range | Meaning |
|---|---|---|---|
| `enabled` | true | bool | false = Flint never touches this light |
| `curve` | `natural` | `natural` · `brightness_only` · `fixed` · `switch` | natural = curve + colour; brightness_only = leave colour alone; fixed = `fixed_pct` whenever needed; switch = on/off only |
| `fixed_pct` | 80 | 1–100 | level for `fixed` |
| `scale` | 1.0 | 0.25–2.0 | multiplies the curve (this lamp brighter/dimmer than the room) |
| `offset_pct` | 0 | −50–50 | added after scaling |
| `min_pct` / `max_pct` | 0 / 100 | 0–100 | final clamp while on |
| `kelvin_offset` | 0 | −1000–1000 | warmer (−) / cooler (+) than the schema |
| `soft_start` | `first_of_day` | `never` · `first_of_day` · `always` | when to fade in from off |
| `soft_start_speed` | `normal` | `fast` 2 s · `normal` 20 s · `slow` 2 min · `sunrise` 15 min | how long that fade takes |

## 3. Who wins (precedence, lowest first)

1. **Built-in defaults** (this document)
2. **Config files**: `settings.yaml`, `rooms/*.yaml`
3. **UI settings** (stored in Echo, Lantern's database)
4. **Locks**: a locked setting can't be changed by 3, by a reset, or by any future auto-tuning, until unlocked.

At run time, on top of the settings: **layers** (TV, party, sleep, wake, seasonal lift; each eases in and
out), then **manual nudges** (decay away over the half-life), then **pins** from the Luxie switchboard
(pinned / off / ignore for a light in a discovered state).

## 4. The pipeline (every tick, every light)

Each stage appears in the light's **trace** ("why is this light at 42 %?"), in this order:

| # | Stage | Does |
|---|---|---|
| 1 | `presence` | room occupied? zone occupied (for `own` zones)? → needed / idle / off |
| 2 | `darkness` | `d` from ambient lux (or the sun), smoothed so a passing cloud doesn't flick lights |
| 3 | `curve` | general curve → base level; night cap in the night window |
| 4 | `light` | the light's curve mode, × scale, + offset |
| 5 | `layers` | TV / party / sleep / wake blended by weight; seasonal lift |
| 6 | `manual` | the decaying punch from a manual change |
| 7 | `clamp` | the light's min/max |
| 8 | `pins` | switchboard pin / off / ignore for the current state |
| 9 | `output` | fade time (soft start/stop); only sent if it changed |

## 5. Invariants (always true; tested)

* Nobody present (after the clear delay) → the light is off, unless pinned.
* A light with `enabled: false`, or `ignore` pinned, is never sent a command.
* Output never exceeds the light's `max_pct`, or the night cap during the night window (pins excepted).
* Dry run sends nothing to Home Assistant.
* An unchanged target is never re-sent.

## 6. Serviceability

* The whole house model is **versioned** (`version: 1`) and validated as one piece; every error is reported
  at once, with its path (e.g. `rooms.lounge.lights.overhead.offset_pct: must be between -50 and 50`).
* **Export / import** the whole model as one JSON file (backup, move to a new install, diff a change).
* Every light has a **trace** (§4). Nothing is a black box.
* **Health** lists anything that weakens decisions: sensors unavailable, lux fallback, contested lights.
